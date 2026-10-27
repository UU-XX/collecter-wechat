# -*- coding: utf-8 -*-
"""用模拟进程查询和合成缓存验证安全错误提示，不查询真实微信。"""

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import cli
from read_errors import ReadError
from wechat_source import own_processes


class ReadErrorTests(unittest.TestCase):
    """区分进程未启动、查询失败与任意异常，不输出第三方原始内容。"""

    def test_process_not_running_has_specific_code(self):
        with patch("wechat_source.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="[]")):
            with self.assertRaises(ReadError) as caught:
                own_processes()
        self.assertEqual(caught.exception.code, "PROCESS_NOT_RUNNING")

    def test_process_query_failure_never_prints_raw_stderr(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="synthetic-private-marker")
        with patch("wechat_source.subprocess.run", return_value=result), self.assertRaises(ReadError) as caught:
            own_processes()
        self.assertEqual(caught.exception.code, "PROCESS_QUERY")
        self.assertNotIn("synthetic-private-marker", str(caught.exception))

    def test_missing_powershell_timeout_and_malformed_output_are_safe(self):
        for error in (OSError("synthetic-private-marker"), subprocess.TimeoutExpired("synthetic-private-marker", 30)):
            with self.subTest(error=type(error).__name__), patch("wechat_source.subprocess.run", side_effect=error), self.assertRaises(ReadError) as caught:
                own_processes()
            self.assertEqual(caught.exception.code, "PROCESS_QUERY")
        for output in ("synthetic-private-marker", '{}', '[{"pid":0,"memory":1}]', '[{"pid":1}]'):
            with self.subTest(output=output), patch("wechat_source.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)), self.assertRaises(ReadError) as caught:
                own_processes()
            self.assertEqual(caught.exception.code, "PROCESS_QUERY")

    def test_valid_process_query_is_sorted_without_extra_actions(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps([{"pid":123, "memory":1}, {"pid":456, "memory":2}]))
        with patch("wechat_source.subprocess.run", return_value=result) as query:
            self.assertEqual(own_processes(), [(456, 2), (123, 1)])
        self.assertIn("$ErrorActionPreference='Stop'", query.call_args.args[0][-1])

    def test_cli_displays_safe_message_and_recovery_hint(self):
        output = io.StringIO()
        with redirect_stderr(output):
            cli.report_error(ReadError("PROCESS_NOT_RUNNING"))
        self.assertIn("PROCESS_NOT_RUNNING", output.getvalue())
        self.assertIn("请先启动并登录微信", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_unknown_error_code_is_not_echoed(self):
        error = ReadError("synthetic-private-marker")
        output = io.StringIO()
        with redirect_stderr(output):
            cli.report_error(error)
        self.assertEqual(error.code, "READ_PREPARE")
        self.assertNotIn("synthetic-private-marker", output.getvalue())


if __name__ == "__main__":
    unittest.main()
