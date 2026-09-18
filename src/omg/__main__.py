"""启用 ``python -m omg`` 入口。

需先让 ``omg`` 包可导入（``pip install -e .``，或 ``PYTHONPATH=src python -m omg``）。
"""
from omg.app import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
