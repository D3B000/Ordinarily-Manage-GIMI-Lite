"""
GitHub 资源智能下载模块

针对 GIMI-PACKAGE 和 XXMI-Libs-Package 下载场景优化,
参考 cit (https://gitee.com/solider245/cit) 的「多代理 URL 转换 + 智能下载」
思路并集成到本模块中。

cit 功能对照:
  - change (URL→代理地址)        → build_download_candidates() 已实现
  - get    (智能下载 release/raw) → download_file_smart() 已实现
                                    (当前场景固定 release/source zip,无需 raw 判定)
  - clone  (git clone 加速)       → 不复刻 (当前只需下载 zip,不 clone 仓库)
  - sub    (submodule 加速)       → 不复刻 (同上)
  - 常用软件/系统加速              → 不复刻 (与 GIMI/XXMI 下载无关)

核心能力:
- 网络环境智能识别 (能访问 YouTube → 直连 GitHub,跳过代理)
- 多公共代理自动 failover (8 个代理按优先级排序,参考社区实测列表)
- HTTP Range 断点续传 (网络中断后从已下载位置继续,不重新开始)
- 智能重试 + 指数退避 (单 URL 多次重试,失败自动切换下一代理)
- SSL 双重回退 (默认验证 → 不验证)
- 可选代理测速 (select_fastest_proxy,选当前最快代理)
- 兼容现有 DownloadSignals 进度/状态回调

代理启用决策 (should_use_proxy):
  - 非 GitHub URL → 不使用代理
  - YouTube 可访问 → 不使用代理 (用户已具备科学上网能力,直连更快)
  - YouTube 不可访问 → 使用代理 (启用多代理 failover)
  探测结果缓存 5 分钟,避免每次下载都消耗探测时间。

传输通道 (get_openers):
  「加不加第三方代理前缀」之外还有一层更底的问题: urllib 在 Windows 上会
  自动读取注册表里的系统代理 (如 Clash 的 127.0.0.1:7892), 因此平时直接调
  urlopen() 走的其实一直是系统代理, 并非真正的直连。当代理客户端对某个域名
  (实测: api.github.com) 的 CONNECT 隧道不稳定时, 会抛
  ``SSL: UNEXPECTED_EOF_WHILE_READING``, 而此时绕过系统代理的直连往往是通的。
  故每个请求都在「系统代理 / 直连」两种通道上各试一次, 顺序按网络环境自适应。

场景说明:
- GIMI-PACKAGE 和 XXMI-Libs-Package 均为 GitHub release/source zip 下载,
  非 pip 安装,因此 PyPI 国内镜像不适用 (仅作预留接口保留)。
- 外部加速工具需用户单独安装,不在此集成,避免引入额外运行时依赖。

迁移说明: 原 OMGDev/func/download.py,现作为 omg.core.download 纳入标准包。
"""

import os
import ssl
import json
import time
import socket
import logging
import urllib.request
import urllib.error
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)


# ============================================================
# 配置常量
# ============================================================

# GitHub HTTPS 加速代理列表 (按优先级排序)
# 这些代理通过 "代理前缀 + 原始 GitHub URL" 的形式转发请求
# 代理可能随时失效,优先级排序 + 自动 failover 保证可用性
#
# 优先级依据: speedTest.py 3 轮实测平均速度排序 (2026-08-19)
#   #1 gh-proxy.com      0.96 MB/s  100% 成功率 (最快且稳定)
#   #2 ghproxy.net       0.26 MB/s  100% 成功率 (稳定备选)
#   #3 githubproxy.cc    0.01 MB/s   67% 成功率 (可用但慢)
#   #4-6 以下为备选,本次测试失败但可能是临时问题 (DNS/SSL/超时),保留兜底
#
# 已移除 (实测确认不可用):
#   - ghproxy.homeboyc.cn  HTTP 403 Forbidden (服务限制访问)
#   - gitclone.com         HTTP 404 Not Found (不支持 release zip 下载,仅支持 git clone)
GITHUB_PROXIES: List[str] = [
    "https://gh-proxy.com/",          # 实测最快 0.96 MB/s, 100% 成功率
    "https://ghproxy.net/",           # 实测 0.26 MB/s, 100% 成功率 (稳定备选)
    "https://githubproxy.cc/",        # 实测 0.01 MB/s, 67% 成功率
    "https://hub.gitmirror.com/",    # 实测 DNS 失败 (可能临时,保留备选)
    "https://gh.llkk.cc/",            # 实测超时 (可能临时,保留备选)
    "https://gh.api.99988866.xyz/",   # 实测 SSL 失败 (可能临时,保留备选)
]

# PyPI 国内镜像 (仅用于 pip install -i 场景,当前 GIMI/d3d11 不涉及)
PYPI_MIRRORS: List[str] = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.mirrors.ustc.edu.cn/simple/",
]

# User-Agent
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 下载参数
DEFAULT_TIMEOUT = 30              # 连接/读取超时 (秒)
DEFAULT_CHUNK_SIZE = 524288       # 512KB
MAX_RETRIES_PER_URL = 2           # 每个 URL 最多重试次数 (不含首次尝试)
RETRY_BACKOFF_BASE = 1.5          # 指数退避基数 (秒)
RESUME_PROBE_TIMEOUT = 8          # 探测 Range 支持的超时

MAX_TOTAL_TRIES = 8              # 单 URL 最大尝试次数 (通道数 × 重试轮次 的上限)

# YouTube 可达性探测配置
# 逻辑: 能访问 YouTube → 用户已具备科学上网能力 → 直连 GitHub 通常更快,跳过代理
#       不能访问 YouTube → 启用多代理 failover
YOUTUBE_PROBE_URL = "https://www.youtube.com"
YOUTUBE_PROBE_TIMEOUT = 4         # 探测超时 (秒)
YOUTUBE_CACHE_TTL = 300           # 探测结果缓存时间 (秒),避免每次下载都探测

# 全局缓存: YouTube 可达性 (None=未探测, True/False=已探测)
_youtube_reachable_cache: Optional[bool] = None
_youtube_cache_time: float = 0.0


# ============================================================
# SSL 上下文辅助
# ============================================================

def _ssl_ctx() -> ssl.SSLContext:
    """默认 SSL 上下文 (验证证书)"""
    return ssl.create_default_context()


def _ssl_ctx_noverify() -> ssl.SSLContext:
    """不验证证书的 SSL 上下文 (兜底回退)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ============================================================
# 传输通道: 系统代理 / 直连 × SSL 验证 / 不验证
# ============================================================

def get_system_proxies() -> dict:
    """当前生效的系统代理配置。

    Windows 上来自注册表 (Internet Settings/ProxyServer), 其它平台来自环境变量。
    返回空 dict 表示系统未配置代理。
    """
    try:
        return dict(urllib.request.getproxies())
    except Exception:
        return {}


def has_system_proxy() -> bool:
    """系统是否配置了 HTTP(S) 代理。"""
    return bool(get_system_proxies())


# 通道缓存: key=(系统代理快照, 是否直连优先), value=(通道名, opener) 列表。
# 构造 SSLContext 需要读取系统证书库, 有实际开销, 故按快照缓存;
# 用户开关代理客户端后快照变化会自动重建。
_openers_cache: dict = {}


def get_openers(prefer_direct: bool = False) -> List[Tuple[str, urllib.request.OpenerDirector]]:
    """返回按优先级排列的传输通道: ``[(通道名, opener), ...]``。

    urllib 在 Windows 上会自动读取注册表中的系统代理, 所以裸 ``urlopen()``
    并不是真正的直连。要拿到真正的直连必须显式传 ``ProxyHandler({})``。
    本函数把两种通道都造出来, 让调用方在「代理客户端对某域名抽风」时还有
    一条活路 —— 实测 api.github.com 被系统代理重置时, 直连通道是可以通的。

    每个通道内部再叠一层 SSL 双重回退 (验证证书 → 不验证), 共 4 个组合:

        prefer_direct=False  →  系统代理/SSL → 系统代理/SSL-不验证 → 直连/... → 直连/...
        prefer_direct=True   →  直连/SSL → 直连/SSL-不验证 → 系统代理/... → 系统代理/...

    系统未配置代理时「直连」与「系统代理」是同一条路, 此时只返回 2 个组合,
    避免重复请求。

    Args:
        prefer_direct: True 时把直连排在前 (能直连的场景更快且更稳)。

    Returns:
        通道列表, 每个元素为 (可读通道名, opener)。opener 已绑定 SSL 上下文,
        调用时用 ``opener.open(req, timeout=...)``。
    """
    key = (tuple(sorted(get_system_proxies().items())), prefer_direct)
    cached = _openers_cache.get(key)
    if cached:
        return cached

    # None = 跟随系统代理 (ProxyHandler 无参构造会自行读取 getproxies)
    # {}   = 显式绕过系统代理 (真·直连)
    channels: List[Tuple[str, Optional[dict]]] = [("系统代理", None)]
    if key[0]:
        channels.append(("直连", {}))
    if prefer_direct:
        channels.reverse()

    openers: List[Tuple[str, urllib.request.OpenerDirector]] = []
    for chan_name, proxy_map in channels:
        for ssl_name, ctx in (("SSL", _ssl_ctx()), ("SSL-不验证", _ssl_ctx_noverify())):
            handler = (urllib.request.ProxyHandler() if proxy_map is None
                       else urllib.request.ProxyHandler(proxy_map))
            # 传入自建 HTTPSHandler 会顶掉 build_opener 的默认 HTTPSHandler,
            # 从而让 SSL 上下文生效。
            opener = urllib.request.build_opener(
                handler, urllib.request.HTTPSHandler(context=ctx)
            )
            openers.append((f"{chan_name}/{ssl_name}", opener))

    _openers_cache[key] = openers
    return openers


def reset_openers_cache() -> None:
    """清空通道缓存 (用户切换代理客户端后调用, 下次请求会重新探测)。"""
    _openers_cache.clear()


# ============================================================
# 网络环境探测: YouTube 可达性
# ============================================================

def is_youtube_reachable(timeout: float = YOUTUBE_PROBE_TIMEOUT,
                         force_refresh: bool = False) -> bool:
    """探测 YouTube 是否可访问

    用于判断用户网络环境是否已具备科学上网能力:
      - YouTube 可访问 → 直连 GitHub 通常更快,跳过代理
      - YouTube 不可访问 → 启用多代理 failover

    结果缓存 YOUTUBE_CACHE_TTL 秒,避免每次下载都消耗探测时间。

    Args:
        timeout: 探测超时 (秒)
        force_refresh: 强制刷新缓存,忽略 TTL

    Returns:
        True=可访问, False=不可访问
    """
    global _youtube_reachable_cache, _youtube_cache_time

    now = time.time()
    if (not force_refresh
            and _youtube_reachable_cache is not None
            and now - _youtube_cache_time < YOUTUBE_CACHE_TTL):
        return _youtube_reachable_cache

    # 实际探测 YouTube (HEAD 请求,减少流量)
    reachable = False
    for ctx in [_ssl_ctx(), _ssl_ctx_noverify()]:
        try:
            req = urllib.request.Request(
                YOUTUBE_PROBE_URL,
                method="HEAD",
                headers={"User-Agent": DEFAULT_UA},
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if resp.status < 400:
                    reachable = True
                    break
        except Exception:
            continue

    _youtube_reachable_cache = reachable
    _youtube_cache_time = now
    logger.info(f"YouTube 可达性探测: {reachable} (缓存 {YOUTUBE_CACHE_TTL}s)")
    return reachable


def reset_youtube_cache() -> None:
    """重置网络环境缓存

    用户切换 VPN/代理后可调用, 使下次请求重新探测网络环境。
    同时清空传输通道缓存 (系统代理配置可能已变化)。
    """
    global _youtube_reachable_cache, _youtube_cache_time
    _youtube_reachable_cache = None
    _youtube_cache_time = 0.0
    reset_openers_cache()


def should_use_proxy(url: str = "") -> bool:
    """根据网络环境智能判断是否需要使用代理

    决策规则:
      1. 非 GitHub URL → 不使用代理 (代理仅对 GitHub URL 生效)
      2. YouTube 可访问 → 不使用代理 (用户已具备科学上网能力)
      3. YouTube 不可访问 → 使用代理 (启用多代理 failover)

    Args:
        url: 待下载的 URL,用于判断是否为 GitHub 域名

    Returns:
        True=使用代理, False=直连
    """
    if url and not is_github_url(url):
        return False
    return not is_youtube_reachable()


# ============================================================
# URL 构建与场景识别
# ============================================================

def is_github_url(url: str) -> bool:
    """判断是否为 GitHub 域名 URL"""
    return "github.com" in url or "api.github.com" in url


def build_proxy_urls(original_url: str,
                     proxies: Optional[List[str]] = None) -> List[str]:
    """为 GitHub URL 构建代理 URL 候选列表 (不含原 URL)

    Args:
        original_url: 原 GitHub URL
        proxies: 代理前缀列表, None 表示使用默认 GITHUB_PROXIES

    Returns:
        代理 URL 列表,按优先级排序
    """
    if not is_github_url(original_url):
        return []
    proxies = proxies if proxies is not None else GITHUB_PROXIES
    urls: List[str] = []
    for prefix in proxies:
        if not prefix.endswith("/"):
            prefix += "/"
        urls.append(prefix + original_url)
    return urls


def build_download_candidates(original_url: str) -> List[str]:
    """为下载构建候选 URL 列表: 代理在前,原 URL 在后

    智能 failover: 按优先级尝试,失败自动切换下一个。
    """
    candidates = build_proxy_urls(original_url)
    if original_url not in candidates:
        candidates.append(original_url)
    return candidates


# ============================================================
# 智能测速 (可选,选择当前可用的最快代理)
# ============================================================

def _probe_proxy(proxy_prefix: str, test_url: str,
                 timeout: float = 4.0) -> Optional[float]:
    """快速测速单个代理 (HEAD 请求)

    Returns:
        响应时间 (秒),失败返回 None
    """
    if not proxy_prefix.endswith("/"):
        proxy_prefix += "/"
    test_full = proxy_prefix + test_url
    req = urllib.request.Request(
        test_full, method="HEAD", headers={"User-Agent": DEFAULT_UA}
    )
    for ctx in [_ssl_ctx(), _ssl_ctx_noverify()]:
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if resp.status < 400:
                    return time.time() - t0
        except Exception:
            continue
    return None


def select_fastest_proxy(test_url: str,
                        proxies: Optional[List[str]] = None,
                        timeout: float = 4.0) -> Optional[str]:
    """测速选择最快的可用代理

    Args:
        test_url: 用于测速的 GitHub URL (建议选小文件)
        proxies: 代理列表, None 表示使用默认

    Returns:
        最快的代理前缀 (含尾随 /),全部不可用返回 None
    """
    proxies = proxies if proxies is not None else GITHUB_PROXIES
    results = []
    for prefix in proxies:
        t = _probe_proxy(prefix, test_url, timeout=timeout)
        if t is not None:
            results.append((t, prefix))
            logger.info(f"代理测速 {prefix}: {t*1000:.0f}ms")
        else:
            logger.info(f"代理测速 {prefix}: 不可用")
    if not results:
        return None
    results.sort()
    return results[0][1]


# ============================================================
# 断点续传探测
# ============================================================

def _supports_resume(url: str, ua: str = DEFAULT_UA,
                    timeout: float = RESUME_PROBE_TIMEOUT) -> bool:
    """检测目标 URL 是否支持 Range 请求 (断点续传)

    通过发送 Range: bytes=0-0 探测:
    - 206 Partial Content → 支持
    - 200 OK → 不支持 (服务器忽略了 Range)
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": ua, "Range": "bytes=0-0"}
    )
    for ctx in [_ssl_ctx(), _ssl_ctx_noverify()]:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.status == 206
        except Exception:
            continue
    return False


# ============================================================
# 核心下载: 单 URL + 断点续传 + 智能重试
# ============================================================

def _download_single_url(url: str, dest_path: str, signals=None,
                        progress_range: Tuple[int, int] = (0, 100),
                        timeout: int = DEFAULT_TIMEOUT,
                        chunk_size: int = DEFAULT_CHUNK_SIZE,
                        max_retries: int = MAX_RETRIES_PER_URL,
                        should_cancel=None,
                        openers=None
                        ) -> Tuple[bool, str]:
    """单 URL 下载: 断点续传 + 智能重试 + 多通道回退

    Args:
        openers: 传输通道列表, 见 get_openers()。None 时按默认顺序自动获取。

    Returns:
        (success, error_msg)
    """
    ua = DEFAULT_UA
    last_err = ""
    if openers is None:
        openers = get_openers()

    # 尝试编排: 通道 × 重试, 展平成单层循环。
    # 同一轮内切换通道不等待 (失败通常是立刻返回), 走完一轮仍失败才指数退避,
    # 避免「通道数 × 重试数」把等待时间成倍放大。
    rounds = max_retries + 1
    total_tries = min(len(openers) * rounds, MAX_TOTAL_TRIES)

    for try_idx in range(total_tries):
        chan_name, opener = openers[try_idx % len(openers)]

        # 第 2 轮起, 每轮开始前退避一次
        if try_idx and try_idx % len(openers) == 0:
            wait = RETRY_BACKOFF_BASE ** (try_idx // len(openers))
            logger.info(f"等待 {wait:.1f}s 后重试 ({url})...")
            time.sleep(wait)

        # 已下载字节数 (用于断点续传)
        existing_size = 0
        if os.path.exists(dest_path):
            try:
                existing_size = os.path.getsize(dest_path)
            except OSError:
                existing_size = 0

        # 构造请求头
        headers = {"User-Agent": ua}
        if existing_size > 0:
            # 仅当支持 Range 时才追加续传头
            # 注意: _supports_resume 内部已有 SSL 双重回退
            if _supports_resume(url, ua=ua, timeout=min(timeout, RESUME_PROBE_TIMEOUT)):
                headers["Range"] = f"bytes={existing_size}-"
                logger.info(f"断点续传: 从 {existing_size} 字节继续 ({url})")
            else:
                # 不支持 Range,清空已下载部分重新开始
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                existing_size = 0

        # 传输通道回退: (系统代理 / 直连) × (SSL 验证 / 不验证)
        # 每个 opener 自带 SSL 上下文, 故逐级切换即可覆盖原来的双重回退,
        # 并额外获得「代理客户端抽风时改走直连」的能力。
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                status = resp.status
                if status == 206:
                    # 断点续传成功: 追加写入
                    mode = "ab"
                    content_range = resp.headers.get("Content-Range", "")
                    if content_range:
                        try:
                            total_size = int(content_range.split("/")[-1])
                        except (IndexError, ValueError):
                            total_size = 0
                    else:
                        total_size = 0
                elif status == 200:
                    # 全新下载: 覆盖写入
                    mode = "wb"
                    existing_size = 0
                    total_size = int(resp.headers.get("Content-Length", 0) or 0)
                elif status == 416:
                    # Range Not Satisfiable: 文件可能已完整下载
                    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                        logger.info(f"文件已完整下载 (416): {dest_path}")
                        return True, ""
                    # 否则按全新下载处理
                    mode = "wb"
                    existing_size = 0
                    total_size = 0
                else:
                    return False, f"HTTP {status}"

                downloaded = existing_size
                p_lo, p_hi = progress_range

                with open(dest_path, mode) as f:
                    while True:
                        try:
                            chunk = resp.read(chunk_size)
                        except socket.timeout:
                            # 读超时: 保存已下载部分,下次重试续传
                            logger.warning(
                                f"读取超时,已保存 {downloaded} 字节: {url}"
                            )
                            break
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if should_cancel is not None and should_cancel():
                            logger.info("下载被用户取消: %s", url)
                            return False, "已取消"
                        if signals and total_size > 0:
                            pct = downloaded / total_size * (p_hi - p_lo) + p_lo
                            try:
                                signals.progress.emit(float(pct))
                            except Exception:
                                pass
                            mb_d = downloaded / 1048576
                            mb_t = total_size / 1048576
                            try:
                                signals.status.emit(
                                    f"下载中... {mb_d:.1f} / {mb_t:.1f} MB",
                                    "info",
                                )
                            except Exception:
                                pass

                # 检查是否下载完成
                if total_size > 0 and downloaded < total_size:
                    last_err = "下载未完成 (中断)"
                    continue  # 通道未下载完,切换下一通道重试 (Range 续传)
                return True, ""

        except ssl.SSLError as e:
            # 换下一个通道 (可能是系统代理的 CONNECT 隧道被重置, 直连反而通)
            last_err = f"SSL 连接失败: {e}"
            logger.warning(f"SSL 错误 ({chan_name}), 切换通道: {e}")
            continue
        except urllib.error.HTTPError as e:
            if e.code == 416:
                # 在异常分支处理 416 (有些代理会以异常形式返回)
                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                    logger.info(f"文件已完整下载 (416 异常): {dest_path}")
                    return True, ""
                last_err = f"HTTP {e.code}: {e.reason}"
            elif 400 <= e.code < 500 and e.code != 429:
                # 客户端错误 (非限流),不重试,直接返回
                return False, f"HTTP {e.code}: {e.reason}"
            else:
                last_err = f"HTTP {e.code}: {e.reason}"
            continue  # HTTP 错误,切换下一通道重试
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            last_err = f"网络错误: {e}"
            logger.warning(f"下载尝试 {try_idx + 1} 失败 ({url}): {e}")
            continue  # 网络错误,切换下一通道重试
        except Exception as e:
            last_err = f"下载失败: {e}"
            logger.warning(f"下载尝试 {try_idx + 1} 失败 ({url}): {e}")
            continue

    return False, last_err


def download_file_smart(url: str, dest_path: str, signals=None,
                        progress_range: Tuple[int, int] = (0, 100),
                        timeout: int = DEFAULT_TIMEOUT,
                        chunk_size: int = DEFAULT_CHUNK_SIZE,
                        use_proxies: bool = True,
                        should_cancel=None) -> Tuple[bool, str]:
    """智能下载: 多代理 failover + 断点续传 + 智能重试

    Args:
        url: 原始下载 URL
        dest_path: 目标保存路径
        signals: DownloadSignals 对象 (可选,提供 progress/status 信号)
        progress_range: 进度范围 (与原 _download_file 兼容)
        timeout: 超时秒数
        chunk_size: 块大小
        use_proxies: 是否使用代理 (True 时为 GitHub URL 自动构建代理候选)

    Returns:
        (success, error_msg)
    """
    # 构建候选 URL 列表
    if use_proxies and is_github_url(url) and should_use_proxy(url):
        candidates = build_download_candidates(url)
    else:
        candidates = [url]
        if use_proxies and is_github_url(url):
            logger.info("YouTube 可访问,直连 GitHub (跳过代理)")

    logger.info(f"下载候选 URL (共 {len(candidates)} 个): {candidates}")

    # 与候选 URL 构造逻辑保持一致: 仅当走代理前缀时才优先系统代理通道
    prefer_direct = (candidates == [url])

    last_err = ""
    for i, candidate_url in enumerate(candidates):
        if i > 0:
            logger.info(f"切换至下一个候选 URL: {candidate_url}")
            try:
                if signals and hasattr(signals, "status") and signals.status:
                    signals.status.emit("切换下载源重试中...", "info")
            except Exception:
                pass
        ok, err = _download_single_url(
            candidate_url, dest_path, signals=signals,
            progress_range=progress_range, timeout=timeout,
            chunk_size=chunk_size, max_retries=MAX_RETRIES_PER_URL,
            should_cancel=should_cancel,
            openers=get_openers(prefer_direct=prefer_direct),
        )
        if ok:
            return True, ""
        if err == "已取消":
            # 用户取消：立即终止所有候选源重试
            return False, "已取消"
        last_err = err

    # 所有候选都失败: 清理损坏的部分文件
    if os.path.exists(dest_path):
        try:
            if os.path.getsize(dest_path) < 1024:
                # <1KB 视为空文件,删除
                os.remove(dest_path)
        except OSError:
            pass

    return False, last_err


# ============================================================
# 兼容现有 API: fetch_url / fetch_json_api
# ============================================================

def _extract_proxy_label(candidate_url: str, original_url: str) -> str:
    """从候选 URL 中提取代理名称用于 UI 展示。

    例: "https://gh-proxy.com/https://github.com/..." → "gh-proxy.com"
         "https://github.com/..." (原 URL) → "github.com 直连"
    """
    if candidate_url == original_url:
        return "github.com 直连"
    # 代理前缀是 candidate_url 去掉尾部的 original_url
    if candidate_url.endswith(original_url):
        prefix = candidate_url[:-len(original_url)]
        # 去掉 scheme 和尾随斜杠
        prefix = prefix.replace("https://", "").replace("http://", "").rstrip("/")
        if prefix:
            return prefix
    # fallback: 提取第一个 host
    try:
        from urllib.parse import urlparse
        p = urlparse(candidate_url)
        return p.netloc or "代理"
    except Exception:
        return "代理"


def fetch_url_smart(url: str, timeout: int = 15,
                    use_proxies: bool = True,
                    status_cb=None,
                    headers: Optional[dict] = None) -> Optional[str]:
    """获取 URL 内容,返回解码文本。支持多代理 failover + SSL 回退

    替代 main.py 中的 _fetch_url 函数。

    Args:
        url: 目标 URL
        timeout: 超时秒数
        use_proxies: 是否启用多代理 failover
        status_cb: 可选回调 fn(proxy_label: str, attempt_idx: int, total: int)
                   每次切换到新的候选 URL 时调用,用于 UI 反馈
        headers: 可选额外请求头,会合并进 browser_headers(可覆盖 UA/Accept 等)。
                 例:{"Accept-Language": "ko-KR,ko;q=0.9"} 强制服务端按语言本地化。
    """
    browser_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        browser_headers.update(headers)

    if use_proxies and is_github_url(url) and should_use_proxy(url):
        candidates = build_download_candidates(url)
        prefer_direct = False
    else:
        candidates = [url]
        prefer_direct = True
        if use_proxies and is_github_url(url):
            logger.info("YouTube 可访问,直连 GitHub (跳过代理前缀)")

    total = len(candidates)
    openers = get_openers(prefer_direct=prefer_direct)
    for i, candidate_url in enumerate(candidates):
        label = _extract_proxy_label(candidate_url, url)
        if status_cb:
            try:
                status_cb(label, i, total)
            except Exception:
                pass
        for chan_name, opener in openers:
            try:
                req = urllib.request.Request(candidate_url, headers=browser_headers)
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8")
            except Exception as e:
                logger.warning(f"请求失败 ({candidate_url} · {chan_name}): {e}")
                continue

    return None


def fetch_json_api_smart(url: str, timeout: int = 15,
                        use_proxies: bool = True,
                        status_cb=None) -> Optional[dict]:
    """获取 JSON API。支持多代理 failover + SSL 回退

    替代 main.py 中的 _fetch_json_api 函数。

    Args:
        url: 目标 URL
        timeout: 超时秒数
        use_proxies: 是否启用多代理 failover
        status_cb: 可选回调 fn(proxy_label: str, attempt_idx: int, total: int)
                   每次切换到新的候选 URL 时调用,用于 UI 反馈
    """
    headers = {
        "User-Agent": "OMG-GIMI-Manager/1.0",
        "Accept": "application/vnd.github+json",
    }

    if use_proxies and is_github_url(url) and should_use_proxy(url):
        candidates = build_download_candidates(url)
        prefer_direct = False
    else:
        candidates = [url]
        prefer_direct = True
        if use_proxies and is_github_url(url):
            logger.info("YouTube 可访问,直连 GitHub (跳过代理前缀)")

    total = len(candidates)
    openers = get_openers(prefer_direct=prefer_direct)
    for i, candidate_url in enumerate(candidates):
        label = _extract_proxy_label(candidate_url, url)
        if status_cb:
            try:
                status_cb(label, i, total)
            except Exception:
                pass
        for chan_name, opener in openers:
            try:
                req = urllib.request.Request(candidate_url, headers=headers)
                with opener.open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                logger.warning(f"API 请求失败 ({candidate_url} · {chan_name}): {e}")
                continue

    return None


# ============================================================
# PyPI 镜像接口 (预留,当前 GIMI/d3d11 场景不涉及)
# ============================================================

def get_pypi_mirror(index: int = 0) -> str:
    """获取 PyPI 镜像 URL (用于 pip install -i 参数)

    注意: 当前 GIMI-PACKAGE 和 XXMI-Libs-Package 下载场景均为
          GitHub release zip,不涉及 pip 安装,此函数为预留接口。
    """
    if 0 <= index < len(PYPI_MIRRORS):
        return PYPI_MIRRORS[index]
    return PYPI_MIRRORS[0]


# ============================================================
# 模块自检
# ============================================================

if __name__ == "__main__":
    import sys as _sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print("=== GitHub 代理列表 ===")
    for p in GITHUB_PROXIES:
        print(f"  {p}")
    print("\n=== PyPI 镜像列表 (当前场景不适用) ===")
    for m in PYPI_MIRRORS:
        print(f"  {m}")
    print("\n=== URL 构建测试 ===")
    test = "https://github.com/SilentNightSound/GIMI-Package/releases/latest"
    print(f"原始: {test}")
    for c in build_download_candidates(test):
        print(f"  候选: {c}")

    # 可选: 代理测速 (传入 --probe 参数触发,默认不运行以免每次耗时)
    if "--probe" in _sys.argv:
        print("\n=== 代理测速 (GitHub releases 页面) ===")
        fastest = select_fastest_proxy(test, timeout=4.0)
        if fastest:
            print(f"\n最快代理: {fastest}")
        else:
            print("\n所有代理当前均不可用")
    else:
        print("\n(传入 --probe 参数可触发代理测速)")

    # 可选: YouTube 可达性探测 (传入 --youtube 参数触发)
    if "--youtube" in _sys.argv:
        print("\n=== YouTube 可达性探测 ===")
        reachable = is_youtube_reachable(force_refresh=True)
        print(f"YouTube 可访问: {reachable}")
        print(f"将{'启用' if should_use_proxy(test) else '跳过'}代理下载 GitHub 资源")
    else:
        print("(传入 --youtube 参数可触发 YouTube 可达性探测)")
