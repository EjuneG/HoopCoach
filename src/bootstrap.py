#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap.py — HoopCoach 壳肉分离引导入口（PyInstaller --onedir 稳定外壳）

核心职责：
1. 作为 HoopCoach.exe 的唯一冻结入口，将 Python 3.12 / Tkinter / Pillow 运行时稳定固化在
   HoopCoach.exe + _internal/ 中；
2. 业务源码平铺在同级 src/ 目录中，日常 99% 的版本更新只需下载 ~0.6 MB 轻量热更包，
   不替换 .exe 本身，极大节省流量并规避杀软拦截；
3. 支持 `--apply-staged-update --wait-pid <pid>` 原子热替换模式：等待旧进程退出后，
   在加载任何业务模块前原地完成备份、覆盖（严格保护用户配置与历史战绩）与失败自动回滚，
   无需任何外部 .bat / PowerShell 脚本即可无缝拉起新版本。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from typing import Iterable

# 显式引入所有业务模块会用到的标准库与第三方核心依赖，确保 PyInstaller 稳定打入 _internal/
import concurrent.futures  # noqa: F401
import difflib  # noqa: F401
import getpass  # noqa: F401
import glob  # noqa: F401
import hashlib  # noqa: F401
import html  # noqa: F401
import http.server  # noqa: F401
import math  # noqa: F401
import mimetypes  # noqa: F401
import platform  # noqa: F401
import re  # noqa: F401
import signal  # noqa: F401
import socket  # noqa: F401
import ssl  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
import webbrowser  # noqa: F401
import zipfile  # noqa: F401

try:
    import tkinter as tk  # noqa: F401
    from tkinter import filedialog, messagebox, ttk  # noqa: F401
except Exception:
    tk = None
    messagebox = None

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: F401
except Exception:
    pass


IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))

STAGING_DIR_NAME = "_update_staging"
BACKUP_DIR_NAME = "_update_backup"
SINGLE_INSTANCE_MUTEX_NAME = "Local\\HoopCoach_SingleInstance_Mutex"
MAIN_WINDOW_TITLE = "🏀 HoopCoach · 星奈育成军师"

_MUTEX_HANDLE = None

# 常见压缩软件未解压直接双击运行时的临时路径特征（小写匹配）
_ARCHIVE_TEMP_PATTERNS = (
    re.compile(r"/temp/rar\$", re.I),
    re.compile(r"/temp/7z[a-z0-9_.-]*", re.I),
    re.compile(r"/temp/temp\d+_", re.I),
    re.compile(r"/temp/wz[0-9a-f]{2,}", re.I),
    re.compile(r"/temp/bnz\.", re.I),
    re.compile(r"/temp/bandizip", re.I),
    re.compile(r"/temp/haozip", re.I),
    re.compile(r"/temp/360zip", re.I),
    re.compile(r"/appdata/local/temp/.*\.zip/", re.I),
    re.compile(r"/appdata/local/temp/.*\.rar/", re.I),
    re.compile(r"/appdata/local/temp/.*\.7z/", re.I),
)


def detect_archive_temp_run(base_dir: str, temp_root: str | None = None) -> tuple[bool, str]:
    """
    检测程序是否位于系统临时解压目录（如 WinRAR / 7-Zip / 资源管理器压缩包内直接双击运行）。
    返回 (is_temp_archive_run, reason)。
    """
    if not base_dir:
        return False, ""
    norm = os.path.abspath(base_dir).replace("\\", "/")
    low = norm.lower()

    for pat in _ARCHIVE_TEMP_PATTERNS:
        if pat.search(low):
            return True, f"路径命中压缩包临时解压目录：{norm}"

    troot = temp_root if temp_root is not None else tempfile.gettempdir()
    if troot:
        t_norm = os.path.abspath(troot).replace("\\", "/").rstrip("/").lower()
        if t_norm and (low == t_norm or low.startswith(t_norm + "/")):
            rel = low[len(t_norm):].lstrip("/")
            first_seg = rel.split("/", 1)[0] if rel else ""
            if any(
                first_seg.startswith(prefix)
                for prefix in ("rar$", "7z", "temp1_", "temp2_", "temp3_", "wz", "bnz.", "bandizip", "haozip", "360zip")
            ):
                return True, f"程序正运行在系统临时目录子文件夹中：{norm}"
    return False, ""


def warn_if_archive_temp_run(base_dir: str) -> bool:
    """
    若检测到压缩包直跑，弹窗提醒用户先完整解压。
    返回 True 表示应中止启动，False 表示继续运行。
    """
    hit, _reason = detect_archive_temp_run(base_dir)
    if not hit:
        return False
    msg = (
        "检测到你似乎直接在压缩包里双击了程序！\n\n"
        "在压缩包临时目录中运行会导致配置、游戏路径记忆和历史战绩在关闭后丢失，且无法正常热更新。\n\n"
        "请先将压缩包完整解压到普通文件夹（如桌面或 D:\\HoopCoach）再双击运行。\n\n"
        "是否立即退出去解压？（强烈推荐选「是」）"
    )
    if messagebox is not None and tk is not None:
        try:
            root = tk.Tk()
            root.withdraw()
            should_exit = messagebox.askyesno(
                "HoopCoach · 请先解压压缩包",
                msg,
                parent=root,
            )
            root.destroy()
            return bool(should_exit)
        except Exception:
            pass
    sys.stderr.write(f"[HoopCoach] {msg}\n")
    return False


def _focus_existing_window_win32(window_title: str = MAIN_WINDOW_TITLE) -> bool:
    """Windows 下查找已有 HoopCoach 主窗口并将其还原置顶。"""
    if not IS_WINDOWS:
        return False
    try:
        u32 = ctypes.windll.user32
        u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u32.FindWindowW.restype = ctypes.c_void_p
        u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.ShowWindow.restype = ctypes.c_int
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u32.SetForegroundWindow.restype = ctypes.c_int

        hwnd = u32.FindWindowW(None, window_title)
        if not hwnd:
            # 模糊匹配包含 HoopCoach 或 星奈育成军师 的顶级窗口
            found = ctypes.c_void_p(0)
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            u32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
            u32.GetWindowTextLengthW.restype = ctypes.c_int
            u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            u32.GetWindowTextW.restype = ctypes.c_int
            u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
            u32.IsWindowVisible.restype = ctypes.c_int

            def _enum_cb(h, _lp):
                if u32.IsWindowVisible(h):
                    n = u32.GetWindowTextLengthW(h)
                    if n > 0:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        u32.GetWindowTextW(h, buf, n + 1)
                        t = buf.value or ""
                        if "HoopCoach" in t or "星奈育成军师" in t:
                            found.value = h
                            return 0
                return 1

            u32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            hwnd = found.value

        if hwnd:
            SW_RESTORE = 9
            u32.ShowWindow(hwnd, SW_RESTORE)
            u32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def acquire_single_instance_mutex(
    mutex_name: str = SINGLE_INSTANCE_MUTEX_NAME,
    window_title: str = MAIN_WINDOW_TITLE,
    focus_existing: bool = True,
) -> tuple[bool, object]:
    """
    Windows 下通过命名 Mutex 保证单实例运行：
    - 首次运行返回 (True, handle)，调用方需保持 handle 存活至进程结束；
    - 已有实例在运行时，自动唤起已有窗口并返回 (False, None)。
    - 非 Windows 平台直接返回 (True, None)。
    """
    global _MUTEX_HANDLE
    if not IS_WINDOWS:
        return True, None
    ERROR_ALREADY_EXISTS = 183
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.GetLastError.argtypes = []
        k32.GetLastError.restype = ctypes.c_uint32
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        handle = k32.CreateMutexW(None, 0, mutex_name)
        last_err = int(k32.GetLastError())
        if not handle:
            return True, None
        if last_err == ERROR_ALREADY_EXISTS:
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass
            if focus_existing:
                _focus_existing_window_win32(window_title)
            return False, None
        _MUTEX_HANDLE = handle
        return True, handle
    except Exception:
        return True, None

# 绝对受保护的用户个人数据路径（相对 BASE_DIR，统一使用 '/' 分隔、小写比较）
PROTECTED_REL_PATHS = (
    "config/hud_config.json",
    "config/local_paths.json",
    "config/button_coords.json",
    "config/lobby_coords.json",
    "config/training_goal.yaml",
    "data/runs",
    "data/note_pool.json",
    "data/note_visual_cache.json",
    "data/equip_pool.json",
    "data/account_snapshot.json",
    "data/daily_queue.json",
    "dashboard/data",
)


def get_base_dir() -> str:
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_protected_relpath(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").strip("/")
    low = norm.lower()
    for p in PROTECTED_REL_PATHS:
        pl = p.lower()
        if low == pl or low.startswith(pl + "/"):
            return True
    return False


def _wait_for_pid_exit(pid: int, timeout_sec: float = 15.0) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    deadline = time.monotonic() + max(0.5, float(timeout_sec))

    if IS_WINDOWS:
        SYNCHRONIZE = 0x00100000
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.WaitForSingleObject.restype = ctypes.c_uint32
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k32.OpenProcess(SYNCHRONIZE, 0, int(pid))
            if handle:
                try:
                    k32.WaitForSingleObject(handle, int(timeout_sec * 1000))
                finally:
                    k32.CloseHandle(handle)
                time.sleep(0.15)
                return
        except Exception:
            pass

    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.12)
    time.sleep(0.1)


def _iter_payload_files(payload_dir: str) -> Iterable[tuple[str, str]]:
    for root, dirs, files in os.walk(payload_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            src_abs = os.path.join(root, fn)
            rel = os.path.relpath(src_abs, payload_dir).replace("\\", "/")
            yield rel, src_abs


def _remove_path_quiet(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except Exception:
        pass


def apply_staged_patch_update(base_dir: str) -> tuple[bool, str]:
    """
    原子应用位于 `<base_dir>/_update_staging/payload/` 的轻量热更包：
    1. 逐文件备份被覆盖的旧文件到 `_update_backup/`；
    2. 严格跳过 `PROTECTED_REL_PATHS`，确保用户配置、游戏目录记忆与历史战绩零损失；
    3. 如更新过程中任一步失败，立即自动回滚已修改文件；
    4. 成功后清理 `_update_staging/` 与 `src/__pycache__/`，避免旧字节码缓存污染。
    """
    staging_dir = os.path.join(base_dir, STAGING_DIR_NAME)
    ready_file = os.path.join(staging_dir, "ready.json")
    payload_dir = os.path.join(staging_dir, "payload")
    if not (os.path.isfile(ready_file) and os.path.isdir(payload_dir)):
        return False, "未找到待应用的暂存更新包"

    try:
        with open(ready_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        return False, f"更新清单损坏：{e}"

    if meta.get("mode") not in (None, "patch"):
        return False, f"暂存包模式不匹配：{meta.get('mode')}"

    backup_dir = os.path.join(base_dir, BACKUP_DIR_NAME)
    _remove_path_quiet(backup_dir)
    os.makedirs(backup_dir, exist_ok=True)

    backed_up: list[str] = []
    created_new: list[str] = []

    try:
        for rel, src_abs in _iter_payload_files(payload_dir):
            if not rel or rel.startswith("..") or os.path.isabs(rel):
                continue
            if is_protected_relpath(rel):
                continue
            dst_abs = os.path.normpath(os.path.join(base_dir, rel))
            if not os.path.abspath(dst_abs).startswith(os.path.abspath(base_dir) + os.sep):
                continue

            if os.path.exists(dst_abs):
                bak_abs = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(bak_abs), exist_ok=True)
                shutil.copy2(dst_abs, bak_abs)
                backed_up.append(rel)
            else:
                created_new.append(rel)

            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
            shutil.copy2(src_abs, dst_abs)

        # 清理旧 .pyc 缓存，防止导入旧字节码
        _remove_path_quiet(os.path.join(base_dir, "src", "__pycache__"))
        _remove_path_quiet(staging_dir)
        _remove_path_quiet(backup_dir)
        ver = str(meta.get("target_version") or "").strip()
        return True, f"已成功热更新至 {ver}" if ver else "已完成热更新"
    except Exception as e:
        # 自动回滚
        for rel in created_new:
            _remove_path_quiet(os.path.join(base_dir, rel))
        for rel in backed_up:
            bak_abs = os.path.join(backup_dir, rel)
            dst_abs = os.path.join(base_dir, rel)
            try:
                if os.path.isfile(bak_abs):
                    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                    shutil.copy2(bak_abs, dst_abs)
            except Exception:
                pass
        _remove_path_quiet(staging_dir)
        return False, f"热更新失败并已自动回滚：{e}"


def _show_fatal_error(base_dir: str, title: str, detail: str) -> None:
    log_path = os.path.join(base_dir, "bootstrap_error.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(detail)
    except Exception:
        pass
    if messagebox is not None and tk is not None:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                title,
                f"{detail[:900]}\n\n完整错误日志已写入：\n{log_path}",
                parent=root,
            )
            root.destroy()
            return
        except Exception:
            pass
    sys.stderr.write(f"{title}\n{detail}\n")


def _cleanup_stale_backup(base_dir: str) -> None:
    bak = os.path.join(base_dir, BACKUP_DIR_NAME)
    if os.path.isdir(bak):
        _remove_path_quiet(bak)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--apply-staged-update", action="store_true")
    parser.add_argument("--wait-pid", type=int, default=0)
    args, _unknown = parser.parse_known_args(argv)

    base_dir = get_base_dir()

    if args.apply_staged_update:
        if args.wait_pid > 0:
            _wait_for_pid_exit(args.wait_pid, timeout_sec=15.0)
        ok, msg = apply_staged_patch_update(base_dir)
        if not ok:
            _show_fatal_error(base_dir, "HoopCoach 热更新异常", msg)
    else:
        # 压缩包内直跑防呆（仅正常启动时检测，避免误伤热更重启）
        if warn_if_archive_temp_run(base_dir):
            sys.exit(0)
        # 若存在上次遗留的就绪热更包（仅在冻结态安全应用，不触碰源码开发仓）
        ready_file = os.path.join(base_dir, STAGING_DIR_NAME, "ready.json")
        if IS_FROZEN and os.path.isfile(ready_file):
            apply_staged_patch_update(base_dir)
        else:
            _cleanup_stale_backup(base_dir)

    # 在旧热更父进程退出并完成文件覆盖后，再申请单实例互斥锁（防热更重启自撞）
    acquired, _mutex_handle = acquire_single_instance_mutex()
    if not acquired:
        sys.exit(0)

    src_dir = os.path.join(base_dir, "src")
    for p in (base_dir, src_dir):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    try:
        import win_launcher

        win_launcher.main(skip_startup_guards=True)
    except Exception:
        _show_fatal_error(
            base_dir,
            "HoopCoach 启动失败",
            traceback.format_exc(),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
