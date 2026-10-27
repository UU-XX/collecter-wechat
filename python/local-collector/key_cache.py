# -*- coding: utf-8 -*-
"""数据库密钥仅新增加密缓存，保留旧明文文件供用户自行处置。"""

import json
from pathlib import Path

from local_storage import _protect, atomic_write


def load_keys(directory):
    """优先读取加密缓存，损坏时明确失败，不悄悄回退旧密钥。"""
    directory = Path(directory)
    encrypted = directory / "keys.dat"
    legacy = directory / "keys.json"
    try:
        if encrypted.exists():
            value = json.loads(_protect(encrypted.read_bytes(), decrypt=True))
        elif legacy.exists():
            value = json.loads(legacy.read_text(encoding="utf-8"))
        else:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("_db_dir"), str):
            raise ValueError("密钥缓存格式无效")
        return value
    except (ValueError, TypeError, RuntimeError) as exc:
        raise RuntimeError("数据库密钥缓存无法读取，原文件已保留；请核对当前 Windows 用户和缓存备份。") from exc


def save_keys(directory, value):
    """只更新本工具的加密文件，不写入或删除旧明文缓存。"""
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    atomic_write(Path(directory) / "keys.dat", _protect(data))
