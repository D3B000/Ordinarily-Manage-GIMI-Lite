"""
omg.domain.settings — 设置页「功能实现」层（业务逻辑）

本模块只负责「行为」，不含任何 UI 布局：
  - 路径配置的读取 / 写入（omg.core.config.ConfigManager）；
  - 储存机制 = 改动即生效：任意字段变更立即 set + save 落盘 config.json；
  - 文件 / 目录选择对话框（QFileDialog）。

UI（窗口、标题栏、侧边栏、卡片、控件）全部在 omg.pages.setting 中。
本模块 **不反向依赖 pages**：它只通过 PathPage 暴露的公开接口
（Qt 信号 game_path_changed / gimi_path_changed / browse_game_requested /
browse_gimi_requested，以及 setter/getter set_game_path / get_game_path 等）
来驱动 UI，从而保持 domain → 无 pages 依赖的单向结构。

设计要点（用户要求）：
  1. 路径配置（第一步）：游戏路径 + GIMI 路径。
  2. 无「应用 / 取消」：UI 层不提供该操作栏，本层也不会产生「待应用」的中间态。
  3. 储存机制 = 改动即生效。
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QMessageBox

from omg.core.config import ConfigManager
from omg.core.logging_setup import logger
from omg.core.window_behavior import (
    DEFAULT_IDLE_OPACITY,
    KEY_OMG_ALWAYS_ON_TOP,
    KEY_OMG_IDLE_OPACITY,
    KEY_OMG_LAUNCH_HOLD,
    KEY_OMG_LAUNCH_HOLD_MS,
    DEFAULT_LAUNCH_HOLD_MS,
    clamp_idle_opacity,
    clamp_launch_hold_ms,
)


# 持久化键名（与 core/config.PERSISTENT_KEYS 对齐）
KEY_GAME = "game_folder"
KEY_GIMI = "gimi_folder"

# GIMI 页（d3dx.ini + 更新）
KEY_HUNTING = "hunting"            # d3dx.ini: Hunting，取值 "0"/"1"/"2"
KEY_WARNING = "warning"            # d3dx.ini: Warning，取值 "0"/"1"
KEY_AUTO_CHECK = "auto_check_update"   # 打开 OMG 时检查更新（1/0）
KEY_WHITELIST = "gimi_update_whitelist"  # 更新白名单（list[str]）

# 启动页（启动方法 / 自定义启动 / 注入库）
KEY_LAUNCH_METHOD = "launch_method"                   # native / shell / manual
KEY_CUSTOM_ENABLED = "custom_launch_enabled"         # 自定义启动开关（1/0）
KEY_CUSTOM_CMD = "custom_launch_cmd"                 # 自定义启动命令
KEY_CUSTOM_INJECT_MODE = "custom_launch_inject_mode"  # hook / inject / bypass
KEY_LIB_ENABLED = "extra_libraries_enabled"          # 注入库开关（1/0）
KEY_LIB = "extra_libraries"                          # 注入库列表（多行文本）

# OMG 页（窗口行为；键名与常量由 omg.core.window_behavior 定义，避免两份漂移）
KEY_OMG_PIN = KEY_OMG_ALWAYS_ON_TOP                    # 默认置顶（1/0）
KEY_OMG_OPACITY = KEY_OMG_IDLE_OPACITY                 # 闲置透明度（int 20~100）

# 分段选项 → 持久化取值（与 core/config / loader 消费端一致，统一小写）
LAUNCH_METHODS = ["native", "shell", "manual"]
INJECT_MODES = ["hook", "inject", "bypass"]


def _safe_index(options, value, default: int = 0) -> int:
    """把持久化的字符串值映射回分段控件索引；找不到时回落到 default。"""
    try:
        return options.index(value)
    except (ValueError, TypeError):
        return default


class SettingsController:
    """设置页功能控制器：把 PathPage 的 UI 交互映射到 ConfigManager。

    UI 在 omg.pages.setting.SettingsWindow / PathPage 中创建，并在构建完成后
    调用本控制器的 ``bind(window)`` 完成行为绑定。绑定后，所有路径改动都会
    即时落盘，无需「应用」按钮。
    """

    def __init__(self, config: Optional[ConfigManager] = None) -> None:
        # 共享的 ConfigManager 实例（路径改动经由它即时读写并落盘）
        self.cfg = config if config is not None else ConfigManager()
        if config is None:
            # 不传 config 会新建一个独立 ConfigManager：设置改动只写进该实例，
            # 与「开始」按钮的 LaunchController 不是同一份数据，导致配置看起来
            # 已保存但实际不生效。正常应用路径必须传入共享实例，此处告警以便排查。
            logger.warning(
                "SettingsController 未收到共享的 ConfigManager，已新建独立实例；"
                "设置改动不会同步到 LaunchController，启动/路径配置将无法生效。"
            )

    # ------------------------------------------------------------------
    # 绑定：把 UI 信号接到本控制器的方法
    # ------------------------------------------------------------------
    def bind(self, window) -> None:
        """将设置窗口（omg.pages.setting.SettingsWindow）各页交互绑定到本控制器。"""
        # ---- 路径页（第一步）----
        path = window.path_page
        path.set_game_path(self.cfg.get(KEY_GAME, "") or "")
        path.set_gimi_path(self.cfg.get(KEY_GIMI, "") or "")
        path.game_path_changed.connect(self._on_game_changed)
        path.gimi_path_changed.connect(self._on_gimi_changed)
        path.browse_game_requested.connect(lambda: self._browse_game(path))
        path.browse_gimi_requested.connect(lambda: self._browse_gimi(path))

        # ---- GIMI 页 ----
        gimi = window.gimi_page
        # 回填当前已保存值（与 core/config.PERSISTENT_KEYS 对齐）
        gimi.set_hunting(int(self.cfg.get(KEY_HUNTING, 0) or 0))
        gimi.set_warning(int(self.cfg.get(KEY_WARNING, 0) or 0))
        # 注意：用 `or 0` 而非 `or 1` —— 旧代码 `or 1` 会让保存为 0（关闭）的
        # 值在回填时被强制判定为开启，导致「关闭开关」永远不生效。
        gimi.set_autocheck(bool(int(self.cfg.get(KEY_AUTO_CHECK, 1) or 0)))
        gimi.set_whitelist(list(self.cfg.get(KEY_WHITELIST, []) or []))
        # 监听交互 → 即时落盘
        gimi.hunting_changed.connect(self._on_hunting_changed)
        gimi.warning_changed.connect(self._on_warning_changed)
        gimi.autocheck_changed.connect(self._on_autocheck_changed)
        gimi.whitelist_changed.connect(self._on_whitelist_changed)
        gimi.add_file_requested.connect(lambda: self._add_whitelist_files(gimi))

        # ---- 启动页（启动方法 / 自定义启动 / 注入库）----
        startup = window.startup_page
        # 回填当前已保存值（与 core/config.PERSISTENT_KEYS 对齐）
        startup.set_method(_safe_index(
            LAUNCH_METHODS, self.cfg.get(KEY_LAUNCH_METHOD, "native")))
        startup.set_custom(bool(int(self.cfg.get(KEY_CUSTOM_ENABLED, 0) or 0)))
        startup.set_custom_cmd(self.cfg.get(KEY_CUSTOM_CMD, "") or "")
        startup.set_inject_method(_safe_index(
            INJECT_MODES, (self.cfg.get(KEY_CUSTOM_INJECT_MODE, "hook") or "hook").lower()))
        startup.set_lib(bool(int(self.cfg.get(KEY_LIB_ENABLED, 0) or 0)))
        startup.set_lib_text(self.cfg.get(KEY_LIB, "") or "")
        # 监听交互 → 即时落盘
        startup.method_changed.connect(self._on_launch_method_changed)
        startup.custom_toggled.connect(self._on_custom_toggled)
        startup.custom_cmd_changed.connect(self._on_custom_cmd_changed)
        startup.inject_method_changed.connect(self._on_custom_inject_changed)
        startup.lib_toggled.connect(self._on_lib_toggled)
        startup.lib_text_changed.connect(self._on_lib_text_changed)

        # ---- OMG 页（窗口行为：默认置顶 / 闲置透明度 / 启动防误触）----
        # 与其它页一致：先回填（此时信号未连接，不会写盘），再连接信号。
        omg = window.omg_page
        omg.set_always_on_top(bool(int(self.cfg.get(KEY_OMG_PIN, 0) or 0)))
        omg.set_idle_opacity(clamp_idle_opacity(
            self.cfg.get(KEY_OMG_OPACITY, DEFAULT_IDLE_OPACITY)))
        omg.set_launch_hold(bool(int(
            self.cfg.get(KEY_OMG_LAUNCH_HOLD, 0) or 0)))
        omg.set_launch_hold_ms(clamp_launch_hold_ms(
            self.cfg.get(KEY_OMG_LAUNCH_HOLD_MS, DEFAULT_LAUNCH_HOLD_MS)))
        omg.always_on_top_toggled.connect(self._on_omg_pin_toggled)
        omg.idle_opacity_changed.connect(self._on_omg_opacity_changed)
        omg.launch_hold_toggled.connect(self._on_omg_hold_toggled)
        omg.launch_hold_ms_changed.connect(self._on_omg_hold_ms_changed)

    # ------------------------------------------------------------------
    # 改动即生效：写入 ConfigManager 并落盘
    # ------------------------------------------------------------------
    def _save(self, key: str, text: str) -> None:
        """仅在与当前值不同时写入并保存，避免无效写盘。"""
        current = self.cfg.get(key, "") or ""
        if current == text:
            return
        self.cfg.set(key, text)
        self.cfg.save()

    def _on_game_changed(self, text: str) -> None:
        self._save(KEY_GAME, text)

    def _on_gimi_changed(self, text: str) -> None:
        self._save(KEY_GIMI, text)
        # 切换 GIMI 目录后重新探测版本：使 home 页 InfoZone 与「检查更新」
        # 使用新目录的版本，而非换之前的旧版本。
        if os.path.isdir(text):
            try:
                self.cfg.reload_versions()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # GIMI 页：d3dx.ini + 更新（改动即生效）
    # ------------------------------------------------------------------
    def _on_hunting_changed(self, index: int) -> None:
        self._save(KEY_HUNTING, str(index))

    def _on_warning_changed(self, index: int) -> None:
        self._save(KEY_WARNING, str(index))

    def _on_autocheck_changed(self, on: bool) -> None:
        self._save(KEY_AUTO_CHECK, 1 if on else 0)

    def _on_whitelist_changed(self, items) -> None:
        """更新白名单变更：与当前值不同才写盘（增 / 删都会带来差异）。"""
        current = list(self.cfg.get(KEY_WHITELIST, []) or [])
        if current == list(items):
            return
        self.cfg.set(KEY_WHITELIST, list(items))
        self.cfg.save()

    # ------------------------------------------------------------------
    # 启动页：启动方法 / 自定义启动 / 注入库（改动即生效）
    # ------------------------------------------------------------------
    def _on_launch_method_changed(self, index: int) -> None:
        self._save(KEY_LAUNCH_METHOD, LAUNCH_METHODS[index])

    def _on_custom_toggled(self, on: bool) -> None:
        self._save(KEY_CUSTOM_ENABLED, 1 if on else 0)

    def _on_custom_cmd_changed(self, text: str) -> None:
        self._save(KEY_CUSTOM_CMD, text)

    def _on_custom_inject_changed(self, index: int) -> None:
        self._save(KEY_CUSTOM_INJECT_MODE, INJECT_MODES[index])

    def _on_lib_toggled(self, on: bool) -> None:
        self._save(KEY_LIB_ENABLED, 1 if on else 0)

    def _on_lib_text_changed(self, text: str) -> None:
        self._save(KEY_LIB, text)

    # ------------------------------------------------------------------
    # OMG 页：窗口行为（改动即生效）
    # ------------------------------------------------------------------
    def _on_omg_pin_toggled(self, on: bool) -> None:
        self._save(KEY_OMG_PIN, 1 if on else 0)

    def _on_omg_opacity_changed(self, value) -> None:
        """闲置透明度改动：先 clamp（20~100，非法回落 42）再落盘。

        int 存储，不走 ``_save``（其字符串比较对 int 会误判「值未变」漏写盘）。
        """
        n = clamp_idle_opacity(value)
        if self.cfg.get(KEY_OMG_OPACITY, DEFAULT_IDLE_OPACITY) == n:
            return
        self.cfg.set(KEY_OMG_OPACITY, n)
        self.cfg.save()

    def _on_omg_hold_toggled(self, on: bool) -> None:
        self._save(KEY_OMG_LAUNCH_HOLD, 1 if on else 0)

    def _on_omg_hold_ms_changed(self, value) -> None:
        """长按时间改动：先 clamp（200~3000，非法回落 800）再落盘。

        int 存储，不走 ``_save``（其字符串比较对 int 会误判「值未变」漏写盘）。
        """
        n = clamp_launch_hold_ms(value)
        if self.cfg.get(KEY_OMG_LAUNCH_HOLD_MS, DEFAULT_LAUNCH_HOLD_MS) == n:
            return
        self.cfg.set(KEY_OMG_LAUNCH_HOLD_MS, n)
        self.cfg.save()

    def _add_whitelist_files(self, page) -> None:
        """点击「+」：打开文件选择窗口，将 GIMI 目录内的文件加入更新白名单。

        仅允许选择 GIMI 目录内的文件（白名单语义：更新解压时跳过这些文件），
        白名单以**相对 GIMI 目录**的规范路径落盘（改 GIMI 目录也不失效）；
        自动去重；选中后即时刷新 UI 列表并落盘。
        """
        gimi_dir = self.cfg.get(KEY_GIMI, "") or ""
        if not gimi_dir or not os.path.isdir(gimi_dir):
            QMessageBox.warning(
                page, "未配置 GIMI 目录",
                "请先在「路径」页设置 GIMI 目录，再添加更新白名单文件。",
            )
            return
        files, _ = QFileDialog.getOpenFileNames(
            page, "选择更新白名单文件", gimi_dir,
            "所有文件 (*);;配置文件 (*.ini *.json *.cfg *.txt)",
        )
        if not files:
            return
        wl = list(self.cfg.get(KEY_WHITELIST, []) or [])
        # 去重集合：已存条目统一视为「相对 GIMI 目录」的规范路径（兼容历史绝对路径）
        wl_lower = {self._normalize_wl_entry(p, gimi_dir).lower() for p in wl}
        added = False
        for f in files:
            abs_path = os.path.normpath(os.path.abspath(f))
            if not self._is_path_inside(abs_path, gimi_dir):
                QMessageBox.warning(
                    page, "文件不在 GIMI 目录内",
                    f"已跳过（白名单仅支持 GIMI 目录内的文件）：\n{abs_path}",
                )
                continue
            rel = self._normalize_wl_entry(abs_path, gimi_dir)
            if rel.lower() in wl_lower:
                continue
            wl.append(rel)
            wl_lower.add(rel.lower())
            added = True
        if added:
            page.set_whitelist(wl)            # 刷新 UI 列表
            self._on_whitelist_changed(wl)    # 即时落盘

    @staticmethod
    def _normalize_wl_entry(entry: str, gimi_dir: str) -> str:
        """把白名单条目归一化为相对 GIMI 目录的规范路径（正斜杠）。

        兼容历史数据中保存的绝对路径：若是绝对路径则换算为相对路径；
        否则视为相对路径直接归一化。统一去除前导斜杠、折叠重复分隔符。
        """
        entry = entry.replace("\\", "/").strip().lstrip("/")
        if os.path.isabs(entry):
            try:
                rel = os.path.relpath(os.path.normpath(entry), gimi_dir)
            except (ValueError, OSError):
                rel = os.path.normpath(entry)
            entry = rel
        return os.path.normpath(entry).replace("\\", "/")

    @staticmethod
    def _is_path_inside(path: str, base: str) -> bool:
        """判断 path 是否位于 base 目录内（含 base 自身）。"""
        try:
            common = os.path.commonpath(
                [os.path.realpath(base), os.path.realpath(path)]
            )
        except ValueError:
            return False
        return common == os.path.realpath(base)

    # ------------------------------------------------------------------
    # 浏览对话框
    # ------------------------------------------------------------------
    def _browse_game(self, page) -> None:
        """浏览游戏目录：选择文件夹后回填文本框（进而即时保存）。"""
        initial = page.get_game_path()
        path = QFileDialog.getExistingDirectory(page, "选择游戏目录", initial)
        if path:
            page.set_game_path(path)

    def _browse_gimi(self, page) -> None:
        """浏览 GIMI 目录：选择后回填文本框（进而即时保存）。"""
        initial = page.get_gimi_path()
        path = QFileDialog.getExistingDirectory(page, "选择 GIMI 目录", initial)
        if path:
            page.set_gimi_path(path)
