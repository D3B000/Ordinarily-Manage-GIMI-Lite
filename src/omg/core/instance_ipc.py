"""
omg.core.instance_ipc — 单实例「激活已有实例」的进程间通知（纯 Win32，零 Qt 依赖）。

为什么不用 QLocalServer / QLocalSocket
-------------------------------------
原来 ``ensure_single_instance`` 里预留了 ``ACTIVATE_SERVER_NAME`` 打算用
QLocalSocket 发激活信号，但**服务端从未实现**，于是第二个实例只能静默退出——
托盘化之后这尤其致命：用户双击图标什么反应都没有。

补服务端有两条路：

1. ``QLocalServer``（QtNetwork）——直观，但要额外 ``import PySide6.QtNetwork``。
   本机机械盘冷启动每个模块约 10~25 ms，且打包 spec 还得确认收集了 QtNetwork。
2. Win32 ``RegisterWindowMessage`` + ``SendMessageTimeout(HWND_BROADCAST)`` ——
   零新模块、零冷启动成本、无需事件循环即可发送（第二实例连 QApplication
   都不用创建，比原来的 ``QCoreApplication(sys.argv)`` 脏实例干净得多）。

本模块选 2：注册一个会话唯一的消息号，第二个实例广播它，已有实例用
``omg.ui.instance_activator.InstanceActivator``（Qt 原生事件过滤器）接收。

消息携带发送方 PID（wParam）：接收方据此忽略自己发出的广播，避免自激。
本模块刻意不导入 PySide6，headless / 打包环境均可安全导入。
"""

from __future__ import annotations

import ctypes
import os
import sys

__all__ = [
    "ACTIVATE_MESSAGE_NAME",
    "activate_message_id",
    "broadcast_activate",
]

#: 注册窗口消息名（会话内唯一；改动它会让新旧版本互不相认，勿随意改）
ACTIVATE_MESSAGE_NAME = "OMG_ActivateInstance_v1"

_HWND_BROADCAST = 0xFFFF
_SMTO_ABORTIFHUNG = 0x0002

_u32 = getattr(ctypes, "windll", None)

_msg_id_cache = 0


def activate_message_id() -> int:
    """返回「激活已有实例」的注册消息号；失败返回 0。

    ``RegisterWindowMessage`` 在同一 Windows 会话内对所有进程返回同一个值，
    且不会被其它程序的普通消息号撞车（返回值落在 0xC000~0xFFFF 区间）。
    """
    global _msg_id_cache
    if _msg_id_cache:
        return _msg_id_cache
    if sys.platform != "win32" or _u32 is None:
        return 0
    try:
        fn = _u32.user32.RegisterWindowMessageW
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_uint32
        mid = int(fn(ACTIVATE_MESSAGE_NAME))
    except Exception:
        return 0
    # 0 表示注册失败（极罕见）
    _msg_id_cache = mid
    return mid


def broadcast_activate(timeout_ms: int = 1200) -> bool:
    """向系统中所有顶层窗口广播「激活」消息（第二个实例调用）。

    用 ``SendMessageTimeout`` 而不是 ``SendMessage``：广播会投递到**每一个**
    顶层窗口，若其中某个窗口无响应，普通 SendMessage 会把本进程挂死。

    :return: 广播是否成功发出。**注意**：返回值只代表发出，不代表有人接收——
             Win32 广播无法确认接收方；已有实例若没装接收器（旧版本 / 异常），
             本次调用同样返回 True。
    """
    if sys.platform != "win32" or _u32 is None:
        return False
    mid = activate_message_id()
    if not mid:
        return False
    try:
        fn = _u32.user32.SendMessageTimeoutW
        fn.argtypes = [
            ctypes.c_void_p,    # HWND
            ctypes.c_uint32,    # UINT   msg
            ctypes.c_size_t,    # WPARAM (UINT_PTR)
            ctypes.c_ssize_t,   # LPARAM (LONG_PTR)
            ctypes.c_uint32,    # UINT   flags
            ctypes.c_uint32,    # UINT   timeout
            ctypes.POINTER(ctypes.c_size_t),  # PDWORD_PTR result
        ]
        fn.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t(0)
        ok = fn(ctypes.c_void_p(_HWND_BROADCAST), mid,
                ctypes.c_size_t(os.getpid()), ctypes.c_ssize_t(0),
                _SMTO_ABORTIFHUNG, int(timeout_ms), ctypes.byref(result))
        return bool(ok)
    except Exception:
        return False
