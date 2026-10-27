# -*- coding: utf-8 -*-
"""使用合成导出验证逐日分析、长响应和失败留存，不访问真实模型。"""

import io
import json
import re
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from unittest.mock import patch

from analysis_report import generate_report
from cli_local import SummaryStream
from model_settings import ModelError
from model_stream import read_model_stream
from model_summary import summarize_result
import test_model_summary as existing_tests


class DailySummaryTests(unittest.TestCase):
    """复用既有隔离夹具，只在测试拥有的数据库里构造跨日记录。"""

    setUp = existing_tests.ModelSummaryTests.setUp
    tearDown = existing_tests.ModelSummaryTests.tearDown

    def dates(self, count):
        """向合成结果增加逐日哨兵，第一条选在北京时间午夜边界。"""
        with closing(sqlite3.connect(self.directory / "records.sqlite3")) as store:
            store.execute("DELETE FROM messages WHERE rowid<> (SELECT MIN(rowid) FROM messages)")
            columns = [row[1] for row in store.execute("PRAGMA table_info(messages)")]
            row = dict(zip(columns, store.execute("SELECT * FROM messages").fetchone()))
            store.execute("DELETE FROM messages")
            for index in range(count):
                item = {**row, "message_key": f"day-{index}", "sent_at": self.window.start + index * 86400,
                        "text": f"每日哨兵{index:03}，安排当天会议。"}
                store.execute("INSERT INTO messages (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [item[name] for name in columns])
            store.commit()

    def test_daily_artifacts_and_final_summary(self):
        """每日单独分析和落盘，总体只汇总每日摘要，原数据库字节不变。"""
        self.dates(3)
        before = (self.directory / "records.sqlite3").read_bytes()
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "合成每日完整分析。"
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
        summary = data["content_summary"]
        self.assertEqual([day["date"] for day in summary.get("days", [])], ["2025-01-01", "2025-01-02", "2025-01-03"])
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(len(list((root / "每日总结").glob("*.md"))), 3)
        self.assertTrue((root / "总体总结.md").is_file())
        self.assertIn("每日分析总结", (root / "分析报告.html").read_text(encoding="utf-8"))
        prompts = [call.args[0] for call in client.return_value.generate.call_args_list]
        self.assertTrue(all(sum(f"每日哨兵{i:03}" in prompt for i in range(3)) <= 1 for prompt in prompts))
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)

    def test_whole_year_has_no_day_count_rejection(self):
        """全年逐日处理，不因日期数量拒绝；总体汇总也不会遗漏某天。"""
        self.dates(365)

        def generate(prompt, **options):
            """以日期哨兵模拟摘要，验证总体恰好包含每一天一次。"""
            return " ".join(re.findall(r"每日哨兵\d{3}", prompt))

        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.side_effect = generate
            result = summarize_result(self.directory, self.config, confirm_large=lambda plan: True)
        self.assertEqual(len(result.get("days", [])), 365)
        self.assertEqual(result["status"], "complete")
        self.assertGreaterEqual(result["requests"], 365)
        self.assertEqual(re.findall(r"每日哨兵\d{3}", result["text"]), [f"每日哨兵{index:03}" for index in range(365)])

    def test_long_complete_summary_is_not_rejected(self):
        """完整的长正文超过旧字符门槛仍应保存，不能误报响应错误。"""
        answer = "这是完整分析。" * 1000
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = answer
            result = summarize_result(self.directory, self.config)
        self.assertEqual(result["text"], answer)

    def test_failure_retains_completed_days_and_safe_diagnostic_log(self):
        """后一天失败时，先前完成的日分析已落盘，日志不含正文或认证信息。"""
        self.dates(3)

        def generate(prompt, **options):
            """第二天模拟格式失败，失败前第一天必须已经保存。"""
            if "每日哨兵001" in prompt:
                saved = list((self.base / "reports").glob("*/每日总结/2025-01-01.md"))
                self.assertEqual(len(saved), 1)
                raise ModelError("RESPONSE", {"stage": "stream_json", "text_chars": 0})
            return "第一天完整分析，不得进入诊断日志。"

        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.side_effect = generate
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
        summary = data["content_summary"]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(len(summary.get("days", [])), 1)
        self.assertEqual(summary.get("progress", {}).get("date"), "2025-01-02")
        self.assertFalse((root / "总体总结.md").exists())
        log = (root / "模型诊断.jsonl").read_text(encoding="utf-8")
        self.assertIn("stream_json", log)
        for secret in ("每日哨兵", "第一天完整分析", self.config["api_key"]):
            self.assertNotIn(secret, log)
        page = (root / "分析报告.html").read_text(encoding="utf-8")
        self.assertIn("RESPONSE", page)
        self.assertIn("2025-01-02", page)
        self.assertIn("已完成", page)

    def test_reasoning_never_appears_in_terminal(self):
        """关闭思考显示后，只显示总结正文，终端控制字符仍被过滤。"""
        output = io.StringIO()
        with redirect_stdout(output):
            stream = SummaryStream()
            stream.enabled = True
            stream("start", "")
            stream("reasoning", "不得输出的合成思考")
            stream("text", "合成正文\x1b[2J")
            stream("end", "")
        self.assertNotIn("不得输出", output.getvalue())
        self.assertIn("合成正文", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())

    def test_large_stream_and_parse_diagnostics(self):
        """协议开销超过旧累计上限仍能完成，坏事件能定位且不泄露原文。"""
        event = json.dumps({"choices": [{"delta": {"reasoning_content": "思" * 100}, "finish_reason": None}]}, ensure_ascii=False)
        finish = json.dumps({"choices": [{"delta": {"content": "完整正文"}, "finish_reason": "stop"}]}, ensure_ascii=False)
        payload = (("data: " + event + "\n\n") * 7000 + "data: " + finish + "\n\n").encode("utf-8")
        result = read_model_stream(io.BytesIO(payload), "chat", lambda kind, text: None, "")
        self.assertEqual(result.text, "完整正文")
        with self.assertRaises(ModelError) as caught:
            read_model_stream(io.BytesIO(b"data: PRIVATE_INVALID_EVENT\n\n"), "chat", lambda kind, text: None, "")
        self.assertEqual(caught.exception.details.get("stage"), "stream_json")
        self.assertNotIn("PRIVATE_INVALID_EVENT", json.dumps(caught.exception.details))

    def test_cancel_and_overall_failure_keep_all_finished_days(self):
        """总体汇总失败或取消时，全部已完成日期都可查看，不能发布伪完整总体总结。"""
        self.dates(3)
        for failure in (ModelError("TIMEOUT"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), patch("model_summary.ModelClient") as client:
                client.return_value.generate.side_effect = ["第一天。", "第二天。", "第三天。", failure]
                root, data = generate_report(self.directory, self.base / "reports", model_config=self.config)
            self.assertIn(data["content_summary"]["status"], ("failed", "cancelled"))
            self.assertEqual(len(data["content_summary"]["days"]), 3)
            self.assertEqual(data["content_summary"]["progress"]["phase"], "overall")
            self.assertEqual(len(list((root / "每日总结").glob("*.md"))), 3)
            self.assertFalse((root / "总体总结.md").exists())

    def test_unknown_time_is_separate_and_resource_error_is_specific(self):
        """时间无效的正文不静默丢失；单次保护触发时记录具体步骤和上限。"""
        with closing(sqlite3.connect(self.directory / "records.sqlite3")) as store:
            store.execute("UPDATE messages SET sent_at=999999999999999 WHERE rowid=(SELECT MIN(rowid) FROM messages)")
            store.commit()
        with patch("model_summary.ModelClient") as client:
            client.return_value.generate.return_value = "合成完整摘要。"
            result = summarize_result(self.directory, self.config)
        self.assertEqual({item["date"] for item in result["days"]}, {"2025-01-01", "时间无效"})
        self.assertEqual(result["messages"], 2)
        with patch("model_stream.MAX_LINE", 64), self.assertRaises(ModelError) as caught:
            read_model_stream(io.BytesIO(b"data: " + b"x" * 100 + b"\n\n"), "chat", lambda kind, text: None, "")
        self.assertEqual(caught.exception.code, "RESPONSE_SIZE")
        self.assertEqual(caught.exception.details["stage"], "stream_line")

    def test_declining_large_plan_saves_cancelled_report_with_no_completed_days(self):
        """拒绝大任务只取消发送，计划日期不得被误当成已有日分析，报告仍正常保存。"""
        with closing(sqlite3.connect(self.directory / "records.sqlite3")) as store:
            store.execute("UPDATE messages SET text=?", ("合成" * 100000,))
            store.commit()
        with patch("model_summary.ModelClient", side_effect=AssertionError("拒绝不得调用模型")):
            root, data = generate_report(self.directory, self.base / "reports", model_config=self.config, confirm_large=lambda plan: False)
        summary = data["content_summary"]
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(summary.get("days", []), [])
        self.assertEqual(summary["requests"], 0)
        self.assertTrue((root / "分析报告.html").is_file())
        self.assertFalse((root / "总体总结.md").exists())


if __name__ == "__main__":
    unittest.main()
