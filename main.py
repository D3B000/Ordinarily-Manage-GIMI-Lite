"""OMG 开发期薄入口。

生产环境请以可安装包方式运行（``pip install -e .`` 后 ``python -m omg`` 或 ``omg``）。
本文件仅为「未安装时直接从仓库根运行」的便捷入口，会把 ``src`` 加入 sys.path。

注意：包内部（omg.*）已无任何 sys.path 注入；此处的路径处理仅服务于本地开发启动。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from omg.app import main

if __name__ == "__main__":
    sys.exit(main())
