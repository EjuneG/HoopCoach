#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
issue_reporter.py — 零 Token 安全 GitHub Issue 快捷反馈 & 脱敏诊断包导出模块（纯标准库）

核心设计：
1. 绝不内置任何 GitHub Personal Access Token 或 OAuth 凭据，100% 通过默认浏览器打开预填的
   `https://github.com/<owner>/<repo>/issues/new?title=...&body=...&labels=...`；
2. 严格脱敏本机用户名、家目录路径（Linux `/home/<user>`、macOS `/Users/<user>`、Windows `C:\Users\<user>`）
   以及任何形似 Token 的敏感片段，绝不打包个人账号池/战绩/本地路径配置；
3. 一键导出脱敏诊断包 `lqsncoach_diag_YYYYMMDD_HHMMSS.zip`（含脱敏环境 JSON + HUD 开关 + 最新日志末尾 200 行脱敏片段），
   并打开所在文件夹方便直接拖进 GitHub Issue 上传；
4. 若正文超过 URL 安全上限，自动截断 URL 并把完整 Markdown 复制到剪贴板，提示用户 Ctrl+V 粘贴。
"""

from __future__ import annotations

import ctypes
from datetime import datetime
import getpass
import json
import os
import platform
import re
import subprocess
import sys
import urllib.parse
import webbrowser
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
MAX_GITHUB_ISSUE_URL_LEN = 7200

ISSUE_CATEGORIES = [
    ("bug", "🐛 Bug 反馈", "[Bug]", "bug"),
    ("badge_offset", "📐 角标位置偏移", "[角标偏移]", "bug,ui"),
    ("feature", "💡 功能改进建议", "[建议]", "enhancement"),
]


def load_app_meta() -> dict:
    """读取 config/app_meta.json（版本号与公开 GitHub 仓库 owner/repo）。"""
    meta = {
        "app_name": "HoopCoach",
        "version": "v0.10.1",
        "github_repo": "",
    }
    if os.path.isfile(APP_META_FILE):
        try:
            with open(APP_META_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in ("app_name", "version", "github_repo"):
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
            # 仅在已有默认 root 时读取，避免无头环境下新建 Tk 窗口
            root = getattr(tk, "_default_root", None)
            if root is not None:
                res_str = f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}"
                dpi_scale = round(float(root.winfo_fpixels("1i")) / 96.0, 2)
        except Exception:
            pass
    return res_str, dpi_scale


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
        # 仅读取文件尾部最多 256 KB，避免大日志占内存
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


def export_diagnostic_zip(
    diag_info: dict,
    log_path: str = "",
    out_dir: str = "",
    hud_config: Optional[dict] = None,
) -> str:
    """
    一键导出脱敏诊断 zip 包：
    - diagnostic_env.json（脱敏环境与窗口信息）
    - hud_config_sanitized.json（仅开关与偏科，无本机路径）
    - log_tail_sanitized.txt（最新日志末尾 200 行脱敏片段，可选）
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
        tail_txt = _read_sanitized_log_tail(log_path, max_lines=200)
        if tail_txt:
            zf.writestr("log_tail_sanitized.txt", tail_txt)

    return zip_path


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
        safe_d = _sanitize_recursive(diag_info)
        lines = [
            f"- **版本**: `{safe_d.get('version', 'unknown')}` (exe={safe_d.get('frozen_exe')})",
            f"- **系统**: `{safe_d.get('os', 'unknown')}` / Python `{safe_d.get('python', '')}`",
            f"- **屏幕 / DPI**: `{safe_d.get('screen_resolution', '未知')}` @ `{safe_d.get('dpi_scale', 1.0)}x`",
            f"- **游戏客户区**: `{safe_d.get('game_client_size', '未知')}` (比例 `{safe_d.get('game_aspect_ratio')}`, 16:9内容盒 `{safe_d.get('content_box_size')}`)",
            f"- **HUD 状态**: `{safe_d.get('hud_mode', 'unknown')}` | Pillow=`{safe_d.get('pil_available')}` | 焦点跟随=`{safe_d.get('follow_game_focus')}`",
            f"- **策略与场景**: 流派=`{safe_d.get('strategy')}` | 偏科=`{safe_d.get('focus_stat')}`×`{safe_d.get('focus_bonus')}` | 场景=`{safe_d.get('active_view_mode')}` (第{safe_d.get('round')}训 / R{safe_d.get('exp_round')})",
        ]
        sections.append("### 自动采集的脱敏环境信息\n" + "\n".join(lines))

    if zip_filename:
        sections.append(
            f"### 诊断包附件（可选）\n已本地导出脱敏诊断包 `{sanitize_text(os.path.basename(zip_filename))}`，可直接拖拽到此处上传。"
        )
    else:
        sections.append("### 截图 / 附件（可选）\n如有画面偏移或报错，可直接粘贴截图或拖入诊断包。")

    return "\n\n".join(sections) + "\n"


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
    """打开「🐛 反馈 Bug / 提建议」快捷弹窗（与桌面小控制台统一深色风格）。"""
    if tk is None:
        raise RuntimeError("当前环境缺少 tkinter")

    meta = load_app_meta()
    ctx = get_context_fn() if callable(get_context_fn) else {}
    diag_info = collect_diagnostic_info(ctx)
    log_path = str(ctx.get("log_path") or "")
    hud_config = ctx.get("hud_config") if isinstance(ctx.get("hud_config"), dict) else None

    dlg = tk.Toplevel(parent)
    dlg.title("🐛 反馈 Bug / 提改进建议 — HoopCoach")
    dlg.geometry("520x540")
    dlg.minsize(480, 500)
    dlg.configure(bg="#0f172a")
    dlg.transient(parent)
    try:
        dlg.grab_set()
    except Exception:
        pass

    var_cat = tk.StringVar(value="bug")
    var_title = tk.StringVar(value="")
    var_include_diag = tk.BooleanVar(value=True)
    var_status = tk.StringVar(
        value=(
            f"目标仓库：{meta['github_repo']} · 零 Token 安全跳转浏览器"
            if meta.get("github_repo")
            else "提示：尚未配置公开仓库地址，可一键复制 Markdown 或导出诊断包发给开发者"
        )
    )
    exported_zip = [""]

    # 顶栏
    hdr = tk.Frame(dlg, bg="#0f172a")
    hdr.pack(fill="x", padx=16, pady=(14, 8))
    tk.Label(
        hdr,
        text="🐛 反馈 Bug / 提改进建议",
        bg="#0f172a",
        fg="#f8fafc",
        font=("Microsoft YaHei UI", 13, "bold"),
    ).pack(side="left")
    tk.Label(
        hdr,
        text=f"{meta['version']} · 自动脱敏",
        bg="#1e3a5f",
        fg="#7dd3fc",
        font=("Microsoft YaHei UI", 8, "bold"),
        padx=8,
        pady=3,
    ).pack(side="right")

    # 卡片主体
    card = tk.Frame(dlg, bg="#1e293b", highlightbackground="#334155", highlightthickness=1)
    card.pack(fill="both", expand=True, padx=16, pady=(0, 10))

    # 1. 反馈类型
    tk.Label(
        card,
        text="1. 反馈类型",
        bg="#1e293b",
        fg="#fbbf24",
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(10, 4))
    cat_row = tk.Frame(card, bg="#1e293b")
    cat_row.pack(fill="x", padx=12, pady=(0, 8))
    for key, label, _pfx, _lb in ISSUE_CATEGORIES:
        tk.Radiobutton(
            cat_row,
            text=label,
            variable=var_cat,
            value=key,
            bg="#1e293b",
            fg="#e2e8f0",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        ).pack(side="left", padx=(0, 12))

    # 2. 简短标题
    tk.Label(
        card,
        text="2. 简短标题",
        bg="#1e293b",
        fg="#fbbf24",
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(0, 4))
    ent_title = tk.Entry(
        card,
        textvariable=var_title,
        bg="#0f172a",
        fg="#f8fafc",
        insertbackground="#f8fafc",
        relief="flat",
        highlightbackground="#334155",
        highlightthickness=1,
        font=("Microsoft YaHei UI", 10),
    )
    ent_title.pack(fill="x", padx=12, pady=(0, 10), ipady=5)

    # 3. 详细说明
    tk.Label(
        card,
        text="3. 问题描述 / 复现步骤 / 改进想法",
        bg="#1e293b",
        fg="#fbbf24",
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(0, 4))
    txt_desc = tk.Text(
        card,
        bg="#0f172a",
        fg="#e2e8f0",
        insertbackground="#f8fafc",
        relief="flat",
        highlightbackground="#334155",
        highlightthickness=1,
        font=("Microsoft YaHei UI", 9),
        height=7,
        wrap="word",
        padx=8,
        pady=6,
    )
    txt_desc.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    # 4. 脱敏环境信息勾选与摘要
    tk.Checkbutton(
        card,
        text="附带自动采集的脱敏环境与窗口信息（不含本机路径、用户名或账号数据）",
        variable=var_include_diag,
        bg="#1e293b",
        fg="#cbd5e1",
        selectcolor="#0f172a",
        activebackground="#1e293b",
        activeforeground="#ffffff",
        font=("Microsoft YaHei UI", 8),
        anchor="w",
    ).pack(fill="x", padx=10, pady=(0, 4))

    env_preview = (
        f"版本 {diag_info['version']} · {diag_info['os']} · 屏幕 {diag_info['screen_resolution']} "
        f"({diag_info['dpi_scale']}×) · 游戏窗 {diag_info['game_client_size']} · "
        f"HUD {'贴身角标' if diag_info['hud_mode'] == 'diegetic_badges' else '紧凑浮窗'} · 场景 {diag_info['active_view_mode']}"
    )
    tk.Label(
        card,
        text=env_preview,
        bg="#0f172a",
        fg="#94a3b8",
        font=("Microsoft YaHei UI", 8),
        anchor="w",
        justify="left",
        wraplength=460,
        padx=8,
        pady=5,
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
            zpath = export_diagnostic_zip(diag_info, log_path=log_path, hud_config=hud_config)
            exported_zip[0] = zpath
            var_status.set(f"✅ 已导出脱敏诊断包：{os.path.basename(zpath)}（可直接拖入 GitHub Issue）")
            reveal_file_in_explorer(zpath)
        except Exception as e:
            messagebox.showerror("导出诊断包失败", str(e), parent=dlg)

    def _on_copy_md() -> None:
        md = _build_current_md()
        _copy_to_clipboard(md)
        var_status.set("✅ 已复制脱敏反馈 Markdown 到剪贴板，可直接粘贴到 GitHub / 群聊")

    def _on_submit_github() -> None:
        md = _build_current_md()
        repo = meta.get("github_repo", "")
        url, truncated = build_github_issue_url(repo, var_cat.get(), var_title.get(), md)
        if not url:
            _copy_to_clipboard(md)
            messagebox.showinfo(
                "尚未配置 GitHub 仓库地址",
                "已将脱敏反馈 Markdown 复制到剪贴板！\n\n"
                "你可以直接粘贴发给开发者，或在 config/app_meta.json 中填入 github_repo（格式 owner/repo）即可一键直达 GitHub Issue 页面。",
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

    # 底部按钮栏
    btn_bar = tk.Frame(dlg, bg="#0f172a")
    btn_bar.pack(fill="x", padx=16, pady=(0, 6))
    tk.Button(
        btn_bar,
        text="📦 导出诊断包（可选）",
        command=_on_export_zip,
        bg="#334155",
        fg="#f8fafc",
        activebackground="#475569",
        activeforeground="#ffffff",
        relief="flat",
        font=("Microsoft YaHei UI", 9),
        padx=10,
        pady=7,
        cursor="hand2",
    ).pack(side="left", padx=(0, 8))

    tk.Button(
        btn_bar,
        text="📋 复制内容",
        command=_on_copy_md,
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
        btn_bar,
        text="🚀 打开 GitHub 提交",
        command=_on_submit_github,
        bg="#16a34a",
        fg="#ffffff",
        activebackground="#15803d",
        activeforeground="#ffffff",
        relief="flat",
        font=("Microsoft YaHei UI", 9, "bold"),
        padx=12,
        pady=7,
        cursor="hand2",
    ).pack(side="right", fill="x", expand=True)

    tk.Label(
        dlg,
        textvariable=var_status,
        bg="#0f172a",
        fg="#7dd3fc",
        font=("Microsoft YaHei UI", 8),
        anchor="w",
        justify="left",
        wraplength=486,
    ).pack(fill="x", padx=16, pady=(0, 12))

    return dlg
