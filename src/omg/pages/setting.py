"""
omg.pages.setting — 设置页「UI 布局」

本文件**只写界面**（窗口外壳、标题栏、侧边栏、卡片、路径页控件、样式），
不包含任何配置读写逻辑。行为（路径读取/写入、即时落盘、文件选择对话框）
收口在 omg.domain.settings.SettingsController。

分工：
  - 本文件提供 SettingsWindow / PathPage 等控件与样式；
  - PathPage 通过 Qt 信号把「文本改动 / 浏览点击」抛给外部；
  - SettingsWindow 在构建末尾调用 domain 的 SettingsController.bind(self)，
    由 domain 完成所有功能绑定（单向：pages → domain，无反向依赖）。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import QByteArray, Qt, QPoint, QSize, QRectF, Signal
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QBrush, QFont, QPixmap,
    QIntValidator,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget, QStackedWidget, QLineEdit,
    QScrollArea, QListWidget, QListWidgetItem, QPlainTextEdit,
)
from omg.core.window_behavior import (
    DEFAULT_IDLE_OPACITY, MAX_IDLE_OPACITY, MIN_IDLE_OPACITY,
    DEFAULT_LAUNCH_HOLD_MS, MAX_LAUNCH_HOLD_MS, MIN_LAUNCH_HOLD_MS,
)
from omg.ui.window_flags import window_flags  # noqa: E402  顶层 flags 单一真源
from omg.ui.window_geo import CenteredPopupMixin
from omg.ui.OMGPopCard import install_pop_cards
from omg.pages.guide.game_folder import attach_game_folder_guide


# ===========================================================================
# 21th 风格颜色 Token
# ===========================================================================
BG_WINDOW = "#1F1F21"
BG_CARD = "#2C2C2E"
BG_CARD_HOVER = "#38383B"
BG_CARD_PRESSED = "#444448"
BG_INPUT = "#1A1A1C"
BORDER = "#3A3A3C"
ACCENT = "#0A84FF"            # iOS 系统蓝（深色模式）
ACCENT_HOVER = "#409CFF"
ACCENT_PRESSED = "#0A6ED1"
SEPARATOR = "#3A3A3C"
TEXT_PRIMARY = "#F5F5F7"
TEXT_SECONDARY = "#98989D"
TEXT_ON_ACCENT = "#FFFFFF"      # Accent 填充上的文字（Primary 按钮 / 分段选中项）统一用白色
WARN = "#FF453A"                # iOS 系统红（深色模式）：非法输入 / 危险提示

RADIUS_WIN = 6
RADIUS_CTRL = 4
RADIUS_CARD = 6

# ---- 分段控件 / 开关 ----
RADIUS_SEG_TRACK = 6                         # 轨道圆角
RADIUS_SEG_BTN = 4                          # 按钮圆角
SEG_HEIGHT = 32                             # 控件总高（与行内控件一致）
SEG_MIN_BTN_H = 18                          # 按钮最小高
SEG_ACCENT_SOFT = "rgba(10,132,255,0.14)"   # 未选段悬停（半透明强调色）
TRACK_OFF = "#3A3A3C"                       # 开关：关闭态轨道色

# 窗口尺寸
WIN_W = 640
WIN_H = 360

# 标签栏
SIDEBAR_W = 120
SIDEBAR_ITEM_H = 36
SIDEBAR_INDICATOR_W = 3
SIDEBAR_INDICATOR_H = 20

# 分页定义：(显示名称, 左侧标识条颜色)
PAGES = [
    ("路径", ACCENT),
    ("GIMI", ACCENT),
    ("启动", ACCENT),
    ("OMG", ACCENT),
]


# ===========================================================================
# 图标工具（设置页与便捷构建页共用）
# ===========================================================================
_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "resources", "icons",
)

# 标题栏关闭按钮图标。该 SVG 的图形在 1024 viewBox 中约占 72%（四周各留 ~144），
# 故 iconSize 取 16 时图形实际约 11.5px —— 在 24×24 按钮内既有 4px 呼吸边、
# 又完整显示不裁切，视觉重量与之前的 starrail 图标（iconSize 14 ≈ 10.9px）相当。
CLOSE_ICON = "genshin_function_control_close.svg"
CLOSE_ICON_SIZE = 16

# 白名单增删按钮图标（位于 resources/icons/setting/，无 fill，按主题前景色着色以适配深色主题）
ADD_ICON = "setting/add.svg"
DELETE_ICON = "setting/delete.svg"
WHITELIST_ICON_SIZE = 16


def tinted_icon(filename: str, px: int, color: str) -> QIcon:
    """加载 resources/icons/ 下的 SVG，按指定颜色着色后渲染为 QIcon。

    项目里的功能图标 SVG 多数**不带 fill**（如 genshin_function_control_close.svg），
    默认会被渲染成黑色，在 OMGLite 的深色主题下几乎不可见。这里把颜色绑定到主题
    前景色（深色主题传 TEXT_PRIMARY 等浅色），从而实现深色适配；
    将来若切浅色主题，只需传入深色即可，无需改图标文件。

    委托给 omg.ui.icon_loader（优先 Qt 资源、回退磁盘，避免回归）。
    """
    from omg.ui.icon_loader import tinted_icon as _tinted_icon
    return _tinted_icon(filename, px, color)


# ===========================================================================
# QSS 样式表（无 BottomBar / 应用 / 取消 相关规则）
# ===========================================================================
SETTINGS_QSS = f"""
QWidget#SettingsRoot {{
    background: transparent;
}}
QFrame#TitleBar {{
    background: {BG_WINDOW};
    border: none;
    border-top-left-radius: {RADIUS_WIN}px;
    border-top-right-radius: {RADIUS_WIN}px;
}}
QLabel#TitleText {{
    color: {TEXT_PRIMARY};
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
QPushButton#TitleBtn {{
    background: transparent;
    border: none;
    padding: 0;
    border-radius: {RADIUS_CTRL}px;
    color: {TEXT_SECONDARY};
}}
QPushButton#TitleBtn:hover {{
    background: {BG_CARD_HOVER};
    color: {TEXT_PRIMARY};
}}
QPushButton#TitleBtn:pressed {{
    background: {BG_CARD_PRESSED};
    color: {TEXT_PRIMARY};
}}
QFrame#Sidebar {{
    background: {BG_WINDOW};
    border-right: 1px solid {BORDER};
}}
QFrame#ContentArea {{
    background: {BG_WINDOW};
    border: none;
}}
QFrame#SidebarItem {{
    background: transparent;
    border: none;
    border-radius: 0;
}}
QFrame#SidebarItem:hover {{
    background: {BG_CARD_HOVER};
}}
QLabel#ContentTitle {{
    color: {TEXT_PRIMARY};
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}}
QLabel#ContentDesc {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    background: transparent;
}}
QLabel#SectionTitle {{
    color: {TEXT_PRIMARY};
    font-size: 13px;
    font-weight: 700;
    background: transparent;
}}
QPushButton#ContentBtn {{
    background: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 6px 16px;
    font-size: 12px;
}}
QPushButton#ContentBtn:hover {{
    background: {BG_CARD_HOVER};
    border: 1px solid {ACCENT};
}}
QPushButton#ContentBtn:pressed {{
    background: {BG_CARD_PRESSED};
}}

/* ---- 卡片 ---- */
QFrame#Card {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD}px;
}}
QLabel#CardTitle {{
    color: {TEXT_PRIMARY};
    font-size: 12px;
    font-weight: 600;
    background: transparent;
}}
QLabel#FieldLabel {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
    background: transparent;
}}
QLabel#HintText {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    background: transparent;
}}

/* ---- 输入框 ---- */
QLineEdit {{
    background: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 5px 8px;
    font-size: 12px;
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit::placeholder {{
    color: {TEXT_SECONDARY};
}}
/* 非法输入态：由代码 setProperty("invalid", True/False) + style().polish() 切换 */
QLineEdit[invalid="true"] {{
    border: 1px solid {WARN};
    color: {WARN};
}}

/* ---- 浏览按钮 ---- */
QPushButton#BrowseBtn {{
    background: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 5px 12px;
    font-size: 12px;
}}
QPushButton#BrowseBtn:hover {{
    background: {BG_CARD_HOVER};
    border: 1px solid {ACCENT};
}}

/* ---- 图标按钮（白名单增删等，无文字、避免 padding 裁切图标）---- */
QPushButton#IconBtn {{
    background: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 0;
}}
QPushButton#IconBtn:hover {{
    background: {BG_CARD_HOVER};
    border: 1px solid {ACCENT};
}}

/* ---- 分隔线 ---- */
QFrame#HSeparator {{
    background: {SEPARATOR};
    border: none;
    max-height: 1px;
}}

/* ---- 分段控件：选中=Primary 填充 / 未选=Ghost（主题蓝） ---- */
QWidget#SegmentedControl {{
    background: transparent;
}}
QFrame#SegTrack {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SEG_TRACK}px;
}}
QPushButton#SegBtn {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_SEG_BTN}px;
    padding: 4px 14px;
    font-weight: 600;
    font-size: 12px;
    color: {TEXT_ON_ACCENT};
    min-height: {SEG_MIN_BTN_H}px;
}}
QPushButton#SegBtn:hover {{
    background: {SEG_ACCENT_SOFT};
}}
QPushButton#SegBtn[selected="true"] {{
    background: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}
QPushButton#SegBtn[selected="true"]:hover {{
    background: {ACCENT_HOVER};
}}

/* ---- 主要按钮（Primary）：Accent 填充 + 白色文字 ----
   设置页与便捷构建页共用同一套定义（便捷构建页的 QUICKBUILD_QSS 不再重复声明）。 */
QPushButton#PrimaryBtn {{
    background: {ACCENT};
    color: {TEXT_ON_ACCENT};
    border: none;
    border-radius: {RADIUS_CTRL}px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#PrimaryBtn:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#PrimaryBtn:pressed {{
    background: {ACCENT_PRESSED};
}}

/* ---- 页面内容容器：透明，并使用选择器避免级联覆盖子控件背景 ---- */
QWidget#PageContent {{
    background: transparent;
}}

/* ---- 列表 ---- */
QListWidget {{
    background: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    font-size: 12px;
    outline: none;
}}
QListWidget::item {{
    padding: 4px 8px;
    border-bottom: 1px solid {SEPARATOR};
}}
QListWidget::item:selected {{
    background: {BG_CARD_HOVER};
    color: {ACCENT};
}}

/* ---- 长文本输入框 ---- */
QPlainTextEdit {{
    background: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 5px 8px;
    font-size: 12px;
}}
QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}

/* ---- 滚动条 ---- */
QScrollBar:vertical {{
    background: {BG_WINDOW};
    width: 8px;
    border: none;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_SECONDARY};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: none;
}}
QScrollBar:horizontal {{
    background: {BG_WINDOW};
    height: 8px;
    border: none;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT_SECONDARY};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    height: 0px;
    background: none;
    border: none;
}}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: none;
}}
"""


# ===========================================================================
# 卡片容器
# ===========================================================================
class Card(QWidget):
    """带标题的卡片容器：标题在卡片外部上方，body 独立带卡片样式。"""

    def __init__(
        self,
        title: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(4)

        if title:
            self._title_label = QLabel(title, self)
            self._title_label.setObjectName("CardTitle")
            self._outer.addWidget(self._title_label)

        self._body = QFrame(self)
        self._body.setObjectName("Card")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(12, 10, 12, 10)
        self._body_layout.setSpacing(8)
        self._outer.addWidget(self._body)

    @property
    def body_layout(self) -> QVBoxLayout:
        return self._body_layout


# ===========================================================================
# 水平分隔线
# ===========================================================================
class HSeparator(QFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("HSeparator")
        self.setFixedHeight(1)


# ===========================================================================
# 分段控件（选中=Primary 填充 / 未选=Ghost）
# ===========================================================================
class SegmentedControl(QWidget):
    """圆角矩形容器：选中段 = Primary 填充（ACCENT + 白色文字），未选段 = Ghost（白色文字）。

    结构沿用 Lab/stylekit.py 的分段控件规则，配色与几何适配本页深色主题：
    轨道用沉底色（BG_INPUT）+ 主题边框发丝线，整体高度与行内控件一致（32px）。
    通过 currentChanged(int) 抛出选中变化，供外部绑定。
    """

    currentChanged = Signal(int)

    def __init__(self, items: list[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("SegmentedControl")
        self._items = list(items)
        self._index = 0

        self._track = QFrame(self)
        self._track.setObjectName("SegTrack")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        self._btns: list[QPushButton] = []
        for i, name in enumerate(self._items):
            b = QPushButton(str(name))
            b.setObjectName("SegBtn")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, idx=i: self.setCurrent(idx))
            self._btns.append(b)
            lay.addWidget(b, alignment=Qt.AlignVCenter)
        self.setFixedHeight(SEG_HEIGHT)
        self._refresh()

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        self._track.setGeometry(self.rect())
        super().resizeEvent(e)

    def setCurrent(self, i: int) -> None:
        if i == self._index or not (0 <= i < len(self._btns)):
            return
        self._index = i
        self._refresh()
        self.currentChanged.emit(i)

    def currentIndex(self) -> int:
        return self._index

    def _refresh(self) -> None:
        for i, b in enumerate(self._btns):
            b.setProperty("selected", "true" if i == self._index else "false")
            if b.style():
                b.style().polish(b)


# ===========================================================================
# 开关（圆角轨道 + 白色旋钮）
# ===========================================================================
class Switch(QWidget):
    """iOS 风格开关：开启时轨道使用强调色，关闭时使用轨道灰。"""

    toggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._on = False
        self.setFixedSize(40, 22)
        self.setCursor(Qt.PointingHandCursor)

    @property
    def on(self) -> bool:
        return self._on

    def setOn(self, value: bool) -> None:
        if self._on == value:
            return
        self._on = value
        self.update()
        self.toggled.emit(self._on)

    def toggle(self) -> None:
        self.setOn(not self._on)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(e)

    def paintEvent(self, e) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        h = r.height()
        track_r = h / 2
        pad = 3
        knob_d = h - 2 * pad

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT if self._on else TRACK_OFF))
        p.drawRoundedRect(0, 0, r.width(), r.height(), track_r, track_r)

        kx = pad + (r.width() - 2 * pad - knob_d) if self._on else pad
        ky = pad
        p.setBrush(QColor(0, 0, 0, 38))
        p.drawEllipse(QRectF(kx + 0.5, ky + 1.0, knob_d, knob_d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(kx, ky, knob_d, knob_d))


# ===========================================================================
# iOS 风格滑块（细轨道 + 白色旋钮，与 Switch 同一套视觉语言）
# ===========================================================================
SLIDER_TRACK_H = 4          # 轨道高度
SLIDER_KNOB_D = 18          # 旋钮直径


class IOSSlider(QWidget):
    """iOS 风格滑块：4px 圆角轨道（已填充段 = ACCENT）+ 18px 白色旋钮。

    交互语义（对齐 iOS 设置页滑块）：
      - 按下 / 拖动 → ``valueChanged(int)`` 实时连发（供输入框联动预览，不落盘）；
      - 松手 → ``committed(int)`` 一次（domain 据此落盘，避免拖动过程写盘风暴）；
      - 键盘 ←/→/↑/↓ → 步进 ±1，同样走 valueChanged + committed。

    回填用 :meth:`setValue(v, block=True)`（不发信号，避免触发写盘）。
    """

    valueChanged = Signal(int)
    committed = Signal(int)

    def __init__(
        self,
        minimum: int = 0,
        maximum: int = 100,
        value: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._min = int(minimum)
        self._max = int(maximum)
        self._value = self._clamp(int(value))
        self._dragging = False
        self.setFixedHeight(22)          # 与 Switch 同高，行内对齐
        self.setMinimumWidth(140)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    # ---- 值域 ----
    def _clamp(self, v: int) -> int:
        return max(self._min, min(self._max, v))

    def value(self) -> int:
        return self._value

    def minimum(self) -> int:
        return self._min

    def maximum(self) -> int:
        return self._max

    def setValue(self, v: int, block: bool = False) -> None:
        n = self._clamp(int(v))
        if n == self._value:
            return
        self._value = n
        self.update()
        if not block:
            self.valueChanged.emit(self._value)

    # ---- 坐标 ↔ 值 ----
    def _x_to_value(self, x: float) -> int:
        usable = max(self.width() - SLIDER_KNOB_D, 1)
        t = (x - SLIDER_KNOB_D / 2.0) / usable
        t = max(0.0, min(1.0, t))
        return self._min + round(t * (self._max - self._min))

    def _value_to_knob_x(self) -> float:
        usable = max(self.width() - SLIDER_KNOB_D, 1)
        t = (self._value - self._min) / float(self._max - self._min or 1)
        return t * usable

    # ---- 交互 ----
    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self.setValue(self._x_to_value(e.position().x()))
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        if self._dragging:
            self.setValue(self._x_to_value(e.position().x()))
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.committed.emit(self._value)
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:  # type: ignore[override]
        key = e.key()
        if key in (Qt.Key_Left, Qt.Key_Down, Qt.Key_Right, Qt.Key_Up):
            old = self._value
            step = -1 if key in (Qt.Key_Left, Qt.Key_Down) else 1
            self.setValue(self._value + step)
            if self._value != old:
                self.committed.emit(self._value)
        else:
            super().keyPressEvent(e)

    # ---- 绘制 ----
    def paintEvent(self, e) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        cy = r.height() / 2.0
        track_r = SLIDER_TRACK_H / 2.0
        knob_d = SLIDER_KNOB_D
        kx = self._value_to_knob_x()

        p.setPen(Qt.NoPen)
        # 轨道：未填充段
        p.setBrush(QColor(TRACK_OFF))
        p.drawRoundedRect(QRectF(0, cy - track_r, r.width(), SLIDER_TRACK_H),
                          track_r, track_r)
        # 轨道：已填充段（至旋钮中心）
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(0, cy - track_r, kx + knob_d / 2.0, SLIDER_TRACK_H),
                          track_r, track_r)
        # 旋钮：投影 + 白色圆
        p.setBrush(QColor(0, 0, 0, 38))
        p.drawEllipse(QRectF(kx + 0.5, cy - knob_d / 2.0 + 1.0, knob_d, knob_d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(kx, cy - knob_d / 2.0, knob_d, knob_d))


# ===========================================================================
# 侧边栏标签项
# ===========================================================================
class SidebarItem(QFrame):
    """单个分页标签项：左侧绘制选中标识条，右侧文字。"""

    def __init__(
        self,
        label: str,
        indicator_color: str,
        index: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarItem")
        self.setFixedHeight(SIDEBAR_ITEM_H)
        self.setCursor(Qt.PointingHandCursor)
        self._label = label
        self._indicator_color = QColor(indicator_color)
        self._index = index
        self._selected = False

    @property
    def index(self) -> int:
        return self._index

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            sidebar = self.parent()
            if sidebar is not None:
                sidebar._on_item_clicked(self._index)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        if self._selected:
            bg_path = QPainterPath()
            bg_path.addRoundedRect(
                4.0, 2.0, w - 8.0, h - 4.0,
                float(RADIUS_CTRL), float(RADIUS_CTRL)
            )
            p.fillPath(bg_path, QBrush(QColor(BG_CARD)))

            ix = 8.0
            iy = (h - SIDEBAR_INDICATOR_H) / 2
            ind_path = QPainterPath()
            ind_path.addRoundedRect(
                ix, iy,
                float(SIDEBAR_INDICATOR_W),
                float(SIDEBAR_INDICATOR_H),
                float(SIDEBAR_INDICATOR_W) / 2,
                float(SIDEBAR_INDICATOR_W) / 2,
            )
            p.fillPath(ind_path, QBrush(self._indicator_color))

        text_x = 18.0 if self._selected else 14.0
        text_color = QColor(TEXT_PRIMARY) if self._selected else QColor(TEXT_SECONDARY)
        p.setPen(QPen(text_color))
        font = QFont()
        font.setPointSize(9)
        if self._selected:
            font.setBold(True)
        p.setFont(font)
        p.drawText(
            QRectF(text_x, 0, w - text_x - 8, h),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._label,
        )
        p.end()


# ===========================================================================
# 侧边栏容器
# ===========================================================================
class Sidebar(QFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_W)

        self._items: list[SidebarItem] = []
        self._current_index = 0
        self._on_select_callback = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignTop)

        for i, (name, color) in enumerate(PAGES):
            item = SidebarItem(name, color, i, self)
            self._items.append(item)
            layout.addWidget(item)

        layout.addStretch()
        self._update_selection()

    def set_on_select(self, callback) -> None:
        self._on_select_callback = callback

    def _on_item_clicked(self, index: int) -> None:
        self._current_index = index
        self._update_selection()
        if self._on_select_callback:
            self._on_select_callback(index)

    def _update_selection(self) -> None:
        for i, item in enumerate(self._items):
            item.set_selected(i == self._current_index)


# ===========================================================================
# 路径页（UI；功能在 omg.domain.settings.SettingsController）
# ===========================================================================
class PathPage(QWidget):
    """路径配置页 UI：游戏路径 + GIMI 路径。

    本页只负责「画出控件」。交互通过信号抛出：
      - game_path_changed(str) / gimi_path_changed(str)：文本框内容变更；
      - browse_game_requested() / browse_gimi_requested()：点击「浏览」按钮。
    具体行为（读 config、即时落盘、打开对话框）由 domain 的 SettingsController 绑定实现。
    """

    game_path_changed = Signal(str)
    gimi_path_changed = Signal(str)
    browse_game_requested = Signal()
    browse_gimi_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        card = Card("路径配置", self)
        body = card.body_layout

        # ---------------- 游戏路径 ----------------
        row_game = QHBoxLayout()
        row_game.setSpacing(8)
        lbl_game = QLabel("游戏", self)
        lbl_game.setObjectName("FieldLabel")
        lbl_game.setFixedWidth(40)
        row_game.addWidget(lbl_game)

        # 「游戏」悬浮指引卡（动图 + 说明文字）。必须持有引用，否则构造结束后
        # 控制器与 QMovie 会被回收，卡片内容变空白 / 动图不播。
        self._game_guide = attach_game_folder_guide(lbl_game)

        self._edit_game = QLineEdit(self)
        self._edit_game.setPlaceholderText("选择游戏所在文件夹...")
        self._edit_game.textChanged.connect(self.game_path_changed)
        row_game.addWidget(self._edit_game, 1)

        btn_game = QPushButton("浏览", self)
        btn_game.setObjectName("BrowseBtn")
        btn_game.setCursor(Qt.PointingHandCursor)
        btn_game.clicked.connect(self.browse_game_requested)
        row_game.addWidget(btn_game)
        body.addLayout(row_game)

        # ---------------- GIMI 路径 ----------------
        row_gimi = QHBoxLayout()
        row_gimi.setSpacing(8)
        lbl_gimi = QLabel("GIMI", self)
        lbl_gimi.setObjectName("FieldLabel")
        lbl_gimi.setFixedWidth(40)
        row_gimi.addWidget(lbl_gimi)

        self._edit_gimi = QLineEdit(self)
        self._edit_gimi.setPlaceholderText("选择 GIMI 目录...")
        self._edit_gimi.textChanged.connect(self.gimi_path_changed)
        row_gimi.addWidget(self._edit_gimi, 1)

        btn_gimi = QPushButton("浏览", self)
        btn_gimi.setObjectName("BrowseBtn")
        btn_gimi.setCursor(Qt.PointingHandCursor)
        btn_gimi.clicked.connect(self.browse_gimi_requested)
        row_gimi.addWidget(btn_gimi)
        body.addLayout(row_gimi)

        layout.addWidget(card)

        layout.addStretch()

    # ------------------------------------------------------------------
    # 公开接口（供 domain.SettingsController 读写）
    # ------------------------------------------------------------------
    def set_game_path(self, text: str) -> None:
        self._edit_game.setText(text or "")

    def get_game_path(self) -> str:
        return self._edit_game.text().strip()

    def set_gimi_path(self, text: str) -> None:
        self._edit_gimi.setText(text or "")

    def get_gimi_path(self) -> str:
        return self._edit_gimi.text().strip()


# ===========================================================================
# GIMI 页（UI；功能由 omg.domain.settings.SettingsController 后续绑定）
# ===========================================================================
class GimiPage(QWidget):
    """GIMI 配置页 UI：d3dx.ini 卡片（Hunting / Warning 分段）+ 更新卡片（检查开关 + 白名单）。

    本页只负责「画出控件」，交互通过信号抛出：
      - hunting_changed(int) / warning_changed(int)：分段控件选中变化；
      - autocheck_changed(bool)：更新检查开关变化；
      - whitelist_changed(list)：删除白名单项后抛出；
      - add_file_requested()：点击「+」请求添加文件（由 domain 打开文件选择窗口）。
    具体行为（读写 config）由 domain 的 SettingsController 绑定实现。
    """

    hunting_changed = Signal(int)
    warning_changed = Signal(int)
    autocheck_changed = Signal(bool)
    whitelist_changed = Signal(list)   # 更新白名单变更（删后）
    add_file_requested = Signal()      # 点击「+」：请求添加白名单文件（由 domain 打开文件选择窗口）

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        content = QWidget()
        # 必须带选择器：无选择器的样式表会级联到所有子控件并覆盖应用级 QSS
        content.setObjectName("PageContent")
        content.setStyleSheet("QWidget#PageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ---- 卡片1：d3dx.ini ----
        card_d3dx = Card("d3dx.ini", self)
        body1 = card_d3dx.body_layout

        # Hunting 分段控件（右侧对齐）
        row_hunt = QHBoxLayout()
        row_hunt.setSpacing(8)
        lbl_hunt = QLabel("Hunting", content)
        lbl_hunt.setObjectName("FieldLabel")
        lbl_hunt.setFixedWidth(60)
        row_hunt.addWidget(lbl_hunt)
        row_hunt.addStretch()
        self._seg_hunting = SegmentedControl(["0", "1", "2"], content)
        self._seg_hunting.currentChanged.connect(self.hunting_changed)
        row_hunt.addWidget(self._seg_hunting)
        body1.addLayout(row_hunt)

        body1.addWidget(HSeparator(content))

        # Warning 分段控件（右侧对齐）
        row_warn = QHBoxLayout()
        row_warn.setSpacing(8)
        lbl_warn = QLabel("Warning", content)
        lbl_warn.setObjectName("FieldLabel")
        lbl_warn.setFixedWidth(60)
        row_warn.addWidget(lbl_warn)
        row_warn.addStretch()
        self._seg_warning = SegmentedControl(["0", "1"], content)
        self._seg_warning.currentChanged.connect(self.warning_changed)
        row_warn.addWidget(self._seg_warning)
        body1.addLayout(row_warn)

        layout.addWidget(card_d3dx)

        # ---- 卡片2：更新 ----
        card_update = Card("更新", self)
        body2 = card_update.body_layout

        # 检查更新开关
        row_check = QHBoxLayout()
        row_check.setSpacing(8)
        lbl_check = QLabel("打开 OMG 时检查更新", content)
        lbl_check.setObjectName("FieldLabel")
        row_check.addWidget(lbl_check)
        row_check.addStretch()
        self._switch_check = Switch(content)
        self._switch_check.toggled.connect(self.autocheck_changed)
        row_check.addWidget(self._switch_check)
        body2.addLayout(row_check)

        body2.addWidget(HSeparator(content))

        # 更新白名单
        lbl_whitelist = QLabel("更新白名单", content)
        lbl_whitelist.setObjectName("FieldLabel")
        body2.addWidget(lbl_whitelist)

        # 列表 + 增删按钮
        row_list = QHBoxLayout()
        row_list.setSpacing(6)

        self._whitelist = QListWidget(content)
        self._whitelist.setFixedHeight(80)
        # 再次点击已选中的项 → 取消选中（见 _on_whitelist_clicked）
        self._whitelist.itemClicked.connect(self._on_whitelist_clicked)
        self._whitelist_sel_row = -1     # 上次点击选中的行
        row_list.addWidget(self._whitelist, 1)

        col_btns = QVBoxLayout()
        col_btns.setSpacing(4)
        btn_add = QPushButton(content)
        btn_add.setObjectName("IconBtn")
        btn_add.setFixedSize(28, 28)
        btn_add.setIcon(tinted_icon(ADD_ICON, WHITELIST_ICON_SIZE, TEXT_PRIMARY))
        btn_add.setIconSize(QSize(WHITELIST_ICON_SIZE, WHITELIST_ICON_SIZE))
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.setToolTip("添加白名单文件")
        btn_add.clicked.connect(self.add_file_requested)
        col_btns.addWidget(btn_add)

        btn_del = QPushButton(content)
        btn_del.setObjectName("IconBtn")
        btn_del.setFixedSize(28, 28)
        btn_del.setIcon(tinted_icon(DELETE_ICON, WHITELIST_ICON_SIZE, TEXT_PRIMARY))
        btn_del.setIconSize(QSize(WHITELIST_ICON_SIZE, WHITELIST_ICON_SIZE))
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.setToolTip("删除选中的白名单文件")
        btn_del.clicked.connect(self._on_del_item)
        col_btns.addWidget(btn_del)

        col_btns.addStretch()
        row_list.addLayout(col_btns)
        body2.addLayout(row_list)

        layout.addWidget(card_update)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_del_item(self) -> None:
        row = self._whitelist.currentRow()
        if row >= 0:
            self._whitelist.takeItem(row)
            self._whitelist_sel_row = -1
            self.whitelist_changed.emit(self.get_whitelist_items())

    def _on_whitelist_clicked(self, item: QListWidgetItem) -> None:
        """再次点击已选中的项 → 取消选中。

        Qt 默认点击只切换 / 保持选中，无法「点掉」。这里记录上次点击的行，
        若本次点击的就是它，则清空选中（删除按钮随之失效，符合直觉）。
        仅影响选中态，不改变列表内容，因此不抛 whitelist_changed。
        """
        if item is None:
            return
        row = self._whitelist.row(item)
        if row == self._whitelist_sel_row:
            self._whitelist.setCurrentRow(-1)
            self._whitelist.clearSelection()
            self._whitelist_sel_row = -1
        else:
            self._whitelist_sel_row = row

    # ------------------------------------------------------------------
    # 公开接口（供 domain.SettingsController 读写）
    # ------------------------------------------------------------------
    def set_hunting(self, index: int) -> None:
        self._seg_hunting.setCurrent(index)

    def get_hunting(self) -> int:
        return self._seg_hunting.currentIndex()

    def set_warning(self, index: int) -> None:
        self._seg_warning.setCurrent(index)

    def get_warning(self) -> int:
        return self._seg_warning.currentIndex()

    def set_autocheck(self, value: bool) -> None:
        self._switch_check.setOn(value)

    def get_autocheck(self) -> bool:
        return self._switch_check.on

    def get_whitelist_items(self) -> list:
        """返回当前白名单文本列表（按显示顺序）。"""
        return [
            self._whitelist.item(i).text()
            for i in range(self._whitelist.count())
        ]

    def set_whitelist(self, items) -> None:
        """用给定列表回填白名单（绑定 / 启动时调用，不触发 whitelist_changed）。"""
        self._whitelist.clear()
        self._whitelist_sel_row = -1
        for it in (items or []):
            self._whitelist.addItem(QListWidgetItem(str(it)))


# ===========================================================================
# 启动页（UI；功能由 omg.domain.settings.SettingsController 后续绑定）
# ===========================================================================
# 自定义启动命令输入框的详细帮助（由 OMGTips 以多行纯文本气泡呈现）
_TIP_CUSTOM_LAUNCH_CMD = (
    "自定义启动命令 · 填写说明\n"
    "────────────────────\n"
    "用途：整条作为 Windows 命令行执行，\n"
    "覆盖默认启动游戏 exe（仅本开关打开时生效）。\n"
    "适合用自定义启动器 / 批处理 / 带参命令行启动。\n"
    "\n"
    "格式：可执行路径 + 参数，以空格分隔；\n"
    "路径含空格须用英文双引号 \" 包裹。\n"
    "支持 .exe / .bat / .cmd，可用 %环境变量% 与 start。\n"
    "\n"
    "长度：建议 ≤ 8191 字符（Windows 命令行上限）。\n"
    "\n"
    "示例 1（无空格路径，引号可省略）：\n"
    "D:\\Games\\Genshin Impact\\launcher.exe\n"
    "\n"
    "示例 2（运行批处理并传参）：\n"
    "D:\\Tools\\start_game.bat --account test\n"
    "\n"
    "示例 3（含空格路径必须用引号包裹）：\n"
    "\"D:\\My Games\\Genshin Impact\\GenshinImpact.exe\" -windowed -noborder"
)


class StartupPage(QWidget):
    """启动配置页 UI：启动方法、自定义启动、注入库。

    交互通过信号抛出：
      - method_changed(int)：启动方法分段变化；
      - custom_toggled(bool) / custom_cmd_changed(str)：自定义启动开关与命令；
      - inject_method_changed(int)：注入方式分段变化；
      - lib_toggled(bool) / lib_text_changed(str)：注入库开关与文本。
    """

    method_changed = Signal(int)
    custom_toggled = Signal(bool)
    custom_cmd_changed = Signal(str)
    inject_method_changed = Signal(int)
    lib_toggled = Signal(bool)
    lib_text_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        content = QWidget()
        content.setObjectName("PageContent")
        content.setStyleSheet("QWidget#PageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ---- 卡片1：启动方法 ----
        card_method = Card("启动方法", self)
        body_m = card_method.body_layout

        row_method = QHBoxLayout()
        row_method.setSpacing(8)
        lbl_method = QLabel("启动方法", content)
        lbl_method.setObjectName("FieldLabel")
        lbl_method.setFixedWidth(60)
        row_method.addWidget(lbl_method)
        row_method.addStretch()
        self._seg_method = SegmentedControl(["Native", "Shell", "Manual"], content)
        self._seg_method.currentChanged.connect(self.method_changed)
        row_method.addWidget(self._seg_method)
        body_m.addLayout(row_method)

        layout.addWidget(card_method)

        # ---- 高级 分组标题 ----
        lbl_advanced = QLabel("高级", content)
        lbl_advanced.setObjectName("SectionTitle")
        layout.addWidget(lbl_advanced)

        # ---- 卡片2：自定义启动（响应式）----
        card_custom = Card(None, self)
        body_c = card_custom.body_layout

        # 标题行：标签 + 开关（标签为普通字段标签，非卡片标题）
        row_custom_head = QHBoxLayout()
        row_custom_head.setSpacing(8)
        lbl_custom = QLabel("自定义启动", content)
        lbl_custom.setObjectName("FieldLabel")
        row_custom_head.addWidget(lbl_custom)
        row_custom_head.addStretch()
        self._switch_custom = Switch(content)
        self._switch_custom.toggled.connect(self._on_custom_toggled)
        row_custom_head.addWidget(self._switch_custom)
        body_c.addLayout(row_custom_head)

        # 折叠区（开关打开时显示）
        self._body_custom = QWidget(content)
        lay_body_custom = QVBoxLayout(self._body_custom)
        lay_body_custom.setContentsMargins(0, 8, 0, 0)
        lay_body_custom.setSpacing(8)

        self._edit_custom_cmd = QLineEdit(content)
        self._edit_custom_cmd.setPlaceholderText("自定义启动命令 / 参数...")
        self._edit_custom_cmd.setToolTip(_TIP_CUSTOM_LAUNCH_CMD)
        # 交互型提示（OMGPopCard.HOVER_INTERACTIVE）：鼠标移入卡片不隐藏，
        # 可自由拖选 / 复制说明文字；移出「输入框 + 卡片」、点击卡外或按
        # Esc 时才关闭。tipInteractive 隐含可选中（tipSelectable）。
        self._edit_custom_cmd.setProperty("tipInteractive", True)
        self._edit_custom_cmd.textChanged.connect(self.custom_cmd_changed)
        lay_body_custom.addWidget(self._edit_custom_cmd)

        lay_body_custom.addWidget(HSeparator(content))

        row_inject = QHBoxLayout()
        row_inject.setSpacing(8)
        lbl_inject = QLabel("注入方式", content)
        lbl_inject.setObjectName("FieldLabel")
        lbl_inject.setFixedWidth(60)
        row_inject.addWidget(lbl_inject)
        row_inject.addStretch()
        self._seg_inject = SegmentedControl(["Hook", "Inject", "Bypass"], content)
        self._seg_inject.currentChanged.connect(self.inject_method_changed)
        row_inject.addWidget(self._seg_inject)
        lay_body_custom.addLayout(row_inject)

        self._body_custom.setVisible(False)
        body_c.addWidget(self._body_custom)

        layout.addWidget(card_custom)

        # ---- 卡片3：注入库（响应式）----
        card_lib = Card(None, self)
        body_l = card_lib.body_layout

        row_lib_head = QHBoxLayout()
        row_lib_head.setSpacing(8)
        lbl_lib = QLabel("注入库", content)
        lbl_lib.setObjectName("FieldLabel")
        row_lib_head.addWidget(lbl_lib)
        row_lib_head.addStretch()
        self._switch_lib = Switch(content)
        self._switch_lib.toggled.connect(self._on_lib_toggled)
        row_lib_head.addWidget(self._switch_lib)
        body_l.addLayout(row_lib_head)

        # 折叠区（开关打开时显示）
        self._body_lib = QWidget(content)
        lay_body_lib = QVBoxLayout(self._body_lib)
        lay_body_lib.setContentsMargins(0, 8, 0, 0)
        lay_body_lib.setSpacing(8)

        self._edit_lib = QPlainTextEdit(content)
        self._edit_lib.setPlaceholderText("每行一个注入库路径 / 名称...")
        self._edit_lib.setFixedHeight(90)
        self._edit_lib.textChanged.connect(
            lambda: self.lib_text_changed.emit(self._edit_lib.toPlainText())
        )
        lay_body_lib.addWidget(self._edit_lib)

        self._body_lib.setVisible(False)
        body_l.addWidget(self._body_lib)

        layout.addWidget(card_lib)

        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_custom_toggled(self, on: bool) -> None:
        self._body_custom.setVisible(on)
        self.custom_toggled.emit(on)

    def _on_lib_toggled(self, on: bool) -> None:
        self._body_lib.setVisible(on)
        self.lib_toggled.emit(on)

    # ------------------------------------------------------------------
    # 公开接口（供 domain.SettingsController 读写）
    # ------------------------------------------------------------------
    def set_method(self, index: int) -> None:
        self._seg_method.setCurrent(index)

    def get_method(self) -> int:
        return self._seg_method.currentIndex()

    def set_custom(self, value: bool) -> None:
        self._switch_custom.setOn(value)

    def get_custom(self) -> bool:
        return self._switch_custom.on

    def set_custom_cmd(self, text: str) -> None:
        self._edit_custom_cmd.setText(text or "")

    def get_custom_cmd(self) -> str:
        return self._edit_custom_cmd.text().strip()

    def set_inject_method(self, index: int) -> None:
        self._seg_inject.setCurrent(index)

    def get_inject_method(self) -> int:
        return self._seg_inject.currentIndex()

    def set_lib(self, value: bool) -> None:
        self._switch_lib.setOn(value)

    def get_lib(self) -> bool:
        return self._switch_lib.on

    def set_lib_text(self, text: str) -> None:
        self._edit_lib.setPlainText(text or "")

    def get_lib_text(self) -> str:
        return self._edit_lib.toPlainText().strip()


# ===========================================================================
# OMG 页（选项卡片：默认置顶 / 闲置时透明度）
# ===========================================================================
class OmgPage(QWidget):
    """OMG 页 UI：「选项」卡片。

    控件与持久化键（见 omg.core.window_behavior）：
      - ``omg_always_on_top``（默认置顶，1/0）：启动时主窗口自动置顶；
      - ``omg_idle_opacity``（闲置时透明度，20~100）：失去焦点且未悬浮时
        窗口降至该不透明度，聚焦 / 悬浮时恢复不透明（100 = 不变淡）；
      - ``omg_launch_hold_enabled``（启动按钮防误触，1/0）：开启后启动按钮
        不再是「点按即执行」，而是「按住达到设定时间后执行」，期间松开不执行；
      - ``omg_launch_hold_ms``（长按时间，200~3000）：上述长按时长（ms）。

    本页只画控件，交互通过信号抛出，读写 config 由 domain.SettingsController 绑定：
      - always_on_top_toggled(bool)：置顶开关变化；
      - idle_opacity_changed(int)：透明度提交（仅在合法且与上次不同时发出；
        滑块**松手**才提交，输入框回车 / 失焦才提交，拖动过程只联动预览）；
      - launch_hold_toggled(bool)：防误触开关变化；
      - launch_hold_ms_changed(int)：长按时间提交（回车 / 失焦才提交）。
    """

    always_on_top_toggled = Signal(bool)
    idle_opacity_changed = Signal(int)
    launch_hold_toggled = Signal(bool)
    launch_hold_ms_changed = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        content = QWidget()
        content.setObjectName("PageContent")
        content.setStyleSheet("QWidget#PageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # ---- 卡片：选项 ----
        self.card_options = Card("选项", self)
        body = self.card_options.body_layout

        # 行1：默认置顶开关
        row_pin = QHBoxLayout()
        row_pin.setSpacing(8)
        col_pin = QVBoxLayout()
        col_pin.setSpacing(2)
        lbl_pin = QLabel("默认置顶", content)
        lbl_pin.setObjectName("FieldLabel")
        desc_pin = QLabel(
            "启动时主窗口自动置顶；运行中仍可用菜单「置顶 / 取消置顶」临时切换。"
            "该设置只决定下次启动的初始状态。", content,
        )
        desc_pin.setObjectName("HintText")
        desc_pin.setWordWrap(True)
        col_pin.addWidget(lbl_pin)
        col_pin.addWidget(desc_pin)
        row_pin.addLayout(col_pin, 1)
        self._switch_pin = Switch(content)
        row_pin.addWidget(self._switch_pin, alignment=Qt.AlignVCenter)
        body.addLayout(row_pin)

        body.addWidget(HSeparator(content))

        # 行2：闲置时透明度（滑块 + 输入框联动）
        col_op = QVBoxLayout()
        col_op.setSpacing(2)
        lbl_op = QLabel("闲置时透明度", content)
        lbl_op.setObjectName("FieldLabel")
        desc_op = QLabel(
            "失去焦点且未悬浮时窗口降至该不透明度，聚焦或悬浮时恢复不透明"
            f"（{MIN_IDLE_OPACITY}–{MAX_IDLE_OPACITY}，{MAX_IDLE_OPACITY} = 不变淡）。"
            "调整后下一次焦点 / 悬浮变化即生效。", content,
        )
        desc_op.setObjectName("HintText")
        desc_op.setWordWrap(True)
        col_op.addWidget(lbl_op)
        col_op.addWidget(desc_op)

        row_ctrl = QHBoxLayout()
        row_ctrl.setSpacing(8)
        self._edit_opacity = QLineEdit(content)
        self._edit_opacity.setFixedWidth(56)
        self._edit_opacity.setAlignment(Qt.AlignCenter)
        # validator 只挡非数字（0~999 放行）：若校验过严，输入中间态会被判
        # intermediate，回车 / 失焦不触发 editingFinished，业务层就拦不到它。
        # 0~100 的值域校验由 _on_opacity_edited 做。
        self._edit_opacity.setValidator(QIntValidator(0, 999, self._edit_opacity))
        row_ctrl.addWidget(self._edit_opacity, alignment=Qt.AlignVCenter)
        unit = QLabel("%", content)
        unit.setObjectName("HintText")
        row_ctrl.addWidget(unit, alignment=Qt.AlignVCenter)
        col_op.addLayout(row_ctrl)
        body.addLayout(col_op)

        # 非法输入提示（校验失败时显示，输入合法后自动隐藏）
        self._warn = QLabel("", content)
        self._warn.setObjectName("HintText")
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet(
            f"QLabel#HintText {{ color: {WARN}; background: transparent; }}"
        )
        self._warn.setVisible(False)
        body.addWidget(self._warn)

        # ---- 启动按钮防误触（开关 + 长按时间）----
        body.addWidget(HSeparator(content))

        # 行3：启动按钮防误触开关
        row_hold = QHBoxLayout()
        row_hold.setSpacing(8)
        col_hold = QVBoxLayout()
        col_hold.setSpacing(2)
        lbl_hold = QLabel("启动按钮防误触", content)
        lbl_hold.setObjectName("FieldLabel")
        desc_hold = QLabel(
            "开启后，启动按钮不再「点按即执行」，而是「按住直到达到设定时间才执行」；"
            "按住期间松开鼠标则不执行启动，避免误触。", content,
        )
        desc_hold.setObjectName("HintText")
        desc_hold.setWordWrap(True)
        col_hold.addWidget(lbl_hold)
        col_hold.addWidget(desc_hold)
        row_hold.addLayout(col_hold, 1)
        self._switch_hold = Switch(content)
        row_hold.addWidget(self._switch_hold, alignment=Qt.AlignVCenter)
        body.addLayout(row_hold)

        # 行4：长按时间（仅输入框，无滑块）；开关关闭时禁用
        col_hold_ms = QVBoxLayout()
        col_hold_ms.setSpacing(2)
        lbl_hold_ms = QLabel("长按时间", content)
        lbl_hold_ms.setObjectName("FieldLabel")
        desc_hold_ms = QLabel(
            f"需按住启动按钮达到该时长（{MIN_LAUNCH_HOLD_MS}–{MAX_LAUNCH_HOLD_MS} 毫秒）"
            "才会执行启动。", content,
        )
        desc_hold_ms.setObjectName("HintText")
        desc_hold_ms.setWordWrap(True)
        col_hold_ms.addWidget(lbl_hold_ms)
        col_hold_ms.addWidget(desc_hold_ms)

        row_hold_ms = QHBoxLayout()
        row_hold_ms.setSpacing(8)
        self._edit_hold_ms = QLineEdit(content)
        self._edit_hold_ms.setFixedWidth(64)
        self._edit_hold_ms.setAlignment(Qt.AlignCenter)
        # 同透明度输入：validator 只挡非数字（0~9999 放行），值域校验放 handler
        self._edit_hold_ms.setValidator(QIntValidator(0, 9999, self._edit_hold_ms))
        row_hold_ms.addWidget(self._edit_hold_ms, alignment=Qt.AlignVCenter)
        unit_ms = QLabel("ms", content)
        unit_ms.setObjectName("HintText")
        row_hold_ms.addWidget(unit_ms, alignment=Qt.AlignVCenter)
        col_hold_ms.addLayout(row_hold_ms)
        body.addLayout(col_hold_ms)

        self._warn_hold = QLabel("", content)
        self._warn_hold.setObjectName("HintText")
        self._warn_hold.setWordWrap(True)
        self._warn_hold.setStyleSheet(
            f"QLabel#HintText {{ color: {WARN}; background: transparent; }}"
        )
        self._warn_hold.setVisible(False)
        self._hold_ms_enabled = True  # 由 _sync_hold_enabled 维护
        body.addWidget(self._warn_hold)

        layout.addWidget(self.card_options)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        # 内部联动：输入框提交 → 抛 idle_opacity_changed 给 domain 落盘
        self._edit_opacity.editingFinished.connect(self._on_opacity_edited)
        self._switch_pin.toggled.connect(self._on_pin_toggled)

        # 防误触：开关 / 长按时间输入框 联动
        self._switch_hold.toggled.connect(self._on_hold_toggled)
        self._edit_hold_ms.editingFinished.connect(self._on_hold_ms_edited)

        self._last_valid_opacity = DEFAULT_IDLE_OPACITY
        self._edit_opacity.blockSignals(True)
        self._edit_opacity.setText(str(DEFAULT_IDLE_OPACITY))
        self._edit_opacity.blockSignals(False)

        self._last_valid_hold_ms = DEFAULT_LAUNCH_HOLD_MS
        self._edit_hold_ms.blockSignals(True)
        self._edit_hold_ms.setText(str(DEFAULT_LAUNCH_HOLD_MS))
        self._edit_hold_ms.blockSignals(False)
        self._sync_hold_enabled(False)  # 默认开关关闭 → 时间行禁用

    # ------------------------------------------------------------------
    # 内部：交互 → 校验 / 抛信号
    # ------------------------------------------------------------------
    def _on_pin_toggled(self, on: bool) -> None:
        self.always_on_top_toggled.emit(on)

    # ------------------------------------------------------------------
    # 内部：防误触（开关 + 长按时间）
    # ------------------------------------------------------------------
    def _on_hold_toggled(self, on: bool) -> None:
        self._sync_hold_enabled(on)
        self.launch_hold_toggled.emit(on)

    def _on_hold_ms_edited(self) -> None:
        """输入框提交（回车 / 失焦）：非法则回滚上次合法值并提示，合法则提交。"""
        text = self._edit_hold_ms.text().strip()
        try:
            value = int(text)
        except ValueError:
            value = -1

        if not (MIN_LAUNCH_HOLD_MS <= value <= MAX_LAUNCH_HOLD_MS):
            self._warn_hold.setText(
                f"长按时间需为 {MIN_LAUNCH_HOLD_MS}–{MAX_LAUNCH_HOLD_MS} 的整数（毫秒），"
                f"已恢复为 {self._last_valid_hold_ms}。"
            )
            self._warn_hold.setVisible(True)
            self._sync_hold_ms(self._last_valid_hold_ms)
            return

        self._warn_hold.setVisible(False)
        if value == self._last_valid_hold_ms:
            return
        self._last_valid_hold_ms = value
        self.launch_hold_ms_changed.emit(value)

    def _sync_hold_ms(self, value: int) -> None:
        """把某值同步到输入框（全部 blockSignals，不触发交互回调）。"""
        self._edit_hold_ms.blockSignals(True)
        self._edit_hold_ms.setText(str(value))
        self._edit_hold_ms.blockSignals(False)

    def _sync_hold_enabled(self, on: bool) -> None:
        """开关打开才允许调长按时间；关闭时禁用时间相关控件（变灰）。"""
        self._hold_ms_enabled = on
        self._edit_hold_ms.setEnabled(on)

    def _on_opacity_edited(self) -> None:
        """输入框提交（回车 / 失焦）：非法则回滚上次合法值并提示，合法则提交。"""
        text = self._edit_opacity.text().strip()
        try:
            value = int(text)
        except ValueError:
            value = -1

        if not (MIN_IDLE_OPACITY <= value <= MAX_IDLE_OPACITY):
            self._warn.setText(
                f"透明度需为 {MIN_IDLE_OPACITY}–{MAX_IDLE_OPACITY} 的整数，"
                f"已恢复为 {self._last_valid_opacity}。"
            )
            self._warn.setVisible(True)
            self._sync_controls(self._last_valid_opacity)
            return

        self._warn.setVisible(False)
        if value == self._last_valid_opacity:
            return
        self._last_valid_opacity = value
        self.idle_opacity_changed.emit(value)

    def _sync_controls(self, value: int) -> None:
        """把某值同步到输入框（全部 blockSignals，不触发交互回调）。"""
        self._edit_opacity.blockSignals(True)
        self._edit_opacity.setText(str(value))
        self._edit_opacity.blockSignals(False)

    # ------------------------------------------------------------------
    # 公开接口（供 domain.SettingsController 读写）
    # ------------------------------------------------------------------
    def set_always_on_top(self, value: bool) -> None:
        self._switch_pin.setOn(bool(value))

    def get_always_on_top(self) -> bool:
        return self._switch_pin.on

    def set_idle_opacity(self, value: int) -> None:
        """回填透明度（绑定 / 启动时调用，不触发 idle_opacity_changed）。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = DEFAULT_IDLE_OPACITY
        n = max(MIN_IDLE_OPACITY, min(MAX_IDLE_OPACITY, n))
        self._last_valid_opacity = n
        self._sync_controls(n)

    def get_idle_opacity(self) -> int:
        return self._last_valid_opacity

    # ---- 启动按钮防误触 ----
    def set_launch_hold(self, value: bool) -> None:
        """回填开关（绑定 / 启动时调用，不触发 launch_hold_toggled）。"""
        self._switch_hold.setOn(bool(value))
        self._sync_hold_enabled(bool(value))

    def get_launch_hold(self) -> bool:
        return self._switch_hold.on

    def set_launch_hold_ms(self, value: int) -> None:
        """回填长按时间（绑定 / 启动时调用，不触发 launch_hold_ms_changed）。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = DEFAULT_LAUNCH_HOLD_MS
        n = max(MIN_LAUNCH_HOLD_MS, min(MAX_LAUNCH_HOLD_MS, n))
        self._last_valid_hold_ms = n
        self._sync_hold_ms(n)

    def get_launch_hold_ms(self) -> int:
        return self._last_valid_hold_ms


# ===========================================================================
# 通用内容页（用于占位，后续步骤补全）
# ===========================================================================
class PlaceholderPage(QWidget):
    def __init__(self, title: str, desc: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        t = QLabel(title, self)
        t.setObjectName("ContentTitle")
        layout.addWidget(t)

        d = QLabel(desc, self)
        d.setObjectName("ContentDesc")
        d.setWordWrap(True)
        layout.addWidget(d)
        layout.addStretch()


# ===========================================================================
# 设置窗口（UI）
# ===========================================================================
class SettingsWindow(CenteredPopupMixin, QWidget):
    """设置页窗口：640×360，左侧竖向标签栏 + 右侧内容区域（仅 UI）。

    行为收口在 omg.domain.settings.SettingsController：构建末尾调用
    ``SettingsController(config).bind(self)`` 完成所有功能绑定。
    本窗口**不**持有 ConfigManager，也不实现「应用 / 取消」。
    """

    def __init__(self, parent: Optional[QWidget] = None, config: Optional[object] = None,
                 tray_mode: bool = False) -> None:
        # 任务栏策略（2026-09-18 改）：**仅首页**不进任务栏，其它页面一律显示。
        # 早期是「与 HomeWindow 同进同退」地隐藏，但设置页/便捷构建页是长时间
        # 停留的独立窗口，用户切走后需要任务栏按钮才能切回来——Alt+Tab 之外
        # 没有可靠入口。tray_mode 参数仅为签名兼容保留，不再影响 flags。
        super().__init__(parent, window_flags(hide_from_taskbar=False))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("SettingsRoot")
        self.setFixedSize(WIN_W, WIN_H)
        # 几何记忆：首次打开居中，之后按上次关闭位置弹出
        self.init_geometry_persist("settings", config)

        # 样式仅作用于本设置窗口，不覆盖全局 app 样式表，
        # 避免与 Home 窗口的 HOME_QSS 互相覆盖导致「样式丢失」。
        self.setStyleSheet(SETTINGS_QSS)

        # ---- 主容器 ----
        self._frame = QFrame(self)
        self._frame.setObjectName("SettingsFrame")
        self._frame.setGeometry(0, 0, WIN_W, WIN_H)
        self._frame.setStyleSheet(
            f"QFrame#SettingsFrame {{"
            f"  background: {BG_WINDOW};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: {RADIUS_WIN}px;"
            f"}}"
        )

        main_layout = QVBoxLayout(self._frame)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- 标题栏 ----
        self._build_title_bar(main_layout)

        # ---- 主体 ----
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._sidebar = Sidebar(self._frame)
        self._sidebar.set_on_select(self._on_page_changed)
        body.addWidget(self._sidebar)

        self._content_stack = QStackedWidget(self._frame)
        self._content_stack.setObjectName("ContentArea")

        # 路径页（第一步：UI 在此，功能由 domain 绑定）
        self.path_page = PathPage(self._frame)
        self._content_stack.addWidget(self.path_page)

        # GIMI 页
        self.gimi_page = GimiPage(self._frame)
        self._content_stack.addWidget(self.gimi_page)

        # 启动页
        self.startup_page = StartupPage(self._frame)
        self._content_stack.addWidget(self.startup_page)

        # OMG 页（选项卡片：默认置顶 / 闲置时透明度）
        self.omg_page = OmgPage(self._frame)
        self._content_stack.addWidget(self.omg_page)

        body.addWidget(self._content_stack, 1)
        main_layout.addLayout(body, 1)

        # ---- 功能收口至 domain（单向：pages → domain）----
        from omg.domain.settings import SettingsController
        self._controller = SettingsController(config)
        self._controller.bind(self)

        self._drag_offset: Optional[QPoint] = None

        # 自定义 tooltip 气泡：覆盖本窗口所有控件，屏蔽原生 QToolTip 直角 + 阴影。
        install_pop_cards(self, lambda: "dark")

    def _build_title_bar(self, parent_layout: QVBoxLayout) -> None:
        title_bar = QFrame(self._frame)
        title_bar.setObjectName("TitleBar")
        title_bar.setFixedHeight(32)

        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(12, 0, 8, 0)
        tb_layout.setSpacing(0)

        title_label = QLabel("设置", title_bar)
        title_label.setObjectName("TitleText")
        tb_layout.addWidget(title_label)
        tb_layout.addStretch()

        # 关闭按钮：使用 genshin_function_control_close.svg 图标
        # （该 SVG 无 fill，按主题前景色 TEXT_PRIMARY 着色以适配深色主题）
        btn_close = QPushButton(title_bar)
        btn_close.setObjectName("TitleBtn")
        btn_close.setFixedSize(24, 24)
        btn_close.setIcon(tinted_icon(CLOSE_ICON, CLOSE_ICON_SIZE, TEXT_PRIMARY))
        btn_close.setIconSize(QSize(CLOSE_ICON_SIZE, CLOSE_ICON_SIZE))
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.close)
        tb_layout.addWidget(btn_close)

        parent_layout.addWidget(title_bar)

    def _on_page_changed(self, index: int) -> None:
        self._content_stack.setCurrentIndex(index)

    # ---- 拖拽 ----
    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        super().closeEvent(event)


# ===========================================================================
# 直接运行入口（开发调试用）
# ===========================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(SETTINGS_QSS)
    win = SettingsWindow()
    win.show()
    sys.exit(app.exec())
