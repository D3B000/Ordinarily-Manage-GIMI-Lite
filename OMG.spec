# -*- mode: python ; coding: utf-8 -*-
# OMG.spec — src-layout 自动收集（Phase 5 真包收口）+ 资源白名单（体积收口）
#
# 运行（在本仓库根目录执行，且环境已安装 pyinstaller / PySide6 / pefile / capstone）：
#     pyinstaller --noconfirm --workpath build --distpath dist OMG.spec
#     或直接跑 build_pyinstaller.ps1
#
# 说明：
#   - pathex=['src'] 让 PyInstaller 按标准包自动收集 omg.*，不硬编码 hiddenimports。
#   - resources/ 的 datas 由 pyi_filter.collect_resources() 生成 —— **白名单**而非整树，
#     见 pyi_filter.py 顶部说明（resources/ 同时是运行时数据目录，整树照收会把本地
#     跑过一次便捷构建的源码树与编译残渣带进发行包，实测占 442 MB）。
#   - contents_directory='bin' 使运行时 sys._MEIPASS = <exe_dir>\bin，
#     与 omg.core.paths 对 PyInstaller 路径的假设一致。
import os
import sys

block_cipher = None

SRC = "src"
RESOURCES = os.path.join(SRC, "omg", "resources")
APP_ICO = os.path.join(RESOURCES, "icons", "krr.ico")

# 资源白名单 / Qt 二道过滤逻辑集中在此模块，可脱离 PyInstaller 自测：
#     python pyi_filter.py
sys.path.insert(0, globals().get("SPECPATH", os.getcwd()))
from pyi_filter import (  # noqa: E402
    collect_resources,
    filter_qt_binaries,
    filter_qt_datas,
)

a = Analysis(
    [os.path.join(SRC, "omg", "app.py")],
    pathex=[SRC],
    binaries=[],
    datas=collect_resources(RESOURCES),
    hiddenimports=[
        "omg",
        "omg.core",
        "omg.core.win32",
        "omg.domain",
        "omg.domain.inject",
        "omg.domain.ritual",
        "omg.domain.upx",
        "omg.ui",
        "omg.pages",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 涂装(py) 的 Cython 加速统一从 bin/resources/binaries/ritual/_cython_analysis.pyd
    # （datas 收集）加载，不再把 pyd 收进 omg.domain.ritual.prlib 包内。
    #
    # 其余排除项分三类：
    #   1) 用不到的 GUI/标准库绑定；
    #   2) 全库无 import 方的自有死包 —— domain/media（连带 ffmpeg 死资产）
    #      与 domain/modtools（连带 texture_tools.dll 死资产）；
    #   3) 被 hook 误收的第三方 —— cryptography（由 requests→urllib3 的
    #      pyopenssl 支线带进，全库零引用，还附带 pyi_rth_cryptography_openssl）、
    #      zstandard（零引用）、pywin32 的 win32api/win32evtlog（由
    #      logging.handlers.NTEventLogHandler 带进，项目访问 Win32 一律走 ctypes）。
    #   注意：capstone 必须保留 —— prlib/analysis.py 的 BFS 解码本体就是它，
    #   TransformOptions.skip_code_analysis 默认 False（alchemy._paint_py 也没关），
    #   缺了会丢失 branch/RIP 引用发现。requests / certifi / charset_normalizer
    #   也不能排除 —— 蓝奏云下载依赖它们。
    excludes=[
        "PyQt5",
        "PyQt6",
        "tkinter",
        "omg.domain.ritual.prlib._cython_analysis",
        "omg.domain.media",
        "omg.domain.modtools",
        "cryptography",
        "zstandard",
        "win32api",
        "win32evtlog",
        "win32evtlogutil",
        "win32pdh",
        "win32security",
        "win32process",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# 二道过滤：PySide6 hook 无差别收集进来的 Qt 二进制 / 插件 / 翻译。
# 必须在 PYZ 之前 —— 后面的 PYZ / EXE / COLLECT 都以 a.binaries / a.datas 为准。
a.binaries = filter_qt_binaries(a.binaries)
a.datas = filter_qt_datas(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OMG",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    contents_directory="bin",
    icon=APP_ICO if os.path.isfile(APP_ICO) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="OMG",
)

# NOTE: No qt.conf is generated. PySide6 6.10 registers its own plugin path
# (bin/PySide6/plugins) at import via its relocatable mechanism, so the frozen
# app resolves qwindows.dll without qt.conf. Verified: dist/OMG starts without it.

