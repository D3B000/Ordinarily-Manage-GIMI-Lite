"""omg.ui.idle_fade — 闲置变淡的 Qt 控制器（计时 / 缓动 / 输入监听）。

决策逻辑全部在 :mod:`omg.core.idle_fade_fsm`（纯函数、可 headless 单测），
本模块只负责三件脏活：

1. 把 Qt 事件翻译成 FSM 事件（点击 / hover / 焦点 / hold 令牌）；
2. 管理三个计时器（淡出延迟、hover 唤醒确认、光标轮询）；
3. 用 ``QVariantAnimation`` 做 opacity 缓动（淡出慢、淡入快，中途反向不跳变）。

用法::

    self._fade = IdleFadeController(self, config=self._config)
    self._fade.attach(self)
    self._fade.hold("focus", True)          # 有保持条件时冻结淡出
    self._fade.notify_activity(strong=True)  # 点击 / 按键：立即唤醒

**测试约束**：构造时可用 ``params=`` 注入小数值（如 ``fade_delay=60``），
否则冒烟测试要等真实 900ms，既慢又不稳定。
"""

from __future__ import annotations

from typing import Optional, Set

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QTimer,
)
from PySide6.QtGui import QCursor, QWindow
from PySide6.QtWidgets import QApplication, QWidget

from omg.core.idle_fade_fsm import (
    Ctx,
    FadeAction,
    IdleFadeEvent,
    IdleFadeState,
    next_state,
)
from omg.core.window_behavior import FadeParams, resolve_fade_params

__all__ = ["IdleFadeController", "CURSOR_POLL_INTERVAL"]

#: 光标轮询间隔（ms）。窗口未开 mouseTracking，靠轮询捕获「在窗口内移动」。
#: 200ms 足够跟手（配合 enterEvent 的即时通知），开销可忽略。
CURSOR_POLL_INTERVAL = 200

#: 缓动帧间隔（ms），≈60fps
ANIM_FRAME_INTERVAL = 16

#: 触发强活动的鼠标事件类型（跳过 hover 确认，立即唤醒）
_PRESS_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
)

#: 缓动曲线标识（手写插值，见 _ease）
OUT_CUBIC = "out_cubic"   # 淡出：先快后慢，退场从容
OUT_QUAD = "out_quad"     # 淡入：更快回到不透明，响应优先


def _ease(t: float, curve: str) -> float:
    """把线性进度 t∈[0,1] 映射到缓动进度。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    if curve == OUT_CUBIC:
        return 1.0 - (1.0 - t) ** 3
    return 1.0 - (1.0 - t) ** 2  # OUT_QUAD


class IdleFadeController(QObject):
    """把一个顶层窗口的「闲置变淡」时机托管给 FSM。

    :param window: 受控窗口（通常就是持有者自身）
    :param config: ``ConfigManager``；为 None 时用默认参数
    :param params: 静态参数覆盖（测试注入小数值用），为 None 则每次从 config 读
    """

    def __init__(self, window, config=None, params: Optional[FadeParams] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._window = window
        self._config = config
        self._static_params = params

        self._state = IdleFadeState.OPAQUE
        self._holds: Set[str] = set()

        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.timeout.connect(self._on_delay_elapsed)

        self._wake_timer = QTimer(self)
        self._wake_timer.setSingleShot(True)
        self._wake_timer.timeout.connect(self._on_wake_timeout)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(CURSOR_POLL_INTERVAL)
        self._poll_timer.timeout.connect(self._poll_cursor)
        self._last_pos: Optional[QPoint] = None
        self._inside = False

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(ANIM_FRAME_INTERVAL)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._clock = QElapsedTimer()
        self._anim_from = 1.0
        self._anim_to = 1.0
        self._anim_dur = 0
        self._anim_curve = OUT_CUBIC

        # 是否处于 attach 状态。detach() 之后（如托盘模式下面板被收起）任何
        # 外部输入（hold / notify_activity / 光标轮询）都不应再驱动 FSM——
        # 否则 START_DELAY 会把刚停掉的计时器重新拉起来，让隐藏窗口空转。
        self._attached = False

        self._filtered = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def attach(self, window=None) -> None:
        """绑定窗口、装上全局输入监听，并启动第一轮换出倒计时。"""
        if window is not None:
            self._window = window
        if self._window is None:
            return
        app = QApplication.instance()
        if app is not None and not self._filtered:
            app.installEventFilter(self)
            self._filtered = True
        self._last_pos = None
        self._inside = self._cursor_inside()
        self._poll_timer.start()
        # 初始态是 OPAQUE：先把透明度复位（上个实例的残留值不该被继承），
        # 再启动第一轮淡出倒计时——否则窗口会一直停在 attach 前的透明度上。
        self._attached = True
        self._apply_action(FadeAction.SET_OPAQUE)
        self._apply_action(FadeAction.START_DELAY)

    def detach(self) -> None:
        """停掉一切计时与动画并卸载监听（窗口关闭 / 收进托盘时必须调用）。"""
        self._attached = False
        self._delay_timer.stop()
        self._wake_timer.stop()
        self._poll_timer.stop()
        self._anim_timer.stop()
        if self._filtered:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._filtered = False
        self._holds.clear()

    # ------------------------------------------------------------------
    # 外部输入
    # ------------------------------------------------------------------
    def hold(self, key: str, on: bool) -> None:
        """设置 / 解除一个保持令牌（冻结倒计时并强制不透明）。

        多个令牌可叠加，全部解除后才恢复倒计时。重复设置同一值不产生事件。
        """
        had = bool(self._holds)
        if on:
            self._holds.add(key)
        else:
            self._holds.discard(key)
        now = bool(self._holds)
        if now == had:
            return  # 集合未变化（重复设置 / 移除不存在的 key）
        if now:
            self._dispatch(IdleFadeEvent.HOLD_ON)
        elif self._state is IdleFadeState.HELD:
            self._dispatch(IdleFadeEvent.HOLD_OFF_CLEAR)

    def notify_activity(self, strong: bool = False) -> None:
        """记录一次活动。

        :param strong: True = 点击 / 按键 / 获得焦点，立即唤醒；
                       False = hover 进入或窗口内鼠标移动，需确认后才唤醒。
        """
        self._dispatch(IdleFadeEvent.ACTIVITY_STRONG if strong
                       else IdleFadeEvent.ACTIVITY_WEAK)

    def notify_leave(self) -> None:
        """光标离开窗口：取消尚未完成的 hover 唤醒确认。"""
        if self._state is IdleFadeState.WAKING:
            self._dispatch(IdleFadeEvent.WAKE_CANCELLED)

    def reevaluate(self) -> None:
        """重读参数并重算当前目标值（置顶切换 / 参数变化后调用）。"""
        self._dispatch(IdleFadeEvent.CONFIG_CHANGED)

    def apply_params(self, params: Optional[FadeParams]) -> None:
        """覆盖参数（测试注入小数值，或运行时调整手感）。

        传 ``None`` 可切回「每次从 config 即时读」（默认行为）。
        """
        self._static_params = params
        self.reevaluate()

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def state(self) -> IdleFadeState:
        return self._state

    @property
    def is_held(self) -> bool:
        return bool(self._holds)

    def current_opacity(self) -> float:
        if self._window is None:
            return 1.0
        return float(self._window.windowOpacity())

    # ------------------------------------------------------------------
    # 参数
    # ------------------------------------------------------------------
    def _params(self) -> FadeParams:
        """当前参数。有静态注入时用静态值，否则每次从 config 即时读。"""
        if self._static_params is not None:
            return self._static_params
        return resolve_fade_params(self._config)

    def _ctx(self) -> Ctx:
        p = self._params()
        return Ctx(idle_opacity=p.idle_opacity,
                   wake_confirm_ms=p.wake_confirm)

    # ------------------------------------------------------------------
    # Qt 事件 → FSM 事件
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        et = event.type()
        if et in _PRESS_EVENTS:
            if self._cursor_inside():
                self.notify_activity(strong=True)
        elif et == QEvent.Type.Wheel:
            if self._cursor_inside():
                self.notify_activity(strong=True)
        elif et in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            # 按键只在本窗口（或其子控件）内才算活动。
            # 全局事件过滤器会收到 QWindow（原生窗口句柄）事件——QWindow 不是 QWidget，
            # 直接喂给 isAncestorOf 会抛 TypeError。必须按类型显式分流，且绝不给
            # isAncestorOf 传任何非 QWidget 的对象：
            #   • obj 就是本窗口（QWidget）           → 命中
            #   • obj 是 QWindow（本窗口的 windowHandle）→ 命中本窗口的句柄
            #   • obj 是 QWidget（含子控件）           → 用 isAncestorOf 判祖先
            #   • 其它（其它窗口的控件/句柄、QAction 等）→ 不视为本窗口内活动
            w = self._window
            if w is not None:
                if obj is w:
                    inside = True
                elif isinstance(obj, QWindow):
                    # 原生窗口句柄：只可能是「本窗口自己的句柄」才算活动
                    inside = obj is w.windowHandle()
                elif isinstance(obj, QWidget):
                    # 子控件（QWidget）：用 isAncestorOf 判祖先关系（obj 确定是 QWidget）
                    inside = w.isAncestorOf(obj)
                else:
                    inside = False
                if inside:
                    self.notify_activity(strong=True)
        return False

    def _cursor_inside(self) -> bool:
        if self._window is None:
            return False
        return self._window.geometry().contains(QCursor.pos())

    def _poll_cursor(self) -> None:
        """轮询光标：在窗口内**移动**算弱活动；移出则取消唤醒确认。

        注意「停留不动」不产生活动——这正是修复「鼠标停在窗口上就永不淡出」
        的关键。轮询而非 mouseTracking，是为了免去给所有子控件递归开启。
        """
        if self._window is None:
            return
        inside = self._cursor_inside()
        if inside:
            pos = QCursor.pos()
            if self._last_pos is None or pos != self._last_pos:
                self._last_pos = pos
                self.notify_activity(strong=False)
            self._inside = True
        else:
            if self._inside:
                self._inside = False
                self._last_pos = None
                self.notify_leave()

    # ------------------------------------------------------------------
    # 计时器回调
    # ------------------------------------------------------------------
    def _on_delay_elapsed(self) -> None:
        self._dispatch(IdleFadeEvent.DELAY_ELAPSED)

    def _on_wake_timeout(self) -> None:
        """hover 确认到期：光标仍在窗口内才唤醒，否则视作划过。"""
        if self._cursor_inside():
            self._dispatch(IdleFadeEvent.WAKE_CONFIRMED)
        else:
            self._dispatch(IdleFadeEvent.WAKE_CANCELLED)

    # ------------------------------------------------------------------
    # 动画：手动逐帧插值（不用 QVariantAnimation）
    #
    # 手写的原因：QVariantAnimation 依赖平台的动画驱动时钟，在 offscreen 冒烟
    # 环境下推进极不稳定（实测淡出 40ms 的动画数秒都跑不完）。改用 QTimer +
    # QElapsedTimer 自行插值，帧率与耗时都由自己控制，测试与真机表现一致。
    # ------------------------------------------------------------------
    def _start_anim(self, target: float, duration_ms: int, curve: str) -> None:
        if self._window is None:
            return
        current = self.current_opacity()
        self._anim_timer.stop()
        if duration_ms <= 0 or abs(current - target) < 1e-3:
            # 无过渡（参数 0）或已在目标值：直接落位并补一个完成事件
            self._window.setWindowOpacity(target)
            self._dispatch(IdleFadeEvent.ANIM_FINISHED)
            return
        self._anim_from = current
        self._anim_to = float(target)
        self._anim_dur = int(duration_ms)
        self._anim_curve = curve
        self._clock.start()
        self._anim_timer.start()

    def _stop_anim(self) -> None:
        """停帧（不触发完成事件）——被 hold 抢占时用。"""
        self._anim_timer.stop()

    def _tick_anim(self) -> None:
        if self._window is None or self._anim_dur <= 0:
            self._anim_timer.stop()
            return
        t = self._clock.elapsed() / float(self._anim_dur)
        done = t >= 1.0
        if done:
            t = 1.0
        v = self._anim_from + (self._anim_to - self._anim_from) * _ease(
            t, self._anim_curve)
        self._window.setWindowOpacity(v)
        if done:
            self._anim_timer.stop()
            self._dispatch(IdleFadeEvent.ANIM_FINISHED)

    # ------------------------------------------------------------------
    # FSM 驱动
    # ------------------------------------------------------------------
    def _dispatch(self, event: IdleFadeEvent) -> None:
        # 未 attach（已 detach / 面板收进托盘）：吞掉一切事件，不让 FSM 重启计时器。
        if not self._attached:
            return
        state, actions = next_state(self._state, event, self._ctx())
        self._state = state
        for act in actions:
            self._apply_action(act)

    def _apply_action(self, act: FadeAction) -> None:
        p = self._params()
        if act is FadeAction.START_DELAY:
            self._delay_timer.stop()
            if p.fade_delay <= 0:
                # 0ms 也走一轮事件循环，避免同步递归
                self._delay_timer.setInterval(0)
            else:
                self._delay_timer.setInterval(int(p.fade_delay))
            self._delay_timer.start()
        elif act is FadeAction.STOP_DELAY:
            self._delay_timer.stop()
        elif act is FadeAction.START_WAKE:
            self._wake_timer.stop()
            self._wake_timer.setInterval(max(0, int(p.wake_confirm)))
            self._wake_timer.start()
        elif act is FadeAction.STOP_WAKE:
            self._wake_timer.stop()
        elif act is FadeAction.START_FADE_OUT:
            self._start_anim(p.idle_opacity_f, p.fade_out, OUT_CUBIC)
        elif act is FadeAction.START_FADE_IN:
            self._start_anim(1.0, p.fade_in, OUT_QUAD)
        elif act is FadeAction.STOP_ANIM:
            self._stop_anim()
        elif act is FadeAction.SET_OPAQUE:
            if self._window is not None:
                self._window.setWindowOpacity(1.0)
        elif act is FadeAction.SET_IDLE:
            if self._window is not None:
                self._window.setWindowOpacity(p.idle_opacity_f)
