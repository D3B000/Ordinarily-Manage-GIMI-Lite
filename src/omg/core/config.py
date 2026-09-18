"""
omg.core.config — ConfigManager（从 OMGDev/main.py 抽取）。

双机制配置管理器：
- 运行时变量（``_data``）：程序执行期间使用的全部取值，提供快速、稳定的内存访问。
- 持久化配置（``config.json``）：调用 ``save()`` 时只把 ``PERSISTENT_KEYS`` 写入磁盘。

对外 API 与原 ``main.ConfigManager`` **保持一致**，页面层迁移时无需改动调用方式。
"""

import json
import os
import shutil

from omg.core.logging_setup import logger
from omg.core.paths import (
    CONFIG_PATH,
    get_gimi_dir,
    read_gimi_version_from_ini,
    read_orfix_version,
    read_teffx_version,
)

# 持久化到 config.json 的键（用户偏好与设置）
PERSISTENT_KEYS = {
    "setup_complete", "mode", "game_folder", "gimi_folder",
    "theme", "hunting", "warning",
    "inject_method", "launch_method",
    "custom_launch_enabled", "custom_launch_cmd", "custom_launch_inject_mode",
    "extra_libraries_enabled", "extra_libraries",
    "loader_launch_path", "auto_check_update", "bg_file",
    "d3d11_version", "d3d11_ritual",
    "gimi_release_body", "gimi_release_version",
    "gimi_update_whitelist",
    "ritual_after_build",
    # 性能组 - INI优化
    "perf_ini_auto_on_launch",
    # 性能组 - 纹理瘦身
    "perf_tex_mode", "perf_tex_custom_w", "perf_tex_custom_h",
    "perf_tex_percent", "perf_tex_backup",
    "perf_tex_operation", "perf_tex_output_format",
    # UI 性能组 - 列表渲染策略（见 omg.core.list_render）
    "ui_list_simple_render", "ui_list_simple_threshold",
    # 窗口行为组 - 默认置顶 / 闲置透明度 / 淡入淡出时机 / 启动防误触（见 omg.core.window_behavior）
    "omg_always_on_top", "omg_idle_opacity",
    "omg_idle_fade_delay", "omg_idle_fade_out",
    "omg_idle_fade_in", "omg_idle_wake_confirm",
    "omg_launch_hold_enabled", "omg_launch_hold_ms",
    # 资源浏览界面外观组 - 置顶 / 整体透明度（独立于主窗口，见 omg.core.window_behavior）
    "mm_always_on_top", "mm_opacity",
    # 弹出窗口几何记忆（key -> [x, y]），由 omg.ui.window_geo 读写
    "window_geometry",
}

# 仅运行时的键（不写入 config.json），在运行时从 INI 文件探测或操作期间设置
RUNTIME_ONLY_KEYS = {
    "gimi_version", "orfix_version", "teffx_version",
}


class ConfigManager:
    """Dual-mechanism config manager.

    - Runtime variables (``_data``): all values the program uses during execution.
      Provides fast, stable in-memory access.
    - Persistent config (``config.json``): only PERSISTENT_KEYS are written to
      disk when ``save()`` is called explicitly.

    Usage::

        cfg = ConfigManager()          # loads from config.json + detects versions
        cfg.get("theme")               # read runtime value
        cfg.set("theme", "light")      # update runtime value (no disk write)
        cfg.save()                     # persist PERSISTENT_KEYS to config.json
        snap = cfg.snapshot()          # deep copy for cancel-revert
        cfg.restore(snap)              # restore from snapshot
    """

    def __init__(self, config_path: str = CONFIG_PATH, preloaded: dict | None = None):
        self._path = config_path
        self._data: dict = {}
        # Qt 信号持有者：版本重新探测完成时发出 versions_changed（无 Qt 环境为 None）
        self._sig = None
        if preloaded is not None:
            # Optimized fast-path: caller has already read config.json once.
            # Avoid repeated disk I/O during startup.
            self._data = dict(preloaded)
            first_launch = not os.path.exists(self._path)
            self._migrate()
            if first_launch:
                self._init_first_launch()
                self.save()
                logger.info("首次启动：已生成默认 config.json")
            # [P2-2] Async INI scan — offloads 30-150ms from critical start path
            self._detect_versions_async()
        else:
            self._load()

    # ---- internal load / save ----
    def _load(self):
        """Load persistent config from disk, then detect runtime-only values."""
        first_launch = not os.path.exists(self._path)
        if not first_launch:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                # config.json corrupted — try backup
                bak_path = self._path + ".bak"
                if os.path.isfile(bak_path):
                    try:
                        with open(bak_path, "r", encoding="utf-8") as f:
                            self._data = json.load(f)
                        logger.warning("config.json 损坏，已从备份恢复")
                    except Exception:
                        self._data = {}
                        logger.error("config.json 及备份均损坏，使用默认配置")
                else:
                    self._data = {}
                    logger.error("config.json 损坏且无备份，使用默认配置")
        else:
            self._data = {}
            logger.info("首次启动：config.json 不存在，将生成默认配置")
        # Migrate old values
        self._migrate()
        # First launch: initialize defaults and persist to disk
        if first_launch:
            self._init_first_launch()
            self.save()
            logger.info("首次启动：已生成默认 config.json")
        # [P2-2] Async INI scan — offloads 30-150ms from critical start path
        self._detect_versions_async()

    def _migrate(self):
        """Migrate config values to current format. OMGLite: force mode=OWN."""
        # auto_check_update: bool → int (1/0), default 1
        val = self._data.get("auto_check_update")
        if val is None:
            self._data["auto_check_update"] = 1
        elif isinstance(val, bool):
            self._data["auto_check_update"] = 1 if val else 0
        # OMGLite: mode is always OWN
        self._data["mode"] = "OWN"
        # setup_complete: skip guide in OMGLite
        self._data["setup_complete"] = True

    def _init_first_launch(self):
        """首次启动初始化：暗色主题、默认背景、3dmloader.dll 注入方式。"""
        # 主题：暗色
        self._data.setdefault("theme", "dark")
        # 背景：空字符串 = 默认渐变背景（不使用图片或视频）
        self._data.setdefault("bg_file", "")
        # 注入方式：hook = 调用 3dmloader.dll 直接导入
        self._data.setdefault("inject_method", "hook")
        # 启动方式：native
        self._data.setdefault("launch_method", "native")

    def _detect_versions(self):
        """Detect GIMI/ORFix/TexFx versions from INI files (runtime-only, synchronous)."""
        gimi_dir = get_gimi_dir(self._data)
        if os.path.isdir(gimi_dir):
            self._data["gimi_version"] = read_gimi_version_from_ini(gimi_dir)
            self._data["orfix_version"] = read_orfix_version(gimi_dir)
            self._data["teffx_version"] = read_teffx_version(gimi_dir)
        else:
            self._data["gimi_version"] = ""
            self._data["orfix_version"] = ""
            self._data["teffx_version"] = ""

    # ---- [P2-2] Async version detection ----
    def _detect_versions_async(self):
        """[P2-2] Detect versions off the UI thread (avoids 30-150ms startup block).

        Strategy:
        1. Immediately set empty values so callers never see stale/undefined keys.
        2. Dispatch work to QThreadPool.globalInstance() → runs on a worker thread.
        3. Once done, write results back into self._data, and if a QApplication
           instance exists, post a lightweight UI refresh (refresh only the 3 tag
           labels, NOT the whole home page) via QTimer.singleShot(0, ...).

        This is fully backward compatible: callers reading get("gimi_version")
        will simply see "" for ~20-80ms until the worker completes.
        """
        gimi_dir = get_gimi_dir(self._data)
        # (1) Immediate defaults — same shape as synchronous path
        self._data["gimi_version"] = ""
        self._data["orfix_version"] = ""
        self._data["teffx_version"] = ""
        if not gimi_dir or not os.path.isdir(gimi_dir):
            return  # nothing to scan; UI stays on "未知"

        # NOTE: QThreadPool / QRunnable are only safe to use after the first
        #       import of PySide6.QtCore. The import is top-level in this file.
        try:
            from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
        except Exception:
            # Environment without Qt (e.g. unit test cli) → fall back to sync
            self._detect_versions()
            return

        cfg_self = self

        class _VersionSignals(QObject):
            done = Signal(str, str, str)  # gimi_v, orfix_v, teffx_v

        class _VersionWorker(QRunnable):
            def __init__(self, gd, signals):
                super().__init__()
                self._gd = gd
                self._sig = signals

            def run(self):
                # Entirely CPU-bound INI scanning; runs off the UI thread.
                gv = read_gimi_version_from_ini(self._gd)
                ov = read_orfix_version(self._gd)
                tv = read_teffx_version(self._gd)
                self._sig.done.emit(gv, ov, tv)

        signals = _VersionSignals()
        # Keep a strong reference on the instance so nothing gets GC'd mid-flight.
        self._version_signals_ref = signals  # noqa: attribute defined outside __init__
        self._version_worker_ref = _VersionWorker(gimi_dir, signals)  # noqa

        @Slot(str, str, str)
        def _on_done(gv, ov, tv):
            cfg_self._data["gimi_version"] = gv
            cfg_self._data["orfix_version"] = ov
            cfg_self._data["teffx_version"] = tv
            # 通知订阅者（如 HomeWindow 的 InfoZone）：版本已重新探测完成。
            # 信号在子线程 emit，Qt 会自动以 QueuedConnection 派发到主线程槽，
            # 无需手动 QTimer.singleShot。无 Qt 环境（headless）下信号为空，跳过。
            sig = cfg_self._ensure_signals()
            if sig is not None:
                sig.versions_changed.emit()

        signals.done.connect(_on_done)
        QThreadPool.globalInstance().start(self._version_worker_ref)

    def save(self):
        """Persist only PERSISTENT_KEYS to config.json (atomic write)."""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        persistent = {k: v for k, v in self._data.items() if k in PERSISTENT_KEYS}
        # Write to temp file first, then atomic replace to prevent corruption
        tmp_path = self._path + ".tmp"
        bak_path = self._path + ".bak"
        try:
            # Create backup of current config before overwriting
            if os.path.isfile(self._path):
                try:
                    shutil.copy2(self._path, bak_path)
                except Exception:
                    pass  # backup failure is non-fatal
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(persistent, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._path)
        except Exception:
            # If atomic replace fails, try direct write as fallback
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(persistent, f, indent=2, ensure_ascii=False)
            except Exception:
                logger.error("配置保存失败", exc_info=True)
                return
        logger.debug(f"配置已保存 ({len(persistent)} 项)")

    # ---- public API ----
    def get(self, key: str, default=None):
        """Get a runtime value."""
        return self._data.get(key, default)

    def set(self, key: str, value):
        """Set a runtime value (no disk write)."""
        self._data[key] = value

    def update(self, data: dict):
        """Batch update runtime values."""
        self._data.update(data)

    def contains(self, key: str) -> bool:
        return key in self._data

    def snapshot(self) -> dict:
        """Create a deep copy of all runtime data (for cancel-revert)."""
        return json.loads(json.dumps(self._data))

    def restore(self, snap: dict):
        """Restore all runtime data from a snapshot."""
        self._data = json.loads(json.dumps(snap))

    def to_dict(self) -> dict:
        """Return the raw runtime dict (callers may mutate in-place)."""
        return self._data

    # ---- convenience ----
    def get_gimi_dir(self) -> str:
        """OMGLite: Always OWN mode — returns config_data['gimi_folder']."""
        return self._data.get("gimi_folder", "") or ""

    def is_first_launch(self) -> bool:
        """OMGLite: No guide page — never considered first launch."""
        return False

    def refresh_versions(self):
        """Re-detect versions from INI files (e.g. after download/refresh)."""
        self._detect_versions()

    # ---- 版本变化信号（供 UI 在探测完成后刷新显示）----
    def _ensure_signals(self):
        """惰性创建 Qt 信号持有者；无 PySide6 环境返回 None（headless 安全）。"""
        if self._sig is not None:
            return self._sig
        try:
            from PySide6.QtCore import QObject, Signal
        except Exception:
            return None

        class _ConfigSignals(QObject):
            # 版本重新探测完成（GIMI / ORFix / TexFx），无附加参数
            versions_changed = Signal()

        self._sig = _ConfigSignals()
        return self._sig

    @property
    def versions_changed(self):
        """返回 versions_changed 信号对象，供 UI 层 connect（无 Qt 时为 None）。"""
        sig = self._ensure_signals()
        return sig.versions_changed if sig is not None else None

    def reload_versions(self):
        """重新探测 GIMI/ORFix/TexFx 版本（例如切换 gimi_folder 之后）。

        复用 ``_detect_versions_async``：有 Qt 时异步探测并在完成后发出
        ``versions_changed``；无 Qt 时自动回退为同步探测。供设置页切换 GIMI
        目录后调用，使 home 页 InfoZone 与「检查更新」使用新目录的版本。
        """
        self._detect_versions_async()

    def __contains__(self, key):
        return key in self._data

    def __repr__(self):
        return f"ConfigManager({len(self._data)} keys)"
