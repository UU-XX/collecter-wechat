# -*- coding: utf-8 -*-
"""仅使用合成密钥验证加密兼容、损坏保护及显式内存授权。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from key_cache import load_keys, save_keys
from wechat_source import WechatSource
from read_errors import ReadError


@unittest.skipUnless(os.name == "nt", "需要 Windows 用户数据保护")
class KeyCacheTests(unittest.TestCase):
    """每个案例使用独立目录，不读取真实账号、缓存或微信进程。"""

    def setUp(self):
        """准备仅含一页零字节的合成数据库和模拟读取工具。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve()
        self.root = self.folder / "account"
        (self.root / "contact").mkdir(parents=True)
        (self.root / "contact/contact.db").write_bytes(bytes(4096))
        self.source = WechatSource(self.root)
        self.source.cache = self.folder / "cache"
        self.relative = str(Path("contact/contact.db"))
        self.value = {"_db_dir": str(self.root), self.relative: {"enc_key": "11" * 32}}
        self.tool = SimpleNamespace(_get_key_info=lambda value, relative: value.get(relative),
                                    verify_enc_key=lambda key, page: key == bytes.fromhex("11" * 32),
                                    _scan_memory_raw_key=Mock())

    def tearDown(self):
        """只清理由本测试创建并持有的目录。"""
        self.temporary.cleanup()

    def test_legacy_cache_is_preserved_and_new_cache_is_encrypted(self):
        """兼容输入只读，新写入文件不能直接出现合成密钥。"""
        self.source.cache.mkdir()
        legacy = self.source.cache / "keys.json"
        legacy.write_text(json.dumps(self.value), encoding="utf-8")
        original = legacy.read_bytes()
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", side_effect=AssertionError("不能扫描微信")):
            self.source.prepare()
        self.assertEqual(legacy.read_bytes(), original)
        encrypted = (self.source.cache / "keys.dat").read_bytes()
        self.assertNotIn(("11" * 32).encode(), encrypted)
        self.assertEqual(load_keys(self.source.cache), self.value)
        self.tool._scan_memory_raw_key.assert_not_called()

    def test_corrupt_encrypted_cache_never_falls_back_or_changes_files(self):
        """加密缓存损坏时不能绕过保护读取旧缓存或自动扫描。"""
        self.source.cache.mkdir()
        encrypted = self.source.cache / "keys.dat"
        legacy = self.source.cache / "keys.json"
        encrypted.write_bytes(b"synthetic-broken-encrypted-cache")
        legacy.write_text(json.dumps(self.value), encoding="utf-8")
        original = {path: path.read_bytes() for path in (encrypted, legacy)}
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", side_effect=AssertionError("不能扫描微信")):
            with self.assertRaisesRegex(RuntimeError, "密钥缓存无法读取"):
                self.source.prepare(read_memory=True)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_missing_cache_does_not_authorize_memory_by_default(self):
        """没有缓存时失败并提示授权，不生成空正式缓存。"""
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes") as processes:
            with self.assertRaisesRegex(RuntimeError, "尚无可用密钥"):
                self.source.prepare()
        processes.assert_not_called()
        self.tool._scan_memory_raw_key.assert_not_called()
        self.assertFalse((self.source.cache / "keys.dat").exists())

    def test_explicit_scan_uses_mock_and_removes_transient_plaintext(self):
        """仅明确授权才调用模拟扫描，结束后只留下加密正式缓存。"""
        def scan(root, destination):
            """模拟既有工具输出，绝不接触进程。"""
            self.assertEqual(root, str(self.root))
            Path(destination).write_text(json.dumps(self.value), encoding="utf-8")

        self.tool._scan_memory_raw_key.side_effect = scan
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", return_value=[(123, 1)]) as processes:
            self.source.prepare(read_memory=True)
        processes.assert_called_once()
        self.tool._scan_memory_raw_key.assert_called_once()
        self.assertEqual(load_keys(self.source.cache), self.value)
        self.assertEqual([path.name for path in self.source.cache.iterdir()], ["keys.dat"])

    def test_encrypted_round_trip_and_failed_rewrite_preserve_previous(self):
        """系统加密失败前不能覆盖已保存的可用缓存。"""
        save_keys(self.source.cache, self.value)
        path = self.source.cache / "keys.dat"
        original = path.read_bytes()
        with patch("key_cache._protect", side_effect=RuntimeError("合成加密失败")):
            with self.assertRaises(RuntimeError):
                save_keys(self.source.cache, self.value)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(load_keys(self.source.cache), self.value)

    def test_scan_without_matching_keys_has_safe_error_and_keeps_cache(self):
        """扫描器异常不能暴露内容，原有缓存和私密信息均不得被改写或输出。"""
        self.tool._scan_memory_raw_key.side_effect = RuntimeError("synthetic-private-marker")
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", return_value=[(123, 1)]):
            with self.assertRaises(ReadError) as caught:
                self.source.prepare(read_memory=True)
        self.assertEqual(caught.exception.code, "KEY_SCAN_FAILED")
        self.assertNotIn("synthetic-private-marker", str(caught.exception))
        self.assertFalse((self.source.cache / "keys.dat").exists())
        self.assertFalse(list(self.source.cache.glob("keys.pending-*")))

    def test_all_process_open_failures_are_distinguished_from_key_mismatch(self):
        """只识别固定失败事件，不把原始扫描输出传给用户。"""
        def denied(root, destination):
            """模拟读取组件无法打开进程，不进行任何系统调用。"""
            self.tool._print("[WARN] 无法打开进程 PID=123，跳过（尝试以管理员身份运行）")
            self.tool._print("synthetic-private-marker")
            raise RuntimeError("synthetic-private-marker")
        self.tool._scan_memory_raw_key.side_effect = denied
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", return_value=[(123, 1)]):
            with self.assertRaises(ReadError) as caught:
                self.source.prepare(read_memory=True)
        self.assertEqual(caught.exception.code, "PROCESS_ACCESS")
        self.assertNotIn("synthetic-private-marker", str(caught.exception))

    def test_scanner_result_is_reverified_before_saving(self):
        """第三方返回格式正确但验证不通过的密钥，不能成为正式缓存。"""
        def scan(root, destination):
            """只写入合成的错误候选，模拟读取组件结果异常。"""
            Path(destination).write_text(json.dumps({"_db_dir": str(self.root), self.relative: {"enc_key": "22" * 32}}), encoding="utf-8")
        self.tool._scan_memory_raw_key.side_effect = scan
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", return_value=[(123, 1)]):
            with self.assertRaises(ReadError) as caught:
                self.source.prepare(read_memory=True)
        self.assertEqual(caught.exception.code, "CONTACT_KEY_UNVERIFIED")
        self.assertFalse((self.source.cache / "keys.dat").exists())

    def test_incomplete_database_header_stops_before_memory_access(self):
        """源文件不是完整首页时明确失败，不能转去扫描进程。"""
        (self.root / "contact/contact.db").write_bytes(b"incomplete")
        with patch("wechat_source.load_tool", return_value=self.tool), patch("wechat_source.own_processes", side_effect=AssertionError("不能读进程")):
            with self.assertRaises(ReadError) as caught:
                self.source.prepare(read_memory=True)
        self.assertEqual(caught.exception.code, "DATABASE_FILES")


if __name__ == "__main__":
    unittest.main()
