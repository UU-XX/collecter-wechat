# -*- coding: utf-8 -*-
"""使用合成会话和隔离结果验证批量、增量及续导，不读取微信。"""

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from collector_core import collect, parse_range
from export_jobs import run_batch, select_conversations
from result_store import task_lease
from workflow_selftest import SyntheticSource, workflow_self_test
from wechat_source import Conversation
import test_media_collect as media_tests


class WorkflowTests(unittest.TestCase):
    """验证真实风险：错账号继承、晚到消息遗漏、重复记录和旧文件被覆盖。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.window = parse_range("2025-01-01", "2025-01-01")
        self.item = Conversation("synthetic@chatroom", "合成群", "群聊")
        self.rows = {self.item.username: [(1, 101, 1, self.window.start + 100, 1, 0, "member:\n初始消息")]}
        self.source = SyntheticSource(self.base / "account", [self.item], self.rows)

    def tearDown(self):
        """仅释放本测试的独占目录。"""
        self.temp.cleanup()

    def run_export(self, **options):
        """常规导出与新功能共用同一入口。"""
        return collect(self.source, self.item, self.window, self.base / "exports", **options)

    def test_complete_workflow_selftest(self):
        """源码和便携包执行相同的完整合成验收。"""
        workflow_self_test(self.base / "self-test")

    def test_incremental_finds_older_late_arrival_and_preserves_original(self):
        """不使用最大时间戳跳读，早于现有记录的晚到消息也能进入新结果。"""
        original, _ = self.run_export()
        before = {path.name: path.read_bytes() for path in original.iterdir()}
        self.rows[self.item.username].append((2, 102, 1, self.window.start + 50, 1, 0, "member:\n晚到消息"))
        updated, report = self.run_export(seed_from=original, mode="incremental")
        self.assertEqual(report["incremental"]["inherited_messages"], 1)
        self.assertEqual(report["incremental"]["new_messages"], 1)
        self.assertEqual(report["count"], 2)
        self.assertEqual(before, {path.name: path.read_bytes() for path in original.iterdir()})
        self.assertNotEqual(original, updated)

    def test_unchanged_incremental_does_not_decode_old_messages_again(self):
        """去重在正文解析之前进行，重复记录不重复处理或生成新媒体。"""
        original, _ = self.run_export()
        with patch("collector_core.decode_text", side_effect=AssertionError("重复消息不应解码")):
            _, report = self.run_export(seed_from=original, mode="incremental")
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["incremental"]["new_messages"], 0)

    def test_wrong_account_conversation_options_and_window_stop_before_source_read(self):
        """所有继承约束先于源分片读取，旧结果始终保持原字节。"""
        original, _ = self.run_export()
        opens = self.source.opens
        self.source.root = self.base / "wrong-account"
        with self.assertRaises(ValueError):
            self.run_export(seed_from=original, mode="resume")
        self.source.root = self.base / "account"
        for changes in ({"include_videos": True}, {"mode": "incremental"}):
            if changes.get("mode") == "incremental":
                old = self.window
                self.window = parse_range("2025-01-01", "2025-01-01 12:00:00")
            with self.assertRaises(ValueError):
                self.run_export(seed_from=original, mode=changes.pop("mode", "resume"), **changes)
            if 'old' in locals():
                self.window = old
        self.assertEqual(self.source.opens, opens)

    def test_cancelled_task_resumes_to_new_directory(self):
        """取消发生在读取后也保留已提交消息，续导不会原地改写。"""
        event = threading.Event()

        def cancel(text):
            """模拟用户在第一个分片读取前请求安全取消。"""
            if "正在读取消息分片" in text:
                event.set()

        original, report = self.run_export(notify=cancel, cancel=event)
        self.assertIn("已取消", report["status"])
        before = (original / "records.sqlite3").read_bytes()
        event.clear()
        updated, result = self.run_export(seed_from=original, mode="resume", cancel=event)
        self.assertEqual(result["count"], 1)
        self.assertNotEqual(updated, original)
        self.assertEqual((original / "records.sqlite3").read_bytes(), before)

    def test_task_lock_and_legacy_missing_identity_are_not_bypassed(self):
        """已有任务仍在使用或旧结果无法证明账号身份时，不猜测或删除锁。"""
        original, _ = self.run_export()
        with task_lease(original), self.assertRaises(ValueError):
            self.run_export(seed_from=original, mode="resume")
        path = original / "采集说明.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop("source_fingerprint")
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_export(seed_from=original, mode="resume")

    def test_task_lease_blocks_another_process(self):
        """另一个受控 Python 子进程不能绕过当前任务的文件锁。"""
        original, _ = self.run_export()
        code = "from result_store import task_lease\nimport sys\ntry:\n with task_lease(sys.argv[1]): pass\nexcept ValueError:\n sys.exit(4)\n"
        with task_lease(original):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(original)], cwd=Path(__file__).parent,
                                    env=dict(os.environ), capture_output=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_all_identifiers_are_validated_and_duplicates_execute_once(self):
        """一个错误编号不能导致前面部分会话先被导出。"""
        self.assertEqual(select_conversations([self.item], [self.item.short_id, self.item.username]), [self.item])
        with self.assertRaises(ValueError):
            select_conversations([self.item], [self.item.short_id, "missing"])

    def test_batch_reuses_bounded_snapshots_and_releases_them(self):
        """两个会话共用同一小分片，预算为零时仍可独立读取而不改变结果。"""
        other = Conversation("other@chatroom", "另一合成群", "群聊")
        self.source.items.append(other)
        self.rows[other.username] = [(1, 201, 1, self.window.start, 1, 0, "member:\n另一消息")]
        _, plan = run_batch(self.source, [self.item, other], self.window, self.base / "batches")
        self.assertEqual(plan["status"], "已完成")
        self.assertEqual(self.source.opens, 1)
        with patch("batch_source.MAX_CACHED_BYTES", 0):
            _, plan = run_batch(self.source, [self.item, other], self.window, self.base / "batches")
        self.assertEqual(plan["status"], "已完成")
        self.assertEqual(self.source.opens, 3)

    def test_completed_batch_incremental_inherits_each_conversation(self):
        """延长批量结束时间时，每个会话独立继承，晚到消息也能进入累计归档。"""
        other = Conversation("other-incremental@chatroom", "另一合成群", "群聊")
        self.source.items.append(other)
        self.rows[other.username] = [(1, 201, 1, self.window.start, 1, 0, "member:\n另一消息")]
        root, _ = run_batch(self.source, [self.item, other], self.window, self.base / "batches")
        before = (root / "批量说明.json").read_bytes()
        self.rows[self.item.username].append((2, 102, 1, self.window.start + 10, 1, 0, "member:\n更早的晚到消息"))
        window = parse_range("2025-01-01", "2025-01-02")
        updated, report = run_batch(self.source, [self.item, other], window, self.base / "batches", previous=root, mode="incremental")
        self.assertEqual(report["status"], "已完成")
        self.assertEqual([task["count"] for task in report["tasks"]], [2, 1])
        self.assertNotEqual(updated, root)
        self.assertEqual((root / "批量说明.json").read_bytes(), before)

    def test_inherited_media_is_verified_and_bad_copy_can_be_retried(self):
        """继承文件摘要不匹配时只修复新结果，原导出和原缓存都保持不变。"""
        helper = media_tests.MediaCollectionTests()
        helper.setUp()
        try:
            original, _, records = helper.run_collect()
            media = next(row["media"] for row in records if row.get("media", {}).get("kind") == "file")
            file = original / media["path"]
            file.write_bytes(b"synthetic-corrupted-export")
            updated, report = collect(helper.source(), helper.conversation, helper.window, helper.base / "updated",
                                      include_videos=True, include_files=True, seed_from=original, mode="resume")
            self.assertTrue(report["media"]["complete"])
            self.assertEqual((updated / media["path"]).read_bytes(), helper.file)
            self.assertEqual(file.read_bytes(), b"synthetic-corrupted-export")
        finally:
            helper.tearDown()
