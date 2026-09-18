"""
omg.ui.OMGPopCard — OMG 风格悬浮卡片（Pop Card）。

移植自 ``Lab/popover.py``，并承担原 ``omg.ui.omg_tips``（OMGTips）的职责：

1. **通用悬浮卡片**：``OMGPopCard(trigger=btn, title=...)``，hover-intent 显隐
   （进入延迟 120ms 显、离开延迟 180ms 隐，卡片自身也算热区），可自由往
   ``card.body`` 里塞内容；
2. **全应用提示气泡（替代 OMGTips）**：``install_pop_cards(self)`` 把过滤器装到
   ``QApplication`` 上，接管**所有**控件（含运行时动态创建的 Mod 卡片）的
   ``toolTip()``，用本卡片绘制并**彻底屏蔽原生 QToolTip**（原生气泡是直角 +
   OS 阴影，QSS 圆角裁不掉投影）。
   快捷方式以 ``OMGKeyCap`` 渲染——文本里出现 ``Ctrl+S`` / ``Alt+` `` 这类
   组合键（或控件带 ``popShortcut`` 属性）时自动画成键帽。

与 OMGTips 的差异（为什么要换）：
* 原来是 470 行手绘文本气泡（逐行 drawText、自己做字符索引选区），样式与 OMG
  卡片体系（``#2C2C2E`` 卡底 / ``#3A3A3C`` 边框 / 8px 圆角）不一致；
* 本卡片走「透明顶层窗 + 实体子容器」结构，阴影加在子容器上（直接加在透明窗会
  触发 Windows 的 ``UpdateLayeredWindowIndirect failed``），配色 / 字号 / 圆角
  统一复用 OMG Token，并留出标题区与自由内容区；
  ⚠ 顶层窗的透明留白 ``MARGIN`` 必须 ≥ 阴影的 blur + offset，否则阴影脏区会
  溢出窗口边界，同样会让 ``UpdateLayeredWindowIndirect`` 失败（内容不刷新）；
  定位时也是**整个窗口矩形**（含留白）参与屏幕边界夹取，见 ``_place``；
* 文字选取改为 ``QLabel`` 的原生选区（``TextSelectableByMouse``），比自绘选区
  少一半代码且行为与系统一致；Ctrl+C 仍由应用级过滤器兜底（卡片不抢焦点）。

用法：:

    from omg.ui.OMGPopCard import install_pop_cards, show_transient_card

    install_pop_cards(self, lambda: "dark")     # 替代 install_tips(self, ...)
    show_transient_card("已复制", btn.mapToGlobal(QPoint(0, btn.height())))

    # 手动构造一张悬浮卡片
    card = OMGPopCard(trigger=btn, title="快捷键")
    card.add_text("打开资源浏览")
    card.add_shortcut(["Alt", "`"])
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omg.ui.OMGKeyCap import OMGKeyCapGroup

# ---------------------------------------------------------------------------
# OMG Token（与 omg/pages/setting.py、home.py 同色系；ui 层不反向依赖 pages）
# ---------------------------------------------------------------------------
_TOKENS = {
    "dark": {
        "surface": "#2C2C2E",        # BG_CARD
        "surface_alt": "#38383B",    # BG_CARD_HOVER
        "border": "#3A3A3C",         # BORDER
        "text": "#F5F5F7",           # TEXT_PRIMARY
        "text_secondary": "#98989D",  # TEXT_SECONDARY
        "title": "#FFFFFF",
        "divider": "#3A3A3C",
        "shadow": QColor(0, 0, 0, 150),
        "accent": "#0A84FF",
    },
    "light": {
        "surface": "#FFFFFF",
        "surface_alt": "#F2F2F4",
        "border": "rgba(0,0,0,0.10)",
        "text": "#1D1D1F",
        "text_secondary": "#6B6B73",
        "title": "#18181B",
        "divider": "rgba(0,0,0,0.10)",
        "shadow": QColor(0, 0, 0, 60),
        "accent": "#0A84FF",
    },
}

FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE = 13            # 正文（OMG 紧凑档）
FONT_SIZE_TITLE = 14      # 标题
FONT_SIZE_FOOTER = 12     # 页脚操作提示（次级，略小于正文）
TEXT_MAX_W = 460          # 正文单标签最大宽，超出自动换行（防溢出屏幕）
MAX_INLINE_GROUPS = 5     # 切换键组数上限：超过则内容区改成竖直滚动（见 _sync_scroll）
SCROLLBAR_W = 10          # 滚动区给竖直滚动条预留的宽度（细条 8 + 外边距 2）

RADIUS = 8                # 卡片圆角（介于 RADIUS_CTRL=4 与 RADIUS_CARD=6 之上，浮层需更柔）
PAD_X = 12                # 内容左右内边距
PAD_Y = 10                # 内容上下内边距
SPACING = 8               # 元素间距

SHADOW_BLUR = 18          # 阴影模糊半径
SHADOW_OFFSET_Y = 3       # 阴影下偏移
# 顶层窗的透明留白**必须**装得下整片阴影：QGraphicsDropShadowEffect 的绘制
# 区域是「卡片矩形四周各外扩 blur，再按 offset 平移」，留白小于它时脏区会
# 溢出窗口边界 → Windows 给 layered 窗口的 UpdateLayeredWindowIndirect 收到
# 越界 dirty rect → 失败（参数错误）→ 卡片内容不刷新 / 出现残影。
# 故留白 ≥ blur + |offset|，再多留 4px 余量。改阴影参数时留白自动跟随。
MARGIN = SHADOW_BLUR + abs(SHADOW_OFFSET_Y) + 4
EDGE_PAD = 6              # 卡片距屏幕可用区边缘的最小留白
TRIGGER_GAP = 8           # 卡片与触发控件的间距

SHOW_DELAY = 120          # hover-intent：进入热区到显示
HIDE_DELAY = 180          # hover-intent：离开热区到收起
HIDE_DELAY_TIP = 150      # tooltip 模式：离开 trigger 到收起

# ---------------------------------------------------------------------------
# 悬浮行为模式（互斥；默认展示型 = 历史线上行为，存量调用方零影响）
# ---------------------------------------------------------------------------
# 展示型：纯展示。卡片对鼠标穿透（不可交互），鼠标移出触发区（含移入卡片）
#         后短暂延迟（HIDE_DELAY_TIP）即隐藏。适合「看一眼就走」的提示。
# 交互型：卡内可自由悬停 / 拖选 / 复制文字；鼠标移出「触发区 + 卡片」热区、
#         点击两者之外、或按 Esc 时才关闭（隐含 selectable=True）。
# 调用方式：给触发控件挂动态属性 ``tipInteractive=True``（应用级过滤器按
# 属性分派），或程序化调用 ``show_tip(..., hover_mode=...)``。
HOVER_DISPLAY = "display"          # 展示型（默认）
HOVER_INTERACTIVE = "interactive"  # 交互型
_HOVER_MODES = (HOVER_DISPLAY, HOVER_INTERACTIVE)


def _tokens(theme: str) -> dict:
    return _TOKENS.get(theme, _TOKENS["dark"])


# ---------------------------------------------------------------------------
# 组合键解析：把 "Alt+`" / "资源浏览（Alt+`）" 识别成键帽
# ---------------------------------------------------------------------------
_KEY_ALIASES = {
    "ctrl": "Ctrl", "control": "Ctrl", "ctl": "Ctrl",
    "alt": "Alt", "shift": "Shift", "win": "Win", "cmd": "Cmd",
    "esc": "Esc", "escape": "Esc", "enter": "Enter", "return": "Enter",
    "space": "Space", "tab": "Tab", "backspace": "Backspace",
    "del": "Del", "delete": "Del", "ins": "Ins",
    "home": "Home", "end": "End", "pgup": "PgUp", "pgdn": "PgDn",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
}
# 主键盘上的**全部**单字符符号键（US 布局，逐个枚举以免漏项）：
#   数字行 ``~ ! @ # $ % ^ & * ( )`` / ``- _ = +``
#   中排 ``{ [ } ]`` ``\ |``        底排 ``; :`` ``' "`` ``, < . > / ?``
# 之前只列了 `` ` ~ - = / [ ] \ ; ' , . ``，``*`` ``!`` ``@`` ``#`` ``$`` ``%``
# ``^`` ``&`` ``(`` ``)`` ``_`` ``{`` ``}`` ``|`` ``:`` ``"`` ``<`` ``>`` ``?``
# 全被判成「不是键」→ 切换键（如小键盘 ``*``）退化成纯文本。
# 注：``+`` / ``＋`` 是组合键分隔符（见 ``tokenize_keys`` 的 split），实际不会
# 单独成键，列在这里只为枚举完整。
_PUNCT_KEYS = set("`~!@#$%^&*()-_=+{}[]\\|;:'\",<.>/?")
_ARROW_KEYS = set("↑↓←→")


def _is_key_token(tok: str) -> bool:
    """单枚按键是否合法（避免把英文单词误判成键）。

    回归背景：mod 名 ``Citalai Fluffy Cat Mod（toggle）`` 里的 ``toggle``
    曾被括号写法当成键渲染成键帽。规则收紧为——方向箭头 / 已知键名别名
    （Ctrl、Alt、Del…）/ ``F1``-``F24`` / 小键盘 ``Num*`` / **单个**
    字母数字或标点。多字母生词（``toggle`` / ``mode`` / ``DISABLED``）不是键。
    """
    if not tok or len(tok) > 10:
        return False
    if tok in _ARROW_KEYS:
        return True
    if tok.lower() in _KEY_ALIASES:
        return True
    if re.fullmatch(r"F\d{1,2}", tok):
        return True
    # 小键盘：Num0-9 与 Num+ / Num- / Num* / Num/ / Num.
    if re.fullmatch(r"Num[0-9+\-*/.]", tok):
        return True
    return len(tok) == 1 and (tok.isalnum() or tok in _PUNCT_KEYS)


def tokenize_keys(expr: str) -> Optional[List[str]]:
    """``"Alt+`"`` → ``["Alt", "`"]``；不是组合键返回 None。"""
    parts = [p.strip() for p in re.split(r"[+＋]", expr or "") if p.strip()]
    if not parts or len(parts) > 6:
        return None
    if not all(_is_key_token(p) for p in parts):
        return None
    return [_KEY_ALIASES.get(p.lower(), p) for p in parts]


def parse_shortcut_line(line: str) -> Optional[Tuple[str, List[str]]]:
    """识别整行是否为「标签 + 组合键」。

    支持三种写法：``标签: Alt+` `` / ``资源浏览（Alt+`）`` / 整行 ``Ctrl+Shift+S``。
    识别失败返回 None（按普通文本渲染）。
    """
    s = (line or "").strip()
    if not s:
        return None
    m = re.match(r"^(?P<label>[^:：+]{0,24})[:：]\s*(?P<keys>.+)$", s)
    if m:
        keys = tokenize_keys(m.group("keys"))
        if keys:
            return (m.group("label").strip(), keys)
    m = re.match(r"^(?P<label>[^（(]*)[（(]\s*(?P<keys>[^）)]+)\s*[）)]$", s)
    if m:
        keys = tokenize_keys(m.group("keys"))
        if keys:
            return (m.group("label").strip(), keys)
    keys = tokenize_keys(s)
    if keys and len(keys) >= 2:      # 无标签的裸组合键至少两枚，避免误吞单词
        return ("", keys)
    return None


_DIVIDER_RE = re.compile(r"-{3,}")     # 「---」行 → 分隔线
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # 「**名称**」行 → 加粗文本
_SUFFIX_SEP = " · "                    # 「键: 5 · toggle」→ 键帽行尾注（类型等）


def _split_blocks(text: str) -> List[Tuple[str, object]]:
    """把多行文本切成内容块。

    块类型：``("text", str)`` / ``("bold", str)``（整行 ``**…**`` 加粗）/
    ``("divider", None)``（整行 ``---`` 分隔线）/ ``("keys", (label, keys),
    suffix)``——``键: 5 · toggle`` 这类「键帽行 + `` · `` 尾注」，尾注（类型
    等补充说明）与键帽同行渲染（见 ``add_shortcut`` 的 ``suffix``）。

    约定由 ``core/mods_manager.format_toggle_keys_tip`` 等生成方遵守；普通
    文本行不受影响（含 `` · `` 但前段不是组合键行时仍按纯文本渲染）。
    """
    blocks: List[Tuple[str, object]] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            blocks.append(("text", "\n".join(buf)))
            buf.clear()

    for raw in (text or "").split("\n"):
        line = raw.strip()
        if _DIVIDER_RE.fullmatch(line):
            flush()
            blocks.append(("divider", None))
            continue
        m = _BOLD_RE.fullmatch(line)
        if m:
            flush()
            blocks.append(("bold", m.group(1)))
            continue
        parsed = None
        suffix = ""
        if _SUFFIX_SEP in line:
            head, tail = line.split(_SUFFIX_SEP, 1)
            p2 = parse_shortcut_line(head)
            if p2 is not None:
                parsed, suffix = p2, tail.strip()
        if parsed is None:
            parsed = parse_shortcut_line(line)
        if parsed is not None:
            flush()
            blocks.append(("keys", parsed, suffix))
        else:
            buf.append(raw)
    flush()
    return blocks


def _clamp(v: int, lo: int, hi: int) -> int:
    """夹到 [lo, hi]；区间为空（可用区比卡片还小）时贴 lo，保证不越界。"""
    return lo if hi < lo else min(max(v, lo), hi)


def _group_count(blocks: Sequence[Tuple[str, object]]) -> int:
    """内容里的「组」数量：优先按加粗行（切换键组名）计，无则按键帽行计。

    用于判断切换键明细是否需要滚动（``MAX_INLINE_GROUPS``）：一组 = 一个
    切换键（组名 + 键行 + 回退行，由 ``---`` 分隔）。
    """
    n = sum(1 for b in blocks if b[0] == "bold")
    if n:
        return n
    return sum(1 for b in blocks if b[0] == "keys")


def _clear_layout(lay) -> None:
    """清空布局并销毁其中的控件 / 子布局。"""
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
            continue
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
            sub.setParent(None)
            sub.deleteLater()


def divider(theme: str = "dark") -> QFrame:
    """横向发丝分隔线（1px，用背景色填充；HLine+color 在 QSS 下不画线）。

    打 ``omgDivider`` 标记：切换键明细里「一组」= 两条分隔线之间的一段，
    ``_first_groups_height`` 靠它切分并量出前 N 组的高度。
    """
    line = QFrame()
    line.setObjectName("OMGPopCardDivider")
    line.setProperty("omgDivider", True)
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background:{_tokens(theme)['divider']};border:none;")
    return line


class _BodyScrollArea(QScrollArea):
    """承载卡片实体内容的滚动区（仅当内容组数超过 ``MAX_INLINE_GROUPS`` 启用）。

    为什么要自定义：``_place`` 的前提是「首次 sizeHint 一次算准」（可见态
    resize 会被 Windows DWM 修正几何、卡片被越撑越大），而 ``QScrollArea``
    默认 sizeHint 等于**完整内容**高度——切换键一多卡片就撑到几屏高。这里把
    sizeHint 与 maximum 都收敛到 ``max_h``（= 前 N 组的实际高度），并在滚动条
    出现时补上它的宽度，布局才算得出稳定尺寸。
    """

    def __init__(self, max_h: int, theme: str = "dark", parent=None) -> None:
        super().__init__(parent)
        self._max_h = max(1, int(max_h))
        self.setObjectName("OMGPopCardScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.set_theme(theme)

    def set_max_h(self, max_h: int) -> None:
        """更新视口高度上限（内容换了但仍在滚动态时复用同一滚动区）。"""
        self._max_h = max(1, int(max_h))
        self.updateGeometry()

    def set_theme(self, theme: str) -> None:
        t = _tokens(theme)
        self.setStyleSheet(
            f"QScrollArea#OMGPopCardScroll{{background:transparent;"
            f"border:none;}}"
            f"QScrollBar:vertical{{background:transparent;width:8px;"
            f"margin:2px;}}"
            f"QScrollBar::handle:vertical{{background:{t['border']};"
            f"border-radius:4px;min-height:24px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical"
            f"{{height:0;}}")

    def sizeHint(self) -> QSize:      # type: ignore[override]
        w = self.widget()
        hint = w.sizeHint() if w is not None else QSize()
        h = min(hint.height(), self._max_h)
        extra = SCROLLBAR_W if hint.height() > self._max_h else 0
        return QSize(hint.width() + extra, h)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        # 不能返回完整内容高：布局的最小尺寸会在 show 后写进顶层窗
        # minimumSize，把刚算准的几何重新撑大（见 _place 注释）。
        return QSize(0, min(self._max_h, 48))


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------
class OMGPopCard(QWidget):
    """OMG 风格悬浮卡片。

    Args:
        trigger: 触发控件；传入后启用 hover-intent 自动显隐（Popover 用法）。
                 不传则由外部 / 应用级过滤器驱动（tooltip 用法）。
        title: 可选标题（标题下自动插一条分隔线）。
        theme: ``"dark"``（默认）/ ``"light"``。
    """

    def __init__(self, trigger: QWidget | None = None, title: str | None = None,
                 theme: str = "dark", parent=None):
        super().__init__(
            None, Qt.FramelessWindowHint | Qt.ToolTip | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setStyleSheet("background:transparent;")
        self.setFocusPolicy(Qt.NoFocus)

        self._theme = theme if theme in _TOKENS else "dark"
        self._trigger = trigger
        self._interactive = trigger is not None   # 有 trigger 时可进入卡片
        self._selectable = False
        self._hover_mode = HOVER_DISPLAY   # 悬浮行为模式（show_tip 按次覆盖）
        self._mouse_on_trigger = False
        self._ever_shown = False       # 窗口是否真显示过（决定共享卡是否换新）
        self._text_labels: List[QLabel] = []
        self._footer: Optional[QWidget] = None
        self._footer_labels: List[QLabel] = []
        self._copied_badge: Optional[QLabel] = None
        # 主题切换时按上次内容重建
        self._last_text = ""
        self._last_selectable = False
        self._last_shortcut: Optional[str] = None

        # 离开热区 / trigger 后的延迟收起
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._do_hide)
        # hover-intent（仅 trigger 模式）
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._show_now)

        # 实体卡片容器：阴影 / 背景 / 边框都加在这里（不能直接加在透明顶层窗上）
        self._surface = QFrame(self)
        self._surface.setObjectName("OMGPopCardSurface")
        cv = QVBoxLayout(self._surface)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        if title:
            hb = QHBoxLayout()
            hb.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
            self._title = QLabel(title, self._surface)
            self._title.setObjectName("OMGPopCardTitle")
            hb.addWidget(self._title)
            cv.addLayout(hb)
            cv.addWidget(divider(self._theme))
        else:
            self._title = None

        # 实体内容容器：内容统一挂在 ``_content`` 上，组数过多时整块搬进
        # ``_BodyScrollArea``（见 ``_sync_scroll``），卡片高度才不会失控。
        self._content = QWidget(self._surface)
        self._content.setObjectName("OMGPopCardContent")
        self.body = QVBoxLayout(self._content)
        self.body.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        self.body.setSpacing(SPACING)
        cv.addWidget(self._content)
        self._scroll: Optional[_BodyScrollArea] = None

        shadow = QGraphicsDropShadowEffect(self._surface)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(_tokens(self._theme)["shadow"])
        self._surface.setGraphicsEffect(shadow)
        self._shadow = shadow

        root = QVBoxLayout(self)
        root.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        root.addWidget(self._surface)
        self._apply_theme()

        self.setAttribute(Qt.WA_TransparentForMouseEvents, not self._interactive)

        if trigger is not None:
            # Popover 用法：hover-intent + 全局鼠标热区判定。
            # 用真实全局坐标判热区，规避 Qt.ToolTip 窗口显示瞬间向 trigger 伪造
            # Enter/Leave 造成的反复显隐；并把整层窗口（含阴影留白）纳入热区，
            # 指针划过「卡片与触发器之间的间隙」不会被误判离开。
            trigger.installEventFilter(self)
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def set_theme(self, theme: str) -> None:
        if theme not in _TOKENS or theme == self._theme:
            return
        self._theme = theme
        self._apply_theme()
        # 内容里的标签 / 键帽按新主题重建
        self.set_content(self._last_text, self._last_selectable, self._last_shortcut)

    def _apply_theme(self) -> None:
        t = _tokens(self._theme)
        self._surface.setStyleSheet(
            f"#OMGPopCardSurface{{background:{t['surface']};"
            f"border:1px solid {t['border']};border-radius:{RADIUS}px;}}"
            f"#OMGPopCardContent{{background:transparent;border:none;}}")
        if self._title is not None:
            self._title.setStyleSheet(
                f"color:{t['title']};font-family:'{FONT_FAMILY}';"
                f"font-size:{FONT_SIZE_TITLE}px;font-weight:600;")
        self._shadow.setColor(t["shadow"])
        for lab in self._text_labels:
            lab.setStyleSheet(self._text_qss(
                False, bold=bool(lab.property("boldText"))))
        for lab in self._footer_labels:
            lab.setStyleSheet(self._text_qss(True, FONT_SIZE_FOOTER))
        if self._scroll is not None:
            self._scroll.set_theme(self._theme)
        for grp in self._surface.findChildren(OMGKeyCapGroup):
            grp.set_theme(self._theme)
        for line in self._surface.findChildren(QFrame):
            if line is not self._surface and line.height() == 1:
                line.setStyleSheet(
                    f"background:{t['divider']};border:none;")

    def _text_qss(self, secondary: bool, size: int = FONT_SIZE,
                  bold: bool = False) -> str:
        t = _tokens(self._theme)
        color = t["text_secondary"] if secondary else t["text"]
        weight = ";font-weight:600" if bold else ""
        return (f"color:{color};font-family:'{FONT_FAMILY}';"
                f"font-size:{size}px{weight};background:transparent;"
                f"border:none;")

    # ------------------------------------------------------------------
    # 内容组装
    # ------------------------------------------------------------------
    def clear_body(self) -> None:
        _clear_layout(self.body)
        self._text_labels = []
        self._footer = None
        self._footer_labels = []

    def add_widget(self, w: QWidget) -> QWidget:
        self.body.addWidget(w)
        return w

    def add_divider(self) -> None:
        self.body.addWidget(divider(self._theme))

    def add_text(self, text: str, selectable: bool = False,
                 secondary: bool = False, bold: bool = False,
                 hcenter: bool = False, max_w: int | None = None) -> QLabel:
        """添加一段（可多行的）纯文本。

        长行按内容宽限宽（TEXT_MAX_W）并开启自动换行；宽度取「最长行实际
        像素宽 + 2px 余量」与上限的较小值——不设最小宽度下限，短文本（如
        单个 mod 名）的卡片能收紧到与文本贴合。
        ``hcenter``：单条短文本时水平居中，让左右留白对称。
        ``bold``：加粗（切换键分组名等标题行）。
        ``max_w``：额外限宽（取 ``min(max_w, TEXT_MAX_W)``）。用于「卡片宽度
        由某个自定义控件（如动图）决定」的场景——把文本压到该宽度内换行，
        避免较长文本反过来把卡片撑得比控件还宽。
        """
        lab = QLabel(text)
        if bold:
            lab.setProperty("boldText", True)   # 主题切换时 _apply_theme 复读
        lab.setStyleSheet(self._text_qss(secondary, bold=bold))
        lab.setWordWrap(True)
        if hcenter:
            lab.setAlignment(Qt.AlignHCenter)
        # 测宽字体必须与 QSS 渲染字体**完全一致**：QSS font-size:13px 是像素
        # 字号，QFont(FONT_FAMILY, 13) 是点字号（≈17px）——用后者测宽会把
        # 标签固定宽放大 ~1.33 倍，卡片右侧留出一大截空白。
        fm_font = QFont(FONT_FAMILY)
        fm_font.setPixelSize(FONT_SIZE)
        # 测宽字重必须与渲染一致：加粗行（切换名）若按常规体度量，固定宽会
        # 偏小，加粗后文字超出 QLabel 矩形被裁掉 → 「切换名称显示不全」。
        fm_font.setWeight(QFont.Bold if bold else QFont.Normal)
        fm = QFontMetrics(fm_font)
        lines = (text or "").split("\n")
        need = max((fm.horizontalAdvance(ln) for ln in lines), default=0)
        cap = TEXT_MAX_W if max_w is None else max(12, min(int(max_w), TEXT_MAX_W))
        lab.setFixedWidth(min(max(need + 2, 12), cap))
        lab.setTextInteractionFlags(
            Qt.TextSelectableByMouse if selectable else Qt.NoTextInteraction)
        lab.setCursor(Qt.IBeamCursor if selectable else Qt.ArrowCursor)
        self.body.addWidget(lab)
        self._text_labels.append(lab)
        return lab

    def add_shortcut(self, keys: Sequence[str], label: str = "",
                     suffix: str = "") -> QWidget:
        """添加一行「标签 + 键帽组合（+ 尾注）」，键帽由 ``OMGKeyCap`` 渲染。

        Args:
            keys: 如 ``["Alt", "`"]`` / ``["Ctrl", "Shift", "S"]``。
            label: 左侧说明文字，可为空。
            suffix: 键帽右侧的次级尾注文字（如切换键类型 ``toggle``），
                与键帽同行；可为空。
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        if label:
            lab = QLabel(label)
            lab.setStyleSheet(self._text_qss(False))
            lay.addWidget(lab)
        lay.addWidget(OMGKeyCapGroup(list(keys), theme=self._theme))
        if suffix:
            s = QLabel(suffix)
            s.setStyleSheet(self._text_qss(secondary=True))
            lay.addWidget(s)
        lay.addStretch()
        self.body.addWidget(row)
        return row

    def set_content(self, text: str, selectable: bool = False,
                    shortcut: str | None = None,
                    footer: bool = True) -> None:
        """按文本重建内容区：普通行渲染成文本，组合键行渲染成键帽。

        另支持（见 ``_split_blocks``）：``---`` 分隔线行、``**加粗**`` 行、
        「键: X · 尾注」同行尾注。仅当整体是**单条纯文本**（如 mod 名）时
        水平居中，结构化内容保持左对齐。

        Args:
            footer: 可选取内容是否在末尾追加「拖动选择文字 · Ctrl+C 复制」
                操作提示。悬浮提示（tooltip）默认保留；**点击触发的明细卡**
                （``show_click_card``）默认关闭——那行提示对「点开看一眼」
                的场景是噪音（复制能力本身不受影响）。
        """
        self._last_text = text or ""
        self._last_selectable = selectable
        self._last_shortcut = shortcut
        self.clear_body()
        blocks = _split_blocks(text)
        # 仅「单行纯文本」（如 mod 名 / 文件夹名）水平居中；多行段落与
        # 结构化内容保持左对齐，阅读动线不被打散。
        hcenter = (len(blocks) == 1 and blocks[0][0] == "text"
                   and "\n" not in str(blocks[0][1]))
        for blk in blocks:
            kind = blk[0]
            if kind == "keys":
                (label, keys), suffix = blk[1], blk[2]
                self.add_shortcut(keys, label, suffix=suffix)
            elif kind == "divider":
                self.add_divider()
            elif kind == "bold":
                self.add_text(str(blk[1]), selectable=selectable, bold=True)
            else:
                self.add_text(str(blk[1]), selectable=selectable,
                              hcenter=hcenter)
        if shortcut:
            keys = tokenize_keys(shortcut)
            if keys:
                self.add_divider()
                self.add_shortcut(keys, "快捷键")
        if selectable and footer:
            self.add_divider()
            self._build_footer()
        # 组数过多时把内容搬进滚动区（卡片高度 = 前 MAX_INLINE_GROUPS 组）
        self._sync_scroll(_group_count(blocks) > MAX_INLINE_GROUPS)

    def _sync_scroll(self, need: bool) -> None:
        """按需把 ``_content`` 装进 / 取出滚动区（阈值 ``MAX_INLINE_GROUPS``）。"""
        cv = self._surface.layout()
        if need:
            max_h = self._first_groups_height(MAX_INLINE_GROUPS)
            if self._scroll is not None:      # 已在滚动态：只更新视口高度
                self._scroll.set_max_h(max_h)
                return
            cv.removeWidget(self._content)
            self._content.setParent(None)
            area = _BodyScrollArea(max_h, self._theme, self._surface)
            area.setWidget(self._content)
            cv.addWidget(area, 1)
            self._scroll = area
        elif not need and self._scroll is not None:
            # 先摘下内容再销毁滚动区：否则 content 会随滚动区一起被回收
            self._scroll.takeWidget()
            cv.removeWidget(self._scroll)
            self._scroll.setParent(None)
            self._scroll.deleteLater()
            self._scroll = None
            cv.addWidget(self._content)

    def _first_groups_height(self, groups: int) -> int:
        """前 ``groups`` 组内容的实际高度（组 = 两条分隔线之间的一段）。

        用它当滚动视口高度，卡片刚好展示这么多组、其余滚动——比写死像素高度
        贴合内容（组高随键行有无而变）。
        """
        lay = self.body
        total = 0
        seen = 0
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is None:
                continue
            if total:
                total += lay.spacing()
            total += w.sizeHint().height()
            if w.property("omgDivider"):
                seen += 1
                if seen >= groups:
                    break
        return max(total, 60)

    def _build_footer(self) -> None:
        """页脚操作提示；Ctrl+C 用 OMGKeyCap 键帽渲染（此前为纯文本）。

        注意页脚是普通布局项，复制反馈**不能**靠重建页脚实现——可见状态下
        任何布局变化都会触发 Qt 对顶层窗口的自动 resize（DWM 隐形边框会把
        几何撑大 + 警告刷屏），反馈改用 ``copy_selection`` 里的悬浮 badge。
        """
        if self._footer is not None:
            self.body.removeWidget(self._footer)
            self._footer.deleteLater()
            self._footer = None
            self._footer_labels = []
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        l1 = QLabel("拖动选择文字 ·")
        l1.setStyleSheet(self._text_qss(True, FONT_SIZE_FOOTER))
        lay.addWidget(l1)
        self._footer_labels.append(l1)
        lay.addWidget(OMGKeyCapGroup(["Ctrl", "C"], theme=self._theme))
        l2 = QLabel("复制")
        l2.setStyleSheet(self._text_qss(True, FONT_SIZE_FOOTER))
        lay.addWidget(l2)
        self._footer_labels.append(l2)
        lay.addStretch()
        self.body.addWidget(box)
        self._footer = box

    # ------------------------------------------------------------------
    # 文本选取 / 复制
    # ------------------------------------------------------------------
    def is_interactive(self) -> bool:
        return self._selectable and self.isVisible()

    def has_selection(self) -> bool:
        return any(lab.hasSelectedText() for lab in self._text_labels)

    def selected_text(self) -> str:
        parts = [lab.selectedText() for lab in self._text_labels
                 if lab.hasSelectedText()]
        return "\n".join(parts)

    def copy_selection(self) -> None:
        txt = self.selected_text() or self._last_text
        if not txt:
            return
        QApplication.clipboard().setText(txt)
        # 反馈用悬浮 badge（toast），不参与布局：可见状态下动布局会让 Qt
        # 自动 resize 顶层窗口 → DWM 修正几何 → 警告 + 卡片变形。
        t = _tokens(self._theme)
        if self._copied_badge is not None:
            self._copied_badge.deleteLater()
            self._copied_badge = None
        badge = QLabel("✓ 已复制", self._surface)
        badge.setStyleSheet(
            f"color:{t['text']};background:{t['surface_alt']};"
            f"border:1px solid {t['border']};border-radius:4px;"
            f"font-family:'{FONT_FAMILY}';font-size:{FONT_SIZE_FOOTER}px;"
            f"padding:3px 10px;")
        badge.adjustSize()
        badge.move((self._surface.width() - badge.width()) // 2,
                   (self._surface.height() - badge.height()) // 2)
        badge.show()
        badge.raise_()
        self._copied_badge = badge
        QTimer.singleShot(1200, self._dismiss_badge)

    def _dismiss_badge(self) -> None:
        if self._copied_badge is not None:
            self._copied_badge.hide()
            self._copied_badge.deleteLater()
            self._copied_badge = None

    # ------------------------------------------------------------------
    # 显隐
    # ------------------------------------------------------------------
    def show_tip(self, text: str, widget_rect: QRect, selectable: bool = False,
                 shortcut: str | None = None,
                 hover_mode: str = HOVER_DISPLAY,
                 footer: bool = True) -> None:
        """以提示气泡的形式显示内容（定位基于触发控件的全局矩形）。

        Args:
            hover_mode: ``HOVER_DISPLAY``（展示型，默认，与历史行为一致）或
                ``HOVER_INTERACTIVE``（交互型）。交互型自动隐含
                ``selectable=True``，并启用「点击卡片外 / Esc 立即关闭」。
            footer: 末尾是否追加「拖动选择文字 · Ctrl+C 复制」操作提示
                （仅可选取内容有效；见 ``set_content``）。
        """
        self._hover_mode = (hover_mode if hover_mode in _HOVER_MODES
                            else HOVER_DISPLAY)
        if self._hover_mode == HOVER_INTERACTIVE:
            selectable = True    # 交互型必然可选中复制
        self._selectable = selectable
        self.set_content(text, selectable=selectable, shortcut=shortcut,
                         footer=footer)
        # 非可选取气泡对鼠标透明：鼠标事件穿透到下方触发控件，否则气泡紧贴光标
        # 出现会抢走鼠标 → 触发控件收到伪造 Leave → 隐藏 → 又 Enter → 闪烁死循环。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not selectable)
        # 前提（_place docstring）：共享卡每次显示前都换新实例（_ensure_fresh_card），
        # 布局无历史缓存，首次 sizeHint 一次算准、全程无可见态 resize。
        self._place(widget_rect)
        self.show()
        self._ever_shown = True
        self.raise_()
        self.update()      # 内容尺寸相近时 show() 是 no-op，强制重绘
        self._hide_timer.stop()

    show_card = show_tip   # 语义化别名

    def show_for(self, widget: QWidget, text: str | None = None,
                 selectable: bool = False,
                 hover_mode: str = HOVER_DISPLAY) -> None:
        """对某个控件显示气泡（文本缺省取它的 toolTip）。

        控件带 ``tipInteractive`` 动态属性时强制交互型（见常量区说明）。
        """
        interactive = bool(widget.property("tipInteractive"))
        rect = widget.rect().translated(widget.mapToGlobal(QPoint(0, 0)))
        self.show_tip(text if text is not None else widget.toolTip(), rect,
                      selectable=selectable or interactive,
                      shortcut=widget.property("popShortcut") or None,
                      hover_mode=HOVER_INTERACTIVE if interactive
                      else hover_mode)

    def hide_tip(self) -> None:
        """延迟收起（鼠标仍在触发控件上则不收起）。"""
        self._hide_timer.stop()
        if self._mouse_on_trigger:
            return
        self._hide_timer.start(HIDE_DELAY_TIP)

    hide_card = hide_tip

    def set_mouse_on_trigger(self, on: bool) -> None:
        """由应用级过滤器通知「鼠标是否还在触发控件 / 卡片上」。"""
        self._mouse_on_trigger = on
        if on:
            self._hide_timer.stop()
        else:
            # 正在拖选文字时保持可见
            if self.has_selection():
                return
            self._hide_timer.start(HIDE_DELAY_TIP)

    def _do_hide(self) -> None:
        self.hide()
        self._selectable = False
        for lab in self._text_labels:
            lab.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setAttribute(Qt.WA_TransparentForMouseEvents,
                          not self._interactive)

    @staticmethod
    def _screen_for(rect: QRect):
        """取触发区所在的屏幕，退化顺序：触发区中心 → 光标 → 主屏。

        多显示器下必须按触发区选屏，否则会把卡片夹到主屏边界上（副屏靠边
        触发时卡片被拖回主屏，甚至整体落到负坐标）。
        """
        for pt in (rect.center() if isinstance(rect, QRect) else None,
                   QCursor.pos()):
            s = QApplication.screenAt(pt) if pt is not None else None
            if s is not None:
                return s
        return QApplication.primaryScreen()

    def _place(self, rect: QRect) -> None:
        """定位：下方优先 → 放不下翻上方 → 仍放不下贴边，并把**整个窗口**
        （含阴影留白）完整夹进屏幕可用区。

        前提：本卡是**全新实例、从未显示**（tooltip 共享卡每次显示前都会
        换新实例，见 ``_ensure_fresh_card``）。此时布局没有历史缓存，
        ``sizeHint`` 一次算准，resize 只更新状态、不触发 native
        SetWindowPos，Windows DWM 的隐形边框修正（``Unable to set
        geometry`` 警告与几何被撑大）根本不会发生。

        1. **夹紧对象是窗口矩形，不是实体卡片矩形**：顶层窗一旦出现负坐标
           或越过屏幕下 / 右边界，layered 窗口的脏区就落在窗口外，
           ``UpdateLayeredWindowIndirect`` 直接失败（参数错误），卡片内容
           不再刷新。留白部分也必须一起夹紧。
        2. **minimumSize 先清零**：show 之后 QLayout::activate() 会把布局
           最小尺寸写入窗口 minimumSize，内容变短后旧 minimum 会锁死
           resize（实测恒卡在 536x139 不肯缩小）。
        """
        self.setMinimumSize(0, 0)
        self.ensurePolished()
        self.resize(self.sizeHint())
        w, h = self.width(), self.height()
        cw, ch = max(1, w - 2 * MARGIN), max(1, h - 2 * MARGIN)

        scr = self._screen_for(rect)
        sg = (scr.availableGeometry() if scr is not None
              else QRect(0, 0, max(w + 2 * EDGE_PAD, 640),
                         max(h + 2 * EDGE_PAD, 480)))
        lo_x = sg.left() + EDGE_PAD
        hi_x = sg.right() - EDGE_PAD - w
        lo_y = sg.top() + EDGE_PAD
        hi_y = sg.bottom() - EDGE_PAD - h

        # 水平：与触发区中心对齐后夹紧（窗口左边 = 卡片左边 - 留白）
        x = _clamp(rect.center().x() - cw // 2 - MARGIN, lo_x, hi_x)
        # 垂直：下方 → 上方 → 选剩余空间大的一侧
        below = rect.bottom() + TRIGGER_GAP - MARGIN
        above = rect.top() - TRIGGER_GAP - ch - MARGIN
        if below <= hi_y:
            y = below
        elif above >= lo_y:
            y = above
        else:
            # 上下都放不下（卡片比上下剩余空间都高）：选空间大的一侧
            y = below if (hi_y - below) >= (above - lo_y) else above
        y = _clamp(y, lo_y, hi_y)      # 兜底：透明留白也不越界
        self.move(int(x), int(y))

    # ------------------------------------------------------------------
    # hover-intent（trigger 模式）
    # ------------------------------------------------------------------
    def eventFilter(self, obj, ev):
        et = ev.type()
        if obj is self._trigger and et == QEvent.Enter:
            if self._trigger_live():
                self._schedule_show()
        elif obj is self._trigger and et in (QEvent.Hide, QEvent.Close):
            # 触发控件被隐藏：立刻取消待显示 + 收起卡片。
            # 该分支不可省——卡片靠**全局鼠标位置**判热区，而隐藏只是让窗口
            # 不可见，触发控件的全局矩形依然停留在原位；若不收起，鼠标划过
            # 屏幕上落在那个残留矩形里的任何点都会重新弹出卡片（表现为「悬浮
            # 首页菜单按钮却弹出设置页的指引卡」）。窗口关闭 / 切换分页 /
            # 切到别的窗口都会走到这里。
            self._show_timer.stop()
            if self.isVisible():
                self._do_hide()
        if et == QEvent.MouseMove and self._trigger is not None:
            self._on_mouse_move(QCursor.pos())
        return super().eventFilter(obj, ev)

    def _trigger_live(self) -> bool:
        """触发控件当前是否「可悬浮」：自身可见且所属顶层窗口可见。

        ``QWidget.isVisible()`` 在任一祖先隐藏（顶层窗口关闭、``QStackedWidget``
        非当前页）时即为 False，正好覆盖全部失效场景。C++ 侧已析构（窗口被
        delete）时返回 False，避免悬垂指针。
        """
        t = self._trigger
        if t is None:
            return False
        try:
            if not t.isVisible():
                return False
            w = t.window()
            return w is None or w.isVisible()
        except RuntimeError:
            return False      # 底层 C++ 对象已释放

    def _hot_at(self, gpos: QPoint) -> bool:
        # 触发控件不可见时**整块热区失效**：窗口关了 / 分页切走了，残留矩形
        # 不能再参与命中判定，否则会误吞屏幕上同位置的其它窗口的鼠标移动。
        if self._trigger_live():
            trig = self._trigger.rect()
            trig.translate(self._trigger.mapToGlobal(QPoint(0, 0)))
            if trig.contains(gpos):
                return True
        # 卡片自身也算热区（鼠标移入卡片不会收起，可拖选文字）
        return self.isVisible() and self.geometry().contains(gpos)

    def _on_mouse_move(self, gpos: QPoint) -> None:
        if self._hot_at(gpos):
            self._hide_timer.stop()
            if not self.isVisible() and not self._show_timer.isActive():
                self._show_timer.start(SHOW_DELAY)
        else:
            self._show_timer.stop()
            if self.isVisible() and not self._hide_timer.isActive():
                self._hide_timer.start(HIDE_DELAY)

    def _schedule_show(self) -> None:
        self._hide_timer.stop()
        if not self.isVisible() and not self._show_timer.isActive():
            self._show_timer.start(SHOW_DELAY)

    def _show_now(self) -> None:
        self._show_timer.stop()
        # trigger 模式内容静态（构造时已定），首次 hint 即最终 hint；
        # 复用时内容不变 → sizeHint 不变 → show 后布局无需重算。
        if not self.isVisible():
            self._place(self._trigger.rect().translated(
                self._trigger.mapToGlobal(QPoint(0, 0))))
        self.show()
        self._ever_shown = True
        self.raise_()


# ---------------------------------------------------------------------------
# 应用级提示系统（替代 omg.ui.omg_tips.install_tips）
# ---------------------------------------------------------------------------
def _ensure_fresh_card(card: "OMGPopCard") -> "OMGPopCard":
    """返回一张「从未显示过」的 tooltip 共享卡；显示过则换新实例并迁移引用。

    原因（实测踩坑）：复用已显示过的卡片时，``set_content`` 换内容后顶层
    布局的 sizeHint 缓存不会失效（invalidate / LayoutRequest 都刷不动），
    唯一能让布局重算的 activate 只在 show 时发生，且要求窗口可见 —— 于是
    每次换内容都是一连串**可见态** resize；Windows DWM 给无边框工具窗附加
    隐形边框，返回几何比请求值大 ~17px（Qt 打印 ``Unable to set geometry``
    警告），卡片还会被越撑越大、贴近屏幕时被拖出边界。

    而**全新实例**的首次 sizeHint 是精确的（布局无历史缓存、无 native 窗口），
    show 一次到位、零警告。tooltip 显示频率低，重建一张几十个控件的卡片
    开销可忽略；旧卡 deleteLater 回收。
    """
    app = QApplication.instance()
    if card is None or not card._ever_shown:
        return card
    new = OMGPopCard(theme=card._theme)
    if app is not None and getattr(app, "_omg_pop_card", None) is card:
        app._omg_pop_card = new
        app._omg_pop_filter._card = new
    card.deleteLater()
    return new


class _PopCardFilter(QObject):
    """应用级事件过滤器：接管 toolTip 显示、屏蔽原生 QToolTip。"""

    def __init__(self, card: OMGPopCard, get_theme_fn=None):
        super().__init__()
        self._card = card
        self._get_theme = get_theme_fn or (lambda: "dark")
        self._current: Optional[QWidget] = None
        self._closing = False

    def _in_card_scope(self, obj: QWidget) -> bool:
        """obj 是否在卡片自身内部（含卡片与其子孙控件）。"""
        return obj is self._card or self._card.isAncestorOf(obj)

    def _in_trigger_scope(self, obj: QWidget) -> bool:
        """obj 是否在当前触发控件范围内（含触发控件与其子孙控件）。"""
        cur = self._current
        return cur is not None and (obj is cur or cur.isAncestorOf(obj))

    def _dismiss_current(self) -> None:
        """立即收起当前提示（交互型的点击外部 / Esc 关闭路径）。"""
        self._current = None
        self._card._hide_timer.stop()
        self._card._do_hide()

    def eventFilter(self, obj, event):
        if self._closing:
            return False
        et = event.type()
        # 应用级过滤器会收到 QWindow / QAction / QTimer 等非控件对象的事件，
        # 它们没有 toolTip() / rect()，必须排除，否则 AttributeError。
        if not isinstance(obj, QWidget):
            return False
        if et == QEvent.ToolTip:
            return True                       # 屏蔽原生 QToolTip
        if et == QEvent.Enter:
            if obj is self._card:
                self._card.set_mouse_on_trigger(True)
                return False
            tip = obj.toolTip()
            if tip:
                self._card = _ensure_fresh_card(self._card)
                self._current = obj
                self._card.set_theme(self._get_theme())
                self._card.set_mouse_on_trigger(True)
                interactive = bool(obj.property("tipInteractive"))
                self._card.show_tip(
                    tip,
                    obj.rect().translated(obj.mapToGlobal(QPoint(0, 0))),
                    selectable=(bool(obj.property("tipSelectable"))
                                or interactive),
                    shortcut=obj.property("popShortcut") or None,
                    hover_mode=HOVER_INTERACTIVE if interactive
                    else HOVER_DISPLAY)
            return False
        if et == QEvent.Leave:
            if obj is self._card:
                self._card.set_mouse_on_trigger(False)
            elif obj is self._current:
                self._card.set_mouse_on_trigger(False)
            return False
        if et == QEvent.KeyPress:
            interactive = (getattr(self._card, "_hover_mode", HOVER_DISPLAY)
                           == HOVER_INTERACTIVE)
            # 交互型：Esc 立即关闭卡片（吞掉按键，避免误触底层控件的 Esc 语义）
            if (interactive and self._card.isVisible()
                    and event.key() == Qt.Key_Escape):
                self._dismiss_current()
                return True
            # 卡片可交互且有选区时拦截 Ctrl+C（避免误触其它控件的复制）
            if ((event.modifiers() & Qt.ControlModifier)
                    and event.key() == Qt.Key_C
                    and self._card.is_interactive()
                    and self._card.has_selection()):
                self._card.copy_selection()
                return True
            return False
        if et == QEvent.MouseButtonPress:
            # 交互型：点击「触发区 + 卡片」热区之外 → 立即关闭。
            # 卡内点击（拖选文字）与触发控件上的点击不算外部，照常放行。
            if (self._card.isVisible()
                    and getattr(self._card, "_hover_mode", HOVER_DISPLAY)
                    == HOVER_INTERACTIVE
                    and not (self._in_card_scope(obj)
                             or self._in_trigger_scope(obj))):
                self._dismiss_current()
            return False
        if et in (QEvent.Hide, QEvent.Close):
            # 只关心「当前触发控件被隐藏 / 关闭」——应用级过滤器会看到全 App 的
            # Hide 事件，若不加以限定，任意无关控件隐藏都会把提示拽走。
            if self._current is not None and obj is self._current:
                self._card.set_mouse_on_trigger(False)
                self._card.hide_tip()
                self._current = None
            return False
        return False


def install_pop_cards(parent_widget, get_theme_fn=None) -> OMGPopCard:
    """安装 OMGPopCard 提示系统（替代 ``omg.ui.omg_tips.install_tips``）。

    过滤器装在 ``QApplication`` 上，因此**调用之后才动态创建**的控件（如 Mod
    卡片）也会被覆盖，且原生 QToolTip 全 App 被屏蔽。整个 App 共享一张卡片，
    重复调用只刷新主题回调。

    Args:
        parent_widget: 调用方（页面 / 窗口），同时作为主题回调默认来源。
        get_theme_fn: 返回 ``"dark"`` / ``"light"`` 的可调用。

    触发控件可选动态属性：
        ``popShortcut``: ``"Alt+\`"`` 等，卡片末尾渲染键帽快捷键行；
        ``tipSelectable``: 卡内文字可拖选复制（展示型 + 可选中）；
        ``tipInteractive``: **交互型**模式——卡内可自由悬停 / 选中复制，
            点击卡外或按 Esc 关闭（隐含 selectable）。
    """
    app = QApplication.instance()
    if app is None:
        return _install_on_widget(parent_widget, get_theme_fn)

    if get_theme_fn is None:
        get_theme_fn = lambda: getattr(parent_widget, "_theme", "dark")  # noqa: E731

    card = getattr(app, "_omg_pop_card", None)
    if card is None:
        card = OMGPopCard(theme=(get_theme_fn() if get_theme_fn else "dark"))
        app._omg_pop_card = card
        app._omg_pop_filter = _PopCardFilter(card, get_theme_fn)
        app.installEventFilter(app._omg_pop_filter)
    else:
        app._omg_pop_filter._get_theme = get_theme_fn

    parent_widget._omg_pop_card = card
    parent_widget._omg_pop_filter = app._omg_pop_filter
    for child in parent_widget.findChildren(QWidget):
        if child is not card:
            child.installEventFilter(app._omg_pop_filter)
    return card


def _install_on_widget(parent_widget, get_theme_fn=None) -> OMGPopCard:
    """QApplication 尚未创建时的退化路径（仅覆盖静态子控件）。"""
    if get_theme_fn is None:
        get_theme_fn = lambda: getattr(parent_widget, "_theme", "dark")  # noqa: E731
    if getattr(parent_widget, "_omg_pop_card", None) is not None:
        return parent_widget._omg_pop_card
    card = OMGPopCard(theme="dark")
    flt = _PopCardFilter(card, get_theme_fn)
    parent_widget._omg_pop_card = card
    parent_widget._omg_pop_filter = flt
    for child in parent_widget.findChildren(QWidget):
        if child is not card:
            child.installEventFilter(flt)
    return card


def get_pop_card() -> Optional[OMGPopCard]:
    """返回应用级共享的 OMGPopCard（未安装则返回 None）。"""
    app = QApplication.instance()
    if app is None:
        return None
    return getattr(app, "_omg_pop_card", None)


def show_click_card(widget: QWidget, text: str | None = None,
                    selectable: bool = False,
                    hover_mode: str = HOVER_INTERACTIVE,
                    shortcut: str | None = None,
                    toggle: bool = True,
                    footer: bool = False) -> bool:
    """**点击触发**显示共享卡（与悬浮即显的 tooltip 互补）。

    用于「点按钮才看」的明细场景（如 Mod 卡片的切换键明细按钮）：

    * 默认 **交互型**（``HOVER_INTERACTIVE``）——卡内可悬停、拖选复制，
      点击卡片 / 按钮之外或按 Esc 关闭；
    * 默认**不显示**页脚操作提示（``footer=False``）：点击明细卡不需要
      「拖动选择文字 · Ctrl+C 复制」这行引导，复制能力本身照旧；
    * ``toggle=True``（默认）时再次点击同一个按钮收起（按钮自身点击不被
      ``_PopCardFilter`` 当作「点击外部」，否则会「刚关闭又被点开」）；
    * 会把 ``_current`` 指向 ``widget``，于是「触发控件被隐藏 / 关闭」
      （卡片滚出视野、页面切换）也会自动收起。

    Returns:
        本次调用后卡片是否处于**显示**状态（False = 被 toggle 收起）。
    """
    app = QApplication.instance()
    if app is None or widget is None:
        return False
    flt = getattr(app, "_omg_pop_filter", None)
    card = get_pop_card()
    if card is None:
        install_pop_cards(widget)
        card = get_pop_card()
        flt = getattr(app, "_omg_pop_filter", None)
    if card is None:
        return False
    # 已显示且锚在同一按钮上 → 收起（toggle）
    if (toggle and card.isVisible() and flt is not None
            and flt._current is widget):
        flt._dismiss_current()
        return False
    card = _ensure_fresh_card(card)
    if app is not None and getattr(app, "_omg_pop_card", None) is not card:
        app._omg_pop_card = card
    if flt is not None:
        flt._card = card
        flt._current = widget
        card.set_theme(flt._get_theme())
    rect = widget.rect().translated(widget.mapToGlobal(QPoint(0, 0)))
    card.set_mouse_on_trigger(True)      # 别让鼠标离开按钮就把卡收走
    card.show_tip(text if text is not None else widget.toolTip(), rect,
                  selectable=selectable, shortcut=shortcut,
                  hover_mode=hover_mode, footer=footer)
    return True


def show_transient_card(text: str, global_pos: QPoint, theme: str = "dark",
                        hide_ms: int = 2500) -> None:
    """程序化弹出一张卡片（不依赖 hover），用于错误 / 状态通知。

    替代 ``omg.ui.omg_tips.show_transient_tip``，参数保持一致。
    """
    card = get_pop_card()
    if card is not None:
        card = _ensure_fresh_card(card)
        if app is not None:
            app._omg_pop_card = card
    if card is None:
        install_pop_cards(QWidget())
        card = get_pop_card()
    if card is None:
        return
    card.set_theme(theme)
    card.set_mouse_on_trigger(False)
    card.show_tip(text, QRect(global_pos, QSize(1, 1)))
    QTimer.singleShot(hide_ms, card._do_hide)
