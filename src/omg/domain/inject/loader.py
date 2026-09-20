"""
loader.py  -  Game launcher with 3DMigoto DLL injection for OMG

Supports three injection methods:
  - "loader"  : Run 3DMigoto Loader.exe from GIMI (WH_CBT hook, default).
                Most reliable — native C++ exe passes DllMain checks.
  - "hook"    : WH_CBT hook via 3dmloader.dll called directly from Python.
                Requires d3dx.ini [Loader] loader = <process_name>.
  - "inject"  : CreateRemoteThread via 3dmloader.dll's Inject export.

All methods place d3d11.dll in GIMI (never in the game directory).
"""

import os
import re
import sys
import ctypes
import ctypes as ct
import logging
import subprocess
import shutil
import threading
import time
from pathlib import Path

import ctypes.wintypes as wt

logger = logging.getLogger("OMG")

from omg.core.paths import BINARIES_DIR
_LIB_DIR = Path(BINARIES_DIR)
INJECTOR_SRC = _LIB_DIR / "3dmloader.dll"
LOADER_EXE_SRC = _LIB_DIR / "3DMigoto Loader.exe"


# ---------------------------------------------------------------------------
# Safe file copy helper
# ---------------------------------------------------------------------------
def _safe_copy(src, dst):
    """Copy file, removing destination first if locked."""
    try:
        shutil.copy2(str(src), str(dst))
    except PermissionError:
        logger.warning(f"文件被占用，尝试删除后重试: {dst}")
        try:
            os.remove(str(dst))
        except Exception:
            pass
        shutil.copy2(str(src), str(dst))


# ---------------------------------------------------------------------------
# Patch d3dx.ini before launch
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 写入纪律：串行化 + 原子替换
# ---------------------------------------------------------------------------
# 进程内所有 d3dx.ini 写者共用这一把锁。d3dx.ini 的改写是典型的
# read-modify-write：「读全文 → 改几行 → 整份写回」。若两个写者交错，后写的
# 会用自己读到的旧快照覆盖掉前一个写者的全部改动。
#
# 现存的写者：
#   - UI 线程：设置页改 hunting / show_warnings（去抖后调用）
#   - 启动 worker（QThread）：启动前写 target / launch / loader
# 因此 read 也必须放进锁内的最后一刻，不能提前到临界区之外。
D3DX_WRITE_LOCK = threading.RLock()

# os.replace 在目标文件被别的进程以 FILE_SHARE_READ 打开时会 PermissionError
# ——3DMigoto 的 Injector 正是这样读 d3dx.ini 的。它的读取窗口只有几毫秒，
# 短暂重试即可跨过去；反复失败则说明文件被长期占用（编辑器/杀软），放弃。
_REPLACE_ATTEMPTS = 4
_REPLACE_DELAY = 0.03


def _atomic_replace_text(ini_path: Path, text: str) -> None:
    """把 *text* 原子地写入 *ini_path*：同目录写 ``.tmp`` 后 ``os.replace``。

    与 ``Path.write_text``（先 truncate 再写）的关键差别：目标文件在任意时刻
    要么是完整的旧内容、要么是完整的新内容，不存在「空文件 / 半截文件」的中间
    态。写一半失败只会留下一个 ``.tmp``，目标文件一个字节都不会变。

    失败时抛出最后一次的 OSError（并由 ``finally`` 清理 ``.tmp`` 残留）。
    """
    tmp_path = ini_path.with_name(ini_path.name + ".tmp")
    last_error: BaseException | None = None
    try:
        tmp_path.write_text(text, encoding="utf-8")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, ini_path)
                return
            except OSError as e:
                last_error = e
                if attempt < _REPLACE_ATTEMPTS - 1:
                    time.sleep(_REPLACE_DELAY)
        raise last_error
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ini 值行：`值` + 可选的行尾注释（`;` 及其后内容，连同前面的间隔一起保留）
_INI_LINE_RE = re.compile(r"^(?P<value>.*?)(?P<comment>[ \t]*;.*)?$")


def patch_d3dx_ini_values(ini_path: Path, values: dict,
                          create_missing: bool = True) -> bool:
    """幂等地把 ``values`` 写入 d3dx.ini。

    Args:
        ini_path: d3dx.ini 路径。
        values: ``{(section_lower, key_lower): 目标值}``。
        create_missing: 键不存在时是否新建（插到所属 section 首行之后）。

    Returns:
        True 表示确有改动并已写盘；False 表示所有键现值都与目标一致，
        文件一字未动（不刷新 mtime，也不会吃掉行尾注释）。
    """
    # 读取必须在锁内：这是「读到的不是最新快照就无法安全写回」的必要条件。
    with D3DX_WRITE_LOCK:
        return _patch_d3dx_ini_values_locked(ini_path, values, create_missing)


def _patch_d3dx_ini_values_locked(ini_path: Path, values: dict,
                                  create_missing: bool) -> bool:
    lines = ini_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)

    patched_keys = set()
    current_section = ""
    changed = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            current_section = stripped[1:stripped.index("]")].strip().lower()
            continue
        if "=" in stripped and not stripped.startswith(";") and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip().lower()
            lookup = (current_section, key)
            if lookup in values:
                patched_keys.add(lookup)
                raw_value = stripped.split("=", 1)[1]
                # d3dx.ini 支持行尾注释（如 `hunting = 0 ; 保持关闭`）：比较时
                # 只看注释之前的部分，改写时把注释连同前面的间隔原样接回去。
                match = _INI_LINE_RE.match(raw_value)
                current_value = (match.group("value") or "").strip()
                suffix = match.group("comment") or ""

                if values[lookup] is None:
                    # 本次不写入该键。但若盘上是个空值行（历史版本留下的
                    # `launch = `），3DMigoto 读到空值会在游戏内左上角报
                    # 警告，这里顺手注释掉，做一次自愈。
                    if current_value == "":
                        lines[i] = ";" + line
                        changed = True
                    continue

                target_value = str(values[lookup]).strip()
                if current_value == target_value:
                    continue
                lines[i] = f"{key} = {values[lookup]}{suffix}\n"
                changed = True

    if create_missing:
        for lookup, value in values.items():
            if lookup in patched_keys or value is None:
                continue
            section_name, key = lookup
            section_found = False
            for i, line in enumerate(lines):
                s = line.strip()
                if s.startswith("[") and "]" in s:
                    sec = s[1:s.index("]")].strip().lower()
                    if sec == section_name:
                        lines.insert(i + 1, f"{key} = {value}\n")
                        section_found = True
                        break
            if not section_found:
                lines.append(f"\n[{section_name}]\n")
                lines.append(f"{key} = {value}\n")
            changed = True

    if changed:
        _atomic_replace_text(ini_path, "".join(lines))
    return changed


# 可在游戏运行中热更新的键（3DMigoto 支持按键 reload ini）
D3DX_RUNTIME_KEYS = (("hunting", "hunting"), ("logging", "show_warnings"))


def patch_d3dx_runtime_options(ini_path: Path, hunting: int,
                               show_warnings: int) -> bool:
    """只写 [Hunting] hunting 与 [Logging] show_warnings（幂等）。

    这两个键被 3DMigoto 注册为 reloadable，游戏运行中改完 ini 再按 reload
    键即可生效，因此关闭设置时也要落盘（不再等到下次启动）。
    """
    return patch_d3dx_ini_values(ini_path, {
        ("hunting", "hunting"): str(hunting),
        ("logging", "show_warnings"): str(show_warnings),
    })


def patch_d3dx_ini(ini_path: Path, target: str, hunting: int,
                    show_warnings: int, launch: str | None = None,
                    loader: str = "") -> bool:
    """
    Modify d3dx.ini in-place:
    - [Loader]  target = <target>
                launch  = <launch>   (``None`` 表示本次不碰该键)
                loader  = <loader>   (process name that is allowed to load d3d11.dll)
    - [Hunting] hunting = <hunting>
    - [Logging] show_warnings = <show_warnings>

    幂等：值与现值一致时跳过（含 hunting / show_warnings），全一致则不写盘。

    ``launch`` 传 ``None`` 的含义（重要）：
        ``[Loader] launch`` 是 3DMigoto 官方 loader.exe 用来拉起游戏的命令。
        OMGLite 的链路上没有任何组件读它——d3d11.dll 源码与 3dmloader.dll
        （其二进制内 "launch" 字符串 0 命中）都不解析这个键。因此：
        - 自定义启动时既不写自定义命令，也不该把键清空成空值——空值会让
          3DMigoto 在游戏内左上角报「launch 为空」的警告，误导用户；
        - 传 ``None`` 表示保持文件原样：有值留着、没有值就不创建；
        - 若盘上残留着历史写入的空值行，会被自动注释掉（自愈）。

    Returns:
        True 表示确有改动并写盘。
    """
    return patch_d3dx_ini_values(ini_path, {
        ("loader", "target"): target,
        ("loader", "launch"): launch,
        ("loader", "loader"): loader,
        ("hunting", "hunting"): str(hunting),
        ("logging", "show_warnings"): str(show_warnings),
    })


# ---------------------------------------------------------------------------
# Find game exe in game directory
# ---------------------------------------------------------------------------
def find_game_exe(game: Path):
    """Return (game_exe: Path, target: str) for the game in *game*."""
    exe_files = [f for f in game.iterdir() if f.suffix.lower() == ".exe"]
    if not exe_files:
        return None, None

    game_exe = exe_files[0]
    for f in exe_files:
        name_lower = f.name.lower()
        if "genshinimpact" in name_lower or "yuanshen" in name_lower:
            game_exe = f
            break

    return game_exe, game_exe.name


# ---------------------------------------------------------------------------
# Copy d3d11.dll to GIMI
# ---------------------------------------------------------------------------
def _copy_d3d11_to_gimi(gimi: Path) -> tuple[bool, str, Path]:
    """Copy d3d11.dll to GIMI. Returns (ok, msg, d3d11_path).

    Smart selection priority:
    1. lib/MysteriousRitual/d3d11.dll  (神秘仪式加工产物)
    2. lib/d3d11/ 目录下的文件 (d3d11InOMG 加工产物)
    3. lib/d3d11.dll  (原始编译产物)
    """
    d3d11_dst = gimi / "d3d11.dll"

    try:
        ritual_dll = _LIB_DIR / "MysteriousRitual" / "d3d11.dll"
        d3d11_out = _LIB_DIR / "d3d11"
        d3d11_src = _LIB_DIR / "d3d11.dll"

        if ritual_dll.is_file():
            logger.info(f"复制 MysteriousRitual 加工 d3d11 -> {gimi}")
            _safe_copy(ritual_dll, d3d11_dst)
        elif d3d11_out.is_dir() and any(f.is_file() for f in d3d11_out.iterdir()):
            logger.info(f"复制已加工 d3d11 文件: {d3d11_out} -> {gimi}")
            for f in d3d11_out.iterdir():
                if f.is_file():
                    _safe_copy(f, gimi / f.name)
        elif d3d11_src.is_file():
            logger.info(f"复制原始 d3d11.dll -> {gimi}")
            _safe_copy(d3d11_src, d3d11_dst)
        else:
            return False, f"找不到 d3d11.dll ({d3d11_src})", d3d11_dst
    except Exception as e:
        return False, f"复制 d3d11.dll 失败: {e}", d3d11_dst

    if not d3d11_dst.is_file():
        return False, "d3d11.dll 未能复制到 GIMI 目录!", d3d11_dst
    return True, "", d3d11_dst


# ---------------------------------------------------------------------------
# Game process launcher — supports Native / Shell / Manual / Custom command
# ---------------------------------------------------------------------------
def _launch_game_process(game_exe: Path, game: Path,
                         launch_method: str = "native",
                         custom_cmd: str = "") -> bool:
    """
    Launch the game process.
    If *custom_cmd* is non-empty, run it via shell instead of game_exe.
    Returns True if launched, False if manual mode (user must start).
    """
    if launch_method == "manual":
        logger.info("手动模式，等待用户启动游戏...")
        return False

    # Custom launch command overrides normal exe launch
    if custom_cmd:
        logger.info(f"自定义启动: {custom_cmd}")
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(custom_cmd, shell=True,
                         creationflags=DETACHED_PROCESS)
        return True

    if launch_method == "shell":
        # Use 3dmloader.dll's StartProcess (wraps ShellExecuteW)
        if not INJECTOR_SRC.is_file():
            logger.warning("3dmloader.dll 不存在，回退到 Native 启动")
        else:
            try:
                lib = ctypes.cdll.LoadLibrary(str(INJECTOR_SRC.resolve()))
                lib.StartProcess.argtypes = (wt.LPCWSTR, wt.LPCWSTR,
                                             wt.LPCWSTR)
                lib.StartProcess.restype = ct.c_int
                result = lib.StartProcess(
                    wt.LPCWSTR(str(game_exe)),
                    wt.LPCWSTR(str(game)),
                    wt.LPCWSTR(""))
                # Free 3dmloader.dll
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.FreeLibrary.argtypes = [wt.HMODULE]
                kernel32.FreeLibrary(lib._handle)
                if result == 0:
                    logger.info(f"ShellExecute 启动游戏: {game_exe}")
                    return True
                else:
                    logger.warning(f"StartProcess 返回 {result}，"
                                   f"回退到 Native")
            except Exception as e:
                logger.warning(f"Shell 启动失败: {e}，回退到 Native")

    # Default: Native (subprocess.Popen)
    logger.info(f"Native 启动游戏: {game_exe}")
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(str(game_exe), cwd=str(game),
                     creationflags=DETACHED_PROCESS)
    return True


# ---------------------------------------------------------------------------
# Inject extra DLL libraries into a running process
# ---------------------------------------------------------------------------
def _inject_extra_dlls(pid: int, dll_paths: list[Path],
                       timeout: int = 15) -> None:
    """Inject each DLL in *dll_paths* into process *pid* via 3dmloader.dll."""
    if not dll_paths:
        return
    if not INJECTOR_SRC.is_file():
        raise FileNotFoundError(f"找不到 {INJECTOR_SRC}")

    lib = ctypes.cdll.LoadLibrary(str(INJECTOR_SRC))
    try:
        lib.Inject.argtypes = (wt.DWORD, wt.LPCWSTR, ct.c_int)
        lib.Inject.restype = ct.c_int
        for dll_path in dll_paths:
            logger.info(f"注入额外库 (PID={pid}): {dll_path}")
            result = lib.Inject(
                wt.DWORD(pid),
                wt.LPCWSTR(str(dll_path.resolve())),
                ct.c_int(timeout))
            if result != 0:
                raise RuntimeError(
                    f"注入 {dll_path.name} 失败 (code={result})")
    finally:
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.FreeLibrary.argtypes = [wt.HMODULE]
            kernel32.FreeLibrary(lib._handle)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Loader-exe method  (run 3DMigoto Loader.exe from GIMI — fallback)
#
# Copies 3DMigoto Loader.exe to GIMI and runs it.  The loader reads d3dx.ini,
# loads d3d11.dll, sets the WH_CBT hook, launches the game, waits for
# injection, then exits.  This is the most reliable method because
# 3DMigoto Loader.exe is a native C++ program that passes d3d11.dll's
# DllMain verify_intended_target() check via directory match.
# ---------------------------------------------------------------------------
def _loader_exe_method(gimi: Path, game: Path, game_exe, target: str,
                       hunting: int, warning: int,
                       launch_method: str = "native",
                       custom_launch_path: str = "",
                       warning_msg: str = "") -> tuple[bool, str]:
    # Copy d3d11.dll to GIMI
    ok, msg, _ = _copy_d3d11_to_gimi(gimi)
    if not ok:
        return False, msg

    # Patch d3dx.ini in GIMI
    gimi_ini = gimi / "d3dx.ini"
    if not gimi_ini.is_file():
        return False, "找不到 d3dx.ini 配置文件!"

    # Launch value: custom path > game exe > empty (manual)
    if custom_launch_path:
        launch_val = custom_launch_path
    elif launch_method != "manual":
        launch_val = str(game_exe)
    else:
        launch_val = ""
    # Always set loader to "3DMigoto Loader.exe" for Loader method
    loader_val = "3DMigoto Loader.exe"
    try:
        patch_d3dx_ini(gimi_ini, target, hunting, warning,
                        launch=launch_val, loader=loader_val)
        logger.info(f"d3dx.ini 已更新: target={target}, hunting={hunting}, "
                    f"show_warnings={warning}, launch={launch_val or '(空)'}, "
                    f"loader={loader_val}")
    except Exception as e:
        logger.warning(f"修改 d3dx.ini 失败: {e}")

    # Check 3DMigoto Loader.exe
    if not LOADER_EXE_SRC.is_file():
        return False, f"找不到 {LOADER_EXE_SRC}"

    # Copy Loader.exe to GIMI
    loader_dst = gimi / "3DMigoto Loader.exe"
    try:
        _safe_copy(LOADER_EXE_SRC, loader_dst)
    except Exception as e:
        logger.warning(f"复制 3DMigoto Loader.exe 失败: {e}")
    if not loader_dst.is_file():
        return False, "3DMigoto Loader.exe 未能复制到 GIMI!"

    # Run from GIMI
    logger.info(f"运行 3DMigoto Loader: {loader_dst}")
    try:
        proc = subprocess.run(
            [str(loader_dst)],
            cwd=str(gimi),
            capture_output=True,
            text=True,
            timeout=60 if launch_method != "manual" else 600,
        )
    except subprocess.TimeoutExpired:
        if launch_method == "manual":
            return False, ("3DMigoto Loader 等待超时（10分钟）。\n"
                           "请确保在启动后及时手动启动游戏，或切换为自动启动模式。")
        return False, "3DMigoto Loader 超时"
    except Exception as e:
        return False, f"启动 3DMigoto Loader 失败: {e}"

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    for line in stdout.splitlines():
        logger.info(f"[Loader] {line}")
    for line in stderr.splitlines():
        logger.warning(f"[Loader] {line}")

    if proc.returncode != 0:
        logger.warning(f"Loader 退出码: {proc.returncode}")
        if stderr:
            return False, f"3DMigoto Loader 出错 (code={proc.returncode})"
        return False, f"3DMigoto Loader 异常退出 (code={proc.returncode})"

    logger.info("3DMigoto Loader 执行完成!")
    success_msg = "游戏已启动!"
    if warning_msg:
        success_msg = f"{success_msg}\n⚠ {warning_msg}"
    return True, success_msg


# ---------------------------------------------------------------------------
# Hook method  (WH_CBT hook via 3dmloader.dll called directly from Python)
#
# KEY INSIGHT: d3d11.dll's DllMain calls verify_intended_target() which checks
# if the current process matches the [Loader] loader= value in d3dx.ini.
# By setting loader = <current_process_name> (e.g. "python.exe" or
# "OMG.exe"), the check passes and HookLibrary succeeds.
# ---------------------------------------------------------------------------
def _hook_method(gimi: Path, game: Path, game_exe, target: str,
                 hunting: int, warning: int,
                 launch_method: str = "native",
                 custom_cmd: str = "",
                 bypass: bool = False,
                 extra_dll_paths: list[Path] | None = None) -> tuple[bool, str]:
    """
    WH_CBT hook injection.  When *bypass* is True, skip d3d11.dll injection
    entirely (only inject *extra_dll_paths*).
    """
    extra_dll_paths = extra_dll_paths or []

    # ---- Bypass mode: no d3d11 injection ----
    if bypass:
        logger.info("Bypass 模式，跳过 d3d11.dll 注入")
        try:
            _launch_game_process(game_exe, game, launch_method,
                                 custom_cmd=custom_cmd)
        except Exception as e:
            return False, f"启动失败: {e}"

        if extra_dll_paths:
            if launch_method == "manual":
                pid = _wait_for_process(target, timeout=300)
            else:
                pid = _wait_for_process(target, timeout=30)
            if pid is None:
                return False, f"等待 {target} 进程超时"
            try:
                _inject_extra_dlls(pid, extra_dll_paths)
                logger.info("额外库注入成功!")
            except Exception as e:
                return False, f"注入额外库失败: {e}"
        return True, "游戏已启动!"

    # ---- Normal Hook mode: d3d11 + optional extra libs ----
    # Copy d3d11.dll to GIMI
    ok, msg, d3d11_path = _copy_d3d11_to_gimi(gimi)
    if not ok:
        return False, msg

    # Patch d3dx.ini in GIMI
    gimi_ini = gimi / "d3dx.ini"
    if not gimi_ini.is_file():
        return False, "找不到 d3dx.ini 配置文件!"

    process_name = os.path.basename(sys.executable)  # e.g. "python.exe"
    # 自定义命令启动时 launch 保持原样（传 None = 不碰该键）。
    # 不能写成空值：3DMigoto 读到空的 launch 会在游戏内左上角报警告。
    if custom_cmd:
        launch_val = None
    else:
        launch_val = str(game_exe) if launch_method != "manual" else None

    try:
        patch_d3dx_ini(gimi_ini, target, hunting, warning,
                        launch=launch_val, loader=process_name)
        logger.info(f"d3dx.ini 已更新: target={target}, loader={process_name}, "
                    f"hunting={hunting}, show_warnings={warning}, "
                    f"launch={launch_val or '(空)'}")
    except Exception as e:
        logger.warning(f"修改 d3dx.ini 失败: {e}")

    # Check 3dmloader.dll
    if not INJECTOR_SRC.is_file():
        return False, f"找不到 {INJECTOR_SRC}"

    # Load 3dmloader.dll and call HookLibrary
    logger.info("加载 3dmloader.dll ...")
    try:
        lib = ctypes.cdll.LoadLibrary(str(INJECTOR_SRC.resolve()))
    except Exception as e:
        return False, f"加载 3dmloader.dll 失败: {e}"

    try:
        lib.HookLibrary.argtypes = (wt.LPCWSTR, ct.POINTER(wt.HHOOK),
                                    ct.POINTER(wt.HANDLE))
        lib.HookLibrary.restype = ct.c_int

        hook = wt.HHOOK()
        mutex = wt.HANDLE()
        result = lib.HookLibrary(
            wt.LPCWSTR(str(d3d11_path.resolve())),
            ct.byref(hook), ct.byref(mutex))

        if result == 100:
            return False, "另一个 3DMigoto Loader 实例正在运行"
        elif result == 200:
            return False, "加载 d3d11.dll 失败 (DllMain 验证未通过)"
        elif result == 300:
            return False, "d3d11.dll 缺少 CBTProc 入口点"
        elif result == 400:
            return False, "设置 Windows Hook 失败"
        elif result != 0:
            return False, f"HookLibrary 未知错误 (code={result})"

        logger.info("Hook 设置成功!")
    except Exception as e:
        return False, f"HookLibrary 调用失败: {e}"

    # Launch game or wait for manual launch
    try:
        _launch_game_process(game_exe, game, launch_method,
                             custom_cmd=custom_cmd)

        # Wait for injection
        logger.info(f"等待 {target} 注入...")
        lib.WaitForInjection.argtypes = (wt.LPCWSTR, wt.LPCWSTR, ct.c_int)
        lib.WaitForInjection.restype = ct.c_int

        timeout = 15 if launch_method != "manual" else 300
        inj_result = lib.WaitForInjection(
            wt.LPCWSTR(str(d3d11_path.resolve())),
            wt.LPCWSTR(target),
            ct.c_int(timeout))

        if inj_result == 0:
            logger.info("d3d11 注入验证成功!")
        else:
            logger.warning("注入验证超时，但游戏可能仍在运行")

        # Inject extra libraries (need target PID)
        if extra_dll_paths:
            pid = _wait_for_process(target, timeout=15)
            if pid is not None:
                try:
                    _inject_extra_dlls(pid, extra_dll_paths)
                    logger.info("额外库注入成功!")
                except Exception as e:
                    logger.warning(f"注入额外库失败: {e}")
            else:
                logger.warning("等待目标进程超时，跳过额外库注入")
    finally:
        # Clean up hook
        try:
            lib.UnhookLibrary.argtypes = (ct.POINTER(wt.HHOOK),
                                          ct.POINTER(wt.HANDLE))
            lib.UnhookLibrary.restype = ct.c_int
            lib.UnhookLibrary(ct.byref(hook), ct.byref(mutex))
        except Exception:
            pass
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.FreeLibrary.argtypes = [wt.HMODULE]
            kernel32.FreeLibrary(lib._handle)
        except Exception:
            pass

    if inj_result != 0:
        return True, "游戏已启动，但注入验证超时，Mod 可能未生效"
    return True, "游戏已启动!"


# ---------------------------------------------------------------------------
# Inject method  (CreateRemoteThread via 3dmloader.dll)
# ---------------------------------------------------------------------------
def _inject_method(gimi: Path, game: Path, game_exe, target: str,
                   hunting: int, warning: int,
                   launch_method: str = "native",
                   custom_cmd: str = "",
                   extra_dll_paths: list[Path] | None = None) -> tuple[bool, str]:
    extra_dll_paths = extra_dll_paths or []

    # Copy d3d11.dll to GIMI (NOT game directory — anti-cheat)
    ok, msg, d3d11_path = _copy_d3d11_to_gimi(gimi)
    if not ok:
        return False, msg

    # Patch d3dx.ini in GIMI
    gimi_ini = gimi / "d3dx.ini"
    if not gimi_ini.is_file():
        return False, "找不到 d3dx.ini 配置文件!"
    try:
        process_name = os.path.basename(sys.executable)
        patch_d3dx_ini(gimi_ini, target, hunting, warning,
                        loader=process_name)
        logger.info(f"GIMI d3dx.ini 已更新: target={target}")
    except Exception as e:
        logger.warning(f"修改 GIMI d3dx.ini 失败: {e}")

    # Clean up any d3d11.dll / d3dx.ini left in game directory
    for fname in ("d3d11.dll", "d3dx.ini"):
        stale = game / fname
        if stale.is_file():
            try:
                stale.unlink()
                logger.info(f"已清理游戏目录残留文件: {stale}")
            except Exception as e:
                logger.warning(f"清理游戏目录文件失败 {stale}: {e}")

    # Check 3dmloader.dll
    if not INJECTOR_SRC.is_file():
        return False, f"找不到 {INJECTOR_SRC}"

    # Launch game or wait for manual launch
    _launch_game_process(game_exe, game, launch_method,
                         custom_cmd=custom_cmd)
    wait_timeout = 30 if launch_method != "manual" else 300

    # Wait for target process
    logger.info(f"等待 {target} 进程...")
    pid = _wait_for_process(target, timeout=wait_timeout)
    if pid is None:
        return False, f"等待 {target} 进程超时"

    logger.info(f"找到 {target} (PID={pid})，开始注入...")

    # Inject d3d11.dll via 3dmloader.dll
    try:
        lib = ctypes.cdll.LoadLibrary(str(INJECTOR_SRC))
    except Exception as e:
        return False, f"加载 3dmloader.dll 失败: {e}"

    try:
        lib.Inject.argtypes = (wt.DWORD, wt.LPCWSTR, ct.c_int)
        lib.Inject.restype = ct.c_int
        result = lib.Inject(pid, wt.LPCWSTR(str(d3d11_path.resolve())),
                            ct.c_int(15))
    except Exception as e:
        return False, f"注入调用失败: {e}"
    finally:
        # Free 3dmloader.dll (matches XXMI-Launcher's finally: injector.unload())
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.FreeLibrary.argtypes = [wt.HMODULE]
            kernel32.FreeLibrary(lib._handle)
        except Exception:
            pass

    if result != 0:
        return False, f"注入失败 (code={result})"

    logger.info("d3d11 注入成功!")

    # Inject extra libraries
    if extra_dll_paths:
        try:
            _inject_extra_dlls(pid, extra_dll_paths)
            logger.info("额外库注入成功!")
        except Exception as e:
            logger.warning(f"注入额外库失败: {e}")

    return True, "游戏已启动!"


def _wait_for_process(name: str, timeout: int = 30) -> int | None:
    """Poll for a process with the given name. Return PID or None."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SNAPPROCESS = 0x2
    INVALID_HANDLE = -1

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = kernel32.CreateToolhelp32Snapshot(SNAPPROCESS, 0)
        if snap == INVALID_HANDLE or snap is None:
            time.sleep(0.5)
            continue

        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        found_pid = None

        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == name.lower():
                    found_pid = pe.th32ProcessID
                    break
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break

        kernel32.CloseHandle(snap)
        if found_pid is not None:
            return found_pid
        time.sleep(0.5)

    return None


# ---------------------------------------------------------------------------
# Public process monitoring helpers (low-power, event-driven)
# ---------------------------------------------------------------------------

def find_process_pid(name: str, max_attempts: int = 10,
                     interval_sec: float = 1.5,
                     stop_event=None) -> int | None:
    """Find a process PID by executable name.  Robust lookup with retry.

    Compared to ``_wait_for_process`` this function accepts an optional
    *stop_event* (``threading.Event``) so the caller can cancel early,
    and uses longer intervals by default to reduce CPU churn.

    Args:
        name: Executable file name (case-insensitive), e.g. "GenshinImpact.exe"
        max_attempts: How many snapshots to take before giving up
        interval_sec: Seconds between snapshots
        stop_event: If provided and set, the search stops immediately

    Returns:
        PID as int, or ``None`` if not found.
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260
    INVALID_HANDLE_VALUE = -1

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    kernel32 = ctypes.windll.kernel32
    target = name.lower()

    for attempt in range(max_attempts):
        if stop_event is not None and stop_event.is_set():
            return None

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or snap is None:
            if attempt < max_attempts - 1:
                time.sleep(interval_sec)
            continue

        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        found_pid = None

        try:
            if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    if pe.szExeFile.lower() == target:
                        found_pid = pe.th32ProcessID
                        break
                    if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(snap)

        if found_pid is not None:
            logger.debug(f"find_process_pid: 找到 {name} PID={found_pid}"
                         f" (尝试 {attempt + 1}/{max_attempts})")
            return found_pid

        if attempt < max_attempts - 1:
            time.sleep(interval_sec)

    logger.debug(f"find_process_pid: 未找到 {name}"
                 f" (已尝试 {max_attempts} 次，间隔 {interval_sec}s)")
    return None


def is_process_running(name: str) -> bool:
    """One-shot synchronous check — does a process with *name* exist right now?

    Uses a single snapshot (low overhead).  Intended for heartbeat /
    safety-net checks, not for polling.
    """
    return find_process_pid(name, max_attempts=1, interval_sec=0.0) is not None


def wait_for_process_exit(pid: int, stop_event=None,
                          wake_interval_ms: int = 500) -> bool:
    """Event-driven wait for a process to exit.  **Near-zero CPU usage**.

    Uses ``WaitForSingleObject`` on the process handle — the OS blocks the
    waiting thread until the process actually exits (or the wake interval
    fires, so we can honour a *stop_event*).

    Args:
        pid: Target process identifier
        stop_event: Optional ``threading.Event`` — when set, return False
        wake_interval_ms: How often to wake and check *stop_event*
                          (500 ms is a good default — responsive yet cheap)

    Returns:
        ``True`` if the process exited, ``False`` if we were cancelled
        via *stop_event* or couldn't open the handle.
    """
    import ctypes

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    INVALID_HANDLE_VALUE = -1

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle or handle == INVALID_HANDLE_VALUE:
        logger.warning(f"wait_for_process_exit: OpenProcess(PID={pid}) 失败")
        return False

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            result = kernel32.WaitForSingleObject(handle, wake_interval_ms)
            if result == WAIT_OBJECT_0:
                logger.info(f"wait_for_process_exit: PID={pid} 已退出")
                return True
            # WAIT_TIMEOUT → loop and check stop_event again
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Main launch function
# ---------------------------------------------------------------------------
def launch_game(gimi_dir: str, game_folder: str,
                hunting: int = 2, warning: int = 0,
                method: str = "loader",
                launch_method: str = "native",
                custom_launch_enabled: bool = False,
                custom_launch_cmd: str = "",
                custom_launch_inject_mode: str = "Hook",
                extra_dll_paths: list[Path] | None = None,
                loader_launch_path: str = "") -> tuple[bool, str]:
    """
    Launch game with 3DMigoto DLL injection.

    method:
        "loader"  – 3DMigoto Loader.exe (WH_CBT hook, default, most reliable)
        "hook"    – WH_CBT hook via 3dmloader.dll (direct Python call)
        "inject"  – CreateRemoteThread via 3dmloader.dll
    launch_method:
        "native"  – subprocess.Popen (default)
        "shell"   – ShellExecute via 3dmloader.dll's StartProcess
        "manual"  – user starts the game manually
    custom_launch_enabled:
        When True, use *custom_launch_cmd* instead of game exe and
        *custom_launch_inject_mode* instead of *method*.
    extra_dll_paths:
        List of additional DLL paths to inject via WriteProcessMemory.

    Returns (success: bool, message: str).
    """
    gimi = Path(gimi_dir)
    game = Path(game_folder)

    if not gimi.is_dir():
        return False, "GIMI 目录不存在!"
    if not game.is_dir():
        return False, "游戏目录不存在!"

    game_exe, target = find_game_exe(game)
    if game_exe is None:
        return False, f"在 {game} 中未找到 .exe 文件!"

    # Determine effective injection method and launch command
    if custom_launch_enabled:
        effective_cmd = custom_launch_cmd.strip()
        effective_mode = custom_launch_inject_mode.lower()
        logger.info(f"自定义启动已启用: cmd={effective_cmd or '(空)'}, "
                    f"inject_mode={custom_launch_inject_mode}")
    else:
        effective_cmd = ""
        effective_mode = method

    logger.info(f"目标进程: {target}, 注入方法: {effective_mode}")

    if effective_mode == "bypass":
        return _hook_method(gimi, game, game_exe, target, hunting, warning,
                            launch_method=launch_method,
                            custom_cmd=effective_cmd,
                            bypass=True,
                            extra_dll_paths=extra_dll_paths)
    elif effective_mode == "hook":
        return _hook_method(gimi, game, game_exe, target, hunting, warning,
                            launch_method=launch_method,
                            custom_cmd=effective_cmd,
                            extra_dll_paths=extra_dll_paths)
    elif effective_mode == "inject":
        return _inject_method(gimi, game, game_exe, target, hunting, warning,
                              launch_method=launch_method,
                              custom_cmd=effective_cmd,
                              extra_dll_paths=extra_dll_paths)
    else:
        # Default: loader (3DMigoto Loader.exe — most reliable)
        # Note: custom launch and extra libs are not supported in loader mode
        if custom_launch_enabled:
            logger.warning("自定义启动和注入库不支持 Loader 模式，"
                           "回退到默认行为")
            return _loader_exe_method(
                gimi, game, game_exe, target, hunting, warning,
                launch_method=launch_method,
                custom_launch_path=loader_launch_path,
                warning_msg="自定义启动和注入库不支持 Loader 模式，已回退到默认启动行为")
        return _loader_exe_method(gimi, game, game_exe, target, hunting,
                                  warning, launch_method=launch_method,
                                  custom_launch_path=loader_launch_path)
