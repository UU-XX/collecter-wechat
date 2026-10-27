# -*- coding: utf-8 -*-
"""导出明确关联的本机视频及文件，缺失和不支持的情况保留状态。"""

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from bootstrap import Cancelled, check_cancel
from resource_metadata import resource_digests
from media_files import MAX_MEDIA_BYTES, SAFE_EXTENSIONS, MediaError, copy_candidate

HEX = re.compile(r"[0-9a-fA-F]{32}")


def metadata(text, kind):
    """仅提取本地关联必需字段，密钥、远程地址和原始 XML 不进入结果。"""
    if len(text) > 2 * 1024 * 1024 or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise MediaError("媒体消息结构暂不支持")
    try:
        root = ET.fromstring(text[text.index("<"):])
    except (ValueError, ET.ParseError):
        raise MediaError("媒体消息结构暂不支持") from None
    node = root if root.tag == "videomsg" else root.find(".//videomsg")
    if kind == 43:
        digest = node.get("md5", "") if node is not None else ""
        return {"kind": "video", "original_name": "视频.mp4", "extension": "mp4",
                "digests": {digest.lower()} if HEX.fullmatch(digest) else set(), "expected_size": None}
    app = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app is None:
        raise MediaError("媒体消息结构暂不支持")
    if (app.findtext("type") or "").strip() != "6":
        return None
    name = (app.findtext("title") or "").strip()
    if not name or len(name) > 180 or re.search(r'[\x00-\x1f\x7f/\\:*?"<>|]', name) or name in (".", "..") or name.endswith((".", " ")):
        raise MediaError("附件文件名暂不支持")
    extension = (app.findtext("appattach/fileext") or Path(name).suffix.lstrip(".")).strip().lower()
    if not re.fullmatch(r"[a-z0-9]{1,12}", extension):
        extension = "bin"
    digest = (app.findtext("md5") or app.findtext("appattach/md5") or "").strip()
    length = app.findtext("appattach/totallen")
    length = length.strip() if length is not None else None
    if length is not None and (not length.isdigit() or int(length) > MAX_MEDIA_BYTES):
        raise MediaError("附件大小字段暂不支持")
    return {"kind": "file", "original_name": name, "extension": extension,
            "digests": {digest.lower()} if HEX.fullmatch(digest) else set(),
            "expected_size": int(length) if length is not None else None}


class MediaCollector:
    """资源库限定会话与消息身份；只复制本次任务选中的资源。"""

    def __init__(self, source, conversation, store, directory, notify, cancel, videos=False, files=False):
        self.source, self.store, self.directory = source, store, directory
        self.notify, self.cancel = notify, cancel
        self.enabled = {"video": videos, "file": files}
        self.jobs, self.stack, self.resources, self.chat_ids = [], ExitStack(), None, []
        self.resource_status = "资源库不可用，尝试消息中的明确摘要"
        self.account = source.root.parent.resolve()
        self.folders = {"video": self.account / "msg/video",
                        "file": self.account / "msg/attach" / hashlib.md5(conversation.username.encode()).hexdigest(),
                        "shared_file": self.account / "msg/file"}
        for folder in self.folders.values():
            if folder.resolve().relative_to(self.account).parts != folder.relative_to(self.account).parts:
                raise MediaError("媒体缓存目录关联无效")
        try:
            # 额外资源查询仅用缓存，不继承或新增任何内存扫描授权。
            source.prepare(notify, cancel, include_resources=True, read_memory=False)
            connection, _ = self.stack.enter_context(source.open_db(str(Path("message/message_resource.db")), cancel))
            connection.execute("SELECT chat_id,message_local_id,message_svr_id,message_create_time,message_local_type,packed_info FROM MessageResourceInfo LIMIT 0")
            self.chat_ids = [row[0] for row in connection.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (conversation.username,))]
            self.resources, self.resource_status = connection, "已读取主库资源映射"
        except Cancelled:
            self.stack.close()
            raise
        except Exception:
            self.stack.close()

    def add(self, identity, local_id, server_id, timestamp, kind, text, packed=None):
        """只登记新增的目标消息，非文件卡片继续沿用原有文字导出。"""
        if kind == 43 and not self.enabled["video"] or kind == 49 and not self.enabled["file"]:
            return
        info = metadata(text, kind)
        if info is None:
            return
        digests = set(info["digests"])
        if kind == 43 and isinstance(packed, bytes) and len(packed) <= 1024 * 1024:
            # 新版消息可直接携带缓存文件标识；仅收集完整的固定长度标识，不猜首个命中。
            digests.update(value.decode("ascii").lower() for value in
                           re.findall(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", packed))
        if len(digests) > 64:
            raise MediaError("媒体标识过多，未自动选择")
        if self.resources is not None:
            for chat_id in self.chat_ids:
                sql = "SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? AND message_local_id=? AND message_create_time=? AND (message_local_type & 4294967295)=?"
                parameters = [chat_id, local_id, timestamp, kind]
                if server_id not in (None, 0, "0", ""):
                    sql += " AND message_svr_id=?"
                    parameters.append(server_id)
                for (blob,) in self.resources.execute(sql, parameters):
                    check_cancel(self.cancel)
                    digests.update(resource_digests(blob))
                    if len(digests) > 64:
                        raise MediaError("媒体标识过多，未自动选择")
        info["digests"] = digests
        self.store.execute("INSERT INTO media_attachments(message_key,kind,status,original_name) VALUES (?,?,?,?)",
                           (identity, info["kind"], "待处理", info["original_name"]))
        self.jobs.append((identity, info))

    def candidates(self, info):
        """按已知摘要或文件名精确定位，文件名匹配仍必须通过内容摘要校验。"""
        kind = info["kind"]
        folders = [(self.folders[kind], "" if kind == "video" else "File")]
        if kind == "video":
            folders.append((self.folders["file"], "Video"))
        else:
            # 账号共用文件缓存只按选中消息的明确文件名定位，随后必须通过内容摘要校验。
            folders.append((self.folders["shared_file"], ""))
        names = {digest + ".mp4" for digest in info["digests"]} if kind == "video" else {
            info["original_name"], *(digest + "." + info["extension"] for digest in info["digests"])}
        if kind == "file" and not Path(info["original_name"]).suffix and info["extension"] != "bin":
            names.add(info["original_name"] + "." + info["extension"])
        matches = []
        for folder, subfolder in folders:
            if not folder.is_dir():
                continue
            for month in sorted(folder.iterdir()):
                check_cancel(self.cancel)
                if not re.fullmatch(r"\d{4}-\d{2}", month.name):
                    continue
                for name in sorted(names):
                    path = month / subfolder / name if subfolder else month / name
                    if path.is_file():
                        if not path.resolve().is_relative_to(folder.resolve()):
                            raise MediaError("媒体路径关联无效")
                        matches.append((path, folder))
                        if len(matches) > 64:
                            raise MediaError("媒体候选过多，未自动选择")
        return matches

    def finish(self):
        """逐个复制并校验；不同内容的多候选明确失败，绝不按顺序猜测。"""
        for position, (identity, info) in enumerate(self.jobs, 1):
            self.notify(f"正在导出本机视频及文件 {position}/{len(self.jobs)}。")
            check_cancel(self.cancel)
            held, fingerprints, failure = [], set(), "本机媒体缺失"
            try:
                if not info["digests"]:
                    raise MediaError("缺少可验证媒体摘要")
                for path, folder in self.candidates(info):
                    try:
                        temporary, detail = copy_candidate(path, folder, self.directory / ("videos" if info["kind"] == "video" else "files"),
                                                           info["kind"], info["digests"], info["expected_size"], self.cancel)
                        held.append((temporary, detail))
                        fingerprints.add(detail["sha256"])
                    except MediaError as error:
                        failure = str(error)
                    except OSError:
                        failure = "媒体文件无法读取"
                if not held:
                    raise MediaError(failure)
                if len(fingerprints) != 1:
                    raise MediaError("资源关联不唯一")
                check_cancel(self.cancel)
                extension = info["extension"] if info["extension"] in SAFE_EXTENSIONS else "bin"
                relative = ("videos/" if info["kind"] == "video" else "files/") + hashlib.sha256(identity.encode()).hexdigest()[:24] + "." + extension
                destination = self.directory / relative
                if destination.exists():
                    raise MediaError("导出目标已存在，未覆盖")
                temporary, detail = held[0]
                temporary.rename(destination)
                verification = "资源标识及MP4容器检查；未转码或验证全部帧" if info["kind"] == "video" else "内容MD5匹配；未打开或执行附件"
                self.store.execute("UPDATE media_attachments SET status='已导出',path=?,bytes=?,sha256=?,verification=? WHERE message_key=?",
                                   (relative, detail["bytes"], detail["sha256"], verification, identity))
            except MediaError as error:
                self.store.execute("UPDATE media_attachments SET status=?,error_type='MediaError' WHERE message_key=?", (str(error), identity))
            except OSError:
                self.store.execute("UPDATE media_attachments SET status='媒体写入失败',error_type='OSError' WHERE message_key=?", (identity,))
            finally:
                for temporary, _ in held:
                    temporary.unlink(missing_ok=True)
                self.store.commit()

    def close(self):
        """释放资源快照，把尚未处理的消息明确标为未完成。"""
        self.stack.close()
        self.store.execute("UPDATE media_attachments SET status='未完成' WHERE status='待处理'")

    def summary(self):
        """主库读取完成和媒体文件导出完整分别统计。"""
        counts = Counter(dict(self.store.execute("SELECT status,COUNT(*) FROM media_attachments GROUP BY status")))
        by_kind = {kind: {"enabled": enabled,
                          "total": self.store.execute("SELECT COUNT(*) FROM media_attachments WHERE kind=?", (kind,)).fetchone()[0],
                          "collected": self.store.execute("SELECT COUNT(*) FROM media_attachments WHERE kind=? AND status='已导出'", (kind,)).fetchone()[0]}
                   for kind, enabled in self.enabled.items()}
        return {"enabled": True, "complete": sum(counts.values()) == counts["已导出"], "statuses": dict(counts),
                "kinds": by_kind, "resource_status": self.resource_status,
                "files": {path: {"bytes": size, "sha256": digest} for path, size, digest in
                          self.store.execute("SELECT path,bytes,sha256 FROM media_attachments WHERE status='已导出'")},
                "limitation": "仅复制可靠关联的本机文件，单文件上限1GiB；视频仅检查MP4容器，不解密、不转码、不联网补取；语音和实况未实现。"}
