"""
omg.core.logging_setup — 日志初始化（从 OMGDev/main.py 抽取）。

设计：
- 进程级 ``"OMG"`` logger，输出到 ``%EXE_DIR%/log/omg_<时间戳>.log``（路径来自
  ``omg.core.paths.LOG_PATH``），同时把 ``sys.stdout`` 重定向进 logger，便于在
  无终端环境（冻结 exe）收集 stdout 输出。
- **不含** Qt 消息处理器（``qInstallMessageHandler`` 依赖 PySide6，留待 ui 层在
  QApplication 创建后安装）。
- 幂等：重复 ``import`` / 重复 ``setup_logging()`` 不会重复挂载 handler、不会重复
  包装 stdout。

本模块 **不依赖 PySide6**，导入即可完成日志装配。
"""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from omg.core.paths import LOG_PATH

logger = logging.getLogger("OMG")
logger.setLevel(logging.DEBUG)
logger.propagate = False  # 防止父 logger 重复输出

_log_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
_fh = RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_log_fmt)
# 防止模块重载时重复挂 handler
if not any(isinstance(h, RotatingFileHandler) and h.baseFilename == _fh.baseFilename
           for h in logger.handlers):
    logger.addHandler(_fh)

_in_logger_write = threading.Lock()  # 递归保护（线程安全）


class _LoggerStream:
    """把 stdout 写入重定向进 logger。"""

    def __init__(self, level=logging.INFO):
        self._level = level
        self._buf = ""

    def write(self, msg):
        if not msg or msg.isspace():
            return
        if not _in_logger_write.acquire(blocking=False):
            return  # 防递归
        try:
            self._buf += msg
            if "\n" in self._buf:
                lines = self._buf.splitlines()
                self._buf = ""
                for line in lines:
                    line = line.strip()
                    if line:
                        logger.log(self._level, line)
        finally:
            _in_logger_write.release()

    def flush(self):
        if not self._buf.strip():
            return
        if not _in_logger_write.acquire(blocking=False):
            return
        try:
            logger.log(self._level, self._buf.strip())
            self._buf = ""
        finally:
            _in_logger_write.release()

    @property
    def encoding(self):
        return "utf-8"


def setup_logging() -> logging.Logger:
    """确保日志 handler 与 stdout 重定向已就位（可重复调用，幂等）。

    Returns:
        配置好的 ``"OMG"`` logger。
    """
    # handler 已在模块加载时挂载（带去重保护）；此处保证 stdout 仅被包装一次。
    if not isinstance(sys.stdout, _LoggerStream):
        sys.stdout = _LoggerStream(logging.INFO)
    # 注意：stderr 不重定向到 LoggerStream——所有错误日志都通过
    # logger.exception()/logger.error() 直接进文件 handler。重定向 stderr 会导致
    # Python 把 traceback 写 stderr 而 logger.exception() 又写一遍，产生重复条目。
    return logger


# 模块导入即完成日志装配（与 OMGDev/main.py 原行为一致）。
setup_logging()
