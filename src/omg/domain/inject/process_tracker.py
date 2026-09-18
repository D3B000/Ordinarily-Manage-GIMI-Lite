"""
process_tracker.py — 进程监控（移植自 XXMI-Launcher-2.2.1）

移植说明：
  - 完整保留 XXMI 的「监控跑在独立 multiprocessing 子进程（ProcessWaiter）」设计，
    主线程不阻塞，结果通过共享 ``Value`` 返回。
  - OS 底层改用 ctypes 实现（CreateToolhelp32Snapshot 枚举进程、EnumWindows 枚举窗口），
    以贴合 OMGLite 既有 loader.py 的风格，且不引入 psutil / pywin32 依赖。
  - 返回约定：(WaitResult, pid)；pid 为负表示异常（-100 未找到 / -200 超时 / -300 被杀）。

公开 API：
  - get_process(name=None, pid=None) -> psutil 风格的进程信息元组 (pid, name) 或 None
  - get_hwnds_for_pid(pid, check_visibility=False) -> [hwnd, ...]
  - wait_for_process(name, timeout, with_window, cmd, inject_dll, check_visibility)
  - wait_for_process_exit(name, timeout, kill_timeout)
  - ProcessPriority / WaitResult 枚举
"""

import logging
import time
import ctypes
import ctypes.wintypes as wt
from enum import Enum
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 常量 / 结构体
# ---------------------------------------------------------------------------
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value  # -1

# 进程判活：只要 SYNCHRONIZE 权限即可 WaitForSingleObject，权限最小化
PROCESS_SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32():
    return ctypes.windll.kernel32


def _user32():
    return ctypes.windll.user32


# ---------------------------------------------------------------------------
# 进程 / 窗口枚举（ctypes 实现，替代 psutil / pywin32）
# ---------------------------------------------------------------------------
def enum_processes() -> List[Tuple[int, str]]:
    """返回 [(pid, exe_name), ...]，等价于 psutil.process_iter 的最小实现。"""
    kernel32 = _kernel32()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or snap is None:
        return []

    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    out: List[Tuple[int, str]] = []
    try:
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out.append((int(pe.th32ProcessID), pe.szExeFile))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return out


def get_process(process_id: Optional[int] = None,
                process_name: Optional[str] = None) -> Optional[Tuple[int, str]]:
    """按名或 PID 查找进程，返回 (pid, name)；找不到返回 None。

    等价于 XXMI 的 get_process()，但用 ctypes 实现。
    """
    want_name = process_name.lower() if process_name else None
    for pid, name in enum_processes():
        if process_id is not None and pid == process_id:
            return pid, name
        if want_name is not None and name.lower() == want_name:
            return pid, name
    return None


def get_pid_name(process_id: int) -> Optional[str]:
    """按 PID 反查进程名。用于识别 PID 复用（原 PID 已被别的进程占用）。"""
    if not process_id or process_id <= 0:
        return None
    for pid, name in enum_processes():
        if pid == int(process_id):
            return name
    return None


def is_pid_running(process_id: Optional[int]) -> bool:
    """按 PID 判活：进程仍在运行返回 True。

    与 ``is_process_running``（进程名快照）的关键差异：
    快照枚举会列出**已退出但句柄未关闭**的僵尸进程，而 ``OpenProcess`` 对
    这类进程会直接失败 —— 因此本函数能把僵尸正确识别为「已退出」。
    """
    if not process_id or process_id <= 0:
        return False

    kernel32 = _kernel32()
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD

    handle = kernel32.OpenProcess(PROCESS_SYNCHRONIZE, False, int(process_id))
    if not handle:
        return False
    try:
        # 进程对象在退出时变为 signaled；WAIT_TIMEOUT 表示还活着
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def get_hwnds_for_pid(pid: int, check_visibility: bool = False) -> List[int]:
    """返回某进程拥有的所有顶层窗口句柄；check_visibility=True 时仅保留可见且未最小化的窗口。"""
    user32 = _user32()
    hwnds: List[int] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        pid_buf = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
        if pid_buf.value == pid:
            if check_visibility and (
                not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd)
            ):
                return True
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return hwnds


# ---------------------------------------------------------------------------
# 枚举：进程优先级 / 等待结果
# ---------------------------------------------------------------------------
class ProcessPriority(Enum):
    IDLE_PRIORITY_CLASS = 'Low'
    BELOW_NORMAL_PRIORITY_CLASS = 'Below Normal'
    NORMAL_PRIORITY_CLASS = 'Normal'
    ABOVE_NORMAL_PRIORITY_CLASS = 'Above Normal'
    HIGH_PRIORITY_CLASS = 'High'
    REALTIME_PRIORITY_CLASS = 'Realtime'

    def get_process_flag(self):
        import subprocess
        return getattr(subprocess, self.name)


class WaitResult(Enum):
    Found = 0
    NotFound = -100
    Timeout = -200
    Terminated = -300


# ---------------------------------------------------------------------------
# 子进程等待器（核心轮询逻辑，跑在独立进程里）
# ---------------------------------------------------------------------------
# ProcessWaiter 已迁到独立的 omg.domain.inject.process_waiter 模块：它的基类
# multiprocessing.Process 会让 import 链拉起约 10 个标准库模块（含 pickle /
# copy / concurrent.futures），而这些只在用户「等待游戏进程」时才需要。因此这里
# 不在模块顶层 import，改为下方 wait_for_process / wait_for_process_exit 内部
# 本地导入，避免污染冷启动路径。


# ---------------------------------------------------------------------------
# 公开封装
# ---------------------------------------------------------------------------
def wait_for_process(process_name: str, timeout: int = 10,
                     with_window: bool = False, cmd=None,
                     inject_dll=None, check_visibility: bool = False
                     ) -> Tuple[WaitResult, int]:
    """等待进程出现（可选要求已出现窗口）。返回 (WaitResult, pid)。"""
    # 本地惰性导入：ProcessWaiter 依赖 multiprocessing（约 10 个标准库模块），
    # 仅在实际需要等待进程时才加载，避免污染冷启动路径。
    from .process_waiter import ProcessWaiter
    waiter = ProcessWaiter(process_name, timeout, with_window,
                           cmd=cmd, inject_dll=inject_dll,
                           check_visibility=check_visibility)
    waiter.start()
    waiter.join()
    result = int(waiter.data.value)
    if result < 0:
        return WaitResult(result), -1
    return WaitResult.Found, result


def wait_for_process_exit(process_name: str, timeout: int = 10,
                          kill_timeout: int = -1) -> Tuple[WaitResult, int]:
    """等待进程退出。kill_timeout>=0 时到点强杀。返回 (WaitResult, pid/-1)。"""
    # 本地惰性导入：ProcessWaiter 依赖 multiprocessing，仅在实际等待时加载。
    from .process_waiter import ProcessWaiter
    waiter = ProcessWaiter(process_name, timeout, wait_exit=True,
                           kill_timeout=kill_timeout)
    waiter.start()
    waiter.join()
    result = int(waiter.data.value)
    if result < 0:
        return WaitResult(result), -1
    return WaitResult.Found, result


def find_process_pid(name: str, timeout: int = 30) -> Optional[int]:
    """便捷封装：在 timeout 秒内轮询等待进程出现，返回 pid 或 None。"""
    result, pid = wait_for_process(name, timeout=timeout)
    return pid if result == WaitResult.Found else None


def is_process_running(name: str) -> bool:
    """单次快照判断进程是否存在（心跳 / 安全网检查用）。"""
    return get_process(process_name=name) is not None
