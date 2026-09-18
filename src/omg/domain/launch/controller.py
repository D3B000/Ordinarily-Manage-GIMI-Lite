"""
launch.controller — 主页「开始」按钮的业务编排（状态机 + 参数装配）。

设计（对应 home_start_design.md v2）：
  - 状态机：IDLE → INJECTING → DONE(disabled) / ERROR(可用重试)
  - toggle()：仅 IDLE / ERROR 响应；进入 INJECTING 后按钮由 home 层禁用
  - 预检（preflight）：路径 / d3dx.ini / 3dmloader.dll / d3d11.dll / 单实例 / 自定义命令
  - 装配：读 ConfigManager → 构造 LaunchTask（含 StartRequest），传入 Worker 线程

本类不直接触达任何 GUI 控件，只通过以下 Signal 与 UI 通信：
  - state_changed(state, detail) ：刷新图标 / 禁用态 / tooltip
  - log_line(line)              ：可选日志浮层
  - finished(ok, msg)           ：收尾（同时内部更新状态机）
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Signal, QTimer

from omg.core.paths import BINARIES_DIR
from omg.domain.inject.launcher import StartRequest
from omg.domain.inject.loader import find_game_exe
from omg.domain.inject.process_tracker import (
    get_pid_name,
    get_process,
    is_pid_running,
    is_process_running,
)

from .types import LaunchState, LaunchTask
from .worker import LaunchWorker

logger = logging.getLogger("OMG")


class LaunchController(QObject):
    """「开始」按钮的业务控制器。"""

    state_changed = Signal(str, str)   # (state, detail)
    log_line = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, config) -> None:
        """
        Args:
            config: ConfigManager 实例（omg.core.config.ConfigManager）
        """
        super().__init__()
        self._config = config
        self._state = LaunchState.IDLE
        self._worker: Optional[LaunchWorker] = None
        # DONE 态进程退出监控：游戏运行中禁用开始按钮，退出后自动回 IDLE
        self._active_target = ""
        # 注入成功后记录的游戏 PID。按 PID 判活可识别「已退出但句柄未关闭」
        # 的僵尸进程（快照枚举仍会列出它们，进程名匹配会被骗住）。
        # 为 0 表示未取得 PID，回退到进程名匹配。
        self._active_pid = 0
        self._exit_timer = QTimer(self)
        self._exit_timer.setInterval(1000)
        self._exit_timer.timeout.connect(self._on_exit_tick)

    # ------------------------------------------------------------------
    # UI 绑定
    # ------------------------------------------------------------------
    def bind(self, home_window) -> None:
        """把状态信号接到 home 窗口的回调上。"""
        self.state_changed.connect(home_window._on_launch_state)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def toggle(self) -> None:
        """开始按钮点击。仅 IDLE / ERROR 态响应（INJECTING 已禁用、DONE 待游戏退出）。"""
        if self._state in (LaunchState.INJECTING, LaunchState.DONE):
            return
        self._start()

    def is_busy(self) -> bool:
        """是否正处于注入中（用于 UI 在极端情况下二次防护）。"""
        return self._state == LaunchState.INJECTING

    # ------------------------------------------------------------------
    # 内部：启动流程
    # ------------------------------------------------------------------
    def _start(self) -> None:
        self._set_state(LaunchState.INJECTING, "正在注入…")
        ok, msg, task = self._build_task()
        if not ok:
            self._on_finished(False, msg)
            return
        self._active_target = task.d3dx_target
        self._active_pid = 0

        # 在 UI 线程装配完成后再交给工作线程，避免线程内读配置竞态
        self._worker = LaunchWorker(task)
        self._worker.state_changed.connect(self._on_worker_state)
        self._worker.log_line.connect(self.log_line)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_worker_state(self, state: str, detail: str) -> None:
        self.state_changed.emit(state, detail)

    def _on_finished(self, ok: bool, msg: str) -> None:
        if ok:
            self._set_state(LaunchState.DONE, msg or "注入完成，游戏运行中")
            self._active_pid = self._resolve_pid()
            self._start_exit_watch()
        else:
            self._set_state(LaunchState.ERROR, msg or "注入失败")
            self._stop_exit_watch()
        self.finished.emit(ok, msg)

    def _set_state(self, state: LaunchState, detail: str) -> None:
        self._state = state
        self.state_changed.emit(state.value, detail)

    # ------------------------------------------------------------------
    # 进程退出监控（DONE 态：游戏运行中禁用，退出后自动回 IDLE 可用）
    # ------------------------------------------------------------------
    def _start_exit_watch(self) -> None:
        """进入 DONE 后启动监控：游戏进程退出即自动回到 IDLE（按钮恢复可用）。"""
        if self._active_target:
            self._exit_timer.start()

    def _stop_exit_watch(self) -> None:
        """停止进程退出监控（失败 / 重新启动等场景）。"""
        self._exit_timer.stop()

    def _resolve_pid(self) -> int:
        """注入成功后记录游戏 PID，供退出监控按 PID 判活。

        注入完成时 ``wait_for_window`` 已确认进程存在，因此单次快照足够。
        取不到 PID（manual 模式 / 极端时序）时返回 0，监控回退到进程名匹配。
        """
        if not self._active_target:
            return 0
        found = get_process(process_name=self._active_target)
        return int(found[0]) if found else 0

    def _on_exit_tick(self) -> None:
        if not self._active_target:
            return

        if self._active_pid:
            # PID 判活 + 进程名复核：前者识别僵尸（句柄未关闭的已退进程），
            # 后者防止 PID 被系统复用后误判为「游戏还在」。
            name = get_pid_name(self._active_pid)
            alive = (
                is_pid_running(self._active_pid)
                and bool(name)
                and name.lower() == self._active_target.lower()
            )
        else:
            alive = is_process_running(self._active_target)

        if not alive:
            self._exit_timer.stop()
            self._active_pid = 0
            self._set_state(LaunchState.IDLE, "游戏已退出，可再次开始")

    # ------------------------------------------------------------------
    # 内部：装配 LaunchTask + 预检
    # ------------------------------------------------------------------
    def _build_task(self) -> Tuple[bool, str, Optional[LaunchTask]]:
        cfg = self._config
        gimi_dir = (cfg.get("gimi_folder") or "").strip()
        game_dir = (cfg.get("game_folder") or "").strip()
        hunting = int(cfg.get("hunting") or 0)
        warning = int(cfg.get("warning") or 0)
        inject_method = (cfg.get("inject_method") or "hook").lower()
        launch_method = (cfg.get("launch_method") or "native").lower()
        custom_enabled = bool(cfg.get("custom_launch_enabled"))
        custom_cmd = (cfg.get("custom_launch_cmd") or "").strip()
        custom_inject_mode = (cfg.get("custom_launch_inject_mode") or "hook").lower()

        # ============ 预检 ============
        if not game_dir:
            return False, "未配置游戏目录（game_folder 为空）", None
        if not os.path.isdir(game_dir):
            return False, f"游戏目录不存在: {game_dir}", None

        game_exe, target = find_game_exe(Path(game_dir))
        if game_exe is None or target is None:
            return False, f"在 {game_dir} 中未找到 .exe 文件!", None

        if not gimi_dir:
            return False, "未配置 GIMI 目录（gimi_folder 为空）", None
        if not os.path.isdir(gimi_dir):
            return False, f"GIMI 目录不存在: {gimi_dir}", None

        gimi_ini = Path(gimi_dir) / "d3dx.ini"
        if not gimi_ini.is_file():
            return False, f"找不到 d3dx.ini: {gimi_ini}", None

        # 3dmloader.dll 位于 resources/binaries/loader/ 子目录下
        injector_path = Path(BINARIES_DIR) / "loader" / "3dmloader.dll"
        if not injector_path.is_file():
            # 回退：直接位于 binaries/ 下（历史布局兼容）
            fallback = Path(BINARIES_DIR) / "3dmloader.dll"
            if fallback.is_file():
                injector_path = fallback
        if not injector_path.is_file():
            return False, f"找不到 3dmloader.dll: {injector_path}", None

        # v2 关键变更：启动阶段不再复制 d3d11.dll，假定其已在 GIMI 内就位
        d3d11_path = Path(gimi_dir) / "d3d11.dll"
        if not d3d11_path.is_file():
            return False, "GIMI 内未找到 d3d11.dll，请先执行 d3d11 加工", None

        if is_process_running(target):
            return False, f"目标进程 {target} 已在运行", None

        if custom_enabled and not custom_cmd:
            return False, "已启用自定义启动但启动命令为空", None

        # ============ 解析注入模式 ============
        if custom_enabled and custom_cmd:
            effective_mode = custom_inject_mode
            resolved_cmd = custom_cmd
        else:
            effective_mode = inject_method
            resolved_cmd = None
        use_hook = (effective_mode == "hook")

        # ============ d3dx.ini patch 参数 ============
        if resolved_cmd:
            launch_val = resolved_cmd
        elif launch_method != "manual":
            launch_val = str(game_exe)
        else:
            launch_val = ""
        # loader=当前进程名（与 Hook/Direct 注入时 d3d11 的 DllMain 校验一致）
        loader_val = os.path.basename(sys.executable)

        # ============ 额外注入库 ============
        extra_dll_paths: List[Path] = []
        if cfg.get("extra_libraries_enabled"):
            raw = cfg.get("extra_libraries") or ""
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    extra_dll_paths.append(Path(line))

        request = StartRequest(
            process_name=target,
            exe_path=Path(game_exe),
            xxmi_dll_path=d3d11_path,
            use_hook=use_hook,
            custom_launch_cmd=resolved_cmd,
            inject_dll_paths=extra_dll_paths,
            process_start_method=launch_method.upper(),
            process_timeout=30,
        )

        task = LaunchTask(
            gimi_dir=gimi_dir,
            d3dx_target=target,
            d3dx_hunting=hunting,
            d3dx_warning=warning,
            d3dx_launch=launch_val,
            d3dx_loader=loader_val,
            injector_path=str(injector_path),
            request=request,
        )
        return True, "", task
