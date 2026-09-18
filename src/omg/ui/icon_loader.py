"""
omg.ui.icon_loader — 统一 SVG 图标加载（Qt 资源优先，磁盘回退）

为什么存在
----------
图标原本按绝对磁盘路径逐个读取 ``resources/icons/...``。在机械盘冷启动时，每个
文件都是一次随机小读（本机实测约 9.3 ms/次），9 个首页图标就接近 80 ms。

现在所有 SVG 编译进了 ``resources/icons/icons.rcc``（由 ``icons.qrc`` 生成），
启动时注册后可用一个 ``QResource`` 读取，把 N 次随机读合并为 1 次顺序读。
磁盘路径作为回退保留：万一 ``.rcc`` 缺失（如 dev 直接跑源码且未生成 rcc），
行为与原先完全一致，不引入回归风险。

资源别名与磁盘相对路径一致，例如：
    :/icons/home/play.svg   ←→  resources/icons/home/play.svg
    :/icons/setting/add.svg ←→  resources/icons/setting/add.svg
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QFile, QIODevice
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import Qt

RESOURCE_PREFIX = ":/icons/"


def read_svg_bytes(name: str) -> bytes | None:
    """按资源别名读取 SVG 原始字节；优先 Qt 资源，回退磁盘。

    ``name`` 形如 ``home/play.svg``、``setting/add.svg``，对应 icons.qrc 中的别名。
    """
    # 1) 优先资源（.rcc 已注册时）
    res = QFile(RESOURCE_PREFIX + name)
    if res.exists() and res.open(QIODevice.ReadOnly):
        try:
            data = res.readAll().data()
            return bytes(data)
        finally:
            res.close()

    # 2) 回退磁盘：resources/icons/<name>
    from omg.core.paths import RESOURCES_DIR
    import os
    disk = os.path.join(RESOURCES_DIR, "icons", name)
    if os.path.isfile(disk):
        try:
            with open(disk, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    return None


def register_icon_resources() -> bool:
    """注册图标资源。

    采用生成的 Python 资源模块 ``omg.ui.icons_rc``（import 即注册，数据内联、
    无外部文件依赖，对 PyInstaller 打包也透明）。若导入失败，加载器会自动回退到
    磁盘逐文件读取，不影响功能。

    注：PySide6 6.10 的 ``QResource.registerResource()`` 对外部 ``.rcc`` 二进制注册
    会返回 False（已知问题），因此不采用 ``.rcc`` 方案。
    """
    try:
        from . import icons_rc  # noqa: F401  (import 的副作用即注册 Qt 资源)
        return True
    except Exception:
        return False


def svg_icon(name: str, px: int, color: str) -> QIcon:
    """加载 SVG 为单色剪影图标（原图渲染后用 SourceIn 合成模式统一着色）。

    用于首页图标：无论 SVG 内部填充色如何，都重着为 ``color``。
    """
    data = read_svg_bytes(name)
    if not data:
        return QIcon()
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return QIcon()
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(p)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(pm.rect(), QColor(color))
    p.end()
    return QIcon(pm)


def tinted_icon(name: str, px: int, color: str) -> QIcon:
    """加载 SVG 并按主题前景色着色（用于无 fill 的功能图标）。

    与 setting.py 原 ``tinted_icon`` 行为一致：注入/覆盖 fill 与 stroke。
    """
    data = read_svg_bytes(name)
    if not data:
        return QIcon()
    try:
        svg = data.decode("utf-8")
    except UnicodeDecodeError:
        svg = data.decode("utf-8", "replace")
    # 注入 / 覆盖 fill，使图标颜色随主题前景色变化
    if 'fill="' not in svg:
        svg = svg.replace("<svg", f'<svg fill="{color}"', 1)
    else:
        svg = svg.replace('fill="black"', f'fill="{color}"')
    # 线稿图标（fill="none" + stroke）同样要按主题前景色着色
    svg = svg.replace('stroke="black"', f'stroke="{color}"')
    svg = svg.replace('stroke="#000000"', f'stroke="{color}"')
    # currentColor 线稿图标（lucide 风格）按主题前景色着色——否则 currentColor
    # 在无显式 color 的控件上回退成黑色，在深色主题下几乎不可见
    svg = svg.replace('stroke="currentColor"', f'stroke="{color}"')
    svg = svg.replace('fill="currentColor"', f'fill="{color}"')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return QIcon()
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(p)
    p.end()
    return QIcon(pm)
