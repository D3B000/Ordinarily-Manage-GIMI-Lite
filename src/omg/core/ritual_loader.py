"""
omg.core.ritual_loader — 仪式整包（单一 .pyd）的运行时加载器。

公开仓库不含 ``omg.domain.ritual`` 源码，仪式的全部符号（prlib 的
analyze_internal / apply_patches / plan_patches / TransformOptions /
validate_transformation、d3d11_builder 的 find_msbuild / ALL_SAFE_FILES、
d3d11InOMG 的 run_pack / run_paint）都编译进 ``ritual_bundle.pyd`` 并按名导出。

本模块只依赖标准库 + omg.core.paths，刻意不依赖 alchemy，以避免
alchemy / build_env 之间的循环导入——二者都从这里取 load_ritual_bundle()。
"""
from __future__ import annotations

import os
import sys
import importlib.machinery
import importlib.util

from omg.core.paths import BINARIES_DIR
from omg.core.logging_setup import logger

#: 仪式整包编译产物（单一 .pyd）。开发态位于 src/omg/resources/binaries/ritual/，
#: 由 PyInstaller datas 收集到运行态 bin/resources/binaries/ritual/。
RITUAL_BUNDLE_PYD = os.path.join(BINARIES_DIR, "ritual", "ritual_bundle.pyd")

#: 挂到 sys.modules 的模块名。
#: 必须与 .pyd 导出的初始化函数一致：CPython 会按导入时使用的模块名查找
#: ``PyInit_<name>``。Nuitka 以入口 ritual_bundle.py 编译，导出的是
#: ``PyInit_ritual_bundle``，故此处只能用 "ritual_bundle"；若改成其它名字，
#: 加载会报 "dynamic module does not define module export function"。
_RITUAL_BUNDLE_MOD = "ritual_bundle"

#: Cython BFS 加速模块，随包单独分发（与 ritual_bundle.pyd 同目录）。
#: Nuitka 无法把扩展模块(.pyd)嵌入单一 bundle（报 "Extension modules cannot
#: be inspected"），故 Cython 加速只能随包分发；其源码 _cython_analysis.pyx 是
#: 重度 C 级实现（malloc / C 数组 / 位图），改写成纯 Python 会丢失全部收益，
#: 因此不做重写，改为运行期预注册。
CYTHON_ANALYSIS_PYD = os.path.join(BINARIES_DIR, "ritual", "_cython_analysis.pyd")

#: 必须与 pyd 导出的 PyInit__cython_analysis 一致（已扫二进制确认），
#: 且正好等于 bundle 内 analysis.py 所 import 的名字。
_CYTHON_ANALYSIS_MOD = "_cython_analysis"


def ensure_cython_analysis() -> bool:
    """把随包分发的 ``_cython_analysis.pyd`` 注册为顶层模块 ``_cython_analysis``。

    bundle 内 ``analysis.py`` 的 ``import _cython_analysis`` 是运行期真实 import
    （Nuitka 无法静态解析扩展模块），会先查 ``sys.modules``；此处提前注册即可命中，
    从而启用 Cython BFS。注册失败则 prlib 自动回落纯 Python BFS（结果一致、较慢）。

    Returns:
        True 表示 Cython BFS 加速可用。
    """
    if _CYTHON_ANALYSIS_MOD in sys.modules:
        return True
    if not os.path.isfile(CYTHON_ANALYSIS_PYD):
        return False
    try:
        loader = importlib.machinery.ExtensionFileLoader(
            _CYTHON_ANALYSIS_MOD, CYTHON_ANALYSIS_PYD
        )
        spec = importlib.util.spec_from_file_location(
            _CYTHON_ANALYSIS_MOD, CYTHON_ANALYSIS_PYD, loader=loader
        )
        if spec is None:
            return False
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        sys.modules[_CYTHON_ANALYSIS_MOD] = mod
        return True
    except Exception:
        logger.warning("Cython 加速模块注册失败: %s",
                       CYTHON_ANALYSIS_PYD, exc_info=True)
        return False


def load_ritual_bundle():
    """加载仪式整包 ``ritual_bundle.pyd``，返回模块；不可用返回 ``None``。

    用 ExtensionFileLoader 按路径加载（与 alchemy.ensure_cython_analysis 同机制）。
    文件缺失或加载失败均返回 None，调用方据此优雅降级——公开仓库无此 .pyd 时
    仪式功能不可用，但不影响应用启动与导入。

    加载 bundle 前会先尝试注册 Cython BFS 加速模块，使 bundle 内
    ``import _cython_analysis`` 能命中。
    """
    # 先挂 Cython 加速，再加载 bundle（bundle 内的 import 才会命中 sys.modules）
    ensure_cython_analysis()
    if _RITUAL_BUNDLE_MOD in sys.modules:
        return sys.modules[_RITUAL_BUNDLE_MOD]
    if not os.path.isfile(RITUAL_BUNDLE_PYD):
        return None
    try:
        loader = importlib.machinery.ExtensionFileLoader(
            _RITUAL_BUNDLE_MOD, RITUAL_BUNDLE_PYD
        )
        spec = importlib.util.spec_from_file_location(
            _RITUAL_BUNDLE_MOD, RITUAL_BUNDLE_PYD, loader=loader
        )
        if spec is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        sys.modules[_RITUAL_BUNDLE_MOD] = mod
        return mod
    except Exception:
        logger.warning("仪式整包加载失败: %s", RITUAL_BUNDLE_PYD, exc_info=True)
        return None
