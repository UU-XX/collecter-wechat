# -*- coding: utf-8 -*-
"""从指定开发目录增量整理命令行与Skill共用源码，不复制用户数据或GUI。"""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil


def closure(files, roots):
    """仅沿本地模块的静态导入选择源码，动态自检导入也纳入。"""
    pending, selected = list(roots), set()
    while pending:
        name = pending.pop()
        if name in selected or name not in files:
            continue
        selected.add(name)
        for node in ast.walk(ast.parse(files[name].read_text(encoding="utf-8-sig"))):
            imports = [alias.name.split(".")[0] for alias in node.names] if isinstance(node, ast.Import) else [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else []
            pending.extend(item for item in imports if item in files)
    return selected


def main():
    """由维护者明确指定源码位置，公开仓库不保存开发机绝对路径。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    root = Path(__file__).resolve().parents[1]
    files = {path.stem: path for path in source.glob("*.py")}
    names = closure(files, ["cli"])
    forbidden = {"app", "collector_ui", "collector_tray", "collector_orb", "collector_window", "collector_calendar", "collector_appearance"}
    if names & forbidden:
        raise RuntimeError("命令行导入了桌面组件，不能发布")
    tests = []
    for name in sorted(files):
        if name.startswith("test_") and all(item in names or item.startswith("test_") for item in closure(files, [name])):
            tests.append(name)
    target = root / "python/local-collector"
    target.mkdir(parents=True, exist_ok=True)
    for name in sorted(names | set(tests)):
        shutil.copy2(files[name], target / files[name].name)
    for name in ("启动CLI.cmd", "CollecterCLI.spec"):
        shutil.copy2(source / name, target / name)
    for folder, patterns in (("assets", ["collecter-dark.ico"]), ("test-fixtures", ["synthetic-16x16.mp4", "synthetic-wal-main.db", "synthetic-wal-log.bin"])):
        (target / folder).mkdir(exist_ok=True)
        for pattern in patterns:
            shutil.copy2(source / folder / pattern, target / folder / pattern)
    skill = root / "skills/collecter/scripts"
    engine = skill / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    skill_names = closure(files, ["collector_core", "wechat_source", "process_accounts", "local_analysis", "model_summary", "analysis_report", "export_jobs"])
    for name in sorted(skill_names):
        shutil.copy2(files[name], engine / files[name].name)
    for tools in (root / "python/reviewed-tools", skill / "reviewed-tools"):
        tools.mkdir(parents=True, exist_ok=True)
        for name in ("group_probe.py", "read_target_group.py", "wcdb_key_tool_windows.79f1b5b92e12.py"):
            shutil.copy2(source.parent / "reviewed-tools" / name, tools / name)
    manifest = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for base in (root / "python", skill) for path in sorted(base.rglob("*")) if path.is_file()}
    (root / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"core_modules": len(names), "tests": len(tests), "skill_modules": len(skill_names)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
