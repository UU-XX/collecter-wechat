# -*- coding: utf-8 -*-
"""用合成消息验证新格式、文件校验及导出失败保护。"""

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from collector_core import collect, export_results, parse_range
from test_collector import FakeSource
from wechat_source import Conversation


class ExportFormatTests(unittest.TestCase):
    """对照真实文件与任务数据库，不读取微信或外部资源。"""

    def setUp(self):
        """准备独立输出根及相同时间范围的合成消息。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name).resolve()
        self.window = parse_range("2025-01-01", "2025-01-01")
        self.conversation = Conversation("synthetic@chatroom", "合成导出", "群聊")

    def tearDown(self):
        """仅释放测试创建的独立目录。"""
        self.temporary.cleanup()

    def source(self, body, *, fail=False, kind=1):
        """构造包含正文和原生消息身份的模拟数据源。"""
        return FakeSource(self.conversation, [(1, 101, kind, self.window.start, 1, 0, "member:\n" + body)], fail=fail)

    def test_csv_multiline_and_formula_are_text_without_changing_json(self):
        """CSV 正确处理逗号、引号和换行，JSONL 仍保留原正文。"""
        body = '=HYPERLINK("https://example.invalid","示例")\n下一行,带逗号'
        directory, report = collect(self.source(body), self.conversation, self.window, self.output)
        with (directory / "messages.csv").open(encoding="utf-8-sig", newline="") as stream:
            records = list(csv.DictReader(stream))
        self.assertEqual(records[0]["正文"], "'" + body)
        self.assertEqual(len(records), report["count"])
        original = json.loads((directory / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(original["text"], body)

    def test_markdown_treats_message_markup_and_remote_images_as_text(self):
        """普通正文不能插入远程图片或网页脚本。"""
        body = '![远程图](https://example.invalid/p.png)\n<script>bad()</script>\n# 伪标题'
        directory, _ = collect(self.source(body), self.conversation, self.window, self.output)
        text = (directory / "聊天记录.md").read_text(encoding="utf-8")
        self.assertNotIn("![远程图]", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("> \\# 伪标题", text)

    def test_report_matches_files_and_separates_archival_completeness(self):
        """文件生成完整不等于微信历史完整，摘要必须能独立核对。"""
        directory, report = collect(self.source("中文消息"), self.conversation, self.window, self.output)
        self.assertEqual(report["exports"]["status"], "complete")
        self.assertEqual(set(report["exports"]["files"]), {"消息记录.txt", "messages.jsonl", "聊天记录.md", "messages.csv"})
        for name, metadata in report["exports"]["files"].items():
            data = (directory / name).read_bytes()
            self.assertEqual(metadata["bytes"], len(data))
            self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
        self.assertFalse(report["completeness"]["wal_merged"])
        self.assertFalse(report["completeness"]["cross_shard_snapshot"])
        self.assertEqual(report["message_types"]["1"]["count"], report["count"])

    def test_writer_failure_keeps_database_and_publishes_no_incomplete_files(self):
        """格式写入中断后仍有可恢复数据，不能把结果显示成完成。"""
        with patch("collector_core.export_markdown_csv", side_effect=OSError("合成磁盘错误")):
            directory, report = collect(self.source("保留消息"), self.conversation, self.window, self.output)
        self.assertEqual(report["exports"]["status"], "failed")
        self.assertIn("结果导出失败", report["status"])
        with closing(sqlite3.connect(directory / "records.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual({path.name for path in directory.iterdir()}, {"records.sqlite3", "采集说明.json"})
        saved = json.loads((directory / "采集说明.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["exports"]["status"], "failed")

    def test_existing_exports_are_never_overwritten(self):
        """误对已有结果再次发布时，所有旧导出逐字节保持原样。"""
        directory, report = collect(self.source("原有结果"), self.conversation, self.window, self.output)
        originals = {path.name: path.read_bytes() for path in directory.iterdir() if path.suffix != ".sqlite3"}
        with closing(sqlite3.connect(directory / "records.sqlite3")) as database:
            with self.assertRaises(FileExistsError):
                export_results(database, directory, report)
        for name, content in originals.items():
            self.assertEqual((directory / name).read_bytes(), content)
        self.assertFalse(list(directory.glob(".export-*")))

    def test_image_messages_remain_records_without_image_export(self):
        """去掉导图功能仍保留图片消息身份，新任务不建立图片附件表或输出目录。"""
        directory, report = collect(self.source("图片", kind=3), self.conversation, self.window, self.output)
        with closing(sqlite3.connect(directory / "records.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT type FROM messages").fetchone()[0], 3)
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone())
        record = json.loads((directory / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("image", record)
        self.assertNotIn("images", report)
        self.assertFalse((directory / "images").exists())


if __name__ == "__main__":
    unittest.main()
