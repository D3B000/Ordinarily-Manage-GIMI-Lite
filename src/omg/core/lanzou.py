"""
omg.core.lanzou — 蓝奏云「文件夹分享」免登录下载。

便捷构建页「源 = 已构建文件」的下载后端：已构建好的 d3d11.dll 由 DoPE 侧批量
打包成 ``<时间戳>.zip``（如 ``20260825_135633.zip``）上传到蓝奏云文件夹分享，
OMGLite 这边凭「分享链接 + 提取码」直接拉取，**不需要登录、不读 cookie**。

实现移植自 ``DoPE/getFileFromLanzoui.py``。之所以不直接复用那套代码：
  1. DoPE 是独立的旁支工程，不属于 ``omg`` 包，不应被 OMGLite 依赖；
  2. 它依赖随包分发的 ``LanZouCloud-API-2.6.10`` 目录里的 ``acw_sc__v2``
     反爬计算函数，而 2.6.10 官方库本身**不支持**文件夹分享下载。
 因此这里把真正用到的三个函数（``remove_notes`` / ``unsbox`` / ``hex_xor``）
 内联进来，只依赖 ``requests``。

免登录下载流程（已实测可用的私有端点）：
  1) GET 分享页 → 解析 filemoreajax.php?file=<fid>、uid、puid、t、k
  2) POST filemoreajax.php（带 pwd）→ 文件列表（id / name_all / size），
     分页 pg 递增直到不足 50 条
  3) 对每个文件：GET https://<host>/<id>（可能触发 acw_sc__v2 反爬，需计算
     cookie 重试）→ 取 <iframe src> → GET iframe 页 → 解析 ajaxfile.php 的
     real_fid / wp_sign / ajaxdata
  4) POST ajaxfile.php（kd=0 分支）→ 直链 → 跟随重定向下载（需带 Referer）

注意事项：
  · t / k 的变量名由蓝奏云随机化（如 ``ibjqf1`` / ``_hi2gu``），**不能写死**，
    必须先从 ``file()`` 的 data 块读出变量名再回页面解析其值。
  · ``requests`` 在模块内**按需导入**（函数级 import），这样 headless 环境
    （如没有 requests 的隔离解释器跑单测）也能正常 import 本模块。
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional, Tuple

from omg.core.logging_setup import logger

# ---------------------------------------------------------------------------
# 默认分享（DoPE 上传「已构建文件」的文件夹）
# ---------------------------------------------------------------------------
DEFAULT_SHARE_URL = "https://wwbrf.lanzoul.com/b014xhxlxe"
DEFAULT_SHARE_PWD = "4g4a"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/75.0.3770.100 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}
PAGE_SIZE = 50          # 蓝奏云单页最多 50 条
RETRY = 3               # 取直链重试次数
CHUNK = 1 << 16         # 写盘分片 64KB
TIMEOUT = 20            # 普通请求超时
DL_TIMEOUT = 120        # 下载请求超时


class LanzouError(RuntimeError):
    """蓝奏云交互失败（页面结构变化 / 提取码错误 / 被限流等）。"""


# ---------------------------------------------------------------------------
# acw_sc__v2 反爬 cookie 计算（移植自 LanZouCloud-API 的 lanzou/api/utils.py）
# ---------------------------------------------------------------------------
def remove_notes(html: str) -> str:
    """删除网页的 HTML / JS 注释（蓝奏云前端常把旧代码注释掉就上线）。"""
    html = re.sub(r'<!--.+?-->|\s+//\s*.+', '', html)
    html = re.sub(r'(.+?[,;])\s*//.+', r'\1', html)
    return html


def _unsbox(arg: str) -> str:
    """按固定置换表还原 arg1 的字符顺序。"""
    table = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25,
             13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3,
             28, 34, 37, 12, 36]
    out = ["" for _ in table]
    for idx, ch in enumerate(arg):
        for pos, want in enumerate(table):
            if want == idx + 1:
                out[pos] = ch
    return "".join(out)


def _hex_xor(arg: str, key: str) -> str:
    """两两十六进制字符与固定密钥异或。"""
    res = []
    for i in range(0, min(len(arg), len(key)), 2):
        v = int(arg[i:i + 2], 16) ^ int(key[i:i + 2], 16)
        res.append(format(v, '02x'))
    return "".join(res)


def calc_acw_sc__v2(html_text: str) -> str:
    """从含 ``arg1='...'`` 的反爬页面计算出 acw_sc__v2 cookie 值。"""
    m = re.search(r"arg1='([0-9A-Z]+)'", html_text)
    arg1 = m.group(1) if m else ""
    return _hex_xor(_unsbox(arg1), "3000176000856006061501533003690027800375")


def sanitize(name: str) -> str:
    """清理文件名中的非法字符。"""
    return re.sub(r'[\\/:*?"<>|]', '_', (name or "").strip()) or "unnamed"


def split_share_url(share_url: str) -> str:
    """取分享链接的 host（如 ``https://wwbrf.lanzoul.com``）。"""
    m = re.match(r'(https?://[^/]+)', share_url or "")
    return m.group(1) if m else "https://lanzoul.com"


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
class LanzouFolder:
    """一个蓝奏云文件夹分享：列目录 + 下载其中任意文件。

    典型用法::

        folder = LanzouFolder(url, pwd)
        files = folder.list_files()                 # [(id, name, size), ...]
        folder.download(file_id, dest, progress_cb=...)

    同一个实例持有同一个 ``requests.Session``（cookie 在取直链过程中会用到），
    因此列目录与下载应当复用同一实例。
    """

    def __init__(self, share_url: str = DEFAULT_SHARE_URL,
                 share_pwd: str = DEFAULT_SHARE_PWD) -> None:
        import requests  # 按需导入：headless 环境可以不装 requests
        self.url = (share_url or DEFAULT_SHARE_URL).strip()
        self.pwd = (share_pwd or DEFAULT_SHARE_PWD).strip()
        self.host = split_share_url(self.url)
        self._session = requests.Session()
        self._params: Optional[dict] = None     # 解析出的 fid / uid / puid / t / k

    # ---- 基础 GET（带 acw 反爬处理）----
    def _get(self, url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT):
        r = self._session.get(url, headers=headers or HEADERS, timeout=timeout)
        if 'acw_sc__v2' in r.text:
            try:
                self._session.cookies.set("acw_sc__v2", calc_acw_sc__v2(r.text))
                r = self._session.get(url, headers=headers or HEADERS, timeout=timeout)
            except Exception as e:  # 反爬计算失败不致命，继续用原始响应
                logger.debug("acw_sc__v2 计算失败: %s", e)
        return r

    # ---- 分享页参数解析 ----
    def _parse_params(self) -> dict:
        """解析 filemoreajax.php 所需的 fid / uid / puid / t / k（结果缓存）。"""
        if self._params is not None:
            return self._params
        html = remove_notes(self._get(self.url).text)

        m = re.search(r"url : '/filemoreajax.php\?file=(\d+)'", html)
        if not m:
            raise LanzouError("无法从分享页解析文件夹 id（不是文件夹分享链接，或页面结构已变化）")
        fid = m.group(1)

        uid = re.search(r"'uid'\s*:\s*'(\d+)'", html)
        puid = re.search(r"'puid'\s*:\s*'([^']+)'", html)
        if not (uid and puid):
            raise LanzouError("分享页缺少 uid/puid 参数（页面结构已变化）")

        # t / k 的变量名是动态随机的，先从 file() 的 data 块读出变量名，再回页面取值
        pos = html.find("function file")
        func = html[pos:pos + 1600] if pos >= 0 else html
        t_var = re.search(r"'t'\s*:\s*([A-Za-z_$][\w$]*)", func)
        k_var = re.search(r"'k'\s*:\s*([A-Za-z_$][\w$]*)", func)
        if not (t_var or k_var):
            raise LanzouError("分享页缺少 t/k 参数标识（页面结构已变化）")

        def resolve(name: str) -> str:
            mm = re.search(r"(?:var\s+)?" + re.escape(name) + r"\s*=\s*'([^']*)'", html)
            return mm.group(1) if mm else ""

        self._params = {
            "fid": fid,
            "uid": uid.group(1),
            "puid": puid.group(1),
            "t": resolve(t_var.group(1)) if t_var else "",
            "k": resolve(k_var.group(1)) if k_var else "",
        }
        return self._params

    # ---- 列目录 ----
    def list_files(self, status_cb: Optional[Callable[[str], None]] = None) -> list:
        """列出分享内的全部文件，返回 ``[{"id", "name", "size"}, ...]``。"""
        p = self._parse_params()
        files, pg = [], 1
        while True:
            data = {'lx': 2, 'fid': p["fid"], 'uid': p["uid"], 'puid': p["puid"],
                    'pg': pg, 'rep': '0', 't': p["t"], 'k': p["k"],
                    'up': 1, 'ls': 1, 'pwd': self.pwd}
            resp = self._session.post(
                self.host + "/filemoreajax.php?file=" + p["fid"], data=data,
                headers={**HEADERS, 'Referer': self.url,
                         'X-Requested-With': 'XMLHttpRequest'},
                timeout=TIMEOUT,
            )
            try:
                j = resp.json()
            except Exception:
                raise LanzouError(
                    f"filemoreajax.php 返回非 JSON（提取码错误或被限流）：{resp.text[:120]}"
                )
            if j.get('zt') != 1:
                info = j.get('info') or j.get('text')
                if pg == 1:
                    raise LanzouError(f"列出文件失败（提取码错误？）：{info}")
                break
            batch = j.get('text', []) or []
            if not batch:
                break
            for n in batch:
                name = (n.get('name_all') or '').strip()
                if not name or str(n.get('id')) == '-1':
                    continue
                files.append({"id": n['id'], "name": name, "size": n.get('size', '')})
            if status_cb:
                try:
                    status_cb(f"已列出 {len(files)} 个文件")
                except Exception:
                    pass
            if len(batch) < PAGE_SIZE:
                break
            pg += 1
            time.sleep(0.3)      # 轻微限速，避免触发风控
        return files

    # ---- 取直链 ----
    def direct_url(self, file_id: str) -> str:
        """走 文件页 → iframe → ajaxfile.php 流程，返回可下载的直链。"""
        page_url = f"{self.host}/{file_id}"
        html = remove_notes(self._get(page_url).text)

        m = re.search(r'<iframe.*?src="(.+?)"', html)
        if not m:
            raise LanzouError(f"文件页未找到 iframe（页面结构已变化）: {file_id}")
        para = m.group(1)

        dp = self._session.get(self.host + para,
                               headers={**HEADERS, 'Referer': page_url},
                               timeout=TIMEOUT)
        dt = remove_notes(dp.text)
        real = re.search(r"ajaxfile\.php\?file=(\d+)", dt)
        wp = re.search(r"wp_sign = '([^']+)'", dt)
        ax = re.search(r"ajaxdata = '([^']+)'", dt)
        if not (real and wp and ax):
            raise LanzouError(f"iframe 页未解析到下载参数: {file_id}")
        real_fid, wp_sign, ajaxdata = real.group(1), wp.group(1), ax.group(1)

        resp = self._session.post(
            self.host + "/ajaxfile.php?file=" + real_fid,
            data={'action': 'downprocess', 'websignkey': ajaxdata,
                  'signs': ajaxdata, 'sign': wp_sign, 'websign': '',
                  'kd': 0, 'ves': 1},
            headers={**HEADERS, 'Referer': self.host + para,
                     'X-Requested-With': 'XMLHttpRequest'},
            timeout=TIMEOUT,
        )
        try:
            j = resp.json()
        except Exception:
            raise LanzouError(f"ajaxfile.php 返回非 JSON：{resp.text[:120]}")
        if j.get('zt') != 1 or not j.get('url'):
            raise LanzouError(f"获取直链失败：zt={j.get('zt')} inf={j.get('inf')}")
        dom = j.get('dom') or 'https://slssctm.dmpdmp.com'
        return dom + '/file/' + j['url']

    # ---- 下载 ----
    def download(self, file_id: str, dest_path: str,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 progress_cb: Optional[Callable[[int, int], None]] = None,
                 retry: int = RETRY) -> Tuple[bool, str]:
        """下载单个文件到 ``dest_path``（先写 .part，完成后原子替换）。

        Args:
            file_id: :meth:`list_files` 返回的 id。
            dest_path: 目标文件路径（所在目录须已存在）。
            should_cancel: 返回 True 时中断（已写入的 .part 会被删除）。
            progress_cb: ``fn(received_bytes, total_bytes)``；``total_bytes``
                为 0 表示服务端未给出长度。
            retry: 取直链的重试次数。

        Returns:
            (success, message)
        """
        durl = None
        last_err = ""
        for attempt in range(1, retry + 1):
            if should_cancel and should_cancel():
                return False, "已取消"
            try:
                durl = self.direct_url(file_id)
                break
            except Exception as e:
                last_err = str(e)
                logger.warning("取直链失败(%d/%d): %s", attempt, retry, e)
                time.sleep(1.5 * attempt)
        if not durl:
            return False, f"无法获取直链: {last_err}"

        try:
            resp = self._session.get(
                durl, headers={**HEADERS, 'Referer': f"{self.host}/{file_id}"},
                stream=True, allow_redirects=True, timeout=DL_TIMEOUT,
            )
        except Exception as e:
            return False, f"下载请求失败: {e}"
        if resp.status_code != 200:
            return False, f"下载 HTTP {resp.status_code}"

        total = 0
        try:
            total = int(resp.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            total = 0

        tmp = dest_path + ".part"
        received = 0
        try:
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_content(CHUNK):
                    if should_cancel and should_cancel():
                        f.close()
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        return False, "已取消"
                    if not chunk:
                        continue
                    f.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(received, total)
                        except Exception:
                            pass
            os.replace(tmp, dest_path)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False, f"写盘失败: {e}"
        return True, f"{received} bytes"
