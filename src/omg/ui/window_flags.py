"""
omg.ui.window_flags — 顶层窗口 flags 的单一真源。

为什么存在
----------
OMGLite 改成托盘常驻后，「这个窗口要不要出现在任务栏」成了一个跨窗口的
一致性要求，规则集中在这里，避免每个窗口各写一份
``Qt.FramelessWindowHint | Qt.Window``（历史上就是因为各写各的，资源浏览页
早就用了 ``Qt.Tool`` 而其它页面没有）。

当前策略（2026-09-18 起）
------------------------
**只有首页（HomeWindow）不进任务栏**，其它页面（设置 / 便捷构建 / 资源浏览）
一律显示任务栏图标——它们是会长时间停留的独立窗口，用户切走后需要任务栏
按钮才能切回来；首页由托盘图标 + ``Alt+` `` 负责召回，不需要任务栏位。

唯一的例外是「命令输出侧页」（``ui/OMGCmdOutput.py``）：它是 1.5s 后自动
关闭的临时浮层，进任务栏只会闪一个无意义的按钮，故仍隐藏。

Windows 语义备忘
----------------
- ``Qt.Tool`` → ``WS_EX_TOOLWINDOW``：不进任务栏、不进 Alt+Tab。
- 反过来，带 owner 的窗口（QWidget(parent=home)）通常也不进任务栏，但那是
  Windows 的隐式规则、随 Qt 版本与窗口类型波动，不值得依赖；显式 ``Qt.Tool``
  才是可判定的（见 probe/taskbar_flag_check.py 读 GWL_EXSTYLE 的验证方式）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

__all__ = ["window_flags", "is_hidden_from_taskbar"]


def window_flags(*, frameless: bool = True,
                 hide_from_taskbar: bool = False) -> Qt.WindowType:
    """构造顶层窗口用的 flags（必须在 ``super().__init__(parent, flags)`` 里传入）。

    事后调用 ``setWindowFlag`` 会走 ``setParent`` 重建原生窗口：丢位置、闪一下，
    还会打断已经装好的事件过滤器与几何记忆，所以一律构造时传。

    :param frameless: 是否无边框（OMGLite 所有窗口都是自绘圆角面板，恒为 True）。
    :param hide_from_taskbar: 是否不进任务栏。按当前策略**仅首页与临时浮层
        传 True**，常规子页面传 False。
    """
    flags: Qt.WindowType = Qt.Window
    if frameless:
        flags |= Qt.FramelessWindowHint
    if hide_from_taskbar:
        flags |= Qt.Tool
    return flags


def is_hidden_from_taskbar(window) -> bool:
    """判定窗口当前是否「不进任务栏」。

    注意：``Qt.Tool`` 是复合位（Popup|Dialog = 0xb），只按位与取真值会被
    ``Qt.Window`` 位命中而误判，必须判完整包含。
    """
    try:
        flags = window.windowFlags()
    except Exception:
        return False
    return (flags & Qt.Tool) == Qt.Tool
