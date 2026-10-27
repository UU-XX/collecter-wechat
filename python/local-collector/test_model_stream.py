# -*- coding: utf-8 -*-
"""用真实回环HTTP和有限字节流验证逐步输出、结束标记和隐私边界。"""

import io
import json
import threading
import unittest
from contextlib import redirect_stdout

from cli_local import SummaryStream
from model_client import ModelClient
from model_selftest import SYNTHETIC_KEY, synthetic_api, synthetic_config
from model_settings import ModelError
from model_stream import DeltaFilter, read_model_stream


class ModelStreamTests(unittest.TestCase):
    """测试只连接本机临时模拟接口，不读取配置或使用真实模型。"""

    def test_five_protocols_stream_public_reasoning_and_text(self):
        """五类接口按真实事件格式逐步交付，返回值只包含最终正文。"""
        with synthetic_api() as server:
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    events = []
                    answer = ModelClient(synthetic_config(server, protocol)).generate_stream("合成问题", on_delta=lambda kind, text: events.append((kind, text)))
                    self.assertEqual(answer, "合成流式正文。")
                    self.assertEqual("".join(text for kind, text in events if kind == "reasoning"), "合成思考。")
                    self.assertEqual("".join(text for kind, text in events if kind == "text"), answer)
                    self.assertEqual(len(server.requests), 1)
                    if protocol == "gemini":
                        self.assertIn(":streamGenerateContent?alt=sse", server.requests[0]["path"])
                    else:
                        self.assertIs(server.requests[0]["body"]["stream"], True)

    def test_first_piece_arrives_before_response_is_finished(self):
        """服务端等待放行结束事件时，客户端必须已经显示首个真实片段。"""
        with synthetic_api() as server:
            server.hold_final = threading.Event()
            first_piece = threading.Event()
            answers, failures = [], []

            def receive():
                """独立接收以证明并非等到全部响应结束后才拆分文字。"""
                try:
                    answers.append(ModelClient(synthetic_config(server)).generate_stream("合成问题", on_delta=lambda kind, text: first_piece.set()))
                except Exception as error:
                    failures.append(type(error).__name__)

            thread = threading.Thread(target=receive)
            thread.start()
            try:
                self.assertTrue(first_piece.wait(timeout=2))
                self.assertTrue(thread.is_alive())
            finally:
                server.hold_final.set()
                thread.join(timeout=3)
            self.assertFalse(failures)
            self.assertEqual(answers, ["合成流式正文。"])

    def test_cutoff_thinking_only_and_disconnect_are_distinct_and_not_retried(self):
        """完整正文、仅思考、额度用尽和流断开不能混成同一个空文本错误。"""
        with synthetic_api() as server:
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                for variant, code in (("incomplete", "OUTPUT_LIMIT"), ("thinking-only", "THINKING_ONLY"), ("disconnect", "INCOMPLETE"), ("empty", "NO_TEXT")):
                    with self.subTest(protocol=protocol, variant=variant):
                        server.requests.clear()
                        server.variant = variant
                        with self.assertRaises(ModelError) as caught:
                            ModelClient(synthetic_config(server, protocol)).generate_stream("合成问题")
                        self.assertEqual(caught.exception.code, code)
                        self.assertEqual(len(server.requests), 1)
                        self.assertNotIn(SYNTHETIC_KEY, json.dumps(caught.exception.details))

    def test_json_fallback_never_repeats_request(self):
        """接口忽略流标志而返回完整JSON时照常使用，不能自动再次计费。"""
        with synthetic_api() as server:
            server.variant = "fallback"
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    output = []
                    answer = ModelClient(synthetic_config(server, protocol)).generate_stream("合成问题", on_delta=lambda kind, text: output.append(text))
                    self.assertEqual(answer, "合成接口测试通过")
                    self.assertEqual("".join(output), answer)
                    self.assertEqual(len(server.requests), 1)

    def test_key_redaction_handles_split_and_cross_channel_fragments(self):
        """已知密钥跨片段或跨公开通道出现时也不能被拼接输出。"""
        parts = []
        display = DeltaFilter(SYNTHETIC_KEY, lambda kind, text: parts.append((kind, text)))
        display.emit("reasoning", "开始。" + SYNTHETIC_KEY[:12])
        display.emit("text", SYNTHETIC_KEY[12:] + "结束。")
        display.finish()
        text = "".join(value for _, value in parts)
        self.assertNotIn(SYNTHETIC_KEY, text)
        self.assertIn("[已隐藏密钥]", text)
        self.assertEqual(text, "开始。[已隐藏密钥]结束。")
        with synthetic_api() as server:
            server.variant = "secret"
            parts = []
            answer = ModelClient(synthetic_config(server)).generate_stream("合成问题", on_delta=lambda kind, text: parts.append(text))
            self.assertNotIn(SYNTHETIC_KEY, "".join(parts) + answer)

    def test_sse_heartbeats_multiline_json_and_bad_data(self):
        """心跳和多行事件不破坏UTF8文本，坏数据和未完成流必须失败。"""
        response = io.BytesIO(':心跳\r\nevent: message\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"合成中文"},"finish_reason":"stop"}]}\r\n\r\n'.encode("utf-8"))
        result = read_model_stream(response, "chat", lambda kind, text: None, "")
        self.assertEqual(result.text, "合成中文")
        for data in (b'data: invalid\n\n', b'data: \xff\n\n', b'data: {"choices": [null]}\n\n'):
            with self.subTest(data=data), self.assertRaises(ModelError):
                read_model_stream(io.BytesIO(data), "chat", lambda kind, text: None, "")

    def test_terminal_stream_controls_and_redirected_logs(self):
        """交互终端只显示正文，思考和日志正文均不输出，控制序列不能执行。"""
        output = io.StringIO()
        with redirect_stdout(output):
            stream = SummaryStream()
            stream.enabled = True
            stream("start", "")
            stream("reasoning", "合成思考\x1b[2J\n")
            stream("text", "合成正文")
            stream("end", "")
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("模型思考", output.getvalue())
        self.assertNotIn("合成思考", output.getvalue())
        self.assertIn("[总结正文]", output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            stream = SummaryStream()
            stream("text", "不得写入日志的正文")
            stream("end", "")
        self.assertEqual(output.getvalue(), "")
