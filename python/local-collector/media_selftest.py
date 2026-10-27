# -*- coding: utf-8 -*-
"""冻结程序以固定视频和合成附件验证完整链路，绝不访问真实账号。"""

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def media_self_test(directory):
    """合成消息源只提供一个内存数据库，资源库缺失时验证明确摘要回退。"""
    from collector_core import collect, parse_range
    from wechat_source import Conversation

    directory = Path(directory)
    fixture = Path(__file__).parent / "test-fixtures/synthetic-16x16.mp4"
    if not fixture.is_file():
        fixture = Path(__file__).parent / "self-test/synthetic-16x16.mp4"
    video = fixture.read_bytes()
    file_data = "合成冻结附件\n".encode("utf-8")
    video_id, file_id = hashlib.md5(video).hexdigest(), hashlib.md5(file_data).hexdigest()
    root = directory / "synthetic-media-account/db_storage"
    root.mkdir(parents=True)
    video_folder, file_folder = root.parent / "msg/video/2025-01", root.parent / "msg/file/2025-01"
    video_folder.mkdir(parents=True)
    file_folder.mkdir(parents=True)
    (video_folder / (video_id + ".mp4")).write_bytes(video)
    (file_folder / "合成附件.txt").write_bytes(file_data)
    conversation = Conversation("synthetic-media", "合成媒体自检", "个人")
    table = "Msg_" + hashlib.md5(conversation.username.encode()).hexdigest()
    window = parse_range("2025-01-01", "2025-01-01")

    def prepare(notify, cancel, include_resources=False, read_memory=False):
        """任意内存授权都会让自检失败，合成来源不读取本机配置。"""
        assert include_resources and not read_memory

    @contextmanager
    def open_db(relative, cancel=None):
        """只返回合成消息库，其他数据库明确不存在。"""
        if Path(relative).name == "message_resource.db":
            raise FileNotFoundError("合成资源库不存在")
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            connection.execute("INSERT INTO Name2Id VALUES ('synthetic')")
            connection.execute(f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,WCDB_CT_message_content INTEGER,message_content TEXT)')
            connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', [
                (1, 1, 43, window.start, 1, 0, f'<msg><videomsg md5="{video_id}"/></msg>'),
                (2, 2, 49, window.start + 1, 1, 0, f'<msg><appmsg><title>合成附件.txt</title><type>6</type><md5>{file_id}</md5><appattach><fileext>txt</fileext><totallen>{len(file_data)}</totallen></appattach></appmsg></msg>')])
            yield connection, {}
        finally:
            connection.close()

    source = SimpleNamespace(root=root, names={"synthetic": "合成人员"}, contact_metadata={},
                             prepare=prepare, open_db=open_db, shards=lambda: ["message/message_0.db"])
    output, report = collect(source, conversation, window, directory / "synthetic-media-exports", include_videos=True, include_files=True)
    assert report["count"] == 2 and report["media"]["complete"]
    assert (output / "聊天记录.html").is_file()
    for relative, detail in report["media"]["files"].items():
        data = (output / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == detail["sha256"]
        assert data in (video, file_data)
    assert not list(output.rglob("*.partial"))
