# -*- coding: utf-8 -*-
"""厂商预设和独立的本机加密配置，加载配置不会请求模型或读取聊天。"""

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from bootstrap import PRIVATE
from local_storage import _protect, atomic_write

SETTINGS_FILE = PRIVATE / "model-settings.dat"
PROTOCOLS = {"chat": "OpenAI 兼容对话", "responses": "OpenAI Responses", "anthropic": "Anthropic Messages",
             "gemini": "Gemini 原生", "ollama": "Ollama 原生"}
# 地址来自厂商官方文档，仅预填通用按量接口；地区、套餐和专属网关可由用户修改。
PROVIDERS = [
    ("openai", "OpenAI", "https://api.openai.com/v1", "responses"),
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "chat"),
    ("qwen", "阿里云百炼（中国内地）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat"),
    ("qwen-intl", "阿里云百炼（新加坡）", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "chat"),
    ("kimi", "Kimi / Moonshot", "https://api.moonshot.cn/v1", "chat"),
    ("glm", "智谱 GLM（通用接口）", "https://open.bigmodel.cn/api/paas/v4", "chat"),
    ("siliconflow", "硅基流动（中国站）", "https://api.siliconflow.cn/v1", "chat"),
    ("ark", "火山方舟（通用接口）", "https://ark.cn-beijing.volces.com/api/v3", "chat"),
    ("qianfan", "百度千帆", "https://qianfan.baidubce.com/v2", "chat"),
    ("hunyuan", "腾讯混元（原兼容接口）", "https://api.hunyuan.cloud.tencent.com/v1", "chat"),
    ("anthropic", "Anthropic / Claude", "https://api.anthropic.com/v1", "anthropic"),
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini"),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "chat"),
    ("ollama", "本地 Ollama", "http://127.0.0.1:11434", "ollama"),
    ("lmstudio", "本地 LM Studio", "http://127.0.0.1:1234/v1", "chat"),
    ("custom", "自定义接口 / 第三方网关", "", "chat"),
]

MODEL_ERRORS = {
    "CONFIG": "模型配置无效，请核对厂商、URL、协议和模型名称。",
    "NOT_CONFIGURED": "尚未配置模型，请先进入模型设置。",
    "SECRET": "模型配置无法解密，原文件保留，请在当前 Windows 用户下重新配置。",
    "AUTH": "密钥无效或无权限，请核对厂商、地区、套餐和账号权限。",
    "NOT_FOUND": "模型或接口不存在；列表接口也可能未开放，可手动填写模型后测试。",
    "RATE_LIMIT": "接口限流或额度不足，请核对账号额度后手动重试。",
    "TIMEOUT": "接口超时，本次没有自动重试。",
    "NETWORK": "网络或证书连接失败，请核对地址和网络。",
    "REDIRECT": "接口返回重定向，未转发密钥；请填写最终接口地址。",
    "HTTP": "服务端未接受请求，请核对协议、模型及参数支持。",
    "RESPONSE": "接口返回的数据无法按所选协议解析，请查看诊断中的失败步骤。",
    "REQUEST_SIZE": "单次请求超过资源保护范围，请缩小单次分段；已完成的每日分析已保留。",
    "RESPONSE_SIZE": "单次响应正文或事件超过资源保护范围；已完成的每日分析已保留。",
    "NO_TEXT": "接口未返回完整文本，不能确认该模型可用于总结。",
    "OUTPUT_LIMIT": "接口报告生成达到输出或上下文上限，正文未完整；请核对接口默认值或显式预算，不会自动重复调用。",
    "THINKING_ONLY": "接口只返回了思考内容，没有返回总结正文；本次不作为完整总结保存。",
    "INCOMPLETE": "模型响应中途结束或未正常完成；本次不作为完整总结保存。",
    "UPSTREAM_RESOURCE": "接口报告后端推理资源不足，请求被服务端打断；这不是本机大小限制，也未自动重试。",
    "BLOCKED": "模型拒绝或阻止了本次内容，未生成完整总结。",
    "MODEL": "请先选择或手动填写一个模型。",
    "SUMMARY_CONFIRM": "本会话需要较多模型请求，尚未发送内容；请在交互报告中确认，或明确追加--allow-large-summary。",
}

# 仅保留官方协议中的固定结束枚举，未知服务端字符串不能进入日志。
FINISH_REASONS = {"stop", "end_turn", "stop_sequence", "STOP", "length", "max_tokens", "max_output_tokens", "MAX_TOKENS",
                  "incomplete", "content_filter", "refusal", "SAFETY", "RECITATION", "PROHIBITED_CONTENT",
                  "insufficient_system_resource", "aborted", "tool_calls", "function_call", "missing", "other"}


class ModelError(RuntimeError):
    """只公开固定错误码，异常不包含密钥、服务端原文或聊天正文。"""

    def __init__(self, code, details=None):
        self.code = code if isinstance(code, str) and code in MODEL_ERRORS else "RESPONSE"
        self.details = {}
        for key in ("text_chars", "reasoning_chars", "requested_max_tokens", "response_bytes", "limit_bytes", "event_index"):
            if isinstance(details, dict) and type(details.get(key)) is int and 0 <= details[key] <= 10**9:
                self.details[key] = details[key]
        if isinstance(details, dict) and isinstance(details.get("finish_reason"), str) and details["finish_reason"] in FINISH_REASONS:
            self.details["finish_reason"] = details["finish_reason"]
        if isinstance(details, dict) and type(details.get("termination_received")) is bool:
            self.details["termination_received"] = details["termination_received"]
        if isinstance(details, dict) and type(details.get("provider_default_budget")) is bool:
            self.details["provider_default_budget"] = details["provider_default_budget"]
        if isinstance(details, dict) and isinstance(details.get("end_marker"), str) and details["end_marker"] in {"finish_reason", "done_marker", "protocol_end", "eof", "json"}:
            self.details["end_marker"] = details["end_marker"]
        stages = {"request_body", "response_body", "response_json", "response_shape", "stream_line", "stream_event",
                  "stream_encoding", "stream_json", "stream_shape", "stream_finish", "text_limit", "model_output"}
        if isinstance(details, dict) and isinstance(details.get("stage"), str) and details["stage"] in stages:
            self.details["stage"] = details["stage"]
        super().__init__(MODEL_ERRORS[self.code])


def normalize_url(value, protocol):
    """接受基础地址或完整调用地址，保留网关前缀，不在任意域名后猜测版本。"""
    if not isinstance(protocol, str) or protocol not in PROTOCOLS or not isinstance(value, str) or len(value) > 2048 or "\\" in value or any(character.isspace() or ord(character) < 32 for character in value):
        raise ModelError("CONFIG")
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.port == 0:
            raise ValueError()
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError()
    except ValueError:
        raise ModelError("CONFIG") from None
    path = parts.path.rstrip("/")
    if protocol == "gemini" and "/models/" in path and path.endswith(":generateContent"):
        path = path.split("/models/", 1)[0]
    suffixes = {"chat": ("/chat/completions", "/models"), "responses": ("/responses", "/models"),
                "anthropic": ("/messages", "/models"), "gemini": ("/models",), "ollama": ("/api/chat", "/api/tags", "/api")}
    for suffix in suffixes[protocol]:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path and protocol in ("anthropic", "gemini"):
        path = "/v1" if protocol == "anthropic" else "/v1beta"
    if not path and parts.hostname == "api.openai.com":
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def validate_config(value):
    """仅保存所需字段，密钥不写入参数历史，模型名不用于拼接任意请求地址。"""
    if not isinstance(value, dict) or not isinstance(value.get("provider"), str) or value["provider"] not in {item[0] for item in PROVIDERS}:
        raise ModelError("CONFIG")
    protocol = value.get("protocol")
    key, model = value.get("api_key", ""), value.get("model", "")
    if any(not isinstance(item, str) or len(item) > limit or any(ord(character) < 32 or ord(character) == 127 for character in item) for item, limit in ((key, 8192), (model, 256))):
        raise ModelError("CONFIG")
    return {"provider": value["provider"], "protocol": protocol, "url": normalize_url(value.get("url"), protocol),
            "api_key": key.strip(), "model": model.strip()}


def save_config(value, path=SETTINGS_FILE):
    """复用当前用户数据保护和原子保存，不修改采集、平台或外观配置。"""
    atomic_write(path, _protect(json.dumps(validate_config(value), ensure_ascii=False).encode("utf-8")))


def load_config(path=SETTINGS_FILE):
    """损坏配置原样保留，不静默回退或使用其他程序的密钥。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError()
        return validate_config(json.loads(_protect(path.read_bytes(), decrypt=True)))
    except (ValueError, RuntimeError, TypeError, KeyError, OSError):
        raise ModelError("SECRET") from None
