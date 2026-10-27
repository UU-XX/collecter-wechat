# -*- coding: utf-8 -*-
"""保存上次手动测试状态，标题读取缓存不会探测网络或读取微信。"""

import datetime as dt
import hashlib
import json
from pathlib import Path

from bootstrap import PRIVATE
from local_storage import _protect, atomic_write
from model_settings import MODEL_ERRORS, ModelError, validate_config

STATUS_FILE = PRIVATE / "model-test-status.dat"
ZONE = dt.timezone(dt.timedelta(hours=8))


def config_identity(config):
    """把地址、认证、协议及模型共同绑定为摘要，避免显示其他配置的测试结果。"""
    value = json.dumps(validate_config(config), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_test_status(config, result=None, error_code=None, path=STATUS_FILE):
    """独立加密写入状态，不修改已有模型配置或保存服务端正文。"""
    available = error_code is None
    if available and (not isinstance(result, dict) or result.get("available") is not True):
        raise ModelError("CONFIG")
    latency = result.get("latency_ms", 0) if available else 0
    if not isinstance(latency, int) or isinstance(latency, bool) or not 0 <= latency <= 3600000:
        raise ModelError("CONFIG")
    value = {"schema": 1, "identity": config_identity(config), "available": available,
             "checked_at": dt.datetime.now(ZONE).isoformat(), "latency_ms": latency,
             "error_code": error_code if error_code in MODEL_ERRORS else "RESPONSE" if not available else ""}
    atomic_write(path, _protect(json.dumps(value, ensure_ascii=False).encode("utf-8")))


def load_test_status(config, path=STATUS_FILE):
    """只返回与当前配置完全匹配的历史测试，损坏时保留原文件。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 16 * 1024:
            raise ValueError()
        value = json.loads(_protect(path.read_bytes(), decrypt=True))
        if not isinstance(value, dict) or value.get("schema") != 1 or type(value.get("available")) is not bool:
            raise ValueError()
        if value.get("identity") != config_identity(config):
            return None
        time = dt.datetime.fromisoformat(value["checked_at"])
        latency = value["latency_ms"]
        if time.tzinfo is None or type(latency) is not int or not 0 <= latency <= 3600000:
            raise ValueError()
        if not value["available"] and value.get("error_code") not in MODEL_ERRORS:
            raise ValueError()
        return value
    except (ValueError, RuntimeError, KeyError, TypeError, OSError):
        raise ModelError("SECRET") from None
