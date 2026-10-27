# -*- coding: utf-8 -*-
"""通过合成会话和模拟模型验证内容总结、发送边界与失败保留。"""

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

import cli
from cli_models import confirm_large_summary
from analysis_report import generate_report
from collector_core import collect, parse_range
from model_settings import ModelError
from model_summary import plan_summary, summarize_result
from model_selftest import synthetic_api, synthetic_config
from workflow_selftest import SyntheticSource
from wechat_source import Conversation


class ModelSummaryTests(unittest.TestCase):
    """总结仅依据指定导出，测试没有任何真实聊天或外部网络请求。"""

    def setUp(self):
        """建立一次独立的合成导出和虚构模型配置。"""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.item = Conversation("synthetic-summary@chatroom", "合成总结群", "群聊")
        self.window = parse_range("2025-01-01", "2025-01-01")
        rows = [(1, 101, 1, self.window.start + 100, 1, 0, "member:\n周五开会，只是提议。"),
                (2, 102, 1, self.window.start + 200, 2, 0, "second:\n待确认时间。")]
        self.source = SyntheticSource(self.base / "source", [self.item], {self.item.username: rows})
        self.directory, _ = collect(self.source, self.item, self.window, self.base / "exports")
        self.config = {"provider": "custom", "protocol": "chat", "url": "https://synthetic.invalid/v1", "api_key": "synthetic-summary-key", "model": "synthetic-text"}

    def tearDown(self):
        """只释放本测试拥有的临时目录。"""
        self.temp.cleanup()

    def test_statistics_default_never_initializes_model(self):
        """默认报告只统计，不因项目已有模型模块就自动发送正文。"""
        with patch("model_summary.ModelClient", side_effect=AssertionError("默认不得调用模型")):
            root, data = generate_report(self.directory, self.base / "reports")
        self.assertNotIn("content_summary", data)
        self.assertNotIn("周五开会", (root / "分析报告.html").read_text(encoding="utf-8"))

    def test_authorized_summary_is_escaped_and_preserves_source(self):
        """已授权时只传正文文字，模型输出作为普通文字显示，源库字节不变。"""
        before = (self.directory / "records.sqlite3").read_bytes()
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "主要话题：会议时间尚待确认。\n<script>合成标记</script>"
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
        self.assertEqual(data["content_summary"]["status"], "complete")
        self.assertEqual(data["content_summary"]["messages"], 2)
        self.assertIn("周五开会", client.return_value.generate.call_args.args[0])
        page = (root / "分析报告.html").read_text(encoding="utf-8")
        self.assertIn("聊天内容总结", page)
        self.assertIn("&lt;script&gt;合成标记", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn(self.config["api_key"], json.dumps(data, ensure_ascii=False))
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)

    def test_large_plan_waits_for_confirmation_before_any_model_call(self):
        """大记录先展示完整规模，未确认时零请求，确认后可处理原先超限的数据。"""
        with closing(sqlite3.connect(self.directory / "records.sqlite3")) as store:
            store.execute("UPDATE messages SET text=?", ("合成" * 100000,))
            store.commit()
        plan = plan_summary(self.directory)
        self.assertGreater(plan["chunks"], 16)
        with patch("model_summary.ModelClient", side_effect=AssertionError("未确认不得初始化模型")), self.assertRaises(ModelError) as caught:
            summarize_result(self.directory, self.config)
        self.assertEqual(caught.exception.code, "SUMMARY_CONFIRM")
        with patch("model_summary.ModelClient", side_effect=AssertionError("拒绝不得调用模型")):
            cancelled = summarize_result(self.directory, self.config, confirm_large=lambda value: False)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["requests"], 0)
        before = (self.directory / "records.sqlite3").read_bytes()
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "合成的大记录摘要。"
            result = summarize_result(self.directory, self.config, confirm_large=lambda value: value == plan)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["requests"], plan["planned_requests"])
        self.assertEqual(client.return_value.generate.call_count, plan["planned_requests"])
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)

    def test_chunks_merge_without_dropping_records(self):
        """先完整规划，再分段和成对归并；没有按时间抽样或丢弃正文。"""
        with patch("model_summary.CHUNK_CHARS", 80), patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "待办：会议时间待确认。"
            plan = plan_summary(self.directory)
            result = summarize_result(self.directory, self.config)
        self.assertGreater(plan["chunks"], 1)
        self.assertEqual(plan["messages"], 2)
        self.assertEqual(result["requests"], 2 * plan["chunks"] - 1)
        self.assertEqual(client.return_value.generate.call_count, result["requests"])

    def test_failure_or_cancel_preserves_statistics_and_safe_status(self):
        """失败和中断不能发布伪完整摘要，也不能把服务端秘密写入报告。"""
        for error in (ModelError("AUTH"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch("model_summary.ModelClient") as client:
                client.return_value.generate.side_effect = error
                root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
            self.assertEqual(data["total"], 2)
            self.assertIn(data["content_summary"]["status"], ("failed", "cancelled"))
            self.assertTrue((root / "统计数据.json").is_file())
            self.assertNotIn(self.config["api_key"], (root / "分析报告.html").read_text(encoding="utf-8"))

    def test_cli_requires_explicit_flag_for_model_summary(self):
        """单独报告命令仅在明确带发送标志时加载接口配置和调用模型。"""
        arguments = ["report", "--input", str(self.directory), "--output", str(self.base / "reports")]
        with patch("cli_local.required_config", side_effect=AssertionError("默认不得加载模型")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(arguments), 0)
        with patch("cli_local.required_config", return_value=self.config), patch("model_summary.ModelClient") as client, \
                redirect_stdout(io.StringIO()), patch("cli.prepare_source", side_effect=AssertionError("不得读取微信")):
            client.return_value.generate.return_value = "会议时间待确认。"
            self.assertEqual(cli.main([*arguments, "--with-model"]), 0)
            self.assertGreater(client.return_value.generate.call_count, 0)
            client.return_value.generate_stream.assert_not_called()

    def test_streamed_merge_keeps_every_chunk_in_chronological_order(self):
        """以编号哨兵验证奇数分段及不同树层均无遗漏、重复或顺序颠倒。"""
        def generate(prompt, **options):
            """逐段保留哨兵，合并时只拼接两个已有摘要。"""
            if prompt.startswith("以下是聊天资料片段"):
                return prompt.split("\n", 1)[1]
            return prompt.split("\n", 1)[1].replace("\n\n", "")

        for count in (1, 2, 3, 5, 6, 7, 17):
            chunks = [f"[{number:02}]" for number in range(count)]
            with self.subTest(chunks=count), patch("model_summary.summary_chunks", side_effect=lambda store, day=None, offset=0: iter(chunks)), patch("model_summary.ModelClient") as client:
                client.return_value.generate.side_effect = generate
                result = summarize_result(self.directory, self.config, confirm_large=lambda plan: True)
            self.assertEqual(result["text"], "".join(chunks))
            self.assertEqual(result["requests"], 2 * count - 1)

    def test_noninteractive_large_summary_requires_explicit_allow_flag(self):
        """参数模式不因已有发送授权就默认进行较多请求，也不能因失败重新读微信。"""
        with closing(sqlite3.connect(self.directory / "records.sqlite3")) as store:
            store.execute("UPDATE messages SET text=?", ("合成" * 100000,))
            store.commit()
        arguments = ["report", "--input", str(self.directory), "--output", str(self.base / "reports"), "--with-model"]
        with patch("cli_local.required_config", return_value=self.config), patch("sys.stdin.isatty", return_value=False), \
                patch("model_summary.ModelClient", side_effect=AssertionError("未确认不得调用模型")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(arguments), 3)
        with patch("cli_local.required_config", return_value=self.config), patch("sys.stdin.isatty", return_value=False), \
                patch("model_summary.ModelClient") as client, redirect_stdout(io.StringIO()), \
                patch("cli.prepare_source", side_effect=AssertionError("不得读取微信")):
            client.return_value.generate.return_value = "合成的大记录摘要。"
            self.assertEqual(cli.main([*arguments, "--allow-large-summary"]), 0)
            self.assertGreater(client.return_value.generate.call_count, 31)
            client.return_value.generate_stream.assert_not_called()

    def test_large_confirmation_displays_plan_and_defaults_no(self):
        """确认页给出具体规模，空输入、退出或再次中断均不能开始发送。"""
        plan = {"messages": 2000, "characters": 160000, "chunks": 20, "planned_requests": 39}
        for answer, expected in (("", False), ("n", False), ("y", True)):
            output = io.StringIO()
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer), redirect_stdout(output):
                self.assertIs(confirm_large_summary(plan), expected)
            self.assertIn("2,000", output.getvalue())
            self.assertIn("39", output.getvalue())
        for error in (KeyboardInterrupt, EOFError):
            with patch("builtins.input", side_effect=error), redirect_stdout(io.StringIO()):
                self.assertFalse(confirm_large_summary(plan))

    def test_deepseek_budget_override_and_incomplete_json_keep_statistics(self):
        """DeepSeek预算可调整，非流式超限仍提供诊断并保留统计，不保存半截正文。"""
        config = {**self.config, "provider": "deepseek"}
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "合成摘要。"
            summarize_result(self.directory, config)
            self.assertIsNone(client.return_value.generate.call_args.kwargs["max_tokens"])
            summarize_result(self.directory, config, max_output_tokens=16384)
            self.assertEqual(client.return_value.generate.call_args.kwargs["max_tokens"], 16384)
        before = (self.directory / "records.sqlite3").read_bytes()
        with synthetic_api() as server:
            server.variant = "incomplete"
            config = {**synthetic_config(server), "provider": "deepseek"}
            root, data = generate_report(self.directory, self.base / "reports", model_config=config, on_delta=lambda kind, text: None)
            self.assertEqual(len(server.requests), 1)
            self.assertNotIn("max_tokens", server.requests[0]["body"])
        summary = data["content_summary"]
        self.assertEqual(summary["error_code"], "OUTPUT_LIMIT")
        self.assertIs(summary["diagnostics"]["provider_default_budget"], True)
        self.assertNotIn("requested_max_tokens", summary["diagnostics"])
        self.assertEqual(summary["diagnostics"]["stage"], "model_output")
        self.assertNotIn("合成接口测试通过", (root / "分析报告.html").read_text(encoding="utf-8"))
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)
