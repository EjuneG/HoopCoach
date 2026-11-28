#!/usr/bin/env python3
"""
跨平台兼容层 (Linux & Windows)
- 自动探测项目根目录 BASE_DIR（兼容源码运行与 PyInstaller 绿色目录冻结运行）与游戏日志目录 LOG_DIR / DOCS_DIR
- Windows 下支持自动搜索常见盘符、或弹窗选择游戏文件夹并记忆到 config/local_paths.json
- 提供 Windows 原生免第三方依赖的 Tkinter + Win32 API 悬浮窗 (WindowsFloatingHUD)：
  * 默认「仅游戏窗口在前台时显示」，Alt+Tab 切走或最小化秒隐身，绝不压住其他软件
  * 双窗分层：顶栏可左键拖拽/右键折叠（不抢焦点、不占任务栏），正文区原生 WS_EX_TRANSPARENT 鼠标点穿
  * 与 Linux 版 live_coach 全部场景接口对齐（训练/周末/剧情/选技能/选人笔记/过场自动隐身）
"""

import ctypes
import html
import json
import os
import re
import sys
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    # PyInstaller --onedir 绿色目录包：BASE_DIR 指向 .exe 同级目录，配置与战绩直接读写在同目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DIR = os.path.join(BASE_DIR, "config")
LOCAL_PATHS_FILE = os.path.join(CONFIG_DIR, "local_paths.json")

LINUX_DEFAULT_DOCS_DIR = os.path.expanduser(
    "~/Sandbox/lqsn-pc/extracted/games/LQSN/_game_data_/Documents"
)
LINUX_DEFAULT_LOG_DIR = os.path.join(LINUX_DEFAULT_DOCS_DIR, "Logs")


def _normalize_game_dirs(candidate_path: str) -> Optional[tuple[str, str]]:
    """
    给定用户选择的任意层级目录（如 LQSN 根目录、_game_data_、Documents 或 Logs），
    推导并返回 (docs_dir, log_dir)。若无效则返回 None。
    """
    if not candidate_path:
        return None
    p = os.path.abspath(os.path.expanduser(candidate_path))
    if not os.path.exists(p):
        return None
    if os.path.isfile(p):
        p = os.path.dirname(p)

    # 1. 直接就是 Logs 目录
    if os.path.basename(p).lower() == "logs":
        return os.path.dirname(p), p

    # 2. 常见子路径组合探测
    rel_candidates = [
        ("Documents", os.path.join("Documents", "Logs")),
        (".", "Logs"),
        (os.path.join("_game_data_", "Documents"), os.path.join("_game_data_", "Documents", "Logs")),
        (
            os.path.join("games", "LQSN", "_game_data_", "Documents"),
            os.path.join("games", "LQSN", "_game_data_", "Documents", "Logs"),
        ),
        (
            os.path.join("LQSN", "_game_data_", "Documents"),
            os.path.join("LQSN", "_game_data_", "Documents", "Logs"),
        ),
    ]
    for docs_rel, logs_rel in rel_candidates:
        logs_full = os.path.join(p, logs_rel)
        if os.path.isdir(logs_full):
            docs_full = os.path.normpath(os.path.join(p, docs_rel))
            return docs_full, logs_full

    # 3. 刚装好游戏还没生成 Logs，但存在 _game_data_ 目录
    gd_root = os.path.join(p, "_game_data_")
    if os.path.isdir(gd_root):
        docs_full = os.path.join(gd_root, "Documents")
        logs_full = os.path.join(docs_full, "Logs")
        try:
            os.makedirs(logs_full, exist_ok=True)
        except Exception:
            pass
        if os.path.isdir(logs_full):
            return docs_full, logs_full

    return None


def _auto_scan_windows_game_dirs() -> Optional[tuple[str, str]]:
    """在 Windows 常见盘符与安装位置中快速搜寻 LQSN 日志目录。"""
    search_roots = [
        os.path.dirname(BASE_DIR),
        os.path.expanduser("~"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.path.expanduser("~"), "Downloads"),
    ]
    for drive in "CDEFGHI":
        drive_root = f"{drive}:\\"
        if os.path.exists(drive_root):
            search_roots.extend([
                drive_root,
                os.path.join(drive_root, "Games"),
                os.path.join(drive_root, "Program Files"),
                os.path.join(drive_root, "Program Files (x86)"),
                os.path.join(drive_root, "SteamLibrary", "steamapps", "common"),
                os.path.join(drive_root, "WeGameApps", "rail_apps"),
            ])

    folder_names = [
        "LQSN",
        "篮球少女",
        "篮球少女：飞跃巅峰",
        "HoopGirls",
        os.path.join("lqsn-pc", "extracted", "games", "LQSN"),
    ]
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        res = _normalize_game_dirs(root)
        if res:
            return res
        for fname in folder_names:
            cand = os.path.join(root, fname)
            res = _normalize_game_dirs(cand)
            if res:
                return res
    return None


def get_saved_local_paths() -> Optional[tuple[str, str]]:
    if not os.path.exists(LOCAL_PATHS_FILE):
        return None
    try:
        with open(LOCAL_PATHS_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        saved_log = cfg.get("log_dir") or cfg.get("game_dir")
        return _normalize_game_dirs(saved_log)
    except Exception:
        return None


def set_game_dir(candidate_path: str) -> Optional[tuple[str, str]]:
    """校验并保存用户指定的游戏目录或 Logs 目录。"""
    res = _normalize_game_dirs(candidate_path)
    if res:
        _save_local_paths(res[0], res[1])
        return res
    return None


def choose_game_dir_via_dialog(parent=None) -> Optional[tuple[str, str]]:
    """弹出系统文件夹选择器供用户指定游戏目录并保存。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        temp_root = None
        if parent is None:
            temp_root = tk.Tk()
            temp_root.withdraw()
            temp_root.attributes("-topmost", True)
            parent = temp_root
        selected = filedialog.askdirectory(
            parent=parent,
            title="请选择《篮球少女》游戏安装文件夹（含 LQSN.exe 或 _game_data_ / Logs）",
        )
        if temp_root is not None:
            temp_root.destroy()
        if selected:
            return set_game_dir(selected)
    except Exception:
        pass
    return None


def resolve_game_dirs(interactive_on_missing: bool = True) -> tuple[str, str]:
    """
    返回 (DOCS_DIR, LOG_DIR)：
    1. 优先读取环境变量 LQSN_LOG_DIR / LQSN_GAME_DIR
    2. 其次读取 config/local_paths.json
    3. Linux 下默认使用本机已知路径
    4. Windows 下先自动扫描常见安装位置，找不到时（可选）弹出文件夹选择框并保存至 config/local_paths.json
    """
    for env_key in ("LQSN_LOG_DIR", "LQSN_GAME_DIR"):
        env_val = os.environ.get(env_key)
        if env_val:
            res = _normalize_game_dirs(env_val)
            if res:
                return res

    saved = get_saved_local_paths()
    if saved:
        return saved

    if not IS_WINDOWS and os.path.isdir(LINUX_DEFAULT_LOG_DIR):
        return LINUX_DEFAULT_DOCS_DIR, LINUX_DEFAULT_LOG_DIR

    if IS_WINDOWS:
        scanned = _auto_scan_windows_game_dirs()
        if scanned:
            _save_local_paths(scanned[0], scanned[1])
            return scanned

        if interactive_on_missing:
            chosen = _prompt_user_for_game_dir()
            if chosen:
                _save_local_paths(chosen[0], chosen[1])
                return chosen
        return "", ""

    return LINUX_DEFAULT_DOCS_DIR, LINUX_DEFAULT_LOG_DIR


def _save_local_paths(docs_dir: str, log_dir: str):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOCAL_PATHS_FILE, "w", encoding="utf-8") as f:
            json.dump({"docs_dir": docs_dir, "log_dir": log_dir}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _prompt_user_for_game_dir() -> Optional[tuple[str, str]]:
    """当自动搜索未找到日志目录时，在 Windows/桌面环境弹出目录选择框或命令行输入。"""
    print("⚠️ 未在默认位置找到《篮球少女》日志目录 (_game_data_/Documents/Logs)。")
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(
            "🏀 星奈育成军师 — 首次运行路径设置",
            "未自动定位到《篮球少女：飞跃巅峰》游戏目录。\n\n"
            "请在接下来的窗口中选择你的游戏安装文件夹（例如包含 LQSN.exe 或 _game_data_ 的文件夹），选一次即可永久记住！",
        )
        selected = filedialog.askdirectory(title="请选择《篮球少女》游戏安装文件夹 (LQSN)")
        root.destroy()
        if selected:
            res = _normalize_game_dirs(selected)
            if res:
                print(f"✅ 已成功绑定日志目录: {res[1]}")
                return res
    except Exception:
        pass

    try:
        if sys.stdin and sys.stdin.isatty():
            manual = input("👉 请粘贴《篮球少女》游戏目录或 Logs 目录路径并按回车: ").strip().strip('"').strip("'")
            res = _normalize_game_dirs(manual)
            if res:
                return res
    except Exception:
        pass
    return None


# =====================================================================
# Windows 原生只读窗口探测 & 悬浮窗样式辅助（纯 user32 只读查询 + 自窗样式）
# =====================================================================

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def win32_get_mouse_location() -> dict:
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    hwnd = ctypes.windll.user32.WindowFromPoint(pt)
    return {"X": int(pt.x), "Y": int(pt.y), "WINDOW": int(hwnd or 0)}


def win32_click_at(x: int, y: int, double_click: bool = False, interval_sec: float = 0.35):
    import time

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double_click:
        time.sleep(interval_sec)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def win32_find_game_hwnd(window_title: str = "篮球少女：飞跃巅峰") -> int:
    """只读查找游戏窗口句柄（先精确匹配标题，再模糊匹配含「篮球少女 / HoopGirls」的非本进程窗口）。"""
    if not IS_WINDOWS:
        return 0
    try:
        user32 = ctypes.windll.user32
        own_pid = os.getpid()

        hwnd = user32.FindWindowW(None, window_title)
        if hwnd and user32.IsWindowVisible(hwnd):
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) != own_pid:
                return int(hwnd)

        found = [0]
        enum_proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        keywords = ("篮球少女", "hoopgirls", "hoop girls")

        def _cb(h, _lp):
            if not user32.IsWindowVisible(h):
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if int(pid.value) == own_pid:
                return True
            length = user32.GetWindowTextLengthW(h)
            if length <= 0 or length > 200:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            title = buf.value
            low = title.lower()
            if any(k in low for k in keywords) and "军师" not in title and "lqsncoach" not in low and "hoopcoach" not in low:
                rect = _RECT()
                if user32.GetWindowRect(h, ctypes.byref(rect)):
                    if (rect.right - rect.left) >= 320 and (rect.bottom - rect.top) >= 200:
                        found[0] = int(h)
                        return False
            return True

        user32.EnumWindows(enum_proc_t(_cb), 0)
        return found[0]
    except Exception:
        return 0


def win32_query_game_rect(window_title: str = "篮球少女：飞跃巅峰") -> Optional[tuple[int, int, int, int]]:
    """返回游戏窗口客户区（不含系统标题栏）的屏幕坐标 (x, y, w, h)；若未运行或最小化返回 None。"""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = win32_find_game_hwnd(window_title)
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None

        # 优先使用客户区坐标，窗口模式也能精准贴合游戏画面右上角，不盖住 Windows 标题栏
        crect = _RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(crect)):
            cw = crect.right - crect.left
            ch = crect.bottom - crect.top
            if cw > 100 and ch > 100:
                pt = _POINT(0, 0)
                if user32.ClientToScreen(hwnd, ctypes.byref(pt)):
                    return (int(pt.x), int(pt.y), int(cw), int(ch))

        rect = _RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 100 and h > 100:
                return (int(rect.left), int(rect.top), int(w), int(h))
    except Exception:
        pass
    return None


def win32_is_foreground_in(allowed_hwnds: set[int]) -> bool:
    """只读检查当前前台激活窗口（或其根属主）是否属于允许集合（例如游戏窗口或 HUD 自身）。"""
    if not IS_WINDOWS:
        return True
    try:
        user32 = ctypes.windll.user32
        fg = int(user32.GetForegroundWindow() or 0)
        if not fg:
            return False
        if fg in allowed_hwnds:
            return True
        GA_ROOT = 2
        root_fg = int(user32.GetAncestor(fg, GA_ROOT) or 0)
        return root_fg in allowed_hwnds
    except Exception:
        return True


def _win32_get_toplevel_hwnd(tk_win) -> int:
    if not IS_WINDOWS:
        return 0
    try:
        tk_win.update_idletasks()
        wid = int(tk_win.winfo_id())
        parent = int(ctypes.windll.user32.GetParent(wid) or 0)
        return parent or wid
    except Exception:
        return 0


def _win32_apply_overlay_styles(tk_win, click_through: bool = False) -> int:
    """
    为自身悬浮窗设置 Win32 扩展样式：
    - WS_EX_TOOLWINDOW: 不在任务栏/Alt+Tab 列表里添乱
    - WS_EX_NOACTIVATE: 点击顶栏不抢走游戏前台焦点
    - WS_EX_LAYERED | WS_EX_TRANSPARENT (click_through=True): 正文区 100% 鼠标点穿直达游戏
    """
    if not IS_WINDOWS:
        return 0
    try:
        hwnd = _win32_get_toplevel_hwnd(tk_win)
        if not hwnd:
            return 0
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_LAYERED = 0x00080000
        WS_EX_NOACTIVATE = 0x08000000
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020

        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if click_through:
            ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        return hwnd
    except Exception:
        return 0


# =====================================================================
# Windows 免依赖置顶悬浮窗 (Tkinter + Win32 API 双窗分层)
# =====================================================================

class _TkGLibShim:
    def __init__(self, root):
        self._root = root

    def timeout_add(self, interval_ms: int, callback):
        def _tick():
            try:
                keep_going = callback()
            except Exception:
                keep_going = True
            if keep_going is not False:
                try:
                    self._root.after(interval_ms, _tick)
                except Exception:
                    pass

        self._root.after(interval_ms, _tick)

    def idle_add(self, callback, *args):
        try:
            self._root.after(1, lambda: callback(*args))
        except Exception:
            pass


class _TkGtkShim:
    def __init__(self, root):
        self._root = root

    def events_pending(self) -> bool:
        return False

    def main_iteration(self):
        try:
            self._root.update_idletasks()
        except Exception:
            pass

    def main(self):
        self._root.mainloop()


class WindowsFloatingHUD:
    """
    Windows 平台专用的置顶半透明悬浮窗（Python 标准库 tkinter + ctypes.windll.user32）：
    - 无需安装 GTK3 / PyGObject 等任何第三方库
    - 支持两种挂载模式：独立运行（自建 tk.Tk）或嵌入桌面控制台（作为 master 的 tk.Toplevel）
    - 默认开启「仅游戏窗口在前台时显示」：切微信/浏览器/终端或游戏最小化时瞬间自动隐身
    - 双窗分层设计：
      * 顶栏窗口 (self.root)：支持鼠标左键拖拽、右键一键折叠/展开，不抢焦点、不占任务栏
      * 正文窗口 (self.body_win)：紧贴顶栏正下方，设 WS_EX_TRANSPARENT 100% 鼠标点击穿透
    """

    def __init__(
        self,
        strategy: dict,
        auto_play: bool,
        stat_names: dict,
        get_whistle_step_fn,
        note_markup_fn,
        hud_config: Optional[dict] = None,
        master=None,
        follow_game_focus: bool = True,
        diegetic_mode: Optional[bool] = None,
    ):
        import tkinter as tk
        from hud_badges import DEFAULT_HUD_ANCHORS
        from win_diegetic_hud import PIL_AVAILABLE, Win32BadgePool

        if IS_WINDOWS and master is None:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass

        self.tk = tk
        self._owns_root = master is None
        self.strategy = strategy
        self.auto_play = auto_play
        self.stat_names = stat_names
        self.get_whistle_step = get_whistle_step_fn
        self.format_note_markup = note_markup_fn
        self.hud_config = dict(hud_config or {})

        self.pil_available = bool(PIL_AVAILABLE)
        want_diegetic = (
            bool(self.hud_config.get("diegetic_mode", True))
            if diegetic_mode is None
            else bool(diegetic_mode)
        )
        # 缺少 Pillow 时优雅降级为紧凑浮窗模式
        self.diegetic_mode = bool(want_diegetic and self.pil_available)

        try:
            import live_coach as _lc

            self.anchors = _lc.load_ui_anchor_coords()
        except Exception:
            self.anchors = DEFAULT_HUD_ANCHORS

        self.follow_game_focus = bool(follow_game_focus)
        self.user_enabled = True
        self._scene_hidden = False
        self._is_shown = True
        self._closing = False

        # 主根窗 = 紧凑顶栏 / 贴身模式单行状态胶囊（可拖拽、可右键切换、不抢焦点）
        self.root = tk.Tk() if master is None else tk.Toplevel(master)
        self.root.title("🏀 星奈育成军师 HUD")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.configure(bg="#f59e0b")

        # 独立正文子窗 = 100% 鼠标点击穿透
        self.body_win = tk.Toplevel(self.root)
        self.body_win.title("🏀 星奈军师正文")
        self.body_win.overrideredirect(True)
        self.body_win.attributes("-topmost", True)
        self.body_win.attributes("-alpha", 0.90)
        self.body_win.configure(bg="#f59e0b")

        # 方案 A 原生抗锯齿分层穿透角标池（10 枚）
        self.badge_pool = Win32BadgePool(size=10)
        self._active_badge_specs: list[dict] = []
        self.trough_markup = "待命｜进入选人或育成自动贴标"
        self._top_slot_custom_offset = None
        self._trough_custom_offset = None
        self.on_diegetic_changed_cb = None

        self.GLib = _TkGLibShim(self.root)
        self.Gtk = _TkGtkShim(self.root)

        self.collapsed = False
        self.full_markup = ""
        self.tip_markup = ""
        self.mini_markup = "<span foreground='#fbbf24' weight='bold'>🏀 星奈军师待命中...</span>"
        self.custom_offset = None
        self.manual_pos = None
        self.drag_active = False
        self.drag_start_root = (0, 0)
        self.drag_start_win = (0, 0)
        self.last_game_rect = None
        self.on_auto_scan_cb = None
        self.on_focus_changed_cb = None

        self._last_state = None
        self._last_turn_scored = None
        self._last_wk_adv = None
        self._last_story_adv = None
        self._last_skill_evals = None
        self._last_note_adv = None
        self._active_view_mode = "ready"
        self._in_scene_refresh = False

        self._header_hwnd = 0
        self._body_hwnd = 0

        # 顶栏布局
        self.header_inner = tk.Frame(self.root, bg="#1e293b", bd=0, highlightthickness=0)
        self.header_inner.pack(fill="both", expand=True, padx=2, pady=(2, 1))

        self.header_text = tk.Text(
            self.header_inner,
            bg="#1e293b",
            fg="#f8fafc",
            font=("Microsoft YaHei UI", 10),
            bd=0,
            highlightthickness=0,
            wrap="none",
            height=1,
            width=46,
            padx=8,
            pady=4,
            cursor="fleur",
        )
        self.header_text.pack(fill="x", expand=True)
        self.header_text.bind("<ButtonPress-1>", self._on_header_press)
        self.header_text.bind("<ButtonRelease-1>", self._on_header_release)
        self.header_text.bind("<B1-Motion>", self._on_header_motion)
        self.header_text.bind("<ButtonPress-3>", self._on_header_right_click)

        # 正文布局（独立穿透窗）
        self.body_inner = tk.Frame(self.body_win, bg="#121726", bd=0, highlightthickness=0)
        self.body_inner.pack(fill="both", expand=True, padx=2, pady=(0, 2))

        self.body_text = tk.Text(
            self.body_inner,
            bg="#121726",
            fg="#e2e8f0",
            font=("Microsoft YaHei UI", 10),
            bd=0,
            highlightthickness=0,
            wrap="word",
            height=4,
            width=46,
            padx=10,
            pady=6,
            cursor="arrow",
        )
        self.body_text.pack(fill="both", expand=True)

        # 绑定 Win32 不抢焦点 + 正文穿透扩展样式
        self.root.update_idletasks()
        self.body_win.update_idletasks()
        self._header_hwnd = _win32_apply_overlay_styles(self.root, click_through=False)
        self._body_hwnd = _win32_apply_overlay_styles(self.body_win, click_through=True)

        mode_str = "🤖自动代打" if auto_play else "实时军师"
        self._update_header(f"<span weight='bold' foreground='#fbbf24'>🏀 星奈 · {mode_str}</span>")
        init_body = "<span foreground='#cbd5e1'>⏳ 监听中，进入选人或育成自动刷新</span>"
        init_tip = "<span foreground='#94a3b8'>按住顶栏拖动  ·  右键折叠/切模式  ·  正文鼠标穿透</span>"
        self.set_markup(init_body, mini_summary="🏀 星奈待命", tip_markup=init_tip)
        self.sync_position_and_raise(force=True)
        self.GLib.timeout_add(250, self.sync_position_and_raise)

    def _rec_enabled(self, key: str) -> bool:
        recs = (self.hud_config or {}).get("recommendations")
        if not isinstance(recs, dict):
            return True
        return bool(recs.get(key, True))

    def _uses_bottom_trough(self) -> bool:
        return self._active_view_mode in ("initactor", "skill_choice")

    def _is_native_slot_mode(self) -> bool:
        return bool(self.diegetic_mode and self._active_view_mode != "initactor")

    def set_diegetic_mode(self, enabled: bool) -> bool:
        """一键热切「✨ 原生贴身角标 / 📋 紧凑浮窗」；若缺少 Pillow 自动降级返回 False。"""
        want = bool(enabled)
        if want and not self.pil_available:
            self.diegetic_mode = False
            self.hide_all_badges()
            self._apply_current_markup()
            return False
        self.diegetic_mode = want
        if not self.diegetic_mode:
            self.collapsed = False
            self.hide_all_badges()
        if self._last_state is not None:
            self.refresh_scene_overlay(self._last_state)
        else:
            self._apply_current_markup()
        if callable(self.on_diegetic_changed_cb):
            try:
                self.on_diegetic_changed_cb(self.diegetic_mode)
            except Exception:
                pass
        return self.diegetic_mode

    def set_follow_game_focus(self, enabled: bool):
        self.follow_game_focus = bool(enabled)
        self.sync_position_and_raise(force=True)

    def set_user_enabled(self, enabled: bool):
        self.user_enabled = bool(enabled)
        if not self.user_enabled:
            self.badge_pool.hide_all()
        self.sync_position_and_raise(force=True)

    def reset_position(self):
        self.custom_offset = None
        self._top_slot_custom_offset = None
        self._trough_custom_offset = None
        self.manual_pos = None
        self.sync_position_and_raise(force=True)

    def hide_all_badges(self):
        self._active_badge_specs = []
        self.badge_pool.hide_all()

    def get_diagnostic_context(self) -> dict:
        """供快捷 Issue 反馈模块采集脱敏 HUD / 窗口状态上下文。"""
        from hud_badges import compute_game_content_box

        rect = self.last_game_rect or (win32_query_game_rect() if IS_WINDOWS else None)
        content_box = compute_game_content_box(rect) if rect else None
        aspect = round(rect[2] / float(rect[3]), 3) if (rect and rect[3] > 0) else None
        st = self._last_state or {}
        return {
            "hud_mode": "diegetic_badges" if self.diegetic_mode else "compact_card",
            "pil_available": self.pil_available,
            "follow_game_focus": self.follow_game_focus,
            "active_view_mode": self._active_view_mode,
            "game_client_size": f"{rect[2]}x{rect[3]}" if rect else "未检测到游戏窗口",
            "game_aspect_ratio": aspect,
            "content_box_size": f"{content_box[2]}x{content_box[3]}" if content_box else None,
            "round": int(st.get("round") or 0),
            "exp_round": int(st.get("exp_round") or 0),
            "round_type": int(st.get("round_type") or 1),
        }

    def set_hud_config(self, new_cfg: dict, state: Optional[dict] = None):
        self.hud_config = dict(new_cfg or {})
        if "diegetic_mode" in self.hud_config:
            self.diegetic_mode = bool(self.hud_config["diegetic_mode"] and self.pil_available)
            if not self.diegetic_mode:
                self.hide_all_badges()
        st = state or self._last_state
        if st is not None:
            self.refresh_scene_overlay(st)
        else:
            self._apply_current_markup()

    def refresh_scene_overlay(self, state: dict):
        """根据游戏当前场景栈同步角标池、单行状态槽与过场自动隐身（完整对齐 14 种场景）。"""
        if self._closing or state is None or self._in_scene_refresh:
            return
        self._in_scene_refresh = True
        try:
            self._last_state = state
            from hud_badges import build_badges_for_mode

            try:
                import live_coach as lc

                mode = lc.compute_active_hud_view_mode(state)
            except Exception:
                mode = "ready"
            self._active_view_mode = mode

            if mode == "hidden":
                self._scene_hidden = True
                self.hide_all_badges()
                self.sync_position_and_raise(force=True)
                return

            if mode == "initactor":
                self._scene_hidden = not self._rec_enabled("note")
                self.hide_all_badges()
                self._apply_current_markup()
                return

            if mode in ("ycjm01", "ycjm02", "story_choice", "skill_choice", "store", "match_ready") or mode.startswith("weekend_"):
                specs, trough_line = build_badges_for_mode(
                    mode,
                    state,
                    hud_config=self.hud_config,
                    anchors=self.anchors,
                    strategy=self.strategy,
                    last_turn_scored=self._last_turn_scored,
                    last_wk_adv=self._last_wk_adv,
                    last_story_adv=self._last_story_adv,
                    last_skill_evals=self._last_skill_evals,
                    stat_names=self.stat_names,
                    get_whistle_step_fn=self.get_whistle_step,
                )
                if not specs and not trough_line:
                    self._scene_hidden = True
                    self.hide_all_badges()
                    self.sync_position_and_raise(force=True)
                    return
                self._scene_hidden = False
                self._active_badge_specs = list(specs or [])
                if trough_line:
                    self.trough_markup = trough_line
                if not self.diegetic_mode:
                    self.badge_pool.hide_all()
                self._apply_current_markup()
                return

            self._scene_hidden = False
            self._apply_current_markup()
        finally:
            self._in_scene_refresh = False

    def _set_windows_visible(self, show_header: bool, show_body: bool):
        try:
            if not show_header:
                self.badge_pool.hide_all()
            if show_header:
                if self.root.state() == "withdrawn":
                    self.root.deiconify()
                    self._header_hwnd = _win32_apply_overlay_styles(self.root, click_through=False)
                self.root.attributes("-topmost", True)
            else:
                if self.root.state() != "withdrawn":
                    self.root.withdraw()

            if show_header and show_body and not self.collapsed and not self._is_native_slot_mode():
                if self.body_win.state() == "withdrawn":
                    self.body_win.deiconify()
                    self._body_hwnd = _win32_apply_overlay_styles(self.body_win, click_through=True)
                self.body_win.attributes("-topmost", True)
            else:
                if self.body_win.state() != "withdrawn":
                    self.body_win.withdraw()
            self._is_shown = show_header
        except Exception:
            pass

    def _sync_body_geometry(self, hx: int, hy: int):
        if self._is_native_slot_mode():
            return
        try:
            self.root.update_idletasks()
            self.body_win.update_idletasks()
            hw = max(self.root.winfo_width(), self.root.winfo_reqwidth(), 360)
            hh = max(self.root.winfo_height(), self.root.winfo_reqheight(), 28)
            bh = max(self.body_win.winfo_reqheight(), 48)
            self.body_win.geometry(f"{hw}x{bh}+{hx}+{hy + hh}")
        except Exception:
            pass

    def close_cleanly(self, quit_main_loop: bool = True):
        if self._closing:
            return
        self._closing = True
        try:
            self.badge_pool.destroy_all()
        except Exception:
            pass
        try:
            self.body_win.withdraw()
            self.body_win.destroy()
        except Exception:
            pass
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            if self._owns_root and quit_main_loop:
                self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _render_pango_to_text_widget(self, widget, markup_str: str, is_header: bool = False):
        widget.configure(state="normal")
        widget.delete("1.0", "end")

        token_re = re.compile(r"(<span\b[^>]*>|</span>|<b>|</b>)", re.I)
        parts = token_re.split(markup_str or "")
        style_stack = [{"fg": "#f8fafc" if is_header else "#e2e8f0", "bold": False}]
        tag_counter = 0

        for part in parts:
            if not part:
                continue
            low = part.lower()
            if low.startswith("<span"):
                cur = dict(style_stack[-1])
                fg_m = re.search(r"foreground=['\"]([^'\"]+)['\"]", part, re.I)
                if fg_m:
                    cur["fg"] = fg_m.group(1)
                if "weight='bold'" in low or 'weight="bold"' in low:
                    cur["bold"] = True
                style_stack.append(cur)
            elif low == "</span>":
                if len(style_stack) > 1:
                    style_stack.pop()
            elif low == "<b>":
                cur = dict(style_stack[-1])
                cur["bold"] = True
                style_stack.append(cur)
            elif low == "</b>":
                if len(style_stack) > 1:
                    style_stack.pop()
            else:
                txt = html.unescape(part)
                st = style_stack[-1]
                tag_name = f"t_{tag_counter}"
                tag_counter += 1
                font_spec = ("Microsoft YaHei UI", 10, "bold") if st["bold"] else ("Microsoft YaHei UI", 10)
                widget.tag_configure(tag_name, foreground=st["fg"], font=font_spec)
                widget.insert("end", txt, (tag_name,))

        if not is_header:
            raw_lines = widget.get("1.0", "end-1c").splitlines()
            est_rows = 0
            for ln in raw_lines:
                est_rows += max(1, (len(ln) + 42) // 44)
            widget.configure(height=max(2, min(24, est_rows)))
        widget.configure(state="disabled")

    def _on_header_press(self, event):
        self.drag_active = True
        self.drag_start_root = (event.x_root, event.y_root)
        self.drag_start_win = (self.root.winfo_x(), self.root.winfo_y())
        return "break"

    def _on_header_release(self, event):
        if self.drag_active:
            self.drag_active = False
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            w = max(self.root.winfo_width(), self.root.winfo_reqwidth(), 220)
            if self.last_game_rect:
                from hud_badges import compute_game_content_box

                gx, gy, gw, _gh = compute_game_content_box(self.last_game_rect) or self.last_game_rect
                if self._is_native_slot_mode():
                    if self._uses_bottom_trough():
                        self._trough_custom_offset = (wx - gx, wy - gy)
                    else:
                        self._top_slot_custom_offset = (wx - gx, wy - gy)
                else:
                    self.custom_offset = ((gx + gw) - (wx + w), wy - gy)
            else:
                self.manual_pos = (wx, wy)
            if not self._is_native_slot_mode():
                self._sync_body_geometry(wx, wy)
        return "break"

    def _on_header_motion(self, event):
        if self.drag_active:
            dx = event.x_root - self.drag_start_root[0]
            dy = event.y_root - self.drag_start_root[1]
            nx = max(0, self.drag_start_win[0] + dx)
            ny = max(0, self.drag_start_win[1] + dy)
            self.root.geometry(f"+{nx}+{ny}")
            if not self.collapsed and not self._is_native_slot_mode():
                self._sync_body_geometry(nx, ny)
        return "break"

    def _on_header_right_click(self, event):
        if self.diegetic_mode:
            # 贴身角标模式下右键顶栏状态条：一键热切回紧凑卡片模式
            self.set_diegetic_mode(False)
        else:
            self.collapsed = not self.collapsed
            self._apply_current_markup()
        return "break"

    def sync_position_and_raise(self, force=False):
        if self._closing:
            return False
        if self.drag_active:
            return True

        # 1. 用户关闭或过场隐身 → 全隐藏（含角标池）
        if not self.user_enabled or self._scene_hidden:
            self._set_windows_visible(False, False)
            return True

        game_hwnd = win32_find_game_hwnd() if IS_WINDOWS else 0
        rect = win32_query_game_rect() if game_hwnd else None

        # 2. 仅游戏在前台时显示（Windows 下彻底告别压住微信/浏览器/终端！）
        if IS_WINDOWS and self.follow_game_focus:
            if not game_hwnd or not rect:
                self._set_windows_visible(False, False)
                return True
            allowed = {game_hwnd}
            if self._header_hwnd:
                allowed.add(self._header_hwnd)
            if self._body_hwnd:
                allowed.add(self._body_hwnd)
            allowed.update(self.badge_pool.get_hwnds())
            if not win32_is_foreground_in(allowed):
                self._set_windows_visible(False, False)
                return True

        native_slot = self._is_native_slot_mode()
        self._set_windows_visible(True, (not self.collapsed) and (not native_slot))

        self.root.update_idletasks()
        hud_w = max(self.root.winfo_reqwidth(), 220 if native_slot else 360)
        hud_h = max(self.root.winfo_reqheight(), 28)

        target_x, target_y = self.root.winfo_x(), self.root.winfo_y()
        moved = False
        prev_rect = self.last_game_rect
        if rect:
            from hud_badges import compute_game_content_box

            self.last_game_rect = rect
            content_rect = compute_game_content_box(rect) or rect
            gx, gy, gw, gh = content_rect
            if native_slot:
                use_bot = self._uses_bottom_trough()
                cust_off = self._trough_custom_offset if use_bot else self._top_slot_custom_offset
                if cust_off is not None:
                    target_x = max(0, gx + cust_off[0])
                    target_y = max(0, gy + cust_off[1])
                elif use_bot:
                    tr_cfg = self.anchors.get("bottom_trough", {"rx": 0.530, "ry": 0.915})
                    cx = int(gx + gw * float(tr_cfg.get("rx", 0.530)))
                    cy = int(gy + gh * float(tr_cfg.get("ry", 0.915)))
                    target_x = max(gx + 8, min(gx + gw - hud_w - 8, cx - hud_w // 2))
                    target_y = max(gy + 4, min(gy + gh - hud_h - 4, cy - hud_h // 2))
                else:
                    ts_cfg = self.anchors.get("top_bar_slot", {"rx": 0.665, "ry": 0.042})
                    cx = int(gx + gw * float(ts_cfg.get("rx", 0.665)))
                    cy = int(gy + gh * float(ts_cfg.get("ry", 0.042)))
                    min_x = int(gx + gw * 0.560)
                    max_x = max(min_x, int(gx + gw * 0.800) - hud_w)
                    target_x = max(min_x, min(max_x, cx - hud_w // 2))
                    target_y = max(gy + 4, min(gy + gh - hud_h - 4, cy - hud_h // 2))
            else:
                margin_r, margin_t = self.custom_offset if self.custom_offset is not None else (14, 12)
                target_x = max(0, gx + gw - hud_w - margin_r)
                target_y = max(0, gy + margin_t)
            if force or abs(self.root.winfo_x() - target_x) > 3 or abs(self.root.winfo_y() - target_y) > 3:
                self.root.geometry(f"+{target_x}+{target_y}")
                moved = True
        elif force and self.manual_pos is None:
            sw = self.root.winfo_screenwidth()
            target_x = max(0, sw - hud_w - 30)
            target_y = 40
            self.root.geometry(f"+{target_x}+{target_y}")
            moved = True

        if native_slot:
            if rect and self._active_badge_specs:
                if force or rect != prev_rect:
                    self.badge_pool.apply_specs(self._active_badge_specs, rect)
            else:
                self.badge_pool.hide_all()
        else:
            self.badge_pool.hide_all()
            if not self.collapsed and (force or moved):
                self._sync_body_geometry(target_x, target_y)
        return True

    def _update_header(self, header_markup: str):
        self._last_header = header_markup
        if not self.collapsed and not self._is_native_slot_mode():
            self._render_pango_to_text_widget(self.header_text, header_markup, is_header=True)

    def _apply_current_markup(self):
        if self._is_native_slot_mode():
            # 轻巧单行原生状态胶囊样式（冰灰底 + 深藏青字）
            from hud_badges import strip_markup_to_clean_text

            self.root.configure(bg="#9ec2d8")
            self.header_inner.configure(bg="#d8e5ed")
            self.header_text.configure(bg="#d8e5ed", fg="#253b52", width=36)
            raw_t = self.trough_markup or "星奈｜实时参谋就绪"
            if "｜" in raw_t:
                tag_s, body_s = raw_t.split("｜", 1)
            else:
                tag_s, body_s = "星奈", strip_markup_to_clean_text(raw_t)
            tag_clean = strip_markup_to_clean_text(tag_s)[:8] or "星奈"
            body_clean = strip_markup_to_clean_text(body_s)[:36]
            slot_mk = (
                f"<span foreground='#0284c7' weight='bold'>[{html.escape(tag_clean)}]</span> "
                f"<span foreground='#1e3a5f' weight='bold'>{html.escape(body_clean)}</span>"
            )
            est_w = max(24, min(52, len(tag_clean) * 2 + len(body_clean) * 2 + 4))
            self.header_text.configure(width=est_w)
            self._render_pango_to_text_widget(self.header_text, slot_mk, is_header=True)
            self._set_windows_visible(self._is_shown, False)
        else:
            # 紧凑浮窗深色卡片样式
            self.root.configure(bg="#f59e0b")
            self.header_inner.configure(bg="#1e293b")
            self.header_text.configure(bg="#1e293b", fg="#f8fafc", width=46)
            if self.collapsed:
                self._render_pango_to_text_widget(
                    self.header_text,
                    f"{self.mini_markup}  <span foreground='#64748b'>▸</span>",
                    is_header=True,
                )
                self._set_windows_visible(self._is_shown, False)
            else:
                self._render_pango_to_text_widget(
                    self.header_text,
                    getattr(self, "_last_header", "🏀 星奈 · 实时军师"),
                    is_header=True,
                )
                combined = self.full_markup
                tip_clean = (getattr(self, "tip_markup", "") or "").strip()
                if tip_clean:
                    combined = f"{combined}\n{tip_clean}" if combined else tip_clean
                self._render_pango_to_text_widget(self.body_text, combined, is_header=False)
        self.sync_position_and_raise(force=True)

    def set_markup(
        self,
        markup_str: str,
        mini_summary: str = None,
        equip_icons: list = None,
        tip_markup: str = "",
    ):
        self.full_markup = markup_str
        self.tip_markup = tip_markup or ""
        if mini_summary:
            self.mini_markup = mini_summary
        self._apply_current_markup()

    @staticmethod
    def _format_hud_tag_spans(tags: list, muted: bool = False) -> str:
        if not tags:
            return ""
        if muted:
            joined = " · ".join(html.escape(t) for t in tags[:2])
            return f"  <span foreground='#64748b'>{joined}</span>"
        spans = []
        for t in tags[:4]:
            esc = html.escape(t)
            if t.startswith(("⚠️", "🚨")):
                spans.append(f"<span foreground='#fca5a5'>{esc}</span>")
            elif any(k in t for k in ("哨冲", "必爆", "满格爆气", "满彩")):
                spans.append(f"<span foreground='#fde047'>{esc}</span>")
            elif any(k in t for k in ("差1升", "50%爆", "冲3爆", "冲6爆", "冲9爆", "突破")):
                spans.append(f"<span foreground='#93c5fd'>{esc}</span>")
            else:
                spans.append(f"<span foreground='#cbd5e1'>{esc}</span>")
        sep = "<span foreground='#475569'> · </span>"
        return f"  {sep.join(spans)}"

    def render_skill_choice(self, state: dict, skill_evals: list):
        """渲染育成选技能三选一界面。"""
        self._last_state = state
        self._last_skill_evals = list(skill_evals or [])
        try:
            from skill_advisor import get_char_role_tags
            roles = sorted(get_char_role_tags(int(state.get("main_char_id") or 0)) - {"通用"})
        except Exception:
            roles = []
        role_str = "/".join(roles) if roles else "通用"
        self._update_header(
            f"<span weight='bold' foreground='#38bdf8'>🎯 技能三选一 · 职责[{html.escape(role_str)}]</span>"
        )
        lines = []
        slot_uncertain = any(bool(x.get("slot_uncertain") or x.get("is_unknown")) for x in self._last_skill_evals)
        slot_names = ("候选1", "候选2", "候选3") if slot_uncertain else ("左卡", "中卡", "右卡")
        show_sec = self._rec_enabled("secondary")
        for item in self._last_skill_evals[:3]:
            st_k = item.get("style_key", "second")
            if not show_sec and st_k == "second":
                continue
            s_lbl = slot_names[item["slot_idx"]] if 0 <= item.get("slot_idx", -1) < 3 else f"#{item.get('slot_idx', 0)+1}"
            btxt = html.escape(item.get("badge_text", ""))
            snm = html.escape(item.get("skill_name", ""))
            rsn = html.escape(item.get("reason", ""))
            color = (
                "#4ade80"
                if st_k == "best"
                else ("#fca5a5" if st_k == "warn" else ("#94a3b8" if st_k == "dup" else "#7dd3fc"))
            )
            lines.append(
                f"<span foreground='{color}' weight='bold'>{s_lbl} {btxt}</span>  "
                f"<span foreground='#f8fafc' weight='bold'>{snm}</span>  "
                f"<span foreground='#cbd5e1'>{rsn}</span>"
            )
        top_sk = next((x for x in self._last_skill_evals if x.get("rank") == 1), None)
        mini = (
            f"<span foreground='#38bdf8' weight='bold'>🎯选技能</span>  "
            f"<span foreground='#4ade80' weight='bold'>{html.escape(top_sk['skill_name'])}</span>"
            if top_sk
            else "<span foreground='#38bdf8' weight='bold'>🎯选技能</span>"
        )
        self.set_markup("\n".join(lines), mini_summary=mini, tip_markup="")
        self.refresh_scene_overlay(state)

    def render_story_choice(self, state: dict, ev_advice: dict):
        self._last_state = state
        self._last_story_adv = ev_advice
        ev_title = html.escape(ev_advice["ev_name"])
        self._update_header(
            f"<span weight='bold' foreground='#38bdf8'>📖 {ev_title}</span>"
        )
        lines = []
        tip_str = ""
        scored_opts = ev_advice.get("scored_options") or []
        show_sec = self._rec_enabled("secondary")
        if scored_opts:
            top_ev = scored_opts[0]["ev"]
            for i, item in enumerate(scored_opts[: (3 if show_sec else 1)]):
                t_esc = html.escape(item["title"])
                e_esc = html.escape(item["effects"])
                if i == 0:
                    lines.append(
                        f"<span foreground='#4ade80' weight='bold'>👑 {t_esc}</span> "
                        f"<span foreground='#86efac'>EV {item['ev']:.0f}</span>  "
                        f"<span foreground='#e2e8f0'>{e_esc}</span>"
                    )
                else:
                    diff = top_ev - item["ev"]
                    badge = "🥈" if i == 1 else "🥉"
                    if diff >= 35:
                        lines.append(
                            f"<span foreground='#64748b'>{badge} {t_esc}  -{diff:.0f} EV  {e_esc}</span>"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#38bdf8' weight='bold'>{badge} {t_esc}</span> "
                            f"<span foreground='#94a3b8'>-{diff:.0f} EV</span>  "
                            f"<span foreground='#cbd5e1'>{e_esc}</span>"
                        )
        else:
            for ol in ev_advice["opt_lines"]:
                lines.append(f"<span foreground='#e2e8f0'>{html.escape(ol)}</span>")
            if ev_advice.get("recommendation"):
                tip_str = f"<span foreground='#fde047'>{html.escape(ev_advice['recommendation'])}</span>"
        first_rec_line = ev_advice["recommendation"].splitlines()[0]
        self.set_markup(
            "\n".join(lines),
            mini_summary=f"<span foreground='#38bdf8' weight='bold'>📖 {ev_title}</span> <span foreground='#4ade80' weight='bold'>{html.escape(first_rec_line)}</span>",
            tip_markup=tip_str,
        )

    def render_weekend_advice(self, state: dict, wk: dict):
        self._last_state = state
        self._last_wk_adv = wk
        day_esc = html.escape(wk["day_tag"])
        has_wt = bool(wk.get("has_weekend_train") and wk.get("best_train_opt"))
        show_sec = self._rec_enabled("secondary")
        if has_wt:
            self._update_header(
                f"<span weight='bold' foreground='#fbbf24'>⚡ {day_esc}</span>"
            )
        else:
            self._update_header(
                f"<span weight='bold' foreground='#c084fc'>🌴 {day_esc}</span>"
            )
        best_c = html.escape(wk.get("best_compact") or wk.get("headline", ""))
        best_ev = float(wk.get("best_ev", 0.0))
        best_act = wk.get("best_act") or (8, 19)
        alt_raw = wk.get("alt_compact", "")

        if has_wt:
            tr_opt = wk["best_train_opt"]
            top_t = tr_opt["train_item"]
            t2_item = wk.get("second_train_item")
            out_opt = wk.get("best_outing_opt")
            out2_opt = wk.get("second_outing_opt")
            lines = []
            if best_act[0] == 0:
                tag_html = self._format_hud_tag_spans(top_t.get("hud_tags") or [], muted=False)
                lines.append(
                    f"<span foreground='#4ade80' weight='bold'>👑 加训</span> "
                    f"<span foreground='#86efac'>EV {best_ev:.0f}</span>  "
                    f"<span foreground='#f8fafc' weight='bold'>{best_c}</span>{tag_html}"
                )
                if show_sec and t2_item:
                    diff_t2 = max(0.0, best_ev - float(t2_item.get("route_ev", t2_item["score"])))
                    ps_d2 = int(t2_item["cmd"].get("ps", 0))
                    ps_s2 = f"+{ps_d2}体" if ps_d2 > 0 else f"{ps_d2}体"
                    t2_esc = html.escape(f"🏋️【{t2_item['name']}】({ps_s2})")
                    tag_html_2 = self._format_hud_tag_spans(t2_item.get("hud_tags") or [], muted=(diff_t2 >= 35.0))
                    if diff_t2 >= 35.0:
                        lines.append(
                            f"<span foreground='#64748b'>🥈 次训  -{diff_t2:.0f} EV  {t2_esc}</span>{tag_html_2}"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#38bdf8' weight='bold'>🥈 次训</span> "
                            f"<span foreground='#94a3b8'>-{diff_t2:.0f} EV</span>  "
                            f"<span foreground='#cbd5e1'>{t2_esc}</span>{tag_html_2}"
                        )
                if show_sec and out_opt:
                    diff_out = max(0.0, best_ev - float(out_opt["total_ev"]))
                    out_c = html.escape(out_opt["compact"])
                    if diff_out >= 35.0:
                        lines.append(
                            f"<span foreground='#94a3b8'>🌴 外出  -{diff_out:.0f} EV  {out_c}</span>"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#c084fc' weight='bold'>🌴 外出</span> "
                            f"<span foreground='#d8b4fe'>-{diff_out:.0f} EV</span>  "
                            f"<span foreground='#e2e8f0'>{out_c}</span>"
                        )
            else:
                lines.append(
                    f"<span foreground='#4ade80' weight='bold'>👑 外出</span> "
                    f"<span foreground='#86efac'>EV {best_ev:.0f}</span>  "
                    f"<span foreground='#f8fafc' weight='bold'>{best_c}</span>"
                )
                if show_sec:
                    diff_tr = max(0.0, best_ev - float(tr_opt["total_ev"]))
                    tr_c = html.escape(tr_opt["compact"])
                    tag_html_tr = self._format_hud_tag_spans(top_t.get("hud_tags") or [], muted=(diff_tr >= 35.0))
                    if diff_tr >= 35.0:
                        lines.append(
                            f"<span foreground='#94a3b8'>🏋️ 加训  -{diff_tr:.0f} EV  {tr_c}</span>{tag_html_tr}"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#fbbf24' weight='bold'>🏋️ 加训</span> "
                            f"<span foreground='#fde68a'>-{diff_tr:.0f} EV</span>  "
                            f"<span foreground='#e2e8f0'>{tr_c}</span>{tag_html_tr}"
                        )
                    if out2_opt:
                        diff_o2 = max(0.0, best_ev - float(out2_opt["total_ev"]))
                        out2_c = html.escape(out2_opt["compact"])
                        if diff_o2 >= 35.0:
                            lines.append(
                                f"<span foreground='#64748b'>🥈 外出次选  -{diff_o2:.0f} EV  {out2_c}</span>"
                            )
                        else:
                            lines.append(
                                f"<span foreground='#38bdf8' weight='bold'>🥈 外出次选</span> "
                                f"<span foreground='#94a3b8'>-{diff_o2:.0f} EV</span>  "
                                f"<span foreground='#cbd5e1'>{out2_c}</span>"
                            )
        else:
            lines = [
                f"<span foreground='#4ade80' weight='bold'>👑 首选</span> "
                f"<span foreground='#86efac'>EV {best_ev:.0f}</span>  "
                f"<span foreground='#f8fafc' weight='bold'>{best_c}</span>"
            ]
            if show_sec and alt_raw:
                alt_c = html.escape(alt_raw)
                diff_ev = float(wk.get("alt_diff_ev", 0.0))
                if diff_ev >= 35.0:
                    lines.append(
                        f"<span foreground='#64748b'>🥈 次选  -{diff_ev:.0f} EV  {alt_c}</span>"
                    )
                else:
                    lines.append(
                        f"<span foreground='#38bdf8' weight='bold'>🥈 次选</span> "
                        f"<span foreground='#94a3b8'>-{diff_ev:.0f} EV</span>  "
                        f"<span foreground='#cbd5e1'>{alt_c}</span>"
                    )

        tip_lines = []
        if has_wt and best_act[0] != 0 and wk.get("second_train_item"):
            t2_i = wk["second_train_item"]
            d_t2 = max(0.0, best_ev - float(t2_i.get("route_ev", t2_i["score"])))
            tip_lines.append(
                f"<span foreground='#fde68a'>🏋️ 次优加训：{html.escape(t2_i['name'])}(EV {t2_i.get('route_ev', t2_i['score']):.0f}, -{d_t2:.0f})</span>"
            )
        if wk.get("hud_lookahead"):
            tip_lines.append(
                f"<span foreground='#fcd34d' weight='bold'>{html.escape(wk['hud_lookahead'])}</span>"
            )
        shop_extra = [x for x in (wk.get("hud_free_tip", ""), wk.get("hud_shop_tip", "")) if x]
        if shop_extra and self._rec_enabled("shop"):
            tip_lines.append(
                "<span foreground='#7dd3fc'>"
                + "  ·  ".join(html.escape(x) for x in shop_extra)
                + "</span>"
            )
        g_list = wk.get("scored_gashapon") or []
        if (
            len(g_list) > 1
            and (g_list[0]["ev"] - g_list[1]["ev"]) <= 12.0
            and ("扭蛋" in wk.get("best_compact", "") or "扭蛋" in alt_raw or wk.get("best_act") == (6, 17))
        ):
            tip_lines.append(
                f"<span foreground='#f9a8d4'>🎰 扭蛋近分同档：{html.escape(g_list[1]['short'])}</span>"
            )

        mini_hdr = (
            f"<span foreground='#fbbf24' weight='bold'>⚡{day_esc}</span>"
            if has_wt
            else f"<span foreground='#c084fc' weight='bold'>🌴{day_esc}</span>"
        )
        mini = f"{mini_hdr}  <span foreground='#4ade80' weight='bold'>👑{best_c}</span>"
        self.set_markup("\n".join(lines), mini_summary=mini, tip_markup="\n".join(tip_lines))

    def render_turn(self, state: dict, scored: list, extra_tip: str = ""):
        self._last_state = state
        self._last_turn_scored = scored
        try:
            from live_coach import format_hud_turn_header, format_weekday_hud_tips
            hdr_str = format_hud_turn_header(state)
            tip_lines = list(format_weekday_hud_tips(state, scored))
        except Exception:
            hdr_str = f"<span weight='bold' foreground='#fbbf24'>🏀 第{state['round']:02d}/28训</span>"
            tip_lines = []

        self._update_header(hdr_str)

        lines = []
        top_ev = scored[0]["score"] if scored else 0.0
        show_sec = self._rec_enabled("secondary")
        for i, item in enumerate(scored[: (2 if show_sec else 1)]):
            name_esc = html.escape(item["name"])
            tags = item.get("hud_tags") or []
            if i == 0:
                tag_html = self._format_hud_tag_spans(tags, muted=False)
                lines.append(
                    f"<span foreground='#4ade80' weight='bold'>👑 {name_esc}</span> "
                    f"<span foreground='#86efac'>EV {item['score']:.0f}</span>"
                    f"{tag_html}"
                )
            else:
                diff = top_ev - item["score"]
                if diff >= 35.0:
                    tag_html = self._format_hud_tag_spans(tags, muted=True)
                    lines.append(
                        f"<span foreground='#64748b'>🥈 {name_esc}  -{diff:.0f} EV</span>{tag_html}"
                    )
                else:
                    tag_html = self._format_hud_tag_spans(tags, muted=False)
                    lines.append(
                        f"<span foreground='#38bdf8' weight='bold'>🥈 {name_esc}</span> "
                        f"<span foreground='#94a3b8'>-{diff:.0f} EV</span>"
                        f"{tag_html}"
                    )

        if extra_tip and self._rec_enabled("shop"):
            tip_lines.append(f"<span foreground='#7dd3fc'>{html.escape(extra_tip)}</span>")

        if int(state.get("round", 0)) == 1 and state.get("main_char_id"):
            try:
                from equipment_advisor import check_lineup_and_equip_synergy
                cfg_ids = sorted(set(state.get("supactor_card_to_cfg", {}).values()))
                syn_tips = check_lineup_and_equip_synergy(
                    int(state["main_char_id"]),
                    state.get("teammate_char_ids", []),
                    cfg_ids,
                )
                for st_line in syn_tips[:2]:
                    tip_lines.append(f"<span foreground='#fde047'>{html.escape(st_line)}</span>")
            except Exception:
                pass

        if scored:
            best = scored[0]
            mini = (
                f"<span foreground='#fbbf24' weight='bold'>🏀{state['round']:02d}训</span>  "
                f"<span foreground='#4ade80' weight='bold'>👑{html.escape(best['name'])} (EV {best['score']:.0f})</span>"
            )
        else:
            mini = f"<span foreground='#fbbf24' weight='bold'>🏀{state['round']:02d}训</span>"
        self.set_markup("\n".join(lines), mini_summary=mini, tip_markup="\n".join(tip_lines))

    def render_note_selection(self, adv: dict, scan_badge: str = "", scan_status_line: str = ""):
        self._last_note_adv = adv
        c = adv["character"]
        cname = html.escape(c.get("short_name", ""))
        pos = html.escape(c.get("best_pos", ""))
        badge_html = f"  <span foreground='#4ade80'>{html.escape(scan_badge)}</span>" if scan_badge else ""
        self._update_header(
            f"<span weight='bold' foreground='#38bdf8'>📒 选人配装</span>  "
            f"<span foreground='#f8fafc' weight='bold'>{cname}</span>"
            f"<span foreground='#94a3b8'>({pos})</span>"
            f"{badge_html}"
        )
        markup = self.format_note_markup(adv, scan_status_line=scan_status_line)
        equip_items = adv.get("equip_icon_items") or []
        if equip_items:
            eq_txt = "  ".join(
                f"{'借' if it.get('is_borrow') else ''}{it.get('name', '')[:4]}"
                for it in equip_items[:5]
            )
            markup = f"{markup}\n<span foreground='#cbd5e1'>🎒 {html.escape(eq_txt)}</span>"
        top_b = adv["borrow_combos"][0] if adv.get("borrow_combos") else None
        top_o = adv["pure_own_combos"][0] if adv.get("pure_own_combos") else None
        top_g = adv["generic_combos"][0] if adv.get("generic_combos") else None
        if top_b:
            mini = (
                f"<span foreground='#38bdf8' weight='bold'>📒{cname}</span>  "
                f"<span foreground='#4ade80' weight='bold'>👑借:{html.escape(top_b['note1']['actor_name'])}+{html.escape(top_b['note2']['actor_name'])}({top_b['total_compat']}分·{html.escape(top_b['rank_name'])})</span>"
            )
        elif top_o:
            mini = (
                f"<span foreground='#38bdf8' weight='bold'>📒{cname}</span>  "
                f"<span foreground='#facc15' weight='bold'>💎自:{html.escape(top_o['note1']['actor_name'])}+{html.escape(top_o['note2']['actor_name'])}({top_o['total_compat']}分·{html.escape(top_o['rank_name'])})</span>"
            )
        elif top_g:
            mini = (
                f"<span foreground='#38bdf8' weight='bold'>📒{cname}</span>  "
                f"<span foreground='#4ade80' weight='bold'>🌐通:{html.escape(top_g['note1']['actor_name'])}+{html.escape(top_g['note2']['actor_name'])}({top_g['total_compat']}分·{html.escape(top_g['rank_name'])})</span>"
            )
        else:
            mini = f"<span foreground='#38bdf8' weight='bold'>📒{cname} 配装推荐</span>"
        self.set_markup(
            markup,
            mini_summary=mini,
            equip_icons=equip_items,
            tip_markup=adv.get("hud_tip_markup", ""),
        )
