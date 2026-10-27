# -*- coding: utf-8 -*-
"""仅在本机读取指定群的存档，原数据库只读，解密内容只驻留内存。"""

import hashlib
import importlib.util
import json
import logging
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path


def load_tool():
    """固定已审阅的工具版本，并关闭可能带敏感字段的第三方输出。"""
    sys.dont_write_bytecode = True
    path = Path(__file__).with_name("wcdb_key_tool_windows.79f1b5b92e12.py")
    expected = "9de22ccc5070bfc5325ece1bdf4da8ca537eea594b821fc2acd46a62c0e41958"
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError("工具内容与审阅版本不一致")
    spec = importlib.util.spec_from_file_location("reviewed_wcdb", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool._print = lambda *args, **kwargs: None
    logging.disable(logging.CRITICAL)
    return tool


def open_archive(run, relative, *, source_root=None, keys=None):
    """复制稳定主库并在内存解密；不查询其他会话，不合并在线写入日志。"""
    tool = load_tool()
    # 新调用直接传入密钥，保留旧研究脚本的文件接口兼容性。
    if source_root is None:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        source_root = manifest["source_root"]
    root = Path(source_root).resolve()
    source = (root / relative).resolve()
    source.relative_to(root)
    if keys is None:
        keys = json.loads((run / "keys.json").read_text(encoding="utf-8"))
    key_info = tool._get_key_info(keys, relative)
    if not key_info:
        raise RuntimeError("该分片缺少已验证密钥")
    snapshot = run / ("encrypted-snapshot-" + uuid.uuid4().hex + ".db")
    before = source.stat()
    shutil.copyfile(source, snapshot)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("复制过程中数据库变化，本次副本不能用于结论")
    key = bytes.fromhex(key_info["enc_key"])
    data = bytearray()
    with snapshot.open("rb") as stream:
        first = stream.read(tool.PAGE_SZ)
        if not tool.verify_enc_key(key, first):
            raise RuntimeError("数据库首页校验失败")
        stream.seek(0)
        page_number = 0
        while page := stream.read(tool.PAGE_SZ):
            if len(page) != tool.PAGE_SZ:
                raise RuntimeError("数据库尾页不完整")
            page_number += 1
            data.extend(tool._decrypt_page(key, page, page_number))
    # 内存反序列化不能使用写前日志模式，只调整内存副本的文件格式标记。
    # 此处不代表合并了日志；原数据库和加密副本均保持不变。
    data[18:20] = b"\x01\x01"
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA query_only=ON")
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("数据库一致性检查未通过")
    except Exception:
        connection.close()
        raise
    wal = Path(str(source) + "-wal")
    metadata = {
        "database": relative,
        "main_database_checked": True,
        "wal_bytes_not_merged": wal.stat().st_size if wal.exists() else 0,
    }
    return connection, metadata


def quoted(name):
    """仅用于从数据库结构读取出的标识符，查询值另用参数绑定。"""
    return '"' + name.replace('"', '""') + '"'
