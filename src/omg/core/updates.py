"""
omg.core.updates — 运行环境探测、单实例互斥、媒体后端选择（从 OMGDev/main.py 抽取）。

本模块聚合原 ``main()`` 中与「启动装配 / 运行环境」相关的无 UI 逻辑：
- ``ensure_single_instance`` / ``release_instance_mutex``：命名互斥体单实例（打包 exe 后同样有效）。
- ``select_media_backend`` / ``configure_qt_media_backend``：按背景视频编码格式选择
  Qt Multimedia 后端（ffmpeg 软解 vs MediaFoundation 硬解），必须在 QApplication 创建前完成。
- ``setup_qt_plugin_path``：为 Nuitka / PyInstaller 冻结环境注册 Qt 插件路径。
- ``fix_taskbar_icon``：设置 AppUserModelID 修复 Windows 任务栏图标。

PySide6 / QtNetwork 仅在函数内部惰性导入，故本模块可在 headless 环境安全导入。
"""

import ctypes
import logging
import os
import shutil
import sys
import tempfile
import zipfile

from omg.core.logging_setup import logger
from omg.core.paths import (
    BIN_DIR,
    IS_NUITKA,
    IS_PYINSTALLER,
    _find_bg_file,
)

# 单实例互斥体名称（与激活信号服务名配套）
SINGLE_INSTANCE_MUTEX = "OMG_SINGLE_INSTANCE_MUTEX"
ACTIVATE_SERVER_NAME = "OMG_ACTIVATE_SERVER"

# 媒体后端扩展名分类
_H264_EXTS = {"mp4", "m4v", "mov", "avi", "wmv", "ts", "m2ts", "mts", "3gp", "3g2", "m4a"}
_VPX_EXTS = {"webm", "mkv", "av1", "ivf", "opus", "ogg", "oga"}
_IMG_EXTS = {"jpg", "jpeg", "png", "bmp", "webp", "gif", "heic", "tif", "tiff"}

# 模块级持有互斥体句柄，供 release_instance_mutex() 在退出时释放
_INSTANCE_MUTEX = None


# ---------------------------------------------------------------------------
# 单实例互斥
# ---------------------------------------------------------------------------
def ensure_single_instance(mutex_name: str = SINGLE_INSTANCE_MUTEX,
                          activate_server: str = ACTIVATE_SERVER_NAME) -> bool:
    """确保本进程是唯一的 OMG 实例。

    通过 Windows 命名互斥体实现；若已存在实例，则向该实例的本地 socket
    服务器发送 ``activate`` 激活信号，并返回 ``False``（调用方应退出）。

    Args:
        mutex_name: 命名互斥体名称。
        activate_server: 既有实例监听的 QLocalServer 名称（用于发激活信号）。

    Returns:
        ``True`` 表示本进程是主实例、可继续启动；``False`` 表示已有实例在运行。

    非 Windows 平台直接返回 ``True``（无互斥体机制）。

    检测到已有实例时，会先用 ``omg.core.instance_ipc.broadcast_activate()``
    广播激活消息（纯 Win32，不再为了发一条消息而临时 new 一个
    ``QCoreApplication`` + ``QLocalSocket``）。已有实例侧的接收器是
    ``omg.ui.instance_activator.InstanceActivator``，收到后显示面板。
    """
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            # 已有实例运行中 → 广播激活消息后退出
            try:
                from omg.core.instance_ipc import broadcast_activate
                broadcast_activate()
                logger.info("检测到已有实例运行，已发送激活信号后退出")
            except Exception as _e:
                logger.warning("向既有实例发送激活信号失败: %s", _e)
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception as _e:
        logger.warning("单实例互斥体创建失败，放行（%s）", _e)
        return True


def release_instance_mutex() -> None:
    """释放 ensure_single_instance 创建的互斥体句柄（进程退出前调用）。"""
    global _INSTANCE_MUTEX
    if _INSTANCE_MUTEX is not None and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
        except Exception:
            pass
        _INSTANCE_MUTEX = None


# ---------------------------------------------------------------------------
# 媒体后端选择（必须在 QApplication 创建前设置）
# ---------------------------------------------------------------------------
def select_media_backend(bg_file: str = "") -> str:
    """根据背景视频扩展名选择 Qt Multimedia 后端。

    策略：
    - .mp4/.m4v/.mov/.avi/.wmv/.ts/.m2ts 等 H.264 → ``"windows"``（MediaFoundation DXVA2 硬解）
    - .webm/.mkv/.av1 等 VP9/AV1 → ``"ffmpeg"``（MF 不支持，FFmpeg 软解保证可播）
    - 图片 / 空 / 未知 → ``"ffmpeg"``（最兼容）

    Args:
        bg_file: 背景媒体文件完整路径或文件名。

    Returns:
        ``"ffmpeg"`` 或 ``"windows"``。
    """
    ext = os.path.splitext(bg_file)[1].lower().lstrip(".")
    if ext in _H264_EXTS:
        return "windows"
    if ext in _IMG_EXTS:
        return "ffmpeg"
    if ext in _VPX_EXTS:
        return "ffmpeg"
    # 空 / 未知 / 其他 → 默认 ffmpeg 最兼容
    return "ffmpeg"


def configure_qt_media_backend(pre_cfg: dict | None = None,
                              bg_file: str = "") -> str:
    """计算并应用 Qt 媒体后端环境变量，返回所选后端。

    顺序（与 OMGDev/main.py 一致）：
    1. 优先使用参数 ``bg_file``；
    2. 否则从 ``pre_cfg["bg_file"]`` 读取；
    3. 否则从 ``pre_cfg["bg"]["file"]`` 读取；
    4. 否则用 ``_find_bg_file()`` 回退探测。

    VP9/AV1 软解时额外设置 ``QT_FFMPEG_THREAD_COUNT=2`` 降低 CPU 占用；
    最终用 ``os.environ.setdefault`` 设置 ``QT_MEDIA_BACKEND``（保留用户外部显式设置）。

    Args:
        pre_cfg: 预读取的 config.json 字典（可选）。
        bg_file: 明确指定的背景文件（优先于 pre_cfg）。

    Returns:
        所选后端字符串（``"ffmpeg"`` / ``"windows"``）。
    """
    pre_cfg = pre_cfg or {}
    if not bg_file:
        bg_file = pre_cfg.get("bg_file") or ""
    if not bg_file:
        bg_file = (pre_cfg.get("bg") or {}).get("file") or ""
    if not bg_file:
        fb_path, _is_vid = _find_bg_file()
        if _is_vid and fb_path:
            bg_file = fb_path

    ext = os.path.splitext(bg_file)[1].lower().lstrip(".")
    backend = select_media_backend(bg_file)

    if ext in _VPX_EXTS:
        # FFmpeg VP9 软解默认线程过多反而拉高总 CPU%；限制为 2 线程
        os.environ.setdefault("QT_FFMPEG_THREAD_COUNT", "2")
        logger.info("背景视频格式=%s (VP9/AV1)，MF 不支持 → 使用 FFmpeg 软解后端（线程=2）", ext)
    elif ext in _H264_EXTS:
        logger.info("背景视频格式=%s，启用 MediaFoundation DXVA2 GPU 硬解后端", ext)
    elif ext == "":
        logger.info("未设置背景视频 → 使用 FFmpeg 默认后端")
    else:
        logger.info("未知背景视频格式=%s → 使用 FFmpeg 兼容后端", ext)

    os.environ.setdefault("QT_MEDIA_BACKEND", backend)
    logger.info("QT_MEDIA_BACKEND=%s", os.environ.get("QT_MEDIA_BACKEND"))
    return backend


# ---------------------------------------------------------------------------
# 运行环境探测辅助
# ---------------------------------------------------------------------------
def setup_qt_plugin_path() -> None:
    """为 Nuitka / PyInstaller 冻结环境注册 Qt 插件路径。

    Nuitka 打包的插件目录为 ``BIN_DIR/PySide6/qt-plugins``，PyInstaller 为
    ``BIN_DIR/PySide6/plugins``；存在时写入 ``QT_PLUGIN_PATH``，否则不动。
    """
    if IS_NUITKA:
        qt_plugins = os.path.join(BIN_DIR, "PySide6", "qt-plugins")
    elif IS_PYINSTALLER:
        qt_plugins = os.path.join(BIN_DIR, "PySide6", "plugins")
    else:
        return
    if os.path.isdir(qt_plugins):
        os.environ["QT_PLUGIN_PATH"] = qt_plugins
        logger.info("已注册 Qt 插件路径: %s", qt_plugins)


def fix_taskbar_icon(app_id: str = "OMG.App") -> None:
    """设置 AppUserModelID，让 Windows 任务栏正确显示 exe 图标而非默认 Python 图标。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as _e:
        logger.debug("设置 AppUserModelID 失败（可忽略）: %s", _e)


# ---------------------------------------------------------------------------
# 更新解压（白名单跳过；从 OMGDev/main.py 抽取）
# ---------------------------------------------------------------------------
class ExtractCancelled(Exception):
    """解压过程中被用户取消（由 ``should_cancel`` 触发）。"""
    pass


def safe_extract_zip(zf, dest_dir: str, skip_files=None, should_cancel=None,
                    progress_cb=None) -> None:
    """解压 zip，带 Zip Slip 防护，并支持跳过白名单文件与取消。

    Args:
        zf: 已打开的 ``zipfile.ZipFile`` 实例。
        dest_dir: 目标目录。
        skip_files: 可选的相对路径集合（正斜杠），解压时跳过这些成员
                    （大小写不敏感、路径已规范化后匹配）。对应「更新白名单」：
                    这些文件在更新解压时不被覆盖，保留用户自定义内容。
        should_cancel: 可选无参可调用对象；返回 True 时立即抛出
                       :class:`ExtractCancelled`（用于用户中断更新解压）。
        progress_cb: 可选可调用 ``fraction(0~1)``；每解压完一个成员回调一次，
                     供 UI 细分「解压」阶段进度，避免进度圈在解压时静止后突然跳满。

    从 OMGDev/main.py._safe_extract_zip 原样移植，并增加取消钩子与进度回调。
    """
    dest_real = os.path.realpath(dest_dir)
    skip_set = set()
    if skip_files:
        for s in skip_files:
            if not s:
                continue
            # 规范化：反斜杠 → 正斜杠、去除前导斜杠、折叠重复分隔符、小写以便不区分大小写匹配
            norm = os.path.normpath(s).replace("\\", "/").lstrip("/")
            skip_set.add(norm.lower())

    members = zf.namelist()
    total = len(members) or 1
    for i, member in enumerate(members):
        if should_cancel is not None and should_cancel():
            raise ExtractCancelled(member)
        target = os.path.realpath(os.path.join(dest_dir, member))
        if not target.startswith(dest_real + os.sep) and target != dest_real:
            raise ValueError(f"Zip Slip: 非法路径 {member!r}")
        # 跳过白名单文件（大小写不敏感、路径规范化）
        if member.lower() in skip_set:
            logger.info("白名单跳过: %s", member)
        else:
            zf.extract(member, dest_dir)
        if progress_cb is not None:
            progress_cb((i + 1) / total)


def _norm_member(member: str) -> str:
    """把 zip 成员名规范为相对 deploy_dir 的正斜杠路径（与 safe_extract_zip 一致）。"""
    return os.path.normpath(member).replace("\\", "/").lstrip("/")


def _safe_remove(path: str) -> None:
    """尽力删除文件，忽略失败。"""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _prepare_rollback(zip_path: str, deploy_dir: str, skip_files) -> str | None:
    """解压前备份将被覆盖的「已存在」文件，返回备份目录；无需要备份则返回 None。

    仅备份会被本次解压覆盖（不在白名单内）且当前已存在于 deploy_dir 的文件；
    备份目录内写入 ``_members.txt`` 清单（zip 全部成员相对路径），供回退时
    删除「新增文件」（解压前不存在、被部分解压创建的文件）。
    """
    skip_set = set()
    if skip_files:
        for s in skip_files:
            if not s:
                continue
            skip_set.add(os.path.normpath(s).replace("\\", "/").lstrip("/").lower())
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            backup_dir = tempfile.mkdtemp(prefix="omg_gimi_rollback_")
            backed_any = False
            for member in members:
                rel = _norm_member(member)
                if not rel or rel.lower() in skip_set:
                    continue
                src = os.path.join(deploy_dir, member)
                if not os.path.isfile(src):
                    continue  # 仅备份已存在文件（新增文件无需回退）
                dst = os.path.join(backup_dir, member)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                backed_any = True
            with open(os.path.join(backup_dir, "_members.txt"), "w", encoding="utf-8") as mf:
                mf.write("\n".join(members))
            # 记录白名单成员，回退时绝不删除（它们本就不该被覆盖）
            with open(os.path.join(backup_dir, "_skip.txt"), "w", encoding="utf-8") as sf:
                sf.write("\n".join(sorted(skip_set)))
            return backup_dir if backed_any else None
    except Exception as e:  # 备份失败不阻断更新，仅丢失回退能力
        logger.warning("准备回退备份失败（将跳过回退）: %s", e)
        return None


def _rollback(backup_dir: str | None, deploy_dir: str, zip_path: str) -> None:
    """回退到更新前状态：恢复备份文件、删除「新增文件」、删除临时 zip 与备份目录。"""
    try:
        members: set = set()
        skip_members: set = set()
        if backup_dir and os.path.isdir(backup_dir):
            mf = os.path.join(backup_dir, "_members.txt")
            if os.path.isfile(mf):
                with open(mf, "r", encoding="utf-8") as f:
                    members = {_norm_member(m) for m in f.read().splitlines() if m.strip()}
            sf = os.path.join(backup_dir, "_skip.txt")
            if os.path.isfile(sf):
                with open(sf, "r", encoding="utf-8") as f:
                    skip_members = {m.strip().lower() for m in f.read().splitlines() if m.strip()}
            # 1) 恢复备份的已存在文件
            for root, _dirs, files in os.walk(backup_dir):
                files = [x for x in files if x not in ("_members.txt", "_skip.txt")]
                for fn in files:
                    bk = os.path.join(root, fn)
                    rel = os.path.relpath(bk, backup_dir)
                    tgt = os.path.join(deploy_dir, rel)
                    os.makedirs(os.path.dirname(tgt), exist_ok=True)
                    shutil.copy2(bk, tgt)
            # 2) 删除解压「新增」的文件（非白名单 zip 成员、更新前不存在、被部分解压创建）
            for member in members:
                if member.lower() in skip_members:
                    continue  # 白名单文件：从不删除
                tgt = os.path.join(deploy_dir, member)
                if os.path.isfile(tgt) and not os.path.isfile(os.path.join(backup_dir, member)):
                    try:
                        os.remove(tgt)
                    except OSError:
                        pass
        # 3) 删除临时 zip 与备份目录
        _safe_remove(zip_path)
    finally:
        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)


def build_gimi_update_skip_files(deploy_dir: str, whitelist) -> list:
    """把 config 中存储的白名单转换为相对 deploy_dir 的跳过列表。

    白名单条目为**相对 GIMI 目录**的规范路径（正斜杠）；本函数兼容仍
    以绝对路径保存的历史数据：若是绝对路径则换算为相对路径。统一过滤掉
    逃逸 deploy_dir 的条目（归一化后不以 ``..`` 开头）。返回相对路径列表，
    供 :func:`safe_extract_zip` 做大小写不敏感匹配。

    从 OMGDev/main.py download_and_deploy_gimi 的白名单转换逻辑移植并改造。
    """
    skip = []
    if not isinstance(whitelist, list) or not whitelist:
        return skip
    deploy_real = os.path.realpath(deploy_dir)
    for entry in whitelist:
        if not entry:
            continue
        # 兼容历史绝对路径：统一换算为相对 deploy_dir 的规范路径
        if os.path.isabs(entry):
            try:
                rel = os.path.relpath(
                    os.path.normpath(os.path.abspath(entry)), deploy_real
                )
            except (ValueError, OSError):
                continue
        else:
            rel = os.path.normpath(entry)
        # 仅保留 deploy_dir 内部的路径（已逃逸则跳过）
        if rel.startswith(".."):
            continue
        skip.append(rel.replace("\\", "/"))
    return skip


def deploy_gimi_update(zip_path: str, deploy_dir: str, whitelist=None,
                       should_cancel=None, progress_cb=None) -> tuple:
    """解压 GIMI 更新包到 deploy_dir，并跳过白名单中的文件（不覆盖）。

    支持「用户取消」：解压前先备份将被覆盖的已存在文件，若解压过程中
    ``should_cancel`` 返回 True（或解压失败），则回退到更新前状态并删除临时
    zip 与备份目录；成功则清理临时文件。

    Args:
        zip_path: 已下载的更新包 zip 路径。
        deploy_dir: 部署目录（即 config 的 gimi_folder）。
        whitelist: 可选的白名单列表（相对 GIMI 目录的路径）；传 ``cfg.get('gimi_update_whitelist')``。
        should_cancel: 可选无参可调用；返回 True 时中断解压并触发回退。
        progress_cb: 可选可调用 ``fraction(0~1)``；透传给 safe_extract_zip，
                     供 UI 细分「解压」阶段进度。

    Returns:
        ``(success, message)``：
        - 成功 → ``(True, "ok")``
        - 用户取消（解压前）→ ``(False, "已取消")``；解压中取消 → ``(False, "已取消，已回退")``
        - 失败 → ``(False, "解压失败: ...")``（同样尝试回退）
    """
    os.makedirs(deploy_dir, exist_ok=True)
    skip = build_gimi_update_skip_files(deploy_dir, whitelist) or None
    backup_dir = _prepare_rollback(zip_path, deploy_dir, skip)
    cancelled = False
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # 备份已完成，进入解压阶段：向 UI 上报解压起点（fraction=0）
            if progress_cb is not None:
                progress_cb(0.0)
            safe_extract_zip(
                zf, deploy_dir, skip_files=skip,
                should_cancel=should_cancel, progress_cb=progress_cb,
            )
    except ExtractCancelled:
        cancelled = True
    except zipfile.BadZipFile as e:
        _rollback(backup_dir, deploy_dir, zip_path)
        return False, f"解压失败: {e}"
    except Exception as e:  # 其它解压异常同样回退
        logger.error("GIMI 解压异常，尝试回退: %s", e, exc_info=True)
        _rollback(backup_dir, deploy_dir, zip_path)
        return False, f"解压失败: {e}"

    if cancelled:
        _rollback(backup_dir, deploy_dir, zip_path)
        return False, "已取消，已回退"

    # 成功：清理临时 zip 与备份目录
    _safe_remove(zip_path)
    if backup_dir and os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir, ignore_errors=True)
    return True, "ok"

