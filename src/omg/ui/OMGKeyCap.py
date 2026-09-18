"""
omg.ui.OMGKeyCap — OMG 风格键盘按键块（键帽）。

移植自 ``Lab/keycap.py``（apple-design 键帽质感），并做 OMG 化改造：

* **配色换成 OMG Token**：深色主题下键帽顶面 ``#3A3A3C → #2C2C2E``、裙边
  ``#1C1C1E → #141416``、发丝边框用半透明白（而非浅色主题的黑色），
  与 OMG 的卡片 / 输入框体系（``#2C2C2E`` 卡底、``#3A3A3C`` 边框）同色系；
  文字走 ``#F5F5F7`` / 次级 ``#98989D``，在深底上高对比可读。
* **尺寸改对齐标题栏图标按钮**：键帽高 ``24px``（= 资源浏览页标题栏
  ``TitleBtn`` 的 24×24），圆角 4px、裙边 2px，宽度由文本实测宽度 +
  14px 内边距得出（「A」窄、「Alt」宽），不再是固定 26 宽。
* **字号走像素单位**：标签 ``12px`` Bold。注意 ``QFont(family, 12)`` 是
  **点字号**（≈16px），此处统一用 ``setPixelSize``，保证测宽与绘制一致、且
  不会像点字号那样把键帽撑大。
* **不再依赖 stylekit**：Lab 版依赖 ``stylekit`` 取色，这里把两套色板内联，
  ``omg/ui`` 层不引入 pages / 外部样式库依赖，避免循环导入。
* 交互保留：按下时键帽下沉 2px、裙边收扁，并 emit ``keyPressed``。

用法：:

    from omg.ui.OMGKeyCap import OMGKeyCap, OMGKeyCapGroup

    cap = OMGKeyCap("A")                       # 单键
    cap = OMGKeyCap("1", sub="!")              # 主字符 + 上档字符
    cap = OMGKeyCap("Enter", min_width=60)     # 宽键
    row = OMGKeyCapGroup(["Alt", "`"])        # 「Alt + `」整行（自动加 + 号）
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

# 与 OMG 提示卡片一致的字族；键帽标签用**像素**字号（见下方 _font）
FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE = 12
FONT_WEIGHT = QFont.Weight.Bold

# ---------------------------------------------------------------------------
# 色板：dark 为 OMG 默认（与 setting.py / home.py 的 Token 同色系）
# ---------------------------------------------------------------------------
_PALETTES = {
    "dark": {
        "face_top": "#3A3A3C",           # 顶面亮部（= OMG 边框色，比卡底亮一档）
        "face_bot": "#2C2C2E",           # 顶面暗部（= OMG 卡片底色）
        "skirt_top": "#1C1C1E",          # 裙边上沿（比窗口底 #1F1F21 更暗）
        "skirt_bot": "#141416",          # 裙边下沿
        "border": QColor(255, 255, 255, 40),   # 发丝边框：半透明白
        "hilite": QColor(255, 255, 255, 55),   # 顶部内高光
        "text": QColor("#F5F5F7"),             # TEXT_PRIMARY
        "text_muted": QColor("#98989D"),       # TEXT_SECONDARY
    },
    "light": {
        "face_top": "#FFFFFF",
        "face_bot": "#EEF0F3",
        "skirt_top": "#D7D7DD",
        "skirt_bot": "#B6B6C0",
        "border": QColor(0, 0, 0, 40),
        "hilite": QColor(255, 255, 255, 210),
        "text": QColor("#1D1D1F"),
        "text_muted": QColor("#6B6B73"),
    },
}


def _palette(theme: str) -> dict:
    return _PALETTES.get(theme, _PALETTES["dark"])


def _font(size: int = FONT_SIZE) -> QFont:
    """像素字号字体（测宽与绘制必须共用同一份，否则宽度会偏）。"""
    f = QFont(FONT_FAMILY)
    f.setPixelSize(size)
    f.setWeight(FONT_WEIGHT)
    return f


# ---------------------------------------------------------------------------
# 键帽本体
# ---------------------------------------------------------------------------
class OMGKeyCap(QWidget):
    """一个 OMG 风格键帽（纯自绘）。

    Args:
        text: 主字符，如 ``"A"`` / ``"Alt"`` / ``"Enter"``。
        sub: 上档字符（更小更弱），如 ``"1"`` 键的 ``"!"``。
        min_width: 强制最小宽度，用于 Enter / Shift / 空格等宽键。
        theme: ``"dark"``（默认）/ ``"light"``。
    """

    keyPressed = Signal(str)

    # 高度与资源浏览页标题栏图标按钮（24×24）对齐；宽度按文本实测
    HEIGHT = 24          # 常规键帽高
    HEIGHT_SUB = 30      # 带上档字符时加高
    RADIUS = 4           # 键帽圆角
    MIN_WIDTH = 22       # 单字符键帽最小宽
    _PAD_X = 14          # 文本左右内边距合计
    _DEPTH = 2           # 底部裙边厚度（按下时收扁到 1）

    def __init__(self, text: str = "A", sub: str | None = None,
                 min_width: int | None = None,
                 theme: str = "dark", parent=None):
        super().__init__(parent)
        self._text = text
        self._sub = sub
        self._theme = theme if theme in _PALETTES else "dark"
        self._pressed = False
        self._hovered = False

        h = self.HEIGHT_SUB if sub else self.HEIGHT
        w = max(int(min_width or 0), self._text_width(text, sub) + self._PAD_X)
        self.setFixedSize(max(w, self.MIN_WIDTH), h)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

    # ---- 尺寸 ----
    @staticmethod
    def _text_width(text: str, sub: Optional[str]) -> int:
        font = _font()
        if QApplication.instance() is None:
            return max(len(text), len(sub or "")) * 7
        fm = QFontMetricsF(font)
        return int(max(fm.horizontalAdvance(text),
                       fm.horizontalAdvance(sub) if sub else 0))

    # ---- 主题 ----
    def set_theme(self, theme: str) -> None:
        if theme in _PALETTES and theme != self._theme:
            self._theme = theme
            self.update()

    # ---- 交互 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
            self.keyPressed.emit(self._text)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pressed:
            self._pressed = False
            self.update()
        super().mouseReleaseEvent(e)

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(e)

    # ---- 绘制 ----
    def paintEvent(self, e):
        pal = _palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        r = self.RADIUS
        press = 2 if self._pressed else 0
        depth = 1 if self._pressed else self._DEPTH

        # 1) 裙边：底部深色，构成 3D 凸起
        skirt = QLinearGradient(0, 0, 0, h)
        skirt.setColorAt(0, QColor(pal["skirt_top"]))
        skirt.setColorAt(1, QColor(pal["skirt_bot"]))
        p.setPen(Qt.NoPen)
        p.setBrush(skirt)
        p.drawRoundedRect(0, press, w, h - press, r, r)

        # 2) 顶面：上亮下暗渐变，下沿压到 (h - depth) 露出裙边
        face_h = h - depth - press
        face = QLinearGradient(0, 0, 0, face_h)
        face.setColorAt(0, QColor(pal["face_top"]))
        face.setColorAt(1, QColor(pal["face_bot"]))
        if self._hovered and not self._pressed:
            # 悬停时整体提亮一档（OMG 的 BG_CARD_HOVER 语义）
            face.setColorAt(0, QColor(pal["face_top"]).lighter(115))
            face.setColorAt(1, QColor(pal["face_bot"]).lighter(115))
        p.setBrush(face)
        p.drawRoundedRect(1, 1 + press, w - 2, face_h - 1, r - 1, r - 1)

        # 3) 顶部内高光：1px 亮线
        p.setPen(QPen(pal["hilite"], 1))
        p.drawLine(r, 2 + press, w - r, 2 + press)

        # 4) 发丝边框
        p.setPen(QPen(pal["border"], 1))
        p.drawRoundedRect(0.5, 0.5 + press, w - 1, h - 1 - press, r, r)

        # 5) 文本（与 _text_width 同一字体，保证尺寸计算与绘制一致）
        p.setFont(_font())
        if self._sub:
            p.setPen(pal["text"])
            p.drawText(QRectF(0, press, w, h * 0.58), Qt.AlignCenter, self._text)
            f2 = _font(FONT_SIZE - 2)
            p.setFont(f2)
            p.setPen(pal["text_muted"])
            p.drawText(QRectF(0, press + h * 0.42, w, h * 0.5),
                       Qt.AlignCenter, self._sub)
        else:
            p.setPen(pal["text"])
            p.drawText(QRectF(0, press, w, h), Qt.AlignCenter, self._text)
        p.end()


# ---------------------------------------------------------------------------
# 组合键行：「Alt + `」
# ---------------------------------------------------------------------------
class OMGKeyCapGroup(QWidget):
    """把若干键名渲染成一排键帽，中间自动插入弱化的 ``+`` 号。

    用法：``OMGKeyCapGroup(["Ctrl", "Shift", "S"])``。
    """

    def __init__(self, keys: Iterable[str], theme: str = "dark", parent=None):
        super().__init__(parent)
        self._theme = theme
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for i, k in enumerate(keys):
            if i:
                lay.addWidget(self._plus())
            lay.addWidget(OMGKeyCap(str(k), theme=theme))
        self.setFocusPolicy(Qt.NoFocus)

    def _plus(self) -> QLabel:
        lab = QLabel("+")
        lab.setStyleSheet(
            f"color:{_palette(self._theme)['text_muted'].name()};"
            "background:transparent;border:none;")
        lab.setFocusPolicy(Qt.NoFocus)
        return lab

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        for cap in self.findChildren(OMGKeyCap):
            cap.set_theme(theme)
        for lab in self.findChildren(QLabel):
            lab.setStyleSheet(
                f"color:{_palette(theme)['text_muted'].name()};"
                "background:transparent;border:none;")


def keycap_group_width(keys: List[str]) -> int:
    """估算一组键帽的横向占用（布局前的粗略值，仅用于预留空间）。"""
    if not keys:
        return 0
    total = sum(max(OMGKeyCap._text_width(str(k), None) + OMGKeyCap._PAD_X,
                    OMGKeyCap.MIN_WIDTH) for k in keys)
    return total + (len(keys) - 1) * (4 + 8)   # 间隔 + "+" 号
