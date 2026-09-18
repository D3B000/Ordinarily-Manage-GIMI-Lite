"""
omg.pages.quick_build — 「便捷构建」窗口（源 = GitHub 源码 的业务已实现）

弹出窗口，尺寸与设置页一致（640×360），垂直布局：
  - D3D11 卡片：分段控件「源」[GitHub 源码, 已构建文件]；源 下方有分隔线
        · 选 GitHub 源码：选择版本 + 弹簧 + 下拉框 / 下载按钮
        · 选 已构建文件：下载 / 下载按钮
  - 构建 卡片（仅 GitHub 源码 显示）：
        · "项目" + 弹簧 + 下拉框（占位项）+ 构建按钮(primary)（无浏览按钮）
        · 分隔线
        · "环境" + 弹簧 + 动态状态标签（已就绪 / 未就绪 / 检测中…）+ 重新检测按钮
        · 分隔线
        · 优化构建（FieldLabel + switch）
  - 文件优化 卡片（原「神秘仪式」卡片，仅卡片标题改名，内部控件名不变）：
        · 顶部「神秘仪式」switch（关闭→隐藏下方全部，开启→显示）
        · 目标文件 禁用输入框 + 浏览按钮；分隔线；仪式类型 分段[包装,涂装(py),涂装(rs)]，执行按钮位于分段控件右侧
  - 自动化 卡片：
        · "复制到 GIMI" + 弹簧 + 分段控件[构建产物, 仪式产物] + 执行按钮(primary)
        · 分隔线
        · "构建完成后执行神秘仪式" + switch

含输入框（QLineEdit）的子布局不插弹簧（label 直接贴输入框）；其余带 label 的可交互控件行在 label 与控件间插入弹簧，label 左对齐、控件右对齐。
关闭按钮使用 genshin_function_control_close.svg（复用 setting.tinted_icon 按主题前景色 TEXT_PRIMARY 着色以适配深色主题；iconSize 16 → 图形约 11.5px，24×24 按钮内完整显示）；下拉框统一使用 `ArrowComboBox`（圆角细边框、右侧 chevron 为 xiala.svg 顺时针旋转 90°，
展开时箭头变系统蓝、收起恢复默认色、按文本内容自适应宽度）；「选择版本」与「项目」两个
下拉框共用该类，样式完全一致；
Primary 按钮与分段控件「选中项」均为「Accent 填充 + 白色文字」：该样式已收敛到共享的
SETTINGS_QSS（令牌 TEXT_ON_ACCENT = 白色），设置页与便捷构建页一致；聚焦虚线方框已去除。

「源 = GitHub 源码」的业务（后端全部在 omg.core.alchemy，UI 只发信号 / 收信号）：
  · 打开窗口即拉取 SpectrumQT/XXMI-Libs-Package 近 7 个 release 版本号；
    拉取中下拉框末项为不可点击的「正在拉取」，成功后变为可点击的「重新拉取」，
    并默认选中最新版本（列表按版本号新→旧）。
  · 「下载」→ 下载所选版本的源码 zip 并解压到 resources/alchemy/project/
    XXMI-Libs-Package-<version>/；期间按钮变进度圈（可点击中断），结束恢复按钮。
  · 「构建」→ 用本机 MSBuild 编译所选项目，开启「优化构建」时先向源文件注入随机
    死代码；产物 d3d11.dll 复制到 resources/alchemy/artifact/；期间按钮变进度圈，
    无论成功失败都会恢复按钮；成功后按「构建完成后执行神秘仪式」决定是否自动接仪式。
    点「构建」前先查「环境」检测结果：有阻塞项（缺 MSBuild / 工具集 / SDK / 源码 /
    静态库…）时直接拦下并给出第一条阻塞项的建议，不再甩一屏 MSBuild 原始输出。
  · 「文件优化」→ 目标文件默认 artifact/d3d11.dll（可浏览改选）；三种仪式分别对应
    core.alchemy.RITUAL_PACK / RITUAL_PAINT_PY / RITUAL_PAINT_RS，产物写入
    resources/alchemy/output/d3d11.dll；执行期间按钮变进度圈。
  · 「自动化」→ 把 artifact 或 output 的 d3d11.dll 复制 / 替换到 GIMI 目录根。

「构建」卡片的「环境」行（后端在 omg.core.build_env）：
  · 打开窗口 / 切换项目 / 下载完源码 / 点「重新检测」→ 后台跑一次检测（vswhere 等
    子进程 ~100ms，故走 QThread，不卡 UI），期间标签显示「检测中…」。
  · 结果渲染成动态标签：无阻塞项 → 绿色「已就绪」；否则橙色「未就绪」。
  · 悬浮标签 → OMGtips 气泡显示完整清单（每项一行：符号 + 标题 + 结论，
    MISS/WARN 额外缩进一行建议）；标签 hover 加下划线 + 手型光标，点击 = 重新检测。
  · 需求从 .vcxproj 解析（工具集 / SDK / 静态库 / 后处理目录），不写死常量表。

「源 = 已构建文件」的业务（后端同样在 omg.core.alchemy，网络部分在 omg.core.lanzou）：
  · 「下载」→ 免登录拉取蓝奏云「文件夹分享」里的全部 <时间戳>.zip（实现参考
    DoPE/getFileFromLanzoui.py，不依赖 LanZouCloud-API 目录），逐个解压到
    resources/alchemy/cloud/<时间戳>/；已就绪的批次自动跳过。期间按钮变进度圈
    （可点击中断），结束恢复按钮。
  · 该模式下「构建」卡片整块隐藏（无需本地 MSBuild）。
  · 「文件优化」的目标文件固定为 resources/alchemy/input/d3d11.dll。点「执行」时
    先从最新批次目录（如 20260825_135633）里**随机**挑一个含 d3d11.dll 的子文件夹，
    把它的 dll 复制成 input/d3d11.dll，再对该文件执行所选仪式，产物同样落到
    output/d3d11.dll。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QPoint, QSize, QRect, QThread, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

# 复用设置页的组件与设计令牌，保证视觉一致
from omg.pages.setting import (  # noqa: F401  (re-export 供本模块使用)
    ACCENT, ACCENT_HOVER, ACCENT_PRESSED, BG_CARD, BG_CARD_HOVER,
    BG_INPUT, BG_WINDOW, BORDER, CLOSE_ICON, CLOSE_ICON_SIZE, Card,
    HSeparator, RADIUS_CTRL, RADIUS_WIN, SETTINGS_QSS, SegmentedControl,
    Switch, TEXT_ON_ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, WIN_H, WIN_W,
    tinted_icon,
)

from omg.core import alchemy
from omg.core import paths
from omg.core.logging_setup import logger
from omg.widgets.progress_ring import DownloadProgressRing
from omg.ui.window_flags import window_flags  # noqa: E402  顶层 flags 单一真源
from omg.ui.window_geo import CenteredPopupMixin
from omg.ui.OMGPopCard import install_pop_cards, show_transient_card


def _fmt_size(nbytes: int) -> str:
    """把字节数格式化为人类可读字符串（B / KB / MB / GB）。"""
    if nbytes < 1024:
        return f"{nbytes} B"
    kb = nbytes / 1024.0
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.1f} MB"
    gb = mb / 1024.0
    return f"{gb:.2f} GB"


def _dir_size(path: str) -> int:
    """递归统计目录占用字节数（忽略无法访问的文件）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total

# 图标目录（resources/icons，与 home 页的 icons/home 平级）。
# 统一走 paths.ICONS_DIR：开发态解析到 src/omg/resources/icons，
# 冻结态解析到 bin/resources/icons —— 避免 __file__ 相对推算在 one-dir 打包下
# 算出 bin/omg/resources/icons（该目录不存在，导致下拉箭头 QSS url() 失效）。
_ICONS_DIR = paths.ICONS_DIR
# 便捷构建专用图标（箭头等）
_ICONS_QB_DIR = os.path.join(_ICONS_DIR, "quick_build")


def _qb_icon_path(filename: str) -> str:
    """便捷构建专用图标的绝对路径（QSS url() 需要，统一转为正斜杠）。"""
    return os.path.join(_ICONS_QB_DIR, filename).replace("\\", "/")


# 下拉箭头：xiala.svg 绕中心 (512,512) 顺时针旋转 90° 得到的下向双箭头
# （旋转后包围盒 x 82~942 / y 238~803，完整落在 viewBox 内，不会被裁切）
_ARROW_GRAY = _qb_icon_path("xiala-down.svg")
_ARROW_ACCENT = _qb_icon_path("xiala-down-accent.svg")

# 下拉框末项（动态动作项）文案
_TEXT_FETCHING = "正在拉取"
_TEXT_REFETCH = "重新拉取"
_TEXT_FETCH_FAIL = "拉取失败"
_TEXT_REFRESH_OPTS = "更新选项"
_TEXT_NO_PROJECT = "未找到项目"

# 环境状态标签（构建卡片「环境」行）
_TEXT_ENV_PENDING = "检测中…"
_TEXT_ENV_READY = "已就绪"
_TEXT_ENV_NOT_READY = "未就绪"
# 状态配色：就绪=系统绿，未就绪=系统橙（未就绪不一定是错误，如「源码未下载」，
# 故不用红色告警色），检测中=次要灰。
_ENV_COLOR_READY = "#30D158"
_ENV_COLOR_NOT_READY = "#FF9F0A"
_ENV_COLOR_PENDING = TEXT_SECONDARY
# 悬浮清单的兜底文案（检测尚未出结果时）
_TIP_ENV_PENDING = "构建环境：正在检测…"

# 关闭窗口时等待后台线程退出的最长时间（秒）。超时后若仍有不可中断的任务
# （构建 / 仪式）在跑，则隐藏窗口、等其结束后自动关闭，而不是强行析构线程。
_CLOSE_WAIT_SEC = 3.0


class ArrowComboBox(QComboBox):
    """统一风格的 iOS 风下拉框：展开时箭头变系统蓝，收起恢复默认色。

    本页所有下拉框（选择版本 / 项目）都使用本类，以保证样式完全一致：
    基础样式（边框 / 圆角 / 下拉区 / 弹出项）全部来自 QUICKBUILD_QSS，
    本类只负责在 showPopup / hidePopup 时切换 ::down-arrow 的图标颜色。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("ArrowCombo")
        self._set_arrow(_ARROW_GRAY)

    def _set_arrow(self, path: str) -> None:
        # 仅覆盖箭头图标；基础样式（边框 / 圆角 / 下拉区）沿用 QUICKBUILD_QSS
        self.setStyleSheet(
            "QComboBox#ArrowCombo::down-arrow {"
            f" image: url({path});"
            "}"
        )

    def showPopup(self) -> None:  # type: ignore[override]
        self._set_arrow(_ARROW_ACCENT)
        super().showPopup()

    def hidePopup(self) -> None:  # type: ignore[override]
        super().hidePopup()
        self._set_arrow(_ARROW_GRAY)

    # ------------------------------------------------------------------
    # 动态项助手：末项作为「动作项」，可单独设置是否可点击
    # ------------------------------------------------------------------
    def add_action_item(self, text: str, enabled: bool = True) -> int:
        """追加一个动作项（如「重新拉取」/「更新选项」）并返回其索引。"""
        self.addItem(text)
        idx = self.count() - 1
        item = self.model().item(idx)
        if item is not None:
            item.setEnabled(enabled)
        return idx

    def set_item_enabled(self, index: int, enabled: bool) -> None:
        item = self.model().item(index)
        if item is not None:
            item.setEnabled(enabled)

    def is_action_index(self, index: int) -> bool:
        """末项（动作项）索引判定。"""
        return self.count() > 0 and index == self.count() - 1


class EnvStatusLabel(QLabel):
    """「环境」行的动态状态标签：已就绪 / 未就绪 / 检测中…

    三条约束：

    1. **文字与配色由 :meth:`set_state` 统一设置**：给 label 设过 inline
       stylesheet 后，窗口级 QSS 就不再作用于它，所以样式必须一次写全
       （颜色 / 字号 / 透明底），否则会掉字号。
    2. **hover 反馈**：进入时加下划线、离开时去掉（QSS 的 ``text-decoration``
       对 QLabel 支持不稳定，这里直接改 font，行为确定）。配合手型光标，
       提示「这个标签可以点 / 有东西可看」。
    3. **自定义信号叫 ``activated``**：QPushButton 有原生 ``clicked``，子类里
       同名信号会遮蔽原生信号并自环递归崩栈（项目踩过，见 TaskButton 注释）。
       QLabel 虽然没有原生 clicked，仍统一用 ``activated`` 保持约定。
    """

    activated = Signal()

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("EnvStatus")
        self.setCursor(Qt.PointingHandCursor)
        self.set_state(text, _ENV_COLOR_PENDING)

    def set_state(self, text: str, color: str) -> None:
        """一次性设置文案与配色（样式写全，避免继承中断后掉字号）。"""
        self.setText(text)
        self.setStyleSheet(
            f"QLabel#EnvStatus {{"
            f"  color: {color};"
            f"  font-size: 12px;"
            f"  background: transparent;"
            f"}}"
        )

    # ---- 点击 = 重新检测（accept 防止冒泡到窗口拖拽逻辑出错）----
    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            event.accept()
            self.activated.emit()
            return
        super().mouseReleaseEvent(event)

    # ---- hover 下划线 ----
    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._set_underline(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._set_underline(False)
        super().leaveEvent(event)

    def _set_underline(self, on: bool) -> None:
        font = self.font()
        font.setUnderline(on)
        self.setFont(font)


class TaskButton(QPushButton):
    """自带进度圈的按钮：忙碌时文字隐藏、圈显示在按钮正中央。

    继承 QPushButton（非独立容器），因此在布局中与普通按钮尺寸完全一致，
    不会出现「按钮 ↔ 进度圈」切换时的高度跳动问题。

    - ``cancellable=True`` 时圈心绘制停止方块，点击发出 :attr:`cancelled`
      （用于可中断的下载）；构建 / 仪式等不可中断的任务设为 False。
    - 进度圈统一画**白色**（弧线 / 轨道 / 停止方块）。原因：PrimaryBtn 底色是
      ``#0A84FF``，而进度圈默认的强调蓝恰好也是 ``#0A84FF``，弧线与按钮完全同色、
      根本看不见；白色与按钮文字同色，在蓝底和深灰底（ContentBtn ``#2C2C2E``）
      上都清晰。

    .. warning::
       自定义信号**绝不能**命名为 ``clicked``。QPushButton 自带 ``clicked``，
       PySide6 中同名信号会遮蔽原生信号（原生发射改投到 Python 信号），
       此时再写 ``super().clicked.connect(self.clicked)`` 就形成自环无限递归，
       点击即栈溢出硬崩——**没有 traceback、try/except 也接不住**。
       因此这里用 :attr:`activated` 作为对外的点击信号。
    """

    activated = Signal()
    cancelled = Signal()

    def __init__(self, text: str, object_name: str = "ContentBtn",
                 cancellable: bool = True, ring_size: int = 18,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName(object_name)
        self.setCursor(Qt.PointingHandCursor)
        self._original_text = text
        self._cancellable = cancellable

        # 进度圈：子控件，绝对定位居中。默认隐藏。
        self._ring = DownloadProgressRing(self, size=ring_size, thickness=2,
                                           cancellable=cancellable)
        # 白色配色：弧线纯白，轨道/命中区用半透明白（在蓝底与深灰底上都能看清）
        self._ring.setAccentColor(QColor(255, 255, 255))
        self._ring.setTrackColor(QColor(255, 255, 255, 70))
        self._ring.setHoverColor(QColor(255, 255, 255, 45))
        self._ring.hide()
        # 原生 clicked(bool) → activated()（丢掉 checked 参数，保持 0 参签名）
        super().clicked.connect(self.activated)
        self._ring.cancelled.connect(self.cancelled)

    # ---- 尺寸：每次 resize 都把圈居中 ----
    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        sz = self._ring.sizeHint()
        x = (self.width() - sz.width()) // 2
        y = (self.height() - sz.height()) // 2
        self._ring.setGeometry(x, y, sz.width(), sz.height())

    # ---- 对外接口 ----
    def set_busy(self, busy: bool) -> None:
        """切换按钮文字 / 进度圈显示。"""
        if busy:
            self._ring.setProgress(0.0)
            self.setText("")
            self._ring.show()
        else:
            self.setText(self._original_text)
            self._ring.hide()
            self._ring.setProgress(0.0)

    def set_progress(self, percent: float) -> None:
        self._ring.setProgress(max(0.0, min(1.0, float(percent) / 100.0)))

    def set_status(self, text: str) -> None:
        """把阶段状态作为 tooltip 挂到按钮上（hover 可见）。"""
        self.setToolTip(text or "")

    def set_enabled(self, enabled: bool) -> None:
        self.setEnabled(enabled)


class CheckBox(QWidget):
    """深色主题自定义复选框：圆角方块 + 白色对勾。

    不直接用原生 QCheckBox，避免深主题下勾选指示器的样式不一致；本控件只负责
    绘制与状态，自身对鼠标透明（点击由所在行统一转发）。
    """

    toggled = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._checked = False
        self.setFixedSize(14, 14)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRect(1, 1, 12, 12)
        if self._checked:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(rect, 3, 3)
            pen = QPen(QColor("#FFFFFF"))
            pen.setWidth(1.6)
            pen.setJoinStyle(Qt.MiterJoin)
            p.setPen(pen)
            p.drawLine(4, 6, 6, 9)
            p.drawLine(6, 9, 10, 4)
        else:
            p.setPen(QPen(QColor(BORDER)))
            p.setBrush(QColor(BG_INPUT))
            p.drawRoundedRect(rect, 3, 3)

    def set_checked(self, checked: bool) -> None:
        if self._checked != checked:
            self._checked = checked
            self.update()
            self.toggled.emit(checked)

    def is_checked(self) -> bool:
        return self._checked


class _CleanupRow(QWidget):
    """清理页的一行：复选框 + 名称 + 大小；点击整行即可切换选中。"""

    def __init__(self, name: str, path: str, size: int,
                 on_toggle, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path = path
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(8)

        self._cb = CheckBox(self)
        self._cb.toggled.connect(lambda v: on_toggle(path, v))
        lay.addWidget(self._cb)

        name_lbl = QLabel(name, self)
        name_lbl.setObjectName("FieldLabel")
        lay.addWidget(name_lbl, 1)

        size_lbl = QLabel(_fmt_size(size), self)
        size_lbl.setObjectName("TitleHint")
        lay.addWidget(size_lbl)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._cb.set_checked(not self._cb.is_checked())
            return
        super().mouseReleaseEvent(event)

    def sync(self, checked: bool) -> None:
        self._cb.set_checked(checked)


# 在设置页 QSS 基础上补充便捷构建页特有的控件样式（QComboBox / 主要按钮 / 焦点）
QUICKBUILD_QSS = f"""
QScrollArea#ContentArea {{
    background: transparent;
    border: none;
}}

/* 清除聚焦时的虚线方框 */
*:focus {{
    outline: none;
}}

/* 下拉框（iOS 风格：圆角细边框、右侧双箭头，展开时箭头变系统蓝） */
QComboBox {{
    background: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    padding: 5px 30px 5px 10px;
    font-size: 12px;
    selection-color: {TEXT_PRIMARY};
    selection-background-color: {BG_CARD_HOVER};
}}
QComboBox:hover, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox:disabled {{
    color: {TEXT_SECONDARY};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 28px;
    border: none;
    background: transparent;
}}
/* 箭头在 ::drop-down 区域内居中（Qt 默认定位），因此相对右边缘内缩、完整可见；
   区域(28px) 宽于箭头(14px) 与旋转后较宽的图形(约 12px)，不会被裁切。 */
QComboBox::down-arrow {{
    image: url({_ARROW_GRAY});
    width: 14px;
    height: 14px;
    subcontrol-origin: padding;
    subcontrol-position: center;
}}
QComboBox QAbstractItemView {{
    background: {BG_CARD};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CTRL}px;
    outline: none;
    padding: 4px;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    border-radius: 4px;
}}
QComboBox QAbstractItemView::item:selected {{
    background: {ACCENT};
    color: {TEXT_ON_ACCENT};
}}

/* 说明：Primary 按钮与分段控件「选中项」的样式已统一收敛到 SETTINGS_QSS
   （Accent 填充 + TEXT_ON_ACCENT 白色文字），本窗口拼接 SETTINGS_QSS 时
   自动继承，此处不再重复声明，确保设置页与便捷构建页始终一致。 */

/* 标题栏分隔线：与资源浏览页一致（次要文字色 1px 竖线） */
QLabel#TitleSep {{ background: {TEXT_SECONDARY}; border: none; }}

/* 清理页底部操作条 / 说明文字 */
QFrame#CleanBottomBar {{ background: {BG_WINDOW}; border: none; border-top: 1px solid {BORDER}; }}
QLabel#TitleHint {{ color: {TEXT_SECONDARY}; font-size: 11px; background: transparent; }}
QLabel#CleanSummary {{ color: {TEXT_PRIMARY}; font-size: 12px; background: transparent; }}
"""


class QuickBuildWindow(CenteredPopupMixin, QWidget):
    """「便捷构建」窗口：640×360，垂直滚动内容。"""

    def __init__(self, parent: Optional[QWidget] = None, config=None,
                 tray_mode: bool = False) -> None:
        # 任务栏策略（2026-09-18 改）：**仅首页**不进任务栏，其它页面一律显示
        # （理由见 setting.SettingsWindow.__init__ 注释）。tray_mode 仅为签名
        # 兼容保留，不再影响 flags。
        super().__init__(parent, window_flags(hide_from_taskbar=False))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("QuickBuildRoot")
        self.setFixedSize(WIN_W, WIN_H)
        # 几何记忆：首次打开居中，之后按上次关闭位置弹出
        self.init_geometry_persist("quick_build", config)

        #: ConfigManager（读 GIMI 目录等）；为 None 时「复制到 GIMI」不可用
        self._config = config

        alchemy.ensure_dirs()

        # 仅作用于本窗口，避免与 Home 窗口的 HOME_QSS 互相覆盖
        self.setStyleSheet(SETTINGS_QSS + QUICKBUILD_QSS)

        # ---- 外壳 frame（圆角不透明背景）----
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

        self._build_title_bar(main_layout)

        # ---- 内容区（可滚动，垂直布局）----
        self._scroll = QScrollArea(self._frame)
        self._scroll.setObjectName("ContentArea")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        main_layout.addWidget(self._scroll, 1)

        # 内容容器：同时容纳主分页与清理分页。注意 QScrollArea.setWidget 会**销毁**
        # 旧控件，不能靠反复 setWidget 切换页面，因此把两页都作为容器子控件常驻，
        # 用可见性切换（见 _show_page）。
        self._content_container = QWidget(self._scroll)
        self._content_container.setObjectName("PageContent")
        container_layout = QVBoxLayout(self._content_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        self._scroll.setWidget(self._content_container)

        # 主分页（构建）：默认进入此页，无论退出时停留在哪一页
        self._main_page = QWidget(self._content_container)
        self._main_page.setObjectName("PageContent")
        content_layout = QVBoxLayout(self._main_page)
        content_layout.setContentsMargins(16, 14, 16, 14)
        content_layout.setSpacing(12)
        container_layout.addWidget(self._main_page)

        self._drag_offset: Optional[QPoint] = None

        # ---- 业务状态（须早于 _build_content：源切换回调会读取这些字段）----
        self._releases: list = []            # alchemy.fetch_releases 结果
        self._versions: list = []            # 版本下拉框中的版本号（不含动作项）
        self._project_versions: list = []    # 项目下拉框中的版本号（不含动作项）
        self._target_custom: bool = False    # 目标文件是否被用户手动浏览改选
        self._env_report = None              # 最近一次构建环境检测报告（build_env.EnvReport）

        self._fetch_thread = None
        self._dl_thread = None          # GitHub 源码：下载源码 zip
        self._pdl_thread = None         # 已构建文件：下载蓝奏云压缩包
        self._build_thread = None
        self._ritual_thread = None
        self._copy_thread = None
        self._env_thread = None         # 构建环境检测（可重入，防重入见 _start_thread）
        self._clean_thread = None        # 清理线程

        # 分页状态：主分页（构建）为默认页，进入时总是回到主分页（不记忆退出页）
        self._current_page = "main"
        self._selected: dict = {}        # path -> 是否选中（清理）
        self._path_size: dict = {}       # path -> 字节数（清理）
        self._path_category: dict = {}   # path -> 类别标题（清理确认框分组用）

        self._fetch_sig = alchemy.TaskSignals()
        self._dl_sig = alchemy.TaskSignals()
        self._pdl_sig = alchemy.TaskSignals()
        self._build_sig = alchemy.TaskSignals()
        self._ritual_sig = alchemy.TaskSignals()
        self._copy_sig = alchemy.TaskSignals()
        self._env_sig = alchemy.TaskSignals()

        self._build_content(self._main_page, content_layout)

        # ---- 信号一次性接线（放在 UI 构建之后，避免重复 connect）----
        self._wire_signals()

        # ---- 初始化各卡片的动态数据 ----
        # 注意顺序：环境检测依赖「项目」下拉框的当前选中项，故先刷新项目再检测。
        self._refresh_projects()
        self._refresh_env()
        self._sync_target_default()
        self._start_fetch_releases()

        # ---- 清理分页骨架（lazy 渲染：进入时再扫描目录）----
        self._cleanup_page = QWidget(self._content_container)
        self._cleanup_page.setObjectName("PageContent")
        self._cleanup_layout = QVBoxLayout(self._cleanup_page)
        self._cleanup_layout.setContentsMargins(16, 14, 16, 14)
        self._cleanup_layout.setSpacing(12)
        self._cleanup_page.setVisible(False)
        container_layout.addWidget(self._cleanup_page)

        # ---- 底部操作条（仅清理分页显示）----
        self._build_bottom_bar(main_layout)

        # 默认进入主分页（构建），无论上次退出时停留在哪一页
        self._show_page("main")

        # 自定义 tooltip 气泡：覆盖本窗口所有控件，屏蔽原生 QToolTip 直角 + 阴影。
        install_pop_cards(self, lambda: "dark")

    def _wire_signals(self) -> None:
        """把各任务的进度 / 状态 / 结果信号接到 UI（只接一次）。"""
        self._dl_sig.progress.connect(self._btn_download.set_progress)
        self._dl_sig.status.connect(self._btn_download.set_status)
        self._dl_sig.finished.connect(self._on_download_finished)

        self._pdl_sig.progress.connect(self._btn_prebuilt.set_progress)
        self._pdl_sig.status.connect(self._btn_prebuilt.set_status)
        self._pdl_sig.finished.connect(self._on_prebuilt_finished)

        self._build_sig.progress.connect(self._btn_build.set_progress)
        self._build_sig.status.connect(self._btn_build.set_status)
        self._build_sig.finished.connect(self._on_build_finished)

        self._ritual_sig.progress.connect(self._btn_ritual.set_progress)
        self._ritual_sig.status.connect(self._btn_ritual.set_status)
        self._ritual_sig.finished.connect(self._on_ritual_finished)

        self._copy_sig.finished.connect(self._on_copy_finished)

        # 「优化构建」会改变检测项（开启后多一项「注入基线」），切换后重跑一次
        self._switch_optimize.toggled.connect(lambda _on: self._refresh_env())

    # ------------------------------------------------------------------
    # 标题栏
    # ------------------------------------------------------------------
    def _build_title_bar(self, parent_layout: QVBoxLayout) -> None:
        title_bar = QFrame(self._frame)
        title_bar.setObjectName("TitleBar")
        title_bar.setFixedHeight(32)

        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(12, 0, 8, 0)
        tb_layout.setSpacing(4)

        self._title_label = QLabel("便捷构建", title_bar)
        self._title_label.setObjectName("TitleText")
        tb_layout.addWidget(self._title_label)
        tb_layout.addStretch()

        # 清理入口（clean.svg）：点击进入「清理」分页
        self._btn_clean = QPushButton(title_bar)
        self._btn_clean.setObjectName("TitleBtn")
        self._btn_clean.setFixedSize(24, 24)
        self._btn_clean.setIcon(
            tinted_icon("quick_build/clean.svg", 16, TEXT_PRIMARY))
        self._btn_clean.setIconSize(QSize(16, 16))
        self._btn_clean.setCursor(Qt.PointingHandCursor)
        self._btn_clean.setToolTip("清理 alchemy 文件")
        self._btn_clean.clicked.connect(lambda: self._show_page("cleanup"))
        tb_layout.addWidget(self._btn_clean)

        # 清理按钮与（关闭 / 返回）之间的竖分隔线，颜色对齐资源浏览页（次要文字色）
        self._title_sep = QLabel(title_bar)
        self._title_sep.setObjectName("TitleSep")
        self._title_sep.setFixedSize(1, 16)
        tb_layout.addWidget(self._title_sep)

        # 返回按钮（仅清理分页显示）：chevron-left.svg，24×24 图标居中
        self._btn_back = QPushButton(title_bar)
        self._btn_back.setObjectName("TitleBtn")
        self._btn_back.setFixedSize(24, 24)
        self._btn_back.setIcon(
            tinted_icon("chevron-left.svg", 16, TEXT_PRIMARY))
        self._btn_back.setIconSize(QSize(16, 16))
        self._btn_back.setCursor(Qt.PointingHandCursor)
        self._btn_back.setToolTip("返回")
        self._btn_back.clicked.connect(lambda: self._show_page("main"))
        self._btn_back.setVisible(False)
        tb_layout.addWidget(self._btn_back)

        # 关闭按钮（主分页显示；清理分页隐藏，由返回按钮替代）
        self._btn_close = QPushButton(title_bar)
        self._btn_close.setObjectName("TitleBtn")
        self._btn_close.setFixedSize(24, 24)
        self._btn_close.setIcon(tinted_icon(CLOSE_ICON, CLOSE_ICON_SIZE, TEXT_PRIMARY))
        self._btn_close.setIconSize(QSize(CLOSE_ICON_SIZE, CLOSE_ICON_SIZE))
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.clicked.connect(self.close)
        tb_layout.addWidget(self._btn_close)

        parent_layout.addWidget(title_bar)

    # ------------------------------------------------------------------
    # 内容构建
    # ------------------------------------------------------------------
    def _build_content(self, content: QWidget, layout: QVBoxLayout) -> None:
        # 1) D3D11 卡片（始终可见；GitHub 源码 → 版本行，已构建文件 → 下载行）
        self._build_d3d11_card(content, layout)

        # 2) 构建 卡片（仅 GitHub 源码 显示；内含 Builder 与「优化构建」）
        self._build_card = self._build_build_card(content)
        layout.addWidget(self._build_card)

        # 3) 神秘仪式 卡片
        self._build_ritual_card(content, layout)

        # 4) 自动化 卡片
        self._build_automation_card(content, layout)

        layout.addStretch(1)

        # 初始：默认源=GitHub 源码 → 显示 GitHub 专属区块
        self._on_source_changed(0)
        # 神秘仪式 默认开启 → 显示 目标文件 / 仪式类型
        self._on_ritual_toggled(True)

    def _build_d3d11_card(self, parent: QWidget, layout: QVBoxLayout) -> None:
        card = Card("D3D11", parent)
        body = card.body_layout

        # 源：分段控件
        row_src = QHBoxLayout()
        row_src.setSpacing(8)
        lbl_src = QLabel("源", card)
        lbl_src.setObjectName("FieldLabel")
        lbl_src.setFixedWidth(60)
        row_src.addWidget(lbl_src)
        row_src.addStretch(1)

        self._seg_source = SegmentedControl(["GitHub 源码", "已构建文件"], card)
        row_src.addWidget(self._seg_source, 1)
        body.addLayout(row_src)

        # 分隔线（源 下方）
        body.addWidget(HSeparator(card))

        # 选择版本（仅在 GitHub 源码 时显示，用容器 widget 控制可见性）
        self._version_widget = QWidget(card)
        vlay = QHBoxLayout(self._version_widget)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(8)

        lbl_ver = QLabel("选择版本", card)
        lbl_ver.setObjectName("FieldLabel")
        lbl_ver.setFixedWidth(60)
        vlay.addWidget(lbl_ver)
        vlay.addStretch(1)

        self._combo_version = ArrowComboBox(card)
        # 文本宽度：下拉框按文本内容自适应（保留右侧图标区域，不固定外框宽度）
        self._combo_version.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._combo_version.activated.connect(self._on_version_activated)
        vlay.addWidget(self._combo_version)

        # 下载按钮：执行中切换为进度圈（可点击中断）
        self._btn_download = TaskButton("下载", "ContentBtn", cancellable=True)
        self._btn_download.activated.connect(self._on_download_clicked)
        self._btn_download.cancelled.connect(self._on_download_cancelled)
        vlay.addWidget(self._btn_download)
        body.addWidget(self._version_widget)

        # 下载（仅在选择「已构建文件」时显示，用容器 widget 控制可见性）
        self._prebuilt_widget = QWidget(card)
        play = QHBoxLayout(self._prebuilt_widget)
        play.setContentsMargins(0, 0, 0, 0)
        play.setSpacing(8)
        lbl_dl = QLabel("下载", card)
        lbl_dl.setObjectName("FieldLabel")
        lbl_dl.setFixedWidth(60)
        play.addWidget(lbl_dl)
        play.addStretch(1)
        # 与 GitHub 源码的「下载」一致：执行中变进度圈，可点击中断
        self._btn_prebuilt = TaskButton("下载", "ContentBtn", cancellable=True)
        self._btn_prebuilt.activated.connect(self._on_prebuilt_clicked)
        self._btn_prebuilt.cancelled.connect(self._on_prebuilt_cancelled)
        play.addWidget(self._btn_prebuilt)
        body.addWidget(self._prebuilt_widget)

        self._seg_source.currentChanged.connect(self._on_source_changed)
        layout.addWidget(card)

    def _build_build_card(self, parent: QWidget) -> Card:
        card = Card("构建", parent)
        body = card.body_layout

        # 环境（卡片第二项）：label + 弹簧 + 动态状态标签 + 重新检测 按钮。
        # 输入框已取消 —— 旧版把检测到的 MSBuild 路径塞进禁用输入框，既占宽度又
        # 只回答了「MSBuild 在不在」；改为一个状态词 + 悬浮清单，信息密度更高。
        row_b = QHBoxLayout()
        row_b.setSpacing(8)
        lbl_b = QLabel("环境", card)
        lbl_b.setObjectName("FieldLabel")
        row_b.addWidget(lbl_b)
        row_b.addStretch(1)

        # 动态状态标签：检测中… / 已就绪 / 未就绪；悬浮显示完整清单，点击重新检测
        self._lbl_env = EnvStatusLabel(_TEXT_ENV_PENDING, card)
        self._lbl_env.setToolTip(_TIP_ENV_PENDING)
        self._lbl_env.activated.connect(self._refresh_env)
        row_b.addWidget(self._lbl_env)

        self._btn_redetect = QPushButton("重新检测", card)
        self._btn_redetect.setObjectName("BrowseBtn")
        self._btn_redetect.setCursor(Qt.PointingHandCursor)
        self._btn_redetect.clicked.connect(self._on_redetect_env)
        row_b.addWidget(self._btn_redetect)
        body.addLayout(row_b)

        # 分隔线（Builder 下方）
        body.addWidget(HSeparator(card))

        # 项目（行结构与「选择版本」一致：label + 弹簧 + 下拉框 + 按钮）
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("项目", card)
        lbl.setObjectName("FieldLabel")
        row.addWidget(lbl)
        row.addStretch(1)

        # 项目：下拉框列出 project/ 下已下载的版本，末项为「更新选项」
        # 与「选择版本」共用 ArrowComboBox，样式完全一致（含弹出时箭头变系统蓝）；
        # 同样按文本内容自适应宽度，不拉伸。
        self._combo_project = ArrowComboBox(card)
        self._combo_project.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._combo_project.activated.connect(self._on_project_activated)
        row.addWidget(self._combo_project)

        # 构建按钮：执行中切换为进度圈（MSBuild 不可中断 → cancellable=False）
        self._btn_build = TaskButton("构建", "PrimaryBtn", cancellable=False)
        self._btn_build.activated.connect(self._on_build_clicked)
        row.addWidget(self._btn_build)
        body.addLayout(row)

        # 分隔线（项目 下方）
        body.addWidget(HSeparator(card))

        # 优化构建（与「项目」一致：FieldLabel 样式 + switch）
        row_opt = QHBoxLayout()
        row_opt.setSpacing(8)
        lbl_opt = QLabel("优化构建", card)
        lbl_opt.setObjectName("FieldLabel")
        row_opt.addWidget(lbl_opt)
        row_opt.addStretch(1)
        self._switch_optimize = Switch(card)
        row_opt.addWidget(self._switch_optimize)
        body.addLayout(row_opt)

        # 本卡片三个 label 都是短标签，统一取三者所需宽度的较大值作为共享
        # 「最小宽度」：既保证完整显示，又保持三行标签等宽、控件左端对齐。
        # 用 minimumWidth 而非 fixedWidth，字号 / DPI 变化时仍可按 sizeHint 加宽。
        label_w = max(lbl_b.sizeHint().width(),
                      lbl.sizeHint().width(),
                      lbl_opt.sizeHint().width())
        lbl_b.setMinimumWidth(label_w)
        lbl.setMinimumWidth(label_w)
        lbl_opt.setMinimumWidth(label_w)

        return card

    def _build_ritual_card(self, parent: QWidget, layout: QVBoxLayout) -> None:
        # 卡片标题已由「神秘仪式」改为「文件优化」（卡片内部控件名保持不变）
        card = Card("文件优化", parent)
        body = card.body_layout

        # 顶部：神秘仪式 switch（关闭隐藏下方全部，开启显示）
        row_top = QHBoxLayout()
        row_top.setSpacing(8)
        lbl_top = QLabel("神秘仪式", card)
        lbl_top.setObjectName("FieldLabel")
        lbl_top.setFixedWidth(60)
        row_top.addWidget(lbl_top)
        row_top.addStretch(1)
        self._switch_ritual = Switch(card)
        self._switch_ritual.setOn(True)
        self._switch_ritual.toggled.connect(self._on_ritual_toggled)
        row_top.addWidget(self._switch_ritual)
        body.addLayout(row_top)

        # 详情容器：受「神秘仪式」开关控制可见性
        self._ritual_detail = QWidget(card)
        dlay = QVBoxLayout(self._ritual_detail)
        dlay.setContentsMargins(0, 0, 0, 0)
        dlay.setSpacing(8)

        # 目标文件
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("目标文件", card)
        lbl.setObjectName("FieldLabel")
        lbl.setFixedWidth(60)
        row.addWidget(lbl)

        self._edit_target = QLineEdit(card)
        self._edit_target.setPlaceholderText("未选择文件")
        self._edit_target.setDisabled(True)
        row.addWidget(self._edit_target, 1)

        self._btn_browse = QPushButton("浏览", card)
        self._btn_browse.setObjectName("BrowseBtn")
        self._btn_browse.setCursor(Qt.PointingHandCursor)
        self._btn_browse.clicked.connect(self._on_browse_target)
        row.addWidget(self._btn_browse)
        dlay.addLayout(row)

        # 分隔线
        dlay.addWidget(HSeparator(card))

        # 仪式类型 分段控件 + 执行按钮（执行位于分段控件右侧）
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lbl2 = QLabel("仪式类型", card)
        lbl2.setObjectName("FieldLabel")
        lbl2.setFixedWidth(60)
        row2.addWidget(lbl2)
        row2.addStretch(1)

        self._seg_ritual = SegmentedControl(["包装", "涂装(py)", "涂装(rs)"], card)
        row2.addWidget(self._seg_ritual, 1)

        # 执行按钮：执行中切换为进度圈（仪式不可中断 → cancellable=False）
        self._btn_ritual = TaskButton("执行", "PrimaryBtn", cancellable=False)
        self._btn_ritual.activated.connect(self._on_ritual_clicked)
        row2.addWidget(self._btn_ritual)
        dlay.addLayout(row2)

        body.addWidget(self._ritual_detail)

        layout.addWidget(card)

    def _build_automation_card(self, parent: QWidget, layout: QVBoxLayout) -> None:
        card = Card("自动化", parent)
        body = card.body_layout

        # 复制到 GIMI + 弹簧 + 分段控件 + 执行按钮
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel("复制到 GIMI", card)
        lbl.setObjectName("FieldLabel")
        row.addWidget(lbl)
        row.addStretch(1)

        self._seg_copy = SegmentedControl(["构建产物", "仪式产物"], card)
        row.addWidget(self._seg_copy, 1)

        self._btn_copy = QPushButton("执行", card)
        self._btn_copy.setObjectName("PrimaryBtn")
        self._btn_copy.setCursor(Qt.PointingHandCursor)
        self._btn_copy.clicked.connect(self._on_copy_clicked)
        row.addWidget(self._btn_copy)
        body.addLayout(row)

        # 分隔线（复制到 GIMI 下方）
        body.addWidget(HSeparator(card))

        # 构建完成后执行神秘仪式 switch
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lbl2 = QLabel("构建完成后执行神秘仪式", card)
        lbl2.setObjectName("FieldLabel")
        row2.addWidget(lbl2)
        row2.addStretch(1)
        self._switch_auto_ritual = Switch(card)
        row2.addWidget(self._switch_auto_ritual)
        body.addLayout(row2)

        # 本卡片两个 label 文本较长（远超其他卡片的 60px），不能沿用固定 60px，否则被裁切。
        # 取两者所需宽度的较大值作为共享「最小宽度」：既保证文本完整显示，又让两行标签等宽、
        # 控件左端对齐。用 minimumWidth 而非 fixedWidth，字号/DPI 变化时标签仍可按 sizeHint
        # 继续加宽，不会被截断。
        label_w = max(lbl.sizeHint().width(), lbl2.sizeHint().width())
        lbl.setMinimumWidth(label_w)
        lbl2.setMinimumWidth(label_w)

        layout.addWidget(card)

    # ------------------------------------------------------------------
    # 线程生命周期统一管理
    # ------------------------------------------------------------------
    def _start_thread(self, attr: str, thread) -> bool:
        """统一启动任务线程：防重入 + 安全回收。

        Qt 的两条铁律，违反任一条都会让进程直接 abort（表现为「闪退」）：
          1. 不能在 QThread 仍在运行时销毁它（"Destroyed while thread is
             still running"）；
          2. 不能让异常逃出 QThread.run()（见 alchemy._CancelThread 的兜底）。

        这里统一处理第 1 条：用 ``deleteLater`` 让 Qt 在线程**真正结束后**
        再回收 C++ 对象，同时把 Python 端引用置空，避免悬垂引用；已运行的
        同名线程直接忽略本次启动请求（防重入）。

        Args:
            attr: 存放线程引用的实例属性名（如 "_ritual_thread"）。
            thread: 待启动的 QThread。

        Returns:
            True 表示已启动；False 表示因重入被忽略。
        """
        old = getattr(self, attr, None)
        if old is not None and old.isRunning():
            return False
        setattr(self, attr, thread)
        # deleteLater 由 Qt 在事件循环中安全执行；随后清空 Python 端引用
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda a=attr: setattr(self, a, None))
        thread.start()
        return True

    # ------------------------------------------------------------------
    # UI 联动
    # ------------------------------------------------------------------
    def _is_prebuilt(self) -> bool:
        """当前源是否为「已构建文件」."""
        return self._seg_source.currentIndex() == 1

    def _on_source_changed(self, index: int) -> None:
        is_github = (index == 0)
        self._version_widget.setVisible(is_github)
        self._prebuilt_widget.setVisible(not is_github)
        self._build_card.setVisible(is_github)
        # 两种源的目标文件默认值不同（artifact ↔ input），切换时重置「已手动改选」
        # 标记，让默认值按当前源重新回填。
        self._target_custom = False
        self._sync_target_default()
        if is_github and not self._versions:
            # 切回 GitHub 源码且尚无版本数据 → 立即拉取一次
            self._start_fetch_releases()

    def _on_ritual_toggled(self, on: bool) -> None:
        self._ritual_detail.setVisible(on)

    # ------------------------------------------------------------------
    # 通用：轻提示（气泡）+ 日志
    # ------------------------------------------------------------------
    @staticmethod
    def _notify(widget: QWidget, message: str, is_error: bool = False) -> None:
        """在控件下方弹出短提示气泡，并写入日志（完整文本进日志）。"""
        text = (message or "").strip()
        if not text:
            return
        if is_error:
            logger.error("便捷构建: %s", text)
        else:
            logger.info("便捷构建: %s", text)
        shown = text if len(text) <= 140 else text[:137] + "..."
        try:
            show_transient_card(shown, widget.mapToGlobal(QPoint(0, widget.height())),
                                theme="dark", hide_ms=2500)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # D3D11：版本列表拉取
    # ------------------------------------------------------------------
    def _start_fetch_releases(self) -> None:
        """拉取近 7 个 release 版本号；拉取中末项为不可点击的「正在拉取」。"""
        if self._fetch_thread is not None and self._fetch_thread.isRunning():
            return
        self._render_version_combo(self._versions, _TEXT_FETCHING, False)
        thread = alchemy.FetchReleasesThread(self._fetch_sig)
        thread.result.connect(self._on_releases_ready)
        self._start_thread("_fetch_thread", thread)

    def _render_version_combo(self, versions: list, action_text: str,
                              action_enabled: bool, select: Optional[str] = None) -> None:
        """重绘版本下拉框：版本号 + 末项动作项。"""
        combo = self._combo_version
        combo.blockSignals(True)
        combo.clear()
        for v in versions:
            combo.addItem(v)
        combo.add_action_item(action_text, action_enabled)
        if select and select in versions:
            combo.setCurrentIndex(versions.index(select))
        elif versions:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(combo.count() - 1)   # 仅剩动作项时选中它
        combo.blockSignals(False)

    def _on_version_activated(self, index: int) -> None:
        """末项 = 「重新拉取」→ 重新拉取版本列表。"""
        if self._combo_version.is_action_index(index):
            self._start_fetch_releases()

    def _on_releases_ready(self, entries: list) -> None:
        if not entries:
            # 失败：首项显示「拉取失败」，末项仍为可点击的「重新拉取」
            self._versions = []
            self._releases = []
            self._render_version_combo([], _TEXT_REFETCH, True)
            combo = self._combo_version
            combo.blockSignals(True)
            combo.insertItem(0, _TEXT_FETCH_FAIL)
            combo.set_item_enabled(0, False)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._notify(self._combo_version, "版本列表拉取失败，可点「重新拉取」重试",
                         is_error=True)
            return

        self._releases = list(entries)
        self._versions = [e.get("version", "") for e in self._releases]
        # 成功：默认选中最新版本（列表已按新→旧排序）
        self._render_version_combo(self._versions, _TEXT_REFETCH, True)

    def _current_release(self) -> Optional[dict]:
        """当前选中的 release 条目（选中末项或列表为空时返回 None）。"""
        combo = self._combo_version
        idx = combo.currentIndex()
        if idx < 0 or combo.is_action_index(idx):
            return None
        if 0 <= idx < len(self._releases):
            return self._releases[idx]
        return None

    # ------------------------------------------------------------------
    # D3D11：下载源码
    # ------------------------------------------------------------------
    def _on_download_clicked(self) -> None:
        release = self._current_release()
        if release is None:
            self._notify(self._btn_download, "请先选择要下载的版本", is_error=True)
            return
        if self._dl_thread is not None and self._dl_thread.isRunning():
            return

        self._btn_download.set_busy(True)
        self._btn_download.set_status("准备下载…")

        thread = alchemy.DownloadProjectThread(self._dl_sig, release)
        self._start_thread("_dl_thread", thread)

    def _on_download_cancelled(self) -> None:
        if self._dl_thread is not None:
            self._dl_thread.request_cancel()

    def _on_download_finished(self, ok: bool, msg: str) -> None:
        self._btn_download.set_busy(False)
        self._btn_download.set_status("")
        self._notify(self._btn_download, msg, is_error=not ok)
        if ok:
            # 新版本已落盘 → 刷新「项目」下拉框，并针对新源码重跑环境检测
            self._refresh_projects()
            self._refresh_env()

    # ------------------------------------------------------------------
    # D3D11：下载已构建文件（蓝奏云 → cloud/）
    # ------------------------------------------------------------------
    def _on_prebuilt_clicked(self) -> None:
        """拉取蓝奏云分享里的已构建文件包并解压到 cloud/。"""
        if self._pdl_thread is not None and self._pdl_thread.isRunning():
            return

        self._btn_prebuilt.set_busy(True)
        self._btn_prebuilt.set_status("准备下载…")

        thread = alchemy.DownloadPrebuiltThread(self._pdl_sig)
        self._start_thread("_pdl_thread", thread)

    def _on_prebuilt_cancelled(self) -> None:
        if self._pdl_thread is not None:
            self._pdl_thread.request_cancel()

    def _on_prebuilt_finished(self, ok: bool, msg: str) -> None:
        self._btn_prebuilt.set_busy(False)
        self._btn_prebuilt.set_status("")
        self._notify(self._btn_prebuilt, msg, is_error=not ok)
        if ok:
            # 已构建文件已就绪 → 回填「目标文件」默认值 input/d3d11.dll
            self._sync_target_default()

    # ------------------------------------------------------------------
    # 构建：Builder 检测 / 项目列表 / 构建
    # ------------------------------------------------------------------
    def _refresh_env(self) -> None:
        """后台重跑一次构建环境检测。

        检测要起 vswhere 等子进程（实测 ~120ms），放在 UI 线程会让窗口卡顿，
        因此统一走 ``EnvCheckThread``；期间状态标签显示「检测中…」。
        依赖「项目」下拉框的当前选中项（源码根目录由它决定）。
        """
        if self._env_thread is not None and self._env_thread.isRunning():
            return  # 防重入：上一次还没跑完就不重复起线程
        self._lbl_env.set_state(_TEXT_ENV_PENDING, _ENV_COLOR_PENDING)
        self._lbl_env.setToolTip(_TIP_ENV_PENDING)

        thread = alchemy.EnvCheckThread(
            self._env_sig,
            version=self._current_project_version() or "",
            optimize=self._switch_optimize.on,
        )
        thread.report.connect(self._on_env_ready)
        self._start_thread("_env_thread", thread)

    def _on_env_ready(self, report) -> None:
        """渲染检测结果：状态词 + 悬浮清单。"""
        self._env_report = report
        if report is None:  # 理论上不会发生（run_report 兜住了异常），防御性分支
            self._lbl_env.set_state(_TEXT_ENV_NOT_READY, _ENV_COLOR_NOT_READY)
            self._lbl_env.setToolTip("构建环境：检测未完成，点击可重新检测")
            return

        if report.ready:
            self._lbl_env.set_state(_TEXT_ENV_READY, _ENV_COLOR_READY)
        else:
            self._lbl_env.set_state(_TEXT_ENV_NOT_READY, _ENV_COLOR_NOT_READY)
        # 悬浮清单：每项一行（符号 + 标题 + 结论），MISS/WARN 再缩进一行建议
        self._lbl_env.setToolTip(report.to_tip_text())

        if report.error:
            logger.warning("便捷构建: 环境检测异常 %s", report.error)
        elif report.blockers:
            logger.info("便捷构建: 环境未就绪，%d 项待处理（%s）",
                        len(report.blockers), report.blockers[0].title)

    def _on_redetect_env(self) -> None:
        """「重新检测」按钮 / 点击状态标签：重新跑一次检测（异步，结果写回标签）。"""
        self._refresh_env()

    def _env_block_reason(self) -> str:
        """未就绪时给构建按钮用的一句话原因（取第一条阻塞项 + 建议动作）。"""
        report = self._env_report
        if report is None:
            return "构建环境仍在检测中，请稍候"
        if not report.blockers:
            return report.error or "构建环境未就绪"
        item = report.blockers[0]
        parts = [item.title]
        if item.detail:
            parts.append(f"：{item.detail}")
        if item.hint:
            parts.append(f"。{item.hint}")
        if item.fix and item.fix.label:
            parts.append(f"（{item.fix.label}）")
        return "".join(parts)

    def _refresh_projects(self, select: Optional[str] = None) -> None:
        """扫描 project/ 目录，重建「项目」下拉框（末项 = 更新选项）。"""
        versions = alchemy.list_project_versions()
        self._project_versions = versions
        combo = self._combo_project
        combo.blockSignals(True)
        combo.clear()
        if versions:
            for v in versions:
                combo.addItem(v)
            combo.add_action_item(_TEXT_REFRESH_OPTS, True)
            if select and select in versions:
                combo.setCurrentIndex(versions.index(select))
            else:
                combo.setCurrentIndex(0)
        else:
            combo.addItem(_TEXT_NO_PROJECT)
            combo.set_item_enabled(0, False)
            combo.add_action_item(_TEXT_REFRESH_OPTS, True)
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_project_activated(self, index: int) -> None:
        """末项 = 「更新选项」→ 重新扫描 project/ 目录；否则视为切换了项目。

        切换项目会改变源码根目录（进而改变工具集 / SDK / 静态库等需求），
        因此这里必须重跑一次环境检测。
        """
        if self._combo_project.is_action_index(index):
            self._refresh_projects(select=self._current_project_version())
        self._refresh_env()

    def _current_project_version(self) -> Optional[str]:
        combo = self._combo_project
        idx = combo.currentIndex()
        if idx < 0 or combo.is_action_index(idx):
            return None
        if 0 <= idx < len(self._project_versions):
            return self._project_versions[idx]
        return None

    def _on_build_clicked(self) -> None:
        version = self._current_project_version()
        if not version:
            self._notify(self._btn_build, "请先在 project 目录准备源码（下载）并选中项目",
                         is_error=True)
            return
        # 环境门禁：有阻塞项时直接拦下并给出第一条建议，而不是让用户看一屏
        # MSBuild 原始输出（MSB8020 / MSB8036 / LNK1104 之类）。
        if self._env_report is None or not self._env_report.ready:
            self._notify(self._btn_build, self._env_block_reason(), is_error=True)
            return
        if self._build_thread is not None and self._build_thread.isRunning():
            return

        self._btn_build.set_busy(True)
        self._btn_build.set_status("准备构建…")

        thread = alchemy.BuildThread(
            self._build_sig, version, optimize=self._switch_optimize.on
        )
        self._start_thread("_build_thread", thread)

    def _on_build_finished(self, ok: bool, msg: str) -> None:
        # 无论成功 / 失败都恢复构建按钮
        self._btn_build.set_busy(False)
        self._btn_build.set_status("")
        self._notify(self._btn_build, msg, is_error=not ok)
        if not ok:
            return

        # 构建产物已落入 artifact/ → 刷新仪式目标文件默认值
        self._sync_target_default()

        # 「构建完成后执行神秘仪式」：开关开启且仪式本身也开启 → 自动接仪式；
        # 仪式开关关闭则直接跳过。
        if self._switch_auto_ritual.on and self._switch_ritual.on:
            self._start_ritual(auto=True)

    # ------------------------------------------------------------------
    # 文件优化（神秘仪式）
    # ------------------------------------------------------------------
    def _sync_target_default(self) -> None:
        """回填「目标文件」默认值：源不同，默认值也不同。

        · GitHub 源码   → artifact/d3d11.dll（需先构建出产物才回填）
        · 已构建文件    → cloud/ 下的最新批次目录（点「执行」时直接在里面随机抽
          一个 d3d11 执行，**不再复制到 input**）。默认值刻意不指到 input/，
          以免命中「目标指向 input 就直跑该文件」的分支。

        用户一旦通过「浏览」改选过（``_target_custom``），后续构建完成也不再覆盖，
        避免打断手动选择（切换「源」会重置该标记，见 :meth:`_on_source_changed`）。
        """
        if self._target_custom:
            return
        if self._is_prebuilt():
            # 只是个“来源指示”，真正的 dll 在仪式开始时随机抽；不指向 input/。
            batches = alchemy.list_cloud_batches()
            self._edit_target.setText(
                os.path.join(alchemy.CLOUD_DIR, batches[0]) if batches
                else alchemy.CLOUD_DIR
            )
            return
        if os.path.isfile(alchemy.ARTIFACT_DLL):
            self._edit_target.setText(alchemy.ARTIFACT_DLL)

    def _on_browse_target(self) -> None:
        start_dir = alchemy.INPUT_DIR if self._is_prebuilt() else alchemy.ARTIFACT_DIR
        path, _ = QFileDialog.getOpenFileName(
            self, "选择目标文件", start_dir, "DLL 文件 (*.dll);;所有文件 (*.*)"
        )
        if not path:
            return
        self._edit_target.setText(path)
        self._target_custom = True

    def _ritual_kind(self) -> str:
        mapping = (
            alchemy.RITUAL_PACK,        # 包装
            alchemy.RITUAL_PAINT_PY,    # 涂装(py)
            alchemy.RITUAL_PAINT_RS,    # 涂装(rs)
        )
        idx = self._seg_ritual.currentIndex()
        return mapping[idx] if 0 <= idx < len(mapping) else alchemy.RITUAL_PACK

    def _start_ritual(self, auto: bool = False) -> None:
        if not self._switch_ritual.on:
            return

        target = self._edit_target.text().strip()
        prepare = None

        # 1) 目标路径位于 input/ → 不论源是什么模式，都**只**对该文件执行仪式，
        #    不随机、不复制（“路径选择为 input 时”的硬规则）。
        if (target and os.path.dirname(os.path.abspath(target))
                == os.path.abspath(alchemy.INPUT_DIR)):
            if not os.path.isfile(target):
                self._notify(self._btn_ritual, f"目标文件不存在: {target}",
                             is_error=True)
                return
            # prepare 保持 None，直接用 target

        # 2) 用户显式改选过某个真实文件（包括 cloud 里某个具体 dll）→ 直接对该文件执行，
        #    不随机。
        elif self._target_custom and target and os.path.isfile(target):
            pass  # 直接用 target

        # 3) 已构建文件且未改选（默认指向 cloud 批次）→ 直接在 cloud 最新批次里随机
        #    抽一个 d3d11.dll 执行，**不再落 input**（产物进 output/）。
        elif self._is_prebuilt():
            prepare = alchemy.pick_random_prebuilt_dll
            target = ""  # 实际文件由 prepare 在运行时随机决定

        # 4) GitHub 源码 → 直接对 artifact / 用户自定义目标执行（文件必须存在）。
        else:
            if not target or not os.path.isfile(target):
                self._notify(self._btn_ritual, "目标文件不存在，请先构建或选择文件",
                             is_error=True)
                return

        if self._ritual_thread is not None and self._ritual_thread.isRunning():
            return

        self._btn_ritual.set_busy(True)
        self._btn_ritual.set_status("正在执行仪式…")

        thread = alchemy.RitualThread(self._ritual_sig, self._ritual_kind(),
                                      target, prepare=prepare)
        self._start_thread("_ritual_thread", thread)
        if auto:
            logger.info("便捷构建: 构建完成，自动执行神秘仪式")

    def _on_ritual_clicked(self) -> None:
        self._start_ritual()

    def _on_ritual_finished(self, ok: bool, msg: str) -> None:
        self._btn_ritual.set_busy(False)
        self._btn_ritual.set_status("")
        self._notify(self._btn_ritual, msg, is_error=not ok)

    # ------------------------------------------------------------------
    # 自动化：复制到 GIMI
    # ------------------------------------------------------------------
    def _gimi_dir(self) -> str:
        if self._config is None:
            return ""
        try:
            return self._config.get_gimi_dir() or ""
        except Exception:
            return ""

    def _on_copy_clicked(self) -> None:
        source = (alchemy.COPY_ARTIFACT if self._seg_copy.currentIndex() == 0
                  else alchemy.COPY_OUTPUT)
        gimi_dir = self._gimi_dir()
        if not gimi_dir:
            self._notify(self._btn_copy, "未配置 GIMI 目录，请先在设置中指定",
                         is_error=True)
            return
        if self._copy_thread is not None and self._copy_thread.isRunning():
            return

        thread = alchemy.CopyGimiThread(self._copy_sig, source, gimi_dir)
        self._start_thread("_copy_thread", thread)

    def _on_copy_finished(self, ok: bool, msg: str) -> None:
        self._notify(self._btn_copy, msg, is_error=not ok)

    # ------------------------------------------------------------------
    # 关闭：尽量中断仍在跑的可取消任务
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 分页切换（主分页 = 构建；清理分页）
    # ------------------------------------------------------------------
    def _show_page(self, page: str) -> None:
        """切换主分页 / 清理分页，并同步标题栏与底部操作条。

        两页均作为内容容器的常驻子控件，靠可见性切换（setWidget 会销毁旧控件，
        故不能反复 setWidget）。主分页：标题「便捷构建」，显示清理入口 + 分隔线 +
        关闭按钮，隐藏底栏；清理分页：标题「清理」，隐藏清理入口与分隔线，关闭按钮
        变为返回按钮，显示底栏。
        """
        self._current_page = page
        if page == "main":
            self._title_label.setText("便捷构建")
            self._main_page.setVisible(True)
            self._cleanup_page.setVisible(False)
            self._btn_clean.setVisible(True)
            self._title_sep.setVisible(True)
            self._btn_back.setVisible(False)
            self._btn_close.setVisible(True)
            self._bottom_bar.setVisible(False)
        else:
            self._title_label.setText("清理")
            self._refresh_cleanup_page()
            self._main_page.setVisible(False)
            self._cleanup_page.setVisible(True)
            self._btn_clean.setVisible(False)
            self._title_sep.setVisible(False)
            self._btn_back.setVisible(True)
            self._btn_close.setVisible(False)
            self._bottom_bar.setVisible(True)

    # ------------------------------------------------------------------
    # 底部操作条（仅清理分页）
    # ------------------------------------------------------------------
    def _build_bottom_bar(self, parent_layout: QVBoxLayout) -> None:
        bar = QFrame(self._frame)
        bar.setObjectName("CleanBottomBar")
        bar.setFixedHeight(50)

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)

        self._lbl_summary = QLabel("", bar)
        self._lbl_summary.setObjectName("CleanSummary")
        lay.addWidget(self._lbl_summary)
        lay.addStretch(1)

        self._btn_select_all = QPushButton("全选", bar)
        self._btn_select_all.setObjectName("BrowseBtn")
        self._btn_select_all.setCursor(Qt.PointingHandCursor)
        self._btn_select_all.clicked.connect(self._on_select_all_clicked)
        lay.addWidget(self._btn_select_all)

        self._btn_clean_action = TaskButton("清理选中", "PrimaryBtn", cancellable=False)
        self._btn_clean_action.activated.connect(self._on_clean_clicked)
        lay.addWidget(self._btn_clean_action)

        bar.setVisible(False)
        parent_layout.addWidget(bar)
        self._bottom_bar = bar

    # ------------------------------------------------------------------
    # 清理：扫描 / 渲染 / 选择
    # ------------------------------------------------------------------
    def _scan_cleanup_items(self) -> "dict":
        """扫描 alchemy 各子目录，返回 {key: (标题, 提示, [(名称, 路径, 字节数), ...])}。"""
        groups: dict = {}

        proj = []
        for v in alchemy.list_project_versions():
            p = alchemy.project_dir_for(v)
            if os.path.isdir(p):
                proj.append((v, p, _dir_size(p)))
        groups["project"] = ("源码工程", "删除需重新下载", proj)

        cloud = []
        for b in alchemy.list_cloud_batches():
            p = os.path.join(alchemy.CLOUD_DIR, b)
            if os.path.isdir(p):
                cloud.append((b, p, _dir_size(p)))
        groups["cloud"] = ("已构建文件", "删除需重新下载", cloud)

        art = []
        if os.path.isfile(alchemy.ARTIFACT_DLL):
            art.append(("d3d11.dll", alchemy.ARTIFACT_DLL,
                        os.path.getsize(alchemy.ARTIFACT_DLL)))
        groups["artifact"] = ("构建产物", "可重建", art)

        out = []
        if os.path.isfile(alchemy.OUTPUT_DLL):
            out.append(("d3d11.dll", alchemy.OUTPUT_DLL,
                        os.path.getsize(alchemy.OUTPUT_DLL)))
        groups["output"] = ("仪式产物", "可重建", out)

        inp = []
        if os.path.isdir(alchemy.INPUT_DIR):
            for f in sorted(os.listdir(alchemy.INPUT_DIR)):
                if f.startswith("."):
                    continue
                fp = os.path.join(alchemy.INPUT_DIR, f)
                if os.path.isfile(fp):
                    inp.append((f, fp, os.path.getsize(fp)))
        groups["input"] = ("输入暂存", "手动放置", inp)

        temp = []
        if os.path.isdir(alchemy.ALCHEMY_DIR):
            for f in sorted(os.listdir(alchemy.ALCHEMY_DIR)):
                if f.startswith(".stage_"):
                    p = os.path.join(alchemy.ALCHEMY_DIR, f)
                    if os.path.isdir(p):
                        temp.append((f, p, _dir_size(p)))
        groups["temp"] = ("临时文件", "始终安全", temp)
        return groups

    @staticmethod
    def _clear_layout(layout) -> None:
        """递归清空布局并销毁其中的控件。"""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    QuickBuildWindow._clear_layout(sub)

    def _refresh_cleanup_page(self) -> None:
        """重建清理分页：按目录分组，组内逐项勾选 + 全选按钮 + 大小。"""
        layout = self._cleanup_layout
        self._clear_layout(layout)
        self._group_select_btns: dict = {}
        self._group_paths: dict = {}
        self._cleanup_rows_by_path: dict = {}
        self._path_category = {}
        self._path_size = {}

        data = self._scan_cleanup_items()

        # 裁剪失效选择（已不存在的路径）
        current_paths = set()
        for _title, _hint, items in data.values():
            for _name, path, size in items:
                current_paths.add(path)
                self._path_size[path] = size
        for k in list(self._selected.keys()):
            if k not in current_paths:
                del self._selected[k]

        any_items = False
        for key, (title, hint, items) in data.items():
            if not items:
                continue
            any_items = True
            paths = [it[1] for it in items]
            self._group_paths[key] = paths
            self._path_category.update({p: title for p in paths})

            group = QWidget(self._cleanup_page)
            glay = QVBoxLayout(group)
            glay.setContentsMargins(0, 0,0, 0)
            glay.setSpacing(6)

            head = QHBoxLayout()
            head.setSpacing(8)
            accent = ACCENT if key != "temp" else "#FF9F0A"
            bar = QLabel(group)
            bar.setFixedSize(4, 16)
            bar.setStyleSheet(f"background:{accent};border:none;border-radius:2px;")
            head.addWidget(bar)
            t = QLabel(title, group)
            t.setObjectName("CardTitle")
            head.addWidget(t)
            total = sum(it[2] for it in items)
            info = QLabel(f"{len(items)} 项 · {_fmt_size(total)}（{hint}）", group)
            info.setObjectName("TitleHint")
            head.addWidget(info)
            head.addStretch(1)
            btn_all = QPushButton("全选", group)
            btn_all.setObjectName("BrowseBtn")
            btn_all.setCursor(Qt.PointingHandCursor)
            btn_all.clicked.connect(lambda _c=False, k=key: self._on_group_select_all(k))
            head.addWidget(btn_all)
            self._group_select_btns[key] = btn_all
            glay.addLayout(head)

            rows = []
            for name, path, size in items:
                row = _CleanupRow(name, path, size, self._on_item_toggled, group)
                row.sync(self._selected.get(path, False))
                glay.addWidget(row)
                rows.append(row)
            self._cleanup_rows_by_path[key] = rows
            layout.addWidget(group)

        if not any_items:
            empty = QLabel("没有可清理的文件", self._cleanup_page)
            empty.setObjectName("TitleHint")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)

        layout.addStretch(1)
        self._refresh_summary()
        self._refresh_group_select_labels()

    def _on_item_toggled(self, path: str, checked: bool) -> None:
        self._selected[path] = checked
        self._refresh_summary()
        self._refresh_group_select_labels()

    def _on_group_select_all(self, key: str) -> None:
        paths = self._group_paths.get(key, [])
        all_sel = bool(paths) and all(self._selected.get(p, False) for p in paths)
        new_state = not all_sel
        for p in paths:
            self._selected[p] = new_state
        for row in self._cleanup_rows_by_path.get(key, []):
            row.sync(new_state)
        self._refresh_summary()
        self._refresh_group_select_labels()

    def _on_select_all_clicked(self) -> None:
        all_paths = [p for paths in self._group_paths.values() for p in paths]
        all_sel = bool(all_paths) and all(self._selected.get(p, False) for p in all_paths)
        new_state = not all_sel
        for p in all_paths:
            self._selected[p] = new_state
        for rows in self._cleanup_rows_by_path.values():
            for row in rows:
                row.sync(new_state)
        self._refresh_summary()
        self._refresh_group_select_labels()

    def _refresh_group_select_labels(self) -> None:
        for key, btn in self._group_select_btns.items():
            paths = self._group_paths.get(key, [])
            all_sel = bool(paths) and all(self._selected.get(p, False) for p in paths)
            btn.setText("取消" if all_sel else "全选")
        all_paths = [p for paths in self._group_paths.values() for p in paths]
        all_sel = bool(all_paths) and all(self._selected.get(p, False) for p in all_paths)
        self._btn_select_all.setText("取消" if all_sel else "全选")

    def _refresh_summary(self) -> None:
        n = 0
        total = 0
        for p, v in self._selected.items():
            if v and p in self._path_size:
                n += 1
                total += self._path_size[p]
        self._lbl_summary.setText(
            f"已选 {n} 项 · 将释放 <font color=\"{ACCENT}\">{_fmt_size(total)}</font>"
        )
        self._btn_clean_action.setEnabled(n > 0)

    # ------------------------------------------------------------------
    # 清理：执行
    # ------------------------------------------------------------------
    def _on_clean_clicked(self) -> None:
        selected = [p for p, v in self._selected.items() if v]
        if not selected:
            return
        dlg = _ConfirmCleanupDialog(self, self._selected, self._path_size,
                                   self._path_category)
        if dlg.exec() != QDialog.Accepted:
            return

        self._btn_clean_action.set_busy(True)
        self._btn_clean_action.set_status("正在清理…")
        thread = _CleanupThread(selected)
        thread.finished.connect(self._on_cleanup_finished)
        self._start_thread("_clean_thread", thread)

    def _on_cleanup_finished(self, ok: bool, msg: str) -> None:
        self._btn_clean_action.set_busy(False)
        self._btn_clean_action.set_status("")
        self._notify(self._btn_clean_action, msg, is_error=not ok)
        if ok:
            self._selected.clear()
            self._refresh_cleanup_page()

    def _running_threads(self) -> list:
        """当前仍在运行的任务线程列表。"""
        threads = (self._fetch_thread, self._dl_thread, self._pdl_thread,
                   self._build_thread, self._ritual_thread, self._copy_thread,
                   self._env_thread, self._clean_thread)
        return [t for t in threads if t is not None and t.isRunning()]

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """关闭前先停止可取消的线程，并等待不可中断的任务收尾。

        直接销毁窗口会让仍在运行的 QThread 随窗口一同析构，Qt 会 abort
        （"Destroyed while thread is still running"，表现为闪退）。处理顺序：
          1. 可取消的线程（拉取 / 下载 / 复制）先 request_cancel；
          2. 限时等待它们退出（:data:`_CLOSE_WAIT_SEC`）；
          3. 若仍有不可中断的任务（构建 / 仪式）在跑，则先隐藏窗口并忽略
             本次关闭，等它们结束后由 :meth:`_close_when_idle` 真正关闭。
        """
        running = self._running_threads()
        if not running:
            super().closeEvent(event)
            return

        for t in running:
            try:
                t.request_cancel()
            except Exception:
                pass

        deadline = time.time() + _CLOSE_WAIT_SEC
        for t in running:
            remaining = int(max(0.0, deadline - time.time()) * 1000)
            if remaining > 0:
                t.wait(remaining)

        still = self._running_threads()
        if still:
            event.ignore()
            self.hide()
            logger.info("便捷构建: 仍有 %d 个任务在运行，窗口已隐藏，结束后自动关闭",
                        len(still))
            for t in still:
                t.finished.connect(self._close_when_idle)
            return
        super().closeEvent(event)

    def _close_when_idle(self) -> None:
        """所有后台任务结束后真正关闭窗口（延迟关闭的收尾）。"""
        if not self._running_threads():
            self.close()

    # ------------------------------------------------------------------
    # 拖拽
    # ------------------------------------------------------------------
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
            if self._current_page == "cleanup":
                self._show_page("main")
            else:
                self.close()
        super().keyPressEvent(event)


class _CleanupThread(QThread):
    """清理线程：删除选中的文件 / 目录（rmtree 忽略错误），进度回传 UI。"""

    progress = Signal(float)
    finished = Signal(bool, str)

    def __init__(self, paths: list) -> None:
        super().__init__()
        self._paths = paths

    def run(self) -> None:  # type: ignore[override]
        try:
            total = len(self._paths)
            done = 0
            for p in self._paths:
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    elif os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
                done += 1
                if total:
                    self.progress.emit(100.0 * done / total)
            self.finished.emit(True, f"已清理 {done} 项")
        except Exception as e:  # 兜底：任何异常都转为失败信号，绝不逃出线程
            logger.error("清理失败: %s", e, exc_info=True)
            self.finished.emit(False, f"清理失败: {e}")


class _ConfirmCleanupDialog(QDialog):
    """清理确认框：按类别列出将被删除的文件与体积，确认后才执行。"""

    def __init__(self, parent, selected: dict, path_size: dict,
                 path_category: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认清理")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint
                           | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(360)

        frame = QFrame(self)
        frame.setObjectName("ConfirmFrame")
        frame.setStyleSheet(
            f"QFrame#ConfirmFrame {{ background: {BG_WINDOW};"
            f" border: 1px solid {BORDER}; border-radius: {RADIUS_WIN}px; }}"
        )
        flay = QVBoxLayout(frame)
        flay.setContentsMargins(16, 14, 16, 14)
        flay.setSpacing(10)

        title = QLabel("确认清理", frame)
        title.setStyleSheet(
            f"color:{TEXT_PRIMARY}; font-size:14px; font-weight:600;"
            f" background:transparent;")
        flay.addWidget(title)

        warn = QLabel("以下文件将被永久删除，操作不可撤销：", frame)
        warn.setStyleSheet(
            f"color:{TEXT_SECONDARY}; font-size:12px; background:transparent;")
        flay.addWidget(warn)

        # 按类别分组统计
        cats: dict = {}
        total = 0
        for p, sel in selected.items():
            if not sel:
                continue
            cat = path_category.get(p, "其它")
            cats.setdefault(cat, []).append(
                (os.path.basename(p) or p, path_size.get(p, 0)))
        scroll = QScrollArea(frame)
        scroll.setObjectName("ContentArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(240)
        inner = QWidget(scroll)
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(6)
        for cat, items in cats.items():
            chead = QLabel(cat, inner)
            chead.setStyleSheet(
                f"color:{ACCENT}; font-size:12px; font-weight:600;"
                f" background:transparent;")
            ilay.addWidget(chead)
            csum = 0
            for nm, sz in items:
                csum += sz
                line = QLabel(f"  {nm}  ·  {_fmt_size(sz)}", inner)
                line.setStyleSheet(
                    f"color:{TEXT_PRIMARY}; font-size:12px; background:transparent;")
                ilay.addWidget(line)
            subt = QLabel(f"  {len(items)} 项 · {_fmt_size(csum)}", inner)
            subt.setStyleSheet(
                f"color:{TEXT_SECONDARY}; font-size:11px; background:transparent;")
            ilay.addWidget(subt)
            total += csum
        ilay.addStretch(1)
        scroll.setWidget(inner)
        flay.addWidget(scroll)

        total_lbl = QLabel(
            f"合计：{sum(1 for v in selected.values() if v)} 项 · {_fmt_size(total)}",
            frame)
        total_lbl.setStyleSheet(
            f"color:{TEXT_PRIMARY}; font-size:12px; font-weight:600;"
            f" background:transparent;")
        flay.addWidget(total_lbl)

        blay = QHBoxLayout()
        blay.setSpacing(8)
        blay.addStretch(1)
        btn_cancel = QPushButton("取消", frame)
        btn_cancel.setObjectName("BrowseBtn")
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        blay.addWidget(btn_cancel)
        btn_ok = QPushButton("清理", frame)
        btn_ok.setObjectName("PrimaryBtn")
        btn_ok.setCursor(Qt.PointingHandCursor)
        btn_ok.clicked.connect(self.accept)
        blay.addWidget(btn_ok)
        flay.addLayout(blay)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(frame)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(SETTINGS_QSS)
    win = QuickBuildWindow()
    win.show()
    sys.exit(app.exec())
