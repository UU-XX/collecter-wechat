# -*- coding: utf-8 -*-
"""固定合成加密主库和日志验收，不查询微信进程或用户配置。"""

import hashlib
import sqlite3
import sys
from pathlib import Path

from wal_snapshot import open_archive


def fixture_paths():
    """源码和便携包使用同一组无个人数据的固定样本。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    root = base / ("self-test" if getattr(sys, "frozen", False) else "test-fixtures")
    return root / "synthetic-wal-main.db", root / "synthetic-wal-log.bin"


def wal_self_test(directory):
    """日志中独有的记录必须可读，查询只读且样本原字节不得改变。"""
    directory = Path(directory)
    root, run = directory / "synthetic-wal-account", directory / "synthetic-wal-snapshot"
    (root / "contact").mkdir(parents=True)
    run.mkdir()
    main, log = fixture_paths()
    target = root / "contact/contact.db"
    target.write_bytes(main.read_bytes())
    wal = Path(str(target) + "-wal")
    wal.write_bytes(log.read_bytes())
    before = [hashlib.sha256(path.read_bytes()).digest() for path in (target, wal)]
    connection, metadata = open_archive(run, "contact/contact.db", source_root=root,
                                        keys={"contact/contact.db": {"enc_key": bytes(range(32)).hex()}})
    try:
        assert connection.execute("SELECT value FROM sample ORDER BY rowid").fetchall() == [("main-only",), ("wal-only",)]
        assert metadata["wal_merged"] and metadata["snapshot_checked"]
        try:
            connection.execute("DELETE FROM sample")
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("快照不能允许写入")
    finally:
        connection.close()
    assert before == [hashlib.sha256(path.read_bytes()).digest() for path in (target, wal)]
