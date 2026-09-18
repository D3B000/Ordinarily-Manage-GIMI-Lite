"""
progress_ring.py — App Store 风格的下载进度圈（PySide6）。

视觉来源（emilkowalski/skills / apple-design）：
  * 外圈 = 极淡的发丝轨道（track_off），进度段用单一强调蓝（accent）顺时针填充；
  * 进度从 12 点方向（顶部）起，顺时针增长（iOS 惯例）；
  * 描边用 RoundCap / RoundJoin —— 端点圆润才显得"贵"；
  * 中心是「停止」按钮：一个圆角方块（iOS 下载中的样子），点击发出 cancelled；
  * 命中区只在中心圆内，且 hover 时给一个若有若无的浅底，提示可点；
  * 支持浅/深主题，颜色直接复用 stylekit 的 token。

实现要点（避坑）：
  * Qt 的 drawArc 角度方向在 y 轴朝下时容易画反，这里用显式点采样构造
    顺时针弧线，完全可控、不依赖 drawArc 的隐式方向；
  * 进度用 Qt Property 暴露，可直接用 QPropertyAnimation 做平滑过渡；
  * 全部纯自绘（paintEvent），不依赖任何原生 widget，offscreen 也能渲染验证。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Property, Qt, QPointF, QRectF, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


# 复用 stylekit 的语义色，保证整库一致。这里内联一份最小集合，避免强依赖。
_ACCENT = "#3b82f6"
_TRACK = "#e4e4e7"          # COLORS["track_off"]
_HOVER_LIGHT = QColor(0, 0, 0, 8)
_HOVER_DARK = QColor(255, 255, 255, 18)


class DownloadProgressRing(QWidget):
    """App Store 风格下载进度圈。

    用法：
        ring = DownloadProgressRing(size=44)
        ring.setProgress(0.0)            # 0..1
        ring.cancelled.connect(on_stop)  # 点击中心停止按钮
        ring.setDark(True)               # 切深主题
        ring.setAccentColor("#FFFFFF")   # 显式覆盖弧线色（None 恢复主题默认）
    """

    # Qt 属性：允许 QPropertyAnimation(ring, b"progress") 平滑驱动
    progressChanged = Signal(float)
    cancelled = Signal()

    def __init__(self, parent=None, size: int = 44, thickness: int | None = None,
                 cancellable: bool = True):
        super().__init__(parent)
        self._size = size
        self._thickness = thickness if thickness else max(2, round(size * 0.075))
        self._progress = 0.0
        self._dark = False
        self._hover = False
        self._pos = QPointF(-1, -1)        # 最近一次光标相对位置
        # 显式配色覆盖（None = 跟随 setDark 主题）。用于按钮底色与强调蓝
        # 相同或过近、导致弧线看不出来的场景（如蓝底 PrimaryBtn 必须画白弧）。
        self._accent_override: QColor | None = None
        self._track_override: QColor | None = None
        self._hover_override: QColor | None = None
        # 是否可点击中断：False 时不绘制中心停止方块（用于不可中断的任务，
        # 如 MSBuild 编译），避免给出无效的停止暗示。
        self._cancellable = bool(cancellable)
        # 不确定（循环）动画：检查中使用的“蓝色条沿轨道匀速扫一圈 + 拖影”
        self._indeterminate = False
        self._indet_progress = 0.0         # 当前周期相位 0..1
        self._indet_elapsed = 0.0          # 当前周期已过毫秒
        self._indet_cycles = 0             # 已完成周期数（用于角度连续累计）
        self._indet_period = 1500.0        # 单圈周期（ms）
        self._indet_timer = QTimer(self)
        self._indet_timer.setInterval(16)
        self._indet_timer.timeout.connect(self._indet_tick)
        self.setFixedSize(size, size)
        self.setCursor(Qt.ArrowCursor)

    # ---- Qt Property（供动画用） ----
    def getProgress(self) -> float:
        return self._progress

    def setProgress(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if value == self._progress:
            return
        self._progress = value
        self.update()
        self.progressChanged.emit(self._progress)

    progress = Property(float, getProgress, setProgress)

    # ---- 主题 ----
    def setDark(self, dark: bool) -> None:
        if dark == self._dark:
            return
        self._dark = dark
        self.update()

    def isDark(self) -> bool:
        return self._dark

    # ---- 配色覆盖（优先于 setDark 主题） ----
    @staticmethod
    def _to_color(value) -> "QColor | None":
        """把 QColor / "#rgb" / "#rrggbb" / "#rrggbbaa" / None 归一为 QColor 副本。"""
        if value is None:
            return None
        if isinstance(value, QColor):
            return QColor(value)
        return QColor(str(value))

    def setAccentColor(self, value) -> None:
        """覆盖弧线（以及中心停止方块）的颜色；传 None 恢复主题默认。"""
        c = self._to_color(value)
        if c != self._accent_override:
            self._accent_override = c
            self.update()

    def setTrackColor(self, value) -> None:
        """覆盖底部轨道的颜色；传 None 恢复主题默认。"""
        c = self._to_color(value)
        if c != self._track_override:
            self._track_override = c
            self.update()

    def setHoverColor(self, value) -> None:
        """覆盖命中区 hover 浅底的颜色；传 None 恢复主题默认。"""
        c = self._to_color(value)
        if c != self._hover_override:
            self._hover_override = c
            self.update()

    # ---- 不确定（循环）动画 ----
    def set_indeterminate(self, on: bool) -> None:
        """开启 / 关闭循环动画（检查中）：开启后蓝色条沿轨道匀速扫圈，带拖影，循环。"""
        if on == self._indeterminate:
            return
        self._indeterminate = on
        if on:
            self._indet_progress = 0.0
            self._indet_elapsed = 0.0
            self._indet_cycles = 0
            self._indet_timer.start()
        else:
            self._indet_timer.stop()
        self.update()

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def _indet_tick(self) -> None:
        self._indet_elapsed += self._indet_timer.interval()
        while self._indet_elapsed >= self._indet_period:
            self._indet_elapsed -= self._indet_period
            self._indet_cycles += 1
        self._indet_progress = self._indet_elapsed / self._indet_period
        self.update()

    def _paint_indeterminate(self, p, c, r, th, accent) -> None:
        """蓝色条沿轨道匀速扫一圈：头部（亮）按线性相位走位，身后拖一段
        指数衰减的半透明尾迹（拖影 / 动感模糊感）；头部角度连续累计，循环不跳变。

        采用与 _build_arc_path 一致的「显式点采样」构造弧线，方向完全可控，
        不依赖 drawArc 的隐式角度约定。
        """
        head_abs = self._indet_cycles + self._indet_progress
        # 头部角度（度）：顶部 -90°、顺时针递增（与 _build_arc_path 同一约定）
        head_deg = -90.0 + head_abs * 360.0
        tail = 0.5                                   # 尾迹长度（占一圈比例）
        segs = 64
        pen = QPen(accent, th, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        pts = []
        for i in range(segs + 1):
            frac = i / segs                          # 0=头部, 1=尾端
            ang = math.radians(head_deg - frac * tail * 360.0)
            pts.append(QPointF(c.x() + r * math.cos(ang), c.y() + r * math.sin(ang)))
        for k in range(segs):
            a = 1.0 - (k + 0.5) / segs               # 头部亮 → 尾端透明
            col = QColor(accent)
            col.setAlpha(int(255 * (a ** 1.7)))
            pen.setColor(col)
            p.setPen(pen)
            p.drawLine(pts[k], pts[k + 1])

    # ---- 几何 ----
    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def _center(self) -> QPointF:
        return QPointF(self._size / 2, self._size / 2)

    def _ring_radius(self) -> float:
        # 轨道半径 = 中心到外缘，再内缩半个线宽，避免描边被裁切
        return self._size / 2 - self._thickness / 2 - 0.5

    def _stop_square_rect(self) -> QRectF:
        """中心停止方块（圆角），尺寸随控件缩放。"""
        side = self._size * 0.26
        c = self._center()
        r = side / 2
        return QRectF(c.x() - r, c.y() - r, side, side)

    def _hit_radius(self) -> float:
        """可点击的中心命中半径（略大于停止方块，手感更好）。"""
        return self._size * 0.34

    # ---- 交互 ----
    def _inside_stop(self, pos: QPointF) -> bool:
        c = self._center()
        dx = pos.x() - c.x()
        dy = pos.y() - c.y()
        return (dx * dx + dy * dy) <= self._hit_radius() ** 2

    def enterEvent(self, e):
        self._hover = True
        self._pos = e.position()
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.setCursor(Qt.ArrowCursor)
        self.update()
        super().leaveEvent(e)

    def mouseMoveEvent(self, e):
        self._pos = e.position()
        if self._cancellable and self._inside_stop(self._pos):
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if self._cancellable and e.button() == Qt.LeftButton and self._inside_stop(
            QPointF(e.position().x(), e.position().y())
        ):
            self.cancelled.emit()
        super().mousePressEvent(e)

    # ---- 绘制 ----
    def _build_arc_path(self) -> QPainterPath:
        """从 12 点（顶部）起、按当前进度顺时针扫过的弧线路径。"""
        c = self._center()
        r = self._ring_radius()
        start_deg = -90.0                      # 顶部
        end_deg = start_deg + self._progress * 360.0
        n = max(2, int(120 * max(0.01, self._progress)))
        path = QPainterPath()
        for i in range(n + 1):
            t = i / n
            ang = math.radians(start_deg + (end_deg - start_deg) * t)
            # 屏幕坐标 y 朝下：正角度即顺时针，顶部 = -90°
            x = c.x() + r * math.cos(ang)
            y = c.y() + r * math.sin(ang)
            pt = QPointF(x, y)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        return path

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._center()
        r = self._ring_radius()
        th = self._thickness

        # 先取主题默认色，再用显式覆盖（setAccentColor / setTrackColor / setHoverColor）
        if self._dark:
            track = QColor(255, 255, 255, 28)
            accent = QColor(10, 132, 255)      # iOS dark 模式蓝更亮一点
            hov = _HOVER_DARK
        else:
            track = QColor(_TRACK)
            accent = QColor(_ACCENT)
            hov = _HOVER_LIGHT
        if self._track_override is not None:
            track = QColor(self._track_override)
        if self._accent_override is not None:
            accent = QColor(self._accent_override)
        if self._hover_override is not None:
            hov = QColor(self._hover_override)

        # hover 命中区浅底（仅当鼠标在停止区内时提示可点）
        if self._cancellable and self._hover and self._inside_stop(self._pos):
            p.setPen(Qt.NoPen)
            p.setBrush(hov)
            p.drawEllipse(c, self._hit_radius(), self._hit_radius())

        # 1) 背景轨道（完整淡圈）
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(track, th, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawEllipse(c, r, r)

        # 2) 不确定（循环）动画：蓝色条沿轨道匀速扫圈 + 拖影
        if self._indeterminate:
            self._paint_indeterminate(p, c, r, th, accent)
        #    确定进度弧（更新中）：强调蓝顺时针填充
        elif self._progress > 0.0:
            p.setPen(QPen(accent, th, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(self._build_arc_path())

        # 3) 中心停止方块（圆角），颜色用强调蓝（与进度弧一致，更贴合 App Store）
        #    不可中断的任务（cancellable=False）不绘制，避免无效的停止暗示。
        if self._cancellable:
            sq = self._stop_square_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(sq, sq.width() * 0.22, sq.height() * 0.22)

        p.end()
