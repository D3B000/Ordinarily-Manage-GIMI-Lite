"""可复用的弹出窗口几何记忆：首次打开居中，之后按上次关闭位置弹出。

设计目标
--------
OMGLite 的「设置」「便捷构建」等弹出窗口是带 ``Qt.Window`` 标志的顶层
``QWidget``，固定尺寸、单例复用（创建一次后反复 ``show()`` / ``hide()``）。
需求：*首次打开出现在屏幕中心，后续按最后关闭的位置弹出*。

由于窗口对象在单次会话内被复用，Qt 本身会保留其几何；本模块真正要解决的是
**跨进程 / 跨会话**的记忆，以及「首次居中」的初始行为。

用法（任意未来的顶层弹出窗口只要照抄即可）::

    from omg.ui.window_geo import CenteredPopupMixin

    class MyPopup(CenteredPopupMixin, QWidget):
        def __init__(self, parent=None, config=None):
            super().__init__(parent, Qt.Window)
            self.setFixedSize(W, H)
            # 在 setFixedSize 之后、show 之前注册记忆
            self.init_geometry_persist("my_popup", config)

    # 之后无需任何额外代码：CenteredPopupMixin 自动接管
    #   - showEvent：本实例首次展示时，有记忆则恢复，没有则居中；
    #   - hideEvent：窗口隐藏（含关闭）时把当前位置写入 config。

记忆存储于 ``ConfigManager`` 的 ``window_geometry`` 键（dict: key -> [x, y]），
已加入 ``PERSISTENT_KEYS``，随 config.json 一并落盘。``config`` 为 None（如
独立调试入口）时静默跳过，不影响窗口正常显示。
"""

from PySide6.QtWidgets import QApplication

#: config.json 中的几何记忆键
GEO_KEY = "window_geometry"


class CenteredPopupMixin:
    """为顶层弹出窗口提供「首次居中、之后按上次位置弹出」能力。

    仅依赖两个实例属性（``_geo_key`` / ``_geo_config``），不要求窗口额外代码。
    窗口必须：
      * 是顶层窗口（带 ``Qt.Window`` 标志）；
      * 在 ``__init__`` 中调用 :meth:`init_geometry_persist` 注册 key 与 config。
    """

    def init_geometry_persist(self, key: str, config,
                              legacy_keys=()) -> None:
        """注册几何持久化。必须在 ``setFixedSize`` 之后、``show`` 之前调用。

        Args:
            key: 该窗口在记忆表中的唯一标识（如 ``"settings"`` / ``"resources"``）。
            config: ``ConfigManager`` 实例；为 None 时退出记忆（仅居中一次）。
            legacy_keys: 历史键名（可迭代）。窗口改名后用它把老用户已保存的位置
                搬过来，避免「改个名字，所有人上次的位置全丢、被迫重新居中」。
                仅在新键尚无记忆时迁移，迁移后删除旧键。
        """
        self._geo_key = key
        self._geo_config = config
        # 本实例生命周期内只应用一次：窗口是单例复用，后续 show 由 Qt 保留几何，
        # 不必每次都重新居中 / 恢复，否则会覆盖用户当次拖动的结果。
        self._geo_applied = False
        self._migrate_legacy_geometry(key, config, legacy_keys)

    @staticmethod
    def _migrate_legacy_geometry(key: str, config, legacy_keys) -> None:
        """把 legacy_keys 下已保存的位置搬到 key（仅当 key 尚无记忆）。"""
        if config is None or not legacy_keys:
            return
        geo = config.get(GEO_KEY)
        if not isinstance(geo, dict) or geo.get(key) is not None:
            return
        for old in legacy_keys:
            pos = geo.get(old)
            if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
                continue
            geo = dict(geo)
            geo[key] = [pos[0], pos[1]]
            geo.pop(old, None)
            config.set(GEO_KEY, geo)
            try:
                config.save()
            except Exception:
                pass
            return

    # ------------------------------------------------------------------
    # 展示时：首次居中或恢复上次位置
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if getattr(self, "_geo_applied", False):
            return
        self._geo_applied = True
        self._apply_saved_geometry()

    def _apply_saved_geometry(self) -> None:
        pos = self._load_position()
        if pos is not None:
            self.move(pos[0], pos[1])
            return
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        fg = self.frameGeometry()
        screen = QApplication.screenAt(fg.center()) or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = avail.x() + max(0, (avail.width() - fg.width()) // 2)
        y = avail.y() + max(0, (avail.height() - fg.height()) // 2)
        self.move(x, y)

    def _load_position(self):
        cfg = getattr(self, "_geo_config", None)
        if cfg is None:
            return None
        geo = cfg.get(GEO_KEY)
        if not isinstance(geo, dict):
            return None
        pos = geo.get(getattr(self, "_geo_key", ""))
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                x, y = int(pos[0]), int(pos[1])
            except (TypeError, ValueError):
                return None
            # 记忆可能来自已断开的显示器 —— 落点必须在某个屏幕可视区内，
            # 否则窗口会“消失”。越界则回退到居中。
            if self._is_on_screen(x, y):
                return [x, y]
        return None

    @staticmethod
    def _is_on_screen(x: int, y: int) -> bool:
        for screen in QApplication.screens():
            avail = screen.availableGeometry()
            # 允许标题栏 / 拖拽把手略微探出，故给 8px 余量
            if (avail.x() - 8) <= x <= (avail.x() + avail.width() - 8) and \
               (avail.y() - 8) <= y <= (avail.y() + avail.height() - 8):
                return True
        return False

    # ------------------------------------------------------------------
    # 隐藏 / 关闭时：保存当前位置
    # ------------------------------------------------------------------
    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._save_position()

    def _save_position(self) -> None:
        cfg = getattr(self, "_geo_config", None)
        if cfg is None:
            return
        pos = self.pos()
        geo = cfg.get(GEO_KEY)
        geo = dict(geo) if isinstance(geo, dict) else {}
        geo[getattr(self, "_geo_key", "")] = [pos.x(), pos.y()]
        cfg.set(GEO_KEY, geo)
        try:
            cfg.save()
        except Exception:
            # 持久化失败不应影响窗口交互
            pass
