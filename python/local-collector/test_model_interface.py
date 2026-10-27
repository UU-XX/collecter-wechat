# -*- coding: utf-8 -*-
"""只使用本机临时模拟接口验证协议、认证、密钥保护及手动触发边界。"""

import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.error import URLError
from unittest.mock import patch

import cli
import cli_models
from model_client import ModelClient
from model_selftest import SYNTHETIC_KEY, synthetic_api, synthetic_config
from model_settings import ModelError, PROVIDERS, load_config, normalize_url, save_config, validate_config


class ModelInterfaceTests(unittest.TestCase):
    """各厂商协议仅对合成HTTP响应验收，不证明真实账户或模型权限。"""

    def test_five_protocols_list_and_generate_real_local_http(self):
        """真实回环HTTP覆盖请求路径、认证头、分页和固定测试正文。"""
        with synthetic_api() as server:
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    server.requests.clear()
                    client = ModelClient(synthetic_config(server, protocol))
                    self.assertEqual(client.list_models(), {"models": ["synthetic-a", "synthetic-b"], "complete": True})
                    self.assertTrue(client.test()["available"])
                    request = server.requests[-1]
                    header = "x-api-key" if protocol == "anthropic" else "x-goog-api-key" if protocol == "gemini" else "Authorization"
                    headers = {name.lower(): value for name, value in request["headers"].items()}
                    self.assertIn(SYNTHETIC_KEY, headers[header.lower()])
                    body = json.dumps(request["body"], ensure_ascii=False)
                    self.assertIn("连通性测试", body)
                    self.assertNotIn("聊天正文", body)
                    if protocol == "responses":
                        self.assertIs(request["body"]["store"], False)

    def test_redirect_never_forwards_authentication(self):
        """重定向必须失败，禁止把认证头继续发送到返回地址。"""
        with synthetic_api() as server:
            config = synthetic_config(server)
            config["url"] = server.url + "/redirect"
            with self.assertRaises(ModelError) as caught:
                ModelClient(config).list_models()
            self.assertEqual(caught.exception.code, "REDIRECT")
            self.assertEqual(len(server.requests), 1)

    def test_fixed_errors_hide_response_and_no_automatic_retry(self):
        """认证、限流及服务端错误都不能泄露响应正文或重复计费请求。"""
        with synthetic_api() as server:
            for variant, code in (("auth", "AUTH"), ("rate", "RATE_LIMIT"), ("error", "RESPONSE")):
                with self.subTest(variant=variant):
                    server.variant = variant
                    server.requests.clear()
                    client = ModelClient(synthetic_config(server))
                    with self.assertRaises(ModelError) as caught:
                        client.list_models() if variant == "auth" else client.test()
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))
                    self.assertEqual(len(server.requests), 1)

    def test_incomplete_empty_and_malformed_output_are_not_marked_available(self):
        """有HTTP响应不等于模型可用，未完成输出和异常结构都要明确失败。"""
        with synthetic_api() as server:
            for protocol in ("chat", "responses", "anthropic", "gemini", "ollama"):
                for variant in ("incomplete", "empty", "malformed"):
                    with self.subTest(protocol=protocol, variant=variant):
                        server.variant = variant
                        with self.assertRaises(ModelError):
                            ModelClient(synthetic_config(server, protocol)).test()

    def test_timeout_is_static_and_bounded(self):
        """超时只返回固定错误，测试不等待真实网络或连接远程主机。"""
        config = {"provider": "custom", "protocol": "chat", "url": "https://synthetic.invalid/v1", "api_key": SYNTHETIC_KEY, "model": "synthetic"}
        client = ModelClient(config)
        with patch.object(client.opener, "open", side_effect=URLError(socket.timeout(SYNTHETIC_KEY))) as request:
            with self.assertRaises(ModelError) as caught:
                client.test()
        self.assertEqual(caught.exception.code, "TIMEOUT")
        request.assert_called_once()
        self.assertLessEqual(request.call_args.kwargs["timeout"], 60)

    def test_oversized_response_and_key_echo_are_not_published(self):
        """过大响应被拒绝，正常文本若回显密钥也必须遮蔽。"""
        config = {"provider": "custom", "protocol": "chat", "url": "https://synthetic.invalid/v1", "api_key": SYNTHETIC_KEY, "model": "synthetic"}
        client = ModelClient(config)
        with patch("model_client.MAX_RESPONSE", 64), patch.object(client.opener, "open", return_value=io.BytesIO(b"x" * 65)), self.assertRaises(ModelError) as caught:
            client.list_models()
        self.assertEqual(caught.exception.code, "RESPONSE_SIZE")
        self.assertEqual(caught.exception.details["stage"], "response_body")
        with patch.object(client, "request", return_value={"choices": [{"message": {"content": SYNTHETIC_KEY}, "finish_reason": "stop"}]}):
            self.assertNotIn(SYNTHETIC_KEY, client.generate("合成文字"))

    def test_catalog_limit_is_explicit(self):
        """截断目录必须标为未完整，不把未读取的型号当作不存在。"""
        with synthetic_api() as server:
            server.variant = "many"
            result = ModelClient(synthetic_config(server)).list_models()
        self.assertEqual(len(result["models"]), 1000)
        self.assertFalse(result["complete"])

    def test_url_validation_preserves_prefix_and_rejects_credentials(self):
        """不猜测网关版本，不接收查询密钥、用户口令或明文远程认证地址。"""
        self.assertEqual(normalize_url("https://example.invalid/gateway/v4/chat/completions", "chat"), "https://example.invalid/gateway/v4")
        self.assertEqual(normalize_url("https://example.invalid/v1beta/models/test:generateContent", "gemini"), "https://example.invalid/v1beta")
        self.assertEqual(normalize_url("http://127.0.0.1:11434/api/chat", "ollama"), "http://127.0.0.1:11434")
        for url in ("https://user:password@example.invalid/v1", "https://example.invalid/v1?key=secret", "https://example.invalid/#secret", "file:///tmp/model", "http://example.invalid/v1", "https://example.invalid/\n"):
            with self.subTest(url=url), self.assertRaises(ModelError):
                normalize_url(url, "chat")
        for provider, _, url, protocol in PROVIDERS:
            if url:
                self.assertEqual(normalize_url(url, protocol), url)

    def test_config_is_dpapi_encrypted_and_corrupt_original_is_preserved(self):
        """使用独立临时配置验证当前用户保护，禁止明文密钥与损坏回退。"""
        with tempfile.TemporaryDirectory() as temporary, synthetic_api() as server:
            path = Path(temporary) / "model.dat"
            config = synthetic_config(server)
            save_config(config, path)
            self.assertEqual(load_config(path), config)
            self.assertNotIn(SYNTHETIC_KEY.encode(), path.read_bytes())
            path.write_bytes(b"synthetic-invalid")
            with self.assertRaises(ModelError) as caught:
                load_config(path)
            self.assertEqual(caught.exception.code, "SECRET")
            self.assertEqual(path.read_bytes(), b"synthetic-invalid")

    def test_provider_default_url_can_be_accepted_or_overridden_without_network(self):
        """先选厂商自动预填URL，修改地址后不继承旧密钥或旧模型。"""
        old = {"provider": "deepseek", "protocol": "chat", "url": "https://api.deepseek.com", "api_key": SYNTHETIC_KEY, "model": "synthetic-a"}
        for value, expected, retained in (("", old["url"], True), ("https://other.invalid/v1", "https://other.invalid/v1", False)):
            with self.subTest(value=value), patch("cli_models.load_config", return_value=old), patch("cli_models.read_secret", return_value=""), \
                    patch("builtins.input", side_effect=["2", value, ""]), patch("cli_models.save_config") as save, \
                    patch("cli_models.ModelClient", side_effect=AssertionError("保存不得联网")), redirect_stdout(io.StringIO()):
                cli_models.configure_model()
            self.assertEqual(save.call_args.args[0]["url"], expected)
            self.assertEqual(save.call_args.args[0]["api_key"], SYNTHETIC_KEY if retained else "")
            self.assertEqual(save.call_args.args[0]["model"], "synthetic-a" if retained else "")

    def test_show_and_home_never_expose_key_or_start_network(self):
        """查看配置和首页只做本地显示，密钥及控制序列不可出现在终端。"""
        with synthetic_api() as server:
            config = synthetic_config(server)
        output = io.StringIO()
        with patch("cli_models.ModelClient", side_effect=AssertionError("不得联网")), redirect_stdout(output), patch("cli_models.load_config", return_value=config):
            self.assertEqual(cli.main(["model", "show"]), 0)
            self.assertEqual(cli.main(["--logo"]), 0)
            with patch("builtins.input", return_value="0"):
                cli_models.interactive_models()
        self.assertNotIn(SYNTHETIC_KEY, output.getvalue())
        self.assertEqual(cli_models.safe_text("\x1b\n" + SYNTHETIC_KEY, config), "  [已隐藏密钥]")

    def test_manual_model_selection_does_not_require_catalog_support(self):
        """模型列表不开放时可直接保存ID，保存操作不擅自验证或调用模型。"""
        with synthetic_api() as server:
            config = synthetic_config(server)
        with patch("cli_models.save_config") as save, patch("cli_models.ModelClient", side_effect=AssertionError("不得联网")), redirect_stdout(io.StringIO()):
            cli_models.select_model(config, "custom-model")
        self.assertEqual(save.call_args.args[0]["model"], "custom-model")

    def test_model_commands_never_prepare_wechat(self):
        """目录和连通性命令只访问显式模拟接口，不枚举微信或导出目录。"""
        with synthetic_api() as server, patch("cli_models.load_config", return_value=synthetic_config(server)), \
                patch("cli.prepare_source", side_effect=AssertionError("不得读取微信")), patch("cli.own_processes", side_effect=AssertionError("不得查询微信")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["model", "models"]), 0)
            self.assertEqual(cli.main(["model", "test"]), 0)

    def test_summary_consent_is_per_task_default_no_and_does_not_call_model(self):
        """已有有效配置仍不能默认发送聊天，确认动作本身也不调用模型。"""
        with synthetic_api() as server:
            config = synthetic_config(server)
        for answer, accepted in (("", False), ("n", False), ("y", True)):
            with self.subTest(answer=answer), patch("cli_models.load_config", return_value=config), patch("builtins.input", return_value=answer), \
                    patch("cli_models.ModelClient", side_effect=AssertionError("确认阶段不得联网")), redirect_stdout(io.StringIO()):
                result = cli_models.choose_summary_config()
            self.assertEqual(result, config if accepted else None)
