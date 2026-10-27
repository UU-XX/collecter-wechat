# -*- coding: utf-8 -*-
"""验证本地分页、时区统计和离线报告转义，不读取微信或加载远程资源。"""

import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import cli
from analysis_report import generate_report
from cli_local import export_analysis
from collector_core import collect, parse_range
from export_jobs import run_batch
from local_analysis import analyze_result, browse_records
from result_store import TaskError, open_result
from workflow_selftest import SyntheticSource
from wechat_source import Conversation


class LocalAnalysisTests(unittest.TestCase):
    """统计来自原始任务表，HTML 和终端不能执行消息中的标记或控制序列。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.item = Conversation("synthetic@chatroom", '<script>合成群</script>', "群聊")
        self.window = parse_range("2024-12-31", "2025-01-03")
        boundary = int(dt.datetime(2025, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
        rows = [(1, 101, 1, boundary - 1, 1, 0, "member:\n旧年"),
                (2, 102, 1, boundary, 1, 0, "member:\n100%_关键字"),
                (3, 103, 3, boundary + 86400, 2, 0, "second:\n图片"),
                (4, 104, 1, boundary + 172800, None, 0, "未知发送人")]
        self.source = SyntheticSource(self.base / "source", [self.item], {self.item.username: rows})
        self.directory, _ = collect(self.source, self.item, self.window, self.base / "exports")

    def tearDown(self):
        """只释放合成数据所在的临时目录。"""
        self.temp.cleanup()

    def test_pagination_literal_search_sender_and_type_filter(self):
        page = browse_records(self.directory, page=2, page_size=2)
        self.assertEqual(page["total"], 4)
        self.assertEqual(len(page["records"]), 2)
        self.assertEqual(browse_records(self.directory, search="100%_")["total"], 1)
        self.assertEqual(browse_records(self.directory, sender="second", message_type=3)["total"], 1)
        self.assertEqual(browse_records(self.directory, search="' OR 1=1 --")["total"], 0)
        with self.assertRaises(ValueError):
            browse_records(self.directory, page_size=201)

    def test_all_records_timezone_same_names_and_unknown_senders(self):
        """统一报告覆盖跨年记录，按北京时间分组，不把同名身份合并。"""
        data = analyze_result(self.directory)
        self.assertEqual(data["kind"], "群聊")
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["observed_participants"], 2)
        self.assertEqual(data["unknown_sender_messages"], 1)
        self.assertEqual(data["longest_streak_days"], 4)
        self.assertEqual(data["months"]["2024-12"], 1)
        self.assertEqual(data["months"]["2025-01"], 3)
        self.assertEqual(len(data["participants"]), 2)
        self.assertEqual(data["participants"][0]["name"], data["participants"][1]["name"])
        self.assertEqual(sum(data["hours"].values()), 4)
        self.assertEqual(sum(data["weekdays"].values()), 4)

    def test_empty_result_and_personal_chat_are_recognized_automatically(self):
        """个人结果不需要选择报告类型，空数据也能生成清楚的离线说明。"""
        person = Conversation("synthetic-person", "合成个人聊天", "个人")
        source = SyntheticSource(self.base / "personal-source", [person], {})
        directory, _ = collect(source, person, self.window, self.base / "personal-exports")
        root, data = generate_report(directory, self.base / "reports")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["active_days"], 0)
        self.assertEqual(data["longest_streak_days"], 0)
        self.assertEqual(data["kind"], "个人聊天")
        page = (root / "分析报告.html").read_text(encoding="utf-8")
        self.assertIn("总体分析报告", page)
        self.assertIn("对话发言分布", page)
        self.assertIn("没有记录", page)
        self.assertNotIn("群成员", page)
        self.assertNotIn("发言前5位", page)

    def test_html_is_offline_escaped_and_contains_no_message_body(self):
        before = (self.directory / "records.sqlite3").read_bytes()
        root, data = generate_report(self.directory, self.base / "reports")
        page = (root / "分析报告.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>合成群", page)
        self.assertIn("&lt;script&gt;合成群", page)
        self.assertNotIn("100%_关键字", page)
        self.assertIn("default-src 'none'", page)
        self.assertIn("成员参与情况", page)
        self.assertIn("发言前5位", page)
        self.assertEqual(json.loads((root / "统计数据.json").read_text(encoding="utf-8"))["total"], data["total"])
        self.assertEqual((self.directory / "records.sqlite3").read_bytes(), before)

    def test_local_cli_commands_never_prepare_source(self):
        with patch("cli.prepare_source", side_effect=AssertionError("不得访问微信")), \
                patch("cli.own_processes", side_effect=AssertionError("不得查询微信")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["browse", "--input", str(self.directory), "--search", "关键字"]), 0)
            self.assertEqual(cli.main(["report", "--input", str(self.directory), "--output", str(self.base / "reports")]), 0)

    def test_export_batch_reports_are_local_and_auto_detect_each_conversation(self):
        """批量报告分别保存在本次会话结果内，自动识别类型且不修改任务库。"""
        person = Conversation("synthetic-person", "合成个人聊天", "个人")
        items = [self.item, person]
        source = SyntheticSource(self.base / "batch-source", items, self.source.rows)
        directory, plan = run_batch(source, items, self.window, self.base / "batch-results")
        before = {directory / task["directory"] / "records.sqlite3": (directory / task["directory"] / "records.sqlite3").read_bytes() for task in plan["tasks"]}
        with patch("cli.prepare_source", side_effect=AssertionError("分析不得读取微信")), \
                patch("cli.own_processes", side_effect=AssertionError("分析不得查询进程")), redirect_stdout(io.StringIO()):
            export_analysis(directory, plan, lambda error: self.fail(type(error).__name__))
        reports = list(directory.glob("*/report-*/统计数据.json"))
        self.assertEqual(len(reports), 2)
        self.assertEqual({json.loads(path.read_text(encoding="utf-8"))["kind"] for path in reports}, {"群聊", "个人聊天"})
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_single_report_entry_and_two_interactive_prompts(self):
        """菜单只询问来源与保存位置，旧分类和年份选项从命令帮助中移除。"""
        subcommands = cli.parser()._subparsers._group_actions[0].choices
        self.assertIn("report", subcommands)
        self.assertTrue({"stats", "profile", "annual"}.isdisjoint(subcommands))
        with patch("builtins.input", side_effect=[str(self.directory), str(self.base / "interactive-reports")]) as prompts, \
                patch("cli.prepare_source", side_effect=AssertionError("不得访问微信")), redirect_stdout(io.StringIO()):
            cli.interactive_report()
        self.assertEqual(prompts.call_count, 2)

    def test_readonly_result_rejects_sql_write(self):
        with open_result(self.directory) as connection, self.assertRaises(Exception):
            connection.execute("DELETE FROM messages")

    def test_resume_cli_inherits_frozen_range_and_account_guard_runs_first(self):
        """续导命令可直接使用原范围，错账号在准备密钥之前失败。"""
        with patch("cli.prepare_source", return_value=(self.source, [self.item])) as prepare, redirect_stdout(io.StringIO()):
            code = cli.main(["resume", "--account", str(self.source.root), "--from", str(self.directory), "--output", str(self.base / "resumed")])
        self.assertEqual(code, 0)
        self.assertFalse(prepare.call_args.args[1])
        with patch("cli.prepare_source", side_effect=AssertionError("错账号不得进入读取准备")), redirect_stdout(io.StringIO()):
            code = cli.main(["resume", "--account", str(self.base / "wrong"), "--from", str(self.directory)])
        self.assertEqual(code, 2)

    def test_legacy_result_has_actionable_static_hint_without_reading_source(self):
        """旧版本可做本地分析，但续导必须先建立可核对账号的新基线。"""
        path = self.directory / "采集说明.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report.pop("source_fingerprint")
        path.write_text(json.dumps(report), encoding="utf-8")
        output = io.StringIO()
        with patch("cli.prepare_source", side_effect=AssertionError("不得读取源账号")), redirect_stdout(io.StringIO()), redirect_stderr(output):
            code = cli.main(["resume", "--account", str(self.source.root), "--from", str(self.directory)])
        self.assertEqual(code, 2)
        self.assertIn("BASE_IDENTITY", output.getvalue())
        self.assertIn("建立基线", output.getvalue())
        self.assertNotIn("synthetic-private-marker", str(TaskError("synthetic-private-marker")))
