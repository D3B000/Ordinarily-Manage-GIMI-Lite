"""omg.core.list_render — 列表渲染策略（无 Qt 依赖，可 headless 导入）。

移植自 nahida-desktop 3.2.0「设置 → 模组 → 性能」卡片的语义（非实现）：

    isVirtualizationEnabled = enabled && count >= threshold

**为什么不照搬实现**：nahida 是 React DOM 全量渲染，需要 ``@tanstack/react-virtual``
手写虚拟化器；而 Qt 的 item view（QListView + Model + Delegate）**原生只对可视区
paint**，几千项也无需额外虚拟化。因此这里只移植「用户可配置的语义」：

- ``ui_list_simple_render``（逃生舱）：强制一律走简化渲染（QListView + Delegate）；
- ``ui_list_simple_threshold``（阈值）：项数 ≥ 阈值时才走简化渲染，低于阈值时用
  每项一个真实 QWidget 的布局（交互最完整：拖放、内嵌按钮、动画）。

阈值规则与源端对齐：默认 30；前端要求 ≥10；后端 normalize 时非正数回落默认。

消费端（未来的资源浏览页）只需调用 :func:`resolve_list_render_mode` 决定用哪条
渲染路径，两条路径对上层暴露等价接口（refresh / scroll_to_path）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "KEY_UI_LIST_SIMPLE_RENDER",
    "KEY_UI_LIST_SIMPLE_THRESHOLD",
    "DEFAULT_LIST_THRESHOLD",
    "MIN_LIST_THRESHOLD",
    "clamp_list_threshold",
    "resolve_list_render_mode",
    "resolve_list_render_mode_from_config",
]

# 持久化键名（与 core/config.PERSISTENT_KEYS 对齐）
KEY_UI_LIST_SIMPLE_RENDER = "ui_list_simple_render"          # 1/0：强制简化渲染（逃生舱）
KEY_UI_LIST_SIMPLE_THRESHOLD = "ui_list_simple_threshold"    # int：阈值，默认 30

DEFAULT_LIST_THRESHOLD = 30     # 对应 nahida defaultVirtualizationThreshold
MIN_LIST_THRESHOLD = 10         # 对应 nahida 前端校验「阈值不得小于 10」


def _as_int(value: Any, fallback: int) -> int:
    """尽力把配置值转成 int，失败（None / 空串 / 非数字）回落 fallback。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def clamp_list_threshold(value: Any) -> int:
    """阈值合法化：非数字 / ≤0 → 默认 30；小于最小值 → 抬到 10。

    对齐源端两处校验：
      - Go ``clampVirtualizationThreshold``：NaN / Inf / ≤0 → 默认；否则 Trunc 取整；
      - React 前端：提交时 <10 直接拒绝保存（toast 警告）。
    """
    n = _as_int(value, 0)
    if n <= 0:
        return DEFAULT_LIST_THRESHOLD
    return max(n, MIN_LIST_THRESHOLD)


def resolve_list_render_mode(
    count: Any,
    threshold: Any = None,
    force_simple: bool = False,
) -> bool:
    """判定渲染模式：返回 ``True`` = 用简化渲染（QListView + Delegate）。

    参数：
        count: 列表项数（非法值按 0 处理）；
        threshold: 阈值，缺省时用 :data:`DEFAULT_LIST_THRESHOLD`；
        force_simple: 逃生舱开关，打开时恒返回 ``True``。
    """
    if force_simple:
        return True
    return _as_int(count, 0) >= clamp_list_threshold(threshold)


def resolve_list_render_mode_from_config(cfg: Any, count: Any) -> bool:
    """从 ConfigManager（或任何实现 ``get(key, default)`` 的对象）读取设置并判定。

    ``cfg`` 用鸭子类型访问，仅依赖 ``get``，便于测试用轻量替身。
    """
    get = getattr(cfg, "get", None)
    if get is None:
        return resolve_list_render_mode(count)
    force = bool(_as_int(get(KEY_UI_LIST_SIMPLE_RENDER, 0), 0))
    threshold = get(KEY_UI_LIST_SIMPLE_THRESHOLD, DEFAULT_LIST_THRESHOLD)
    return resolve_list_render_mode(count, threshold, force)
