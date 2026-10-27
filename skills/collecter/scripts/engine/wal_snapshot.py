# -*- coding: utf-8 -*-
"""复制稳定的加密主库与日志，在内存重建最后一次有效提交的只读快照。"""

import hashlib
import hmac
import sqlite3
import struct
import uuid
from pathlib import Path

from Crypto.Cipher import AES

from bootstrap import check_cancel
from group_probe import load_tool
from read_errors import ReadError

PAGE_BYTES = 4096
RESERVED_BYTES = 80
MAX_SNAPSHOT_BYTES = 1024 ** 3


def wal_checksum(data, seed=(0, 0), endian="<"):
    """按 SQLite 的成对无符号整数算法累计日志校验和。"""
    if len(data) % 8:
        raise ReadError("WAL_INVALID")
    first, second = seed
    for left, right in struct.iter_unpack(endian + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def _state(path):
    """同时记录大小、文件身份和高精度时间，识别追加、替换与日志重置。"""
    try:
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino
    except FileNotFoundError:
        return None


def _digest(path, cancel=None, destination=None):
    """分块校验加密字节，必要时写入本次专属副本，不写入原文件。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            check_cancel(cancel)
            size += len(block)
            if size > MAX_SNAPSHOT_BYTES:
                raise ReadError("SNAPSHOT_INVALID")
            digest.update(block)
            if destination is not None:
                destination.write(block)
    return digest.digest()


def _rollback_active(source):
    """回滚日志头非零时不按主库快照读取，冷日志的清零头不需要恢复。"""
    try:
        with Path(str(source) + "-journal").open("rb") as stream:
            return any(stream.read(8))
    except FileNotFoundError:
        return False


def stable_pair(run, source, cancel=None):
    """最多重试三次复制整对文件，并重新核对原文件摘要及元数据。"""
    wal = Path(str(source) + "-wal")
    for _ in range(3):
        check_cancel(cancel)
        if _rollback_active(source):
            continue
        before = [_state(path) for path in (source, wal)]
        if before[0] is None or any(state and state[0] > MAX_SNAPSHOT_BYTES for state in before):
            raise ReadError("SNAPSHOT_INVALID")
        copies, digests = [], []
        accepted = False
        try:
            for path, state in zip((source, wal), before):
                if state is None:
                    copies.append(None)
                    digests.append(None)
                    continue
                copy = run / ("encrypted-" + uuid.uuid4().hex + (".wal" if path == wal else ".db"))
                copies.append(copy)
                with copy.open("xb") as stream:
                    digests.append(_digest(path, cancel, stream))
            after = [_state(path) for path in (source, wal)]
            if before != after:
                continue
            verified = [_digest(path, cancel) if state else None for path, state in zip((source, wal), after)]
            if digests == verified and after == [_state(path) for path in (source, wal)] and not _rollback_active(source):
                accepted = True
                return copies
        except FileNotFoundError:
            continue
        finally:
            if not accepted:
                for copy in copies:
                    if copy is not None:
                        copy.unlink(missing_ok=True)
    raise ReadError("SNAPSHOT_BUSY")


def committed_pages(path, cancel=None):
    """只收录提交标记以前的最新页；旧盐尾部和未完成帧不进入结果。"""
    metadata = {"wal_checked": True, "wal_merged": False, "wal_committed_frames": 0,
                "wal_pending_frames": 0, "wal_tail_bytes": 0, "wal_bytes_not_merged": 0,
                "wal_status": "absent"}
    if path is None or path.stat().st_size == 0:
        return {}, None, metadata
    length = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ReadError("WAL_INVALID")
        magic, version, page_size = struct.unpack_from(">III", header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_BYTES:
            raise ReadError("WAL_INVALID")
        endian = "<" if magic == 0x377F0682 else ">"
        checksum = wal_checksum(header[:24], endian=endian)
        if checksum != struct.unpack_from(">II", header, 24):
            raise ReadError("WAL_INVALID")
        committed, pending = {}, {}
        count, last_commit, database_pages = 0, 0, None
        while True:
            check_cancel(cancel)
            position = stream.tell()
            frame = stream.read(24 + PAGE_BYTES)
            if not frame:
                break
            if len(frame) != 24 + PAGE_BYTES or frame[8:16] != header[16:24]:
                metadata["wal_tail_bytes"] = length - position
                break
            page, size = struct.unpack_from(">II", frame)
            if not page or page > MAX_SNAPSHOT_BYTES // PAGE_BYTES or size > MAX_SNAPSHOT_BYTES // PAGE_BYTES:
                raise ReadError("WAL_INVALID")
            checksum = wal_checksum(frame[:8] + frame[24:], checksum, endian)
            if checksum != struct.unpack_from(">II", frame, 16):
                raise ReadError("WAL_INVALID")
            count += 1
            pending[page] = position + 24
            if size:
                committed.update(pending)
                committed = {number: offset for number, offset in committed.items() if number <= size}
                pending.clear()
                database_pages, last_commit = size, count
        metadata.update(wal_merged=bool(last_commit), wal_committed_frames=last_commit,
                        wal_pending_frames=count - last_commit,
                        wal_bytes_not_merged=max(0, length - 32 - last_commit * (24 + PAGE_BYTES)),
                        wal_status="merged" if last_commit else "no-commit")
        return committed, database_pages, metadata


def open_archive(run, relative, *, source_root, keys, cancel=None):
    """逐页认证后解密并验证 SQLite，只返回内存连接及不含密钥的覆盖元数据。"""
    tool = load_tool()
    root = Path(source_root).resolve()
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
        Path(str(source) + "-wal").resolve().relative_to(root)
        Path(str(source) + "-journal").resolve().relative_to(root)
    except ValueError:
        raise ReadError("DATABASE_FILES") from None
    info = tool._get_key_info(keys, relative)
    if not info:
        raise ReadError("DATABASE_FILES")
    try:
        key = bytes.fromhex(info["enc_key"])
        if len(key) != 32:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ReadError("PAGE_AUTH") from None
    main, wal = stable_pair(Path(run), source, cancel)
    if main.stat().st_size < PAGE_BYTES or main.stat().st_size % PAGE_BYTES:
        raise ReadError("SNAPSHOT_INVALID")
    pages, count, metadata = committed_pages(wal, cancel)
    count = count or main.stat().st_size // PAGE_BYTES
    with main.open("rb") as stream:
        salt = stream.read(16)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(value ^ 0x3A for value in salt), 2, dklen=32)
    data = bytearray()
    # 日志副本为空时仅复用主库句柄，所有文件都来自本次临时目录。
    with main.open("rb") as main_stream, (wal or main).open("rb") as wal_stream:
        for page_number in range(1, count + 1):
            check_cancel(cancel)
            stream = wal_stream if page_number in pages else main_stream
            stream.seek(pages[page_number] if page_number in pages else (page_number - 1) * PAGE_BYTES)
            page = stream.read(PAGE_BYTES)
            if len(page) != PAGE_BYTES:
                raise ReadError("WAL_INVALID")
            start = 16 if page_number == 1 else 0
            if page_number == 1 and page[:16] != salt:
                raise ReadError("PAGE_AUTH")
            mac = hmac.new(mac_key, page[start:PAGE_BYTES - 64] + struct.pack("<I", page_number), hashlib.sha512).digest()
            if not hmac.compare_digest(mac, page[-64:]):
                raise ReadError("PAGE_AUTH")
            iv = page[PAGE_BYTES - RESERVED_BYTES:PAGE_BYTES - 64]
            plain = AES.new(key, AES.MODE_CBC, iv).decrypt(page[start:PAGE_BYTES - RESERVED_BYTES])
            data.extend((b"SQLite format 3\0" if page_number == 1 else b"") + plain + bytes(RESERVED_BYTES))
    data[18:20] = b"\x01\x01"
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(data)
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReadError("SNAPSHOT_INVALID")
    except Exception:
        connection.close()
        raise ReadError("SNAPSHOT_INVALID") from None
    metadata.update(database=relative, main_database_checked=True, snapshot_checked=True,
                    snapshot_scope="per-database", wal_pages_applied=len(pages))
    return connection, metadata
