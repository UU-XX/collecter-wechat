# -*- coding: utf-8 -*-
"""手动批量及续导计划，每个会话保留独立结果和可核对的完成状态。"""

import datetime as dt
import json
import uuid
from contextlib import ExitStack
from pathlib import Path

from bootstrap import Cancelled, check_cancel
from collector_core import SHANGHAI, TimeRange, collect
from export_seed import media_options
from local_storage import atomic_write
from result_store import TaskError, load_result, source_identity, task_lease
from batch_source import batch_source


def frozen_window(report):
    """续导使用原任务冻结的秒级边界，不重新按当前时刻扩大范围。"""
    return TimeRange(int(dt.datetime.fromisoformat(report["effective_start"]).timestamp()),
                     int(dt.datetime.fromisoformat(report["effective_end_exclusive"]).timestamp()),
                     report["requested_start"], report["requested_end"])


def select_conversations(items, identifiers, all_conversations=False):
    """完整验证选择后才启动首个任务，重复编号只执行一次。"""
    if all_conversations:
        if not items:
            raise ValueError("没有可导出的会话。")
        return list(items)
    selected = []
    for identifier in identifiers:
        matches = [item for item in items if identifier in (item.username, item.short_id)]
        if len(matches) != 1:
            raise ValueError("会话不存在或短编号不唯一。")
        if matches[0] not in selected:
            selected.append(matches[0])
    if not selected:
        raise ValueError("请明确选择至少一个会话。")
    return selected


def load_batch(path):
    """批量说明只引用本目录内的任务，不接受外部路径或目录穿越。"""
    path = Path(path).expanduser().resolve()
    if path.is_file():
        path = path.parent
    description = path / "批量说明.json"
    description.resolve(strict=True).relative_to(path)
    if description.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("批量说明超过读取限制。")
    manifest = json.loads(description.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("tasks"), list):
        raise ValueError("批量说明格式不支持。")
    for task in manifest["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("conversation_id"), str):
            raise ValueError("批量任务缺少会话身份。")
        if task.get("directory"):
            (path / task["directory"]).resolve(strict=True).relative_to(path)
    return path, manifest


def run_batch(source, conversations, window, output, notify=lambda text: None, cancel=None,
              include_videos=False, include_files=False, previous=None, mode="resume"):
    """取消后保留清单；重新续导时复核全部选定范围并去重，不按旧偏移跳读。"""
    original = load_batch(previous) if previous else None
    identity = source_identity(source)
    if original:
        _, plan = original
        if not identity or plan["source_fingerprint"] != identity:
            raise TaskError("BASE_IDENTITY")
        if [item.username for item in conversations] != [task["conversation_id"] for task in plan["tasks"]]:
            raise TaskError("BASE_SCOPE")
        frozen = frozen_window(plan)
        valid_window = frozen == window if mode == "resume" else frozen.start == window.start and frozen.end <= window.end
        if mode not in ("resume", "incremental") or media_options(plan) != (include_videos, include_files) or not valid_window:
            raise TaskError("BASE_SCOPE")
        if mode == "incremental" and plan["status"] != "已完成":
            raise TaskError("BASE_INCOMPLETE")
    root = Path(output).expanduser().resolve() / ("batch-" + dt.datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    plan = {"schema": 1, "source_fingerprint": identity, "status": "进行中",
            "requested_start": window.requested_start, "requested_end": window.requested_end,
            "effective_start": dt.datetime.fromtimestamp(window.start, SHANGHAI).isoformat(),
            "effective_end_exclusive": dt.datetime.fromtimestamp(window.end, SHANGHAI).isoformat(),
            "options": {"include_videos": include_videos, "include_files": include_files},
            "tasks": [{"conversation_id": item.username, "name": item.name, "status": "未开始", "directory": None} for item in conversations]}
    path = root / "批量说明.json"

    def save():
        """每个会话前后原子记录状态，只写本次新建的批量目录。"""
        atomic_write(path, json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8"))

    with ExitStack() as leases:
        leases.enter_context(task_lease(root))
        if original:
            leases.enter_context(task_lease(original[0]))
        source = leases.enter_context(batch_source(source))
        save()
        try:
            for index, (item, task) in enumerate(zip(conversations, plan["tasks"])):
                check_cancel(cancel)
                task["status"] = "进行中"
                save()
                notify(f"正在导出会话 {index + 1}/{len(conversations)}。")
                seed = original[1]["tasks"][index].get("directory") if original else None
                if seed and not (original[0] / seed / "records.sqlite3").is_file():
                    seed = None
                def created(directory):
                    """任务库创建前登记目录，意外退出后仍能定位已经提交的结果。"""
                    task["directory"] = directory.name
                    save()
                try:
                    directory, report = collect(source, item, window, root, notify, cancel,
                                                include_videos=include_videos, include_files=include_files,
                                                seed_from=original[0] / seed if seed else None, mode=mode if seed else "new", on_created=created)
                    task.update(directory=directory.name, status=report["status"], count=report["count"],
                                complete=report["status"] == "主库存档采集完成" and report.get("exports", {}).get("status") == "complete" and report.get("media", {}).get("complete", True))
                except Cancelled:
                    raise
                except Exception as error:
                    task.update(status="未完成", error_type=type(error).__name__, complete=False)
                    if isinstance(error, TaskError):
                        task["error_code"] = error.code
                save()
                check_cancel(cancel)
            plan["status"] = "已完成" if all(task.get("complete") for task in plan["tasks"]) else "部分未完成"
        except Cancelled:
            plan["status"] = "已取消"
        finally:
            save()
    return root, plan
