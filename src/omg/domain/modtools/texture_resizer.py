"""
texture_resizer.py — Port of nahida-desktop 2.54.1 Texture Resizer core logic.

Provides DDS metadata scanning, valid downscale-candidate computation,
and batch execution hooks (actual DDS decode/resize/re-encode is delegated
to Rust in the future; this module provides pure-Python fast scanning,
settings, preview-sizing and backup-on-write utilities).

Supported detection
-------------------
  * Magic + header-size validity
  * Width / Height / Depth / MipmapCount / ArraySize (cubemap = 6)
  * Block-compressed format identification:
      Legacy FourCC:  DXT1→BC1, DXT3→BC2, DXT5→BC3,
                      ATI1/BC4U→BC4, ATI2/BC5U→BC5
      DX10 extension: DXGI_FORMAT enum → BC1~BC7 / R8G8B8A8 / B8G8R8A8 etc.
      RGB bitmask:     BGRA8888 / RGBA8888 / BGRX8888 (uncompressed 32bpp)
  * sRGB vs Linear vs Unknown color-space classification.

Candidate-size algorithm (identical to nahida TS/Rust):
  1. GCD(w, h) → (ratioW, ratioH) reduced aspect ratio.
  2. max_scale = floor(min(w / (ratioW*1024), h / (ratioH*1024)))
  3. Generate sizes (ratioW*1024*s, ratioH*1024*s) for s ∈ [1, max_scale].
  4. Filter out sizes >= the source size.
  5. Pick largest size whose w <= bound_w and h <= bound_h (if custom mode),
     or pick the size closest to the target percentage (percent mode).
"""

import os
import math
import struct
import shutil
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDS constants
# ---------------------------------------------------------------------------

DDS_MAGIC = b"DDS "
DDS_HEADER_SIZE = 124
DDS_HEADER_DXT10_SIZE = 20
FOURCC_DX10 = b"DX10"

DDPF_ALPHAPIXELS = 0x1
DDPF_FOURCC      = 0x4
DDPF_RGB         = 0x40

DDSCAPS2_CUBEMAP           = 0x200
DDSCAPS2_CUBEMAP_ALL_FACES = 0xFC00
DDSCAPS2_VOLUME            = 0x200000

# BC (block-compression) estimated bytes-per-pixel (for size preview)
BC_BYTES_PER_PIXEL = {
    "BC1": 0.5, "BC1_SRGB": 0.5,
    "BC2": 1.0, "BC2_SRGB": 1.0,
    "BC3": 1.0, "BC3_SRGB": 1.0,
    "BC4": 0.5, "BC4_SRGB": 0.5,
    "BC5": 1.0, "BC5_SRGB": 1.0,
    "BC6H_UF16": 1.0, "BC6H_SF16": 1.0,
    "BC7": 1.0, "BC7_SRGB": 1.0,
}

# Legacy FourCC map
FOURCC_TO_FMT = {
    b"DXT1": "BC1",
    b"DXT3": "BC2",
    b"DXT5": "BC3",
    b"BC4U": "BC4",
    b"ATI1": "BC4",
    b"BC5U": "BC5",
    b"ATI2": "BC5",
}

# DXGI_FORMAT enum values → short string name (subset supported by image-rs /
# Nahida. Unknowns are reported as "DXGI_XXX" so the UI still shows them.)
DXGI_FMT_MAP = {
    71:  "BC1_UNORM", 72:  "BC1_UNORM_SRGB",
    74:  "BC2_UNORM", 75:  "BC2_UNORM_SRGB",
    77:  "BC3_UNORM", 78:  "BC3_UNORM_SRGB",
    80:  "BC4_UNORM", 81:  "BC4_SNORM",
    83:  "BC5_UNORM", 84:  "BC5_SNORM",
    95:  "BC6H_UF16",  96:  "BC6H_SF16",
    98:  "BC7_UNORM",  99:  "BC7_UNORM_SRGB",
    10:  "R8G8B8A8_UNORM",   11:  "R8G8B8A8_UNORM_SRGB",
    28:  "B8G8R8A8_UNORM",   29:  "B8G8R8A8_UNORM_SRGB",
    24:  "B8G8R8X8_UNORM",
    87:  "R8_UNORM",
    61:  "R16G16B16A16_FLOAT",
    2:   "R32G32B32A32_FLOAT",
    16:  "R16_FLOAT",  17:  "R16G16_FLOAT",
}
DXGI_NUMERIC = {v: k for k, v in DXGI_FMT_MAP.items()}

OUTPUT_FORMATS_PER_SPACE = {
    "srgb": ["BC1_UNORM_SRGB", "BC3_UNORM_SRGB", "BC5_UNORM",
             "BC7_UNORM_SRGB", "B8G8R8A8_UNORM_SRGB", "R8G8B8A8_UNORM_SRGB"],
    "linear": ["BC1_UNORM", "BC3_UNORM", "BC4_UNORM", "BC5_UNORM",
               "BC6H_UF16", "BC7_UNORM", "B8G8R8A8_UNORM", "R8G8B8A8_UNORM",
               "R16G16B16A16_FLOAT", "R32G32B32A32_FLOAT"],
    "unknown": ["BC1_UNORM", "BC3_UNORM", "BC7_UNORM"],
}


# ---------------------------------------------------------------------------
# Settings / Result data classes
# ---------------------------------------------------------------------------

@dataclass
class TextureResizeSettings:
    mode: str = "custom"         # 'custom' (dim-bound) or 'percent'
    operation: str = "resize"    # 'resize' | 'resize_and_convert' | 'convert'
    percent: int = 50            # 1..99, percent mode target size
    custom_w: int = 2048         # custom mode max width  (1024-step)
    custom_h: int = 2048         # custom mode max height (1024-step)
    output_format: str = ""      # empty = keep original format
    backup: bool = True          # create .bak before write
    # Scope options (set by caller – which mod(s) to process)
    scope: str = "all"           # 'all' | 'selected' (single mod folder)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "TextureResizeSettings":
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        # type safety
        s.mode = s.mode if s.mode in ("custom", "percent") else "custom"
        s.operation = s.operation if s.operation in (
            "resize", "resize_and_convert", "convert") else "resize"
        s.percent = max(1, min(99, int(s.percent or 50)))
        s.custom_w = max(1024, int(s.custom_w or 2048))
        s.custom_h = max(1024, int(s.custom_h or 2048))
        return s


@dataclass
class TextureMeta:
    path: str
    width: int = 0
    height: int = 0
    depth: int = 1
    mipmaps: int = 1
    array_size: int = 1
    is_cubemap: bool = False
    format: str = ""           # BC1 / BC3 / R8G8B8A8 etc
    color_space: str = "unknown"   # 'srgb' / 'linear' / 'unknown'
    file_bytes: int = 0

    def bpp(self) -> float:
        """Estimated bytes-per-pixel (for VRAM preview only)."""
        base = BC_BYTES_PER_PIXEL.get(self.format)
        if base:
            return base
        fmt = self.format.upper()
        if any(x in fmt for x in ("SRGB", "UNORM", "SNORM", "FLOAT")):
            if "R8G8B8A8" in fmt or "B8G8R8A8" in fmt or "B8G8R8X8" in fmt:
                return 4.0
            if "R16G16B16A16" in fmt:
                return 8.0
            if "R32G32B32A32" in fmt:
                return 16.0
            if "R16" in fmt and "G16" not in fmt:
                return 2.0
            if "R8" in fmt and "G8" not in fmt:
                return 1.0
        return 4.0

    def estimated_vram_bytes(self, w: Optional[int] = None,
                             h: Optional[int] = None) -> int:
        """Rough VRAM estimate for an entire mip-chain (sum 1..1/4^n)."""
        w = w if w else self.width
        h = h if h else self.height
        levels = self.mipmaps or 1
        total = 0
        bw, bh = w, h
        bpp = self.bpp()
        for _ in range(levels):
            total += max(1, bw) * max(1, bh) * bpp
            if bw == 1 and bh == 1:
                break
            bw //= 2
            bh //= 2
        total *= max(1, self.array_size)
        total *= max(1, self.depth)
        return int(total)


@dataclass
class ResizePlanItem:
    meta: TextureMeta
    target_w: int = 0
    target_h: int = 0
    target_format: str = ""
    action: str = "skip"       # 'resize' | 'convert' | 'both' | 'skip'
    reason: str = ""           # why skipped / chosen

    def delta_vram(self) -> int:
        original = self.meta.estimated_vram_bytes()
        new = self.meta.estimated_vram_bytes(
            w=self.target_w or self.meta.width,
            h=self.target_h or self.meta.height)
        return new - original


@dataclass
class TextureResizeReport:
    processed: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    original_total_vram: int = 0
    new_total_vram: int = 0
    items: list = field(default_factory=list)   # ResizePlanItem


# ---------------------------------------------------------------------------
# DDS header parsing (pure Python)
# ---------------------------------------------------------------------------

def parse_dds_meta(path: str) -> Optional[TextureMeta]:
    """Parse a DDS file header. Returns None on any I/O / magic error."""
    try:
        size = os.path.getsize(path)
        if size < 128:
            return None
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != DDS_MAGIC:
                return None
            hdr = f.read(DDS_HEADER_SIZE)
            if len(hdr) < DDS_HEADER_SIZE:
                return None
    except OSError as e:
        logger.debug("DDS 读取失败 %s: %s", path, e)
        return None

    try:
        # <7I 11I 5I 4I 2I> = 29 uint32 total = 116 bytes, matches hdr[0:116].
        #   7I: hsize,flags,h,w,pitch,depth,mips
        #  11I: dwReserved1[11] (ignored)
        #  5I: pixelformat.size/.flags/.fourcc/.rgb_bit_count
        #  4I: pixelformat.R/G/B/A masks
        #  2I: caps, caps2
        _FMT = "<7I 11I 5I 4I 2I"
        unpacked = struct.unpack(_FMT, hdr[:struct.calcsize(_FMT)])
        (hsize, flags, h, w, pitch, depth, mips) = unpacked[0:7]
        # unpacked[7:18] = dwReserved1[11], skip
        (pf_size, pf_flags, fourcc, rgb_bits, rmask,
         gmask, bmask, amask) = unpacked[18:26]
        (caps, caps2) = unpacked[26:28]
    except (struct.error, ValueError):
        return None

    if hsize != DDS_HEADER_SIZE or pf_size != 32 or h <= 0 or w <= 0:
        return None
    array_size = 1
    is_cube = bool(caps2 & DDSCAPS2_CUBEMAP)
    if is_cube:
        # All 6 faces: count as array_size=6
        faces = bin(caps2 & DDSCAPS2_CUBEMAP_ALL_FACES).count("1")
        array_size = max(1, faces)

    fmt = ""
    color_space = "unknown"

    if pf_flags & DDPF_FOURCC:
        fourcc_bytes = struct.pack("<I", fourcc)
        if fourcc_bytes == FOURCC_DX10:
            try:
                with open(path, "rb") as f:
                    f.seek(4 + DDS_HEADER_SIZE)
                    dx10 = f.read(DDS_HEADER_DXT10_SIZE)
                dxgi_fmt, res_dim, misc_flag, array_size_raw, misc_flags2 = \
                    struct.unpack("<5I", dx10)
                if array_size_raw:
                    if is_cube:
                        # cubemap array_size = faces * array_size_raw (common case)
                        # but keep as reported.  We'll respect reported value:
                        array_size = max(array_size, int(array_size_raw))
                    else:
                        array_size = int(array_size_raw)
                fmt = DXGI_FMT_MAP.get(dxgi_fmt, f"DXGI_{dxgi_fmt}")
            except Exception:
                fmt = "DX10(unknown)"
        else:
            fmt = FOURCC_TO_FMT.get(fourcc_bytes, fourcc_bytes.decode("ascii", "replace"))

    if not fmt and pf_flags & DDPF_RGB:
        if rgb_bits == 32 and rmask == 0xFF0000 and gmask == 0xFF00 and bmask == 0xFF and amask == 0xFF000000:
            fmt = "BGRA8888"
        elif rgb_bits == 32 and rmask == 0xFF and gmask == 0xFF00 and bmask == 0xFF0000 and amask == 0xFF000000:
            fmt = "RGBA8888"
        elif rgb_bits == 32 and rmask == 0xFF0000 and gmask == 0xFF00 and bmask == 0xFF and amask == 0x0:
            fmt = "BGRX8888"
        else:
            fmt = f"RGB{rgb_bits}"

    # Color-space classification based on format name
    fmt_lower = fmt.lower()
    if "_srgb" in fmt_lower:
        color_space = "srgb"
    elif ("bc6h" in fmt_lower or "float" in fmt_lower or "norm" in fmt_lower
          or fmt_lower in {"bgra8888", "rgba8888", "bgrx8888"}):
        color_space = "linear"

    if pf_flags & DDPF_ALPHAPIXELS and color_space == "unknown":
        color_space = "linear"

    meta = TextureMeta(
        path=path, width=w, height=h,
        depth=max(1, depth) if (caps2 & DDSCAPS2_VOLUME) else 1,
        mipmaps=max(1, mips),
        array_size=max(1, array_size),
        is_cubemap=is_cube,
        format=fmt,
        color_space=color_space,
        file_bytes=size,
    )
    return meta


# ---------------------------------------------------------------------------
# Valid downscale candidates – identical to Nahida shared/utils.ts /
# texture_resizer.rs valid_downscale_candidates.
# ---------------------------------------------------------------------------

def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def valid_downscale_candidates(w: int, h: int) -> list:
    """Return list of (candidate_w, candidate_h) smaller than source,
    preserving aspect ratio and aligned to 1024 * aspect-unit steps."""
    if w <= 0 or h <= 0:
        return []
    g = _gcd(w, h)
    if g <= 0:
        return []
    ratio_w = w // g
    ratio_h = h // g
    unit_w = ratio_w * 1024
    unit_h = ratio_h * 1024
    if unit_w <= 0 or unit_h <= 0:
        return []
    max_scale_w = w // unit_w
    max_scale_h = h // unit_h
    max_scale = min(max_scale_w, max_scale_h)
    if max_scale < 1:
        return []
    cands = []
    for s in range(1, max_scale + 1):
        cw = ratio_w * 1024 * s
        ch = ratio_h * 1024 * s
        if cw < w and ch < h:
            cands.append((cw, ch))
    # Largest first (preferred order for preview dropdown)
    cands.sort(key=lambda p: p[0] * p[1], reverse=True)
    return cands


def pick_target_by_custom(w: int, h: int, bound_w: int, bound_h: int):
    """Pick the largest candidate that fits within (bound_w, bound_h).
    Returns (tw, th) or None if no valid candidate."""
    cands = valid_downscale_candidates(w, h)
    if not cands:
        return None
    for cw, ch in cands:
        if cw <= bound_w and ch <= bound_h:
            return (cw, ch)
    return None


def pick_target_by_percent(w: int, h: int, percent: int):
    """Pick the candidate closest to (w * percent/100, h * percent/100).
    percent ∈ [1..99]. Returns (tw, th) or None."""
    cands = valid_downscale_candidates(w, h)
    if not cands:
        return None
    pct = max(1, min(99, int(percent))) / 100.0
    target_pixels = (w * pct) * (h * pct)
    best = None
    best_diff = None
    for cw, ch in cands:
        diff = abs(cw * ch - target_pixels)
        if best_diff is None or diff < best_diff:
            best = (cw, ch)
            best_diff = diff
    return best


# ---------------------------------------------------------------------------
# Batch scanning & planning
# ---------------------------------------------------------------------------

def list_folder_textures(folder: str, recursive: bool = True,
                         progress_cb=None,
                         follow_symlinks: bool = True) -> list:
    """Scan a folder for *.dds files and return list[TextureMeta].

    follow_symlinks=True 时 os.walk 会跟随目录符号链接 (mklink /D)，
    这样 Mods 下通过符号链接挂载的外部 mod 文件夹也会被扫描到。
    Windows junction (mklink /J) 总是会被跟随，不受此参数影响。
    """
    metas: list = []
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        return metas
    if recursive:
        it = (os.path.join(r, f)
              for r, d, fs in os.walk(folder, followlinks=follow_symlinks)
              for f in fs if f.lower().endswith(".dds"))
    else:
        it = (os.path.join(folder, f) for f in os.listdir(folder)
              if f.lower().endswith(".dds"))
    for idx, p in enumerate(it, 1):
        meta = parse_dds_meta(p)
        if meta:
            metas.append(meta)
        if progress_cb and idx % 25 == 0:
            try:
                progress_cb(idx, -1, os.path.basename(p))
            except Exception:
                pass
    return metas


def plan_resize(metas: list, settings: TextureResizeSettings) -> list:
    """Return a list of ResizePlanItem per meta, describing what action to do."""
    items: list = []
    op = settings.operation
    for meta in metas:
        item = ResizePlanItem(meta=meta)
        tw, th, tfmt = meta.width, meta.height, ""
        need_resize = False
        need_convert = False
        skip_reason = ""

        # 1) Compute target size
        if op in ("resize", "resize_and_convert"):
            if settings.mode == "custom":
                chosen = pick_target_by_custom(
                    meta.width, meta.height, settings.custom_w, settings.custom_h)
            else:
                chosen = pick_target_by_percent(
                    meta.width, meta.height, settings.percent)
            if chosen:
                tw, th = chosen
                if tw != meta.width or th != meta.height:
                    need_resize = True
            else:
                skip_reason = "无有效的缩放候选尺寸（宽高比或尺寸过小）"

        # 2) Compute target format
        if op in ("convert", "resize_and_convert") and settings.output_format:
            tfmt = settings.output_format
            if tfmt.lower() != meta.format.lower():
                need_convert = True
            elif not need_resize:
                skip_reason = "输出格式与源格式一致，跳过"

        if not need_resize and not need_convert:
            item.action = "skip"
            item.reason = skip_reason or "无需变更"
        else:
            actions = []
            if need_resize:
                actions.append("resize")
            if need_convert:
                actions.append("convert")
            item.action = "both" if len(actions) == 2 else actions[0]
            item.target_w = tw
            item.target_h = th
            item.target_format = tfmt
        items.append(item)
    return items


def estimate_report(items: list) -> TextureResizeReport:
    """Summarize VRAM before/after without executing any conversion.
    The returned report can be shown to user in the preview UI."""
    rep = TextureResizeReport()
    rep.processed = len(items)
    for it in items:
        orig = it.meta.estimated_vram_bytes()
        rep.original_total_vram += orig
        if it.action == "skip":
            rep.skipped += 1
            rep.new_total_vram += orig
        else:
            rep.updated += 1
            new = it.meta.estimated_vram_bytes(
                w=it.target_w or it.meta.width,
                h=it.target_h or it.meta.height)
            rep.new_total_vram += new
        rep.items.append(it)
    return rep


# ---------------------------------------------------------------------------
# Backup & write stubs (actual pixel transform requires Rust DLL).
# These helpers are safe placeholders; they only verify backup semantics.
# ---------------------------------------------------------------------------

def backup_dds(path: str) -> bool:
    """Copy a DDS file next to itself with .bak suffix, unless .bak exists."""
    bak = path + ".bak"
    if os.path.isfile(bak):
        return True
    try:
        shutil.copy2(path, bak)
        return True
    except OSError as e:
        logger.warning("DDS 备份失败 %s: %s", path, e)
        return False


def _bytes_human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    val = float(n)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.2f} {units[i]}"


def format_summary(report: TextureResizeReport) -> str:
    saved = report.original_total_vram - report.new_total_vram
    ratio = (saved / report.original_total_vram * 100.0
             if report.original_total_vram else 0.0)
    return (f"共扫描 {report.processed} 个DDS：更新候选 {report.updated}，"
            f"跳过 {report.skipped}，失败 {report.failed}。\n"
            f"预估VRAM：{_bytes_human(report.original_total_vram)} → "
            f"{_bytes_human(report.new_total_vram)} "
            f"（节省 {_bytes_human(max(0, saved))}，{ratio:.1f}%）")


# ==========================================================================
# Rust texture_tools.dll FFI bridge (ctypes)
# ==========================================================================
#
# 设计说明：
#   DLL 提供 2 个 extern "C" 入口：
#     texture_resize_ffi(input, output, op, mode, cw, ch, pct, fmt, backup,
#                        meta_out : *u32[8], msg_out: **char) -> i32
#     texture_free_ffi_string(msg : *char) -> ()
#
#   mode 编码：0 = custom, 1 = percent, 2 = ignore-size (convert only)
#   op 值："resize" / "resize_and_convert" / "convert"
#   meta_out 布局：[orig_w, orig_h, tgt_w, tgt_h, _pad, _pad, backup_flag, updated_flag]
#
#   返回码：
#     0 = 成功且实际写盘
#     1 = 成功但因尺寸/格式一致跳过
#    <0 = 错误，msg_out 非空时指向可读错误描述字符串（必须 free）

import ctypes
from pathlib import Path

_DLL_HANDLE = None          # type: Optional[ctypes.CDLL]
_DLL_LOAD_ERR = None        # type: Optional[str]


def _bin_lib_dir() -> Path:
    """Locate bin/lib/ regardless of how process was launched (Python or Nuitka)."""
    # 优先使用环境变量 BIN_DIR（OMG main.py 会注入）
    bin_dir = os.environ.get("BIN_DIR")
    if bin_dir and os.path.isdir(bin_dir):
        p = Path(bin_dir) / "lib"
        if p.is_dir():
            return p
    here = Path(os.path.abspath(__file__)).resolve().parents[1]  # bin/
    candidate = here / "lib"
    if candidate.is_dir():
        return candidate
    return here


def get_texture_tools_dll() -> Optional[ctypes.CDLL]:
    """Lazy-load texture_tools.dll once. Returns None on failure (reason
    stored in _DLL_LOAD_ERR, 可用 get_texture_tools_load_error() 查询)。"""
    global _DLL_HANDLE, _DLL_LOAD_ERR
    if _DLL_HANDLE is not None:
        return _DLL_HANDLE
    dll_path = _bin_lib_dir() / "texture_tools.dll"
    if not dll_path.is_file():
        _DLL_LOAD_ERR = f"未找到纹理工具库: {dll_path}"
        return None
    try:
        lib = ctypes.CDLL(str(dll_path))
    except OSError as e:
        _DLL_LOAD_ERR = f"加载 texture_tools.dll 失败: {e}"
        return None
    # 配置 ctypes 签名
    lib.texture_resize_ffi.restype = ctypes.c_int32
    lib.texture_resize_ffi.argtypes = [
        ctypes.c_char_p,  # input_path (utf-8)
        ctypes.c_char_p,  # output_path (utf-8, nullable = overwrite)
        ctypes.c_char_p,  # op
        ctypes.c_uint32,  # mode_kind
        ctypes.c_uint32,  # custom_w
        ctypes.c_uint32,  # custom_h
        ctypes.c_uint32,  # percent
        ctypes.c_char_p,  # output_format
        ctypes.c_uint32,  # backup (0/1)
        ctypes.POINTER(ctypes.c_uint32),  # meta_out[8]
        ctypes.POINTER(ctypes.c_char_p),  # msg_out
    ]
    lib.texture_free_ffi_string.restype = None
    lib.texture_free_ffi_string.argtypes = [ctypes.c_char_p]
    _DLL_HANDLE = lib
    return lib


def get_texture_tools_load_error() -> Optional[str]:
    """If DLL load failed, returns the error string; otherwise None."""
    if _DLL_HANDLE is not None:
        return None
    # ensure we've tried loading
    get_texture_tools_dll()
    return _DLL_LOAD_ERR


def texture_format_to_ffi(fmt: str) -> bytes:
    """Translate 'BC3_UNORM_SRGB' / None / '' into what Rust parses."""
    if not fmt:
        return b"KEEP_ORIGINAL"
    return fmt.encode("utf-8")


def execute_plan(
    items: list,
    settings: "TextureResizeSettings",
    progress_cb=None,
) -> TextureResizeReport:
    """Execute resize/convert on every item using the Rust DLL.

    Args:
        items: list[ResizePlanItem] produced by `plan_resize` / produced by caller.
        settings: TextureResizeSettings (needed for output_format / backup).
        progress_cb: optional callable(done: int, total: int, name: str)

    Returns:
        TextureResizeReport with filled failed list and actual updated count.
    """
    report = TextureResizeReport()
    total = len(items)
    report.processed = total
    lib = get_texture_tools_dll()

    # 决定 mode_kind & op 字符串
    if settings.mode == "custom":
        mode_kind = 0
        cw = settings.custom_w
        ch = settings.custom_h
        pct = 50
    elif settings.mode == "percent":
        mode_kind = 1
        cw = 2048
        ch = 2048
        pct = settings.percent
    else:
        mode_kind = 2
        cw = ch = 0
        pct = 0

    for idx, it in enumerate(items):
        # progress_cb 允许用户取消：若回调显式返回 False，提前 break
        if progress_cb is not None:
            try:
                rc = progress_cb(idx, total, os.path.basename(it.meta.path))
                if rc is False:
                    report.failed += 1
                    it.action = "skip"
                    it.reason = "用户取消"
                    continue
            except Exception:
                pass

        orig_meta = it.meta
        orig_est = orig_meta.estimated_vram_bytes()
        report.original_total_vram += orig_est

        if it.action == "skip":
            report.skipped += 1
            report.new_total_vram += orig_est
            report.items.append(it)
            continue

        if lib is None:
            report.failed += 1
            report.items.append(it)
            it.action = "skip"
            it.reason = _DLL_LOAD_ERR or "texture_tools.dll 不可用"
            continue

        # 操作：resize → "resize"；both → "resize_and_convert"；convert → "convert"
        op_map = {
            "resize": "resize",
            "convert": "convert",
            "both": "resize_and_convert",
        }
        op_str = op_map.get(it.action, "resize")

        # output_format: convert/both 需要 tfmt；resize 单独时用 tfmt（若非空）否则 KEEP
        tfmt = (it.target_format or "").strip()
        if op_str == "resize" and (not tfmt or tfmt.upper() == "KEEP_ORIGINAL"):
            fmt_arg = b"KEEP_ORIGINAL"
        else:
            fmt_arg = texture_format_to_ffi(tfmt) if tfmt else b"KEEP_ORIGINAL"

        meta = (ctypes.c_uint32 * 8)()
        msg_ptr = ctypes.c_char_p(None)
        try:
            ret = lib.texture_resize_ffi(
                it.meta.path.encode("utf-8"),
                None,           # 原地覆盖；Rust 端先写临时再 rename
                op_str.encode("utf-8"),
                ctypes.c_uint32(mode_kind),
                ctypes.c_uint32(cw),
                ctypes.c_uint32(ch),
                ctypes.c_uint32(pct),
                fmt_arg,
                ctypes.c_uint32(1 if settings.backup else 0),
                ctypes.cast(meta, ctypes.POINTER(ctypes.c_uint32)),
                ctypes.byref(msg_ptr),
            )
        except OSError as e:
            report.failed += 1
            report.new_total_vram += orig_est
            it.action = "skip"
            it.reason = f"DLL 调用异常: {e}"
            report.items.append(it)
            continue

        # 读取错误字符串（存在则需释放）
        err_msg = None
        if msg_ptr.value is not None:
            err_msg = msg_ptr.value.decode("utf-8", errors="replace")
            try:
                lib.texture_free_ffi_string(msg_ptr)
            except Exception:
                pass

        if ret < 0:
            report.failed += 1
            report.new_total_vram += orig_est
            it.action = "skip"
            it.reason = err_msg or f"处理失败 (code={ret})"
            report.items.append(it)
            continue

        # ret == 0 或 1：从 meta 解包实际信息
        ow, oh, tw, th, _p1, _p2, backup_flag, updated_flag = [int(x) for x in meta]

        if updated_flag:
            report.updated += 1
            # 更新 item 为实际处理结果
            it.target_w = tw
            it.target_h = th
            new_est = orig_meta.estimated_vram_bytes(w=tw, h=th)
            report.new_total_vram += new_est
        else:
            report.skipped += 1
            it.action = "skip"
            it.reason = it.reason or "尺寸/格式未变更"
            report.new_total_vram += orig_est
        report.items.append(it)

    return report
