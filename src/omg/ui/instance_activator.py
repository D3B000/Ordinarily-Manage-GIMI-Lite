"""
omg.ui.instance_activator — 接收「激活已有实例」广播（补上单实例的另一半）。

配套 ``omg.core.instance_ipc``：那边只负责广播（纯 Win32，无需 Qt），这里负责
接收——用 ``QAbstractNativeEventFilter`` 把 Windows 广播消息桥成 Qt 信号。

为什么不用 QLocalServer：见 ``omg.core.instance_ipc`` 模块说明（冷启动成本 +
打包依赖）。代价是 Win32 广播无法确认接收方，因此「已有实例是否真的醒了」
不可知；但这不影响使用——首个实例必然装了本接收器，装不上也只是退化成
原来的静默退出。
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

from omg.core.instance_ipc import activate_message_id

__all__ = ["InstanceActivator"]


class _MSG(ctypes.Structure):
    """Win32 MSG（只关心前四个字段；与 ui/global_hotkey.py 的 _MSG 同构）。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_uint64),
        ("lParam", ctypes.c_int64),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_int32),
        ("pt_y", ctypes.c_int32),
    ]


class _ActivateFilter(QAbstractNativeEventFilter):
    def __init__(self, owner: "InstanceActivator") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, eventType, message):  # type: ignore[override]
        try:
            if not bytes(eventType).startswith(b"windows_"):
                return False, 0
        except Exception:
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        except Exception:
            return False, 0
        if msg.message == self._owner.message_id:
            # wParam = 发送方 PID；自己发的（理论上不会）直接忽略，避免自激
            if int(msg.wParam) != os.getpid():
                self._owner._on_activate()
            return True, 0
        return False, 0


class InstanceActivator(QObject):
    """监听「激活」广播，收到后发 ``activated``（通常连到 ``HomeWindow.show_panel``）。

    用法（首个实例，QApplication 已创建之后）::

        act = InstanceActivator(app)
        act.activated.connect(win.show_panel)
        act.install()     # 返回 False 表示平台不支持 / 消息注册失败

    必须在 Python 侧持有引用（示例里的 ``act``），Qt 只保存过滤器裸指针。
    """

    #: 收到其它实例的激活请求（已在 Qt 主线程派发）
    activated = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._installed = False
        self._filter: object = None
        self._message_id = activate_message_id()

    @property
    def message_id(self) -> int:
        return self._message_id

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> bool:
        """装上原生事件过滤器。需要 QApplication 已存在。"""
        if self._installed:
            return True
        if sys.platform != "win32" or not self._message_id:
            return False
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return False
            filt = _ActivateFilter(self)
            app.installNativeEventFilter(filt)
            self._filter = filt      # Python 侧持有，防 GC
            self._installed = True
            return True
        except Exception:
            return False

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None and self._filter is not None:
                app.removeNativeEventFilter(self._filter)
        except Exception:
            pass
        self._filter = None
        self._installed = False

    def _on_activate(self) -> None:
        self.activated.emit()
