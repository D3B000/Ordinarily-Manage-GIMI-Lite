"""菜单图标与项高的统一口径（首页面板菜单 / 托盘右键菜单共用）。

菜单的留白节奏之所以要做进图标里，而不是靠 QSS：QMenu 的动作布局矩形**同时**
决定图标列与 hover 高亮块的绘制范围，两者左缘恒齐平，QSS 的 padding / margin
（含负值）都无法让它们错开（实测：margin 连内容一起平移，或插进图标中间）。
于是把「块内图标左侧留空」画成图标自带的透明左内边距，字形被整体右推
``MENU_GUTTER``，四段留白就都相等了。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QProxyStyle, QStyle

from omg.ui.icon_loader import svg_icon

# 菜单项留白节奏（四段等距，单位 px，均为 MENU_GUTTER）：
#   边框 → 高亮块左端（QMenu padding-left）
#   → 图标字形（图标自带透明左内边距 MENU_GUTTER）
#   → 文本（item padding-left 3 + 样式固定 7）
MENU_GUTTER = 10
MENU_ICON_SIZE = 14                                  # 菜单图标字形尺寸
MENU_ICON_COL = MENU_ICON_SIZE + MENU_GUTTER         # 图标列宽（含左侧透明留空）

# 项内容高度：图标只按 14px 自然高绘制，但 QMenu 为它预留 PM_SmallIconSize（方形
# 24px）的盒子 → 带图标项内容高 24、无图标项只有文字高 17，一高一矮（36 vs 29）。
# 这里固定为「文字行高 17」，让图标菜单与全应用其它菜单的项高完全一致。
# 必须 >= 文字行高（否则文字被裁）且 >= 图标高 14（否则图标被裁）；改 font-size
# 时同步改它，回归脚本会校验墨迹没被裁掉。
MENU_ITEM_CONTENT_H = 17

# 用了这套图标的菜单统一挂这个 objectName：HOME_QSS 里 ``QMenu#IconMenu::item``
# 靠它把项内容高度压到 MENU_ITEM_CONTENT_H（见上）。托盘菜单同样要挂。
ICON_MENU_ID = "IconMenu"


class _MenuIconStyle(QProxyStyle):
    """把菜单的 ``PM_SmallIconSize`` 提到 ``MENU_ICON_COL``，避免图标被缩小。

    QMenu 按样式表的 SmallIconSize（默认 16）请求/绘制图标，宽 24 的
    「字形 + 透明留空」图标会被等比缩小到 16×9——10px 留空被压缩成 6.7px，
    字形高度也从 14 缩到 9，两者都破坏等距节奏。本样式只挂在菜单上
    （进程共享、自持引用），不影响其它控件的图标尺寸。
    """

    def pixelMetric(self, metric, option, widget=None):  # type: ignore[override]
        if metric == QStyle.PM_SmallIconSize and (
                widget is None or isinstance(widget, QMenu)):
            return MENU_ICON_COL
        return super().pixelMetric(metric, option, widget)


_MENU_ICON_STYLE_CACHE: dict = {}


def menu_icon_style() -> _MenuIconStyle:
    """进程内共享的菜单图标样式（QMenu.setStyle 不接管所有权，需自持引用）。"""
    st = _MENU_ICON_STYLE_CACHE.get("style")
    if st is None:
        st = _MenuIconStyle()
        _MENU_ICON_STYLE_CACHE["style"] = st
    return st


def _svg_icon(filename: str, px: int = 16, color: str = "#FFFFFF") -> QIcon:
    """从 resources/icons/home/ 加载 SVG 为单色 ``QIcon``（统一染成白色剪影）。"""
    return svg_icon("home/" + filename, px, color)


def menu_icon(filename: Optional[str]) -> QIcon:
    """菜单项图标：左侧带 ``MENU_GUTTER`` px 透明内边距。

    ``filename`` 为 ``None`` 时返回一个**全透明**的同尺寸占位图标：不显示任何
    字形，但仍占住 24px 图标列，使该项文本与带图标项左对齐（托盘菜单用它做到
    「不加图标、只对齐缩进」）。
    """
    pm = QPixmap(MENU_ICON_COL, MENU_ICON_SIZE)
    pm.fill(Qt.transparent)
    if filename:
        src = _svg_icon(filename, MENU_ICON_SIZE).pixmap(
            QSize(MENU_ICON_SIZE, MENU_ICON_SIZE))
        painter = QPainter(pm)
        painter.drawPixmap(MENU_GUTTER, 0, src)
        painter.end()
    return QIcon(pm)


def new_icon_menu(parent=None) -> QMenu:
    """按统一口径造一个菜单：挂 objectName + 防缩放的图标样式。

    ``setStyle`` 不接管所有权，调用方要自己持有样式引用（本模块用进程级缓存
    保存，故不必额外持有）。
    """
    menu = QMenu(parent) if parent is not None else QMenu()
    menu.setObjectName(ICON_MENU_ID)
    menu.setStyle(menu_icon_style())
    return menu
