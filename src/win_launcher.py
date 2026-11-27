#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win_launcher.py — HoopCoach · 星奈军师桌面控制台与实时悬浮军师入口

设计目标：
- 双击 HoopCoach.exe 即可使用，清爽直观掌控实时护航与战绩复盘
- 一个轻巧明亮的桌面控制窗集中管理：
  1. 游戏日志目录自动探测 / 一键手动绑定
  2. 游戏内悬浮 HUD 开关、「仅游戏在前台时显示」开关、浮窗位置复位
  3. 流派策略（训练/商店/扭蛋）、偏科属性倍率、各模块推荐开关（实时保存并热生效）
  4. 一键打开本地作战室网页（战绩复盘 & 实盘笔记/器材配装）与一键扫描历史战绩
- 严格外部只读监听游戏明文日志（_game_data_/Documents/Logs），绝不包含任何自动点击或内存读取逻辑
"""

from __future__ import annotations

import ctypes
from datetime import datetime
import base64
import glob
import io
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from typing import Optional

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

try:
    from tkinter import font as tkfont
except Exception:
    tkfont = None


def _pil_to_tk_photo(pil_img):
    """将 PIL.Image 转为 Tk PhotoImage：优先 PIL.ImageTk，缺失时回退 Tk 8.6 原生 PNG data（兼容 rt1 热更壳）。"""
    if pil_img is None or tk is None:
        return None
    try:
        from PIL import ImageTk  # type: ignore

        return ImageTk.PhotoImage(pil_img)
    except Exception:
        pass
    try:
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return tk.PhotoImage(data=b64)
    except Exception:
        return None

# 确保 src 目录可被导入（兼容源码运行与 PyInstaller 冻结模式）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import analyzer
import dashboard_server
import equipment_advisor as ea
import issue_reporter
import live_coach as lc
from log_mirror import LiveLogMirror
import note_advisor as na
import platform_compat as pc
from skill_advisor import evaluate_skill_choices
import updater


STRATEGY_OPTIONS = [
    ("training_flow", "训练流 · 均衡冲分（推荐）"),
    ("store_flow", "商店流 · 代币买药特化"),
    ("gacha_flow", "扭蛋流 · 摇奖扭蛋特化"),
]

FOCUS_STAT_OPTIONS = [
    (None, "无偏科 · 纯总评最优"),
    (101, "偏投射"),
    (102, "偏弹跳"),
    (103, "偏防守"),
    (104, "偏体能"),
    (105, "偏运控"),
    (106, "偏意识"),
]

FOCUS_BONUS_OPTIONS = [
    (1.15, "1.15× 轻度"),
    (1.30, "1.30× 强力"),
]

# 亮色日系校园篮球番 × 漫画硬边贴纸色板（与贴身角标 HUD / 作战室 / 官网共享 Design Tokens）
THEME = {
    "bg": "#F5F8FF",               # 画纸奶白底
    "bg_alt": "#EAF1FF",           # 次浅底 / 轨道底
    "surface": "#FFFFFF",          # 纯白卡片底
    "surface_hover": "#F0F5FF",    # 卡片内悬停浅底
    "well": "#EAF1FF",             # 控件槽 / 胶囊轨道底
    "well_hover": "#DCE7FF",       # 控件槽悬停
    "ink": "#101B33",              # 漫画深墨蓝描边 & 主文字
    "border": "#1D2F55",           # 卡片次级墨蓝描边
    "card_shadow": "#1D2F55",      # 轻巧漫画硬偏移阴影
    "divider": "#DCE6FA",          # 卡片内柔和分隔线
    "text": "#101B33",             # 主文字（深墨蓝）
    "text_secondary": "#435370",   # 次级正文
    "muted": "#6C7D9C",            # 辅助灰蓝
    "subtle": "#6C7D9C",           # 弱提示文字
    "blue": "#1B5BFF",             # 运动宝蓝（选中页签 / 强调）
    "blue_deep": "#1139A6",        # 深运动蓝
    "sky": "#22A7FF",              # 清透天蓝
    "accent": "#1B5BFF",           # 强调链接蓝
    "accent_hover": "#1139A6",     # 链接悬停蓝
    "primary": "#FF5126",          # 炽热篮球橙红（主 CTA：打开作战室）
    "primary_hover": "#E03A12",    # 炽橙悬停
    "gold": "#FFB703",             # 冠军金
    "gold_soft": "#FFF6DB",        # 暖金浅底（看板娘气泡）
    "gold_fg": "#8A4F00",          # 暖金深字
    "secondary_btn": "#EAF1FF",    # 次级按钮浅蓝底
    "secondary_btn_hover": "#DCE7FF",
    "seg_active_bg": "#1B5BFF",    # 分段胶囊选中底（宝蓝）
    "seg_active_fg": "#FFFFFF",    # 分段胶囊选中字
    "ok_bg": "#E6FAF4",            # 薄荷绿浅底
    "ok_fg": "#097A58",            # 深薄荷绿字（亮底高对比）
    "ok_dot": "#12B886",           # 薄荷绿运行圆点
    "hud_on_bg": "#12B886",        # HUD 运行态薄荷绿按钮
    "hud_on_hover": "#0CA678",     # HUD 运行态悬停
    "hud_off_bg": "#435370",       # HUD 暂停态深灰蓝按钮
    "hud_off_hover": "#2E3E5C",    # HUD 暂停态悬停
    "warn_dot": "#FFB703",         # 警告冠军金圆点
    "warn_bg": "#FFB703",          # 更新高亮底（冠军金）
    "warn_hover": "#E8A200",       # 更新高亮悬停
    "warn_fg": "#101B33",          # 更新高亮深墨蓝字
    "err_dot": "#F03E5A",          # 异常莓红圆点
    "berry": "#F03E5A",            # 警示莓红
    "feedback_bg": "#E8F0FF",      # 反馈按钮浅宝蓝底
    "feedback_hover": "#D4E4FF",   # 反馈按钮悬停
    "feedback_fg": "#1139A6",      # 反馈按钮深宝蓝字
    "scroll_thumb": "#B9CAE8",     # 亮色滚动条滑块
    "scroll_thumb_hover": "#1B5BFF",
}

FONT_FAMILY = "Microsoft YaHei UI" if pc.IS_WINDOWS else "Noto Sans CJK SC"

# Q 版看板娘精选顺位与元气应援台词
MASCOT_FEATURED_IDS = (1001, 1002, 1046, 1012, 1007, 1030, 1045, 1004, 1021, 1018)

MASCOT_FALLBACK_NAMES: dict[int, str] = {
    1001: "澪奈",
    1002: "龙宫寺",
    1004: "和泉",
    1007: "闪堂",
    1008: "仓理",
    1009: "克拉丽丝",
    1011: "羽田",
    1012: "望月",
    1013: "百目鬼",
    1016: "雨森",
    1017: "皆川",
    1018: "毒岛",
    1020: "佐久间",
    1021: "千圣",
    1025: "春澄",
    1030: "真崎",
    1036: "和歌",
    1042: "远野",
    1045: "白神",
    1046: "希雅",
}

MASCOT_COACH_QUIPS: dict[int, str] = {
    1001: "今天也要元气满满冲破万！",
    1002: "彩圈一亮，直接拉满拿下！",
    1046: "稳住节奏，每回合都在涨分！",
    1012: "体力算准，绝不白白赌失败率！",
    1007: "外线手感火热，这把稳了！",
    1030: "控场到位，队友羁绊全拉满！",
    1045: "冷静看 EV，稳稳冲出新纪录！",
    1004: "内线卡位就绪，篮板全部收下！",
    1021: "篮下统治力全开，冲向 S+！",
    1018: "关键回合果断出手，绝不手软！",
}

_MASCOT_GENERIC_QUIPS = (
    "场边应援已就位，这把一起冲新高！",
    "哨子充能看好时机，收益直接起飞！",
    "商店好货别错过，属性冲起来！",
    "羁绊拉满彩圈多，破万近在眼前！",
)

_LAUNCHER_PREFS_REL = os.path.join("config", "launcher_ui.json")


def load_launcher_ui_prefs(base_dir: Optional[str] = None) -> dict:
    """读取桌面控制台轻量 UI 偏好（如 Q 版看板娘编号），缺省安全返回空字典。"""
    root = base_dir or pc.BASE_DIR
    path = os.path.join(root, _LAUNCHER_PREFS_REL)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_launcher_ui_prefs(prefs: dict, base_dir: Optional[str] = None) -> None:
    """保存桌面控制台轻量 UI 偏好到 config/launcher_ui.json（静默容错）。"""
    root = base_dir or pc.BASE_DIR
    path = os.path.join(root, _LAUNCHER_PREFS_REL)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception:
        pass


def discover_mascot_stickers(base_dir: Optional[str] = None) -> list[dict]:
    """扫描可用的 Q 版篮球少女贴纸素材（优先 heads/q_head_s_*.png，回退 site/q_*.png）。"""
    root = base_dir or pc.BASE_DIR
    found: dict[int, dict] = {}

    head_pattern = os.path.join(root, "data", "hoopgirls", "heads", "q_head_s_*.png")
    for fp in sorted(glob.glob(head_pattern)):
        bn = os.path.basename(fp)
        m = re.match(r"^q_head_s_(\d+)_(.+)\.png$", bn)
        if not m:
            continue
        cid = int(m.group(1))
        name = m.group(2).strip() or MASCOT_FALLBACK_NAMES.get(cid, f"球员{cid}")
        quip = MASCOT_COACH_QUIPS.get(
            cid, _MASCOT_GENERIC_QUIPS[cid % len(_MASCOT_GENERIC_QUIPS)]
        )
        found[cid] = {"char_id": cid, "name": name, "path": fp, "quip": quip}

    if not found:
        fallback_patterns = (
            os.path.join(root, "packaging", "assets", "site", "q_*.png"),
            os.path.join(root, "assets", "site", "q_*.png"),
        )
        for pat in fallback_patterns:
            for fp in sorted(glob.glob(pat)):
                bn = os.path.basename(fp)
                m = re.match(r"^q_(\d+)\.png$", bn)
                if not m:
                    continue
                cid = int(m.group(1))
                name = MASCOT_FALLBACK_NAMES.get(cid, f"球员{cid}")
                quip = MASCOT_COACH_QUIPS.get(
                    cid, _MASCOT_GENERIC_QUIPS[cid % len(_MASCOT_GENERIC_QUIPS)]
                )
                found[cid] = {"char_id": cid, "name": name, "path": fp, "quip": quip}
            if found:
                break

    if not found:
        return []

    featured_rank = {cid: idx for idx, cid in enumerate(MASCOT_FEATURED_IDS)}
    return sorted(
        found.values(),
        key=lambda item: (featured_rank.get(item["char_id"], 9999), item["char_id"]),
    )


def render_mascot_sticker_image(
    img_path: str,
    size: int = 44,
    hover: bool = False,
    bg_hex: str = "#FFFFFF",
):
    """用 Pillow 2× 超采样渲染带墨蓝硬边与漫画阴影的圆角 Q 版贴纸（缺库/缺图返回 None）。"""
    if not img_path or not os.path.isfile(img_path):
        return None
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        return None

    try:
        scale = 2
        offset = 2
        out_w = size + offset
        out_h = size + offset
        w2, h2 = out_w * scale, out_h * scale
        s2 = size * scale
        off2 = offset * scale
        rad2 = 11 * scale
        border_w = 2 * scale

        canvas = Image.new("RGBA", (w2, h2), bg_hex)
        draw = ImageDraw.Draw(canvas)

        # 硬边漫画偏移阴影
        draw.rounded_rectangle(
            [off2, off2, off2 + s2 - 1, off2 + s2 - 1],
            radius=rad2,
            fill=THEME["ink"],
        )

        # 圆角底色
        fill_bg = THEME["gold_soft"] if hover else THEME["well"]
        draw.rounded_rectangle(
            [0, 0, s2 - 1, s2 - 1],
            radius=rad2,
            fill=fill_bg,
        )

        # 头像等比缩放并按圆角裁剪贴入
        inner_pad = border_w + 1
        inner_box = s2 - inner_pad * 2
        with Image.open(img_path) as raw:
            avatar = raw.convert("RGBA")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
        # 适度放大 8% 让 Q 版头部更饱满可爱
        crop_s = int(inner_box * 1.08)
        avatar = avatar.resize((crop_s, crop_s), resample)
        dx = (crop_s - inner_box) // 2
        dy = max(0, int((crop_s - inner_box) * 0.35))
        avatar_cropped = avatar.crop((dx, dy, dx + inner_box, dy + inner_box))

        mask = Image.new("L", (inner_box, inner_box), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, inner_box - 1, inner_box - 1],
            radius=max(4, rad2 - inner_pad),
            fill=255,
        )
        tile = Image.new("RGBA", (inner_box, inner_box), (0, 0, 0, 0))
        tile.paste(avatar_cropped, (0, 0), mask)
        canvas.alpha_composite(tile, (inner_pad, inner_pad))

        # 清晰深墨蓝 / 宝蓝描边
        stroke_col = THEME["blue"] if hover else THEME["ink"]
        draw.rounded_rectangle(
            [0, 0, s2 - 1, s2 - 1],
            radius=rad2,
            outline=stroke_col,
            width=border_w,
        )

        return canvas.resize((out_w, out_h), resample).convert("RGB")
    except Exception:
        return None


class LQSNCoachDesktopApp:
    """HoopCoach 桌面控制台 + 游戏内悬浮 HUD 统一调度器（亮色校园漫画风双页签布局）。"""

    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("当前 Python 环境未安装 tkinter（Windows 官方 Python 默认自带；Linux 请安装 python3-tk）。")
        if pc.IS_WINDOWS:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title("HoopCoach · 星奈军师")
        screen_h = int(self.root.winfo_screenheight() or 900)
        screen_w = int(self.root.winfo_screenwidth() or 1440)
        init_w = min(532, max(472, screen_w - 40))
        init_h = min(584, max(500, screen_h - 90))
        self.root.geometry(f"{init_w}x{init_h}")
        self.root.minsize(468, min(500, init_h))
        self.root.configure(bg=THEME["bg"])
        self._apply_window_icon()
        self._use_bold_cjk = self._detect_bold_cjk_support()

        self._closing = False
        self._attaching_log = False
        self._scanning_runs = False
        self._checking_updates = False
        self._latest_update_res = None
        self._app_meta = updater.load_app_meta()
        self._dashboard_srv = None
        self._dashboard_url = ""
        self._dashboard_engine = None
        self._dashboard_monitor = None
        self._note_static_db = None
        self._note_pool_cache: dict = {}
        self._note_pool_mtime: float = -2.0
        self._event_db: Optional[dict] = None
        self._logged_runtime_errors: set[tuple[str, str, str]] = set()
        self._scroll_canvas = None
        self._scroll_bar = None
        self._scroll_window_id = None
        self._cards_host = None
        self._active_tab = "home"
        self._tab_frames: dict[str, tk.Frame] = {}
        self._tab_buttons: dict[str, tk.Button] = {}
        self._mode_buttons: dict[str, tk.Button] = {}
        self._wrap_labels: list[tuple[tk.Label, int]] = []
        self._status_dot_lbl: Optional[tk.Label] = None
        self._btn_update_settings: Optional[tk.Button] = None
        self._update_highlighted = False
        self._mascot_items: list[dict] = discover_mascot_stickers(pc.BASE_DIR)
        self._mascot_index: int = self._resolve_initial_mascot_index()
        self._mascot_photo_normal = None
        self._mascot_photo_hover = None
        self._mascot_btn: Optional[tk.Label] = None
        self._mascot_chip_lbl: Optional[tk.Label] = None
        self._mascot_quip_bubble: Optional[tk.Frame] = None
        self._mascot_quip_after_id = None
        self.var_mascot_name = tk.StringVar(value="")
        self.var_mascot_quip = tk.StringVar(value="")

        self.docs_dir, self.log_dir = pc.resolve_game_dirs(interactive_on_missing=False)
        self.hud_cfg = lc.load_hud_config()
        self.strategy_name = "training_flow"
        self.strategy = lc.load_strategy(self.strategy_name)

        self.mirror: Optional[LiveLogMirror] = None
        self._last_rendered_sig = ""
        self._last_note_char_id: Optional[int] = None
        self._last_story_key = None

        # Tk 控制变量
        self.var_hud_enabled = tk.BooleanVar(value=True)
        self.var_follow_focus = tk.BooleanVar(value=True)
        self.var_auto_check_updates = tk.BooleanVar(
            value=bool(self.hud_cfg.get("auto_check_updates", True))
        )
        self.var_diegetic_mode = tk.StringVar(
            value="diegetic" if bool(self.hud_cfg.get("diegetic_mode", True)) else "compact"
        )
        self.var_hud_hint = tk.StringVar(value="")
        self.var_strategy = tk.StringVar(value=STRATEGY_OPTIONS[0][1])
        cur_fc = self.hud_cfg.get("focus") or {}
        cur_stat = cur_fc.get("stat")
        cur_bonus = float(cur_fc.get("bonus") or 1.15)
        self.var_focus_stat = tk.StringVar(
            value=next((lbl for sid, lbl in FOCUS_STAT_OPTIONS if sid == cur_stat), FOCUS_STAT_OPTIONS[0][1])
        )
        self.var_focus_bonus = tk.StringVar(
            value=next(
                (lbl for bv, lbl in FOCUS_BONUS_OPTIONS if abs(bv - cur_bonus) < 0.05),
                FOCUS_BONUS_OPTIONS[0][1],
            )
        )
        self.var_recs: dict[str, tk.BooleanVar] = {}
        for key, _label, _desc, _aliases in lc.HUD_RECOMMENDATION_SPECS:
            self.var_recs[key] = tk.BooleanVar(value=lc.is_hud_rec_enabled(self.hud_cfg, key))

        self.var_recs_summary = tk.StringVar(value="")
        self.var_status_badge = tk.StringVar(value="正在初始化...")
        self.var_dir_display = tk.StringVar(value=self.log_dir or "未绑定游戏日志目录")
        self.var_live_preview = tk.StringVar(value="等待进入《篮球少女》育成或选人界面...")
        self.var_scan_status = tk.StringVar(value="本地作战室就绪")

        self._setup_styles()
        self._build_ui()
        self._refresh_recs_summary()

        # 在同一 Tk 主循环下挂载游戏内悬浮 HUD（Toplevel + 分层角标池）
        self.hud = pc.WindowsFloatingHUD(
            strategy=self.strategy,
            auto_play=False,
            stat_names=lc.STAT_NAMES,
            get_whistle_step_fn=lc.get_whistle_step,
            note_markup_fn=lc.format_character_note_advice_hud_markup,
            hud_config=self.hud_cfg,
            master=self.root,
            follow_game_focus=self.var_follow_focus.get(),
            diegetic_mode=(self.var_diegetic_mode.get() == "diegetic"),
        )
        self.hud.on_diegetic_changed_cb = self._on_hud_diegetic_synced
        self.var_diegetic_mode.set("diegetic" if self.hud.diegetic_mode else "compact")
        self._sync_mode_segment_styles()
        self._refresh_hud_hint_text()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.log_dir and os.path.isdir(self.log_dir):
            self._start_attach_latest_async()
        else:
            self._set_connection_status("warn", "未定位游戏目录，请点击右侧选择")
            self.root.after(350, self._prompt_first_time_dir)

        self.root.after(220, self._poll_log_tick)
        if self.var_auto_check_updates.get():
            self.root.after(1800, lambda: self._start_check_updates(silent=True))

    def _apply_window_icon(self) -> None:
        """Windows 控制台窗口接入专属篮球 .ico 图标（Linux / 缺失时静默跳过）。"""
        candidates = (
            os.path.join(pc.BASE_DIR, "packaging", "assets", "hoopcoach.ico"),
            os.path.join(pc.BASE_DIR, "assets", "hoopcoach.ico"),
        )
        ico_path = next((p for p in candidates if os.path.isfile(p)), "")
        if not ico_path:
            return
        if pc.IS_WINDOWS:
            try:
                self.root.iconbitmap(default=ico_path)
                return
            except Exception:
                pass
        try:
            from PIL import Image  # type: ignore

            img = Image.open(ico_path).resize((64, 64))
            photo = _pil_to_tk_photo(img)
            if photo is not None:
                self._window_icon_photo = photo
                self.root.iconphoto(True, photo)
        except Exception:
            pass

    def _detect_bold_cjk_support(self) -> bool:
        """Windows 原生 GDI/DirectWrite 与启用 Xft 的 Linux Tk 支持中文粗体；无 Xft 点阵 Tk 回退常规字重避免缺字。"""
        if pc.IS_WINDOWS or tkfont is None:
            return True
        try:
            actual_fam = str(
                tkfont.Font(root=self.root, family=FONT_FAMILY, size=10).actual().get("family") or ""
            ).lower()
            return actual_fam not in ("", "fixed")
        except Exception:
            return True

    def _font(self, size: int, bold: bool = False) -> tuple:
        if getattr(self, "_use_bold_cjk", True):
            return (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)
        # 无 Xft 的 X11 点阵 Tk 仅在 8~10pt 拥有完整 CJK 字形表
        safe_size = min(10, max(8, size))
        return (FONT_FAMILY, safe_size)

    def _resolve_initial_mascot_index(self) -> int:
        if not self._mascot_items:
            return 0
        prefs = load_launcher_ui_prefs(pc.BASE_DIR)
        saved_id = prefs.get("mascot_char_id")
        if saved_id is not None:
            try:
                target_id = int(saved_id)
                for idx, item in enumerate(self._mascot_items):
                    if item["char_id"] == target_id:
                        return idx
            except Exception:
                pass
        return 0

    def _current_mascot(self) -> Optional[dict]:
        if not self._mascot_items:
            return None
        return self._mascot_items[self._mascot_index % len(self._mascot_items)]

    def _refresh_mascot_sticker(self) -> None:
        item = self._current_mascot()
        if not item:
            self.var_mascot_name.set("")
            return
        self.var_mascot_name.set(f"★ {item['name']}")
        if self._mascot_btn is None:
            return
        try:
            img_norm = render_mascot_sticker_image(
                item["path"], size=44, hover=False, bg_hex=THEME["surface"]
            )
            img_hov = render_mascot_sticker_image(
                item["path"], size=44, hover=True, bg_hex=THEME["surface"]
            )
            if img_norm is None:
                return
            photo_norm = _pil_to_tk_photo(img_norm)
            if photo_norm is None:
                return
            self._mascot_photo_normal = photo_norm
            self._mascot_photo_hover = _pil_to_tk_photo(img_hov) or self._mascot_photo_normal
            self._mascot_btn.configure(image=self._mascot_photo_normal)
        except Exception:
            pass

    def _show_mascot_quip(self, item: dict) -> None:
        quip_text = f"★ {item['name']}：{item['quip']}"
        self.var_mascot_quip.set(quip_text)
        if not hasattr(self, "root") or self.root is None:
            return
        try:
            if self._mascot_quip_after_id is not None:
                self.root.after_cancel(self._mascot_quip_after_id)
                self._mascot_quip_after_id = None
            if self._mascot_quip_bubble is not None:
                self._mascot_quip_bubble.destroy()
                self._mascot_quip_bubble = None

            bubble = tk.Frame(
                self.root,
                bg=THEME["gold_soft"],
                highlightbackground=THEME["ink"],
                highlightthickness=2,
                padx=10,
                pady=3,
            )
            tk.Label(
                bubble,
                text=quip_text,
                bg=THEME["gold_soft"],
                fg=THEME["ink"],
                font=self._font(8, bold=True),
            ).pack()
            bubble.place(x=74, y=62)
            bubble.lift()
            self._mascot_quip_bubble = bubble
            self._mascot_quip_after_id = self.root.after(3400, self._hide_mascot_quip)
        except Exception:
            pass

    def _hide_mascot_quip(self) -> None:
        self._mascot_quip_after_id = None
        if self._mascot_quip_bubble is not None:
            try:
                self._mascot_quip_bubble.destroy()
            except Exception:
                pass
            self._mascot_quip_bubble = None

    def _on_rotate_mascot(self, _event=None) -> None:
        """点击顶栏 Q 版贴纸轮换篮球少女，记住偏好并弹出轻快应援气泡。"""
        if not self._mascot_items:
            return
        if len(self._mascot_items) > 1:
            self._mascot_index = (self._mascot_index + 1) % len(self._mascot_items)
        item = self._current_mascot()
        if not item:
            return
        prefs = load_launcher_ui_prefs(pc.BASE_DIR)
        prefs["mascot_char_id"] = item["char_id"]
        save_launcher_ui_prefs(prefs, pc.BASE_DIR)
        self._refresh_mascot_sticker()
        self._show_mascot_quip(item)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Anime.TCombobox",
            fieldbackground=THEME["bg"],
            background=THEME["well"],
            foreground=THEME["text"],
            selectbackground=THEME["bg"],
            selectforeground=THEME["text"],
            arrowcolor=THEME["ink"],
            bordercolor=THEME["border"],
            lightcolor=THEME["border"],
            darkcolor=THEME["border"],
            padding=(10, 6),
            font=self._font(9),
        )
        style.map(
            "Anime.TCombobox",
            fieldbackground=[("readonly", THEME["bg"]), ("focus", THEME["surface"])],
            foreground=[("readonly", THEME["text"]), ("disabled", THEME["muted"])],
            background=[("active", THEME["well_hover"]), ("pressed", "#C7DAFF")],
            bordercolor=[("focus", THEME["blue"]), ("hover", THEME["blue"])],
            lightcolor=[("focus", THEME["blue"]), ("hover", THEME["blue"])],
            darkcolor=[("focus", THEME["blue"]), ("hover", THEME["blue"])],
            arrowcolor=[("active", THEME["blue"]), ("hover", THEME["blue"])],
            selectbackground=[("readonly", THEME["bg"])],
            selectforeground=[("readonly", THEME["text"])],
        )
        # 下拉弹出列表同步亮色漫画高对比风格，杜绝黑底黑字或白底白字
        try:
            self.root.option_add("*TCombobox*Listbox.background", THEME["surface"])
            self.root.option_add("*TCombobox*Listbox.foreground", THEME["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", THEME["blue"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
            self.root.option_add("*TCombobox*Listbox.font", self._font(9))
        except Exception:
            pass
        style.configure(
            "Anime.Vertical.TScrollbar",
            background=THEME["scroll_thumb"],
            troughcolor=THEME["bg"],
            bordercolor=THEME["bg"],
            lightcolor=THEME["scroll_thumb"],
            darkcolor=THEME["scroll_thumb"],
            arrowcolor=THEME["border"],
            relief="flat",
            width=8,
        )
        style.map(
            "Anime.Vertical.TScrollbar",
            background=[("active", THEME["scroll_thumb_hover"]), ("pressed", THEME["blue_deep"])],
        )

    def _sync_scroll_layout(self, _event=None) -> None:
        if self._scroll_canvas is None or self._cards_host is None:
            return
        try:
            c_w = self._scroll_canvas.winfo_width()
            c_h = self._scroll_canvas.winfo_height()
            if c_w > 40 and self._scroll_window_id is not None:
                self._scroll_canvas.itemconfigure(self._scroll_window_id, width=c_w)
                for lbl, inset in self._wrap_labels:
                    try:
                        lbl.configure(wraplength=max(240, c_w - inset))
                    except Exception:
                        pass
            req_h = self._cards_host.winfo_reqheight()
            self._scroll_canvas.configure(scrollregion=(0, 0, max(c_w, 1), max(req_h, 1)))
            if self._scroll_bar is not None:
                need_scroll = req_h > max(c_h, 1) + 2
                if need_scroll and not self._scroll_bar.winfo_ismapped():
                    self._scroll_bar.pack(side="right", fill="y", padx=(0, 4), pady=(0, 4))
                elif not need_scroll and self._scroll_bar.winfo_ismapped():
                    self._scroll_bar.pack_forget()
                    self._scroll_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _on_mousewheel(self, event) -> None:
        if self._scroll_canvas is None or self._cards_host is None:
            return
        try:
            # 仅在内容高度超出视口时滚动，且避免干扰下拉列表自身滚轮
            if isinstance(event.widget, ttk.Combobox):
                return
            if self._cards_host.winfo_reqheight() <= self._scroll_canvas.winfo_height() + 2:
                return
            if getattr(event, "num", None) == 4:
                self._scroll_canvas.yview_scroll(-2, "units")
            elif getattr(event, "num", None) == 5:
                self._scroll_canvas.yview_scroll(2, "units")
            elif getattr(event, "delta", 0):
                step = -1 if event.delta > 0 else 1
                units = max(1, int(abs(event.delta) / 120)) * 2
                self._scroll_canvas.yview_scroll(step * units, "units")
        except Exception:
            pass

    def _bind_hover(self, widget: tk.Widget, normal_bg: str, hover_bg: str) -> None:
        def _enter(_e):
            try:
                if str(widget.cget("state")) != "disabled":
                    widget.configure(bg=hover_bg)
            except Exception:
                pass

        def _leave(_e):
            try:
                widget.configure(bg=normal_bg)
            except Exception:
                pass

        widget.bind("<Enter>", _enter, add="+")
        widget.bind("<Leave>", _leave, add="+")

    def _make_card(
        self,
        parent: tk.Widget,
        title: Optional[str] = None,
        right_text: Optional[str] = None,
        right_var: Optional[tk.StringVar] = None,
        right_fg: Optional[str] = None,
        pady_bottom: int = 12,
        accent_color: Optional[str] = None,
    ) -> tuple[tk.Frame, Optional[tk.Frame]]:
        """创建带漫画硬边描边与轻巧偏移阴影的纯白卡片。"""
        wrapper = tk.Frame(parent, bg=THEME["bg"])
        wrapper.pack(fill="x", padx=20, pady=(0, pady_bottom))

        shadow = tk.Frame(wrapper, bg=THEME["card_shadow"])
        shadow.place(x=3, y=3, relwidth=1.0, relheight=1.0, width=-3, height=-3)

        outer = tk.Frame(
            wrapper,
            bg=THEME["surface"],
            highlightbackground=THEME["ink"],
            highlightthickness=2,
        )
        outer.pack(fill="x", padx=(0, 3), pady=(0, 3))
        outer.lift()

        hdr_row = None
        if title:
            hdr_row = tk.Frame(outer, bg=THEME["surface"])
            hdr_row.pack(fill="x", padx=16, pady=(12, 9))
            tk.Frame(
                hdr_row,
                bg=accent_color or THEME["blue"],
                width=4,
                height=14,
            ).pack(side="left", padx=(0, 7))
            tk.Label(
                hdr_row,
                text=title,
                bg=THEME["surface"],
                fg=THEME["ink"],
                font=self._font(9, bold=True),
                anchor="w",
            ).pack(side="left")
            if right_var is not None:
                tk.Label(
                    hdr_row,
                    textvariable=right_var,
                    bg=THEME["surface"],
                    fg=right_fg or THEME["blue"],
                    font=self._font(8, bold=True),
                    anchor="e",
                ).pack(side="right")
            elif right_text:
                tk.Label(
                    hdr_row,
                    text=right_text,
                    bg=THEME["surface"],
                    fg=right_fg or THEME["muted"],
                    font=self._font(8, bold=True),
                    anchor="e",
                ).pack(side="right")

        body = tk.Frame(outer, bg=THEME["surface"])
        body.pack(fill="x", padx=16, pady=(0 if title else 14, 14))
        return body, hdr_row

    def _set_connection_status(self, level: str, text: str) -> None:
        self.var_status_badge.set(text)
        if self._status_dot_lbl is None:
            return
        dot_color = {
            "ok": THEME["ok_dot"],
            "warn": THEME["warn_dot"],
            "err": THEME["err_dot"],
        }.get(level, THEME["muted"])
        try:
            self._status_dot_lbl.configure(fg=dot_color)
        except Exception:
            pass

    def _switch_tab(self, tab_key: str) -> None:
        if tab_key not in self._tab_frames:
            return
        self._active_tab = tab_key
        for k, frame in self._tab_frames.items():
            if k == tab_key:
                frame.pack(fill="x", expand=True)
            else:
                frame.pack_forget()
        for k, btn in self._tab_buttons.items():
            if k == tab_key:
                btn.configure(
                    bg=THEME["seg_active_bg"],
                    fg=THEME["seg_active_fg"],
                    activebackground=THEME["blue_deep"],
                    activeforeground="#FFFFFF",
                    font=self._font(9, bold=True),
                )
            else:
                btn.configure(
                    bg=THEME["well"],
                    fg=THEME["text_secondary"],
                    activebackground=THEME["well_hover"],
                    activeforeground=THEME["ink"],
                    font=self._font(9),
                )
        if self._scroll_canvas is not None:
            try:
                self._scroll_canvas.yview_moveto(0.0)
            except Exception:
                pass
        self.root.after(15, self._sync_scroll_layout)

    def _select_diegetic_mode(self, mode_val: str) -> None:
        if self.var_diegetic_mode.get() == mode_val:
            return
        self.var_diegetic_mode.set(mode_val)
        self._on_diegetic_mode_changed()

    def _sync_mode_segment_styles(self) -> None:
        cur = self.var_diegetic_mode.get()
        for val, btn in self._mode_buttons.items():
            if val == cur:
                btn.configure(
                    bg=THEME["seg_active_bg"],
                    fg=THEME["seg_active_fg"],
                    activebackground=THEME["blue_deep"],
                    activeforeground="#FFFFFF",
                    font=self._font(9, bold=True),
                )
            else:
                btn.configure(
                    bg=THEME["well"],
                    fg=THEME["text_secondary"],
                    activebackground=THEME["well_hover"],
                    activeforeground=THEME["ink"],
                    font=self._font(9),
                )

    def _refresh_recs_summary(self) -> None:
        total = len(self.var_recs)
        enabled = sum(1 for v in self.var_recs.values() if v.get())
        if enabled == total:
            self.var_recs_summary.set(f"推荐模块 · 全部开启（{enabled}/{total}）")
        elif enabled == 0:
            self.var_recs_summary.set(f"推荐模块 · 已全部关闭（0/{total}）")
        else:
            self.var_recs_summary.set(f"推荐模块 · 已开启 {enabled}/{total} 项")

    def _on_enable_all_recs(self) -> None:
        for var in self.var_recs.values():
            var.set(True)
        self._on_recs_changed()

    def _build_ui(self) -> None:
        # ── 0. 顶部校园运动番双色装饰条（炽橙 + 运动宝蓝） ──
        accent_bar = tk.Frame(self.root, bg=THEME["blue"], height=4)
        accent_bar.pack(fill="x")
        accent_bar.pack_propagate(False)
        tk.Frame(accent_bar, bg=THEME["primary"], width=124, height=4).pack(side="left")
        tk.Frame(accent_bar, bg=THEME["gold"], width=28, height=4).pack(side="left")

        # ── 1. 顶栏（白底墨蓝边 + 可点击轮换 Q 版看板娘 + 品牌标题 + 双页签胶囊） ──
        header_shell = tk.Frame(self.root, bg=THEME["surface"])
        header_shell.pack(fill="x")
        top = tk.Frame(header_shell, bg=THEME["surface"])
        top.pack(fill="x", padx=20, pady=(11, 11))
        tk.Frame(header_shell, bg=THEME["ink"], height=2).pack(fill="x")

        brand_left = tk.Frame(top, bg=THEME["surface"])
        brand_left.pack(side="left")

        # 可点击轮换 Q 版看板娘贴纸（缺 Pillow 或缺图时静默降级不崩溃）
        if self._mascot_items:
            first_img = render_mascot_sticker_image(
                self._current_mascot()["path"], size=44, hover=False, bg_hex=THEME["surface"]
            )
            if first_img is not None:
                try:
                    photo_norm = _pil_to_tk_photo(first_img)
                    if photo_norm is None:
                        raise RuntimeError("PhotoImage unavailable")
                    self._mascot_photo_normal = photo_norm
                    hov_img = render_mascot_sticker_image(
                        self._current_mascot()["path"], size=44, hover=True, bg_hex=THEME["surface"]
                    )
                    self._mascot_photo_hover = _pil_to_tk_photo(hov_img) or self._mascot_photo_normal
                    mascot_lbl = tk.Label(
                        brand_left,
                        image=self._mascot_photo_normal,
                        bg=THEME["surface"],
                        bd=0,
                        highlightthickness=0,
                        cursor="hand2",
                    )
                    mascot_lbl.pack(side="left", padx=(0, 10))
                    mascot_lbl.bind("<Button-1>", self._on_rotate_mascot)
                    mascot_lbl.bind(
                        "<Enter>",
                        lambda _e, w=mascot_lbl: w.configure(image=self._mascot_photo_hover)
                        if self._mascot_photo_hover is not None
                        else None,
                    )
                    mascot_lbl.bind(
                        "<Leave>",
                        lambda _e, w=mascot_lbl: w.configure(image=self._mascot_photo_normal)
                        if self._mascot_photo_normal is not None
                        else None,
                    )
                    self._mascot_btn = mascot_lbl
                except Exception:
                    self._mascot_btn = None

        brand_stack = tk.Frame(brand_left, bg=THEME["surface"])
        brand_stack.pack(side="left")
        tk.Label(
            brand_stack,
            text="HoopCoach · 星奈军师",
            bg=THEME["surface"],
            fg=THEME["ink"],
            font=self._font(11, bold=True),
            anchor="w",
        ).pack(anchor="w")

        meta_row = tk.Frame(brand_stack, bg=THEME["surface"])
        meta_row.pack(anchor="w", pady=(3, 0))
        tk.Label(
            meta_row,
            text=str(self._app_meta.get("version") or "v0.10.2"),
            bg=THEME["well"],
            fg=THEME["blue"],
            font=self._font(7, bold=True),
            padx=6,
            pady=1,
        ).pack(side="left")

        if self._mascot_items:
            cur_m = self._current_mascot()
            self.var_mascot_name.set(f"★ {cur_m['name']}")
            chip = tk.Label(
                meta_row,
                textvariable=self.var_mascot_name,
                bg=THEME["gold_soft"],
                fg=THEME["ink"],
                font=self._font(7, bold=True),
                padx=6,
                pady=1,
                cursor="hand2",
            )
            chip.pack(side="left", padx=(6, 0))
            chip.bind("<Button-1>", self._on_rotate_mascot)
            self._bind_hover(chip, THEME["gold_soft"], THEME["gold"])
            self._mascot_chip_lbl = chip

        # 右侧分段导航胶囊：实时护航 | 偏好与工具
        tab_track = tk.Frame(
            top,
            bg=THEME["well"],
            highlightbackground=THEME["ink"],
            highlightthickness=2,
            padx=3,
            pady=3,
        )
        tab_track.pack(side="right")
        for tab_key, tab_label in (("home", "实时护航"), ("settings", "偏好与工具")):
            tb = tk.Button(
                tab_track,
                text=tab_label,
                command=lambda k=tab_key: self._switch_tab(k),
                bg=THEME["well"],
                fg=THEME["text_secondary"],
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=self._font(9),
                padx=11,
                pady=4,
                cursor="hand2",
            )
            tb.pack(side="left")
            self._tab_buttons[tab_key] = tb

        # ── 2. 底部固定栏（实时建议速览 + 轻量更新/反馈入口） ──
        bottom_wrap = tk.Frame(self.root, bg=THEME["bg"])
        bottom_wrap.pack(fill="x", side="bottom", padx=20, pady=(6, 13))

        preview_pill = tk.Frame(
            bottom_wrap,
            bg=THEME["surface"],
            highlightbackground=THEME["ink"],
            highlightthickness=2,
        )
        preview_pill.pack(fill="x")
        tk.Frame(preview_pill, bg=THEME["primary"], width=5).pack(side="left", fill="y")
        lbl_preview = tk.Label(
            preview_pill,
            textvariable=self.var_live_preview,
            bg=THEME["surface"],
            fg=THEME["ink"],
            font=self._font(9, bold=True),
            anchor="w",
            padx=11,
            pady=8,
            wraplength=440,
            justify="left",
        )
        lbl_preview.pack(side="left", fill="x", expand=True)
        self._wrap_labels.append((lbl_preview, 84))

        util_row = tk.Frame(bottom_wrap, bg=THEME["bg"])
        util_row.pack(fill="x", pady=(8, 0))
        self.btn_update = tk.Button(
            util_row,
            text="检查更新",
            command=self._on_click_update,
            bg=THEME["bg"],
            fg=THEME["text_secondary"],
            activebackground=THEME["well"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self._font(8, bold=True),
            padx=6,
            pady=2,
            cursor="hand2",
        )
        self.btn_update.pack(side="left")
        self.btn_update.bind(
            "<Enter>",
            lambda _e: self.btn_update.configure(
                bg=THEME["warn_hover"] if self._update_highlighted else THEME["well"]
            )
            if str(self.btn_update.cget("state")) != "disabled"
            else None,
            add="+",
        )
        self.btn_update.bind(
            "<Leave>",
            lambda _e: self.btn_update.configure(
                bg=THEME["warn_bg"] if self._update_highlighted else THEME["bg"]
            ),
            add="+",
        )

        btn_fb_link = tk.Button(
            util_row,
            text="反馈 Bug / 提建议",
            command=self._on_open_feedback,
            bg=THEME["bg"],
            fg=THEME["text_secondary"],
            activebackground=THEME["well"],
            activeforeground=THEME["blue"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self._font(8),
            padx=6,
            pady=2,
            cursor="hand2",
        )
        btn_fb_link.pack(side="right")
        self._bind_hover(btn_fb_link, THEME["bg"], THEME["well"])

        # ── 3. 中间自适应内容区（大屏零滚动条，高 DPI 小屏自动显现亮色细滚动条） ──
        scroll_wrap = tk.Frame(self.root, bg=THEME["bg"])
        scroll_wrap.pack(fill="both", expand=True, pady=(12, 0))
        self._scroll_canvas = tk.Canvas(
            scroll_wrap,
            bg=THEME["bg"],
            highlightthickness=0,
            bd=0,
        )
        self._scroll_bar = ttk.Scrollbar(
            scroll_wrap,
            orient="vertical",
            command=self._scroll_canvas.yview,
            style="Anime.Vertical.TScrollbar",
        )
        self._scroll_canvas.configure(yscrollcommand=self._scroll_bar.set)
        self._scroll_canvas.pack(side="left", fill="both", expand=True)

        self._cards_host = tk.Frame(self._scroll_canvas, bg=THEME["bg"])
        self._scroll_window_id = self._scroll_canvas.create_window(
            (0, 0),
            window=self._cards_host,
            anchor="nw",
        )
        self._cards_host.bind("<Configure>", self._sync_scroll_layout)
        self._scroll_canvas.bind("<Configure>", self._sync_scroll_layout)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

        # 搭建两个页签容器
        tab_home = tk.Frame(self._cards_host, bg=THEME["bg"])
        tab_settings = tk.Frame(self._cards_host, bg=THEME["bg"])
        self._tab_frames = {"home": tab_home, "settings": tab_settings}

        self._build_home_tab(tab_home)
        self._build_settings_tab(tab_settings)
        self._switch_tab("home")
        self.root.after(40, self._sync_scroll_layout)

    def _build_home_tab(self, parent: tk.Frame) -> None:
        # ── 卡片 1：连接状态 + 实时悬浮窗主控 ──
        c1, _ = self._make_card(parent, title=None, pady_bottom=12)

        conn_row = tk.Frame(c1, bg=THEME["surface"])
        conn_row.pack(fill="x")
        self._status_dot_lbl = tk.Label(
            conn_row,
            text="●",
            bg=THEME["surface"],
            fg=THEME["warn_dot"],
            font=self._font(10),
        )
        self._status_dot_lbl.pack(side="left", padx=(0, 7))
        lbl_status = tk.Label(
            conn_row,
            textvariable=self.var_status_badge,
            bg=THEME["surface"],
            fg=THEME["ink"],
            font=self._font(10, bold=True),
            anchor="w",
            justify="left",
        )
        lbl_status.pack(side="left", fill="x", expand=True)
        btn_dir_quick = tk.Button(
            conn_row,
            text="切换目录",
            command=self._on_choose_game_dir,
            bg=THEME["secondary_btn"],
            fg=THEME["ink"],
            activebackground=THEME["secondary_btn_hover"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=1,
            font=self._font(8, bold=True),
            padx=10,
            pady=3,
            cursor="hand2",
        )
        btn_dir_quick.pack(side="right", padx=(8, 0))
        self._bind_hover(btn_dir_quick, THEME["secondary_btn"], THEME["secondary_btn_hover"])

        # 柔和分隔线
        tk.Frame(c1, bg=THEME["divider"], height=1).pack(fill="x", pady=11)

        # HUD 主开关 + 样式胶囊切换
        hud_row = tk.Frame(c1, bg=THEME["surface"])
        hud_row.pack(fill="x")

        self.btn_toggle_hud = tk.Button(
            hud_row,
            text="● 悬浮窗运行中 · 点击暂停",
            command=self._on_toggle_hud,
            bg=THEME["hud_on_bg"],
            fg="#FFFFFF",
            activebackground=THEME["hud_on_hover"],
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            highlightbackground=THEME["ink"],
            highlightthickness=2,
            font=self._font(9, bold=True),
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.btn_toggle_hud.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.btn_toggle_hud.bind(
            "<Enter>",
            lambda _e: self.btn_toggle_hud.configure(
                bg=THEME["hud_on_hover"] if self.var_hud_enabled.get() else THEME["hud_off_hover"]
            ),
            add="+",
        )
        self.btn_toggle_hud.bind(
            "<Leave>",
            lambda _e: self.btn_toggle_hud.configure(
                bg=THEME["hud_on_bg"] if self.var_hud_enabled.get() else THEME["hud_off_bg"]
            ),
            add="+",
        )

        mode_track = tk.Frame(
            hud_row,
            bg=THEME["well"],
            highlightbackground=THEME["ink"],
            highlightthickness=2,
            padx=3,
            pady=3,
        )
        mode_track.pack(side="right")
        for val, lbl in (("diegetic", "贴身角标"), ("compact", "紧凑浮窗")):
            mb = tk.Button(
                mode_track,
                text=lbl,
                command=lambda v=val: self._select_diegetic_mode(v),
                bg=THEME["well"],
                fg=THEME["text_secondary"],
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=self._font(9),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            mb.pack(side="left")
            self._mode_buttons[val] = mb

        lbl_hint = tk.Label(
            c1,
            textvariable=self.var_hud_hint,
            bg=THEME["surface"],
            fg=THEME["muted"],
            font=self._font(8),
            anchor="w",
            justify="left",
            wraplength=430,
        )
        lbl_hint.pack(fill="x", pady=(10, 0))
        self._wrap_labels.append((lbl_hint, 88))

        # ── 卡片 2：育成策略与偏科（实时热生效） ──
        c2, _ = self._make_card(
            parent,
            title="育成策略",
            right_text="保存即热生效",
            right_fg=THEME["blue"],
            pady_bottom=12,
            accent_color=THEME["blue"],
        )

        row_strat = tk.Frame(c2, bg=THEME["surface"])
        row_strat.pack(fill="x", pady=(0, 9))
        tk.Label(
            row_strat,
            text="育成流派",
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=self._font(9, bold=True),
            width=9,
            anchor="w",
        ).pack(side="left")
        cb_strat = ttk.Combobox(
            row_strat,
            textvariable=self.var_strategy,
            values=[lbl for _, lbl in STRATEGY_OPTIONS],
            state="readonly",
            style="Anime.TCombobox",
        )
        cb_strat.pack(side="left", fill="x", expand=True)
        cb_strat.bind("<<ComboboxSelected>>", self._on_strategy_changed)

        row_focus = tk.Frame(c2, bg=THEME["surface"])
        row_focus.pack(fill="x", pady=(0, 10))
        tk.Label(
            row_focus,
            text="偏科引导",
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=self._font(9, bold=True),
            width=9,
            anchor="w",
        ).pack(side="left")
        cb_fstat = ttk.Combobox(
            row_focus,
            textvariable=self.var_focus_stat,
            values=[lbl for _, lbl in FOCUS_STAT_OPTIONS],
            state="readonly",
            width=18,
            style="Anime.TCombobox",
        )
        cb_fstat.pack(side="left", fill="x", expand=True, padx=(0, 8))
        cb_fstat.bind("<<ComboboxSelected>>", self._on_focus_changed)

        cb_fbonus = ttk.Combobox(
            row_focus,
            textvariable=self.var_focus_bonus,
            values=[lbl for _, lbl in FOCUS_BONUS_OPTIONS],
            state="readonly",
            width=11,
            style="Anime.TCombobox",
        )
        cb_fbonus.pack(side="right")
        cb_fbonus.bind("<<ComboboxSelected>>", self._on_focus_changed)

        # 模块开关摘要桥接条（点击直达第二页）
        mod_bridge = tk.Frame(c2, bg=THEME["surface"])
        mod_bridge.pack(fill="x")
        tk.Label(
            mod_bridge,
            textvariable=self.var_recs_summary,
            bg=THEME["surface"],
            fg=THEME["muted"],
            font=self._font(8),
            anchor="w",
        ).pack(side="left")
        btn_go_mods = tk.Button(
            mod_bridge,
            text="自定义模块 ▶",
            command=lambda: self._switch_tab("settings"),
            bg=THEME["surface"],
            fg=THEME["blue"],
            activebackground=THEME["well"],
            activeforeground=THEME["blue_deep"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self._font(8, bold=True),
            padx=5,
            pady=1,
            cursor="hand2",
        )
        btn_go_mods.pack(side="right")
        self._bind_hover(btn_go_mods, THEME["surface"], THEME["well"])

        # ── 卡片 3：战绩复盘与作战室 ──
        c3, _ = self._make_card(
            parent,
            title="战绩复盘 & 配装作战室",
            right_var=self.var_scan_status,
            right_fg=THEME["blue"],
            pady_bottom=4,
            accent_color=THEME["primary"],
        )
        tool_row = tk.Frame(c3, bg=THEME["surface"])
        tool_row.pack(fill="x")
        btn_dash = tk.Button(
            tool_row,
            text="▶ 打开作战室网页",
            command=self._on_open_dashboard,
            bg=THEME["primary"],
            fg="#FFFFFF",
            activebackground=THEME["primary_hover"],
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            highlightbackground=THEME["ink"],
            highlightthickness=2,
            font=self._font(9, bold=True),
            padx=16,
            pady=8,
            cursor="hand2",
        )
        btn_dash.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self._bind_hover(btn_dash, THEME["primary"], THEME["primary_hover"])

        self.btn_scan_runs = tk.Button(
            tool_row,
            text="扫描历史战绩",
            command=self._on_scan_runs,
            bg=THEME["secondary_btn"],
            fg=THEME["ink"],
            activebackground=THEME["secondary_btn_hover"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=2,
            font=self._font(9, bold=True),
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.btn_scan_runs.pack(side="right")
        self._bind_hover(self.btn_scan_runs, THEME["secondary_btn"], THEME["secondary_btn_hover"])

    def _build_settings_tab(self, parent: tk.Frame) -> None:
        # ── 卡片 1：HUD 推荐模块精细开关 ──
        s1, s1_hdr = self._make_card(
            parent,
            title="HUD 推荐模块开关",
            pady_bottom=12,
            accent_color=THEME["blue"],
        )
        if s1_hdr is not None:
            btn_all_on = tk.Button(
                s1_hdr,
                text="恢复全开",
                command=self._on_enable_all_recs,
                bg=THEME["surface"],
                fg=THEME["blue"],
                activebackground=THEME["well"],
                activeforeground=THEME["blue_deep"],
                relief="flat",
                bd=0,
                highlightthickness=0,
                font=self._font(8, bold=True),
                padx=5,
                pady=1,
                cursor="hand2",
            )
            btn_all_on.pack(side="right")
            self._bind_hover(btn_all_on, THEME["surface"], THEME["well"])

        chk_grid = tk.Frame(s1, bg=THEME["surface"])
        chk_grid.pack(fill="x")
        chk_grid.columnconfigure(0, weight=1)
        chk_grid.columnconfigure(1, weight=1)
        for idx, (key, label, _desc, _aliases) in enumerate(lc.HUD_RECOMMENDATION_SPECS):
            r, c = divmod(idx, 2)
            cb = tk.Checkbutton(
                chk_grid,
                text=f" {label}",
                variable=self.var_recs[key],
                command=self._on_recs_changed,
                bg=THEME["surface"],
                fg=THEME["ink"],
                selectcolor=THEME["well"],
                activebackground=THEME["surface_hover"],
                activeforeground=THEME["blue"],
                bd=0,
                highlightthickness=0,
                font=self._font(9),
                anchor="w",
                cursor="hand2",
            )
            cb.grid(row=r, column=c, sticky="w", padx=(0, 14), pady=4)

        # ── 卡片 2：窗口行为与游戏目录 ──
        s2, _ = self._make_card(
            parent,
            title="窗口行为与日志目录",
            pady_bottom=12,
            accent_color=THEME["ok_dot"],
        )
        tk.Checkbutton(
            s2,
            text=" 仅游戏处于前台时显示（切窗自动隐身，推荐）",
            variable=self.var_follow_focus,
            command=self._on_toggle_follow_focus,
            bg=THEME["surface"],
            fg=THEME["ink"],
            selectcolor=THEME["well"],
            activebackground=THEME["surface_hover"],
            activeforeground=THEME["blue"],
            bd=0,
            highlightthickness=0,
            font=self._font(9),
            anchor="w",
            cursor="hand2",
        ).pack(fill="x", pady=(0, 10))

        dir_row = tk.Frame(s2, bg=THEME["surface"])
        dir_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            dir_row,
            textvariable=self.var_dir_display,
            bg=THEME["well"],
            fg=THEME["text_secondary"],
            highlightbackground="#C5D5F5",
            highlightthickness=1,
            font=self._font(8),
            anchor="w",
            padx=10,
            pady=5,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        btn_choose_dir = tk.Button(
            dir_row,
            text="选择目录",
            command=self._on_choose_game_dir,
            bg=THEME["secondary_btn"],
            fg=THEME["ink"],
            activebackground=THEME["secondary_btn_hover"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=1,
            font=self._font(8, bold=True),
            padx=11,
            pady=4,
            cursor="hand2",
        )
        btn_choose_dir.pack(side="right")
        self._bind_hover(btn_choose_dir, THEME["secondary_btn"], THEME["secondary_btn_hover"])

        reset_row = tk.Frame(s2, bg=THEME["surface"])
        reset_row.pack(fill="x")
        btn_reset = tk.Button(
            reset_row,
            text="复位悬浮窗位置",
            command=self._on_reset_hud_pos,
            bg=THEME["secondary_btn"],
            fg=THEME["ink"],
            activebackground=THEME["secondary_btn_hover"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=1,
            font=self._font(8, bold=True),
            padx=11,
            pady=4,
            cursor="hand2",
        )
        btn_reset.pack(side="left")
        self._bind_hover(btn_reset, THEME["secondary_btn"], THEME["secondary_btn_hover"])
        lbl_borderless = tk.Label(
            reset_row,
            text="独占全屏不可见时请切「无边框窗口化」",
            bg=THEME["surface"],
            fg=THEME["muted"],
            font=self._font(8),
            anchor="w",
            justify="left",
            wraplength=290,
        )
        lbl_borderless.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self._wrap_labels.append((lbl_borderless, 210))

        # ── 卡片 3：热更新与脱敏反馈 ──
        s3, _ = self._make_card(
            parent,
            title="热更新与诊断反馈",
            pady_bottom=4,
            accent_color=THEME["gold"],
        )
        tk.Checkbutton(
            s3,
            text=" 启动时自动检查新版本（仅 ~2 KB 清单，不偷跑流量）",
            variable=self.var_auto_check_updates,
            command=self._on_toggle_auto_check_updates,
            bg=THEME["surface"],
            fg=THEME["ink"],
            selectcolor=THEME["well"],
            activebackground=THEME["surface_hover"],
            activeforeground=THEME["blue"],
            bd=0,
            highlightthickness=0,
            font=self._font(9),
            anchor="w",
            cursor="hand2",
        ).pack(fill="x", pady=(0, 10))

        action_row = tk.Frame(s3, bg=THEME["surface"])
        action_row.pack(fill="x")
        self._btn_update_settings = tk.Button(
            action_row,
            text="检查新版本",
            command=self._on_click_update,
            bg=THEME["secondary_btn"],
            fg=THEME["ink"],
            activebackground=THEME["secondary_btn_hover"],
            activeforeground=THEME["ink"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["border"],
            highlightthickness=1,
            font=self._font(9, bold=True),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self._btn_update_settings.pack(side="left", padx=(0, 10))
        self._btn_update_settings.bind(
            "<Enter>",
            lambda _e: self._btn_update_settings.configure(
                bg=THEME["warn_hover"] if self._update_highlighted else THEME["secondary_btn_hover"]
            )
            if self._btn_update_settings is not None
            and str(self._btn_update_settings.cget("state")) != "disabled"
            else None,
            add="+",
        )
        self._btn_update_settings.bind(
            "<Leave>",
            lambda _e: self._btn_update_settings.configure(
                bg=THEME["warn_bg"] if self._update_highlighted else THEME["secondary_btn"]
            )
            if self._btn_update_settings is not None
            else None,
            add="+",
        )

        btn_feedback = tk.Button(
            action_row,
            text="◆ 反馈 Bug / 提建议（自动脱敏）",
            command=self._on_open_feedback,
            bg=THEME["feedback_bg"],
            fg=THEME["feedback_fg"],
            activebackground=THEME["feedback_hover"],
            activeforeground=THEME["blue_deep"],
            relief="flat",
            bd=0,
            highlightbackground=THEME["blue"],
            highlightthickness=1,
            font=self._font(9, bold=True),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        btn_feedback.pack(side="left", fill="x", expand=True)
        self._bind_hover(btn_feedback, THEME["feedback_bg"], THEME["feedback_hover"])

    # ------------------------------------------------------------------
    # 目录选择与日志异步附着
    # ------------------------------------------------------------------

    def _prompt_first_time_dir(self) -> None:
        if self._closing:
            return
        if messagebox.askyesno(
            "HoopCoach · 星奈军师 — 首次运行",
            "未自动找到《篮球少女：飞跃巅峰》游戏目录。\n\n"
            "现在选择你的游戏安装文件夹（包含 LQSN.exe 或 _game_data_ / Logs）吗？\n选一次即可永久记住。",
            parent=self.root,
        ):
            self._on_choose_game_dir()

    def _on_choose_game_dir(self) -> None:
        chosen = pc.choose_game_dir_via_dialog(parent=self.root)
        if not chosen:
            return
        self.docs_dir, self.log_dir = chosen
        self.var_dir_display.set(self.log_dir)
        if self._dashboard_monitor is not None:
            self._dashboard_monitor.set_log_dir(self.log_dir)
        if self.mirror is not None:
            self.mirror.close()
            self.mirror = None
        self._last_rendered_sig = ""
        self._start_attach_latest_async()

    def _start_attach_latest_async(self) -> None:
        if self._attaching_log or not self.log_dir or not os.path.isdir(self.log_dir):
            return
        self._attaching_log = True
        self._set_connection_status("warn", "正在读取最新游戏日志...")

        def _worker():
            mirror = LiveLogMirror(self.log_dir)
            ok = False
            err = ""
            try:
                ok = mirror.attach_latest()
            except Exception as e:
                err = str(e)
            self.root.after(0, lambda: self._on_attach_done(mirror, ok, err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_attach_done(self, mirror: LiveLogMirror, ok: bool, err: str) -> None:
        self._attaching_log = False
        if self._closing:
            mirror.close()
            return
        if self.mirror is not None:
            self.mirror.close()
        self.mirror = mirror
        if ok and mirror.log_path:
            self._set_connection_status("ok", f"已连接 · {os.path.basename(mirror.log_path)}")
            self._last_rendered_sig = ""
            self._render_current_state(force=True)
        elif err:
            self._set_connection_status("err", f"日志读取异常：{err}")
        else:
            self._set_connection_status("warn", "目录已绑定 · 等待游戏生成日志")

    # ------------------------------------------------------------------
    # 控制面板交互事件
    # ------------------------------------------------------------------

    def _on_toggle_hud(self) -> None:
        enabled = not self.var_hud_enabled.get()
        self.var_hud_enabled.set(enabled)
        self.hud.set_user_enabled(enabled)
        if enabled:
            self.btn_toggle_hud.configure(
                text="● 悬浮窗运行中 · 点击暂停",
                bg=THEME["hud_on_bg"],
                activebackground=THEME["hud_on_hover"],
            )
        else:
            self.btn_toggle_hud.configure(
                text="○ 悬浮窗已暂停 · 点击开启",
                bg=THEME["hud_off_bg"],
                activebackground=THEME["hud_off_hover"],
            )

    def _refresh_hud_hint_text(self) -> None:
        if self.var_diegetic_mode.get() == "diegetic":
            self.var_hud_hint.set(
                "鼠标完全穿透 · 过场与切窗自动隐身（建议游戏设为无边框窗口化）"
            )
        else:
            self.var_hud_hint.set(
                "按住顶栏拖动 · 右键折叠 · 正文区鼠标直接点穿游戏"
            )

    def _on_diegetic_mode_changed(self) -> None:
        want_diegetic = self.var_diegetic_mode.get() == "diegetic"
        actual = self.hud.set_diegetic_mode(want_diegetic)
        if want_diegetic and not actual:
            self.var_diegetic_mode.set("compact")
            messagebox.showinfo(
                "已自动切换为紧凑浮窗",
                "当前环境缺少图形渲染组件，暂无法启用贴身角标模式。\n"
                "已自动保持「紧凑浮窗」模式；使用官方 Windows 完整包即可直接开启贴身角标。",
                parent=self.root,
            )
        self.hud_cfg["diegetic_mode"] = bool(actual)
        self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        self._sync_mode_segment_styles()
        self._refresh_hud_hint_text()
        self._render_current_state(force=True)

    def _on_hud_diegetic_synced(self, is_diegetic: bool) -> None:
        self.var_diegetic_mode.set("diegetic" if is_diegetic else "compact")
        self.hud_cfg["diegetic_mode"] = bool(is_diegetic)
        try:
            self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        except Exception:
            pass
        self._sync_mode_segment_styles()
        self._refresh_hud_hint_text()

    def _get_feedback_context(self) -> dict:
        ctx = self.hud.get_diagnostic_context() if hasattr(self.hud, "get_diagnostic_context") else {}
        fc = self.hud_cfg.get("focus") or {}
        st = self.mirror.state if self.mirror else None
        extra_snap = {
            "last_train_cmds_count": len(self.mirror.last_train_cmds) if self.mirror else 0,
            "strategy": self.strategy_name,
        }
        game_rect = None
        try:
            grect = getattr(self.hud, "_last_game_rect", None)
            if grect and len(grect) == 4:
                game_rect = tuple(int(v) for v in grect)
        except Exception:
            game_rect = None
        ctx.update({
            "strategy": self.strategy_name,
            "focus_stat": fc.get("stat"),
            "focus_bonus": fc.get("bonus"),
            "log_path": (self.mirror.log_path if self.mirror else ""),
            "hud_config": self.hud_cfg,
            "state_snapshot": issue_reporter.build_state_snapshot(st, extra=extra_snap) if st else None,
            "runtime_error_path": os.path.join(pc.BASE_DIR, "runtime_error.log"),
            "game_rect": game_rect,
        })
        return ctx

    def _on_open_feedback(self) -> None:
        try:
            issue_reporter.open_feedback_dialog(self.root, get_context_fn=self._get_feedback_context)
        except Exception as e:
            messagebox.showerror("打开反馈窗口失败", str(e), parent=self.root)

    def _on_toggle_auto_check_updates(self) -> None:
        self.hud_cfg["auto_check_updates"] = bool(self.var_auto_check_updates.get())
        self.hud_cfg = lc.save_hud_config(self.hud_cfg)

    def _sync_update_buttons(
        self,
        *,
        state: str = "normal",
        footer_text: str,
        settings_text: str,
        highlight: bool = False,
        up_to_date: bool = False,
    ) -> None:
        self._update_highlighted = bool(highlight)
        if highlight:
            self.btn_update.configure(
                state=state,
                text=footer_text,
                bg=THEME["warn_bg"],
                fg=THEME["warn_fg"],
                activebackground=THEME["warn_hover"],
                activeforeground=THEME["ink"],
                highlightbackground=THEME["ink"],
                highlightthickness=1,
                padx=8,
            )
            if self._btn_update_settings is not None:
                self._btn_update_settings.configure(
                    state=state,
                    text=settings_text,
                    bg=THEME["warn_bg"],
                    fg=THEME["warn_fg"],
                    activebackground=THEME["warn_hover"],
                    activeforeground=THEME["ink"],
                    highlightbackground=THEME["ink"],
                )
        else:
            fg_foot = THEME["ok_fg"] if up_to_date else THEME["text_secondary"]
            fg_set = THEME["ok_fg"] if up_to_date else THEME["ink"]
            self.btn_update.configure(
                state=state,
                text=footer_text,
                bg=THEME["bg"],
                fg=fg_foot,
                activebackground=THEME["well"],
                activeforeground=THEME["ink"],
                highlightthickness=0,
                padx=6,
            )
            if self._btn_update_settings is not None:
                self._btn_update_settings.configure(
                    state=state,
                    text=settings_text,
                    bg=THEME["secondary_btn"],
                    fg=fg_set,
                    activebackground=THEME["secondary_btn_hover"],
                    activeforeground=THEME["ink"],
                    highlightbackground=THEME["border"],
                )

    def _on_click_update(self) -> None:
        if self._latest_update_res is not None and self._latest_update_res.has_update:
            updater.open_update_dialog(
                self.root,
                self._latest_update_res,
                on_restart_required=self._on_close,
            )
            return
        self._start_check_updates(silent=False)

    def _start_check_updates(self, silent: bool = True) -> None:
        if self._checking_updates or self._closing:
            return
        self._checking_updates = True
        if not silent:
            self._sync_update_buttons(
                state="disabled",
                footer_text="检查更新中...",
                settings_text="检查中...",
            )
            self.var_scan_status.set("正在检查新版本...")

        def _worker() -> None:
            res = updater.check_for_updates(meta=self._app_meta)
            try:
                self.root.after(0, lambda r=res, s=silent: self._on_check_updates_done(r, s))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_updates_done(self, res: updater.UpdateCheckResult, silent: bool) -> None:
        self._checking_updates = False
        if self._closing:
            return

        if not res.ok:
            self._sync_update_buttons(
                state="normal",
                footer_text="检查更新",
                settings_text="检查新版本",
            )
            if not silent:
                self.var_scan_status.set("检查更新失败")
                if res.release_page_url:
                    if messagebox.askyesno(
                        "检查更新失败",
                        f"暂时无法连接更新服务器：\n{res.error}\n\n要直接在浏览器打开 GitHub Releases 页面查看吗？",
                        parent=self.root,
                    ):
                        webbrowser.open(res.release_page_url)
                else:
                    messagebox.showwarning("检查更新失败", res.error, parent=self.root)
            return

        self._latest_update_res = res
        if res.has_update:
            sz = updater.format_bytes(res.total_download_bytes)
            self._sync_update_buttons(
                state="normal",
                footer_text=f"▲ 发现新版 {res.latest_version}（{sz}）",
                settings_text=f"▲ 升级至 {res.latest_version}",
                highlight=True,
            )
            self.var_scan_status.set(f"新版 {res.latest_version} 可更新")
            if not silent:
                updater.open_update_dialog(
                    self.root,
                    res,
                    on_restart_required=self._on_close,
                )
        else:
            self._sync_update_buttons(
                state="normal",
                footer_text=f"✓ 已是最新 ({res.current_version})",
                settings_text="✓ 当前已是最新",
                up_to_date=True,
            )
            if not silent:
                self.var_scan_status.set(f"已是最新 {res.current_version}")
                updater.open_update_dialog(
                    self.root,
                    res,
                    on_restart_required=self._on_close,
                )

    def _on_toggle_follow_focus(self) -> None:
        self.hud.set_follow_game_focus(self.var_follow_focus.get())

    def _on_reset_hud_pos(self) -> None:
        self.hud.reset_position()

    def _on_strategy_changed(self, _event=None) -> None:
        sel = self.var_strategy.get()
        for key, lbl in STRATEGY_OPTIONS:
            if lbl == sel:
                self.strategy_name = key
                break
        try:
            self.strategy = lc.load_strategy(self.strategy_name)
            self.hud.strategy = self.strategy
            if self._dashboard_monitor is not None:
                self._dashboard_monitor.strategy = self.strategy
        except Exception as e:
            messagebox.showerror("加载策略失败", str(e), parent=self.root)
            return
        self._render_current_state(force=True)

    def _on_focus_changed(self, _event=None) -> None:
        sel_stat = self.var_focus_stat.get()
        stat_id = next((sid for sid, lbl in FOCUS_STAT_OPTIONS if lbl == sel_stat), None)
        sel_bonus = self.var_focus_bonus.get()
        bonus_val = next((bv for bv, lbl in FOCUS_BONUS_OPTIONS if lbl == sel_bonus), 1.15)
        fc = dict(self.hud_cfg.get("focus") or {})
        fc["stat"] = stat_id
        fc["bonus"] = bonus_val
        if stat_id is not None:
            fc["last_stat"] = stat_id
        self.hud_cfg["focus"] = fc
        self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        self.hud.set_hud_config(self.hud_cfg, state=(self.mirror.state if self.mirror else None))
        self._render_current_state(force=True)

    def _on_recs_changed(self) -> None:
        recs = dict(self.hud_cfg.get("recommendations") or {})
        for key, var in self.var_recs.items():
            recs[key] = bool(var.get())
        self.hud_cfg["recommendations"] = recs
        self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        self._refresh_recs_summary()
        self.hud.set_hud_config(self.hud_cfg, state=(self.mirror.state if self.mirror else None))
        self._render_current_state(force=True)

    def _on_open_dashboard(self) -> None:
        if self._dashboard_srv is None:
            try:
                srv, url, engine, monitor = dashboard_server.start_background_server(
                    host="127.0.0.1",
                    port=8765,
                    log_dir=self.log_dir,
                )
                self._dashboard_srv = srv
                self._dashboard_url = url
                self._dashboard_engine = engine
                self._dashboard_monitor = monitor
            except Exception as e:
                messagebox.showerror("启动作战室失败", str(e), parent=self.root)
                return
        webbrowser.open(self._dashboard_url)
        self.var_scan_status.set("作战室已在浏览器打开")

    def _on_scan_runs(self) -> None:
        if self._scanning_runs:
            return
        if not self.log_dir or not os.path.isdir(self.log_dir):
            messagebox.showwarning("尚未绑定目录", "请先选择《篮球少女》游戏目录后再扫描历史战绩。", parent=self.root)
            return
        self._scanning_runs = True
        self.btn_scan_runs.configure(state="disabled", text="扫描中...")
        self.var_scan_status.set("正在增量扫描战绩...")

        def _worker():
            try:
                # 必须用控制台已绑定的日志目录，避免模块导入时的 LOG_DIR 为空/过期
                # 导致 schema 升级全量重算时扫到空目录并写出空归档
                analyzer.LOG_DIR = self.log_dir
                runs = analyzer.extract_all_runs(strategy_name=self.strategy_name, force_rebuild=False)
                n = len(runs or [])

                # 同步角色/笔记/器材个人池，供作战室「笔记与配装」「相性查询」使用
                pool_msg = self._sync_personal_pools()
                if self._dashboard_engine is not None:
                    self._dashboard_engine.invalidate_caches()
                if n == 0:
                    log_n = len(glob.glob(os.path.join(self.log_dir, "*.txt")))
                    if log_n > 0:
                        msg = f"未解析到完赛局（日志 {log_n} 个）。可再试一次或查看 runtime_error.log"
                    else:
                        msg = "日志目录内没有 .txt 文件"
                else:
                    msg = f"已归档 {n} 局战绩"
                if pool_msg:
                    msg = f"{msg} · {pool_msg}"
            except Exception as e:
                msg = f"扫描失败：{e}"
                self._record_runtime_error("扫描历史战绩", e)
            self.root.after(0, lambda: self._on_scan_done(msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_done(self, msg: str) -> None:
        self._scanning_runs = False
        if self._closing:
            return
        self.btn_scan_runs.configure(state="normal", text="扫描历史战绩")
        self.var_scan_status.set(msg)

    # ------------------------------------------------------------------
    # 按需数据缓存与轻量去重运行时错误记录
    # ------------------------------------------------------------------

    def _get_note_static_db(self):
        if self._note_static_db is None:
            self._note_static_db = lc.load_note_static_db()
        return self._note_static_db

    def _sync_personal_pools(self) -> str:
        """从日志增量同步笔记池与器材池；返回简短状态文案。"""
        if not self.log_dir or not os.path.isdir(self.log_dir):
            return ""
        parts: list[str] = []
        try:
            docs_dir = self.docs_dir or os.path.dirname(self.log_dir)
            na.DOCS_DIR = docs_dir
            na.LOG_DIR = self.log_dir
            chars, _rules, _rel, entries_cat = self._get_note_static_db()
            pool = na.sync_note_and_actor_pool(chars, entries_cat, force_rescan=False)
            self._note_pool_cache = pool if isinstance(pool, dict) else {}
            try:
                self._note_pool_mtime = os.path.getmtime(
                    os.path.join(pc.BASE_DIR, "data", "note_pool.json")
                )
            except OSError:
                self._note_pool_mtime = -1.0
            n_own = len((pool or {}).get("own_notes") or {})
            n_bor = len((pool or {}).get("borrowed_notes") or {})
            n_act = len((pool or {}).get("owned_actors") or [])
            parts.append(f"角色{n_act}/笔记{n_own}+借{n_bor}")
        except Exception as e:
            self._record_runtime_error("同步笔记角色池", e)
        try:
            eq_pool = ea.sync_equip_pool_from_logs(self.log_dir, force_rescan=False)
            n_eq = len((eq_pool or {}).get("owned_cards") or {})
            parts.append(f"器材{n_eq}")
        except Exception as e:
            self._record_runtime_error("同步器材池", e)
        return "池 " + " ".join(parts) if parts else ""

    def _get_note_pool(self) -> dict:
        pool_path = os.path.join(pc.BASE_DIR, "data", "note_pool.json")
        try:
            mt = os.path.getmtime(pool_path) if os.path.isfile(pool_path) else -1.0
        except OSError:
            mt = -1.0
        if mt == self._note_pool_mtime:
            return self._note_pool_cache
        if mt < 0:
            # 冷启动：尚无笔记池文件时，尝试从日志同步一次
            if self.log_dir and os.path.isdir(self.log_dir):
                try:
                    self._sync_personal_pools()
                    return self._note_pool_cache
                except Exception as e:
                    self._record_runtime_error("冷启动同步笔记池", e)
            self._note_pool_cache = {}
            self._note_pool_mtime = -1.0
            return self._note_pool_cache
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._note_pool_cache = data if isinstance(data, dict) else {}
        except Exception as e:
            self._note_pool_cache = {}
            self._record_runtime_error("读取本地笔记池", e)
        self._note_pool_mtime = mt
        return self._note_pool_cache

    def _get_event_db(self) -> dict:
        if self._event_db is None:
            self._event_db = lc.load_event_db() or {}
        return self._event_db

    def _record_runtime_error(self, context: str, exc: Exception) -> None:
        key = (context, type(exc).__name__, str(exc))
        if key in self._logged_runtime_errors:
            return
        self._logged_runtime_errors.add(key)
        log_path = os.path.join(pc.BASE_DIR, "runtime_error.log")
        try:
            if os.path.isfile(log_path) and os.path.getsize(log_path) > 512 * 1024:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as rf:
                    tail = rf.read()[-128 * 1024 :]
                with open(log_path, "w", encoding="utf-8") as wf:
                    wf.write(tail)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tb = traceback.format_exc()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{context}] {type(exc).__name__}: {exc}\n{tb}\n")
        except Exception:
            pass
        try:
            self.var_live_preview.set(f"! {context}异常（{type(exc).__name__}），详情已写入 runtime_error.log")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 实时日志增量轮询与 HUD 渲染调度
    # ------------------------------------------------------------------

    def _poll_log_tick(self) -> None:
        if self._closing:
            return
        try:
            if self.mirror is None:
                if self.log_dir and os.path.isdir(self.log_dir) and glob.glob(os.path.join(self.log_dir, "*.txt")):
                    self._start_attach_latest_async()
            else:
                prev_log = self.mirror.log_path
                evs = self.mirror.poll()
                if self.mirror.log_path and self.mirror.log_path != prev_log:
                    self._set_connection_status("ok", f"已连接 · {os.path.basename(self.mirror.log_path)}")
                    self._render_current_state(force=True)
                elif evs:
                    self._render_current_state(force=False)
        except Exception as e:
            self._record_runtime_error("日志轮询", e)
        self.root.after(220, self._poll_log_tick)

    def _render_current_state(self, force: bool = False) -> None:
        if not self.mirror:
            return
        state = self.mirror.state
        mode = lc.compute_active_hud_view_mode(state)

        # 生成轻量去重签名，避免重复重绘闪烁
        skill_choices_raw = state.get("cur_skill_choices") or state.get("skill_choices") or ()
        skill_sig = tuple(
            (int(x.get("id") or x.get("skill_id") or 0), int(x.get("lv") or x.get("level") or 0))
            if isinstance(x, dict)
            else x
            for x in skill_choices_raw
        )
        store_sig = tuple(
            (int(it.get("id") or 0), int(it.get("num") or 0))
            for it in (state.get("store_items") or ())
            if isinstance(it, dict)
        )
        sig = (
            mode,
            int(state.get("exp_round") or 0),
            int(state.get("round") or 0),
            int(state.get("round_type") or 1),
            int(state.get("ps") or 0),
            int(state.get("gold") or 0),
            bool(state.get("fired_70", False)),
            bool(state.get("fired_71", False)),
            store_sig,
            state.get("cur_story"),
            state.get("pre_edu_char_id"),
            skill_sig,
            len(self.mirror.last_train_cmds),
            self.strategy_name,
            (self.hud_cfg.get("focus") or {}).get("stat"),
            (self.hud_cfg.get("focus") or {}).get("bonus"),
        )
        sig_str = repr(sig)
        if not force and sig_str == self._last_rendered_sig:
            self.hud.refresh_scene_overlay(state)
            return
        self._last_rendered_sig = sig_str

        # 1. 开局选人笔记与本命器材推荐
        if mode == "initactor" and state.get("pre_edu_char_id"):
            try:
                chars, rules, rel_scores, _entries_cat = self._get_note_static_db()
                pool = self._get_note_pool()
                adv = lc.recommend_notes_for_character(
                    int(state["pre_edu_char_id"]), chars, rules, rel_scores, pool
                )
                if adv:
                    self.hud.render_note_selection(adv, "", "")
                    cname = adv["character"].get("short_name", "")
                    if adv.get("borrow_combos") or adv.get("pure_own_combos"):
                        self.var_live_preview.set(f"◆ 选人配装：{cname}（已推荐最优笔记组合与 5 件器材）")
                    else:
                        self.var_live_preview.set(f"◆ 选人配装：{cname}（通用最优高相性搭档 + 毕业 5 器材）")
            except Exception as e:
                self._record_runtime_error("选人配装推荐", e)
            self.hud.refresh_scene_overlay(state)
            return

        # 2. 选技能三选一
        skill_choices = state.get("cur_skill_choices") or state.get("skill_choices")
        if mode in ("skill", "skill_choice") and skill_choices:
            try:
                pool = self._get_note_pool()
                evals = evaluate_skill_choices(
                    int(state.get("main_char_id") or 0),
                    list(skill_choices or []),
                    set(state.get("learned_skills") or ()),
                    note_pool=(pool if pool else None),
                )
                if evals:
                    self.hud.render_skill_choice(state, evals)
                    top = next((x for x in evals if x.get("rank") == 1), evals[0])
                    self.var_live_preview.set(f"★ 技能三选一：首选【{top.get('skill_name', '')}】")
            except Exception as e:
                self._record_runtime_error("选技能推荐", e)
            self.hud.refresh_scene_overlay(state)
            return

        # 3. 剧情分支选项
        if mode in ("story", "story_choice") and state.get("cur_story"):
            try:
                sid, sname = state["cur_story"]
                event_db = self._get_event_db()
                adv = lc.evaluate_story_event(str(sid), str(sname), state, self.strategy, event_db)
                if adv:
                    self.hud.render_story_choice(state, adv)
                    first_rec = (adv.get("recommendation") or "").splitlines()[0]
                    self.var_live_preview.set(f"◆ 事件【{adv.get('ev_name', '')}】：{first_rec}")
            except Exception as e:
                self._record_runtime_error("剧情分支推荐", e)
            self.hud.refresh_scene_overlay(state)
            return

        # 4. 周末 / 商店 / 扭蛋 / 街机 / 公园
        if int(state.get("round_type") or 1) == 2 and int(state.get("exp_round") or 0) > 0:
            try:
                wk = lc.evaluate_weekend_and_shop(state, self.strategy)
                if wk:
                    self.hud.render_weekend_advice(state, wk)
                    self.var_live_preview.set(
                        f"◆ {wk.get('day_tag', '周末')}：★ {wk.get('best_compact') or wk.get('headline', '')} (EV {wk.get('best_ev', 0):.0f})"
                    )
            except Exception as e:
                self._record_runtime_error("周末决策推荐", e)
            self.hud.refresh_scene_overlay(state)
            return

        # 5. 平日训练 6 选 1
        if self.mirror.last_train_cmds and len(self.mirror.last_train_cmds) == 6:
            try:
                scored = lc.score_training_turn(state, self.mirror.last_train_cmds, self.strategy)
                best_ev = scored[0]["score"] if scored else None
                shop_tip = lc.format_weekday_shop_tip(
                    state,
                    best_ev=best_ev,
                    strategy=self.strategy,
                    scored_cmds=scored,
                )
                if scored:
                    self.hud.render_turn(state, scored, shop_tip)
                    best = scored[0]
                    self.var_live_preview.set(
                        f"◆ 第{int(state.get('round') or 0):02d}/28训 · 体{int(state.get('ps') or 0)} 金{int(state.get('gold') or 0)} ▶ ★ {best['name']} (EV {best['score']:.0f})"
                    )
            except Exception as e:
                self._record_runtime_error("平日训练推荐", e)
            self.hud.refresh_scene_overlay(state)
            return

        # 6. 其他场景（大厅 / 加载 / 过场 / 结算）自动隐藏悬浮窗
        self.hud.refresh_scene_overlay(state)
        if mode == "hidden":
            self.var_live_preview.set("○ 当前处于大厅 / 剧情 / 过场界面，游戏内浮窗已自动隐藏")

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.hud.close_cleanly(quit_main_loop=False)
        except Exception:
            pass
        if self.mirror is not None:
            self.mirror.close()
        if self._dashboard_srv is not None:
            try:
                self._dashboard_srv.shutdown()
                self._dashboard_srv.server_close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main(skip_startup_guards: bool = False) -> None:
    if not skip_startup_guards:
        try:
            import bootstrap

            if bootstrap.warn_if_archive_temp_run(pc.BASE_DIR):
                return
            acquired, _mutex_handle = bootstrap.acquire_single_instance_mutex()
            if not acquired:
                return
        except Exception:
            pass
    app = LQSNCoachDesktopApp()
    app.run()


if __name__ == "__main__":
    main()
