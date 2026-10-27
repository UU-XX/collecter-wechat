# -*- coding: utf-8 -*-
"""仅构建命令行、Python和独立Skill分发包，不包含桌面客户端。"""

import argparse
import datetime as dt
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile


def digest(path):
    """对分发文件计算摘要，不访问用户数据目录。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive(folder, destination):
    """只压缩明确指定的分发目录，不包含缓存、配置或开发输出。"""
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as package:
        for file in sorted(folder.rglob("*")):
            if file.is_file() and not {"__pycache__", ".venv", "venv", ".git"}.intersection(file.parts) and not file.name.startswith(".env"):
                package.write(file, folder.name + "/" + file.relative_to(folder).as_posix())


def main():
    """每次使用新的构建目录与发行目录，已有目录则停止而不覆盖。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?", args.version):
        raise ValueError("版本格式应为0.1.0")
    if os.name != "nt":
        raise RuntimeError("CLI构建需要Windows")
    root = Path(__file__).resolve().parents[1]
    source = root / "python/local-collector"
    build = root / "build-output" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    release = root / "releases" / args.version
    build.mkdir(parents=True, exist_ok=False)
    release.mkdir(parents=True, exist_ok=False)
    cli = release / ("Collecter-CLI-Windows-x64-" + args.version)
    cli.mkdir()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(build / "work"),
                    "--distpath", str(cli), str(source / "CollecterCLI.spec")], cwd=source, check=True)
    application = cli / "CollecterCLI"
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(root / name, application / name)
    shutil.copytree(root / "third-party-licenses", application / "第三方许可")
    for name in ("pycryptodome", "zstandard", "PyInstaller"):
        distribution = metadata.distribution(name)
        for item in distribution.files or []:
            if "/licenses/" in str(item).lower() or Path(item).name.lower().startswith(("license", "copying")):
                file = Path(distribution.locate_file(item))
                if file.is_file():
                    destination = application / "第三方许可" / name / file.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, destination)
    shutil.copy2(Path(sys.base_prefix) / "LICENSE.txt", application / "第三方许可/Python-LICENSE.txt")
    (application / "使用说明.txt").write_text("Collecter CLI " + args.version + "\n完整解压后打开CollecterCLI.exe，保留_internal。\n个人非商业使用；商业用途需另行书面授权，见LICENSE。\n菜单：1导出，2浏览，3报告，4模型设置。\n导出和统计在本机完成，AI总结按所配置模型处理内容。\n", encoding="utf-8")
    probe = build / "isolated-check"
    probe.mkdir()
    env = {name.upper(): value for name, value in os.environ.items()}
    env.update(LOCALAPPDATA=str(probe / "local"), APPDATA=str(probe / "roaming"))
    for name in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(name, None)
    env["PATH"] = os.pathsep.join((str(Path(env["SYSTEMROOT"]) / "System32"), env["SYSTEMROOT"]))
    subprocess.run([str(application / "CollecterCLI.exe"), "--self-test-dir", str(probe)], cwd=probe, env=env,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=60, check=True)
    checked = json.loads((probe / "self-test.json").read_text(encoding="utf-8"))
    if not checked.get("ok") or not checked.get("frozen"):
        raise RuntimeError("冻结自检未通过")
    forbidden = ("tkinter", "collector_ui", "collector_tray", "collecter.exe")
    if any(any(word in file.name.lower() for word in forbidden) for file in application.rglob("*")):
        raise RuntimeError("发行包含桌面组件")
    archive(cli, release / (cli.name + ".zip"))
    python = release / ("Collecter-Python-" + args.version)
    python.mkdir()
    shutil.copytree(root / "python", python / "python", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(root / name, python / name)
    shutil.copytree(root / "third-party-licenses", python / "third-party-licenses")
    (python / "README.md").write_text("# Collecter Python\n\n在python目录创建虚拟环境，安装requirements.txt，然后运行local-collector/cli.py。\n\n```powershell\npy -3.12 -m venv .venv\n& '.\\.venv\\Scripts\\python.exe' -m pip install -r requirements.txt\n& '.\\.venv\\Scripts\\python.exe' -B '.\\local-collector\\cli.py'\n```\n\n个人非商业使用，商业用途需另行授权，见LICENSE。\n", encoding="utf-8")
    archive(python, release / (python.name + ".zip"))
    archive(root / "skills/collecter", release / ("Collecter-Skill-" + args.version + ".zip"))
    record = {"version": args.version, "frozen_checks": checked, "assets": {file.name: digest(file) for file in sorted(release.glob("*.zip"))}}
    (release / "verification.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (release / "SHA256SUMS.txt").write_text("".join(value + "  " + name + "\n" for name, value in record["assets"].items()), encoding="utf-8")
    print(json.dumps({"release": str(release), "assets": record["assets"], "checks": len(checked["checks"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
