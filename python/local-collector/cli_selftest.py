# -*- coding: utf-8 -*-
"""控制台发行版使用隔离目录和合成数据自检，不打开桌面窗口。"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


def self_test(directory):
    """仅检查打包依赖及本地输出，禁止接触真实账号和内存。"""
    from Crypto.Cipher import AES
    import zstandard
    import cli
    from bootstrap import PRIVATE
    from collector_core import LIMITATION, create_store, export_results, store_rows
    from group_probe import load_tool
    from key_cache import load_keys, save_keys

    directory = Path(directory).resolve()
    if not PRIVATE.resolve().is_relative_to(directory):
        raise ValueError("自检必须使用隔离的本机数据目录。")
    checks = []
    assert callable(load_tool().verify_enc_key)
    checks.append("审阅读取模块校验及加载")
    sample = b"synthetic-cli-check"
    assert zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(sample)) == sample
    cipher = AES.new(bytes(16), AES.MODE_ECB)
    assert cipher.decrypt(cipher.encrypt(bytes(16))) == bytes(16)
    checks.append("压缩及加密依赖")
    keys = {"_db_dir": str(directory), "synthetic": {"enc_key": "33" * 32}}
    cache = directory / "synthetic-key-cache"
    save_keys(cache, keys)
    assert load_keys(cache) == keys
    assert ("33" * 32).encode() not in (cache / "keys.dat").read_bytes()
    checks.append("合成密钥缓存加密恢复")
    output = directory / "synthetic-exports"
    output.mkdir()
    store = create_store(directory / "synthetic-records.sqlite3")
    try:
        store_rows(store, [(1, 1, 1, 1735689600, "synthetic", 0, "合成控制台自检")], "synthetic", {}, False, None)
        store.commit()
        report = {"conversation_name": "合成自检", "kind": "个人", "status": "主库存档采集完成", "count": 1,
                  "requested_start": "2025-01-01", "requested_end": "2025-01-01", "limitation": LIMITATION}
        export_results(store, output, report)
        assert report["exports"]["status"] == "complete"
        assert set(report["exports"]["files"]) == {"消息记录.txt", "messages.jsonl", "聊天记录.md", "messages.csv"}
    finally:
        store.close()
    checks.append("任务数据库与四种文字导出")
    from media_selftest import media_self_test
    media_self_test(directory)
    checks.append("固定MP4及合成附件完整导出和摘要校验")
    # 通过失败哨兵证明显示标识和退出菜单均不发现账号或启动读取。
    with patch("cli.find_accounts", side_effect=AssertionError("不应发现账号")), \
            patch("cli.own_processes", side_effect=AssertionError("不应查询微信进程")), \
            patch("cli.WechatSource", side_effect=AssertionError("不应读取微信")), \
            patch("cli.collect", side_effect=AssertionError("不应开始任务")), redirect_stdout(io.StringIO()):
        assert cli.main(["--logo"]) == 0
        with patch("builtins.input", return_value="0"):
            assert cli.interactive() == 0
    checks.append("标识及退出菜单不触发读取")
    from read_errors import ReadError
    error_output = io.StringIO()
    with redirect_stderr(error_output), redirect_stdout(io.StringIO()), \
            patch("cli.prepare_source", side_effect=ReadError("PROCESS_NOT_RUNNING")):
        assert cli.main(["conversations", "--account", str(directory / "synthetic-account")]) == 2
    assert "PROCESS_NOT_RUNNING" in error_output.getvalue() and "请先启动并登录微信" in error_output.getvalue()
    checks.append("读取错误码及安全恢复提示")
    from process_accounts import account_choices
    synthetic_root = directory / "synthetic-lock-account/db_storage"
    (synthetic_root / "contact").mkdir(parents=True)
    (synthetic_root / "contact/contact.db").write_bytes(b"synthetic-lock-check")
    with patch("process_accounts.using_processes", return_value={123}):
        choices = account_choices([synthetic_root], [(123, 1)])
    assert choices[0].process_ids == (123,) and choices[0].status == "进程正在使用"
    checks.append("进程与账号目录关联及未知状态边界")
    from wal_selftest import wal_self_test
    wal_self_test(directory)
    checks.append("合成加密WAL提交记录及只读快照")
    from workflow_selftest import workflow_self_test
    workflow_self_test(directory)
    checks.append("批量取消续导、晚到消息增量及本地浏览总体报告")
    from model_selftest import model_self_test
    model_self_test(directory)
    checks.append("合成本地接口五类模型协议、分页及配置加密")
    from model_selftest import daily_model_self_test
    daily_model_self_test(directory)
    checks.append("逐日模型分析、总体汇总、思考隐藏及失败留存诊断")
    assert "tkinter" not in sys.modules and "app" not in sys.modules
    assert not any(name in sys.modules for name in ("image_collect", "image_decode", "image_probe", "image_errors"))
    checks.append("控制台运行不加载桌面界面或图片导出模块")
    return {"ok": True, "frozen": bool(getattr(sys, "frozen", False)), "checks": checks}
