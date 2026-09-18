"""
pyi_filter.py —— OMGLite 打包白名单（仅供 OMGLite/OMG.spec 使用）

存在理由
--------
`src/omg/resources/` 同时扮演两个角色：
  1. **随包分发的只读资源**（icons/、binaries/ 里的运行必需资产）；
  2. **运行时数据目录**（alchemy/ 下载源码、缓存蓝奏云已构建文件、落构建产物）。

OMG.spec 原先写 `datas=[(RESOURCES, "resources")]` 整棵树照收，于是「本地在开发态
跑过一次便捷构建」就会把几百 MB 的源码树、MSBuild 中间产物和运行时缓存一起打进
发行包（实测 788 MB 的冻结产物里有 ~442 MB 是这么来的）。

本模块把「该进包的下限集合」显式写出来，构建时用它遍历生成 datas 列表；同时提供
Qt 侧的二道过滤，剔除 PyInstaller / PySide6 hook 无差别收集进来、而本项目并不需要
的二进制与翻译。

设计约束
--------
· 只依赖标准库 —— 可在没有 PyInstaller 的解释器里直接 `python pyi_filter.py` 自测。
· 所有路径先归一化为正斜杠再比较，避免 Windows 反斜杠漏判。
· **只做排除**，不新增任何文件；目录 / 文件缺失一律静默跳过（幂等，可重复跑）。
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# resources/ 侧
# ---------------------------------------------------------------------------
#: alchemy 下 **运行时生成** 的子目录 —— 一律不进包。
#: 便捷构建页会在这些目录里下载源码、缓存云端已构建文件、落 artifact/output，
#: 打包时若照收就会把本地一次操作的结果变成几百 MB 的发行体积。
#: 「纯在线」策略下连 project/ 也不发：用户首次构建时由页面从 GitHub 拉取。
RUNTIME_DIRS = ("project", "cloud", "output", "artifact", "input")

#: 无调用方或与别处字节重复的整棵目录（相对 resources/ 的路径）
DROP_TREES = (
    # 唯一潜在使用方 omg/domain/media/HoYoMediaExtractor._find_ffmpeg()，
    # 而 omg.domain.media / omg.domain.modtools 全库无任何 import 方；
    # 且它探测的是 <pkg>/../lib/ffmpeg/bin/，与打包落点 resources/binaries/ 也不一致。
    "binaries/ffmpeg",
    # d3d11 种子（预涂装版）。改为「必须先走一次便捷构建」——它本来就在
    # inject/loader.py::_copy_d3d11_to_gimi 的三级 fallback 里抢在最前面。
    "binaries/MysteriousRitual",
)

#: 无调用方或与别处 sha256 完全相同的单文件（相对 resources/ 的路径）
DROP_FILES = (
    # 原本被 MysteriousRitual 永久遮蔽，永不触达
    "binaries/d3d11.dll",
    # 唯一使用方 omg/domain/modtools/texture_resizer.py，该包全库无 import 方
    "binaries/texture_tools.dll",
    # 与 binaries/ritual/ 下同名文件字节完全相同；
    # alchemy.UPX_EXE / DIVERSIFIER_DLL 与 d3d11InOMG.run_pack/run_paint
    # 传的都是显式 ritual/ 路径，根副本无用
    "binaries/upx.exe",
    "binaries/pe_diversifier.dll",
)

#: MSBuild / 编译中间产物扩展名。若将来决定把某个源码版本打进包（把
#: RUNTIME_DIRS 里的 "project" 去掉即可），这道过滤负责兜住 288 MB 的 .obj/.pdb。
SKIP_EXT = frozenset({
    ".obj", ".iobj", ".ipdb", ".pdb", ".pch", ".tlog",
    ".idb", ".ilk", ".chk", ".recipe", ".clean", ".log",
})


def _norm(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def _is_dropped_dir(rel_parts: tuple, rel_path: str) -> bool:
    """rel_parts / rel_path 均以 resources/ 为根、已归一化。"""
    if not rel_parts:
        return False
    if rel_parts[0] == "alchemy":
        # alchemy 整体只留「非运行时」内容：目前等于什么都不留。
        if len(rel_parts) == 1:
            return False
        return rel_parts[1] in RUNTIME_DIRS
    for tree in DROP_TREES:
        tree_parts = tree.split("/")
        if rel_parts[:len(tree_parts)] == tuple(tree_parts):
            return True
    return False


def collect_resources(resources_root: str, prefix: str = "resources"):
    """遍历 resources/ 生成 PyInstaller 的 datas 列表（白名单过滤后的结果）。

    Args:
        resources_root: 源码里的 `src/omg/resources`。
        prefix: 打包后的目标前缀（应与 `omg.core.paths.RESOURCES_DIR` 假设一致）。

    Returns:
        list[tuple[str, str]]，每项为 ``(源文件绝对/相对路径, 目标目录)``。
    """
    out: list = []
    if not os.path.isdir(resources_root):
        return out

    for dirpath, dirnames, filenames in os.walk(resources_root):
        rel_dir = os.path.relpath(dirpath, resources_root)
        rel_parts = () if rel_dir == "." else tuple(_norm(rel_dir).split("/"))
        rel_path = "/".join(rel_parts)

        # 剪枝：命中的子目录不再下钻
        keep = []
        for name in dirnames:
            child_parts = rel_parts + (name,)
            if _is_dropped_dir(child_parts, "/".join(child_parts)):
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            rel_file = f"{rel_path}/{name}" if rel_path else name
            if rel_file in DROP_FILES:
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            dest_dir = f"{prefix}/{rel_path}" if rel_path else prefix
            out.append((os.path.join(dirpath, name), dest_dir))
    return out


# ---------------------------------------------------------------------------
# Qt / PySide6 侧二道过滤
# ---------------------------------------------------------------------------
#: PyInstaller 的 PySide6 hook 无差别收集进来、本项目用不到的二进制。
#: 依据：`PyInstaller/utils/hooks/qt/__init__.py::collect_extra_binaries()`
#: （第 641-642 行）专为「动态 OpenGL 应用」收集 opengl32sw / d3dcompiler_??，
#: 而 OMGLite 是纯 QWidget 光栅渲染；QtQuick/Qml 全库零引用；Qt6Pdf 无 QPdfDocument 使用方。
#:
#: 严禁在此删除 libcrypto-3.dll / libssl-3.dll —— 它们是 Python 3.11 标准库 _ssl 的
#: 64 位运行时依赖（PE Machine=0x8664），ssl.py / requests 走 HTTPS 都靠它。
#: 上一轮误判为「32 位 Qt 版」而剔除，导致冻结程序一 import ssl 就
#: "DLL load failed while importing _ssl"。已在干净构建 + 真机 import ssl 验证。
QT_DROP_BINARIES = frozenset({
    "opengl32sw.dll",
    "d3dcompiler_47.dll",
    "qt6pdf.dll",
    "qt6quick.dll",
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll",
    "qt6virtualkeyboard.dll",
})

#: 目标路径片段（dest 已归一化为正斜杠）。platforminputcontexts 整目录随
#: Qt6VirtualKeyboard 一起消失；qdirect2d/qminimal/qoffscreen 三个 platform
#: 插件本项目不用（只用 qwindows），imageformats 里保留 png/jpeg/webp/svg/ico/gif。
QT_DROP_PLUGIN_PATHS = (
    "plugins/platforms/qdirect2d.dll",
    "plugins/platforms/qminimal.dll",
    "plugins/platforms/qoffscreen.dll",
    "plugins/platforminputcontexts/",
    "plugins/imageformats/qpdf.dll",
    "plugins/imageformats/qicns.dll",
    "plugins/imageformats/qtga.dll",
    "plugins/imageformats/qwbmp.dll",
)

#: Qt 自带翻译只保留简体中文（界面文案本身全为硬编码中文；保留它是为了
#: QFileDialog / QMessageBox 这类 Qt 标准对话框的按钮文字）。
QT_KEEP_TRANSLATION_LANG = "zh_CN"


def _entry_dest(entry) -> str:
    """取 PyInstaller TOC 项的「目标名」，兼容 2/3 元组。"""
    return _norm(str(entry[0]))


def filter_qt_binaries(entries):
    """过滤 PyInstaller `a.binaries`。"""
    kept = []
    for entry in entries:
        dest = _entry_dest(entry)
        if os.path.basename(dest).lower() in QT_DROP_BINARIES:
            continue
        if any(frag in dest for frag in QT_DROP_PLUGIN_PATHS):
            continue
        kept.append(entry)
    return kept


def filter_qt_datas(entries):
    """过滤 PyInstaller `a.datas`（只处理 Qt 翻译目录）。"""
    kept = []
    for entry in entries:
        dest = _entry_dest(entry)
        if "pyside6/translations/" in dest.lower():
            if QT_KEEP_TRANSLATION_LANG not in dest:
                continue
        kept.append(entry)
    return kept


# ---------------------------------------------------------------------------
# 自测 / 试算：python pyi_filter.py [resources_root]
# ---------------------------------------------------------------------------
def _self_test(root: str) -> int:
    items = collect_resources(root)
    total = 0
    by_dir: dict = {}
    for src, dest in items:
        size = os.path.getsize(src) if os.path.isfile(src) else 0
        total += size
        top = "/".join(dest.split("/")[:2])
        acc = by_dir.setdefault(top, [0, 0])
        acc[0] += size
        acc[1] += 1

    print(f"resources_root = {root}")
    print(f"入选 {len(items)} 个文件，合计 {total / 1048576:.2f} MB")
    for top, (size, count) in sorted(by_dir.items(), key=lambda kv: -kv[1][0]):
        print(f"  {size / 1048576:10.2f} MB {count:5d}  {top}")

    # 断言：运行时目录一个都不能入选
    bad = [d for _, d in items
           if any(part in RUNTIME_DIRS for part in _norm(d).split("/")[1:])]
    if bad:
        print(f"[FAIL] 运行时目录泄漏进包: {bad[:5]}", file=sys.stderr)
        return 1

    # 断言：黑名单文件一个都不能入选
    leak = [s for s, _ in items
            if any(_norm(s).endswith(f) for f in DROP_FILES)]
    if leak:
        print(f"[FAIL] 黑名单文件泄漏进包: {leak}", file=sys.stderr)
        return 1

    print("[OK] 运行时目录与黑名单文件均未入选")
    return 0


if __name__ == "__main__":
    default_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "src", "omg", "resources"
    )
    raise SystemExit(_self_test(sys.argv[1] if len(sys.argv) > 1 else default_root))
