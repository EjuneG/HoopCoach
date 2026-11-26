#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
updater.py — HoopCoach 轻量热更新引擎（纯标准库 · 零偷跑流量 · 国内多线路自动回退）

核心机制：
1. 极省流量（两层清单 + 壳肉分离）：
   - 启动检查仅拉取 ~1.5 KB 的 `manifest.json`（走 `releases/latest/download/`，不消耗 GitHub API 限额）；
   - 日常 99% 版本更新只需下载 ~0.6 MB 的 `HoopCoach-Patch-<ver>.zip`（纯代码/前端/策略/静态 JSON），
     完全不重下 25 MB Python 运行时壳与 10 MB 历史头像/图标；
   - 游戏新增角色或器材图标时，仅比对 `assets_manifest.json` 并按需补拉本地缺失的那几张 PNG（每张 ~30 KB）；
   - 仅当 `runtime_version` 升级（底层 Python/依赖变更）时才引导下载整包。
2. 零偷跑流量：
   - 永不在后台静默下载更新包，发现新版后展示精确待下载字节数，由用户点击确认后才开始下载。
3. 国内直连 + 梯子双稳：
   - 自动继承 Windows 系统代理；按序尝试 `custom_mirrors` -> GitHub 直连/代理 -> 国内公开加速镜像，
     并记住本轮最快的可用线路。
4. 个人数据 100% 零丢失：
   - 双重校验受保护路径（`hud_config.json`、`local_paths.json`、`data/runs/` 等），更新前后绝不覆写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Optional

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

import platform_compat as pc

APP_META_FILE = os.path.join(pc.CONFIG_DIR, "app_meta.json")
ASSETS_STATE_FILE = os.path.join(pc.CONFIG_DIR, "assets_state.json")

STAGING_DIR_NAME = "_update_staging"
DEFAULT_RUNTIME_VERSION = "rt1"
USER_AGENT = "HoopCoach-Updater/1.0"

# 国内直连兜底公开 GitHub 加速镜像（仅在直连/系统代理超时或失败时按序回退）
DEFAULT_MIRROR_PREFIXES: tuple[str, ...] = (
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
)

# 绝对禁止热更新触碰的个人配置文件与运行数据目录
PROTECTED_REL_PATHS: tuple[str, ...] = (
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

# 记住本次会话最近一次连通成功的镜像前缀（"" 表示直连/系统代理）
_LAST_GOOD_PREFIX: Optional[str] = None
_PREFIX_LOCK = threading.Lock()


@dataclass
class AssetDownloadItem:
    rel_path: str
    size: int
    sha256: str
    url: str


@dataclass
class UpdateCheckResult:
    ok: bool = False
    has_update: bool = False
    mode: str = "none"  # "none" | "patch" | "full"
    current_version: str = "v0.10.1"
    latest_version: str = ""
    current_runtime: str = DEFAULT_RUNTIME_VERSION
    latest_runtime: str = DEFAULT_RUNTIME_VERSION
    notes: str = ""
    published_at: str = ""
    release_page_url: str = ""
    source_label: str = ""
    patch_info: dict = field(default_factory=dict)
    full_info: dict = field(default_factory=dict)
    assets_hash: str = ""
    missing_assets: list[AssetDownloadItem] = field(default_factory=list)
    patch_bytes: int = 0
    missing_assets_bytes: int = 0
    total_download_bytes: int = 0
    error: str = ""


def load_app_meta(base_dir: Optional[str] = None) -> dict:
    """读取 config/app_meta.json（版本号、壳版本、公开仓库与自定义镜像）。"""
    meta = {
        "app_name": "HoopCoach",
        "version": "v0.10.1",
        "runtime_version": DEFAULT_RUNTIME_VERSION,
        "github_repo": "EjuneG/HoopCoach",
        "custom_mirrors": [],
    }
    meta_file = (
        os.path.join(base_dir, "config", "app_meta.json")
        if base_dir
        else APP_META_FILE
    )
    if os.path.isfile(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in ("app_name", "version", "runtime_version", "github_repo"):
                    if isinstance(disk.get(k), str) and disk[k].strip():
                        meta[k] = disk[k].strip()
                cm = disk.get("custom_mirrors")
                if isinstance(cm, list):
                    meta["custom_mirrors"] = [
                        str(x).strip() for x in cm if isinstance(x, str) and str(x).strip()
                    ]
        except Exception:
            pass
    return meta


def format_bytes(num_bytes: int) -> str:
    n = max(0, int(num_bytes or 0))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def parse_version(ver: str) -> tuple[tuple[int, ...], int, tuple]:
    """
    解析 SemVer 版本字符串为可排序元组：
    - `v0.10.2` > `v0.10.2-beta.1` > `v0.10.1`
    - 兼容非标准日期串兜底（至少提取数字序列）
    """
    s = (ver or "").strip()
    if s.lower().startswith("v"):
        s = s[1:]
    if not s:
        return ((0, 0, 0), 1, ())
    core, sep, pre = s.partition("-")
    nums = [int(x) for x in re.findall(r"\d+", core)]
    while len(nums) < 3:
        nums.append(0)
    is_stable = 1 if not sep else 0
    pre_parts: list = []
    if pre:
        for tok in re.split(r"[.\-_]+", pre):
            if tok.isdigit():
                pre_parts.append((0, int(tok)))
            elif tok:
                pre_parts.append((1, tok.lower()))
    return (tuple(nums), is_stable, tuple(pre_parts))


def is_newer_version(remote_ver: str, local_ver: str) -> bool:
    if not remote_ver or not remote_ver.strip():
        return False
    return parse_version(remote_ver) > parse_version(local_ver)


def is_protected_relpath(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").strip("/")
    low = norm.lower()
    for p in PROTECTED_REL_PATHS:
        pl = p.lower()
        if low == pl or low.startswith(pl + "/"):
            return True
    return False


def _format_mirror_url(prefix: str, raw_url: str) -> str:
    if not prefix:
        return raw_url
    p = prefix.strip()
    if "{url}" in p:
        return p.replace("{url}", raw_url)
    if not p.endswith("/"):
        p += "/"
    return p + raw_url


def _label_for_prefix(prefix: str) -> str:
    if not prefix:
        return "GitHub 直连/系统代理"
    parsed = urllib.parse.urlparse(prefix.replace("{url}", ""))
    host = parsed.netloc or prefix
    return f"镜像({host})"


def build_candidate_urls(
    raw_url: str,
    custom_mirrors: Optional[list[str]] = None,
    include_public_mirrors: bool = True,
) -> list[tuple[str, str, str]]:
    """
    生成有序候选线路列表：`[(prefix, url, label), ...]`。
    优先级：上次已验证连通线路 -> 用户自定义镜像 -> GitHub 直连/代理 -> 公开国内加速镜像。
    """
    prefixes: list[str] = []
    with _PREFIX_LOCK:
        last_good = _LAST_GOOD_PREFIX

    if last_good is not None:
        prefixes.append(last_good)

    for m in custom_mirrors or []:
        if m and m not in prefixes:
            prefixes.append(m)

    if "" not in prefixes:
        prefixes.append("")

    if include_public_mirrors and raw_url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")):
        for m in DEFAULT_MIRROR_PREFIXES:
            if m not in prefixes:
                prefixes.append(m)

    return [(p, _format_mirror_url(p, raw_url), _label_for_prefix(p)) for p in prefixes]


def _remember_good_prefix(prefix: str) -> None:
    global _LAST_GOOD_PREFIX
    with _PREFIX_LOCK:
        _LAST_GOOD_PREFIX = prefix


def _http_fetch_bytes(
    raw_url: str,
    custom_mirrors: Optional[list[str]] = None,
    timeout: float = 5.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> tuple[bytes, str]:
    """按多线路自动回退拉取小文件（manifest / assets_manifest）。"""
    errors: list[str] = []
    candidates = build_candidate_urls(raw_url, custom_mirrors=custom_mirrors)
    for prefix, url, label in candidates:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, */*",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if status and int(status) >= 400:
                    raise urllib.error.HTTPError(url, int(status), f"HTTP {status}", resp.headers, None)
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"响应体积超出限制 ({max_bytes} B)")
                _remember_good_prefix(prefix)
                return data, label
        except Exception as e:
            errors.append(f"{label}: {e}")
    raise RuntimeError("所有更新线路均连接失败：" + " | ".join(errors[:3]))


def _http_download_file(
    raw_url: str,
    dst_path: str,
    expected_sha256: str = "",
    custom_mirrors: Optional[list[str]] = None,
    timeout: float = 12.0,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """按多线路下载文件至 dst_path，支持实时进度、取消与 SHA256 严格校验。"""
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    candidates = build_candidate_urls(raw_url, custom_mirrors=custom_mirrors)
    errors: list[str] = []

    for prefix, url, label in candidates:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消下载")
        tmp_path = dst_path + ".part"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Cache-Control": "no-cache",
            },
        )
        try:
            hasher = hashlib.sha256()
            downloaded = 0
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200)
                if status and int(status) >= 400:
                    raise urllib.error.HTTPError(url, int(status), f"HTTP {status}", resp.headers, None)
                total_hdr = resp.headers.get("Content-Length")
                total = int(total_hdr) if (total_hdr and total_hdr.isdigit()) else 0
                with open(tmp_path, "wb") as out_f:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("已取消下载")
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if progress_cb is not None:
                            progress_cb(downloaded, total, label)

            actual_sha = hasher.hexdigest().lower()
            if expected_sha256 and actual_sha != expected_sha256.strip().lower():
                raise ValueError(f"SHA256 校验失败（期望 {expected_sha256[:12]}，实际 {actual_sha[:12]}）")

            os.replace(tmp_path, dst_path)
            _remember_good_prefix(prefix)
            return label
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("已取消下载")
            errors.append(f"{label}: {e}")

    raise RuntimeError("下载失败：" + " | ".join(errors[:3]))


def compute_file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def _load_local_assets_hash(base_dir: str) -> str:
    p = os.path.join(base_dir, "config", "assets_state.json")
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return str(d.get("assets_hash") or "").strip()
    except Exception:
        pass
    return ""


def compute_missing_assets(
    base_dir: str,
    assets_manifest: dict,
    repo: str,
    tag: str,
) -> list[AssetDownloadItem]:
    """
    比对本地 `data/hoopgirls/` 与远端 `assets_manifest.json`，
    仅挑出缺失或体积/哈希变化的图片文件，绝不重复下载老图。
    """
    files_map = assets_manifest.get("files") if isinstance(assets_manifest, dict) else None
    if not isinstance(files_map, dict):
        return []
    raw_base = (
        str(assets_manifest.get("raw_base_url") or "").strip().rstrip("/")
        or f"https://raw.githubusercontent.com/{repo}/{tag}"
    )
    missing: list[AssetDownloadItem] = []
    for rel_path, info in sorted(files_map.items()):
        if not isinstance(info, dict):
            continue
        norm = str(rel_path).replace("\\", "/").strip("/")
        if not norm.startswith("data/hoopgirls/") or ".." in norm.split("/"):
            continue
        exp_sha = str(info.get("sha256") or "").strip().lower()
        exp_size = int(info.get("size") or 0)
        local_abs = os.path.join(base_dir, norm)
        need = False
        if not os.path.isfile(local_abs):
            need = True
        elif exp_size > 0 and os.path.getsize(local_abs) != exp_size:
            need = True
        elif exp_sha:
            try:
                if compute_file_sha256(local_abs) != exp_sha:
                    need = True
            except Exception:
                need = True
        if need:
            url = str(info.get("url") or "").strip() or f"{raw_base}/{urllib.parse.quote(norm, safe='/')}"
            missing.append(AssetDownloadItem(rel_path=norm, size=exp_size, sha256=exp_sha, url=url))
    return missing


def check_for_updates(
    meta: Optional[dict] = None,
    base_dir: Optional[str] = None,
    timeout: float = 5.0,
) -> UpdateCheckResult:
    """
    拉取远端 `manifest.json`（~1.5 KB）并精确计算本次是否需要更新、更新模式与精确下载体积。
    永不下载任何更新压缩包。
    """
    bdir = base_dir or pc.BASE_DIR
    m = meta or load_app_meta(bdir)
    repo = str(m.get("github_repo") or "").strip().strip("/")
    cur_ver = str(m.get("version") or "v0.10.1").strip()
    cur_rt = str(m.get("runtime_version") or DEFAULT_RUNTIME_VERSION).strip()
    custom_mirrors = list(m.get("custom_mirrors") or [])

    res = UpdateCheckResult(
        current_version=cur_ver,
        current_runtime=cur_rt,
        release_page_url=f"https://github.com/{repo}/releases/latest" if repo else "",
    )
    if not repo or "/" not in repo:
        res.error = "未配置有效的公开仓库地址（config/app_meta.json -> github_repo）"
        return res

    manifest_url = f"https://github.com/{repo}/releases/latest/download/manifest.json"
    try:
        raw_bytes, src_label = _http_fetch_bytes(
            manifest_url,
            custom_mirrors=custom_mirrors,
            timeout=timeout,
            max_bytes=512 * 1024,
        )
        manifest = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 格式无效")
    except Exception as e:
        res.error = str(e)
        return res

    res.ok = True
    res.source_label = src_label
    remote_ver = str(manifest.get("version") or "").strip()
    remote_rt = str(manifest.get("runtime_version") or DEFAULT_RUNTIME_VERSION).strip()
    res.latest_version = remote_ver or cur_ver
    res.latest_runtime = remote_rt
    res.notes = str(manifest.get("notes") or "").strip()
    res.published_at = str(manifest.get("published_at") or "").strip()
    res.patch_info = manifest.get("patch") if isinstance(manifest.get("patch"), dict) else {}
    res.full_info = manifest.get("full") if isinstance(manifest.get("full"), dict) else {}
    res.assets_hash = str(manifest.get("assets_hash") or "").strip()
    if remote_ver:
        res.release_page_url = f"https://github.com/{repo}/releases/tag/{remote_ver}"

    if not is_newer_version(remote_ver, cur_ver):
        res.has_update = False
        res.mode = "none"
        return res

    res.has_update = True

    # 判断走轻量热更（~0.6 MB）还是整包升级（壳版本升级时）
    can_patch = (remote_rt == cur_rt) and bool(res.patch_info.get("url"))
    if can_patch:
        res.mode = "patch"
        res.patch_bytes = int(res.patch_info.get("size") or 0)

        # 仅当远端图片资源总哈希与本地不同时，才拉取 assets_manifest.json 检查增量缺图
        assets_info = manifest.get("assets_manifest") if isinstance(manifest.get("assets_manifest"), dict) else {}
        local_assets_hash = _load_local_assets_hash(bdir)
        if res.assets_hash and res.assets_hash != local_assets_hash and assets_info.get("url"):
            try:
                am_bytes, _ = _http_fetch_bytes(
                    str(assets_info["url"]),
                    custom_mirrors=custom_mirrors,
                    timeout=timeout,
                    max_bytes=2 * 1024 * 1024,
                )
                exp_am_sha = str(assets_info.get("sha256") or "").strip().lower()
                if exp_am_sha and hashlib.sha256(am_bytes).hexdigest().lower() != exp_am_sha:
                    raise ValueError("assets_manifest.json 校验失败")
                am_data = json.loads(am_bytes.decode("utf-8"))
                res.missing_assets = compute_missing_assets(bdir, am_data, repo, remote_ver)
                res.missing_assets_bytes = sum(a.size for a in res.missing_assets)
            except Exception:
                # 图片清单偶发失败不阻塞主代码热更新
                res.missing_assets = []
                res.missing_assets_bytes = 0

        res.total_download_bytes = res.patch_bytes + res.missing_assets_bytes
    else:
        res.mode = "full"
        res.total_download_bytes = int(res.full_info.get("size") or 0)

    return res


def _safe_extract_zip(zip_path: str, dst_dir: str, strip_top_folder: str = "") -> int:
    """
    安全解压 zip（防 Zip Slip 路径穿越，并自动过滤任何受保护用户文件路径）。
    当 strip_top_folder 非空（例如整包内顶层目录为 `HoopCoach/`）时自动剥离该前缀。
    """
    os.makedirs(dst_dir, exist_ok=True)
    abs_dst = os.path.abspath(dst_dir)
    extracted = 0
    prefix = strip_top_folder.strip("/") + "/" if strip_top_folder else ""

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            raw_name = member.filename.replace("\\", "/").lstrip("/")
            if prefix and raw_name.startswith(prefix):
                raw_name = raw_name[len(prefix):]
            if not raw_name or ".." in raw_name.split("/"):
                raise ValueError(f"检测到非法压缩包路径：{member.filename}")
            if is_protected_relpath(raw_name):
                continue
            target = os.path.abspath(os.path.join(abs_dst, raw_name))
            if not target.startswith(abs_dst + os.sep):
                raise ValueError(f"检测到越界路径：{member.filename}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member, "r") as src_f, open(target, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
            extracted += 1
    return extracted


def stage_update(
    check_res: UpdateCheckResult,
    base_dir: Optional[str] = None,
    meta: Optional[dict] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    """
    下载、校验并将更新内容安全解压至 `<base_dir>/_update_staging/payload/`，
    最后写入 `ready.json` 供重启原子应用。返回 staging 目录路径。
    """
    if not check_res.has_update or check_res.mode not in ("patch", "full"):
        raise ValueError("当前没有可下载的更新")

    bdir = base_dir or pc.BASE_DIR
    m = meta or load_app_meta(bdir)
    custom_mirrors = list(m.get("custom_mirrors") or [])

    staging_dir = os.path.join(bdir, STAGING_DIR_NAME)
    payload_dir = os.path.join(staging_dir, "payload")
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(payload_dir, exist_ok=True)

    total_est = max(1, int(check_res.total_download_bytes or 0))
    zip_path = os.path.join(staging_dir, "package.zip")

    if check_res.mode == "patch":
        pkg_info = check_res.patch_info
        url = str(pkg_info.get("url") or "").strip()
        sha = str(pkg_info.get("sha256") or "").strip()
        if not url:
            raise ValueError("manifest 缺少 patch 下载地址")

        def _patch_prog(done: int, _t: int, lbl: str) -> None:
            if progress_cb is not None:
                progress_cb(min(done, total_est), total_est, f"正在下载热更包 · {lbl}")

        _http_download_file(
            url,
            zip_path,
            expected_sha256=sha,
            custom_mirrors=custom_mirrors,
            progress_cb=_patch_prog,
            cancel_event=cancel_event,
        )
        _safe_extract_zip(zip_path, payload_dir)

        # 按需增量补拉缺失的新增角色/器材图片
        base_done = max(check_res.patch_bytes, os.path.getsize(zip_path))
        assets_done_acc = 0
        for idx, item in enumerate(check_res.missing_assets, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("已取消下载")
            dst_img = os.path.join(payload_dir, item.rel_path)

            def _img_prog(done: int, _t: int, lbl: str, _idx=idx, _acc=assets_done_acc) -> None:
                if progress_cb is not None:
                    progress_cb(
                        min(base_done + _acc + done, total_est),
                        total_est,
                        f"补拉新增图标 ({_idx}/{len(check_res.missing_assets)}) · {lbl}",
                    )

            _http_download_file(
                item.url,
                dst_img,
                expected_sha256=item.sha256,
                custom_mirrors=custom_mirrors,
                progress_cb=_img_prog,
                cancel_event=cancel_event,
            )
            assets_done_acc += max(item.size, os.path.getsize(dst_img))

        # 写入 assets_state.json，避免下次启动重复检查同一批图片
        if check_res.assets_hash:
            state_dst = os.path.join(payload_dir, "config", "assets_state.json")
            os.makedirs(os.path.dirname(state_dst), exist_ok=True)
            with open(state_dst, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "assets_hash": check_res.assets_hash,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
                f.write("\n")
    else:
        pkg_info = check_res.full_info
        url = str(pkg_info.get("url") or "").strip()
        sha = str(pkg_info.get("sha256") or "").strip()
        if not url:
            raise ValueError("manifest 缺少整包下载地址")

        def _full_prog(done: int, t: int, lbl: str) -> None:
            if progress_cb is not None:
                progress_cb(done, max(t, total_est), f"正在下载完整更新包 · {lbl}")

        _http_download_file(
            url,
            zip_path,
            expected_sha256=sha,
            custom_mirrors=custom_mirrors,
            timeout=25.0,
            progress_cb=_full_prog,
            cancel_event=cancel_event,
        )
        # 整包压缩结构通常为 HoopCoach/...
        top_prefix = ""
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = [n.replace("\\", "/") for n in zf.namelist() if n and not n.endswith("/")]
            if names and all(n.startswith("HoopCoach/") for n in names):
                top_prefix = "HoopCoach"
        _safe_extract_zip(zip_path, payload_dir, strip_top_folder=top_prefix)

    # 删掉中间压缩包省空间，仅保留校验通过的解压载荷
    try:
        os.remove(zip_path)
    except Exception:
        pass

    ready_payload = {
        "mode": check_res.mode,
        "target_version": check_res.latest_version,
        "runtime_version": check_res.latest_runtime,
        "staged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(staging_dir, "ready.json"), "w", encoding="utf-8") as f:
        json.dump(ready_payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    if progress_cb is not None:
        progress_cb(total_est, total_est, "校验通过，准备重启应用更新...")

    return staging_dir


def _build_windows_full_update_script(base_dir: str, payload_dir: str, exe_path: str, pid: int) -> str:
    """仅在罕见的底层壳版本升级（mode == 'full'）时生成隐藏过渡脚本。"""
    ps_script = os.path.join(tempfile.gettempdir(), f"hoopcoach_full_update_{pid}.ps1")
    protected_ps = ", ".join(f"'{p.lower()}'" for p in PROTECTED_REL_PATHS)
    content = f"""
$ErrorActionPreference = 'Stop'
$targetPid = {int(pid)}
$baseDir = '{base_dir.replace("'", "''")}'
$payloadDir = '{payload_dir.replace("'", "''")}'
$exePath = '{exe_path.replace("'", "''")}'
$protected = @({protected_ps})

for ($i = 0; $i -lt 120; $i++) {{
    if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 250
}}
Start-Sleep -Milliseconds 350

Get-ChildItem -Path $payloadDir -Recurse -File | ForEach-Object {{
    $rel = $_.FullName.Substring($payloadDir.Length).TrimStart('\\', '/').Replace('\\', '/')
    $low = $rel.ToLowerInvariant()
    $skip = $false
    foreach ($p in $protected) {{
        if ($low -eq $p -or $low.StartsWith($p + '/')) {{ $skip = $true; break }}
    }}
    if (-not $skip) {{
        $dst = Join-Path $baseDir $rel
        $dstDir = Split-Path -Parent $dst
        if (-not (Test-Path $dstDir)) {{ New-Item -ItemType Directory -Force -Path $dstDir | Out-Null }}
        Copy-Item -Path $_.FullName -Destination $dst -Force
    }}
}}

Remove-Item -Path (Join-Path $baseDir '{STAGING_DIR_NAME}') -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $exePath) {{
    Start-Process -FilePath $exePath -WorkingDirectory $baseDir
}}
Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""
    with open(ps_script, "w", encoding="utf-8-sig") as f:
        f.write(content.strip() + "\n")
    return ps_script


def launch_apply_and_restart(mode: str = "patch", base_dir: Optional[str] = None) -> tuple[bool, str]:
    """
    触发暂存更新的原子应用与自动重启：
    - `patch` 模式：由 `HoopCoach.exe --apply-staged-update --wait-pid <pid>` 直接在自身进程完成，0 个外部脚本；
    - `full` 模式（罕见壳升级）：启动隐藏 PowerShell 完成 exe/_internal 覆盖后重启。
    """
    bdir = base_dir or pc.BASE_DIR
    if not pc.IS_FROZEN:
        return False, "当前为开发者源码运行模式，暂不自动覆盖本地代码仓，请使用 git pull 更新。"

    pid = os.getpid()
    if mode == "patch":
        cmd = [sys.executable, "--apply-staged-update", "--wait-pid", str(pid)]
        kwargs: dict = {"cwd": bdir, "close_fds": True}
        if pc.IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, **kwargs)
        return True, "正在无缝热更新并重启..."

    if mode == "full" and pc.IS_WINDOWS:
        payload_dir = os.path.join(bdir, STAGING_DIR_NAME, "payload")
        ps_path = _build_windows_full_update_script(bdir, payload_dir, sys.executable, pid)
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            ps_path,
        ]
        subprocess.Popen(
            cmd,
            cwd=tempfile.gettempdir(),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True, "正在应用完整版本更新并重启..."

    return False, "当前平台不支持自动覆盖完整运行库，请前往 Release 页面手动下载。"


# ----------------------------------------------------------------------
# Tkinter 更新对话框（与桌面小控制台深蓝卡片风统一）
# ----------------------------------------------------------------------

def open_update_dialog(
    parent,
    check_res: UpdateCheckResult,
    on_restart_required: Optional[Callable[[], None]] = None,
) -> None:
    """弹出清爽的更新详情与一键更新进度窗口。"""
    if tk is None or ttk is None or messagebox is None:
        return

    win = tk.Toplevel(parent)
    win.title("🏀 HoopCoach · 版本更新")
    win.geometry("460x410")
    win.minsize(430, 380)
    win.configure(bg="#0f172a")
    win.transient(parent)
    win.grab_set()

    cancel_event = threading.Event()
    downloading = {"active": False}

    # 顶栏
    hdr = tk.Frame(win, bg="#0f172a")
    hdr.pack(fill="x", padx=16, pady=(14, 8))
    title_text = (
        f"✨ 发现新版本 {check_res.latest_version}"
        if check_res.has_update
        else f"✅ 当前已是最新版本（{check_res.current_version}）"
    )
    tk.Label(
        hdr,
        text=title_text,
        bg="#0f172a",
        fg="#f8fafc",
        font=("Microsoft YaHei UI", 12, "bold"),
        anchor="w",
    ).pack(side="left")

    if check_res.has_update:
        badge_txt = "⚡ 轻量热更" if check_res.mode == "patch" else "📦 完整更新"
        badge_bg = "#14532d" if check_res.mode == "patch" else "#78350f"
        badge_fg = "#86efac" if check_res.mode == "patch" else "#fde68a"
        tk.Label(
            hdr,
            text=badge_txt,
            bg=badge_bg,
            fg=badge_fg,
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            pady=3,
        ).pack(side="right")

    # 版本与流量卡片
    info_card = tk.Frame(win, bg="#1e293b", highlightbackground="#334155", highlightthickness=1)
    info_card.pack(fill="x", padx=16, pady=(0, 10))

    if check_res.has_update:
        size_line = f"📦 本次需下载：{format_bytes(check_res.total_download_bytes)}"
        if check_res.mode == "patch":
            if check_res.missing_assets:
                size_line += (
                    f"（代码与数据 {format_bytes(check_res.patch_bytes)}"
                    f" + {len(check_res.missing_assets)} 张新图 {format_bytes(check_res.missing_assets_bytes)}）"
                )
            else:
                size_line += "（纯代码与数据，免重下程序壳与老图片）"
        else:
            size_line += "（包含底层运行库升级）"

        summary_txt = (
            f"当前版本：{check_res.current_version}   →   最新版本：{check_res.latest_version}\n"
            f"{size_line}\n"
            f"🛡️ 个人保护：游戏目录记忆、偏科配置与历史战绩 100% 原样保留"
        )
    else:
        summary_txt = (
            f"当前版本：{check_res.current_version}（运行库 {check_res.current_runtime}）\n"
            f"连接线路：{check_res.source_label or '已校验'}\n"
            f"无需更新，放心使用即可～"
        )

    tk.Label(
        info_card,
        text=summary_txt,
        bg="#1e293b",
        fg="#e2e8f0",
        font=("Microsoft YaHei UI", 9),
        justify="left",
        anchor="w",
        padx=12,
        pady=10,
        wraplength=405,
    ).pack(fill="x")

    # 更新说明
    notes_lbl = tk.Label(
        win,
        text="📝 更新内容",
        bg="#0f172a",
        fg="#fbbf24",
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    )
    notes_lbl.pack(fill="x", padx=16, pady=(0, 4))

    notes_box = tk.Text(
        win,
        bg="#1e293b",
        fg="#cbd5e1",
        font=("Microsoft YaHei UI", 9),
        relief="flat",
        highlightbackground="#334155",
        highlightthickness=1,
        height=7,
        wrap="word",
        padx=10,
        pady=8,
    )
    notes_box.pack(fill="both", expand=True, padx=16, pady=(0, 10))
    notes_box.insert("1.0", check_res.notes or "（本次发布未填写详细更新说明）")
    notes_box.configure(state="disabled")

    # 进度区
    var_prog_text = tk.StringVar(
        value="提示：点击下方按钮才开始下载，绝不后台偷跑流量。" if check_res.has_update else ""
    )
    tk.Label(
        win,
        textvariable=var_prog_text,
        bg="#0f172a",
        fg="#7dd3fc",
        font=("Microsoft YaHei UI", 8),
        anchor="w",
        wraplength=420,
    ).pack(fill="x", padx=16, pady=(0, 4))

    pbar = ttk.Progressbar(win, mode="determinate", maximum=100)
    pbar.pack(fill="x", padx=16, pady=(0, 12))

    # 底部按钮
    btn_row = tk.Frame(win, bg="#0f172a")
    btn_row.pack(fill="x", padx=16, pady=(0, 14))

    def _on_close_dialog() -> None:
        if downloading["active"]:
            cancel_event.set()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close_dialog)

    if check_res.has_update:
        btn_update = tk.Button(
            btn_row,
            text=f"⬇️ 立即更新并重启（{format_bytes(check_res.total_download_bytes)}）",
            bg="#15803d",
            fg="#ffffff",
            activebackground="#166534",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=12,
            pady=7,
            cursor="hand2",
        )
        btn_update.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def _start_download() -> None:
            if downloading["active"]:
                return
            if not pc.IS_FROZEN:
                messagebox.showinfo(
                    "源码开发模式提示",
                    "当前是通过 Python 源码直接运行的，不自动覆盖本地代码目录。\n"
                    "请使用 git pull 更新，或点击右侧按钮在浏览器查看 Release 页面。",
                    parent=win,
                )
                return
            downloading["active"] = True
            cancel_event.clear()
            btn_update.configure(state="disabled", text="⏳ 下载校验中...")
            var_prog_text.set("⏳ 正在连接最快线路...")

            def _prog(done: int, total: int, msg: str) -> None:
                pct = min(100.0, (done / max(1, total)) * 100.0)
                txt = f"{msg}  ({format_bytes(done)} / {format_bytes(total)})"
                try:
                    win.after(0, lambda p=pct, t=txt: (pbar.configure(value=p), var_prog_text.set(t)))
                except Exception:
                    pass

            def _worker() -> None:
                try:
                    stage_update(
                        check_res,
                        progress_cb=_prog,
                        cancel_event=cancel_event,
                    )
                    ok, msg = launch_apply_and_restart(mode=check_res.mode)
                    if not ok:
                        raise RuntimeError(msg)

                    def _finish() -> None:
                        var_prog_text.set("✅ 校验通过，正在重启应用更新...")
                        try:
                            win.destroy()
                        except Exception:
                            pass
                        if on_restart_required is not None:
                            on_restart_required()

                    win.after(0, _finish)
                except Exception as e:
                    err_msg = str(e)

                    def _fail() -> None:
                        downloading["active"] = False
                        btn_update.configure(
                            state="normal",
                            text=f"🔁 重试更新（{format_bytes(check_res.total_download_bytes)}）",
                        )
                        pbar.configure(value=0)
                        var_prog_text.set(f"⚠️ {err_msg}")

                    try:
                        win.after(0, _fail)
                    except Exception:
                        pass

            threading.Thread(target=_worker, daemon=True).start()

        btn_update.configure(command=_start_download)

    if check_res.release_page_url:
        import webbrowser

        tk.Button(
            btn_row,
            text="🌐 网页查看",
            command=lambda: webbrowser.open(check_res.release_page_url),
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=7,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_row,
        text="稍后再说" if check_res.has_update else "关闭",
        command=_on_close_dialog,
        bg="#334155",
        fg="#cbd5e1",
        activebackground="#475569",
        activeforeground="#ffffff",
        relief="flat",
        font=("Microsoft YaHei UI", 9),
        padx=10,
        pady=7,
        cursor="hand2",
    ).pack(side="right")
