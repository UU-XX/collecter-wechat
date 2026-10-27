# -*- coding: utf-8 -*-
"""通过系统文件占用信息关联微信进程与账号，不读取数据库正文或进程内存。"""

import ctypes
import os
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path


class UniqueProcess(ctypes.Structure):
    """系统进程身份同时包含编号与启动时间。"""

    _fields_ = [("pid", wt.DWORD), ("started", wt.FILETIME)]


class ProcessInfo(ctypes.Structure):
    """与 Windows 文件占用查询返回的进程结构保持一致。"""

    _fields_ = [("process", UniqueProcess), ("app_name", wt.WCHAR * 256),
                ("service_name", wt.WCHAR * 64), ("app_type", ctypes.c_int),
                ("status", wt.ULONG), ("session_id", wt.DWORD), ("restartable", wt.BOOL)]


def using_processes(files):
    """仅注册文件并查询占用者；不调用关闭、重启应用或读内存的接口。"""
    if not files:
        return set()
    if os.name != "nt":
        raise OSError("当前系统不支持此文件占用查询")
    library = ctypes.WinDLL("rstrtmgr.dll")
    library.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.LPWSTR]
    library.RmStartSession.restype = wt.DWORD
    library.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT, ctypes.POINTER(wt.LPCWSTR),
                                            wt.UINT, ctypes.POINTER(UniqueProcess), wt.UINT, ctypes.POINTER(wt.LPCWSTR)]
    library.RmRegisterResources.restype = wt.DWORD
    library.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT),
                                 ctypes.POINTER(ProcessInfo), ctypes.POINTER(wt.DWORD)]
    library.RmGetList.restype = wt.DWORD
    library.RmEndSession.argtypes = [wt.DWORD]
    library.RmEndSession.restype = wt.DWORD
    session, key = wt.DWORD(), ctypes.create_unicode_buffer(33)
    if library.RmStartSession(ctypes.byref(session), 0, key):
        raise OSError("系统文件占用查询暂不可用")
    try:
        names = (wt.LPCWSTR * len(files))(*(str(Path(file).resolve()) for file in files))
        if library.RmRegisterResources(session, len(files), names, 0, None, 0, None):
            raise OSError("系统文件占用登记失败")
        needed, count, reasons = wt.UINT(), wt.UINT(), wt.DWORD()
        result = library.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        for _ in range(3):
            if result == 0:
                return set()
            if result != 234 or not 0 < needed.value <= 256:
                raise OSError("系统文件占用查询未完成")
            entries = (ProcessInfo * needed.value)()
            count.value = needed.value
            result = library.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reasons))
            if result == 0:
                if count.value > len(entries):
                    raise OSError("系统文件占用结果异常")
                return {int(entries[index].process.pid) for index in range(count.value)}
        raise OSError("查询期间进程变化，请手动刷新")
    finally:
        library.RmEndSession(session)


@dataclass(frozen=True)
class AccountChoice:
    """文件占用可以关联进程，但不能用来宣称网络在线或登录成功。"""

    root: Path
    status: str
    process_ids: tuple = ()


def account_choices(roots, processes):
    """仅把属于当前用户的微信占用视为关联，其他进程和不明情况均不猜测。"""
    running = {pid for pid, _ in processes}
    choices = []
    for root in roots:
        root, matched = Path(root), set()
        status = "本地历史" if not running else "归属待确认"
        if running:
            try:
                # 每个账号只登记一组数据库文件，限定数量，不打开或解密文件。
                files = [root / "contact/contact.db"]
                shards = sorted((root / "message").glob("message_*.db"), key=lambda path: path.name, reverse=True)
                files.extend(shards[:63])
                matched = using_processes([file for file in files if file.is_file()]) & running
                if matched:
                    status = "进程正在使用"
            except (OSError, ValueError):
                status = "占用信息不可用"
        choices.append(AccountChoice(root, status, tuple(sorted(matched))))
    return sorted(choices, key=lambda item: (not bool(item.process_ids), str(item.root).casefold()))
