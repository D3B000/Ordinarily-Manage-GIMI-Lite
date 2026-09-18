"""
米哈游启动器 - 背景资源提取工具 (API + CLI)
=============================================

原理:
  启动器使用 CEF (Chromium Embedded Framework) 渲染 UI。
  静态背景图 (WEBP/PNG) 和动态背景视频 (WEBM) 均从 CDN 加载并缓存。
  本工具通过解析 Local Storage 和 CEF 缓存, 精确定位各游戏当前使用的背景资源。

作为模块导入:
    from omg.domain.media.auto_extract import HypLauncher

    launcher = HypLauncher()

    # --- 查询类 ---

    # 获取各游戏当前背景日期
    dates = launcher.get_game_background_dates()
    # {'hk4e_cn': '2026/06/16', 'hkrpg_cn': '2026/05/26', ...}

    # 获取指定游戏当前视频的 CDN URL
    url = launcher.get_current_video_url('hk4e_cn')

    # 获取 CEF 缓存中所有已缓存的 WEBM URL (按日期分组)
    urls_by_date = launcher.get_cached_webm_urls()

    # 获取缓存中所有可提取的静态背景图 (不复制)
    images = launcher.get_cached_images()

    # 获取缓存中视频的 HTTP 元信息 (总大小/ETag 等)
    metadata = launcher.get_video_metadata()

    # 列出输出目录中已有的文件
    files = launcher.list_output_files()

    # 检查启动器是否正在运行
    running = launcher.is_running()

    # --- 操作类 ---

    # 下载指定游戏的当前背景视频
    videos = launcher.download_current_videos(['hk4e_cn'])

    # 下载指定 URL 的视频
    videos = launcher.download_videos(['https://...'])

    # 提取缓存中的静态背景图 (启动器关闭时可获取完整版)
    images = launcher.extract_images()

    # 验证已下载视频的完整性
    results = launcher.verify_all_videos()

    # --- 高级流程 ---

    # 一键完整提取 (关闭→提取图片→重启→下载当前视频)
    report = launcher.run_full()

    # 仅下载当前视频 (不关闭启动器)
    report = launcher.run_download_current()

    # 仅提取缓存中的图片
    report = launcher.run_extract_only()

作为命令行使用:
    python auto_extract.py                          # 一键完整提取 (原神)
    python auto_extract.py --game hkrpg_cn          # 指定游戏
    python auto_extract.py --all                    # 所有游戏
    python auto_extract.py --download               # 仅下载当前视频 (不关闭启动器)
    python auto_extract.py --extract                # 仅提取缓存图片
    python auto_extract.py --list                   # 列出所有缓存的视频 URL
    python auto_extract.py --verify                 # 验证已下载视频的完整性

支持的游戏:
    hk4e_cn   原神
    hkrpg_cn  崩坏: 星穹铁道
    nap_cn    绝区零
    bh3_cn    崩坏3

依赖: Python 3.8+ (无需第三方库, Windows only)
"""
import os
import re
import sys
import time
import shutil
import ctypes
import ctypes.wintypes as wt
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional, Callable, Tuple
from datetime import datetime

__all__ = [
    'HypLauncher',
    'VideoInfo',
    'ImageInfo',
    'ExtractReport',
    'VideoMetadata',
    'GameInfo',
    'GAME_NAMES',
]

# ============================================================
# 常量
# ============================================================
GAME_NAMES = {
    'hk4e_cn':  '原神',
    'hkrpg_cn': '星穹铁道',
    'nap_cn':   '绝区零',
    'bh3_cn':   '崩坏3',
}


# ============================================================
# 数据结构
# ============================================================
@dataclass
class VideoInfo:
    """下载的视频信息"""
    url: str
    filename: str
    filepath: str
    size: int               # bytes
    success: bool
    error: Optional[str] = None
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    is_complete: bool = True


@dataclass
class ImageInfo:
    """提取的缓存图片信息"""
    cache_file: str
    filename: str
    filepath: str
    size: int               # bytes
    format: str             # 'webp', 'png', 'jpg'
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class VideoMetadata:
    """缓存中视频的 HTTP 元信息 (从 data_3 提取)"""
    content_type: str
    total_size: int         # bytes
    etag: str
    content_md5: str
    content_range: str
    last_modified: str
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class GameInfo:
    """游戏背景资源信息"""
    game_biz: str           # 如 'hk4e_cn'
    game_name: str          # 如 '原神'
    bg_date: str            # 当前背景日期, 如 '2026/06/16'
    bg_url: str             # 静态背景图 URL
    video_url: Optional[str] = None   # 匹配到的视频 URL
    video_match_method: Optional[str] = None  # 匹配方式


@dataclass
class ExtractReport:
    """完整提取流程的结果报告"""
    videos: List[VideoInfo] = field(default_factory=list)
    images: List[ImageInfo] = field(default_factory=list)
    metadata: List[VideoMetadata] = field(default_factory=list)
    game_infos: List[GameInfo] = field(default_factory=list)
    new_urls: List[str] = field(default_factory=list)
    launcher_killed: bool = False
    launcher_started: bool = False
    elapsed: float = 0.0

    @property
    def video_count(self) -> int:
        return len(self.videos)

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def complete_videos(self) -> List[VideoInfo]:
        return [v for v in self.videos if v.is_complete]

    @property
    def failed_videos(self) -> List[VideoInfo]:
        return [v for v in self.videos if not v.success]

    def summary(self) -> str:
        lines = [
            f'视频: {self.video_count} 个 (完整 {len(self.complete_videos)}, 失败 {len(self.failed_videos)})',
            f'图片: {self.image_count} 个',
            f'耗时: {self.elapsed:.1f}s',
        ]
        return '\n'.join(lines)


# ============================================================
# 默认配置
# ============================================================
_DEFAULT_CONFIG = {
    'launcher_dir': r'h:\miHoYo Launcher',
    'launcher_exe': r'h:\miHoYo Launcher\1.16.1.364\launcher.exe',
    'output_dir': r'h:\miHoYo Launcher\genshin_backgrounds',
    'monitor_timeout': 180,
    'poll_interval': 2,
}


def _resolve_config(overrides: Optional[Dict] = None) -> Dict:
    cfg = dict(_DEFAULT_CONFIG)
    cfg['appdata'] = os.path.expandvars('%APPDATA%')
    cfg['hyp_dir'] = os.path.join(cfg['appdata'], r'miHoYo\HYP\1_1')
    cfg['cache_dir'] = os.path.join(cfg['hyp_dir'], r'fedata\Cache\Cache_Data')
    cfg['ls_dir'] = os.path.join(cfg['hyp_dir'], r'fedata\Local Storage\leveldb')
    cfg['log_dir'] = os.path.join(cfg['hyp_dir'], 'logs')
    if overrides:
        cfg.update(overrides)
    return cfg


# ============================================================
# 核心类
# ============================================================
class HypLauncher:
    """
    米哈游启动器背景资源提取器

    用法:
        launcher = HypLauncher()
        # 或自定义配置:
        launcher = HypLauncher(output_dir=r'D:\\my_bg')
    """

    def __init__(self, **kwargs):
        """
        初始化提取器。

        可选参数:
            launcher_dir:    启动器安装目录
            launcher_exe:    launcher.exe 完整路径
            output_dir:      资源保存目录
            monitor_timeout: 监控超时秒数 (默认 180)
            poll_interval:   轮询间隔秒数 (默认 2)
            verbose:         是否打印日志 (默认 True)
        """
        self._cfg = _resolve_config(kwargs)
        self._verbose = kwargs.get('verbose', True)
        self._ended_video_urls = []  # Cached from _get_bg_dates
        os.makedirs(self._cfg['output_dir'], exist_ok=True)

    # ----------------------------------------------------------
    # 公共 API: 查询类
    # ----------------------------------------------------------

    def get_game_background_dates(self) -> Dict[str, str]:
        """
        从 Local Storage 的 lastBackgroundUrlByBiz 中读取各游戏当前背景日期。

        Returns:
            {game_biz: date_str} 如 {'hk4e_cn': '2026/06/16'}
        """
        return self._get_bg_dates()

    def get_game_infos(self) -> List[GameInfo]:
        """
        获取所有游戏的背景资源信息, 包括静态背景日期和匹配到的视频 URL。

        Returns:
            GameInfo 列表
        """
        bg_dates = self._get_bg_dates()
        bg_urls = self._get_bg_urls()
        urls_by_date = self.get_cached_webm_urls()

        results = []
        for biz, date in bg_dates.items():
            name = GAME_NAMES.get(biz, biz)
            bg_url = bg_urls.get(biz, '')
            video_url, method = self._match_video_url(biz, date, urls_by_date)
            results.append(GameInfo(
                game_biz=biz,
                game_name=name,
                bg_date=date,
                bg_url=bg_url,
                video_url=video_url,
                video_match_method=method,
            ))
        return results

    def get_current_video_url(self, game_biz: str = 'hk4e_cn') -> Optional[str]:
        """
        获取指定游戏当前背景视频的 CDN URL。

        原理: 通过 lastBackgroundUrlByBiz 中该游戏的背景日期,
        与 CEF 缓存中 WEBM URL 的日期进行匹配。

        Args:
            game_biz: 游戏标识, 如 'hk4e_cn' (原神), 'hkrpg_cn' (星铁)

        Returns:
            WEBM URL 或 None
        """
        bg_dates = self._get_bg_dates()
        date = bg_dates.get(game_biz)
        if not date:
            return None
        urls_by_date = self.get_cached_webm_urls()
        url, _ = self._match_video_url(game_biz, date, urls_by_date)
        return url

    def get_cached_webm_urls(self) -> Dict[str, Set[str]]:
        """
        从 CEF 缓存 data_1 中提取所有已缓存的 WEBM URL, 按日期分组。

        Returns:
            {date_str: set(url)} 如 {'2026/06/16': {'https://...'}}
        """
        return self._get_cached_webm_urls()

    def get_webm_urls(self) -> Set[str]:
        """获取 Local Storage 中所有 WEBM 视频 URL (历史所有缓存过的)。"""
        return self._scan_webm_urls(self._cfg['ls_dir'])

    def get_video_metadata(self) -> List[VideoMetadata]:
        """从 CEF 缓存 data_3 中提取所有 video/webm 的 HTTP 元信息。"""
        return self._parse_video_metadata()

    def get_cached_images(self) -> List[ImageInfo]:
        """列出 CEF 缓存中所有可提取的图片 (不复制)。"""
        return self._scan_cached_images()

    def list_output_files(self) -> Dict[str, List[str]]:
        """列出输出目录中已有的文件。返回 {'videos': [...], 'images': [...]}"""
        out = self._cfg['output_dir']
        if not os.path.isdir(out):
            return {'videos': [], 'images': []}
        videos = sorted(f for f in os.listdir(out) if f.endswith('.webm'))
        images = sorted(f for f in os.listdir(out) if f.endswith(('.webp', '.png', '.jpg')))
        return {'videos': videos, 'images': images}

    def is_running(self) -> bool:
        """检查启动器是否正在运行。"""
        return bool(self._find_processes())

    # ----------------------------------------------------------
    # 公共 API: 操作类
    # ----------------------------------------------------------

    def download_current_videos(self, game_bizs: Optional[List[str]] = None,
                                on_progress: Optional[Callable] = None) -> List[VideoInfo]:
        """
        下载指定游戏的当前背景视频。

        通过日期匹配精确定位各游戏当前使用的视频, 从 CDN 下载完整版。

        Args:
            game_bizs: 游戏标识列表, 默认 ['hk4e_cn'] (原神)
                       可选: 'hk4e_cn', 'hkrpg_cn', 'nap_cn', 'bh3_cn'
            on_progress: 可选回调 fn(VideoInfo) 每下载完一个时调用

        Returns:
            VideoInfo 列表 (包含成功和失败的)
        """
        if game_bizs is None:
            game_bizs = ['hk4e_cn']

        bg_dates = self._get_bg_dates()
        urls_by_date = self.get_cached_webm_urls()

        urls_to_download = []
        for biz in game_bizs:
            date = bg_dates.get(biz)
            if not date:
                self._log(f'{GAME_NAMES.get(biz, biz)}: 未找到背景日期', 'WARN')
                continue
            url, method = self._match_video_url(biz, date, urls_by_date)
            if url:
                name = GAME_NAMES.get(biz, biz)
                self._log(f'{name}: 匹配到视频 ({method})', 'OK')
                urls_to_download.append(url)
            else:
                name = GAME_NAMES.get(biz, biz)
                self._log(f'{name}: 未找到匹配的视频', 'WARN')

        if not urls_to_download:
            self._log('没有可下载的视频', 'WARN')
            return []

        return self.download_videos(urls_to_download, on_progress=on_progress)

    def download_videos(self, urls: List[str],
                        on_progress: Optional[Callable] = None) -> List[VideoInfo]:
        """
        从 CDN 下载完整的 WEBM 视频。

        Args:
            urls: WEBM URL 列表
            on_progress: 可选回调 fn(VideoInfo) 每下载完一个时调用

        Returns:
            VideoInfo 列表 (包含成功和失败的)
        """
        if not urls:
            self._log('没有需要下载的 URL', 'WARN')
            return []

        results = []
        for i, url in enumerate(sorted(urls), 1):
            fname = url.split('/')[-1]
            out_path = os.path.join(self._cfg['output_dir'], f'full_{fname}')

            if os.path.exists(out_path) and os.path.getsize(out_path) > 1048576:
                fsize = os.path.getsize(out_path)
                vi = VideoInfo(url=url, filename=f'full_{fname}', filepath=out_path,
                               size=fsize, success=True)
                self._probe_video(vi)
                results.append(vi)
                self._log(f'  [{i}] 已存在: {fname} ({fsize/1024/1024:.2f} MB)')
                if on_progress:
                    on_progress(vi)
                continue

            try:
                self._log(f'  [{i}/{len(urls)}] 下载: {fname} ...', end='')
                import ssl
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                last_err = None
                for attempt in range(1, 4):
                    try:
                        req = urllib.request.Request(url, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                            'Referer': 'https://launcher.mihoyo.com/'
                        })
                        with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
                            with open(out_path, 'wb') as f:
                                while True:
                                    chunk = resp.read(262144)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                        break
                    except (ssl.SSLError, urllib.error.URLError) as e:
                        last_err = e
                        if attempt < 3:
                            self._log(f' SSL错误, 重试({attempt}/3)...', 'WARN')
                            time.sleep(2)
                        else:
                            raise
                fsize = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
                vi = VideoInfo(url=url, filename=f'full_{fname}', filepath=out_path,
                               size=fsize, success=True)
                self._probe_video(vi)
                self._log(f' OK ({fsize/1024/1024:.2f} MB)', 'OK')
                if on_progress:
                    on_progress(vi)
            except Exception as e:
                vi = VideoInfo(url=url, filename=f'full_{fname}', filepath=out_path,
                               size=0, success=False, error=str(e))
                self._log(f' 失败: {e}', 'ERR')
            results.append(vi)

        return results

    def extract_images(self, on_progress: Optional[Callable] = None) -> List[ImageInfo]:
        """
        从 CEF 缓存中提取静态背景图到 output_dir。

        启动器关闭时可获取完整尺寸图片 (不受 1MB 限制)。
        启动器运行时图片可能被截断到 1MB。

        Args:
            on_progress: 可选回调 fn(ImageInfo) 每提取一张图时调用

        Returns:
            新提取的 ImageInfo 列表
        """
        self._log('提取缓存中的静态背景图...')
        results = []

        for info in self._scan_cached_images():
            dst = os.path.join(self._cfg['output_dir'], info.filename)
            if os.path.exists(dst):
                if os.path.getsize(dst) < info.size:
                    self._copy_file(info.filepath, dst)
                    info.filepath = dst
                    results.append(info)
                    self._log(f'  更新: {info.filename} ({info.size/1024/1024:.2f} MB)', 'OK')
                    if on_progress:
                        on_progress(info)
                continue
            self._copy_file(info.filepath, dst)
            info.filepath = dst
            results.append(info)
            self._log(f'  提取: {info.filename} ({info.size/1024/1024:.2f} MB)', 'OK')
            if on_progress:
                on_progress(info)

        self._log(f'共提取 {len(results)} 张', 'OK')
        return results

    def verify_all_videos(self) -> List[VideoInfo]:
        """验证 output_dir 中所有视频的完整性。"""
        results = []
        out = self._cfg['output_dir']
        if not os.path.isdir(out):
            return results
        for fname in sorted(os.listdir(out)):
            if not fname.endswith('.webm'):
                continue
            fpath = os.path.join(out, fname)
            vi = VideoInfo(url='', filename=fname, filepath=fpath,
                           size=os.path.getsize(fpath), success=True)
            self._probe_video(vi)
            results.append(vi)
            status = '完整' if vi.is_complete else '截断!'
            self._log(f'  {fname}: {vi.duration}s {vi.width}x{vi.height} [{status}]')
        return results

    # ----------------------------------------------------------
    # 公共 API: 启动器控制
    # ----------------------------------------------------------

    def kill_launcher(self, wait: int = 15) -> bool:
        """关闭启动器进程。返回 True 如果已关闭。"""
        procs = self._find_processes()
        if not procs:
            self._log('启动器未在运行', 'OK')
            return True

        for name, pid in procs:
            self._log(f'关闭 {name} (PID: {pid})...')
            try:
                subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                              capture_output=True, timeout=10)
            except:
                pass

        for i in range(wait):
            time.sleep(1)
            if not self._find_processes():
                self._log(f'已关闭 ({i+1}s)', 'OK')
                return True

        self._log('可能未完全退出', 'WARN')
        return False

    def start_launcher(self) -> None:
        """启动启动器。"""
        exe = self._cfg['launcher_exe']
        self._log(f'启动: {exe}')
        subprocess.Popen([exe], cwd=os.path.dirname(exe))

    # ----------------------------------------------------------
    # 公共 API: 高级流程
    # ----------------------------------------------------------

    def run_full(self, game_bizs: Optional[List[str]] = None,
                 on_progress: Optional[Callable] = None) -> ExtractReport:
        """
        一键完整提取: 关闭→提取完整图片→下载当前视频→验证

        关键: 关闭启动器后再提取图片, 可获取不受 1MB 限制的完整尺寸图片。
        视频通过日期匹配精确定位, 无需重启启动器。

        Args:
            game_bizs: 要下载视频的游戏列表, 默认 ['hk4e_cn']
            on_progress: 可选回调 fn(event, **kwargs)
        """
        t0 = time.time()
        report = ExtractReport()

        def _emit(event, **kw):
            if on_progress:
                on_progress(event, kw)

        # 1. 获取游戏信息
        _emit('game_info')
        report.game_infos = self.get_game_infos()
        for gi in report.game_infos:
            self._log(f'{gi.game_name}: 背景日期 {gi.bg_date}')

        # 2. 关闭启动器 (关键: 关闭后图片不再截断)
        _emit('kill_launcher')
        report.launcher_killed = self.kill_launcher()
        time.sleep(2)

        # 3. 提取完整图片 (启动器已关闭)
        _emit('extract_images')
        report.images = self.extract_images()

        # 4. 下载当前视频
        _emit('download')
        report.videos = self.download_current_videos(game_bizs, on_progress=on_progress)

        # 5. 元信息
        report.metadata = self.get_video_metadata()

        report.elapsed = time.time() - t0
        _emit('done', report=report)
        return report

    def run_download_current(self, game_bizs: Optional[List[str]] = None,
                             on_progress: Optional[Callable] = None) -> ExtractReport:
        """
        仅下载当前视频模式: 不关闭启动器, 直接下载指定游戏的当前背景视频。

        Args:
            game_bizs: 游戏列表, 默认 ['hk4e_cn']
            on_progress: 可选回调 fn(VideoInfo)
        """
        t0 = time.time()
        report = ExtractReport()
        report.game_infos = self.get_game_infos()
        report.videos = self.download_current_videos(game_bizs, on_progress=on_progress)
        report.elapsed = time.time() - t0
        return report

    def run_extract_only(self) -> ExtractReport:
        """仅提取模式: 不关闭/重启启动器, 只提取缓存中的图片资源。"""
        report = ExtractReport()
        report.images = self.extract_images()
        report.metadata = self.get_video_metadata()
        report.videos = self.verify_all_videos()
        return report

    # ----------------------------------------------------------
    # 内部方法: 核心定位逻辑
    # ----------------------------------------------------------

    def _get_bg_dates(self) -> Dict[str, str]:
        """
        从 Local Storage 读取各游戏当前背景日期。

        V8 序列化会压缩重复字符串 (包括游戏 ID), 导致完整 ID 不可直接读取。
        改用片段匹配策略:
        1. 从 lastBackgroundUrlByBiz 提取所有背景图哈希 (按出现顺序)
        2. 在相邻哈希之间的二进制数据中搜索游戏 ID 片段 (≥3 字符)
           V8 压缩后仍保留短前缀 (如 hk4e_cn → hk4), 片段位于对应哈希之前
        3. 对未匹配到片段的哈希, 从 gameHubsByBiz 读取游戏顺序作为回退
        4. 在 CEF 缓存 data_1 中查找每个哈希对应的完整日期
        """
        result = {}
        ls_dir = self._cfg['ls_dir']

        # Step 1: 从 lastBackgroundUrlByBiz 提取哈希, 通过片段匹配游戏 ID
        bg_hashes = []   # [(biz, hash_id), ...]
        for fname in sorted(os.listdir(ls_dir), reverse=True):
            if not fname.endswith(('.log', '.ldb')):
                continue
            fpath = os.path.join(ls_dir, fname)
            try:
                data = self._read_file_shared(fpath)
                if not data:
                    continue
            except:
                continue

            bg_idx = data.find(b'lastBackgroundUrlByBiz')
            if bg_idx < 0:
                continue

            bg_chunk = data[bg_idx:bg_idx + 3000]

            # 提取所有哈希 (按出现顺序)
            all_hashes = list(re.finditer(rb'([a-f0-9]{20,}_\d+)', bg_chunk))
            if not all_hashes:
                continue

            hash_values = [m.group(1).decode('ascii') for m in all_hashes]
            hash_positions = [(m.start(1), m.end(1)) for m in all_hashes]

            # 通过片段匹配将游戏 ID 分配到对应哈希
            # JSON 结构: {"biz1":"...hash0...","biz1":"...hash1...",...}
            # 游戏 ID 片段出现在对应哈希之前 (作为 JSON 键)
            matched = {}   # hash_index → biz

            # 检查第一个哈希之前的区域 (第一个 JSON 键)
            if hash_positions:
                before_first = bg_chunk[:hash_positions[0][0]]
                for biz in GAME_NAMES:
                    for plen in range(len(biz), 2, -1):
                        if biz[:plen].encode() in before_first:
                            matched[0] = biz
                            break
                    if 0 in matched:
                        break

            # 检查相邻哈希之间的区域
            for i in range(len(hash_positions) - 1):
                between = bg_chunk[hash_positions[i][1]:hash_positions[i + 1][0]]
                for biz in GAME_NAMES:
                    if biz in matched.values():
                        continue
                    for plen in range(len(biz), 2, -1):
                        if biz[:plen].encode() in between:
                            matched[i + 1] = biz
                            break
                    if i + 1 in matched:
                        break

            # 回退: 从 gameHubsByBiz 获取游戏顺序, 分配到未匹配的哈希
            if len(matched) < len(hash_values):
                game_order = []
                gh_idx = data.find(b'gameHubsByBiz')
                if gh_idx >= 0:
                    gh_chunk = data[gh_idx:gh_idx + 10000]
                    for m in re.finditer(rb'"gameBiz"\s*:\s*"([^"]+)"', gh_chunk):
                        biz = m.group(1).decode('ascii')
                        if biz in GAME_NAMES and biz not in game_order:
                            game_order.append(biz)
                if not game_order:
                    game_order = [b for b in GAME_NAMES if b in ('nap_cn', 'hkrpg_cn', 'hk4e_cn')]

                for i in range(len(hash_values)):
                    if i not in matched:
                        for biz in game_order:
                            if biz not in matched.values():
                                matched[i] = biz
                                break

            for i, h in enumerate(hash_values):
                if i in matched:
                    bg_hashes.append((matched[i], h))
            break

        if not bg_hashes:
            # Fallback: 尝试原始正则 (适用于未压缩的数据)
            for fname in sorted(os.listdir(ls_dir), reverse=True):
                if not fname.endswith(('.log', '.ldb')):
                    continue
                fpath = os.path.join(ls_dir, fname)
                try:
                    data = self._read_file_shared(fpath)
                    if not data:
                        continue
                except:
                    continue
                idx = data.find(b'lastBackgroundUrlByBiz')
                if idx < 0:
                    continue
                chunk = data[idx:idx + 3000]
                for biz in GAME_NAMES:
                    pattern = biz.encode() + rb'.*?launcher-public/(\d{4}/\d{2}/\d{2})/'
                    m = re.search(pattern, chunk)
                    if m:
                        result[biz] = m.group(1).decode('ascii')
                if result:
                    break
            return result

        # Step 2: 在 data_1 缓存中查找每个哈希对应的日期
        data_1_path = os.path.join(self._cfg['cache_dir'], 'data_1')
        data_1 = None
        if os.path.isfile(data_1_path):
            try:
                data_1 = self._read_file_shared(data_1_path)
            except:
                pass

        if data_1:
            for biz, hash_id in bg_hashes:
                hash_bytes = hash_id.encode('ascii')
                idx = data_1.find(hash_bytes)
                if idx >= 0:
                    context = data_1[max(0, idx - 100):idx + len(hash_bytes)]
                    m = re.search(rb'(\d{4}/\d{2}/\d{2})', context)
                    if m:
                        result[biz] = m.group(1).decode('ascii')

        return result

    def _get_bg_urls(self) -> Dict[str, str]:
        """从 Local Storage 读取各游戏当前静态背景图 URL。"""
        result = {}
        ls_dir = self._cfg['ls_dir']
        for fname in sorted(os.listdir(ls_dir), reverse=True):
            if not fname.endswith(('.log', '.ldb')):
                continue
            fpath = os.path.join(ls_dir, fname)
            try:
                data = self._read_file_shared(fpath)
                if not data:
                    continue
            except:
                continue

            idx = data.find(b'lastBackgroundUrlByBiz')
            if idx < 0:
                continue

            chunk = data[idx:idx + 5000]
            for biz in GAME_NAMES:
                pattern = biz.encode() + rb'.*?(https://launcher-webstatic[^\x00"]+\.(?:webp|png|jpg))'
                m = re.search(pattern, chunk)
                if m:
                    result[biz] = m.group(1).decode('ascii', errors='replace')
            break

        return result

    def _get_cached_webm_urls(self) -> Dict[str, Set[str]]:
        """从 CEF 缓存 data_1 中提取所有 WEBM URL, 按日期分组。"""
        data_1_path = os.path.join(self._cfg['cache_dir'], 'data_1')
        if not os.path.isfile(data_1_path):
            return {}

        data = self._read_file_shared(data_1_path)
        if not data:
            return {}

        urls_by_date = {}
        pattern = rb'https://launcher-webstatic\.mihoyo\.com/launcher-public/(\d{4}/\d{2}/\d{2})/[a-f0-9]+_\d+\.webm'

        for m in re.finditer(pattern, data):
            url = m.group(0).decode('ascii')
            date = m.group(1).decode('ascii')
            if date not in urls_by_date:
                urls_by_date[date] = set()
            urls_by_date[date].add(url)

        return urls_by_date

    def _match_video_url(self, game_biz: str, bg_date: str,
                         urls_by_date: Dict[str, Set[str]]) -> Tuple[Optional[str], Optional[str]]:
        """
        根据游戏背景日期匹配视频 URL。

        匹配策略:
          1. 精确日期匹配: 视频 URL 日期 == 背景日期
          2. 最近日期匹配: 取不超过背景日期的最近视频

        Returns:
            (url, method) 或 (None, None)
        """
        # 精确匹配
        if bg_date in urls_by_date:
            urls = urls_by_date[bg_date]
            if urls:
                return sorted(urls)[0], 'exact_date'

        # 最近日期
        matching = [d for d in sorted(urls_by_date.keys(), reverse=True) if d <= bg_date]
        if matching:
            closest = matching[0]
            urls = urls_by_date[closest]
            if urls:
                return sorted(urls)[0], f'closest_date({closest})'

        return None, None

    # ----------------------------------------------------------
    # 内部方法: 文件操作
    # ----------------------------------------------------------

    def _log(self, msg, level='INFO', end='\n'):
        if not self._verbose:
            return
        ts = datetime.now().strftime('%H:%M:%S')
        prefix = {'INFO': ' ', 'OK': ' ', 'WARN': ' ', 'ERR': ' '}
        print(f"[{ts}]{prefix.get(level, ' ')} {msg}", end=end, flush=True)

    def _copy_file(self, src, dst):
        """复制文件, 使用 Windows API 共享读取以处理锁定文件。"""
        try:
            shutil.copy2(src, dst)
        except PermissionError:
            data = self._read_file_shared(src)
            if data:
                with open(dst, 'wb') as f:
                    f.write(data)
            else:
                raise

    def _read_file_shared(self, filepath: str) -> Optional[bytes]:
        """使用 Windows CreateFile 以共享读取模式打开被锁定的文件。"""
        GENERIC_READ = 0x80000000
        SHARE_ALL = 0x00000007
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            filepath, GENERIC_READ, SHARE_ALL,
            None, 3, 0x80, None
        )
        if handle == ctypes.wintypes.HANDLE(-1).value or handle is None:
            return None
        try:
            size = kernel32.GetFileSize(handle, None)
            buf = ctypes.create_string_buffer(size)
            n = wt.DWORD(0)
            kernel32.ReadFile(handle, buf, size, ctypes.byref(n), None)
            return buf.raw[:n.value]
        finally:
            kernel32.CloseHandle(handle)

    def _find_processes(self) -> List[Tuple[str, int]]:
        procs = []
        for name in ['launcher.exe', 'HYP.exe']:
            try:
                r = subprocess.run(
                    ['tasklist', '/FI', f'IMAGENAME eq {name}', '/FO', 'CSV', '/NH'],
                    capture_output=True, text=True, timeout=10
                )
                for line in r.stdout.strip().split('\n'):
                    if name.lower() in line.lower():
                        parts = line.split(',')
                        if len(parts) >= 2:
                            procs.append((name, int(parts[1].strip('"'))))
            except:
                continue
        return procs

    # ----------------------------------------------------------
    # 内部方法: 缓存解析
    # ----------------------------------------------------------

    def _scan_webm_urls(self, directory: str) -> Set[str]:
        """从 LevelDB 文件中提取所有 WEBM URL。"""
        urls = set()
        if not os.path.isdir(directory):
            return urls
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue
            data = self._read_file_shared(fpath)
            if not data:
                continue
            found = re.findall(rb'https?://[^\x00\x22\x27\x3e\x3c\x60\x0a\x0d]{5,400}\.webm', data)
            for u in found:
                urls.add(u.decode('ascii', errors='replace'))
            paths = re.findall(rb'public/\d{4}/\d{2}/\d{2}/[a-f0-9]+_[0-9]+\.webm', data)
            for p in paths:
                urls.add('https://launcher-webstatic.mihoyo.com/launcher-' + p.decode('ascii'))
        return urls

    def _scan_cached_images(self) -> List[ImageInfo]:
        """扫描 CEF 缓存中的图片文件。"""
        results = []
        cache = self._cfg['cache_dir']
        if not os.path.isdir(cache):
            return results
        for fname in sorted(os.listdir(cache)):
            if not fname.startswith('f_'):
                continue
            fpath = os.path.join(cache, fname)
            if not os.path.isfile(fpath):
                continue
            fsize = os.path.getsize(fpath)
            if fsize < 100 * 1024:
                continue
            try:
                with open(fpath, 'rb') as f:
                    header = f.read(12)
            except:
                continue
            fmt = None
            if header[:4] == b'RIFF' and len(header) >= 12 and header[8:12] == b'WEBP':
                fmt = 'webp'
            elif header[:8] == b'\x89PNG\r\n\x1a\n':
                fmt = 'png'
            elif header[:3] == b'\xff\xd8\xff':
                fmt = 'jpg'
            if fmt:
                results.append(ImageInfo(
                    cache_file=fname,
                    filename=f'full_{fname}.{fmt}',
                    filepath=fpath,
                    size=fsize,
                    format=fmt,
                ))
        return results

    def _parse_video_metadata(self) -> List[VideoMetadata]:
        """从 data_3 中提取 video/webm 的 HTTP 响应头信息。"""
        data = self._read_file_shared(os.path.join(self._cfg['cache_dir'], 'data_3'))
        if not data:
            return []
        entries = []
        pos = 0
        while True:
            pos = data.find(b'content-type:video/webm', pos)
            if pos == -1:
                break
            start = pos
            while start > 0 and data[start-1] != 0:
                start -= 1
            end = pos + 23
            while end < len(data) - 1:
                if data[end] == 0 and data[end+1] == 0:
                    break
                end += 1
            region = data[start:end]
            parts = [p.decode('ascii', errors='replace') for p in region.split(b'\x00') if p]
            headers = {}
            for part in parts:
                if ':' in part:
                    k, _, v = part.partition(':')
                    headers[k.strip().lower()] = v.strip()
            cr = headers.get('content-range', '')
            m = re.search(r'(\d+)/(\d+)', cr)
            total = int(m.group(2)) if m else int(headers.get('content-length', '0'))
            entries.append(VideoMetadata(
                content_type='video/webm',
                total_size=total,
                etag=headers.get('etag', ''),
                content_md5=headers.get('content-md5', ''),
                content_range=cr,
                last_modified=headers.get('last-modified', ''),
                headers=headers,
            ))
            pos = end + 1
        return entries

    def _probe_video(self, vi: VideoInfo):
        """用 ffprobe 填充 VideoInfo 的编码/尺寸/时长信息。"""
        if not os.path.exists(vi.filepath):
            return
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries',
                 'format=duration:stream=codec_name,width,height',
                 '-of', 'csv=p=0', vi.filepath],
                capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                try:
                    vi.duration = round(float(line), 2)
                    continue
                except ValueError:
                    pass
                try:
                    val = int(line)
                    if vi.width is None:
                        vi.width = val
                    elif vi.height is None:
                        vi.height = val
                    continue
                except ValueError:
                    pass
                if vi.codec is None:
                    vi.codec = line
            if vi.duration is not None and vi.duration < 3:
                vi.is_complete = False
        except:
            pass


# ============================================================
# CLI 入口
# ============================================================
def main():
    args = sys.argv[1:]
    do_list = '--list' in args
    do_verify = '--verify' in args
    do_extract = '--extract' in args
    do_download = '--download' in args
    do_all = '--all' in args
    do_info_json = '--info-json' in args

    target_game = None
    output_dir_override = None
    for i, arg in enumerate(args):
        if arg == '--game' and i + 1 < len(args):
            target_game = args[i + 1]
        elif arg == '--output-dir' and i + 1 < len(args):
            output_dir_override = args[i + 1]

    # --info-json: 输出 JSON 格式的游戏信息 (供外部调用)
    if do_info_json:
        import json
        launcher = HypLauncher(verbose=False)
        bg_dates = launcher.get_game_background_dates()
        urls_by_date = launcher.get_cached_webm_urls()
        games = {}
        for biz, date in bg_dates.items():
            url, method = launcher._match_video_url(biz, date, urls_by_date)
            games[biz] = {
                'name': GAME_NAMES.get(biz, biz),
                'bg_date': date,
                'video_url': url,
                'match_method': method,
            }
        json.dump({'games': games}, sys.stdout, ensure_ascii=False)
        return

    launcher_kwargs = {}
    if output_dir_override:
        launcher_kwargs['output_dir'] = output_dir_override
    launcher = HypLauncher(**launcher_kwargs)

    print()
    print('=' * 65)
    print('  米哈游启动器 - 背景资源提取工具')
    print('=' * 65)
    print()

    # --list: 列出所有缓存的视频 URL
    if do_list:
        urls_by_date = launcher.get_cached_webm_urls()
        print(f'缓存中共 {sum(len(v) for v in urls_by_date.values())} 个 WEBM URL:\n')
        for date in sorted(urls_by_date.keys(), reverse=True):
            for url in sorted(urls_by_date[date]):
                print(f'  [{date}] {url.split("/")[-1]}')
        return

    # --verify: 验证已下载视频
    if do_verify:
        print('验证已下载视频:\n')
        launcher.verify_all_videos()
        return

    # --extract: 仅提取缓存图片
    if do_extract:
        report = launcher.run_extract_only()
        print(f'\n{report.summary()}')
        return

    # 确定要处理的游戏列表
    if do_all:
        game_bizs = list(GAME_NAMES.keys())
    elif target_game:
        game_bizs = [target_game]
    else:
        game_bizs = ['hk4e_cn']

    # --download: 仅下载当前视频 (不关闭启动器)
    if do_download:
        report = launcher.run_download_current(game_bizs)
        print(f'\n{report.summary()}')
        files = launcher.list_output_files()
        print(f'  视频文件: {len(files["videos"])} 个')
        print(f'  保存目录: {launcher._cfg["output_dir"]}')
        print()
        return

    # 默认: 完整流程
    report = launcher.run_full(game_bizs)

    print()
    print('=' * 65)
    print('  提取完成!')
    print('=' * 65)
    print(f'  {report.summary()}')
    files = launcher.list_output_files()
    print(f'  视频文件: {len(files["videos"])} 个')
    print(f'  图片文件: {len(files["images"])} 个')
    print(f'  保存目录: {launcher._cfg["output_dir"]}')
    print()


if __name__ == '__main__':
    main()
