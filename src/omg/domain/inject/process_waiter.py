"""
process_waiter.py — 子进程等待器（ProcessWaiter）

为什么单独拆出来
----------------
``ProcessWaiter`` 继承自 ``multiprocessing.Process``，类定义时就必须 import
``multiprocessing``。而 ``multiprocessing`` 顶层还会拖入 ``pickle`` / ``copy`` /
``concurrent.futures`` 等约 10 个模块，在机械盘冷启动时为每个模块各付一次寻道
（本机实测约 9.3 ms/次）。

``ProcessWaiter`` 只在用户实际「等待游戏进程出现 / 退出」时才需要（即
``wait_for_process`` / ``wait_for_process_exit`` 被调用时），因此它被单独放进
本模块，``process_tracker`` 改为在调用处才本地导入，避免污染启动路径。
"""

from __future__ import annotations

import time
from multiprocessing import Process, Value


class ProcessWaiter(Process):
    """
    等待进程「出现」或「退出」。

    self.data.value 返回值约定：
      0..MAX_PID : 在超时前找到进程（返回 pid）
      -100        : 在超时前未找到进程（wait_exit 模式且进程始终不在）
      -200        : 超时
      -300        : 达到 kill_timeout 后被强杀
    """

    def __init__(self, process_name: str, timeout: int = -1,
                 with_window: bool = False, wait_exit: bool = False,
                 kill_timeout: int = -1, cmd=None, inject_dll=None,
                 check_visibility: bool = False):
        super().__init__()
        # daemon=True：主进程退出时由 multiprocessing 直接终止本等待进程。
        # 否则 multiprocessing.util._exit_function 会 join 非 daemon 子进程，
        # 在等待窗口的 30 秒窗口内关闭 OMG 会导致主进程长时间退不掉。
        self.daemon = True
        self.process_name = process_name
        self.timeout = int(timeout)
        self.with_window = with_window
        self.check_visibility = check_visibility
        self.wait_exit = wait_exit
        self.kill_timeout = kill_timeout
        self.cmd = cmd
        self.inject_dll = inject_dll
        self.data = Value('i', -100)

    def run(self):
        # 局部导入，避免模块加载期依赖 process_tracker（也避免 spawn 子进程时的导入环）
        from omg.domain.inject.process_tracker import (
            get_process, get_hwnds_for_pid, _kernel32)

        if self.cmd:
            import subprocess
            subprocess.Popen(self.cmd)

        time_start = time.time()

        while True:
            current_time = time.time()

            if self.timeout != -1 and current_time - time_start >= self.timeout:
                break

            process = get_process(process_name=self.process_name)

            if process is not None:
                self.data.value = process[0]  # pid

                if not self.wait_exit:
                    # 等待进程「出现」模式
                    if not self.with_window:
                        if self.inject_dll:
                            # XXMI 原实现未提供 DLL 注入钩子，保持 NotImplemented 行为
                            raise NotImplementedError
                        return
                    elif len(get_hwnds_for_pid(pid=self.data.value,
                                              check_visibility=self.check_visibility)) != 0:
                        if self.inject_dll:
                            raise NotImplementedError
                        return

                # wait_exit 模式：达到 kill_timeout 后强杀
                if self.kill_timeout != -1 and current_time - time_start >= self.kill_timeout:
                    self.data.value = -300
                    pid = process[0]
                    try:
                        _kernel32().TerminateProcess(
                            _kernel32().OpenProcess(0x0001, False, pid), 0)
                    except Exception:
                        pass

            elif self.wait_exit:
                return

            time.sleep(0.1)

        # 超时
        self.data.value = -200
