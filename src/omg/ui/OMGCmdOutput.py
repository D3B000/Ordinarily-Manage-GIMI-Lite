"""
omg.ui.OMGCmdOutput — OMG 风格「命令输出」侧页。

给耗时操作（如「写回差分值」「构建」「文件优化」）一个**贴着主窗口侧面**的
实时输出页：

* 尺寸：固定 320×360（透明顶层窗四周留 ``MARGIN`` 阴影留白）；
* 位置：优先贴在锚点窗口**右侧**；右侧放不下则退到**左侧**；与锚点之间留
  ``GAP`` 的细微间隔，整体再按屏幕可用区夹取，保证完整可见；
* 标题：固定 ``>_``（终端提示符意象），标题栏的关闭按钮与其它页面同款
  （``genshin_function_control_close.svg`` + ``TitleBtn`` 样式）；
* 内容：等宽字体只读文本域，逐行追加（``append``），**仅垂直滚动条**，
  水平方向按控件宽度自动换行（``WidgetWidth``）；
* 关闭逻辑：运行结束调用 :meth:`finish` —— **成功则延时自动关闭**，
  **失败 / 异常则保留页面**，方便用户查看报错；用户也可随时点关闭按钮手动收起；
* 置顶：锚点窗口置顶时可用 ``set_topmost(True)`` 同步（Win32 ``SetWindowPos``，
  不重建原生窗口，因此不闪烁）。

用法::

    panel = OMGCmdOutput(theme="dark")
    panel.reset("构建")            # 清空并（可选）写窗口标题
    panel.place_beside(self)       # 贴到主窗口右侧（放不下则左侧）
    panel.set_topmost(self._pinned)
    panel.show()
    panel.append("扫描到 12 个 .ini")
    ...
    panel.finish(True)             # 成功 → 延时自动关闭
    panel.finish(False)            # 失败 / 异常 → 保留页面

设计取舍：与 ``OMGPopCard`` 同一套「透明顶层窗 + 实体子容器」结构——阴影加在
子容器上（直接加在透明顶层窗会让 Windows 的 ``UpdateLayeredWindowIndirect``
失败），顶层窗四周留 ``MARGIN`` 透明边给阴影；配色复用 OMG Token，ui 层不反向
依赖 pages。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omg.ui.icon_loader import tinted_icon
from omg.ui.window_flags import window_flags

# ---------------------------------------------------------------------------
# OMG Token（与 omg/ui/OMGPopCard.py 同色系）
# ---------------------------------------------------------------------------
_TOKENS = {
    "dark": {
        "surface": "#2C2C2E",
        "border": "#3A3A3C",
        "text": "#F5F5F7",
        "text_secondary": "#98989D",
        "title": "#FFFFFF",
        "divider": "#3A3A3C",
        "console_bg": "#1F1F21",
        "accent": "#0A84FF",
        "ok": "#639922",
        "error": "#E0554E",
        "hover": "#38383B",
        "pressed": "#444448",
        "shadow": QColor(0, 0, 0, 150),
    },
    "light": {
        "surface": "#FFFFFF",
        "border": "rgba(0,0,0,0.10)",
        "text": "#1D1D1F",
        "text_secondary": "#6B6B73",
        "title": "#18181B",
        "divider": "rgba(0,0,0,0.10)",
        "console_bg": "#F6F6F8",
        "accent": "#0A84FF",
        "ok": "#4A8B1F",
        "error": "#C0392B",
        "hover": "rgba(0,0,0,0.06)",
        "pressed": "rgba(0,0,0,0.10)",
        "shadow": QColor(0, 0, 0, 60),
    },
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"
FONT_SIZE_TITLE = 13          # ">_" 使用等宽字体，终端提示符意象（与其它页面 TitleText 同字号）
FONT_SIZE_CONSOLE = 12

PANEL_W = 320          # 固定可视宽度（不含阴影留白）
PANEL_H = 360          # 固定可视高度（不含阴影留白）
PANEL_MIN_W = 240      # 仅当屏幕可用宽度不足时作为下限夹取
PANEL_MIN_H = 160
GAP = 8                # 与锚点窗口之间的细微间隔
EDGE_PAD = 6           # 距屏幕可用区边缘的最小留白

RADIUS = 8             # 面板圆角
SHADOW_BLUR = 18
SHADOW_OFFSET_Y = 3
# 顶层窗透明留白必须装得下整片阴影（同 OMGPopCard，否则 layered 窗口刷新失败）
MARGIN = SHADOW_BLUR + abs(SHADOW_OFFSET_Y) + 4

MAX_LINES = 2000       # 输出行数上限（超出丢弃最旧的，防长任务吃内存）

# 成功完成后自动关闭的延时（毫秒）
CLOSE_DELAY = 1500

# 同款关闭按钮（与其它页面一致）
CLOSE_ICON = "genshin_function_control_close.svg"
CLOSE_ICON_SIZE = 16
TEXT_PRIMARY = "#F5F5F7"      # 深色主题前景色（关闭图标着色用）

# 固定标题（终端提示符意象）
TITLE_TEXT = ">_"


def _tokens(theme: str) -> dict:
    return _TOKENS.get(theme, _TOKENS["dark"])


class OMGCmdOutput(QWidget):
    """贴在主窗口侧面的命令输出页（只读、逐行追加、可手动关闭）。

    Args:
        theme: ``"dark"``（默认）/ ``"light"``。
        title: 窗口标题（任务栏 / 无障碍用），标题栏文字固定为 ``>_``。
    """

    def __init__(self, parent=None, theme: str = "dark",
                 title: str = "命令输出") -> None:
        # Qt.Tool：不进任务栏 / Alt+Tab，且与主窗口互不抢焦点
        super().__init__(None, window_flags(frameless=True,
                                            hide_from_taskbar=True))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle(title)

        self._theme = theme if theme in _TOKENS else "dark"
        self._topmost = False
        self._auto_close_timer: Optional[QTimer] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        root.setSpacing(0)

        # 实体面板：背景 / 边框 / 阴影都在这里（透明顶层窗上不能加阴影）
        self._surface = QFrame(self)
        self._surface.setObjectName("OMGCmdOutputSurface")
        sl = QVBoxLayout(self._surface)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        head = QFrame(self._surface)
        head.setObjectName("TitleBar")
        head.setFixedHeight(32)        # 与其它 OMG 页面标题栏同高
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 0, 8, 0)
        hl.setSpacing(4)
        self._title = QLabel(TITLE_TEXT, head)
        self._title.setObjectName("TitleText")   # 与其它页面同款标题样式
        hl.addWidget(self._title)
        self._hint = QLabel("", head)      # 运行态提示：运行中… / 完成 / 失败
        self._hint.setObjectName("OMGCmdOutputHint")
        hl.addWidget(self._hint)
        hl.addStretch(1)
        # 同款关闭按钮（与其它页面一致：TitleBtn + 主题前景色线稿图标）
        self._btn_close = QPushButton(head)
        self._btn_close.setObjectName("TitleBtn")
        self._btn_close.setFixedSize(24, 24)
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.setIcon(
            tinted_icon(CLOSE_ICON, CLOSE_ICON_SIZE, TEXT_PRIMARY))
        self._btn_close.setIconSize(QSize(CLOSE_ICON_SIZE, CLOSE_ICON_SIZE))
        self._btn_close.clicked.connect(self.hide)
        hl.addWidget(self._btn_close)
        sl.addWidget(head)

        line = QFrame(self._surface)
        line.setObjectName("OMGCmdOutputSep")
        line.setFixedHeight(1)
        sl.addWidget(line)

        self._view = QPlainTextEdit(self._surface)
        self._view.setObjectName("OMGCmdOutputView")
        self._view.setReadOnly(True)
        # 重新设计：水平按控件宽度自动换行，只保留垂直滚动条
        self._view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        font = QFont(MONO_FAMILY)
        font.setPixelSize(FONT_SIZE_CONSOLE)
        self._view.setFont(font)
        sl.addWidget(self._view, 1)

        shadow = QGraphicsDropShadowEffect(self._surface)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(_tokens(self._theme)["shadow"])
        self._surface.setGraphicsEffect(shadow)
        self._shadow = shadow

        root.addWidget(self._surface)
        self._apply_theme()
        self.setFixedSize(PANEL_W + 2 * MARGIN, PANEL_H + 2 * MARGIN)

    # ------------------------------------------------------------------
    # 外观
    # ------------------------------------------------------------------
    def _apply_theme(self) -> None:
        t = _tokens(self._theme)
        self.setStyleSheet("background:transparent;")
        self._surface.setStyleSheet(
            f"#OMGCmdOutputSurface{{background:{t['surface']};"
            f"border:1px solid {t['border']};border-radius:{RADIUS}px;}}"
            f"#OMGCmdOutputSep{{background:{t['divider']};border:none;}}")
        self._title.setStyleSheet(
            f"color:{t['title']};font-family:'{MONO_FAMILY}';"
            f"font-size:{FONT_SIZE_TITLE}px;font-weight:600;")
        self._view.setStyleSheet(
            f"QPlainTextEdit#OMGCmdOutputView{{background:{t['console_bg']};"
            f"color:{t['text']};border:none;"
            f"border-bottom-left-radius:{RADIUS - 1}px;"
            f"border-bottom-right-radius:{RADIUS - 1}px;"
            f"padding:8px 10px;selection-background-color:{t['accent']};}}"
            # 滚动条融入面板：细条 + 圆角把手（默认原生样式在深色面板上太扎眼）
            f"QScrollBar:vertical{{background:transparent;width:8px;margin:2px;}}"
            f"QScrollBar::handle:vertical{{background:{t['border']};"
            f"border-radius:4px;min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical"
            f"{{height:0;}}")
        self._btn_close.setStyleSheet(
            f"QPushButton#TitleBtn{{background:transparent;border:none;"
            f"padding:0;border-radius:4px;}}"
            f"QPushButton#TitleBtn:hover{{background:{t['hover']};}}"
            f"QPushButton#TitleBtn:pressed{{background:{t['pressed']};}}")
        self._shadow.setColor(t["shadow"])

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def reset(self, title: Optional[str] = None) -> None:
        """清空输出并（可选）改写窗口标题，标记本次运行开始。

        同时取消任何待执行的自动关闭（新一轮运行不应被上一轮的延时关闭打断）。
        """
        self.cancel_auto_close()
        if title:
            self.setWindowTitle(title)
        self._view.clear()
        self.set_hint("运行中…")

    def set_hint(self, text: str, ok: Optional[bool] = None) -> None:
        """设置标题栏右侧的状态提示；``ok`` 非 None 时按成功/失败着色。"""
        t = _tokens(self._theme)
        color = t["text_secondary"]
        if ok is True:
            color = t["ok"]
        elif ok is False:
            color = t["error"]
        self._hint.setText(text)
        self._hint.setStyleSheet(
            f"color:{color};font-family:'{FONT_FAMILY}';font-size:12px;")

    def mark_done(self, ok: bool) -> None:
        """仅更新标题栏右侧的状态提示（完成 / 失败），不触发关闭。"""
        self.set_hint("完成" if ok else "失败", ok)

    def finish(self, ok: bool) -> None:
        """标记结束并按结果决定去留。

        * 成功 → 延时 :data:`CLOSE_DELAY` 后自动关闭；
        * 失败 / 异常 → 保留页面，方便用户查看报错。

        调用方在耗时任务真正结束时调用（如 ``finished`` 信号回调）。
        """
        self.mark_done(ok)
        if ok:
            self._schedule_auto_close()
        else:
            self.cancel_auto_close()

    def append(self, line: str) -> None:
        """追加一行输出（可安全地从工作线程经信号调用）。"""
        self._view.appendPlainText(line if line is not None else "")
        # 超过上限时丢掉最旧的，避免长任务把内存吃满
        if self._view.blockCount() > MAX_LINES:
            kept = self._view.toPlainText().splitlines()[-MAX_LINES:]
            self._view.setPlainText("\n".join(kept))
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def text(self) -> str:
        return self._view.toPlainText()

    # ------------------------------------------------------------------
    # 自动关闭
    # ------------------------------------------------------------------
    def cancel_auto_close(self) -> None:
        """取消任何待执行的自动关闭定时器。"""
        if self._auto_close_timer is not None:
            self._auto_close_timer.stop()
            self._auto_close_timer.deleteLater()
            self._auto_close_timer = None

    def _schedule_auto_close(self, delay: int = CLOSE_DELAY) -> None:
        """（成功时）安排一次延时自动关闭；重复调用会刷新计时。"""
        self.cancel_auto_close()
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.hide)
        self._auto_close_timer.start(delay)

    def keep_open(self) -> None:
        """显式保持页面打开（取消待执行的自动关闭）。"""
        self.cancel_auto_close()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # 任何路径的收起都取消挂着的自动关闭，避免「关了又弹」或野定时器
        self.cancel_auto_close()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    # 定位 / 置顶
    # ------------------------------------------------------------------
    def place_beside(self, anchor: QWidget, prefer_right: bool = True) -> str:
        """贴到锚点窗口侧面：右侧优先，放不下则左侧；返回实际落在哪一侧。

        尺寸固定为 :data:`PANEL_W` × :data:`PANEL_H`，并按屏幕可用区夹取，
        保证完整可见。
        """
        ag = anchor.frameGeometry()
        screen = QApplication.screenAt(ag.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else ag

        w = max(PANEL_MIN_W, min(PANEL_W, avail.width() - 2 * EDGE_PAD))
        h = max(PANEL_MIN_H, min(PANEL_H, avail.height() - 2 * EDGE_PAD))
        self.setFixedSize(w + 2 * MARGIN, h + 2 * MARGIN)

        right_x = ag.right() + 1 + GAP
        left_x = ag.left() - GAP - w
        if prefer_right and right_x + w <= avail.right() - EDGE_PAD:
            x, side = right_x, "right"
        elif left_x >= avail.left() + EDGE_PAD:
            x, side = left_x, "left"
        elif right_x + w <= avail.right() - EDGE_PAD:
            x, side = right_x, "right"      # 左侧也放不下时仍偏右
        else:
            # 两侧都挤：贴右边界（不遮住锚点主体）
            x, side = max(avail.left() + EDGE_PAD,
                          avail.right() - EDGE_PAD - w), "right"
        y = max(avail.top() + EDGE_PAD,
                min(ag.top(), avail.bottom() - EDGE_PAD - h))
        self.move(QPoint(x - MARGIN, y - MARGIN))
        return side

    def set_topmost(self, on: bool) -> None:
        """跟随锚点窗口的置顶状态（Win32 z 序，不重建窗口、不闪）。"""
        self._topmost = bool(on)
        if self.isVisible():
            self._apply_topmost()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._topmost:
            # 原生窗口此时才创建完成，winId() 有效
            self._apply_topmost()

    def _apply_topmost(self) -> None:
        if sys.platform != "win32":
            self.setWindowFlag(Qt.WindowStaysOnTopHint, self._topmost)
            return
        hwnd = int(self.winId())
        if hwnd == 0:
            return
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_int
        flag = ctypes.c_void_p(-1) if self._topmost else ctypes.c_void_p(-2)
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        user32.SetWindowPos(ctypes.c_void_p(hwnd), flag, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
