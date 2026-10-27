# -*- coding: utf-8 -*-
"""只查询已导出的任务数据库，提供分页浏览与可复核的客观统计。"""

import datetime as dt
import math

from result_store import TaskError, load_result, open_result, task_lease

ZONE = dt.timezone(dt.timedelta(hours=8))


def _where(search="", sender="", message_type=None, start=None, end=None):
    """所有用户输入均使用绑定参数，关键词中的通配符按普通字符处理。"""
    clauses, values = [], []
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("COALESCE(text,'') LIKE ? ESCAPE '\\'")
        values.append("%" + escaped + "%")
    if sender:
        clauses.append("(sender_id=? OR sender_name=?)")
        values.extend((sender, sender))
    if message_type is not None:
        clauses.append("type=?")
        values.append(int(message_type))
    for column, value in (("sent_at>=?", start), ("sent_at<?", end)):
        if value is not None:
            clauses.append(column)
            values.append(int(value))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), values


def browse_records(directory, *, search="", sender="", message_type=None, start=None, end=None, page=1, page_size=20):
    """每次只取当前页，分页及筛选不把整段聊天载入内存。"""
    if not 1 <= page_size <= 200 or page < 1:
        raise TaskError("ANALYSIS_OPTIONS")
    directory, report = load_result(directory)
    where, values = _where(search, sender, message_type, start, end)
    with task_lease(directory), open_result(directory) as store:
        total = store.execute("SELECT COUNT(*) FROM messages" + where, values).fetchone()[0]
        rows = store.execute("SELECT message_key,sent_at,sender_id,sender_name,type,text,parse_error FROM messages" + where +
                             " ORDER BY sent_at,message_key LIMIT ? OFFSET ?", [*values, page_size, (page - 1) * page_size]).fetchall()
    return {"conversation": report["conversation_name"], "page": page, "pages": max(1, math.ceil(total / page_size)),
            "total": total, "records": [dict(row) for row in rows]}


def _streak(days):
    """只根据有记录的自然日计算连续活跃天数，不推断离线活动。"""
    best = current = 0
    previous = None
    for value in days:
        day = dt.date.fromisoformat(value)
        current = current + 1 if previous is not None and day - previous == dt.timedelta(days=1) else 1
        previous, best = day, max(best, current)
    return best


def analyze_result(directory):
    """聚合所选结果的全部记录，按原会话标识自动识别群聊或个人聊天。"""
    directory, report = load_result(directory)
    kind = "群聊" if report["conversation_id"].endswith("@chatroom") else "个人聊天"
    where, values = _where()
    with task_lease(directory), open_result(directory) as store:
        total, first, last, errors = store.execute("SELECT COUNT(*),MIN(sent_at),MAX(sent_at),SUM(parse_error IS NOT NULL) FROM messages" + where, values).fetchone()

        def distribution(expression):
            """表达式由本模块固定提供，筛选值仍使用参数绑定。"""
            return {str(row[0]): row[1] for row in store.execute("SELECT " + expression + ",COUNT(*) FROM messages" + where + " GROUP BY 1 ORDER BY 1", values) if row[0] is not None}

        days = distribution("strftime('%Y-%m-%d',sent_at,'unixepoch','+8 hours')")
        months = distribution("strftime('%Y-%m',sent_at,'unixepoch','+8 hours')")
        hours = distribution("strftime('%H',sent_at,'unixepoch','+8 hours')")
        weekdays = distribution("strftime('%w',sent_at,'unixepoch','+8 hours')")
        types = distribution("type")
        known = "sender_id IS NOT NULL AND sender_id<>''"
        conjunction = " AND " if where else " WHERE "
        participants = store.execute("SELECT COUNT(DISTINCT sender_id) FROM messages" + where + conjunction + known, values).fetchone()[0]
        unknown = store.execute("SELECT COUNT(*) FROM messages" + where + conjunction + "NOT (" + known + ")", values).fetchone()[0]
        # 按身份分组，同名成员不合并；显示名取该身份最后一条记录，空身份另记未知消息数。
        ranks = store.execute("SELECT sender_id,COUNT(*) AS amount,COUNT(DISTINCT strftime('%Y-%m-%d',sent_at,'unixepoch','+8 hours')) AS days FROM messages" + where + conjunction + known +
                              " GROUP BY sender_id ORDER BY amount DESC,sender_id LIMIT 30", values).fetchall()
        people = []
        for actor, amount, active_days in ranks:
            name = store.execute("SELECT sender_name FROM messages" + where + conjunction + "sender_id=? ORDER BY sent_at DESC,message_key DESC LIMIT 1", [*values, actor]).fetchone()[0]
            people.append({"name": name or "未识别参与者", "messages": amount, "active_days": active_days, "share": amount / total if total else 0})
    return {"conversation_name": report["conversation_name"], "kind": kind,
            "created_at": dt.datetime.now(ZONE).isoformat(), "total": total, "first_timestamp": first, "last_timestamp": last,
            "active_days": len(days), "longest_streak_days": _streak(sorted(days)), "observed_participants": participants,
            "unknown_sender_messages": unknown, "parse_errors": errors or 0, "invalid_time_messages": total - sum(days.values()),
            "months": months, "days": days, "hours": {f"{hour:02}": hours.get(f"{hour:02}", 0) for hour in range(24)},
            "weekdays": {str(day): weekdays.get(str(day), 0) for day in (1, 2, 3, 4, 5, 6, 0)}, "message_types": types,
            "participants": people, "source_status": report["status"],
            "participation_profile": {"top_five_share": sum(person["messages"] for person in people[:5]) / total if total else 0},
            "source_completeness": report.get("completeness", {}),
            "requested_range": {"start": report["requested_start"], "end": report["requested_end"]},
            "limitation": "仅统计所选本机导出记录。" + ("活跃参与者不等于群成员总数；" if kind == "群聊" else "") + "报告不证明聊天历史完整，未知发送人单独统计。"}
