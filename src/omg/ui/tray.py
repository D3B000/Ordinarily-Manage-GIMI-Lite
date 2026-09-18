"""
omg.ui.tray — 系统托盘控制器（OMGLite 托盘常驻形态）。

为什么存在
----------
OMGLite 的主面板是一个 176×42 的无边框小条，常驻屏幕比常驻任务栏更合适：
任务栏按钮对它几乎没有价值（没有标题、没有最小化需求），反而占一个位置。
改为托盘式之后：

- 主面板（``HomeWindow``）以 ``Qt.Tool`` 创建 → 不进任务栏、不进 Alt+Tab；
- 托盘图标（``resources/icons/krr.ico``）成为进程的唯一常驻入口；
- 右键菜单：显示面板 / 资源浏览 / 复位面板 / —— / 退出程序。

关键纪律
--------
1. **仅在 ``QSystemTrayIcon.isSystemTrayAvailable()`` 为真时启用托盘模式**。
   托盘不可用时若仍把主窗口藏起来，进程将既看不见也退不掉。因此可用性判定
   必须在 ``QApplication`` 创建之后、``HomeWindow`` 构造之前完成，由调用方
   （``omg.app.main``）据此决定是否传 ``tray_mode=True``。
2. 退出前显式 ``tray.hide()``：否则 Windows 资源管理器重启之前会残留僵尸图标
   （Qt 不保证进程退出时自动清理，explorer 侧只在会话结束才刷新）。
3. 左键（单击 / 双击）一律「显示面板」且保持**幂等**——不做显隐切换，避免
   双击被解释成两次切换后窗口又回到隐藏。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from omg.core.paths import ICONS_DIR

__all__ = [
    "TRAY_ICON_PATH",
    "TrayController",
    "is_tray_available",
    "load_tray_icon",
]

# 托盘图标：与 OMGDev 一脉相承的 krr.ico（内含多尺寸，Qt 会按 DPI 自动挑选）
TRAY_ICON_PATH = os.path.join(ICONS_DIR, "krr.ico")

_DEFAULT_TOOLTIP = "OMG"


def is_tray_available() -> bool:
    """系统托盘是否可用（须在 ``QApplication`` 创建之后调用）。"""
    try:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception:
        # 极端环境（无 QApplication / 无桌面会话）下宁可判定为不可用，
        # 由调用方回退到「任务栏可见」的普通窗口模式。
        return False


def load_tray_icon(path: Optional[str] = None) -> QIcon:
    """加载托盘图标：优先 ``krr.ico``，缺失时回退应用图标，再退化为空图标。

    返回空图标时 ``QSystemTrayIcon.show()`` 仍能工作（只是没有图案），
    不会中断启动流程。
    """
    for cand in (path, TRAY_ICON_PATH):
        if cand and os.path.isfile(cand):
            icon = QIcon(cand)
            if not icon.isNull():
                return icon
    app = QApplication.instance()
    if app is not None and not app.windowIcon().isNull():
        return app.windowIcon()
    return QIcon()


class _TaskbarCreatedFilter(QAbstractNativeEventFilter):
    """explorer 重启后的托盘图标自愈。

    Windows 资源管理器崩溃 / 重启时会向所有顶层窗口广播 ``TaskbarCreated``，
    此时所有托盘图标都会从通知区消失（explorer 不再认识它们），必须重新注册。
    不处理的话表现为：「explorer 重启后 OMG 还在跑，但托盘图标不见了，只能靠
    任务管理器结束进程」——即所谓的图标滞留 / 丢失。
    """

    def __init__(self, owner: "TrayController") -> None:
        super().__init__()
        self._owner = owner
        self._msg_id = 0
        if sys.platform == "win32":
            try:
                fn = ctypes.windll.user32.RegisterWindowMessageW
                fn.argtypes = [ctypes.c_wchar_p]
                fn.restype = ctypes.c_uint32
                self._msg_id = int(fn("TaskbarCreated"))
            except Exception:
                self._msg_id = 0

    @property
    def message_id(self) -> int:
        return self._msg_id

    def nativeEventFilter(self, eventType, message):  # type: ignore[override]
        if not self._msg_id:
            return False, 0
        try:
            if not bytes(eventType).startswith(b"windows_"):
                return False, 0
        except Exception:
            return False, 0
        try:
            # MSG: HWND, UINT message, WPARAM, LPARAM, ...
            msg_id = ctypes.cast(int(message),
                                 ctypes.POINTER(ctypes.c_uint32))[1]
        except Exception:
            return False, 0
        if msg_id == self._msg_id:
            self._owner.recreate()
            return True, 0
        return False, 0


class TrayController(QObject):
    """托盘图标 + 右键菜单的持有者。

    回调均可选；``on_quit`` 缺省时退化为 ``QApplication.quit()``。
    ``parent`` 建议传 ``QApplication`` 实例，保证控制器在整个事件循环期间存活
    （否则局部变量被回收会导致托盘图标与菜单一起消失）。

    进程内**只允许存在一个**活动控制器（``_active``）：重复构造会先撤掉旧图标，
    从根上杜绝「同时出现两个托盘图标」。
    """

    #: 当前活动的控制器（None 表示还没有）
    _active: Optional["TrayController"] = None

    def __init__(
        self,
        on_show_panel: Optional[Callable[[], None]] = None,
        on_resources: Optional[Callable[[], None]] = None,
        on_reset_panel: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        parent: Optional[QObject] = None,
        icon_path: Optional[str] = None,
        tooltip: str = _DEFAULT_TOOLTIP,
    ) -> None:
        super().__init__(parent)
        self._on_show_panel = on_show_panel
        self._on_resources = on_resources
        self._on_reset_panel = on_reset_panel
        self._on_quit = on_quit
        self._tooltip = tooltip
        self._icon_path = icon_path

        # 图标去重：同一进程内若已有一个控制器，先把它的图标撤掉
        old = TrayController._active
        if old is not None and old is not self:
            old.dispose()
        TrayController._active = self

        self._menu = self._build_menu()
        self._tray = self._make_tray()

        app = QApplication.instance()
        if app is not None:
            # 退出前主动隐藏，避免 explorer 侧残留僵尸图标
            app.aboutToQuit.connect(self.dispose)

        self._taskbar_filter = _TaskbarCreatedFilter(self)
        if app is not None and self._taskbar_filter.message_id:
            app.installNativeEventFilter(self._taskbar_filter)

        self._tray.show()

    # ------------------------------------------------------------------
    # 构造 / 自愈
    # ------------------------------------------------------------------
    def _make_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(load_tray_icon(self._icon_path), self)
        tray.setToolTip(self._tooltip)
        tray.setContextMenu(self._menu)
        tray.activated.connect(self._on_activated)
        return tray

    def recreate(self) -> None:
        """重建托盘图标（explorer 重启后自愈用）。

        不复用旧 ``QSystemTrayIcon``：它内部的 NOTIFYICONDATA 已随 explorer
        会话失效，只 hide/show 未必能重新注册，干脆整体换一个新的。
        """
        try:
            old = self._tray
            old.hide()
            old.setParent(None)
            old.deleteLater()
        except Exception:
            pass
        self._tray = self._make_tray()
        self._tray.show()

    # ------------------------------------------------------------------
    # 菜单
    # ------------------------------------------------------------------
    def _build_menu(self) -> QMenu:
        """右键菜单：显示面板 / 资源浏览 / 复位面板 / —— / 退出程序。

        「复位面板」用于面板被拖出可视区（多显示器插拔 / 分辨率变化）后的兜底：
        把它移回屏幕中心并清空位置记忆，否则用户点了也看不到任何变化。

        刻意不加图标：Windows 原生托盘菜单里塞图标既挤又破坏与其它托盘程序
        的一致性，文字已经足够表达。
        """
        menu = QMenu()
        menu.addAction("显示面板", self._show_panel)
        menu.addAction("资源浏览", self._open_resources)
        menu.addAction("复位面板", self._reset_panel)
        menu.addSeparator()
        menu.addAction("退出程序", self._quit)
        return menu

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_activated(self, reason) -> None:
        """左键单击 / 双击 → 显示面板（幂等，不做显隐切换）。"""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_panel()

    def _show_panel(self) -> None:
        if self._on_show_panel is not None:
            self._on_show_panel()

    def _open_resources(self) -> None:
        """菜单「资源浏览」：打开资源浏览面板（首页内部的同名入口）。"""
        if self._on_resources is not None:
            self._on_resources()

    def _reset_panel(self) -> None:
        """菜单「复位面板」：把面板移回屏幕中心。"""
        if self._on_reset_panel is not None:
            self._on_reset_panel()

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    @property
    def tray_icon(self) -> QSystemTrayIcon:
        return self._tray

    def notify(self, title: str, text: str,
               icon=QSystemTrayIcon.MessageIcon.Information,
               timeout_ms: int = 3000) -> None:
        """气泡通知（当前无调用方，保留供后续更新提示接入）。"""
        self._tray.showMessage(title, text, icon, timeout_ms)

    def hide(self) -> None:
        """隐藏托盘图标（正常由 ``aboutToQuit`` 自动触发）。"""
        self._tray.hide()

    def dispose(self) -> None:
        """彻底销毁：隐藏图标、摘掉过滤器、释放进程内单例标记。

        与 ``hide()`` 的区别：hide 只是暂时不显示（可再 show），dispose 用于
        「本控制器不再使用」——包括退出前清理、以及被新的控制器顶替时。
        """
        try:
            self._tray.hide()
            self._tray.setContextMenu(None)
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None and getattr(self, "_taskbar_filter", None) is not None:
                app.removeNativeEventFilter(self._taskbar_filter)
        except Exception:
            pass
        if TrayController._active is self:
            TrayController._active = None

    @staticmethod
    def active() -> Optional["TrayController"]:
        """当前活动的控制器（没有则 None）。"""
        return TrayController._active
