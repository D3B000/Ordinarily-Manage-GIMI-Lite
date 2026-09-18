"""
OMGLite - Home Window
遵循 REFACTOR_PLAN.md 目录结构：src/omg/pages/home.py

窗口规格：
  - 尺寸：176×42（无系统标题栏，FramelessWindowHint），宽度随版本文本动态变化（最小 176）
  - 布局：水平布局（QHBoxLayout），子控件垂直居中（AlignVCenter）
  - 风格：21th / Windows 11 现代风格（深色 #1F1F21 背景）
  - 控件从左到右依次：
      1) 10×26  拖拽区
      2) 26×26  菜单按钮（点击弹出菜单：设置 / 置顶 / 隐藏 / 退出程序）
      3) 26×26  工具按钮（点击弹出菜单，当前含「便捷构建」占位入口）
      4) 垂直分隔线
      5) 26×26  开始按钮
      6) 26px 高矩形区域（InfoZone，内含 ghost 切换/更新按钮 + 版本文本）

InfoZone 设计要点：
  - 文本：始终只显示本地 GIMI 版本号（10px / SemiBold，高对比主色）。不显示
    “已是最新版本 / 发现新版本 / 正在更新 / 错误”等动态状态文本，状态一律通过
    ghost 图标 + tooltip 表达。
  - 宽度：按当前版本文本实际宽度动态计算（下界 ZONE_W=56，对应窗口最小宽 176）。
  - ghost 按钮：22×22，留出呼吸边；hover 切换 change.svg，更新可用时切换 arrow-up.svg，
    检查中叠加 progress_ring 加载圈（匀速扫圈 + 拖影）。
  - 已移除：InfoZone 进度条填充（更新中不再把 InfoZone 变为进度条）。

更新检查交互（手动）：
  - ghost 按钮空闲悬浮 → change.svg；点击检查本地 GIMI 与 GitHub 版本差异。
  - 相同（含本地更新）→ 文本不变（仍为版本号），ghost 保持 gi.svg，
    tooltip “已是最新 · 点击重新检查”。
  - 远端更新 → ghost 图标变 arrow-up.svg，tooltip “x.x.x -> x.x.x”（文本仍为版本号）。
  - 再次点击 → 更新：ghost 上叠加 progress_ring 加载圈，tooltip 显示进度 / 状态信息
    （文本仍为版本号）；点击加载圈（任意位置）即中断更新，触发 GIMI 目录回退并删除临时 zip。
  - 更新完毕 → ghost 恢复 gi.svg，刷新版本号。
  - 检查中 ghost 上叠加 progress_ring 加载圈：蓝色条沿轨道匀速扫一圈
    （带拖影 / 动感模糊），循环至下一阶段；点击圈即中断检查。
  - 更新中 ghost 上叠加 progress_ring 加载圈（点击可中断，显示确定进度）；
    失败时文本仍为版本号，tooltip 保留错误详情。
"""

from __future__ import annotations

import math
import re
import sys
from typing import Optional

from PySide6.QtCore import (
    Qt, QByteArray, QElapsedTimer, QEvent, QPoint, QPointF, QSize, QRectF, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QIcon, QPainter, QPainterPath, QPen, QBrush, QPixmap,
    QFontMetrics,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QWidget,
    QSizePolicy,
)

from omg.domain.launch.controller import LaunchController
from omg.domain.settings import KEY_AUTO_CHECK  # 打开 OMG 时检查更新（与设置页 GIMI 页开关同源）
from omg.core.paths import (
    read_gimi_version_from_ini,
    read_orfix_version,
    read_teffx_version,
)
# 只导入轻量的信号类（仅依赖 QtCore）。CheckThread / UpdateThread 会拉起
# omg.core.gimi_update → download → urllib.request → ssl（连带 5 MB 的
# libcrypto-3.dll），因此下沉到「自动检查 / 用户点击」触发时再导入（见 _start_check）。
from omg.core.gimi_signals import GimiUpdateSignals
from omg.core.window_behavior import (
    KEY_OMG_ALWAYS_ON_TOP,
    resolve_launch_hold_enabled,
    resolve_launch_hold_ms,
)
from omg.ui.global_hotkey import MOD_ALT, VK_OEM_3, GlobalHotkey
from omg.ui.idle_fade import IdleFadeController
from omg.ui.OMGPopCard import install_pop_cards
from omg.ui.window_flags import window_flags  # 顶层 flags 单一真源
from omg.ui.window_geo import CenteredPopupMixin  # 几何记忆：首次居中 / 之后按上次位置
from omg.widgets.progress_ring import DownloadProgressRing

from omg.core.logging_setup import logger


# ===========================================================================
# 21th 风格 (Windows 11 现代) 颜色 Token
# ===========================================================================
BG_WINDOW = "#1F1F21"         # 不透明背景
BG_CARD = "#2C2C2E"           # 卡片/按钮底色
BG_CARD_HOVER = "#38383B"     # 悬停态
BG_CARD_PRESSED = "#444448"   # 按下态
BORDER = "#3A3A3C"            # 实色边框
ACCENT = "#0A84FF"            # iOS 系统蓝（深色模式，开始按钮强调色）
ACCENT_HOVER = "#409CFF"      # 系统蓝悬停（更亮）
ACCENT_PRESSED = "#0A6ED1"    # 系统蓝按下（更暗）
SEPARATOR = "#3A3A3C"         # 分隔线
DRAG_BG = "#4C4C50"           # 拖拽区背景（较原 #3A3A3C 更浅）
TEXT_PRIMARY = "#F5F5F7"      # 主文字色（浅，用于深色背景）
TEXT_SECONDARY = "#98989D"    # 次文字色
ICON_COLOR = "#FFFFFF"        # 图标统一色（白色，覆盖所有 SVG 图标 + spinner）

RADIUS_WIN = 6                # 窗口圆角
RADIUS_CTRL = 4               # 控件圆角
RADIUS_DRAG = 3               # 拖拽区圆角

# ===========================================================================
# 几何尺寸
# ===========================================================================
WIN_W = 168                   # 主窗口宽（最小宽度；随 InfoZone 文本动态增长）
                            # 168：在「拖拽区→菜单按钮」与「InfoZone→右边界」间距都收到
                            # GAP(=5，实际视觉≈6) 后，按最小版本区 ZONE_W 重新收紧的窗口宽。
                            # 该值须与布局实际内容宽（layout.sizeHint()）一致（见 _measure_gaps
                            # 校验），否则窗口不是过宽（右边界残留空隙）就是过窄（右侧被压缩），
                            # 都会破坏两间距相等的契约。
WIN_H = 42                    # 主窗口高

DRAG_W = 10                   # 拖拽区宽
DRAG_H = 26                   # 拖拽区高

BTN_SIZE = 26                 # 菜单/工具/开始按钮边长

SEP_W = 1                     # 垂直分隔线宽
SEP_H = 20                    # 垂直分隔线高

ZONE_W = 56                   # InfoZone 宽（最小宽度；随版本文本动态增长）
                            # 仍为 56：版本文本仅少 5px 余量，长文本仍会动态加宽窗口。
ZONE_H = 26                   # InfoZone 高

MARGIN_L = 5                  # 左边距（窗口左 → 拖拽区）
GAP = 5                       # 目标视觉等距：拖拽区→菜单按钮 与 InfoZone→右边界 共用且恒等
MARGIN_R = GAP               # 右边距（= 上述视觉间距）
SPACING = 2                  # 控件间常规间距
# 显式 spacer：使「拖拽区→菜单按钮」的视觉间距 = GAP = 「InfoZone→右边界」间距。
# QHBoxLayout 在 spacer 两侧各叠加一次 SPACING，故显式 spacer = GAP - SPACING
# （实际视觉间距 = (GAP-SPACING) + SPACING = GAP；布局另有固定 +1 像素偏移，两边一致，
# 因此两间距仍恒等）。
DRAG_RIGHT_GAP = GAP - SPACING

GHOST_SIZE = 22               # ghost 按钮边长（26px InfoZone 内留出 2px 呼吸边）
GHOST_ICON_SIZE = 14          # ghost 图标尺寸
RING_SIZE = 18                 # 加载圈边长（略小于 ghost 按钮，居中叠加以留出呼吸边）

#: 前台自愈看门狗轮询间隔（ms）。只用一次 GetForegroundWindow + GetWindowThreadProcessId，
#: 开销可忽略；且仅在「前台归属发生变化」时才真正驱动 FSM，不会打断闲置倒计时。
FG_POLL_INTERVAL = 500
ZONE_PAD_L = 2                # InfoZone 左侧内边距
ZONE_GAP = 2                  # ghost 按钮与版本文本间距
ZONE_PAD_R = 5                # InfoZone 右侧内边距

FONT_SIZE_V = 10              # 版本文本字号（比原 9px 易读，同时避免窗口过宽）
FONT_WEIGHT_V = 600           # 版本文本字重（SemiBold，提升小字号可读性）

# 状态常量
STATE_IDLE = "idle"
STATE_CHECKING = "checking"
STATE_UP_TO_DATE = "up_to_date"
STATE_AVAILABLE = "available"
STATE_UPDATING = "updating"
STATE_ERROR = "error"


# ===========================================================================
# QSS 样式表
# ===========================================================================
HOME_QSS = f"""
QWidget#HomeRoot {{
    background: transparent;
}}
QFrame#DragArea {{
    background: transparent;
}}
QPushButton#CtrlBtn {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 0;
    outline: none;
}}
QPushButton#CtrlBtn:hover {{
    background: {BG_CARD_HOVER};
    border: 1px solid {BORDER};
}}
QPushButton#CtrlBtn:pressed {{
    background: {BG_CARD_PRESSED};
}}
QPushButton#CtrlBtn::menu-indicator {{
    width: 0px;
    height: 0px;
    image: none;
}}
QPushButton#StartBtn {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    border-radius: {RADIUS_CTRL}px;
    padding: 0;
    outline: none;
}}
QPushButton#StartBtn:hover {{
    background: {ACCENT_HOVER};
    border: 1px solid {ACCENT_HOVER};
}}
QPushButton#StartBtn:pressed {{
    background: {ACCENT_PRESSED};
    border: 1px solid {ACCENT_PRESSED};
}}
QFrame#Separator {{
    background: {SEPARATOR};
    border: none;
}}
QFrame#InfoZone {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
}}
GhostButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_CTRL}px;
    padding: 0;
    outline: none;
}}
GhostButton:hover {{
    background: rgba(10, 132, 255, 0.10);
    border: 1px solid {BORDER};
}}
QMenu {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 5px;
    color: {TEXT_PRIMARY};
}}
QMenu::item {{
    /* 左内边距直接叠加成「图标→文本」间距：实际间距 = padding-left + 样式固定的 7px。
       图标本身不参与内边距（始终贴内容区左侧），故 padding-left 同时决定无图标项的
       文本缩进。整体间距参照设置页输入框原生右键菜单的留白感——图标离左、右文本
       离右都留出呼吸边距。 */
    padding: 6px 16px 6px 3px;
    border-radius: 3px;
    color: {TEXT_PRIMARY};
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {BG_CARD_HOVER};
}}
QMenu::separator {{
    height: 1px;
    background: {SEPARATOR};
    margin: 4px 8px;
}}
"""


# ===========================================================================
# SVG 图标加载辅助
# ===========================================================================
def _svg_icon(filename: str, px: int = 16, color: str = ICON_COLOR) -> QIcon:
    """从 resources/icons/home/ 加载 SVG 文件为 QIcon。

    无论 SVG 内部填充色是什么，统一重新着色为 ``color``（默认白色）：
    先渲染原图，再用 SourceIn 合成模式把整个像素图填充为目标色，
    从而把任意配色的图标变成单色剪影。

    优先走 Qt 资源（icons.rcc 注册后，把 N 次磁盘随机读合并为 1 次顺序读），
    缺失时回退磁盘，避免回归。
    """
    from omg.ui.icon_loader import svg_icon
    return svg_icon("home/" + filename, px, color)


def _make_version_font() -> "QFont":
    """构造版本文本专用字体，渲染与测宽必须用同一个实例。

    与旧版（字体写在样式表里、测宽却用默认字体）不同，这里显式创建 QFont 并同时
    用于 ``setFont`` 与 ``QFontMetrics``，确保测量到的字形宽度 = 实际渲染宽度，
    从根本上消除「算窄 → 版本号被裁切」的问题。
    """
    from PySide6.QtGui import QFont
    f = QFont()
    f.setFamily("Microsoft YaHei")
    f.setPixelSize(FONT_SIZE_V)
    f.setWeight(QFont.Weight.DemiBold if hasattr(QFont.Weight, "DemiBold")
                else FONT_WEIGHT_V)
    return f


# ===========================================================================
# Ghost 按钮（透明背景，hover/check 时强调色微光；支持 hover 换图标 + 旋转）
# ===========================================================================
class CancelRing(DownloadProgressRing):
    """加载圈子类：更新流程中覆盖在 ghost 按钮上，点击（圈内任意位置）即中断。"""

    def mousePressEvent(self, e):  # type: ignore[override]
        if e.button() == Qt.LeftButton:
            self.cancelled.emit()
        super().mousePressEvent(e)


class GhostButton(QPushButton):
    """Ghost 样式按钮。

    - 空闲态悬浮 → 切换到 hover 图标（change.svg）。
    - 检查中 → 由 home.py 叠加 progress_ring 加载圈（indeterminate 匀速扫圈 + 拖影）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("GhostButton")
        self._idle = QIcon()
        self._hover = QIcon()
        self._hover_on = False
        self._spinning = False

    def set_idle_icon(self, icon: QIcon) -> None:
        self._idle = icon
        if not self._spinning:
            self.setIcon(self._idle)

    def set_hover_icon(self, icon: QIcon) -> None:
        self._hover = icon

    def enable_hover(self, on: bool) -> None:
        self._hover_on = on
        self.setAttribute(Qt.WA_Hover, on)
        if not on and not self._spinning:
            self.setIcon(self._idle)

    def set_spinning(self, on: bool) -> None:
        self._spinning = on
        if not on:
            self.setIcon(self._idle)

    def set_spin_icon(self, icon: QIcon) -> None:
        if self._spinning:
            self.setIcon(icon)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        if self._hover_on and not self._spinning and self.isEnabled():
            self.setIcon(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        if self._hover_on and not self._spinning:
            self.setIcon(self._idle)
        super().leaveEvent(event)


# ===========================================================================
# 开始按钮（带注入中动态 spinner）
# ===========================================================================
class StartButton(QPushButton):
    """开始按钮：空闲/完成/失败显示 play.svg；注入中旋转 circle_loading.svg。

    长按防误触：当防误触开启（由外部按需调用 :meth:`start_hold`）时，按下后启动
    一个计时器，达到设定时长才发 ``hold_completed``（由外部触发启动）；按下期间
    松开则调用 :meth:`cancel_hold` 取消，不触发。绘制层在 :meth:`paintEvent` 用
    一圈进度弧实时反映剩余时间。
    """

    hold_completed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StartBtn")
        self.setFixedSize(BTN_SIZE, BTN_SIZE)
        self.setCursor(Qt.PointingHandCursor)

        self._spinning = False
        self._angle = 0.0
        self._play_icon = _svg_icon("play.svg", 14)

        # 预渲染 spinner 到位图一次（避免逐帧重绘 SVG 的卡顿）；实际旋转在
        # paintEvent 内用 QPainter 旋转这张位图完成——时间驱动、连续无跳变。
        self._spinner_pixmap = self._make_spinner_pixmap("home/circle_loading.svg", 32)

        self.setIcon(self._play_icon)
        self.setIconSize(QSize(14, 14))

        self._spin_clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ≈60fps：仅触发重绘，角度由时间推导
        self._timer.timeout.connect(self._tick)

        # 长按防误触状态
        self._holding = False
        self._hold_ms = 0
        self._hold_progress = 0.0
        self._hold_clock = QElapsedTimer()
        self._hold_timer = QTimer(self)
        self._hold_timer.setInterval(16)  # ≈60fps 刷新进度弧
        self._hold_timer.timeout.connect(self._hold_tick)

    @staticmethod
    def _make_spinner_pixmap(name: str, size: int = 32):
        """预渲染 spinner SVG 为白色单色位图（仅一次）。

        高分辨率离屏渲染，paintEvent 内再缩放绘制，保证高分屏下旋转依旧清晰。
        ``name`` 为资源别名（如 ``home/circle_loading.svg``），优先走 Qt 资源、
        回退磁盘。
        """
        from omg.ui.icon_loader import read_svg_bytes
        data = read_svg_bytes(name)
        if not data:
            return None
        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            return None
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        renderer.render(p, QRectF(0, 0, size, size))
        p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p.fillRect(pm.rect(), QColor(ICON_COLOR))
        p.end()
        return pm

    def set_spinning(self, on: bool) -> None:
        if on == self._spinning:
            return
        self._spinning = on
        if on:
            self._angle = 0.0
            if self._spinner_pixmap is not None:
                self.setIcon(QIcon())  # 清空原生图标，改由 paintEvent 绘制旋转位图
            self._spin_clock.restart()
            self._timer.start()
        else:
            self._timer.stop()
            self.setIcon(self._play_icon)
            self.update()

    def _tick(self) -> None:
        # 时间驱动连续旋转：angle 由经过时间推导（与帧率无关，
        # 即使偶发丢帧也只是多转一点，不会跳变或漂移，肉眼更顺滑）。
        if not self._spinning:
            return
        elapsed = self._spin_clock.elapsed()
        self._angle = (elapsed / 1200.0 * 360.0) % 360.0
        self.update()

    # ------------------------------------------------------------------
    # 长按防误触
    # ------------------------------------------------------------------
    @property
    def holding(self) -> bool:
        return self._holding

    def start_hold(self, ms: int) -> None:
        """开始长按计时期；达到 ms 后发 ``hold_completed``。

        仅当按钮可用且 ms 合法才生效；重复调用以新的 ms 重启。
        """
        if not self.isEnabled() or ms <= 0:
            return
        self._holding = True
        self._hold_ms = ms
        self._hold_progress = 0.0
        self._hold_clock.start()
        self._hold_timer.start()
        self.update()

    def cancel_hold(self) -> None:
        """取消长按（中途松开鼠标）：不发 ``hold_completed``，复位进度弧。"""
        if not self._holding:
            return
        self._holding = False
        self._hold_timer.stop()
        self._hold_progress = 0.0
        self.update()

    def _hold_tick(self) -> None:
        if not self._holding:
            self._hold_timer.stop()
            return
        elapsed = self._hold_clock.elapsed()
        frac = min(1.0, elapsed / max(1, self._hold_ms))
        self._hold_progress = frac
        self.update()
        if frac >= 1.0:
            # 达成：标记结束并复位，再发完成信号（避免与后续松开事件竞态）
            self._holding = False
            self._hold_timer.stop()
            self._hold_progress = 0.0
            self.update()
            self.hold_completed.emit()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self._spinning:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            rect = self.rect()
            if self._spinner_pixmap is not None:
                # 预渲染位图旋转绘制（高分屏清晰、连续顺滑）
                pm = self._spinner_pixmap
                target = 14
                p.translate(rect.width() / 2, rect.height() / 2)
                p.rotate(self._angle)
                p.drawPixmap(
                    QRectF(-target / 2.0, -target / 2.0, target, target),
                    pm, QRectF(pm.rect()),
                )
            else:
                # 兜底：无 SVG 时手绘旋转圆弧
                cx, cy = rect.width() // 2, rect.height() // 2
                margin = 6
                sz = min(rect.width(), rect.height()) - 2 * margin
                p.translate(cx, cy)
                p.rotate(self._angle)
                pen = QPen(QColor("#FFFFFF"), 2.0, Qt.SolidLine, Qt.RoundCap)
                p.setPen(pen)
                p.drawArc(-sz // 2, -sz // 2, sz, sz, 0, 270 * 16)
            p.end()
        elif self._holding and self._hold_progress > 0.0:
            # 长按进度弧：外圈轨道 + 已按压比例的高亮弧（从 12 点顺时针）
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            rect = self.rect()
            margin = 3
            sz = min(rect.width(), rect.height()) - 2 * margin
            # 轨道（半透明白）
            pen = QPen(QColor(255, 255, 255, 55), 3.0, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(margin, margin, sz, sz, 0, 360 * 16)
            # 进度（iOS 蓝）
            pen = QPen(QColor("#4C9AFF"), 3.0, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            span = int(-self._hold_progress * 360 * 16)
            p.drawArc(margin, margin, sz, sz, 90 * 16, span)
            p.end()


# ===========================================================================
# 拖拽区控件
# ===========================================================================
class DragArea(QFrame):
    """10×26 拖拽手柄：按住拖动整个 Home 窗口。"""

    # 拖动状态（True=按住开始拖 / False=松开）。
    # 名字刻意避开 QFrame 原生信号（clicked/pressed/...）以免同名遮蔽导致递归崩溃。
    dragStateChanged = Signal(bool)

    _COL_LEFT_X = 2.5
    _COL_RIGHT_X = 6.5
    _DOT_R = 1.2

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("DragArea")
        self.setFixedSize(DRAG_W, DRAG_H)
        self.setCursor(Qt.SizeAllCursor)
        self._drag_offset: Optional[QPoint] = None

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = float(DRAG_W)
        h = float(DRAG_H)

        bg_path = QPainterPath()
        bg_path.addRoundedRect(0.5, 0.5, w - 1.0, h - 1.0, float(RADIUS_DRAG), float(RADIUS_DRAG))
        p.fillPath(bg_path, QBrush(QColor(DRAG_BG)))
        border_pen = QPen(QColor(BORDER), 1.0)
        border_pen.setCosmetic(True)
        p.setPen(border_pen)
        p.drawPath(bg_path)

        dot_core = QColor("#1A1A1C")
        dot_shadow = QColor("#0E0E0F")
        dot_highlight = QColor("#4C4C50")

        right_ys = [3.0, 9.7, 16.3, 23.0]
        left_ys = [6.3, 13.0, 19.7]
        all_dots = [(self._COL_LEFT_X, y) for y in left_ys] + \
                   [(self._COL_RIGHT_X, y) for y in right_ys]

        r = self._DOT_R
        for (cx, cy) in all_dots:
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(dot_core))
            p.drawEllipse(QPointF(cx, cy), r, r)

            hl_path = QPainterPath()
            hl_path.arcMoveTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 60.0)
            hl_path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 60.0, 110.0)
            pen_hl = QPen(dot_highlight, 0.8)
            pen_hl.setCosmetic(True)
            p.setPen(pen_hl)
            p.setBrush(Qt.NoBrush)
            p.drawPath(hl_path)

            sh_path = QPainterPath()
            sh_path.arcMoveTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 240.0)
            sh_path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 240.0, 110.0)
            pen_sh = QPen(dot_shadow, 0.8)
            pen_sh.setCosmetic(True)
            p.setPen(pen_sh)
            p.setBrush(Qt.NoBrush)
            p.drawPath(sh_path)

        p.end()
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            win = self.window()
            if win is not None:
                self._drag_offset = (
                    event.globalPosition().toPoint()
                    - win.frameGeometry().topLeft()
                )
                self.dragStateChanged.emit(True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_offset is not None:
            win = self.window()
            if win is not None:
                win.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_offset is not None:
            self._drag_offset = None
            self.dragStateChanged.emit(False)
        super().mouseReleaseEvent(event)


def _is_our_process_foreground() -> Optional[bool]:
    """Win32 判定：当前系统前台窗口是否属于本进程。

    返回 True / False；非 Windows 或判定失败返回 ``None``（调用方应退回
    :meth:`QApplication.applicationState`）。

    为什么不直接用 ``applicationState()``：主面板与资源浏览页都是 ``Qt.Tool``
    （无任务栏、不进 Alt+Tab），实测在「热键唤出子窗口 → 再关闭」之后，Qt 会把
    ``applicationState()`` 卡在 ``ApplicationActive`` 且不再发出
    ``ApplicationInactive``。此时焦点保持令牌永不释放，表现为「关闭资源浏览页后
    启动页闲置淡化失效」——即便 child 令牌已正确释放也无济于事。
    ``GetForegroundWindow`` 是 Windows 上唯一可靠的地面真值，用于自愈纠正。
    """
    try:
        import ctypes
        import os
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) == os.getpid()
    except Exception:
        return None


# ===========================================================================
# 主窗口：HomeWindow
# ===========================================================================
class HomeWindow(CenteredPopupMixin, QWidget):
    """Home 窗口：176×42（宽度随版本文本动态增长），无标题栏，水平布局+垂直居中。

    ``tray_mode=True`` 时以 ``Qt.Tool`` 创建：不进任务栏、不进 Alt+Tab，
    进程的唯一常驻入口是系统托盘（``omg.ui.tray.TrayController``）。此时
    「关闭面板」= 收进托盘（``closeEvent`` 里 ignore + hide），只有托盘菜单的
    「退出程序」走 ``request_exit()`` 才真正结束进程。

    几何记忆（``CenteredPopupMixin``）：首启动居中于屏幕，之后在**上次关闭时
    的位置**弹出；位置随 ``hideEvent`` 写入 ``config["window_geometry"]["home"]``。
    托盘「复位面板」会清空该记忆并重新居中（见 :meth:`reset_panel`）。
    """

    #: 几何记忆在 config["window_geometry"] 中的键
    GEO_KEY_HOME = "home"

    def __init__(self, parent: Optional[QWidget] = None,
                 launch_controller: Optional[LaunchController] = None,
                 config=None, tray_mode: bool = False) -> None:
        # Tool = WS_EX_TOOLWINDOW：不进任务栏 / Alt+Tab。必须在构造时通过
        # flags 传入，事后 setWindowFlag 会重建窗口（丢位置、闪一下）。
        flags = window_flags(hide_from_taskbar=tray_mode)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("HomeRoot")
        self.setFixedSize(WIN_W, WIN_H)

        # 托盘模式：关闭面板即隐藏（进程常驻），退出需走 request_exit()
        self._tray_mode = bool(tray_mode)
        self._quitting = False
        self._fade_detached = False

        # 「开始」按钮业务控制器
        self._launch: Optional[LaunchController] = launch_controller
        if self._launch is not None:
            self._launch.bind(self)

        # 配置（读取本地 GIMI 版本 / 驱动更新）
        self._config = config

        # 几何记忆：首启动居中，之后按上次关闭位置弹出。
        # 必须在 setFixedSize 之后、show 之前注册（居中要用确定的尺寸）；
        # config 为 None（独立调试入口）时退化为「仅居中一次」，不影响显示。
        self.init_geometry_persist(self.GEO_KEY_HOME, config)

        # 配置版本重新探测完成（启动异步检测 / 切换 GIMI 目录）时刷新 InfoZone
        if self._config is not None:
            sig = getattr(self._config, "versions_changed", None)
            if sig is not None:
                sig.connect(self._refresh_version_tags)

        # 更新状态机
        self._state = STATE_IDLE
        self._local_version = ""
        self._remote_version = ""

        # 闲置变淡：时机决策在 omg.core.idle_fade_fsm（纯逻辑），
        # 计时与缓动由 omg.ui.idle_fade.IdleFadeController 托管。
        # attach() 在 _build_ui() 之后调用——判定光标是否在窗口内需要确定的几何。
        self._fade = IdleFadeController(self, config=self._config)
        self.setWindowOpacity(1.0)

        # 默认置顶（设置 → OMG → 选项）：启动即应用。
        # 必须在 _build_ui() 之前设置：菜单项文字按当前 windowFlags() 决定
        # 「置顶 / 取消置顶」。运行中的菜单切换仍是会话级，不回写该键。
        if self._config is not None and bool(
                int(self._config.get(KEY_OMG_ALWAYS_ON_TOP, 0) or 0)):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # 更新线程（按需创建，避免重复）
        self._gimi_sig = GimiUpdateSignals()
        self._gimi_sig.check_result.connect(self._on_check_result)
        self._gimi_sig.status.connect(self._on_update_status)
        self._gimi_sig.progress.connect(self._on_update_progress)
        self._gimi_sig.finished.connect(self._on_update_finished)
        self._check_thread: Optional[CheckThread] = None
        self._update_thread: Optional[UpdateThread] = None

        # 加载圈（更新 / 检查中覆盖 ghost 按钮；点击可中断流程）
        self._cancel_pending = False

        # 应用全局 QSS
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(HOME_QSS)
            # 应用级前后台切换信号：作为「焦点」保持令牌的唯一权威来源。
            # 选它而不是 focusWindowChanged —— 后者在「焦点切到非 Qt 原生窗口
            # （如全屏游戏）」时常常不触发或仍把焦点判给本窗口，导致「置顶状态下
            # 在别的窗口操作却不淡化」（焦点令牌被卡在 True）。applicationStateChanged
            # 在切到任意别的窗口/游戏时稳定发 ApplicationInactive，回前台时发
            # ApplicationActive，对原生窗口过渡同样可靠。
            app.applicationStateChanged.connect(self._on_app_state_changed)

        self._build_ui()
        self._load_local_version()

        # 打开 OMG 时自动检查 GIMI 更新（设置 → GIMI → 「打开 OMG 时检查更新」开关）：
        # 开关开启时，在启动阶段静默跑一次检查，复用手动检查线程路径（_start_check）。
        # 默认开启（config.default.json: auto_check_update=1）；关闭则完全不触发，
        # 仅保留 ghost 按钮供用户手动检查。
        if self._config is not None and bool(
                int(self._config.get(KEY_AUTO_CHECK, 1) or 0)):
            self._start_check()

        # 启动闲置变淡：先绑定窗口与输入监听，再按**真实**应用前台状态设定初始
        # focus 令牌（旧逻辑硬编码 _focused=True，后台启动时窗口会一直不透明）。
        # _fg_active 必须在 attach 之前就位——_sync_child_hold 会读它。
        self._fg_active: Optional[bool] = None
        self._fade.attach(self)
        if app is not None:
            self._on_app_state_changed(app.applicationState())

        # 前台自愈看门狗：applicationStateChanged 对 Qt.Tool 窗口在「热键唤出子窗口
        # 再关闭」后会卡在 Active 且不再发出 Inactive（见 _is_our_process_foreground
        # 说明）。用 Win32 GetForegroundWindow 定期核对真实前台进程，一旦确认本进程
        # 已不在前台就强制释放 focus / child 令牌，让启动页恢复闲置淡化。
        # 仅在真实平台启用：offscreen 冒烟环境没有真实前台窗口，
        # GetForegroundWindow 恒返回别的进程，会让所有断言都误判成后台。
        self._fg_timer = QTimer(self)
        self._fg_timer.setInterval(FG_POLL_INTERVAL)
        self._fg_timer.timeout.connect(self._on_fg_watchdog)
        if app is not None and app.platformName() != "offscreen":
            self._fg_timer.start()

        # 全局热键 Alt+` ：显示 / 隐藏资源浏览界面。
        # 用系统级热键（而不是 QShortcut）是因为要在游戏内 / OMG 失焦时也能按。
        # 注册失败（组合键被占用、非 Windows）只写日志，不影响其它功能。
        self._res_hotkey: Optional[GlobalHotkey] = None
        self._init_res_hotkey()

        # 自定义 tooltip 气泡：覆盖本窗口所有控件（含动态控件由应用级过滤器兜底），
        # 并屏蔽原生 QToolTip 的直角 + 阴影。
        install_pop_cards(self, lambda: "dark")

    # ------------------------------------------------------------------
    # 全局热键
    # ------------------------------------------------------------------
    def _init_res_hotkey(self) -> None:
        """注册 Alt+` 全局热键，切换资源浏览界面显隐。"""
        hk = GlobalHotkey(VK_OEM_3, MOD_ALT, parent=self)
        hk.activated.connect(self._toggle_resources)
        hk.failed.connect(lambda why: logger.warning(f"Alt+` 全局热键未生效：{why}"))
        if hk.register():
            self._res_hotkey = hk
        else:
            hk.deleteLater()

    def _toggle_resources(self) -> None:
        """Alt+`：资源浏览界面可见则隐藏，否则创建 / 显示并置前。"""
        win = getattr(self, "_resources_win", None)
        if win is not None and win.isVisible():
            win.hide()
            QTimer.singleShot(0, self._sync_child_hold)
            return
        self._on_resources()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(MARGIN_L, 0, MARGIN_R, 0)
        layout.setSpacing(SPACING)
        layout.setAlignment(Qt.AlignVCenter)

        # 1) 拖拽区（拖动期间保持窗口不透明，见 _on_drag_state_changed）
        drag_area = DragArea()
        drag_area.dragStateChanged.connect(self._on_drag_state_changed)
        layout.addWidget(drag_area)
        layout.addSpacing(DRAG_RIGHT_GAP)

        # 2) 菜单按钮
        self._btn_menu = QPushButton()
        self._btn_menu.setObjectName("CtrlBtn")
        self._btn_menu.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._btn_menu.setIcon(_svg_icon("menu.svg", 16))
        self._btn_menu.setIconSize(QSize(16, 16))
        self._btn_menu.setCursor(Qt.PointingHandCursor)
        popup_menu = QMenu(self)
        act_settings = popup_menu.addAction(_svg_icon("setting.svg", 14), "设置")
        popup_menu.addSeparator()
        pinned_now = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
        act_pin = popup_menu.addAction(
            _svg_icon("up.svg", 14), "取消置顶" if pinned_now else "置顶"
        )
        self._act_pin = act_pin
        act_pin.triggered.connect(self._on_toggle_always_on_top)
        act_hide = popup_menu.addAction("隐藏面板" if self._tray_mode else "最小化")
        act_close = popup_menu.addAction("退出程序")
        act_settings.triggered.connect(self._on_settings_clicked)
        act_hide.triggered.connect(self._on_minimize_clicked)
        # 必须接 request_exit 而非 self.close：托盘模式下 closeEvent 会 ignore + hide
        # （「关闭」= 收进托盘），那样点「退出程序」只会把面板藏起来、进程完全不退出。
        # request_exit 置 _quitting 后走完整清理路径并 app.quit()，与托盘右键菜单一致。
        act_close.triggered.connect(self.request_exit)
        self._btn_menu.setMenu(popup_menu)
        layout.addWidget(self._btn_menu)

        # 3) 工具按钮（点击弹出菜单）
        self._btn_tool = QPushButton()
        self._btn_tool.setObjectName("CtrlBtn")
        self._btn_tool.setFixedSize(BTN_SIZE, BTN_SIZE)
        self._btn_tool.setIcon(_svg_icon("tool.svg", 16))
        self._btn_tool.setIconSize(QSize(16, 16))
        self._btn_tool.setCursor(Qt.PointingHandCursor)
        self._btn_tool.setMenu(self._build_tool_menu())
        layout.addWidget(self._btn_tool)

        # 4) 分隔线
        self._separator = QFrame()
        self._separator.setObjectName("Separator")
        self._separator.setFixedSize(SEP_W, SEP_H)
        layout.addWidget(self._separator)

        # 5) 开始按钮
        self._btn_start = StartButton()
        self._btn_start.setCursor(Qt.PointingHandCursor)
        self._btn_start.setToolTip("开始游戏")
        # 点按执行（防误触关闭时）；按下 / 松开 / 长按达成 用于防误触模式
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_start.pressed.connect(self._on_start_pressed)
        self._btn_start.released.connect(self._on_start_released)
        self._btn_start.hold_completed.connect(self._on_hold_completed)
        self._refresh_start_tooltip()
        layout.addWidget(self._btn_start)

        # 6) InfoZone（版本区，内含 ghost 按钮 + 版本文本）
        self._info_zone = QFrame(self)
        self._info_zone.setObjectName("InfoZone")
        self._info_zone.setFixedSize(ZONE_W, ZONE_H)
        zone_layout = QHBoxLayout(self._info_zone)
        zone_layout.setContentsMargins(ZONE_PAD_L, 0, ZONE_PAD_R, 0)
        zone_layout.setSpacing(ZONE_GAP)
        zone_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self._ghost_btn = GhostButton(self._info_zone)
        self._ghost_btn.setFixedSize(GHOST_SIZE, GHOST_SIZE)
        self._icon_gi = _svg_icon("gi.svg", GHOST_ICON_SIZE)
        self._icon_change = _svg_icon("change.svg", GHOST_ICON_SIZE)
        self._icon_arrow = _svg_icon("arrow-up.svg", GHOST_ICON_SIZE)
        self._ghost_btn.set_idle_icon(self._icon_gi)
        self._ghost_btn.set_hover_icon(self._icon_change)
        self._ghost_btn.setIconSize(QSize(GHOST_ICON_SIZE, GHOST_ICON_SIZE))
        self._ghost_btn.setCursor(Qt.PointingHandCursor)
        self._ghost_btn.enable_hover(True)
        self._ghost_btn.clicked.connect(self._on_ghost_clicked)
        zone_layout.addWidget(self._ghost_btn)

        # 加载圈：覆盖在 ghost 按钮之上，更新/检查流程中显示；点击中心即中断。
        # 边长略小于 ghost 按钮并居中，留出呼吸边（视觉上更小巧）。
        self._ring = CancelRing(parent=self._ghost_btn, size=RING_SIZE, thickness=2)
        self._ring.setObjectName("UpdateRing")
        self._ring.setDark(True)
        off = (GHOST_SIZE - RING_SIZE) // 2
        self._ring.setGeometry(off, off, RING_SIZE, RING_SIZE)
        self._ring.raise_()
        self._ring.hide()
        self._ring.cancelled.connect(self._on_ring_cancelled)

        self._version_label = QLabel("", self._info_zone)
        # 关键点：版本文本用显式 QFont 渲染，且宽度测量也必须用「同一个」QFont 的
        # QFontMetrics。旧实现把 font-family/size/weight 写在样式表里，而测量却用
        # label.font()（未带样式表的默认字体），二者字形宽度不一致 → 算出的文本宽偏小
        # → InfoZone 被算窄 → 版本号右侧被裁切。这里统一用 _version_font 同时驱动渲染与测量。
        self._version_font = _make_version_font()
        self._version_label.setFont(self._version_font)
        self._version_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; "
            "padding: 0px; background: transparent; border: none;"
        )
        # 文本在剩余空间中左对齐，视觉上更稳定；整体宽度随版本文本动态变化
        self._version_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._version_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        zone_layout.addWidget(self._version_label)

        layout.addWidget(self._info_zone)

    # ------------------------------------------------------------------
    # 闲置变淡：只做「事件 → 控制器输入」的翻译
    #
    # 时机规则（何时淡出、何时唤醒、忙碌/拖动/子窗口时如何抑制）全部在
    # omg.core.idle_fade_fsm；计时与缓动在 omg.ui.idle_fade。本窗口不再自己
    # 维护「悬浮 / 焦点」两个布尔量——那是旧实现把所有时机问题都压进一行
    # 三元表达式的根源。
    # ------------------------------------------------------------------
    def _on_app_state_changed(self, state) -> None:
        """应用前后台切换（切到其它窗口 / 游戏 → ApplicationInactive）时刷新焦点保持令牌。

        作为「焦点」保持令牌的**唯一**权威来源（不再监听 changeEvent 或
        focusWindowChanged，避免多个来源乱序竞争）。后台即视为失焦 → 解除
        focus 保持、闲置计时恢复；回到前台（点 OMGLite 或其任意子窗口）→ 视为
        有焦点 → 保持不透明。

        关键修复：切到其它应用时，即便管理页 / 设置页 / 便捷构建页仍开着，也必须
        释放 ``child`` 保持令牌。否则它会一直冻结淡出，表现为「打开管理页后再切到
        游戏 / 浏览器，启动页不再变淡」。回到前台时按子窗口当前可见性重建 ``child``
        保持，不影响「子窗口开着时不淡化」的原意（该场景由 focus 令牌即可保证）。
        """
        active = (state == Qt.ApplicationState.ApplicationActive)
        if active:
            self._fade.hold("focus", True)
            self._fade.notify_activity(strong=True)
            # 回到前台：按子窗口当前可见性重建 child 保持（管理页仍开则继续保留）
            self._sync_child_hold()
        else:
            # 切到其它应用：释放 focus 与 child，允许启动页闲置淡化
            self._fade.hold("focus", False)
            self._fade.hold("child", False)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._fade.notify_activity(strong=False)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        # 从父窗口进入子控件时也会收到 leaveEvent：若光标几何仍落在窗口内，
        # 视为仍在窗口上，不取消唤醒确认（避免划过按钮时误判离开）。
        if self.geometry().contains(QCursor.pos()):
            super().leaveEvent(event)
            return
        self._fade.notify_leave()
        super().leaveEvent(event)

    def _on_drag_state_changed(self, dragging: bool) -> None:
        """拖动期间保持不透明：鼠标可能滑出窗口几何，不应触发淡出。"""
        self._fade.hold("drag", dragging)
        if dragging:
            self._fade.notify_activity(strong=True)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        """监听子窗口（设置 / 便捷构建 / 资源浏览）显隐，同步 child 保持令牌。

        捕获 Show / Hide / Close / Destroy 四类事件：
          * Hide 在窗口**真正隐藏后**才发射，此刻 isVisible() 已稳定为 False，
            判定最准确——彻底规避「closeEvent 内窗口尚可见、isVisible() 仍为真
            把 child 误置 True」的时序陷阱；
          * Show 在窗口显示后发射，用于按当前 active 重新置 child（覆盖热键唤出
            后应用才切到前台的情形）；
          * Close / Destroy 兜底（Close 可能被子窗口 event.ignore() 而仍可见，
            故仍延后一帧判定）。
        统一用 ``QTimer.singleShot(0)`` 延后到事件真正落定后再刷新。
        """
        et = event.type()
        if et in (QEvent.Type.Show, QEvent.Type.Hide,
                  QEvent.Type.Close, QEvent.Type.Destroy):
            if obj is getattr(self, "_settings_win", None) \
                    or obj is getattr(self, "_quick_build_win", None) \
                    or obj is getattr(self, "_resources_win", None):
                QTimer.singleShot(0, self._sync_child_hold)
        return False

    def _sync_child_hold(self) -> None:
        """按「子窗口是否还可见」刷新 child 保持令牌。

        关键：只有在**应用处于前台（active）**时才允许冻结首页淡出。若 OMGLite
        本身不在前台（例如用 Alt+` 在游戏内唤出资源浏览页、游戏仍占前台），即便
        子窗口可见也不冻结——否则首页会被 ``child`` 令牌永久卡在不透明，又因为 app
        始终没经过 Active→Inactive 的切换，令牌永不被释放，表现为「用快捷键打开 Mod
        管理页后，首页闲置淡化失效」。让 app 回到前台（用户 alt-tab 到管理器）时，
        ``_on_app_state_changed(Active)`` 会再次调用本方法把它置真。
        """
        active = self._effective_active()
        visible = False
        for attr in ("_settings_win", "_quick_build_win", "_resources_win"):
            win = getattr(self, attr, None)
            if win is not None and win.isVisible():
                visible = True
        self._fade.hold("child", visible and active)

    def _effective_active(self) -> bool:
        """权威的「本应用是否在前台」判定。

        优先用看门狗从 Win32 ``GetForegroundWindow`` 得到的地面真值
        （``self._fg_active``）；看门狗未启用 / 尚未跑过首轮时退回
        :meth:`QApplication.applicationState`。
        """
        if self._fg_active is not None:
            return self._fg_active
        app = QApplication.instance()
        if app is None:
            return True
        return app.applicationState() == Qt.ApplicationState.ApplicationActive

    def _on_fg_watchdog(self) -> None:
        """前台自愈：用 Win32 地面真值纠正被 Qt 卡住的 focus / child 令牌。

        主面板与子窗口都是 ``Qt.Tool``，实测「热键唤出资源浏览页 → 关闭」之后
        Qt 不再发出 ``ApplicationInactive``，``applicationState()`` 恒为 Active，
        focus 令牌永远不释放 → 启动页再也不淡化。本轮询绕过 Qt 直接问系统
        「前台窗口是不是本进程的」，一旦确认不在前台就强制释放，实现自愈。
        """
        fg = _is_our_process_foreground()
        if fg is None or fg == self._fg_active:
            return  # 无法判定 / 无变化：不打扰 FSM（避免打断闲置倒计时）
        self._fg_active = fg
        if fg:
            self._fade.hold("focus", True)
            self._fade.notify_activity(strong=True)
        else:
            self._fade.hold("focus", False)
            self._fade.hold("child", False)
        self._sync_child_hold()

    def _on_minimize_clicked(self) -> None:
        """菜单项「隐藏」（托盘模式）/「最小化」（非托盘回退形态）。

        托盘模式：直接 hide 收起主页，进程继续常驻，由托盘「显示面板」恢复。
        不再用 showMinimized()——主页没有任务栏按钮，最小化后用户无从恢复，
        而且 Tool 窗口的最小化在 Windows 上表现很怪（缩成桌面左下角小条）。

        非托盘模式（托盘不可用时的回退形态）：仍走最小化。此时主页有任务栏
        按钮，隐藏等于把唯一的恢复入口也藏掉，会让进程变成「看得见但摸不着」。
        """
        if self._tray_mode:
            self.hide()
        else:
            self.showMinimized()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # 托盘模式：关闭面板 = 收进托盘，进程继续常驻（热键 Alt+` 仍可用）。
        # 必须 ignore + hide，而不是走下面的清理分支——否则关一次面板就等于退出。
        if self._tray_mode and not self._quitting:
            event.ignore()
            self.hide()
            return
        # 系统级热键是进程资源，退出前显式注销（不依赖 __del__ 时机）
        if getattr(self, "_res_hotkey", None) is not None:
            self._res_hotkey.unregister()
            self._res_hotkey = None
        # 资源浏览窗口已改为无 parent 的独立顶层窗口（避免置顶联动），
        # 关闭 home 时不会随父级自动销毁，必须显式关掉，否则它会泄漏并残留屏幕。
        res_win = getattr(self, "_resources_win", None)
        if res_win is not None:
            res_win.close()
            self._resources_win = None
        self._fade.detach()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 托盘模式：面板显隐 / 退出（由 omg.ui.tray.TrayController 回调）
    # ------------------------------------------------------------------
    def show_panel(self) -> None:
        """托盘「显示面板」：从隐藏 / 最小化状态恢复并置前。

        隐藏期间已 detach 的闲置淡化控制器会在 showEvent 里重新 attach；
        这里再补一次前后台状态刷新，确保 focus 令牌与真实状态一致。
        """
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        app = QApplication.instance()
        if app is not None:
            self._on_app_state_changed(app.applicationState())

    def show_resources(self) -> None:
        """托盘「资源浏览」：与工具菜单 / Alt+` 同一入口。"""
        self._on_resources()

    def reset_panel(self) -> None:
        """托盘「复位面板」：把面板移到屏幕中心，并清掉几何记忆。

        面板可能被拖出屏幕（多显示器插拔、分辨率变化后尤其常见），此时托盘
        「显示面板」虽然能把它 show 出来，但用户仍然看不见它——需要一个不依赖
        当前位置的兜底入口。

        两个动作缺一不可：
          * 居中——复用 ``CenteredPopupMixin._center_on_screen``（按光标所在屏幕）；
          * 清记忆——否则下一次 ``hideEvent`` 又把越界坐标写回去，等于没复位。

        面板处于隐藏态时顺带显示出来，避免用户点了「复位」却看不到任何变化。
        """
        self._center_on_screen()
        self._clear_saved_position()
        if not self.isVisible():
            self.show_panel()

    def _clear_saved_position(self) -> None:
        """删除本窗口在 ``config["window_geometry"]`` 里的位置记忆。"""
        cfg = getattr(self, "_geo_config", None)
        if cfg is None:
            return
        geo = cfg.get("window_geometry")
        geo = dict(geo) if isinstance(geo, dict) else {}
        if geo.pop(getattr(self, "_geo_key", ""), None) is None:
            return
        cfg.set("window_geometry", geo)
        try:
            cfg.save()
        except Exception:
            pass

    def request_exit(self) -> None:
        """托盘「退出程序」：走完整清理路径（closeEvent）后结束事件循环。"""
        self._quitting = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        """收进托盘后停掉淡化计时与全局输入监听，避免后台空转。"""
        if getattr(self, "_fade", None) is not None:
            self._fade.detach()
            self._fade_detached = True
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[override]
        """重新显示时恢复淡化控制器（hideEvent 的逆操作）。

        首次 show 时 ``_fade_detached`` 为 False（__init__ 里已 attach），
        不会重复启动计时器。
        """
        if getattr(self, "_fade_detached", False) \
                and getattr(self, "_fade", None) is not None:
            self._fade_detached = False
            self._fade.attach(self)
            app = QApplication.instance()
            if app is not None:
                self._on_app_state_changed(app.applicationState())
        super().showEvent(event)

    def _on_toggle_always_on_top(self, _checked: bool = False) -> None:
        """切换窗口置顶：重设 WindowStaysOnTopHint 后重新 show()。

        非 checkable 菜单项，文字在「置顶 / 取消置顶」间切换。
        置顶不改变淡入淡出规则；切换后重新应用，避免 show() 重建窗口导致状态错乱。
        """
        pinned = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, not pinned)
        self.show()
        # show() 会重建窗口：按真实应用前台状态重设令牌并重读参数
        app = QApplication.instance()
        if app is not None:
            self._on_app_state_changed(app.applicationState())
        self._fade.reevaluate()
        # 同步菜单项文字
        if getattr(self, "_act_pin", None) is not None:
            self._act_pin.setText("取消置顶" if not pinned else "置顶")

    # ------------------------------------------------------------------
    # 本地版本（一次性读取，避免频繁读盘）
    # ------------------------------------------------------------------
    def _load_local_version(self) -> None:
        local = ""
        if self._config is not None:
            local = self._config.get("gimi_version", "")
            if not local:
                local = read_gimi_version_from_ini(self._config.get_gimi_dir())
        self._local_version = local or ""
        self._set_version_text(self._local_version or "未知")
        self._set_state(STATE_IDLE)
        # tooltip 显示 ORFix / TexFx 版本（异步检测完成前可能为“未知”，由 _refresh_version_tags 刷新）
        self._version_label.setToolTip(self._build_version_tooltip())

    def _build_version_tooltip(self) -> str:
        """构造版本文本 tooltip：显示读取到的 ORFix 与 TexFx 版本。

        由 OMGPopCard（omg.ui.OMGPopCard）统一绘制，不再依赖原生 QToolTip；
        卡片用 QLabel 渲染纯文本、不解析 HTML，因此此处只提供纯文本，用 \\n
        分行（组合键行会自动渲染成 OMGKeyCap 键帽）。
        """
        gimi_dir = self._config.get_gimi_dir() if self._config else ""
        orfix = self._config.get("orfix_version", "") if self._config else ""
        teffx = self._config.get("teffx_version", "") if self._config else ""
        if not orfix and gimi_dir:
            orfix = read_orfix_version(gimi_dir)
        if not teffx and gimi_dir:
            teffx = read_teffx_version(gimi_dir)
        # 最上方一行展示本地 GIMI 版本号（与 InfoZone 文本同源，便于一眼核对）。
        gimi_ver = self._local_version or "未知"
        return f"GIMI: {gimi_ver}\nORFix: {orfix or '未知'}\nTexFx: {teffx or '未知'}"

    def _refresh_version_tags(self) -> None:
        """ConfigManager 重新探测完版本（启动异步检测 / 切换 GIMI 目录后）回调：
        刷新本地版本缓存与 InfoZone 版本号、tooltip。

        旧目录遗留的 AVAILABLE / ERROR / UP_TO_DATE 状态会重置回 IDLE（ghost 恢复
        gi.svg）——旧目录的“发现新版本 / 错误 / 已是最新”对新目录不再成立。
        检查 / 更新进行中（CHECKING / UPDATING）则只刷新版本号与 tooltip，不打断流程。
        """
        if self._config is not None:
            local = self._config.get("gimi_version", "")
            if not local:
                local = read_gimi_version_from_ini(self._config.get_gimi_dir())
            if local:
                self._local_version = local

        # 版本号文本始终显示当前本地版本，状态由 ghost 图标 + tooltip 表达
        self._set_version_text(self._local_version or "未知")

        if self._state in (STATE_CHECKING, STATE_UPDATING):
            self._version_label.setToolTip(self._build_version_tooltip())
            return

        self._set_state(
            STATE_IDLE,
            tooltip="检查更新",
            ghost_icon=self._icon_gi,
            hover=True,
            enabled=True,
        )
        self._version_label.setToolTip(self._build_version_tooltip())

    # ------------------------------------------------------------------
    # 版本文本 + 动态宽度
    # ------------------------------------------------------------------
    def _set_version_text(self, text: str) -> None:
        """设置 InfoZone 版本文本（始终为本地版本号，不显示动态状态文本）。"""
        self._version_label.setText(text)
        # 字体已在 _build_ui 用 _version_font 显式设置；此处仅刷新颜色样式，
        # 不再写 font-*，避免样式表覆盖导致渲染字体与测宽字体再次不一致。
        self._version_label.setFont(self._version_font)
        self._version_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; "
            "padding: 0px; background: transparent; border: none;"
        )
        self._relayout(text)

    def _relayout(self, text: str) -> None:
        """根据文本长度动态设置 InfoZone / 窗口宽度（最小为原始尺寸 WIN_W）。"""
        # 用与渲染完全一致的字体测宽；+4 缓冲防止字形四舍五入 / QLabel 内边距导致
        # 右侧最后一像素被裁（实测长版本号恰好顶满，留余量更稳）。
        fm = QFontMetrics(self._version_font)
        text_w = fm.horizontalAdvance(text) + 4
        needed = ZONE_PAD_L + GHOST_SIZE + ZONE_GAP + text_w + ZONE_PAD_R
        new_zone_w = max(ZONE_W, int(needed))
        self._info_zone.setFixedSize(new_zone_w, ZONE_H)
        new_win_w = WIN_W + (new_zone_w - ZONE_W)
        self.setFixedSize(new_win_w, WIN_H)
        self.layout().activate()

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------
    def _set_state(self, state: str, tooltip: str = "检查更新",
                   ghost_icon: Optional[QIcon] = None,
                   hover: bool = True, enabled: bool = True) -> None:
        self._state = state
        if ghost_icon is not None:
            self._ghost_btn.set_idle_icon(ghost_icon)
        self._ghost_btn.enable_hover(hover)
        self._ghost_btn.setEnabled(enabled)
        self._ghost_btn.setToolTip(tooltip)

    # ------------------------------------------------------------------
    # ghost 点击：根据状态决定 检查 / 更新
    # ------------------------------------------------------------------
    def _on_ghost_clicked(self) -> None:
        if self._state in (STATE_IDLE, STATE_UP_TO_DATE, STATE_ERROR):
            self._start_check()
        elif self._state == STATE_AVAILABLE:
            self._start_update()
        elif self._state == STATE_CHECKING:
            self._cancel_check()
        elif self._state == STATE_UPDATING:
            self._on_ring_cancelled()

    def _start_check(self) -> None:
        # 进入检查中：ghost 保持可点击（点击加载圈即中断），显示“匀速扫圈 + 拖影”动画
        self._state = STATE_CHECKING
        self._ghost_btn.enable_hover(False)
        self._ghost_btn.setEnabled(True)
        self._show_ring(indeterminate=True)
        self._ghost_btn.setToolTip("检查中…（点击停止）")

        # 惰性导入：首次点击时才付出 urllib/ssl 这条链的导入与 DLL 分页成本，
        # 这点开销被随后的网络请求完全掩盖。
        from omg.core.gimi_update import CheckThread

        self._check_thread = CheckThread(self._config, self._gimi_sig, self)
        self._check_thread.finished.connect(self._check_thread.deleteLater)
        self._check_thread.start()

    def _on_check_result(self, status: str, local: str, remote: str, msg: str) -> None:
        self._local_version = local or self._local_version
        self._remote_version = remote
        self._cancel_pending = False
        self._hide_ring()
        # 文本保持版本号，状态通过 ghost 图标 + tooltip 表达
        self._set_version_text(self._local_version or "未知")

        if status == "cancelled":
            self._set_state(
                STATE_IDLE, tooltip="已取消检查",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )
            self._cancel_pending = False
            return
        if status == "up_to_date":
            self._set_state(
                STATE_UP_TO_DATE,
                tooltip="已是最新 · 点击重新检查",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )
        elif status == "update_available":
            self._set_state(
                STATE_AVAILABLE,
                tooltip=f"{local} -> {remote}",
                ghost_icon=self._icon_arrow, hover=False, enabled=True,
            )
        else:  # error
            self._set_state(
                STATE_ERROR,
                tooltip=msg or "检查失败",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )

    def _start_update(self) -> None:
        # UpdateThread 拉起 omg.core.gimi_update → download → urllib/ssl（含 5 MB
        # libcrypto-3.dll），仅当用户点击「更新」时才需要，故本地惰性导入。
        from omg.core.gimi_update import UpdateThread

        # 进入更新中：ghost 保持可点击（点击加载圈即中断，并回退 + 删临时文件）
        self._state = STATE_UPDATING
        self._ghost_btn.enable_hover(False)
        self._ghost_btn.setEnabled(True)
        self._show_ring(indeterminate=False)
        self._ghost_btn.setToolTip("更新中…（点击取消）")

        self._update_thread = UpdateThread(self._config, self._gimi_sig, None, self)
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    def _on_update_status(self, msg: str, role: str) -> None:
        if self._state == STATE_UPDATING and msg:
            self._ghost_btn.setToolTip(f"{msg}（点击取消）")

    def _on_update_progress(self, p: float) -> None:
        if self._state == STATE_UPDATING:
            self._ring.setProgress(max(0.0, min(1.0, float(p) / 100.0)))

    def _on_update_finished(self, ok: bool, msg: str) -> None:
        self._hide_ring()
        was_cancel = self._cancel_pending
        self._cancel_pending = False
        if ok:
            # 重新读取本地版本（一次 ini 读取）
            local = ""
            if self._config is not None:
                local = read_gimi_version_from_ini(self._config.get_gimi_dir())
            self._local_version = local or self._local_version
            self._set_version_text(self._local_version or "未知")
            self._set_state(
                STATE_IDLE,
                tooltip="检查更新",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )
            # ORFix / TexFx 随 GIMI 包更新，刷新 tooltip
            self._version_label.setToolTip(self._build_version_tooltip())
        else:
            if was_cancel and "已取消" in msg:
                # 用户中断：状态已在点击时切换，这里仅刷新 tooltip 为最终结果
                self._set_state(
                    STATE_IDLE, tooltip=msg or "已取消",
                    ghost_icon=self._icon_gi, hover=True, enabled=True,
                )
                return
            self._set_state(
                STATE_ERROR,
                tooltip=msg or "更新失败",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )

    # ------------------------------------------------------------------
    # 加载圈：显示 / 隐藏 / 不确定动画 / 点击中断
    # ------------------------------------------------------------------
    def _show_ring(self, indeterminate: bool = False) -> None:
        """在 ghost 按钮上叠加加载圈（检查 / 更新中使用），并隐藏原图标。

        忙碌期间挂上 busy 保持令牌：此时即使用户切走，窗口也应保持不透明，
        否则进度根本看不清。
        """
        self._ghost_btn.setIcon(QIcon())
        self._ring.setProgress(0.0)
        self._ring.set_indeterminate(indeterminate)
        self._ring.show()
        self._ring.raise_()
        self._fade.hold("busy", True)

    def _hide_ring(self) -> None:
        """隐藏加载圈（图标由后续 _set_state 恢复），同时解除 busy 保持。"""
        self._ring.set_indeterminate(False)
        self._ring.hide()
        self._ring.setProgress(0.0)
        self._fade.hold("busy", False)

    # ------------------------------------------------------------------
    # 检查中：匀速扫圈由 progress_ring 的 indeterminate 模式负责（见 progress_ring.py）
    # ------------------------------------------------------------------
    def _cancel_check(self) -> None:
        """检查中点击加载圈：中断检查流程（线程收到取消；视觉即时复位）。"""
        if self._check_thread is not None:
            self._cancel_pending = True
            self._check_thread.request_cancel()
        self._hide_ring()
        self._ghost_btn.setIcon(self._icon_gi)
        self._set_state(
            STATE_IDLE, tooltip="已取消检查",
            ghost_icon=self._icon_gi, hover=True, enabled=True,
        )
        self._cancel_pending = False

    def _on_ring_cancelled(self) -> None:
        """点击加载圈：中断当前检查 / 更新流程。"""
        if self._state == STATE_CHECKING and self._check_thread is not None:
            self._cancel_check()
        elif self._state == STATE_UPDATING and self._update_thread is not None:
            self._cancel_pending = True
            self._update_thread.request_cancel()
            self._hide_ring()
            self._ghost_btn.setIcon(self._icon_gi)
            self._set_state(
                STATE_IDLE, tooltip="已取消，正在回退…",
                ghost_icon=self._icon_gi, hover=True, enabled=True,
            )

    @staticmethod
    def _short_error(msg: str) -> str:
        """把错误详情压缩为 InfoZone 可显示的简短文本（≤约6字）。

        注：当前 InfoZone 只显示版本号、不显示动态状态文本，本方法暂无调用点；
        保留它是为了后续若恢复「错误短文本」提示时可直接复用。
        """
        if not msg:
            return "错误"
        head = msg.split(":", 1)[0].split("（", 1)[0].strip()
        mapping = {
            "网络错误": "网络错误",
            "解析失败": "解析失败",
            "下载失败": "下载失败",
            "解压失败": "解压失败",
            "无法获取版本信息": "检查失败",
            "未找到下载链接": "检查失败",
        }
        if head in mapping:
            return mapping[head]
        # 兜底：取前 6 个字符
        return head[:6]

    # ------------------------------------------------------------------
    # 自绘：磨砂透明背景
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(35, 35, 40, 217))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), RADIUS_WIN, RADIUS_WIN)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), RADIUS_WIN, RADIUS_WIN)
        p.end()
        super().paintEvent(event)

    # ---- 事件回调 ----
    def _on_settings_clicked(self) -> None:
        from omg.pages.setting import SettingsWindow
        if not hasattr(self, "_settings_win") or self._settings_win is None:
            # 必须传入本窗口持有的 ConfigManager 实例：设置页（路径 / GIMI / 启动）
            # 与「开始」按钮的 LaunchController 必须共享同一份运行时配置。
            # 若不传，SettingsController 会新建一个独立 ConfigManager，
            # 改动只写进那个实例，LaunchController 读到的仍是旧值，导致配置不生效。
            self._settings_win = SettingsWindow(self, config=self._config,
                                                tray_mode=self._tray_mode)
            # 监听关闭：子窗口可见期间 home 需保持不透明（见 _sync_child_hold）。
            # eventFilter 统一捕获 Show / Hide / Close 事件并刷新 child 保持令牌，
            # 其中 Hide 在窗口真正隐藏后才发射，isVisible() 此刻一定准确，杜绝
            # 「关闭瞬间 isVisible() 仍为真把 child 误置 True 且外界纠正不可靠」
            # 导致的启动页闲置淡化永久失效。
            self._settings_win.installEventFilter(self)
        self._settings_win.show()
        self._settings_win.raise_()
        self._settings_win.activateWindow()
        # 关键：旧逻辑下子窗口一抢焦点 home 立刻变淡，用户在设置页调
        # 「闲置透明度」滑块时看不到 home 的实际效果。打开期间冻结淡出。
        # 用 _sync_child_hold 而非硬置 True：若本就在后台（如热键唤出）则不冻结，
        # 避免首页被 child 令牌永久卡死（见 _sync_child_hold 说明）。
        self._sync_child_hold()

    def _build_tool_menu(self) -> QMenu:
        """工具按钮下拉菜单：含「便捷构建」与「资源浏览」入口。

        与菜单按钮（#2）保持一致，使用 setMenu 在点击时弹出。
        """
        menu = QMenu(self)
        act_build = menu.addAction("便捷构建")
        act_build.triggered.connect(self._on_quick_build)
        act_res = menu.addAction("资源浏览")
        act_res.triggered.connect(self._on_resources)
        return menu

    def _on_quick_build(self) -> None:
        """点击「便捷构建」：打开便捷构建窗口（单例复用）。

        传入 ConfigManager：便捷构建页要用其中的 gimi_folder 执行「复制到 GIMI」
        （构建产物 / 仪式产物 → GIMI 目录根的 d3d11.dll）。
        """
        from omg.pages.quick_build import QuickBuildWindow
        if not hasattr(self, "_quick_build_win") or self._quick_build_win is None:
            self._quick_build_win = QuickBuildWindow(self, config=self._config,
                                                     tray_mode=self._tray_mode)
            self._quick_build_win.installEventFilter(self)
        self._quick_build_win.show()
        self._quick_build_win.raise_()
        self._quick_build_win.activateWindow()
        # 构建 / 仪式进行中同样保持不透明（与设置窗口同理）
        self._sync_child_hold()

    def _on_resources(self) -> None:
        """点击「资源浏览」：打开资源浏览面板（单例复用）。

        传入 ConfigManager：面板用它读取 gimi_folder 定位 GIMI/Mods，
        与首页共享同一份运行时配置。
        """
        from omg.pages.resources import ResourcesWindow
        if not hasattr(self, "_resources_win") or self._resources_win is None:
            # parent 必须为 None：Mod 是独立顶层窗口（parent=self 会让 Windows
            # 把它当成 Home 的「被拥有窗口」，从而对它 SetWindowPos(HWND_TOPMOST)
            # 时把 Home 也一并提为 topmost——即「Mod 置顶联动首页」的根因）。
            self._resources_win = ResourcesWindow(config=self._config,
                                                     tray_mode=self._tray_mode)
            self._resources_win.installEventFilter(self)
        self._resources_win.show()
        self._resources_win.raise_()
        self._resources_win.activateWindow()
        # 关键修复：用 _sync_child_hold 而非硬置 True。若 OMGLite 当前不在前台
        # （如用 Alt+` 在游戏内唤出管理页、游戏仍占前台），则不冻结首页淡出，
        # 否则 child 令牌永久卡死、首页永不淡化（见 _sync_child_hold 说明）。
        self._sync_child_hold()

    def _on_start_clicked(self) -> None:
        """点按执行路径（防误触关闭时）。

        防误触开启时，真正的启动由 ``_on_hold_completed`` 在长按达成后触发，
        此处直接 return，避免一次点按既走 clicked 又走 hold 造成重复启动。
        """
        if resolve_launch_hold_enabled(self._config):
            return
        if self._launch is not None:
            self._launch.toggle()

    def _on_start_pressed(self) -> None:
        """按下启动按钮：若开启了防误触，开始长按计时期。"""
        if resolve_launch_hold_enabled(self._config):
            self._btn_start.start_hold(resolve_launch_hold_ms(self._config))

    def _on_start_released(self) -> None:
        """松开启动按钮：若正处于长按且未达时长，取消（不执行启动）。"""
        if self._btn_start.holding:
            self._btn_start.cancel_hold()

    def _on_hold_completed(self) -> None:
        """长按达成：执行启动（与点按路径互斥，且只触发一次）。"""
        if self._launch is not None:
            self._launch.toggle()

    def _refresh_start_tooltip(self) -> None:
        """按当前配置刷新启动按钮提示：防误触开启时标注「长按 Xms 启动」。"""
        if resolve_launch_hold_enabled(self._config):
            self._btn_start.setToolTip(
                f"长按 {resolve_launch_hold_ms(self._config)} 毫秒启动（防误触）")
        else:
            self._btn_start.setToolTip("开始游戏")

    def _on_launch_state(self, state: str, detail: str) -> None:
        btn = self._btn_start
        if state == "injecting":
            btn.set_spinning(True)
            btn.setEnabled(False)
        elif state == "done":
            btn.set_spinning(False)
            btn.setEnabled(False)
        elif state == "error":
            btn.set_spinning(False)
            btn.setEnabled(True)
        elif state == "idle":
            btn.set_spinning(False)
            btn.setEnabled(True)
        # 注入期间同样算忙碌：保持不透明
        self._fade.hold("busy", state == "injecting")
        if detail:
            btn.setToolTip(detail)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)


# ===========================================================================
# 直接运行入口（开发调试用）
# ===========================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(HOME_QSS)
    win = HomeWindow()
    win.show()
    sys.exit(app.exec())
