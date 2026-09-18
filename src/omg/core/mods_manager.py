"""
omg.core.mods_manager — 扫描 GIMI/Mods、按角色目录自动分类、手动归类持久化。

职责边界（纯业务逻辑，不依赖 PySide6）：
  * 扫描目录：返回 Mods 下的文件与文件夹条目（名称 / 路径 / 类型 / 大小 / 时间 /
    所属分组 / 启用状态）。扫描模型借鉴 nahida-desktop 的「分组 / 模组」两层结构，
    见 scan_mods；
  * 自动分类：用 gachabase 角色目录的别名把条目名匹配到「角色」；武器 / 怪物无线上
    数据源，默认归入「其它」；
  * 手动归类：用户在「其它」里把条目改归到 角色 / 武器 / 怪物，写入
    ``cache/mods_classification.json`` 覆盖表，下次扫描自动生效（跨会话持久）。
  * 视图聚合：根据一级 / 二级筛选把分类结果整理成「分组」列表交给 UI 渲染。

两种分类模式**互斥**（由 ``cache/mods_classification.json`` 的 mode 字段持久）：

  * ``folder`` 文件夹模式：Mods 下的物理分组（第一层目录名）就是分类，
    不做任何名称猜测、也不读覆盖表——拖文件夹即完成归类，所见即所得；
  * ``auto`` 自动分类模式（默认）：忽略物理分组，单条优先级为
      1. 命中手动覆盖表 → 用覆盖值（source="manual"）；
      2. 匹配角色目录（见 gachabase.match_character，最长别名子串优先）→ 角色；
      3. 都不中 → 「其它」（source="none"）。

两套逻辑彻底分开、互不干扰：文件夹模式不读覆盖表、不猜角色；
自动分类模式不读 group 字段。这样物理层级与虚拟分类不会争夺真相源——
选了哪个模式，就只有那套规则生效，不存在「以谁为准」的歧义。
"""

from __future__ import annotations

import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from omg.core import gachabase as gb

#: 类别中文标签
CATEGORY_LABELS = {
    gb.CATEGORY_CHARACTER: "角色",
    gb.CATEGORY_WEAPON: "武器",
    gb.CATEGORY_MONSTER: "怪物",
    gb.CATEGORY_OTHER: "其它",
}

#: 分类模式（互斥）。文件夹模式用物理层级，自动模式用名称匹配 + 覆盖表。
#: 两者彻底分开：folder 不读覆盖表也不猜角色，auto 不读 group 字段。
MODE_FOLDER = "folder"
MODE_AUTO = "auto"
DEFAULT_MODE = MODE_AUTO
MODE_LABELS = {MODE_FOLDER: "文件夹", MODE_AUTO: "自动分类"}

#: 未分组（模组直接放在 Mods 根目录，group 为空串）的键与显示名
UNGROUPED_KEY = ""
UNGROUPED_LABEL = "未分组"

#: 分类来源（UI 角标用），标明「它为什么被归到这里」
SOURCE_LABELS = {
    "manual": "手动",    # 命中手动覆盖表
    "auto": "自动",      # 名称匹配到角色
    "none": "未识别",    # 自动模式下没匹配上，落到「其它」
    "folder": "文件夹",  # 文件夹模式：分组即物理目录
}

#: 手动归类覆盖表路径（与角色缓存同目录）
OVERRIDE_FILE = os.path.join(gb.CACHE_DIR, "mods_classification.json")

#: 切换键 / 缩略图索引缓存路径（与覆盖表同目录）。按 mod 绝对路径缓存其 mtime+size
#: 与解析结果，未变则跳过目录遍历与 .ini 读取，显著加速重复打开 Mod 页（机械盘尤甚）。
INDEX_FILE = os.path.join(gb.CACHE_DIR, "mods_index.json")
#: 索引文件结构版本；结构变更时 +1，旧版直接整体失效重扫。
INDEX_VERSION = 1

#: 缩略图候选扩展名
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
#: 缩略图排除词（借鉴 nahida-desktop）：这些是贴图资源，不是给人看的预览图，
#: 若不加排除，mod 文件夹里的 normal/light/material/diffuse 贴图会被误当封面。
_PREVIEW_EXCLUDE = ("normal", "light", "material", "diffuse")
#: 次要提示词（preview 已单独按最高优先级处理，这里放次一档的封面常见命名）
_PREVIEW_HINTS = ("thumb", "banner", "cover")
#: 预览搜索深度：默认只看模组根目录一层；需要时可放宽到 3 层
_PREVIEW_DEPTH_ROOT = 1
_PREVIEW_DEPTH_DEEP = 3

#: 判定「裸模组」所需的模组签名扩展名——只有这些才说明这是个可被 3DMigoto
#: 加载的 mod，而非仅含贴图 / 预览图的纯资源目录。图片扩展名见 _IMAGE_EXTS，
#: 不在此列；一个只装了一张 png 预览图的文件夹不应被当成模组（逻辑优化）。
_MOD_SIGNATURE_EXTS = {
    ".ini", ".modinfo", ".cfg", ".dll", ".fx", ".hlsl",
    ".meltext", ".nvdb", ".melbasis",
}

#: 容器最多下钻层数。正常结构两层就够（分组/模组），放宽到 4 是为了兼容
#: Hub 这类聚合目录；超过则停止，防止异常深的目录树拖慢扫描。
_MAX_CONTAINER_DEPTH = 4

#: 3DMigoto 生态惯例：文件夹加 DISABLED 前缀表示停用（与 nahida-desktop 同款）。
#: 要求带分隔符（"DISABLED Foo" / "DISABLED_Foo"），"DisabledFoo" 连写不算。
_DISABLED_RE = re.compile(r"^(?:disabled[\s_]*)+[\s_]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def _dir_has_ini(path: str) -> bool:
    """目录自身（非递归）是否直接包含 .ini 文件。"""
    try:
        return any(c.lower().endswith(".ini") for c in os.listdir(path))
    except OSError:
        return False


def _has_any_file(path: str) -> bool:
    """目录内（递归）是否存在任意普通文件——找到第一个立刻返回（早停）。

    借鉴 nahida-desktop 的 ``hasAnyFile``：判定「这个目录到底有没有实际内容」。
    用显式栈而非递归，且命中即返回，因此即便目录很深、文件很多，代价也只到
    「发现第一个文件」为止。空目录 / 全是空子目录 → False。
    """
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            return True
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def _has_mod_signature_file(path: str) -> bool:
    """目录内（递归）是否存在带模组签名的文件（.ini/.modinfo/.dll/...）。

    与 ``_has_any_file`` 的区别：图片（png/jpg/...）、readme 这类纯资源文件
    不算。用于「裸模组」判定——一个只有预览 png 的文件夹不是模组。
    """
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            if os.path.splitext(e.name)[1].lower() \
                                    in _MOD_SIGNATURE_EXTS:
                                return True
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def is_disabled(name: str) -> bool:
    """目录名是否表示「已禁用」。

    3DMigoto 生态惯例：给文件夹加 ``DISABLED`` 前缀来停用模组
    （nahida-desktop 同款正则 ``(?i)^(?:disabled[\\s_]*)+[\\s_]+``）。
    注意必须带分隔符（``DISABLED Foo`` / ``DISABLED_Foo``），
    ``DisabledFoo`` 这类连写不算，避免误伤正常命名。
    """
    return bool(_DISABLED_RE.match(name.strip()))


#: 启用时用于摘除 DISABLED 前缀（与 _DISABLED_RE 对称：要求带分隔符）。
_DISABLED_STRIP_RE = re.compile(r"^(?:disabled[\s_]+)+", re.IGNORECASE)


def _strip_disabled(name: str) -> str:
    """摘除开头的 DISABLED 前缀（可叠加多个）。

    与 ``is_disabled`` 对称：只摘带分隔符的（``DISABLED Foo`` / ``DISABLED_Foo``），
    ``DisabledFoo`` 连写不摘，避免误伤正常命名。
    """
    return _DISABLED_STRIP_RE.sub("", name.strip())


def _rename_unique(path: str, new_name: str) -> str:
    """重命名为 new_name；若目标已存在则追加 `` (n)`` 直到不冲突。

    沿用 nahida-desktop 的 ``renameUnique`` 思路，避免启用/禁用时与目标同名而失败。
    """
    parent = os.path.dirname(path)
    cand = os.path.join(parent, new_name)
    if not os.path.exists(cand):
        os.rename(path, cand)
        return cand
    base, ext = os.path.splitext(new_name)
    i = 1
    while True:
        cand = os.path.join(parent, f"{base} ({i}){ext}")
        if not os.path.exists(cand):
            os.rename(path, cand)
            return cand
        i += 1


def set_mod_enabled(path: str, enabled: bool) -> Tuple[bool, str, str]:
    """切换单个模组的启用 / 禁用（照搬 nahida-desktop：加 / 去 ``DISABLED`` 前缀重命名）。

    这是**真实的磁盘改动**：直接重命名 GIMI/Mods 下的文件夹或松散 .ini，
    3DMigoto 靠前缀判断加载与否（下次注入时生效）。

    Args:
        path: 模组文件夹或松散 .ini 的当前路径。
        enabled: True=启用（摘除前缀），False=禁用（加前缀）。
    Returns:
        (ok, new_path, msg)：成功时 ok=True、new_path 为重命名后的路径；
        失败 ok=False、new_path 退回原路径、msg 为错误原因。已是目标状态则 ok=True 不改名。
    """
    if not os.path.exists(path):
        return (False, path, "路径不存在")
    name = os.path.basename(path)
    try:
        if enabled and is_disabled(name):
            new_path = _rename_unique(path, _strip_disabled(name) or name)
            return (True, new_path, "")
        if (not enabled) and (not is_disabled(name)):
            new_path = _rename_unique(path, "DISABLED " + name)
            return (True, new_path, "")
    except OSError as e:
        return (False, path, str(e))
    return (True, path, "")  # 已是目标状态，无需改动


# ---------------------------------------------------------------------------
# 切换键解析（port of nahida-desktop internal/mod/scanner.go）
# ---------------------------------------------------------------------------
# 3DMigoto / GIMI 的 mod 用 .ini 里的 [Key*] 段声明「切换键」：按键触发时把某个
# $变量在若干取值间循环（如换装、换姿态）。Nahida Desktop 用原生模块/Go 扫描这些
# 段并展示给用户；OMGLite 这里做等价的纯 Python 解析，把结果挂到条目上供 UI 显示。
# 真正的「按键 → 切换动作」映射仍由游戏内 3DMigoto 在 F10 重载时完成，OMGLite 只
# 负责识别与展示这些绑定（不拦截游戏内按键）。

def _parse_ini_file(path: str) -> List[dict]:
    """解析单个 .ini，提取其中所有 [Key*] 切换键段（port of nahida parseINI）。

    规则与 nahida 对齐：逐行读取、去 UTF-8 BOM（仅首行）、跳过空行与 ``;``/``#``
    注释、跟踪 ``[Section]`` 头；段切换时调用 ``_section_toggle`` 尝试产出一个
    ToggleKey。键名统一小写后存入，便于查找 ``key``/``back``/``type`` 与 ``$`` 变量。
    """
    result: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return result
    section = ""
    values: Dict[str, str] = {}

    def flush() -> None:
        tk = _section_toggle(section, os.path.basename(path), values)
        if tk is not None:
            result.append(tk)

    if lines and lines[0] and lines[0][:1] == "\ufeff":
        lines[0] = lines[0][1:]
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            section = line[1:-1]
            values = {}
            continue
        if section:
            at = line.find("=")
            if at >= 0:
                k = line[:at].strip().lower()
                v = line[at + 1:].strip()
                values[k] = v
    flush()
    return result


def _section_toggle(section: str, file_name: str,
                    data: Dict[str, str]) -> Optional[dict]:
    """从一个 [Key*] 段产出单个 ToggleKey（port of nahida sectionToggle）。

    识别条件：段名以小写 ``key`` 开头；段内存在 ``$`` 变量；该变量按逗号拆分后
    至少有 2 个取值，**或** ``type=hold``（单值也可循环）。命中的变量取第一个，
    返回其 section / 文件 / key / back / type / variable / values / current_value。
    """
    if not section.lower().startswith("key"):
        return None
    type_value = data.get("type")
    is_hold = type_value is not None and type_value.lower() == "hold"
    variables = sorted(k for k in data if k.startswith("$"))
    for variable in variables:
        parts = [p.strip() for p in data[variable].split(",")]
        if len(parts) < 2 and not is_hold:
            continue
        current = parts[0] if parts else ""
        return {
            "section_name": section,
            "ini_file_name": file_name,
            "key": data.get("key") or None,
            "back": data.get("back") or None,
            "type": type_value or None,
            "variable": variable,
            "values": parts,
            "current_value": current or None,
        }
    return None


def parse_toggle_keys(item: dict) -> List[dict]:
    """收集一个 mod 条目的全部切换键（目录递归找 .ini，或松散 .ini 本身）。

    Args:
        item: 扫描条目字典，读 path / is_dir / ext。
    Returns:
        切换键字典列表（结构见 ``_section_toggle``），无则空列表。
    """
    path = item.get("path", "")
    if not path:
        return []
    ini_paths: List[str] = []
    if item.get("is_dir"):
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith(".ini"):
                    ini_paths.append(os.path.join(root, fn))
    elif item.get("ext") == ".ini":
        ini_paths.append(path)
    else:
        return []
    out: List[dict] = []
    for p in ini_paths:
        out.extend(_parse_ini_file(p))
    return out


#: 切换键内部字符串（如 ``vk_f1`` / ``ctrl vk_f1``）→ 可读标签的查表，
#: 移植自 nahida-desktop frontend/src/shared/key-formatter.ts（精简常用项）。
_KEY_LABEL_MAP = {
    "vk_control": "Ctrl", "control": "Ctrl",
    "vk_lcontrol": "Ctrl", "vk_rcontrol": "Ctrl",
    "vk_menu": "Alt", "alt": "Alt",
    "vk_lmenu": "Alt", "vk_rmenu": "Alt",
    "vk_shift": "Shift", "shift": "Shift",
    "vk_lshift": "Shift", "vk_rshift": "Shift",
    "vk_lwin": "Win", "vk_rwin": "Win",
    "vk_up": "↑", "up": "↑",
    "vk_down": "↓", "down": "↓",
    "vk_left": "←", "left": "←",
    "vk_right": "→", "right": "→",
    "vk_return": "Enter", "enter": "Enter",
    "vk_space": "Space", "vk_escape": "Esc", "escape": "Esc",
    "vk_back": "Backspace", "vk_tab": "Tab",
    "vk_delete": "Del", "vk_insert": "Ins",
    "vk_prior": "PgUp", "vk_next": "PgDn",
    "vk_home": "Home", "vk_end": "End",
    "vk_oem_3": "`", "vk_oem_minus": "-", "vk_oem_plus": "+",
    "vk_oem_comma": ",", "vk_oem_period": ".",
    "vk_oem_1": ";", "vk_oem_2": "/", "vk_oem_4": "[",
    "vk_oem_5": "\\", "vk_oem_6": "]", "vk_oem_7": "'",
}


def format_toggle_key(key_str: Optional[str]) -> str:
    """把一个内部键串（如 ``ctrl vk_f1``）渲染成可读标签（如 ``Ctrl + F1``）。

    规则：``no_`` 前缀是「禁止同时按住」约束、不是键，跳过；``VK_``/``XB_`` 前缀
    剥离后按 F\\d / 单字符 / NUMPAD 等处理；其余原样保留。多键用 `` + `` 连接。
    """
    if not key_str:
        return ""
    labels: List[str] = []
    for tok in key_str.strip().split():
        lt = tok.lower()
        if lt.startswith("no_"):
            continue
        if lt in _KEY_LABEL_MAP:
            labels.append(_KEY_LABEL_MAP[lt])
            continue
        base = tok.upper()
        if base.startswith("VK_"):
            stripped = base[3:]
            if len(stripped) == 1 or re.match(r"^F\d+$", stripped):
                labels.append(stripped)
            elif stripped.startswith("NUMPAD"):
                labels.append("Num" + stripped[6:])
            else:
                labels.append(stripped)
        elif base.startswith("XB_"):
            labels.append(base[3:])
        else:
            labels.append(tok)
    return " + ".join(labels)


def format_toggle_keys_tip(item: dict) -> str:
    """生成该 mod 切换键明细的多行文本（供 OMGPopCard 悬浮卡片显示）。

    每个切换键一组，组间用 ``---`` 行分隔（渲染成发丝分隔线）；组名用
    ``**…**`` 包裹（渲染成加粗行）；「键: X · 类型」把类型与键帽放**同行**
    （`` · `` 尾注语法，见 ``omg.ui.OMGPopCard._split_blocks``）；没有键行时
    类型落到回退行，键/回退都没有则退化为独立「类型 X」纯文本行。没有切换
    键返回空串。不展示变量与取值（信息过细、易挤窄气泡）。
    """
    tks = item.get("toggle_keys") or []
    if not tks:
        return ""
    lines: List[str] = []
    for i, tk in enumerate(tks):
        if i:
            lines.append("---")
        lines.append(f"**{tk.get('section_name') or '切换键'}**")
        key = format_toggle_key(tk.get("key"))
        back = format_toggle_key(tk.get("back"))
        typ = (tk.get("type") or "").strip()
        suffix = f" · {typ}" if typ else ""
        if key:
            lines.append(f"键: {key}{suffix}")
        if back:
            lines.append(f"回退: {back}{'' if key else suffix}")
        if not key and not back and suffix:
            lines.append(f"类型 {typ}")
    return "\n".join(lines)


def _make_item(name: str, path: str, is_dir: bool, group: str) -> dict:
    """构造条目。stat 失败时退化为空大小 / 0 时间，不让单个坏条目中断整轮扫描。"""
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    return {
        "name": name,
        "path": path,
        "is_dir": is_dir,
        "size": size,
        "mtime": mtime,
        "ext": os.path.splitext(name)[1].lower(),
        # 所属分组（Mods 下的第一层容器名；模组直接放在根目录下时为 ""）
        "group": group,
        # 是否启用（目录名带 DISABLED 前缀 → 停用，UI 可据此置灰 / 加角标）
        "enabled": (not is_disabled(name)) if is_dir else True,
    }


def _list_entries(container: str) -> Tuple[List[Tuple[str, str]],
                                           List[Tuple[str, str]],
                                           List[Tuple[str, str]]]:
    """列出 container 的直接子项，返回 (子目录, .ini 文件, 其它文件) 三元组。

    只取 (name, path) 字符串而非 DirEntry：DirEntry 的缓存属性在 scandir
    迭代器关闭后不保证可用，这里全程用路径重新 stat，行为更可预期。
    """
    dirs: List[Tuple[str, str]] = []
    inis: List[Tuple[str, str]] = []
    others: List[Tuple[str, str]] = []
    try:
        with os.scandir(container) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append((e.name, e.path))
                    elif e.name.lower().endswith(".ini"):
                        inis.append((e.name, e.path))
                    else:
                        others.append((e.name, e.path))
                except OSError:
                    continue
    except OSError:
        pass
    return dirs, inis, others


def _collect(container: str, group: str, depth: int, out: List[dict]) -> None:
    """扫描 container 的直接子项，把「模组」收进 out；容器则递归下钻。

    判定规则（借鉴 nahida-desktop 的两层模型，并放宽为「容器可下钻」）：
      * 子目录**自身含 .ini** → 是模组，收录后**不再下钻**。
        这是关键：模组内部的 ``textures/`` 等资源子目录不会被误当成模组，
        它们本就是该模组的一部分，而不是独立条目。
      * 子目录**不含 .ini** → 先递归探查内部：
          - 内部找到模组 → 它是容器（如 ``Hub``），自身**不**收录；
          - 内部无模组、但自己有任意文件 → 它是「无 .ini 的裸模组」
            （只有贴图 / 其它资源，nahida 的 ``hasAnyFile`` 判定），收录；
          - 内部无模组且自己也没内容 → 空壳，跳过。

    这样既支持 ``Mods/Character/AmberMod`` 的标准两层结构，也能正确穿透
    ``Mods/Hub/...`` 这类聚合目录，同时不会把资源子目录重复列成模组。

    松散的 .ini（直接躺在容器里的）也作为文件模组收录——3DMigoto 会加载它；
    顶层（depth 0）的其它文件一并收录，容器内的非 .ini 文件则忽略。
    """
    if depth > _MAX_CONTAINER_DEPTH:
        return
    dirs, inis, others = _list_entries(container)

    # 容器自身不是模组，其直属 .ini 就是「松散模组」
    for name, path in inis:
        out.append(_make_item(name, path, False, group))
    if depth == 0:
        for name, path in others:
            out.append(_make_item(name, path, False, group))

    pending: List[Tuple[str, str]] = []
    for name, path in dirs:
        if _dir_has_ini(path):
            out.append(_make_item(name, path, True, group))
        else:
            pending.append((name, path))

    for name, path in pending:
        before = len(out)
        # 第一层子目录名即「分组」；更深层继承上层分组
        _collect(path, name if depth == 0 else group, depth + 1, out)
        if len(out) == before and _has_mod_signature_file(path):
            # 不含 .ini 也没有更深层的模组，但自身带了可被加载的模组签名文件
            # （.ini/.modinfo/.dll 等）→ 视为「裸模组」收录；
            # 仅有图片 / readme 的纯资源目录不算模组，回落为容器（文件夹）。
            out.append(_make_item(name, path, True, group))


def scan_mods(mods_dir: str) -> List[dict]:
    """扫描 Mods 目录，返回条目列表（模组文件夹 / 松散 .ini / 顶层文件）。

    结构假设（与 nahida-desktop 一致）：推荐 ``Mods/<分组>/<模组>`` 两层，
    但也兼容直接把模组放在 ``Mods`` 根目录，以及 ``Hub`` 这类更深的聚合目录。

    返回的每条：{name, path, is_dir, size, mtime, ext, group, enabled}
      * 文件夹排在文件之前，均按修改时间倒序（最新模组在最前）；
      * 隐藏条目（以 . 开头）跳过；符号链接目录不跟随（避免环路）。
    目录不存在时返回空列表（面板据此显示空态）。
    """
    if not mods_dir or not os.path.isdir(mods_dir):
        return []
    items: List[dict] = []
    _collect(mods_dir, "", 0, items)
    items.sort(key=lambda x: (0 if x["is_dir"] else 1, -x["mtime"]))
    return items


def build_folder_tree(mods_dir: str, max_depth: int = 6) -> dict:
    """列出 Mods 下的真实目录层级，供文件夹模式的左侧目录树使用。

    与 ``scan_mods``（产出「条目 = 模组」）不同，这里产出**所有目录**的树
    （含分组目录与 Hub 这类容器），容器也会作为可展开的树节点出现。
    隐藏目录（以 ``.`` 开头）跳过，符号链接不跟随，深度封顶 ``max_depth``
    防止异常深的目录树拖慢构建。

    Returns:
        嵌套节点 ``{"name","path","rel","children":[...]}``，根节点 name 为空、
        path 为 mods_dir；``rel`` 为相对 Mods 的路径（用于持久化 manual_subgroups，
        跨 Mods 目录迁移仍有效）。
    """
    root = {"name": "", "path": mods_dir, "rel": "", "children": []}

    def rec(node: dict, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            with os.scandir(node["path"]) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        child = {
                            "name": e.name,
                            "path": e.path,
                            "rel": os.path.relpath(e.path, mods_dir),
                            "children": [],
                        }
                        node["children"].append(child)
                        rec(child, depth + 1)
        except OSError:
            return

    rec(root, 0)
    return root


def list_folder_children(mods_dir: str, folder_path: str) -> List[str]:
    """返回 folder_path 下的直接子目录名（供 UI 判断树节点是否还有子层）。"""
    try:
        return [e.name for e in os.scandir(folder_path)
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    except OSError:
        return []


def find_thumbnail(item: dict, search_subfolders: bool = False) -> Optional[str]:
    """为条目找一个缩略图文件路径（无则返回 None）。

    借鉴 nahida-desktop 的预览评分，按分数取最优候选：
      * 文件名以 ``preview`` 开头 +1000，仅包含 +500；
      * 包含次要提示词（thumb / banner / cover）+300；
      * 位于根目录（而非子文件夹里）+200——就近优先，也更可能是真正的封面；
      * 命中排除词（normal / light / material / diffuse）直接淘汰：
        这些是给模型用的贴图，不是给人看的预览图，不排除会把它们误当封面；
      * 同分取路径字典序靠前者，保证多次扫描结果稳定。

    ``search_subfolders=False``（默认）只看根目录一层，开销最小；
    置 True 则放宽到 3 层（对应 nahida 的「在子文件夹中搜索预览」设置）。
    """
    if not item["is_dir"]:
        name = os.path.basename(item["path"])
        if item["ext"] not in _IMAGE_EXTS:
            return None
        lower = name.lower()
        if any(w in lower for w in _PREVIEW_EXCLUDE):
            return None
        return item["path"]

    root = item["path"]
    max_depth = _PREVIEW_DEPTH_DEEP if search_subfolders else _PREVIEW_DEPTH_ROOT
    best: Optional[Tuple[int, str]] = None
    stack = [(root, 1)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((e.path, depth + 1))
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if os.path.splitext(e.name)[1].lower() not in _IMAGE_EXTS:
                        continue
                    lower = e.name.lower()
                    if any(w in lower for w in _PREVIEW_EXCLUDE):
                        continue
                    score = 0
                    if lower.startswith("preview"):
                        score += 1000
                    elif "preview" in lower:
                        score += 500
                    elif any(w in lower for w in _PREVIEW_HINTS):
                        score += 300
                    if depth == 1:
                        score += 200
                    cand = (score, e.path)
                    # 高分优先；同分取路径字典序靠前者，保证结果稳定
                    if (best is None or cand[0] > best[0]
                            or (cand[0] == best[0] and cand[1] < best[1])):
                        best = cand
        except OSError:
            continue
    return best[1] if best else None


def find_thumbnail_deep(item: dict) -> Optional[str]:
    """先只看根目录一层，没找到再逐层向下找（UI 缩略图统一入口）。

    绝大多数 Mod / 文件夹的 preview 图就在根目录，一层扫描开销最小；只有根目录
    确实没有候选图时才放宽到 ``_PREVIEW_DEPTH_DEEP`` 层，避免为了少数「预览图
    藏在子目录」的情况让每次扫描都遍历整棵子树。

    注意两级搜索的评分是**分开**算的：根层没图才会下钻，不会出现「深层普通图
    压过根目录 preview 图」的错位（评分里根层 +200 只是同一次搜索内的相对权重）。
    """
    if not item.get("is_dir"):
        return find_thumbnail(item)
    hit = find_thumbnail(item, search_subfolders=False)
    if hit:
        return hit
    return find_thumbnail(item, search_subfolders=True)


def _scan_mod_derived(item: dict) -> Tuple[Optional[str], List[dict]]:
    """单次遍历 mod 子树，同时收集缩略图候选与 .ini 路径（合并原 find_thumbnail_deep + parse_toggle_keys 两次遍历）。

    Returns: (thumb_path_or_None, toggle_keys_list)
      * 缩略图沿用 find_thumbnail_deep 的「根层优先、根层无图再下钻 ≤3 层」评分（见
        _IMAGE_EXTS / _PREVIEW_*），与原两次遍历结果完全一致，且只走 1 次 os.walk；
      * 切换键遍历内全部 .ini（无深度上限，与原 parse_toggle_keys 行为一致）。
    命中索引缓存时 classify_items 不会调用本函数（见 _load_index）。
    """
    path = item.get("path", "")
    if not path:
        return None, []
    # 松散文件：.ini 直接解析；图片自身即缩略图；其它无缩略图也无切换键
    if not item.get("is_dir"):
        ext = (item.get("ext") or "").lower()
        if ext == ".ini":
            return None, _parse_ini_file(path)
        if ext in _IMAGE_EXTS:
            lower = os.path.basename(path).lower()
            if not any(w in lower for w in _PREVIEW_EXCLUDE):
                return path, []
        return None, []
    root = path
    max_depth = _PREVIEW_DEPTH_DEEP
    img_cands: List[Tuple[int, str, int]] = []  # (score, path, depth)
    ini_paths: List[str] = []
    try:
        for cur, _dirs, files in os.walk(root):
            suffix = cur[len(root):].lstrip(os.sep)
            depth = 1 if suffix == "" else suffix.count(os.sep) + 1
            for fn in files:
                lower = fn.lower()
                ext = os.path.splitext(lower)[1]
                if ext in _IMAGE_EXTS:
                    if depth <= max_depth and not any(w in lower for w in _PREVIEW_EXCLUDE):
                        score = 0
                        if lower.startswith("preview"):
                            score += 1000
                        elif "preview" in lower:
                            score += 500
                        elif any(w in lower for w in _PREVIEW_HINTS):
                            score += 300
                        if depth == 1:
                            score += 200
                        img_cands.append((score, os.path.join(cur, fn), depth))
                elif ext == ".ini":
                    ini_paths.append(os.path.join(cur, fn))
    except OSError:
        return None, []
    # 缩略图：根层优先（与 find_thumbnail_deep 两级评分分开算，结果等价）
    root_best = None
    deep_best = None
    for score, p, depth in img_cands:
        cand = (score, p)
        if depth == 1:
            if root_best is None or cand[0] > root_best[0] or (
                    cand[0] == root_best[0] and cand[1] < root_best[1]):
                root_best = cand
        else:
            if deep_best is None or cand[0] > deep_best[0] or (
                    cand[0] == deep_best[0] and cand[1] < deep_best[1]):
                deep_best = cand
    thumb = root_best[1] if root_best is not None else (
        deep_best[1] if deep_best is not None else None)
    # 切换键：解析遍历到的全部 .ini
    out: List[dict] = []
    for p in ini_paths:
        out.extend(_parse_ini_file(p))
    return thumb, out


# ---------------------------------------------------------------------------
# 手动归类覆盖表
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """读取整个状态文件（覆盖表 + 当前模式）。文件缺失 / 损坏 → 空 dict。"""
    if not os.path.isfile(OVERRIDE_FILE):
        return {}
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            data = json_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    _atomic_write_json(OVERRIDE_FILE, state)


def _load_index() -> dict:
    """读取切换键 / 缩略图索引缓存；缺失 / 损坏 / 版本不符 → 空索引（整体重扫）。"""
    if not os.path.isfile(INDEX_FILE):
        return {}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json_load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_index(entries: dict) -> None:
    """原子写回索引缓存（含版本号）。单文件读写，失败静默不阻断扫描。"""
    try:
        _atomic_write_json(INDEX_FILE, {"version": INDEX_VERSION, "entries": entries})
    except OSError:
        pass


def load_mode() -> str:
    """读取当前分类模式（folder / auto）。未设置或损坏时回落 DEFAULT_MODE。"""
    m = _load_state().get("mode")
    return m if m in (MODE_FOLDER, MODE_AUTO) else DEFAULT_MODE


def save_mode(mode: str) -> None:
    """持久化分类模式。非法值按 DEFAULT_MODE 处理。"""
    state = _load_state()
    state.setdefault("overrides", {})
    state["mode"] = mode if mode in (MODE_FOLDER, MODE_AUTO) else DEFAULT_MODE
    _write_state(state)


def load_overrides() -> Dict[str, dict]:
    """读取手动归类覆盖表：{条目名: {category, character_id?}}。

    仅在自动分类模式下参与判定；文件夹模式完全不读它。
    """
    ov = _load_state().get("overrides")
    return ov if isinstance(ov, dict) else {}


def save_override(name: str, category: str,
                  character_id: Optional[str] = None) -> Dict[str, dict]:
    """写入 / 更新单条手动归类，返回最新覆盖表（并保留当前模式）。"""
    state = _load_state()
    overrides = state.get("overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    if category == gb.CATEGORY_OTHER and character_id is None:
        # 「其它」且非来自角色改归：若之前是角色覆盖则清除
        overrides.pop(name, None)
    else:
        rec: Dict[str, Optional[str]] = {"category": category}
        if character_id:
            rec["character_id"] = character_id
        overrides[name] = rec
    state["overrides"] = overrides
    _write_state(state)
    return overrides


def clear_override(name: str) -> Dict[str, dict]:
    """移除单条覆盖（恢复自动分类），并保留当前模式。"""
    state = _load_state()
    overrides = state.get("overrides")
    overrides = overrides if isinstance(overrides, dict) else {}
    if overrides.pop(name, None) is not None:
        state["overrides"] = overrides
        _write_state(state)
    return overrides


# ---------------------------------------------------------------------------
# 「子文件夹树」标记（文件夹模式：右键目录 → 设为可向下展开的子目录树）
# ---------------------------------------------------------------------------

def load_manual_subgroups() -> set:
    """读取「设为子文件夹树」的目录集合（存的是相对 Mods 的路径）。"""
    v = _load_state().get("manual_subgroups")
    return set(v) if isinstance(v, (list, set)) else set()


def save_manual_subgroup(rel: str, enabled: bool) -> set:
    """增删单个目录的「子文件夹树」标记，返回最新集合（持久化到同一状态文件）。"""
    state = _load_state()
    s = set(state.get("manual_subgroups") or [])
    if enabled:
        s.add(rel)
    else:
        s.discard(rel)
    state["manual_subgroups"] = sorted(s)
    _write_state(state)
    return s


def is_manual_subgroup(rel: str) -> bool:
    """某目录是否已被标记为「子文件夹树」（用于打开时自动展开）。"""
    return rel in load_manual_subgroups()


# ---------------------------------------------------------------------------
# 手动「标记为 Mod / 文件夹」覆盖（文件夹模式：用户手动修正某个目录的归类）
# ---------------------------------------------------------------------------

def norm_kind_rel(rel: str) -> str:
    """把相对路径规范化：剥掉末级目录名上的 DISABLED 前缀。

    手动归类键按规范化后的 rel 存储，这样切换启用 / 禁用（加 DISABLED 前缀会
    改末级名）后覆盖依然命中，不会因重命名而丢失。
    """
    parent, base = os.path.split(rel)
    return os.path.join(parent, _strip_disabled(base)) if base else rel


def load_manual_kind() -> Dict[str, str]:
    """读取「标记为 Mod/文件夹」覆盖表：{规范化 rel: "mod" | "folder"}。"""
    v = _load_state().get("manual_kind")
    return v if isinstance(v, dict) else {}


def save_manual_kind(rel: str, kind: str) -> Dict[str, str]:
    """写入 / 更新单条覆盖（kind ∈ {"mod","folder"}），返回最新表并持久化。"""
    state = _load_state()
    k = norm_kind_rel(rel)
    table = state.get("manual_kind")
    table = table if isinstance(table, dict) else {}
    table[k] = kind
    state["manual_kind"] = table
    _write_state(state)
    return table


def get_manual_kind(rel: str) -> Optional[str]:
    """查询某相对路径被手动标记为 mod 还是 folder（None=未手动标记）。"""
    return load_manual_kind().get(norm_kind_rel(rel))


# ---------------------------------------------------------------------------
# 遮罩预览图（UI 侧：缩略图加高斯模糊，用于不便直接展示的 mod）
# ---------------------------------------------------------------------------
def load_masked() -> Dict[str, bool]:
    """读取「遮罩预览图」表：{规范化 rel: True}（只存开启的项）。"""
    v = _load_state().get("masked_thumbs")
    return v if isinstance(v, dict) else {}


def set_masked(rel: str, on: bool) -> None:
    """开启 / 关闭某个 mod 的缩略图遮罩（按规范化 rel 持久化，跟随重命名）。"""
    state = _load_state()
    k = norm_kind_rel(rel)
    table = state.get("masked_thumbs")
    table = table if isinstance(table, dict) else {}
    if on:
        table[k] = True
    else:
        table.pop(k, None)
    state["masked_thumbs"] = table
    _write_state(state)


def is_masked(rel: str) -> bool:
    """该相对路径是否开启了缩略图遮罩。"""
    return bool(load_masked().get(norm_kind_rel(rel)))


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

def classify_items(items: List[dict], chars: List[dict],
                    overrides: Optional[Dict[str, dict]] = None,
                    mode: Optional[str] = None) -> List[dict]:
    """把扫描条目分类为 categorized 列表。

    Args:
        mode: MODE_FOLDER / MODE_AUTO；None 表示读持久化的模式。

    每条新增字段：category / character_id / character(角色记录或 None) /
    element_id / source(来源：manual|auto|none|folder) / thumb。

    文件夹模式不猜角色、也不读覆盖表：物理 group 就是分类，category 统一置
    为「其它」（该模式下它不参与分组），source 置为 "folder"。
    """
    if mode is None:
        mode = load_mode()
    if overrides is None:
        overrides = load_overrides() if mode == MODE_AUTO else {}
    char_by_id = ({c["character_id"]: c for c in chars}
                  if mode == MODE_AUTO else {})
    # 切换键 / 缩略图索引缓存：按 mod 路径缓存 mtime+size 与解析结果，未变则跳过
    # 目录遍历与 .ini 读取（见 _scan_mod_derived / _load_index）。单次扫描只读写一次
    # 索引文件，且全命中时不落盘（dirty=False）。
    index = _load_index()
    seen: set = set()
    dirty = False
    out: List[dict] = []
    for it in items:
        rec = dict(it)
        key = it.get("path", "")
        cached = index.get(key) if key else None
        if (cached is not None
                and cached.get("mtime") == it.get("mtime")
                and cached.get("size") == it.get("size")):
            # 缓存命中：直接复用，跳过本次遍历与解析
            thumb = cached.get("thumb")
            toggle = list(cached.get("toggle_keys") or [])
        else:
            # 缓存未命中或 mod 已改动：单次遍历同时收图候选与 .ini（合并原两次遍历）
            thumb, toggle = _scan_mod_derived(it)
            if key:
                index[key] = {
                    "mtime": it.get("mtime"),
                    "size": it.get("size"),
                    "thumb": thumb,
                    "toggle_keys": toggle,
                }
                dirty = True
        if key:
            seen.add(key)
        rec["thumb"] = thumb
        rec["toggle_keys"] = toggle
        if mode == MODE_FOLDER:
            rec.update({
                "category": gb.CATEGORY_OTHER,
                "character_id": None,
                "character": None,
                "element_id": None,
                "source": "folder",
            })
            out.append(rec)
            continue
        ov = overrides.get(it["name"])
        if ov:
            cat = ov.get("category", gb.CATEGORY_OTHER)
            cid = ov.get("character_id")
            ch = char_by_id.get(cid) if cid else None
            rec.update({
                "category": cat,
                "character_id": cid,
                "character": ch,
                "element_id": ch["element_id"] if ch else None,
                "source": "manual",
            })
            out.append(rec)
            continue
        ch = gb.match_character(it["name"], chars)
        if ch:
            rec.update({
                "category": gb.CATEGORY_CHARACTER,
                "character_id": ch["character_id"],
                "character": ch,
                "element_id": ch["element_id"],
                "source": "auto",
            })
        else:
            rec.update({
                "category": gb.CATEGORY_OTHER,
                "character_id": None,
                "character": None,
                "element_id": None,
                "source": "none",
            })
        out.append(rec)
    # 清理已删除 / 改名的失效索引条目，避免索引无限膨胀；有变更才落盘。
    for stale in [k for k in index if k not in seen]:
        index.pop(stale, None)
        dirty = True
    if dirty:
        _save_index(index)
    return out


# ---------------------------------------------------------------------------
# 视图聚合（供 UI 渲染）
# ---------------------------------------------------------------------------

def list_groups(categorized: List[dict]) -> List[str]:
    """文件夹模式用：结果里出现的物理分组键（名称升序，未分组排最后）。

    UI 拿它动态生成一级筛选项，因此顺序必须稳定可预期——不能随扫描顺序变。
    """
    names = set()
    for c in categorized:
        g = c.get("group") or UNGROUPED_KEY
        if g:
            names.add(g)
    out = sorted(names)
    if any(not (c.get("group") or UNGROUPED_KEY) for c in categorized):
        out.append(UNGROUPED_KEY)
    return out


def build_view(categorized: List[dict], cat1: str, cat2_element: str,
               mode: Optional[str] = None) -> List[dict]:
    """根据当前模式与筛选，把分类结果整理成分组列表。

    Args:
        cat1: 一级筛选。自动模式取 all/character/weapon/monster/other；
              文件夹模式取 all 或具体分组键（空串 = 未分组）。
        cat2_element: 二级元素筛选（all/1..8），**仅自动模式 + cat1==character** 生效
        mode: MODE_FOLDER / MODE_AUTO；None 表示读持久化的模式

    Returns:
        分组列表，每个：{kind, key?, character_id?, element_id?, title, badge?, items}
    """
    if mode is None:
        mode = load_mode()
    if mode == MODE_FOLDER:
        return _folder_groups(categorized, cat1)

    if cat1 == gb.CATEGORY_CHARACTER:
        return _character_groups(categorized, cat2_element)
    if cat1 in (gb.CATEGORY_WEAPON, gb.CATEGORY_MONSTER, gb.CATEGORY_OTHER):
        items = [c for c in categorized if c["category"] == cat1]
        return [{
            "kind": cat1,
            "title": CATEGORY_LABELS[cat1],
            "items": items,
        }] if items else []

    # cat1 == all
    groups: List[dict] = []
    char_groups = _character_groups(categorized, "all")
    groups.extend(char_groups)
    for cat in (gb.CATEGORY_WEAPON, gb.CATEGORY_MONSTER, gb.CATEGORY_OTHER):
        items = [c for c in categorized if c["category"] == cat]
        if items:
            groups.append({
                "kind": cat,
                "title": CATEGORY_LABELS[cat],
                "items": items,
            })
    return groups


def _folder_groups(categorized: List[dict], cat1: str) -> List[dict]:
    """文件夹模式：按物理分组聚合，cat1 为 "all" 或具体分组键。

    分组按名称升序（稳定可预期），「未分组」固定排最后；组内保持扫描顺序
    （修改时间倒序，最新的在前）。
    """
    by_group: Dict[str, dict] = {}
    for c in categorized:
        key = c.get("group") or UNGROUPED_KEY
        g = by_group.get(key)
        if g is None:
            g = {
                "kind": "group",
                "key": key,
                "title": key if key else UNGROUPED_LABEL,
                "items": [],
            }
            by_group[key] = g
        g["items"].append(c)

    keys = sorted(k for k in by_group if k)
    if UNGROUPED_KEY in by_group:
        keys.append(UNGROUPED_KEY)
    groups = [by_group[k] for k in keys]
    # 注意用 != "all" 判断而非真值判断：未分组的键是空串，真值判断会漏筛
    if cat1 != "all":
        groups = [g for g in groups if g["key"] == cat1]
    return groups


def _character_groups(categorized: List[dict], cat2_element: str) -> List[dict]:
    """按角色聚合（仅含命中条目），按角色 id 降序；cat2_element!=all 时按元素过滤。"""
    by_char: Dict[str, dict] = {}
    order: List[str] = []
    for c in categorized:
        if c["category"] != gb.CATEGORY_CHARACTER or not c["character"]:
            continue
        cid = c["character_id"]
        if cid not in by_char:
            by_char[cid] = {
                "kind": gb.CATEGORY_CHARACTER,
                "character_id": cid,
                "element_id": c["element_id"],
                "title": c["character"]["display"],
                "badge": _element_badge(c["character"]),
                "items": [],
                "_sort": int(c["character"]["id"] or 0),
            }
            order.append(cid)
        by_char[cid]["items"].append(c)

    groups = [by_char[cid] for cid in order]
    if cat2_element and cat2_element != "all":
        groups = [g for g in groups if _char_has_element(g, cat2_element)]
    groups.sort(key=lambda g: g["_sort"], reverse=True)
    for g in groups:
        g.pop("_sort", None)
    return groups


def _char_has_element(group: dict, element_id: str) -> bool:
    """角色组是否含某元素：按组内**条目自身的元素**判断（最精确）。

    每个 Mod 条目在分类时已带自己的 element_id（来自匹配到的具体 variant），
    因此「火」筛选只显示确实含有火属性 mod 的角色组——避免旅行者这种全元素角色
    因存在冰 variant 而出现在冰筛选下（尽管某个具体 mod 是火）。
    """
    for it in group["items"]:
        if it.get("element_id") == element_id:
            return True
    return False


def _element_badge(char: dict) -> str:
    elem = gb.ELEMENTS.get(char.get("element_id", ""), {})
    return elem.get("cn", "")


# ---------------------------------------------------------------------------
# JSON 写工具
# ---------------------------------------------------------------------------

def json_load(f) -> dict:
    import json
    return json.load(f)


def _atomic_write_json(path: str, data: dict) -> None:
    import json
    import tempfile
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# .ini 全局差分写回（port of ScriptTools / Global Persist Swapkey.py）
# ---------------------------------------------------------------------------
# 3DMigoto 把「切换键的当前取值」记在 GIMI 根目录的 d3dx_user.ini 里
# （形如 ``$\mods\<模组目录>\<变量名> = 3``，即用户按键切换后的差分值），
# 而 mod 自己的 .ini 里写的是**初始值**（``global persist $xxx = 1``）。
# 游戏重启后持久化变量会复位——本函数把 d3dx_user.ini 里的差分值**写回**各
# mod 的 .ini，使当前状态固化成新的默认值。

#: d3dx_user.ini 中「模组目录\变量名 = 值」行的匹配式（行首，值域为数字）
_MASTER_LINE_RE = re.compile(r"\$\\mods\\([^\\]+)\\(.+?)\s*= ([\d\.eE\-]+)")
#: mod 自身的 .ini 中待替换的 ``global persist $xxx = 值`` 行
_PERSIST_RE = re.compile(r"global persist \$(\w+) = ([\d\.eE\-]+)")
#: 命名空间声明行（有它就按 ``namespace.swapkey`` 在 master 里取值）
_NAMESPACE_RE = re.compile(r"namespace\s*=\s*(.+)")


def collect_ini_files(folder: str, ignore_disabled: bool = True) -> List[str]:
    """递归收集 folder 下的全部 .ini 文件（按路径排序，结果稳定）。

    Args:
        folder: 起始目录（mod 目录 / 当前浏览目录皆可）。
        ignore_disabled: True 时跳过**文件名**含 DISABLED 的（对应被停用的
            松散 ini；目录被停用不影响其内部的 ini，与原脚本行为一致）。
    """
    out: List[str] = []
    if not folder or not os.path.isdir(folder):
        return out
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if os.path.splitext(fn)[1].lower() != ".ini":
                continue
            if ignore_disabled and "DISABLED" in fn.upper():
                continue
            out.append(os.path.join(root, fn))
    return sorted(out)


def _read_text(path: str) -> Optional[str]:
    """读 ini 文本（UTF-8 严格；非 UTF-8 返回 None，由调用方跳过并计数）。"""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _master_ini_path(mods_dir: str) -> str:
    """d3dx_user.ini 的路径 = Mods 目录的**上一级**（GIMI 根目录）。"""
    return os.path.join(os.path.dirname(os.path.abspath(mods_dir)),
                        "d3dx_user.ini")


def _read_master_swapkeys(master_ini_path: str) -> Tuple[Dict[str, str], str]:
    """解析 d3dx_user.ini，返回 (映射, 原文)。

    映射键形如 ``模组目录\\变量名``（全小写，反斜杠分隔），值为当前取值。
    原文一并返回：带 namespace 的 mod 需要在原文里按 ``namespace.key`` 搜索。
    """
    content = _read_text(master_ini_path)
    if content is None:
        return {}, ""
    mapping: Dict[str, str] = {}
    for line in content.splitlines():
        m = _MASTER_LINE_RE.match(line)
        if m:
            mod_name, swapkey, value = m.groups()
            mapping[f"{mod_name}\\{swapkey}".lower()] = value
    return mapping, content


def write_back_swapkeys(mods_dir: str,
                        target_dir: Optional[str] = None,
                        emit: Optional[Callable[[str], None]] = None
                        ) -> Tuple[bool, str]:
    """把 d3dx_user.ini 里的差分值写回 target_dir 下（递归）各 mod 的 .ini。

    Args:
        mods_dir: ``<GIMI>/Mods``，用于算 mod 内 .ini 的相对键（原脚本的
            ``modpath``），d3dx_user.ini 取其上一级目录。
        target_dir: 本次写回的作用域；**缺省即 Mods 根**（UI 固定按 GIMI
            路径 / Mods 写回，不再跟随当前浏览目录）。
        emit: 可选的**逐行输出回调**（UI 的「命令输出」侧页挂在这里实时打印）。
            由工作线程调用，故只应转发到 Qt 信号；回调抛错一律吞掉，不影响写回。

    Returns:
        ``(ok, msg)``——**不抛异常**（领域函数约定）。ok=False 表示没做成
        （缺 d3dx_user.ini / 无 .ini / 无映射 / 全部文件读取失败）。
    """
    def say(line: str) -> None:
        if emit is None:
            return
        try:
            emit(line)
        except Exception:
            pass

    if not mods_dir or not os.path.isdir(mods_dir):
        return False, "未配置 Mods 目录"
    master_path = _master_ini_path(mods_dir)
    if not os.path.isfile(master_path):
        return False, "未找到 d3dx_user.ini"
    say(f"差分值来源：{master_path}")
    mapping, master_content = _read_master_swapkeys(master_path)
    if not mapping and not master_content:
        return False, "d3dx_user.ini 无法读取（非 UTF-8？）"
    if not mapping:
        return False, "d3dx_user.ini 中没有差分值"
    say(f"读到差分值 {len(mapping)} 项")

    scope = target_dir or mods_dir
    inis = collect_ini_files(scope)
    if not inis:
        return False, "当前目录没有 .ini 文件"
    say(f"作用域：{scope}")
    say(f"扫描到 {len(inis)} 个 .ini")

    mods_root = os.path.abspath(mods_dir)
    changed_files = 0
    changed_values: List[int] = [0]
    skipped = 0

    for ini_path in inis:
        try:
            rel_name = os.path.relpath(ini_path, scope)
        except ValueError:                  # 跨盘等极端情况，退回完整路径
            rel_name = ini_path
        content = _read_text(ini_path)
        if content is None:
            skipped += 1
            say(f"[跳过] {rel_name}（无法按 UTF-8 读取）")
            continue
        ns_match = _NAMESPACE_RE.search(content)
        namespace = ns_match.group(1).strip() if ns_match else None
        modified = [False]
        file_values = [0]

        def replace(m, _ns=namespace, _file=ini_path, _rel=rel_name) -> str:
            swapkey, old_value = m.group(1), m.group(2)
            if _ns:
                # 带命名空间：在 master 原文里按 ``namespace.swapkey`` 取值
                dotted = re.sub(r"[\\/]", ".", _ns)
                search = re.search(
                    rf"{re.escape(dotted)}.{re.escape(swapkey)}"
                    r"\s*=\s*([\d\.eE\-]+)", master_content)
                new_value = search.group(1) if search else None
            else:
                rel = os.path.relpath(_file, mods_root)
                key = f"{rel}\\{swapkey}".lower().replace("/", "\\")
                new_value = mapping.get(key)
            if new_value is None or new_value == old_value:
                return m.group(0)
            modified[0] = True
            file_values[0] += 1
            changed_values[0] += 1
            say(f"{_rel}：${swapkey} {old_value} → {new_value}")
            return f"global persist ${swapkey} = {new_value}"

        updated = _PERSIST_RE.sub(replace, content)
        if not modified[0]:
            continue
        try:
            with open(ini_path, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
        except OSError:
            skipped += 1
            say(f"[失败] {rel_name}（无法写入，文件被占用？）")
            continue
        changed_files += 1
        say(f"[已写回] {rel_name}（{file_values[0]} 项）")

    if changed_files == 0:
        tail = f"，{skipped} 个读取失败" if skipped else ""
        return False, f"没有需要更新的值（值已是最新{tail}）"
    tail = f"，{skipped} 个跳过" if skipped else ""
    return True, (f"已写回 {changed_files} 个 .ini，"
                  f"更新 {changed_values[0]} 个值{tail}")


if __name__ == "__main__":
    # 独立调试：扫描并打印分类结果
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from omg.core.paths import get_gimi_dir
    # 无 config 时直接用相邻 GIMI 目录
    mods = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "..", "GIMI", "Mods")
    mods = os.path.abspath(mods)
    items = scan_mods(mods)
    print(f"Mods @ {mods}: {len(items)} 条目")
    for it in items[:10]:
        print("  ", it["name"], "目录" if it["is_dir"] else "文件")
