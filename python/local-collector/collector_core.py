# -*- coding: utf-8 -*-
"""采集一个会话的指定时间范围，流式存储并生成独立结果。"""

import datetime as dt
import hashlib
import html
import json
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from contextlib import ExitStack
from pathlib import Path

from bootstrap import Cancelled, check_cancel, quote_identifier
from read_target_group import decode_text, readable_body
from export_formats import export_markdown_csv
from local_storage import atomic_write
from media_export import media_html, media_record
from result_store import source_identity, task_lease
from export_seed import inherit_result, validate_seed
from read_errors import ReadError

SHANGHAI = dt.timezone(dt.timedelta(hours=8))
LIMITATION = "读取本机数据库及已提交日志的分片独立快照；不保证多个分片处于同一时刻，本机没有的历史无法补回。"
TYPE_NAMES = {1: "文字", 3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置", 49: "卡片/接龙", 50: "通话", 10000: "系统"}


@dataclass(frozen=True)
class TimeRange:
    """内部使用左闭右开范围，界面日期与精确到秒的结束时刻均包含。"""

    start: int
    end: int
    requested_start: str
    requested_end: str


def parse_range(start_text, end_text, now=None):
    """支持日期或具体时刻；纯日期的结束值包含当天整天。"""
    def parse(value, ending=False):
        value = value.strip().replace("/", "-").replace("T", " ")
        for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI)
                if ending:
                    parsed += dt.timedelta(days=1) if pattern == "%Y-%m-%d" else dt.timedelta(seconds=1)
                return int(parsed.timestamp())
            except ValueError:
                continue
        raise ValueError("时间格式应为 2025-01-01 或 2025-01-01 08:30:00。")
    start, end = parse(start_text), parse(end_text, True)
    if end <= start:
        raise ValueError("结束时间不能早于开始时间。")
    instant = now or dt.datetime.now(SHANGHAI)
    end = min(end, int(instant.timestamp()) + 1)
    if start >= end:
        raise ValueError("开始时间不能晚于当前时间。")
    return TimeRange(start, end, start_text, end_text)


def stamp(value):
    """显示固定东八区时间，不依赖系统时区。"""
    return dt.datetime.fromtimestamp(value, SHANGHAI).isoformat()


def create_store(path):
    """每个任务使用新文件，既有结果不覆盖。"""
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE messages (
        message_key TEXT PRIMARY KEY, sent_at INTEGER NOT NULL,
        shard TEXT NOT NULL, local_id INTEGER, server_id TEXT,
        sender_id TEXT, sender_name TEXT, type INTEGER, subtype INTEGER,
        text TEXT, parse_error TEXT
    )""")
    connection.execute("CREATE INDEX message_time ON messages(sent_at, message_key)")
    # 仅在新任务中建立视频和附件表，不迁移或覆盖既有数据库。
    connection.execute("""CREATE TABLE media_attachments (
        message_key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
        path TEXT, original_name TEXT, bytes INTEGER, sha256 TEXT,
        verification TEXT, error_type TEXT
    )""")
    return connection


def store_rows(store, rows, shard, names, group, cancel, media=None):
    """按原生消息编号去重，不把相同正文的不同消息合并。"""
    for row in rows:
        lid, server_id, raw_type, timestamp, sender, compression, content = row[:7]
        packed = row[7] if len(row) > 7 else None
        check_cancel(cancel)
        kind = int(raw_type) & 0xFFFFFFFF
        identity = "s:" + str(server_id) if server_id not in (None, 0, "0", "") else f"l:{shard}:{lid}"
        existing = store.execute("SELECT 1 FROM messages WHERE message_key=?", (identity,)).fetchone()
        retry_media = False
        if existing:
            attachment = store.execute("SELECT status FROM media_attachments WHERE message_key=?", (identity,)).fetchone()
            retry_media = media is not None and kind in (43, 49) and (not attachment or attachment[0] != "已导出")
            if not retry_media:
                continue
        raw_text = ""
        try:
            raw_text = decode_text(content, compression)
            text = raw_text
            item = {"type": kind, "content": text, "sender_id": sender if group else None}
            text = readable_body(item)
            error = None
        except Exception as exc:
            text, error = "[正文解析失败，可在结果说明中查看状态]", type(exc).__name__
        inserted = store.execute(
            "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity, timestamp, shard, lid, str(server_id or ""), sender,
             names.get(sender, "未识别发送人"), kind, int(raw_type) >> 32, text, error),
        )
        if kind in (43, 49) and media is not None and (inserted.rowcount or retry_media):
            if retry_media:
                store.execute("DELETE FROM media_attachments WHERE message_key=?", (identity,))
            try:
                media.add(identity, lid, server_id, timestamp, kind, raw_text, packed)
            except Cancelled:
                raise
            except Exception as exc:
                from media_files import MediaError
                # 仅记录预定义状态；第三方异常正文、密钥和源路径不进入结果。
                status = str(exc) if isinstance(exc, MediaError) else "媒体关联失败"
                store.execute("INSERT OR REPLACE INTO media_attachments(message_key,kind,status,error_type) VALUES (?,?,?,?)",
                              (identity, "video" if kind == 43 else "file", status, type(exc).__name__))


def export_results(store, directory, report):
    """先写本任务专属临时目录，完整生成后再发布，不覆盖已有文件。"""
    report["exports"] = {"status": "writing", "files": {}}
    with tempfile.TemporaryDirectory(prefix=".export-", dir=directory) as temporary:
        staged = Path(temporary).resolve()
        staged.relative_to(Path(directory).resolve())
        write_export_results(store, staged, report)
        export_markdown_csv(store, staged, report, stamp, TYPE_NAMES)
        files = sorted(staged.iterdir())
        # 先检查全部目标，避免后一个文件冲突时已经发布前面的文件。
        if any((directory / path.name).exists() for path in files):
            raise FileExistsError("导出目标已存在，原文件未覆盖。")
        for path in files:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            metadata = {"bytes": path.stat().st_size, "sha256": digest}
            path.rename(directory / path.name)
            report["exports"]["files"][path.name] = metadata
    report["exports"]["status"] = "complete"


def write_export_results(store, directory, report):
    """按时间流式生成文件，不输出正文到程序日志。"""
    store.row_factory = sqlite3.Row
    with (directory / "消息记录.txt").open("x", encoding="utf-8") as text_file, (directory / "messages.jsonl").open("x", encoding="utf-8") as json_file:
        text_file.write(f"会话：{report['conversation_name']}（{report['kind']}）\n")
        text_file.write(f"请求范围：{report['requested_start']} 至 {report['requested_end']}\n")
        text_file.write(f"状态：{report['status']}；记录数：{report['count']}\n{report.get('limitation', LIMITATION)}\n\n")
        text_file.write("导出文件是否完整，以同目录的采集说明.json 为准。\n\n")
        for row in store.execute("SELECT * FROM messages ORDER BY sent_at,message_key"):
            item = dict(row)
            item["time"] = stamp(item["sent_at"])
            item["type_name"] = TYPE_NAMES.get(item["type"], "其他")
            media = media_record(store, item["message_key"])
            if media:
                item["media"] = media
            json_file.write(json.dumps(item, ensure_ascii=False) + "\n")
            text_file.write(f"{item['time']}  {item['sender_name']}  [{item['type_name']}]\n{item['text']}\n")
            if media:
                text_file.write(f"{'视频' if media['kind'] == 'video' else '附件'}：{media['status']} · {media['original_name'] or ''} · {media['path'] or ''}\n")
            text_file.write("\n")
    if report.get("media", {}).get("enabled"):
        export_media_page(store, directory, report)


def export_media_page(store, directory, report):
    """离线页面按消息显示本机媒体，所有聊天文字转义，不加载远程资源。"""
    escape = html.escape
    with (directory / "聊天记录.html").open("x", encoding="utf-8") as stream:
        stream.write('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
        stream.write('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; media-src \'self\' file:; style-src \'unsafe-inline\'; base-uri \'none\'; object-src \'none\'">')
        stream.write('<title>本机聊天采集记录</title><style>body{font:16px/1.6 sans-serif;max-width:900px;margin:32px auto;padding:0 20px;background:#f5f6f8;color:#24292f}article{background:white;padding:18px;margin:14px 0;border-radius:8px}p{white-space:pre-wrap;overflow-wrap:anywhere}video{max-width:100%;max-height:600px}video{display:block;width:100%;max-width:640px}small{color:#586069}</style>')
        stream.write(f"<h1>{escape(report['conversation_name'])}</h1><p>{escape(report.get('limitation', LIMITATION))}</p>")
        if report.get("media", {}).get("enabled"):
            stream.write("<p>视频仅检查MP4容器，不转码或保证所有编码可播放；附件只提供本地下载，不自动打开。</p>")
        stream.write("<p>导出文件是否完整，以同目录的采集说明.json 为准。</p>")
        for row in store.execute("SELECT * FROM messages ORDER BY sent_at,message_key"):
            stream.write(f"<article><small>{escape(stamp(row['sent_at']))} · {escape(row['sender_name'] or '')}</small><p>{escape(row['text'] or '')}</p>")
            stream.write(media_html(media_record(store, row["message_key"])))
            stream.write("</article>")
        stream.write("</html>")


def collect(source, conversation, window, output, notify=lambda text: None, cancel=None,
            include_videos=False, include_files=False, *, seed_from=None, mode="new", on_created=None):
    """锁定任务后创建新结果；增量与续导只从旧任务读取已提交内容。"""
    if mode not in ("new", "incremental", "resume") or (seed_from is None) != (mode == "new"):
        raise ValueError("导出模式与原任务目录不匹配。")
    with ExitStack() as stack:
        seed = None
        if seed_from is not None:
            stack.enter_context(task_lease(seed_from))
            seed = validate_seed(seed_from, source, conversation, window, include_videos, include_files, mode)
        directory = Path(output).expanduser().resolve() / (dt.datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        stack.enter_context(task_lease(directory))
        return _collect(source, conversation, window, directory, notify, cancel, include_videos, include_files, seed, mode, on_created)


def _collect(source, conversation, window, directory, notify, cancel, include_videos, include_files, seed, mode, on_created):
    """每次仅查询指定会话；取消和部分失败仍保留已完成数据及明确状态。"""
    check_cancel(cancel)
    directory.mkdir(parents=True, exist_ok=False)
    table = "Msg_" + hashlib.md5(conversation.username.encode()).hexdigest()
    report = {
        "conversation_name": conversation.name, "conversation_id": conversation.username,
        "kind": conversation.kind, "requested_start": window.requested_start,
        "requested_end": window.requested_end, "effective_start": stamp(window.start),
        "effective_end_exclusive": stamp(window.end), "created_at": dt.datetime.now(SHANGHAI).isoformat(),
        "status": "采集中", "limitation": LIMITATION, "coverage": [],
        "contact_snapshot": source.contact_metadata, "count": 0,
        "source_fingerprint": source_identity(source), "mode": mode,
        "options": {"include_videos": include_videos, "include_files": include_files},
        "incremental": {"strategy": "rescan-window-and-deduplicate", "inherited_messages": 0, "new_messages": 0},
        "media": {"enabled": include_videos or include_files, "complete": not (include_videos or include_files)},
    }
    report_path = directory / "采集说明.json"
    if seed is not None:
        report["limitation"] += " 本次为累计归档，继承记录保留，不是源库当前删改状态的完整镜像。"
    atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    if on_created is not None:
        on_created(directory)
    store = create_store(directory / "records.sqlite3")
    media = None
    try:
        if seed is not None:
            try:
                inherit_result(store, directory, seed, cancel)
            finally:
                report["incremental"]["inherited_messages"] = store.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            report["incremental"]["base_created_at"] = seed[1].get("created_at")
        if include_videos or include_files:
            try:
                from media_collect import MediaCollector
                media = MediaCollector(source, conversation, store, directory, notify, cancel, include_videos, include_files)
            except Cancelled:
                raise
            except Exception as exc:
                report["media"].update(status="媒体模块未能启动", error_type=type(exc).__name__)
                notify("媒体模块未能启动，继续导出消息并保留失败状态。")
        shards = source.shards()
        report["expected_shards"] = len(shards)
        for position, shard in enumerate(shards, 1):
            check_cancel(cancel)
            notify(f"正在读取消息分片 {position}/{len(shards)}；只查询选中的会话。")
            coverage = {"database": shard, "status": "未完成"}
            try:
                check_cancel(cancel)
                with source.open_db(shard, cancel) as (connection, metadata):
                    coverage.update(metadata)
                    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if exists:
                        count, first, last = connection.execute(f"SELECT COUNT(*),MIN(create_time),MAX(create_time) FROM {quote_identifier(table)}").fetchone()
                        coverage.update(archive_count=count, earliest=stamp(first) if first else None, latest=stamp(last) if last else None)
                        # 分批读取全部命中记录，不沿用验证脚本的最近几十条限制。
                        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")}
                        packed = "m.packed_info_data" if media is not None and "packed_info_data" in columns else "NULL"
                        cursor = connection.execute(
                            f"SELECT m.local_id,m.server_id,m.local_type,m.create_time,n.user_name,"
                            f"m.WCDB_CT_message_content,m.message_content,{packed} FROM {quote_identifier(table)} m "
                            "LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id "
                            "WHERE m.create_time>=? AND m.create_time<? ORDER BY m.create_time,m.local_id",
                            (window.start, window.end),
                        )
                        while rows := cursor.fetchmany(500):
                            store_rows(store, rows, shard, source.names, conversation.kind == "群聊", cancel, media)
                            store.commit()
                    coverage["status"] = "已检查" if exists else "主库无该会话表"
            except Cancelled:
                coverage["status"] = "已取消，分片未读完"
                report["coverage"].append(coverage)
                raise
            except Exception as exc:
                # 不把第三方异常中的账号目录、密钥或原始正文写到日志。
                coverage.update(status="读取失败", error_type=type(exc).__name__)
                if isinstance(exc, ReadError):
                    coverage["error_code"] = exc.code
            report["coverage"].append(coverage)
        check_cancel(cancel)
        report["status"] = "部分分片失败" if any(item["status"] == "读取失败" for item in report["coverage"]) else "主库存档采集完成"
        if not shards:
            report["status"] = "未找到消息分片"
        if media is not None:
            media.finish()
    except Cancelled:
        report["status"] = "已取消，结果不完整"
    except Exception as exc:
        report.update(status="任务中断，结果不完整", error_type=type(exc).__name__)
    finally:
        if media is not None:
            media.close()
            report["media"] = media.summary()
    try:
        store.commit()
        count, first, last = store.execute("SELECT COUNT(*),MIN(sent_at),MAX(sent_at) FROM messages").fetchone()
        report.update(count=count, collected_first=stamp(first) if first else None, collected_last=stamp(last) if last else None)
        report["incremental"]["new_messages"] = count - report["incremental"]["inherited_messages"]
        report["parse_errors"] = store.execute("SELECT COUNT(*) FROM messages WHERE parse_error IS NOT NULL").fetchone()[0]
        report["message_types"] = {str(kind): {"name": TYPE_NAMES.get(kind, "其他"), "count": total}
                                   for kind, total in store.execute("SELECT type,COUNT(*) FROM messages GROUP BY type")}
        checked = bool(report["coverage"]) and all(item.get("snapshot_checked") for item in report["coverage"])
        report["completeness"] = {"scope": "committed-main-and-wal" if checked else "main-database-only", "wal_merged": any(item.get("wal_merged") for item in report["coverage"]),
                                  "wal_checked": checked,
                                  "wal_bytes_not_merged": sum(item.get("wal_bytes_not_merged", 0) for item in report["coverage"]),
                                  "cross_shard_snapshot": False}
        report["completeness"]["cumulative_archive"] = seed is not None
        report["finished_at"] = dt.datetime.now(SHANGHAI).isoformat()
        if include_videos and media is None:
            store.execute("INSERT OR IGNORE INTO media_attachments(message_key,kind,status) SELECT message_key,'video','媒体模块未能启动' FROM messages WHERE type=43")
        # 媒体模块失败时新增的状态也必须在关闭数据库前提交。
        store.commit()
        try:
            export_results(store, directory, report)
        except Exception as exc:
            report["collection_status"] = report["status"]
            report.update(status="结果导出失败，任务数据库已保留", export_error=type(exc).__name__)
            report.setdefault("exports", {"files": {}})["status"] = "failed"
        report["finished_at"] = dt.datetime.now(SHANGHAI).isoformat()
        atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        store.close()
    return directory, report
