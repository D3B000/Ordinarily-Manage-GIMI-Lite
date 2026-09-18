"""
omg_tips.py - Custom tooltip bubble system for OMGLite.

**已废弃（2026-09-17）**：提示气泡已由 ``omg.ui.OMGPopCard``（OMG 风格悬浮卡片
+ OMGKeyCap 键帽）替代，App 内不再调用本模块；保留文件仅供
``tests/_smoke_omg_tips_select.py`` 与历史参照，确认无依赖后可整体删除。

移植自 OMGDev/func/OMGTips.py，并针对 OMGLite 做了「全量覆盖」增强：

- install_tips() 会把事件过滤器装到 QApplication 实例上（应用级），从而覆盖
  **所有**控件，包括运行时动态创建的卡片（资源浏览页的 ItemCard / FolderCard /
  ThumbLabel 等），而这些控件在 install_tips 调用时尚不存在，单纯的
  findChildren() 装不到过滤器。
- 应用级过滤器会拦截所有 ToolTip 事件并返回 True，**彻底屏蔽原生 QToolTip**，
  避免「自定义圆角气泡 + 原生直角投影/阴影」并存（原生 tooltip 窗口自带 OS 投影，
  QSS 的圆角只裁内容、裁不掉投影，故旧样式会露出来）。

用法：
    from omg.ui.omg_tips import install_tips
    # 在 UI 构建完成后调用（QApplication 已存在）：
    install_tips(self)                      # 默认主题 dark
    install_tips(self, lambda: "dark")      # 显式主题
"""

from PySide6.QtWidgets import QWidget, QApplication
from PySide6.QtCore import Qt, QObject, QTimer, QRect, QPoint, QSize, QRectF
from PySide6.QtGui import QCursor, QPainter, QColor, QPen, QFont, QFontMetrics, QPainterPath

# 气泡文本的字号：size 计算（_update_size）与绘制（paintEvent）必须用同一套参数，
# 否则气泡尺寸与实际绘制的文字不匹配（换行溢出 / 留白过多），故提取为常量。
_TIP_FONT_FAMILY = "Microsoft YaHei UI"
_TIP_FONT_SIZE = 10


class TipBubble(QWidget):
    """Tooltip-like bubble as a frameless always-on-top window."""

    def __init__(self):
        super().__init__(None)  # No parent — independent top-level window
        self.setWindowFlags(
            Qt.ToolTip           # Tooltip window type (no taskbar, no focus)
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 关键：bubble 对鼠标事件透明，鼠标事件穿透到下方触发控件。
        # 否则气泡作为置顶窗口出现在光标附近会抢占鼠标 → 触发控件收到伪造的
        # Leave → 延迟隐藏 → 气泡消失后光标又落在触发控件上 → 再次 Enter 显示，
        # 形成「显示→隐藏→显示」的闪烁死循环（悬浮切换键 chip 时尤其明显，因为
        # 气泡紧贴 chip 下方）。设为透明后，气泡的显隐永远不会产生 Leave/Enter。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setStyleSheet("background: transparent;")
        self._text = ""
        self._theme = "dark"
        self._delay_hide_timer = QTimer(self)
        self._delay_hide_timer.setSingleShot(True)
        self._delay_hide_timer.timeout.connect(self._do_hide)
        self._mouse_on_trigger = False  # 鼠标是否仍在触发 widget 上
        # ---- 文本选取状态 ----
        self._selectable = False        # 该气泡是否允许选中文字
        self._selecting = False         # 是否正在拖动选中
        self._sel_anchor = None         # 选区锚点（全局字符索引）
        self._sel_focus = None          # 选区活动端（全局字符索引）
        self._copied_flash = False      # 「已复制」闪烁提示
        # 绘制 / 命中测试用的度量缓存
        self._fm = QFontMetrics(QFont(_TIP_FONT_FAMILY, _TIP_FONT_SIZE))
        self._line_h = self._fm.height() + 2
        self._x0 = 12
        self._y0 = 0
        self._footer_y = 0

    def set_theme(self, theme: str):
        self._theme = theme

    def show_tip(self, text: str, widget_rect: QRect, selectable: bool = False):
        """显示 tip，位置基于触发 widget 的矩形区域计算。

        Args:
            selectable: 为 True 时气泡切换为可交互态（不再对鼠标透明），
                支持拖动选中文字并按 Ctrl+C 复制；为 False 时保持透明（hover
                即用，避免闪烁——此前的默认行为，不影响 Mod 切换键等 chip）。
        """
        self._text = text
        self._selectable = selectable
        self._selecting = False
        self._sel_anchor = None
        self._sel_focus = None
        self._copied_flash = False
        self._update_size()
        # 可选取气泡必须能接收鼠标事件才能完成拖动选中；其余气泡保持透明，
        # 鼠标事件穿透到下方触发控件，避免「显示→隐藏→显示」闪烁死循环。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not selectable)

        # 位置：优先显示在 widget 下方，居中于 widget
        scr = QApplication.primaryScreen().geometry()
        tip_w = self.width()
        tip_h = self.height()

        # 默认在 widget 正下方，水平居中
        x = widget_rect.center().x() - tip_w // 2
        y = widget_rect.bottom() + 8

        # 下方空间不够，改为上方
        if y + tip_h > scr.bottom() - 10:
            y = widget_rect.top() - tip_h - 8

        # 水平边界约束
        x = max(scr.left() + 4, min(x, scr.right() - tip_w - 4))
        y = max(scr.top() + 4, min(y, scr.bottom() - tip_h - 4))

        self.move(x, y)
        self.show()
        self.raise_()
        # 强制触发重绘：当 bubble 已显示且新文本尺寸与旧文本相近时，
        # setFixedSize 不会触发 update()，show() 对已可见窗口也是 no-op，
        # 导致快速切换 widget 时 tooltip 内容不更新（如"关于"→"设置"）。
        self.update()
        self._delay_hide_timer.stop()

    def hide_tip(self):
        """延迟 150ms 后隐藏，如果鼠标仍在 trigger 上则不隐藏。"""
        self._delay_hide_timer.stop()
        if self._mouse_on_trigger:
            return
        self._delay_hide_timer.start(150)

    def _do_hide(self):
        """实际执行隐藏"""
        self.hide()
        # 复位选取态，下次显示是干净的；并恢复透明（hover 即用）行为。
        self._selectable = False
        self._selecting = False
        self._sel_anchor = None
        self._sel_focus = None
        self._copied_flash = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_mouse_on_trigger(self, on: bool):
        """由 filter 调用，通知 trigger 区域的鼠标状态。"""
        self._mouse_on_trigger = on
        if on:
            self._delay_hide_timer.stop()
        else:
            # trigger 离开后，启动延迟隐藏
            self._delay_hide_timer.start(150)

    # ------------------------------------------------------------------
    # 文本选取 / 复制
    # ------------------------------------------------------------------
    def is_interactive(self) -> bool:
        """气泡当前是否处于可交互（可选取）状态。"""
        return self._selectable and self.isVisible()

    def is_selecting(self) -> bool:
        return self._selecting

    def has_selection(self) -> bool:
        return (self._sel_anchor is not None and self._sel_focus is not None
                and self._sel_anchor != self._sel_focus)

    def selected_text(self) -> str:
        """返回当前选中的子串（无选区返回空串）。"""
        if not self.has_selection():
            return ""
        a, b = sorted((self._sel_anchor, self._sel_focus))
        return self._text[a:b]

    def copy_selection(self):
        """将选中文字复制到剪贴板；无选区则复制整段提示。"""
        txt = self.selected_text() or self._text
        if txt:
            QApplication.clipboard().setText(txt)
            self._copied_flash = True
            self.update()
            QTimer.singleShot(1200, self._clear_flash)

    def _clear_flash(self):
        self._copied_flash = False
        self.update()

    def _char_index_at(self, pos: QPoint) -> int:
        """把气泡内坐标映射到全局字符索引（含换行符各占 1）。"""
        lines = self._text.split("\n")
        if not lines:
            return 0
        rel = pos.y() - self._y0 + self._line_h / 2
        li = int(rel // self._line_h)
        li = max(0, min(li, len(lines) - 1))
        line = lines[li]
        x = pos.x() - self._x0
        k = len(line)
        for k in range(len(line) + 1):
            if self._fm.horizontalAdvance(line[:k]) >= x:
                break
        acc = 0
        for j in range(li):
            acc += len(lines[j]) + 1
        return acc + k

    def mousePressEvent(self, event):
        if not self._selectable:
            return super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self._selecting = True
            self._sel_anchor = self._char_index_at(event.pos())
            self._sel_focus = self._sel_anchor
            self.grabMouse()  # 拖出气泡范围仍能持续选中
            self.update()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._sel_focus = self._char_index_at(event.pos())
            self.update()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            self.releaseMouse()
            self.update()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        # 气泡获得焦点时的 Ctrl+C 兜底（应用级过滤器也会拦截，见 _TipBubbleFilter）
        if (event.modifiers() & Qt.ControlModifier) and event.key() == Qt.Key_C \
                and self.has_selection():
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def _update_size(self):
        fm = QFontMetrics(QFont(_TIP_FONT_FAMILY, _TIP_FONT_SIZE))
        self._fm = fm
        lines = self._text.split("\n")
        max_w = max(fm.horizontalAdvance(line) for line in lines) if lines else 0
        line_h = fm.height() + 2
        self._line_h = line_h
        w = max_w + 24
        # 可选取气泡底部多留一行操作提示
        footer_h = line_h if self._selectable else 0
        h = line_h * len(lines) + 16 + footer_h
        self.setFixedSize(max(w, 40), h)
        # 文字块在「去除底部提示行」的区域内垂直居中
        text_area_h = h - footer_h
        total_h = line_h * len(lines)
        self._y0 = (text_area_h - total_h) // 2 + fm.ascent()
        self._x0 = 12
        self._footer_y = text_area_h + line_h - 6

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        # Background
        if self._theme == "light":
            p.setBrush(QColor(255, 255, 255, 240))
            p.setPen(QPen(QColor(0, 0, 0, 25), 1))
            text_color = QColor(51, 51, 51)
        else:
            p.setBrush(QColor(28, 28, 30, 235))
            p.setPen(QPen(QColor(255, 255, 255, 30), 1))
            text_color = QColor(255, 255, 255)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0, 0, -1, -1), 8, 8)
        p.drawPath(path)

        # 选区高亮（绘制在文字之下）
        lines = self._text.split("\n")
        if self.has_selection():
            a, b = sorted((self._sel_anchor, self._sel_focus))
            starts = []
            acc = 0
            for ln in lines:
                starts.append(acc)
                acc += len(ln) + 1
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(76, 194, 255, 90))  # 强调色半透明
            for i, ln in enumerate(lines):
                s = starts[i]
                lo = max(a, s)
                hi = min(b, s + len(ln))
                if hi > lo:
                    x1 = self._x0 + self._fm.horizontalAdvance(ln[:lo - s])
                    x2 = self._x0 + self._fm.horizontalAdvance(ln[:hi - s])
                    ytop = self._y0 - self._fm.ascent() + i * self._line_h
                    p.drawRoundedRect(QRectF(x1, ytop, x2 - x1, self._line_h), 3, 3)

        # Text
        p.setPen(text_color)
        font = QFont(_TIP_FONT_FAMILY, _TIP_FONT_SIZE)
        p.setFont(font)
        fm = QFontMetrics(font)
        line_h = fm.height() + 2
        lines = self._text.split("\n")
        # 与选区高亮共用 _update_size 计算的度量，保证对齐（可选取气泡含底部提示行）
        y_start = self._y0
        x_start = self._x0
        for i, line in enumerate(lines):
            p.drawText(x_start, y_start + i * line_h, line)

        # 可选取气泡底部操作提示
        if self._selectable:
            p.setPen(QColor(150, 150, 150) if self._theme == "dark"
                     else QColor(120, 120, 120))
            tip = "✓ 已复制" if self._copied_flash else "拖动选择文字 · Ctrl+C 复制"
            p.drawText(x_start, self._footer_y, tip)
        p.end()


class _TipBubbleFilter(QObject):
    """Event filter: shows TipBubble on Enter, hides on Leave, blocks default QToolTip."""

    def __init__(self, bubble, get_theme_fn=None):
        super().__init__()
        self._bubble = bubble
        self._get_theme = get_theme_fn or (lambda: "dark")
        self._current_widget = None
        self._closing = False

    def eventFilter(self, obj, event):
        if self._closing:
            return False
        et = event.type()
        # 应用级过滤器会收到整个 App 的所有 QObject 事件（含 QWindow / QAction /
        # QTimer 等），这些对象没有 toolTip() / rect()，需排除，否则会抛
        # AttributeError。只对 QWidget 处理 tooltip 逻辑。
        if not isinstance(obj, QWidget):
            return False
        if et == event.Type.ToolTip:
            return True  # block default QToolTip（避免原生直角+阴影露出来）
        if et == event.Type.Enter:
            # 鼠标进入气泡本身：取消隐藏计时（保持可见，便于选中 / 复制）
            if obj is self._bubble:
                self._bubble.set_mouse_on_trigger(True)
                return False
            if obj.toolTip():
                self._current_widget = obj
                self._bubble.set_theme(self._get_theme())
                self._bubble.set_mouse_on_trigger(True)
                # 传入 widget 的全局矩形，用于定位；selectable 标记决定气泡是否可交互
                sel = bool(obj.property("tipSelectable"))
                self._bubble.show_tip(
                    obj.toolTip(),
                    obj.rect().translated(obj.mapToGlobal(QPoint(0, 0))),
                    selectable=sel)
            return False
        elif et == event.Type.Leave:
            if obj is self._bubble:
                # 离开气泡：未在选择中才允许隐藏（选择/复制过程中保持可见）
                if not self._bubble.is_selecting():
                    self._bubble.set_mouse_on_trigger(False)
                return False
            if obj is self._current_widget:
                self._bubble.set_mouse_on_trigger(False)
            return False
        elif et == event.Type.KeyPress:
            # 气泡处于可交互态且有选区时，拦截 Ctrl+C 复制到剪贴板（避免误改其它控件）
            if ((event.modifiers() & Qt.ControlModifier) and event.key() == Qt.Key_C
                    and self._bubble.is_interactive() and self._bubble.has_selection()):
                self._bubble.copy_selection()
                return True
            return False
        elif et in (event.Type.Hide, event.Type.Close):
            self._bubble.set_mouse_on_trigger(False)
            self._bubble.hide_tip()
            self._current_widget = None
        return False


def install_tips(parent_widget, get_theme_fn=None):
    """
    安装自定义 TipBubble 系统，覆盖 parent_widget 及其所有子控件。

    关键增强（相对 OMGDev 原版）：
    - 同时把事件过滤器装到 QApplication.instance() 上（应用级）。这样即使是
      install_tips 调用**之后**才动态创建的控件（如 Mod 卡片），其原生 ToolTip
      也会被拦截、改用自定义气泡；且原生 QToolTip 全 App 被屏蔽，不会露出直角+阴影。
    - 整个 App 共享同一个 bubble / filter（挂在 QApplication 上），重复调用
      install_tips 只会刷新主题回调、不会重复创建气泡。

    Args:
        parent_widget: 调用方（通常是某页面 / 窗口）。既用于 findChildren 静态
                       覆盖，也作为主题回调的默认来源（getattr(parent_widget,
                       "_theme", "dark")）。
        get_theme_fn: 返回当前主题 "dark" / "light" 的可调用；缺省读 parent._theme。
    """
    app = QApplication.instance()
    if app is None:
        # 尚未创建 QApplication：退化为仅覆盖静态子控件（仍可用，但动态控件无覆盖）
        return _install_on_widget(parent_widget, get_theme_fn)

    if get_theme_fn is None:
        get_theme_fn = lambda: getattr(parent_widget, "_theme", "dark")

    # 整个 App 共享一份 bubble / filter，挂在 QApplication 上
    bubble = getattr(app, "_tip_bubble", None)
    if bubble is None:
        bubble = TipBubble()
        tip_filter = _TipBubbleFilter(bubble, get_theme_fn)
        app._tip_bubble = bubble
        app._tip_filter = tip_filter
        app.installEventFilter(tip_filter)  # 应用级：覆盖所有控件 + 屏蔽原生 ToolTip
    else:
        # 后续调用：刷新主题回调为最新调用方的，并复用已装好的应用级过滤器
        app._tip_filter._get_theme = get_theme_fn

    # 与 parent 绑定，便于显式处理；静态子控件也装一份（应用级已全覆，此处为兼容）
    parent_widget._tip_bubble = bubble
    parent_widget._tip_filter = app._tip_filter
    for child in parent_widget.findChildren(QWidget):
        if child is not bubble:
            child.installEventFilter(app._tip_filter)
    return bubble


def _install_on_widget(parent_widget, get_theme_fn=None):
    """QApplication 尚未创建时的退化路径（仅静态子控件覆盖）。"""
    if get_theme_fn is None:
        get_theme_fn = lambda: getattr(parent_widget, "_theme", "dark")
    if hasattr(parent_widget, "_tip_bubble") and parent_widget._tip_bubble is not None:
        bubble = parent_widget._tip_bubble
        tip_filter = parent_widget._tip_filter
    else:
        bubble = TipBubble()
        tip_filter = _TipBubbleFilter(bubble, get_theme_fn)
        parent_widget._tip_bubble = bubble
        parent_widget._tip_filter = tip_filter
    for child in parent_widget.findChildren(QWidget):
        if child is not bubble:
            child.installEventFilter(tip_filter)
    return bubble


def get_tip_bubble():
    """返回应用级共享的 TipBubble（未安装则返回 None）。"""
    app = QApplication.instance()
    if app is None:
        return None
    return getattr(app, "_tip_bubble", None)


def show_transient_tip(text: str, global_pos: QPoint,
                       theme: str = "dark", hide_ms: int = 2500) -> None:
    """程序化弹出一个自定义气泡（不依赖 hover 事件），用于错误/状态通知。

    与 hover tooltip 共用应用级共享的 TipBubble，因此视觉风格一致，不会露出
    原生 QToolTip 的直角 + 阴影。定时自动隐藏。

    Args:
        text: 提示文本。
        global_pos: 气泡定位的全局坐标（气泡显示在该点下方）。
        theme: "dark" / "light"。
        hide_ms: 自动隐藏延迟（毫秒）。
    """
    bubble = get_tip_bubble()
    if bubble is None:
        # 尚未安装：先确保安装（app 无 _theme → 默认 dark）
        install_tips(QApplication.instance() if QApplication.instance() else QWidget())
        bubble = get_tip_bubble()
    if bubble is None:
        return
    bubble.set_theme(theme)
    bubble.set_mouse_on_trigger(False)
    # 用位于 global_pos 的 1x1 矩形作定位基准，show_tip 会把它放在该点下方
    bubble.show_tip(text, QRect(global_pos, QSize(1, 1)))
    QTimer.singleShot(hide_ms, bubble._do_hide)

