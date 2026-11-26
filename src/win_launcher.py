#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win_launcher.py — LQSNCoach 免命令行桌面控制台与实时悬浮军师入口（纯只读、零侵入）

设计目标：
- 双击 LQSNCoach.exe 即可使用，无需黑框命令行或配置 Python 环境
- 一个清爽小巧的桌面控制窗集中管理：
  1. 游戏日志目录自动探测 / 一键手动绑定
  2. 游戏内悬浮 HUD 开关、「仅游戏在前台时显示」开关、浮窗位置复位
  3. 流派策略（训练/商店/扭蛋）、偏科属性倍率、各模块推荐开关（实时保存并热生效）
  4. 一键打开本地作战室网页（战绩复盘 & 实盘笔记/器材配装）与一键扫描历史战绩
- 严格只读监听游戏明文日志（_game_data_/Documents/Logs），绝不包含任何自动点击或内存读取逻辑
"""

from __future__ import annotations

import ctypes
import glob
import os
import sys
import threading
import webbrowser
from typing import Optional

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = None
    messagebox = None
    ttk = None

# 确保 src 目录可被导入（兼容源码运行与 PyInstaller 冻结模式）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import analyzer
import dashboard_server
import issue_reporter
import live_coach as lc
from log_mirror import LiveLogMirror
import platform_compat as pc
from skill_advisor import evaluate_skill_choices


STRATEGY_OPTIONS = [
    ("training_flow", "训练流（推荐 · 均衡冲分）"),
    ("store_flow", "商店流（代币/买药特化）"),
    ("gacha_flow", "扭蛋流（摇奖/扭蛋特化）"),
]

FOCUS_STAT_OPTIONS = [
    (None, "无偏科（纯总评最优）"),
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


class LQSNCoachDesktopApp:
    """免命令行桌面小控制台 + 游戏内悬浮 HUD 统一调度器。"""

    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("当前 Python 环境未安装 tkinter（Windows 官方 Python 默认自带；Linux 请安装 python3-tk）。")
        if pc.IS_WINDOWS:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title("🏀 HoopCoach · 星奈育成军师")
        self.root.geometry("468x742")
        self.root.minsize(440, 690)
        self.root.configure(bg="#0f172a")

        self._closing = False
        self._attaching_log = False
        self._scanning_runs = False
        self._dashboard_srv = None
        self._dashboard_url = ""
        self._dashboard_engine = None
        self._dashboard_monitor = None

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

        self.var_status_badge = tk.StringVar(value="⏳ 正在初始化...")
        self.var_dir_display = tk.StringVar(value=self.log_dir or "未绑定游戏日志目录")
        self.var_live_preview = tk.StringVar(value="等待进入《篮球少女》育成或选人界面...")
        self.var_scan_status = tk.StringVar(value="")

        self._setup_styles()
        self._build_ui()

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
        self._refresh_hud_hint_text()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.log_dir and os.path.isdir(self.log_dir):
            self._start_attach_latest_async()
        else:
            self.var_status_badge.set("⚠️ 未自动定位到游戏目录，请点击下方选择")
            self.root.after(350, self._prompt_first_time_dir)

        self.root.after(220, self._poll_log_tick)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#0f172a",
            background="#334155",
            foreground="#f8fafc",
            arrowcolor="#f8fafc",
            bordercolor="#334155",
        )

    def _make_card(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg="#1e293b", highlightbackground="#334155", highlightthickness=1)
        outer.pack(fill="x", padx=14, pady=(0, 10))
        hdr = tk.Label(
            outer,
            text=title,
            bg="#1e293b",
            fg="#fbbf24",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        )
        hdr.pack(fill="x", padx=12, pady=(8, 4))
        body = tk.Frame(outer, bg="#1e293b")
        body.pack(fill="x", padx=12, pady=(0, 10))
        return body

    def _build_ui(self) -> None:
        # 顶栏标题
        top = tk.Frame(self.root, bg="#0f172a")
        top.pack(fill="x", padx=14, pady=(12, 10))
        tk.Label(
            top,
            text="🏀 星奈育成军师",
            bg="#0f172a",
            fg="#f8fafc",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        tk.Label(
            top,
            text="纯只读 · 零侵入",
            bg="#14532d",
            fg="#86efac",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=8,
            pady=3,
        ).pack(side="right", pady=2)

        # 卡片 1：游戏连接与日志目录
        c1 = self._make_card(self.root, "📂 游戏日志连接")
        tk.Label(
            c1,
            textvariable=self.var_status_badge,
            bg="#1e293b",
            fg="#4ade80",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", pady=(0, 4))

        dir_row = tk.Frame(c1, bg="#1e293b")
        dir_row.pack(fill="x")
        tk.Label(
            dir_row,
            textvariable=self.var_dir_display,
            bg="#0f172a",
            fg="#94a3b8",
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            padx=8,
            pady=5,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Button(
            dir_row,
            text="选择目录",
            command=self._on_choose_game_dir,
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=3,
            cursor="hand2",
        ).pack(side="right")

        # 卡片 2：游戏内实时悬浮 HUD
        c2 = self._make_card(self.root, "✨ 游戏内实时悬浮窗 (HUD)")
        btn_row = tk.Frame(c2, bg="#1e293b")
        btn_row.pack(fill="x", pady=(0, 6))

        self.btn_toggle_hud = tk.Button(
            btn_row,
            text="⏸ 悬浮窗运行中（点击暂停）",
            command=self._on_toggle_hud,
            bg="#15803d",
            fg="#ffffff",
            activebackground="#166534",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_toggle_hud.pack(side="left", fill="x", expand=True, padx=(0, 8))

        tk.Button(
            btn_row,
            text="🎯 复位位置",
            command=self._on_reset_hud_pos,
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=6,
            cursor="hand2",
        ).pack(side="right")

        mode_row = tk.Frame(c2, bg="#1e293b")
        mode_row.pack(fill="x", pady=(0, 4))
        tk.Label(
            mode_row,
            text="显示样式：",
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 9),
            width=9,
            anchor="w",
        ).pack(side="left")
        tk.Radiobutton(
            mode_row,
            text="✨ 贴身角标（默认）",
            variable=self.var_diegetic_mode,
            value="diegetic",
            command=self._on_diegetic_mode_changed,
            bg="#1e293b",
            fg="#fef08a",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(0, 12))
        tk.Radiobutton(
            mode_row,
            text="📋 紧凑浮窗",
            variable=self.var_diegetic_mode,
            value="compact",
            command=self._on_diegetic_mode_changed,
            bg="#1e293b",
            fg="#e2e8f0",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        ).pack(side="left")

        tk.Checkbutton(
            c2,
            text="仅在游戏窗口处于前台时显示（切微信/浏览器秒隐身，推荐）",
            variable=self.var_follow_focus,
            command=self._on_toggle_follow_focus,
            bg="#1e293b",
            fg="#e2e8f0",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x")

        tk.Label(
            c2,
            textvariable=self.var_hud_hint,
            bg="#1e293b",
            fg="#94a3b8",
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            justify="left",
            wraplength=410,
        ).pack(fill="x", pady=(4, 0))

        # 卡片 3：流派、偏科与推荐开关
        c3 = self._make_card(self.root, "⚙️ 育成策略与模块开关（实时热生效）")

        row_strat = tk.Frame(c3, bg="#1e293b")
        row_strat.pack(fill="x", pady=(0, 6))
        tk.Label(
            row_strat,
            text="育成流派：",
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 9),
            width=10,
            anchor="w",
        ).pack(side="left")
        cb_strat = ttk.Combobox(
            row_strat,
            textvariable=self.var_strategy,
            values=[lbl for _, lbl in STRATEGY_OPTIONS],
            state="readonly",
            style="Dark.TCombobox",
        )
        cb_strat.pack(side="left", fill="x", expand=True)
        cb_strat.bind("<<ComboboxSelected>>", self._on_strategy_changed)

        row_focus = tk.Frame(c3, bg="#1e293b")
        row_focus.pack(fill="x", pady=(0, 8))
        tk.Label(
            row_focus,
            text="偏科引导：",
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 9),
            width=10,
            anchor="w",
        ).pack(side="left")
        cb_fstat = ttk.Combobox(
            row_focus,
            textvariable=self.var_focus_stat,
            values=[lbl for _, lbl in FOCUS_STAT_OPTIONS],
            state="readonly",
            width=18,
            style="Dark.TCombobox",
        )
        cb_fstat.pack(side="left", fill="x", expand=True, padx=(0, 6))
        cb_fstat.bind("<<ComboboxSelected>>", self._on_focus_changed)

        cb_fbonus = ttk.Combobox(
            row_focus,
            textvariable=self.var_focus_bonus,
            values=[lbl for _, lbl in FOCUS_BONUS_OPTIONS],
            state="readonly",
            width=12,
            style="Dark.TCombobox",
        )
        cb_fbonus.pack(side="right")
        cb_fbonus.bind("<<ComboboxSelected>>", self._on_focus_changed)

        chk_grid = tk.Frame(c3, bg="#1e293b")
        chk_grid.pack(fill="x")
        for idx, (key, label, _desc, _aliases) in enumerate(lc.HUD_RECOMMENDATION_SPECS):
            r, c = divmod(idx, 2)
            cb = tk.Checkbutton(
                chk_grid,
                text=label,
                variable=self.var_recs[key],
                command=self._on_recs_changed,
                bg="#1e293b",
                fg="#e2e8f0",
                selectcolor="#0f172a",
                activebackground="#1e293b",
                activeforeground="#ffffff",
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            )
            cb.grid(row=r, column=c, sticky="w", padx=(0, 12), pady=1)

        # 卡片 4：作战室与历史战绩复盘
        c4 = self._make_card(self.root, "📊 战绩复盘 & 笔记器材配装")
        tool_row = tk.Frame(c4, bg="#1e293b")
        tool_row.pack(fill="x")
        tk.Button(
            tool_row,
            text="🌐 打开作战室网页",
            command=self._on_open_dashboard,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=12,
            pady=7,
            cursor="hand2",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_scan_runs = tk.Button(
            tool_row,
            text="🔄 扫描历史战绩",
            command=self._on_scan_runs,
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.btn_scan_runs.pack(side="right")

        tk.Label(
            c4,
            textvariable=self.var_scan_status,
            bg="#1e293b",
            fg="#7dd3fc",
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(4, 6))

        tk.Button(
            c4,
            text="🐛 反馈 Bug / 提改进建议（自动脱敏 · 零 Token）",
            command=self._on_open_feedback,
            bg="#312e81",
            fg="#e0e7ff",
            activebackground="#3730a3",
            activeforeground="#ffffff",
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(fill="x")

        # 底部：最新建议一行速览
        footer = tk.Frame(self.root, bg="#020617", highlightbackground="#334155", highlightthickness=1)
        footer.pack(fill="x", side="bottom", padx=14, pady=(0, 12))
        tk.Label(
            footer,
            textvariable=self.var_live_preview,
            bg="#020617",
            fg="#cbd5e1",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
            padx=10,
            pady=7,
            wraplength=415,
            justify="left",
        ).pack(fill="x")

    # ------------------------------------------------------------------
    # 目录选择与日志异步附着
    # ------------------------------------------------------------------

    def _prompt_first_time_dir(self) -> None:
        if self._closing:
            return
        if messagebox.askyesno(
            "🏀 星奈育成军师 — 首次运行",
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
        self.var_status_badge.set("⏳ 正在读取最新游戏日志...")

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
            self.var_status_badge.set(f"✅ 已连接：{os.path.basename(mirror.log_path)}")
            self._last_rendered_sig = ""
            self._render_current_state(force=True)
        elif err:
            self.var_status_badge.set(f"⚠️ 日志读取异常：{err}")
        else:
            self.var_status_badge.set("⏳ 目录已绑定，等待游戏启动生成日志...")

    # ------------------------------------------------------------------
    # 控制面板交互事件
    # ------------------------------------------------------------------

    def _on_toggle_hud(self) -> None:
        enabled = not self.var_hud_enabled.get()
        self.var_hud_enabled.set(enabled)
        self.hud.set_user_enabled(enabled)
        if enabled:
            self.btn_toggle_hud.configure(text="⏸ 悬浮窗运行中（点击暂停）", bg="#15803d", activebackground="#166534")
        else:
            self.btn_toggle_hud.configure(text="▶ 悬浮窗已暂停（点击开启）", bg="#475569", activebackground="#64748b")

    def _refresh_hud_hint_text(self) -> None:
        if self.var_diegetic_mode.get() == "diegetic":
            self.var_hud_hint.set(
                "💡 贴身角标：奶白抗锯齿章直接贴在游戏按钮上 · 100% 鼠标穿透 · 右键顶栏状态条可切回紧凑卡片"
            )
        else:
            self.var_hud_hint.set(
                "💡 紧凑浮窗：按住顶栏可自由拖动 · 右键顶栏可折叠 · 正文区鼠标直接点穿游戏"
            )

    def _on_diegetic_mode_changed(self) -> None:
        want_diegetic = self.var_diegetic_mode.get() == "diegetic"
        actual = self.hud.set_diegetic_mode(want_diegetic)
        if want_diegetic and not actual:
            self.var_diegetic_mode.set("compact")
            messagebox.showinfo(
                "已自动切换为紧凑浮窗",
                "当前 Python 环境未安装 Pillow，暂无法启用原生抗锯齿贴身角标。\n"
                "已自动保持「📋 紧凑浮窗」模式；安装 pillow（pip install pillow）或使用官方 Windows 压缩包即可启用贴身角标。",
                parent=self.root,
            )
        self.hud_cfg["diegetic_mode"] = bool(actual)
        self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        self._refresh_hud_hint_text()
        self._render_current_state(force=True)

    def _on_hud_diegetic_synced(self, is_diegetic: bool) -> None:
        self.var_diegetic_mode.set("diegetic" if is_diegetic else "compact")
        self.hud_cfg["diegetic_mode"] = bool(is_diegetic)
        try:
            self.hud_cfg = lc.save_hud_config(self.hud_cfg)
        except Exception:
            pass
        self._refresh_hud_hint_text()

    def _get_feedback_context(self) -> dict:
        ctx = self.hud.get_diagnostic_context() if hasattr(self.hud, "get_diagnostic_context") else {}
        fc = self.hud_cfg.get("focus") or {}
        ctx.update({
            "strategy": self.strategy_name,
            "focus_stat": fc.get("stat"),
            "focus_bonus": fc.get("bonus"),
            "log_path": (self.mirror.log_path if self.mirror else ""),
            "hud_config": self.hud_cfg,
        })
        return ctx

    def _on_open_feedback(self) -> None:
        try:
            issue_reporter.open_feedback_dialog(self.root, get_context_fn=self._get_feedback_context)
        except Exception as e:
            messagebox.showerror("打开反馈窗口失败", str(e), parent=self.root)

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
        self.var_scan_status.set(f"🌐 作战室已就绪：{self._dashboard_url}")

    def _on_scan_runs(self) -> None:
        if self._scanning_runs:
            return
        if not self.log_dir or not os.path.isdir(self.log_dir):
            messagebox.showwarning("尚未绑定目录", "请先选择《篮球少女》游戏目录后再扫描历史战绩。", parent=self.root)
            return
        self._scanning_runs = True
        self.btn_scan_runs.configure(state="disabled", text="⏳ 扫描中...")
        self.var_scan_status.set("⏳ 正在增量解析游戏日志并归档战绩...")

        def _worker():
            try:
                runs = analyzer.extract_all_runs(strategy_name=self.strategy_name, force_rebuild=False)
                n = len(runs or [])
                if self._dashboard_engine is not None:
                    self._dashboard_engine.invalidate_caches()
                msg = f"✅ 历史战绩同步完成：共归档 {n} 局（可在作战室查看复盘）"
            except Exception as e:
                msg = f"⚠️ 扫描失败：{e}"
            self.root.after(0, lambda: self._on_scan_done(msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_done(self, msg: str) -> None:
        self._scanning_runs = False
        if self._closing:
            return
        self.btn_scan_runs.configure(state="normal", text="🔄 扫描历史战绩")
        self.var_scan_status.set(msg)

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
                    self.var_status_badge.set(f"✅ 已连接：{os.path.basename(self.mirror.log_path)}")
                    self._render_current_state(force=True)
                elif evs:
                    self._render_current_state(force=False)
        except Exception:
            pass
        self.root.after(220, self._poll_log_tick)

    def _render_current_state(self, force: bool = False) -> None:
        if not self.mirror:
            return
        state = self.mirror.state
        mode = lc.compute_active_hud_view_mode(state)

        # 生成轻量去重签名，避免重复重绘闪烁
        sig = (
            mode,
            int(state.get("exp_round") or 0),
            int(state.get("round") or 0),
            int(state.get("round_type") or 1),
            int(state.get("ps") or 0),
            int(state.get("gold") or 0),
            state.get("cur_story"),
            state.get("pre_edu_char_id"),
            tuple(state.get("skill_choices") or ()),
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
                adv = lc.get_character_note_advice(int(state["pre_edu_char_id"]))
                if adv:
                    self.hud.render_note_selection(adv, "", "")
                    cname = adv["character"].get("short_name", "")
                    self.var_live_preview.set(f"📒 选人配装：{cname}（已推荐最优笔记组合与 5 件器材）")
            except Exception:
                pass
            self.hud.refresh_scene_overlay(state)
            return

        # 2. 选技能三选一
        skill_choices = state.get("cur_skill_choices") or state.get("skill_choices")
        if mode in ("skill", "skill_choice") and skill_choices:
            try:
                evals = evaluate_skill_choices(
                    skill_choices or [],
                    main_char_id=int(state.get("main_char_id") or 0),
                    learned_skill_ids=set(state.get("learned_skills") or ()),
                    note_skill_hints=state.get("note_skill_hints") or {},
                )
                if evals:
                    self.hud.render_skill_choice(state, evals)
                    top = next((x for x in evals if x.get("rank") == 1), evals[0])
                    self.var_live_preview.set(f"🎯 技能三选一：首选【{top.get('skill_name', '')}】")
            except Exception:
                pass
            self.hud.refresh_scene_overlay(state)
            return

        # 3. 剧情分支选项
        if mode in ("story", "story_choice") and state.get("cur_story"):
            try:
                sid, sname = state["cur_story"]
                adv = lc.lookup_story_choice_advice(str(sid), str(sname), state, self.strategy)
                if adv:
                    self.hud.render_story_choice(state, adv)
                    first_rec = (adv.get("recommendation") or "").splitlines()[0]
                    self.var_live_preview.set(f"📖 事件【{adv.get('ev_name', '')}】：{first_rec}")
            except Exception:
                pass
            self.hud.refresh_scene_overlay(state)
            return

        # 4. 周末 / 商店 / 扭蛋 / 街机 / 公园
        if int(state.get("round_type") or 1) == 2 and int(state.get("exp_round") or 0) > 0:
            try:
                wk = lc.evaluate_weekend_and_shop(state, self.strategy)
                if wk:
                    self.hud.render_weekend_advice(state, wk)
                    self.var_live_preview.set(
                        f"🌴 {wk.get('day_tag', '周末')}：👑 {wk.get('best_compact') or wk.get('headline', '')} (EV {wk.get('best_ev', 0):.0f})"
                    )
            except Exception:
                pass
            self.hud.refresh_scene_overlay(state)
            return

        # 5. 平日训练 6 选 1
        if self.mirror.last_train_cmds and len(self.mirror.last_train_cmds) == 6:
            try:
                scored = lc.score_training_turn(state, self.mirror.last_train_cmds, self.strategy)
                shop_tip = lc.build_shop_hud_tip(state, self.strategy) if hasattr(lc, "build_shop_hud_tip") else ""
                if scored:
                    self.hud.render_turn(state, scored, shop_tip)
                    best = scored[0]
                    self.var_live_preview.set(
                        f"🏀 第{int(state.get('round') or 0):02d}/28训 · 体{int(state.get('ps') or 0)} 金{int(state.get('gold') or 0)} → 👑 {best['name']} (EV {best['score']:.0f})"
                    )
            except Exception:
                pass
            self.hud.refresh_scene_overlay(state)
            return

        # 6. 其他场景（大厅 / 加载 / 过场 / 结算）自动隐藏悬浮窗
        self.hud.refresh_scene_overlay(state)
        if mode == "hidden":
            self.var_live_preview.set("⏳ 当前处于大厅 / 剧情 / 过场界面，游戏内浮窗已自动隐藏")

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


def main() -> None:
    app = LQSNCoachDesktopApp()
    app.run()


if __name__ == "__main__":
    main()
