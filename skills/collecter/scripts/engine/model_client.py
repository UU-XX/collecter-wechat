# -*- coding: utf-8 -*-
"""统一的文本模型调用和模型发现，所有网络动作必须由调用方明确触发。"""

import json
import socket
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from model_settings import FINISH_REASONS, ModelError, validate_config
from model_stream import DeltaFilter, StreamResult, read_model_stream

# 只保护单次完整响应的内存，不将全年记录或流式协议开销累计为上限。
MAX_RESPONSE = 16 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    """禁止把认证头自动带到重定向地址，包括同域重定向。"""

    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


class ModelClient:
    """只处理文本，不执行模型工具、不上传附件、不自动重试或后台轮询。"""

    def __init__(self, config, timeout=30):
        self.config = validate_config(config)
        self.timeout = max(1, min(float(timeout), 600))
        handlers = [NoRedirect()]
        if urlsplit(self.config["url"]).hostname in ("localhost", "127.0.0.1", "::1"):
            handlers.append(ProxyHandler({}))
        self.opener = build_opener(*handlers)

    def request(self, path, body=None, *, stream=False, on_delta=None):
        """请求和响应均限大小，TLS按系统默认验证，错误正文一律不向外传播。"""
        key, protocol = self.config["api_key"], self.config["protocol"]
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Collecter/Local"}
        if stream:
            headers["Accept"] = "application/x-ndjson" if protocol == "ollama" else "text/event-stream"
        if key:
            if protocol == "anthropic":
                headers["x-api-key"] = key
            elif protocol == "gemini":
                headers["x-goog-api-key"] = key
            else:
                headers["Authorization"] = "Bearer " + key
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if data is not None and len(data) > MAX_REQUEST:
            raise ModelError("REQUEST_SIZE", {"stage": "request_body", "limit_bytes": MAX_REQUEST})
        request = Request(self.config["url"] + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "").lower() if stream else ""
                if stream and ("event-stream" in content_type or "ndjson" in content_type):
                    return read_model_stream(response, protocol, on_delta or (lambda kind, text: None), key)
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ModelError("RESPONSE_SIZE", {"stage": "response_body", "response_bytes": len(raw), "limit_bytes": MAX_RESPONSE})
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ModelError("RESPONSE", {"stage": "response_json", "response_bytes": len(raw)}) from None
            if not isinstance(result, dict) or result.get("error"):
                raise ModelError("RESPONSE", {"stage": "response_shape"})
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            raise ModelError("REDIRECT" if 300 <= code < 400 else "AUTH" if code in (401, 403) else "NOT_FOUND" if code == 404 else "RATE_LIMIT" if code == 429 else "HTTP") from None
        except (TimeoutError, socket.timeout):
            raise ModelError("TIMEOUT") from None
        except URLError as error:
            raise ModelError("TIMEOUT" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "NETWORK") from None
        except HTTPException:
            raise ModelError("INCOMPLETE") from None
        except (ValueError, UnicodeError, OSError):
            raise ModelError("RESPONSE", {"stage": "response_body"}) from None

    def list_models(self):
        """查询厂商目录，最多十页及一千个型号；目录存在不代表逐个调用已验证。"""
        protocol = self.config["protocol"]
        models, seen, token, complete = [], set(), "", True
        for _ in range(10):
            path = "/api/tags" if protocol == "ollama" else "/models"
            if token:
                path += "?" + urlencode({"pageToken" if protocol == "gemini" else "after_id": token})
            result = self.request(path)
            rows = result.get("models" if protocol in ("gemini", "ollama") else "data")
            if not isinstance(rows, list):
                raise ModelError("RESPONSE")
            for row in rows:
                if not isinstance(row, dict):
                    raise ModelError("RESPONSE")
                if protocol == "gemini":
                    methods = row.get("supportedGenerationMethods", [])
                    if not isinstance(methods, list):
                        raise ModelError("RESPONSE")
                    if "generateContent" not in methods:
                        continue
                name = row.get("name" if protocol in ("gemini", "ollama") else "id")
                if protocol == "gemini" and isinstance(name, str):
                    name = name.removeprefix("models/")
                if not isinstance(name, str) or not name or len(name) > 256 or any(not character.isprintable() for character in name):
                    raise ModelError("RESPONSE")
                if name not in seen:
                    models.append(name)
                    seen.add(name)
            next_token = result.get("nextPageToken", "") if protocol == "gemini" else result.get("last_id", "") if protocol == "anthropic" and result.get("has_more") else ""
            if len(models) > 1000:
                complete = False
                break
            if not next_token:
                return {"models": models, "complete": not result.get("has_more", False)}
            if not isinstance(next_token, str) or next_token == token or len(next_token) > 2048:
                raise ModelError("RESPONSE")
            token = next_token
            complete = False
        return {"models": models[:1000], "complete": complete}

    def generate(self, text, *, system="请按要求用中文回答。", max_tokens=2048):
        """形状不符合协议的响应转为固定错误，不把第三方返回结构暴露给界面。"""
        try:
            return self._generate(text, system=system, max_tokens=max_tokens)
        except ModelError as error:
            if max_tokens is None and self.config["protocol"] != "anthropic":
                error.details["provider_default_budget"] = True
            if type(max_tokens) is int and 0 <= max_tokens <= 10**9:
                error.details["requested_max_tokens"] = max_tokens
            raise
        except (AttributeError, TypeError, KeyError, IndexError, ValueError):
            raise ModelError("RESPONSE", {"stage": "model_output", "requested_max_tokens": max_tokens}) from None

    def generate_stream(self, text, *, system="请按要求用中文回答。", max_tokens=2048, on_delta=None):
        """逐步接收公开思考摘要及正文；服务忽略流标志时兼容完整JSON，不重复发送请求。"""
        try:
            return self._generate(text, system=system, max_tokens=max_tokens, stream=True, on_delta=on_delta)
        except ModelError as error:
            if type(max_tokens) is int and 0 <= max_tokens <= 10**9:
                error.details["requested_max_tokens"] = max_tokens
            raise
        except (AttributeError, TypeError, KeyError, IndexError, ValueError):
            raise ModelError("RESPONSE", {"stage": "model_output", "requested_max_tokens": max_tokens}) from None

    def _generate(self, text, *, system, max_tokens, stream=False, on_delta=None):
        """使用固定文本协议，不附加工具、联网搜索或任何外部资源。"""
        protocol, model = self.config["protocol"], self.config["model"]
        if not model:
            raise ModelError("MODEL")
        if max_tokens is not None and (type(max_tokens) is not int or not 16 <= max_tokens <= 10**9):
            raise ModelError("CONFIG")
        # 原生Messages要求必填预算，沿用既有兼容值；其余协议允许交给接口默认值。
        if protocol == "anthropic" and max_tokens is None:
            max_tokens = 8192
        messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        if protocol == "responses":
            path, body = "/responses", {"model": model, "instructions": system, "input": text, "max_output_tokens": max_tokens, "store": False}
        elif protocol == "anthropic":
            path, body = "/messages", {"model": model, "system": system, "messages": messages[1:], "max_tokens": max_tokens}
        elif protocol == "gemini":
            path = "/models/" + quote(model.removeprefix("models/"), safe="") + (":streamGenerateContent?alt=sse" if stream else ":generateContent")
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": {"maxOutputTokens": max_tokens}}
        elif protocol == "ollama":
            path, body = "/api/chat", {"model": model, "messages": messages, "stream": stream, "options": {"num_predict": max_tokens}}
        else:
            token_field = "max_completion_tokens" if model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")) else "max_tokens"
            path, body = "/chat/completions", {"model": model, "messages": messages, token_field: max_tokens, "stream": stream}
            if urlsplit(self.config["url"]).hostname == "api.openai.com":
                body["store"] = False
        # 未显式指定预算时不发送任何输出上限字段，不能用零或任意大数模拟无限。
        if max_tokens is None:
            body.pop("max_tokens", None)
            body.pop("max_completion_tokens", None)
            body.pop("max_output_tokens", None)
            if protocol == "gemini":
                body.pop("generationConfig", None)
            elif protocol == "ollama":
                body.pop("options", None)
        if stream and protocol != "gemini":
            body["stream"] = True
        result = self.request(path, body, stream=True, on_delta=on_delta) if stream else self.request(path, body)
        if isinstance(result, StreamResult):
            return result.text
        reasoning, reason = "", None
        if protocol == "responses":
            blocks = [part for output in result.get("output", []) if output.get("type") == "message" for part in output.get("content", [])]
            answer = "\n".join(part.get("text", "") for part in blocks if part.get("type") == "output_text")
            reasoning = "\n".join(part.get("text", "") for output in result.get("output", []) if output.get("type") == "reasoning" for part in output.get("summary", []) if part.get("type") == "summary_text")
            incomplete = result.get("status") not in (None, "completed")
            reason = (result.get("incomplete_details") or {}).get("reason")
        elif protocol == "anthropic":
            answer = "\n".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
            reasoning = "\n".join(part.get("thinking", "") for part in result.get("content", []) if part.get("type") == "thinking")
            reason = result.get("stop_reason")
            incomplete = reason not in (None, "end_turn", "stop_sequence")
        elif protocol == "gemini":
            if result.get("promptFeedback", {}).get("blockReason"):
                raise ModelError("BLOCKED")
            candidate = (result.get("candidates") or [{}])[0]
            answer = "\n".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []) if not part.get("thought"))
            reasoning = "\n".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []) if part.get("thought"))
            reason = candidate.get("finishReason")
            incomplete = reason not in (None, "STOP")
        elif protocol == "ollama":
            answer = result.get("message", {}).get("content", "")
            reasoning = result.get("message", {}).get("thinking", "")
            reason = result.get("done_reason")
            incomplete = result.get("done") is False or reason not in (None, "stop")
        else:
            choice = (result.get("choices") or [{}])[0]
            answer = choice.get("message", {}).get("content", "")
            reasoning = choice.get("message", {}).get("reasoning_content") or choice.get("message", {}).get("reasoning") or ""
            if isinstance(answer, list):
                answer = "\n".join(part.get("text", "") for part in answer if part.get("type") == "text")
            reason = choice.get("finish_reason")
            incomplete = reason not in (None, "stop")
        safe_reason = reason if isinstance(reason, str) and reason in FINISH_REASONS else "missing" if reason is None else "other"
        details = {"text_chars": len(answer) if isinstance(answer, str) else 0, "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
                   "finish_reason": safe_reason, "requested_max_tokens": max_tokens, "stage": "model_output",
                   "termination_received": reason is not None, "end_marker": "json"}
        if reason == "insufficient_system_resource":
            raise ModelError("UPSTREAM_RESOURCE", details)
        if reason in ("length", "max_tokens", "max_output_tokens", "MAX_TOKENS"):
            raise ModelError("OUTPUT_LIMIT", details)
        if reason in ("content_filter", "refusal", "SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
            raise ModelError("BLOCKED", details)
        if incomplete:
            raise ModelError("INCOMPLETE", details)
        if not isinstance(answer, str) or not answer.strip():
            raise ModelError("THINKING_ONLY" if reasoning else "NO_TEXT", details)
        if stream and on_delta is not None:
            display = DeltaFilter(self.config["api_key"], on_delta)
            if isinstance(reasoning, str):
                display.emit("reasoning", reasoning)
            display.emit("text", answer)
            display.finish()
        # 防止异常服务把认证内容回显到报告或终端。
        return answer.replace(self.config["api_key"], "[已隐藏密钥]").strip() if self.config["api_key"] else answer.strip()

    def test(self):
        """只发送固定测试文字，不从调用方读取聊天或复用历史会话。"""
        started = time.monotonic()
        self.generate("连通性测试，请仅回复 OK。", max_tokens=1024)
        return {"model": self.config["model"], "latency_ms": round((time.monotonic() - started) * 1000), "available": True}
