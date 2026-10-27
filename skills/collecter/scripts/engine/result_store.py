# -*- coding: utf-8 -*-
"""校验本地导出身份，提供只读结果连接及跨进程任务互斥。"""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from bootstrap import PRIVATE

_ACTIVE = set()
_GUARD = threading.Lock()

TASK_ERRORS = {
    "RESULT_INVALID": ("本地结果结构不支持或含未处理的写入日志。", "请选择包含采集说明.json和records.sqlite3的导出目录；原文件不会修改。"),
    "TASK_BUSY": ("该任务仍在运行或被另一进程使用。", "请等待原任务结束后手动续导，不需要删除锁文件。"),
    "BASE_IDENTITY": ("原任务缺少账号身份摘要，或账号及会话与当前选择不一致。", "旧结果仍可浏览分析；请用新版本手动建立基线，并选择原账号和会话。"),
    "BASE_SCOPE": ("续导或增量的时间范围、会话选择或媒体选项不匹配。", "续导沿用冻结范围；增量保留开始时间，只延长结束时间，媒体选项沿用原任务。"),
    "BASE_INCOMPLETE": ("原任务尚未完成，不能作为增量基线。", "请先使用普通续导完成原范围，或另建新任务。"),
    "ANALYSIS_OPTIONS": ("浏览参数不合法。", "页码至少为1，每页1到200条。"),
}


class TaskError(ValueError):
    """只公开预定义的任务错误，不把来源字段或任意异常正文作为提示。"""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in TASK_ERRORS else "RESULT_INVALID"
        super().__init__(TASK_ERRORS[self.code][0])


def source_identity(source):
    """账号路径只用于本机身份摘要，不把原目录写进任务说明。"""
    root = source if isinstance(source, (str, Path)) else getattr(source, "root", None)
    return hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest() if root else None


def load_result(directory):
    """只接受显式指定目录中的说明和任务库，拒绝越界链接和过大说明。"""
    directory = Path(directory).expanduser().resolve()
    if directory.is_file():
        directory = directory.parent
    report_path = directory / "采集说明.json"
    database = directory / "records.sqlite3"
    for path in (report_path, database):
        path.resolve(strict=True).relative_to(directory)
    if report_path.stat().st_size > 8 * 1024 * 1024:
        raise TaskError("RESULT_INVALID")
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict) or not isinstance(report.get("conversation_id"), str):
        raise TaskError("RESULT_INVALID")
    return directory, report


@contextmanager
def task_lease(directory):
    """锁文件仅写入私有目录，进程退出后操作系统自动释放，不更改旧导出。"""
    directory = Path(directory).resolve()
    if directory.is_file():
        directory = directory.parent
    identity = hashlib.sha256(str(directory).casefold().encode()).hexdigest()
    with _GUARD:
        if identity in _ACTIVE:
            raise TaskError("TASK_BUSY")
        _ACTIVE.add(identity)
    stream = None
    locked = False
    try:
        folder = PRIVATE / "task-locks"
        folder.mkdir(parents=True, exist_ok=True)
        stream = (folder / (identity + ".lock")).open("a+b")
        if stream.seek(0, 2) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise TaskError("TASK_BUSY") from None
        yield
    finally:
        if stream is not None:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()
        with _GUARD:
            _ACTIVE.discard(identity)


@contextmanager
def open_result(directory):
    """结果库始终以不可变只读模式打开；有遗留日志时拒绝原地恢复。"""
    directory, _ = load_result(directory)
    database = directory / "records.sqlite3"
    if any(path.exists() and path.stat().st_size for path in (Path(str(database) + "-wal"), Path(str(database) + "-journal"))):
        raise TaskError("RESULT_INVALID")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        table = connection.execute("SELECT type FROM sqlite_master WHERE name='messages'").fetchone()
        if not table or table[0] != "table":
            raise TaskError("RESULT_INVALID")
        connection.execute("SELECT message_key,sent_at,shard,local_id,server_id,sender_id,sender_name,type,subtype,text,parse_error FROM messages LIMIT 0")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise TaskError("RESULT_INVALID")
        yield connection
    finally:
        connection.close()
