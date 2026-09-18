"""
omg.domain.launch — 主页「开始」按钮的启动编排层（方案 B）。

把"点击开始 → 注入 → 完成/失败"的业务逻辑从 home.py 抽出来，分为：
  - controller : LaunchController（状态机 + 参数装配 + 信号分发）
  - worker     : LaunchWorker（QThread，跑 patch d3dx.ini + MigotoInjector.run()）
  - types      : LaunchState / LaunchTask

home.py 只负责把按钮点击转给 controller.toggle()，并据 controller.state_changed
刷新图标 / 禁用态 / tooltip，不直接碰注入逻辑。
"""

from .types import LaunchState, LaunchTask
from .controller import LaunchController
from .worker import LaunchWorker

__all__ = ["LaunchState", "LaunchTask", "LaunchController", "LaunchWorker"]
