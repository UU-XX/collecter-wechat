# -*- coding: utf-8 -*-
"""复用本机已验证的读取工具，不安装全局依赖。"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOOLS = BASE.parent / "reviewed-tools"
PRIVATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WechatLocalCollector"
sys.dont_write_bytecode = True
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "python-deps"))


class Cancelled(Exception):
    """用户在安全边界取消任务。"""


def check_cancel(event):
    """后台任务不直接访问界面，只检查取消标记。"""
    if event is not None and event.is_set():
        raise Cancelled()


def quote_identifier(name):
    """数据库标识符转义，查询值另行参数绑定。"""
    return '"' + name.replace('"', '""') + '"'
