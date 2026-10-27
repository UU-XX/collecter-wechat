# -*- coding: utf-8 -*-
"""验证标题状态、配置绑定及终端排版，禁止访问真实配置或模型接口。"""

import io
from pathlib import Path
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import cli
import cli_models
from cli_theme import cell_width, render_header
from model_settings import ModelError
from model_status import load_test_status, save_test_status


class ModelStatusTests(unittest.TestCase):
    """状态只表示过去明确测试的结果，标题展示没有网络副作用。"""

    def setUp(self):
        """使用虚构配置，不从本机模型设置恢复任何数据。"""
        self.config = {"provider": "custom", "protocol": "chat", "url": "https://synthetic.invalid/v1", "api_key": "synthetic-header-key", "model": "synthetic-model"}

    def test_status_encrypted_and_bound_to_every_relevant_config_field(self):
        """密钥、地址、协议、厂商或模型变化都不能借用旧测试结论。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.dat"
            save_test_status(self.config, {"available": True, "latency_ms": 123}, path=path)
            status = load_test_status(self.config, path)
            self.assertTrue(status["available"])
            self.assertEqual(status["latency_ms"], 123)
            self.assertNotIn(self.config["api_key"].encode(), path.read_bytes())
            for key, value in (("model", "other"), ("api_key", "other"), ("url", "https://other.invalid/v1"), ("protocol", "responses"), ("provider", "openai")):
                with self.subTest(field=key):
                    self.assertIsNone(load_test_status({**self.config, key: value}, path))
            save_test_status(self.config, error_code="AUTH", path=path)
            self.assertEqual(load_test_status(self.config, path)["error_code"], "AUTH")
            self.assertFalse(load_test_status(self.config, path)["available"])

    def test_corrupt_status_is_preserved_and_header_does_not_guess_online(self):
        """损坏测试记录不修改原文件，也不能被当作在线或测试通过。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.dat"
            path.write_bytes(b"synthetic-invalid")
            with self.assertRaises(ModelError):
                load_test_status(self.config, path)
            self.assertEqual(path.read_bytes(), b"synthetic-invalid")
        with patch("cli_models.load_config", return_value=self.config), patch("cli_models.load_test_status", side_effect=ModelError("SECRET")):
            self.assertEqual(cli_models.model_header()["status"], "测试记录不可读")

    def test_header_all_local_states_and_secret_redaction(self):
        """首页只读取本地状态，任何分支均不能初始化模型客户端。"""
        with patch("cli_models.ModelClient", side_effect=AssertionError("首页不得联网")):
            with patch("cli_models.load_config", return_value=None):
                self.assertEqual(cli_models.model_header()["status"], "未配置")
            with patch("cli_models.load_config", side_effect=ModelError("SECRET")):
                self.assertEqual(cli_models.model_header()["status"], "配置不可读")
            with patch("cli_models.load_config", return_value={**self.config, "model": ""}):
                self.assertEqual(cli_models.model_header()["status"], "未选择模型")
            config = {**self.config, "model": "x-" + self.config["api_key"]}
            with patch("cli_models.load_config", return_value=config), patch("cli_models.load_test_status", return_value=None):
                header = cli_models.model_header()
                self.assertEqual(header["status"], "未测试")
                self.assertNotIn(self.config["api_key"], header["model"])
            for available in (False, True):
                status = {"available": available, "checked_at": "2026-10-26T15:00:00+08:00", "latency_ms": 15, "error_code": "AUTH"}
                with patch("cli_models.load_config", return_value=self.config), patch("cli_models.load_test_status", return_value=status):
                    header = cli_models.model_header()
                self.assertEqual(header["checked_at"], "10-26 15:00")
                self.assertIn("上次测试通过" if available else "上次测试失败", header["status"])

    def test_test_records_success_and_failure_without_extra_requests(self):
        """状态保存独立于生成请求，测试失败仍保留固定错误码。"""
        result = {"available": True, "latency_ms": 20, "model": self.config["model"]}
        with patch("cli_models.ModelClient") as client, patch("cli_models.save_test_status") as save, redirect_stdout(io.StringIO()):
            client.return_value.test.return_value = result
            cli_models.test_model(self.config)
        client.return_value.test.assert_called_once()
        save.assert_called_once_with(self.config, result, None)
        with patch("cli_models.ModelClient") as client, patch("cli_models.save_test_status") as save, redirect_stdout(io.StringIO()):
            client.return_value.test.side_effect = ModelError("AUTH")
            with self.assertRaises(ModelError):
                cli_models.test_model(self.config)
        client.return_value.test.assert_called_once()
        save.assert_called_once_with(self.config, None, "AUTH")

    def test_header_fits_long_names_wide_narrow_and_plain_output(self):
        """长型号、中英文和控制字符均不能突破终端边界或执行控制序列。"""
        info = {"model": "很长的模型名称" * 12 + "\x1b[2J", "status": "上次测试失败 [RATE_LIMIT]", "checked_at": "10-26 15:00", "level": "error"}
        for color in (False, True):
            for columns in (40, 65, 66, 80, 120):
                with self.subTest(color=color, columns=columns):
                    text = render_header(color, columns, info)
                    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
                    self.assertIn("当前模型", plain)
                    self.assertIn("模型状态", plain)
                    self.assertIn("测试时间", plain)
                    self.assertNotIn("\x1b", plain)
                    for line in plain.splitlines():
                        self.assertLessEqual(cell_width(line), columns)

    def test_returning_to_menu_refreshes_header_but_logo_stays_pure(self):
        """从设置返回后重新显示状态，纯标识命令不加载本地模型配置。"""
        first = {"model": "未设置", "status": "未配置", "level": "muted"}
        second = {"model": "synthetic-model", "status": "未测试", "level": "warning"}
        output = io.StringIO()
        with patch("cli.model_header", side_effect=[first, second]) as header, patch("cli.interactive_models"), \
                patch("builtins.input", side_effect=["4", "", "0"]), redirect_stdout(output):
            self.assertEqual(cli.interactive(), 0)
        self.assertEqual(header.call_count, 2)
        self.assertIn("当前模型：synthetic-model", output.getvalue())
        with patch("cli.model_header", side_effect=AssertionError("纯标识不读取配置")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--logo"]), 0)
