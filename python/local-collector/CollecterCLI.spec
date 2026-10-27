# -*- coding: utf-8 -*-
"""独立控制台版本不包含桌面界面、托盘或用户运行数据。"""

from pathlib import Path

base = Path(SPECPATH)
tools = base.parent / "reviewed-tools"
analysis = Analysis(
    [str(base / "cli.py")],
    pathex=[str(base), str(tools), str(tools / "python-deps")],
    binaries=[], datas=[(str(tools / "wcdb_key_tool_windows.79f1b5b92e12.py"), "."),
                        (str(base / "test-fixtures" / "synthetic-16x16.mp4"), "self-test"),
                        (str(base / "test-fixtures" / "synthetic-wal-main.db"), "self-test"),
                        (str(base / "test-fixtures" / "synthetic-wal-log.bin"), "self-test")],
    # 固定审阅脚本动态导入标准库，必须显式打包，不能依赖其他模块间接带入。
    hiddenimports=["zstandard.backend_c", "glob"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "_tkinter", "PIL.ImageTk", "app", "collector_ui", "collector_tray", "collector_appearance",
              "numpy", "matplotlib", "pandas", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="CollecterCLI", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
    icon=str(base / "assets" / "collecter-dark.ico"),
)
distribution = COLLECT(
    executable, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="CollecterCLI",
)
