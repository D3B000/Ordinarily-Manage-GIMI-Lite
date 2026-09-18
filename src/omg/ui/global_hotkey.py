"""omg.ui.global_hotkey — Windows 全局热键（RegisterHotKey + Qt 原生事件过滤）。

为什么不用 ``QShortcut`` / ``QAction``：它们只在**本应用有焦点**时生效。资源浏览
界面的开关要在游戏里（全屏独占、OMG 在后台）也能按，所以必须走系统级热键。

实现方式（Win32）：

1. ``RegisterHotKey(NULL, id, MOD_ALT, VK_OEM_3)`` —— hwnd 传 NULL，热键消息
   投递到**调用线程**的消息队列，不绑定任何窗口，因此窗口销毁 / 重建（例如切
   置顶时 setWindowFlag 重建原生窗口）都不会让热键失效；
2. Qt 的 Windows 事件派发器在 peek 到 ``WM_HOTKEY`` 时会先过一遍
   ``QAbstractNativeEventFilter``，故装一个过滤器即可把系统消息桥接成 Qt 信号。

注意：注册的是进程级资源，同一 id 重复注册会失败（ERROR_HOTKEY_ALREADY_REGISTERED，
常见原因是被别的程序抢了组合键）。失败不影响其它功能，只发 ``failed`` 信号。

非 Windows 平台（以及 headless 冒烟测试里注册失败时）一律静默降级：
``register()`` 返回 False，不抛异常。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

logger = logging.getLogger(__name__)

__all__ = [
    "WM_HOTKEY",
    "MOD_ALT", "MOD_CONTROL", "MOD_SHIFT", "MOD_WIN", "MOD_NOREPEAT",
    "VK_OEM_3",
    "GlobalHotkey",
]

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000      # 按住不放只报一次，避免长按疯狂触发

#: 主键盘区左上角「`/~」键（US 布局）。Qt 侧是 Qt.Key_QuoteLeft，Win32 侧是这个。
VK_OEM_3 = 0xC0


class _MSG(ctypes.Structure):
    """Win32 MSG 结构（只关心前四个字段，后面补齐以便 cast 读指针安全）。"""

    if sys.platform == "win32":
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint32),
            ("wParam", ctypes.c_uint64),
            ("lParam", ctypes.c_int64),
            ("time", ctypes.c_uint32),
            ("pt_x", ctypes.c_int32),
            ("pt_y", ctypes.c_int32),
        ]
    else:                                    # pragma: no cover - 非 Windows 占位
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint32),
            ("wParam", ctypes.c_uint64),
            ("lParam", ctypes.c_int64),
            ("time", ctypes.c_uint32),
            ("pt_x", ctypes.c_int32),
            ("pt_y", ctypes.c_int32),
        ]


class _HotkeyFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 翻译成 owner 的 ``activated`` 信号。

    必须被 Python 侧持有引用（存在 owner 上），否则会被 GC 掉——Qt 侧只保存
    裸指针，不负责生命周期。
    """

    def __init__(self, owner: "GlobalHotkey") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, eventType, message):  # type: ignore[override]
        # Windows 上事件类型形如 b"windows_generic_MSG" / b"windows_dispatcher_MSG"
        try:
            if not bytes(eventType).startswith(b"windows_"):
                return False, 0
        except Exception:
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY and int(msg.wParam) == self._owner.hotkey_id:
            self._owner._on_hotkey()
            return True, 0
        return False, 0


class GlobalHotkey(QObject):
    """一个系统级热键。用法::

        hk = GlobalHotkey(VK_OEM_3, MOD_ALT, parent=self)
        hk.activated.connect(self.toggle_window)
        hk.register()

    Args:
        vk: 虚拟键码（见 ``VK_*`` 常量）。
        modifiers: ``MOD_ALT`` / ``MOD_CONTROL`` / ``MOD_SHIFT`` / ``MOD_WIN``
            的按位或；内部总会加上 ``MOD_NOREPEAT``。
        hotkey_id: 热键 id，同一线程内唯一即可，默认 0xBEEF。
    """

    #: 热键被按下（已过滤掉重复与非本 id 的消息）
    activated = Signal()
    #: 注册失败（多半是被其它程序占用），参数是可读的失败原因
    failed = Signal(str)

    def __init__(self, vk: int, modifiers: int, hotkey_id: int = 0xBEEF,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._vk = int(vk)
        self._modifiers = int(modifiers) | MOD_NOREPEAT
        self.hotkey_id = int(hotkey_id)
        self._registered = False
        self._filter: Optional[_HotkeyFilter] = None

    # ---- 生命周期 ----
    @property
    def registered(self) -> bool:
        """热键是否已成功注册到系统。"""
        return self._registered

    def register(self) -> bool:
        """注册热键并装上原生事件过滤器；失败返回 False（不抛异常）。"""
        if self._registered:
            return True
        if sys.platform != "win32":
            self.failed.emit("仅 Windows 支持全局热键")
            return False
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance()
        if application is None:
            self.failed.emit("QApplication 尚未创建")
            return False
        try:
            ok = bool(ctypes.windll.user32.RegisterHotKey(
                None, self.hotkey_id, self._modifiers, self._vk))
        except Exception as exc:                      # 极端环境（无 user32）
            self.failed.emit(f"RegisterHotKey 调用失败：{exc}")
            return False
        if not ok:
            code = ctypes.windll.kernel32.GetLastError()
            self.failed.emit(f"组合键可能已被其它程序占用（错误码 {code}）")
            return False
        # 过滤器交给 Qt 前必须自己留住引用
        self._filter = _HotkeyFilter(self)
        application.installNativeEventFilter(self._filter)
        self._registered = True
        return True

    def unregister(self) -> None:
        """注销热键并摘掉过滤器（重复调用安全）。"""
        if self._filter is not None:
            from PySide6.QtWidgets import QApplication
            application = QApplication.instance()
            if application is not None:
                application.removeNativeEventFilter(self._filter)
            self._filter = None
        if self._registered and sys.platform == "win32":
            try:
                ctypes.windll.user32.UnregisterHotKey(None, self.hotkey_id)
            except Exception as exc:                  # pragma: no cover
                logger.debug(f"UnregisterHotKey 失败：{exc}")
        self._registered = False

    # ---- 内部 ----
    def _on_hotkey(self) -> None:
        logger.debug("全局热键触发")
        self.activated.emit()

    def __del__(self):  # pragma: no cover - 兜底释放系统资源
        try:
            self.unregister()
        except Exception:
            pass
