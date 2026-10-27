# -*- coding: utf-8 -*-
"""合成目录验证应用容器重定向和临时路径边界，不读取微信。"""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wechat_source import WechatSource


class TemporaryPathTests(unittest.TestCase):
    """原 AppData 别名与文件解析路径不同时仍保留越界保护。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.logical = self.root / "logical-private"
        self.physical = self.root / "redirected-private" / "temporary"
        self.physical.mkdir(parents=True)
        self.marker = self.physical / "location-synthetic"
        self.marker.touch()
        self.source = WechatSource.__new__(WechatSource)
        self.source.root = self.root / "synthetic-account"
        self.source.keys = {}
        self.connection = MagicMock()

    def tearDown(self):
        self.temporary.cleanup()

    def test_redirected_file_parent_is_used_for_snapshot_and_cleanup(self):
        """模拟父目录别名未重定向，而新文件已解析到实际私有目录。"""
        runs = []

        def open_archive(run, relative, *, source_root, keys, cancel=None):
            """检查只在已解析的实际目录中建立本次合成副本。"""
            runs.append(run)
            self.assertEqual(run.parent, self.physical)
            self.assertEqual(relative, "contact/contact.db")
            self.assertEqual(source_root, self.source.root)
            self.assertIs(keys, self.source.keys)
            self.assertFalse((run / "keys.json").exists())
            self.assertFalse((run / "manifest.json").exists())
            return self.connection, {"synthetic": True}

        marker = nullcontext(SimpleNamespace(name=str(self.marker)))
        with patch("wechat_source.PRIVATE", self.logical), patch("wechat_source.tempfile.NamedTemporaryFile", return_value=marker) as create_marker, patch("wechat_source.open_archive", side_effect=open_archive):
            with self.source.open_db("contact/contact.db") as (connection, metadata):
                self.assertIs(connection, self.connection)
                self.assertTrue(metadata["synthetic"])
            self.assertEqual(create_marker.call_args.kwargs["dir"], self.logical / "temporary")
        self.connection.close.assert_called_once()
        self.assertFalse(runs[0].exists())
        self.assertTrue(self.marker.exists())

    def test_actual_escape_is_rejected_before_opening_archive(self):
        """真正越界的快照目录仍被拒绝，不能借兼容性修复绕过保护。"""
        outside = self.root / "outside"
        outside.mkdir()
        marker = nullcontext(SimpleNamespace(name=str(self.marker)))
        with patch("wechat_source.PRIVATE", self.logical), patch("wechat_source.tempfile.NamedTemporaryFile", return_value=marker), patch("wechat_source.tempfile.TemporaryDirectory", return_value=nullcontext(str(outside))), patch("wechat_source.open_archive") as archive:
            with self.assertRaisesRegex(RuntimeError, "临时目录路径校验未通过"):
                with self.source.open_db("contact/contact.db"):
                    self.fail("越界目录不能进入读取阶段。")
        archive.assert_not_called()
        self.assertFalse((outside / "keys.json").exists())

    def test_normal_directory_marker_and_snapshot_are_released(self):
        """普通路径也能读取并关闭，自动释放本次创建的空文件和目录。"""
        with patch("wechat_source.PRIVATE", self.logical), patch("wechat_source.open_archive", return_value=(self.connection, {})):
            with self.source.open_db("contact/contact.db"):
                pass
        self.connection.close.assert_called_once()
        self.assertEqual(list((self.logical / "temporary").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
