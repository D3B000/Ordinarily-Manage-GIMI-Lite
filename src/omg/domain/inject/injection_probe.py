"""
injection_probe.py — 注入结果的本地独立校验（不依赖 3dmloader.dll 的黑盒判定）

背景
----
``3dmloader.dll!WaitForInjection`` 的语义经反汇编 + 注入实验确认如下：

    for i in range(timeout):
        if check(process_name, dll_path): return 0   # 视为已注入
        Sleep(1000)                                  # 固定 1 秒粒度
    return 1

``check()`` 的流程是：快照枚举所有同名进程 -> 对每个进程用
``CreateToolhelp32Snapshot(SNAPMODULE)`` 枚举已加载模块 -> 要求某个模块的
``szExePath`` **字符串与传入路径完全一致**才算命中。

这意味着任何一个路径形变都会造成假阴性（false negative）：

  - 8.3 短路径（``G:\\XXX~1\\d3d11.dll`` vs ``G:\\My Mods\\d3d11.dll``）
  - 盘符 / 目录大小写差异
  - 未展开的 ``..``、尾部斜杠、混合分隔符 ``/`` 与 ``\\``
  - NT/UNC 前缀（``\\\\?\\``、``\\\\??\\``）
  - 符号链接 / junction / subst 映射盘
  - 目标进程为本机位数不同的 WOW64 进程（``SNAPMODULE`` 取不到模块）

表现就是「DLL 确实注入成功、mod 也生效」，但上层仍打印
``Failed to verify d3d11.dll -> Game.exe hook!``。

本模块提供**第二条独立校验通道**：直接从 Windows 侧读取目标进程的模块表，
路径比较前先做规范化（短名展开 + 大小写折叠 + 分隔符归一），再按需放宽到
「文件名相同」的兜底判定。任一侧能证明 DLL 已就位，注入就应视为成功。

公开 API
--------
  - snapshot_modules(pid)          -> (modules, error)
  - find_loaded_module(name, dll)  -> ModuleProbe
  - wait_for_module(name, dll, ..) -> ModuleProbe
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Tuple

from .process_tracker import enum_processes

# ---------------------------------------------------------------------------
# Win32 常量 / 结构体
# ---------------------------------------------------------------------------
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MAX_MODULE_NAME32 = 255
MAX_PATH = 260


class _MODULEENTRY32W(ctypes.Structure):
    """MODULEENTRY32W（x64 下 sizeof == 1080）。"""

    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", ctypes.c_wchar * MAX_PATH),
    ]


def _kernel32():
    return ctypes.windll.kernel32


# ---------------------------------------------------------------------------
# 路径规范化
# ---------------------------------------------------------------------------
def long_path(path: str) -> str:
    """把 8.3 短名还原为长名（失败时原样返回）。"""
    k32 = _kernel32()
    try:
        k32.GetLongPathNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
        k32.GetLongPathNameW.restype = wt.DWORD
        buf = ctypes.create_unicode_buffer(4096)
        length = k32.GetLongPathNameW(str(path), buf, 4096)
        if 0 < length <= 4096:
            return buf.value
    except Exception:
        pass
    return str(path)


def normalize_path(path: str) -> str:
    """路径归一化：长短名展开 + 统一分隔符 + 去 NT 前缀 + 大小写折叠。

    仅用于比较，不用于任何文件系统操作。
    """
    if not path:
        return ""
    text = str(path).replace("/", "\\")
    text = text.strip()
    # 去掉 \\?\ / \\?？\ 之类的 NT 前缀后再展开长短名
    stripped = text
    if stripped.startswith("\\\\?\\"):
        stripped = stripped[4:]
    try:
        stripped = long_path(stripped)
    except Exception:
        pass
    try:
        stripped = os.path.normpath(stripped)
    except Exception:
        pass
    if stripped.startswith("\\\\?\\"):
        stripped = stripped[4:]
    return stripped.casefold()


def _basename_fold(path: str) -> str:
    return PureWindowsPath(str(path).replace("/", "\\")).name.casefold()


# ---------------------------------------------------------------------------
# 模块枚举
# ---------------------------------------------------------------------------
def snapshot_modules(pid: int) -> Tuple[List[Tuple[str, str]], int]:
    """枚举某进程已加载模块的 ``(szExePath, szModule)`` 列表。

    返回 ``(modules, last_error)``；``modules`` 为空且 ``last_error != 0``
    表示快照 / 枚举失败（常见于位数不匹配、目标进程受保护或权限不足）。
    """
    k32 = _kernel32()
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32FirstW.restype = wt.BOOL
    k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32NextW.restype = wt.BOOL

    # SNAPMODULE 覆盖同位数进程，SNAPMODULE32 覆盖 WOW64 进程，二者并用
    flags = TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
    snap = k32.CreateToolhelp32Snapshot(flags, int(pid))
    if not snap or snap == INVALID_HANDLE_VALUE:
        return [], ctypes.get_last_error()

    modules: List[Tuple[str, str]] = []
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k32.Module32FirstW(snap, ctypes.byref(entry)):
            return modules, ctypes.get_last_error()
        while True:
            modules.append((entry.szExePath, entry.szModule))
            if not k32.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return modules, 0


def pids_for_name(process_name: str) -> List[int]:
    """返回所有匹配进程名的 PID（支持传入带路径的进程名）。"""
    target = _basename_fold(process_name)
    pids: List[int] = []
    for pid, name in enum_processes():
        if name.casefold() == target:
            pids.append(pid)
    return pids


# ---------------------------------------------------------------------------
# 校验结果
# ---------------------------------------------------------------------------
@dataclass
class ModuleProbe:
    """一次本地模块校验的完整结果（含诊断信息）。"""

    found: bool = False
    pid: Optional[int] = None
    module_path: str = ""
    reason: str = "not_checked"
    scanned_pids: List[int] = field(default_factory=list)
    last_error: int = 0
    strict_match: bool = False          # True=规范化后路径一致；False=仅文件名命中
    candidates: List[str] = field(default_factory=list)

    @property
    def inconclusive(self) -> bool:
        """True 表示「无法取证」而非「确认未注入」。

        典型成因：模块快照取不到句柄（位数不匹配、目标进程受保护、权限不足）。
        此时不该断言注入失败——上层应降级为警告而不是报错。
        """
        return not self.found and self.reason == 'module_snapshot_failed'

    def describe(self) -> str:
        """供日志使用的单行诊断文本。"""
        parts = [
            f"found={self.found}",
            f"pid={self.pid}",
            f"module={self.module_path or '-'}",
            f"match={'strict' if self.strict_match else ('name-only' if self.found else 'none')}",
            f"reason={self.reason}",
            f"scanned_pids={self.scanned_pids}",
        ]
        if self.last_error:
            parts.append(f"win32_error={self.last_error}")
        if self.candidates:
            parts.append(f"target_modules={self.candidates[:4]}")
        return ", ".join(parts)


def find_loaded_module(process_name: str, dll_path: str | Path,
                       allow_name_only: bool = False) -> ModuleProbe:
    """在所有同名进程中查找目标 DLL 是否已加载。

    判定顺序：
      1. 原样字符串一致（等价于 3dmloader 的判定，便于对照）
      2. 规范化后的完整路径一致（长短名 / 大小写 / 分隔符 / NT 前缀差异都不影响）
      3. **可选**文件名一致（``allow_name_only=True``）

    第 3 条默认关闭且**必须保持关闭**：DX11 游戏几乎必然加载
    ``C:\\Windows\\System32\\d3d11.dll``，仅凭文件名判定会把系统 DLL 当成
    注入成功，反而掩盖真正的失败。所有同名模块一律记入 ``candidates`` 供诊断。
    """
    probe = ModuleProbe(reason="no_target_process")
    want_path = str(dll_path)
    want_norm = normalize_path(want_path)
    want_name = _basename_fold(want_path)

    pids = pids_for_name(process_name)
    probe.scanned_pids = pids
    if not pids:
        return probe

    name_only_hit: Optional[ModuleProbe] = None
    probe.reason = "module_not_found"
    snapshot_failed = False

    for pid in pids:
        modules, err = snapshot_modules(pid)
        if err and not modules:
            probe.last_error = err
            snapshot_failed = True
            continue

        for module_path, module_name in modules:
            if _basename_fold(module_name or module_path) != want_name:
                continue
            if module_path not in probe.candidates:
                probe.candidates.append(module_path)

            if module_path == want_path or normalize_path(module_path) == want_norm:
                probe.found = True
                probe.pid = pid
                probe.module_path = module_path
                probe.strict_match = True
                probe.reason = "path_match"
                return probe

            if name_only_hit is None:
                name_only_hit = ModuleProbe(
                    found=True, pid=pid, module_path=module_path,
                    reason="name_match", scanned_pids=pids,
                    strict_match=False, last_error=err,
                    candidates=list(probe.candidates),
                )

    if snapshot_failed and not probe.candidates:
        # 一个模块都没读到，且至少一次快照失败 -> 无法取证
        probe.reason = "module_snapshot_failed"
        return probe

    if allow_name_only and name_only_hit is not None:
        return name_only_hit

    if probe.candidates:
        probe.reason = "path_mismatch"
    return probe


def wait_for_module(process_name: str, dll_path: str | Path,
                    timeout: float = 1.0, interval: float = 0.25) -> ModuleProbe:
    """在 timeout 秒内以细粒度轮询本地模块校验。"""
    deadline = time.time() + max(0.0, timeout)
    probe = find_loaded_module(process_name, dll_path)
    while not probe.found and time.time() < deadline:
        time.sleep(interval)
        probe = find_loaded_module(process_name, dll_path)
    return probe
