# -*- coding: utf-8 -*-
"""校验并流式复制本机媒体，不执行附件、不转码、不联网。"""

import hashlib
import os
import struct
import tempfile
from pathlib import Path

from bootstrap import check_cancel

MAX_MEDIA_BYTES = 1024 * 1024 * 1024
SAFE_EXTENSIONS = {"pdf", "txt", "csv", "md", "json", "xml", "zip", "7z", "rar", "doc", "docx",
                   "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "jpg", "jpeg", "png", "gif",
                   "webp", "mp3", "wav", "mp4", "mov"}


class MediaError(ValueError):
    """只携带预定义状态，不向结果泄露源路径或原始异常。"""


def video_container(stream, size):
    """检查 MP4 顶层容器边界，不把文件头命中当作全部帧可播放。"""
    position, boxes = 0, set()
    for _ in range(4096):
        if position == size:
            break
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            raise MediaError("视频容器损坏或编码暂不支持")
        length, kind = struct.unpack(">I4s", header)
        header_size = 8
        if length == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                raise MediaError("视频容器损坏或编码暂不支持")
            length, header_size = struct.unpack(">Q", extended)[0], 16
        elif length == 0:
            length = size - position
        if length < header_size or position + length > size:
            raise MediaError("视频容器损坏或编码暂不支持")
        if kind == b"ftyp" and length < 16 or kind == b"mdat" and length <= header_size:
            raise MediaError("视频容器损坏或编码暂不支持")
        boxes.add(kind)
        position += length
    if position != size or not {b"ftyp", b"moov", b"mdat"} <= boxes:
        raise MediaError("视频加密或格式暂不支持")
    stream.seek(0)


def copy_candidate(path, allowed_root, destination, kind, digests, expected_size, cancel):
    """先复制到本任务的独占临时文件，校验后由调用方发布。"""
    check_cancel(cancel)
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(allowed_root.resolve()) or not resolved.is_file():
        raise MediaError("媒体路径关联无效")
    before = resolved.stat()
    if before.st_size > MAX_MEDIA_BYTES:
        raise MediaError("媒体超过本版本大小限制")
    if expected_size is not None and before.st_size != expected_size:
        raise MediaError("媒体大小与消息不符")
    destination.mkdir(exist_ok=True)
    temporary = None
    try:
        with resolved.open("rb") as source:
            opened = os.fstat(source.fileno())
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if identity(opened) != identity(before):
                raise MediaError("读取时媒体发生变化")
            if kind == "video":
                video_container(source, opened.st_size)
            md5, sha256, size = hashlib.md5(), hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(prefix=".media-", suffix=".partial", dir=destination, delete=False) as target:
                temporary = Path(target.name)
                while block := source.read(1024 * 1024):
                    check_cancel(cancel)
                    size += len(block)
                    if size > MAX_MEDIA_BYTES:
                        raise MediaError("媒体超过本版本大小限制")
                    md5.update(block)
                    sha256.update(block)
                    target.write(block)
            if identity(os.fstat(source.fileno())) != identity(before) or identity(resolved.stat()) != identity(before) or size != before.st_size:
                raise MediaError("读取时媒体发生变化")
            if kind == "file" and md5.hexdigest() not in digests:
                raise MediaError("文件摘要与消息不符")
        check_cancel(cancel)
        return temporary, {"bytes": size, "sha256": sha256.hexdigest()}
    except BaseException:
        # 只删除本次独占创建的临时文件，原缓存与既有导出保持原样。
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
