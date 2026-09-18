"""
omg.ui.OMGCmdOutput — OMG 风格「命令输出」侧页。

给耗时操作（如「写回差分值」）一个**贴着主窗口侧面**的实时输出页：

* 位置：优先贴在锚点窗口**右侧**；右侧放不下则退到**左侧**；与锚点之间留
  ``GAP`` 的细微间隔，整体再按屏幕可用区夹取，保证完整可见；
* 内容：等宽字体只读文本域，逐行追加（``append``），自动滚到底部；
* 生命周期：运行期间常驻，结束后由调用方延时自动关闭，最终结果 / 错误由调用方
  记入 log（本组件只负责「显示」，不碰日志与业务）；
* 置顶：锚点窗口置顶时可用 ``set_topmost(True)`` 同步（Win32 ``SetWindowPos``，
  不重建原生窗口，因此不闪烁）。

用法::

    panel = OMGCmdOutput(theme="dark")
    panel.reset("写回差分值")
    panel.place_beside(self)        # 贴到主窗口右侧（放不下则左侧）
    panel.set_topmost(self._pinned)
    panel.show()
    panel.append("扫描到 12 个 .ini")
    ...
    panel.mark_done(True)
    QTimer.singleShot(1500, panel.hide)

设计取舍：与 ``OMGPopCard`` 同一套「透明顶层窗 + 实体子容器」结构——阴影加在
子容器上（直接加在透明顶层窗会让 Windows 的 ``UpdateLayeredWindowIndirect``
失败），顶层窗四周留 ``MARGIN`` 透明边给阴影；配色复用 OMG Token，ui 层不反向
依赖 pages。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import QPoint, Qt
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
        "shadow": QColor(0, 0, 0, 60),
    },
}

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"
FONT_SIZE_TITLE = 13
FONT_SIZE_CONSOLE = 12

PANEL_W = 320          # 可视宽度（不含阴影留白）
PANEL_MIN_W = 240
PANEL_MIN_H = 160
GAP = 8                # 与锚点窗口之间的细微间隔
EDGE_PAD = 6           # 距屏幕可用区边缘的最小留白

RADIUS = 8             # 面板圆角
PAD_X = 12             # 标题区左右内边距
PAD_Y = 8              # 标题区上下内边距
SHADOW_BLUR = 18
SHADOW_OFFSET_Y = 3
# 顶层窗透明留白必须装得下整片阴影（同 OMGPopCard，否则 layered 窗口刷新失败）
MARGIN = SHADOW_BLUR + abs(SHADOW_OFFSET_Y) + 4

MAX_LINES = 2000       # 输出行数上限（超出丢弃最旧的，防长任务吃内存）


def _tokens(theme: str) -> dict:
    return _TOKENS.get(theme, _TOKENS["dark"])


class OMGCmdOutput(QWidget):
    """贴在主窗口侧面的命令输出页（只读、逐行追加、可手动关闭）。

    Args:
        theme: ``"dark"``（默认）/ ``"light"``。
        title: 标题栏文案，可用 :meth:`reset` 每次运行前改写。
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

        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        root.setSpacing(0)

        # 实体面板：背景 / 边框 / 阴影都在这里（透明顶层窗上不能加阴影）
        self._surface = QFrame(self)
        self._surface.setObjectName("OMGCmdOutputSurface")
        sl = QVBoxLayout(self._surface)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)

        head = QWidget(self._surface)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(PAD_X, PAD_Y, PAD_X - 4, PAD_Y)
        hl.setSpacing(8)
        self._title = QLabel(title, head)
        self._title.setObjectName("OMGCmdOutputTitle")
        hl.addWidget(self._title)
        self._hint = QLabel("", head)      # 运行态提示：运行中… / 完成 / 失败
        self._hint.setObjectName("OMGCmdOutputHint")
        hl.addWidget(self._hint)
        hl.addStretch(1)
        self._btn_close = QPushButton("×", head)
        self._btn_close.setObjectName("OMGCmdOutputClose")
        self._btn_close.setFixedSize(18, 18)
        self._btn_close.setCursor(Qt.PointingHandCursor)
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
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
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
        self.setFixedSize(PANEL_W + 2 * MARGIN, PANEL_MIN_H + 2 * MARGIN)

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
            f"color:{t['title']};font-family:'{FONT_FAMILY}';"
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
            f"{{height:0;}}"
            f"QScrollBar:horizontal{{background:transparent;height:8px;"
            f"margin:2px;}}"
            f"QScrollBar::handle:horizontal{{background:{t['border']};"
            f"border-radius:4px;min-width:24px;}}"
            f"QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal"
            f"{{width:0;}}")
        self._btn_close.setStyleSheet(
            f"QPushButton#OMGCmdOutputClose{{color:{t['text_secondary']};"
            f"background:transparent;border:none;font-size:14px;}}"
            f"QPushButton#OMGCmdOutputClose:hover{{color:{t['text']};}}")
        self._shadow.setColor(t["shadow"])

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def reset(self, title: Optional[str] = None) -> None:
        """清空输出并（可选）改写标题，标记本次运行开始。"""
        if title:
            self.setWindowTitle(title)
            self._title.setText(title)
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
        self.set_hint("完成" if ok else "失败", ok)

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
    # 定位 / 置顶
    # ------------------------------------------------------------------
    def place_beside(self, anchor: QWidget, prefer_right: bool = True) -> str:
        """贴到锚点窗口侧面：右侧优先，放不下则左侧；返回实际落在哪一侧。

        高度跟随锚点（并按屏幕可用区夹取），与锚点之间留 ``GAP``。
        """
        ag = anchor.frameGeometry()
        screen = QApplication.screenAt(ag.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else ag

        w = max(PANEL_MIN_W, min(PANEL_W, avail.width() - 2 * EDGE_PAD))
        h = max(PANEL_MIN_H,
                min(ag.height(), avail.height() - 2 * EDGE_PAD))
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
