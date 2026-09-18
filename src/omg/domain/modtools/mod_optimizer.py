"""
mod_optimizer.py — Port of XXMI-Launcher 2.2.1 Optimize Mods core logic.

Provides INI-level optimization for GIMI / 3DMigoto mod directories:
  - RogueIni detection: embedded d3dx.ini inside a Mod (keywords check)
  - UnwantedFile: known-bad ini files (EFMI VSCheck, various shaderfixes helpers)
  - UnwantedTrigger: deprecated CheckTextureOverride slots (ib/vb0 on EFMI/WWMI)
  - GlobalTrigger (FPS killer): unbounded CheckTextureOverride inside ShaderRegex
  - Duplicate-library detection vs Core/GIMI/Libraries (GIMI only)

All modifications are written back with a .omg_bak first-copy backup.
Skipped untouched files via mtime cache stored at BIN_DIR/cache/ini_optimizer.json.
"""

import os
import re
import json
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "light":  "green",
    "medium": "yellow",
    "heavy":  "orange",
    "severe": "red",
    "fatal":  "dark_red",
}

@dataclass
class IniIssue:
    reason: str            # 'rogue' / 'unwanted_file' / 'unwanted_trigger' / 'global_trigger'
    message: str
    severity: str = "medium"   # light / medium / heavy / severe / fatal
    file_path: str = ""
    line_no: int = 0
    line_text: str = ""
    fixed_line: str = ""      # replacement for line-level fixes
    namespace: str = ""       # [TextureOverrideXXX] section name
    triggers: int = 0         # number of global triggers


@dataclass
class OptimizeResult:
    scanned: int = 0
    modified: int = 0
    skipped: int = 0
    errors: int = 0
    issues: list = field(default_factory=list)         # IniIssue list
    disabled_files: list = field(default_factory=list) # paths disabled (DISABLED_ prefix)
    fixed_lines: list = field(default_factory=list)    # line-level auto-fixes
    summaries: dict = field(default_factory=dict)      # category -> count

    def __post_init__(self):
        self.summaries = {
            "rogue": 0, "unwanted_file": 0, "unwanted_trigger": 0,
            "global_trigger": 0, "duplicate_library": 0,
        }


# ---------------------------------------------------------------------------
# Constants – unwanted files / triggers
# ---------------------------------------------------------------------------

# Known bad files inside ShaderFixes that should be disabled
UNWANTED_SHADERFIX_FILES = {
    "3dvision2sbs.ini", "help.ini", "mouse.ini", "upscale.ini",
}
# EFMI-only bad file inside Mods
UNWANTED_EFMI_FILES = {"VSCheck.ini"}

# Active-importer check → what slot triggers we patch out
IMPORTER_BAD_SLOTS = {
    "EFMI": {"ib": "EFMI"},
    "WWMI": {"ib": "WWMI", "vb0": "WWMI"},
}

# Pattern matching CheckTextureOverride line, captures slot
RE_CHECK_TEX = re.compile(
    r"^\s*(;?)\s*checktextureoverride\s*=\s*([A-Za-z0-9_\-]+)\s*(.*)$",
    re.IGNORECASE,
)
RE_SECTION = re.compile(r"^\s*\[(.+)\]\s*$")
RE_INCLUDE = re.compile(r"^\s*\[include\s*\]\s*$", re.IGNORECASE)
RE_OPTION = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*(.*)$")
RE_NAMESPACE = re.compile(r"^\s*namespace\s*=\s*(\S+)", re.IGNORECASE)
RE_COMMANDLIST_CALL = re.compile(
    r"\b(?:run|call|run_compute_shader|run_ps|run_vs|postprocess)\s*\(\s*([A-Za-z0-9_\-\.]+)\s*\)",
    re.IGNORECASE,
)

# Rogue ini detection keys (a d3dx.ini embedded in a Mod)
ROGUE_KEYWORDS_RE = re.compile(
    r"^\s*\[(?:loader|system|stereo|convergence|separation|preset|device|hunting|profile)\s*\]",
    re.IGNORECASE,
)
ROGUE_INCLUDE_MODS_RE = re.compile(
    r"^\s*include_recursive\s*=\s*mods\b", re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Cache – avoid re-validating files with unchanged mtime & size
# ---------------------------------------------------------------------------

class IniValidatorCache:
    """Stores (mtime_ns, size) → validated OK mapping per path."""

    def __init__(self, cache_path: str):
        self._path = cache_path
        self._data: dict = {}
        self._dirty = False
        self.load()

    def load(self):
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.warning("INI optimizer cache corrupted: %s", e)
                self._data = {}
        self._dirty = False

    def save(self):
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
        except Exception as e:
            logger.warning("INI optimizer cache save failed: %s", e)

    def _key(self, mtime_ns: int, size: int) -> str:
        return f"{mtime_ns}|{size}"

    def is_valid(self, path: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        entry = self._data.get(path)
        return isinstance(entry, list) and len(entry) == 3 and \
               self._key(st.st_mtime_ns, st.st_size) == f"{entry[0]}|{entry[1]}" and entry[2]

    def mark(self, path: str, valid: bool):
        try:
            st = os.stat(path)
        except OSError:
            return
        self._data[path] = [st.st_mtime_ns, st.st_size, bool(valid)]
        self._dirty = True

    def reset(self):
        self._data = {}
        self._dirty = True


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------

class IniValidator:
    """Validates a single INI file, returning issues list."""

    def __init__(self, importer: str = "GIMI"):
        """importer in ('GIMI','EFMI','WWMI') – controls slot cleanup rules."""
        self.importer = importer.upper()

    # --- public entry points ---
    def validate_ini(self, ini_path: str,
                     include_stack: Optional[set] = None) -> list:
        """Full validation of one ini file, including its include chain."""
        if include_stack is None:
            include_stack = set()
        ap = os.path.abspath(ini_path)
        if ap in include_stack:
            return []
        include_stack.add(ap)
        try:
            with open(ap, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            return []
        return self.validate_ini_text(text, ini_path, include_stack)

    def validate_ini_text(self, text: str, ini_path: str,
                          include_stack: Optional[set] = None) -> list:
        issues: list = []
        lines = text.splitlines()
        bad_slots = IMPORTER_BAD_SLOTS.get(self.importer, {})

        # Stage 1: RogueIni + UnwantedTrigger (scalar per line)
        cur_section = ""
        commandlist_index: dict = {}   # [CommandList X] → line_no
        trigger_index: dict = {}        # [TextureOverride X] → line_no

        for idx, line in enumerate(lines, start=1):
            # Section detection
            m = RE_SECTION.match(line)
            if m:
                cur_section = m.group(1).strip()
                cs_lower = cur_section.lower()
                if cs_lower.startswith("commandlist"):
                    commandlist_index[cur_section] = idx
                elif cs_lower.startswith("textureoverride"):
                    trigger_index[cur_section] = idx
                # RogueIni: section name matches [loader]/[system]/... etc
                if ROGUE_KEYWORDS_RE.match(line):
                    issues.append(IniIssue(
                        reason="rogue", severity="severe",
                        file_path=ini_path, line_no=idx, line_text=line,
                        message=f"疑似 d3dx.ini 混入 mod：[{cur_section}] 区段",
                    ))
                continue

            # Rogue include_recursive = mods (a d3dx.ini include pointing at Mods)
            if ROGUE_INCLUDE_MODS_RE.match(line):
                issues.append(IniIssue(
                    reason="rogue", severity="severe",
                    file_path=ini_path, line_no=idx, line_text=line,
                    message="包含 include_recursive = mods （疑似主 d3dx.ini 混入Mod）",
                ))

            # UnwantedTrigger: CheckTextureOverride slots reserved for importers
            m2 = RE_CHECK_TEX.match(line)
            if m2 and not m2.group(1):   # not already commented
                slot = m2.group(2).strip()
                if slot in bad_slots:
                    replacement = ""
                    if self.importer == "WWMI" and slot == "ib":
                        replacement = r"$\WWMIv1\enable_ib_callbacks = 1"
                    else:
                        replacement = ";" + line.strip()
                    issues.append(IniIssue(
                        reason="unwanted_trigger", severity="heavy",
                        file_path=ini_path, line_no=idx, line_text=line,
                        fixed_line=replacement,
                        message=f"废弃的 CheckTextureOverride={slot} 触发（{bad_slots[slot]}内置）",
                    ))

        # Stage 2: GlobalTrigger detection — ShaderRegex sections that call
        # into TextureOverrides without a `hash = pattern` filter in-between.
        issues.extend(self._find_global_triggers(lines, ini_path,
                                                 cur_section_stack=[]))
        return issues

    def _find_global_triggers(self, lines, ini_path,
                              cur_section_stack) -> list:
        """Find ShaderRegex sections whose body calls TextureOverrides
        without encountering a `hash = ...` guard. Treat every call target
        as a potential trigger (including nested [CommandList] → [CommandList Y]
        → [TextureOverride X] chains)."""

        issues: list = []
        n = len(lines)
        i = 0
        cur_sec = ""
        while i < n:
            line = lines[i]
            m = RE_SECTION.match(line)
            if m:
                cur_sec = m.group(1).strip()
                sec_lower = cur_sec.lower()
                if sec_lower.startswith("shaderregex") or \
                   sec_lower.startswith("shaderreplace"):
                    # Walk until next [section]
                    guard_seen = False
                    calls: list = []
                    j = i + 1
                    while j < n:
                        l2 = lines[j]
                        if RE_SECTION.match(l2):
                            break
                        l2_stripped = l2.strip()
                        if not l2_stripped or l2_stripped.startswith(";"):
                            j += 1
                            continue
                        # 'hash = ' or 'hash_filter' counts as a guard
                        if l2_stripped.lower().startswith("hash") and \
                           "=" in l2_stripped:
                            guard_seen = True
                        for cm in RE_COMMANDLIST_CALL.finditer(l2):
                            calls.append(cm.group(1))
                        j += 1
                    if not guard_seen and calls:
                        severity, count_label = self._rate_gt(len(calls))
                        issues.append(IniIssue(
                            reason="global_trigger", severity=severity,
                            file_path=ini_path, line_no=i + 1,
                            namespace=cur_sec, triggers=len(calls),
                            line_text=lines[i],
                            message=(f"[{cur_sec}] 无 hash 限定，全局执行 "
                                     f"{len(calls)} 个触发 — {count_label}性能影响"),
                        ))
                    i = j
                    continue
            i += 1
        return issues

    @staticmethod
    def _rate_gt(trigger_count: int):
        if trigger_count <= 2:
            return "light", "轻微"
        if trigger_count <= 5:
            return "medium", "中等"
        if trigger_count <= 15:
            return "heavy", "较高"
        if trigger_count <= 20:
            return "severe", "严重"
        return "fatal", "致命"


# ---------------------------------------------------------------------------
# Helpers – disable / sanitize write-back
# ---------------------------------------------------------------------------

def _first_available_disabled_name(target_dir: str, filename: str) -> str:
    """Return a DISABLED_<name> path that does not yet exist, avoiding
    collisions with multiple disable passes."""
    base = f"DISABLED_{filename}"
    candidate = os.path.join(target_dir, base)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(base)
    i = 2
    while True:
        candidate = os.path.join(target_dir, f"{stem}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def disable_ini_file(abs_path: str) -> Optional[str]:
    """Rename a file or top-level mod folder into DISABLED_ form.
    Returns the new path on success, None on error."""
    if not os.path.exists(abs_path):
        return None
    parent = os.path.dirname(abs_path)
    name = os.path.basename(abs_path)
    try:
        new_path = _first_available_disabled_name(parent, name)
        os.rename(abs_path, new_path)
        return new_path
    except OSError as e:
        logger.warning("禁用失败: %s → %s (%s)", abs_path, name, e)
        return None


def sanitize_ini(abs_path: str, line_issues: list) -> bool:
    """Apply line-level fixes (comment out) to the given INI.
    Creates a .omg_bak copy on first write attempt. Returns True if written."""
    if not line_issues:
        return False
    # map line_no (1-based) → fixed_line
    fixes = {it.line_no: it.fixed_line for it in line_issues if it.fixed_line}
    if not fixes:
        return False
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("读取失败 %s: %s", abs_path, e)
        return False
    modified = False
    for no, replacement in fixes.items():
        idx = no - 1
        if 0 <= idx < len(lines):
            old = lines[idx].rstrip("\r\n")
            # preserve line break style
            lb = "\r\n" if lines[idx].endswith("\r\n") else \
                 ("\n" if lines[idx].endswith("\n") else "")
            if old.strip() != replacement.strip():
                lines[idx] = replacement + lb
                modified = True
    if not modified:
        return False
    # Backup once
    bak = abs_path + ".omg_bak"
    if not os.path.isfile(bak):
        try:
            shutil.copy2(abs_path, bak)
        except OSError:
            pass
    try:
        with open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        return True
    except OSError as e:
        logger.warning("写回失败 %s: %s", abs_path, e)
        return False


# ---------------------------------------------------------------------------
# Folder-level optimization
# ---------------------------------------------------------------------------

class ModOptimizer:
    """High-level API: scans Mods/ShaderFixes/Libraries and applies optimizations.

    Parameters
    ----------
    gimi_dir : str
        GIMI install root (contains Mods/, ShaderFixes/, Core/GIMI/Libraries/).
    cache_path : str
        Path to the JSON cache file (typically BIN_DIR/cache/ini_optimizer.json).
    importer : str
        'GIMI' (default) / 'EFMI' / 'WWMI' — controls unwanted-trigger detection.
    exclude_patterns : list[str]
        Wildcard patterns of paths to skip (read from d3dx.ini exclude_recursive).
    """

    def __init__(self, gimi_dir: str, cache_path: str,
                 importer: str = "GIMI",
                 exclude_patterns: Optional[list] = None):
        self.gimi_dir = os.path.normpath(gimi_dir) if gimi_dir else ""
        self.cache = IniValidatorCache(cache_path)
        self.validator = IniValidator(importer=importer)
        self.exclude_patterns = list(exclude_patterns or [])

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def optimize_mods_folder(self, silent: bool = False,
                             progress_cb=None) -> OptimizeResult:
        """Optimize the Mods/ directory. Returns a result summary.

        Line-level issues (unwanted_trigger) are auto-fixed.
        Rogue ini and unwanted files are auto-disabled.
        Global-trigger issues are reported only (need user confirmation
        via UI in the future); for silent mode we still auto-disable
        files rated severe / fatal."""
        result = OptimizeResult()
        mods_dir = os.path.join(self.gimi_dir, "Mods")
        if not self.gimi_dir or not os.path.isdir(mods_dir):
            return result

        for root, dirs, files in os.walk(mods_dir, topdown=True):
            # Exclude directories by pattern (match basename)
            if self.exclude_patterns:
                dirs[:] = [d for d in dirs
                           if not self._excluded(os.path.join(root, d))]
            for name in files:
                if not name.lower().endswith(".ini"):
                    continue
                full = os.path.join(root, name)
                if self.exclude_patterns and self._excluded(full):
                    continue
                self._process_one_ini(full, result, auto_fix=True,
                                      silent=silent)
                result.scanned += 1
                if progress_cb:
                    try:
                        progress_cb(result.scanned, -1, name)
                    except Exception:
                        pass

        # Auto-disable EFMI VSCheck.ini level files
        self._disable_unwanted_mods_files(mods_dir, result)
        # Duplicate-libraries vs Core/GIMI/Libraries (GIMI only)
        if self.validator.importer == "GIMI":
            self._detect_duplicate_libraries(result, silent=silent)

        self.cache.save()
        self._sum_categories(result)
        return result

    def optimize_shaderfixes_folder(self, progress_cb=None) -> OptimizeResult:
        """Optimize the ShaderFixes/ directory. Auto-disables known
        helper INIs and reports remaining issues."""
        result = OptimizeResult()
        sf_dir = os.path.join(self.gimi_dir, "ShaderFixes")
        if not self.gimi_dir or not os.path.isdir(sf_dir):
            return result
        for name in os.listdir(sf_dir):
            full = os.path.join(sf_dir, name)
            if not os.path.isfile(full) or not name.lower().endswith(".ini"):
                continue
            if name.lower() in UNWANTED_SHADERFIX_FILES:
                new_path = disable_ini_file(full)
                if new_path:
                    result.disabled_files.append(new_path)
                    result.summaries["unwanted_file"] += 1
                    result.issues.append(IniIssue(
                        reason="unwanted_file", severity="medium",
                        file_path=full,
                        message=f"ShaderFixes 中不需要的辅助文件已禁用: {name}",
                    ))
                continue
            self._process_one_ini(full, result, auto_fix=True, silent=True)
            result.scanned += 1
            if progress_cb:
                try:
                    progress_cb(result.scanned, -1, name)
                except Exception:
                    pass
        self.cache.save()
        self._sum_categories(result)
        return result

    def full_optimize(self, silent: bool = False,
                      progress_cb=None) -> OptimizeResult:
        """Convenience: run both folders and merge result."""
        r1 = self.optimize_mods_folder(silent=silent, progress_cb=progress_cb)
        r2 = self.optimize_shaderfixes_folder(progress_cb=progress_cb)
        merged = OptimizeResult()
        for r in (r1, r2):
            merged.scanned += r.scanned
            merged.modified += r.modified
            merged.skipped += r.skipped
            merged.errors += r.errors
            merged.issues.extend(r.issues)
            merged.disabled_files.extend(r.disabled_files)
            merged.fixed_lines.extend(r.fixed_lines)
            for k in merged.summaries:
                merged.summaries[k] += r.summaries.get(k, 0)
        return merged

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _excluded(self, abs_path: str) -> bool:
        rel = os.path.relpath(abs_path, self.gimi_dir).replace("\\", "/")
        base = os.path.basename(abs_path)
        low = base.lower()
        for pat in self.exclude_patterns:
            p = pat.lower().replace("\\", "/")
            if not p:
                continue
            if "*" in p or "?" in p:
                # naive wildcard: * → .* ? → . regex
                import fnmatch
                if fnmatch.fnmatch(low, p) or fnmatch.fnmatch(rel.lower(), p):
                    return True
            else:
                if p in low or p in rel.lower():
                    return True
        return False

    def _process_one_ini(self, abs_path: str, result: OptimizeResult,
                         auto_fix: bool, silent: bool):
        # Cached valid → skip
        if self.cache.is_valid(abs_path):
            result.skipped += 1
            return
        try:
            issues = self.validator.validate_ini(abs_path)
        except Exception as e:
            logger.warning("INI 解析异常 %s: %s", abs_path, e)
            result.errors += 1
            return

        if not issues:
            self.cache.mark(abs_path, True)
            return

        result.issues.extend(issues)
        # Bucket by reason
        has_rogue = any(i.reason == "rogue" for i in issues)
        line_issues = [i for i in issues if i.reason == "unwanted_trigger"
                       and i.fixed_line]
        severe_gt = [i for i in issues if i.reason == "global_trigger"
                     and i.severity in ("severe", "fatal")]

        written = False
        # 1) Rogue → disable file
        if has_rogue:
            new_path = disable_ini_file(abs_path)
            if new_path:
                result.disabled_files.append(new_path)
                result.modified += 1
                written = True
        # 2) Line-level fixes
        if not written and line_issues and auto_fix:
            if sanitize_ini(abs_path, line_issues):
                result.fixed_lines.append(abs_path)
                result.modified += 1
                written = True
        # 3) Severe/Fatal global trigger → disable in silent mode only
        if not written and severe_gt and silent:
            new_path = disable_ini_file(abs_path)
            if new_path:
                result.disabled_files.append(new_path)
                result.modified += 1
                written = True

        if not written:
            # Still has issues (e.g. light/medium GT for user review) —
            # don't mark cached so they re-surface next pass.
            pass
        else:
            self.cache.mark(abs_path, False)  # force re-check next run (content changed)

    def _disable_unwanted_mods_files(self, mods_dir: str, result: OptimizeResult):
        for root, dirs, files in os.walk(mods_dir):
            for name in files:
                if self.validator.importer == "EFMI" and \
                   name.lower() in {n.lower() for n in UNWANTED_EFMI_FILES}:
                    full = os.path.join(root, name)
                    new_path = disable_ini_file(full)
                    if new_path:
                        result.disabled_files.append(new_path)
                        result.summaries["unwanted_file"] += 1

    def _detect_duplicate_libraries(self, result: OptimizeResult, silent: bool):
        libs_dir = os.path.join(self.gimi_dir, "Core", "GIMI", "Libraries")
        mods_dir = os.path.join(self.gimi_dir, "Mods")
        if not os.path.isdir(libs_dir) or not os.path.isdir(mods_dir):
            return

        # Collect namespaces declared in Core libraries
        core_ns = self._collect_namespaces(libs_dir)
        if not core_ns:
            return
        # Scan Mods for INIs declaring any of those namespaces
        for root, dirs, files in os.walk(mods_dir):
            for name in files:
                if not name.lower().endswith(".ini"):
                    continue
                full = os.path.join(root, name)
                ns_here = self._collect_namespaces(full, single_file=True)
                overlap = ns_here & core_ns
                if overlap:
                    result.summaries["duplicate_library"] += 1
                    result.issues.append(IniIssue(
                        reason="duplicate_library", severity="medium",
                        file_path=full,
                        namespace=",".join(sorted(overlap)),
                        message=(f"Mod 中声明的 namespace {sorted(overlap)} 已在 "
                                 "Core\\GIMI\\Libraries 中存在，可能重复加载"),
                    ))
                    if silent:
                        new_path = disable_ini_file(full)
                        if new_path:
                            result.disabled_files.append(new_path)
                            result.modified += 1

    @staticmethod
    def _collect_namespaces(path: str, single_file: bool = False) -> set:
        out = set()
        if single_file:
            files = [path]
        else:
            files = []
            for r, d, fs in os.walk(path):
                for fn in fs:
                    if fn.lower().endswith(".ini"):
                        files.append(os.path.join(r, fn))
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = RE_NAMESPACE.match(line)
                        if m:
                            out.add(m.group(1).strip().lower())
            except OSError:
                pass
        return out

    @staticmethod
    def _sum_categories(result: OptimizeResult):
        for i in result.issues:
            if i.reason in result.summaries:
                # rogue / unwanted_trigger / global_trigger / duplicate_library
                # count 1 per issue (not 1 per file) for user review.
                result.summaries[i.reason] = result.summaries.get(i.reason, 0) + 1


# ---------------------------------------------------------------------------
# d3dx.ini exclude_recursive reader
# ---------------------------------------------------------------------------

def read_exclude_patterns(gimi_dir: str) -> list:
    patterns = []
    d3dx = os.path.join(gimi_dir, "d3dx.ini")
    if not os.path.isfile(d3dx):
        return patterns
    try:
        with open(d3dx, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = re.match(r"^\s*exclude_recursive\s*=\s*(.+)$", line,
                             re.IGNORECASE)
                if m:
                    patterns.append(m.group(1).strip())
    except OSError:
        pass
    return patterns
