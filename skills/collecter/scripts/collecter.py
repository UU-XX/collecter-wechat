# -*- coding: utf-8 -*-
"""Skill独立入口，直接复用随包核心模块，不调用CLI或外部模型。"""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import sys
import threading

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

from analysis_report import generate_report
from collector_core import collect, parse_range
from export_jobs import run_batch, select_conversations
from local_analysis import browse_records
from model_summary import plan_summary, summary_chunks
from process_accounts import account_choices
from read_errors import ReadError
from result_store import TaskError, load_result, open_result, task_lease
from wechat_source import WechatSource, find_accounts, own_processes


def emit(value):
    """输出结构化结果，控制字符由JSON转义，不输出密钥或配置。"""
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def notify(text):
    """进度写到标准错误，标准输出只保留JSON结果。"""
    print(json.dumps({"progress": text}, ensure_ascii=False), file=sys.stderr, flush=True)


@contextmanager
def cancellation():
    """取消时通知核心引擎保存已有结果，不关闭或修改微信进程。"""
    event = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda signum, frame: event.set())
    try:
        yield event
    finally:
        signal.signal(signal.SIGINT, previous)


def parser():
    """所有真实读取都要求显式子命令与目标参数。"""
    result = argparse.ArgumentParser(description="Collecter Skill：随包脚本读取、导出和统计；内容总结由宿主完成。")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("accounts", help="列出本机账号目录，不获取数据库密钥")
    sessions = commands.add_parser("sessions", help="读取指定账号的会话")
    sessions.add_argument("--account", type=Path, required=True)
    sessions.add_argument("--allow-memory", action="store_true", help="本次允许获取数据库密钥")
    export = commands.add_parser("export", help="导出明确选定的会话，始终新建目录")
    export.add_argument("--account", type=Path, required=True)
    export.add_argument("--conversation", action="append", required=True)
    export.add_argument("--start", required=True)
    export.add_argument("--end", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--allow-memory", action="store_true")
    export.add_argument("--videos", action="store_true")
    export.add_argument("--files", action="store_true")
    browse = commands.add_parser("browse", help="分页读取已有导出中的记录")
    browse.add_argument("--input", type=Path, required=True)
    browse.add_argument("--page", type=int, default=1)
    browse.add_argument("--page-size", type=int, default=20)
    browse.add_argument("--search", default="")
    plan = commands.add_parser("plan", help="列出完整日期与分段计划，不调用模型")
    plan.add_argument("--input", type=Path, required=True)
    chunk = commands.add_parser("chunk", help="按日期逐段读取全部有效正文，供宿主分析")
    chunk.add_argument("--input", type=Path, required=True)
    chunk.add_argument("--day", required=True)
    chunk.add_argument("--index", type=int, required=True, help="当天从1开始的片段序号")
    report = commands.add_parser("report", help="生成本地统计报告，不调用模型")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    return result


def run(arguments):
    """复用核心引擎的数据完整性、任务锁、分段和独立目录规则。"""
    if arguments.command == "accounts":
        try:
            processes = own_processes()
        except ReadError as error:
            if error.code != "PROCESS_NOT_RUNNING":
                raise
            processes = []
        return {"accounts": [{"path": str(item.root), "status": item.status} for item in account_choices(find_accounts(), processes)]}
    if arguments.command in ("sessions", "export"):
        window = parse_range(arguments.start, arguments.end) if arguments.command == "export" else None
        with cancellation() as event:
            source = WechatSource(arguments.account.expanduser())
            source.prepare(notify, event, read_memory=arguments.allow_memory,
                           include_resources=bool(window and (arguments.videos or arguments.files)))
            items = source.conversations(event)
            if arguments.command == "sessions":
                return {"sessions": [{"id": item.username, "short_id": item.short_id, "name": item.name, "kind": item.kind} for item in items]}
            selected = select_conversations(items, arguments.conversation, False)
            if len(selected) > 1:
                root, report = run_batch(source, selected, window, arguments.output, notify, event,
                                         include_videos=arguments.videos, include_files=arguments.files)
                return {"directory": str(root), "status": report["status"], "tasks": report["tasks"]}
            root, report = collect(source, selected[0], window, arguments.output, notify, event,
                                   include_videos=arguments.videos, include_files=arguments.files)
            return {"directory": str(root), "status": report["status"], "count": report["count"], "exports": report.get("exports", {})}
    if arguments.command == "browse":
        return browse_records(arguments.input, page=arguments.page, page_size=arguments.page_size, search=arguments.search)
    if arguments.command == "plan":
        return plan_summary(arguments.input)
    if arguments.command == "chunk":
        plan = plan_summary(arguments.input)
        days = plan["days"]
        matched = next((day for day in days if day["date"] == arguments.day), None)
        if matched is None or not 1 <= arguments.index <= matched["chunks"]:
            raise TaskError("ANALYSIS_OPTIONS")
        offset = sum(day["messages"] for day in days[:days.index(matched)])
        directory, _ = load_result(arguments.input)
        with task_lease(directory), open_result(directory) as store:
            for index, text in enumerate(summary_chunks(store, arguments.day, offset), 1):
                if index == arguments.index:
                    return {"date": arguments.day, "index": index, "chunks": matched["chunks"], "text": text}
    if arguments.command == "report":
        root, data = generate_report(arguments.input, arguments.output)
        return {"directory": str(root), "total": data["total"], "active_days": data["active_days"], "model_called": False}
    raise TaskError("ANALYSIS_OPTIONS")


def main(argv=None):
    """失败只暴露受控错误，不把第三方异常原文带给宿主。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = parser().parse_args(argv)
    try:
        result = run(arguments)
        emit(result)
        if arguments.command == "export":
            return 0 if result["status"] == "已完成" or result["status"] == "主库存档采集完成" and result["exports"].get("status") == "complete" else 3
        return 0
    except (ReadError, TaskError) as error:
        emit({"error_code": error.code})
        return 2
    except KeyboardInterrupt:
        emit({"error_code": "CANCELLED"})
        return 130
    except (OSError, ValueError):
        emit({"error_code": "INPUT_OR_IO"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
