# -*- coding: utf-8 -*-
"""增量及续导复用已提交结果，新建累计结果，绝不修改原任务。"""

import hashlib
from pathlib import Path

from bootstrap import check_cancel
from media_export import safe_media_path
from result_store import TaskError, load_result, open_result, source_identity


def media_options(report):
    """新旧说明都按实际媒体开关核对，避免续导时悄悄改变任务范围。"""
    options = report.get("options", {})
    kinds = report.get("media", {}).get("kinds", {})
    return tuple(bool(options.get("include_" + name, kinds.get(kind, {}).get("enabled", False)))
                 for name, kind in (("videos", "video"), ("files", "file")))


def validate_seed(path, source, conversation, window, videos, files, mode):
    """账号、会话、范围和媒体选项全部匹配才允许继承，不从名称猜测身份。"""
    directory, report = load_result(path)
    if not report.get("source_fingerprint") or report["source_fingerprint"] != source_identity(source):
        raise TaskError("BASE_IDENTITY")
    if report["conversation_id"] != conversation.username or report.get("kind") != conversation.kind:
        raise TaskError("BASE_IDENTITY")
    if media_options(report) != (videos, files):
        raise TaskError("BASE_SCOPE")
    from datetime import datetime
    start = int(datetime.fromisoformat(report["effective_start"]).timestamp())
    end = int(datetime.fromisoformat(report["effective_end_exclusive"]).timestamp())
    if start != window.start or (mode == "resume" and end != window.end) or (mode == "incremental" and end > window.end):
        raise TaskError("BASE_SCOPE")
    if mode == "incremental" and (report.get("status") != "主库存档采集完成" or report.get("exports", {}).get("status") != "complete"):
        raise TaskError("BASE_INCOMPLETE")
    with open_result(directory):
        pass
    return directory, report


def inherit_result(store, directory, seed, cancel=None):
    """复制消息及经过摘要验证的媒体，复制失败的媒体保留重试状态。"""
    root, _ = seed
    with open_result(root) as old:
        cursor = old.execute("SELECT message_key,sent_at,shard,local_id,server_id,sender_id,sender_name,type,subtype,text,parse_error FROM messages ORDER BY sent_at,message_key")
        while rows := cursor.fetchmany(500):
            check_cancel(cancel)
            store.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            store.commit()
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_attachments'").fetchone():
            return
        for row in old.execute("SELECT * FROM media_attachments"):
            check_cancel(cancel)
            values = dict(row)
            path = safe_media_path(values.get("path"))
            copied = False
            created = False
            if values.get("status") == "已导出" and path:
                destination = directory / path
                try:
                    source = (root / path).resolve(strict=True)
                    source.relative_to(root)
                    destination.parent.mkdir(exist_ok=True)
                    digest, size = hashlib.sha256(), 0
                    with source.open("rb") as reader, destination.open("xb") as writer:
                        created = True
                        while block := reader.read(1024 * 1024):
                            check_cancel(cancel)
                            writer.write(block)
                            digest.update(block)
                            size += len(block)
                    copied = size == values["bytes"] and digest.hexdigest() == values["sha256"]
                except (OSError, ValueError):
                    pass
                finally:
                    if created and not copied:
                        destination.unlink(missing_ok=True)
            if values.get("status") == "已导出" and not copied:
                values.update(status="继承媒体校验失败，等待重新读取", path=None, error_type="SeedMediaInvalid")
            columns = ("message_key", "kind", "status", "path", "original_name", "bytes", "sha256", "verification", "error_type")
            store.execute("INSERT OR REPLACE INTO media_attachments VALUES (?,?,?,?,?,?,?,?,?)", tuple(values.get(key) for key in columns))
        store.commit()
