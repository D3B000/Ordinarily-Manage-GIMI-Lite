#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HoYoMediaExtractor - 米哈游启动器缓存媒体提取工具

从米哈游启动器 (HoYoPlay) 的 data_1 缓存文件中提取图片和视频链接，
生成 OMG 风格 HTML 页面在浏览器中展示，支持视频原生播放。

使用方式:
    python HoYoMediaExtractor.py [data_1 文件路径]

缓存路径:
    国服: %USERPROFILE%\\AppData\\Roaming\\miHoYo\\HYP\\1_1\\fedata\\Cache\\Cache_Data
    国际服: %USERPROFILE%\\AppData\\Roaming\\Cognosphere\\HYP\\1_0\\fedata\\Cache\\Cache_Data

参考: https://github.com/iBobbyTS/HoYoPlayExtractor
"""

import sys
import os
import re
import json
import tempfile
import webbrowser
import subprocess
import time
import threading
import urllib.request
import shutil
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════

IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp'}
VIDEO_EXTS = {'webm', 'mp4'}
ITEMS_PER_PAGE = 30

CACHE_PATHS = {
    '国服': os.path.normpath(os.path.join(
        os.environ.get('USERPROFILE', ''),
        'AppData', 'Roaming', 'miHoYo', 'HYP', '1_1',
        'fedata', 'Cache', 'Cache_Data'
    )),
    '国际服': os.path.normpath(os.path.join(
        os.environ.get('USERPROFILE', ''),
        'AppData', 'Roaming', 'Cognosphere', 'HYP', '1_0',
        'fedata', 'Cache', 'Cache_Data'
    )),
}

URL_REGEX = re.compile(
    r"https?://[a-zA-Z0-9_\-\.~:/\?#\[\]@!$&'\(\)\*\+,;=%]+"
)

# 米哈游启动器可能的进程名
LAUNCHER_EXE_NAMES = ['hyp.exe', 'hoyoplay.exe', 'launcher.exe', 'mihoyo.exe']


# ═══════════════════════════════════════════════════════════════════
#  Environment Detection
# ═══════════════════════════════════════════════════════════════════

def find_data1_auto() -> tuple:
    """自动查找最新的 data_1 文件，返回 (path, source_label) 或 (None, None)"""
    best_path = None
    best_time = 0
    best_label = None
    for label, dirpath in CACHE_PATHS.items():
        if not os.path.isdir(dirpath):
            continue
        for fname in os.listdir(dirpath):
            if fname.startswith('data_1'):
                fpath = os.path.join(dirpath, fname)
                if os.path.isfile(fpath):
                    mt = os.path.getmtime(fpath)
                    if mt > best_time:
                        best_time = mt
                        best_path = fpath
                        best_label = label
    return best_path, best_label


def check_launcher_running() -> bool:
    """检测米哈游启动器是否正在运行"""
    try:
        output = subprocess.check_output(
            ['tasklist', '/fo', 'csv', '/nh'],
            stderr=subprocess.DEVNULL
        ).decode('utf-8', errors='ignore').lower()
        for name in LAUNCHER_EXE_NAMES:
            if f'"{name}"' in output or f"'{name}'" in output or f'{name} ' in output:
                return True
    except Exception:
        pass
    return False


def check_launcher_installed() -> bool:
    """检测米哈游启动器是否已安装"""
    for dirpath in CACHE_PATHS.values():
        if os.path.isdir(dirpath):
            return True
    return False


DOWNLOAD_SCRIPT_CONTENT = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HoYoMediaExtractor - 单文件下载器"""
import sys, os, urllib.request
from urllib.parse import urlparse, unquote

BG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bg')

def get_filename(url):
    path = unquote(urlparse(url).path)
    return os.path.basename(path) or 'download'

def unique_path(fp):
    if not os.path.exists(fp): return fp
    base, ext = os.path.splitext(fp)
    i = 1
    while os.path.exists(f'{base}_{i}{ext}'): i += 1
    return f'{base}_{i}{ext}'

def download(url):
    os.makedirs(BG_DIR, exist_ok=True)
    dest = unique_path(os.path.join(BG_DIR, get_filename(url)))
    print(f'URL:  {url}')
    print(f'保存到: {dest}\n')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = resp.headers.get('Content-Length')
            total = int(total) if total else None
            dl = 0
            with open(dest, 'wb') as f:
                while True:
                    chunk = resp.read(524288)
                    if not chunk: break
                    f.write(chunk); dl += len(chunk)
                    if total:
                        print(f'\r  {dl/1048576:.1f}/{total/1048576:.1f} MB [{dl*100//total}%]', end='', flush=True)
                    else:
                        print(f'\r  {dl/1048576:.1f} MB', end='', flush=True)
        print(f'\n下载完成!')
    except Exception as e:
        print(f'下载失败: {e}')
        if os.path.exists(dest): os.remove(dest)
        sys.exit(1)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: python download_bg.py <URL>'); input('按回车键退出...'); sys.exit(1)
    download(sys.argv[1]); print(); input('按回车键退出...')
'''


def deploy_download_script(output_dir: str):
    """将下载脚本部署到输出目录"""
    script_path = os.path.join(output_dir, 'download_bg.py')
    with open(script_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(DOWNLOAD_SCRIPT_CONTENT)


# ═══════════════════════════════════════════════════════════════════
#  Local HTTP Server (下载服务)
# ═══════════════════════════════════════════════════════════════════

class _DownloadHandler(BaseHTTPRequestHandler):
    """处理来自浏览器的下载请求"""

    close_connection = True

    def end_headers(self):
        self.send_header('Connection', 'close')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/ffmpeg_check':
            self._handle_ffmpeg_check()
        elif parsed.path == '/download':
            self._handle_download(parsed)
        elif parsed.path == '/job_progress':
            self._handle_job_progress(parsed)
        elif parsed.path == '/' or parsed.path == '/index.html':
            self._serve_html()
        else:
            self.send_error(404)

    def _handle_ffmpeg_check(self):
        """GET /ffmpeg_check → 返回 {available, path}，用于前端决定是否显示转码按钮"""
        path = _find_ffmpeg()
        self._json_response(200, {
            'available': bool(path),
            'path': path,
        })

    def _handle_download(self, parsed):
        """GET /download?url=...&mode=webm|mp4

        异步启动后台任务，立刻返回 job_id，前端通过 /job_progress 轮询。
        """
        params = parse_qs(parsed.query)
        url = params.get('url', [''])[0]
        mode = (params.get('mode', ['webm'])[0] or 'webm').lower()
        if not url:
            self._json_response(400, {'status': 'error', 'error': '缺少 URL 参数'})
            return
        if mode not in ('webm', 'mp4'):
            self._json_response(400, {'status': 'error', 'error': 'mode 必须为 webm 或 mp4'})
            return

        if mode == 'mp4' and not _find_ffmpeg():
            self._json_response(500, {
                'status': 'error',
                'error': '未检测到 ffmpeg.exe，请先安装 FFmpeg 并加入系统 PATH',
            })
            return

        # 异步：创建任务 → 立刻返回 job_id
        bg_dir = self.server.bg_dir
        job_id = uuid.uuid4().hex
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                'job_id': job_id,
                'status': 'queued',
                'phase': 'queue',
                'progress': 0.0,
                'message': '排队中...',
                'filename': '',
                'size': 0,
                'error': '',
                'mode': mode,
            }
        t = threading.Thread(target=_start_bg_job,
                             args=(job_id, url, mode, bg_dir),
                             daemon=True)
        t.start()
        self._json_response(200, {'status': 'ok', 'job_id': job_id})

    def _handle_job_progress(self, parsed):
        """GET /job_progress?id=JOB_ID → 返回当前任务状态快照"""
        params = parse_qs(parsed.query)
        job_id = params.get('id', [''])[0]
        with _JOBS_LOCK:
            snap = dict(_JOBS.get(job_id) or {})
        if not snap:
            self._json_response(404, {'status': 'error', 'error': '任务不存在'})
            return
        self._json_response(200, {'status': 'ok', **snap})

    def _serve_html(self):
        html_path = os.path.join(self.server.output_dir, 'index.html')
        if not os.path.isfile(html_path):
            self.send_error(404)
            return
        with open(html_path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


# 全局服务器引用（供 shutdown() 关闭）
_http_server = None

# ═══════════════════════════════════════════════════════════════════
#  后台下载 + 转码任务（支持轮询进度）
# ═══════════════════════════════════════════════════════════════════

# { job_id: {status, phase, progress(0-100), message, filename, size, error} }
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()

_TRANSCODED_EXTS = {'webm'}  # 目前需要转码的源格式（VP9）


def _find_ffmpeg() -> str:
    """返回系统可执行 ffmpeg 的绝对路径，找不到返回空字符串。

    搜索顺序：项目内置 ffmpeg → shutil.which('ffmpeg') → 常见安装路径。
    """
    # 项目内置路径：bin/lib/ffmpeg/bin/ffmpeg.exe
    _here = os.path.dirname(os.path.abspath(__file__))
    _proj_ffmpeg = os.path.join(_here, '..', 'lib', 'ffmpeg', 'bin', 'ffmpeg.exe')
    if os.path.isfile(_proj_ffmpeg):
        return os.path.normpath(_proj_ffmpeg)

    path = shutil.which('ffmpeg') or ''
    if path and os.path.isfile(path):
        return path
    # 兜底：常见静态解压目录
    for c in [r'C:\ffmpeg\bin\ffmpeg.exe', r'C:\Program Files\ffmpeg\bin\ffmpeg.exe']:
        if os.path.isfile(c):
            return c
    return ''


def _make_unique_path(dest: str) -> str:
    """若 dest 已存在，追加 _1/_2 直至唯一。"""
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(f'{base}_{i}{ext}'):
        i += 1
    return f'{base}_{i}{ext}'


def _parse_ffmpeg_progress(stderr_line: str, total_duration_sec: float) -> float:
    """从 ffmpeg -progress pipe:1 输出解析当前百分比 (0-100)。

    ffmpeg 用 -progress pipe:1 会每行一个 key=value，其中 out_time_ms=毫秒。
    当无法解析或 duration 未知时返回 -1。
    """
    try:
        if '=' not in stderr_line:
            return -1
        k, v = stderr_line.strip().split('=', 1)
        if k == 'out_time_ms':
            ms = int(v)  # 微秒
            if total_duration_sec > 0 and ms >= 0:
                pct = (ms / 1_000_000.0) / total_duration_sec * 100.0
                return max(0.0, min(100.0, pct))
    except Exception:
        return -1
    return -1


def _probe_duration(src_file: str, ffmpeg_path: str) -> float:
    """通过 ffprobe 获取视频总时长（秒），失败返回 0。"""
    ffprobe = os.path.join(os.path.dirname(ffmpeg_path),
                           'ffprobe' + (os.path.splitext(ffmpeg_path)[1] or '.exe'))
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which('ffprobe') or ''
    if not ffprobe or not os.path.isfile(ffprobe):
        return 0.0
    try:
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', src_file],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if r.returncode == 0:
            return float(r.stdout.strip() or 0)
    except Exception:
        pass
    return 0.0


def _update_job(job_id: str, **fields):
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


def _start_bg_job(job_id: str, url: str, mode: str, bg_dir: str):
    """后台线程：下载 +（可选）转码，期间通过 _JOBS 汇报进度。

    mode: 'webm'  -> 直接下载到 bg_dir，保持原扩展名
          'mp4'   -> 先下载到临时目录，再 ffmpeg H.264 转码，输出 .mp4 到 bg_dir
    """
    ffmpeg_path = _find_ffmpeg() if mode == 'mp4' else ''
    if mode == 'mp4' and not ffmpeg_path:
        _update_job(job_id, status='error', phase='error',
                    error='未检测到 ffmpeg.exe，请先安装 FFmpeg 并加入系统 PATH')
        return

    try:
        # --- 阶段 1: 下载 ---
        _update_job(job_id, status='running', phase='download', progress=0.0,
                    message='正在下载...')
        path = unquote(urlparse(url).path)
        orig_name = os.path.basename(path) or 'download'
        orig_ext = os.path.splitext(orig_name)[1].lower().lstrip('.')

        # 下载保存位置：直接 mp4 模式先放临时目录（转码后再移动到 bg_dir）
        if mode == 'mp4':
            tmp_dir = tempfile.mkdtemp(prefix='omg_transcode_')
            tmp_src = _make_unique_path(os.path.join(tmp_dir, orig_name))
        else:
            tmp_dir = None
            tmp_src = _make_unique_path(os.path.join(bg_dir, orig_name))

        os.makedirs(os.path.dirname(tmp_src), exist_ok=True)
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = resp.headers.get('Content-Length')
                total = int(total) if total else None
                downloaded = 0
                with open(tmp_src, 'wb') as f:
                    while True:
                        chunk = resp.read(524288)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total and total > 0:
                            pct = downloaded / total * 100.0
                            mb = downloaded / 1048576
                            _update_job(
                                job_id,
                                progress=max(0.0, min(100.0, pct * 0.5)),  # 下载占总进度 50%
                                message=f'下载中 {mb:.1f}MB / {total/1048576:.1f}MB'
                            )
                        else:
                            mb = downloaded / 1048576
                            _update_job(
                                job_id,
                                progress=min(50.0, 25 + mb * 0.5),
                                message=f'下载中 {mb:.1f}MB'
                            )
        except Exception as e:
            if os.path.exists(tmp_src):
                try:
                    os.remove(tmp_src)
                except Exception:
                    pass
            _update_job(job_id, status='error', phase='error', error=f'下载失败: {e}')
            return

        # webm 直接下载模式 → 到此结束
        if mode != 'mp4':
            actual_name = os.path.basename(tmp_src)
            size_mb = downloaded / 1048576
            _update_job(job_id, status='ok', phase='done', progress=100.0,
                        filename=actual_name, size=downloaded,
                        message=f'下载完成: {actual_name} ({size_mb:.1f}MB)')
            return

        # --- 阶段 2: 转码 MP4 ---
        _update_job(job_id, phase='transcode', progress=50.0, message='准备转码...')

        duration_sec = _probe_duration(tmp_src, ffmpeg_path)
        base_name = os.path.splitext(os.path.basename(tmp_src))[0]
        out_name = base_name + '.mp4'
        final_dest = _make_unique_path(os.path.join(bg_dir, out_name))

        # ffmpeg: VP9 → H.264 libx264 CRF 21 视觉无损，yuv420p 兼容硬解，音频 AAC
        # -progress pipe:1 会在 stdout 输出结构化进度（out_time_ms=...）
        cmd = [
            ffmpeg_path, '-y', '-hide_banner', '-nostdin',
            '-i', tmp_src,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '21',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            '-progress', 'pipe:1',
            '-loglevel', 'error',
            final_dest,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception as e:
            if os.path.exists(final_dest):
                try:
                    os.remove(final_dest)
                except Exception:
                    pass
            _update_job(job_id, status='error', phase='error',
                        error=f'启动 ffmpeg 失败: {e}')
            return

        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                pct = _parse_ffmpeg_progress(line, duration_sec)
                if pct >= 0:
                    # 总进度 50-100% 对应转码阶段
                    overall = 50.0 + pct * 0.5
                    _update_job(job_id, progress=overall,
                                message=f'转码中 {int(pct)}%')
        except Exception:
            pass

        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            rc = -1

        # 清理源临时文件
        try:
            os.remove(tmp_src)
        except Exception:
            pass
        if tmp_dir:
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

        if rc != 0 or not os.path.isfile(final_dest):
            if os.path.exists(final_dest):
                try:
                    os.remove(final_dest)
                except Exception:
                    pass
            _update_job(job_id, status='error', phase='error',
                        error=f'转码失败 (ffmpeg rc={rc})')
            return

        size = os.path.getsize(final_dest)
        size_mb = size / 1048576
        actual_name = os.path.basename(final_dest)
        _update_job(job_id, status='ok', phase='done', progress=100.0,
                    filename=actual_name, size=size,
                    message=f'转码完成: {actual_name} ({size_mb:.1f}MB)')

    except Exception as e:
        _update_job(job_id, status='error', phase='error', error=f'任务异常: {e}')


def start_server(output_dir: str, bg_dir: str) -> int:
    """启动本地 HTTP 服务器，返回端口号"""
    global _http_server

    class BoundHandler(_DownloadHandler):
        pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), BoundHandler)
    server.output_dir = output_dir
    server.bg_dir = bg_dir
    server.allow_reuse_address = True
    port = server.server_address[1]

    _http_server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port


def shutdown():
    """关闭本地 HTTP 服务器"""
    global _http_server
    if _http_server:
        srv = _http_server
        _http_server = None
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass


def is_server_running() -> bool:
    """检查服务器是否正在运行"""
    return _http_server is not None


def format_file_time(filepath: str) -> str:
    """格式化文件修改时间为可读字符串"""
    try:
        mt = os.path.getmtime(filepath)
        dt = datetime.fromtimestamp(mt)
        now = datetime.now()
        delta = now - dt
        if delta.total_seconds() < 60:
            return '刚刚'
        elif delta.total_seconds() < 3600:
            return f'{int(delta.total_seconds() // 60)} 分钟前'
        elif delta.total_seconds() < 86400:
            return f'{int(delta.total_seconds() // 3600)} 小时前'
        elif delta.days < 7:
            return f'{delta.days} 天前'
        else:
            return dt.strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '未知'


# ═══════════════════════════════════════════════════════════════════
#  Parsing
# ═══════════════════════════════════════════════════════════════════

def get_extension(url: str) -> str:
    try:
        path = urlparse(url).path
        _, ext = os.path.splitext(path)
        return ext.lstrip('.').lower()
    except Exception:
        parts = url.split('.')
        if len(parts) > 1:
            return parts[-1].split('?')[0].split('#')[0].lower()
        return ''


def clean_url(raw: str) -> str:
    while raw and raw[-1] in '.,;:!?\'")]}>':
        if raw[-1] in ')])}':
            opener = {'(': ')', '[': ']', '{': '}', '<': '>'}
            match = {v: k for k, v in opener.items()}.get(raw[-1])
            if match and match in raw[:-1]:
                break
        raw = raw[:-1]

    MEDIA_EXTS = ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp',
                  'webm', 'mp4', 'avi', 'mkv',
                  'json', 'svg', 'ico', 'mp3', 'ogg', 'wav')
    for ext in MEDIA_EXTS:
        marker = '.' + ext
        idx = raw.find(marker)
        if idx != -1:
            end = idx + len(marker)
            remainder = raw[end:]
            if remainder and not remainder[0] in ('?', '#', '/', '&', '=', '%'):
                raw = raw[:end]
                break
    return raw


def parse_data1(path: str) -> tuple[list[str], int]:
    """解析 data_1 文件，返回 (media_urls, filtered_count)"""
    with open(path, 'rb') as f:
        raw = f.read()
    text = raw.decode('latin-1')

    matches = URL_REGEX.findall(text)
    seen = set()
    media_urls = []
    filtered_count = 0

    ALL_KNOWN_EXTS = IMAGE_EXTS | VIDEO_EXTS | {
        'json', 'xml', 'html', 'css', 'js', 'txt',
        'yaml', 'yml', 'ini', 'cfg', 'log', 'csv',
        'zip', 'rar', '7z', 'tar', 'gz',
        'exe', 'dll', 'apk', 'dmg',
        'pdf', 'doc', 'xls', 'ppt',
        'svg', 'ico', 'ttf', 'woff', 'woff2',
        'm4a', 'mp3', 'ogg', 'wav', 'flac',
    }

    CDN_MEDIA_PATTERNS = [
        re.compile(r'/launcher-public/\d{4}/\d{2}/\d{2}/[0-9a-f]{20,}'),
        re.compile(r'/ptolemaios/.*[0-9a-f]{20,}'),
    ]

    for m in matches:
        u = clean_url(m)
        if u in seen:
            continue
        seen.add(u)
        ext = get_extension(u)

        if ext and ext in ALL_KNOWN_EXTS:
            media_urls.append(u)
            continue

        parsed_path = urlparse(u).path
        if any(p.search(parsed_path) for p in CDN_MEDIA_PATTERNS):
            media_urls.append(u)
            continue

        filtered_count += 1

    return media_urls, filtered_count


# ═══════════════════════════════════════════════════════════════════
#  HTML Generation
# ═══════════════════════════════════════════════════════════════════

def generate_html(urls: list[str], source_file: str, hint: str = '',
                  server_port: int = 0) -> str:
    """生成 OMG 风格 HTML 页面"""
    # 分类统计
    img_urls = [u for u in urls if get_extension(u) in IMAGE_EXTS]
    vid_urls = [u for u in urls if get_extension(u) in VIDEO_EXTS]
    other_urls = [u for u in urls if u not in img_urls and u not in vid_urls]

    data_json = json.dumps(urls, ensure_ascii=False)
    file_time_str = format_file_time(source_file)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HoYoMediaExtractor - {os.path.basename(source_file)}</title>
<style>
:root {{
    --bg: #0a0a0c;
    --card: rgba(28, 28, 30, 0.72);
    --card-border: rgba(255, 255, 255, 0.08);
    --text: #f5f5f7;
    --text2: #8e8e93;
    --accent: #0A84FF;
    --accent-hover: #409CFF;
    --danger: #FF3B30;
    --success: #30D158;
    --radius: 14px;
    --radius-sm: 10px;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
}}

/* ── 背景 ── */
.bg-layer {{
    position: fixed; inset: 0; z-index: -1;
    background:
        radial-gradient(ellipse at 20% 0%, rgba(10, 132, 255, 0.08) 0%, transparent 60%),
        radial-gradient(ellipse at 80% 100%, rgba(94, 92, 230, 0.06) 0%, transparent 60%),
        var(--bg);
}}

/* ── 容器 ── */
.container {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 24px 20px 60px;
}}

/* ── 标题栏 ── */
.header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;
    padding: 16px 20px;
    background: var(--card);
    backdrop-filter: blur(40px) saturate(180%);
    -webkit-backdrop-filter: blur(40px) saturate(180%);
    border: 1px solid var(--card-border);
    border-radius: var(--radius);
}}
.header h1 {{
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.3px;
}}
.header .meta {{
    font-size: 13px;
    color: var(--text2);
}}

/* ── 提示横幅 ── */
.hint-banner {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 18px;
    margin-bottom: 16px;
    font-size: 13px;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.08);
    border: 1px solid rgba(251, 191, 36, 0.2);
    border-radius: var(--radius-sm);
    line-height: 1.5;
}}
.hint-banner svg {{ color: #fbbf24; flex-shrink: 0; }}

/* ── 筛选栏 ── */
.filters {{
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    align-items: center;
}}
.filter-btn {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    font-size: 13px;
    font-weight: 500;
    color: var(--text2);
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.2s;
    user-select: none;
}}
.filter-btn:hover {{ background: rgba(255,255,255,0.1); color: var(--text); }}
.filter-btn.active {{
    background: rgba(10, 132, 255, 0.15);
    color: var(--accent);
    border-color: rgba(10, 132, 255, 0.3);
}}
.filter-btn .count {{
    font-size: 11px;
    background: rgba(255,255,255,0.1);
    padding: 1px 6px;
    border-radius: 8px;
}}
.filter-btn.active .count {{
    background: rgba(10, 132, 255, 0.2);
}}

.search-box {{
    flex: 1;
    min-width: 200px;
    padding: 7px 14px;
    font-size: 13px;
    color: var(--text);
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 20px;
    outline: none;
    transition: border-color 0.2s;
}}
.search-box::placeholder {{ color: var(--text2); opacity: 0.6; }}
.search-box:focus {{ border-color: var(--accent); }}

/* ── 网格 ── */
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
}}

.card {{
    background: var(--card);
    backdrop-filter: blur(20px) saturate(150%);
    -webkit-backdrop-filter: blur(20px) saturate(150%);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-sm);
    overflow: hidden;
    min-width: 0;
    transition: transform 0.2s, box-shadow 0.2s;
}}
.card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.3);
}}

/* 预览区 */
.card .preview {{
    position: relative;
    width: 100%;
    aspect-ratio: 16/10;
    background: linear-gradient(135deg, rgba(28,28,30,0.9), rgba(18,18,20,0.95));
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    cursor: pointer;
}}
.card .preview img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
}}
.card .preview video {{
    width: 100%;
    height: 100%;
    object-fit: cover;
}}
.card .preview .media-wrap {{
    position: absolute;
    inset: 0;
    z-index: 0;
}}
.card .preview .placeholder {{
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    color: var(--text2);
    font-size: 11px;
    opacity: 0.7;
}}
.card .preview .placeholder svg {{
    width: 32px;
    height: 32px;
    opacity: 0.35;
}}

/* 信息区 */
.card .info {{
    padding: 10px 12px;
    min-width: 0;
}}
.card .info .url {{
    font-size: 11px;
    color: var(--text2);
    line-height: 1.4;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
}}
.card .info .url-full {{
    font-size: 10px;
    color: var(--text2);
    opacity: 0.6;
    line-height: 1.3;
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
}}
.card .info .actions {{
    display: flex;
    gap: 6px;
    margin-top: 8px;
}}
.card .info .actions button {{
    flex: 1;
    min-width: 0;
    padding: 5px 0;
    font-size: 11px;
    font-weight: 500;
    color: var(--accent);
    background: rgba(10, 132, 255, 0.1);
    border: 1px solid rgba(10, 132, 255, 0.15);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
}}
.card .info .actions button:hover {{
    background: rgba(10, 132, 255, 0.2);
}}
.card .info .actions button.copied {{
    color: var(--success);
    background: rgba(48, 209, 88, 0.1);
    border-color: rgba(48, 209, 88, 0.2);
}}
.card .info .actions .dl-btn {{
    flex: 0 0 32px;
    width: 32px;
    min-width: 32px;
    padding: 5px 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--accent);
    background: rgba(10, 132, 255, 0.1);
    border: 1px solid rgba(10, 132, 255, 0.15);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s;
}}
.card .info .actions .dl-btn:hover {{
    background: rgba(10, 132, 255, 0.2);
}}
.card .info .actions .dl-btn svg {{
    width: 14px;
    height: 14px;
}}

/* 类型标签 */
.tag {{
    position: absolute;
    top: 8px;
    left: 8px;
    z-index: 1;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-radius: 6px;
    backdrop-filter: blur(10px);
    pointer-events: none;
}}
.tag.img {{ background: rgba(10, 132, 255, 0.7); color: #fff; }}
.tag.vid {{ background: rgba(255, 55, 48, 0.7); color: #fff; }}
.tag.other {{ background: rgba(142, 142, 147, 0.5); color: #fff; }}

/* ── 分页 ── */
.pagination {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 16px 0;
    flex-wrap: wrap;
}}
.pagination button {{
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 500;
    color: var(--text);
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.2s;
    min-width: 44px;
}}
.pagination button:hover:not(:disabled) {{
    background: rgba(255,255,255,0.14);
}}
.pagination button:disabled {{
    opacity: 0.3;
    cursor: not-allowed;
}}
.pagination .page-info {{
    font-size: 13px;
    color: var(--text2);
    padding: 0 8px;
}}
.pagination .page-jump {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: 8px;
}}
.pagination .page-jump input {{
    width: 60px;
    padding: 6px 10px;
    font-size: 13px;
    color: var(--text);
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    outline: none;
    text-align: center;
    transition: border-color 0.2s;
}}
.pagination .page-jump input:focus {{
    border-color: var(--accent);
}}
.pagination .page-jump input::placeholder {{
    color: var(--text2);
    opacity: 0.5;
}}
.pagination .page-jump button {{
    padding: 6px 12px;
    font-size: 12px;
    min-width: 36px;
}}

/* ── Toast ── */
.toast {{
    position: fixed;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%) translateY(20px);
    padding: 10px 24px;
    font-size: 13px;
    font-weight: 500;
    color: #fff;
    background: rgba(28, 28, 30, 0.9);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    opacity: 0;
    transition: all 0.3s;
    pointer-events: none;
    z-index: 999;
}}
.toast.show {{
    opacity: 1;
    transform: translateX(-50%) translateY(0);
}}

/* ── 任务进度面板（全局固定右下） ── */
.jobs-panel {{
    position: fixed;
    right: 20px;
    bottom: 20px;
    width: 320px;
    max-height: 360px;
    overflow-y: auto;
    z-index: 998;
    background: rgba(28, 28, 30, 0.92);
    backdrop-filter: blur(24px) saturate(150%);
    -webkit-backdrop-filter: blur(24px) saturate(150%);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 14px;
    box-shadow: 0 10px 40px rgba(0,0,0,0.4);
    padding: 12px;
    color: #fff;
}}
.jobs-title {{
    font-size: 13px;
    font-weight: 700;
    margin-bottom: 10px;
    padding-left: 4px;
    color: rgba(255,255,255,0.9);
    letter-spacing: 0.3px;
}}
.jobs-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
}}
.job-item {{
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 10px;
    font-size: 12px;
}}
.job-item .job-head {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
}}
.job-item .job-mode {{
    padding: 2px 8px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.3px;
    background: rgba(10, 132, 255, 0.25);
    color: #64B5F6;
}}
.job-item .job-mode.mp4 {{
    background: rgba(48, 209, 88, 0.2);
    color: #30D158;
}}
.job-item .job-status {{
    font-size: 11px;
    color: rgba(255,255,255,0.6);
}}
.job-item .job-status.ok {{ color: #30D158; }}
.job-item .job-status.error {{ color: #FF453A; }}
.job-item .job-bar {{
    height: 4px;
    background: rgba(255,255,255,0.1);
    border-radius: 4px;
    overflow: hidden;
    margin: 8px 0 6px;
}}
.job-item .job-bar-fill {{
    height: 100%;
    background: linear-gradient(90deg, #0A84FF, #5AC8FA);
    border-radius: 4px;
    transition: width 0.2s;
}}
.job-item .job-bar-fill.error {{
    background: linear-gradient(90deg, #FF453A, #FF6B5C);
}}
.job-item .job-bar-fill.ok {{
    background: linear-gradient(90deg, #30D158, #34C759);
}}
.job-item .job-msg {{
    font-size: 11px;
    color: rgba(255,255,255,0.55);
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}}

/* ── 下载下拉菜单 ── */
.dl-menu {{
    position: fixed;
    z-index: 1002;
    min-width: 230px;
    padding: 6px;
    background: rgba(28, 28, 30, 0.94);
    backdrop-filter: blur(24px) saturate(160%);
    -webkit-backdrop-filter: blur(24px) saturate(160%);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px;
    box-shadow: 0 12px 48px rgba(0,0,0,0.45);
    display: flex;
    flex-direction: column;
    gap: 2px;
}}
.dl-menu-item {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.15s;
    color: #fff;
}}
.dl-menu-item:hover {{
    background: rgba(255,255,255,0.1);
}}
.dl-menu-item.disabled {{
    opacity: 0.35;
    cursor: not-allowed;
    filter: grayscale(0.4);
}}
.dl-menu-item.disabled:hover {{
    background: transparent;
}}
.dl-menu-icon {{
    width: 28px;
    height: 28px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255,255,255,0.08);
    border-radius: 8px;
    font-size: 14px;
}}
.dl-menu-text {{
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
}}
.dl-menu-text b {{
    font-size: 12px;
    font-weight: 600;
    color: #fff;
}}
.dl-menu-text span {{
    font-size: 10px;
    color: rgba(255,255,255,0.55);
}}

/* ── 灯箱 ── */
.lightbox {{
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: none;
    align-items: center;
    justify-content: center;
    background: rgba(0,0,0,0.85);
    backdrop-filter: blur(30px);
    cursor: zoom-out;
}}
.lightbox.open {{ display: flex; }}
.lightbox img, .lightbox video {{
    max-width: 90vw;
    max-height: 90vh;
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}}

/* ── 空状态 ── */
.empty {{
    text-align: center;
    padding: 60px 20px;
    color: var(--text2);
}}
.empty svg {{ width: 48px; height: 48px; opacity: 0.3; margin-bottom: 12px; }}
.empty p {{ font-size: 14px; }}

/* ── 加载 ── */
.card .preview .loading {{
    width: 24px; height: 24px;
    border: 2px solid rgba(255,255,255,0.1);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<div class="bg-layer"></div>

<div class="container">
    <div class="header">
        <h1>HoYoMediaExtractor</h1>
        <span class="meta">{os.path.basename(source_file)} &middot; {len(urls)} 个媒体链接 &middot; {file_time_str}</span>
    </div>

    {('<div class="hint-banner"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;flex-shrink:0"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg><span>' + hint + '</span></div>') if hint else ''}

    <div class="filters">
        <div class="filter-btn active" data-filter="all" onclick="setFilter('all')">
            全部 <span class="count">{len(urls)}</span>
        </div>
        <div class="filter-btn" data-filter="image" onclick="setFilter('image')">
            图片 <span class="count">{len(img_urls)}</span>
        </div>
        <div class="filter-btn" data-filter="video" onclick="setFilter('video')">
            视频 <span class="count">{len(vid_urls)}</span>
        </div>
        <div class="filter-btn" data-filter="other" onclick="setFilter('other')">
            其他 <span class="count">{len(other_urls)}</span>
        </div>
        <input class="search-box" type="text" placeholder="搜索 URL..." oninput="onSearch(this.value)">
    </div>

    <div class="grid" id="grid"></div>
    <div class="empty" id="empty" style="display:none">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
        </svg>
        <p>没有匹配的结果</p>
    </div>
    <div class="pagination" id="pagination"></div>
</div>

<div class="toast" id="toast"></div>
<div class="lightbox" id="lightbox" onclick="closeLightbox()"></div>

<!-- 下载/转码全局进度面板 -->
<div class="jobs-panel" id="jobsPanel" style="display:none;">
    <div class="jobs-title">下载任务</div>
    <div class="jobs-list" id="jobsList"></div>
</div>

<!-- 下载按钮弹出菜单 -->
<div class="dl-menu" id="dlMenu" style="display:none;">
    <div class="dl-menu-item" data-mode="webm">
        <div class="dl-menu-icon">⬇</div>
        <div class="dl-menu-text"><b>以 webm 下载</b><span>保持原编码，播放 CPU 较高</span></div>
    </div>
    <div class="dl-menu-item" data-mode="mp4" id="dlMenuMp4">
        <div class="dl-menu-icon">🎞</div>
        <div class="dl-menu-text"><b>下载并转码为 mp4</b><span>H.264 硬解，播放 CPU 更低</span></div>
    </div>
</div>

<script>
const ALL_URLS = {data_json};
const IMAGE_EXTS = new Set({json.dumps(list(IMAGE_EXTS))});
const VIDEO_EXTS = new Set({json.dumps(list(VIDEO_EXTS))});
const PER_PAGE = {ITEMS_PER_PAGE};

let currentFilter = 'all';
let searchTerm = '';
let currentPage = 1;
let filtered = [];
let pageItems = [];

// 从URL中提取日期 (格式: /YYYY/MM/DD/ 或 /YYYY-MM-DD/)
function extractDate(url) {{
    try {{
        // 匹配 /2025/03/10/ 格式
        const m1 = url.match(/\\/(\\d{{4}})\\/(\\d{{2}})\\/(\\d{{2}})\\//);
        if (m1) {{
            const d = new Date(parseInt(m1[1]), parseInt(m1[2]) - 1, parseInt(m1[3]));
            if (!isNaN(d.getTime())) return d;
        }}
        // 匹配 /2025-03-10/ 或 /2025-03-10_ 格式
        const m2 = url.match(/\\/(\\d{{4}})-(\\d{{2}})-(\\d{{2}})[\\/_]/);
        if (m2) {{
            const d = new Date(parseInt(m2[1]), parseInt(m2[2]) - 1, parseInt(m2[3]));
            if (!isNaN(d.getTime())) return d;
        }}
    }} catch {{}}
    return null;
}}

// 按日期排序：有日期的在前（新→旧），无日期的排末尾
function sortByDate(urls) {{
    const withDate = [];
    const noDate = [];
    for (const url of urls) {{
        const d = extractDate(url);
        if (d) {{
            withDate.push({{ url, date: d }});
        }} else {{
            noDate.push(url);
        }}
    }}
    withDate.sort((a, b) => b.date - a.date);
    return withDate.map(x => x.url).concat(noDate);
}}

// 初始化时对 ALL_URLS 排序
ALL_URLS.sort((a, b) => {{
    const da = extractDate(a);
    const db = extractDate(b);
    if (da && db) return db - da;
    if (da) return -1;
    if (db) return 1;
    return 0;
}});

function getExt(url) {{
    try {{
        const path = new URL(url).pathname;
        const dot = path.lastIndexOf('.');
        return dot >= 0 ? path.slice(dot + 1).toLowerCase() : '';
    }} catch {{ return ''; }}
}}

function getType(url) {{
    const ext = getExt(url);
    if (IMAGE_EXTS.has(ext)) return 'image';
    if (VIDEO_EXTS.has(ext)) return 'video';
    return 'other';
}}

function shortUrl(url) {{
    try {{
        const u = new URL(url);
        const host = u.hostname;
        const pathParts = u.pathname.split('/').filter(Boolean);
        const file = pathParts.length > 0 ? pathParts[pathParts.length - 1] : '';
        if (file && file.length < 50) {{
            return host + '/.../' + file;
        }}
        return host + u.pathname.substring(0, 40);
    }} catch {{
        return url.length > 60 ? url.substring(0, 60) + '...' : url;
    }}
}}

function escAttr(s) {{
    return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function applyFilters() {{
    filtered = ALL_URLS.filter(u => {{
        if (currentFilter !== 'all' && getType(u) !== currentFilter) return false;
        if (searchTerm && !u.toLowerCase().includes(searchTerm.toLowerCase())) return false;
        return true;
    }});
    currentPage = 1;
    render();
}}

function setFilter(f) {{
    currentFilter = f;
    document.querySelectorAll('.filter-btn').forEach(b => {{
        b.classList.toggle('active', b.dataset.filter === f);
    }});
    applyFilters();
}}

function onSearch(val) {{
    searchTerm = val;
    applyFilters();
}}

function render() {{
    const grid = document.getElementById('grid');
    const empty = document.getElementById('empty');
    const totalPages = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
    const start = (currentPage - 1) * PER_PAGE;
    pageItems = filtered.slice(start, start + PER_PAGE);

    if (pageItems.length === 0) {{
        grid.innerHTML = '';
        empty.style.display = '';
    }} else {{
        empty.style.display = 'none';
        grid.innerHTML = pageItems.map((url, idx) => {{
            const type = getType(url);
            const ext = getExt(url);
            let preview = '';
            if (type === 'image') {{
                preview = `<img src="${{escAttr(url)}}" loading="lazy" data-idx="${{idx}}" data-action="lightbox">`;
            }} else if (type === 'video') {{
                preview = `<div class="media-wrap"><video src="${{escAttr(url)}}" preload="metadata" muted data-idx="${{idx}}" data-action="lightbox"></video></div>`;
            }} else {{
                preview = `<div class="placeholder"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>${{ext.toUpperCase() || 'FILE'}}</div>`;
            }}
            const tagClass = type === 'image' ? 'img' : type === 'video' ? 'vid' : 'other';
            const tagText = type === 'image' ? (ext.toUpperCase() || 'IMG') : type === 'video' ? (ext.toUpperCase() || 'VID') : (ext.toUpperCase() || 'OTHER');
            const displayUrl = shortUrl(url);
            return `<div class="card">
                <div class="preview">${{preview}}<span class="tag ${{tagClass}}">${{tagText}}</span></div>
                <div class="info">
                    <div class="url" title="${{escAttr(url)}}">${{escAttr(displayUrl)}}</div>
                    <div class="url-full" title="${{escAttr(url)}}">${{escAttr(url)}}</div>
                    <div class="actions">
                        <button data-idx="${{idx}}" data-action="copy">复制</button>
                        <button data-idx="${{idx}}" data-action="open">打开</button>
                        <button class="dl-btn" data-idx="${{idx}}" data-action="download" title="下载文件">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                        </button>
                    </div>
                </div>
            </div>`;
        }}).join('');
    }}

    // 分页
    const pag = document.getElementById('pagination');
    if (totalPages <= 1) {{
        pag.innerHTML = '';
    }} else {{
        pag.innerHTML = `
            <button ${{currentPage <= 1 ? 'disabled' : ''}} onclick="goPage(1)" title="首页">⟨⟨</button>
            <button ${{currentPage <= 1 ? 'disabled' : ''}} onclick="goPage(${{currentPage - 1}})">上一页</button>
            <span class="page-info">第 ${{currentPage}} / ${{totalPages}} 页</span>
            <button ${{currentPage >= totalPages ? 'disabled' : ''}} onclick="goPage(${{currentPage + 1}})">下一页</button>
            <button ${{currentPage >= totalPages ? 'disabled' : ''}} onclick="goPage(${{totalPages}})" title="尾页">⟩⟩</button>
            <div class="page-jump">
                <input type="number" id="pageJumpInput" min="1" max="${{totalPages}}" placeholder="1-${{totalPages}}" onkeydown="if(event.key==='Enter')jumpToPage()">
                <button onclick="jumpToPage()">跳转</button>
            </div>
        `;
    }}
}}

function goPage(p) {{
    const totalPages = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
    currentPage = Math.max(1, Math.min(p, totalPages));
    render();
    window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}

function jumpToPage() {{
    const input = document.getElementById('pageJumpInput');
    if (!input) return;
    const totalPages = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
    let val = parseInt(input.value, 10);
    if (isNaN(val) || val < 1) val = 1;
    if (val > totalPages) val = totalPages;
    input.value = val;
    goPage(val);
}}

// 事件委托：grid 内所有点击
document.getElementById('grid').addEventListener('click', function(e) {{
    const target = e.target.closest('[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    const idx = parseInt(target.dataset.idx, 10);
    const url = pageItems[idx];
    if (!url) return;

    if (action === 'copy') {{
        navigator.clipboard.writeText(url).then(() => {{
            target.textContent = '已复制';
            target.classList.add('copied');
            showToast('链接已复制到剪贴板');
            setTimeout(() => {{
                target.textContent = '复制';
                target.classList.remove('copied');
            }}, 1500);
        }});
    }} else if (action === 'open') {{
        window.open(url, '_blank');
    }} else if (action === 'download') {{
        const type = getType(url);
        if (type !== 'video') {{
            // 图片/其他：直接走 webm 异步下载（无菜单）
            startJob(url, 'webm', target);
            return;
        }}
        // 视频：显示菜单，选项 = 以 webm 下载 / 下载并转码为 mp4
        openDlMenu(target, url);
    }} else if (action === 'lightbox') {{
        const type = getType(url);
        openLightbox(url, type);
    }}
}});

// 图片加载失败处理
document.getElementById('grid').addEventListener('error', function(e) {{
    if (e.target.tagName === 'IMG') {{
        const preview = e.target.closest('.preview');
        if (preview) {{
            e.target.remove();
            const ph = document.createElement('div');
            ph.className = 'placeholder';
            ph.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>加载失败';
            preview.insertBefore(ph, preview.firstChild);
        }}
    }}
}}, true);

// ── 下载/转码：任务引擎 + 菜单 + 进度渲染 ──
const API_BASE = 'http://127.0.0.1:{server_port}';
let FFMPEG_AVAILABLE = false;
const ACTIVE_JOBS = new Map();  // jobId -> timer handle
const SERVER = '{server_port}';

// 启动任务（mode: webm/mp4），按钮视觉上置灰防止重复点击
async function startJob(url, mode, btn) {{
    // mp4 模式需要 ffmpeg；理论上菜单已做灰度，但此处再兜底
    if (mode === 'mp4' && !FFMPEG_AVAILABLE) {{
        showToast('未检测到 FFmpeg，无法转码 MP4。请安装 ffmpeg 并加入系统 PATH。');
        return;
    }}
    if (btn) {{
        btn.style.opacity = '0.45';
        btn.style.pointerEvents = 'none';
    }}
    try {{
        const resp = await fetch(`${{API_BASE}}/download?url=${{encodeURIComponent(url)}}&mode=${{mode}}`);
        const data = await resp.json();
        if (data.status !== 'ok') {{
            showToast('任务启动失败: ' + (data.error || '未知错误'));
            return;
        }}
        const jobId = data.job_id;
        showToast(mode === 'mp4' ? '开始下载并转码 MP4...' : '开始下载 webm...');
        renderNewJob(jobId, mode);
        // 每 500ms 轮询一次进度（下载 50% + 转码 50%，进度足够平滑）
        const timer = setInterval(() => pollJob(jobId, mode, btn, timer), 500);
        ACTIVE_JOBS.set(jobId, timer);
    }} catch (e) {{
        showToast('任务启动失败: ' + e.message);
    }} finally {{
        if (btn) {{
            btn.style.opacity = '';
            btn.style.pointerEvents = '';
        }}
    }}
}}

async function pollJob(jobId, mode, btn, timer) {{
    try {{
        const resp = await fetch(`${{API_BASE}}/job_progress?id=${{jobId}}`);
        const data = await resp.json();
        if (data.status !== 'ok') {{
            return;
        }}
        updateJobCard(data);
        if (data.job && data.job.status === 'ok') {{
            clearInterval(timer);
            ACTIVE_JOBS.delete(jobId);
            const info = data.job;
            const mb = ((info.size || 0) / 1048576).toFixed(1);
            showToast(`${{info.message || '任务完成'}}${{mb ? ` (${{mb}}MB)` : ''}}`);
            return;
        }}
        if (data.job && data.job.status === 'error') {{
            clearInterval(timer);
            ACTIVE_JOBS.delete(jobId);
            showToast('任务失败: ' + (data.job.error || '未知错误'));
        }}
    }} catch (e) {{
        // 网络波动，继续下次轮询
    }}
}}

function renderNewJob(jobId, mode) {{
    const panel = document.getElementById('jobsPanel');
    const list = document.getElementById('jobsList');
    panel.style.display = '';
    const el = document.createElement('div');
    el.className = 'job-item';
    el.dataset.jobId = jobId;
    const modeLabel = mode === 'mp4' ? 'MP4 转码' : 'WEBm 下载';
    const modeClass = mode === 'mp4' ? 'mp4' : '';
    el.innerHTML = `
        <div class="job-head">
            <span class="job-mode ${{modeClass}}">${{modeLabel}}</span>
            <span class="job-status">准备中</span>
        </div>
        <div class="job-bar"><div class="job-bar-fill" style="width:0%"></div></div>
        <div class="job-msg">—</div>
    `;
    list.prepend(el);
}}

function updateJobCard(payload) {{
    const info = payload.job || payload;
    if (!info || !info.job_id) return;
    const el = document.querySelector(`.job-item[data-job-id="${{info.job_id}}"]`);
    if (!el) return;
    const statusEl = el.querySelector('.job-status');
    const barEl = el.querySelector('.job-bar-fill');
    const msgEl = el.querySelector('.job-msg');
    const progress = Math.max(0, Math.min(100, Number(info.progress) || 0));
    barEl.style.width = progress.toFixed(1) + '%';
    barEl.classList.remove('ok', 'error');
    if (info.status === 'ok') {{
        statusEl.textContent = '已完成';
        statusEl.className = 'job-status ok';
        barEl.classList.add('ok');
    }} else if (info.status === 'error') {{
        statusEl.textContent = '失败';
        statusEl.className = 'job-status error';
        barEl.classList.add('error');
    }} else if (info.phase === 'transcode') {{
        statusEl.textContent = '转码中';
    }} else if (info.phase === 'download') {{
        statusEl.textContent = '下载中';
    }} else {{
        statusEl.textContent = info.phase || '进行中';
    }}
    if (info.message) msgEl.textContent = info.message;
    if (info.filename && info.status === 'ok') {{
        msgEl.textContent = '已保存: ' + info.filename;
    }}
    if (info.error && info.status === 'error') {{
        msgEl.textContent = info.error;
    }}
}}

// ── 下载菜单定位/显示/关闭 ──
let DL_MENU_CTX = null; // ${{url, btn}}

function openDlMenu(btn, url) {{
    const menu = document.getElementById('dlMenu');
    const mp4Item = document.getElementById('dlMenuMp4');
    // mp4 选项按 ffmpeg 可用性置灰
    if (!FFMPEG_AVAILABLE) {{
        mp4Item.classList.add('disabled');
        mp4Item.querySelector('.dl-menu-text span').textContent = '请先安装 FFmpeg 并加入系统 PATH';
    }} else {{
        mp4Item.classList.remove('disabled');
        mp4Item.querySelector('.dl-menu-text span').textContent = 'H.264 硬解，播放 CPU 更低';
    }}
    const rect = btn.getBoundingClientRect();
    let left = rect.left;
    let top = rect.bottom + 6;
    menu.style.left = '0px';
    menu.style.top = '0px';
    menu.style.display = '';
    const mw = menu.offsetWidth;
    const mh = menu.offsetHeight;
    // 右边界与下边界自动回退
    if (left + mw > window.innerWidth - 10) {{
        left = window.innerWidth - mw - 10;
    }}
    if (top + mh > window.innerHeight - 10) {{
        top = rect.top - mh - 6;
    }}
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    DL_MENU_CTX = {{ url, btn }};
}}

function hideDlMenu() {{
    document.getElementById('dlMenu').style.display = 'none';
    DL_MENU_CTX = null;
}}

// 菜单项点击：选择 mode 后启动任务
document.getElementById('dlMenu').addEventListener('click', function(e) {{
    const item = e.target.closest('.dl-menu-item');
    if (!item || !DL_MENU_CTX) return;
    if (item.classList.contains('disabled')) {{
        showToast('请先安装 FFmpeg 并加入系统 PATH');
        hideDlMenu();
        return;
    }}
    const mode = item.dataset.mode;
    const {{ url, btn }} = DL_MENU_CTX;
    hideDlMenu();
    startJob(url, mode, btn);
}});

// 点击其他位置 / ESC 关闭菜单
document.addEventListener('click', function(e) {{
    const menu = document.getElementById('dlMenu');
    if (menu.style.display === 'none') return;
    // 若点击的是下载按钮本身（委托触发 openDlMenu），不立即关闭
    const dlBtn = e.target.closest('.dl-btn, [data-action="download"]');
    if (dlBtn) return;
    if (!menu.contains(e.target)) hideDlMenu();
}});
document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') hideDlMenu();
}});

// ── 启动：检查 ffmpeg 可用性，写入全局开关 ──
(async function initFFmpegFlag() {{
    try {{
        const r = await fetch(`${{API_BASE}}/ffmpeg_check`);
        const d = await r.json();
        FFMPEG_AVAILABLE = !!(d && d.available);
    }} catch {{
        FFMPEG_AVAILABLE = false;
    }}
}})();

function openLightbox(url, type) {{
    const lb = document.getElementById('lightbox');
    if (type === 'video') {{
        lb.innerHTML = `<video src="${{escAttr(url)}}" controls autoplay style="max-width:90vw;max-height:90vh;border-radius:12px"></video>`;
    }} else {{
        lb.innerHTML = `<img src="${{escAttr(url)}}" style="max-width:90vw;max-height:90vh;border-radius:12px">`;
    }}
    lb.classList.add('open');
}}

function closeLightbox() {{
    const lb = document.getElementById('lightbox');
    lb.innerHTML = '';
    lb.classList.remove('open');
}}

function showToast(msg) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2000);
}}

// ESC 关闭灯箱
document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') closeLightbox();
}});

// 初始化
applyFilters();
</script>
</body>
</html>'''
    return html


# ═══════════════════════════════════════════════════════════════════
#  Public API (供 OMG 设置页调用)
# ═══════════════════════════════════════════════════════════════════

def launch(bg_dir: str) -> tuple:
    """
    启动媒体提取 + 本地服务器。
    bg_dir: 下载目标目录（OMG\\bin\\bg）
    返回: (port: int, error: str)
        成功时 error 为空，失败时 port 为 0。
    """
    # 若服务器已在运行，先关闭
    shutdown()

    # 自动查找 data_1
    data1_path, source_label = find_data1_auto()
    if not data1_path:
        return 0, '未找到 data_1 缓存文件，请运行米哈游启动器后重试'

    # 解析
    urls, filtered_count = parse_data1(data1_path)
    n_img = sum(1 for u in urls if get_extension(u) in IMAGE_EXTS)

    if not urls:
        return 0, '未找到任何媒体链接，请运行米哈游启动器浏览页面以获取最新数据'

    # 构建提示
    launcher_running = check_launcher_running()
    hint = ''
    if n_img == 0 and not launcher_running:
        hint = '未找到图片资源。请运行米哈游启动器浏览页面以获取最新背景图。'
    elif n_img == 0 and launcher_running:
        hint = '未找到图片资源。启动器正在运行中，请等待加载完成后关闭启动器再重新提取。'

    # 输出目录（HTML 放在 bg_dir 的上级临时位置，下载直接进 bg_dir）
    output_dir = os.path.join(tempfile.gettempdir(), "HoYoMediaExtractor")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(bg_dir, exist_ok=True)

    # 启动服务器
    port = start_server(output_dir, bg_dir)

    # 生成 HTML
    html_content = generate_html(urls, data1_path, hint=hint, server_port=port)
    html_path = os.path.join(output_dir, "index.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    # 打开浏览器
    webbrowser.open(f"http://127.0.0.1:{port}/")
    return port, ''


if __name__ == '__main__':
    # 独立运行时的回退入口
    import tempfile as _tf
    _port, _err = launch(os.path.join(_tf.gettempdir(), 'HoYoMediaExtractor', 'bg'))
    if _err:
        print(f'[!] {_err}')
    else:
        print(f'服务已启动: http://127.0.0.1:{_port}')
        print('按 Ctrl+C 退出')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            shutdown()
            print('\n已退出。')
