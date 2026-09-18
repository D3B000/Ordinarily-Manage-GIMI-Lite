"""
omg.core.build_env — 构建环境检测（便捷构建页「环境」一行的后端）

设计要点
--------
旧链路只有一句「找 MSBuild.exe 在不在」，而构建能否成功取决于**工具链 + SDK +
源码工程 + 机器状态**四组条件同时成立，所以这里把检测拆成一组可枚举、可解释、
可扩展的 :class:`CheckItem`。

**需求从工程文件里读，不写死常量表**：解析 ``.vcxproj`` 拿到当前配置实际需要的
``PlatformToolset`` / ``WindowsTargetPlatformVersion`` / 静态库 / 后处理目录，再逐
项校验。仓库将来升到 v145、切 ClangCL、换静态库名，检测逻辑自动跟上。

只依赖 stdlib（``logging`` 走 "OMG" logger），因此在 headless 环境可直接自测；
UI 线程侧由 :mod:`omg.core.alchemy` 的 ``EnvCheckThread`` 包装调用。

分层
----
- 工具链（慢，可能起 vswhere 子进程）：``check_toolchain()``
- 源码工程（快，纯解析 + 文件存在性）：``inspect_project()`` / ``check_project()``
- 机器状态（快）：``check_runtime()``
- 汇总：``run_report()``
- 失败翻译：``translate_error()`` / ``describe_build_error()``
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("OMG")

from omg.core.ritual_loader import load_ritual_bundle


# ===========================================================================
# 状态与常量
# ===========================================================================
#: 通过
STATUS_OK = "ok"
#: 能构建但大概率翻车 / 很慢
STATUS_WARN = "warn"
#: 阻塞项：不允许构建
STATUS_MISS = "miss"
#: 因前置条件缺失而无法检查（不计入 ready 判定）
STATUS_SKIP = "skip"

#: 清单里的状态符号（OMGPopCard 用 Microsoft YaHei UI 绘制，这几个字形均存在）
_SYMBOL = {
    STATUS_OK: "√",
    STATUS_MISS: "×",
    STATUS_WARN: "!",
    STATUS_SKIP: "·",
}

DEFAULT_CONFIG = "Release"
DEFAULT_PLATFORM = "x64"
#: 源码工程不存在时的兜底需求（VS2022 默认工具集 / 任意 10.0.x SDK）
DEFAULT_TOOLSET = "v143"
DEFAULT_SDK = "10.0"

#: 仓库内入口工程（相对源码根）。它自带 ProjectReference，编它就带齐依赖。
ENTRY_PROJECT = os.path.join("DirectX11", "DirectX11.vcxproj")
SOLUTION_NAME = "StereovisionHacks.sln"

#: PostBuildEvent 里以 ``$(SolutionDir)`` 引用的仓库内目录，如 ``Dependencies\*.*``
_DEP_DIR_RE = re.compile(r"\$\(SolutionDir\)([A-Za-z0-9_.\-]+)", re.IGNORECASE)

#: 可用空间告警阈值（LTCG 编 88 个 cpp，中间产物很吃盘）
MIN_FREE_GB = 2.0
#: 工程根路径长度告警阈值（MAX_PATH = 260 是真实风险）
MAX_ROOT_LEN = 120

# --- 修复动作类型 ---
FIX_URL = "url"          # 打开下载页
FIX_DIR = "dir"          # 打开目录
FIX_REFETCH = "refetch"  # 重新下载源码
FIX_RETRY = "retry"      # 重试检测
FIX_NONE = "none"

URL_BUILD_TOOLS = "https://visualstudio.microsoft.com/zh-hans/visual-cpp-build-tools/"
URL_WINSDK = "https://developer.microsoft.com/zh-cn/windows/downloads/windows-sdk/"

#: vswhere 是官方权威探测入口（能覆盖所有 SKU / 非默认安装路径）
_VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
#: vswhere 不可用时的兜底候选（VS2022 四种 SKU 的默认路径）
_MSBUILD_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe",
    r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
    r"C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
    r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
)
#: Windows SDK 常见安装根（先后顺序即优先级）
_WIN_KITS = (
    r"C:\Program Files (x86)\Windows Kits\10",
    r"C:\Program Files\Windows Kits\10",
)


# ===========================================================================
# 数据模型
# ===========================================================================
@dataclass
class FixAction:
    """清单里展示的「建议动作」。

    UI 目前只把它渲染成一行建议文本，不渲染按钮（悬浮气泡是纯文本绘制）。
    保留结构化字段是为了将来做行内一键修复时不用改数据层。
    """

    kind: str = FIX_NONE          # FIX_URL / FIX_DIR / FIX_REFETCH / FIX_RETRY / FIX_NONE
    label: str = ""               # 展示文案，如「安装生成工具」
    payload: str = ""             # URL / 目录 / 空


@dataclass
class CheckItem:
    """一项检测结果。"""

    key: str                      # 稳定标识，如 "toolchain.toolset"
    title: str                    # 展示短标题，如「MSVC 工具集」
    status: str = STATUS_SKIP     # STATUS_*
    detail: str = ""              # 一行结论，如 "v143 (14.44.35207)"
    hint: str = ""                # MISS/WARN 时的解释
    fix: Optional[FixAction] = None

    @property
    def symbol(self) -> str:
        return _SYMBOL.get(self.status, _SYMBOL[STATUS_SKIP])

    @property
    def is_blocker(self) -> bool:
        return self.status == STATUS_MISS


@dataclass
class ProjectNeeds:
    """从 vcxproj 解析出的「需求清单」（当前配置实际需要什么）。"""

    root: str = ""
    entry: str = ""
    parsed: bool = False
    error: str = ""
    toolsets: List[str] = field(default_factory=list)
    sdks: List[str] = field(default_factory=list)
    #: [{"raw": 原始 token, "abs": 绝对路径, "rel": 相对源码根}, ...]
    libs: List[dict] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    dep_dirs: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    ltcg: bool = False            # WholeProgramOptimization（LTCG，慢且吃内存）
    sources: int = 0


@dataclass
class EnvReport:
    """一次完整检测的汇总。"""

    items: List[CheckItem] = field(default_factory=list)
    project: str = ""             # 项目版本（如 "1.0.5"）
    config: str = DEFAULT_CONFIG
    platform: str = DEFAULT_PLATFORM
    error: str = ""               # 检测过程本身出错时的说明

    @property
    def blockers(self) -> List[CheckItem]:
        return [i for i in self.items if i.status == STATUS_MISS]

    @property
    def warnings(self) -> List[CheckItem]:
        return [i for i in self.items if i.status == STATUS_WARN]

    @property
    def ready(self) -> bool:
        """是否允许构建：无任何阻塞项。"""
        return bool(self.items) and not self.blockers and not self.error

    # ------------------------------------------------------------------
    # 渲染：悬浮卡片里的清单（纯文本，由 omg.ui.OMGPopCard 绘制）
    # ------------------------------------------------------------------
    def to_tip_text(self, max_detail: int = 44) -> str:
        if not self.items:
            return "构建环境：尚未检测"

        def _clip(text: str) -> str:
            text = (text or "").strip()
            return text if len(text) <= max_detail else text[:max_detail - 1] + "…"

        if self.ready:
            head = "构建环境已就绪"
        else:
            n = len(self.blockers)
            head = f"构建环境未就绪 · {n} 项待处理" if n else "构建环境未就绪"

        lines = [head]
        for item in self.items:
            detail = _clip(item.detail or item.hint)
            lines.append(f"{item.symbol} {item.title}"
                         + (f" — {detail}" if detail else ""))
            if item.status in (STATUS_MISS, STATUS_WARN) and item.hint:
                lines.append(f"  → {_clip(item.hint)}")
        return "\n".join(lines)


# ===========================================================================
# 通用小工具
# ===========================================================================
def _run(cmd: Sequence[str], timeout: float = 8.0) -> str:
    """跑一条命令并返回 stdout；任何异常都退化为空串（检测不许抛）。"""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:  # noqa: BLE001 - 检测阶段吞掉一切
        logger.debug("命令执行失败 %s: %s", cmd, e)
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _ver_key(text: str) -> Tuple:
    """``"10.0.26100.0"`` → ``(10, 0, 26100, 0)``，用于版本排序。"""
    parts = []
    for chunk in re.findall(r"\d+", text or ""):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _project_label(root: str) -> str:
    """``.../XXMI-Libs-Package-1.0.5`` → ``1.0.5``。"""
    name = os.path.basename(os.path.normpath(root)) if root else ""
    if not name:
        return ""
    for prefix in ("XXMI-Libs-Package-", "XXMI-Libs-Package"):
        if name.startswith(prefix):
            return name[len(prefix):].lstrip("-")
    return name


def _clip(text: str, limit: int = 44) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ===========================================================================
# 一、工具链（慢检）
# ===========================================================================
def _edition_of(msbuild: str) -> str:
    """从 MSBuild 路径推断 ``VS2022 · Community`` 这类展示名。"""
    year = edition = ""
    for part in os.path.normpath(msbuild).split(os.sep):
        if re.fullmatch(r"(2015|2017|2019|2022|2026)", part):
            year = part
        low = part.lower()
        if low in ("community", "professional", "enterprise", "buildtools"):
            edition = "Build Tools" if low == "buildtools" else part
    bits = [b for b in (f"VS{year}" if year else "", edition) if b]
    return " · ".join(bits) or os.path.basename(os.path.dirname(msbuild))


def _install_root(msbuild: str) -> str:
    """``<root>\\MSBuild\\Current\\Bin\\MSBuild.exe`` → ``<root>``。"""
    norm = os.path.normpath(msbuild)
    parts = norm.split(os.sep)
    for i, part in enumerate(parts):
        if part.lower() == "msbuild":
            return os.sep.join(parts[:i])
    return os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(norm))))


def locate_msbuild() -> Tuple[str, str]:
    """定位 MSBuild.exe。

    Returns:
        ``(path, edition)``；找不到时为 ``("", "")``。
        vswhere 优先（官方权威，能覆盖非默认安装路径），失败才退回硬编码候选。
    """
    if os.name != "nt":
        return "", ""

    if os.path.isfile(_VSWHERE):
        out = _run([_VSWHERE, "-latest", "-requires",
                    "Microsoft.Component.MSBuild",
                    "-find", r"MSBuild\**\Bin\MSBuild.exe"])
        for line in out.splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                return line, _edition_of(line)

    for path in _MSBUILD_CANDIDATES:
        if os.path.isfile(path):
            return path, _edition_of(path)
    return "", ""


def probe_toolsets(install_root: str,
                   toolsets: Sequence[str]) -> Tuple[bool, str, List[str]]:
    """校验工程所需的生成工具集是否真的装了。

    ``v143`` → 找 ``VC\\Tools\\MSVC\\<ver>\\bin\\Hostx64\\x64\\cl.exe``；
    ``ClangCL`` → 找 VS 自带的 LLVM ``clang-cl.exe``。

    Returns:
        ``(ok, detail, missing)``；detail 形如 ``v143 (14.44.35207)``。
    """
    missing: List[str] = []
    found: List[str] = []
    for toolset in toolsets or [DEFAULT_TOOLSET]:
        low = (toolset or "").lower()
        if low.startswith("clang"):
            hits = glob.glob(os.path.join(install_root, "VC", "Tools", "Llvm",
                                          "x64", "bin", "clang-cl.exe"))
            hits += glob.glob(os.path.join(install_root, "VC", "Tools", "Llvm",
                                           "bin", "clang-cl.exe"))
            (found if hits else missing).append(toolset)
            continue

        # v140 / v141 / v142 / v143 ... → VC\Tools\MSVC\<ver>\bin\Hostx64\x64\cl.exe
        pattern = os.path.join(install_root, "VC", "Tools", "MSVC", "*",
                               "bin", "Hostx64", "x64", "cl.exe")
        versions = []
        for hit in glob.glob(pattern):
            # ...\VC\Tools\MSVC\<ver>\bin\Hostx64\x64\cl.exe → 上溯 4 级才是 <ver>
            ver = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(hit)))))
            if ver:
                versions.append(ver)
        if versions:
            versions.sort(key=_ver_key, reverse=True)
            found.append(f"{toolset} ({versions[0]})")
        else:
            missing.append(toolset)

    if not found:
        return False, "", missing
    return (not missing), "、".join(found), missing


def probe_sdk(required: str = DEFAULT_SDK) -> Tuple[bool, str]:
    """校验 Windows SDK。

    ``<WindowsTargetPlatformVersion>10.0</...>`` 意为任意 10.0.x 即可，
    因此只要 ``Include\\10.0.*`` 与 ``Lib\\<ver>\\um\\x64`` 存在就算通过。

    Returns:
        ``(ok, version)``。
    """
    for kits in _WIN_KITS:
        inc_dir = os.path.join(kits, "Include")
        if not os.path.isdir(inc_dir):
            continue
        try:
            names = [n for n in os.listdir(inc_dir)
                     if os.path.isdir(os.path.join(inc_dir, n))]
        except OSError:
            continue
        candidates = [n for n in names
                      if not required or n.startswith(required)]
        if not candidates:
            continue
        candidates.sort(key=_ver_key, reverse=True)
        for ver in candidates:
            lib_dir = os.path.join(kits, "Lib", ver, "um", "x64")
            if os.path.isdir(lib_dir) or os.path.isdir(os.path.join(kits, "Lib", ver)):
                return True, ver
    return False, ""


def check_toolchain(toolsets: Sequence[str],
                    sdk_required: str = DEFAULT_SDK) -> List[CheckItem]:
    """工具链三项：MSBuild / 生成工具集 / Windows SDK。"""
    items: List[CheckItem] = []

    msbuild, edition = locate_msbuild()
    if msbuild:
        items.append(CheckItem("toolchain.msbuild", "MSBuild", STATUS_OK,
                               edition or os.path.dirname(msbuild)))
    else:
        items.append(CheckItem(
            "toolchain.msbuild", "MSBuild", STATUS_MISS, "未找到",
            "安装 VS2022 生成工具，勾选「C++ 桌面开发」",
            FixAction(FIX_URL, "安装生成工具", URL_BUILD_TOOLS)))

        # MSBuild 都没有 → 无从推断安装根，工具集无法检查
        items.append(CheckItem("toolchain.toolset", "生成工具集", STATUS_SKIP,
                               "未检测（MSBuild 缺失）"))
        items.append(CheckItem("toolchain.sdk", "Windows SDK", STATUS_SKIP,
                               "未检测（MSBuild 缺失）"))
        return items

    root = _install_root(msbuild)
    ok, detail, missing = probe_toolsets(root, toolsets)
    if ok:
        items.append(CheckItem("toolchain.toolset", "生成工具集", STATUS_OK, detail))
    else:
        items.append(CheckItem(
            "toolchain.toolset", "生成工具集", STATUS_MISS,
            f"缺 {'、'.join(missing)}",
            "VS 安装里补装对应 MSVC 工具集（工程要求 v143）",
            FixAction(FIX_URL, "安装生成工具", URL_BUILD_TOOLS)))

    ok, ver = probe_sdk(sdk_required)
    if ok:
        items.append(CheckItem("toolchain.sdk", "Windows SDK", STATUS_OK, ver))
    else:
        items.append(CheckItem(
            "toolchain.sdk", "Windows SDK", STATUS_MISS,
            f"未找到 {sdk_required}.*",
            "安装任意 Windows 10/11 SDK（10.0.x 均可）",
            FixAction(FIX_URL, "安装 SDK", URL_WINSDK)))
    return items


# ===========================================================================
# 二、源码工程（快检）
# ===========================================================================
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _cond_matches(cond: str, config: str, platform: str) -> bool:
    """判断 MSBuild 的 ``Condition`` 是否适用于当前配置。

    只处理最常见的 ``'$(Configuration)|$(Platform)'=='Release|x64'`` 形式：
    先把 ``$(Configuration)`` / ``$(Platform)`` 字面替换，再比较 ``'A'=='B'``。
    无 Condition 视为全局适用。
    """
    if not cond:
        return True
    text = (cond.replace("$(Configuration)", config)
                .replace("$(Platform)", platform))
    match = re.search(r"'([^']*)'\s*==\s*'([^']*)'", text)
    if match:
        return match.group(1).strip().lower() == match.group(2).strip().lower()
    # 含其它形式的比较（如 Contains(...)）时保守视为不适用，避免误取配置
    return "==" not in text


def _is_repo_path(token: str) -> bool:
    """AdditionalDependencies 里区分「仓库内相对路径」与「系统库名」。

    系统库形如 ``XINPUT9_1_0.lib``；仓库内形如 ``..\\Nektra\\NktHookLib64.lib``。
    """
    token = (token or "").strip()
    if not token:
        return False
    return (token.startswith("..") or token.startswith("." + os.sep)
            or token.startswith("./") or os.sep in token or "/" in token)


def _parse_vcxproj(path: str, config: str, platform: str) -> dict:
    """解析单个 vcxproj，返回当前配置下的需求片段。"""
    root_el = ET.parse(path).getroot()
    base = os.path.dirname(os.path.abspath(path))
    info = {
        "path": path, "toolset": "", "sdk": "", "libs": [], "refs": [],
        "postbuild": [], "ltcg": False, "sources": 0,
    }

    for el in root_el.iter():
        name = _localname(el.tag)
        cond = el.get("Condition", "")

        if name == "PropertyGroup" and _cond_matches(cond, config, platform):
            for child in el:
                cn = _localname(child.tag)
                if cn == "PlatformToolset" and child.text:
                    info["toolset"] = child.text.strip()
                elif cn == "WindowsTargetPlatformVersion" and child.text:
                    info["sdk"] = child.text.strip()
                elif cn == "WholeProgramOptimization":
                    if (child.text or "").strip().lower() == "true":
                        info["ltcg"] = True

        elif name == "ItemDefinitionGroup" and _cond_matches(cond, config, platform):
            # 注意：ItemDefinitionGroup 里的 PostBuildEvent **必须**在条件匹配时
            # 才收集 —— 顶层 iter() 会无差别遍历到所有配置的后处理命令，不过滤
            # 就会把 Debug/Win32 的 CopyToGames.bat 误当成 Release|x64 的依赖。
            for child in el.iter():
                cn = _localname(child.tag)
                if cn == "WholeProgramOptimization":
                    if (child.text or "").strip().lower() == "true":
                        info["ltcg"] = True
                elif cn == "AdditionalDependencies" and child.text:
                    for token in re.split(r"[;\s]+", child.text):
                        if token.strip():
                            info["libs"].append(token.strip())
                elif cn == "PostBuildEvent":
                    for cc in child:
                        if _localname(cc.tag) == "Command" and cc.text:
                            info["postbuild"].append(cc.text)

        elif name == "ProjectReference":
            include = el.get("Include")
            if include:
                info["refs"].append(os.path.normpath(
                    os.path.join(base, include.replace("\\", os.sep))))

        elif name == "ClCompile":
            include = el.get("Include") or ""
            if include and "*" not in include:
                info["sources"] += 1

    return info


def inspect_project(root: str, config: str = DEFAULT_CONFIG,
                    platform: str = DEFAULT_PLATFORM) -> ProjectNeeds:
    """解析源码工程，得到「当前配置实际需要什么」。

    从入口工程 ``DirectX11\\DirectX11.vcxproj`` 出发，沿 ``ProjectReference``
    递归（去重），汇总全部工具集 / SDK / 静态库 / 后处理依赖目录。
    """
    needs = ProjectNeeds(root=root or "",
                         entry=os.path.join(root or "", ENTRY_PROJECT))
    if not root or not os.path.isdir(root):
        needs.error = "源码目录不存在"
        return needs
    if not os.path.isfile(needs.entry):
        needs.error = f"入口工程缺失: {ENTRY_PROJECT}"
        return needs

    seen = set()
    libs_seen = set()
    queue = [needs.entry]
    while queue:
        path = os.path.normpath(queue.pop(0))
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        try:
            info = _parse_vcxproj(path, config, platform)
        except Exception as e:  # noqa: BLE001 - 单个工程解析失败不拖垮整体
            logger.warning("解析工程失败 %s: %s", path, e)
            needs.error = f"工程解析失败: {os.path.basename(path)}"
            continue

        needs.projects.append(path)
        needs.sources += info["sources"]
        if info["ltcg"]:
            needs.ltcg = True
        if info["toolset"] and info["toolset"] not in needs.toolsets:
            needs.toolsets.append(info["toolset"])
        if info["sdk"] and info["sdk"] not in needs.sdks:
            needs.sdks.append(info["sdk"])

        for raw in info["libs"]:
            if not _is_repo_path(raw):
                continue
            abs_path = os.path.normpath(os.path.join(os.path.dirname(path), raw))
            if abs_path.lower() in libs_seen:
                continue
            libs_seen.add(abs_path.lower())
            try:
                rel = os.path.relpath(abs_path, os.path.abspath(root))
            except ValueError:
                rel = raw
            needs.libs.append({"raw": raw, "abs": abs_path, "rel": rel})

        for ref in info["refs"]:
            if os.path.normpath(ref) not in (os.path.normpath(r) for r in needs.refs):
                needs.refs.append(ref)
            if os.path.isfile(ref):
                queue.append(ref)

        for cmd in info["postbuild"]:
            for match in _DEP_DIR_RE.finditer(cmd):
                dirname = match.group(1)
                if dirname not in needs.dep_dirs:
                    needs.dep_dirs.append(dirname)

    needs.parsed = bool(needs.projects)
    return needs


def check_project(needs: ProjectNeeds, config: str = DEFAULT_CONFIG,
                  platform: str = DEFAULT_PLATFORM) -> List[CheckItem]:
    """源码工程四项：源码 / sln / 依赖工程 / 静态库 / 后处理资源。"""
    items: List[CheckItem] = []
    root = needs.root
    label = _project_label(root)

    # --- 源码是否下载 ---
    if not root or not os.path.isdir(root):
        items.append(CheckItem(
            "project.source", "源码工程", STATUS_MISS, "未下载",
            "先在「选择版本」里点「下载」拉取源码",
            FixAction(FIX_REFETCH, "下载源码", "")))
        return items
    items.append(CheckItem("project.source", "源码工程", STATUS_OK, label))

    # --- sln + 入口工程 ---
    sln = os.path.join(root, SOLUTION_NAME)
    missing = [p for p in (sln, needs.entry) if not os.path.isfile(p)]
    if missing:
        items.append(CheckItem(
            "project.sln", "解决方案", STATUS_MISS,
            f"缺 {os.path.basename(missing[0])}",
            "源码不完整，重新下载可修复",
            FixAction(FIX_REFETCH, "重新下载源码", "")))
    else:
        detail = f"{config}|{platform}"
        if needs.sources:
            detail += f" · {needs.sources} 个源文件"
        items.append(CheckItem("project.sln", "解决方案", STATUS_OK, detail))

    if not needs.parsed:
        items.append(CheckItem(
            "project.refs", "依赖工程", STATUS_SKIP,
            needs.error or "未解析"))
        return items

    # --- ProjectReference ---
    if needs.refs:
        miss_refs = [r for r in needs.refs if not os.path.isfile(r)]
        if miss_refs:
            items.append(CheckItem(
                "project.refs", "依赖工程", STATUS_MISS,
                f"缺 {len(miss_refs)} 个（共 {len(needs.refs)}）",
                f"缺 {os.path.basename(miss_refs[0])}，源码不完整",
                FixAction(FIX_REFETCH, "重新下载源码", "")))
        else:
            items.append(CheckItem("project.refs", "依赖工程", STATUS_OK,
                                   f"{len(needs.refs)} 个工程齐全"))

    # --- 静态库（链接期 LNK1104 的元凶）---
    if needs.libs:
        miss_libs = [lib for lib in needs.libs if not os.path.isfile(lib["abs"])]
        if miss_libs:
            items.append(CheckItem(
                "project.libs", "静态库", STATUS_MISS,
                f"缺 {', '.join(lib['rel'] for lib in miss_libs[:2])}",
                "源码不完整，链接期会报 LNK1104",
                FixAction(FIX_REFETCH, "重新下载源码", "")))
        else:
            items.append(CheckItem("project.libs", "静态库", STATUS_OK,
                                   f"{len(needs.libs)} 个齐全"))

    # --- PostBuildEvent 依赖的仓库内目录（为空 → 构建末尾 MSB3073）---
    for dirname in needs.dep_dirs:
        path = os.path.join(root, dirname)
        if not os.path.isdir(path):
            items.append(CheckItem(
                f"project.dep.{dirname}", f"{dirname} 目录", STATUS_MISS, "缺失",
                f"构建后步骤要复制 {dirname}\\*，缺失会以 MSB3073 收尾",
                FixAction(FIX_REFETCH, "重新下载源码", "")))
        else:
            try:
                has_item = bool(os.listdir(path))
            except OSError:
                has_item = False
            if has_item:
                items.append(CheckItem(f"project.dep.{dirname}",
                                       f"{dirname} 目录", STATUS_OK, "已就绪"))
            else:
                items.append(CheckItem(
                    f"project.dep.{dirname}", f"{dirname} 目录", STATUS_MISS,
                    "目录为空",
                    f"构建后步骤要复制 {dirname}\\*，为空会以 MSB3073 收尾",
                    FixAction(FIX_REFETCH, "重新下载源码", "")))
    return items


# ===========================================================================
# 三、机器状态（快检）
# ===========================================================================
def _is_locked(path: str) -> bool:
    """文件是否被其它进程占用（以追加方式试探打开）。"""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "a"):
            pass
        return False
    except (PermissionError, OSError):
        return True


def check_runtime(root: str, optimize: bool = False,
                  config: str = DEFAULT_CONFIG,
                  platform: str = DEFAULT_PLATFORM) -> List[CheckItem]:
    """机器状态：产物占用 / 磁盘空间 / 路径长度 / 注入基线。"""
    items: List[CheckItem] = []

    # --- 产物被占用（游戏 / XXMI 运行时会锁 d3d11.dll）---
    dll = os.path.join(root or "", "x64", config, "d3d11.dll")
    if os.path.isfile(dll) and _is_locked(dll):
        items.append(CheckItem(
            "runtime.lock", "产物占用", STATUS_WARN, "d3d11.dll 被占用",
            "可能游戏正在运行，关闭后重试",
            FixAction(FIX_RETRY, "关闭游戏后重试", "")))

    # --- 磁盘空间 ---
    base = root or os.getcwd()
    try:
        usage = shutil.disk_usage(os.path.abspath(base))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < MIN_FREE_GB:
            items.append(CheckItem(
                "runtime.disk", "磁盘空间", STATUS_WARN,
                f"仅剩 {free_gb:.1f} GB",
                f"LTCG 中间产物较大，建议预留 ≥ {MIN_FREE_GB:.0f} GB",
                FixAction(FIX_DIR, "打开目录", os.path.abspath(base))))
    except Exception:  # noqa: BLE001
        pass

    # --- 路径长度（MAX_PATH = 260 是真实风险）---
    if root and len(os.path.abspath(root)) > MAX_ROOT_LEN:
        items.append(CheckItem(
            "runtime.path_len", "路径长度", STATUS_WARN,
            f"{len(os.path.abspath(root))} 字符",
            f"超过 {MAX_ROOT_LEN} 字符，深层子目录可能触到 MAX_PATH",
            FixAction(FIX_DIR, "打开目录", os.path.abspath(root))))

    # --- 注入基线（仅「优化构建」开启时才有意义）---
    if optimize and root:
        try:
            _bundle = load_ritual_bundle()
            if _bundle is None:
                total = have = 0
            else:
                ALL_SAFE_FILES = _bundle.ALL_SAFE_FILES
                total = len(ALL_SAFE_FILES)
                have = sum(1 for rel in ALL_SAFE_FILES
                           if os.path.isfile(os.path.join(root, rel) + ".clean"))
        except Exception:  # noqa: BLE001 - 拿不到清单就跳过这一项
            total = have = 0
        if total and have < total:
            items.append(CheckItem(
                "inject.baseline", "注入基线", STATUS_WARN,
                f"{have}/{total} 个基线",
                "首次构建会自动生成基线，不影响构建",
                FixAction(FIX_NONE, "无需处理", "")))
    return items


# ===========================================================================
# 四、汇总
# ===========================================================================
def run_report(root: str = "", config: str = DEFAULT_CONFIG,
               platform: str = DEFAULT_PLATFORM, optimize: bool = False,
               with_toolchain: bool = True) -> EnvReport:
    """跑一次完整检测，返回 :class:`EnvReport`。

    Args:
        root: 源码工程根目录；为空表示源码尚未下载（项目类检查直接判 MISS）。
        config / platform: 构建配置，决定解析 vcxproj 时取哪一组属性。
        optimize: 「优化构建」开关，决定是否检查注入基线。
        with_toolchain: False 时跳过工具链（省掉 vswhere 子进程，供单测用）。
    """
    report = EnvReport(config=config, platform=platform,
                       project=_project_label(root))
    try:
        needs = inspect_project(root, config, platform) if root else ProjectNeeds(
            root=root, entry=os.path.join(root or "", ENTRY_PROJECT))

        toolsets = needs.toolsets or [DEFAULT_TOOLSET]
        sdk_required = needs.sdks[0] if needs.sdks else DEFAULT_SDK

        if with_toolchain:
            report.items.extend(check_toolchain(toolsets, sdk_required))
        report.items.extend(check_project(needs, config, platform))
        report.items.extend(check_runtime(root, optimize, config, platform))

        if needs.ltcg and needs.parsed:
            report.items.append(CheckItem(
                "build.ltcg", "全程序优化", STATUS_WARN, "已开启（LTCG）",
                "链接期较慢且吃内存，属正常现象",
                FixAction(FIX_NONE, "无需处理", "")))
    except Exception as e:  # noqa: BLE001 - 检测本身绝不能抛
        logger.error("构建环境检测异常: %s", e, exc_info=True)
        report.error = f"检测出错: {e}"
    return report


# ===========================================================================
# 五、MSBuild 失败日志 → 人话
# ===========================================================================
#: (正则, 标题, 建议, fix.kind, fix.payload)
_ERROR_RULES = (
    (r"MSB8020", "缺少生成工具",
     "工程要求的 MSVC 工具集没装（通常是 v143），补装后重试",
     FIX_URL, URL_BUILD_TOOLS),
    (r"MSB8036", "缺少 Windows SDK",
     "装任意 Windows 10/11 SDK（10.0.x 均可）后重试",
     FIX_URL, URL_WINSDK),
    (r"MSB3073", "构建后步骤失败",
     "多半是 Dependencies 目录为空或路径异常，重新下载源码可修复",
     FIX_REFETCH, ""),
    (r"LNK1104", "静态库缺失",
     "源码不完整，缺链接所需的 .lib，重新下载源码可修复",
     FIX_REFETCH, ""),
    (r"C1083", "头文件缺失",
     "源码不完整，重新下载源码可修复",
     FIX_REFETCH, ""),
    (r"LNK1000|LNK1318|LNK1248", "链接器内存不足",
     "LTCG 很吃内存：关闭「优化构建」或先释放内存再试",
     FIX_NONE, ""),
    (r"LNK2038|C1047", "工具集混用",
     "部分库由旧编译器生成，清理源码目录下的 x64\\ 中间目录后重试",
     FIX_DIR, ""),
    (r"MSB4166|MSB4223", "工程文件损坏",
     "vcxproj 无法解析，重新下载源码可修复",
     FIX_REFETCH, ""),
    (r"MSB4019|MSB4025", "工程文件损坏",
     "vcxproj 格式异常，重新下载源码可修复",
     FIX_REFETCH, ""),
)


def translate_error(log_text: str) -> Optional[Tuple[str, str, FixAction]]:
    """把 MSBuild 输出翻译成 ``(标题, 建议, 修复动作)``。

    命中不了任何规则时返回 None，调用方应原样展示原始日志。
    """
    text = log_text or ""
    for pattern, title, hint, kind, payload in _ERROR_RULES:
        if re.search(pattern, text):
            return title, hint, FixAction(kind, _FIX_LABELS.get(kind, ""), payload)
    return None


_FIX_LABELS = {
    FIX_URL: "打开下载页",
    FIX_DIR: "打开目录",
    FIX_REFETCH: "重新下载源码",
    FIX_RETRY: "重试",
    FIX_NONE: "",
}


def describe_build_error(log_text: str) -> str:
    """给失败信息加一行「人话」前缀，保留原始日志尾巴。

    UI 气泡与日志都能直接展示；CLI 调用同样受益。
    """
    text = (log_text or "").strip()
    if not text:
        return text
    translated = translate_error(text)
    if not translated:
        return text
    title, hint, fix = translated
    lines = [f"构建失败：{title}", hint]
    if fix and fix.label:
        lines.append(f"建议：{fix.label}")
    tail = "\n".join(text.splitlines()[-3:])
    if tail:
        lines.append("—" * 20)
        lines.append(tail)
    return "\n".join(lines)
