"""
dll_injector.py — 3DMigoto DLL 注入 / 进程启动（移植自 XXMI-Launcher-2.2.1）

移植说明：
  - 完整保留 XXMI 的 DllInjector 类结构：load / unload / start_process /
    open_process / hook_library / wait_for_injection / unhook_library / inject_libraries。
  - 去除了对 core.locale_manager.L 的硬编码依赖，改走本包 locale.L 垫片。
  - get_short_path 改用 ctypes（GetShortPathNameW）替代 pywin32.win32api。
  - inject_libraries 的进程查找改用本包 process_tracker.enum_processes（ctypes），
    不再依赖 psutil。
  - 启动方式支持 NATIVE（subprocess.Popen）/ SHELL（StartProcess）/ MANUAL（仅等待）。
"""

import logging
import time
import subprocess
import ctypes as ct
import ctypes.wintypes as wt

from pathlib import Path
from typing import List, Optional

from .locale import L
from .errors import InjectError, InjectorError
from .process_tracker import enum_processes
from .injection_probe import ModuleProbe, find_loaded_module, wait_for_module

log = logging.getLogger(__name__)

# 3dmloader 的 WaitForInjection 判定为「未注入」时，本地模块校验的宽限轮询时长。
# 它的 Sleep 粒度是固定 1 秒，可能在 DLL 尚未完成初始化时就放弃，这里补一小段
# 细粒度轮询兜底（对正常情况下的成功判定零开销）。
PROBE_GRACE_SECONDS = 1.0


class DllInjector:
    def __init__(self, injector_lib_path, load_hook: bool = False, load_inject: bool = False):
        self.lib = None
        self.dll_path = None
        self.target_process = None
        self.hook = None
        self.mutex = None

        self.load(Path(injector_lib_path).resolve(), load_hook, load_inject)

        # 最近一次 wait_for_injection 的双通道判定明细（供上层打诊断日志）
        self.last_check: dict = {
            'native': None,      # 3dmloader WaitForInjection 是否返回成功
            'native_code': None,  # 原始返回码（0=成功，其余见 3dmloader 约定）
            'probe': None,       # ModuleProbe：本地模块表校验结果
        }

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _cstr(value) -> str:
        """把 ``c_wchar_p`` 包装或普通 ``str`` 统一取回 Python 字符串。

        ``hook_library`` 里存的是 ``wt.LPCWSTR`` 实例，直接 ``str()`` 会得到
        ctypes 对象的 repr（``<ctypes...>``），必须经 ``.value`` 取值。
        """
        return value.value if hasattr(value, 'value') else str(value)

    @staticmethod
    def get_short_path(path: Path) -> str:
        """返回文件的 8.3 短路径（ctypes 实现，替代 pywin32）。"""
        kernel32 = ct.windll.kernel32
        kernel32.GetShortPathNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
        kernel32.GetShortPathNameW.restype = wt.DWORD

        long_path = str(path.resolve())
        buf = ct.create_unicode_buffer(1024)
        length = kernel32.GetShortPathNameW(long_path, buf, 1024)
        if length == 0:
            return long_path
        if length > 1024:
            buf = ct.create_unicode_buffer(length + 1)
            kernel32.GetShortPathNameW(long_path, buf, length + 1)
        return buf.value

    # ------------------------------------------------------------------
    # 加载 / 卸载
    # ------------------------------------------------------------------
    def load(self, injector_lib_path, load_hook: bool = False, load_inject: bool = False):
        if not injector_lib_path.exists():
            raise InjectorError(L('error_dll_injector_file_not_found',
                                  'Injector file not found: {path}!').format(path=injector_lib_path))

        try:
            self.lib = ct.cdll.LoadLibrary(str(injector_lib_path))
        except Exception as e:
            raise InjectorError(L('error_dll_injector_load_failed',
                                  'Failed to load injector library!')) from e

        try:
            if load_hook:
                self.lib.HookLibrary.argtypes = (wt.LPCWSTR, ct.POINTER(wt.HHOOK), ct.POINTER(wt.HANDLE))
                self.lib.HookLibrary.restype = ct.c_int

                self.lib.WaitForInjection.argtypes = (wt.LPCWSTR, wt.LPCWSTR, ct.c_int)
                self.lib.WaitForInjection.restype = ct.c_int

                self.lib.UnhookLibrary.argtypes = (ct.POINTER(wt.HHOOK), ct.POINTER(wt.HANDLE))
                self.lib.UnhookLibrary.restype = ct.c_int

            if load_inject:
                try:
                    self.lib.Inject.argtypes = (wt.DWORD, wt.LPCWSTR, ct.c_int)
                    self.lib.Inject.restype = ct.c_int
                except AttributeError as e:
                    raise InjectorError(L('error_old_3dmloader', """
                        Provided **3dmloader.dll** is too old, it's missing **Inject** method.

                        Please put **3dmloader.dll** from **v0.7.5+** to `Packages/XXMI` or use **Hook** method (without **Inject Libraries**).
                    """)) from e

        except Exception as e:
            try:
                self.unload()
            except Exception:
                pass
            raise InjectorError(L('error_dll_injector_setup_failed', """
                Failed to setup injector library!

                Error: {error_text}
            """).format(error_text=str(e)))

    def unload(self):
        kernel32 = ct.WinDLL('kernel32', use_last_error=True)
        kernel32.FreeLibrary.argtypes = [wt.HMODULE]
        result = kernel32.FreeLibrary(self.lib._handle)
        if result == 0:
            raise InjectorError(L('error_dll_injector_unload_failed',
                                  'Failed to unload injector library!'))

    # ------------------------------------------------------------------
    # 启动游戏进程
    # ------------------------------------------------------------------
    def start_process(self, exe_path: str, work_dir: Optional[str] = None,
                      start_args: str = ''):
        if work_dir is None:
            work_dir = ''

        result = self.lib.StartProcess(
            wt.LPCWSTR(exe_path),
            wt.LPCWSTR(work_dir),
            wt.LPCWSTR(start_args)
        )

        if result != 0:
            codes = {
                0:  L('dll_injector_shell_error_out_of_memory', 'The operating system is out of memory/resources'),
                2:  L('dll_injector_shell_error_file_not_found', 'File not found'),
                3:  L('dll_injector_shell_error_path_not_found', 'Path not found'),
                5:  L('dll_injector_shell_error_access_denied', 'Access denied'),
                11: L('dll_injector_shell_error_not_win32_app', '.exe file is invalid or not a Win32 app'),
                26: L('dll_injector_shell_error_sharing_violation', 'Sharing violation'),
                31: L('dll_injector_shell_error_no_app_association', 'No application is associated with the file'),
                32: L('dll_injector_shell_error_incomplete_app_association', 'File association is incomplete'),
            }
            error_text = codes.get(
                result,
                L('dll_injector_unknown_shell_error_code', 'Unknown ShellExecute error code {error_code}').format(error_code=result))
            raise InjectorError(L('error_dll_injector_process_start_failed',
                                  'Failed to start {process_name}: {error_text}!').format(
                                      process_name=Path(exe_path).name, error_text=error_text))

    def open_process(self,
                     start_method: str,
                     exe_path: Optional[str],
                     work_dir: Optional[str],
                     start_args: Optional[List[str]],
                     process_flags: Optional[int],
                     process_name: Optional[str] = None,
                     dll_paths: Optional[List[Path]] = None,
                     cmd: Optional[str] = None,
                     inject_timeout: int = 15):

        log.debug('Starting game process %s using %s method: exe_path=%s, work_dir=%s, '
                  'start_args=%s, process_flags=%s, cmd=%s, dll_paths=%s',
                  process_name, start_method, exe_path, work_dir, start_args,
                  process_flags, cmd, dll_paths)

        start_method = start_method.upper()

        if start_method == 'NATIVE':
            if cmd is None:
                cmd = [exe_path] + (start_args or [])
                use_shell = False
            else:
                use_shell = True
            subprocess.Popen(cmd, creationflags=process_flags or 0,
                             cwd=work_dir, shell=use_shell)

        elif start_method == 'SHELL':
            if cmd is None:
                self.start_process(exe_path, work_dir, ' '.join(start_args or []))
            else:
                self.start_process('cmd.exe', None, f'/C "{cmd}"')

        elif start_method == 'MANUAL':
            log.debug('Waiting for user to start the game process %s...', process_name)

        else:
            raise InjectorError(L('error_dll_injector_unknown_start_method',
                                  'Unknown process start method `{start_method}`!').format(start_method=start_method))

        if dll_paths:
            pid = self.inject_libraries(dll_paths, process_name, timeout=inject_timeout)
            if pid == -1:
                raise InjectorError(L('error_dll_injector_injection_failed',
                                      'Failed to inject {dll_paths}!').format(dll_paths=str(dll_paths)))

    # ------------------------------------------------------------------
    # WH_CBT 钩子（Hook 模式）
    # ------------------------------------------------------------------
    def hook_library(self, dll_path: Path, target_process: str):
        if self.hook is not None:
            dll_path = self.dll_path
            self.unhook_library()
            raise InjectorError(L('error_dll_injector_unhook_failed',
                                  'Invalid injector usage: {dll_path} was not unhooked!').format(dll_path=str(dll_path)))

        self.dll_path = wt.LPCWSTR(str(dll_path.resolve()))
        self.target_process = wt.LPCWSTR(target_process)
        self.hook = wt.HHOOK()
        self.mutex = wt.HANDLE()

        result = self.lib.HookLibrary(self.dll_path, ct.byref(self.hook), ct.byref(self.mutex))

        if result == 100:
            raise InjectorError(L('error_dll_injector_another_instance', 'Another instance of 3DMigotoLoader is running!'))
        elif result == 200:
            raise InjectorError(L('error_dll_injector_failed_to_load_dll', 'Failed to load {dll_path}!').format(dll_path=str(dll_path)))
        elif result == 300:
            raise InjectorError(L('error_dll_injector_missing_entry_point', 'Library {dll_path} is missing expected entry point!').format(dll_path=str(dll_path)))
        elif result == 400:
            raise InjectorError(L('error_dll_injector_hook_setup_failed', 'Failed to setup windows hook for {dll_path}!').format(dll_path=str(dll_path)))
        elif result != 0:
            raise InjectorError(L('error_dll_injector_unknown_hook_error', 'Unknown error while hooking {dll_path}!').format(dll_path=str(dll_path)))
        if not bool(self.hook):
            raise InjectorError(L('error_dll_injector_hook_is_null', 'Hook is NULL for {dll_path}!').format(dll_path=str(dll_path)))

    def wait_for_injection(self, timeout: int = 15) -> bool:
        """判定目标进程里 DLL 是否已就位。

        返回 True 表示「已注入」。判定走两条通道：

          1. ``3dmloader.dll!WaitForInjection`` —— 与 XXMI 完全一致的原生判定。
             它对模块路径做**逐字节字符串比较**，因此 8.3 短名 / 大小写 /
             分隔符 / 符号链接等任何路径形变都会造成假阴性。
          2. 本地模块表校验（``injection_probe``）—— 直接读目标的
             ``SNAPMODULE`` 模块列表，路径先规范化再比较。

        只要任一侧为阳性即视为注入成功；两侧皆阴才判定失败。明细写入
        :attr:`last_check`，供上层输出诊断日志。
        """
        if self.dll_path is None:
            raise InjectorError(L('error_dll_injector_path_not_defined', 'Invalid injector usage: dll path is not defined!'))
        if self.target_process is None:
            raise InjectorError(L('error_dll_injector_process_not_defined', 'Invalid injector usage: target process is not defined!'))
        if self.hook is None:
            raise InjectorError(L('error_dll_injector_dll_not_hooked', 'Invalid injector usage: dll is not hooked!'))

        result = self.lib.WaitForInjection(self.dll_path, self.target_process, ct.c_int(timeout))
        native_ok = (result == 0)
        self.last_check = {'native': native_ok, 'native_code': int(result), 'probe': None}

        if native_ok:
            return True

        # 原生判定失败 —— 用本地模块表复核，避免把「已注入」误报成「未注入」
        process_name = self._cstr(self.target_process)
        dll_path = self._cstr(self.dll_path)
        probe = wait_for_module(process_name, dll_path, timeout=PROBE_GRACE_SECONDS)
        self.last_check['probe'] = probe

        if probe.found:
            log.debug('Native hook check failed (code=%s) but module %s is loaded in '
                      'pid %s: %s', result, dll_path, probe.pid, probe.describe())
        return probe.found

    def describe_last_check(self) -> str:
        """最近一次注入校验的双通道明细（供日志 / 排障）。"""
        native = self.last_check.get('native')
        native_code = self.last_check.get('native_code')
        probe: Optional[ModuleProbe] = self.last_check.get('probe')
        if native is None:
            return 'no check performed'
        text = f'native={native} (code={native_code})'
        if probe is not None:
            text += f'; probe[{probe.describe()}]'
        return text

    def last_check_verified_by_probe(self) -> bool:
        """最近一次判定是否由本地模块通道「救回」（原生判定为假阴性）。"""
        native = self.last_check.get('native')
        probe: Optional[ModuleProbe] = self.last_check.get('probe')
        return native is False and probe is not None and probe.found

    def last_check_inconclusive(self) -> bool:
        """最近一次「失败」是否其实只是取不到证（无法确认），而非确认未注入。"""
        native = self.last_check.get('native')
        probe: Optional[ModuleProbe] = self.last_check.get('probe')
        if native is False and probe is None:
            return False
        return native is False and probe is not None and probe.inconclusive

    def unhook_library(self) -> bool:
        if self.hook is None and self.mutex is None:
            return True
        result = self.lib.UnhookLibrary(ct.byref(self.hook), ct.byref(self.mutex))
        self.dll_path = None
        self.target_process = None
        self.hook = None
        self.mutex = None
        return result == 0

    # ------------------------------------------------------------------
    # 直接注入（CreateRemoteThread 模式）
    # ------------------------------------------------------------------
    def inject_libraries(self, dll_paths: List[Path], process_name: str = None,
                         pid: int = None, timeout: int = 15) -> int:
        """轮询查找目标进程，注入 dll_paths 中所有 DLL。成功返回 pid，超时返回 -1。"""
        time_start = time.time()

        while True:
            current_time = time.time()
            if timeout != -1 and current_time - time_start >= timeout:
                return -1

            target_pid = None
            for cand_pid, cand_name in enum_processes():
                if (process_name and cand_name.lower() == process_name.lower()) or \
                   (pid is not None and cand_pid == pid):
                    target_pid = cand_pid
                    break

            if target_pid is not None:
                for dll_path in dll_paths:
                    wide_dll_path = wt.LPCWSTR(str(dll_path.resolve()))
                    result = self.lib.Inject(target_pid, wide_dll_path, ct.c_int(timeout))
                    if result != 0:
                        inject_error = InjectError.from_code(result)
                        error_text = inject_error.format(pid=target_pid, dll_path=dll_path, error_code=result)
                        if dll_path.name == 'd3d11.dll' and len(dll_paths) == 1:
                            raise InjectorError(L('error_dll_injector_failed', """
                                Failed to inject {dll_path}:
                                {error_text}!
                            """).format(dll_path=dll_path, error_text=error_text))
                        else:
                            raise InjectorError(L('error_dll_injector_extra_library_failed', """
                                Failed to inject extra library {dll_path}:
                                {error_text}!
                                Please check Advanced Settings -> Inject Libraries.
                            """).format(dll_path=dll_path, error_text=error_text))
                    else:
                        log.debug('Successfully injected DLL to process %s (PID: %s): %s',
                                  process_name, target_pid, dll_path)
                return target_pid

            time.sleep(0.1)
