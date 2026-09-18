"""
omg.pages.guide.game_folder — 路径页「游戏」项的悬浮指引卡。

用悬浮卡片（``omg.ui.OMGPopCard``）承载一张「动图 + 文字」的竖向卡片：

    动图区：``resources/media/guide-game-folder.gif``（等比缩放、随卡片显隐播放 / 暂停）
    文字区：如何定位游戏文件夹的说明 + 可直接选中复制的示例路径

采用 Popover 用法（``OMGPopCard(trigger=...)``）：hover-intent 自动显隐，且热区
**包含卡片自身**，鼠标移入卡片不会收起 —— 因此可以直接在卡内拖动选中并复制示例
路径（QLabel 的 ``TextSelectableByMouse`` + 原生 Ctrl+C）。

用法（PathPage 内，需持有返回对象防止被回收）::

    from omg.pages.guide.game_folder import attach_game_folder_guide
    self._game_guide = attach_game_folder_guide(lbl_game)
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QImageReader, QMovie
from PySide6.QtWidgets import QLabel, QWidget

from omg.core.paths import RESOURCES_DIR
from omg.ui.OMGPopCard import OMGPopCard, PAD_X

#: 动图文件名（位于 resources/media/ 下）
GIF_NAME = "guide-game-folder.gif"

#: 动图区最大宽度（原图 718x498，按此宽度等比缩放，避免卡片过宽撑出屏幕）
GIF_MAX_W = 340

#: 说明文字；示例路径可直接选中复制
GUIDE_TEXT = (
    "参照动图可在米哈游启动器可快速找到，不需要精确到 .exe。\n"
    "示例：\n"
    "C:/path/to/Genshin Impact/Genshin Impact Game"
)


def gif_path() -> str:
    """返回动图的绝对磁盘路径（打包后指向 ``bin/resources/media/``）。"""
    return os.path.join(RESOURCES_DIR, "media", GIF_NAME)


def _scaled_gif_size(path: str, max_w: int) -> tuple:
    """按 ``max_w`` 等比缩放，返回 ``(w, h)``；读不到尺寸时返回 ``(0, 0)``。"""
    try:
        size = QImageReader(path).size()
    except Exception:
        return 0, 0
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        return 0, 0
    w = min(size.width(), max_w)
    h = max(1, int(round(size.height() * w / size.width())))
    return w, h


class GameFolderGuide(QObject):
    """「游戏」项的悬浮指引卡控制器（持有 QMovie，随卡片显隐播放 / 暂停）。"""

    def __init__(self, trigger: QWidget, theme: str = "dark") -> None:
        super().__init__()
        self.card = OMGPopCard(trigger=trigger, theme=theme)

        # ---- 动图区 ----
        path = gif_path()
        self._movie = QMovie(path)          # QLabel 不接管所有权，必须自持引用
        w, h = _scaled_gif_size(path, GIF_MAX_W)
        self._has_gif = bool(w and h and os.path.isfile(path))
        if self._has_gif:
            self._movie.setScaledSize(QSize(w, h))
            self._gif = QLabel()
            self._gif.setMovie(self._movie)
            self._gif.setFixedSize(w, h)
            self._gif.setAlignment(Qt.AlignCenter)
            self._gif.setStyleSheet("background: transparent;")
            self.card.add_widget(self._gif)
        else:
            self._gif = None

        # 内容区宽度 == 动图显示宽：动图固定宽已把 body 撑到 w，文字再按 w
        # 限宽换行，避免较长文本反过来决定卡片宽度（左右 padding 由 body 的
        # PAD_X 提供，两侧等距）。
        self.content_width = w if self._has_gif else 0

        # ---- 文字区 ----
        self.card.add_text(GUIDE_TEXT, selectable=True,
                           max_w=self.content_width or None)

        # 随卡片显隐播放 / 暂停（隐藏后继续解码动画帧纯属浪费 CPU）
        self.card.installEventFilter(self)

    def surface_width(self) -> int:
        """实体卡片（不含阴影留白）的预期宽度 = 内容宽 + 左右等距 padding。"""
        return self.content_width + 2 * PAD_X if self.content_width else 0

    def eventFilter(self, obj, event) -> bool:
        if obj is self.card:
            if event.type() == QEvent.Show:
                self._movie.start()
            elif event.type() == QEvent.Hide:
                self._movie.stop()
        return super().eventFilter(obj, event)


def attach_game_folder_guide(trigger: QWidget,
                             theme: str = "dark") -> GameFolderGuide:
    """给「游戏」控件挂上悬浮指引卡，返回控制器（调用方需持有引用防回收）。"""
    return GameFolderGuide(trigger, theme=theme)
