# -*- coding: utf-8 -*-
"""读取已定位群的最近存档，仅将目标群的结果保存在本机私有目录。"""

import datetime
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).with_name("python-deps")))
import zstandard

from group_probe import open_archive, quoted


def decode_text(value, compression):
    """解压失败明确报错，不把二进制内容当成聊天文字。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if compression == 4 or value.startswith(b"\x28\xb5\x2f\xfd"):
        value = zstandard.ZstdDecompressor().decompress(value, max_output_size=2 * 1024 * 1024)
    return value.decode("utf-8", errors="strict")


def readable_body(item):
    """移除已核实的群发送人前缀，并将接龙、链接等卡片转为可读文字。"""
    text = item["content"]
    prefix = re.match(r"^([A-Za-z0-9_@.-]+):\r?\n", text)
    if prefix and prefix.group(1) == item["sender_id"]:
        text = text[prefix.end():]
    if item["type"] == 1:
        return text
    if item["type"] == 49:
        # 只解析有界的普通消息字段，不允许文档类型和实体声明。
        if len(text) <= 2 * 1024 * 1024 and not re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
            try:
                root = ET.fromstring(text)
                title = root.findtext(".//appmsg/title") or ""
                description = root.findtext(".//appmsg/des") or ""
                if title or description:
                    body = title + ("\n" + description if description and description != title else "")
                    return "[接龙]\n" + body if title.startswith("#接龙") else "[链接或文件]\n" + body
            except ET.ParseError:
                pass
    types = {3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置", 49: "链接或文件", 50: "通话", 10000: "系统消息"}
    return "[" + types.get(item["type"], "其他消息") + "]"


def run_probe(run):
    """逐片查询指定会话，统计完整主库存档并提取少量最新消息。"""
    target = json.loads((run / "target.json").read_text(encoding="utf-8"))
    keys = json.loads((run / "keys.json").read_text(encoding="utf-8"))
    table = "Msg_" + hashlib.md5(target["username"].encode()).hexdigest()
    cutoff = int(datetime.datetime.now().timestamp()) - 86400
    candidates, coverage, failures = [], [], []
    shards = sorted(name for name in keys if re.fullmatch(r"message[\\/]message_\d+\.db", name))
    for shard in shards:
        connection = None
        try:
            connection, metadata = open_archive(run, shard)
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            metadata["target_present"] = bool(exists)
            if exists:
                count, first, last = connection.execute(
                    f"SELECT COUNT(*), MIN(create_time), MAX(create_time) FROM {quoted(table)}"
                ).fetchone()
                metadata.update(count=count, first=first, last=last)
                metadata["last_day_count"] = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted(table)} WHERE create_time>=?", (cutoff,)
                ).fetchone()[0]
                # 发送人必须在当前分片映射，不能沿用其他分片的编号。
                rows = connection.execute(
                    f"SELECT m.local_id,m.server_id,m.local_type,m.create_time,n.user_name,"
                    f"m.WCDB_CT_message_content,m.message_content FROM {quoted(table)} m "
                    "LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id "
                    "ORDER BY m.create_time DESC,m.local_id DESC LIMIT 40"
                ).fetchall()
                for lid, sid, kind, ts, sender, compression, content in rows:
                    parse_error = None
                    try:
                        text = decode_text(content, compression)
                    except Exception as exc:
                        text = "[正文解析失败]"
                        parse_error = type(exc).__name__
                    candidates.append({
                        "shard": shard, "local_id": lid, "server_id": sid,
                        "type": kind & 0xFFFFFFFF, "timestamp": ts,
                        "sender_id": sender, "content": text, "parse_error": parse_error,
                    })
            coverage.append(metadata)
            print(json.dumps({"database": shard, "target_records": metadata.get("count", 0), "checked": True}, ensure_ascii=False), flush=True)
        except Exception as exc:
            failures.append({"database": shard, "error_type": type(exc).__name__})
            print(json.dumps({"database": shard, "checked": False, "error_type": type(exc).__name__}), flush=True)
        finally:
            if connection is not None:
                connection.close()

    # 这是分片存档行数，不声称已校验跨分片迁移产生的所有重复记录。
    candidates.sort(key=lambda item: (item["timestamp"] or 0, item["shard"], item["local_id"]))
    recent = [item for item in candidates if (item["timestamp"] or 0) >= cutoff]
    selected = (recent or candidates)[-30:]
    connection, contact_meta = open_archive(run, r"contact\contact.db")
    try:
        for item in selected:
            sender = connection.execute(
                "SELECT remark,nick_name FROM contact WHERE username=?", (item["sender_id"],)
            ).fetchone()
            item["sender_name"] = (sender[0] or sender[1]) if sender else "未解析成员"
            item["time"] = datetime.datetime.fromtimestamp(item["timestamp"]).astimezone().isoformat()
    finally:
        connection.close()

    now = datetime.datetime.now().astimezone().isoformat()
    result = {
        "group_name": target["name"], "checked_at": now, "coverage": coverage,
        "failed_shards": failures, "contact_snapshot": contact_meta,
        "selection": "最近一天最多30条" if recent else "最近一天无记录，展示最近30条存档",
        "limitation": "只读取主数据库存档，未合并正在运行的微信写入日志，最新消息可能缺失。",
        "messages": selected,
    }
    # 使用排他创建，避免重复运行覆盖上一轮结果。
    suffix = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    json_path = run / ("group-result-" + suffix + ".json")
    with json_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    text_path = run / ("群消息读取结果-" + suffix + ".txt")
    with text_path.open("x", encoding="utf-8") as stream:
        stream.write(f"群名：{target['name']}\n读取时间：{now}\n范围：{result['selection']}\n{result['limitation']}\n\n")
        for item in selected:
            body = readable_body(item)
            stream.write(f"{item['time']}  {item['sender_name']}\n{body}\n\n")
    preview = []
    for item in selected:
        if item["type"] == 1 and not item["parse_error"]:
            # 控制台仅给出少量匿名样本，完整目标群结果留在私有文件。
            text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", readable_body(item))
            preview.append({"time": item["time"], "text": text[:240]})
    print(json.dumps({
        "group_name": target["name"], "archive_rows": sum(item.get("count", 0) for item in coverage),
        "last_day_rows": sum(item.get("last_day_count", 0) for item in coverage),
        "shards_checked": len(coverage), "failed_shards": failures,
        "first_timestamp": min((item["first"] for item in coverage if item.get("first")), default=None),
        "last_timestamp": max((item["last"] for item in coverage if item.get("last")), default=None),
        "selected_count": len(selected), "text_report": str(text_path),
        "preview": preview[-6:],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run_probe(Path(sys.argv[1]).resolve())
