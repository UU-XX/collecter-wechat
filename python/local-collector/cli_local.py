# -*- coding: utf-8 -*-
"""本地浏览、统计和手动续导入口，不自动发现或读取微信会话。"""

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

from analysis_report import generate_report
from bootstrap import PRIVATE
from collector_core import SHANGHAI, collect, parse_range, stamp
from export_jobs import frozen_window, load_batch, run_batch, select_conversations
from export_seed import media_options
from local_analysis import browse_records
from result_store import TaskError, load_result, source_identity
from cli_models import choose_summary_config, confirm_large_summary, required_config

LOCAL_COMMANDS = {"browse", "report", "resume"}


def terminal_text(value):
    """浏览正文保留换行，但屏蔽终端控制序列及其他不可打印字符。"""
    return "".join(character if character == "\n" or character.isprintable() else " " for character in str(value))


class SummaryStream:
    """保留旧内部回调的兼容实现，当前报告流程不使用逐字输出。"""

    def __init__(self):
        self.enabled = sys.stdout.isatty()
        self.channel = None

    def __call__(self, kind, text):
        """只转交正文片段，思考通道直接忽略。"""
        if kind == "start":
            self.channel = None
            return
        if kind == "end":
            if self.enabled and self.channel is not None:
                print(flush=True)
            self.channel = None
            return
        if not self.enabled or kind != "text" or not text:
            return
        if kind != self.channel:
            print("\n[总结正文]", flush=True)
            self.channel = kind
        print(terminal_text(text), end="", flush=True)


def add_commands(commands):
    """本地分析不要求账号或内存授权，只有续导命令具有显式读取选项。"""
    browse = commands.add_parser("browse", help="分页浏览指定本地结果，不读取微信")
    browse.add_argument("--input", type=Path, required=True, help="已有导出目录")
    browse.add_argument("--search", default="", help="正文关键词，通配符按普通文字处理")
    browse.add_argument("--sender", default="", help="发送人名称或已知本地身份")
    browse.add_argument("--type", type=int, dest="message_type", help="原始消息类型，例如1表示文字")
    browse.add_argument("--page", type=int, default=1)
    browse.add_argument("--page-size", type=int, default=20)
    browse.add_argument("--start", help="可选开始日期或时刻")
    browse.add_argument("--end", help="可选结束日期或时刻")
    browse.add_argument("--full", action="store_true", help="显示完整正文，默认每条最多显示2000字符并标明截断")
    report = commands.add_parser("report", help="生成总体分析报告，自动识别群聊或个人聊天")
    report.add_argument("--input", type=Path, required=True, help="已有导出目录，分析其中全部记录")
    report.add_argument("--output", type=Path, default=PRIVATE / "analysis", help="新报告保存根目录")
    report.add_argument("--with-model", action="store_true", help="明确允许把所选导出正文发送给已配置接口作内容总结")
    report.add_argument("--allow-large-summary", action="store_true", help="配合--with-model，明确允许按完整计划处理较多模型请求")
    report.add_argument("--no-stream", action="store_true", help="兼容旧命令；模型总结已统一等待完整JSON结果")
    report.add_argument("--max-output-tokens", type=int, help="可选输出预算，至少1024；默认不传上限，原生Anthropic必填预算除外")
    resume = commands.add_parser("resume", help="从单会话或批量结果手动续导，始终生成新目录")
    resume.add_argument("--from", dest="previous", type=Path, required=True, help="原任务或批量目录")
    resume.add_argument("--account", required=True, help="原账号的db_storage目录")
    resume.add_argument("--output", type=Path, default=PRIVATE / "exports")
    resume.add_argument("--allow-memory", action="store_true", help="本次明确允许数据库密钥读取")
    resume.add_argument("--incremental", action="store_true", help="从已完成基线增量合并到新的结束日期")
    resume.add_argument("--end", help="增量合并的结束日期，默认今天；普通续导沿用原范围")


def display_page(arguments):
    """只显示用户明确要求的本地当前页，分页查询不访问其他结果目录。"""
    window = parse_range(arguments.start or "1970-01-01", arguments.end or dt.datetime.now(SHANGHAI).strftime("%Y-%m-%d")) if arguments.start or arguments.end else None
    result = browse_records(arguments.input, search=arguments.search, sender=arguments.sender,
                            message_type=arguments.message_type, page=arguments.page, page_size=arguments.page_size,
                            start=window.start if window else None, end=window.end if window else None)
    title = terminal_text(result["conversation"]).replace("\n", " ")
    print(f"\n{title} · 第{result['page']}/{result['pages']}页 · {result['total']}条")
    for row in result["records"]:
        try:
            time = stamp(row["sent_at"])
        except (ValueError, OSError, OverflowError):
            time = "时间无效"
        sender = terminal_text(row["sender_name"]).replace("\n", " ")
        print(f"\n{time} · {sender} · 类型{row['type']}")
        text = terminal_text(row["text"] or "")
        print(text if arguments.full or len(text) <= 2000 else text[:2000] + "\n[仅显示前2000字符，使用--full查看完整正文]")
    if not result["records"]:
        print("当前页没有记录。")
    return result


def show_batch(root, report):
    """只展示汇总状态和任务目录，不把聊天正文写入运行日志。"""
    complete = sum(bool(task.get("complete")) for task in report["tasks"])
    print(f"\n批量任务：{report['status']} · {complete}/{len(report['tasks'])}个会话完成")
    print("保存位置：" + str(root))
    return 0 if report["status"] == "已完成" else 3


def resume_task(arguments, prepare_source, notify, interruptible, show_result):
    """先核对本机账号摘要，再沿用原会话及媒体选项；不切换到其他历史账号。"""
    previous = arguments.previous.expanduser().resolve()
    if previous.is_file():
        previous = previous.parent
    batch = (previous / "批量说明.json").is_file()
    _, report = load_batch(previous) if batch else load_result(previous)
    if not report.get("source_fingerprint") or report["source_fingerprint"] != source_identity(Path(arguments.account)):
        raise TaskError("BASE_IDENTITY")
    if arguments.end and not arguments.incremental:
        raise TaskError("BASE_SCOPE")
    if arguments.incremental and (report.get("status") != ("已完成" if batch else "主库存档采集完成") or
                                  not batch and report.get("exports", {}).get("status") != "complete"):
        raise TaskError("BASE_INCOMPLETE")
    window = parse_range(report["requested_start"], arguments.end or dt.datetime.now(SHANGHAI).strftime("%Y-%m-%d")) if arguments.incremental else frozen_window(report)
    videos, files = media_options(report)
    identifiers = [task["conversation_id"] for task in report["tasks"]] if batch else [report["conversation_id"]]
    with interruptible() as cancel:
        source, items = prepare_source(arguments.account, arguments.allow_memory, videos or files, cancel)
        selected = select_conversations(items, identifiers)
        if batch:
            root, plan = run_batch(source, selected, window, arguments.output, notify, cancel,
                                   include_videos=videos, include_files=files, previous=previous,
                                   mode="incremental" if arguments.incremental else "resume")
            return show_batch(root, plan)
        directory, result = collect(source, selected[0], window, arguments.output, notify, cancel,
                                    include_videos=videos, include_files=files, seed_from=previous,
                                    mode="incremental" if arguments.incremental else "resume")
    return show_result(directory, result)


def run_local(arguments, prepare_source, notify, interruptible, show_result):
    """按明确命令分流，浏览与报告路径不会调用数据库读取准备。"""
    if arguments.command == "browse":
        display_page(arguments)
        return 0
    if arguments.command == "resume":
        return resume_task(arguments, prepare_source, notify, interruptible, show_result)
    config = required_config() if arguments.with_model else None
    confirm = (lambda plan: True) if arguments.allow_large_summary else confirm_large_summary if sys.stdin.isatty() else None
    root, data = generate_report(arguments.input, arguments.output, model_config=config, notify=print, confirm_large=confirm,
                                 max_output_tokens=arguments.max_output_tokens)
    print(f"\n{terminal_text(data['conversation_name'])} · {data['total']}条 · {data['active_days']}个活跃日")
    print("报告位置：" + str(root / "分析报告.html"))
    print(data["limitation"])
    return 3 if config is not None and data.get("content_summary", {}).get("status") in ("failed", "cancelled") else 0


def interactive_history():
    """本地浏览由路径输入触发，不在启动首页枚举或读取历史结果。"""
    arguments = argparse.Namespace(input=Path(input("已有导出目录：").strip().strip('"')), search="", sender="", message_type=None,
                                   page=1, page_size=20, start=None, end=None, full=False)
    while True:
        result = display_page(arguments)
        answer = input("n下一页 / p上一页 / /关键词 / 0返回：").strip()
        if answer == "0":
            return
        if answer.lower() == "n":
            arguments.page = min(arguments.page + 1, result["pages"])
        elif answer.lower() == "p":
            arguments.page = max(1, arguments.page - 1)
        elif answer.startswith("/"):
            arguments.search, arguments.page = answer[1:], 1


def interactive_report():
    """报告只使用显式选定的本地结果，生成后由用户自行打开。"""
    source = Path(input("已有导出目录：").strip().strip('"'))
    output = Path(input("报告保存根目录 [默认本地报告目录]：").strip().strip('"') or PRIVATE / "analysis")
    config = choose_summary_config()
    root, data = generate_report(source, output, model_config=config, notify=print, confirm_large=confirm_large_summary)
    print(f"已生成总体分析报告 · {data['kind']} · {data['total']}条：{root / '分析报告.html'}")


def export_analysis(directory, report, on_error, *, model_config=None):
    """仅为本次明确选中的结果生成报告，单个失败不触发重新采集或阻止其他报告。"""
    try:
        if "tasks" in report:
            directory, plan = load_batch(directory)
            targets = [directory / task["directory"] for task in plan["tasks"] if task.get("directory")]
        else:
            targets = [Path(directory)]
    except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        on_error(error)
        print("分析报告未完成；导出记录保持不变，可在菜单3单独生成报告。")
        return
    for target in targets:
        try:
            root, data = generate_report(target, target, model_config=model_config, notify=print, confirm_large=confirm_large_summary)
            print(f"已生成总体分析报告 · {data['kind']} · {data['total']}条：{root / '分析报告.html'}")
        except KeyboardInterrupt:
            print("已取消生成分析报告；导出记录保持不变。")
            return
        except (ValueError, RuntimeError, OSError, sqlite3.Error, KeyError, TypeError) as error:
            on_error(error)
            print("分析报告未完成；导出记录保持不变，可在菜单3单独生成报告。")
