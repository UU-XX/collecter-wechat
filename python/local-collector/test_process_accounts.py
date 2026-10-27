# -*- coding: utf-8 -*-
"""使用合成目录和本测试进程验证账号占用关联，不访问真实微信。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from process_accounts import account_choices, using_processes


class ProcessAccountTests(unittest.TestCase):
    """进程占用是目录关联证据，未知状态不能被升级为已登录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.roots = []
        for name in ("account-a", "account-b"):
            root = self.directory / name / "db_storage"
            (root / "contact").mkdir(parents=True)
            (root / "contact/contact.db").write_bytes(b"synthetic-database-metadata-only")
            self.roots.append(root)

    def tearDown(self):
        """只清理由本测试持有的临时目录。"""
        self.temp.cleanup()

    def test_linked_account_is_first_and_other_account_is_unconfirmed(self):
        def owners(files):
            """只让第二个合成账号关联到当前允许的模拟进程。"""
            return {123} if files[0].is_relative_to(self.roots[1]) else set()
        with patch("process_accounts.using_processes", side_effect=owners):
            items = account_choices(self.roots, [(123, 10)])
        self.assertEqual(items[0].root, self.roots[1])
        self.assertEqual(items[0].status, "进程正在使用")
        self.assertEqual(items[0].process_ids, (123,))
        self.assertEqual(items[1].status, "归属待确认")

    def test_other_process_is_not_used_to_claim_account_is_active(self):
        with patch("process_accounts.using_processes", return_value={999}):
            items = account_choices(self.roots, [(123, 10)])
        self.assertTrue(all(item.status == "归属待确认" and not item.process_ids for item in items))

    def test_missing_process_does_not_query_file_owners(self):
        with patch("process_accounts.using_processes", side_effect=AssertionError("离线不应查询占用")):
            items = account_choices(self.roots, [])
        self.assertTrue(all(item.status == "本地历史" for item in items))

    def test_unavailable_file_owner_query_is_not_reported_as_online(self):
        with patch("process_accounts.using_processes", side_effect=OSError("synthetic-private-marker")):
            items = account_choices(self.roots, [(123, 10)])
        self.assertTrue(all(item.status == "占用信息不可用" and not item.process_ids for item in items))
        self.assertNotIn("synthetic-private-marker", repr(items))

    def test_metadata_query_does_not_open_database_content(self):
        with patch("process_accounts.using_processes", return_value={123}), \
                patch.object(Path, "open", side_effect=AssertionError("不能打开数据库内容")):
            items = account_choices(self.roots, [(123, 10)])
        self.assertEqual(items[0].status, "进程正在使用")

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件占用查询")
    def test_native_file_query_finds_only_test_process_without_changing_file(self):
        """打开测试自身文件验证真实系统接口，不查询微信进程或微信目录。"""
        marker = self.directory / "synthetic-open-file.db"
        marker.write_bytes(b"synthetic-lock-check")
        with marker.open("rb") as stream:
            self.assertIn(os.getpid(), using_processes([marker]))
            self.assertEqual(stream.read(), b"synthetic-lock-check")
        self.assertEqual(marker.read_bytes(), b"synthetic-lock-check")


if __name__ == "__main__":
    unittest.main()
