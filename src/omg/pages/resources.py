"""
omg.pages.resources — 《原神》「资源浏览」面板（浏览 / 管理 GIMI/Mods 下的文件与文件夹）。

窗口：640×360，与设置页一致；弹出窗口范式复用 CenteredPopupMixin（首次居中、
之后记忆位置），外壳与标题栏照搬 quick_build 的 iOS 风骨架。

布局：
  ┌─ 标题栏：资源浏览 透明度[__]% … [写回] [ini] | [置顶] [×] ─────┐
  │ 面包屑：Mods › Hub › …（点击回跳，› 点出子目录）    │
  ├──────────────────────────────────────────────────┤
  │ 内容区（可滚动，3 列 QGridLayout 横排卡片网格）     │
  └──────────────────────────────────────────────────┘

交互：
  * 标题栏：透明度输入框（20~100%，回车生效）+ 置顶按钮（pinned 图标，
    空心 = 未置顶 / 实心 = 已置顶）；两者只作用于资源浏览界面本身并持久化到 config；
  * 全局热键 Alt+`（系统级，游戏内也能按）显示 / 隐藏本界面——热键注册在
    HomeWindow（omg.ui.global_hotkey），本窗口只负责被显隐；
  * 文件夹模式：顶部面包屑导航（Explorer 同款）——每个「屑」点击回到该层，
    分隔符 › 点击弹出该路径下的目录列表供快速跳转；下方单层卡片网格：
    - 子文件夹卡片：点击进入该目录（下钻），无启用状态条；右键只有
      「标记为 Mod」（与 Mod 卡片上的「标记为文件夹」是同一个选项，按类型切换）；
    - Mod 卡片：左侧绿/灰状态条表示启用/停用，单击切换启用，双击在资源管理器打开；
    - 两类卡片的**缩略图区域**单独可点：用系统默认看图程序打开该图片。
      缩略图会吃掉鼠标事件，不会连带触发卡片的启用切换 / 下钻；
    - 缩略图检索：Mod 卡片递归（find_thumbnail_deep，目标 preview 命名的图）；
      文件夹卡片只搜一层（find_thumbnail），搜不到就空着占位框。
  * 「刷新数据」→ 后台线程重新抓取 gachabase 三语角色数据（进度圈 + 状态）；
    打开时若缓存缺失 / 过期（>7 天）也会自动拉取一次。

设计取舍（内容展示）：自动分类模式用**竖排缩略图卡片网格**（FlowLayout 自动换行）；
文件夹模式（方案 C）用**横排卡片**——缩略图靠左、右侧名称+角标、固定高 72、一行 3 张
用 QGridLayout 均分铺满窗口宽度，窗口缩放时随列宽自适应省略文本。
"""

from __future__ import annotations

import os
import sys
import ctypes
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QPoint, QSize, QUrl, QRect, QRectF, Signal, QTimer
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFontMetrics,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsBlurEffect, QGridLayout,
    QHBoxLayout, QLabel,
    QLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QProxyStyle, QScrollArea, QSizePolicy, QStackedWidget, QStyle,
    QVBoxLayout, QWidget,
)

# 复用设置页令牌 / 组件，保证视觉一致
from omg.pages.setting import (  # noqa: F401
    ACCENT, ACCENT_HOVER, BG_CARD, BG_CARD_HOVER, BG_INPUT, BG_WINDOW,
    BORDER, CLOSE_ICON, CLOSE_ICON_SIZE, Card, RADIUS_CARD, RADIUS_CTRL,
    RADIUS_WIN,
    SETTINGS_QSS, SegmentedControl, TEXT_ON_ACCENT, TEXT_PRIMARY,
    TEXT_SECONDARY, WIN_H, WIN_W, tinted_icon,
)
from omg.core import gachabase as gb
from omg.core import mods_manager as mm
from omg.core import window_behavior as wb
from omg.core.alchemy import _CancelThread, TaskSignals
from omg.core.logging_setup import logger
from omg.ui.window_flags import window_flags  # noqa: E402  顶层 flags 单一真源
from omg.ui.window_geo import CenteredPopupMixin
from omg.ui.OMGCmdOutput import OMGCmdOutput
from omg.ui.OMGPopCard import HOVER_INTERACTIVE, install_pop_cards, show_click_card

# 一级筛选（自动分类模式：固定 5 档；文件夹模式：动态 = 全部 + 物理分组）
CAT1_ITEMS = ["全部", "角色", "武器", "怪物", "其它"]
CAT1_KEYS = [gb.CATEGORY_CHARACTER if x == "角色" else
             gb.CATEGORY_WEAPON if x == "武器" else
             gb.CATEGORY_MONSTER if x == "怪物" else
             gb.CATEGORY_OTHER if x == "其它" else "all" for x in CAT1_ITEMS]
# 分类模式（互斥）：文件夹 = 物理层级即分类；自动分类 = 名称匹配 + 手动覆盖
MODE_ITEMS = [mm.MODE_LABELS[mm.MODE_AUTO], mm.MODE_LABELS[mm.MODE_FOLDER]]
MODE_KEYS = [mm.MODE_AUTO, mm.MODE_FOLDER]
# 二级元素筛选（顺序与 UI 标签一致）
ELEMENT_LABELS = ["全部", "火", "水", "风", "雷", "草", "冰", "岩"]
ELEMENT_ID_ORDER = ["all", "1", "2", "7", "4", "3", "5", "8"]

_CARD_W = 150            # 自动分类模式（竖排卡片）固定宽
_THUMB = 110            # 自动分类模式缩略图边长
_CARD_H = 72            # 文件夹模式（横排卡片）固定高（方案 C）
_THUMB_H = 56           # 文件夹模式缩略图边长，嵌入 72 高卡片
_FOLDER_COLS = 3        # 文件夹模式一行 3 张，铺满窗口宽度
_FOLDER_SPACING = 10    # 卡片间距
_FOLDER_MARGIN = 12     # 网格外边距
_THUMB_BG = "#3A3A3C"   # 缩略图占位底色（Mod/文件夹统一灰底，预览图铺在其上）
# 信息区（缩略图右侧 / 下方的文本列）两段的**固定高度**：无论内容是否存在
# （名称一行还是两行、有没有切换键按钮），两段都占同样的高度，卡片内部各
# 组件的相对位置因此恒定——否则按钮 hide() 后 QHBoxLayout 会把整行连同
# stretch 一起跳过，工具栏塌成 0 高，其余元素整体上移。
_NAME_H = 36            # 横排卡名称固定高（容纳 11px 字号的两行；单行下方留白）
_NAME_H_V = 34          # 竖排卡名称固定高（同上，竖排缩略图更大、稍紧一点）
_BADGE_H = 16           # 竖排卡角标固定高（11px 字号单行，空文本也占位）
_TOOL_H = 24            # 工具栏固定高（= 切换键按钮边长，无按钮时也占位）
# 横排卡高 72 = 信息区上下外边距 6+6 + 名称 36 + 工具栏 24，正好铺满：
# 两段固定高之和恒定，故卡片内部结构不随内容多少而变化。
_CARD_BORDER = "#5A5A5C"  # 卡片静止态描边：在深色磨砂背景上可见
_WIN_ALPHA = 230         # 磨砂面板不透明度（0~255 ≈ 0.9）：压暗背景、保证可读

# 标题栏「置顶」图标：同一枚定位针的两种形态——实心 pinned.svg = 已置顶，
# 空心 pin.svg = 未置顶。两个 SVG 都不带颜色，由 tinted_icon 按主题前景色着色，
# 深色主题下自动为浅色；置顶态不再用蓝色背景强调，只靠实心/空心区分。
PIN_ICON = "mm/pinned.svg"
PIN_OFF_ICON = "mm/pin.svg"
PIN_ICON_SIZE = 16
# 标题栏「当前目录的 ini 文件」按钮图标（mm/ini.svg，无配色，按主题前景色着色）
INI_ICON = "mm/ini.svg"
INI_ICON_SIZE = 16
# 该按钮尺寸（固定方形，图标居中）
INI_BTN_SIZE = 24
# 标题栏「写回差分值」按钮图标（mm/write.svg，与 ini 按钮同款无配色线稿）：
# 把 d3dx_user.ini 里的当前取值写回各 mod 的 .ini（见 mm.write_back_swapkeys）。
WRITE_ICON = "mm/write.svg"
WRITE_ICON_SIZE = 16
WRITE_BTN_SIZE = 24
# 写回的「命令输出」侧页：贴在本窗口右侧（放不下则左侧），与窗口之间留
# CMD_OUT_GAP 的细微间隔；结束后停留 CMD_OUT_CLOSE_DELAY 再自动关闭，
# 让用户来得及看最后一行（见 _on_write_btn_clicked）。
CMD_OUT_GAP = 8
CMD_OUT_CLOSE_DELAY = 1500
# 用户操作的结论（写回 / 启用切换的结果）在状态栏上保留多久不被后台消息覆盖
STATUS_HOLD_MS = 5000
# 卡片右下角「切换键」按钮：keyboard.svg（lucide 线稿，currentColor 由
# tinted_icon 注入主题色）。常态次级灰、悬浮切主前景色，边框 / 底色走 QSS。
KEY_ICON = "mm/keyboard.svg"
KEY_ICON_SIZE = 14
KEY_BTN_SIZE = 24
# 缩略图遮罩：右键菜单「遮罩预览图 / 取消遮罩」给缩略图加高斯模糊，用于不便
# 直接展示的 mod。QGraphicsBlurEffect 的 blurRadius 即高斯半径（像素）。
MASK_BLUR_RADIUS = 8
# 标题栏「资源浏览」与右侧「透明度」控件之间的额外间距（叠加在布局 spacing 上）
TITLE_GAP = 6

_KEY_ICON_CACHE: dict = {}


def key_icon(hover: bool = False) -> "QIcon":
    """缓存后的「切换键」图标：常态 TEXT_SECONDARY / 悬浮 TEXT_PRIMARY。"""
    tag = "hover" if hover else "idle"
    ic = _KEY_ICON_CACHE.get(tag)
    if ic is None:
        ic = tinted_icon(KEY_ICON, KEY_ICON_SIZE,
                         TEXT_PRIMARY if hover else TEXT_SECONDARY)
        _KEY_ICON_CACHE[tag] = ic
    return ic


# 二级菜单（子菜单）的标识箭头：chevron-right.svg（lucide 线稿，currentColor
# 由 tinted_icon 注入主题色）。默认三角由 QStyle 用调色板色绘制，在深色 QSS
# 菜单上几乎看不清（深底深三角），故换成 OMG 图标体系的 chevron。
SUBMENU_ICON = "chevron-right.svg"
SUBMENU_ICON_SIZE = 12

_SUBMENU_CACHE: dict = {}


def submenu_arrow_pixmap() -> "QPixmap":
    """缓存后的子菜单箭头位图（12px，次级前景色）。"""
    pm = _SUBMENU_CACHE.get("pm")
    if pm is None:
        pm = tinted_icon(SUBMENU_ICON, SUBMENU_ICON_SIZE,
                         TEXT_SECONDARY).pixmap(
            QSize(SUBMENU_ICON_SIZE, SUBMENU_ICON_SIZE))
        _SUBMENU_CACHE["pm"] = pm
    return pm


class _SubmenuArrowStyle(QProxyStyle):
    """把菜单里「有二级菜单」的默认三角箭头替换成 chevron-right.svg。

    只接管 ``PE_IndicatorArrowRight``（子菜单箭头专用），上下滚动箭头 /
    工具栏箭头等仍走原生绘制，避免误伤。
    """

    def drawPrimitive(self, element, option, painter,  # type: ignore[override]
                      widget=None) -> None:
        if element == QStyle.PE_IndicatorArrowRight:
            pm = submenu_arrow_pixmap()
            if not pm.isNull():
                r = option.rect
                painter.drawPixmap(r.center().x() - pm.width() // 2,
                                   r.center().y() - pm.height() // 2, pm)
                return
        super().drawPrimitive(element, option, painter, widget)


def submenu_style() -> "QProxyStyle":
    """进程内共享的子菜单箭头样式（QMenu.setStyle 不接管所有权，需自持引用）。"""
    st = _SUBMENU_CACHE.get("style")
    if st is None:
        st = _SubmenuArrowStyle()
        _SUBMENU_CACHE["style"] = st
    return st


def apply_submenu_style(menu: "QMenu") -> "QMenu":
    """给菜单（及其所有子菜单）挂上 chevron 子菜单箭头样式。"""
    st = submenu_style()
    menu.setStyle(st)
    for sub in menu.findChildren(QMenu):
        sub.setStyle(st)
    return menu


class ModKeyButton(QPushButton):
    """卡片右下角的「切换键」图标按钮。

    * 图标双态：进入切主前景色、离开回次级灰（Qt 按钮的 ``QIcon.Active``
      在自绘 / QSS 混合样式下不可靠，这里显式换图标最稳）；
    * 边框与底色交给 QSS（``QPushButton#ModKeyBtn``），常态描边、悬浮提亮
      + 边框转强调色、按下压暗；
    * **不设 toolTip**：应用级提示过滤器会把带 toolTip 的控件做成悬浮即显，
      而本按钮的明细卡要点击才弹（见 ``_on_keys_clicked``）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ModKeyBtn")
        self.setFixedSize(KEY_BTN_SIZE, KEY_BTN_SIZE)
        self.setIcon(key_icon(False))
        self.setIconSize(QSize(KEY_ICON_SIZE, KEY_ICON_SIZE))
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.setIcon(key_icon(True))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.setIcon(key_icon(False))
        super().leaveEvent(event)


# ===========================================================================
# FlowLayout（自动换行的卡片网格）
# ===========================================================================

class FlowLayout(QLayout):
    """Qt 官方 FlowLayout 示例的精简版：子项从左到右排，超出宽度自动换行。"""

    def __init__(self, parent: Optional[QWidget] = None, margin: int = 0,
                 spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: List[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, i: int) -> Optional[QLayoutItem]:  # type: ignore[override]
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int) -> Optional[QLayoutItem]:  # type: ignore[override]
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):  # type: ignore[override]
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # type: ignore[override]
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_h = 0
        sp = self._spacing
        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            if x + w > rect.right() - m.right() and line_h > 0:
                x = rect.x() + m.left()
                y += line_h + sp
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(x, y, w, h))
            x += w + sp
            line_h = max(line_h, h)
        return y + line_h - rect.y() + m.bottom()


# ===========================================================================
# 条目卡片
# ===========================================================================

def _open_image_file(path: str) -> None:
    """用系统默认看图程序打开一张图片（缩略图被点击时的动作）。"""
    if path and os.path.isfile(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _make_thumb(parent: QWidget, path: str, object_name: str,
                style: str, size: int) -> "ThumbLabel":
    """构造卡片缩略图：占位底色打底，命中 preview 图则等比缩放叠加其上。

    点击缩略图 = 打开图片本身，与点击卡片其他区域（切换启用 / 进入目录）严格
    分开——区分逻辑在 ThumbLabel 内部（accept 掉鼠标事件，阻止冒泡到卡片）。
    """
    thumb = ThumbLabel(parent)
    thumb.setFixedSize(size, size)
    thumb.setObjectName(object_name)
    thumb.setStyleSheet(style)
    if path and os.path.isfile(path):
        pm = QPixmap()
        if pm.load(path):
            thumb.setPixmap(pm.scaled(size, size, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation))
            thumb.set_pixmap_path(path)
    thumb.activated.connect(lambda *_, p=path: _open_image_file(p))
    return thumb


class ThumbLabel(QLabel):
    """可点击的缩略图：单击 = 用系统默认程序打开这张图片。

    与卡片点击的区分是关键：卡片空白处单击是「切换启用 / 进入下一级目录」，
    双击是「在资源管理器打开目录」。Qt 里 QWidget 默认的鼠标处理**不** accept
    事件，事件会继续冒泡给父控件——若不显式 accept，点缩略图会顺带触发卡片
    动作（表现成「打开图片的同时还把 mod 停用了」）。

    这里只拦截 mouseRelease / mouseDoubleClick：mousePress 仍冒泡，因此按住
    缩略图拖动依然可以移动整个窗口（窗口拖拽在 ResourcesWindow.mousePressEvent）。
    信号命名 ``activated`` 而非 ``clicked``，避免与父类信号同名（PySide6 下
    同名 Python 信号会替换原生信号，极易引发自连接递归崩溃）。
    """

    activated = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pix_path = ""
        self.setCursor(Qt.PointingHandCursor)

    def set_pixmap_path(self, path: str) -> None:
        """记录当前显示的图片路径（作为点击时要打开的目标）。"""
        self._pix_path = path or ""
        self.setToolTip("点击打开图片" if self._pix_path else "")

    def pixmap_path(self) -> str:
        return self._pix_path

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            event.accept()          # 吃掉释放事件，卡片不会收到单击
            if self._pix_path:
                self.activated.emit()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            event.accept()          # 吃掉双击，卡片不会「在资源管理器打开目录」
            if self._pix_path:
                self.activated.emit()
            return
        super().mouseDoubleClickEvent(event)


class CardBase(QWidget):
    """卡片基类：自己绘制圆角背景 + 描边，避免 QWidget 的 QSS border 被裁/不渲染。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._hovered = False

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 留 1px 给描边，避免切边
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS_CARD, RADIUS_CARD)
        p.fillPath(path, QColor(BG_CARD_HOVER if self._hovered else BG_CARD))
        pen = QPen(QColor(ACCENT if self._hovered else _CARD_BORDER))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawPath(path)


def _elide_two_lines(text: str, fm: QFontMetrics, w: int) -> Tuple[str, bool]:
    """两行省略：第一行尽量占满（优先在空格处断行），第二行放不下用 … 截断。

    卡片名称空间有限，单行 ElideRight 会把长文件夹名截得只剩前几个字符；
    允许折两行能多显示一倍内容，仍放不下再以 … 收尾。

    返回 ``(显示文本, 是否被截断)`` —— 被截断才需要悬浮提示（完整显示时
    再弹提示纯属打扰），见 ``ItemCard._refresh_texts``。
    """
    if w <= 0:
        return text, False
    if fm.horizontalAdvance(text) <= w:
        return text, False
    # 第一行断点：逐字符找到能放下的最长前缀，若其后还有内容则优先回退到
    # 最近一个空格（英文 mod 名以空格分词，硬切单词观感差）。
    cut = 0
    for i in range(1, len(text) + 1):
        if fm.horizontalAdvance(text[:i]) > w:
            break
        cut = i
    if cut == 0:                       # 单个字符都放不下（极端窄卡）
        cut = 1
    line1 = text[:cut]
    if cut < len(text):
        sp = line1.rfind(" ")
        if sp > 0:
            line1 = line1[:sp]
    rest = text[len(line1):].lstrip()
    if not rest:
        return line1, False
    if fm.horizontalAdvance(rest) <= w:
        return line1 + "\n" + rest, False
    return line1 + "\n" + fm.elidedText(rest, Qt.ElideRight, w), True


def _wrapped_fits(text: str, fm: QFontMetrics, w: int, h: int) -> bool:
    """竖排卡名称（``wordWrap=True``）：按给定宽度换行后能否装进给定高度。

    竖排卡不像横排卡那样主动省略，超长名称靠 QLabel 换行，装不下的部分被
    固定高裁掉——所以只有「换行后总高超过可视高」才算没显示全。
    """
    if not text or w <= 0 or h <= 0:
        return True
    return fm.boundingRect(QRect(0, 0, w, 1 << 20),
                           int(Qt.TextWordWrap), text).height() <= h


class ItemCard(CardBase):
    """内容区单个 Mod 条目卡片：缩略图 + 名称 + 角标。双击打开，右键重新归类。"""

    def __init__(self, item: dict, parent: Optional[QWidget] = None,
                 horizontal: bool = False) -> None:
        super().__init__(parent)
        self._item = item
        self._horizontal = horizontal
        self.setObjectName("ModCard")
        self.setCursor(Qt.PointingHandCursor)

        # 「切换键」按钮：仅当该 mod 解析出 [Key*] 切换键时显示，位置固定在卡片
        # 右下角；**点击**弹出切换键明细卡（OMGPopCard，交互型，可点外部/Esc 关闭）——
        # 悬浮即显已被移除（原 QLabel chip 方案），避免鼠标扫过就弹出干扰浏览。
        self._btn_keys = ModKeyButton(self)
        self._btn_keys.clicked.connect(self._on_keys_clicked)
        self._keys_tip = ""

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 左侧启用状态条：绿=启用 / 灰=停用（Mod 卡片核心标识）。
        # 视觉参照设置页侧边导航标签的选中态指示条：全圆角小药丸（宽 4、
        # 圆角 = 宽/2），高度与缩略图一致并垂直居中（横排）/ 顶端对齐缩略图
        # （竖排）——不再通铺卡片全高，避免顶进卡片圆角、随文本列高度漂移。
        # 两侧各留 6px：左侧与卡片边缘脱开（不贴圆角），右侧与缩略图对齐留白。
        self._bar = QFrame(self)
        self._bar.setObjectName("ModStatusBar")
        self._bar.setFixedSize(4, _THUMB_H if horizontal else _THUMB)
        outer.addSpacing(6)
        outer.addWidget(self._bar, 0,
                        Qt.AlignVCenter if horizontal else Qt.AlignTop)
        # 视觉：状态条与缩略图之间留间距，避免紧贴
        outer.addSpacing(6)

        if horizontal:
            # 方案 C 横排卡片，三层结构：
            #   一层：左右（状态条 | 缩略图 | 信息区）
            #   二层（信息区）：上下（名称、工具栏）
            #   三层（工具栏）：左右（弹簧、键盘按钮）
            # 固定高 72、宽随网格拉伸，一行 3 张铺满窗口宽度。
            self.setFixedHeight(_CARD_H)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            # 占位灰底作为背景：无论是否有预览图都先铺一层，预览图等比缩放
            # 居中叠加其上（非方图留白处露出灰底，保证所有卡片缩略图观感一致）。
            # 预览图检索：根目录一层优先，无图再逐层向下找（find_thumbnail_deep）。
            thumb = _make_thumb(self, self._item.get("thumb") or "",
                                "ModThumb", self._placeholder_style(), _THUMB_H)
            outer.addWidget(thumb, 0, Qt.AlignVCenter)
            self._thumb = thumb

            # —— 二层：信息区（名称在上、工具栏在下）——
            txt = QVBoxLayout()
            txt.setContentsMargins(8, 6, 8, 6)
            txt.setSpacing(0)
            name = QLabel(item["name"], self)
            name.setObjectName("ModName")
            name.setWordWrap(False)
            # 名称允许两行 + 缩小字号（11px）；超出两行用 … 截断（见
            # _elide_two_lines / _refresh_texts）。固定高 = 两行行高（_NAME_H），
            # 单行名称下方留白，保证所有卡片文本区起始位置一致。
            name.setFixedHeight(_NAME_H)
            name.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            # 悬浮提示不在这里写死：只有名称**真的被省略**时才挂全名提示，
            # 完整显示时再弹提示纯属打扰（见 _refresh_texts）。
            name.setStyleSheet("font-size:11px;"
                               + ("" if item.get("enabled", True)
                                  else f"color:{TEXT_SECONDARY};"))
            txt.addWidget(name)
            # —— 三层：工具栏（弹簧 + 切换键按钮，按钮贴右下角）——
            # 注意：横排卡只用于文件夹模式，角标恒为空且被 hide()，而
            # QHBoxLayout 会整体跳过隐藏控件（连带 stretch），此前把角标放进
            # 工具栏行导致「弹簧失效、按钮跑到左下角」——故角标不再进横排卡。
            # 工具栏是**固定高的容器**：没有切换键时按钮 hide()，容器仍占
            # _TOOL_H，卡片内各元素位置不随按钮有无而变化。
            tools = QWidget(self)
            tools.setFixedHeight(_TOOL_H)
            row = QHBoxLayout(tools)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.addStretch(1)
            row.addWidget(self._btn_keys, 0, Qt.AlignRight | Qt.AlignVCenter)
            txt.addWidget(tools)
            self._tools = tools
            # 名称 + 工具栏之外的剩余空间交给末端弹簧吸收，避免布局把它
            # 摊给已固定高的控件、造成位置漂移。
            txt.addStretch(1)
            outer.addLayout(txt)
            badge = None
        else:
            # 自动分类模式：竖排卡片（缩略图在上，名称 + 角标在下），固定宽。
            self.setFixedWidth(_CARD_W)
            lay = QVBoxLayout()
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            thumb = _make_thumb(self, self._item.get("thumb") or "",
                                "ModThumb", self._placeholder_style(), _THUMB)
            lay.addWidget(thumb, alignment=Qt.AlignHCenter)
            self._thumb = thumb
            name = QLabel(item["name"], self)
            name.setObjectName("ModName")
            name.setWordWrap(True)
            name.setFixedHeight(_NAME_H_V)   # 竖排卡也是两行
            if not item.get("enabled", True):
                name.setStyleSheet(f"color:{TEXT_SECONDARY};")
            lay.addWidget(name)
            badge = QLabel(self)
            badge.setObjectName("ModBadge")
            badge.setFixedSize(_CARD_W - 4, _BADGE_H)   # 角标同样固定高：空
            badge.setWordWrap(False)                    # 文本时也不塌缩，卡片
            badge.setToolTip(self._badge_text())        # 总高恒定，网格不错行
            lay.addWidget(badge, alignment=Qt.AlignHCenter)
            # 竖排卡片同样把切换键按钮放在右下角（右边/下边各留 6px，不贴边）。
            # 同样是固定高容器（_TOOL_H + 6 底部边距）：没有切换键时按钮隐藏，
            # 这一行照旧占位，卡片总高与「有按钮」时完全一致，网格不会错行。
            tools = QWidget(self)
            tools.setFixedHeight(_TOOL_H + 6)
            row = QHBoxLayout(tools)
            row.setContentsMargins(0, 0, 6, 6)
            row.setSpacing(0)
            row.addStretch(1)
            row.addWidget(self._btn_keys, 0, Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(tools)
            self._tools = tools
            outer.addLayout(lay)

        self._name = name
        self._badge = badge        # 横排卡（文件夹模式）没有角标，恒为 None
        self._refresh_key_hint()
        if not horizontal:
            self._badge.setText(QFontMetrics(self._badge.font()).elidedText(
                self._badge_text(), Qt.ElideMiddle, _CARD_W - 4))
        self._refresh_texts()

        # 初始化时立即按启用状态刷新左侧状态条颜色
        self._update_bar()
        # 缩略图遮罩：按持久化的状态（跟随 DISABLED 重命名）给缩略图上高斯模糊
        self._apply_mask(self._is_masked())

    def _update_bar(self) -> None:
        """按启用状态刷新左侧状态条颜色（绿=启用，灰=停用）。"""
        color = "#639922" if self._item.get("enabled", True) else "#B4B2A9"
        self._bar.setStyleSheet(f"background:{color};border-radius:2px;")

    def _refresh_texts(self) -> None:
        """按当前宽度刷新名称，并**仅在被省略时**挂全名悬浮提示。

        横排卡：做两行省略（``_elide_two_lines``），避免溢出网格列宽；
        竖排卡：交给 QLabel 换行，只判断换完行会不会超出固定高。

        两种情况下「名称能完整显示」都不设 toolTip —— 悬浮即弹的同名提示
        对已经看得见全名的卡片是纯干扰（且会挡住下方卡片）。
        """
        src = self._item["name"]
        # QSS 里的 font-size 要等 polish 后才反映到 font()，否则测宽用的是
        # 默认字号（偏大），会把本可完整显示的名称误判成被截断。
        self._name.ensurePolished()
        fm = QFontMetrics(self._name.font())
        nw = max(20, self._name.width())
        if self._horizontal:
            text, cut = _elide_two_lines(src, fm, nw)
            self._name.setText(text)
        else:
            cut = not _wrapped_fits(src, fm, nw, max(1, self._name.height()))
        self._name.setToolTip(src if cut else "")

    def _refresh_key_hint(self) -> None:
        """卡片右下角切换键按钮：有 [Key*] 切换键才显示。

        明细文本（``format_toggle_keys_tip``）暂存在 ``_keys_tip``，点击时交给
        OMGPopCard 显示——不再是悬浮即显的 chip，鼠标扫过不会弹窗。
        """
        tks = self._item.get("toggle_keys") or []
        if not tks:
            self._keys_tip = ""
            self._btn_keys.hide()
            return
        self._keys_tip = mm.format_toggle_keys_tip(self._item)
        self._btn_keys.show()

    def _on_keys_clicked(self) -> None:
        """点击「切换键」按钮 → 弹出 / 收起切换键明细卡。"""
        if not self._keys_tip:
            return
        show_click_card(self._btn_keys, self._keys_tip,
                        hover_mode=HOVER_INTERACTIVE)

    # ---- 缩略图遮罩 ----
    def _mask_rel(self) -> str:
        """缩略图遮罩的持久化键：mod 路径相对 **Mods 根**（已规范化 DISABLED）。

        用相对路径而非绝对路径：换 GIMI 目录 / 拷贝 mod 后遮罩依旧命中；用
        ``norm_kind_rel`` 规范化，启用 / 禁用（加 DISABLED 前缀改名）也不会丢。
        """
        win = self._find_window()
        mods = win._mods_dir() if win is not None else ""
        path = self._item.get("path", "")
        if not mods or not path:
            return ""
        try:
            return mm.norm_kind_rel(os.path.relpath(path, mods))
        except ValueError:          # 不同盘符等极端情况
            return ""

    def _is_masked(self) -> bool:
        rel = self._mask_rel()
        return bool(rel) and mm.is_masked(rel)

    def _apply_mask(self, on: bool) -> None:
        """给缩略图加 / 去高斯模糊（``QGraphicsBlurEffect`` 直接作用在 QLabel 上）。"""
        thumb = getattr(self, "_thumb", None)
        if thumb is None:
            return
        if on:
            eff = QGraphicsBlurEffect(thumb)
            eff.setBlurRadius(MASK_BLUR_RADIUS)
            thumb.setGraphicsEffect(eff)
        else:
            thumb.setGraphicsEffect(None)

    def _toggle_mask(self) -> None:
        """右键菜单「遮罩预览图 / 取消遮罩」：写回持久化并就地更新模糊。"""
        win = self._find_window()
        if win is None:
            return
        on = win.toggle_mask(self._item.get("path", ""))
        self._apply_mask(on)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # 竖排卡宽度固定，宽度不变时省略结果也不变；横排卡列宽随窗口变化，
        # 必须重算。统一调用，避免漏掉某种模式下的尺寸变化。
        self._refresh_texts()

    def showEvent(self, event) -> None:  # type: ignore[override]
        # 首帧布局完成后名称标签才有真实宽度，此时补算一次——构造时宽度还是
        # 0，会把本可完整显示的名称误判成被截断而挂上多余提示。
        super().showEvent(event)
        self._refresh_texts()

    def _placeholder_style(self) -> str:
        ch = self._item.get("character")
        color = _THUMB_BG
        if ch:
            color = gb.ELEMENTS.get(ch.get("element_id", ""), {}).get("color", color)
        cat = self._item.get("category")
        if cat == gb.CATEGORY_WEAPON:
            color = "#8E8E93"
        elif cat == gb.CATEGORY_MONSTER:
            color = "#C9825A"
        return (f"background:{color};border-radius:{RADIUS_CTRL}px;"
                f"color:{TEXT_ON_ACCENT if cat=='character' else TEXT_PRIMARY};"
                f"font-size:28px;qproperty-alignment:AlignCenter;")

    def _badge_text(self) -> str:
        """角标文本——标明「它为什么被归到这里」。

        文件夹模式：显示所属物理分组（分组本身就是来源，不必再写「文件夹」）；
        自动模式：类别 · 元素 · 来源（手动 / 自动 / 未识别）。
        两种模式都不会串味：folder 不读覆盖表，auto 不读 group。

        注：原先 disabled 状态会在此追加「停用」文字；现改为在卡片右下角放
        切换键按钮（见 ``_refresh_key_hint``），故这里不再写「停用」——
        禁用态仍由左侧绿/灰状态条与变灰的名称清楚传达。
        """
        win = self._find_window()
        if win is not None and win.mode == mm.MODE_FOLDER:
            # 文件夹模式：角标不显示。分组即当前所在路径，在卡片上标出分组名
            # 是冗余信息（例如「Hub」），反而干扰辨识，故留空并隐藏。
            parts = []
        else:
            cat = self._item.get("category")
            parts = [mm.CATEGORY_LABELS.get(cat, "其它")]
            ch = self._item.get("character")
            if ch:
                elem = gb.ELEMENTS.get(ch.get("element_id", ""), {}).get("cn", "")
                if elem:
                    parts.append(elem)
            src = mm.SOURCE_LABELS.get(self._item.get("source", ""), "")
            if src:
                parts.append(src)
        return " · ".join(parts)

    # ---- 交互 ----
    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            win = self._find_window()
            if win is not None and win.mode == mm.MODE_FOLDER:
                # 用 250ms 单发定时器区分「单击=切换」与「双击=打开」，
                # 避免双击时先切两次（净不变）再打开的尴尬。
                self._click_timer = QTimer(self)
                self._click_timer.setSingleShot(True)
                self._click_timer.timeout.connect(self._toggle_enabled)
                self._click_timer.start(250)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            t = getattr(self, "_click_timer", None)
            if t is not None:
                t.stop()
            self._open_location()
        super().mouseDoubleClickEvent(event)

    def _toggle_enabled(self) -> None:
        win = self._find_window()
        if win is not None:
            win.toggle_mod_enabled(self._item)

    def _folder_menu(self) -> Tuple[QMenu, QAction, QAction, QAction]:
        """文件夹模式下 Mod 卡片的右键菜单（抽成方法便于离屏断言）：

        从资源管理器打开 / 遮罩预览图 / —— / .ini 文件（二级菜单，递归） /
        —— / 标记为文件夹。
        """
        menu = QMenu(self)
        # 「标记为文件夹」与文件夹卡片上的「标记为 Mod」是同一个选项，只按卡片
        # 类型切换文案与目标，故不列出互斥的另一项——避免点了没反应的灰项。
        act_open = menu.addAction("从资源管理器打开")
        # 缩略图遮罩：文案按当前状态切换（开启时给出反向动作，符合菜单惯例）
        act_mask = menu.addAction(self.mask_action_text())
        menu.addSeparator()
        # 「.ini 文件」二级菜单：递归列出该 mod 目录下全部 .ini（含子目录），
        # 点击用系统默认应用打开。散在深层子目录里的 ini 也能直接跳到。
        sub_ini = QMenu(".ini 文件", menu)
        path = self._item.get("path", "")
        if self._item.get("ext") == ".ini" and os.path.isfile(path):
            # 卡片本身就是一枚松散 .ini（不是目录），它自己就是唯一条目
            inis, base = [path], os.path.dirname(path)
        else:
            inis = mm.collect_ini_files(path, ignore_disabled=False)
            base = path
        if not inis:
            none_act = sub_ini.addAction("（无 .ini 文件）")
            none_act.setEnabled(False)
        else:
            for p in inis:
                try:
                    label = os.path.relpath(p, base)
                except ValueError:      # 不同盘符等极端情况，退回完整路径
                    label = p
                a = sub_ini.addAction(label.replace("\\", "/"))
                a.triggered.connect(
                    lambda *_, _p=p: QDesktopServices.openUrl(
                        QUrl.fromLocalFile(_p)))
        menu.addMenu(sub_ini)
        menu.addSeparator()
        act_kind = menu.addAction(self.kind_action_text())
        # 箭头的绘制方是**父菜单**（子菜单项画在父菜单里），故样式挂在 menu 上
        apply_submenu_style(menu)
        return menu, act_open, act_mask, act_kind

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        win = self._find_window()
        if win is not None and win.mode == mm.MODE_FOLDER:
            menu, act_open, act_mask, act_kind = self._folder_menu()
            a = menu.exec(event.globalPos())
            if a == act_open:
                self._open_location()
            elif a == act_mask:
                self._toggle_mask()
            elif a == act_kind and win is not None:
                self.apply_kind()
            return
        menu = QMenu(self)
        sub = QMenu("重新归类", menu)
        act_char = sub.addAction("角色…")
        act_w = sub.addAction("武器")
        act_m = sub.addAction("怪物")
        act_o = sub.addAction("其它")
        menu.addMenu(sub)
        act_reset = menu.addAction("恢复自动分类")
        apply_submenu_style(menu)
        act = menu.exec(event.globalPos())
        if act is None or win is None:
            return
        name = self._item["name"]
        if act == act_char:
            win.request_reclassify_character(name)
        elif act == act_w:
            win.apply_override(name, gb.CATEGORY_WEAPON)
        elif act == act_m:
            win.apply_override(name, gb.CATEGORY_MONSTER)
        elif act == act_o:
            win.apply_override(name, gb.CATEGORY_OTHER)
        elif act == act_reset:
            win.reset_override(name)

    def _open_location(self) -> None:
        path = self._item.get("path", "")
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def kind_action_text(self) -> str:
        """右键菜单里那个「标记为 X」的文案：Mod 卡片是「标记为文件夹」。"""
        return "标记为文件夹"

    def mask_action_text(self) -> str:
        """右键菜单里「遮罩」项的文案：已遮罩时给反向动作（取消遮罩）。"""
        return "取消遮罩" if self._is_masked() else "遮罩预览图"

    def apply_kind(self) -> None:
        """执行 kind_action_text() 对应的动作（Mod → 改标为文件夹）。"""
        win = self._find_window()
        if win is not None:
            win.mark_kind(self._item["path"], "folder")

    def _find_window(self):
        p = self.parent()
        while p is not None:
            if isinstance(p, ResourcesWindow):
                return p
            p = p.parent()
        return None


class FolderCard(CardBase):
    """文件夹模式下的子文件夹卡片：点击进入该目录（下钻），不显示启用状态条。

    视觉与 ItemCard 横排卡片一致（缩略图占位 + 名称，高 72、宽随网格拉伸），
    只是没有左侧绿/灰状态条——文件夹不可切换启用，只能下钻。
    """

    def __init__(self, path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path = path
        self.setObjectName("FolderCard")
        self.setFixedHeight(_CARD_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 透明状态条占位：与 Mod 卡片左侧状态条等宽（4px）+ 同样的 6px 间距，
        # 仅用于对齐略缩图横向位置——文件夹不可切换启用，故不画颜色、保持透明。
        self._bar_ph = QFrame(self)
        self._bar_ph.setObjectName("FolderStatusBarPlaceholder")
        self._bar_ph.setFixedWidth(4)
        outer.addWidget(self._bar_ph)
        outer.addSpacing(6)

        # 缩略图：占位灰底作为背景，其上叠加该目录下的预览图（无图则纯灰底）。
        # 文件夹卡片只搜「一层」：预览图必须就在该目录的直接子文件里；
        # 子目录里即使有图也**不**下钻（避免把子文件夹的预览误当本目录封面）。
        # 搜不到 → path 为空 → _make_thumb 只铺灰底占位框，空着。
        thumb = _make_thumb(self, mm.find_thumbnail(
            {"path": self._path, "is_dir": True}) or "",
            "FolderThumb", self._placeholder_style(), _THUMB_H)
        outer.addWidget(thumb)
        outer.addSpacing(6)

        txt = QVBoxLayout()
        txt.setContentsMargins(10, 6, 10, 6)
        txt.setSpacing(4)
        name = QLabel(os.path.basename(path), self)
        name.setObjectName("FolderName")
        name.setWordWrap(False)
        name.setFixedHeight(18)
        # 同 Mod 卡片：能完整显示时不挂提示（见 _refresh_texts）
        txt.addWidget(name)
        txt.addStretch(1)  # 名称靠上，下半部留空以对齐 Mod 卡片
        outer.addLayout(txt)
        self._name = name
        self._refresh_texts()

    def _placeholder_style(self) -> str:
        return (f"background:{_THUMB_BG};border-radius:{RADIUS_CTRL}px;"
                f"qproperty-alignment:AlignCenter;")

    def _refresh_texts(self) -> None:
        """按当前宽度省略文件夹名，避免溢出网格列宽。

        只有真的被省略（ElideMiddle 改写了文本）才挂全名提示——完整显示时
        再弹提示纯属干扰。
        """
        self._name.ensurePolished()
        src = os.path.basename(self._path)
        nw = max(20, self._name.width())
        shown = QFontMetrics(self._name.font()).elidedText(
            src, Qt.ElideMiddle, nw)
        self._name.setText(shown)
        self._name.setToolTip(src if shown != src else "")

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_texts()

    def showEvent(self, event) -> None:  # type: ignore[override]
        # 同 ItemCard：首帧布局后才拿得到真实控件宽度，补算一次省略与提示。
        super().showEvent(event)
        self._refresh_texts()

    # ---- 交互 ----
    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            win = self._find_window()
            if win is not None:
                win.navigate_to(self._path)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._open_location()
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        win = self._find_window()
        menu = QMenu(self)
        # 文案与 Mod 卡片保持一致（见 ItemCard._folder_menu）
        act_open = menu.addAction("从资源管理器打开")
        menu.addSeparator()
        # 本卡片是文件夹，唯一有意义的动作是「标记为 Mod」。它与 Mod 卡片上的
        # 「标记为文件夹」是同一个选项，按卡片类型切换文案与目标。
        act_kind = menu.addAction(self.kind_action_text())
        a = menu.exec(event.globalPos())
        if a == act_open:
            self._open_location()
        elif a == act_kind and win is not None:
            self.apply_kind()

    def _open_location(self) -> None:
        if self._path and os.path.exists(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def kind_action_text(self) -> str:
        """右键菜单里那个「标记为 X」的文案：文件夹卡片是「标记为 Mod」。"""
        return "标记为 Mod"

    def apply_kind(self) -> None:
        """执行 kind_action_text() 对应的动作（文件夹 → 改标为 Mod）。"""
        win = self._find_window()
        if win is not None:
            win.mark_kind(self._path, "mod")

    def _find_window(self):
        p = self.parent()
        while p is not None:
            if isinstance(p, ResourcesWindow):
                return p
            p = p.parent()
        return None


# ===========================================================================
# 角色搜索对话框（手动归类到「角色」用）
# ===========================================================================

class ReclassifyDialog(QDialog):
    """按名称搜索角色并选定，用于把「其它」条目改归到某角色。"""

    def __init__(self, chars: List[dict], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("归类到角色")
        self.setMinimumWidth(360)
        self.setMinimumHeight(360)
        self.setObjectName("ReclassifyDialog")
        self._chars = chars
        self._picked: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索角色名（中 / 英 / 韩）…")
        self._search.textChanged.connect(self._filter)
        lay.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(self._accept)
        lay.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("取消", self)
        btn_cancel.setObjectName("ContentBtn")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("确定", self)
        btn_ok.setObjectName("PrimaryBtn")
        btn_ok.clicked.connect(self._accept)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)

        self._populate("")

    def _populate(self, text: str) -> None:
        self._list.clear()
        q = text.strip().lower()
        for c in self._chars:
            names = c.get("names", {})
            hay = " ".join([c.get("display", "")] + list(names.values())).lower()
            if q and q not in hay:
                continue
            elem = gb.ELEMENTS.get(c.get("element_id", ""), {})
            label = f"{c.get('display', c['character_id'])}  ·  {elem.get('cn', '')}"
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, c["character_id"])
            if elem:
                it.setForeground(QColor(elem.get("color", TEXT_PRIMARY)))
            self._list.addItem(it)

    def _filter(self, text: str) -> None:
        self._populate(text)

    def _accept(self, *args) -> None:
        it = self._list.currentItem()
        if it is None:
            return
        self._picked = it.data(Qt.UserRole)
        self.accept()

    def picked_character_id(self) -> Optional[str]:
        return self._picked


# ===========================================================================
# 后台抓取线程
# ===========================================================================

class FetchThread(_CancelThread):
    """后台重新拉取 gachabase 三语角色数据。"""

    def __init__(self, signals: TaskSignals, parent=None) -> None:
        super().__init__(signals, parent)

    def _run_impl(self) -> None:
        if self.is_cancelled():
            self._emit_finished(False, "已取消")
            return
        ok, payload = gb.update_cache(
            status_cb=lambda lbl, a, b: self._emit_status(lbl)
        )
        if ok:
            self._emit_finished(True, f"已更新 {payload['count']} 个角色")
        else:
            self._emit_finished(False, str(payload))


class ScanThread(_CancelThread):
    """后台扫描 Mods 目录 + 分类（磁盘 I/O 与匹配计算都不阻塞 UI）。

    这是唯一真正读盘的路径。结果挂在线程实例上（items / classified），
    由 _on_scan_finished 取回；筛选切换、手动归类、角色数据更新都**不走这里**，
    而是复用缓存做纯内存操作。
    """

    def __init__(self, mods_dir: str, chars: List[dict],
                 signals: TaskSignals, mode: Optional[str] = None,
                 parent=None) -> None:
        super().__init__(signals, parent)
        self.mods_dir = mods_dir
        self.chars = chars
        self.mode = mode
        self.items: List[dict] = []
        self.classified: List[dict] = []

    def _run_impl(self) -> None:
        self._emit_status("正在扫描 Mods…")
        self.items = mm.scan_mods(self.mods_dir)
        if self.is_cancelled():
            self._emit_finished(False, "已取消")
            return
        self._emit_status(f"正在分类 {len(self.items)} 项…")
        self.classified = mm.classify_items(self.items, self.chars,
                                            None, self.mode)
        if self.is_cancelled():
            self._emit_finished(False, "已取消")
            return
        self._emit_finished(True, f"已扫描 {len(self.items)} 项")


class WriteBackThread(_CancelThread):
    """后台执行差分值写回（读写 ini 不阻塞 UI）。

    逐行输出经 ``signals.status`` 送到「命令输出」侧页；最终结果走
    ``signals.finished``，由页面记入 log。作用域**固定为 GIMI/Mods 根**
    （target_dir 传 None，见 mm.write_back_swapkeys）。
    """

    def __init__(self, mods_dir: str, signals: TaskSignals, parent=None) -> None:
        super().__init__(signals, parent)
        self.mods_dir = mods_dir

    def _run_impl(self) -> None:
        ok, msg = mm.write_back_swapkeys(
            self.mods_dir, None, emit=lambda line: self._emit_status(line))
        self._emit_finished(ok, msg)


# ===========================================================================
# 主窗口
# ===========================================================================

class ResourcesWindow(CenteredPopupMixin, QWidget):
    """《原神》「资源浏览」面板：640×360 弹出窗口。"""

    def __init__(self, parent: Optional[QWidget] = None, config=None,
                 tray_mode: bool = False) -> None:
        # parent 必须为 None：资源浏览窗口是独立顶层窗口（见 home._on_resources 注释）。
        #
        # 任务栏策略（2026-09-18 改）：此前恒用 ``Qt.Tool`` 把它藏进任务栏，理由
        # 是「可单独 SetWindowPos(HWND_TOPMOST) 置顶而不牵连 home」——但置顶独立性
        # 实际来自 **parent=None + Win32 SetWindowPos**，与 Qt.Tool 无关（Qt.Tool
        # 只负责 WS_EX_TOOLWINDOW）。改用 Qt.Window 后仍可用同一套置顶逻辑，
        # 同时满足「除首页外其它页面都要有任务栏图标」。
        # tray_mode 参数仅为与其它页面签名一致保留，不再影响 flags。
        super().__init__(parent, window_flags(hide_from_taskbar=False))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("ResourcesRoot")
        self.setFixedSize(WIN_W, WIN_H)
        self.init_geometry_persist(
            "resources", config, legacy_keys=("mod_manager",)
        )

        self._config = config
        self._chars: List[dict] = []
        self._cat1 = "all"
        self._cat2 = "all"
        # 分类模式（互斥，持久化在 cache/mods_classification.json）：
        #   folder 物理层级即分类，不猜角色也不读覆盖表；
        #   auto   名称匹配角色 + 手动覆盖表，不看 group 字段。
        # 当前版本暂时取消自动分类，强制使用文件夹模式。
        self._mode = mm.MODE_FOLDER
        # 一级筛选的「值」随模式变化：auto 固定 5 档；folder 动态 = 全部 + 分组
        self._cat1_keys: List[str] = list(CAT1_KEYS)
        # 文件夹模式扫到的物理分组（分组列表来自扫描结果，故动态）
        self._groups: List[str] = []
        # 文件夹模式：左侧目录树当前选中的文件夹路径（None 表示未选，渲染时回落到 Mods 根）
        self._selected_folder: Optional[str] = None
        self._fetch_thread = None
        self._fetch_sig = None
        self._drag_offset: Optional[QPoint] = None
        # 置顶状态：用私有字段维护（不走 setWindowFlag，见 set_pinned，避免闪烁）
        self._pinned = False
        # 扫描缓存（性能关键）：
        #   _items      原始扫描结果（只读盘一次）
        #   _classified 分类后的结果
        # 筛选切换 / 手动归类 / 角色数据更新 都只做内存操作，不再重复扫盘。
        self._items: List[dict] = []
        self._classified: List[dict] = []
        self._scan_thread = None
        self._scan_sig = None
        # 差分值写回：后台线程 + 侧边的「命令输出」页（懒创建，见 _cmd_output）
        self._wb_thread = None
        self._wb_sig = None
        self._cmd_out: Optional[OMGCmdOutput] = None
        # 用户操作结果在状态栏展示期间，后台消息不覆盖（见 _set_status）
        self._status_hold = False

        self.setStyleSheet(SETTINGS_QSS + MOD_QSS)

        # 外壳 frame：背景交给 ResourcesWindow.paintEvent 自绘磨砂圆角面板，
        # 这里只作内容容器（透明），不画不透明底——否则会盖掉磨砂透出效果。
        self._frame = QFrame(self)
        self._frame.setObjectName("SettingsFrame")
        self._frame.setGeometry(0, 0, WIN_W, WIN_H)
        self._frame.setStyleSheet(
            f"QFrame#SettingsFrame {{ background: transparent; border: none; }}"
        )

        root = QVBoxLayout(self._frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_title_bar(root)
        self._build_filter_bar(root)
        self._build_content(root)
        self._apply_mode_visibility()

        # 启动：加载缓存 + 后台扫描 + 必要时自动更新
        self._load_catalog_into_memory()
        self._start_scan()
        self._maybe_auto_update()

        # 自定义 tooltip 气泡：覆盖本窗口所有控件（含动态生成的 ItemCard /
        # FolderCard / ThumbLabel），由应用级过滤器兜底屏蔽原生 QToolTip 直角 + 阴影。
        install_pop_cards(self, lambda: "dark")

    # ------------------------------------------------------------------
    # 标题栏
    # ------------------------------------------------------------------
    def _build_title_bar(self, parent_layout: QVBoxLayout) -> None:
        bar = QFrame(self._frame)
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(32)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(4)
        title = QLabel("资源浏览", bar)
        title.setObjectName("TitleText")
        # 悬浮标题显示本窗口的全局热键；组合键由 OMGPopCard 渲染成 OMGKeyCap 键帽
        title.setToolTip("资源浏览")
        title.setProperty("popShortcut", "Alt+`")
        lay.addWidget(title)
        # 标题与「透明度」控件之间再补一点间距（布局整体 spacing=4 偏挤）
        lay.addSpacing(TITLE_GAP)

        # 透明度：移到标题右侧，只作用于资源浏览界面（主窗口的闲置淡出是另一套机制）
        lbl_op = QLabel("透明度", bar)
        lbl_op.setObjectName("TitleHint")
        lay.addWidget(lbl_op)
        self._opacity_edit = QLineEdit(bar)
        self._opacity_edit.setObjectName("OpacityEdit")
        self._opacity_edit.setFixedSize(34, 20)
        self._opacity_edit.setAlignment(Qt.AlignCenter)
        self._opacity_edit.setToolTip("资源浏览界面整体透明度（20~100），回车生效")
        self._opacity_edit.editingFinished.connect(self._on_opacity_edited)
        lay.addWidget(self._opacity_edit)
        lbl_pct = QLabel("%", bar)
        lbl_pct.setObjectName("TitleHint")
        lay.addWidget(lbl_pct)
        lay.addSpacing(6)
        lay.addStretch()

        # 「写回」按钮（在 ini 按钮左侧）：把 d3dx_user.ini 记录的差分值写回
        # 当前目录（递归）下各 .ini 的 global persist 默认值。外观与标题栏其它
        # 按钮完全一致（TitleBtn + 主题前景色线稿图标，无边框、hover 高亮）。
        self._btn_write = QPushButton(bar)
        self._btn_write.setObjectName("TitleBtn")
        self._btn_write.setFixedSize(WRITE_BTN_SIZE, WRITE_BTN_SIZE)
        self._btn_write.setIcon(
            tinted_icon(WRITE_ICON, WRITE_ICON_SIZE, TEXT_PRIMARY))
        self._btn_write.setIconSize(QSize(WRITE_ICON_SIZE, WRITE_ICON_SIZE))
        self._btn_write.setCursor(Qt.PointingHandCursor)
        self._btn_write.setToolTip("把差分值写回 Mods（GIMI 路径）下的 ini")
        self._btn_write.clicked.connect(self._on_write_btn_clicked)
        lay.addWidget(self._btn_write)

        # 「当前目录的 ini 文件」按钮：移到标题栏、置顶左侧，外观与标题栏其它
        # 按钮一致（无边框、hover 高亮）；点击弹当前文件夹 .ini 菜单。
        self._btn_ini = QPushButton(bar)
        self._btn_ini.setObjectName("TitleBtn")
        self._btn_ini.setFixedSize(INI_BTN_SIZE, INI_BTN_SIZE)
        self._btn_ini.setIcon(tinted_icon(INI_ICON, INI_ICON_SIZE, TEXT_PRIMARY))
        self._btn_ini.setIconSize(QSize(INI_ICON_SIZE, INI_ICON_SIZE))
        self._btn_ini.setCursor(Qt.PointingHandCursor)
        self._btn_ini.setToolTip("当前目录的 ini 文件")
        self._btn_ini.clicked.connect(self._on_ini_btn_clicked)
        lay.addWidget(self._btn_ini)

        # ini 与置顶之间的分隔线
        sep = QLabel(bar)
        sep.setObjectName("TitleSep")
        sep.setFixedSize(1, 16)
        lay.addWidget(sep)

        # 置顶：仅控制资源浏览界面，与主窗口 / 其它弹出窗口相互独立
        self._btn_pin = QPushButton(bar)
        self._btn_pin.setObjectName("TitleBtn")
        self._btn_pin.setFixedSize(24, 24)
        self._btn_pin.setCursor(Qt.PointingHandCursor)
        self._btn_pin.setCheckable(True)
        self._btn_pin.toggled.connect(self._on_pin_toggled)
        lay.addWidget(self._btn_pin)

        btn = QPushButton(bar)
        btn.setObjectName("TitleBtn")
        btn.setFixedSize(24, 24)
        btn.setIcon(tinted_icon(CLOSE_ICON, CLOSE_ICON_SIZE, TEXT_PRIMARY))
        btn.setIconSize(QSize(CLOSE_ICON_SIZE, CLOSE_ICON_SIZE))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.close)
        lay.addWidget(btn)
        parent_layout.addWidget(bar)

        # 回填已保存的外观偏好（置顶会重建窗口，故放在控件建好之后统一应用）
        self._apply_window_prefs()

    # ------------------------------------------------------------------
    # 窗口外观：置顶 / 透明度（只影响资源浏览界面，持久化在 config.json）
    # ------------------------------------------------------------------
    def _apply_window_prefs(self) -> None:
        """按 config 恢复置顶与透明度，并同步输入框与图标。"""
        self.set_pinned(wb.resolve_mm_always_on_top(self._config))
        self.set_opacity(wb.resolve_mm_opacity(self._config))

    def set_pinned(self, pinned: bool) -> None:
        """切换资源浏览界面的置顶。

        不走 Qt 的 ``setWindowFlag(WindowStaysOnTopHint)``：后者在部分 Qt 版本下
        会 destroy 原生窗口再重建、并顺带 hide 一下，肉眼表现为「点一下置顶窗口
        闪一下」。改用 Win32 ``SetWindowPos`` 直接改 z 序（见 ``_apply_topmost``），
        无重建、无可见性变化，因此不闪烁。状态用 ``self._pinned`` 维护，``show()``
        之后由 ``showEvent`` 重新应用，保证置顶在显隐之间持续有效。
        """
        pinned = bool(pinned)
        # 置顶图标（pin）必须在初始构造时也绘制：状态未变时本函数早退，但若把
        # _refresh_pin_icon 也关在早退里，首帧按钮会没有图标，需用户点击一次才
        # 出现（打包后尤其明显）。故图标刷新始终执行，置顶 z 序 / 存档仅在变化
        # 时才做。
        if pinned != self._pinned:
            self._pinned = pinned
            self._apply_topmost()
            self._save_window_pref(wb.KEY_MM_ALWAYS_ON_TOP, 1 if pinned else 0)
        self._refresh_pin_icon()

    def _apply_topmost(self) -> None:
        """用 Win32 ``SetWindowPos`` 直接切换窗口 z 序（HWND_TOPMOST / NOTOPMOST）。

        只改 z 序，不重建原生窗口、不改可见性、不抢焦点（SWP_NOACTIVATE），
        因此切换置顶时不会闪。非 Windows 平台退回 Qt 的 ``setWindowFlag``。
        """
        if sys.platform != "win32":
            self.setWindowFlag(Qt.WindowStaysOnTopHint, self._pinned)
            if self.isVisible():
                self.show()
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
        # HWND_TOPMOST = (HWND)-1，HWND_NOTOPMOST = (HWND)-2（按指针宽度补满）
        flag = ctypes.c_void_p(-1) if self._pinned else ctypes.c_void_p(-2)
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd), flag, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def _detach_owner(self) -> None:
        """显式断开 Windows 的 owner 关联，确保 Mod 置顶只影响自身。

        即便 Qt 层面 Mod 已是 ``parent=None`` 的独立顶层窗口，部分 Windows / Qt
        版本仍会把新弹出的工具窗口隐式挂到前台窗口（home）之下当 owner；而
        Windows 规则：对「被拥有窗口」执行 ``SetWindowPos(HWND_TOPMOST)`` 会把
        owner（home）也一并提为 topmost。这里强制把 ``GWLP_HWNDPARENT`` 清零，
        彻底消除 owner 关联，使置顶状态与其它任何页面互相独立。
        """
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        if hwnd == 0:
            return
        user32 = ctypes.windll.user32
        GWLP_HWNDPARENT = -8
        user32.SetWindowLongPtrW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWLP_HWNDPARENT,
                                 ctypes.c_void_p(0))

    def _refresh_pin_icon(self) -> None:
        """按置顶状态换图标：置顶=实心 pinned.svg，未置顶=空心 pin.svg。

        两个图标都不带颜色，统一用 tinted_icon 按主题前景色（TEXT_PRIMARY）着色，
        深色主题下自动为浅色；置顶态不再叠蓝色背景，只靠实心/空心区分状态。
        """
        if getattr(self, "_btn_pin", None) is None:
            return
        pinned = self._pinned
        icon = PIN_ICON if pinned else PIN_OFF_ICON
        self._btn_pin.setIcon(tinted_icon(icon, PIN_ICON_SIZE, TEXT_PRIMARY))
        self._btn_pin.setIconSize(QSize(PIN_ICON_SIZE, PIN_ICON_SIZE))
        self._btn_pin.setToolTip("取消置顶" if pinned else "置顶")
        # 阻断信号回环：toggled 只用来响应用户点击，程序化同步不应再触发一次
        blocked = self._btn_pin.blockSignals(True)
        self._btn_pin.setChecked(pinned)
        self._btn_pin.blockSignals(blocked)

    def _on_pin_toggled(self, checked: bool) -> None:
        """标题栏置顶按钮：checkable 按钮的状态即目标状态。"""
        self.set_pinned(checked)

    def set_opacity(self, percent: int) -> None:
        """设置资源浏览界面整体透明度（百分比 20~100）。"""
        value = wb.clamp_mm_opacity(percent)
        self.setWindowOpacity(value / 100.0)
        if getattr(self, "_opacity_edit", None) is not None:
            self._opacity_edit.setText(str(value))
        self._save_window_pref(wb.KEY_MM_OPACITY, value)

    def _on_opacity_edited(self) -> None:
        """透明度输入框回车 / 失焦：容错解析（允许带 %、允许小数）后生效。"""
        raw = self._opacity_edit.text().strip().rstrip("%").strip()
        try:
            value = int(round(float(raw)))
        except ValueError:
            value = wb.resolve_mm_opacity(self._config)  # 非法输入 → 回落到已存值
        self.set_opacity(value)

    def _save_window_pref(self, key: str, value) -> None:
        """写回 config 并落盘（config 为 None 时静默跳过，如 debug 入口）。"""
        if self._config is None:
            return
        try:
            self._config.set(key, value)
            self._config.save()
        except Exception as exc:  # 配置不可写不应影响界面
            logger.debug(f"保存资源浏览界面外观偏好失败：{exc}")

    # ------------------------------------------------------------------
    # 磨砂面板 + 置顶 z 序在显隐间的保持
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # type: ignore[override]
        """窗口显示：恢复几何 → 去 owner → 置顶 z 序 → 后台重扫。

        顺序很重要：
        1. super() 让 CenteredPopupMixin 完成首次居中 / 恢复上次位置；
        2. _detach_owner() 清零原生 owner，避免置顶牵连 home；
        3. _apply_topmost() 重新把本窗口钉到最前（绕过 Qt 的 flag 缓存）；
        4. _start_scan() 在后台线程重扫 Mods，不阻塞 UI。
        """
        super().showEvent(event)
        self._detach_owner()
        if getattr(self, "_pinned", False):
            self._apply_topmost()
        self._start_scan()
        # 打开窗口时不要让透明度输入框自动抢占焦点（它在标题栏 tab 序最前，
        # Qt 会在 show 时把初始焦点给它）。延迟到 show 完成后再清，确保清得掉。
        if getattr(self, "_opacity_edit", None) is not None:
            QTimer.singleShot(0, self._opacity_edit.clearFocus)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """磨砂透明背景（与首页 HomeWindow 同款）：自绘半透明圆角面板。

        窗口设了 ``WA_TranslucentBackground``、``_frame`` 已改为透明，面板由这里
        绘制——``BG_WINDOW`` 色 + 高 alpha（约 0.9）压在游戏 / 桌面内容之上，既透
        出背景、又足够压暗以保证文字与卡片可读；比纯透明更易读、比纯不透明更轻。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(BG_WINDOW)
        c.setAlpha(_WIN_ALPHA)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawRoundedRect(r, RADIUS_WIN, RADIUS_WIN)
        # 1px 高光描边（与首页一致，强化浮层边缘、提升可读性）
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, RADIUS_WIN, RADIUS_WIN)
        p.end()
        super().paintEvent(event)

    # ------------------------------------------------------------------
    # 筛选栏（固定，不随内容滚动）
    # ------------------------------------------------------------------
    def _build_filter_bar(self, parent_layout: QVBoxLayout) -> None:
        bar = QFrame(self._frame)
        bar.setObjectName("FilterBar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # 模式切换（两套逻辑互斥：文件夹层级 / 自动分类）
        row0 = QHBoxLayout()
        row0.setSpacing(8)
        self._seg_mode = SegmentedControl(MODE_ITEMS)
        # 先同步 UI 再连信号：构造时若当前模式非首段，避免触发一次多余回调
        cur = MODE_KEYS.index(self._mode) if self._mode in MODE_KEYS else 0
        if cur:
            self._seg_mode.setCurrent(cur)
        self._seg_mode.currentChanged.connect(self._on_mode_changed)
        row0.addWidget(self._seg_mode)
        row0.addStretch()
        lay.addLayout(row0)

        # 一级 + 刷新
        self._row1 = QHBoxLayout()
        self._row1.setSpacing(8)
        self._seg1 = SegmentedControl(CAT1_ITEMS)
        self._seg1.currentChanged.connect(self._on_cat1_changed)
        self._row1.addWidget(self._seg1, 1)

        self._btn_refresh = QPushButton("刷新数据", self)
        self._btn_refresh.setObjectName("ContentBtn")
        self._btn_refresh.setCursor(Qt.PointingHandCursor)
        self._btn_refresh.clicked.connect(self._on_refresh_clicked)
        # 文件夹模式不做名称匹配，角色数据用不上（模式切换时同步显隐）
        self._btn_refresh.setVisible(self._mode == mm.MODE_AUTO)
        self._row1.addWidget(self._btn_refresh)
        lay.addLayout(self._row1)

        # 二级（元素）
        self._seg2 = SegmentedControl(ELEMENT_LABELS)
        self._seg2.setVisible(False)
        self._seg2.currentChanged.connect(self._on_cat2_changed)
        lay.addWidget(self._seg2)

        # 状态行
        self._status = QLabel("", self)
        self._status.setObjectName("ModStatus")
        lay.addWidget(self._status)

        parent_layout.addWidget(bar)
        # 给 filter_bar 一个引用，方便在 folder 模式下整体隐藏
        # （暂时取消自动分类模式，保留控件实现以备后续恢复）
        self._filter_bar = bar

    # ------------------------------------------------------------------
    # 内容区
    # ------------------------------------------------------------------
    def _build_content(self, parent_layout: QVBoxLayout) -> None:
        # 面包屑栏：folder 模式专属，auto 模式隐藏；放在 stack 上方紧邻标题栏，
        # 这样视觉上标题栏 → 面包屑 → 内容堆叠形成清晰的「上导航 / 下内容」层次。
        self._build_breadcrumb()
        parent_layout.addWidget(self._breadcrumb)

        # 两种模式共用一个堆叠控件：索引 0 = 自动分类单栏网格；索引 1 = 文件夹网格。
        self._stack = QStackedWidget(self._frame)
        parent_layout.addWidget(self._stack, 1)

        # —— 自动分类模式面板（原单栏卡片网格）——
        self._auto_panel = QWidget(self._stack)
        ap_lay = QVBoxLayout(self._auto_panel)
        ap_lay.setContentsMargins(_FOLDER_MARGIN, 0, _FOLDER_MARGIN, _FOLDER_MARGIN)
        ap_lay.setSpacing(0)
        self._auto_scroll = QScrollArea(self._auto_panel)
        self._auto_scroll.setObjectName("ContentArea")
        self._auto_scroll.setWidgetResizable(True)
        self._auto_scroll.setFrameShape(QScrollArea.NoFrame)
        # 禁用水平滚动条：内容宽度强制适配视口，避免垂直滚动条挤出后
        # 内容 widget 仍按原宽度计算而触发水平条。
        self._auto_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content = QWidget(self._auto_scroll)
        self._content.setObjectName("PageContent")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 12, 12, 12)
        self._content_layout.setSpacing(14)
        self._auto_scroll.setWidget(self._content)
        ap_lay.addWidget(self._auto_scroll, 1)
        self._stack.addWidget(self._auto_panel)

        # —— 文件夹模式面板（仅卡片网格；顶部对齐 + 垂直方向不拉伸）——
        self._folder_panel = QWidget(self._stack)
        fp_lay = QVBoxLayout(self._folder_panel)
        # 关键：给滚动区在窗口内留出内边距（左右下各 _FOLDER_MARGIN），
        # 让竖向滚动条的方角落在磨砂圆角面板内部、不顶到窗口边缘；
        # 上边由面包屑自然分隔，故上边距为 0。网格内部另有 _FOLDER_MARGIN
        # 边距负责卡片与滚动条之间的留白，二者层级独立。
        fp_lay.setContentsMargins(_FOLDER_MARGIN, 0, _FOLDER_MARGIN, _FOLDER_MARGIN)
        fp_lay.setSpacing(0)
        self._folder_scroll = QScrollArea(self._folder_panel)
        self._folder_scroll.setObjectName("ContentArea")
        self._folder_scroll.setWidgetResizable(True)
        self._folder_scroll.setFrameShape(QScrollArea.NoFrame)
        # 禁用水平滚动条：三列网格 + 拉伸卡片应始终铺满视口宽度；
        # 若保留 Auto，垂直条挤出视口时会触发水平条，导致第三列被遮挡。
        self._folder_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 关键：少行（1-2 行）时让首行紧贴顶部，而不是垂直居中或被拉成全高。
        # QScrollArea 默认 widgetResizable=True 会让 widget 被 resize 到 viewport 高，
        # 这里改 AlignTop + 垂直 Fixed 让 widget 用实际内容高度，首行紧贴顶部。
        self._folder_scroll.setAlignment(Qt.AlignTop)
        self._folder_content = QWidget(self._folder_scroll)
        self._folder_content.setObjectName("PageContent")
        # Preferred/Expanding 在少行时会拉伸行高；改 Fixed 让行高 = _CARD_H + spacing。
        self._folder_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # 方案 C：3 列网格，列宽均分 → 卡片宽度 = (窗口宽 - 边距 - 间距) / 3，
        # 自动铺满窗口宽度；窗口缩放时列宽变化、卡片随之重排。
        self._folder_layout = QGridLayout(self._folder_content)
        self._folder_layout.setContentsMargins(_FOLDER_MARGIN, _FOLDER_MARGIN,
                                               _FOLDER_MARGIN, _FOLDER_MARGIN)
        self._folder_layout.setHorizontalSpacing(_FOLDER_SPACING)
        self._folder_layout.setVerticalSpacing(_FOLDER_SPACING)
        for _c in range(_FOLDER_COLS):
            self._folder_layout.setColumnStretch(_c, 1)
        self._folder_scroll.setWidget(self._folder_content)
        fp_lay.addWidget(self._folder_scroll, 1)
        self._stack.addWidget(self._folder_panel)

        self._selected_folder = None

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """当前分类模式（供卡片角标 / 右键菜单判断，见 MODE_KEYS）。"""
        return self._mode

    def _mods_dir(self) -> str:
        gimi = self._config.get_gimi_dir() if self._config else ""
        if not gimi:
            return ""
        return os.path.join(gimi, "Mods")

    def _load_catalog_into_memory(self) -> None:
        cat = gb.load_catalog()
        self._catalog = cat
        self._chars = cat["characters"] if cat else []
        self._update_status()

    def _set_status(self, text: str, hold: bool = False) -> None:
        """写状态栏。

        ``hold=True`` 表示这是**用户操作的直接结果**（如写回完成），短时间内
        不被后台任务（扫描 / 拉取角色数据）的进度与结果消息覆盖——否则异步
        操作的结论刚显示出来就被刷掉，用户根本看不到（见 _set_status_auto）。
        """
        if hold:
            self._status_hold = True
            QTimer.singleShot(STATUS_HOLD_MS, self._release_status_hold)
        self._status.setText(text)

    def _release_status_hold(self) -> None:
        self._status_hold = False

    def _set_status_auto(self, text: str) -> None:
        """后台任务刷新状态栏：用户操作结果展示期间（hold）一律不覆盖。"""
        if self._status_hold:
            return
        self._status.setText(text)

    def _update_status(self) -> None:
        if self._mode == mm.MODE_FOLDER:
            # 文件夹模式不依赖角色数据，隐藏「角色数据 N 个…」文本，避免误导
            self._set_status_auto("")
            return
        if not self._catalog:
            self._set_status_auto("角色数据未缓存，正在拉取…")
            return
        age = gb.catalog_age_days(self._catalog)
        age_txt = f"{age:.1f} 天前" if age is not None else "未知"
        self._set_status_auto(
            f"角色数据 {self._catalog.get('count', 0)} 个 · "
            f"更新于 {self._catalog.get('updated_at', '')[:10]}（{age_txt}）"
        )

    def _start_scan(self) -> None:
        """后台重新扫描 Mods 目录（不阻塞 UI）——**唯一会读盘**的路径。

        触发时机：构造时、每次窗口重新显示（showEvent）。
        筛选切换 / 手动归类 / 角色数据更新都**不会**走到这里。
        """
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return  # 防重入
        mods_dir = self._mods_dir()
        if not mods_dir:
            self._clear_content()
            self._show_empty("未配置 GIMI 目录（设置页 → GIMI 路径）")
            return
        self._status.setText("正在扫描 Mods…")
        sig = TaskSignals()
        sig.status.connect(self._on_scan_status)
        sig.finished.connect(self._on_scan_finished)
        self._scan_sig = sig
        self._scan_thread = ScanThread(mods_dir, self._chars, sig, self._mode)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.start()

    def _on_scan_status(self, text: str) -> None:
        self._set_status_auto(text)

    def _on_scan_finished(self, ok: bool, msg: str) -> None:
        """扫描线程结束：取回结果缓存起来，再渲染。"""
        thread = self._scan_thread
        if ok and thread is not None:
            self._items = thread.items
            self._classified = thread.classified
        if self._mode != mm.MODE_FOLDER:
            # 自动模式：一级筛选项来自实际扫描结果，分组变了要重建控件
            groups = mm.list_groups(self._classified)
            if groups != self._groups:
                self._groups = groups
                self._rebuild_seg1(keep=self._cat1)
        self._scan_thread = None
        self._scan_sig = None
        self._update_status()
        self._render()

    def _reclassify(self) -> None:
        """仅重分类（**不读盘**）：复用缓存的 _items 在内存里重跑分类。

        用于：切换分类模式 / 手动归类 / 取消归类 / 角色数据（缓存）更新后。
        分类依赖字符表与覆盖表，这两者变化都不需要重新扫盘。
        """
        self._classified = mm.classify_items(self._items, self._chars,
                                             None, self._mode)
        self._render()

    def _render(self) -> None:
        """按当前模式渲染内容（**纯内存切片，不读盘**）。"""
        if self._mode == mm.MODE_FOLDER:
            self._stack.setCurrentIndex(1)
            self._render_folder_contents()
        else:
            self._stack.setCurrentIndex(0)
            self._render_auto()

    def _render_auto(self) -> None:
        """自动分类模式：按一级 / 二级筛选渲染分组卡片网格（原单栏逻辑）。"""
        self._clear_content()
        if not self._mods_dir():
            self._show_empty("未配置 GIMI 目录（设置页 → GIMI 路径）")
            return
        if not self._items:
            self._show_empty("Mods 文件夹为空，把 mod 放进来后会自动分类")
            return
        groups = mm.build_view(self._classified, self._cat1, self._cat2,
                               self._mode)
        if not groups:
            self._show_empty("当前筛选下没有匹配项")
            return
        for g in groups:
            self._content_layout.addWidget(self._make_group(g))

    def _render_folder_contents(self) -> None:
        """文件夹模式：面包屑 + 单层卡片网格（子文件夹与 Mod 混排，按名称升序）。

        显示当前文件夹（_selected_folder）的**直接子项**；每个子项按「手动标记」或
        默认判定是文件夹还是 Mod：
          * 文件夹 → FolderCard（点击下钻，无状态条）；
          * Mod    → ItemCard（左侧绿/灰状态条，单击切换启用）。
        子文件夹与 Mod 不再分先后，统一按名称升序混排（用户要求）。
        """
        while self._folder_layout.count():
            w = self._folder_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._render_breadcrumb()
        mods = self._mods_dir()
        if not mods:
            self._show_folder_empty("未配置 GIMI 目录（设置页 → GIMI 路径）")
            return
        if self._selected_folder is None:
            self._selected_folder = mods
        if not self._items:
            self._show_folder_empty("Mods 文件夹为空，建好分组目录后把 mod 放进来")
            return
        sel = self._selected_folder
        # 直接子 Mod（父目录 == 选中文件夹的扫描条目）。
        # 用 _classified（而非 _items）：分类结果已附上 thumb 字段，Mod 卡片才能
        # 显示目录下以 preview 命名的预览图。
        mods_here = [it for it in self._classified
                     if os.path.dirname(it["path"]) == sel]
        mod_by_path = {it["path"]: it for it in mods_here}
        # 手动归类覆盖表（键已按末级 DISABLED 前缀规范化）
        overrides = mm.load_manual_kind()

        # 直接子目录名（含被扫描判定为 Mod 的目录，下面统一处理一次）
        folder_names = mm.list_folder_children(mods, sel)

        # 汇总所有直接子路径，每个只处理一次（避免 Mod 目录重复计入）
        child_paths: set = set()
        for n in folder_names:
            child_paths.add(os.path.join(sel, n))
        for it in mods_here:
            child_paths.add(it["path"])

        entries: List[dict] = []
        for fp in child_paths:
            name = os.path.basename(fp)
            rel = mm.norm_kind_rel(os.path.relpath(fp, mods))
            kind = overrides.get(rel)
            if kind is None:
                kind = "mod" if fp in mod_by_path else "folder"
            if kind == "folder":
                entries.append({"kind": "folder", "name": name, "path": fp})
            elif fp in mod_by_path:
                entries.append({"kind": "mod", "name": name, "item": mod_by_path[fp]})
            else:
                # 被手动标记为 Mod、但本身没有扫描结果的目录：合成条目
                entries.append({"kind": "mod", "name": name,
                                "item": self._synth_mod(fp)})

        if not entries:
            self._show_folder_empty("此文件夹下没有内容")
            return

        # 文件夹与 Mod 统一按名称升序混排；横排卡片按 3 列网格铺满窗口宽度
        entries.sort(key=lambda e: e["name"].lower())
        for idx, e in enumerate(entries):
            if e["kind"] == "folder":
                w = FolderCard(e["path"], self._folder_content)
            else:
                w = ItemCard(e["item"], self._folder_content, horizontal=True)
            self._folder_layout.addWidget(w, idx // _FOLDER_COLS, idx % _FOLDER_COLS)

    def _synth_mod(self, path: str) -> dict:
        """为一个被手动标记为 Mod 的目录合成条目（扫描结果里没有它时）。"""
        name = os.path.basename(path)
        return {
            "name": name,
            "path": path,
            "is_dir": True,
            "enabled": not mm.is_disabled(name),
            # 与扫描条目一致：Mod 卡片递归检索 preview（最多 3 层）
            "thumb": mm.find_thumbnail_deep({"path": path, "is_dir": True}),
            # 切换键：解析该目录 .ini 的 [Key*] 段（port of nahida-desktop）
            "toggle_keys": mm.parse_toggle_keys(
                {"path": path, "is_dir": True, "ext": ""}),
            "group": os.path.basename(os.path.dirname(path)),
            "category": None,
            "character": None,
            "element_id": None,
            "source": "folder",
        }

    def _show_folder_empty(self, text: str) -> None:
        label = QLabel(text, self._folder_content)
        label.setObjectName("ModEmpty")
        label.setWordWrap(True)
        self._folder_layout.addWidget(label, 0, 0, 1, _FOLDER_COLS)

    def _clear_content(self) -> None:
        while self._content_layout.count():
            w = self._content_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def _show_empty(self, text: str) -> None:
        label = QLabel(text, self._content)
        label.setObjectName("ModEmpty")
        label.setWordWrap(True)
        self._content_layout.addWidget(label)

    # ------------------------------------------------------------------
    # 文件夹模式：面包屑导航 + 单层网格
    # ------------------------------------------------------------------
    def _apply_mode_visibility(self) -> None:
        """根据模式切换筛选栏与堆叠页的可见性。

        当前版本（用户要求）**暂时取消自动分类模式**，故 folder 模式下：
          * 整个 filter_bar（含模式切换、一级/二级筛选、刷新按钮、状态行）全部隐藏；
          * 仅保留 breadcrumb + 文件夹卡片网格。
        状态行即便 filter_bar 显示也不再写值（_update_status 在 folder 模式置空），
        但仍保留控件实现，便于将来恢复 auto 模式时复用。
        """
        folder = self._mode == mm.MODE_FOLDER
        self._filter_bar.setVisible(not folder)
        self._breadcrumb.setVisible(folder)
        if hasattr(self, "_stack"):
            self._stack.setCurrentIndex(1 if folder else 0)

    def _breadcrumb_segments(self) -> List[Tuple[str, str]]:
        """从 Mods 根到当前选中文件夹的路径分段：[(标签, 绝对路径), ...]。

        首段固定为「Mods」（Mods 根目录本身）；之后每一层目录名一段。
        面包屑据此生成可点击的「屑」与「›」分隔符（点击弹该层下的子目录列表）。
        """
        mods = self._mods_dir()
        if not mods:
            return []
        target = self._selected_folder or mods
        rel = os.path.relpath(target, mods)
        segs = [] if rel in ("", ".") else rel.split(os.sep)
        out: List[Tuple[str, str]] = [("Mods", mods)]
        acc = mods
        for s in segs:
            acc = os.path.join(acc, s)
            out.append((s, acc))
        return out

    def _build_breadcrumb(self) -> None:
        """创建面包屑栏容器（内容在 _render_breadcrumb 里按当前路径动态重建）。

        父级用 self._frame——breadcrumb 是 stack 外部的「导航条」，与 stack 同级
        挂在 root 布局上，不能再挂在已弃用的 _folder_panel 下。
        """
        self._breadcrumb = QWidget(self._frame)
        self._breadcrumb.setObjectName("Breadcrumb")
        self._breadcrumb.setFixedHeight(30)
        self._bc_layout = QHBoxLayout(self._breadcrumb)
        self._bc_layout.setContentsMargins(12, 2, 12, 2)
        self._bc_layout.setSpacing(0)

    def _render_breadcrumb(self) -> None:
        """按当前路径重建面包屑：屑可点击回跳，› 点击弹该层子目录列表。"""
        while self._bc_layout.count():
            w = self._bc_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        segs = self._breadcrumb_segments()
        for i, (label, path) in enumerate(segs):
            if i > 0:
                # 分隔符 ›：列出其左侧「屑」路径下的直接子目录，供快速跳转
                prev_path = segs[i - 1][1]
                sep = QPushButton("›", self._breadcrumb)
                sep.setObjectName("BreadcrumbSep")
                sep.setCursor(Qt.PointingHandCursor)
                sep.clicked.connect(
                    lambda *_, p=prev_path: self._bc_sep_menu(p))
                self._bc_layout.addWidget(sep)
            crumb = QPushButton(label, self._breadcrumb)
            crumb.setObjectName("BreadcrumbCrumb")
            crumb.setCursor(Qt.PointingHandCursor)
            crumb.clicked.connect(lambda *_, p=path: self.navigate_to(p))
            self._bc_layout.addWidget(crumb)
        self._bc_layout.addStretch(1)

    def _bc_sep_menu(self, path: str) -> None:
        """分隔符 › 被点击：弹出 path 下的直接子目录列表，选中即跳转。"""
        menu = QMenu(self)
        children = mm.list_folder_children(self._mods_dir(), path)
        if not children:
            act = menu.addAction("（无子目录）")
            act.setEnabled(False)
        else:
            for c in sorted(children):
                child_path = os.path.join(path, c)
                a = menu.addAction(c)
                a.triggered.connect(
                    lambda *_, p=child_path: self.navigate_to(p))
        menu.exec(QCursor.pos())

    def _on_write_btn_clicked(self) -> None:
        """标题栏「写回」按钮：d3dx_user.ini 的差分值 → **GIMI 路径 / Mods**。

        3DMigoto 把切换键的当前取值记在 GIMI 根的 d3dx_user.ini，而 mod 自己的
        .ini 里是初始值；本动作把前者写回后者的 ``global persist $x = v`` 行，
        让当前状态固化成新的默认值（详见 mm.write_back_swapkeys）。

        作用域**固定为 Mods 根**（不再跟随当前浏览目录）：差分键由「mod 相对
        Mods 根的路径」构成，只写回子目录会让同一 mod 的其它 ini 与 d3dx_user
        不一致。执行期间在窗口侧面弹出「命令输出」页实时打印，结束后延时自动
        关闭，最终结果 / 错误记入 log。
        """
        mods = self._mods_dir()
        if not mods or not os.path.isdir(mods):
            msg = "写回未执行：未配置 GIMI 目录"
            self._set_status(msg, hold=True)
            logger.error("写回差分值失败：%s（mods=%r）", msg, mods)
            return
        if self._wb_thread is not None and self._wb_thread.isRunning():
            return                                   # 防重入

        panel = self._cmd_output()
        panel.reset("写回差分值")
        panel.append(f"Mods：{mods}")
        side = panel.place_beside(self)
        panel.set_topmost(self._pinned)
        panel.show()
        logger.info("写回差分值开始（输出页在本窗口%s）",
                    "右侧" if side == "right" else "左侧")

        self._btn_write.setEnabled(False)
        sig = TaskSignals()
        sig.status.connect(panel.append)
        sig.finished.connect(self._on_write_finished)
        self._wb_sig = sig
        self._wb_thread = WriteBackThread(mods, sig, self)
        self._wb_thread.finished.connect(self._wb_thread.deleteLater)
        self._wb_thread.start()

    def _on_write_finished(self, ok: bool, msg: str) -> None:
        """写回线程结束：收尾输出页 + 状态栏 + 把结果 / 错误记入 log。"""
        self._wb_thread = None
        self._btn_write.setEnabled(True)
        self._set_status(msg if ok else f"写回未执行：{msg}", hold=True)
        if self._cmd_out is not None:
            self._cmd_out.append(("完成：" if ok else "失败：") + str(msg))
            self._cmd_out.mark_done(ok)
        if ok:
            logger.info("写回差分值完成：%s", msg)
        else:
            logger.error("写回差分值失败：%s", msg)
        # 留一小会儿让用户看到最后一行，再自动关闭输出页
        QTimer.singleShot(CMD_OUT_CLOSE_DELAY, self._close_cmd_output)

    def _cmd_output(self) -> OMGCmdOutput:
        """懒创建「命令输出」侧页（窗口存活期间复用同一实例）。"""
        if self._cmd_out is None:
            self._cmd_out = OMGCmdOutput(theme="dark")
        return self._cmd_out

    def _close_cmd_output(self) -> None:
        """关闭输出页（关闭按钮 / 自动关闭 / 窗口退出都会走到这里）。"""
        if self._cmd_out is not None and self._cmd_out.isVisible():
            self._cmd_out.hide()

    def _on_ini_btn_clicked(self) -> None:
        """标题栏「当前目录的 ini 文件」按钮：弹出当前文件夹（_selected_folder，
        未选时回落 Mods 根）下的全部直接 .ini 文件，点击用系统默认应用打开。
        """
        folder = self._selected_folder or self._mods_dir()
        menu = QMenu(self)
        inis = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith(".ini") and os.path.isfile(os.path.join(folder, f))
        ) if folder and os.path.isdir(folder) else []
        if not inis:
            act = menu.addAction("（本文件夹无 .ini 文件）")
            act.setEnabled(False)
        else:
            for f in inis:
                ini_path = os.path.join(folder, f)
                a = menu.addAction(f)
                a.triggered.connect(
                    lambda *_, p=ini_path: QDesktopServices.openUrl(
                        QUrl.fromLocalFile(p)))
        menu.exec(QCursor.pos())

    def navigate_to(self, path: str) -> None:
        """面包屑/文件夹卡片下钻：跳到 path 并重建面包屑 + 内容网格。"""
        if path and os.path.isdir(path):
            self._selected_folder = path
            self._render_folder_contents()

    # ------------------------------------------------------------------
    # 文件夹模式：卡片点击切换启用 / 禁用
    # ------------------------------------------------------------------
    def mark_kind(self, path: str, kind: str) -> None:
        """右键「标记为 Mod / 文件夹」：持久化手动归类并重渲染当前文件夹。

        kind ∈ {"mod","folder"}。覆盖键按末级 DISABLED 前缀规范化，故切换启用
        （加 DISABLED 前缀改名）后覆盖仍命中，不会因重命名丢失。
        """
        mods = self._mods_dir()
        if not mods or not path:
            return
        rel = os.path.relpath(path, mods)
        mm.save_manual_kind(rel, kind)
        # 重新渲染网格（含面包屑）：当前文件夹下的卡片种类立即反映
        self._render_folder_contents()

    def toggle_mask(self, path: str) -> bool:
        """右键「遮罩预览图 / 取消遮罩」：翻转并持久化该 mod 的缩略图遮罩。

        返回翻转后的状态（True=已遮罩），由卡片就地给缩略图上/去高斯模糊——
        不整页重渲染，避免上百张卡片重建。
        """
        mods = self._mods_dir()
        if not mods or not path:
            return False
        rel = mm.norm_kind_rel(os.path.relpath(path, mods))
        new_on = not mm.is_masked(rel)
        mm.set_masked(rel, new_on)
        return new_on

    def toggle_mod_enabled(self, item: dict) -> None:
        """卡片单击：切换该 Mod 的启用/禁用（重命名加/去 DISABLED 前缀，真实改盘）。"""
        old_path = item.get("path", "")
        if not old_path or not os.path.exists(old_path):
            return
        new_enabled = not item.get("enabled", True)
        ok, new_path, msg = mm.set_mod_enabled(old_path, new_enabled)
        if not ok:
            self._set_status(f"切换失败：{msg}", hold=True)
            return
        # 就地更新缓存（_items 与 _classified 是两份拷贝，都同步）
        item["name"] = os.path.basename(new_path)
        item["path"] = new_path
        item["enabled"] = new_enabled
        for c in self._classified:
            if c.get("path") == old_path:
                c["name"] = item["name"]
                c["path"] = new_path
                c["enabled"] = new_enabled
                break
        # 重命名（加/去 DISABLED 前缀）会改变目录名，预览图在目录内部、其路径
        # 一并变化；旧 thumb 路径已失效 → 按新路径重算缩略图，否则重渲染时
        # isfile 失败、预览图退回纯灰占位（表现为「启用/禁用后略缩图消失」）。
        new_thumb = mm.find_thumbnail_deep(item)
        item["thumb"] = new_thumb
        for c in self._classified:
            if c.get("path") == new_path:
                c["thumb"] = new_thumb
                break
        # 重新渲染网格（含面包屑）：重命名后的新路径 / 启用状态会即时反映
        self._render_folder_contents()

    def _make_group(self, group: dict) -> QWidget:
        wrap = QWidget(self._content)
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        # 组头
        head = QLabel(group.get("title", ""), wrap)
        head.setObjectName("GroupHead")
        if group.get("badge"):
            head.setText(f"{group['title']}  ·  {group['badge']}")
        v.addWidget(head)
        # 卡片网格
        flow_widget = QWidget(wrap)
        flow = FlowLayout(flow_widget, margin=0, spacing=10)
        for it in group["items"]:
            flow.addWidget(ItemCard(it, flow_widget))
        v.addWidget(flow_widget)
        return wrap

    # ------------------------------------------------------------------
    # 筛选回调
    # ------------------------------------------------------------------
    def _rebuild_seg1(self, keep: Optional[str] = None) -> None:
        """按当前模式重建一级筛选控件。

        文件夹模式的分组列表来自扫描结果，是动态的；而 SegmentedControl 的段在
        构造时固定、没有换项接口，所以直接换掉控件（放在 _row1 的原位置）。
        Args:
            keep: 重建后要保留的选中值（分组仍存在时生效）；None 表示复位到「全部」。
        """
        old = self._seg1
        idx = self._row1.indexOf(old)
        self._row1.removeWidget(old)
        old.setParent(None)
        old.deleteLater()

        items: List[str] = list(CAT1_ITEMS)
        self._cat1_keys = list(CAT1_KEYS)
        if self._mode == mm.MODE_FOLDER:
            items = ["全部"] + [g or mm.UNGROUPED_LABEL for g in self._groups]
            self._cat1_keys = ["all"] + list(self._groups)

        seg = SegmentedControl(items)
        # 先同步选中再连信号，避免构造期间触发一次多余的渲染
        self._cat1 = "all"
        if keep and keep in self._cat1_keys:
            self._cat1 = keep
            seg.setCurrent(self._cat1_keys.index(keep))
        seg.currentChanged.connect(self._on_cat1_changed)
        if idx < 0:
            idx = 0
        self._row1.insertWidget(idx, seg, 1)
        self._seg1 = seg

    def _on_mode_changed(self, idx: int) -> None:
        """切换分类模式：两套逻辑互斥，换模式即换整套判定规则与布局。"""
        mode = MODE_KEYS[idx] if 0 <= idx < len(MODE_KEYS) else mm.MODE_AUTO
        if mode == self._mode:
            return
        self._mode = mode
        mm.save_mode(mode)
        folder = mode == mm.MODE_FOLDER
        self._apply_mode_visibility()
        if folder:
            # 文件夹模式：重置选中到 Mods 根；渲染时重建面包屑 + 内容网格
            self._selected_folder = None
        else:
            # 自动模式：一级筛选项整个换掉并复位；二级元素筛选只对自动模式有意义
            self._cat2 = "all"
            groups = mm.list_groups(self._classified)
            self._groups = groups
            self._rebuild_seg1()
        # 分类结果按新模式在内存里重算，不必重新扫盘
        self._reclassify()

    def _on_cat1_changed(self, idx: int) -> None:
        self._cat1 = (self._cat1_keys[idx]
                      if 0 <= idx < len(self._cat1_keys) else "all")
        # 二级（元素）仅「自动分类模式 + 一级=角色」时才出现
        self._seg2.setVisible(self._mode == mm.MODE_AUTO
                              and self._cat1 == gb.CATEGORY_CHARACTER)
        self._render()  # 纯内存切片，不重新扫盘

    def _on_cat2_changed(self, idx: int) -> None:
        self._cat2 = ELEMENT_ID_ORDER[idx]
        self._render()  # 纯内存切片，不重新扫盘

    # ------------------------------------------------------------------
    # 刷新数据（后台线程）
    # ------------------------------------------------------------------
    def _maybe_auto_update(self) -> None:
        if gb.is_stale(self._catalog):
            self._start_fetch()

    def _on_refresh_clicked(self) -> None:
        self._start_fetch()

    def _start_fetch(self) -> None:
        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            return
        self._fetch_sig = TaskSignals()
        self._fetch_sig.status.connect(self._on_fetch_status)
        self._fetch_sig.finished.connect(self._on_fetch_finished)
        self._fetch_thread = FetchThread(self._fetch_sig)
        self._fetch_thread.finished.connect(self._fetch_thread.deleteLater)
        self._btn_refresh.setText("")
        self._btn_refresh.setEnabled(False)
        self._status.setText("正在拉取角色数据…")
        self._fetch_thread.start()

    def _on_fetch_status(self, text: str) -> None:
        self._set_status_auto(text)

    def _on_fetch_finished(self, ok: bool, msg: str) -> None:
        self._btn_refresh.setEnabled(True)
        self._btn_refresh.setText("刷新数据")
        if ok:
            self._load_catalog_into_memory()
            # 分类依赖角色表：复用缓存条目在内存里重分类即可，不必重扫磁盘
            self._reclassify()
            self._set_status_auto(f"{msg} · 分类已刷新")
        else:
            self._set_status_auto(f"刷新失败：{msg}（沿用已有缓存）")
        # 线程已结束且接了 deleteLater：置空引用，避免 closeEvent 访问到已被销毁的
        # C++ 对象（同理见 _on_scan_finished）。否则关窗时 t.isRunning() 会抛
        # "Internal C++ object already deleted"。
        self._fetch_thread = None
        self._fetch_sig = None

    # ------------------------------------------------------------------
    # 手动归类（供 ItemCard 上下文菜单调用）
    # ------------------------------------------------------------------
    def apply_override(self, name: str, category: str,
                       character_id: Optional[str] = None) -> None:
        mm.save_override(name, category, character_id)
        self._reclassify()  # 覆盖表已写盘，这里只做内存重分类，不重扫 Mods

    def reset_override(self, name: str) -> None:
        mm.clear_override(name)
        self._reclassify()  # 同上：仅内存重分类

    def request_reclassify_character(self, name: str) -> None:
        if not self._chars:
            self._status.setText("角色数据未就绪，无法归类到角色")
            return
        dlg = ReclassifyDialog(self._chars, self)
        if dlg.exec() == QDialog.Accepted:
            cid = dlg.picked_character_id()
            if cid:
                self.apply_override(name, gb.CATEGORY_CHARACTER, cid)

    # ------------------------------------------------------------------
    # 拖拽 / 关闭（与 quick_build 一致）
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        for t in (self._fetch_thread, self._scan_thread, self._wb_thread):
            if t is None:
                continue
            try:
                # 引用可能仍非空但 C++ 对象已被 finished→deleteLater 销毁；
                # 任何方法调用都会抛 RuntimeError，直接跳过即可。
                if t.isRunning():
                    t.request_cancel()
                    t.wait(1500)
            except RuntimeError:
                pass
        # 输出页是本窗口的伴生窗口，随窗口一并收起（不让它留在屏幕上）
        self._close_cmd_output()
        super().closeEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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


# ---------------------------------------------------------------------------
# 样式补充
# ---------------------------------------------------------------------------

MOD_QSS = f"""
QScrollArea#ContentArea {{ background: transparent; border: none; }}
QLabel#TitleText {{ color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 600; }}
QFrame#TitleBar {{ background: transparent; }}
QPushButton#TitleBtn {{ background: transparent; border: none; }}
QPushButton#TitleBtn:hover {{ background: {BG_CARD_HOVER}; border-radius: 4px; }}
/* 置顶状态不再用蓝色背景强调，只靠图标实心/空心区分（见 _refresh_pin_icon） */
QLabel#TitleHint {{ color: {TEXT_SECONDARY}; font-size: 11px; }}
QLineEdit#OpacityEdit {{
    background: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: 4px;
    padding: 0px 2px; font-size: 11px;
}}
QLineEdit#OpacityEdit:focus {{ border: 1px solid {ACCENT}; }}

QFrame#FilterBar {{ background: transparent; }}

QLabel#ModStatus {{ color: {TEXT_SECONDARY}; font-size: 11px; }}
QLabel#GroupHead {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
QLabel#ModEmpty {{ color: {TEXT_SECONDARY}; font-size: 12px; padding: 20px; }}

/* ModCard/FolderCard 背景 + 边框走 paintEvent，这里只保留文字样式 */
QLabel#ModName {{ color: {TEXT_PRIMARY}; font-size: 12px; }}
QLabel#ModBadge {{ color: {TEXT_SECONDARY}; font-size: 11px; }}
/* 右下角「切换键」图标按钮：常态细描边 + 次级灰图标（图标色由 ModKeyButton
   显式切换），悬浮提亮底色并把边框转成强调色，按下再压暗一档。 */
QPushButton#ModKeyBtn {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 0px;
}}
QPushButton#ModKeyBtn:hover {{
    background: {BG_CARD_HOVER};
    border: 1px solid {ACCENT};
}}
QPushButton#ModKeyBtn:pressed {{
    background: {BG_INPUT};
    border: 1px solid {ACCENT_HOVER};
}}

QPushButton#ContentBtn {{
    background: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: {RADIUS_CTRL}px;
    padding: 5px 12px; font-size: 12px;
}}
QPushButton#ContentBtn:hover {{ border: 1px solid {ACCENT}; }}
QPushButton#ContentBtn:disabled {{ color: {TEXT_SECONDARY}; }}

QPushButton#PrimaryBtn {{
    background: {ACCENT}; color: {TEXT_ON_ACCENT};
    border: none; border-radius: {RADIUS_CTRL}px;
    padding: 6px 16px; font-size: 12px; font-weight: 600;
}}
QPushButton#PrimaryBtn:hover {{ background: {ACCENT_HOVER}; }}

QDialog#ReclassifyDialog {{
    background: {BG_WINDOW}; border: 1px solid {BORDER};
    border-radius: {RADIUS_WIN}px;
}}
QLineEdit {{
    background: {BG_INPUT}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: {RADIUS_CTRL}px;
    padding: 6px 10px; font-size: 12px;
}}
QListWidget {{
    background: {BG_CARD}; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER}; border-radius: {RADIUS_CTRL}px;
    outline: none;
}}
QListWidget::item {{ padding: 6px 10px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {ACCENT}; color: {TEXT_ON_ACCENT}; }}

QWidget#Breadcrumb {{ background: transparent; }}
QPushButton#BreadcrumbCrumb {{
    background: transparent; border: none;
    color: {ACCENT}; font-size: 12px; padding: 2px 5px; border-radius: 4px;
}}
QPushButton#BreadcrumbCrumb:hover {{ background: {BG_CARD_HOVER}; }}
QPushButton#BreadcrumbSep {{
    background: transparent; border: none;
    color: {TEXT_SECONDARY}; font-size: 12px; padding: 2px 3px; border-radius: 4px;
}}
QPushButton#BreadcrumbSep:hover {{ background: {BG_CARD_HOVER}; color: {TEXT_PRIMARY}; }}
QLabel#TitleSep {{ background: {TEXT_SECONDARY}; border: none; }}

QLabel#FolderName {{ color: {TEXT_PRIMARY}; font-size: 12px; }}
QLabel#FolderThumb {{ background: {_THUMB_BG}; border-radius: {RADIUS_CTRL}px; }}
QFrame#ModStatusBar {{ border-radius: 2px; }}
"""


if __name__ == "__main__":
    app = QApplication([])
    app.setStyleSheet(SETTINGS_QSS)
    win = ResourcesWindow()
    win.show()
    import sys
    sys.exit(app.exec())
