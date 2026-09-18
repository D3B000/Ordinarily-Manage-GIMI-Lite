"""
launch.types — 启动流程的共享类型。

包含：
  - LaunchState : 开始按钮状态机枚举（IDLE / INJECTING / DONE / ERROR）
  - LaunchTask  : 已装配好的单次启动任务（UI 线程内由 Controller 构造，传入
                  Worker 线程执行，避免线程内再读配置引发竞态）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from omg.domain.inject.launcher import StartRequest


class LaunchState(Enum):
    """主页「开始」按钮的状态机状态。"""

    IDLE = "idle"        # 空闲：play.svg，可用，点击触发注入
    INJECTING = "injecting"  # 注入中：circle_loading（动态），禁用（防重复）
    DONE = "done"        # 注入完成：play.svg，保持禁用
    ERROR = "error"      # 注入失败：play.svg，恢复可用（便于重试）


@dataclass
class LaunchTask:
    """一次完整启动 / 注入所需的全部参数（调用前在 UI 线程装配好）。"""

    gimi_dir: str                 # GIMI 目录（d3dx.ini / d3d11.dll 所在）
    d3dx_target: str              # [Loader] target = 游戏进程名
    d3dx_hunting: int             # [Hunting] hunting
    d3dx_warning: int             # [Logging] show_warnings
    d3dx_launch: str              # [Loader] launch = 游戏 exe 路径（manual 留空）
    d3dx_loader: str              # [Loader] loader = 当前进程名（与 Hook/Direct 校验一致）
    injector_path: str            # 3dmloader.dll 完整路径
    request: "StartRequest"       # 引擎层 StartRequest
