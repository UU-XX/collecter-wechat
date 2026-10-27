# -*- coding: utf-8 -*-
"""控制台入口只使用合成会话，验证授权边界、导出和取消。"""

import datetime as dt
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import cli
from bootstrap import Cancelled
from collector_core import SHANGHAI, parse_range
from cli_theme import render_header, render_menu, supports_color
from test_collector import FakeSource
from wechat_source import Conversation
from read_errors import ReadError


class CliTests(unittest.TestCase):
    """覆盖用户可操作入口，避免测试访问真实微信。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.conversation = Conversation("synthetic@chatroom", "合成测试", "群聊")
        timestamp = int(dt.datetime(2025, 1, 1, tzinfo=SHANGHAI).timestamp())
        self.source = FakeSource(self.conversation, [(1, 1, 1, timestamp, 1, 0, "member:\n合成正文")])
        self.output = io.StringIO()
        self.error = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stderr = redirect_stderr(self.error)
        self.stdout.__enter__()
        self.stderr.__enter__()

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        self.stdout.__exit__(None, None, None)
        self.temp.cleanup()

    def arguments(self, *extra):
        """默认使用隔离的合成账号与输出目录。"""
        return ["export", "--account", str(self.directory / "db_storage"), "--conversation", self.conversation.short_id,
                "--start", "2025-01-01", "--end", "2025-01-01", "--output", str(self.directory / "exports"), *extra]

    def test_logo_help_and_exit_do_not_read(self):
        with patch("cli.find_accounts", side_effect=AssertionError), patch("cli.own_processes", side_effect=AssertionError), patch("cli.WechatSource", side_effect=AssertionError), \
                patch("cli.collect", side_effect=AssertionError):
            self.assertEqual(cli.main(["--logo"]), 0)
            with self.assertRaises(SystemExit) as result:
                cli.main(["--help"])
            self.assertEqual(result.exception.code, 0)
            with patch("builtins.input", return_value="0"), patch("sys.stdin.isatty", return_value=True):
                self.assertEqual(cli.main([]), 0)
            with patch("sys.stdin.isatty", return_value=False):
                self.assertEqual(cli.main([]), 0)
        self.assertIn("Collecter", self.output.getvalue())

    def test_isolated_selftest_reports_missing_module_without_exception_body(self):
        """打包诊断可以定位缺失依赖，但不能写入异常携带的任意文本。"""
        error = ModuleNotFoundError("synthetic-private-marker", name="synthetic_missing")
        with patch("cli_selftest.self_test", side_effect=error):
            self.assertEqual(cli.main(["--self-test-dir", str(self.directory)]), 2)
        report = (self.directory / "self-test.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(report)["missing_module"], "synthetic_missing")
        self.assertNotIn("synthetic-private-marker", report)

    @unittest.skipUnless(os.name == "nt", "仅验证 Windows 源码启动器")
    def test_windows_launcher_runs_from_directory_with_spaces(self):
        """实际调用中文批处理，防止换行格式导致命令被截断。"""
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(self.directory / "local")
        env["APPDATA"] = str(self.directory / "roaming")
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "启动CLI.cmd --logo"],
                                cwd=Path(__file__).resolve().parent, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("Collecter", result.stdout)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_terminal_output_and_color_opt_out_stay_plain(self):
        """日志与关闭颜色模式不能混入控制序列或大块像素标识。"""
        with patch("sys.stdout.isatty", return_value=False):
            self.assertFalse(supports_color())
        with patch("sys.stdout.isatty", return_value=True), patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertFalse(supports_color())
        plain = render_header() + render_menu()
        self.assertNotIn("\033", plain)
        self.assertNotIn("▀", plain)
        self.assertIn("Collecter", plain)
        self.assertIn("读取并导出", plain)

    def test_terminal_layout_fits_wide_and_narrow_windows(self):
        """把中文按两列计算，检查标识与菜单不会超出支持的终端宽度。"""
        for columns in (40, 65, 66, 80):
            with self.subTest(columns=columns):
                text = render_header(True, columns) + render_menu(True, columns)
                plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
                for line in plain.splitlines():
                    width = sum(2 if unicodedata.east_asian_width(character) in "WF" else 1 for character in line)
                    self.assertLessEqual(width, columns)

    def test_export_short_id_default_authorization_and_formats(self):
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation])) as prepare:
            self.assertEqual(cli.main(self.arguments()), 0)
        self.assertFalse(prepare.call_args.args[1])
        self.assertFalse(prepare.call_args.args[2])
        task = next((self.directory / "exports").iterdir())
        for name in ("messages.jsonl", "messages.csv", "聊天记录.md", "消息记录.txt", "records.sqlite3", "采集说明.json"):
            self.assertTrue((task / name).is_file(), name)
        rows = [json.loads(line) for line in (task / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["text"] for row in rows], ["合成正文"])
        self.assertNotIn("合成正文", self.output.getvalue())

    def test_bad_range_rejected_before_read(self):
        with patch("cli.prepare_source", side_effect=AssertionError):
            self.assertEqual(cli.main(self.arguments("--start", "2025-02-01")), 2)

    def test_removed_image_flags_are_rejected_before_read(self):
        """已移除的图片参数必须在解析阶段拒绝，不能触发读取。"""
        with patch("cli.prepare_source", side_effect=AssertionError):
            for flag in ("--images", "--allow-image-memory"):
                with self.subTest(flag=flag), self.assertRaises(SystemExit) as caught:
                    cli.main(self.arguments(flag))
                self.assertEqual(caught.exception.code, 2)

    def test_explicit_authorizations_reach_existing_collector(self):
        report = {"status": "主库存档采集完成", "count": 0, "exports": {"status": "complete"}}
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation])) as prepare, \
                patch("cli.collect", return_value=(self.directory, report)) as collect:
            self.assertEqual(cli.main(self.arguments("--allow-memory")), 0)
        self.assertEqual(prepare.call_args.args[1:3], (True, False))
        self.assertEqual(len(collect.call_args.args), 6)

    def test_video_file_flags_prepare_resources_without_memory(self):
        """媒体类型独立启用，不自动授予数据库内存权限。"""
        report = {"status": "主库存档采集完成", "count": 0, "exports": {"status": "complete"},
                  "media": {"enabled": True, "complete": True}}
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation])) as prepare, \
                patch("cli.collect", return_value=(self.directory, report)) as collect:
            self.assertEqual(cli.main(self.arguments("--videos", "--files")), 0)
        self.assertEqual(prepare.call_args.args[1:3], (False, True))
        self.assertEqual(collect.call_args.kwargs, {"include_videos": True, "include_files": True})
        self.assertEqual(len(collect.call_args.args), 6)

    def test_multiple_and_all_conversations_use_batch_without_extra_authorization(self):
        """批量选择必须由明确参数触发，并沿用默认关闭的数据库读取授权。"""
        other = Conversation("second-synthetic@chatroom", "另一合成群", "群聊")
        plan = {"status": "已完成", "tasks": [{"complete": True}, {"complete": True}]}
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation, other])) as prepare, \
                patch("cli.run_batch", return_value=(self.directory, plan)) as batch:
            self.assertEqual(cli.main(self.arguments("--conversation", other.short_id)), 0)
        self.assertEqual(batch.call_args.args[1], [self.conversation, other])
        self.assertFalse(prepare.call_args.args[1])
        arguments = self.arguments()
        offset = arguments.index("--conversation")
        del arguments[offset:offset + 2]
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation, other])), patch("cli.run_batch", return_value=(self.directory, plan)):
            self.assertEqual(cli.main([*arguments, "--all-conversations"]), 0)

    def test_incomplete_media_has_nonzero_exit(self):
        """文字完成也不能把缺失媒体作为完整导出交给脚本。"""
        report = {"status": "主库存档采集完成", "count": 1, "exports": {"status": "complete"},
                  "media": {"enabled": True, "complete": False}}
        self.assertEqual(cli.show_result(self.directory, report), 3)

    def test_missing_or_ambiguous_session_never_collects(self):
        for items in ([], [self.conversation, self.conversation]):
            with self.subTest(count=len(items)), patch("cli.prepare_source", return_value=(self.source, items)), \
                    patch("cli.collect", side_effect=AssertionError):
                self.assertEqual(cli.main(self.arguments()), 2)

    def test_same_names_are_selected_by_identifier(self):
        other = Conversation("other@chatroom", self.conversation.name, "群聊")
        with patch("cli.prepare_source", return_value=(self.source, [other, self.conversation])):
            self.assertEqual(cli.main(self.arguments()), 0)

    def test_interactive_decline_keeps_memory_off_and_does_not_collect(self):
        answers = ["1", "", "", "1", "", "2025-01-01", str(self.directory), "", "", ""]
        with patch("cli.find_accounts", return_value=[self.directory]), \
                patch("cli.own_processes", return_value=[(123, 1)]), \
                patch("cli.prepare_source", return_value=(self.source, [self.conversation])) as prepare, \
                patch("cli.collect", side_effect=AssertionError), patch("builtins.input", side_effect=answers):
            cli.interactive_export()
        self.assertFalse(prepare.call_args.args[1])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_inline_retry_reuses_partial_records_and_preserves_first_result(self):
        """读取失败后原页确认即可补完，不重复输入参数或覆盖失败结果。"""
        self.source.root = self.directory / "synthetic-source"
        self.source.fail = True
        output = self.directory / "inline-retry"
        before = {}

        def retry(prompt):
            """在明确确认时解除合成故障，并记录失败现场的原始字节。"""
            self.assertIn("是否重试", prompt)
            directory = next(output.iterdir())
            before.update({path: path.read_bytes() for path in directory.iterdir() if path.is_file()})
            self.source.fail = False
            return True

        with patch("cli.yes", side_effect=retry) as consent:
            cli.interactive_collect(self.source, [self.conversation], parse_range("2025-01-01", "2025-01-01"), output)
        consent.assert_called_once()
        reports = [json.loads((path / "采集说明.json").read_text(encoding="utf-8")) for path in output.iterdir()]
        self.assertEqual(len(reports), 2)
        resumed = next(report for report in reports if report["mode"] == "resume")
        self.assertEqual(resumed["status"], "主库存档采集完成")
        self.assertEqual(resumed["count"], 1)
        self.assertEqual(resumed["incremental"]["inherited_messages"], 1)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_export_asks_report_option_before_final_confirmation(self):
        """报告选项由当前导出明确确认，默认不生成且不会增加读取授权。"""
        for include_analysis in (False, True):
            with self.subTest(include_analysis=include_analysis), \
                    patch("cli.choose_account", return_value=(self.directory, False)), \
                    patch("cli.prepare_source", return_value=(self.source, [self.conversation])), \
                    patch("cli.choose_conversation", return_value=[self.conversation]), \
                    patch("builtins.input", side_effect=["2025-01-01", "2025-01-01", str(self.directory)]), \
                    patch("cli.yes", side_effect=[False, include_analysis, True]) as choices, patch("cli.interactive_collect") as collect:
                cli.interactive_export()
            self.assertIn("总体分析报告", choices.call_args_list[1].args[0])
            self.assertIn("确认开始", choices.call_args_list[2].args[0])
            self.assertEqual(collect.call_args.kwargs["include_analysis"], include_analysis)
            self.assertFalse(collect.call_args.args[5])

    def test_report_option_generates_once_for_last_retry_result(self):
        """重试沿用报告选项，只分析最后一轮，避免为中间失败结果重复生成。"""
        partial = {"status": "已取消，结果不完整", "count": 1}
        complete = {"status": "主库存档采集完成", "count": 1, "exports": {"status": "complete"}}
        results = [(self.directory / "first", partial), (self.directory / "second", complete)]
        with patch("cli.collect", side_effect=results), patch("cli.yes", return_value=True) as consent, patch("cli.export_analysis") as analysis:
            cli.interactive_collect(self.source, [self.conversation], parse_range("2025-01-01", "2025-01-01"), self.directory, include_analysis=True)
        consent.assert_called_once()
        analysis.assert_called_once_with(self.directory / "second", complete, cli.report_error)

    def test_report_option_handles_partial_result_and_default_off(self):
        """拒绝重试仍可分析已有部分结果，未选报告时不执行分析。"""
        partial = {"status": "已取消，结果不完整", "count": 1}
        window = parse_range("2025-01-01", "2025-01-01")
        with patch("cli.collect", return_value=(self.directory, partial)), patch("cli.yes", return_value=False), patch("cli.export_analysis") as analysis:
            cli.interactive_collect(self.source, [self.conversation], window, self.directory, include_analysis=True)
        analysis.assert_called_once_with(self.directory, partial, cli.report_error)
        with patch("cli.export_analysis", side_effect=AssertionError("默认不得生成报告")), patch("cli.yes", side_effect=AssertionError("成功不得额外询问")):
            cli.interactive_collect(self.source, [self.conversation], window, self.directory / "without-report")

    def test_report_failure_or_cancel_never_restarts_export(self):
        """分析失败或取消只影响报告，不再询问重读微信，也不输出异常正文。"""
        report = {"status": "主库存档采集完成", "count": 1, "exports": {"status": "complete"}}
        for error in (sqlite3.DatabaseError("synthetic-private-marker"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch("cli.collect", return_value=(self.directory, report)) as collect, \
                    patch("cli_local.generate_report", side_effect=error), patch("cli.yes", side_effect=AssertionError("不得重试导出")):
                cli.interactive_collect(self.source, [self.conversation], parse_range("2025-01-01", "2025-01-01"), self.directory, include_analysis=True)
            collect.assert_called_once()
        self.assertNotIn("synthetic-private-marker", self.error.getvalue() + self.output.getvalue())

    def test_cancelled_inline_export_gets_fresh_cancel_event_when_retried(self):
        """取消后仍先保存结果；确认重试时清除上次取消状态并继承记录。"""
        self.source.root = self.directory / "synthetic-source"
        stopped = False

        def cancel_once(text):
            """仅在首轮第二个合成分片前发出安全取消，不产生真实系统信号。"""
            nonlocal stopped
            if "消息分片 2/2" in text and not stopped:
                stopped = True
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

        output = self.directory / "cancel-retry"
        with patch("cli.notify", side_effect=cancel_once), patch("cli.yes", return_value=True) as consent:
            cli.interactive_collect(self.source, [self.conversation], parse_range("2025-01-01", "2025-01-01"), output)
        consent.assert_called_once()
        reports = [json.loads((path / "采集说明.json").read_text(encoding="utf-8")) for path in output.iterdir()]
        self.assertTrue(any("已取消" in report["status"] for report in reports))
        self.assertTrue(any(report["mode"] == "resume" and report["status"] == "主库存档采集完成" for report in reports))

    def test_inline_success_never_prompts_and_decline_does_not_retry(self):
        """成功直接返回；失败默认拒绝时不新增读取或第二个结果目录。"""
        window = parse_range("2025-01-01", "2025-01-01")
        with patch("cli.yes", side_effect=AssertionError("成功不应询问重试")):
            cli.interactive_collect(self.source, [self.conversation], window, self.directory / "success")
        self.source.fail = True
        with patch("builtins.input", return_value=""):
            cli.interactive_collect(self.source, [self.conversation], window, self.directory / "decline")
        self.assertEqual(len(list((self.directory / "decline").iterdir())), 1)

    def test_inline_media_failure_can_retry_without_granting_memory(self):
        """准备媒体失败也原页询问，重试不把默认关闭的内存权限改为允许。"""
        report = {"status": "主库存档采集完成", "count": 0, "exports": {"status": "complete"}}
        with patch.object(self.source, "prepare", create=True, side_effect=[RuntimeError("synthetic-private-marker"), None]) as prepare, \
                patch("cli.collect", return_value=(self.directory, report)) as collect, patch("cli.yes", return_value=True) as consent:
            cli.interactive_collect(self.source, [self.conversation], parse_range("2025-01-01", "2025-01-01"), self.directory, media=True)
        consent.assert_called_once()
        collect.assert_called_once()
        self.assertTrue(all(call.kwargs["read_memory"] is False for call in prepare.call_args_list))
        self.assertNotIn("synthetic-private-marker", self.error.getvalue() + self.output.getvalue())

    def test_inline_batch_retry_reuses_previous_plan(self):
        """批量失败沿用上一轮清单，下一轮取消标记必须为未设置。"""
        other = Conversation("other@chatroom", "另一合成群", "群聊")
        plans = [(self.directory / "first", {"status": "已取消", "tasks": [{"complete": False}]}),
                 (self.directory / "second", {"status": "已完成", "tasks": [{"complete": True}]})]
        with patch("cli.run_batch", side_effect=plans) as batch, patch("cli.yes", return_value=True) as consent:
            cli.interactive_collect(self.source, [self.conversation, other], parse_range("2025-01-01", "2025-01-01"), self.directory)
        consent.assert_called_once()
        self.assertEqual(batch.call_count, 2)
        self.assertEqual(batch.call_args.kwargs["previous"], self.directory / "first")
        self.assertEqual(batch.call_args.kwargs["mode"], "resume")
        self.assertIsNot(batch.call_args_list[0].args[5], batch.call_args_list[1].args[5])
        self.assertFalse(batch.call_args.args[5].is_set())

    def test_prepare_failure_can_retry_in_place_without_expanding_authorization(self):
        """会话准备失败时直接重试原账号，用户拒绝导出后不读取正文。"""
        with patch("cli.choose_account", return_value=(self.directory, False)), \
                patch("cli.prepare_source", side_effect=[RuntimeError("合成故障"), (self.source, [self.conversation])]) as prepare, \
                patch("cli.retry_confirm", return_value=True) as consent, patch("cli.choose_conversation", return_value=None):
            cli.interactive_export()
        consent.assert_called_once()
        self.assertEqual(prepare.call_count, 2)
        self.assertTrue(all(call.args[1] is False for call in prepare.call_args_list))

    def test_retry_interrupt_and_removed_menu_entry_do_not_read(self):
        """重试询问中再次取消视为返回，旧菜单编号不能触发读取。"""
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption=interruption), patch("builtins.input", side_effect=interruption):
                self.assertFalse(cli.retry_confirm("是否重试"))
        self.assertNotIn("增量与续导", render_menu())
        with patch("builtins.input", side_effect=["4", "", "0"]), patch("cli.interactive_models") as models, patch("cli.choose_account", side_effect=AssertionError("模型设置不得读取微信")):
            self.assertEqual(cli.interactive(), 0)
        models.assert_called_once()

    def test_missing_process_returns_before_account_or_record_read(self):
        """微信未运行时，用户返回不能先读取历史目录或密钥。"""
        with patch("cli.own_processes", side_effect=ReadError("PROCESS_NOT_RUNNING")), \
                patch("cli.find_accounts", side_effect=AssertionError("不应发现账号")), \
                patch("cli.prepare_source", side_effect=AssertionError("不应读取记录")), \
                patch("builtins.input", return_value="0"):
            cli.interactive_export()

    def test_process_refresh_happens_before_account_discovery(self):
        """手动刷新后才列账号，不在缺进程时自动循环或采集。"""
        with patch("cli.own_processes", side_effect=[ReadError("PROCESS_NOT_RUNNING"), [(123, 1)]]) as processes, \
                patch("cli.find_accounts", return_value=[self.directory]) as accounts, \
                patch("builtins.input", side_effect=["r", "1"]):
            self.assertEqual(cli.choose_account(), (self.directory, True))
        self.assertEqual(processes.call_count, 2)
        accounts.assert_called_once()

    def test_offline_history_never_offers_memory_authorization(self):
        """离线入口可以读取已有缓存，但不能继承在线读取授权。"""
        def consent(prompt):
            """其他选项均拒绝，任何内存授权提示都会使测试失败。"""
            self.assertNotIn("内存", prompt)
            self.assertNotIn("密钥", prompt)
            return False
        answers = ["l", "1", "", "1", "", "2025-01-01", str(self.directory)]
        with patch("cli.own_processes", side_effect=ReadError("PROCESS_NOT_RUNNING")), \
                patch("cli.find_accounts", return_value=[self.directory]), \
                patch("cli.prepare_source", return_value=(self.source, [self.conversation])) as prepare, \
                patch("cli.collect", side_effect=AssertionError("未确认不能开始任务")), \
                patch("cli.yes", side_effect=consent), patch("builtins.input", side_effect=answers):
            cli.interactive_export()
        self.assertFalse(prepare.call_args.args[1])
        self.assertIn("离线模式", self.output.getvalue())

    def test_account_inventory_distinguishes_local_history(self):
        """无微信进程时，显式目录清单不能把历史账号标成在线。"""
        with patch("cli.own_processes", side_effect=ReadError("PROCESS_NOT_RUNNING")), \
                patch("cli.find_accounts", return_value=[self.directory]), \
                patch("cli.WechatSource", side_effect=AssertionError("清单不应读取库")):
            self.assertEqual(cli.main(["accounts"]), 0)
        self.assertIn("本地历史", self.output.getvalue())

    def test_paging_filter_and_duplicate_name_number(self):
        items = [Conversation(f"synthetic-{index}", "重名", "个人") for index in range(13)]
        with patch("builtins.input", side_effect=["", "n", "13"]):
            self.assertIs(cli.choose_conversation(items), items[12])
        with patch("builtins.input", side_effect=["无匹配", "/重名", "2"]):
            self.assertIs(cli.choose_conversation(items), items[1])

    def test_control_characters_do_not_reach_terminal(self):
        self.assertEqual(cli.label("正常\x1b[2J\n\r\t名称"), "正常 [2J   名称")

    def test_cancel_handler_sets_event_and_restores_signal(self):
        previous = signal.getsignal(signal.SIGINT)
        with cli.interruptible() as event:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(event.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        with patch("cli.prepare_source", side_effect=Cancelled):
            self.assertEqual(cli.main(self.arguments()), 130)

    def test_partial_task_has_nonzero_exit_and_keeps_files(self):
        self.source.fail = True
        with patch("cli.prepare_source", return_value=(self.source, [self.conversation])):
            self.assertEqual(cli.main(self.arguments()), 3)
        self.assertTrue(next((self.directory / "exports").iterdir()).joinpath("records.sqlite3").is_file())

    def test_error_does_not_print_exception_secrets(self):
        with patch("cli.prepare_source", side_effect=RuntimeError("synthetic-private-marker")):
            self.assertEqual(cli.main(self.arguments()), 2)
        self.assertNotIn("synthetic-private-marker", self.error.getvalue() + self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
