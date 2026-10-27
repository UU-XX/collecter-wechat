# -*- coding: utf-8 -*-
"""仅在显式自检时启动临时本地模拟接口，不使用真实厂商、密钥或聊天。"""

from contextlib import contextmanager
from contextlib import redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading

from model_client import ModelClient
from model_settings import load_config, save_config
from model_status import load_test_status, save_test_status

SYNTHETIC_KEY = "synthetic-model-key-not-a-real-credential"


@contextmanager
def synthetic_api():
    """随机端口仅绑定回环地址，退出时关闭监听和线程，不常驻。"""
    class Handler(BaseHTTPRequestHandler):
        """按各协议生成固定假响应，请求仅留在合成测试对象的内存中。"""

        def log_message(self, format, *arguments):
            """禁止HTTP访问日志进入终端或记录认证信息。"""
            pass

        def respond(self, value, code=200, headers=None):
            """发送有限的合成JSON，不读取任何文件或访问远程服务。"""
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def record(self, body=None):
            """合成认证头仅供单元测试核对，不发布或保存到日志。"""
            self.server.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})

        def do_GET(self):
            """模拟分页、权限错误和重定向，模型名均为合成占位。"""
            self.record()
            if self.path.startswith("/redirect"):
                return self.respond({}, 302, {"Location": self.server.url + "/should-not-follow"})
            if self.server.variant == "auth":
                return self.respond({"error": {"message": SYNTHETIC_KEY}}, 401)
            if self.path.startswith("/anthropic"):
                return self.respond({"data": [{"id": "synthetic-a" if "after_id" not in self.path else "synthetic-b"}], "has_more": "after_id" not in self.path, "last_id": "next"})
            if self.path.startswith("/gemini"):
                if "pageToken" in self.path:
                    return self.respond({"models": [{"name": "models/synthetic-b", "supportedGenerationMethods": ["generateContent"]}]})
                return self.respond({"models": [{"name": "models/synthetic-a", "supportedGenerationMethods": ["generateContent"]}, {"name": "models/synthetic-embedding", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "next"})
            if self.path.startswith("/ollama"):
                return self.respond({"models": [{"name": "synthetic-a"}, {"name": "synthetic-b"}]})
            if self.server.variant == "many":
                return self.respond({"data": [{"id": f"synthetic-{number}"} for number in range(1002)]})
            self.respond({"data": [{"id": "synthetic-a"}, {"id": "synthetic-b"}]})

        def do_POST(self):
            """仅解析合成请求，按所选协议回送固定文字或失败状态。"""
            length = min(int(self.headers.get("Content-Length", "0")), 1024 * 1024)
            body = json.loads(self.rfile.read(length))
            self.record(body)
            if self.server.variant == "daily-failure" and len(self.server.requests) == 2:
                return self.respond({"choices": [None]})
            if self.server.variant == "rate":
                return self.respond({"error": {"message": SYNTHETIC_KEY}}, 429)
            if self.server.variant == "error":
                return self.respond({"error": {"message": SYNTHETIC_KEY}})
            if self.server.variant == "malformed":
                return self.respond({"choices": [None], "output": [None], "content": [None], "candidates": [None], "message": None})
            if (body.get("stream") or ":streamGenerateContent" in self.path) and self.server.variant != "fallback":
                return self.respond_stream()
            text = "" if self.server.variant == "empty" else "合成接口测试通过"
            incomplete = self.server.variant == "incomplete"
            if self.path.endswith("/responses"):
                return self.respond({"status": "incomplete" if incomplete else "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})
            if self.path.endswith("/messages"):
                return self.respond({"stop_reason": "max_tokens" if incomplete else "end_turn", "content": [{"type": "text", "text": text}]})
            if self.path.endswith(":generateContent") or ":streamGenerateContent" in self.path:
                return self.respond({"candidates": [{"finishReason": "MAX_TOKENS" if incomplete else "STOP", "content": {"parts": [{"text": text}]}}]})
            if self.path.endswith("/api/chat"):
                return self.respond({"done": True, "done_reason": "length" if incomplete else "stop", "message": {"content": text}})
            self.respond({"choices": [{"finish_reason": "length" if incomplete else "stop", "message": {"content": text}}]})

        def respond_stream(self):
            """按真实格式逐事件发送合成思考及正文，支持中断和输出上限验收。"""
            protocol = self.path.split("/")[1]
            variant = self.server.variant
            reasoning = "合成思考。"
            text = "" if variant in ("empty", "thinking-only") else "合成流式正文。"
            if variant == "secret":
                reasoning, text = "合成思考。", SYNTHETIC_KEY
            if protocol == "responses":
                events = [{"type": "response.reasoning_summary_text.delta", "delta": reasoning}]
                events += [{"type": "response.output_text.delta", "delta": part} for part in (text[:3], text[3:])]
                terminal = {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}} if variant == "incomplete" else {"type": "response.completed"}
            elif protocol == "anthropic":
                events = [{"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": reasoning}}]
                events += [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}} for part in (text[:3], text[3:])]
                events.append({"type": "message_delta", "delta": {"stop_reason": "max_tokens" if variant == "incomplete" else "end_turn"}})
                terminal = {"type": "message_stop"}
            elif protocol == "gemini":
                events = [{"candidates": [{"content": {"parts": [{"thought": True, "text": reasoning}]}}]}]
                events += [{"candidates": [{"content": {"parts": [{"text": part}]}}]} for part in (text[:3], text[3:])]
                terminal = {"candidates": [{"finishReason": "MAX_TOKENS" if variant == "incomplete" else "STOP"}]}
            elif protocol == "ollama":
                events = [{"message": {"thinking": reasoning}, "done": False}]
                events += [{"message": {"content": part}, "done": False} for part in (text[:3], text[3:])]
                terminal = {"message": {}, "done": True, "done_reason": "length" if variant == "incomplete" else "stop"}
            else:
                events = [{"choices": [{"delta": {"reasoning_content": reasoning}}]}]
                events += [{"choices": [{"delta": {"content": part}}]} for part in (text[:3], text[3:])]
                terminal = {"choices": [{"delta": {}, "finish_reason": "length" if variant == "incomplete" else "stop"}]}
            if variant == "empty":
                events = []
            if variant != "disconnect":
                events.append(terminal)
            data = [(json.dumps(event, ensure_ascii=False) + "\n" if protocol == "ollama" else "data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8") for event in events]
            if variant == "bad-stream-json":
                data = [("data: invalid-private-response-" + SYNTHETIC_KEY + "\n\n").encode("utf-8")]
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson" if protocol == "ollama" else "text/event-stream")
            self.send_header("Content-Length", str(sum(map(len, data))))
            self.end_headers()
            try:
                for index, event in enumerate(data):
                    self.wfile.write(event)
                    self.wfile.flush()
                    if index == 0 and self.server.hold_final is not None:
                        self.server.hold_final.wait(timeout=5)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.url = f"http://127.0.0.1:{server.server_port}"
    server.variant, server.requests, server.hold_final = "normal", [], None
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def synthetic_config(server, protocol="chat"):
    """用显式本地假地址构造配置，不读取模型设置文件。"""
    return {"provider": "custom", "protocol": protocol, "url": server.url + "/" + protocol, "api_key": SYNTHETIC_KEY, "model": "synthetic-a"}


def model_self_test(directory):
    """冻结验收覆盖五类协议、目录分页及用户加密，不访问外部网络。"""
    with synthetic_api() as server:
        for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
            config = synthetic_config(server, protocol)
            client = ModelClient(config)
            assert client.list_models() == {"models": ["synthetic-a", "synthetic-b"], "complete": True}
            assert client.test()["available"] is True
            deltas = []
            assert client.generate_stream("合成流式自检", on_delta=lambda kind, text: deltas.append((kind, text))) == "合成流式正文。"
            assert any(kind == "reasoning" for kind, _ in deltas) and any(kind == "text" for kind, _ in deltas)
        path = Path(directory) / "synthetic-model-settings.dat"
        save_config(config, path)
        assert load_config(path) == config and SYNTHETIC_KEY.encode() not in path.read_bytes()
        status_path = Path(directory) / "synthetic-model-status.dat"
        save_test_status(config, {"available": True, "latency_ms": 12}, path=status_path)
        assert load_test_status(config, status_path)["available"] is True
        assert load_test_status({**config, "model": "synthetic-other"}, status_path) is None


def daily_model_self_test(directory):
    """冻结验收使用三天合成数据和回环接口，验证日文件、总体总结及失败诊断。"""
    from analysis_report import generate_report
    from collector_core import collect, parse_range
    from workflow_selftest import SyntheticSource
    from wechat_source import Conversation

    base = Path(directory)
    item = Conversation("synthetic-daily@chatroom", "合成日分析", "群聊")
    window = parse_range("2025-01-01", "2025-01-03")
    rows = [(index + 1, index + 101, 1, window.start + index * 86400, 1, 0, f"member:\n合成每日资料{index}") for index in range(3)]
    source = SyntheticSource(base / "synthetic-daily-source", [item], {item.username: rows})
    original, _ = collect(source, item, window, base / "synthetic-daily-exports")
    before = hashlib.sha256((original / "records.sqlite3").read_bytes()).digest()
    with synthetic_api() as server:
        config = synthetic_config(server)
        output = io.StringIO()

        def no_delta(kind, text):
            """即使旧调用方提供回调，模型总结也不得触发流式输出。"""
            raise AssertionError("总结不得启用流式回调")

        with redirect_stdout(output):
            root, data = generate_report(original, base / "synthetic-daily-reports", model_config=config, on_delta=no_delta)
        assert data["content_summary"]["status"] == "complete" and len(data["content_summary"]["days"]) == 3
        assert len(server.requests) == 5 and len(list((root / "每日总结").glob("*.md"))) == 3
        # 冻结程序的日分析和总体汇总也不能偷偷重新加上固定输出预算。
        assert all("max_tokens" not in request["body"] for request in server.requests)
        assert all(request["body"].get("stream") is False for request in server.requests)
        assert (root / "总体总结.md").is_file() and "合成思考" not in output.getvalue()
        log = (root / "模型诊断.jsonl").read_text(encoding="utf-8")
        assert not any(value in log + output.getvalue() for value in (SYNTHETIC_KEY, "合成每日资料", "合成接口测试通过", "合成流式正文", "合成思考"))
        for variant, completed, stage, requests in (("daily-failure", 1, "model_output", 2), ("malformed", 0, "model_output", 1)):
            server.variant, server.requests = variant, []
            root, data = generate_report(original, base / "synthetic-daily-reports", model_config=config, on_delta=lambda kind, text: None)
            summary = data["content_summary"]
            assert summary["status"] == "failed" and len(summary["days"]) == completed
            assert summary["diagnostics"]["stage"] == stage and len(server.requests) == requests
            assert not (root / "总体总结.md").exists()
            assert SYNTHETIC_KEY not in (root / "模型诊断.jsonl").read_text(encoding="utf-8")
        server.variant = "normal"
        assert ModelClient(config).generate("合成大预算测试", max_tokens=131072) == "合成接口测试通过"
        assert server.requests[-1]["body"]["max_tokens"] == 131072
    assert before == hashlib.sha256((original / "records.sqlite3").read_bytes()).digest()
