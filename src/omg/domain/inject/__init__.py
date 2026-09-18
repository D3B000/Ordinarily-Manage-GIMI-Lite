"""
omg.domain.inject — 3DMigoto 注入 / 启动 / 进程监控引擎

移植自 XXMI-Launcher-2.2.1，拆分为多个模块：
  - process_tracker : 进程监控（ctypes 实现，multiprocessing 子进程等待）
  - dll_injector    : DllInjector 类（加载/启动/钩子/直接注入）
  - launcher        : 编排层（StartRequest / LaunchContext / MigotoInjector）
  - errors          : 注入错误码枚举
  - locale          : 极简本地化垫片 L()

典型用法：
    from omg.domain.inject import MigotoInjector, StartRequest, wait_for_process

    req = StartRequest(
        process_name="GenshinImpact.exe",
        exe_path=r"C:/Game/GenshinImpact.exe",
        xxmi_dll_path=r"C:/GIMI/d3d11.dll",
        use_hook=True,
    )
    MigotoInjector.from_request(req, injector_path=r"C:/GIMI/3dmloader.dll").run()
"""

from .process_tracker import (
    ProcessPriority,
    WaitResult,
    enum_processes,
    get_process,
    get_hwnds_for_pid,
    get_pid_name,
    is_pid_running,
    wait_for_process,
    wait_for_process_exit,
    find_process_pid,
    is_process_running,
)
from .dll_injector import DllInjector
from .errors import InjectError, InjectorError, ErrorMixin
from .injection_probe import (
    ModuleProbe,
    find_loaded_module,
    normalize_path,
    pids_for_name,
    snapshot_modules,
    wait_for_module,
)
from .launcher import (
    StartRequest,
    LaunchContext,
    MigotoInjector,
)
from .locale import L

# 注意：ProcessWaiter 已迁到 omg.domain.inject.process_waiter，且仅在
# wait_for_process / wait_for_process_exit 内部本地导入，刻意不在此处再导出，
# 以免本包 __init__（启动路径会执行）在顶层拉起 multiprocessing 整条链。

__all__ = [
    # 进程监控
    "ProcessPriority",
    "WaitResult",
    "enum_processes",
    "get_process",
    "get_hwnds_for_pid",
    "get_pid_name",
    "is_pid_running",
    "wait_for_process",
    "wait_for_process_exit",
    "find_process_pid",
    "is_process_running",
    # 注入 / 启动
    "DllInjector",
    "StartRequest",
    "LaunchContext",
    "MigotoInjector",
    # 注入校验（本地独立通道）
    "ModuleProbe",
    "find_loaded_module",
    "normalize_path",
    "pids_for_name",
    "snapshot_modules",
    "wait_for_module",
    # 错误码
    "InjectError",
    "InjectorError",
    "ErrorMixin",
    # 本地化
    "L",
]
