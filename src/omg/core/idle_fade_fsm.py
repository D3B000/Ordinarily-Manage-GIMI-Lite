"""omg.core.idle_fade_fsm — 闲置变淡状态机（纯逻辑，无 Qt 依赖）。

把 home 窗口「什么时候该变淡、什么时候该变实」的决策抽成一个**纯函数**，
这样时序规则可以 headless 单测，Qt 层（omg.ui.idle_fade）只负责计时器与动画。

核心思路：把「活动」与「保持」拆成两类输入——

- **活动（ACTIVITY）**：只刷新倒计时，不阻止淡出。
  鼠标在窗口内移动、点击、获得焦点等。因此**鼠标停在窗口上不动也会淡出**
  （修复旧逻辑里 hover 被当成保持条件导致的「假闲置」）。
- **保持（HOLD）**：冻结倒计时并强制不透明，直到令牌全部解除。
  焦点在本窗口 / 忙碌（更新、注入）/ 拖动中 / 子窗口打开中。

状态与迁移（``next_state`` 是唯一入口）：

    OPAQUE ──DELAY_ELAPSED──> FADING_OUT ──ANIM_FINISHED──> IDLE
       ^                          │                          │
       │                      任意 ACTIVITY               ACTIVITY_WEAK
       │                          v                          v
    FADING_IN <──────────────────────────────────────────  WAKING
       │                                                     │
    ANIM_FINISHED                              WAKE_CANCELLED（离开）

    任意状态 ──HOLD_ON──> HELD ──HOLD_OFF_CLEAR──> OPAQUE（重新倒数）

``next_state`` 返回 ``(新状态, 动作元组)``，动作由 Qt 层按序执行（顺序有意义，
例如 STOP_ANIM 必须先于 SET_OPAQUE）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Tuple

__all__ = [
    "IdleFadeState",
    "IdleFadeEvent",
    "FadeAction",
    "Ctx",
    "next_state",
]


class IdleFadeState(Enum):
    """闲置变淡状态机的六个状态。"""

    OPAQUE = auto()      # 不透明，倒计时进行中（每次活动重置）
    HELD = auto()        # 不透明，倒计时冻结（至少有一个 hold 令牌）
    FADING_OUT = auto()  # 缓动中：1.0 → idle
    IDLE = auto()        # 闲置：稳定在 idle 透明度
    WAKING = auto()      # 闲置 + hover 确认倒计时中（仍是 idle 透明度）
    FADING_IN = auto()   # 缓动中：当前值 → 1.0


class IdleFadeEvent(Enum):
    """输入事件。"""

    ACTIVITY_STRONG = auto()  # 点击 / 按键 / 获得焦点：立即唤醒
    ACTIVITY_WEAK = auto()    # hover 进入 / 窗口内鼠标移动：需确认后才唤醒
    HOLD_ON = auto()          # 新增一个保持令牌
    HOLD_OFF_STILL = auto()   # 解除一个令牌，仍有其它令牌
    HOLD_OFF_CLEAR = auto()   # 解除最后一个令牌
    DELAY_ELAPSED = auto()    # 淡出延迟倒计时到期
    WAKE_CONFIRMED = auto()   # hover 确认到期且光标仍在窗口内
    WAKE_CANCELLED = auto()   # hover 确认期内光标离开
    ANIM_FINISHED = auto()    # 缓动动画自然结束
    CONFIG_CHANGED = auto()   # 参数（如 idle_opacity）变化，重算目标值


class FadeAction(Enum):
    """Qt 层需要执行的副作用。"""

    NONE = auto()
    START_DELAY = auto()    # 启动 / 重启淡出倒计时
    STOP_DELAY = auto()     # 停止淡出倒计时
    START_FADE_OUT = auto()  # 启动淡出缓动（从当前值 → idle）
    START_FADE_IN = auto()   # 启动淡入缓动（从当前值 → 1.0）
    STOP_ANIM = auto()      # 停止缓动（不触发 ANIM_FINISHED）
    SET_OPAQUE = auto()     # 立即置 1.0（无动画）
    SET_IDLE = auto()       # 立即置 idle（无动画）
    START_WAKE = auto()     # 启动 hover 确认倒计时
    STOP_WAKE = auto()      # 取消 hover 确认倒计时


@dataclass(frozen=True)
class Ctx:
    """迁移判定所需的只读上下文（由 FadeParams 折算而来）。"""

    idle_opacity: int = 42
    wake_confirm_ms: int = 120

    @property
    def fade_disabled(self) -> bool:
        """idle_opacity ≥ 100 → 永不淡出。"""
        return self.idle_opacity >= 100

    @property
    def wake_immediate(self) -> bool:
        """wake_confirm ≤ 0 → hover 进入即刻唤醒（等同改造前行为）。"""
        return self.wake_confirm_ms <= 0


_NOOP: Tuple[FadeAction, ...] = ()


def next_state(state: IdleFadeState,
               event: IdleFadeEvent,
               ctx: Ctx = Ctx()) -> Tuple[IdleFadeState, Tuple[FadeAction, ...]]:
    """状态迁移纯函数。

    未列出的 (状态, 事件) 组合一律「原地不动、无副作用」——调用方可以放心
    转发任何事件，不必在外部做合法性判断。
    """
    if ctx is None:
        ctx = Ctx()

    # ---- HOLD_ON：从任意状态抢占到 HELD，冻结计时并立刻不透明 ----
    if event is IdleFadeEvent.HOLD_ON:
        if state is IdleFadeState.HELD:
            return state, _NOOP
        return IdleFadeState.HELD, (
            FadeAction.STOP_DELAY,
            FadeAction.STOP_WAKE,
            FadeAction.STOP_ANIM,
            FadeAction.SET_OPAQUE,
        )

    # ---- HELD：冻结期间忽略一切活动 / 计时 / 配置变化 ----
    if state is IdleFadeState.HELD:
        if event is IdleFadeEvent.HOLD_OFF_CLEAR:
            return IdleFadeState.OPAQUE, (FadeAction.START_DELAY,)
        return state, _NOOP

    if state is IdleFadeState.OPAQUE:
        if event in (IdleFadeEvent.ACTIVITY_STRONG, IdleFadeEvent.ACTIVITY_WEAK):
            return state, (FadeAction.START_DELAY,)  # 活动 = 重置倒计时
        if event is IdleFadeEvent.DELAY_ELAPSED:
            if ctx.fade_disabled:
                return state, (FadeAction.START_DELAY,)  # 100% → 永不淡出
            return IdleFadeState.FADING_OUT, (FadeAction.START_FADE_OUT,)
        return state, _NOOP

    if state is IdleFadeState.FADING_OUT:
        # 淡出途中任何活动都立刻反向（从当前值继续，不跳变）
        if event in (IdleFadeEvent.ACTIVITY_STRONG, IdleFadeEvent.ACTIVITY_WEAK):
            return IdleFadeState.FADING_IN, (FadeAction.START_FADE_IN,)
        if event is IdleFadeEvent.ANIM_FINISHED:
            return IdleFadeState.IDLE, _NOOP
        return state, _NOOP

    if state is IdleFadeState.IDLE:
        if event is IdleFadeEvent.ACTIVITY_STRONG:
            return IdleFadeState.FADING_IN, (
                FadeAction.STOP_WAKE, FadeAction.START_FADE_IN)
        if event is IdleFadeEvent.ACTIVITY_WEAK:
            if ctx.wake_immediate:
                return IdleFadeState.FADING_IN, (
                    FadeAction.STOP_WAKE, FadeAction.START_FADE_IN)
            # 起确认倒计时；期间鼠标不动不算新事件，不会无限续期
            return IdleFadeState.WAKING, (FadeAction.START_WAKE,)
        if event is IdleFadeEvent.CONFIG_CHANGED:
            return state, (FadeAction.SET_IDLE,)  # 目标值变了，直接落到新值
        return state, _NOOP

    if state is IdleFadeState.WAKING:
        if event is IdleFadeEvent.ACTIVITY_STRONG:
            return IdleFadeState.FADING_IN, (
                FadeAction.STOP_WAKE, FadeAction.START_FADE_IN)
        if event is IdleFadeEvent.WAKE_CONFIRMED:
            return IdleFadeState.FADING_IN, (FadeAction.START_FADE_IN,)
        if event is IdleFadeEvent.WAKE_CANCELLED:
            return IdleFadeState.IDLE, (FadeAction.STOP_WAKE,)
        return state, _NOOP  # 重复 weak activity 不重置确认倒计时

    if state is IdleFadeState.FADING_IN:
        if event is IdleFadeEvent.ANIM_FINISHED:
            return IdleFadeState.OPAQUE, (FadeAction.START_DELAY,)
        return state, _NOOP  # 淡入途中重复活动无副作用

    return state, _NOOP
