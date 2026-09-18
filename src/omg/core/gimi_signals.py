"""omg.core.gimi_signals — GIMI 更新流程的信号集合（轻量模块，只依赖 QtCore）。

为什么单独拆出来
----------------
``omg.core.gimi_update`` 顶层会导入 ``omg.core.download`` → ``urllib.request``
→ ``ssl``，连带把 ``libcrypto-3.dll``（约 5 MB）拉进进程。在机械盘上，冷启动
要为这 20 多个模块各付一次寻道（本机实测约 9.3 ms/次），还要为那 5 MB 的 DLL
分页买单。

但信号对象不同：``HomeWindow`` **建窗时就要创建它**，而真正干活的
``CheckThread`` / ``UpdateThread`` 只在用户点击「检查更新」后才需要。

所以把信号单独放在这里（只依赖 QtCore，启动时早已加载），让 ``gimi_update``
整条重链可以真正做到惰性导入。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class GimiUpdateSignals(QObject):
    """UI 层关心的统一信号集合。"""

    # 检查完成: (status, local, remote, msg)
    #   status ∈ {"up_to_date", "update_available", "error"}
    check_result = Signal(str, str, str, str)
    # 更新进度 0~100
    progress = Signal(float)
    # 更新状态 (msg, role)
    status = Signal(str, str)
    # 更新完成 (success, msg)
    finished = Signal(bool, str)


__all__ = ["GimiUpdateSignals"]
