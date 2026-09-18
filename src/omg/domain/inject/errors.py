"""
errors.py — 注入错误码（移植自 XXMI-Launcher-2.2.1 DllInjector.InjectError）

将 XXMI 内嵌在 DllInjector 中的 ErrorMixin / InjectError 抽成独立模块，
便于 dll_injector 与上层编排复用，也符合「不塞在一个文件里」的拆分要求。
"""

from enum import Enum

from .locale import L


class ErrorMixin:
    """带数字错误码的枚举成员基类，兼容 XXMI 的 ErrorMixin 行为。"""

    UNKNOWN_ERROR_CODE = (-1, L('error_unknown_error_code', 'Unknown error code {error_code}'))

    def __init__(self, code: int, message: str):
        self.code = code
        self._message = message

    def msg(self) -> str:
        return self._message

    def format(self, **kwargs) -> str:
        return self._message.format(**kwargs)

    @classmethod
    def from_code(cls, code: int):
        """按数字码查找枚举成员；找不到返回 UNKNOWN_ERROR_CODE。"""
        for member in cls:
            if member.code == code:
                return member
        return cls.UNKNOWN_ERROR_CODE


class InjectError(ErrorMixin, Enum):
    """3dmloader.dll 的 Inject 导出函数返回的错误码映射。"""
    PROCESS_NOT_FOUND = (100, L('error_dll_inject_process_not_found', 'Process {pid} not found'))
    INVALID_DLL_PATH = (110, L('error_dll_inject_invalid_dll_path', 'Invalid DLL path {dll_path}'))
    KERNEL32_FAIL = (120, L('error_dll_inject_kernel32_fail', 'Failed to resolve kernel32.dll'))
    LOADLIBRARY_FAIL = (130, L('error_dll_inject_loadlibrary_fail', 'Failed to resolve LoadLibraryW'))
    REMOTE_ALLOC_FAIL = (200, L('error_dll_inject_remote_alloc_fail', 'Failed to allocate remote memory'))
    WRITE_MEMORY_FAIL = (300, L('error_dll_inject_write_memory_fail', 'Failed to write DLL path to process memory'))
    THREAD_FAIL = (400, L('error_dll_inject_thread_fail', 'Failed to create remote thread'))
    THREAD_TIMEOUT = (500, L('error_dll_inject_thread_timeout', 'Injection thread timed out'))
    THREAD_WAIT_FAIL = (510, L('error_dll_inject_thread_wait_fail', 'Injection thread wait failed'))
    INJECTION_FAILED = (600, L('error_dll_inject_injection_failed', 'DLL injection failed'))
    UNKNOWN_ERROR = (700, L('error_dll_inject_unknown_error', 'Unknown low level error'))


class InjectorError(Exception):
    """注入器层面的统一异常（区别于底层 InjectError 枚举）。"""
    pass
