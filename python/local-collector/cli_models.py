# -*- coding: utf-8 -*-
"""模型设置、目录发现和小请求测试入口，不自动采集或发送聊天。"""

import getpass
import datetime as dt
import sys
import warnings

from model_client import ModelClient
from model_settings import MODEL_ERRORS, PROVIDERS, PROTOCOLS, ModelError, load_config, normalize_url, save_config, validate_config
from model_status import ZONE, load_test_status, save_test_status


def safe_text(value, config=None):
    """屏蔽终端控制字符及可能被服务端回显的密钥。"""
    text = str(value)
    if config and config.get("api_key"):
        text = text.replace(config["api_key"], "[已隐藏密钥]")
    return "".join(character if character.isprintable() else " " for character in text)


def report_model_error(error):
    """只显示固定错误，不展示服务端异常正文、认证头或完整响应。"""
    print(f"模型接口 [{error.code}]：{MODEL_ERRORS[error.code]}", file=sys.stderr)


def required_config():
    """显式操作才读取本工具配置，不寻找其他程序的密钥。"""
    config = load_config()
    if config is None:
        raise ModelError("NOT_CONFIGURED")
    return config


def show_config(config):
    """可见配置不显示密钥明文、长度或尾号。"""
    if config is None:
        print("尚未配置模型。")
        return
    provider = next(item[1] for item in PROVIDERS if item[0] == config["provider"])
    print(f"\n{provider} · {PROTOCOLS[config['protocol']]}\nURL：{safe_text(config['url'], config)}")
    print("API Key：" + ("已加密保存" if config["api_key"] else "未设置（本地服务可留空）"))
    print("当前模型：" + (safe_text(config["model"], config) or "未选择"))


def model_header():
    """标题仅显示本地配置和上次手动测试，不把历史成功说成实时在线。"""
    try:
        config = load_config()
    except (ModelError, OSError):
        return {"model": "不可读取", "status": "配置不可读", "level": "error"}
    if not config:
        return {"model": "未设置", "status": "未配置", "level": "muted"}
    model = safe_text(config["model"], config) or "未选择"
    if not config["model"]:
        return {"model": model, "status": "未选择模型", "level": "muted"}
    try:
        status = load_test_status(config)
    except (ModelError, OSError):
        return {"model": model, "status": "测试记录不可读", "level": "warning"}
    if not status:
        return {"model": model, "status": "未测试", "level": "warning"}
    time = dt.datetime.fromisoformat(status["checked_at"]).astimezone(ZONE).strftime("%m-%d %H:%M")
    if status["available"]:
        return {"model": model, "status": f"上次测试通过 · {status['latency_ms']}ms", "checked_at": time, "level": "success"}
    return {"model": model, "status": f"上次测试失败 [{status['error_code']}]", "checked_at": time, "level": "error"}


def record_test_status(config, result=None, error_code=None):
    """缓存写入失败单独提示，不改变实际测试结论或触发重复请求。"""
    if not config.get("model"):
        return
    try:
        save_test_status(config, result, error_code)
    except (ModelError, OSError, RuntimeError):
        print("本次测试结果未能保存，标题将继续显示已有的本地记录。")


def read_secret(prompt):
    """终端不支持隐藏输入时直接停止，禁止回退到明文回显。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ModelError("CONFIG") from None


def configure_model():
    """先选厂商预填地址，URL或厂商改变后绝不沿用旧密钥。"""
    try:
        old = load_config()
    except ModelError as error:
        report_model_error(error)
        old = None
    for index, (_, name, _, _) in enumerate(PROVIDERS, 1):
        print(f"  {index:>2}  {name}")
    answer = input("厂商编号（0返回）：").strip()
    if answer == "0":
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(PROVIDERS):
        raise ModelError("CONFIG")
    provider, _, default_url, protocol = PROVIDERS[int(answer) - 1]
    if provider == "custom":
        for index, name in enumerate(PROTOCOLS.values(), 1):
            print(f"  {index}  {name}")
        choice = input("接口协议 [1]：").strip() or "1"
        if not choice.isdigit() or not 1 <= int(choice) <= len(PROTOCOLS):
            raise ModelError("CONFIG")
        protocol = list(PROTOCOLS)[int(choice) - 1]
    default_url = old["url"] if old and old["provider"] == provider and old["protocol"] == protocol else default_url
    url = normalize_url(input(f"URL [{default_url or '请填写基础地址'}]：").strip() or default_url, protocol)
    same = bool(old and old["provider"] == provider and old["url"] == url and old["protocol"] == protocol)
    key = read_secret("API Key（输入不回显，" + ("回车保留原密钥" if same else "本地无认证服务可留空") + "）：").strip()
    if same and not key:
        key = old["api_key"]
    model = input("模型 ID（可留空，稍后查询选择）：").strip() or (old["model"] if same else "")
    save_config({"provider": provider, "protocol": protocol, "url": url, "api_key": key, "model": model})
    print("已在本机加密保存。可选择查询模型或测试连接；保存本身不发送请求。")


def discover_models(config, choose=False):
    """手动读取目录，分页选择；不逐个生成，也不把目录型号标为已测试。"""
    result = ModelClient(config).list_models()
    models = result["models"]
    print(f"接口返回 {len(models)} 个模型" + ("（目录未读完）" if not result["complete"] else "") + "，尚未逐个验证调用权限。")
    if not choose:
        for model in models:
            print("  " + safe_text(model, config))
        return result
    page = 0
    while True:
        for index in range(page * 20, min((page + 1) * 20, len(models))):
            print(f"  {index + 1:>3}  {safe_text(models[index], config)}")
        answer = input("模型编号 / n下一页 / p上一页 / m手动填写 / 0返回：").strip().lower()
        if answer == "0":
            return result
        if answer == "n":
            page = min(page + 1, max(0, (len(models) - 1) // 20))
        elif answer == "p":
            page = max(0, page - 1)
        elif answer == "m":
            select_model(config, input("模型 ID：").strip())
            return result
        elif answer.isdigit() and 1 <= int(answer) <= len(models):
            select_model(config, models[int(answer) - 1])
            return result


def select_model(config, model):
    """目录不开放时仍可手动填写，模型是否可调用由独立测试判断。"""
    if not model:
        raise ModelError("MODEL")
    value = validate_config({**config, "model": model})
    save_config(value)
    print("已选择：" + safe_text(value["model"], value) + "；尚未测试。")


def test_model(config):
    """单次固定文字测试可能消耗少量额度，不使用聊天或打印模型回复。"""
    print("仅发送固定测试文字，可能消耗少量模型额度。")
    try:
        result = ModelClient(config).test()
    except ModelError as error:
        record_test_status(config, error_code=error.code)
        raise
    record_test_status(config, result=result)
    print(f"已测试可用：{safe_text(result['model'], config)} · 本次耗时 {result['latency_ms']} ms")
    return result


def interactive_models():
    """设置页不后台监测，查询和测试必须由菜单选项分别触发。"""
    while True:
        try:
            show_config(load_config())
        except ModelError as error:
            report_model_error(error)
        print("\n  1 配置厂商、URL 和 API Key\n  2 查询并选择模型\n  3 测试当前模型（少量测试文字）\n  4 手动填写模型 ID\n  0 返回")
        answer = input("选择：").strip()
        if answer == "0":
            return
        try:
            if answer == "1":
                configure_model()
            elif answer == "2":
                discover_models(required_config(), choose=True)
            elif answer == "3":
                test_model(required_config())
            elif answer == "4":
                select_model(required_config(), input("模型 ID：").strip())
        except ModelError as error:
            report_model_error(error)


def add_model_commands(commands):
    """参数命令不接受密钥参数，避免密钥进入命令历史或进程参数。"""
    model = commands.add_parser("model", help="厂商配置、模型目录及手动测试，不读取聊天")
    actions = model.add_subparsers(dest="model_action")
    actions.add_parser("configure", help="交互选择厂商并隐藏输入密钥")
    actions.add_parser("show", help="查看配置，密钥始终隐藏")
    actions.add_parser("models", help="查询接口模型目录，不进行逐个生成测试")
    for name, help_text in (("test", "使用固定文字测试一个模型"), ("select", "手动选择模型")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--model", required=name == "select", help="模型 ID；测试默认使用已选择模型")


def run_model_command(arguments):
    """配置、查询和测试均不访问微信账号、数据库或导出正文。"""
    action = arguments.model_action
    if action is None:
        interactive_models()
    elif action == "configure":
        configure_model()
    elif action == "show":
        show_config(load_config())
    else:
        config = required_config()
        if action == "models":
            discover_models(config)
        elif action == "select":
            select_model(config, arguments.model)
        else:
            test_model({**config, "model": arguments.model or config["model"]})
    return 0


def choose_summary_config():
    """每份报告单独确认发送范围，联网授权不写入配置或沿用到以后任务。"""
    try:
        config = load_config()
        if not config or not config["model"]:
            print("当前仅生成本地统计；内容总结可先从菜单4配置并选择模型。")
            return None
        print("内容总结会把本次所选记录的正文、发送人及时间发送到：" + safe_text(config["url"], config))
        print("使用模型：" + safe_text(config["model"], config))
        return config if input("允许本次使用该接口总结聊天内容 [y/N]：").strip().lower() in ("y", "yes") else None
    except ModelError as error:
        report_model_error(error)
        return None


def confirm_large_summary(plan):
    """大记录先展示可核对的请求规模，拒绝或中断时本会话不发送任何正文。"""
    print(f"本会话共 {plan['messages']:,} 条有效正文，约 {plan['characters']:,} 个发送文本字符，分为 {plan['chunks']:,} 段。")
    print(f"含逐层汇总，预计最多 {plan['planned_requests']:,} 次模型请求；实际费用和耗时以接口为准。")
    try:
        return input("确认处理本会话全部记录 [y/N]：").strip().lower() in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        return False
