"""
launcher.py — 启动 / 注入编排（移植自 XXMI-Launcher-2.2.1 MigotoPackage + MigotoInjector）

移植说明：
  - 保留 XXMI 的核心编排逻辑：run() 分流 Hook / Direct 两种注入路径，
    run_hook_injector（先挂 WH_CBT 钩子再启动游戏，早期+晚期双校验）、
    run_direct_injector（直接启动后注入 d3d11.dll + 额外 DLL）、
    wait_for_window（等游戏窗口出现）。
  - 去除对 XXMI 全局 Config / Events / PackageManager 的依赖：
      * 启动参数改由外部传入的 StartRequest 描述（调用方负责装配）；
      * 原 Events.Fire(...) 全部改为 logging，使引擎层不耦合 GUI / 事件总线。
  - 仅覆盖「注入 / 启动 / 监控」相关功能；包部署、签名校验、更新器等不在本文件。
"""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .dll_injector import DllInjector
from .errors import InjectorError
from .locale import L
from .process_tracker import ProcessPriority, wait_for_process, WaitResult

log = logging.getLogger(__name__)


@dataclass
class StartRequest:
    """一次启动 / 注入请求的外部描述（调用方负责装配，引擎层不再读全局配置）。"""
    process_name: str                       # 游戏进程名，如 "GenshinImpact.exe"
    exe_path: Path                         # 要启动的游戏 exe
    xxmi_dll_path: Path                    # 要注入的 d3d11.dll 路径
    start_args: List[str] = field(default_factory=list)
    work_dir: Optional[str] = None
    use_hook: bool = True                  # True=WH_CBT 钩子；False=直接注入
    custom_launch_cmd: Optional[str] = None  # 自定义启动命令（覆盖 exe_path）
    inject_dll_paths: List[Path] = field(default_factory=list)  # 额外要注入的 DLL
    process_priority: str = 'NORMAL_PRIORITY_CLASS'  # ProcessPriority 枚举名
    process_start_method: str = 'NATIVE'   # NATIVE / SHELL / MANUAL
    process_timeout: int = 30              # 等待游戏窗口出现的超时（秒）


@dataclass
class LaunchContext:
    """由 StartRequest 解析出的内部上下文（含已计算的进程 flags）。"""
    process_name: str
    start_exe_path: Path
    start_args: list
    work_dir: Optional[str]
    process_flags: int
    use_hook: bool
    custom_launch_cmd: Optional[str]
    xxmi_dll_path: Path
    inject_dll_paths: List[Path]
    process_timeout: int
    process_start_method: str


class MigotoInjector:
    def __init__(self, request: StartRequest, injector_path: Path):
        self.request = request
        self.injector_path = Path(injector_path)
        self.injector: Optional[DllInjector] = None
        self.context: LaunchContext = self.build_context(request)

    @classmethod
    def from_request(cls, request: StartRequest, injector_path: Path):
        return cls(request, injector_path)

    # ------------------------------------------------------------------
    # 上下文装配
    # ------------------------------------------------------------------
    @staticmethod
    def build_context(request: StartRequest) -> LaunchContext:
        try:
            priority = ProcessPriority[request.process_priority]
        except KeyError:
            priority = ProcessPriority.NORMAL_PRIORITY_CLASS

        process_flags = (subprocess.CREATE_NEW_CONSOLE |
                        subprocess.CREATE_DEFAULT_ERROR_MODE |
                        priority.get_process_flag())

        return LaunchContext(
            process_name=request.process_name,
            start_exe_path=Path(request.exe_path),
            start_args=list(request.start_args),
            work_dir=request.work_dir,
            process_flags=process_flags,
            use_hook=request.use_hook,
            custom_launch_cmd=request.custom_launch_cmd,
            xxmi_dll_path=Path(request.xxmi_dll_path),
            inject_dll_paths=list(request.inject_dll_paths),
            process_timeout=request.process_timeout,
            process_start_method=request.process_start_method,
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(self):
        self.injector = DllInjector(
            injector_lib_path=self.injector_path,
            load_hook=self.context.use_hook,
            load_inject=not self.context.use_hook or len(self.context.inject_dll_paths) > 0,
        )

        if self.context.use_hook:
            self.run_hook_injector()
        else:
            self.run_direct_injector()

    # ------------------------------------------------------------------
    # 监控：等待游戏窗口出现
    # ------------------------------------------------------------------
    @staticmethod
    def wait_for_window(context: LaunchContext, injection_verified: bool):
        log.info('Waiting for game process window: %s', context.process_name)
        result, _pid = wait_for_process(
            context.process_name, with_window=True,
            timeout=context.process_timeout, check_visibility=True)

        if result == WaitResult.Timeout:
            if injection_verified:
                raise InjectorError(L('error_migoto_game_detection_timeout', """
                    Failed to detect window of game process {process_name}!

                    If game window takes more than {start_timeout} seconds to appear, adjust **Timeout** in **General Settings**.

                    If game crashed, try to follow the Crash Isolation Checklist.
                """).format(
                    process_name=context.process_name,
                    start_timeout=context.process_timeout,
                ))
            else:
                raise InjectorError(L('error_migoto_game_start_failed',
                                      'Failed to start {process_name}!').format(
                                          process_name=context.process_name))

    # ------------------------------------------------------------------
    # 路径 A：直接注入（SetWindowsHookEx / CreateRemoteThread）
    # ------------------------------------------------------------------
    def run_direct_injector(self):
        injector = self.injector
        context = self.context

        dll_paths: List[Path] = []
        if context.xxmi_dll_path and context.xxmi_dll_path.is_file():
            # 若 xxmi dll 已在额外列表里则不重复
            if context.xxmi_dll_path not in context.inject_dll_paths:
                dll_paths.append(context.xxmi_dll_path)
        dll_paths += list(context.inject_dll_paths)

        if dll_paths:
            dll_names = ', '.join([p.name for p in dll_paths])
            log.info('Injecting libraries into %s: %s', context.process_name, dll_names)
        else:
            log.info('No DLLs to inject; launching %s (bypass).', context.process_name)

        try:
            injector.open_process(
                start_method=context.process_start_method,
                exe_path=str(context.start_exe_path),
                work_dir=context.work_dir,
                start_args=context.start_args,
                process_flags=context.process_flags,
                process_name=context.process_name,
                dll_paths=dll_paths,
                cmd=context.custom_launch_cmd,
                inject_timeout=context.process_timeout,
            )
            self.wait_for_window(context, injection_verified=True)
        finally:
            injector.unload()

    # ------------------------------------------------------------------
    # 路径 B：Hook 模式（先钩子后启动，早期 + 晚期双校验）
    # ------------------------------------------------------------------
    def run_hook_injector(self):
        injector = self.injector
        context = self.context

        try:
            log.info('Setting up global windows hook for %s -> %s',
                     context.xxmi_dll_path.name, context.process_name)
            injector.hook_library(context.xxmi_dll_path, context.process_name)

            log.info('Starting game exe: %s', context.process_name)
            injector.open_process(
                start_method=context.process_start_method,
                exe_path=str(context.start_exe_path),
                work_dir=context.work_dir,
                start_args=context.start_args,
                process_flags=context.process_flags,
                process_name=context.process_name,
                dll_paths=context.inject_dll_paths,
                cmd=context.custom_launch_cmd,
                inject_timeout=context.process_timeout,
            )

            # 早期注入校验
            hooked = injector.wait_for_injection(5)
            if hooked:
                log.info(self._check_message('early', True, injector))

            self.wait_for_window(context, injection_verified=hooked)

            # 晚期注入校验
            log.info('Verifying hook for %s -> %s',
                     context.xxmi_dll_path.name, context.process_name)
            late_ok = injector.wait_for_injection(5)
            if late_ok:
                log.info(self._check_message('late', True, injector))
            elif injector.last_check_inconclusive():
                # 两条通道都取不到证（模块快照被拒绝）。这只能说明「无法确认」，
                # 不能断言注入失败——游戏窗口都出来了，按警告处理。
                log.warning(self._check_message('late', False, injector, inconclusive=True))
            elif not hooked:
                # 早、晚两次都确认未注入 —— 这才有必要报 error
                log.error(self._check_message('late', False, injector))
            else:
                # 早期成功 / 晚期失败：DLL 多半是运行中卸载（3DMigoto 报错退出或
                # 游戏重启），降级为 warning，不再伪装成「注入失败」。
                log.warning(self._check_message('late', False, injector))

        finally:
            injector.unhook_library()
            injector.unload()

    # ------------------------------------------------------------------
    # 校验日志
    # ------------------------------------------------------------------
    def _check_message(self, phase: str, ok: bool, injector,
                       inconclusive: bool = False) -> str:
        """拼装一次校验的结论 + 双通道诊断明细。

        3dmloader 的原生判定对模块路径做逐字节比较，短名 / 大小写 / 分隔符差异
        都会造成假阴性；此时以本地模块校验为准，并显式说明是哪一侧给出的结论，
        避免「明明注入成功却打印 Failed to verify」的误导。
        """
        dll_name = self.context.xxmi_dll_path.name
        process_name = self.context.process_name
        detail = injector.describe_last_check()

        if ok:
            if injector.last_check_verified_by_probe():
                return (f'Successfully passed {phase} {dll_name} -> {process_name} hook check '
                        f'(native 3dmloader check was a false negative, module probe confirmed '
                        f'the DLL is loaded; {detail})')
            return f'Successfully passed {phase} {dll_name} -> {process_name} hook check!'

        if inconclusive:
            return (f'Unable to verify {dll_name} -> {process_name} hook ({phase} check): '
                    f'module table is not accessible (usually insufficient privileges against '
                    f'a protected game process), injection state unknown. '
                    f'Try running OMGLite as administrator. {detail}')

        return (f'Failed to verify {dll_name} -> {process_name} hook ({phase} check)! '
                f'{detail}')
