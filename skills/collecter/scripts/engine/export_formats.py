# -*- coding: utf-8 -*-
"""从本次任务数据库生成 Markdown 和 CSV，不重新读取微信。"""

import csv
import html
import re

from media_export import safe_media_path


def markdown_text(value):
    """把正文当作普通文字，避免嵌入远程图片、链接或原始网页标签。"""
    return re.sub(r"([\\`*{}\[\]()#+.!_|>~\-])", r"\\\1", html.escape(str(value or "")))


def csv_text(value):
    """电子表格可能把消息当成公式，CSV 中显式保留为文本。"""
    value = str(value or "")
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_markdown_csv(store, directory, report, stamp, type_names):
    """逐行遍历本地结果；原始正文继续完整保存在 JSONL 和 SQLite。"""
    with (directory / "聊天记录.md").open("x", encoding="utf-8") as markdown, (directory / "messages.csv").open("x", encoding="utf-8-sig", newline="") as csv_file:
        markdown.write(f"# {markdown_text(report['conversation_name'])}\n\n")
        markdown.write(f"读取状态：{markdown_text(report['status'])}；记录数：{report['count']}\n\n")
        markdown.write("导出文件是否完整，以同目录的采集说明.json 为准。\n\n")
        markdown.write(markdown_text(report["limitation"]) + "\n\n")
        writer = csv.writer(csv_file)
        writer.writerow(("消息标识", "时间", "发送人", "消息类型", "正文", "解析错误", "媒体类型", "媒体状态", "媒体路径", "原附件名"))
        query = "SELECT m.*,v.kind AS media_kind,v.status AS media_status,v.path AS media_path,v.original_name FROM messages m LEFT JOIN media_attachments v USING(message_key) ORDER BY sent_at,message_key"
        for row in store.execute(query):
            time = stamp(row["sent_at"])
            kind = type_names.get(row["type"], "其他")
            sender = markdown_text(row["sender_name"]).replace("\n", " ").replace("\r", " ")
            markdown.write(f"## {time} · {sender} · {kind}\n\n")
            for line in str(row["text"] or "").splitlines() or [""]:
                markdown.write("> " + markdown_text(line) + "\n")
            markdown.write("\n")
            if row["media_status"]:
                markdown.write("媒体：" + markdown_text(row["media_status"]) + " · " + markdown_text(row["original_name"]) + "\n\n")
            media_path = safe_media_path(row["media_path"])
            if media_path:
                markdown.write(f"[本地{'视频' if row['media_kind'] == 'video' else '附件'}](<{media_path}>)\n\n")
            writer.writerow(tuple(csv_text(value) for value in (row["message_key"], time, row["sender_name"], kind, row["text"], row["parse_error"], row["media_kind"], row["media_status"], media_path, row["original_name"])))
