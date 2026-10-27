# -*- coding: utf-8 -*-
"""把任务媒体状态写入文字与离线页面，只允许本工具生成的相对路径。"""

import html
import re

from media_files import SAFE_EXTENSIONS


def safe_media_path(value):
    """禁止消息字段把远程网址、绝对路径或活动网页嵌入导出。"""
    value = str(value or "")
    match = re.fullmatch(r"(videos|files)/[0-9a-f]{24}\.([a-z0-9]{1,12})", value)
    if match and (match[1] == "videos" and match[2] == "mp4" or match[1] == "files" and match[2] in SAFE_EXTENSIONS | {"bin"}):
        return value
    return ""


def media_record(store, message_key):
    """仅访问本次任务库，不重新读取源消息或媒体文件。"""
    row = store.execute("SELECT * FROM media_attachments WHERE message_key=?", (message_key,)).fetchone()
    return {key: row[key] for key in row.keys() if key != "message_key"} if row else None


def media_html(item):
    """视频可手动播放，文件仅作为下载链接，正文和名称全部转义。"""
    if not item:
        return ""
    escape = html.escape
    kind = "视频" if item["kind"] == "video" else "附件"
    text = f"<p>{kind}：{escape(item['status'])} · {escape(item.get('original_name') or '')}</p>"
    path = safe_media_path(item.get("path"))
    if path:
        if item["kind"] == "video":
            text += f'<video controls preload="none" src="{path}">当前浏览器无法播放此编码。</video>'
        text += f'<p><a href="{path}" download>下载本地{kind}</a></p>'
    return text
