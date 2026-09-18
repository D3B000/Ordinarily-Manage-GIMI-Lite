"""
locale.py — 极简本地化垫片（移植自 XXMI 的 core.locale_manager.L）

XXMI 的注入/监控引擎大量使用 ``L(key, default)`` 来获取可本地化的提示文案。
为避免把整套 locale_manager 拖进 OMGLite，这里提供一个最小兼容实现：
默认直接返回 default 字符串；若后续 OMGLite 接入真正的本地化系统，
只需替换本模块的 ``L`` 即可。
"""


def L(key: str, default: str = "") -> str:
    """兼容 XXMI 调用签名的最小本地化函数。

    Args:
        key:     本地化键名（当前忽略，保留以兼容调用点）。
        default: 默认（英文/中文）文案，直接返回。

    Returns:
        展示用字符串。
    """
    return default
