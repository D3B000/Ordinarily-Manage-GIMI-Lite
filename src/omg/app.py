"""
omg.app — OMG 应用入口（Phase 5 真包收口）。

设计要点：
- 全程绝对导入，删除历史上 ``main.py`` 对 ``func/``、``page/`` 的 ``sys.path`` 注入。
- GUI 相关依赖（PySide6、``omg.pages``）在 ``main()`` 内惰性导入，使本模块在无
  PySide6 的 headless 环境（CI / 单元测试）下也能被 ``import`` 与静态校验
  （``py_compile``、导入探测、正则静态检查）。
- 真正启动 GUI 仍需在已安装 PySide6 的运行环境执行 ``python -m omg`` 或
  ``python main.py``（由根目录 ``main.py`` 把 ``src`` 加入 sys.path）。
"""
from __future__ import annotations

import argparse
import sys

from omg.core.logging_setup import setup_logging
from omg.core.updates import (
    configure_qt_media_backend,
    ensure_single_instance,
    fix_taskbar_icon,
    release_instance_mutex,
    setup_qt_plugin_path,
)

__all__ = ["main", "parse_args"]


def parse_args(argv=None) -> argparse.Namespace:
    """解析命令行参数（薄封装，便于测试）。"""
    parser = argparse.ArgumentParser(
        prog="omg",
        description="OMG —— 3DMigoto 注入 / 涂装 / mod 管理工具",
    )
    parser.add_argument(
        "--no-single-instance",
        action="store_true",
        help="禁用单实例互斥（允许同时运行多个 OMG）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """应用入口。返回进程退出码。"""
    # 0) 接管 multiprocessing 的 spawn 子进程（必须最先执行）
    #    Windows 下 spawn 出的子进程会带 --multiprocessing-fork 重新执行本程序：
    #      · 必须早于 parse_args —— 否则 argparse 视其为未知参数直接 sys.exit(2)；
    #      · 必须早于 setup_logging —— 否则每个等待进程都会新建一个日志文件。
    #    domain/inject/process_tracker.py 的 ProcessWaiter 依赖本调用；缺失时
    #    打包成 exe 后每次等待窗口都会 spawn 出一个重启整个 GUI 的子进程。
    #    注意：freeze_support() 只在被 PyInstaller 冻结（sys.frozen）时才有意义，
    #    且仅当它作为顶层 import 时才需要。dev 模式（python main.py）下整个
    #    multiprocessing 链（约 10 个标准库模块）纯属白付，因此放冻结态才导入，
    #    避免污染冷启动路径。
    if getattr(sys, "frozen", False):
        import multiprocessing
        multiprocessing.freeze_support()

    args = parse_args(argv)
    logger = setup_logging()
    logger.info("OMG 启动中 …")

    # 1) 单实例互斥（win32 命名 mutex，无需 GUI）
    if not args.no_single_instance and not ensure_single_instance():
        logger.warning("已有另一个 OMG 实例在运行，本次启动退出")
        return 1

    try:
        # 2) GUI 依赖惰性导入（避免 headless 环境强制拉起 PySide6）
        from PySide6.QtWidgets import QApplication

        from omg.core.config import ConfigManager
        from omg.domain.launch.controller import LaunchController
        from omg.pages import HomeWindow

        # 3) Qt 运行时装配（均在 omg.core.updates 内惰性导入 PySide6）
        #    媒体后端环境变量（QT_MEDIA_BACKEND / QT_FFMPEG_THREAD_COUNT）必须在
        #    QApplication 创建「前」设置，否则 Qt 后端插件已加载、无法切换。
        setup_qt_plugin_path()
        fix_taskbar_icon()
        configure_qt_media_backend()

        app = QApplication(sys.argv)
        app.setApplicationName("OMG")
        # 托盘常驻形态：面板收进托盘后进程仍要活着。默认行为（最后一个窗口
        # 关闭即 quit）会让「隐藏面板 + 关掉资源浏览窗口」被判定为退出 → 必须关掉。
        app.setQuitOnLastWindowClosed(False)
        # 子窗口（设置 / 便捷构建 / 资源浏览）标题栏图标
        from omg.ui.tray import load_tray_icon
        app.setWindowIcon(load_tray_icon())

        # 2.5) 注册图标资源（icons.rcc）。缺失时加载器自动回退磁盘，不致命。
        from omg.ui.icon_loader import register_icon_resources
        register_icon_resources()

        # 4) 业务控制器：读配置 + 编排「开始」按钮注入流程，注入 HomeWindow
        config = ConfigManager()
        # 在主线程惰性创建版本变化信号（确保 QObject 线程归属为主线程，
        # 供 _detect_versions_async 完成后跨线程 QueuedConnection 派发到 UI）
        _ = config.versions_changed
        launch = LaunchController(config)

        # 4.5) 托盘模式：可用性必须在 HomeWindow 构造「前」判定——它决定主窗口
        #      是否带 Qt.Tool（不进任务栏）。托盘不可用时保持原行为，绝不把窗口
        #      藏到一个用户找不到的地方。
        from omg.ui.tray import TrayController, is_tray_available
        tray_mode = is_tray_available()

        # 4) Qt 消息处理器（qInstallMessageHandler）应在 QApplication 创建后、
        #    由 ui 层安装，将 Qt 日志并入 OMG logger；此处留待 ui 层接入。
        win = HomeWindow(launch_controller=launch, config=config,
                         tray_mode=tray_mode)
        win.show()

        # 4.6) 单实例的另一半：本实例是「主实例」，装上激活广播接收器。
        #      后续启动的实例会广播激活消息（core/instance_ipc），这里收到后
        #      显示面板——托盘模式下这是用户再次双击图标时唯一的反馈。
        if not args.no_single_instance:
            from omg.ui.instance_activator import InstanceActivator
            activator = InstanceActivator(app)
            activator.activated.connect(win.show_panel)
            # 单独再连一条日志：激活链路跨进程，没有日志时排障只能靠猜
            activator.activated.connect(
                lambda: logger.info("收到其它实例的激活广播，已显示面板"))
            if not activator.install():
                logger.warning("激活广播接收器未装上（重复启动将无反馈）")

        if tray_mode:
            # parent=app：控制器随应用存活，避免被回收导致托盘图标消失
            TrayController(
                on_show_panel=win.show_panel,
                on_resources=win.show_resources,
                on_reset_panel=win.reset_panel,
                on_quit=win.request_exit,
                parent=app,
                tooltip="OMG",
            )
            logger.info("已启用托盘模式（无任务栏图标）")
        else:
            logger.warning("系统托盘不可用，回退为普通窗口模式（任务栏可见）")

        return app.exec()
    finally:
        release_instance_mutex()


if __name__ == "__main__":
    sys.exit(main())
