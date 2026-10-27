# -*- coding: utf-8 -*-
"""简洁终端入口，复用本地读取与导出，不加载桌面界面。"""

import argparse
import datetime as dt
import json
import signal
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from bootstrap import PRIVATE, Cancelled
from cli_theme import show_logo, show_menu
from collector_core import LIMITATION, SHANGHAI, collect, parse_range
from wechat_source import WechatSource, find_accounts, own_processes
from read_errors import READ_ERRORS, ReadError
from process_accounts import account_choices
from cli_local import LOCAL_COMMANDS, add_commands, export_analysis, interactive_history, interactive_report, run_local, show_batch
from export_jobs import frozen_window, run_batch, select_conversations
from export_seed import media_options
from result_store import TASK_ERRORS, TaskError, load_result, source_identity
from cli_models import add_model_commands, choose_summary_config, interactive_models, model_header, report_model_error, run_model_command
from model_settings import ModelError

def label(value):
    """名称仅显示为一行文字，屏蔽终端控制字符。"""
    return "".join(character if character.isprintable() else " " for character in str(value))


@contextmanager
def interruptible():
    """读取期间把 Ctrl+C 转为安全取消，结束后恢复终端原有行为。"""
    event = threading.Event()
    previous = signal.getsignal(signal.SIGINT)

    def stop(signum, frame):
        """只请求取消，已有结果由现有采集流程保留。"""
        if not event.is_set():
            print("\n正在安全停止，请等待当前读取结束……", flush=True)
        event.set()

    signal.signal(signal.SIGINT, stop)
    try:
        yield event
    finally:
        signal.signal(signal.SIGINT, previous)


def notify(text):
    """只显示读取阶段和统计，不展示正文或密钥。"""
    print("  " + label(text), flush=True)


def prepare_source(account, allow_memory, resources, cancel):
    """授权只对当前调用有效，不加载桌面配置或旧平台参数。"""
    source = WechatSource(Path(account).expanduser())
    source.prepare(notify, cancel, include_resources=resources, read_memory=allow_memory)
    return source, source.conversations(cancel)


def choose_conversation(items, multiple=False):
    """关键词筛选与编号分页兼容重名会话，不依赖用户输入内部标识。"""
    keyword = input("会话关键词（回车显示全部）：").strip()
    page = 0
    while True:
        matches = [item for item in items if keyword.casefold() in (item.name + " " + item.nickname).casefold()]
        pages = max(1, (len(matches) + 11) // 12)
        page = min(page, pages - 1)
        print(f"\n会话 {len(matches)} 个 · 第 {page + 1}/{pages} 页")
        for index in range(page * 12, min((page + 1) * 12, len(matches))):
            item = matches[index]
            print(f"  {index + 1:>3}  {label(item.name)} · {item.kind} · {item.short_id}")
        answer = input(("编号（多个用逗号）/ all当前筛选全部 / " if multiple else "编号确认 / ") + "n下一页 / p上一页 / /关键词 / 0返回：").strip()
        if answer == "0":
            return None
        if answer.lower() == "n":
            page = min(page + 1, pages - 1)
        elif answer.lower() == "p":
            page = max(0, page - 1)
        elif answer.startswith("/"):
            keyword, page = answer[1:].strip(), 0
        elif multiple and answer.lower() == "all":
            if matches and yes(f"确认选择当前筛选的全部{len(matches)}个会话"):
                return matches
        elif all(part.strip().isdigit() for part in answer.split(",")):
            numbers = list(dict.fromkeys(int(part.strip()) for part in answer.split(",")))
            if numbers and all(1 <= number <= len(matches) for number in numbers) and (multiple or len(numbers) == 1):
                selected = [matches[number - 1] for number in numbers]
                return selected if multiple else selected[0]
        else:
            print("请输入当前会话编号，或用 /关键词 重新筛选。")


def yes(prompt):
    """敏感授权和导出确认均默认否，不把空输入当成同意。"""
    return input(prompt + " [y/N]：").strip().lower() in ("y", "yes")


def show_result(directory, report):
    """保留主库存档与导出完成的区别，部分结果使用独立退出码。"""
    print(f"\n{label(report['status'])} · {report['count']} 条")
    print("保存位置：" + str(directory))
    codes = {item.get("error_code") for item in report.get("coverage", []) if isinstance(item.get("error_code"), str)}
    for code in sorted(codes & READ_ERRORS.keys()):
        print(f"分片读取 [{code}]：{READ_ERRORS[code][0]}")
    if report.get("mode") in ("resume", "incremental"):
        stats = report.get("incremental", {})
        print(f"本次新增 {stats.get('new_messages', 0)} 条，继承 {stats.get('inherited_messages', 0)} 条；原任务保留。")
    media = report.get("media", {})
    if media.get("enabled"):
        for kind, name in (("video", "视频"), ("file", "附件")):
            stats = media.get("kinds", {}).get(kind, {})
            if stats.get("enabled"):
                print(f"{name}：{stats['collected']}/{stats['total']} 个已导出")
        if not media.get("complete"):
            print("部分媒体未导出，具体原因见采集说明和消息记录。")
    complete = report["status"] == "主库存档采集完成" and report.get("exports", {}).get("status") == "complete"
    complete = complete and (not media.get("enabled") or media.get("complete", False))
    return 0 if complete else 3


def choose_account():
    """先查询进程，再核对目录占用；离线历史必须由用户明确选择。"""
    while True:
        try:
            processes = own_processes()
            print("\n检测到微信进程，正在核对本机账号目录……")
        except ReadError as error:
            report_error(error)
            answer = input("  r 重新检测 / l 使用已有缓存读取离线历史 / 0 返回：").strip().lower()
            if answer == "r":
                continue
            if answer != "l":
                return None
            processes = []
        choices = account_choices(find_accounts(), processes)
        if processes:
            print("进程占用仅表示正在使用本地数据库，不代表已确认登录或服务器在线。")
            if not any(item.process_ids for item in choices):
                print("尚未核对出当前账号，请确认微信已登录并手动选择对应目录。")
        else:
            print("离线模式：仅使用已有密钥缓存，不读取微信内存。")
        for index, item in enumerate(choices, 1):
            print(f"  {index}  {item.status} · {item.root.drive or '本地'} · {label(item.root.parent.name)}")
        while True:
            answer = input("账号编号或 db_storage 路径（r刷新，0返回）：").strip()
            if answer == "0":
                return None
            if answer.lower() == "r":
                break
            if answer.isdigit():
                if 1 <= int(answer) <= len(choices):
                    return choices[int(answer) - 1].root, bool(processes)
                print("编号超出范围，请重新选择。")
                continue
            if answer:
                return Path(answer.strip('"')).expanduser(), bool(processes)
            print("请选择账号，或输入明确的 db_storage 路径。")


def interactive_export():
    """只有选择导出后才查询进程和账号，最终确认后才读取聊天正文。"""
    selection = choose_account()
    if selection is None:
        return
    account, can_read_memory = selection
    allow_memory = yes("允许本次获取数据库密钥？缓存不足时会只读访问微信内存") if can_read_memory else False
    while True:
        try:
            with interruptible() as cancel:
                source, items = prepare_source(account, allow_memory, False, cancel)
            break
        except (KeyboardInterrupt, Cancelled):
            print("\n已取消读取会话，尚未开始导出。")
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            report_error(error)
        if not retry_confirm("是否重试读取会话"):
            return
    selected = choose_conversation(items, multiple=True)
    if selected is None:
        return
    start = input("开始日期 [2025-01-01]：").strip() or "2025-01-01"
    end = input("结束日期 [今天]：").strip() or dt.datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    window = parse_range(start, end)
    output = Path(input("保存目录 [默认本地导出目录]：").strip().strip('"') or PRIVATE / "exports").expanduser()
    media = yes("同时导出已下载的本机视频和文件附件")
    include_analysis = yes("同时生成总体分析报告")
    model_config = choose_summary_config() if include_analysis else None
    print(f"\n{label(selected[0].name) if len(selected) == 1 else str(len(selected)) + '个已选会话'} · {start} 至 {end}")
    print("保存位置：" + str(output))
    print(LIMITATION)
    if not yes("确认开始本地导出"):
        print("已返回，未读取聊天正文。")
        return
    interactive_collect(source, selected, window, output, media, allow_memory, include_analysis=include_analysis, model_config=model_config)


def retry_confirm(prompt):
    """失败页面只在明确同意后重试，空输入、退出输入和再次中断均返回菜单。"""
    try:
        return yes(prompt)
    except (KeyboardInterrupt, EOFError):
        return False


def interactive_collect(source, selected, window, output, media=False, allow_memory=False, *, include_analysis=False, model_config=None):
    """当前结果页手动重试，复用已提交记录，沿用原范围和授权并保留原结果。"""
    previous = None
    last_result = None
    while True:
        try:
            with interruptible() as cancel:
                if media:
                    source.prepare(notify, cancel, include_resources=True, read_memory=allow_memory)
                if len(selected) > 1:
                    extra = {"previous": previous, "mode": "resume"} if previous else {}
                    directory, report = run_batch(source, selected, window, output, notify, cancel, include_videos=media, include_files=media, **extra)
                else:
                    extra = {"seed_from": previous, "mode": "resume"} if previous else {}
                    directory, report = collect(source, selected[0], window, output, notify, cancel, include_videos=media, include_files=media, **extra)
            previous = directory
            last_result = (directory, report)
            code = show_batch(directory, report) if len(selected) > 1 else show_result(directory, report)
            if code == 0:
                break
        except (KeyboardInterrupt, Cancelled):
            print("\n已取消；已有结果保留。")
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            report_error(error)
        print("重试将沿用本次参数；已有结果保留，生成新的结果目录。")
        if not retry_confirm("是否重试本次导出"):
            break
    # 分析与读取重试分开，报告失败不能引发再次读取微信。
    if include_analysis and last_result is not None:
        extra = {"model_config": model_config} if model_config is not None else {}
        export_analysis(*last_result, report_error, **extra)


def report_error(error):
    """不打印第三方异常正文，避免异常携带密钥、路径或聊天内容。"""
    if isinstance(error, ModelError):
        report_model_error(error)
        return
    if isinstance(error, TaskError):
        message, hint = TASK_ERRORS[error.code]
        print(f"未完成 [{error.code}]：{message}\n{hint}", file=sys.stderr)
        return
    if isinstance(error, ReadError):
        message, hint = READ_ERRORS[error.code]
        print(f"未完成 [{error.code}]：{message}\n{hint}", file=sys.stderr)
        return
    print(f"未完成（{type(error).__name__}）：请核对输入、密钥缓存、读取授权和保存目录。", file=sys.stderr)


def interactive():
    """默认只显示菜单，返回菜单不会自动重试或继续读取。"""
    while True:
        show_logo(model_info=model_header())
        show_menu()
        action = input("  选择 › ").strip()
        if action == "0":
            return 0
        if action not in ("1", "2", "3", "4"):
            print("请输入 1、2、3、4 或 0。\n")
            continue
        try:
            if action == "1":
                interactive_export()
            elif action == "2":
                interactive_history()
            elif action == "3":
                interactive_report()
            else:
                interactive_models()
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            report_error(error)
        input("\n回车返回菜单……")


def parser():
    """参数模式复用同一读取链路，不包含网络、上传或定时命令。"""
    result = argparse.ArgumentParser(description="Collecter 简洁 CLI：手动读取，本地导出。无参数进入编号菜单。")
    result.add_argument("--logo", action="store_true", help="只显示 Logo，不访问账号或微信")
    result.add_argument("--self-test-dir", help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command")
    add_commands(commands)
    add_model_commands(commands)
    commands.add_parser("accounts", help="查看微信进程与本机账号目录占用，不读取聊天")
    conversations = commands.add_parser("conversations", help="读取会话列表，显示名称和短编号")
    export = commands.add_parser("export", help="立即读取并导出一个明确会话")
    for command in (conversations, export):
        command.add_argument("--account", required=True, help="所选账号的 db_storage 路径")
        command.add_argument("--allow-memory", action="store_true", help="明确允许本次数据库密钥内存读取，默认关闭")
    targets = export.add_mutually_exclusive_group(required=True)
    targets.add_argument("--conversation", action="append", help="短编号或完整会话标识，可重复指定多个会话")
    targets.add_argument("--all-conversations", action="store_true", help="明确选择所选账号全部会话")
    export.add_argument("--start", default=None, help="开始日期或时刻，默认2025-01-01；有基线时沿用原开始时间")
    export.add_argument("--end", default=None, help="结束日期或时刻，默认今天")
    export.add_argument("--output", type=Path, default=PRIVATE / "exports", help="本地保存根目录，每次新建结果子目录")
    export.add_argument("--videos", action="store_true", help="导出已下载且能明确关联的本机MP4视频，不解密或转码")
    export.add_argument("--files", action="store_true", help="导出可通过内容摘要验证的本机文件附件，不自动打开")
    previous = export.add_mutually_exclusive_group()
    previous.add_argument("--incremental-from", type=Path, help="单会话已完成基线，继承记录并合并新增消息")
    previous.add_argument("--resume-from", type=Path, help="单会话原任务，沿用冻结范围安全续导")
    return result


def command_export(arguments):
    """先验证参数，再读取账号；会话短编号必须唯一匹配。"""
    seed = arguments.resume_from or arguments.incremental_from
    mode = "resume" if arguments.resume_from else "incremental" if arguments.incremental_from else "new"
    baseline = load_result(seed)[1] if seed else None
    if seed and (arguments.all_conversations or len(arguments.conversation) != 1):
        raise ValueError("单会话基线只能对应一个会话；批量续导请使用resume命令。")
    if baseline and baseline.get("source_fingerprint") != source_identity(Path(arguments.account)):
        raise TaskError("BASE_IDENTITY")
    end = arguments.end or dt.datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    window = frozen_window(baseline) if mode == "resume" else parse_range(arguments.start or (baseline["requested_start"] if baseline else "2025-01-01"), end)
    if mode == "resume" and (arguments.start or arguments.end):
        raise TaskError("BASE_SCOPE")
    old_videos, old_files = media_options(baseline) if baseline else (False, False)
    videos, files = arguments.videos or old_videos, arguments.files or old_files
    with interruptible() as cancel:
        source, items = prepare_source(arguments.account, arguments.allow_memory,
                                       videos or files, cancel)
        matches = select_conversations(items, arguments.conversation or [], arguments.all_conversations)
        print(f"已选会话：{len(matches)}个")
        print(LIMITATION)
        if len(matches) > 1:
            directory, report = run_batch(source, matches, window, arguments.output.expanduser(), notify, cancel, include_videos=videos, include_files=files)
            return show_batch(directory, report)
        extra = {"seed_from": seed, "mode": mode} if seed else {}
        directory, report = collect(source, matches[0], window, arguments.output.expanduser(), notify, cancel,
                                    include_videos=videos, include_files=files, **extra)
    return show_result(directory, report)


def main(argv=None):
    """帮助和 Logo 都是无副作用入口，真实读取只由明确命令触发。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if (argv if argv is not None else sys.argv[1:]) or not sys.stdin.isatty():
        show_logo()
    argument_parser = parser()
    arguments = argument_parser.parse_args(argv)
    try:
        if arguments.logo:
            return 0
        if arguments.self_test_dir:
            from cli_selftest import self_test
            directory = Path(arguments.self_test_dir).resolve()
            path = directory / "self-test.json"
            if path.exists():
                return 2
            try:
                report = self_test(directory)
            except Exception as error:
                report = {"ok": False, "error_type": type(error).__name__}
                # 隔离自检只登记缺失依赖的模块名，不保存任意异常正文。
                if isinstance(error, ModuleNotFoundError):
                    report["missing_module"] = error.name
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0 if report["ok"] else 2
        if arguments.command == "accounts":
            try:
                processes = own_processes()
            except ReadError as error:
                if error.code != "PROCESS_NOT_RUNNING":
                    raise
                processes = []
            print("已检测到微信进程，账号登录状态仍需核对。" if processes else "未检测到微信进程，以下仅为本地历史目录。")
            for index, item in enumerate(account_choices(find_accounts(), processes), 1):
                print(f"{index}  {item.status}  {item.root}")
            return 0
        if arguments.command == "conversations":
            with interruptible() as cancel:
                _, items = prepare_source(arguments.account, arguments.allow_memory, False, cancel)
            for item in items:
                print(f"{item.short_id}  {item.kind}  {label(item.name)}")
            return 0
        if arguments.command == "export":
            return command_export(arguments)
        if arguments.command == "model":
            return run_model_command(arguments)
        if arguments.command in LOCAL_COMMANDS:
            return run_local(arguments, prepare_source, notify, interruptible, show_result)
        if not sys.stdin.isatty():
            argument_parser.print_help()
            return 0
        return interactive()
    except (KeyboardInterrupt, Cancelled):
        print("\n已取消；已有任务结果保留。")
        return 130
    except EOFError:
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        report_error(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
