"""
omg.core.alchemy — 「便捷构建」页（omg.pages.quick_build）的业务编排层。

把「源 = GitHub 源码」这条链路的全部后端动作集中在此，UI 层只负责发信号与
刷新控件，二者通过 QThread + Signal 解耦：

  D3D11 卡片    拉取 SpectrumQT/XXMI-Libs-Package 近 N 个 release 版本号
                → 下载所选版本的「源码 zip」→ 解压到
                  resources/alchemy/project/XXMI-Libs-Package-<version>/
  构建 卡片     检测本机 MSBuild（Builder）→ 编译选定项目；「优化构建」开启时
                向源文件注入随机死代码（见 omg.domain.ritual.d3d11_builder）
                → 产物 d3d11.dll 落到 resources/alchemy/artifact/
  文件优化 卡片 对 artifact/d3d11.dll 执行仪式（包装 / 涂装 py / 涂装 rs）
                → 产物落到 resources/alchemy/output/
  自动化 卡片   把 artifact 或 output 的 d3d11.dll 复制 / 替换到 GIMI 目录根

「源 = 已构建文件」链路（DoPE 侧批量构建好的 dll 放在蓝奏云）：
  D3D11 卡片    「下载」→ 免登录拉取蓝奏云文件夹分享里全部 <时间戳>.zip
                → 解压到 resources/alchemy/cloud/<时间戳>/
 文件优化 卡片  目标文件留空（placeholder 提示"随机抽取 cloud 最新批次"）；
                点「执行」时直接从 cloud/ 最新批次里 **随机**抽一个子文件夹的
                d3d11.dll **就地**执行仪式（不复制），产物同样落到 output/。
                若用户手动指定了目标文件（例如 input/d3d11.dll），则**无论源是
                哪种模式**都只对该指定文件执行仪式，不再做随机抽取。

目录约定（全部位于 resources/alchemy 下，随包分发，开发模式锚定到 src/omg）：
  project/   —— 下载的源码工程（可并存多个版本）
  artifact/  —— 构建产物 d3d11.dll
  output/    —— 仪式产物 d3d11.dll
  cloud/     —— 蓝奏云下载的已构建文件（每包一个 <时间戳>/ 目录）
  input/     —— 用户手动放置待处理 dll 的地方（自动化流程不再写入这里）

所有网络 / 磁盘 / 子进程 IO 都在 QThread 内同步执行，通过信号回传进度与结果，
绝不阻塞 UI 线程。本模块只依赖 PySide6.QtCore，可在 headless 环境导入与自测。
"""

from __future__ import annotations

import os
import re
import sys
import random
import shutil
import importlib
import importlib.machinery
import importlib.util
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from omg.core import download
from omg.core import lanzou
from omg.core import updates
from omg.core.gimi_update import DownloadSignals, _parse_version
from omg.core.logging_setup import logger
from omg.core.paths import BINARIES_DIR, RESOURCES_DIR
from omg.core.ritual_loader import load_ritual_bundle

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
XXMI_OWNER = "SpectrumQT"
XXMI_REPO = "XXMI-Libs-Package"

#: 拉取的 release 数量（UI 下拉框「近期 7 个版本」）
RELEASE_LIMIT = 7

XXMI_API_LIST_URL = (
    f"https://api.github.com/repos/{XXMI_OWNER}/{XXMI_REPO}/releases"
    f"?per_page={RELEASE_LIMIT}"
)
XXMI_RELEASES_PAGE_URL = f"https://github.com/{XXMI_OWNER}/{XXMI_REPO}/releases"

#: 源码 zip（GitHub 自动生成的 tag 归档），解压后顶层目录名即 XXMI-Libs-Package-<version>
SOURCE_ZIP_URL = (
    f"https://github.com/{XXMI_OWNER}/{XXMI_REPO}/archive/refs/tags/{{tag}}.zip"
)

# --- alchemy 目录 ---
ALCHEMY_DIR = os.path.join(RESOURCES_DIR, "alchemy")
PROJECT_DIR = os.path.join(ALCHEMY_DIR, "project")
ARTIFACT_DIR = os.path.join(ALCHEMY_DIR, "artifact")
OUTPUT_DIR = os.path.join(ALCHEMY_DIR, "output")
#: 源 = 「已构建文件」时，蓝奏云下载的压缩包解压到 cloud/ 下
CLOUD_DIR = os.path.join(ALCHEMY_DIR, "cloud")
#: 从 cloud/ 随机抽中的 dll 会先落位到 input/，再作为仪式的输入
INPUT_DIR = os.path.join(ALCHEMY_DIR, "input")

PROJECT_PREFIX = "XXMI-Libs-Package-"
SOLUTION_NAME = "StereovisionHacks.sln"

ARTIFACT_DLL = os.path.join(ARTIFACT_DIR, "d3d11.dll")
OUTPUT_DLL = os.path.join(OUTPUT_DIR, "d3d11.dll")
INPUT_DLL = os.path.join(INPUT_DIR, "d3d11.dll")

#: cloud/ 下「批次目录」的命名（DoPE 打包时的时间戳，如 20260825_135633）
BATCH_NAME_RE = re.compile(r"^\d{8}_\d{6}$")
#: 仪式只认这一个文件名
DLL_NAME = "d3d11.dll"
#: 已构建文件的蓝奏云分享（由 DoPE 侧上传），免登录下载实现见 omg.core.lanzou
LANZOU_URL = lanzou.DEFAULT_SHARE_URL
LANZOU_PWD = lanzou.DEFAULT_SHARE_PWD

# 仪式依赖的二进制（随包分发在 resources/binaries/ritual 下）
UPX_EXE = os.path.join(BINARIES_DIR, "ritual", "upx.exe")
DIVERSIFIER_DLL = os.path.join(BINARIES_DIR, "ritual", "pe_diversifier.dll")
# 仪式的全部符号（prlib / d3d11_builder / d3d11InOMG）编译进单一
# ritual_bundle.pyd（见 omg.core.ritual_loader），公开仓库只分发该二进制，
# 不含 ritual 源码。

# 仪式类型（与 UI 分段控件顺序一致）
RITUAL_PACK = "pack"            # 包装：UPX 压缩 + 补齐 junk section
RITUAL_PAINT_PY = "paint_py"    # 涂装(py)：prlib 纯 Python 实现
RITUAL_PAINT_RS = "paint_rs"    # 涂装(rs)：Rust pe_diversifier DLL

# 复制到 GIMI 的来源
COPY_ARTIFACT = "artifact"
COPY_OUTPUT = "output"

# 下载 / 解压阶段的进度占比（避免进度圈在解压时静止后突然跳满）
_DL_END = 85.0
_EX_START = 86.0
_EX_END = 99.0


# ---------------------------------------------------------------------------
# 目录 / 版本工具
# ---------------------------------------------------------------------------
def ensure_dirs() -> None:
    """确保 project / artifact / output / cloud / input 五个目录存在（幂等）。"""
    for d in (PROJECT_DIR, ARTIFACT_DIR, OUTPUT_DIR, CLOUD_DIR, INPUT_DIR):
        os.makedirs(d, exist_ok=True)


def normalize_version(tag: str) -> str:
    """``v1.0.5`` → ``1.0.5``（release tag 去掉前导 v/V）。"""
    return (tag or "").strip().lstrip("vV")


def project_dir_for(version: str) -> str:
    """返回某版本源码工程目录（无论是否已下载）。"""
    return os.path.join(PROJECT_DIR, f"{PROJECT_PREFIX}{normalize_version(version)}")


def source_zip_url(tag: str) -> str:
    return SOURCE_ZIP_URL.format(tag=tag)


def _release_entry(tag: str) -> dict:
    """组装下拉框可用的一条 release 记录。"""
    ver = normalize_version(tag)
    return {
        "tag": tag,
        "version": ver,
        "label": ver,
        "zip_url": source_zip_url(tag),
    }


def _sort_key(entry: dict):
    return _parse_version(entry.get("version", ""))


# ---------------------------------------------------------------------------
# 拉取 release 列表
# ---------------------------------------------------------------------------
def fetch_releases(limit: int = RELEASE_LIMIT,
                   status_cb: Optional[Callable[[str], None]] = None) -> list:
    """拉取近 ``limit`` 个 release（新 → 旧）。

    策略：GitHub API 列表（1 次请求）→ releases 页面 HTML 抓取兜底。
    多代理 failover / SSL 回退由 omg.core.download 内部处理。

    Args:
        limit: 最多返回的版本数。
        status_cb: 可选回调 fn(text)，用于把「通过哪个代理」反馈给 UI。

    Returns:
        list[dict]，每项含 tag / version / label / zip_url；失败返回空列表。
    """
    def _emit(text: str) -> None:
        if status_cb:
            try:
                status_cb(text)
            except Exception:
                pass

    # --- 快速路径：GitHub API ---
    def _api_status(proxy_label, idx, total):
        _emit(f"通过 {proxy_label} · API")

    data = download.fetch_json_api_smart(XXMI_API_LIST_URL, status_cb=_api_status)
    if isinstance(data, list) and data:
        entries = []
        for rel in data:
            if rel.get("draft"):
                continue
            tag = (rel.get("tag_name") or "").strip()
            if not tag:
                continue
            entries.append(_release_entry(tag))
        if entries:
            entries.sort(key=_sort_key, reverse=True)
            logger.info("XXMI 源码版本拉取成功（API）：%s",
                        ", ".join(e["version"] for e in entries[:limit]))
            return entries[:limit]
        logger.info("XXMI API 返回为空，回退 HTML 抓取")
    else:
        logger.info("XXMI API 不可用，回退 HTML 抓取")

    # --- 兜底：releases 页面 HTML ---
    def _page_status(proxy_label, idx, total):
        _emit(f"通过 {proxy_label} · releases")

    html = download.fetch_url_smart(XXMI_RELEASES_PAGE_URL, status_cb=_page_status)
    if not html:
        return []

    seen, tags = set(), []
    for m in re.findall(r'/releases/tag/([^"\'&\s]+)', html):
        tag = m.strip()
        if not tag or tag.startswith("*") or len(tag) <= 1:
            continue
        if tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)

    entries = [_release_entry(t) for t in tags]
    entries.sort(key=_sort_key, reverse=True)
    if entries:
        logger.info("XXMI 源码版本拉取成功（HTML）：%s",
                    ", ".join(e["version"] for e in entries[:limit]))
    return entries[:limit]


# ---------------------------------------------------------------------------
# 本地项目（project 目录）枚举
# ---------------------------------------------------------------------------
def list_project_versions() -> list:
    """列出 project/ 下已下载的源码版本（新 → 旧）。

    目录名形如 ``XXMI-Libs-Package-1.0.5``，展示用版本号即 ``1.0.5``。
    """
    if not os.path.isdir(PROJECT_DIR):
        return []
    versions = []
    for name in os.listdir(PROJECT_DIR):
        if not name.startswith(PROJECT_PREFIX):
            continue
        path = os.path.join(PROJECT_DIR, name)
        if not os.path.isdir(path):
            continue
        ver = name[len(PROJECT_PREFIX):].strip()
        if ver:
            versions.append(ver)
    versions.sort(key=_parse_version, reverse=True)
    return versions


# ---------------------------------------------------------------------------
# 下载 + 解压
# ---------------------------------------------------------------------------
def _single_top_dir(root: str) -> Optional[str]:
    """若 root 下只有一个顶层目录（GitHub 源码 zip 的常见结构），返回其路径。"""
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    dirs = [os.path.join(root, e) for e in entries
            if os.path.isdir(os.path.join(root, e))]
    if len(dirs) == 1 and len(entries) == 1:
        return dirs[0]
    return None


def _cleanup_path(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def download_and_extract(release: dict, signals: DownloadSignals,
                         should_cancel=None,
                         progress_cb: Optional[Callable[[float], None]] = None
                         ) -> tuple:
    """下载指定版本源码 zip 并解压到 project/XXMI-Libs-Package-<version>。

    同版本重名目录会被整体替换（先下载到暂存目录，成功后再替换，避免半包覆盖）。

    Args:
        release: fetch_releases 返回的条目（需含 version / zip_url）。
        signals: DownloadSignals，接收下载阶段进度与状态。
        should_cancel: 可选无参可调用；返回 True 时中断下载 / 解压。
        progress_cb: 可选回调 ``percent(0~100)``，用于 UI 细分解压阶段进度。

    Returns:
        (success, message)
    """
    version = normalize_version(release.get("version", ""))
    zip_url = release.get("zip_url", "")
    if not version or not zip_url:
        return False, "版本信息不完整"

    ensure_dirs()
    zip_path = os.path.join(tempfile.gettempdir(), f"omg_xxmi_src_{version}.zip")

    signals.status.emit("正在下载源码…", "info")
    ok, err = download.download_file_smart(
        zip_url, zip_path, signals=signals, should_cancel=should_cancel
    )
    if not ok:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
        if err == "已取消":
            return False, "已取消"
        return False, f"下载失败: {err}"

    def _p(value: float) -> None:
        if progress_cb:
            progress_cb(max(0.0, min(100.0, float(value))))

    # 暂存目录：先完整解压，再整体替换目标目录（失败只丢暂存，不污染已有工程）
    stage = os.path.join(PROJECT_DIR, f".stage_{version}")
    _cleanup_path(stage)
    os.makedirs(stage, exist_ok=True)

    try:
        signals.status.emit("正在解压…", "info")
        _p(_EX_START)
        with zipfile.ZipFile(zip_path, "r") as zf:
            updates.safe_extract_zip(
                zf, stage,
                should_cancel=should_cancel,
                progress_cb=lambda f: _p(
                    _EX_START + max(0.0, min(1.0, float(f))) * (_EX_END - _EX_START)
                ),
            )
    except updates.ExtractCancelled:
        _cleanup_path(stage)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return False, "已取消"
    except Exception as e:
        _cleanup_path(stage)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        logger.error("源码解压失败: %s", e, exc_info=True)
        return False, f"解压失败: {e}"

    try:
        top = _single_top_dir(stage) or stage
        target = project_dir_for(version)
        if os.path.abspath(top) != os.path.abspath(target):
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(top, target)
        if os.path.abspath(stage) != os.path.abspath(target):
            _cleanup_path(stage)
    except Exception as e:
        logger.error("源码目录落位失败: %s", e, exc_info=True)
        return False, f"整理目录失败: {e}"
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    _p(100.0)
    return True, f"源码已就绪 {version}"


# ---------------------------------------------------------------------------
# 源 = 已构建文件：蓝奏云下载 → cloud/<时间戳>/
# ---------------------------------------------------------------------------
def list_cloud_batches() -> list:
    """列出 cloud/ 下的批次目录名（新 → 旧）。

    目录名是 DoPE 打包时的时间戳 ``YYYYMMDD_HHMMSS``，字典序即时间序，
    倒序后第一项就是最新批次。非时间戳命名的目录跟在后面。
    """
    if not os.path.isdir(CLOUD_DIR):
        return []
    names = []
    for n in os.listdir(CLOUD_DIR):
        if n.startswith("."):
            continue
        if os.path.isdir(os.path.join(CLOUD_DIR, n)):
            names.append(n)
    stamped = sorted([n for n in names if BATCH_NAME_RE.match(n)], reverse=True)
    others = sorted([n for n in names if not BATCH_NAME_RE.match(n)], reverse=True)
    return stamped + others


def list_batch_dlls(batch_dir: str) -> list:
    """列出某批次目录里所有可参与的 d3d11.dll。

    DoPE 打的包结构是 ``<时间戳>/#1/3dmloader.dll``、``#2/d3d11.dll`` ……
    其中 ``#1`` 只有 3dmloader、**没有 d3d11**，所以必须按「子文件夹里确实存在
    d3d11.dll」筛选，不能无脑枚举子目录。
    """
    cands = []
    if not os.path.isdir(batch_dir):
        return cands
    direct = os.path.join(batch_dir, DLL_NAME)
    if os.path.isfile(direct):
        cands.append(direct)
    try:
        subs = sorted(os.listdir(batch_dir))
    except OSError:
        return cands
    for name in subs:
        if name.startswith("."):
            continue
        p = os.path.join(batch_dir, name, DLL_NAME)
        if os.path.isfile(p):
            cands.append(p)
    return cands


def pick_random_prebuilt_dll(progress_cb: Optional[Callable[[float], None]] = None
                             ) -> tuple:
    """从最新批次里随机挑一个 d3d11.dll（**不做任何复制**，直接返回 cloud 里的路径）。

    该函数同时也是仪式的 ``prepare`` 回调：抽中的 dll 会被就地当作仪式输入，
    产物写到 output/。cloud/ 里的原件不会被修改（三种仪式都只读 src、只写 out_dir）。

    Args:
        progress_cb: 可选的进度回调 fn(percent 0~100)。作为 prepare 时由
            :func:`run_ritual` 传入子区间进度函数。

    Returns:
        (ok, message, path)；失败时 path 为空串。
    """
    try:
        batches = list_cloud_batches()
        if not batches:
            return False, "cloud 目录为空，请先点击「下载」获取已构建文件", ""
        for batch in batches:
            cands = list_batch_dlls(os.path.join(CLOUD_DIR, batch))
            if not cands:
                continue
            pick = random.choice(cands)
            rel = os.path.relpath(pick, CLOUD_DIR)
            logger.info("已构建文件：批次 %s 共 %d 个候选，随机选中 %s",
                        batch, len(cands), rel)
            if progress_cb:
                progress_cb(100.0)
            return True, f"已随机选取 {rel}（共 {len(cands)} 个候选）", pick
        return False, "cloud 目录里没有找到任何 d3d11.dll", ""
    except Exception as e:
        logger.error("挑选已构建文件失败: %s", e, exc_info=True)
        return False, f"挑选已构建文件失败: {e}", ""


def stage_input_dll(progress_cb: Optional[Callable[[float], None]] = None) -> tuple:
    """随机抽一个 cloud/ 里的 dll 落位成 input/d3d11.dll（仪式输入）。

    与 :func:`pick_random_prebuilt_dll` 的区别：本函数会把抽中的 dll **复制**到
    input/d3d11.dll，方便用户显式把目标指向 input/ 时拿到一个真实存在的文件。

    UI 默认不再调用本函数（「已构建文件」模式下直接在 cloud 里随机抽，不落 input），
    但保留作为手动落位工具。

    Returns:
        (ok, message, path)；失败时 path 为空串。
    """
    try:
        ensure_dirs()
        if progress_cb:
            progress_cb(20.0)
        ok, msg, src = pick_random_prebuilt_dll()
        if not ok:
            return False, msg, ""
        if os.path.isfile(INPUT_DLL):
            try:
                os.remove(INPUT_DLL)
            except OSError:
                pass
        shutil.copy2(src, INPUT_DLL)
        if progress_cb:
            progress_cb(100.0)
    except Exception as e:
        logger.error("落位 input/d3d11.dll 失败: %s", e, exc_info=True)
        return False, f"准备输入文件失败: {e}", ""
    if not os.path.isfile(INPUT_DLL):
        return False, f"未生成输入文件: {INPUT_DLL}", ""
    return True, f"已随机选取 {msg} → {INPUT_DLL}", INPUT_DLL


def _cloud_stem(name: str) -> str:
    """压缩包名 → cloud/ 下的目标目录名（去掉扩展名并清洗非法字符）。"""
    return os.path.splitext(lanzou.sanitize(name))[0] or "unnamed"


def _extract_cloud_archive(zip_path: str, stem: str, should_cancel=None,
                           progress_cb: Optional[Callable[[float], None]] = None
                           ) -> tuple:
    """把 zip 解压成 cloud/<目录名>/（暂存 + 整体替换，失败只丢暂存）。"""
    stage = os.path.join(CLOUD_DIR, f".stage_{stem}")
    _cleanup_path(stage)
    os.makedirs(stage, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            updates.safe_extract_zip(
                zf, stage, should_cancel=should_cancel, progress_cb=progress_cb
            )
    except updates.ExtractCancelled:
        _cleanup_path(stage)
        return False, "已取消"
    except Exception as e:
        _cleanup_path(stage)
        logger.error("已构建文件解压失败: %s", e, exc_info=True)
        return False, f"解压失败: {e}"

    try:
        top = _single_top_dir(stage) or stage
        name = os.path.basename(os.path.normpath(top))
        # 压缩包没有统一顶层目录时，退回用包名当目录名
        if top == stage or not name or name.startswith("."):
            name = stem
        dst = os.path.join(CLOUD_DIR, name)
        if os.path.abspath(top) != os.path.abspath(dst):
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.move(top, dst)
        if os.path.abspath(stage) != os.path.abspath(dst):
            _cleanup_path(stage)
    except Exception as e:
        logger.error("已构建文件目录落位失败: %s", e, exc_info=True)
        return False, f"整理目录失败: {e}"
    return True, name


def fetch_prebuilt(progress_cb: Optional[Callable[[float], None]] = None,
                   status_cb: Optional[Callable[[str], None]] = None,
                   should_cancel=None,
                   url: str = "", pwd: str = "") -> tuple:
    """下载蓝奏云分享里的已构建文件包并解压到 cloud/<时间戳>/。

    已存在且非空的批次会跳过，重复点击不会重复下载。

    Args:
        progress_cb: 进度回调 ``fn(percent 0~100)``。
        status_cb: 状态文本回调 ``fn(text)``。
        should_cancel: 返回 True 时中断下载 / 解压。
        url / pwd: 覆盖默认的蓝奏云分享链接与提取码。

    Returns:
        (success, message)
    """
    def _s(text: str) -> None:
        if status_cb:
            try:
                status_cb(str(text))
            except Exception:
                pass

    def _p(value: float) -> None:
        if progress_cb:
            try:
                progress_cb(max(0.0, min(100.0, float(value))))
            except Exception:
                pass

    ensure_dirs()
    _s("正在获取文件列表…")
    _p(1.0)
    try:
        folder = lanzou.LanzouFolder(url or LANZOU_URL, pwd or LANZOU_PWD)
        files = folder.list_files(status_cb=_s)
    except Exception as e:
        logger.error("蓝奏云列目录失败: %s", e, exc_info=True)
        return False, f"获取文件列表失败: {e}"
    if should_cancel and should_cancel():
        return False, "已取消"
    if not files:
        return False, "分享内没有可下载的文件（链接失效或提取码错误）"

    total = len(files)
    done = skipped = 0
    for idx, entry in enumerate(files, 1):
        if should_cancel and should_cancel():
            return False, "已取消"

        name = lanzou.sanitize(entry.get("name", ""))
        stem = _cloud_stem(name)
        is_zip = name.lower().endswith(".zip")
        slot = (idx - 1) * 100.0 / total
        span = 100.0 / total

        # 已就绪的批次 / 文件直接跳过
        if is_zip:
            if os.path.isdir(os.path.join(CLOUD_DIR, stem)) and \
                    os.listdir(os.path.join(CLOUD_DIR, stem)):
                skipped += 1
                _s(f"{name} 已存在，跳过")
                _p(slot + span)
                continue
        else:
            dst_file = os.path.join(CLOUD_DIR, name)
            if os.path.isfile(dst_file) and os.path.getsize(dst_file) > 0:
                skipped += 1
                _s(f"{name} 已存在，跳过")
                _p(slot + span)
                continue

        _s(f"正在下载 {name}（{entry.get('size', '')}）")
        tmp = os.path.join(tempfile.gettempdir(), f"omg_cloud_{stem}.part")

        def _dl_progress(rec, tot, _slot=slot, _span=span) -> None:
            if tot and tot > 0:
                _p(_slot + min(1.0, rec / float(tot)) * _span * (_DL_END / 100.0))
            else:
                _p(_slot)      # 服务端未给长度：停在槽位起点，靠状态文本提示

        ok, err = folder.download(
            entry.get("id", ""), tmp,
            should_cancel=should_cancel, progress_cb=_dl_progress,
        )
        if not ok:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if err == "已取消":
                return False, "已取消"
            return False, f"下载 {name} 失败: {err}"

        _p(slot + span * (_DL_END / 100.0))

        if not is_zip:
            try:
                os.replace(tmp, os.path.join(CLOUD_DIR, name))
            except Exception as e:
                logger.error("保存 %s 失败: %s", name, e, exc_info=True)
                return False, f"保存 {name} 失败: {e}"
            done += 1
            _p(slot + span)
            continue

        _s(f"正在解压 {name}…")
        ok, info = _extract_cloud_archive(
            tmp, stem, should_cancel=should_cancel,
            progress_cb=lambda f, _slot=slot, _span=span: _p(
                _slot + _span * (_DL_END + (100.0 - _DL_END)
                                 * max(0.0, min(1.0, float(f)))) / 100.0
            ),
        )
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if not ok:
            return False, info or f"解压 {name} 失败"
        _s(f"{name} 已解压到 cloud/{info}")
        done += 1
        _p(slot + span)

    _p(100.0)
    if done:
        tail = f"，跳过 {skipped} 个" if skipped else ""
        return True, f"已下载 {done} 个包{tail}"
    return True, f"全部 {skipped} 个包已存在，无需重复下载"


# ---------------------------------------------------------------------------
# 构建器检测 / 构建
# ---------------------------------------------------------------------------
def detect_builder():
    """检测本机 MSBuild.exe，返回路径或 None。"""
    try:
        bundle = load_ritual_bundle()
        if bundle is None:
            return None
        return bundle.find_msbuild()
    except Exception as e:  # pragma: no cover - 仅在依赖缺失时触发
        logger.warning("MSBuild 检测异常: %s", e)
        return None


def build_project(version: str, optimize: bool = False,
                  callback: Optional[Callable[[str, int], None]] = None) -> tuple:
    """编译 project/XXMI-Libs-Package-<version>，产物落到 artifact/。

    Args:
        version: 项目版本号（目录名后缀）。
        optimize: 「优化构建」开关 —— True 时先向源文件注入随机死代码再编译。
        callback: 进度回调 fn(text, progress)；progress 为 0~100，-1 表示仅文本。

    Returns:
        (success, info)；成功时 info 为 dict（含 path / sha256 等），失败为错误文本。
    """
    root = project_dir_for(version)
    if not os.path.isdir(root):
        return False, f"项目目录不存在: {root}"
    if not os.path.isfile(os.path.join(root, SOLUTION_NAME)):
        return False, f"解决方案缺失: {os.path.join(root, SOLUTION_NAME)}"

    from omg.domain.ritual import d3d11_builder

    ensure_dirs()
    return d3d11_builder.run_build(
        output_dir=ARTIFACT_DIR,
        callback=callback,
        project_root=root,
        inject=bool(optimize),
    )


def run_env_check(version: str = "", optimize: bool = False):
    """跑一次「构建环境检测」，返回 :class:`omg.core.build_env.EnvReport`。

    与 :func:`detect_builder` 的区别：后者只回答「MSBuild 在不在」，而构建能否
    成功还取决于工具集 / SDK / 源码工程 / 静态库 / 后处理目录等，这里一次性全查，
    并把结果结构化（每项含状态 + 结论 + 建议），供 UI 直接渲染成清单。

    Args:
        version: 项目版本号（目录名后缀）；为空表示源码尚未下载。
        optimize: 「优化构建」开关，决定是否检查注入基线。

    Returns:
        :class:`~omg.core.build_env.EnvReport`；检测过程本身出错时其 ``error``
        非空。该函数**不抛异常**（异常已被 run_report 兜住）。
    """
    from omg.core import build_env

    root = project_dir_for(version) if version else ""
    return build_env.run_report(root, optimize=optimize)


# ---------------------------------------------------------------------------
# 仪式（文件优化）
# ---------------------------------------------------------------------------
def _sub_progress(progress_cb, start: float, end: float) -> Callable[[float], None]:
    """把子阶段的 0~100 映射到整体 [start, end] 区间。"""
    def _fn(value: float) -> None:
        if progress_cb:
            frac = max(0.0, min(100.0, float(value))) / 100.0
            progress_cb(start + frac * (end - start))
    return _fn


def _paint_py(src: Path, dst: Path, progress_cb=None) -> tuple:
    """涂装(py)：prlib 实现（分析 → 规划 → 打补丁 → 校验）。

    符号全部来自 ritual_bundle.pyd（单一编译产物），公开仓库不含 ritual 源码。
    """
    bundle = load_ritual_bundle()
    if bundle is None:
        return False, "仪式模块不可用（缺少 ritual_bundle.pyd）"
    analyze_internal = bundle.analyze_internal
    apply_patches = bundle.apply_patches
    plan_patches = bundle.plan_patches
    TransformOptions = bundle.TransformOptions
    validate_transformation = bundle.validate_transformation

    options = TransformOptions(
        seed=random.getrandbits(63),
        allow_invalid_signature=True,   # 自编译 dll 若带签名，允许失效而不报错
    )

    data = src.read_bytes()
    if progress_cb:
        progress_cb(10.0)

    _pe, _report, approved = analyze_internal(data, options)
    if progress_cb:
        progress_cb(45.0)

    patches = plan_patches(approved, data, options)
    if progress_cb:
        progress_cb(65.0)

    output = apply_patches(data, patches)
    validate_transformation(data, output, _pe, patches)
    if progress_cb:
        progress_cb(90.0)

    dst.write_bytes(output)
    return True, f"涂装(py) 完成，改写 {len(patches)} 处填充"


def run_ritual(kind: str, src_path: str,
               progress_cb: Optional[Callable[[float], None]] = None,
               prepare: Optional[Callable[..., tuple]] = None) -> tuple:
    """对 src_path 执行仪式，产物写入 output/d3d11.dll。

    Args:
        kind: RITUAL_PACK / RITUAL_PAINT_PY / RITUAL_PAINT_RS。
        src_path: 目标 dll（GitHub 源码 → artifact/d3d11.dll；
                  已构建文件默认场景不需要传入，由 prepare 在运行时决定）。
        progress_cb: 进度回调 fn(percent 0~100)。
        prepare: 可选的「前置准备」回调 ``fn(sub_progress) -> (ok, msg, path)``。
                 源 = 已构建文件时传入 :func:`pick_random_prebuilt_dll`：直接在
                 cloud/ 最新批次里随机抽一个 d3d11.dll 并就地当作仪式输入（**不复制
                 到 input**），产物写到 output/；失败则整个仪式中止并返回其错误信息。
                 :func:`stage_input_dll` 也能作为 prepare，区别是它会先把抽中的 dll
                 落位到 input/d3d11.dll（显式选择 input 路径时 UI 不传 prepare，直接跑）。

    Returns:
        (success, message)
    """
    # 整段纳入 try：路径校验 / ensure_dirs / 仪式执行都可能抛异常。
    # 任何异常都转成 (False, msg) 返回，绝不让异常逃到调用方（子线程里
    # 未捕获的异常会让整个进程 abort —— 表现为「点击即闪退」）。
    try:
        bundle = load_ritual_bundle()
        if bundle is None:
            return False, "仪式模块不可用（缺少 ritual_bundle.pyd）"
        if prepare is not None:
            ok, msg, picked = prepare(_sub_progress(progress_cb, 0.0, 10.0))
            if not ok:
                return False, msg
            if picked:
                src_path = picked

        if not isinstance(src_path, (str, bytes, os.PathLike)):
            return False, f"目标路径无效: {src_path!r}"
        src = Path(src_path)
        if not src.is_file():
            return False, f"目标文件不存在: {src}"

        ensure_dirs()
        dst = Path(OUTPUT_DLL)
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass

        # 有前置准备时，前 10% 归准备阶段，仪式本身从 10% 起算
        base = 10.0 if prepare is not None else 0.0
        if kind == RITUAL_PACK:
            bundle.run_pack(
                progress_callback=_sub_progress(progress_cb, base, 100.0),
                src=src, out_dir=Path(OUTPUT_DIR), upx_exe=Path(UPX_EXE),
            )
            msg = "包装完成"
        elif kind == RITUAL_PAINT_RS:
            bundle.run_paint(
                seed=random.getrandbits(63),
                progress_callback=_sub_progress(progress_cb, base, 100.0),
                src=src, out_dir=Path(OUTPUT_DIR),
                diversifier_dll=Path(DIVERSIFIER_DLL),
            )
            msg = "涂装(rs) 完成"
        elif kind == RITUAL_PAINT_PY:
            ok, msg = _paint_py(src, dst, _sub_progress(progress_cb, base, 100.0))
            if not ok:
                return False, msg
        else:
            return False, f"未知仪式类型: {kind}"
    except Exception as e:
        logger.error("仪式执行失败 (%s): %s", kind, e, exc_info=True)
        return False, f"{e}"

    if not dst.is_file():
        return False, f"未生成产物: {dst}"
    if progress_cb:
        progress_cb(100.0)
    return True, f"{msg} → {dst}"


# ---------------------------------------------------------------------------
# 复制到 GIMI
# ---------------------------------------------------------------------------
def copy_to_gimi(source: str, gimi_dir: str) -> tuple:
    """把 artifact / output 的 d3d11.dll 复制（覆盖）到 GIMI 目录根。

    Args:
        source: COPY_ARTIFACT 或 COPY_OUTPUT。
        gimi_dir: 用户配置的 GIMI 目录。

    Returns:
        (success, message)
    """
    src = ARTIFACT_DLL if source == COPY_ARTIFACT else OUTPUT_DLL
    if not gimi_dir or not os.path.isdir(gimi_dir):
        return False, "GIMI 目录无效（请先在设置中配置）"
    if not os.path.isfile(src):
        return False, f"产物不存在: {src}"
    dst = os.path.join(gimi_dir, "d3d11.dll")
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        logger.error("复制 d3d11.dll 到 GIMI 失败: %s", e, exc_info=True)
        return False, f"复制失败: {e}"
    if not os.path.isfile(dst):
        return False, "复制后未找到目标文件"
    return True, f"已复制 → {dst}"


# ---------------------------------------------------------------------------
# 异步线程封装
# ---------------------------------------------------------------------------
class TaskSignals(QObject):
    """通用任务信号集（进度 / 状态 / 结果）。"""

    progress = Signal(float)          # 0 ~ 100
    status = Signal(str)              # 简短状态文本
    finished = Signal(bool, str)      # (success, message)


class _CancelThread(QThread):
    """带取消事件的线程基类。

    **异常兜底（关键）**：PySide6 中 ``QThread.run()`` 里未捕获的异常会直接
    abort 整个进程 —— 表现为「点一下就闪退」，没有 traceback、没有错误提示，
    极难排查。因此基类统一接管 ``run()``，子类改为实现 :meth:`_run_impl`，
    任何异常都会在这里被兜住，转成 ``finished(False, 错误信息)``，让 UI 正常
    恢复按钮并提示用户，而不是让进程崩溃。

    信号发送同样做了保护：即使参数类型不对（如 msg 为 None）也不会让异常逃出
    ``run()``。
    """

    def __init__(self, signals: TaskSignals, parent=None):
        super().__init__(parent)
        self._sig = signals
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ---- 子类实现 _run_impl，不要覆盖 run ----
    def run(self):
        try:
            self._run_impl()
        except Exception as e:
            logger.error("任务线程异常终止: %s", e, exc_info=True)
            self._emit_finished(False, f"执行出错: {e}")

    def _run_impl(self):
        raise NotImplementedError

    # ---- 安全信号发送 ----
    def _emit_finished(self, ok: bool, msg) -> None:
        """发送 finished，吞掉一切发送期异常（避免异常逃到 run() 外）。"""
        try:
            self._sig.finished.emit(bool(ok), str(msg))
        except Exception as e:
            logger.error("finished 信号发送失败: %s", e, exc_info=True)

    def _emit_progress(self, value) -> None:
        try:
            self._sig.progress.emit(float(value))
        except Exception:
            pass

    def _emit_status(self, text) -> None:
        try:
            self._sig.status.emit(str(text))
        except Exception:
            pass


class FetchReleasesThread(_CancelThread):
    """拉取 release 版本号列表。"""

    result = Signal(object)           # list[dict]（失败为空列表）

    def __init__(self, signals: TaskSignals, limit: int = RELEASE_LIMIT, parent=None):
        super().__init__(signals, parent)
        self._limit = limit

    def _run_impl(self):
        try:
            entries = fetch_releases(
                limit=self._limit,
                status_cb=lambda text: self._emit_status(text),
            )
        except Exception as e:
            logger.error("拉取源码版本失败: %s", e, exc_info=True)
            entries = []
        if self.is_cancelled():
            entries = []
        self.result.emit(entries)


class DownloadProjectThread(_CancelThread):
    """下载所选版本源码 zip 并解压到 project/。"""

    def __init__(self, signals: TaskSignals, release: dict, parent=None):
        super().__init__(signals, parent)
        self._release = release

    def _run_impl(self):
        dl = DownloadSignals()
        dl.progress.connect(
            lambda p: self._emit_progress(
                max(0.0, min(_DL_END, float(p) * (_DL_END / 100.0)))
            )
        )
        dl.status.connect(lambda m, _r: self._emit_status(m))

        self._emit_progress(1.0)
        ok, msg = download_and_extract(
            self._release, dl,
            should_cancel=self.is_cancelled,
            progress_cb=lambda p: self._emit_progress(p),
        )
        if ok:
            self._emit_progress(100.0)
        self._emit_finished(ok, msg)


class DownloadPrebuiltThread(_CancelThread):
    """下载蓝奏云「已构建文件」并解压到 cloud/（可中断）。"""

    def __init__(self, signals: TaskSignals, url: str = "", pwd: str = "",
                 parent=None):
        super().__init__(signals, parent)
        self._url = url
        self._pwd = pwd

    def _run_impl(self):
        self._emit_progress(1.0)
        ok, msg = fetch_prebuilt(
            progress_cb=lambda p: self._emit_progress(p),
            status_cb=lambda t: self._emit_status(t),
            should_cancel=self.is_cancelled,
            url=self._url, pwd=self._pwd,
        )
        if ok:
            self._emit_progress(100.0)
        self._emit_finished(ok, msg)


class EnvCheckThread(_CancelThread):
    """后台跑一次构建环境检测（vswhere 子进程 ~100ms，不宜放在 UI 线程）。

    结果通过 :attr:`report` 发出（:class:`~omg.core.build_env.EnvReport`）。
    检测是纯只读操作，可随时取消；取消时不再发 report。
    """

    report = Signal(object)           # build_env.EnvReport

    def __init__(self, signals: TaskSignals, version: str = "",
                 optimize: bool = False, parent=None):
        super().__init__(signals, parent)
        self._version = version
        self._optimize = optimize

    def _run_impl(self):
        self._emit_status("正在检测构建环境…")
        report = run_env_check(self._version, self._optimize)
        if self.is_cancelled():
            return
        self.report.emit(report)


class BuildThread(_CancelThread):
    """编译选定项目，产物落入 artifact/。

    注：MSBuild 一旦启动不便中途安全终止，故本线程不支持取消（UI 侧进度圈不显示
    停止按钮），仅在执行前检查一次取消状态。
    """

    def __init__(self, signals: TaskSignals, version: str,
                 optimize: bool = False, parent=None):
        super().__init__(signals, parent)
        self._version = version
        self._optimize = optimize

    def _run_impl(self):
        if self.is_cancelled():
            self._emit_finished(False, "已取消")
            return

        def _cb(text, progress) -> None:
            if progress is not None and progress >= 0:
                self._emit_progress(progress)
            if text:
                self._emit_status(text)

        self._emit_progress(1.0)
        ok, info = build_project(self._version, self._optimize, callback=_cb)
        if ok:
            self._emit_progress(100.0)
            # info 可能是 dict 也可能是字符串，统一取路径 / 原文
            path = info.get("path", ARTIFACT_DLL) if isinstance(info, dict) else info
            self._emit_finished(True, path)
        else:
            self._emit_finished(False, info)


class RitualThread(_CancelThread):
    """对目标 dll 执行仪式，产物落入 output/。

    ``prepare`` 非 None 时（源 = 已构建文件），仪式开始前会先调用它随机抽一个
    cloud/ 里的 dll 落位到 input/d3d11.dll，再以该文件为输入。
    """

    def __init__(self, signals: TaskSignals, kind: str, src_path: str,
                 prepare: Optional[Callable[..., tuple]] = None, parent=None):
        super().__init__(signals, parent)
        self._kind = kind
        self._src = src_path
        self._prepare = prepare

    def _run_impl(self):
        self._emit_progress(1.0)
        ok, msg = run_ritual(
            self._kind, self._src,
            progress_cb=lambda p: self._emit_progress(p),
            prepare=self._prepare,
        )
        if ok:
            self._emit_progress(100.0)
        self._emit_finished(ok, msg)


class CopyGimiThread(_CancelThread):
    """把 artifact / output 的 d3d11.dll 复制到 GIMI 目录。"""

    def __init__(self, signals: TaskSignals, source: str, gimi_dir: str, parent=None):
        super().__init__(signals, parent)
        self._source = source
        self._gimi_dir = gimi_dir

    def _run_impl(self):
        self._emit_progress(20.0)
        ok, msg = copy_to_gimi(self._source, self._gimi_dir)
        if ok:
            self._emit_progress(100.0)
        self._emit_finished(ok, msg)
