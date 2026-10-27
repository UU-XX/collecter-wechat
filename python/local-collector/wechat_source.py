# -*- coding: utf-8 -*-
"""发现本机账号，读取联系人及隔离的主数据库副本。"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bootstrap import PRIVATE, Cancelled, check_cancel
from group_probe import load_tool
from wal_snapshot import open_archive
from key_cache import load_keys, save_keys
from read_errors import ReadError


@dataclass(frozen=True)
class Conversation:
    """使用真实会话标识绑定，显示时仅提供短编号区分重名。"""

    username: str
    name: str
    kind: str
    nickname: str = ""

    @property
    def short_id(self):
        return hashlib.sha256(self.username.encode()).hexdigest()[:8]


def find_accounts():
    """从微信自己的配置发现账号目录，不递归扫描用户全部磁盘。"""
    config = Path(os.environ.get("APPDATA", "")) / "Tencent/xwechat/config"
    roots = set()
    for ini in config.glob("*.ini"):
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                value = ini.read_text(encoding=encoding).strip()
                base = Path(value) / "xwechat_files"
                if base.is_dir():
                    roots.update(p.resolve() for p in base.glob("*/db_storage") if p.is_dir())
                break
            except (OSError, UnicodeError, ValueError):
                continue
    return sorted(roots, key=str)


def own_processes():
    """只允许读取属于当前登录用户的微信进程，不申请进程写入权限。"""
    command = (
        "$ErrorActionPreference='Stop'; $u=[Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        "$items=@(Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
        "ForEach-Object {$o=Invoke-CimMethod -InputObject $_ -MethodName GetOwner; "
        "if(($o.Domain+'\\'+$o.User) -eq $u){[pscustomobject]@{pid=$_.ProcessId; "
        "memory=[long]$_.WorkingSetSize/1024}}}); ConvertTo-Json -InputObject $items -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReadError("PROCESS_QUERY") from None
    if result.returncode:
        raise ReadError("PROCESS_QUERY")
    try:
        rows = json.loads(result.stdout.strip() or "[]")
        if not isinstance(rows, list):
            raise ValueError()
        processes = [(int(row["pid"]), int(row["memory"])) for row in rows]
        if any(pid <= 0 or memory < 0 for pid, memory in processes):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ReadError("PROCESS_QUERY") from None
    if not rows:
        raise ReadError("PROCESS_NOT_RUNNING")
    return sorted(processes, key=lambda row: -row[1])


class WechatSource:
    """原库只读；缓存及临时加密副本仅放用户私有目录。"""

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not (self.root / "contact/contact.db").is_file():
            raise ValueError("该目录不是有效的微信 db_storage 目录。")
        identity = hashlib.sha256(str(self.root).casefold().encode()).hexdigest()[:16]
        self.cache = PRIVATE / "accounts" / identity
        self.keys = {}
        self.names = {}
        self.contact_metadata = {}

    def shards(self):
        """枚举全部源分片，不能只枚举已拿到密钥的分片。"""
        return [str(p.relative_to(self.root)) for p in sorted((self.root / "message").glob("message_*.db"))
                if re.fullmatch(r"message_\d+\.db", p.name)]

    def _pages(self, include_resources=False):
        """按任务范围收集数据库首页，媒体任务额外验证资源库。"""
        files, salts = [], {}
        relatives = [str(Path("contact/contact.db")), *self.shards()]
        if include_resources and (self.root / "message/message_resource.db").is_file():
            relatives.append(str(Path("message/message_resource.db")))
        for relative in relatives:
            path = self.root / relative
            with path.open("rb") as stream:
                page = stream.read(4096)
            if len(page) != 4096:
                continue
            salt = page[:16].hex()
            files.append((relative, str(path), path.stat().st_size, salt, page))
            salts.setdefault(salt, []).append(relative)
        return files, salts

    def prepare(self, notify=lambda text: None, cancel=None, force=False, include_resources=False, read_memory=False):
        """验证缓存后复用；仅在必要时执行已经审阅的只读密钥读取路径。"""
        check_cancel(cancel)
        try:
            tool = load_tool()
        except (RuntimeError, OSError, ImportError):
            raise ReadError("TOOL_CHECK") from None
        try:
            files, salts = self._pages(include_resources)
            if not any(relative == str(Path("contact/contact.db")) for relative, *_ in files):
                raise ReadError("DATABASE_FILES")
        except OSError:
            raise ReadError("DATABASE_FILES") from None
        try:
            saved = load_keys(self.cache)
        except (RuntimeError, OSError):
            raise ReadError("CACHE_READ") from None
        # 仅复用本工具同一账号的缓存，不自动读取其他项目的历史目录。
        candidates = [saved] if saved is not None else []
        valid, retained = {}, {}
        for saved in candidates:
            try:
                if Path(saved.get("_db_dir", "")).resolve() != self.root:
                    continue
                for relative, _, _, _, page in files:
                    info = tool._get_key_info(saved, relative)
                    if info and tool.verify_enc_key(bytes.fromhex(info["enc_key"]), page):
                        valid[relative] = info
                # 普通文字任务仍保留此前验证的资源库密钥，避免丢失视频和附件读取能力。
                resource = str(Path("message/message_resource.db"))
                resource_path = self.root / resource
                if not include_resources and resource_path.is_file():
                    info = tool._get_key_info(saved, resource)
                    if info:
                        with resource_path.open("rb") as stream:
                            page = stream.read(4096)
                        if len(page) == 4096 and tool.verify_enc_key(bytes.fromhex(info["enc_key"]), page):
                            retained[resource] = info
            except (OSError, ValueError, KeyError, TypeError):
                continue
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ReadError("CACHE_WRITE") from None
        if read_memory and (force or len(valid) < len(files)):
            notify("正在验证本机读取权限；密钥仅在本机使用，不会显示。")
            processes = own_processes()
            tool.collect_db_files = lambda directory: (files, salts)
            tool._get_pids_windows = lambda: processes
            denied = set()
            def scan_status(*args, **kwargs):
                """仅识别固定的进程打开失败事件，其余第三方输出全部丢弃。"""
                check_cancel(cancel)
                if args and isinstance(args[0], str):
                    match = re.match(r"^\[WARN\] 无法打开进程 PID=(\d+)", args[0])
                    if match:
                        denied.add(int(match[1]))
            tool._print = scan_status
            temporary = self.cache / ("keys.pending-" + uuid.uuid4().hex + ".json")
            try:
                tool._scan_memory_raw_key(str(self.root), str(temporary))
                found = json.loads(temporary.read_text(encoding="utf-8"))
                # 扫描结果仍按所选账号的数据库首页复核，不能直接当作可用缓存。
                for relative, _, _, _, page in files:
                    info = tool._get_key_info(found, relative)
                    if info and tool.verify_enc_key(bytes.fromhex(info["enc_key"]), page):
                        valid[relative] = info
            except Cancelled:
                raise
            except Exception:
                if not valid:
                    code = "PROCESS_ACCESS" if denied >= {pid for pid, _ in processes} else "KEY_SCAN_FAILED"
                    raise ReadError(code) from None
                notify("部分分片未取得密钥；会在结果说明中列出。")
            finally:
                # 只移除本采集器创建的临时密钥文件，保留已有正式缓存。
                temporary.unlink(missing_ok=True)
        check_cancel(cancel)
        self.keys = dict(retained | valid, _db_dir=str(self.root))
        if not tool._get_key_info(self.keys, str(Path("contact/contact.db"))):
            raise ReadError("CONTACT_KEY_UNVERIFIED" if read_memory else "CONTACT_KEY_MISSING")
        try:
            save_keys(self.cache, self.keys)
        except (RuntimeError, OSError):
            raise ReadError("CACHE_WRITE") from None

    @contextmanager
    def open_db(self, relative, cancel=None):
        """复用经过验证的内存解密，临时目录关闭后自动释放加密副本。"""
        check_cancel(cancel)
        base = PRIVATE / "temporary"
        base.mkdir(parents=True, exist_ok=True)
        # Windows 应用容器可能只在实际文件上解析出重定向路径。
        # 通过本次创建的空文件确定实际父目录，避免混用两套路径。
        with tempfile.NamedTemporaryFile(prefix="location-", dir=base) as marker:
            base = Path(marker.name).resolve(strict=True).parent
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=base) as directory:
            run = Path(directory)
            # 清理前验证临时目录位于本采集器自己的目录之内。
            try:
                run.resolve(strict=True).relative_to(base.resolve(strict=True))
            except ValueError:
                raise RuntimeError("临时目录路径校验未通过，请重启采集器后重试。") from None
            # 密钥直接传给内存解密包装器，不再写入本次临时目录。
            connection, metadata = open_archive(run, relative, source_root=self.root, keys=self.keys, cancel=cancel)
            try:
                check_cancel(cancel)
                yield connection, metadata
            finally:
                connection.close()

    def conversations(self, cancel=None):
        """读取会话选择所需的联系人名称，不读取任何聊天正文。"""
        with self.open_db(str(Path("contact/contact.db")), cancel) as (connection, metadata):
            rows = connection.execute("SELECT username,nick_name,remark FROM contact").fetchall()
        self.contact_metadata = metadata
        self.names = {username: remark or nickname or "未命名联系人" for username, nickname, remark in rows}
        excluded = {"weixin", "newsapp", "fmessage", "medianote", "floatbottle", "qqmail", "feedsapp", "brandsessionholder"}
        choices = []
        for username, nickname, remark in rows:
            if not username or username in excluded or username.startswith("gh_"):
                continue
            group = username.endswith("@chatroom")
            if "@" in username and not group and not username.endswith("@openim"):
                continue
            choices.append(Conversation(username, remark or nickname or "未命名会话", "群聊" if group else "个人", nickname or ""))
        return sorted(choices, key=lambda item: (item.kind, item.name.casefold(), item.short_id))
