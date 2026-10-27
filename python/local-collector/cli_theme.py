# -*- coding: utf-8 -*-
"""终端只保留品牌标识、菜单和少量强调色，不依赖桌面界面。"""

import os
import shutil
import sys
import unicodedata


# 按原品牌的叠放气泡与叉形眼睛简化为等宽像素，不改动原图资源。
LOGO = (
    "   ▄▄▀▀▀▀▀▀▄▄         ",
    " ▄▀          ▀▄       ",
    "▄▀  ▄ ▄  ▄ ▄  ▀▄      ",
    "█   ▄▀▄  ▄▀▄   █▄▄▄   ",
    "█              █   ▀▄ ",
    " █ ▄          █ ▄ ▄  █",
    "  █▀       ▄▄▀  ▄▀▄▄██",
    " ▄▀▄▄▄▄▀▀▀▀ ▀▄   ▀▄ ▀▄",
    " ▀▀           ▀▀▀▀▀▀▀█",
)


def supports_color():
    """只在交互终端启用颜色，遵循关闭颜色约定并兼容旧控制台。"""
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetConsoleMode.restype = wintypes.BOOL
    handle = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
    mode = wintypes.DWORD()
    if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    # 只开启当前控制台的转义序列支持，不改字体、窗口尺寸或系统主题。
    return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))


def ink(text, code, color):
    """每段单独恢复默认颜色，避免提示符和后续终端命令被染色。"""
    return f"\033[{code}m{text}\033[0m" if color else text


def cell_width(text):
    """按终端单元估算中英文宽度，组合字符不额外占列。"""
    return sum(0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in "WF" else 1 for character in text)


def fit_text(value, width):
    """去掉控制字符并按可用列截短，完整型号仍可在模型设置中查看。"""
    text = "".join(character if character.isprintable() else " " for character in str(value))
    if cell_width(text) <= width:
        return text
    suffix = "." * min(3, max(0, width))
    result = ""
    for character in text:
        if cell_width(result + character) > width - len(suffix):
            break
        result += character
    return result + suffix


def render_header(color=False, columns=80, model_info=None):
    """宽终端并排展示像素标识，窄终端和纯文字输出使用紧凑字标。"""
    if color and columns >= 66:
        text = {
            2: ink("Collecter", "1", color),
            4: "微信记录 · 本地导出",
        }
        if model_info is not None:
            width = columns - 2 - max(cell_width(row) for row in LOGO) - 4
            status_color = {"success": "38;5;79", "error": "38;5;203", "warning": "38;5;214"}.get(model_info.get("level"), "38;5;248")
            text[6] = fit_text("当前模型：" + model_info["model"], width)
            text[7] = ink(fit_text("模型状态：" + model_info["status"], width), status_color, color)
            if model_info.get("checked_at"):
                text[8] = ink(fit_text("测试时间：" + model_info["checked_at"], width), "38;5;248", color)
        rows = ["  " + ink(row, "38;5;79", color) + "    " + text.get(index, "")
                for index, row in enumerate(LOGO)]
        return "\n" + "\n".join(rows) + "\n"
    result = "\n  " + ink("Collecter", "1", color) + "\n  微信记录 · 本地导出\n"
    if model_info is not None:
        result += "  " + fit_text("当前模型：" + model_info["model"], columns - 2) + "\n"
        result += "  " + fit_text("模型状态：" + model_info["status"], columns - 2) + "\n"
        if model_info.get("checked_at"):
            result += "  " + fit_text("测试时间：" + model_info["checked_at"], columns - 2) + "\n"
    return result


def render_menu(color=False, columns=80):
    """主操作使用强调色，退出保持普通文字，不增加菜单层级。"""
    line = "─" * min(58, max(12, columns - 4))
    return ("  " + ink(line, "38;5;248", color) + "\n\n"
            "  " + ink("1", "1;38;5;79", color) + "   " + ink("读取并导出", "1", color) + "\n\n"
            "  2   浏览本地记录\n"
            "  3   总体分析报告\n"
            "  4   模型设置\n\n"
            "  0   退出\n\n")


def show_logo(model_info=None):
    """只绘制调用方提供的标题数据，纯标识入口仍不读取任何配置。"""
    print(render_header(supports_color(), shutil.get_terminal_size((80, 24)).columns, model_info))


def show_menu():
    """窗口尺寸改变后重新计算分隔线，使用终端原有背景与字体。"""
    print(render_menu(supports_color(), shutil.get_terminal_size((80, 24)).columns), end="")
