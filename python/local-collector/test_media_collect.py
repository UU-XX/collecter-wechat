# -*- coding: utf-8 -*-
"""使用固定纯黑视频和合成附件验证本地导出，不读取真实微信。"""

import csv
import hashlib
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from bootstrap import Cancelled
from collector_core import collect, parse_range
from media_export import media_html, safe_media_path
from media_files import copy_candidate
from test_collector import FakeSource
from wechat_source import Conversation

VIDEO_FIXTURE = Path(__file__).with_name("test-fixtures") / "synthetic-16x16.mp4"


class MediaSource(FakeSource):
    """模拟当前会话和另一个会话的资源库，编号相同也不能串群。"""

    def __init__(self, root, conversation, rows, resources):
        super().__init__(conversation, rows)
        self.root, self.resources, self.permissions = root, resources, []

    def prepare(self, notify, cancel, include_resources=False, read_memory=True):
        """记录内存权限供断言，合成来源永远不访问真实进程。"""
        self.permissions.append(read_memory)

    @contextmanager
    def open_db(self, shard, cancel=None):
        """资源查询走独立表，其余消息读取复用现有合成分片。"""
        if Path(shard).name != "message_resource.db":
            with super().open_db(shard, cancel) as result:
                yield result
            return
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE ChatName2Id(user_name TEXT)")
        connection.executemany("INSERT INTO ChatName2Id VALUES (?)", [(self.conversation.username,), ("other@chatroom",)])
        connection.execute("CREATE TABLE MessageResourceInfo(chat_id INTEGER,message_local_id INTEGER,message_svr_id INTEGER,message_create_time INTEGER,message_local_type INTEGER,packed_info BLOB)")
        connection.executemany("INSERT INTO MessageResourceInfo VALUES (?,?,?,?,?,?)", self.resources)
        try:
            yield connection, {}
        finally:
            connection.close()


class MediaCollectionTests(unittest.TestCase):
    """验证完整导出、资源身份、内容校验、失败状态与原文件保护。"""

    def setUp(self):
        """全部缓存与输出都位于本测试独占的临时目录。"""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "account/db_storage"
        self.root.mkdir(parents=True)
        self.conversation = Conversation("synthetic-media@chatroom", "合成媒体会话", "群聊")
        self.window = parse_range("2025-01-01", "2025-01-01")
        self.time = self.window.start
        self.video = VIDEO_FIXTURE.read_bytes()
        self.video_id = "a" * 32
        self.file = "仅用于测试的附件\n".encode("utf-8")
        self.file_id = hashlib.md5(self.file).hexdigest()
        self.video_folder = self.root.parent / "msg/video/2025-01"
        self.file_folder = self.root.parent / "msg/attach" / hashlib.md5(self.conversation.username.encode()).hexdigest() / "2025-01/File"
        self.video_folder.mkdir(parents=True)
        self.file_folder.mkdir(parents=True)
        self.video_path = self.video_folder / (self.video_id + ".mp4")
        self.file_path = self.file_folder / "合成附件.txt"
        self.video_path.write_bytes(self.video)
        self.file_path.write_bytes(self.file)

    def tearDown(self):
        """仅清理本测试创建的目录对象。"""
        self.temp.cleanup()

    def file_xml(self, name="合成附件.txt", digest=None, extension="txt", size=None):
        """构造包含私密候选与远程地址的卡片，验证这些字段不被导出。"""
        digest = self.file_id if digest is None else digest
        size = len(self.file) if size is None else size
        return f'<msg><appmsg><title>{name}</title><type>6</type><md5>{digest}</md5><appattach><fileext>{extension}</fileext><totallen>{size}</totallen><aeskey>synthetic-private-marker</aeskey><cdnattachurl>https://example.invalid/private</cdnattachurl></appattach></appmsg></msg>'

    def source(self, video_xml=None, file_xml=None, resources=None, rows=None):
        """合成消息与资源映射共用独立的模拟来源，不依赖图片模块。"""
        if rows is None:
            video_xml = video_xml if video_xml is not None else f'<msg><videomsg md5="{self.video_id}" aeskey="synthetic-private-marker"/></msg>'
            file_xml = file_xml if file_xml is not None else self.file_xml()
            rows = [(1, 11, 43, self.time, 1, 0, "member:\n" + video_xml),
                    (2, 12, 49, self.time + 1, 1, 0, "member:\n" + file_xml)]
        return MediaSource(self.root, self.conversation, rows, resources or [])

    def run_collect(self, source=None, **options):
        """默认明确启用两类媒体，其他选项仍使用原有安全默认值。"""
        source = source or self.source()
        directory, report = collect(source, self.conversation, self.window, self.base / "exports",
                                    include_videos=True, include_files=True, **options)
        records = [json.loads(line) for line in (directory / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        return directory, report, records

    def test_fixed_video_file_and_all_export_formats(self):
        source = self.source()
        directory, report, records = self.run_collect(source)
        self.assertEqual(source.permissions, [False])
        self.assertTrue(report["media"]["complete"])
        self.assertEqual(report["media"]["kinds"]["video"]["collected"], 1)
        self.assertEqual(report["media"]["kinds"]["file"]["collected"], 1)
        for row, original in zip(records, (self.video, self.file)):
            item = row["media"]
            self.assertEqual(item["status"], "已导出")
            self.assertEqual((directory / item["path"]).read_bytes(), original)
            self.assertEqual(item["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(item["bytes"], len(original))
            self.assertIn(item["path"], (directory / "聊天记录.md").read_text(encoding="utf-8"))
            self.assertIn(item["path"], (directory / "消息记录.txt").read_text(encoding="utf-8"))
            self.assertEqual(report["media"]["files"][item["path"]]["sha256"], item["sha256"])
        page = (directory / "聊天记录.html").read_text(encoding="utf-8")
        self.assertIn('<video controls preload="none"', page)
        self.assertIn('media-src', page)
        self.assertIn('download>下载本地附件', page)
        with (directory / "messages.csv").open(encoding="utf-8-sig", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(csv_rows[1]["媒体路径"], records[1]["media"]["path"])
        for name in ("messages.jsonl", "消息记录.txt", "聊天记录.html", "聊天记录.md", "messages.csv", "采集说明.json"):
            text = (directory / name).read_text(encoding="utf-8-sig")
            self.assertNotIn("synthetic-private-marker", text)
            self.assertNotIn("example.invalid", text)
            self.assertNotIn(str(self.root), text)
        self.assertEqual(self.video_path.read_bytes(), self.video)
        self.assertEqual(self.file_path.read_bytes(), self.file)

    def test_media_off_never_prepares_or_copies(self):
        source = self.source()
        with patch("media_collect.MediaCollector", side_effect=AssertionError("默认不应读取媒体")):
            directory, report = collect(source, self.conversation, self.window, self.base / "exports")
        self.assertFalse(report["media"]["enabled"])
        self.assertEqual(source.permissions, [])
        self.assertFalse((directory / "videos").exists())
        self.assertFalse((directory / "files").exists())

    def test_resource_mapping_requires_chat_time_type_and_server_id(self):
        packed = b"\x12\x22\x0a\x20" + self.video_id.encode()
        for resource in ((2, 1, 11, self.time, 43, packed), (1, 1, 99, self.time, 43, packed),
                         (1, 1, 11, self.time + 1, 43, packed), (1, 1, 11, self.time, 3, packed)):
            with self.subTest(resource=resource[:5]):
                _, report, rows = self.run_collect(self.source(video_xml="<msg><videomsg/></msg>", resources=[resource]))
                self.assertEqual(rows[0]["media"]["status"], "缺少可验证媒体摘要")
                self.assertFalse(report["media"]["complete"])
        _, _, rows = self.run_collect(self.source(video_xml="<msg><videomsg/></msg>", resources=[(1, 1, 11, self.time, 43, packed)]))
        self.assertEqual(rows[0]["media"]["status"], "已导出")

    def test_file_basename_without_digest_is_not_enough(self):
        _, report, rows = self.run_collect(self.source(file_xml=self.file_xml(digest="")))
        self.assertEqual(rows[1]["media"]["status"], "缺少可验证媒体摘要")
        self.assertFalse(report["media"]["complete"])

    def test_account_file_cache_requires_selected_message_content_hash(self):
        folder = self.root.parent / "msg/file/2025-01"
        folder.mkdir(parents=True)
        self.file_path.rename(folder / self.file_path.name)
        _, _, rows = self.run_collect()
        self.assertEqual(rows[1]["media"]["status"], "已导出")
        (folder / self.file_path.name).write_bytes(b"wrong-file")
        _, _, rows = self.run_collect()
        self.assertNotEqual(rows[1]["media"]["status"], "已导出")

    def test_video_file_identifier_from_optional_message_metadata(self):
        """覆盖 XML 标识缺失、新版消息在二进制列保存缓存标识的情况。"""
        source = self.source(video_xml="<msg><videomsg/></msg>")
        original = source.open_db
        table = "Msg_" + hashlib.md5(self.conversation.username.encode()).hexdigest()
        @contextmanager
        def open_db(shard, cancel=None):
            """只为合成消息表增加可选字段，不改变真实来源。"""
            with original(shard, cancel) as (connection, details):
                if Path(shard).name != "message_resource.db":
                    connection.execute(f'ALTER TABLE "{table}" ADD COLUMN packed_info_data BLOB')
                    connection.execute(f'UPDATE "{table}" SET packed_info_data=? WHERE local_type=43', (b"\x12\x20" + self.video_id.encode(),))
                yield connection, details
        source.open_db = open_db
        _, _, rows = self.run_collect(source)
        self.assertEqual(rows[0]["media"]["status"], "已导出")

    def test_wrong_file_content_and_size_are_reported(self):
        _, _, rows = self.run_collect(self.source(file_xml=self.file_xml(digest="b" * 32)))
        self.assertEqual(rows[1]["media"]["status"], "文件摘要与消息不符")
        _, _, rows = self.run_collect(self.source(file_xml=self.file_xml(size=len(self.file) + 1)))
        self.assertEqual(rows[1]["media"]["status"], "媒体大小与消息不符")

    def test_missing_and_encrypted_video_do_not_drop_messages(self):
        self.video_path.unlink()
        _, report, rows = self.run_collect()
        self.assertEqual(rows[0]["media"]["status"], "本机媒体缺失")
        self.video_path.write_bytes(b"synthetic-encrypted-video")
        _, report, rows = self.run_collect()
        self.assertNotEqual(rows[0]["media"]["status"], "已导出")
        self.assertEqual(report["count"], 2)
        self.assertEqual(rows[1]["media"]["status"], "已导出")

    def test_truncated_mp4_is_not_marked_exported(self):
        self.video_path.write_bytes(self.video[:-10])
        directory, _, rows = self.run_collect()
        self.assertIn("损坏", rows[0]["media"]["status"])
        self.assertFalse(list(directory.rglob("*.partial")))

    def test_duplicate_cache_is_ok_but_different_content_is_ambiguous(self):
        other = self.video_folder.parent / "2025-02" / self.video_path.name
        other.parent.mkdir()
        other.write_bytes(self.video)
        _, _, rows = self.run_collect()
        self.assertEqual(rows[0]["media"]["status"], "已导出")
        other.write_bytes(self.video + struct.pack(">I4s", 8, b"free"))
        directory, report, rows = self.run_collect()
        self.assertEqual(rows[0]["media"]["status"], "资源关联不唯一")
        self.assertEqual(report["media"]["kinds"]["video"]["collected"], 0)
        self.assertFalse(list((directory / "videos").iterdir()))

    def test_other_conversation_file_is_not_searched(self):
        other = self.root.parent / "msg/attach" / hashlib.md5(b"other@chatroom").hexdigest() / "2025-01/File"
        other.mkdir(parents=True)
        self.file_path.rename(other / self.file_path.name)
        _, _, rows = self.run_collect()
        self.assertEqual(rows[1]["media"]["status"], "本机媒体缺失")

    def test_resolved_path_escape_is_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(self.file)
        original = Path.resolve
        def redirected(path, *args, **kwargs):
            """模拟文件链接越界，不能实际创建或读取用户目录中的链接。"""
            return outside if path == self.file_path else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            _, _, rows = self.run_collect()
        self.assertEqual(rows[1]["media"]["status"], "媒体路径关联无效")
        self.assertEqual(outside.read_bytes(), self.file)

    def test_existing_media_destination_is_not_overwritten(self):
        destination = None
        def notify(text):
            """在消息已落盘后模拟同一任务目标文件冲突。"""
            nonlocal destination
            if "正在导出本机视频" in text and destination is None:
                task = next((self.base / "exports").iterdir())
                destination = task / "videos" / (hashlib.sha256(b"s:11").hexdigest()[:24] + ".mp4")
                destination.parent.mkdir()
                destination.write_bytes(b"existing-export")
        _, _, rows = self.run_collect(notify=notify)
        self.assertEqual(rows[0]["media"]["status"], "导出目标已存在，未覆盖")
        self.assertEqual(destination.read_bytes(), b"existing-export")

    def test_media_initialization_failure_preserves_video_failure_in_database(self):
        with patch("media_collect.MediaCollector", side_effect=RuntimeError("synthetic-private-error")):
            directory, report, rows = self.run_collect()
        self.assertEqual(report["count"], 2)
        self.assertFalse(report["media"]["complete"])
        self.assertEqual(rows[0]["media"]["status"], "媒体模块未能启动")
        self.assertNotIn("synthetic-private-error", (directory / "采集说明.json").read_text(encoding="utf-8"))

    def test_non_file_card_is_not_treated_as_attachment(self):
        _, report, rows = self.run_collect(self.source(file_xml="<msg><appmsg><title>普通链接</title><type>5</type></appmsg></msg>"))
        self.assertNotIn("media", rows[1])
        self.assertEqual(report["media"]["kinds"]["file"]["total"], 0)

    def test_unparseable_card_is_not_silently_reported_complete(self):
        _, report, rows = self.run_collect(self.source(file_xml="<msg><appmsg><type>6</type>"))
        self.assertEqual(rows[1]["media"]["status"], "媒体消息结构暂不支持")
        self.assertFalse(report["media"]["complete"])

    def test_file_metadata_whitespace_does_not_hide_attachment(self):
        xml = self.file_xml().replace("<type>6</type>", "<type> 6 </type>").replace("<md5>", "<md5>\n").replace("</md5>", "\n</md5>")
        _, _, rows = self.run_collect(self.source(file_xml=xml))
        self.assertEqual(rows[1]["media"]["status"], "已导出")

    def test_file_path_traversal_and_xml_entities_are_rejected(self):
        _, _, rows = self.run_collect(self.source(file_xml=self.file_xml(name="../escape.txt")))
        self.assertEqual(rows[1]["media"]["status"], "附件文件名暂不支持")
        _, _, rows = self.run_collect(self.source(video_xml='<!DOCTYPE msg [<!ENTITY x "bad">]><msg><videomsg/></msg>'))
        self.assertEqual(rows[0]["media"]["status"], "媒体消息结构暂不支持")

    def test_active_attachment_extension_is_exported_as_bin(self):
        self.file_path.rename(self.file_folder / "合成附件.html")
        directory, _, rows = self.run_collect(self.source(file_xml=self.file_xml(name="合成附件.html", extension="html")))
        self.assertTrue(rows[1]["media"]["path"].endswith(".bin"))
        self.assertEqual((directory / rows[1]["media"]["path"]).read_bytes(), self.file)

    def test_csv_formula_filename_is_plain_text(self):
        self.file_path.rename(self.file_folder / "=1+1.txt")
        directory, _, _ = self.run_collect(self.source(file_xml=self.file_xml(name="=1+1.txt")))
        with (directory / "messages.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[1]["原附件名"], "'=1+1.txt")

    def test_cancel_before_media_copy_keeps_task_and_marks_pending(self):
        event = threading.Event()
        def notify(text):
            """只在媒体阶段取消，消息必须已经进入任务数据库。"""
            if "正在导出本机视频" in text:
                event.set()
        directory, report, rows = self.run_collect(notify=notify, cancel=event)
        self.assertEqual(report["status"], "已取消，结果不完整")
        self.assertEqual(report["count"], 2)
        self.assertEqual(rows[0]["media"]["status"], "未完成")
        self.assertFalse(list(directory.rglob("*.partial")))

    def test_cancel_mid_file_copy_removes_only_own_partial_file(self):
        data = b"x" * (2 * 1024 * 1024)
        path = self.file_folder / "合成大文件.txt"
        path.write_bytes(data)
        checks = 0
        def checkpoint(event):
            """第一块落盘后模拟取消，验证部分文件不会被发布。"""
            nonlocal checks
            checks += 1
            if checks == 3:
                raise Cancelled()
        destination = self.base / "partial-output"
        with patch("media_files.check_cancel", side_effect=checkpoint), self.assertRaises(Cancelled):
            copy_candidate(path, self.file_folder, destination, "file", {hashlib.md5(data).hexdigest()}, len(data), None)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(path.read_bytes(), data)

    def test_size_limit_and_copy_write_error_keep_messages(self):
        with patch("media_files.MAX_MEDIA_BYTES", 1):
            _, report, rows = self.run_collect()
        self.assertEqual(rows[0]["media"]["status"], "媒体超过本版本大小限制")
        self.assertEqual(report["count"], 2)
        with patch("media_collect.copy_candidate", side_effect=OSError("synthetic-private-error")):
            directory, _, rows = self.run_collect()
        self.assertEqual(rows[0]["media"]["status"], "媒体文件无法读取")
        self.assertNotIn("synthetic-private-error", (directory / "messages.jsonl").read_text(encoding="utf-8"))

    def test_html_escapes_names_and_rejects_remote_or_active_links(self):
        item = {"kind": "file", "status": "<script>bad()</script>", "original_name": '<img src="bad">', "path": "https://example.invalid/remote"}
        text = media_html(item)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("href=", text)
        self.assertFalse(safe_media_path("files/" + "a" * 24 + ".html"))
        self.assertFalse(safe_media_path("files/../outside.pdf"))

    def test_media_metadata_remains_in_task_database(self):
        directory, _, _ = self.run_collect()
        with closing(sqlite3.connect(directory / "records.sqlite3")) as connection:
            rows = connection.execute("SELECT kind,status,bytes,sha256 FROM media_attachments ORDER BY kind").fetchall()
        self.assertEqual([row[0] for row in rows], ["file", "video"])
        self.assertTrue(all(row[1] == "已导出" and row[2] > 0 and len(row[3]) == 64 for row in rows))


if __name__ == "__main__":
    unittest.main()
