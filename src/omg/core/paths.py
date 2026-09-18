"""
omg.core.paths — 集中资源 / 缓存 / 配置路径解析与运行环境探测。

从 OMGDev/main.py 顶部的路径块与若干路径辅助函数抽取而来，目的是取代散落在
main.py 各处的 ``os.path`` 拼接与对 ``lib/`` / ``bg/`` / ``krr.ico`` 的相对路径引用。

本模块 **不依赖 PySide6**，可在 headless 环境直接导入。
注意：本模块刻意 **不含** 任何 ``sys.path.insert(...)`` 注入——那是旧的扁平
``func/`` / ``page/`` 布局遗留手段，Phase 5 会被彻底移除；新阶段统一使用
``omg.*`` 绝对导入。
"""

import os
import re
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# 运行环境探测
# ---------------------------------------------------------------------------
IS_NUITKA = "__compiled__" in dir()

IS_PYINSTALLER = getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
# PyInstaller (--contents-directory=bin):
#   sys._MEIPASS = exe_dir\bin\  (打包资源位于 resources/: icons, bg, binaries)
#   sys.argv[0] = exe_dir\OMG.exe
#   → BIN_DIR = 打包资源根目录, _EXE_DIR = exe 所在目录 (用户文件)
# Nuitka / 开发模式:
#   所有文件在同一目录, BIN_DIR = _EXE_DIR
_EXE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))

if IS_PYINSTALLER:
    BIN_DIR = sys._MEIPASS          # exe_dir\bin\  (打包资源)
    OMG_ROOT = _EXE_DIR             # GIMI/ 在 exe 旁边
else:
    BIN_DIR = _EXE_DIR
    OMG_ROOT = os.path.dirname(BIN_DIR)

# 用户文件 (config, log) → exe 旁边; 资源文件 (icons, bg) → 打包目录
CONFIG_PATH = os.path.join(_EXE_DIR, "config.json")
LOG_DIR = os.path.join(_EXE_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, f"omg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
GIMI_DIR = os.path.join(OMG_ROOT, "GIMI")
# 资源统一根目录（icons/bg/binaries 均位于 resources/ 下）。
# 开发模式下锚定到本文件所在包 (src/omg)，与启动方式 (sys.argv[0]) 解耦——
# 否则不同入口会算出「缺少 src/omg 一层」的错误路径（如 OMGLite/resources/binaries）。
# 打包模式 (PyInstaller/Nuitka) 资源随 BIN_DIR 一起分发。
if IS_PYINSTALLER or IS_NUITKA:
    RESOURCES_DIR = os.path.join(BIN_DIR, "resources")
else:
    _PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../src/omg
    RESOURCES_DIR = os.path.join(_PKG_ROOT, "resources")

BG_DIR = os.path.join(RESOURCES_DIR, "bg")
ICONS_DIR = os.path.join(RESOURCES_DIR, "icons")

# 媒体文件扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}

# 二进制定位（loader 运行时需要找到 3dmloader.dll / 3DMigoto Loader.exe 等）。
# Phase 5：统一迁到 resources/binaries；实际落地内容由 build_pyinstaller.ps1 的
# $AllowFiles 白名单决定 —— 那里是「本目录该有哪些文件」的单一真源，勿在本文件重复列举。
BINARIES_DIR = os.path.join(RESOURCES_DIR, "binaries")
APP_ICON_PATH = os.path.join(ICONS_DIR, "krr.ico")

# GIMI 版本 INI 默认位置（OMG_ROOT/GIMI 下的相对路径）
ORFIX_INI = os.path.join(GIMI_DIR, "Core", "GIMI", "Libraries", "ORFix.ini")
TEXFX_INI = os.path.join(GIMI_DIR, "Core", "GIMI", "Libraries", "TexFx", "Main.ini")


# ---------------------------------------------------------------------------
# 路径 / 配置辅助函数
# ---------------------------------------------------------------------------
def get_gimi_dir(config_data: dict) -> str:
    """OMGLite: Always OWN mode — 返回 config_data['gimi_folder']（用户自己的 GIMI 位置）。

    若未设置 gimi_folder，返回空字符串，调用方据此判定「未配置」状态。
    """
    return config_data.get("gimi_folder", "") or ""


def truncate_path(path: str, max_len: int = 50) -> str:
    """缩短过长路径显示：保留首尾，中间用 ... 省略。路径统一使用反斜杠。"""
    if not path:
        return path
    p = os.path.normpath(path).replace("/", "\\")
    if len(p) <= max_len:
        return p
    parts = p.split("\\")
    if len(parts) <= 3:
        return p
    return parts[0] + "\\...\\" + "\\".join(parts[-2:])


def _find_bg_file():
    """在 bin\\bg\\ 下找第一个媒体文件。返回 (path, is_video) 或 (None, False)。"""
    if not os.path.isdir(BG_DIR):
        return None, False
    for f in sorted(os.listdir(BG_DIR)):
        fp = os.path.join(BG_DIR, f)
        if not os.path.isfile(fp):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in IMAGE_EXTS:
            return fp, False
        if ext in VIDEO_EXTS:
            return fp, True
    return None, False


# ---------------------------------------------------------------------------
# INI 版本读取（GIMI / ORFix / TexFx）
# ---------------------------------------------------------------------------
def read_gimi_version_from_ini(gimi_dir: str) -> str:
    """从 Core/GIMI/main.ini 读取 GIMI 版本（global $version）。

    返回形如 '8.8.1' 的版本字符串，文件缺失则返回 ''。
    """
    main_ini = os.path.join(gimi_dir, "Core", "GIMI", "main.ini")
    if not os.path.isfile(main_ini):
        return ""
    try:
        with open(main_ini, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                m = re.match(r"global\s+\$version\s*=\s*([\d.]+)", line)
                if m:
                    raw = m.group(1)
                    # 内部格式 (8.81) → 显示格式 (8.8.1)
                    parts = raw.split(".")
                    if len(parts) == 2 and len(parts[1]) >= 2:
                        return f"{parts[0]}.{parts[1][0]}.{parts[1][1:]}"
                    return raw
    except Exception:
        pass
    return ""


def read_orfix_version(gimi_dir: str = None) -> str:
    ini = os.path.join(gimi_dir, "Core", "GIMI", "Libraries", "ORFix.ini") if gimi_dir else ORFIX_INI
    if not os.path.isfile(ini):
        return ""
    try:
        with open(ini, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line.startswith(";"):
                    continue
                m = re.search(r"Version\s+([\d.]+)", line, re.IGNORECASE)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def read_teffx_version(gimi_dir: str = None) -> str:
    ini = os.path.join(gimi_dir, "Core", "GIMI", "Libraries", "TexFx", "Main.ini") if gimi_dir else TEXFX_INI
    if not os.path.isfile(ini):
        return ""
    try:
        with open(ini, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                m = re.match(r"""global\s+\$version\s*=\s*["']?([\d.]+)["']?""", line, re.IGNORECASE)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def binary_path(name: str) -> str:
    """返回打包二进制文件（3dmloader.dll / 3DMigoto Loader.exe / ritual/upx.exe 等）的完整路径。"""
    return os.path.join(BINARIES_DIR, name)
