# -*- coding: utf-8 -*-
"""使用合成会话验证采集边界，不读取真实个人聊天。"""

import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from collector_core import SHANGHAI, collect, parse_range
from wechat_source import Conversation


class FakeSource:
    """用两个合成分片模拟同一会话和一个完全无关的会话。"""

    contact_metadata = {}
    names = {"member": "测试成员", "self": "测试本人"}

    def __init__(self, conversation, rows, second=None, fail=False):
        self.conversation = conversation
        self.rows = [rows, second or []]
        self.fail = fail

    def shards(self):
        return ["message/message_0.db", "message/message_1.db"]

    @contextmanager
    def open_db(self, shard, cancel=None):
        index = int(shard[-4])
        if self.fail and index:
            raise RuntimeError("合成缺密钥")
        connection = sqlite3.connect(":memory:")
        table = "Msg_" + hashlib.md5(self.conversation.username.encode()).hexdigest()
        connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id VALUES (?)", [("member",), ("self",)])
        connection.execute(f'CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, WCDB_CT_message_content INTEGER, message_content BLOB)')
        connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', self.rows[index])
        # 无关表即便含有相同时间的消息，也不得进入结果。
        connection.execute("CREATE TABLE Msg_unrelated (message_content TEXT)")
        connection.execute("INSERT INTO Msg_unrelated VALUES ('不应被采集的内容')")
        try:
            yield connection, {"wal_bytes_not_merged": 128}
        finally:
            connection.close()


class CollectorTests(unittest.TestCase):
    """覆盖真实风险：范围漏采、误去重、身份猜测和不完整结果。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.time = int(dt.datetime(2025, 1, 1, tzinfo=SHANGHAI).timestamp())
        self.group = Conversation("room@chatroom", "合成群", "群聊")
        self.window = parse_range("2025-01-01", "2025-01-01")

    def tearDown(self):
        # 临时测试目录由标准库创建，仅清理由该对象持有的目录。
        self.temp.cleanup()

    def test_inclusive_date_and_second(self):
        self.assertEqual(self.window.end - self.window.start, 86400)
        exact = parse_range("2025/1/1 08:30:00", "2025/1/1 08:30:00")
        self.assertEqual(exact.end - exact.start, 1)
        self.assertEqual(parse_range("2024-02-29", "2024-02-29").end - parse_range("2024-02-29", "2024-02-29").start, 86400)
        with self.assertRaises(ValueError):
            parse_range("2025-02-29", "2025-03-01")
        with self.assertRaises(ValueError):
            parse_range("2025-02-01", "2025-01-01")

    def test_all_pages_scope_and_dedup(self):
        rows = [(i, i, 1, self.time + i, 1, 0, "member:\n重复正文") for i in range(1, 1202)]
        rows.extend([(2000, 2000, 1, self.time - 1, 1, 0, "范围之前"), (2001, 2001, 1, self.window.end, 1, 0, "范围之后")])
        source = FakeSource(self.group, rows, [(99, 1, 1, self.time + 1, 1, 0, "member:\n重复正文")])
        directory, report = collect(source, self.group, self.window, self.output)
        self.assertEqual(report["count"], 1201)
        data = [json.loads(line) for line in (directory / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(data), 1201)
        self.assertTrue(all(item["text"] == "重复正文" for item in data))
        self.assertNotIn("不应被采集", (directory / "消息记录.txt").read_text(encoding="utf-8"))

    def test_person_sender_and_literal_prefix(self):
        person = Conversation("member", "个人测试", "个人")
        source = FakeSource(person, [(1, 1, 1, self.time, 1, 0, "member:\n这就是原文"), (2, 2, 1, self.time + 1, 2, 0, "收到")])
        directory, report = collect(source, person, self.window, self.output)
        data = [json.loads(line) for line in (directory / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["sender_name"] for item in data], ["测试成员", "测试本人"])
        self.assertEqual(data[0]["text"], "member:\n这就是原文")

    def test_card_compression_failure_and_missing_shard(self):
        card = "member:\n<msg><appmsg><title><![CDATA[#接龙\n活动报名]]></title></appmsg></msg>"
        source = FakeSource(self.group, [(1, 1, 49, self.time, 1, 0, card), (2, 2, 1, self.time + 1, 1, 4, b"invalid zstd")], fail=True)
        directory, report = collect(source, self.group, self.window, self.output)
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["parse_errors"], 1)
        self.assertEqual(report["status"], "部分分片失败")
        self.assertIn("活动报名", (directory / "消息记录.txt").read_text(encoding="utf-8"))

    def test_cancel_and_distinct_output(self):
        event = threading.Event()
        source = FakeSource(self.group, [(1, 1, 1, self.time, 1, 0, "文字")])
        def notify(text):
            if "2/2" in text:
                event.set()
        directory, report = collect(source, self.group, self.window, self.output, notify, event)
        self.assertEqual(report["status"], "已取消，结果不完整")
        self.assertEqual(report["count"], 1)
        second, _ = collect(source, self.group, self.window, self.output)
        self.assertNotEqual(directory, second)
        self.assertTrue((directory / "消息记录.txt").exists())


if __name__ == "__main__":
    unittest.main()
