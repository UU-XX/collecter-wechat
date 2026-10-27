# -*- coding: utf-8 -*-
"""把客观聚合数据写成离线报告，不包含聊天正文，不加载远程资源。"""

import datetime as dt
import html
import json
import uuid
from pathlib import Path

from collector_core import TYPE_NAMES
from local_analysis import ZONE, analyze_result

STYLE = """
:root{color-scheme:light;--ink:#202a27;--muted:#53645e;--line:#d3ddd7;--accent:#176b4a;--paper:#fff;--ground:#f3f6f3}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font:16px/1.7 'Segoe UI','Microsoft YaHei',sans-serif}
main{max-width:1080px;margin:auto;padding:40px 28px 64px}header{border-bottom:1px solid var(--line);padding-bottom:24px}
h1{font-size:30px;line-height:1.3;margin:0 0 12px;font-weight:650;overflow-wrap:anywhere}h2{font-size:21px;margin:0 0 20px}p{margin:8px 0;overflow-wrap:anywhere}
.brand{color:var(--accent);font-weight:650}.muted,small{color:var(--muted)}nav{display:flex;flex-wrap:wrap;gap:12px 24px;margin-top:20px}
a{color:var(--accent);text-underline-offset:4px}a:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;margin:28px 0 40px;font-variant-numeric:tabular-nums}
.summary dt{color:var(--muted)}.summary dd{margin:4px 0 0;font-size:24px;font-weight:600}
section{background:var(--paper);padding:28px;margin:24px 0;border:1px solid var(--line)}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
.summary-text{white-space:pre-wrap;overflow-wrap:anywhere;max-width:75ch}
caption{text-align:left;color:var(--muted);margin-bottom:12px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);overflow-wrap:anywhere}th{font-weight:600}
.number{text-align:right;white-space:nowrap}.bar{width:55%}.track{display:block;height:9px;background:#e6eee8}.fill{display:block;height:100%;background:var(--accent)}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px}.columns section{margin:0}.notice{max-width:78ch;margin:24px 0;color:var(--muted)}
::selection{background:#c6e8d4;color:var(--ink)}html{scrollbar-color:#849c8d var(--ground);scroll-behavior:smooth}footer{margin-top:32px;color:var(--muted);font-size:14px}
@media(max-width:700px){main{padding:24px 16px 40px}.summary{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.columns{grid-template-columns:1fr}section{padding:20px 14px}h1{font-size:26px}.bar{width:35%}th,td{padding:9px 6px}}
@media print{body{background:white}main{padding:0}nav{display:none}section{break-inside:avoid}.columns{display:block}.columns section{margin:24px 0}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""

# 诊断只翻译固定阶段，不将服务端正文或第三方异常写进报告。
STAGES = {"request_body": "构造请求", "response_body": "接收响应", "response_json": "解析完整响应JSON",
          "response_shape": "核对响应结构", "stream_line": "读取流式行", "stream_event": "读取流式事件",
          "stream_encoding": "解码流式文字", "stream_json": "解析流式事件JSON", "stream_shape": "核对流式字段",
          "stream_finish": "检查完成标记", "text_limit": "接收总结正文", "model_output": "提取模型正文"}
FINISH_LABELS = {"insufficient_system_resource": "服务端推理资源不足，请求被打断", "aborted": "接口明确报告生成被中断",
                 "missing": "接口未提供结束原因", "other": "接口返回了未识别的结束原因",
                 "tool_calls": "接口返回工具调用，本工具只接受总结正文", "function_call": "接口返回函数调用，本工具只接受总结正文"}


def _bars(title, rows, identifier, caption):
    """柱长只表示数量，同时提供完整文字表格，不依赖颜色理解数据。"""
    escape = html.escape
    maximum = max((amount for _, amount in rows), default=0) or 1
    cells = []
    for label, amount in rows:
        width = amount / maximum * 100
        cells.append(f'<tr><th scope="row">{escape(str(label))}</th><td class="bar"><span class="track" aria-hidden="true"><span class="fill" style="width:{width:.2f}%"></span></span></td><td class="number">{amount:,}</td></tr>')
    return f'<section id="{identifier}"><h2>{escape(title)}</h2><table><caption>{escape(caption)}</caption><thead><tr><th scope="col">项目</th><th scope="col">数量分布</th><th scope="col" class="number">消息数</th></tr></thead><tbody>' + ("".join(cells) or '<tr><td colspan="3">所选范围内没有记录。</td></tr>') + '</tbody></table></section>'


def render_report(data):
    """继承既有离线页面的清晰阅读结构，所有来源文字先转义。"""
    escape = html.escape
    title = "总体分析报告"
    scope = data["requested_range"]
    people = [(person["name"], person["messages"]) for person in data["participants"]]
    type_rows = [(TYPE_NAMES.get(int(kind), "其他"), amount) for kind, amount in data["message_types"].items()]
    weekday_names = {"1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六", "0": "周日"}
    complete = data["source_completeness"]
    snapshot = "已检查各分片提交日志" if complete.get("wal_checked") else "源结果未确认日志合并"
    if complete.get("cumulative_archive"):
        snapshot += "；来源为累计归档，包含继承记录"
    summary = data.get("content_summary")
    summary_link = '<a href="#summary">内容总结</a>' if summary else ""
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'; object-src \'none\'">',
             f'<title>{escape(title)} · Collecter</title><style>{STYLE}</style></head><body><main>',
             f'<header><h1>{escape(title)} · {escape(data["conversation_name"])}</h1><p class="brand">Collecter · 本地离线报告</p>',
             f'<p class="muted">源结果范围：{escape(scope["start"])} 至 {escape(scope["end"])} · {escape(data["kind"])}</p>',
             f'<nav aria-label="报告目录">{summary_link}<a href="#months">月度分布</a><a href="#hours">活跃时段</a><a href="#people">参与情况</a><a href="#quality">数据范围</a></nav></header>',
             '<dl class="summary">' + ''.join(f'<div><dt>{label}</dt><dd>{value:,}</dd></div>' for label, value in
                 (("消息记录", data["total"]), ("活跃自然日", data["active_days"]), ("观察到的参与者", data["observed_participants"]), ("最长连续活跃天数", data["longest_streak_days"]))) + '</dl>']
    if data["total"] == 0:
        parts.append('<p class="notice">所选导出结果中没有记录。可选择其他导出目录重新生成报告。</p>')
    if summary:
        parts.append('<section id="summary"><h2>聊天内容总结</h2>')
        if summary["status"] == "complete":
            parts.append(f'<p class="muted">模型：{escape(summary["model"])} · 已总结正文 {summary["messages"]:,} 条 / 源记录 {summary["source_messages"]:,} 条</p>')
        parts.append(f'<div class="summary-text">{escape(summary["text"])}</div>')
        if summary["status"] in ("failed", "cancelled", "running"):
            status = {"failed": "未完成", "cancelled": "已取消", "running": "进行中"}[summary["status"]]
            parts.append(f'<p><strong>模型分析状态：{status}</strong></p>')
            progress = summary.get("progress", {})
            phase = {"overall": "总体汇总", "daily_merge": "当日汇总"}.get(progress.get("phase"), "每日分析")
            parts.append(f'<p>阶段：{phase} · 当前日期：{escape(progress.get("date") or "全部日期")} · 已完成 {len(summary.get("days", []))} 天。已完成的每日分析可在下方查看。</p>')
            if "completed_requests" in progress:
                parts.append(f'<p>已完成请求：{int(progress["completed_requests"]):,} 次（含分段分析及汇总），不等于已完成整天或总体分析。</p>')
        if summary.get("error_code"):
            details = summary.get("diagnostics", {})
            stage = details.get("stage", "")
            parts.append(f'<p>错误码：{escape(summary["error_code"])} · 失败步骤：{escape(STAGES.get(stage, "接口未提供更细诊断"))} {escape(stage)}</p>')
            if "finish_reason" in details:
                reason = details["finish_reason"]
                parts.append(f'<p>实际结束原因：{escape(FINISH_LABELS.get(reason, reason))}（{escape(reason)}）。</p>')
            if "termination_received" in details:
                received = "已收到" if details["termination_received"] else "未收到"
                parts.append(f'<p>接口终止信号：{received}；终止信号不代表总结成功完成。</p>')
            if details.get("requested_max_tokens"):
                parts.append(f'<p>本次请求输出预算：{int(details["requested_max_tokens"]):,} tokens。</p>')
            elif details.get("provider_default_budget"):
                parts.append('<p>本次请求未设置本地输出 token 上限，使用接口默认值；接口仍可能限制输出或上下文。</p>')
            parts.append('<p>诊断文件：同目录的模型诊断.jsonl。没有自动重发请求。</p>')
        if summary.get("limitation"):
            parts.append(f'<p class="muted">{escape(summary["limitation"])}</p>')
        parts.append('</section>')
        if summary.get("days"):
            parts.append('<section id="daily"><h2>每日分析总结</h2>')
            for day in summary["days"]:
                parts.append(f'<details><summary>{escape(day["date"])} · {day["messages"]:,} 条有效正文 · 已完成</summary><div class="summary-text">{escape(day["text"])}</div></details>')
            parts.append('</section>')
    parts.append(_bars("月度消息", list(data["months"].items()), "months", "按北京时间归入自然月，数量来自本次导出。"))
    parts.append('<div class="columns">' + _bars("一天中的活跃时段", [(hour + " 时", amount) for hour, amount in data["hours"].items()], "hours", "按消息发送时刻统计，不代表在线时长。") +
                 _bars("消息类型", type_rows, "types", "图片和语音等占位记录仍按原类型统计，不代表已导出媒体文件。") + '</div>')
    if data["kind"] == "群聊":
        parts.append(f'<p class="notice">观察到 {data["observed_participants"]} 位有身份记录的参与者。发言前5位贡献了本次消息的 {data["participation_profile"]["top_five_share"]:.1%}；这只描述发言分布，不代表成员总数或在线情况。</p>')
    parts.append(_bars("成员参与情况" if data["kind"] == "群聊" else "对话发言分布", people, "people", "列出发言较多的前30位参与者；按本地身份分别统计，同名者不合并。各人活跃日和占比见统计数据.json。"))
    parts.append(_bars("一周分布", [(weekday_names[day], amount) for day, amount in data["weekdays"].items()], "weekdays", "按北京时间自然日统计。"))
    top_days = sorted(data["days"].items(), key=lambda item: (-item[1], item[0]))[:10]
    parts.append(_bars("消息较多的日期", top_days, "days", "仅反映本次结果中已有的记录。"))
    parts.append(f'<section id="quality"><h2>数据范围与完整性</h2><p>{escape(data["limitation"])}</p><p>源任务状态：{escape(data["source_status"])}；{snapshot}。多个分片不是同一时刻的联合快照。</p><p>正文解析失败 {data["parse_errors"]:,} 条；未知发送人 {data["unknown_sender_messages"]:,} 条；时间无效 {data["invalid_time_messages"]:,} 条。</p><p>统计覆盖所选结果中的全部记录；本机未保存或未导出的历史不计入。</p></section>')
    content_note = "包含本次明确授权生成的模型摘要，可能引用聊天内容。" if summary and (summary["status"] == "complete" or summary.get("days")) else "本页面不包含聊天正文。"
    parts.append(f'<footer>生成时间：{escape(data["created_at"])} · {content_note} 页面打开时不访问微信或远程资源。详细数据见同目录的统计数据.json。</footer></main></body></html>')
    return "".join(parts)


def _save_report(root, data):
    """只更新本次独占新建目录的进度快照，不覆盖原始导出或历史报告。"""
    for name, content in (("统计数据.json", json.dumps(data, ensure_ascii=False, indent=2)), ("分析报告.html", render_report(data))):
        temporary = root / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(root / name)


def generate_report(directory, output, *, model_config=None, notify=lambda text: None, confirm_large=None, on_delta=None, max_output_tokens=None):
    """先创建独立报告，每日完成即保存，后续失败仍保留已完成分析和受控日志。"""
    data = analyze_result(directory)
    root = Path(output).expanduser().resolve() / ("report-" + dt.datetime.now(ZONE).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    if model_config is not None:
        from model_settings import MODEL_ERRORS, ModelError
        from model_summary import summarize_result
        days, progress = [], {}
        data["content_summary"] = {"status": "running", "text": "模型分析正在进行，已完成日期会逐日保存。", "days": days}
        log = root / "模型诊断.jsonl"
        log.touch(exist_ok=False)
        _save_report(root, data)
        notify("模型分析保存目录：" + str(root))

        def journal(event, details):
            """日志只记录阶段、日期、计数和白名单诊断，不保存模型原文或认证配置。"""
            record = {"time": dt.datetime.now(ZONE).isoformat(), "event": event, **details}
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        def record_progress(item):
            """接收内部调度元数据，立即写入诊断，失败时保留最后一个请求位置。"""
            progress.update(item)
            data["content_summary"]["progress"] = dict(progress)
            journal(item["event"], dict(progress))

        def save_day(item):
            """完整日分析先写独立文件，再更新可浏览的报告快照。"""
            folder = root / "每日总结"
            folder.mkdir(exist_ok=True)
            with (folder / (item["date"] + ".md")).open("x", encoding="utf-8") as stream:
                stream.write("# " + item["date"] + " 每日分析\n\n" + item["text"] + "\n")
            days.append(dict(item))
            journal("day_complete", {"date": item["date"], "completed_days": len(days), "requests": item["requests"]})
            _save_report(root, data)

        try:
            data["content_summary"] = summarize_result(directory, model_config, notify, confirm_large=confirm_large, on_delta=on_delta,
                                                        max_output_tokens=max_output_tokens, on_day=save_day, on_progress=record_progress)
            summary = data["content_summary"]
            if summary["status"] == "complete":
                with (root / "总体总结.md").open("x", encoding="utf-8") as stream:
                    stream.write("# 总体分析总结\n\n" + summary["text"] + "\n")
            journal(summary["status"], {"completed_days": len(days), "requests": summary.get("requests", 0)})
        except ModelError as error:
            data["content_summary"] = {"status": "failed", "error_code": error.code, "text": MODEL_ERRORS[error.code] + " 本地统计及已完成的每日分析已保留。",
                                       "days": days, "progress": dict(progress)}
            if error.code == "SUMMARY_CONFIRM":
                data["content_summary"]["requests"] = 0
            if error.details:
                data["content_summary"]["diagnostics"] = dict(error.details)
            journal("failed", {"error_code": error.code, "diagnostics": dict(error.details), "progress": dict(progress), "completed_days": len(days)})
            notify("内容总结未完成：" + MODEL_ERRORS[error.code])
        except KeyboardInterrupt:
            data["content_summary"] = {"status": "cancelled", "text": "已取消内容总结，本地统计及已完成的每日分析已保留。", "days": days, "progress": dict(progress)}
            journal("cancelled", {"progress": dict(progress), "completed_days": len(days)})
    _save_report(root, data)
    return root, data
