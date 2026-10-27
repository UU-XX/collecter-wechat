# -*- coding: utf-8 -*-
"""离线合成会话用于冻结验收，不读取账号配置、真实消息或进程。"""

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from collector_core import collect, parse_range
from export_jobs import run_batch
from local_analysis import analyze_result, browse_records
from analysis_report import generate_report
from cli_local import export_analysis
from wechat_source import Conversation


class SyntheticSource:
    """只根据调用方传入的测试行建立内存分片，没有任何操作系统读取入口。"""

    contact_metadata = {"synthetic": True}
    names = {"member": "示例参与者", "second": "示例参与者"}

    def __init__(self, root, conversations, rows):
        self.root, self.items, self.rows = Path(root), conversations, rows
        self.opens = 0

    def shards(self):
        """固定一份合成分片，覆盖元数据明确标注为模拟。"""
        return ["message/message_0.db"]

    @contextmanager
    def open_db(self, relative, cancel=None):
        """在内存建表，不打开 root 指向的任何文件。"""
        self.opens += 1
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id VALUES (?)", [(name,) for name in self.names])
        for item in self.items:
            table = "Msg_" + hashlib.md5(item.username.encode()).hexdigest()
            connection.execute(f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,WCDB_CT_message_content INTEGER,message_content TEXT)')
            connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', self.rows.get(item.username, []))
        try:
            yield connection, {"synthetic": True, "snapshot_checked": True, "wal_checked": True, "wal_merged": True, "wal_bytes_not_merged": 0}
        finally:
            connection.close()


def workflow_self_test(directory):
    """验证晚到消息增量、分页分析和取消后的批量续导，全部来源均为合成。"""
    directory = Path(directory)
    first = Conversation("synthetic-one@chatroom", "合成群聊", "群聊")
    second = Conversation("synthetic-two@chatroom", "另一合成群聊", "群聊")
    window = parse_range("2025-01-01", "2025-01-01")
    rows = {first.username: [(1, 101, 1, window.start + 100, 1, 0, "member:\n合成初始消息")],
            second.username: [(1, 201, 1, window.start + 200, 1, 0, "member:\n另一合成消息")]}
    source = SyntheticSource(directory / "synthetic-workflow-source", [first, second], rows)
    original, _ = collect(source, first, window, directory / "synthetic-workflow")
    before = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in original.iterdir() if path.is_file()}
    rows[first.username].append((2, 102, 1, window.start + 50, 2, 0, "second:\n合成晚到消息"))
    updated, report = collect(source, first, window, directory / "synthetic-workflow", seed_from=original, mode="incremental")
    assert report["count"] == 2 and report["incremental"]["new_messages"] == 1
    assert before == {path.name: hashlib.sha256(path.read_bytes()).digest() for path in original.iterdir() if path.is_file()}
    assert browse_records(updated, search="晚到")["total"] == 1
    analysis = analyze_result(updated)
    assert analysis["observed_participants"] == 2 and analysis["total"] == 2
    generate_report(updated, directory / "synthetic-reports")
    event = threading.Event()

    def stop(text):
        """只在第二个模拟会话前取消，首个任务必须保留。"""
        if "正在导出会话 2/2" in text:
            event.set()

    batch, plan = run_batch(source, [first, second], window, directory / "synthetic-batches", stop, event)
    assert plan["status"] == "已取消" and plan["tasks"][0]["complete"]
    event.clear()
    resumed_root, resumed = run_batch(source, [first, second], window, directory / "synthetic-batches", cancel=event, previous=batch)
    assert resumed["status"] == "已完成"
    failures = []
    export_analysis(resumed_root, resumed, failures.append)
    assert not failures and len(list(resumed_root.glob("*/report-*/分析报告.html"))) == 2
