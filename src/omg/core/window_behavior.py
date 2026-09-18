"""omg.core.window_behavior — 窗口行为设置（无 Qt 依赖，可 headless 导入）。

设置页「OMG → 选项」卡片背后的纯逻辑，语义与消费端（omg.pages.home.HomeWindow）对齐：

- ``omg_always_on_top``（默认置顶，1/0）：启动时即应用 ``Qt.WindowStaysOnTopHint``；
  菜单里的「置顶 / 取消置顶」仍是会话级开关，本键只决定**下次启动**的初始态；
- ``omg_idle_opacity``（闲置时透明度，int 百分比 20~100）：
  窗口「无活动且无保持条件」时降至该透明度，有活动 / 被保持时不透明。
  100 = 不变淡。默认 42，对应 home 页历史上的硬编码 ``setWindowOpacity(0.42)``。

时机相关的四个参数（默认 UI 不暴露，手改 config.json 亦生效）：

- ``omg_idle_fade_delay``（淡出延迟 ms，默认 900）：最后一个保持条件解除 /
  最后一次活动之后，等待多久才开始淡出；
- ``omg_idle_fade_out`` / ``omg_idle_fade_in``（过渡时长 ms，默认 220 / 140）：
  淡出比淡入慢，符合「响应优先、退场从容」的手感；
- ``omg_idle_wake_confirm``（hover 唤醒确认 ms，默认 120）：闲置态下鼠标进入
  需停留这么久才淡入，避免鼠标无意划过导致「幽灵唤醒」；点击 / 按键立即唤醒。

以上均作用于 OMG 主窗口。资源浏览界面是独立弹出窗口，有自己一套外观偏好
（键名 ``mm_`` 前缀，由 ``omg.pages.resources`` 标题栏读写）：

- ``mm_always_on_top``（资源浏览界面置顶，1/0）：与主窗口置顶互不干扰；
- ``mm_opacity``（整体透明度百分比 20~100，默认 100 = 不透明）。

消费端通过 :func:`resolve_idle_opacity_from_config` / :func:`resolve_fade_params`
取值（每次调用即时读 config，设置页改完后立即生效，无需重启）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "KEY_OMG_ALWAYS_ON_TOP",
    "KEY_OMG_IDLE_OPACITY",
    "KEY_OMG_FADE_DELAY",
    "KEY_OMG_FADE_OUT",
    "KEY_OMG_FADE_IN",
    "KEY_OMG_WAKE_CONFIRM",
    "DEFAULT_IDLE_OPACITY",
    "MIN_IDLE_OPACITY",
    "MAX_IDLE_OPACITY",
    "DEFAULT_FADE_DELAY", "MIN_FADE_DELAY", "MAX_FADE_DELAY",
    "DEFAULT_FADE_OUT", "MIN_FADE_OUT", "MAX_FADE_OUT",
    "DEFAULT_FADE_IN", "MIN_FADE_IN", "MAX_FADE_IN",
    "DEFAULT_WAKE_CONFIRM", "MIN_WAKE_CONFIRM", "MAX_WAKE_CONFIRM",
    "KEY_OMG_LAUNCH_HOLD",
    "KEY_OMG_LAUNCH_HOLD_MS",
    "DEFAULT_LAUNCH_HOLD_MS", "MIN_LAUNCH_HOLD_MS", "MAX_LAUNCH_HOLD_MS",
    "KEY_MM_ALWAYS_ON_TOP",
    "KEY_MM_OPACITY",
    "DEFAULT_MM_OPACITY", "MIN_MM_OPACITY", "MAX_MM_OPACITY",
    "clamp_mm_opacity",
    "resolve_mm_always_on_top",
    "resolve_mm_opacity",
    "clamp_idle_opacity",
    "clamp_fade_delay", "clamp_fade_out", "clamp_fade_in", "clamp_wake_confirm",
    "clamp_launch_hold_ms",
    "resolve_idle_opacity_from_config",
    "resolve_always_on_top",
    "resolve_launch_hold_enabled",
    "resolve_launch_hold_ms",
    "FadeParams",
    "resolve_fade_params",
]

# 持久化键名（与 core/config.PERSISTENT_KEYS 对齐）
KEY_OMG_ALWAYS_ON_TOP = "omg_always_on_top"    # 1/0：默认置顶
KEY_OMG_IDLE_OPACITY = "omg_idle_opacity"      # int：闲置透明度百分比，默认 42
KEY_OMG_FADE_DELAY = "omg_idle_fade_delay"     # int：淡出延迟 ms，默认 900
KEY_OMG_FADE_OUT = "omg_idle_fade_out"         # int：淡出过渡 ms，默认 220
KEY_OMG_FADE_IN = "omg_idle_fade_in"           # int：淡入过渡 ms，默认 140
KEY_OMG_WAKE_CONFIRM = "omg_idle_wake_confirm"  # int：hover 唤醒确认 ms，默认 120
KEY_OMG_LAUNCH_HOLD = "omg_launch_hold_enabled"  # 1/0：启动按钮防误触（长按执行）
KEY_OMG_LAUNCH_HOLD_MS = "omg_launch_hold_ms"    # int：长按时间 ms，默认 800
# 资源浏览界面（独立弹出窗口）的外观：与主窗口的置顶 / 透明度互不干扰，
# 由「资源浏览」标题栏上的按钮 / 输入框直接读写，故键名统一用 mm_ 前缀。
# （mm_ = 历史模块名 mod_manager 的缩写；持久化键一旦改名会让老用户的
#   置顶 / 透明度偏好静默回默认，故键名保持不变，仅界面文案统一为「资源浏览」。）
KEY_MM_ALWAYS_ON_TOP = "mm_always_on_top"  # 1/0：资源浏览界面置顶
KEY_MM_OPACITY = "mm_opacity"              # int：资源浏览界面整体透明度百分比
DEFAULT_MM_OPACITY = 100                   # 100 = 完全不透明
MIN_MM_OPACITY = 20                        # 低于 20% 窗口几乎不可见，难以找回
MAX_MM_OPACITY = 100

DEFAULT_IDLE_OPACITY = 42     # 对应 home 页原硬编码 setWindowOpacity(0.42)
MIN_IDLE_OPACITY = 0          # 0 = 闲置时完全透明（仍可被唤醒恢复）
MAX_IDLE_OPACITY = 100        # 100 = 完全不透明（即不启用变淡）

DEFAULT_FADE_DELAY = 900
MIN_FADE_DELAY, MAX_FADE_DELAY = 0, 5000      # 0 = 立即淡出（等同改造前行为）

DEFAULT_FADE_OUT = 220
MIN_FADE_OUT, MAX_FADE_OUT = 0, 1000          # 0 = 无过渡（等同改造前行为）

DEFAULT_FADE_IN = 140
MIN_FADE_IN, MAX_FADE_IN = 0, 1000

DEFAULT_WAKE_CONFIRM = 120
MIN_WAKE_CONFIRM, MAX_WAKE_CONFIRM = 0, 500   # 0 = 划过即唤醒（等同改造前行为）

DEFAULT_LAUNCH_HOLD_MS = 800
MIN_LAUNCH_HOLD_MS, MAX_LAUNCH_HOLD_MS = 200, 3000  # 体验上 <200ms 与点击无差，>3s 太累


def _as_int(value: Any, fallback: int) -> int:
    """尽力把配置值转成 int，失败（None / 空串 / 非数字）回落 fallback。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def clamp_idle_opacity(value: Any) -> int:
    """闲置透明度合法化：非法 → 默认 42（恰在区间内）；否则夹到 [20, 100]。"""
    n = _as_int(value, DEFAULT_IDLE_OPACITY)
    return max(MIN_IDLE_OPACITY, min(MAX_IDLE_OPACITY, n))


def resolve_idle_opacity_from_config(cfg: Any) -> int:
    """从 ConfigManager（或任何实现 ``get(key, default)`` 的对象）读取闲置透明度。

    ``cfg`` 用鸭子类型访问，仅依赖 ``get``；cfg 为 None（home 窗口 debug 入口
    不传 config）时返回默认 42。返回值语义为百分比，消费端除以 100 使用。
    """
    get = getattr(cfg, "get", None)
    if get is None:
        return DEFAULT_IDLE_OPACITY
    return clamp_idle_opacity(get(KEY_OMG_IDLE_OPACITY, DEFAULT_IDLE_OPACITY))


def resolve_always_on_top(cfg: Any) -> bool:
    """从配置读取「默认置顶」；cfg 为 None 或值非法时返回 False。"""
    get = getattr(cfg, "get", None)
    if get is None:
        return False
    return bool(_as_int(get(KEY_OMG_ALWAYS_ON_TOP, 0), 0))


def resolve_launch_hold_enabled(cfg: Any) -> bool:
    """从配置读取「启动按钮防误触」开关；cfg 为 None 或值非法时返回 False。"""
    get = getattr(cfg, "get", None)
    if get is None:
        return False
    return bool(_as_int(get(KEY_OMG_LAUNCH_HOLD, 0), 0))


def resolve_launch_hold_ms(cfg: Any) -> int:
    """从配置读取「长按时间」（ms）。

    每次调用即时读 config（设置页改完下一次按下即生效，无需重启）；
    cfg 为 None 返回默认 800。
    """
    get = getattr(cfg, "get", None)
    if get is None:
        return DEFAULT_LAUNCH_HOLD_MS
    return clamp_launch_hold_ms(get(KEY_OMG_LAUNCH_HOLD_MS, DEFAULT_LAUNCH_HOLD_MS))


def clamp_mm_opacity(value: Any) -> int:
    """资源浏览界面透明度合法化：非法 → 100（完全不透明），否则夹到 [20, 100]。"""
    n = _as_int(value, DEFAULT_MM_OPACITY)
    return max(MIN_MM_OPACITY, min(MAX_MM_OPACITY, n))


def resolve_mm_always_on_top(cfg: Any) -> bool:
    """读取资源浏览界面的置顶偏好；cfg 为 None / 值非法时返回 False。"""
    get = getattr(cfg, "get", None)
    if get is None:
        return False
    return bool(_as_int(get(KEY_MM_ALWAYS_ON_TOP, 0), 0))


def resolve_mm_opacity(cfg: Any) -> int:
    """读取资源浏览界面的整体透明度（百分比）；cfg 为 None 时返回 100。"""
    get = getattr(cfg, "get", None)
    if get is None:
        return DEFAULT_MM_OPACITY
    return clamp_mm_opacity(get(KEY_MM_OPACITY, DEFAULT_MM_OPACITY))


def _clamp_ms(value: Any, default: int, lo: int, hi: int) -> int:
    """毫秒类参数合法化：非法 → default，否则夹到 [lo, hi]。"""
    n = _as_int(value, default)
    return max(lo, min(hi, n))


def clamp_fade_delay(value: Any) -> int:
    """淡出延迟（ms）：非法 → 900，否则夹到 [0, 5000]。"""
    return _clamp_ms(value, DEFAULT_FADE_DELAY, MIN_FADE_DELAY, MAX_FADE_DELAY)


def clamp_fade_out(value: Any) -> int:
    """淡出过渡时长（ms）：非法 → 220，否则夹到 [0, 1000]。"""
    return _clamp_ms(value, DEFAULT_FADE_OUT, MIN_FADE_OUT, MAX_FADE_OUT)


def clamp_fade_in(value: Any) -> int:
    """淡入过渡时长（ms）：非法 → 140，否则夹到 [0, 1000]。"""
    return _clamp_ms(value, DEFAULT_FADE_IN, MIN_FADE_IN, MAX_FADE_IN)


def clamp_wake_confirm(value: Any) -> int:
    """hover 唤醒确认时长（ms）：非法 → 120，否则夹到 [0, 500]。"""
    return _clamp_ms(value, DEFAULT_WAKE_CONFIRM, MIN_WAKE_CONFIRM, MAX_WAKE_CONFIRM)


def clamp_launch_hold_ms(value: Any) -> int:
    """启动长按时间（ms）：非法 → 800，否则夹到 [200, 3000]。"""
    return _clamp_ms(value, DEFAULT_LAUNCH_HOLD_MS,
                     MIN_LAUNCH_HOLD_MS, MAX_LAUNCH_HOLD_MS)


@dataclass(frozen=True)
class FadeParams:
    """闲置变淡的一组时机参数（毫秒 / 百分比，均为已 clamp 的合法值）。"""

    idle_opacity: int = DEFAULT_IDLE_OPACITY
    fade_delay: int = DEFAULT_FADE_DELAY
    fade_out: int = DEFAULT_FADE_OUT
    fade_in: int = DEFAULT_FADE_IN
    wake_confirm: int = DEFAULT_WAKE_CONFIRM

    @property
    def idle_opacity_f(self) -> float:
        """闲置透明度的 0~1 浮点形式（供 setWindowOpacity 使用）。"""
        return self.idle_opacity / 100.0

    @property
    def fade_disabled(self) -> bool:
        """透明度 100% 即不启用变淡（窗口恒不透明）。"""
        return self.idle_opacity >= MAX_IDLE_OPACITY


def resolve_fade_params(cfg: Any) -> FadeParams:
    """从 ConfigManager（或任何实现 ``get(key, default)`` 的对象）读取全部时机参数。

    ``cfg`` 用鸭子类型访问，仅依赖 ``get``；cfg 为 None（home 窗口 debug 入口
    不传 config）时返回全默认值。
    """
    get = getattr(cfg, "get", None)
    if get is None:
        return FadeParams()
    return FadeParams(
        idle_opacity=clamp_idle_opacity(
            get(KEY_OMG_IDLE_OPACITY, DEFAULT_IDLE_OPACITY)),
        fade_delay=clamp_fade_delay(
            get(KEY_OMG_FADE_DELAY, DEFAULT_FADE_DELAY)),
        fade_out=clamp_fade_out(
            get(KEY_OMG_FADE_OUT, DEFAULT_FADE_OUT)),
        fade_in=clamp_fade_in(
            get(KEY_OMG_FADE_IN, DEFAULT_FADE_IN)),
        wake_confirm=clamp_wake_confirm(
            get(KEY_OMG_WAKE_CONFIRM, DEFAULT_WAKE_CONFIRM)),
    )
