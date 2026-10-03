"""
omg.core.source_prep —源码工程的「构建前预处理」。

背景
----
XXMI-Libs-Package 从 1.2.0 起，入口工程 ``DirectX11\\DirectX11.vcxproj`` 多了一个
自定义 Target：

.. code-block:: xml

    <Target Name="GenerateVersionHeader" BeforeTargets="PrepareForBuild">
      <Exec Command="powershell.exe ... -File tools\\generate-version.ps1 ..." />
    </Target>

``generate-version.ps1`` 会用 ``git rev-parse`` / ``git describe`` / ``git status``
拼出版本信息写进 ``version.generated.h``，而这些命令**只在 Git 仓库内可用**，
否则脚本第一段就``throw "Not inside a Git repository."``，PowerShell 退出码 1，
MSBuild 记``error MSB3073``，入口工程整个失败 —— d3d11.dll 编不出来。

而便捷构建拉的是 GitHub 自动生成的 **tag 归档 zip**
（``archive/refs/tags/<tag>.zip``），解压后只有 ``.gitattributes`` / ``.gitignore``，
**没有 ``.git`` 目录**，于是 100% 触发。1.1.7 没有这个 Target，所以旧版本能编过。

对策（本模块）
-------------
不去``git init`` 伪造仓库（脚本后续还要 ``remote.origin.url`` 和 ``describe --tags``，
光造空仓库不够，还得commit + tag 整棵树，又慢又脏），改为**让这一步变得可选**：

1. **预写** ``version.generated.h``：版本号直接取自源码目录名
   （``XXMI-Libs-Package-1.2.0`` → 1.2.0.0），``VER_COMMENTS_STR`` 填仓库地址。
   有了它，``version.h`` 的 ``#include "version.generated.h"`` 一定能找到文件，
   版本号也仍然是对的（就是所选的那个 release 版本）。
2. **给那行** ``<Exec>`` **加** ``ContinueOnError="true"``：脚本失败降级为警告，
   不再中断构建。反正头文件已经由第1 步备好，脚本跑不跑都不影响结果。

两条都是**幂等**的，且都以「``.git`` 存在则原样跳过」为前提 —— 用户若自行
``git clone`` 了源码，走的还是上游原生流程，我们不碰。

时机
----
解压完成后调一次（``download_and_extract``），构建前再调一次兜底
（``build_project``）—— 后者是为了让**已经躺在 project/ 里的老目录**
（如本次失败的 1.2.0）也能被就地修好，不必让用户重新下载一遍源码。

只依赖标准库，headless 可直接导入与自测。
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, NamedTuple, Optional

logger = logging.getLogger("OMG")


# ===========================================================================
# 常量
# ===========================================================================
#: 入口工程（与 :mod:`omg.core.build_env` 的 ENTRY_PROJECT 一致）
ENTRY_VCXPROJ = os.path.join("DirectX11", "DirectX11.vcxproj")

#: 源码目录名前缀，与 :mod:`omg.core.alchemy` 的 PROJECT_PREFIX 一致
PROJECT_PREFIX = "XXMI-Libs-Package-"

#: 脚本产出的头文件（version.h 会无条件 #include 它）
GENERATED_HEADER = "version.generated.h"

#: 需要打补丁的那个 Target 名
VERSION_TARGET = "GenerateVersionHeader"

#: 匹配 vcxproj 里那行 Exec（只认带 generate-version.ps1 的，避开 PostBuildEvent 的 xcopy）
_EXEC_RE = re.compile(
    r"<Exec\b(?=[^>]*generate-version\.ps1)(?![^>]*ContinueOnError)[^>]*/>",
    re.IGNORECASE,
)

#: 目录名尾部的版本号：XXMI-Libs-Package-1.2.0 → 1.2.0
_VERSION_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)*)\s*$")

#: 官方仓库地址（写进 VER_COMMENTS_STR，语义上等价于脚本里的 remote.origin.url）
UPSTREAM_URL = "https://github.com/SpectrumQT/XXMI-Libs-Package"


# ===========================================================================
# 结果类型
# ===========================================================================
class PrepResult(NamedTuple):
    """一次预处理的结果。``changed`` 为 False 表示无需 / 无法处理。"""

    changed: bool                  # 是否真的改了磁盘
    header_written: bool           # 是否补写了 version.generated.h
    exec_patched: bool             # 是否给 Exec 加了 ContinueOnError
    skipped: str                   # 跳过原因（中文，仅供日志/UI 展示）
    detail: str                    # 一行摘要

    @property
    def ok(self) -> bool:
        """预处理是否处于「构建可以继续」的状态。

        未改动（不需要改/ 是真仓库/ 版本号解析不出）也算 OK —— 只有
        「该改却没改成」才是不 OK。
        """
        return not self.changed or (self.header_written and self.exec_patched)


# ===========================================================================
# 版本号
# ===========================================================================
def version_tuple(root: str) -> Optional[tuple]:
    """从源码目录名解析版本号，返回 ``(major, minor, revision, build)``。

    ``XXMI-Libs-Package-1.2.0`` → ``(1, 2, 0, 0)``；
    ``...-1.4.0.7`` → ``(1, 4, 0, 7)``。解析不出返回 None。
    """
    name = os.path.basename(os.path.normpath(root or ""))
    if name.lower().startswith(PROJECT_PREFIX.lower()):
        name = name[len(PROJECT_PREFIX):]
    match = _VERSION_SUFFIX_RE.search(name)
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


# ===========================================================================
# 头文件
# ===========================================================================
def render_generated_header(root: str) -> Optional[str]:
    """生成 ``version.generated.h`` 的内容；版本号解析不出返回 None。

    格式与 ``generate-version.ps1`` 的 here-string 逐行对齐（含尾部空行），
    这样即使将来有人在真仓库里跑脚本重新生成，也不会产生无谓diff。
    """
    ver = version_tuple(root)
    if ver is None:
        return None
    major, minor, revision, build = ver
    return (
        f"#define VERSION_MAJOR               {major}\n"
        f"#define VERSION_MINOR               {minor}\n"
        f"#define VERSION_REVISION            {revision}\n"
        f"#define VERSION_BUILD               {build}\n"
        "\n"
        '#define VER_COMPANYNAME_STR         "Built by OMGLite"\n'
        f'#define VER_COMMENTS_STR            "{UPSTREAM_URL}"\n'
        "\n"
    )


def _atomic_write_text(path: str, text: str) -> None:
    """原子落盘（.tmp + os.replace），编码 utf-8 无 BOM、行尾 \\n。

    与 d3dx.ini 那套写入纪律一致：中断时不留半截文件，原文件完好。
    """
    tmp = path + ".omgtmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def ensure_generated_header(root: str) -> bool:
    """确保 ``version.generated.h`` 存在。已存在则不动，返回是否新写。"""
    ver = version_tuple(root)
    if ver is None:
        return False
    path = os.path.join(root, GENERATED_HEADER)
    if os.path.isfile(path):
        return False
    content = render_generated_header(root)
    if content is None:
        return False
    try:
        _atomic_write_text(path, content)
    except OSError as e:
        logger.warning("补写 %s 失败: %s", GENERATED_HEADER, e)
        return False
    logger.info("已补写 %s（版本 %s）", GENERATED_HEADER,
                ".".join(str(p) for p in ver))
    return True


# ===========================================================================
# vcxproj 补丁
# ===========================================================================
def _patch_vcxproj_text(text: str) -> Optional[str]:
    """给那行 Exec 加 ``ContinueOnError="true"``；无需改或改不动返回 None。

    以**文本**而非 XML 处理：vcxproj 带 UTF-8 BOM，且 MSBuild 对属性顺序不敏感，
    走 ElementTree 会顺手改掉整份文件的空白与自闭合风格，diff 变成噪音。
    只动匹配到的那一行，其余字节原样保留。
    """
    match = _EXEC_RE.search(text)
    if not match:
        return None
    line = match.group(0)
    # 属性插在标签名之后，与 MSBuild 自己生成的习惯一致
    patched = line.replace("<Exec", '<Exec ContinueOnError="true"', 1)
    return text[:match.start()] + patched + text[match.end():]


def patch_version_exec(root: str) -> bool:
    """给入口工程里 ``GenerateVersionHeader`` 的 Exec 加 ``ContinueOnError``。

    已打过补丁 / 找不到那行/ 文件不可读时返回 False（不抛）。
    """
    path = os.path.join(root, ENTRY_VCXPROJ)
    if not os.path.isfile(path):
        return False
    try:
        # encoding="utf-8-sig" 读掉 BOM，写回时再补上，避免 MSBuild 报文件头非法
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        patched = _patch_vcxproj_text(text)
        if patched is None:
            return False
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(patched)
    except OSError as e:
        logger.warning("补丁 %s 失败: %s", ENTRY_VCXPROJ, e)
        return False
    logger.info("已为 %s 的 GenerateVersionHeader 加 ContinueOnError（源码包非 Git 仓库）",
                ENTRY_VCXPROJ)
    return True


def needs_patch(root: str) -> bool:
    """该目录是否**待**打补丁（只读判断，供环境检测用）。"""
    path = os.path.join(root, ENTRY_VCXPROJ)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return _patch_vcxproj_text(f.read()) is not None
    except OSError:
        return False


# ===========================================================================
# 入口
# ===========================================================================
def is_git_repo(root: str) -> bool:
    """是否是真正的 Git 工作区（``.git`` 目录或文件，兼容 worktree / submodule）。"""
    return os.path.exists(os.path.join(root, ".git"))


def prepare_source(root: str) -> PrepResult:
    """对一份解压好的源码工程做构建前预处理，返回 :class:`PrepResult`。

    幂等：已打过补丁 / 头文件已在/ 确实是 Git 仓库时都不再动磁盘。
    任何 IO 异常都在内部吞掉并降级，绝不让预处理把构建流程本身搞挂。
    """
    if not root or not os.path.isdir(root):
        return PrepResult(False, False, False, "源码目录不存在", "源码目录不存在")

    if is_git_repo(root):
        return PrepResult(False, False, False, "Git 仓库", "源码是 Git 仓库，无需处理")

    if version_tuple(root) is None:
        return PrepResult(False, False, False, "版本号无法识别",
                          f"目录名不含版本号，跳过预处理：{os.path.basename(root)}")

    if not os.path.isfile(os.path.join(root, ENTRY_VCXPROJ)):
        return PrepResult(False, False, False, "入口工程缺失",
                          f"缺少 {ENTRY_VCXPROJ}，跳过预处理")

    if not needs_patch(root) and os.path.isfile(os.path.join(root, GENERATED_HEADER)):
        return PrepResult(False, False, False, "已处理", "预处理已生效，无需重复处理")

    header = ensure_generated_header(root)
    patched = patch_version_exec(root)
    if header and patched:
        return PrepResult(True, True, True, "", "已生成版本头并放行生成步骤")
    # 单条成功也算部分生效（如未来上游改了 Exec 的写法而头文件已补）
    done = [n for n, ok in (("版本头", header), ("Exec 容错", patched)) if ok]
    detail = "、".join(done) if done else "预处理未生效"
    return PrepResult(bool(done), header, patched, "", detail)


def scan_projects(project_dir: str) -> List[str]:
    """列出 project/ 下所有待预处理的源码目录（供 UI 提示或批量修复用）。"""
    out: List[str] = []
    if not project_dir or not os.path.isdir(project_dir):
        return out
    try:
        names = sorted(os.listdir(project_dir))
    except OSError:
        return out
    for name in names:
        full = os.path.join(project_dir, name)
        if not name.startswith(PROJECT_PREFIX) or not os.path.isdir(full):
            continue
        if is_git_repo(full):
            continue
        if needs_patch(full):
            out.append(full)
    return out
