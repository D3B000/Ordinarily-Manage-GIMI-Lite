"""
omg.core.gachabase — 《原神》角色分类数据源（抓取 / 解析 / 本地缓存）。

数据来源：``gi.gachabase.net`` 的 ``/characters/beta`` 页面。该页面是 SvelteKit
单页应用，角色数据以内联 JS 形式嵌在页面 hydration blob 中：

    __sveltekit_xxx.resolve(1, () => (function(...) {
        ...
        return [{ error:null, redirect:null,
            data:{ entries:[ {id:.., slug:.., name:{key:..,text:..},
                elements:[{depot_id:..,element_id:..}], character_id:..,
                base_slug:.., search_data:".."}, ... ] }]
    }(...) ))

要点：
  * ``name.text`` 是**按语言本地化**的角色名（chs / en / ko 三种页面给出三种名）；
  * ``id`` / ``character_id`` / ``slug`` / ``element_id`` 与语言无关（数字与 slug 一致）；
  * 因此三语言各抓一次，按 ``character_id`` 合并即可得到「同一角色的三语名字 + 元素 + id」。

缓存策略（详见模块下方 ``update_cache`` / ``load_catalog`` / ``is_stale``）：
  * 本地缓存文件 ``cache/gachabase_characters.json``，含 ``updated_at`` 时间戳；
  * 打开面板时若缓存缺失或超过 ``STALE_DAYS`` 天，后台线程自动拉取更新；
  * 用户也可手动「刷新数据」；拉取失败则回退到已有缓存，不阻塞使用。

本模块 **不依赖 PySide6**，可在 headless 环境直接导入（GUI 层只调用其纯函数）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Dict, List, Optional

from omg.core import download
from omg.core.paths import CONFIG_PATH

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 元素 id → 元素信息（颜色取自《原神》各元素主色，用于 UI 着色）。
#: 映射由实测数据归纳（以已知角色反查 element_id）：
#:   迪卢克(火)=1  芭芭拉(水)=2  纳西妲(草)=3  雷电将军(雷)=4
#:   神里绫华(冰)=5  温迪(风)=7  钟离(岩)=8   （6 在当前数据集中未出现，保留占位）
ELEMENTS: Dict[str, dict] = {
    "1": {"cn": "火", "en": "Pyro", "color": "#F06C2E"},
    "2": {"cn": "水", "en": "Hydro", "color": "#3DA5F0"},
    "3": {"cn": "草", "en": "Dendro", "color": "#7BC56B"},
    "4": {"cn": "雷", "en": "Electro", "color": "#B07BE0"},
    "5": {"cn": "冰", "en": "Cryo", "color": "#74CFE2"},
    "7": {"cn": "风", "en": "Anemo", "color": "#6FE3B4"},
    "8": {"cn": "岩", "en": "Geo", "color": "#E0B45A"},
}

#: 一级筛选类别
CATEGORY_CHARACTER = "character"
CATEGORY_WEAPON = "weapon"
CATEGORY_MONSTER = "monster"
CATEGORY_OTHER = "other"

#: 抓取语言顺序（决定合并时的「展示名」优先级：chs > en > ko）
LANG_ORDER = ["chs", "en", "ko"]
BASE_URL = "https://gi.gachabase.net/characters/beta?lang={lang}"

#: 抓取时显式声明 Accept-Language，强制站点按「游戏语言」本地化。
#: 某些环境（代理 / CDN / 复用会话带 en 偏好）会忽略 URL 的 lang 参数、
#: 按请求头回退成英文，显式带头可杜绝「韩文变英文」这类问题。
LANG_HEADERS = {
    "chs": "zh-CN,zh;q=0.9",
    "en": "en-US,en;q=0.9",
    "ko": "ko-KR,ko;q=0.9",
}
#: 韩文兜底语言码：实测 ?lang=ko 与 ?lang=kr 均返回韩文，
#: 若某环境 ko 异常（无韩文），回落 kr。
KO_FALLBACK = "kr"

#: 缓存目录（与 config.json 同级的 cache/，开发 / 打包模式均可写）
CACHE_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "gachabase_characters.json")

#: 缓存过期天数（超过则下次打开自动重新拉取）
STALE_DAYS = 7


# ---------------------------------------------------------------------------
# 内联数据解析
# ---------------------------------------------------------------------------

def _find_array(html: str, marker: str):
    """返回 marker 后顶层 [...] 的 (bracket_index, end_index)，找不到返回 None。"""
    i = html.find(marker)
    if i < 0:
        return None
    i += len(marker) - 1  # 指向 '['
    depth = 0
    instr = False
    esc = False
    j = i
    while j < len(html):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return (i, j)
        j += 1
    return None


def _split_objects(arr: str):
    """yield 数组内的顶层 {...} 对象子串（arr 不含外层括号）。"""
    depth = 0
    instr = False
    esc = False
    start = None
    for k, c in enumerate(arr):
        if instr:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == '{':
            if depth == 0:
                start = k
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                yield arr[start:k + 1]
                start = None


def _read_digits(o: str, key: str) -> Optional[str]:
    m = re.search(key + r':(\d+)', o)
    return m.group(1) if m else None


def _read_quoted(o: str, key: str) -> Optional[str]:
    """读取 key:"..." 中的字符串（遇首个未转义引号结束）。"""
    i = o.find(key + ':"')
    if i < 0:
        return None
    i += len(key) + 2
    out = []
    esc = False
    while i < len(o):
        c = o[i]
        if esc:
            out.append(c)
            esc = False
        elif c == '\\':
            esc = True
        elif c == '"':
            break
        else:
            out.append(c)
        i += 1
    return ''.join(out)


def _read_name(o: str) -> Optional[str]:
    """从 name:{...} 对象中取 text 字段（本地化名）。"""
    i = o.find('name:{')
    if i < 0:
        return None
    depth = 0
    instr = False
    esc = False
    j = i + len('name:{') - 1
    while j < len(o):
        c = o[j]
        if instr:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    inner = o[i + len('name:{'):j]
                    t = re.search(r'text:"([^"]*)"', inner)
                    return t.group(1) if t else None
        j += 1
    return None


def _read_element_id(o: str) -> Optional[str]:
    m = re.search(r'element_id:(\d+)', o)
    return m.group(1) if m else None


def parse_lang_html(html: str) -> List[dict]:
    """解析单个语言页面的内联 entries，返回扁平记录列表。"""
    res = _find_array(html, 'data:{entries:[')
    if not res:
        return []
    arr = html[res[0] + 1:res[1]]
    recs: List[dict] = []
    for obj in _split_objects(arr):
        rid = _read_digits(obj, 'id')
        if not rid:
            continue
        recs.append({
            "id": rid,
            "character_id": _read_digits(obj, 'character_id') or rid,
            "slug": _read_quoted(obj, 'slug'),
            "base_slug": _read_quoted(obj, 'base_slug'),
            "name": _read_name(obj),
            "element_id": _read_element_id(obj),
        })
    return recs


def build_catalog(per_lang: Dict[str, List[dict]]) -> List[dict]:
    """把三语言的扁平记录按 character_id 合并成角色目录。

    返回列表按「代表 variant id」降序排列（id 越大越新，最新在前）。
    每个角色含：character_id / id(排序用) / element_id / names{lang:name} /
    display(展示名) / aliases(小写匹配键) / variants。
    """
    by_char: Dict[str, dict] = {}
    for lang, recs in per_lang.items():
        for r in recs:
            cid = r["character_id"] or r["id"]
            d = by_char.setdefault(cid, {"langs": {}, "variants": []})
            if r["name"]:
                d["langs"][lang] = r["name"]
            d["variants"].append({
                "id": r["id"],
                "slug": r["slug"],
                "base_slug": r["base_slug"],
                "element_id": r["element_id"],
            })

    chars: List[dict] = []
    for cid, d in by_char.items():
        names = d["langs"]
        variants = d["variants"]
        element_id = variants[0]["element_id"] or "0"
        try:
            sort_id = max(int(v["id"]) for v in variants if v["id"])
        except (TypeError, ValueError):
            sort_id = 0

        aliases: set = set()
        for nm in names.values():
            if nm:
                aliases.add(nm.lower())
        for v in variants:
            for key in ("slug", "base_slug"):
                val = v.get(key)
                if val:
                    aliases.add(val.lower())
                    last = val.rsplit("-", 1)[-1]
                    if last:
                        aliases.add(last.lower())
            if v["id"]:
                aliases.add(v["id"])
        if cid:
            aliases.add(cid)

        # 旅行者（aether/lumine）数据里 name 为 null，补常用别名
        base = variants[0].get("base_slug") or ""
        if base in ("aether", "lumine"):
            aliases.update(["旅行者", "traveler", "空", "荧", "aether", "lumine"])

        display = names.get("chs") or names.get("en") or names.get("ko") \
            or (variants[0]["slug"] or cid)

        chars.append({
            "character_id": cid,
            "id": str(sort_id),
            "element_id": element_id,
            "names": names,
            "display": display,
            "aliases": sorted(aliases),
            "variants": variants,
        })

    chars.sort(key=lambda c: int(c["id"] or 0), reverse=True)
    return chars


# ---------------------------------------------------------------------------
# 网络抓取 / 缓存
# ---------------------------------------------------------------------------

def _page_has_hangul(html: str) -> bool:
    """粗略判断页面内联数据是否含韩文（按角色名 Hangul 字符）。"""
    recs = parse_lang_html(html)
    return any(re.search(r"[가-힣]", (r.get("name") or "")) for r in recs)


def _fetch_lang(lang: str, status_cb=None) -> Optional[str]:
    """抓取单个语言页面 HTML（复用 download 的多代理 failover + SSL 回退）。

    显式带 Accept-Language 头，确保拿到对应语言（韩文不会被英文覆盖）。
    韩文额外带 ko→kr 兜底：若 ko 页面抓不到，或抓回的页面不含韩文
    （被英文偏好覆盖），回落 kr。
    """
    def _get(code: str) -> Optional[str]:
        url = BASE_URL.format(lang=code)
        return download.fetch_url_smart(
            url, timeout=20, use_proxies=False, status_cb=status_cb,
            headers={"Accept-Language": LANG_HEADERS[lang]},
        )

    html = _get(lang)
    if lang == "ko" and html and not _page_has_hangul(html):
        # 抓到了但内容不是韩文 → 回落 kr
        html = _get(KO_FALLBACK)
    return html or None


def update_cache(status_cb=None) -> tuple:
    """重新拉取三语言数据并写入本地缓存。

    Returns:
        (True, catalog_dict)   成功（catalog_dict 含 meta + characters）
        (False, error_message) 失败
    失败时不写缓存（保留旧缓存），便于上层回退。
    """
    per_lang: Dict[str, List[dict]] = {}
    for lang in LANG_ORDER:
        if status_cb:
            status_cb(f"抓取 {lang}", 0, 1)
        html = _fetch_lang(lang, status_cb)
        if not html:
            return (False, f"无法抓取语言页面：{lang}")
        per_lang[lang] = parse_lang_html(html)
        if not per_lang[lang]:
            return (False, f"语言页面解析为空：{lang}")

    chars = build_catalog(per_lang)
    if not chars:
        return (False, "合并后角色目录为空")

    catalog = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "gi.gachabase.net",
        "stale_days": STALE_DAYS,
        "count": len(chars),
        "elements": ELEMENTS,
        "characters": chars,
    }

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _atomic_write_json(CACHE_FILE, catalog)
    except OSError as e:
        return (False, f"写入缓存失败：{e}")

    return (True, catalog)


def load_catalog() -> Optional[dict]:
    """读取本地缓存；不存在 / 损坏返回 None。"""
    if not os.path.isfile(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "characters" not in data:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def catalog_age_days(catalog: Optional[dict] = None) -> Optional[float]:
    """缓存距现在的天数；无缓存返回 None。"""
    if catalog is None:
        catalog = load_catalog()
    if not catalog:
        return None
    ts = catalog.get("updated_at")
    if not ts:
        return None
    try:
        updated = datetime.fromisoformat(ts)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def is_stale(catalog: Optional[dict] = None) -> bool:
    """缓存是否存在且超过 STALE_DAYS 天（无缓存视为「需要更新」）。"""
    age = catalog_age_days(catalog)
    if age is None:
        return True
    return age > STALE_DAYS


def _atomic_write_json(path: str, data: dict) -> None:
    """原子写 JSON：临时文件 + os.replace，避免半截文件。"""
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
# 匹配辅助（供 mods_manager 调用）
# ---------------------------------------------------------------------------

def match_character(item_name: str, chars: List[dict]) -> Optional[dict]:
    """把 Mods 条目名匹配到角色目录。

    规则：
      * 数字别名（如角色 id）只做**精确**匹配，避免 id 互相子串误伤；
      * 普通别名（名字 / slug）长度 >=3 时才做子串匹配；
      * 精确匹配（含去扩展名）立即返回（最高优先级）；
      * 否则取「最长命中别名」对应的角色，降低短碎片误匹配概率。
    """
    norm = item_name.lower()
    norm_no_ext = os.path.splitext(item_name)[0].lower()
    best: Optional[dict] = None
    best_len = 0
    for c in chars:
        for alias in c.get("aliases", []):
            if not alias:
                continue
            if alias.isdigit():
                if alias == norm or alias == norm_no_ext:
                    return c
                continue
            if alias == norm or alias == norm_no_ext:
                return c
            if len(alias) >= 3 and (alias in norm or alias in norm_no_ext):
                if len(alias) > best_len:
                    best = c
                    best_len = len(alias)
    return best


if __name__ == "__main__":
    # 独立调试：直接拉取并写入缓存
    ok, payload = update_cache()
    if ok:
        print(f"OK: {payload['count']} 角色, 更新于 {payload['updated_at']}")
    else:
        print(f"FAIL: {payload}")
