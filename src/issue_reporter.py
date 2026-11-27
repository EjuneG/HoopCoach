#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
issue_reporter.py — 零 Token 安全反馈模块（支持 Cloudflare Worker 免登直投 + GitHub 预填 + 超级现场诊断包，纯标准库）

核心设计：
1. 客户端绝不内置任何 GitHub Personal Access Token；
   - 若 config/app_meta.json 配置了 `feedback_worker_url`，支持免登一键直投至自建 Cloudflare Worker，由服务端安全创建 GitHub Issue；
   - 若未配置或直连失败，自动无缝降级为「预填 GitHub New Issue 网页」或「复制群聊摘要 + 导出完整现场 zip」；
2. 严格脱敏本机用户名、家目录路径（Linux `/home/<user>`、macOS `/Users/<user>`、Windows `C:\Users\<user>`）
   以及任何形似 Token 的敏感片段，绝不打包个人账号池/本地路径配置；
3. 超级现场诊断包 `hoopcoach_diag_YYYYMMDD_HHMMSS.zip` 包含：
   - `diagnostic_env.json`（脱敏环境、窗口比例与 HUD 模式）
   - `state_snapshot.json`（脱敏实时内存状态：训次/哨子标记/实带器材/商店货架等）
   - `edu_log_compact.txt`（从本局开局起剥离 Unity/XLua 堆栈噪声后的整局精简关键协议日志）
   - `log_tail_sanitized.txt`（原始最新日志末尾 200 行脱敏片段）
   - `runtime_error_sanitized.log`（如有客户端运行异常日志）
   - `hud_config_sanitized.json`（仅开关与偏科，无本机路径）
   - `screenshot.png`（可选：勾选后截取当前游戏窗口画面）
"""

from __future__ import annotations

import ctypes
from datetime import datetime
import getpass
import io
import json
import os
import platform
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from typing import Callable, Optional

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    tk = None
    messagebox = None

import platform_compat as pc

APP_META_FILE = os.path.join(pc.CONFIG_DIR, "app_meta.json")
MAX_GITHUB_ISSUE_URL_LEN = 7200

ISSUE_CATEGORIES = [
    ("bug", "🐛 Bug 反馈", "[Bug]", "bug"),
    ("badge_offset", "📐 角标位置偏移", "[角标偏移]", "bug,ui"),
    ("feature", "💡 功能改进建议", "[建议]", "enhancement"),
]

_COMPACT_LOG_SKIP_PREFIXES = (
    "\t",
    "UnityEngine.",
    "XLua.",
    "P3Client.",
    "Animancer.",
    "DG.Tweening.",
    "System.",
)

_COMPACT_LOG_KEEP_KEYWORDS = (
    "[EDU_MODEL]",
    "[SCENARIO]",
    "[UIManager]OpenUIView",
    "[UIManager]CloseUIView",
    "[UIManager]HideUIView",
    "[UIManager]CloseAll",
    "[UIManager]BlockInput",
    "request_call:    education.",
    "dispatch:    education.",
    "dispatch:    card.sc_team_rank_reward",
    "dispatch:    pvp.cs_challenge_pvp",
    "new_rank =",
    "old_rank =",
    "experience_round =",
    "round_type =",
    "supactor_dict =",
    "supactor_borrow_info =",
    "card_id =",
    "cfg_id =",
    "trammel =",
    "edu_store_info =",
    "edu_manager =",
    "ps =",
    "currency_list =",
)


def load_app_meta() -> dict:
    """读取 config/app_meta.json（版本号、公开 GitHub 仓库及可选反馈 Worker 地址）。"""
    meta = {
        "app_name": "HoopCoach",
        "version": "v0.11.0",
        "github_repo": "",
        "feedback_worker_url": "",
        "feedback_app_key": "",
    }
    if os.path.isfile(APP_META_FILE):
        try:
            with open(APP_META_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in ("app_name", "version", "github_repo", "feedback_worker_url", "feedback_app_key"):
                    if k in disk and isinstance(disk[k], str):
                        meta[k] = disk[k].strip()
        except Exception:
            pass
    return meta


def sanitize_text(text: str) -> str:
    """严格脱敏路径中的用户名、家目录与敏感 Token 模式。"""
    if not text:
        return ""
    out = str(text)

    # 1. 优先脱敏当前系统家目录绝对路径
    try:
        home_dir = os.path.expanduser("~")
        if home_dir and home_dir not in ("/", "~", "\\"):
            out = out.replace(home_dir, "<HOME>")
            out = out.replace(home_dir.replace("\\", "/"), "<HOME>")
    except Exception:
        pass

    # 2. 脱敏常见 OS 用户目录路径（兼容任意其他用户名，便于跨机测试）
    out = re.sub(r"(?i)([A-Z]:\\Users\\)([^\\/\r\n\"'<>]+)", r"\1<USER>", out)
    out = re.sub(r"(?i)([A-Z]:/Users/)([^\\/\r\n\"'<>]+)", r"\1<USER>", out)
    out = re.sub(r"(/home/)([^/\r\n\"'<>:\s]+)", r"\1<USER>", out)
    out = re.sub(r"(/Users/)([^/\r\n\"'<>:\s]+)", r"\1<USER>", out)

    # 3. 脱敏当前系统登录用户名（仅在非极短词时替换，避免误伤普通字符）
    candidates = set()
    for getter in (
        lambda: getpass.getuser(),
        lambda: os.environ.get("USERNAME", ""),
        lambda: os.environ.get("USER", ""),
    ):
        try:
            val = (getter() or "").strip()
            if len(val) >= 3 and val.lower() not in ("user", "admin", "root", "test", "coach"):
                candidates.add(val)
        except Exception:
            pass
    for uname in sorted(candidates, key=len, reverse=True):
        out = re.sub(rf"(?<=[\\/:=\s\"']){re.escape(uname)}(?=[\\/:=\s\"']|$)", "<USER>", out)

    # 4. 脱敏可能误入日志的凭据样式
    out = re.sub(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b", "<REDACTED_TOKEN>", out)
    out = re.sub(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "<REDACTED_TOKEN>", out)
    out = re.sub(r"\bsk-[A-Za-z0-9_\-]{16,}\b", "<REDACTED_KEY>", out)
    out = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{16,}", r"\1<REDACTED>", out)
    return out


def _sanitize_recursive(obj):
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_recursive(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_recursive(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_sanitize_recursive(list(obj)), key=lambda x: str(x))
    return obj


def _detect_screen_and_dpi() -> tuple[str, float]:
    """探测屏幕分辨率与 DPI 缩放比例（纯只读）。"""
    dpi_scale = 1.0
    res_str = "未知"
    if pc.IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            sw = int(user32.GetSystemMetrics(0))
            sh = int(user32.GetSystemMetrics(1))
            if sw > 0 and sh > 0:
                res_str = f"{sw}x{sh}"
            if hasattr(user32, "GetDpiForSystem"):
                dpi = int(user32.GetDpiForSystem() or 96)
                dpi_scale = round(dpi / 96.0, 2)
        except Exception:
            pass
    elif tk is not None:
        try:
            root = getattr(tk, "_default_root", None)
            if root is not None:
                res_str = f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}"
                dpi_scale = round(float(root.winfo_fpixels("1i")) / 96.0, 2)
        except Exception:
            pass
    return res_str, dpi_scale


def build_state_snapshot(state: Optional[dict], extra: Optional[dict] = None) -> dict:
    """提取脱敏后的实时育成内存状态快照，帮助快速定位状态机/事件/器材/商店类 Bug。"""
    if not isinstance(state, dict):
        return {}
    snap = {
        "round": int(state.get("round") or 0),
        "exp_round": int(state.get("exp_round") or 0),
        "round_type": int(state.get("round_type") or 1),
        "edu_state_id": int(state.get("edu_state_id") or 0),
        "ps": int(state.get("ps") or 0),
        "ps_max": int(state.get("ps_max") or 130),
        "gold": int(state.get("gold") or 0),
        "main_char_id": state.get("main_char_id"),
        "pre_edu_char_id": state.get("pre_edu_char_id"),
        "teammate_char_ids": list(state.get("teammate_char_ids") or []),
        "supactor_cfg_ids": sorted({int(v) for v in (state.get("supactor_card_to_cfg") or {}).values() if v}),
        "supactor_trammel": {str(k): int(v) for k, v in (state.get("supactor_trammel") or {}).items()},
        "whistle_awake_level": state.get("whistle_awake_level"),
        "whistle_break_level": state.get("whistle_break_level"),
        "fired_70": bool(state.get("fired_70", False)),
        "fired_71": bool(state.get("fired_71", False)),
        "mgr_energy": int(state.get("mgr_energy") or 0),
        "mgr_explosive": int(state.get("mgr_explosive") or 0),
        "focus_levels": {str(k): int(v) for k, v in (state.get("focus_levels") or {}).items()},
        "train_counts": {str(k): int(v) for k, v in (state.get("train_counts") or {}).items()},
        "store_open": bool(state.get("store_open", False)),
        "store_items": [
            {"id": int(it.get("id") or 0), "price": int(it.get("price") or 0), "num": int(it.get("num") or 0)}
            for it in (state.get("store_items") or [])
            if isinstance(it, dict)
        ],
        "active_states": sorted(int(x) for x in (state.get("active_states") or []) if str(x).isdigit()),
        "cur_story": list(state["cur_story"]) if state.get("cur_story") else None,
        "in_story_choice": bool(state.get("in_story_choice", False)),
        "weekend_map_open": bool(state.get("weekend_map_open", False)),
        "travel_sub_scene": str(state.get("travel_sub_scene") or ""),
        "ycjm_scene": str(state.get("ycjm_scene") or ""),
        "active_ui_views": sorted(str(v) for v in (state.get("active_ui_views") or [])),
    }
    if isinstance(extra, dict):
        snap.update(extra)
    return _sanitize_recursive(snap)


def collect_diagnostic_info(app_context: Optional[dict] = None) -> dict:
    """采集脱敏运行环境、窗口比例、HUD 配置与场景状态信息。"""
    meta = load_app_meta()
    ctx = dict(app_context or {})
    screen_res, dpi_scale = _detect_screen_and_dpi()
    if ctx.get("screen_resolution"):
        screen_res = str(ctx["screen_resolution"])
    if ctx.get("dpi_scale"):
        dpi_scale = float(ctx["dpi_scale"])

    try:
        from win_diegetic_hud import PIL_AVAILABLE
    except Exception:
        PIL_AVAILABLE = False

    diag = {
        "app_name": meta["app_name"],
        "version": meta["version"],
        "frozen_exe": pc.IS_FROZEN,
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "pil_available": bool(ctx.get("pil_available", PIL_AVAILABLE)),
        "screen_resolution": screen_res,
        "dpi_scale": dpi_scale,
        "game_client_size": ctx.get("game_client_size") or "未检测到游戏窗口",
        "game_aspect_ratio": ctx.get("game_aspect_ratio"),
        "content_box_size": ctx.get("content_box_size"),
        "hud_mode": ctx.get("hud_mode") or "unknown",
        "follow_game_focus": ctx.get("follow_game_focus"),
        "strategy": ctx.get("strategy") or "training_flow",
        "focus_stat": ctx.get("focus_stat"),
        "focus_bonus": ctx.get("focus_bonus"),
        "active_view_mode": ctx.get("active_view_mode") or "unknown",
        "round": ctx.get("round", 0),
        "exp_round": ctx.get("exp_round", 0),
        "round_type": ctx.get("round_type", 1),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _sanitize_recursive(diag)


def _read_sanitized_log_tail(log_path: str, max_lines: int = 200) -> str:
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        fsize = os.path.getsize(log_path)
        read_bytes = min(fsize, 256 * 1024)
        with open(log_path, "rb") as f:
            if fsize > read_bytes:
                f.seek(fsize - read_bytes)
            raw = f.read().decode("utf-8", errors="ignore")
        lines = raw.splitlines()[-max_lines:]
        cleaned = [sanitize_text(ln) for ln in lines]
        return "\n".join(cleaned) + ("\n" if cleaned else "")
    except Exception as e:
        return f"# 读取日志片段失败: {sanitize_text(str(e))}\n"


def extract_compact_run_log(log_path: str, max_chars: int = 24000) -> str:
    """
    从最新游戏日志的最后一次开局锚点 (education.cs_start_education) 起，
    剥离 Unity/XLua 堆栈与心跳噪声，浓缩为高密度脱敏协议日志（几十 MB -> 10~24 KB）。
    """
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        fsize = os.path.getsize(log_path)
        # 反向查找最后一次 cs_start_education（最多向前扫 6 MB，避免大日志卡顿）
        scan_bytes = min(fsize, 6 * 1024 * 1024)
        anchor = b"request_call:    education.cs_start_education"
        with open(log_path, "rb") as f:
            start_pos = fsize - scan_bytes
            f.seek(start_pos)
            chunk = f.read(scan_bytes)
        idx = chunk.rfind(anchor)
        if idx != -1:
            line_start = chunk.rfind(b"\n", 0, idx)
            sub_bytes = chunk[line_start + 1 if line_start != -1 else idx :]
        else:
            # 无开局锚点时取尾部最多 512 KB
            sub_bytes = chunk[-512 * 1024 :]

        text = sub_bytes.decode("utf-8", errors="ignore")
        kept: list[str] = []
        in_compact_block = False
        block_lines = 0
        for raw_ln in text.splitlines():
            if not raw_ln or raw_ln.startswith(_COMPACT_LOG_SKIP_PREFIXES) or "cs_heartbeat" in raw_ln:
                continue
            s = raw_ln.strip()
            if not s:
                continue
            if any(
                hdr in s
                for hdr in (
                    "dispatch:    education.sc_education_result",
                    "dispatch:    card.sc_team_rank_reward",
                    "dispatch:    pvp.cs_challenge_pvp",
                )
            ):
                in_compact_block = True
                block_lines = 0
                kept.append(sanitize_text(s[:220]))
                continue
            if in_compact_block:
                block_lines += 1
                kept.append("  " + sanitize_text(s[:200]))
                if s == "}" or block_lines >= 20:
                    in_compact_block = False
                continue
            if any(kw in s for kw in _COMPACT_LOG_KEEP_KEYWORDS):
                kept.append(sanitize_text(s[:240]))

        if not kept:
            return _read_sanitized_log_tail(log_path, max_lines=120)[:max_chars]

        joined = "\n".join(kept)
        if len(joined) > max_chars:
            # 保留开局前 25%（选人/器材/笔记）+ 最新 75%（故障发生现场）
            head_len = max(1500, int(max_chars * 0.25))
            tail_len = max(2000, max_chars - head_len - 80)
            joined = (
                joined[:head_len]
                + f"\n... [中间省略 {len(joined) - head_len - tail_len} 字符] ...\n"
                + joined[-tail_len:]
            )
        return joined + "\n"
    except Exception as e:
        return f"# 提取精简日志失败: {sanitize_text(str(e))}\n"


def _read_sanitized_runtime_error_log(error_log_path: str = "", max_chars: int = 5000) -> str:
    path = error_log_path or os.path.join(pc.BASE_DIR, "runtime_error.log")
    if not path or not os.path.isfile(path):
        return ""
    try:
        fsize = os.path.getsize(path)
        read_bytes = min(fsize, 32 * 1024)
        with open(path, "rb") as f:
            if fsize > read_bytes:
                f.seek(fsize - read_bytes)
            raw = f.read().decode("utf-8", errors="ignore")
        clean = sanitize_text(raw).strip()
        if len(clean) > max_chars:
            clean = "...(truncated)\n" + clean[-max_chars:]
        return clean + ("\n" if clean else "")
    except Exception:
        return ""


def capture_window_screenshot_png(game_rect: Optional[tuple[int, int, int, int]] = None) -> bytes:
    """尽力截取游戏窗口画面（PNG 字节），缺失 Pillow/ImageGrab 或无头环境下静默返回空字节。"""
    if not game_rect or len(game_rect) != 4:
        return b""
    try:
        x, y, w, h = [int(v) for v in game_rect]
        if w < 100 or h < 100:
            return b""
        from PIL import ImageGrab

        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        # 若分辨率过高，等比压到宽 <= 1280 保持 zip 轻量
        if img.width > 1280:
            nh = max(1, int(round(img.height * (1280.0 / img.width))))
            img = img.resize((1280, nh))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return b""


def export_diagnostic_zip(
    diag_info: dict,
    log_path: str = "",
    out_dir: str = "",
    hud_config: Optional[dict] = None,
    *,
    state_snapshot: Optional[dict] = None,
    runtime_error_path: str = "",
    include_screenshot: bool = False,
    game_rect: Optional[tuple[int, int, int, int]] = None,
) -> str:
    """
    一键导出超级脱敏诊断 zip 包：
    - diagnostic_env.json（脱敏环境与窗口信息）
    - state_snapshot.json（脱敏实时内存状态快照，如有）
    - edu_log_compact.txt（本局剥离堆栈后的精简关键日志，如有）
    - log_tail_sanitized.txt（最新日志末尾 200 行脱敏片段，如有）
    - runtime_error_sanitized.log（客户端运行异常日志，如有）
    - hud_config_sanitized.json（仅开关与偏科，无本机路径）
    - screenshot.png（可选：当前游戏窗口截图）
    """
    target_dir = out_dir
    if not target_dir:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        target_dir = desktop if os.path.isdir(desktop) else pc.BASE_DIR
    os.makedirs(target_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(target_dir, f"hoopcoach_diag_{ts}.zip")

    safe_diag = _sanitize_recursive(diag_info or {})
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "diagnostic_env.json",
            json.dumps(safe_diag, indent=2, ensure_ascii=False) + "\n",
        )
        if isinstance(state_snapshot, dict) and state_snapshot:
            zf.writestr(
                "state_snapshot.json",
                json.dumps(_sanitize_recursive(state_snapshot), indent=2, ensure_ascii=False) + "\n",
            )
        if isinstance(hud_config, dict):
            safe_cfg = {
                "diegetic_mode": bool(hud_config.get("diegetic_mode", True)),
                "recommendations": dict(hud_config.get("recommendations") or {}),
                "focus": dict(hud_config.get("focus") or {}),
            }
            zf.writestr(
                "hud_config_sanitized.json",
                json.dumps(_sanitize_recursive(safe_cfg), indent=2, ensure_ascii=False) + "\n",
            )
        compact_txt = extract_compact_run_log(log_path, max_chars=28000)
        if compact_txt:
            zf.writestr("edu_log_compact.txt", compact_txt)
        tail_txt = _read_sanitized_log_tail(log_path, max_lines=200)
        if tail_txt:
            zf.writestr("log_tail_sanitized.txt", tail_txt)
        err_txt = _read_sanitized_runtime_error_log(runtime_error_path, max_chars=8000)
        if err_txt:
            zf.writestr("runtime_error_sanitized.log", err_txt)
        if include_screenshot and game_rect:
            png_bytes = capture_window_screenshot_png(game_rect)
            if png_bytes:
                zf.writestr("screenshot.png", png_bytes)

    return zip_path


def _format_env_lines(diag_info: Optional[dict]) -> str:
    if not diag_info:
        return ""
    safe_d = _sanitize_recursive(diag_info)
    lines = [
        f"- **版本**: `{safe_d.get('version', 'unknown')}` (exe={safe_d.get('frozen_exe')})",
        f"- **系统**: `{safe_d.get('os', 'unknown')}` / Python `{safe_d.get('python', '')}`",
        f"- **屏幕 / DPI**: `{safe_d.get('screen_resolution', '未知')}` @ `{safe_d.get('dpi_scale', 1.0)}x`",
        f"- **游戏客户区**: `{safe_d.get('game_client_size', '未知')}` (比例 `{safe_d.get('game_aspect_ratio')}`, 16:9内容盒 `{safe_d.get('content_box_size')}`)",
        f"- **HUD 状态**: `{safe_d.get('hud_mode', 'unknown')}` | Pillow=`{safe_d.get('pil_available')}` | 焦点跟随=`{safe_d.get('follow_game_focus')}`",
        f"- **策略与场景**: 流派=`{safe_d.get('strategy')}` | 偏科=`{safe_d.get('focus_stat')}`×`{safe_d.get('focus_bonus')}` | 场景=`{safe_d.get('active_view_mode')}` (第{safe_d.get('round')}训 / R{safe_d.get('exp_round')})",
    ]
    return "\n".join(lines)


def build_issue_markdown(
    category_key: str,
    title: str,
    description: str,
    diag_info: Optional[dict] = None,
    *,
    include_diag: bool = True,
    zip_filename: str = "",
) -> str:
    """生成结构清晰的 GitHub Issue Markdown 正文（已自动脱敏）。"""
    cat_label = next((lbl for k, lbl, _pfx, _lb in ISSUE_CATEGORIES if k == category_key), "🐛 Bug 反馈")
    clean_desc = sanitize_text((description or "").strip()) or "（请在此补充问题描述与复现步骤）"

    sections = [
        f"### 反馈类型\n{cat_label}",
        f"### 问题描述 / 建议内容\n{clean_desc}",
    ]
    if include_diag and diag_info:
        sections.append("### 自动采集的脱敏环境信息\n" + _format_env_lines(diag_info))

    if zip_filename:
        sections.append(
            f"### 诊断包附件（可选）\n已本地导出脱敏诊断包 `{sanitize_text(os.path.basename(zip_filename))}`，可直接拖拽到此处上传。"
        )
    else:
        sections.append("### 截图 / 附件（可选）\n如有画面偏移或报错，可直接粘贴截图或拖入诊断包。")

    return "\n\n".join(sections) + "\n"


def build_chat_summary(
    category_key: str,
    title: str,
    description: str,
    diag_info: Optional[dict] = None,
    zip_filename: str = "",
) -> str:
    """生成适合直接粘贴到微信/QQ 群聊的精简脱敏反馈摘要。"""
    prefix = next((pfx for k, _lbl, pfx, _lb in ISSUE_CATEGORIES if k == category_key), "[Bug]")
    safe_d = _sanitize_recursive(diag_info or {})
    clean_title = sanitize_text((title or "").strip()) or "未填标题"
    clean_desc = sanitize_text((description or "").strip()) or "（见截图/诊断包）"
    lines = [
        f"{prefix} {clean_title}",
        f"版本：{safe_d.get('version', 'unknown')} | 系统：{safe_d.get('os', '')} | 场景：{safe_d.get('active_view_mode', 'unknown')} (第{safe_d.get('round', 0)}训/R{safe_d.get('exp_round', 0)})",
        f"说明：{clean_desc}",
    ]
    if zip_filename:
        lines.append(f"现场诊断包：{sanitize_text(os.path.basename(zip_filename))}（请顺手拖进聊天窗口发送）")
    return "\n".join(lines)


def submit_feedback_via_worker(
    worker_url: str,
    category_key: str,
    title: str,
    description: str,
    diag_info: Optional[dict] = None,
    *,
    include_diag: bool = True,
    state_snapshot: Optional[dict] = None,
    log_path: str = "",
    runtime_error_path: str = "",
    reporter_note: str = "",
    app_key: str = "",
    timeout: float = 15.0,
) -> tuple[bool, dict]:
    """
    通过自建 Cloudflare Worker 免登一键提交结构化脱敏反馈至 GitHub Issue。
    返回 `(ok, result_dict)`：
    - 成功：`ok=True`, `result_dict={"issue_number": ..., "issue_url": ...}`
    - 失败：`ok=False`, `result_dict={"error": ...}`
    """
    base = (worker_url or "").strip().rstrip("/")
    if not base or not base.startswith(("https://", "http://")):
        return False, {"error": "尚未配置合法的反馈中转地址"}

    endpoint = base if base.endswith("/api/feedback") else f"{base}/api/feedback"
    clean_title = sanitize_text((title or "").strip())
    if not clean_title:
        return False, {"error": "请先填写简短标题"}

    payload: dict = {
        "category": category_key,
        "title": clean_title,
        "description": sanitize_text((description or "").strip()),
        "reporter_note": sanitize_text((reporter_note or "").strip()),
    }
    if include_diag:
        payload["env_markdown"] = _format_env_lines(diag_info)
        if isinstance(state_snapshot, dict) and state_snapshot:
            payload["state_snapshot"] = json.dumps(_sanitize_recursive(state_snapshot), indent=2, ensure_ascii=False)
        compact_log = extract_compact_run_log(log_path, max_chars=24000)
        if compact_log:
            payload["compact_log"] = compact_log
        err_txt = _read_sanitized_runtime_error_log(runtime_error_path, max_chars=5000)
        if err_txt:
            payload["runtime_error"] = err_txt

    raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"HoopCoach/{(diag_info or {}).get('version', 'dev')}",
    }
    if app_key:
        headers["X-App-Key"] = app_key.strip()

    req = urllib.request.Request(endpoint, data=raw_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body_txt = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body_txt) if body_txt else {}
            if isinstance(data, dict) and data.get("ok"):
                return True, data
            return False, {"error": (data.get("error") if isinstance(data, dict) else "未知响应") or "提交失败"}
    except urllib.error.HTTPError as e:
        try:
            err_raw = e.read().decode("utf-8", errors="ignore")
            err_json = json.loads(err_raw) if err_raw else {}
            msg = err_json.get("error") if isinstance(err_json, dict) else err_raw[:160]
        except Exception:
            msg = str(e)
        return False, {"error": f"HTTP {e.code}: {msg}"}
    except Exception as e:
        return False, {"error": f"网络请求失败: {sanitize_text(str(e))}"}


def build_github_issue_url(
    github_repo: str,
    category_key: str,
    title: str,
    body_md: str,
    max_url_len: int = MAX_GITHUB_ISSUE_URL_LEN,
) -> tuple[str, bool]:
    """
    构造 GitHub New Issue 预填 URL。
    返回 `(url, was_truncated)`；若 `github_repo` 非合法 `owner/repo` 格式，返回 `("", False)`。
    """
    repo = (github_repo or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$", repo):
        return "", False

    prefix, labels = next(
        ((pfx, lb) for k, _lbl, pfx, lb in ISSUE_CATEGORIES if k == category_key),
        ("[Bug]", "bug"),
    )
    clean_title = sanitize_text((title or "").strip())
    if not clean_title:
        clean_title = f"{prefix} 请填写简短标题"
    elif not clean_title.startswith("["):
        clean_title = f"{prefix} {clean_title}"

    clean_body = sanitize_text(body_md or "")
    base_url = f"https://github.com/{repo}/issues/new"

    def _encode(b: str) -> str:
        params = {
            "title": clean_title,
            "body": b,
            "labels": labels,
        }
        return f"{base_url}?{urllib.parse.urlencode(params)}"

    full_url = _encode(clean_body)
    if len(full_url) <= max_url_len:
        return full_url, False

    notice = "\n\n> ⚠️ 反馈正文较长，已完整复制到剪贴板，请直接全选本框并按 Ctrl+V 粘贴完整内容。\n"
    lo, hi = 0, len(clean_body)
    best_url = _encode(notice.strip())
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = _encode(clean_body[:mid] + notice)
        if len(cand) <= max_url_len:
            best_url = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best_url, True


def reveal_file_in_explorer(file_path: str) -> None:
    """在系统文件管理器中打开/选中导出的诊断 zip 所在文件夹。"""
    if not file_path:
        return
    try:
        if pc.IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(file_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", file_path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(os.path.abspath(file_path))])
    except Exception:
        pass


def open_feedback_dialog(
    parent,
    get_context_fn: Optional[Callable[[], dict]] = None,
):
    """打开「反馈 Bug / 提建议」快捷弹窗（亮色校园篮球番主题，支持 Worker 免登直投 + 超级现场包 + 群聊摘要）。"""
    if tk is None:
        raise RuntimeError("当前环境缺少 tkinter")

    meta = load_app_meta()
    ctx = get_context_fn() if callable(get_context_fn) else {}
    diag_info = collect_diagnostic_info(ctx)
    log_path = str(ctx.get("log_path") or "")
    hud_config = ctx.get("hud_config") if isinstance(ctx.get("hud_config"), dict) else None
    state_snapshot = ctx.get("state_snapshot") if isinstance(ctx.get("state_snapshot"), dict) else None
    game_rect = ctx.get("game_rect") if isinstance(ctx.get("game_rect"), (list, tuple)) else None
    runtime_error_path = str(ctx.get("runtime_error_path") or "")
    worker_url = str(meta.get("feedback_worker_url") or "").strip()
    app_key = str(meta.get("feedback_app_key") or "").strip()

    C_BG = "#F3F8FF"
    C_CARD = "#FFFFFF"
    C_WELL = "#EBF3FF"
    C_BORDER = "#C8DAF5"
    C_INK = "#142342"
    C_MUTED = "#4B5E82"
    C_BLUE = "#1B5BFF"
    C_BLUE_DARK = "#1242C4"
    C_ORANGE = "#FF5A1F"
    C_ORANGE_DARK = "#DE440C"

    dlg = tk.Toplevel(parent)
    dlg.title("反馈 Bug / 提改进建议 — HoopCoach")
    dlg.geometry("560x610")
    dlg.minsize(510, 560)
    dlg.configure(bg=C_BG)
    dlg.transient(parent)
    try:
        dlg.grab_set()
    except Exception:
        pass

    var_cat = tk.StringVar(value="bug")
    var_title = tk.StringVar(value="")
    var_include_diag = tk.BooleanVar(value=True)
    var_include_shot = tk.BooleanVar(value=bool(game_rect))
    var_status = tk.StringVar(
        value=(
            "已启用免登直投 · 提交时自动附带脱敏环境、状态快照与本局精简日志"
            if worker_url
            else (
                f"目标仓库：{meta['github_repo']} · 零 Token 预填跳转，或一键导出完整现场包发群聊"
                if meta.get("github_repo")
                else "可一键导出完整脱敏现场包或复制群聊摘要直接发给开发者"
            )
        )
    )
    exported_zip = [""]
    submitting = [False]

    # 顶栏
    hdr = tk.Frame(dlg, bg=C_BG)
    hdr.pack(fill="x", padx=16, pady=(14, 8))
    tk.Label(
        hdr,
        text="反馈 Bug / 提改进建议",
        bg=C_BG,
        fg=C_INK,
        font=("Microsoft YaHei UI", 13, "bold"),
    ).pack(side="left")
    badge_txt = f"{meta['version']} · 免登直投" if worker_url else f"{meta['version']} · 自动脱敏"
    tk.Label(
        hdr,
        text=badge_txt,
        bg="#DCE9FF",
        fg=C_BLUE_DARK,
        font=("Microsoft YaHei UI", 8, "bold"),
        padx=9,
        pady=3,
    ).pack(side="right")

    # 卡片主体
    card = tk.Frame(dlg, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1)
    card.pack(fill="both", expand=True, padx=16, pady=(0, 10))

    # 1. 反馈类型
    tk.Label(
        card,
        text="1. 反馈类型",
        bg=C_CARD,
        fg=C_BLUE_DARK,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(10, 4))
    cat_row = tk.Frame(card, bg=C_CARD)
    cat_row.pack(fill="x", padx=12, pady=(0, 8))
    for key, label, _pfx, _lb in ISSUE_CATEGORIES:
        tk.Radiobutton(
            cat_row,
            text=label,
            variable=var_cat,
            value=key,
            bg=C_CARD,
            fg=C_INK,
            selectcolor=C_WELL,
            activebackground=C_CARD,
            activeforeground=C_BLUE,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        ).pack(side="left", padx=(0, 14))

    # 2. 简短标题
    tk.Label(
        card,
        text="2. 简短标题（必填）",
        bg=C_CARD,
        fg=C_BLUE_DARK,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(0, 4))
    ent_title = tk.Entry(
        card,
        textvariable=var_title,
        bg=C_WELL,
        fg=C_INK,
        insertbackground=C_INK,
        relief="flat",
        highlightbackground=C_BORDER,
        highlightthickness=1,
        font=("Microsoft YaHei UI", 10),
    )
    ent_title.pack(fill="x", padx=12, pady=(0, 10), ipady=5)

    # 3. 详细说明
    tk.Label(
        card,
        text="3. 问题描述 / 复现步骤 / 改进想法",
        bg=C_CARD,
        fg=C_BLUE_DARK,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(0, 4))
    txt_desc = tk.Text(
        card,
        bg=C_WELL,
        fg=C_INK,
        insertbackground=C_INK,
        relief="flat",
        highlightbackground=C_BORDER,
        highlightthickness=1,
        font=("Microsoft YaHei UI", 9),
        height=7,
        wrap="word",
        padx=8,
        pady=6,
    )
    txt_desc.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    # 4. 现场诊断信息选项
    tk.Checkbutton(
        card,
        text="附带自动脱敏的环境信息、内存状态快照与本局精简日志（不含本机路径或账号）",
        variable=var_include_diag,
        bg=C_CARD,
        fg=C_INK,
        selectcolor=C_WELL,
        activebackground=C_CARD,
        activeforeground=C_BLUE,
        font=("Microsoft YaHei UI", 8),
        anchor="w",
    ).pack(fill="x", padx=10, pady=(0, 2))

    tk.Checkbutton(
        card,
        text="导出本地现场包时顺带截取当前游戏窗口画面（仅保存在本机 zip，不自动上传）",
        variable=var_include_shot,
        bg=C_CARD,
        fg=C_MUTED,
        selectcolor=C_WELL,
        activebackground=C_CARD,
        activeforeground=C_BLUE,
        font=("Microsoft YaHei UI", 8),
        anchor="w",
    ).pack(fill="x", padx=10, pady=(0, 6))

    env_preview = (
        f"版本 {diag_info['version']} · {diag_info['os']} · 屏幕 {diag_info['screen_resolution']} "
        f"({diag_info['dpi_scale']}×) · 游戏窗 {diag_info['game_client_size']} · "
        f"HUD {'贴身角标' if diag_info['hud_mode'] == 'diegetic_badges' else '紧凑浮窗'} · "
        f"场景 {diag_info['active_view_mode']} (第{diag_info['round']}训/R{diag_info['exp_round']})"
    )
    tk.Label(
        card,
        text=env_preview,
        bg=C_WELL,
        fg=C_MUTED,
        font=("Microsoft YaHei UI", 8),
        anchor="w",
        justify="left",
        wraplength=500,
        padx=8,
        pady=6,
    ).pack(fill="x", padx=12, pady=(0, 10))

    def _copy_to_clipboard(text: str) -> None:
        try:
            dlg.clipboard_clear()
            dlg.clipboard_append(text)
            dlg.update()
        except Exception:
            pass

    def _build_current_md() -> str:
        return build_issue_markdown(
            var_cat.get(),
            var_title.get(),
            txt_desc.get("1.0", "end-1c"),
            diag_info,
            include_diag=var_include_diag.get(),
            zip_filename=exported_zip[0],
        )

    def _on_export_zip() -> None:
        try:
            zpath = export_diagnostic_zip(
                diag_info,
                log_path=log_path,
                hud_config=hud_config,
                state_snapshot=(state_snapshot if var_include_diag.get() else None),
                runtime_error_path=(runtime_error_path if var_include_diag.get() else ""),
                include_screenshot=bool(var_include_shot.get() and game_rect),
                game_rect=tuple(game_rect) if game_rect else None,
            )
            exported_zip[0] = zpath
            var_status.set(f"✅ 已导出完整现场包：{os.path.basename(zpath)}（可直接发群聊或拖进 Issue）")
            reveal_file_in_explorer(zpath)
        except Exception as e:
            messagebox.showerror("导出诊断包失败", str(e), parent=dlg)

    def _on_copy_chat() -> None:
        summary = build_chat_summary(
            var_cat.get(),
            var_title.get(),
            txt_desc.get("1.0", "end-1c"),
            diag_info,
            zip_filename=exported_zip[0],
        )
        _copy_to_clipboard(summary)
        var_status.set("✅ 已复制群聊/微信精简摘要到剪贴板，可直接粘贴发送！")

    def _fallback_open_github() -> None:
        md = _build_current_md()
        repo = meta.get("github_repo", "")
        url, truncated = build_github_issue_url(repo, var_cat.get(), var_title.get(), md)
        if not url:
            _copy_to_clipboard(md)
            messagebox.showinfo(
                "已复制反馈内容",
                "已将脱敏反馈 Markdown 复制到剪贴板，你可以直接粘贴发给开发者。",
                parent=dlg,
            )
            return
        if truncated:
            _copy_to_clipboard(md)
        webbrowser.open(url)
        if truncated:
            messagebox.showinfo(
                "已打开 GitHub 提交页（正文已复制）",
                "反馈正文略长，已自动将完整 Markdown 复制到剪贴板。\n在浏览器页面中全选正文框并按 Ctrl+V 粘贴即可！",
                parent=dlg,
            )
        else:
            var_status.set("🚀 已在浏览器打开预填的 GitHub Issue 页面！")

    def _on_submit_primary() -> None:
        if not worker_url:
            _fallback_open_github()
            return
        if submitting[0]:
            return
        title_s = var_title.get().strip()
        if not title_s:
            messagebox.showwarning("请填写标题", "先写一句简短标题（例如：70熟练度提示没消失），方便定位哦！", parent=dlg)
            ent_title.focus_set()
            return
        submitting[0] = True
        btn_submit.configure(state="disabled", text="提交中...")
        var_status.set("⏳ 正在安全提交脱敏反馈与本局精简日志...")

        desc_s = txt_desc.get("1.0", "end-1c")
        cat_s = var_cat.get()
        inc_diag = var_include_diag.get()

        def _worker():
            ok, res = submit_feedback_via_worker(
                worker_url,
                cat_s,
                title_s,
                desc_s,
                diag_info,
                include_diag=inc_diag,
                state_snapshot=(state_snapshot if inc_diag else None),
                log_path=(log_path if inc_diag else ""),
                runtime_error_path=(runtime_error_path if inc_diag else ""),
                app_key=app_key,
            )
            dlg.after(0, lambda: _on_worker_done(ok, res))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_worker_done(ok: bool, res: dict) -> None:
        submitting[0] = False
        try:
            btn_submit.configure(state="normal", text="🚀 免登一键提交")
        except Exception:
            return
        if ok:
            issue_no = res.get("issue_number", "")
            issue_url = str(res.get("issue_url") or "")
            if issue_url:
                _copy_to_clipboard(issue_url)
            var_status.set(f"🎉 提交成功！已生成 Issue #{issue_no}（链接已复制）")
            msg = f"感谢反馈！已自动附上本局精简日志并创建 Issue #{issue_no}。\n\n{issue_url}"
            if exported_zip[0]:
                msg += f"\n\n如想补充截图，打开链接后把桌面的 {os.path.basename(exported_zip[0])} 拖进去即可。"
            if issue_url and messagebox.askyesno("反馈提交成功", msg + "\n\n是否在浏览器打开查看这条 Issue？", parent=dlg):
                webbrowser.open(issue_url)
        else:
            err = res.get("error", "网络异常")
            var_status.set(f"⚠️ 直投失败（{err}），已为你准备备用提交方式")
            if messagebox.askyesno(
                "免登直投暂时不可用",
                f"直连反馈服务失败：{err}\n\n"
                "点击「是」改为自动复制反馈并尝试打开 GitHub 网页；\n"
                "点击「否」可点左下角「导出完整现场包」直接发群聊。",
                parent=dlg,
            ):
                _fallback_open_github()

    # 底部按钮栏
    btn_bar = tk.Frame(dlg, bg=C_BG)
    btn_bar.pack(fill="x", padx=16, pady=(0, 6))
    tk.Button(
        btn_bar,
        text="📦 导出完整现场包",
        command=_on_export_zip,
        bg="#FFFFFF",
        fg=C_INK,
        activebackground="#E2EEFF",
        activeforeground=C_BLUE_DARK,
        relief="flat",
        highlightbackground=C_BORDER,
        highlightthickness=1,
        font=("Microsoft YaHei UI", 9),
        padx=10,
        pady=7,
        cursor="hand2",
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_bar,
        text="💬 复制群聊摘要",
        command=_on_copy_chat,
        bg="#FFFFFF",
        fg=C_INK,
        activebackground="#E2EEFF",
        activeforeground=C_BLUE_DARK,
        relief="flat",
        highlightbackground=C_BORDER,
        highlightthickness=1,
        font=("Microsoft YaHei UI", 9),
        padx=10,
        pady=7,
        cursor="hand2",
    ).pack(side="left", padx=(0, 8))

    btn_submit = tk.Button(
        btn_bar,
        text="🚀 免登一键提交" if worker_url else "🚀 打开 GitHub 提交",
        command=_on_submit_primary,
        bg=C_ORANGE if worker_url else C_BLUE,
        fg="#FFFFFF",
        activebackground=C_ORANGE_DARK if worker_url else C_BLUE_DARK,
        activeforeground="#FFFFFF",
        relief="flat",
        font=("Microsoft YaHei UI", 9, "bold"),
        padx=12,
        pady=7,
        cursor="hand2",
    )
    btn_submit.pack(side="right", fill="x", expand=True)

    tk.Label(
        dlg,
        textvariable=var_status,
        bg=C_BG,
        fg=C_MUTED,
        font=("Microsoft YaHei UI", 8),
        anchor="w",
        justify="left",
        wraplength=524,
    ).pack(fill="x", padx=16, pady=(0, 12))

    return dlg
