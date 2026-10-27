# -*- coding: utf-8 -*-
"""在单次手动批量中有界复用只读快照，结束或取消时统一释放。"""

from contextlib import ExitStack, contextmanager

from bootstrap import check_cancel

MAX_CACHED_BYTES = 256 * 1024 * 1024
MAX_CACHED_DATABASES = 8


class BatchSource:
    """委托原账号读取能力，只冻结分片列表并缓存有限数量的内存连接。"""

    def __init__(self, source, lifetime):
        self.source, self.lifetime = source, lifetime
        self.files, self.connections = tuple(source.shards()), {}
        self.cached_bytes = 0

    def __getattr__(self, name):
        """账号、联系人及缓存授权沿用原来源，不增加新的读取权限。"""
        return getattr(self.source, name)

    def shards(self):
        """批量期间新增的分片留待下一次手动任务，不中途改变计划范围。"""
        return list(self.files)

    @contextmanager
    def open_db(self, relative, cancel=None):
        """不驱逐仍被使用的连接；超出预算时临时打开并及时关闭。"""
        check_cancel(cancel)
        if relative in self.connections:
            connection, metadata = self.connections[relative]
            yield connection, dict(metadata)
            return
        with ExitStack() as once:
            connection, metadata = once.enter_context(self.source.open_db(relative, cancel))
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            size = pages * connection.execute("PRAGMA page_size").fetchone()[0]
            if len(self.connections) < MAX_CACHED_DATABASES and self.cached_bytes + size <= MAX_CACHED_BYTES:
                self.lifetime.enter_context(once.pop_all())
                self.connections[relative] = connection, dict(metadata)
                self.cached_bytes += size
            yield connection, dict(metadata)


@contextmanager
def batch_source(source):
    """只在本批任务存活期间持有连接，不跨任务缓存聊天明文。"""
    with ExitStack() as lifetime:
        session = BatchSource(source, lifetime)
        try:
            yield session
        finally:
            session.connections.clear()
