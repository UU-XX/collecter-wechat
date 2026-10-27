# -*- coding: utf-8 -*-
"""验证所有报告入口统一非流式，固定合成接口不会发送真实聊天。"""

import io
import socket
import unittest
from contextlib import redirect_stdout
from urllib.error import URLError
from unittest.mock import patch

import cli
from analysis_report import generate_report
from model_client import ModelClient
from model_selftest import synthetic_api, synthetic_config
from model_summary import summarize_result
import test_model_summary as existing_tests


class NonstreamSummaryTests(unittest.TestCase):
    """旧回调和旧参数只兼容接收，不得重新开启流式请求。"""

    setUp = existing_tests.ModelSummaryTests.setUp
    tearDown = existing_tests.ModelSummaryTests.tearDown

    def test_summary_ignores_legacy_delta_and_uses_summary_timeout(self):
        """旧调用方传入逐字回调时，仍只请求完整结果且不触发逐字显示。"""
        def no_delta(kind, text):
            """非流式报告不得触发旧的逐字显示回调。"""
            raise AssertionError("不得触发流式回调")

        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "完整合成总结。"
            client.return_value.generate_stream.side_effect = AssertionError("不得发出流式请求")
            result = summarize_result(self.directory, self.config, on_delta=no_delta)
        self.assertEqual(result["text"], "完整合成总结。")
        self.assertEqual(client.call_args.kwargs["timeout"], 300)
        client.return_value.generate.assert_called_once()

    def test_cli_default_and_legacy_flag_both_use_json(self):
        """实际CLI参数入口默认非流式，旧关闭流式标志继续兼容且请求不重复。"""
        with synthetic_api() as server:
            for flags in ([], ["--no-stream"]):
                with self.subTest(flags=flags), patch("cli_local.required_config", return_value=synthetic_config(server)), redirect_stdout(io.StringIO()):
                    server.requests.clear()
                    code = cli.main(["report", "--input", str(self.directory), "--output", str(self.base / "reports"), "--with-model", *flags])
                self.assertEqual(code, 0)
                self.assertEqual(len(server.requests), 1)
                self.assertIs(server.requests[0]["body"]["stream"], False)

    def test_five_protocols_request_complete_responses(self):
        """五类接口均请求完整响应，生成正常日文件和总体结果。"""
        with synthetic_api() as server:
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    root, data = generate_report(self.directory, self.base / "reports", model_config=synthetic_config(server, protocol))
                    self.assertEqual(data["content_summary"]["status"], "complete")
                    self.assertEqual(len(server.requests), 1)
                    request = server.requests[0]
                    self.assertFalse(request["body"].get("stream", False))
                    self.assertNotIn("streamGenerateContent", request["path"])
                    self.assertTrue((root / "总体总结.md").is_file())

    def test_nonstream_summary_network_wait_is_extended(self):
        """内容总结有独立等待时间，模拟超时不连接远程接口也不重试。"""
        with patch("model_client.build_opener") as opener:
            opener.return_value.open.side_effect = URLError(socket.timeout("合成超时"))
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
        self.assertEqual(data["content_summary"]["error_code"], "TIMEOUT")
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 300)
        opener.return_value.open.assert_called_once()

    def test_optional_protocols_omit_default_budget_on_every_summary_stage(self):
        """所有日分段和树形汇总均省略默认预算，不能只修复第一次调用。"""
        with synthetic_api() as server, patch("model_summary.CHUNK_CHARS", 80):
            for protocol in ("chat", "responses", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    result = summarize_result(self.directory, synthetic_config(server, protocol))
                    self.assertEqual(result["status"], "complete")
                    self.assertGreater(len(server.requests), 1)
                    for request in server.requests:
                        body = request["body"]
                        self.assertTrue({"max_tokens", "max_completion_tokens", "max_output_tokens"}.isdisjoint(body))
                        self.assertNotIn("maxOutputTokens", body.get("generationConfig", {}))
                        self.assertNotIn("num_predict", body.get("options", {}))

    def test_manual_budget_still_reaches_all_five_protocols(self):
        """用户明确指定的预算原样传递，不再被默认值覆盖。"""
        with synthetic_api() as server:
            for protocol, field in (("chat", "max_tokens"), ("responses", "max_output_tokens"),
                                    ("anthropic", "max_tokens"), ("gemini", "maxOutputTokens"), ("ollama", "num_predict")):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    summarize_result(self.directory, synthetic_config(server, protocol), max_output_tokens=131072)
                    body = server.requests[0]["body"]
                    target = body.get("generationConfig", {}) if protocol == "gemini" else body.get("options", {}) if protocol == "ollama" else body
                    self.assertEqual(target[field], 131072)

    def test_gpt_completion_budget_field_is_also_optional(self):
        """兼容对话中的另一种预算字段同样省略，避免特定模型仍受隐含限制。"""
        with synthetic_api() as server:
            config = {**synthetic_config(server), "model": "gpt-5-synthetic"}
            ModelClient(config).generate("合成文字", max_tokens=None)
            self.assertNotIn("max_completion_tokens", server.requests[-1]["body"])
            ModelClient(config).generate("合成文字", max_tokens=16384)
            self.assertEqual(server.requests[-1]["body"]["max_completion_tokens"], 16384)

    def test_provider_limit_is_still_failure_without_a_local_budget(self):
        """接口自己触发上限时明确标注接口默认值，不把残缺结果误当成功或自动重发。"""
        with synthetic_api() as server:
            server.variant = "incomplete"
            root, data = generate_report(self.directory, self.base / "reports", model_config=synthetic_config(server))
            self.assertEqual(len(server.requests), 1)
        summary = data["content_summary"]
        self.assertEqual(summary["error_code"], "OUTPUT_LIMIT")
        self.assertTrue(summary["diagnostics"]["provider_default_budget"])
        self.assertNotIn("requested_max_tokens", summary["diagnostics"])
        self.assertIn("未设置本地输出 token 上限", (root / "分析报告.html").read_text(encoding="utf-8"))
        self.assertFalse((root / "总体总结.md").exists())

    def test_required_anthropic_budget_remains_compatible(self):
        """原生Messages要求必填预算，未指定时沿用兼容值，不向该协议发无效请求。"""
        with synthetic_api() as server:
            summarize_result(self.directory, synthetic_config(server, "anthropic"))
            self.assertEqual(server.requests[0]["body"]["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()
