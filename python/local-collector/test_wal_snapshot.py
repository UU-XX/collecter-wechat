# -*- coding: utf-8 -*-
"""用真正由 SQLite 生成再加密的样本验证提交边界、页校验和原文件保护。"""

import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bootstrap import Cancelled
from read_errors import ReadError
from wal_selftest import fixture_paths, wal_self_test
from wal_snapshot import open_archive, wal_checksum


def checksums(data, endian=None):
    """测试构造特定日志状态时只重写校验和，保留加密页面与认证信息。"""
    data = bytearray(data)
    endian = endian or ("<" if struct.unpack_from(">I", data)[0] == 0x377F0682 else ">")
    seed = wal_checksum(data[:24], endian=endian)
    struct.pack_into(">II", data, 24, *seed)
    for offset in range(32, len(data) - 4119, 4120):
        seed = wal_checksum(data[offset:offset + 8] + data[offset + 24:offset + 4120], seed, endian)
        struct.pack_into(">II", data, offset + 16, *seed)
    return data


class WalSnapshotTests(unittest.TestCase):
    """所有来源都是本测试独占的加密假数据，系统进程接口不参与。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root, self.run = self.base / "account", self.base / "run"
        (self.root / "contact").mkdir(parents=True)
        self.run.mkdir()
        main, log = fixture_paths()
        self.main = self.root / "contact/contact.db"
        self.wal = Path(str(self.main) + "-wal")
        self.main.write_bytes(main.read_bytes())
        self.wal.write_bytes(log.read_bytes())
        self.keys = {"contact/contact.db": {"enc_key": bytes(range(32)).hex()}}

    def tearDown(self):
        """仅释放测试对象创建的临时目录。"""
        self.temp.cleanup()

    def read(self, **options):
        """只打开合成的固定测试账号。"""
        return open_archive(self.run, "contact/contact.db", source_root=self.root, keys=self.keys, **options)

    def values(self):
        """连接总会关闭，返回查询结果与可公开的校验元数据。"""
        connection, metadata = self.read()
        try:
            return connection.execute("SELECT value FROM sample ORDER BY rowid").fetchall(), metadata
        finally:
            connection.close()

    def test_committed_wal_record_and_readonly_source_are_verified(self):
        """独立冻结验收同样用于源码回归，覆盖仅存在于日志的新记录。"""
        wal_self_test(self.base / "self-test")

    def test_absent_log_keeps_committed_main_database(self):
        """没有日志时也做逐页认证并明确记录日志已检查。"""
        self.wal.unlink()
        rows, metadata = self.values()
        self.assertEqual(rows, [("main-only",)])
        self.assertTrue(metadata["wal_checked"])
        self.assertFalse(metadata["wal_merged"])

    def test_active_rollback_journal_is_not_ignored(self):
        """存在未清零回滚日志时拒绝伪造已提交快照，也不删除日志。"""
        journal = Path(str(self.main) + "-journal")
        journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(24))
        before = journal.read_bytes()
        with self.assertRaises(ReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "SNAPSHOT_BUSY")
        self.assertEqual(journal.read_bytes(), before)
        journal.write_bytes(bytes(32))
        self.assertEqual(len(self.values()[0]), 2)

    def test_big_endian_checksum_is_supported(self):
        """另一种标准校验字节序仍需通过加密页认证。"""
        data = bytearray(self.wal.read_bytes())
        struct.pack_into(">I", data, 0, 0x377F0683)
        self.wal.write_bytes(checksums(data, ">"))
        self.assertEqual(len(self.values()[0]), 2)

    def test_pending_transaction_does_not_override_committed_page(self):
        """未提交页故意使用旧内容，错误合并将导致新增记录丢失。"""
        data = self.wal.read_bytes()
        frame = struct.pack(">II", 2, 0) + data[16:24] + bytes(8) + self.main.read_bytes()[4096:8192]
        self.wal.write_bytes(checksums(data + frame))
        rows, metadata = self.values()
        self.assertEqual(rows, [("main-only",), ("wal-only",)])
        self.assertEqual(metadata["wal_pending_frames"], 1)

    def test_no_commit_uses_main_and_stale_salt_tail_is_ignored(self):
        """没有提交标记不应用日志，重置后的旧盐尾部按 SQLite 规则排除。"""
        original = self.wal.read_bytes()
        data = bytearray(original)
        for offset in range(32, len(data), 4120):
            struct.pack_into(">I", data, offset + 4, 0)
        self.wal.write_bytes(checksums(data))
        self.assertEqual(self.values()[0], [("main-only",)])
        self.wal.write_bytes(original + bytes(4120))
        rows, metadata = self.values()
        self.assertEqual(len(rows), 2)
        self.assertEqual(metadata["wal_tail_bytes"], 4120)

    def test_bad_frame_checksum_never_falls_back_to_main(self):
        """完整帧校验失败必须中止，不把旧主库当作最新结果。"""
        data = bytearray(self.wal.read_bytes())
        data[70] ^= 1
        self.wal.write_bytes(data)
        with self.assertRaises(ReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "WAL_INVALID")

    def test_valid_log_checksum_cannot_bypass_page_authentication(self):
        """重算日志校验仍不能掩盖加密页内容被修改。"""
        data = bytearray(self.wal.read_bytes())
        data[80] ^= 1
        self.wal.write_bytes(checksums(data))
        with self.assertRaises(ReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "PAGE_AUTH")

    def test_truncated_header_is_rejected_and_partial_frame_is_excluded(self):
        """缺失头部不能用于结论；已提交日志之后的未完成帧不进入快照。"""
        original = self.wal.read_bytes()
        self.wal.write_bytes(original[:10])
        with self.assertRaises(ReadError):
            self.read()
        self.wal.write_bytes(original + b"partial")
        self.assertEqual(self.values()[1]["wal_tail_bytes"], 7)

    def test_changing_source_and_cancel_are_bounded(self):
        """持续变化三次后失败，预先取消时不复制文件。"""
        import wal_snapshot
        original = wal_snapshot._digest

        def changing(path, cancel=None, destination=None):
            """只改变本测试自己的来源元数据，模拟复制过程中生产者写入。"""
            result = original(path, cancel, destination)
            if path == self.main:
                state = path.stat()
                os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1000000))
            return result

        with patch("wal_snapshot._digest", side_effect=changing), self.assertRaises(ReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, "SNAPSHOT_BUSY")
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.read(cancel=event)
