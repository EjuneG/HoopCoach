#!/usr/bin/env python3
"""
LQSNCoach — 实时育成军师悬浮 HUD & 自动代打助手 (Live Training Advisor & Auto-Player)
用法：
  1. 实时军师模式（默认弹出屏幕右上角置顶悬浮窗 HUD + 终端同步输出）：
     python3 src/live_coach.py
  2. 校准 1~6 号训练键坐标（首次开启自动代打前跑一次，耗时 10 秒）：
     python3 src/live_coach.py --calibrate
  3. 自动代打模式（悬浮 HUD + 星奈算出最优训练后自动帮你点训练键）：
     python3 src/live_coach.py --auto
  4. 复盘最近一局日志中的训练选择：
     python3 src/live_coach.py --replay-latest
"""

import argparse
import atexit
import faulthandler
import glob
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from analyzer import parse_train_cmd_block
from note_advisor import (
    load_static_db as load_note_static_db,
    sync_note_and_actor_pool,
    recommend_notes_for_character,
    format_character_note_advice_terminal,
    format_character_note_advice_hud_markup,
    capture_game_screenshot,
    screenshot_diff_score,
    scan_notes_from_screen,
    scan_notes_scrolling,
)
from equipment_advisor import check_lineup_and_equip_synergy
try:
    from skill_advisor import (
        evaluate_skill_choices,
        get_char_role_tags,
        has_unknown_skills,
        learn_unknown_skills_from_window_async,
    )
except ImportError:
    from src.skill_advisor import (
        evaluate_skill_choices,
        get_char_role_tags,
        has_unknown_skills,
        learn_unknown_skills_from_window_async,
    )
from platform_compat import (
    BASE_DIR,
    IS_WINDOWS,
    WindowsFloatingHUD,
    resolve_game_dirs,
    win32_click_at,
    win32_get_mouse_location,
)
from hud_badges import (
    ACCENT_PALETTES as SHARED_ACCENT_PALETTES,
    DEFAULT_HUD_ANCHORS,
    HUD_CHAMPION_GOLD_RGB,
    HUD_INK_STROKE_RGBA,
    HUD_PAPER_RGB,
    HUD_SHADOW_RGBA,
    HUD_SKY_BLUE_RGB,
    HUD_TEXT_INK,
    build_badges_for_mode as shared_build_badges_for_mode,
    compute_badge_position,
    compute_game_content_box,
    extract_active_char_id,
    resolve_badge_markups,
    resolve_q_avatar_path,
    split_legacy_markup,
)

_, LOG_DIR = resolve_game_dirs(interactive_on_missing=True)
STRATEGY_DIR = os.path.join(BASE_DIR, "strategies")
CONFIG_DIR = os.path.join(BASE_DIR, "config")
COORDS_FILE = os.path.join(CONFIG_DIR, "button_coords.json")
HUD_CONFIG_FILE = os.path.join(CONFIG_DIR, "hud_config.json")
EVENTS_FILE = os.path.join(BASE_DIR, "data", "event_choices.json")
CHAR_DB_FILE = os.path.join(BASE_DIR, "data", "hoopgirls", "characters.json")
import tempfile
HUD_PID_FILE = os.path.join(tempfile.gettempdir(), "lqsn_hud.pid")
HUD_FALLBACK_LOG = os.path.join(tempfile.gettempdir(), "lqsn_hud.log")


class _SafeOutputStream:
    """防止后台守护化或关闭 Kitty 终端后 stdout/stderr 触发 EIO/BrokenPipeError 导致进程退出。"""

    def __init__(self, primary, fallback_path: str = HUD_FALLBACK_LOG):
        self._primary = primary
        self._fallback_path = fallback_path
        self._fallback_fp = None

    def _open_fallback(self):
        if self._fallback_fp is None:
            try:
                self._fallback_fp = open(self._fallback_path, "a", encoding="utf-8", buffering=1)
            except Exception:
                self._fallback_fp = False
        return self._fallback_fp if self._fallback_fp else None

    def write(self, s):
        if self._primary is not None:
            try:
                return self._primary.write(s)
            except (BrokenPipeError, OSError):
                self._primary = None
        fb = self._open_fallback()
        if fb is not None:
            try:
                return fb.write(s)
            except Exception:
                pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        if self._primary is not None:
            try:
                self._primary.flush()
                return
            except (BrokenPipeError, OSError):
                self._primary = None
        fb = self._open_fallback()
        if fb is not None:
            try:
                fb.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._primary, name)


def _load_character_db() -> dict:
    if os.path.exists(CHAR_DB_FILE):
        try:
            with open(CHAR_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {int(c["id"]): c for c in data if "id" in c}
        except Exception:
            pass
    return {}


CHARACTER_DB = _load_character_db()

STAT_NAMES = {
    101: "投射",
    102: "弹跳",
    103: "防守",
    104: "体能",
    105: "运控",
    106: "意识",
}

TRAIN_NAMES = {
    0: "💤休息",
    1: "1号·投射",
    2: "2号·弹跳",
    3: "3号·防守",
    4: "4号·体能",
    5: "5号·运控",
    6: "6号·意识",
}

# 精选高频球星卡/剧情事件精确选项说明（结合历史日志 effect_id 与实机文本）
KNOWN_EVENT_NOTES = {
    "100050": {
        "name": "免费会员",
        "options": {
            "1": "左【课余进行力量训练】👉 ⚡精力 +10",
            "2": "右【学习力量训练知识】👉 💚获得体能(104) +15~20",
        },
    },
    "400105": {
        "name": "理事长的神秘理疗",
        "options": {
            "1": "选项1 👉 ⚡精力 +10（100%稳健回体）",
            "2": "选项2 👉 🎲约50%随机1项属性+30 / 50%失败⚡精力-10",
            "3": "选项3 👉 🎲约50%随机1项属性+31 / 50%失败⚡精力-10",
        },
    },
    "5200093": {
        "name": "记者来访",
        "options": {
            "1": "选项1(左) 👉 常规属性/羁绊",
            "2": "选项2(右) 👉 投射/弹跳/体能属性加成（历史偏好 26:19）",
        },
    },
}

# 商店道具全量字典（基于全量历史日志 cs_buy_edu_item 实测效果、state_id 持续天数与游戏内真实名称归类）
STORE_ITEM_CATALOG = {
    # 0金白嫖类
    1151: {"name": "免费补给I", "cat": "free", "ps": 0, "tier": "T0", "desc": "🎁0金白嫖道具(必买)"},
    1152: {"name": "免费补给II", "cat": "free", "ps": 0, "tier": "T0", "desc": "🎁0金白嫖道具(必买)"},
    # 体力恢复药水系列（⚠️ 实测铁律：1161/1162/1163 特价体力饮为 50% 概率赌狗药，成功回 +30/+60/+90 体，失败 +0 体！1001~1003/1201~1202 为 100% 必回体）
    1161: {"name": "特价体力饮(小)", "cat": "stamina", "ps": 15, "max_ps": 30, "is_gamble": True, "tier": "T2", "desc": "🎲50%概率+30体/50%+0(期望+15体，20~25金赌狗饮)"},
    1162: {"name": "特价体力饮(中)", "cat": "stamina", "ps": 30, "max_ps": 60, "is_gamble": True, "tier": "T1", "desc": "🎲50%概率+60体/50%+0(期望+30体，35~40金赌狗饮)"},
    1163: {"name": "特价体力饮(大)", "cat": "stamina", "ps": 45, "max_ps": 90, "is_gamble": True, "tier": "T1", "desc": "🎲50%概率+90体/50%+0(期望+45体，45~50金赌狗饮)"},
    1201: {"name": "秘制营养餐I", "cat": "stamina", "ps": 80, "tier": "T0", "desc": "⚡精力+80+属性(55~60金超值大药)"},
    1202: {"name": "秘制营养餐II", "cat": "stamina", "ps": 100, "tier": "T0", "desc": "⚡精力+100+属性(100金满体大药)"},
    1001: {"name": "基础能量饮", "cat": "stamina", "ps": 20, "tier": "T1", "desc": "⚡精力+20/瓶(25~30金)"},
    1002: {"name": "中级能量饮", "cat": "stamina", "ps": 45, "tier": "T1", "desc": "⚡精力+45(45~50金)"},
    1003: {"name": "高级能量饮", "cat": "stamina", "ps": 70, "tier": "T1", "desc": "⚡精力+70(65~70金)"},
    1141: {"name": "复合恢复剂I", "cat": "stamina", "ps": 60, "tier": "T1", "desc": "⚡精力+60+属性(95~100金)"},
    1142: {"name": "复合恢复剂II", "cat": "stamina", "ps": 100, "tier": "T1", "desc": "⚡精力+100+属性(125~130金)"},
    # 支援卡/器材熟练度提升系列（实测仅增加全体未满器材 trammel，不会重洗当前回合合训站位）
    1113: {"name": "全器材熟练+5", "cat": "bond", "trammel_gain": 5, "ps": 0, "tier": "T1", "desc": "🔧全器材熟练度+5(75~80金)"},
    1111: {"name": "全器材熟练+10", "cat": "bond", "trammel_gain": 10, "ps": 0, "tier": "T1", "desc": "🔧全器材熟练度+10(95~100金)"},
    1112: {"name": "全器材熟练+15", "cat": "bond", "trammel_gain": 15, "ps": 0, "tier": "T1", "desc": "🔧全器材熟练度+15(105~110金)"},
    # 持续回合增益 Buff 系列（注意：N天按自然日含周末/比赛日倒计时，周二/周三买吃满训练，周五/周末买会空转浪费！）
    21010: {
        "name": "同心护符",
        "cat": "buff",
        "subcat": "teammate_emoji",
        "state_id": 420,
        "days": 5,
        "ps": 0,
        "tier": "T0",
        "desc": "🧧5天内全队友训练表情+5%加成(100金，周二/三买吃满3~4训)",
    },
    20010: {
        "name": "器材指导教程",
        "cat": "buff",
        "subcat": "equip_boost_ps",
        "state_id": 337,
        "days": 3,
        "ps": 0,
        "tier": "T0",
        "desc": "💿3天内器材/彩圈训练加成+25%·耗体+10%(95~100金)",
    },
    20020: {
        "name": "器材指导教程+",
        "cat": "buff",
        "subcat": "equip_boost_ps",
        "state_id": 337,
        "days": 4,
        "ps": 0,
        "tier": "T0",
        "desc": "💿4天内器材/彩圈训练加成+25%·耗体+10%(115~120金)",
    },
    1181: {
        "name": "器材指导书",
        "cat": "buff",
        "subcat": "equip_boost_free",
        "state_id": 301,
        "days": 3,
        "ps": 0,
        "tier": "T1",
        "desc": "📘3天内器材训练面板+15%·不加耗体(100金)",
    },
    1182: {
        "name": "器材指导书+",
        "cat": "buff",
        "subcat": "equip_boost_free",
        "state_id": 302,
        "days": 4,
        "ps": 0,
        "tier": "T1",
        "desc": "📘4天内器材训练面板+15%·不加耗体(130金)",
    },
    1191: {
        "name": "省体护具I",
        "cat": "buff",
        "subcat": "ps_discount",
        "state_id": 303,
        "days": 3,
        "ps": 0,
        "tier": "T0",
        "desc": "🔥3天内训练耗体减免40%(50金，3训省~27体)",
    },
    1192: {
        "name": "省体护具II",
        "cat": "buff",
        "subcat": "ps_discount",
        "state_id": 304,
        "days": 4,
        "ps": 0,
        "tier": "T0",
        "desc": "🔥4天内训练耗体减免40%(60金，4训省~36体)",
    },
    1131: {
        "name": "神之庇佑",
        "cat": "buff",
        "subcat": "zero_fail",
        "state_id": 205,
        "days": 2,
        "ps": 0,
        "tier": "T1",
        "desc": "🔰2天内训练失败率锁0%(65~70金，仅低体强点大彩圈时买)",
    },
    1132: {
        "name": "神之庇佑+",
        "cat": "buff",
        "subcat": "zero_fail",
        "state_id": 205,
        "days": 3,
        "ps": 0,
        "tier": "T1",
        "desc": "🔰3天内训练失败率锁0%(95~100金，仅低体强点大彩圈时买)",
    },
    1133: {
        "name": "神之庇佑++",
        "cat": "buff",
        "subcat": "zero_fail",
        "state_id": 205,
        "days": 4,
        "ps": 0,
        "tier": "T1",
        "desc": "🔰4天内训练失败率锁0%(120金，仅低体强点大彩圈时买)",
    },
    1121: {"name": "幸运金币福袋", "cat": "gold", "ps": 0, "tier": "T2", "desc": "💰随机开出金币(55~60金)"},
    # 训练设施直升书系列
    13010: {"name": "1号投射设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 1, "desc": "📈1号投射训练等级+1"},
    13020: {"name": "1号投射设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 1, "desc": "📈1号投射训练等级+2"},
    14010: {"name": "2号弹跳设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 2, "desc": "📈2号弹跳训练等级+1"},
    14020: {"name": "2号弹跳设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 2, "desc": "📈2号弹跳训练等级+2"},
    15010: {"name": "3号防守设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 3, "desc": "📈3号防守训练等级+1"},
    15020: {"name": "3号防守设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 3, "desc": "📈3号防守训练等级+2"},
    16010: {"name": "4号体能设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 4, "desc": "📈4号体能训练等级+1"},
    16020: {"name": "4号体能设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 4, "desc": "📈4号体能训练等级+2"},
    17010: {"name": "5号运控设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 5, "desc": "📈5号运控训练等级+1"},
    17020: {"name": "5号运控设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 5, "desc": "📈5号运控训练等级+2"},
    18010: {"name": "6号意识设施书I", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 6, "desc": "📈6号意识训练等级+1"},
    18020: {"name": "6号意识设施书II", "cat": "facility", "ps": 0, "tier": "T1", "train_idx": 6, "desc": "📈6号意识训练等级+2"},
    # 六维属性直加秘籍与特调饮料系列（基础值均为 <1200 时的面板原值；单维 >=1200 时触发 is_property_discount 折半 //2，触及 property_max_tbl 硬截断归零）
    1021: {"name": "全能饮料", "cat": "stat", "mode": "all_6", "stat": 0, "per_stat": 3, "gain": 18, "tier": "T1", "desc": "🍹全属性+3(六维共+18, 70金)"},
    1022: {"name": "全能饮料+", "cat": "stat", "mode": "all_6", "stat": 0, "per_stat": 6, "gain": 36, "tier": "T1", "desc": "🍹全属性+6(六维共+36, 110金)"},
    1023: {"name": "全能饮料++", "cat": "stat", "mode": "all_6", "stat": 0, "per_stat": 10, "gain": 60, "tier": "T1", "desc": "🍹全属性+10(六维共+60, 150金)"},
    1031: {"name": "双维特调饮", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 10, "gain": 20, "tier": "T1", "desc": "🍹双属性各+10(共+20, 60金)"},
    1032: {"name": "双维特调饮+", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 20, "gain": 40, "tier": "T1", "desc": "🍹双属性各+20(共+40, 100金)"},
    1033: {"name": "双维特调饮++", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 30, "gain": 60, "tier": "T1", "desc": "🍹双属性各+30(共+60, 130金)"},
    1041: {"name": "随机属性饮", "cat": "stat", "mode": "rand_1", "stat": 0, "gain": 10, "tier": "T2", "desc": "✨随机1个属性+10(30金)"},
    1042: {"name": "随机属性饮+", "cat": "stat", "mode": "rand_1", "stat": 0, "gain": 20, "tier": "T2", "desc": "✨随机1个属性+20(50金)"},
    1043: {"name": "随机属性饮++", "cat": "stat", "mode": "rand_1", "stat": 0, "gain": 30, "tier": "T2", "desc": "✨随机1个属性+30(60金)"},
    12031: {"name": "特调专属饮料", "cat": "stat", "mode": "rec_1", "stat": 0, "gain": 20, "tier": "T1", "desc": "🍹随机1个推荐属性+20(60金)"},
    12032: {"name": "特调专属饮料+", "cat": "stat", "mode": "rec_1", "stat": 0, "gain": 40, "tier": "T1", "desc": "🍹随机1个推荐属性+40(100金)"},
    12033: {"name": "特调专属饮料++", "cat": "stat", "mode": "rec_1", "stat": 0, "gain": 60, "tier": "T1", "desc": "🍹随机1个推荐属性+60(130金)"},
    13031: {"name": "专属双维饮料", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 10, "gain": 20, "tier": "T1", "desc": "🍹2个推荐属性各+10(共+20, 60金)"},
    13032: {"name": "专属双维饮料+", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 20, "gain": 40, "tier": "T1", "desc": "🍹2个推荐属性各+20(共+40, 100金)"},
    13033: {"name": "专属双维饮料++", "cat": "stat", "mode": "rec_2", "stat": 0, "per_stat": 25, "gain": 50, "tier": "T1", "desc": "🍹2个推荐属性各+25(共+50, 130金)"},
    1051: {"name": "投射秘籍", "cat": "stat", "mode": "single", "stat": 101, "gain": 20, "tier": "T2", "desc": "💪投射+20"},
    1052: {"name": "投射秘籍+", "cat": "stat", "mode": "single", "stat": 101, "gain": 40, "tier": "T2", "desc": "💪投射+40"},
    1053: {"name": "投射秘籍++", "cat": "stat", "mode": "single", "stat": 101, "gain": 60, "tier": "T2", "desc": "💪投射+60"},
    1061: {"name": "弹跳秘籍", "cat": "stat", "mode": "single", "stat": 102, "gain": 20, "tier": "T1", "desc": "💪弹跳+20"},
    1062: {"name": "弹跳秘籍+", "cat": "stat", "mode": "single", "stat": 102, "gain": 40, "tier": "T1", "desc": "💪弹跳+40"},
    1063: {"name": "弹跳秘籍++", "cat": "stat", "mode": "single", "stat": 102, "gain": 60, "tier": "T1", "desc": "💪弹跳+60"},
    1071: {"name": "防守秘籍", "cat": "stat", "mode": "single", "stat": 103, "gain": 20, "tier": "T1", "desc": "💪防守+20"},
    1072: {"name": "防守秘籍+", "cat": "stat", "mode": "single", "stat": 103, "gain": 40, "tier": "T1", "desc": "💪防守+40"},
    1073: {"name": "防守秘籍++", "cat": "stat", "mode": "single", "stat": 103, "gain": 60, "tier": "T1", "desc": "💪防守+60"},
    1081: {"name": "运控秘籍", "cat": "stat", "mode": "single", "stat": 105, "gain": 20, "tier": "T2", "desc": "💪运控+20"},
    1082: {"name": "运控秘籍+", "cat": "stat", "mode": "single", "stat": 105, "gain": 40, "tier": "T2", "desc": "💪运控+40"},
    1083: {"name": "运控秘籍++", "cat": "stat", "mode": "single", "stat": 105, "gain": 60, "tier": "T2", "desc": "💪运控+60"},
    1091: {"name": "体能秘籍", "cat": "stat", "mode": "single", "stat": 104, "gain": 20, "tier": "T1", "desc": "💪体能+20"},
    1092: {"name": "体能秘籍+", "cat": "stat", "mode": "single", "stat": 104, "gain": 40, "tier": "T1", "desc": "💪体能+40"},
    1093: {"name": "体能秘籍++", "cat": "stat", "mode": "single", "stat": 104, "gain": 60, "tier": "T1", "desc": "💪体能+60"},
    1101: {"name": "意识秘籍", "cat": "stat", "mode": "single", "stat": 106, "gain": 20, "tier": "T1", "desc": "💪意识+20"},
    1102: {"name": "意识秘籍+", "cat": "stat", "mode": "single", "stat": 106, "gain": 40, "tier": "T1", "desc": "💪意识+40"},
    1103: {"name": "意识秘籍++", "cat": "stat", "mode": "single", "stat": 106, "gain": 60, "tier": "T1", "desc": "💪意识+60"},
    22010: {"name": "投射小补给", "cat": "stat", "mode": "single", "stat": 101, "gain": 10, "tier": "T2", "desc": "💪投射+10"},
    22011: {"name": "投射中补给", "cat": "stat", "mode": "single", "stat": 101, "gain": 20, "tier": "T2", "desc": "💪投射+20"},
    22012: {"name": "投射大补给", "cat": "stat", "mode": "single", "stat": 101, "gain": 30, "tier": "T2", "desc": "💪投射+30"},
    22020: {"name": "弹跳小补给", "cat": "stat", "mode": "single", "stat": 102, "gain": 10, "tier": "T1", "desc": "💪弹跳+10"},
    22021: {"name": "弹跳中补给", "cat": "stat", "mode": "single", "stat": 102, "gain": 20, "tier": "T1", "desc": "💪弹跳+20"},
    22022: {"name": "弹跳大补给", "cat": "stat", "mode": "single", "stat": 102, "gain": 30, "tier": "T1", "desc": "💪弹跳+30"},
    22030: {"name": "防守小补给", "cat": "stat", "mode": "single", "stat": 103, "gain": 10, "tier": "T1", "desc": "💪防守+10"},
    22031: {"name": "防守中补给", "cat": "stat", "mode": "single", "stat": 103, "gain": 20, "tier": "T1", "desc": "💪防守+20"},
    22032: {"name": "防守大补给", "cat": "stat", "mode": "single", "stat": 103, "gain": 30, "tier": "T1", "desc": "💪防守+30"},
    22040: {"name": "运控小补给", "cat": "stat", "mode": "single", "stat": 105, "gain": 10, "tier": "T2", "desc": "💪运控+10"},
    22041: {"name": "运控中补给", "cat": "stat", "mode": "single", "stat": 105, "gain": 20, "tier": "T2", "desc": "💪运控+20"},
    22042: {"name": "运控大补给", "cat": "stat", "mode": "single", "stat": 105, "gain": 30, "tier": "T2", "desc": "💪运控+30"},
    22050: {"name": "体能小补给", "cat": "stat", "mode": "single", "stat": 104, "gain": 10, "tier": "T1", "desc": "💪体能+10"},
    22051: {"name": "体能中补给", "cat": "stat", "mode": "single", "stat": 104, "gain": 20, "tier": "T1", "desc": "💪体能+20"},
    22052: {"name": "体能大补给", "cat": "stat", "mode": "single", "stat": 104, "gain": 30, "tier": "T1", "desc": "💪体能+30"},
    22060: {"name": "意识小补给", "cat": "stat", "mode": "single", "stat": 106, "gain": 10, "tier": "T1", "desc": "💪意识+10"},
    22061: {"name": "意识中补给", "cat": "stat", "mode": "single", "stat": 106, "gain": 20, "tier": "T1", "desc": "💪意识+20"},
    22062: {"name": "意识大补给", "cat": "stat", "mode": "single", "stat": 106, "gain": 30, "tier": "T1", "desc": "💪意识+30"},
}

STAT_NAME_TO_ID = {v: k for k, v in STAT_NAMES.items()}


def get_char_rec_stats(state: dict) -> list[int]:
    """获取当前育成主角的 2 项推荐属性 ID (101..106)，用于计算『特调专属饮料(12031~12033)』与『专属双维饮料(13031~13033)』的真实命中属性。"""
    mcid = int(state.get("main_char_id", 0) or 0)
    rec_names = CHARACTER_DB.get(mcid, {}).get("recommend_base_attr", [])
    rec_ids = [STAT_NAME_TO_ID[nm] for nm in rec_names if nm in STAT_NAME_TO_ID]
    if len(rec_ids) >= 2:
        return rec_ids[:2]
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    ranked = sorted(range(101, 107), key=lambda k: (prop_max.get(k, 1600), props.get(k, 200)), reverse=True)
    for k in ranked:
        if k not in rec_ids:
            rec_ids.append(k)
        if len(rec_ids) == 2:
            break
    return rec_ids


def calc_real_stat_gain(cur_v: int, max_v: int, raw_gain: float, apply_1200_discount: bool = True) -> int:
    """
    计算游戏内真实的单次属性增加量：
    1. 属性上限硬截断 (property_max_tbl)：已达或超过 max_v 的部分直接归零（+0）；
    2. 1200 属性折半线 (is_property_discount)：所有 sub_type=20 的固定属性获取（商店道具/街机/扭蛋/事件），
       在当前属性 <1200 的区间享受 100% 收益，跨过或处于 >=1200 的区间收益向下取整折半 (// 2)。
    """
    if raw_gain <= 0 or cur_v >= max_v:
        return 0
    raw_i = int(round(raw_gain))
    if apply_1200_discount:
        below_1200 = max(0, min(raw_i, 1200 - cur_v))
        above_1200 = max(0, raw_i - below_1200)
        eff = below_1200 + (above_1200 // 2)
    else:
        eff = raw_i
    return min(eff, max(0, max_v - cur_v))


FIXED_STAT_BASE_W = {101: 1.00, 102: 1.00, 103: 1.00, 104: 1.00, 105: 1.00, 106: 1.00}


def get_ps_max(state: Optional[dict], extra: int = 0) -> int:
    """获取当前真实精力上限 ps_cap（默认 130；吃到 #107 破限蛋后可升至 140+），严禁硬编码 130。"""
    raw_cap = int((state or {}).get("ps_max", 130) or 130)
    return max(130, raw_cap) + int(extra)

DEFAULT_FOCUS_STAT = 101
DEFAULT_FOCUS_BONUS = 1.35
_ACTIVE_FOCUS_CFG = {
    "stat": None,
    "bonus": DEFAULT_FOCUS_BONUS,
    "last_stat": DEFAULT_FOCUS_STAT,
}
_EQUIP_PREF_STAT_CACHE: Optional[dict[int, int]] = None

FOCUS_STAT_ALIASES = {
    "101": 101, "投射": 101, "投": 101, "shoot": 101, "shooting": 101, "ts": 101,
    "102": 102, "弹跳": 102, "弹": 102, "jump": 102, "tt": 102,
    "103": 103, "防守": 103, "防": 103, "def": 103, "defense": 103, "fs": 103,
    "104": 104, "体能": 104, "体": 104, "sta": 104, "stamina": 104, "tn": 104,
    "105": 105, "运控": 105, "运": 105, "ctrl": 105, "dribble": 105, "yk": 105,
    "106": 106, "意识": 106, "意": 106, "iq": 106, "sense": 106, "ys": 106,
}


def _normalize_focus_dict(raw_focus) -> dict:
    """规范化专项偏科配置：{stat: int|None, bonus: float, last_stat: int}。"""
    stat_val = None
    bonus_val = DEFAULT_FOCUS_BONUS
    last_stat_val = DEFAULT_FOCUS_STAT
    if isinstance(raw_focus, dict):
        rs = raw_focus.get("stat")
        if rs is not None and str(rs).strip() != "":
            try:
                si = int(rs)
                if 101 <= si <= 106:
                    stat_val = si
            except Exception:
                pass
        rl = raw_focus.get("last_stat")
        if rl is not None:
            try:
                li = int(rl)
                if 101 <= li <= 106:
                    last_stat_val = li
            except Exception:
                pass
        if stat_val is not None:
            last_stat_val = stat_val
        rb = raw_focus.get("bonus")
        if rb is not None:
            try:
                bf = float(rb)
                if 1.05 <= bf <= 2.00:
                    bonus_val = round(bf, 2)
            except Exception:
                pass
    return {"stat": stat_val, "bonus": bonus_val, "last_stat": last_stat_val}


def _sync_active_focus_from_cfg(cfg: Optional[dict]) -> dict:
    norm = _normalize_focus_dict((cfg or {}).get("focus") if isinstance(cfg, dict) else None)
    _ACTIVE_FOCUS_CFG.update(norm)
    return dict(_ACTIVE_FOCUS_CFG)


def get_active_focus(strategy: Optional[dict] = None) -> tuple[Optional[int], float]:
    """获取当前生效的专项偏科配置 (focus_stat_id | None, bonus_mult)。支持 strategy['_focus_override'] 做纯总评双轨基准。"""
    if isinstance(strategy, dict) and "_focus_override" in strategy:
        ov = strategy["_focus_override"]
        if isinstance(ov, tuple) and len(ov) == 2:
            st, bn = ov
            if st is not None and 101 <= int(st) <= 106:
                return int(st), float(bn)
            return None, 1.0
        return None, 1.0
    st = _ACTIVE_FOCUS_CFG.get("stat")
    if st is not None and 101 <= int(st) <= 106:
        return int(st), float(_ACTIVE_FOCUS_CFG.get("bonus", DEFAULT_FOCUS_BONUS))
    return None, 1.0


def _get_equip_pref_stat(cfg_id: int) -> int:
    """懒加载器材 cfg_id -> 擅长训练属性 ID (101..106) 映射，供专项特化联动器材彩圈期权使用。"""
    global _EQUIP_PREF_STAT_CACHE
    if _EQUIP_PREF_STAT_CACHE is None:
        mp: dict[int, int] = {}
        guide_f = os.path.join(BASE_DIR, "data", "equipment_guide_db.json")
        try:
            if os.path.exists(guide_f):
                with open(guide_f, "r", encoding="utf-8") as f:
                    gd = json.load(f)
                for k_s, eq in (gd.get("equipments") or {}).items():
                    pref_nm = str(eq.get("preferred_training") or "").strip()
                    st_id = STAT_NAME_TO_ID.get(pref_nm, 0)
                    if st_id and str(k_s).isdigit():
                        mp[int(k_s)] = st_id
        except Exception:
            pass
        _EQUIP_PREF_STAT_CACHE = mp
    return _EQUIP_PREF_STAT_CACHE.get(int(cfg_id), 0)


def calc_stat_gain_ev(
    st_id: int,
    raw_gain: float,
    props: dict,
    prop_max: dict,
    strategy: dict = None,
    round_num: int = 20,
    apply_1200_discount: bool = True,
    rem_trains: int = 10,
) -> tuple[float, int]:
    """
    统一将原始固定属性加点 raw_gain（扭蛋/街机小游戏/商店属性道具/剧情固定加点）转化为 (有效属性当量 EV, 真实入账属性点 real_gain)：
    - 超出当前上限 max_v 的溢出部分直接截断为 0（0 EV）；
    - 若 apply_1200_discount=True，自动扣除 >=1200 折半损耗；
    - 扭蛋、街机与商店统一使用 FIXED_STAT_BASE_W 温和权重，不叠加平日训练的 1.35x 追赶膨胀系数（防止固定白板属性虚高碾压滚雪球扭蛋）；
    - 若开启专项偏科 (focus_stat == st_id)，对目标属性叠乘特化倍率并适度放宽衰减线（溢出仍严格归 0）；
    - 若后续仍有训练回合 (rem_trains > 0) 且逼近上限 (>=92%)，附加防溢出衰减系数；仅在中后期 (rem_trains <= 10) 真短板 (<45%上限) 时给予温和补齐倾斜。
    """
    cur_v = int(props.get(st_id, 200))
    max_v = int(prop_max.get(st_id, 1600))
    real_gain = calc_real_stat_gain(cur_v, max_v, raw_gain, apply_1200_discount=apply_1200_discount)
    if real_gain <= 0:
        return 0.0, 0
    focus_st, focus_bonus = get_active_focus(strategy)
    is_focus_stat = bool(focus_st and int(st_id) == focus_st)
    strat_stat_w = ((strategy or {}).get("weights") or {}).get("stat_base_value") or {}
    bw = float(strat_stat_w.get(str(int(st_id)), FIXED_STAT_BASE_W.get(int(st_id), 1.0)))
    if is_focus_stat:
        bw = bw * focus_bonus
    if rem_trains <= 0:
        return round(real_gain * bw, 1), real_gain

    ratio = (cur_v + real_gain * 0.5) / max(1, max_v)
    decay_thresh = 0.94 if is_focus_stat else 0.92
    if ratio >= decay_thresh:
        prox_mult = 0.72
    elif ratio < 0.45 and rem_trains <= 10:
        prox_mult = 1.08
    else:
        prox_mult = 1.0
    return round(real_gain * bw * prox_mult, 1), real_gain


SHOP_BUFF_STATE_META = {
    420: {"icon": "🧧", "name": "同心护符", "short": "🧧同心"},
    337: {"icon": "💿", "name": "器材教程", "short": "💿教程"},
    301: {"icon": "📘", "name": "指导书", "short": "📘指导"},
    302: {"icon": "📘", "name": "指导书+", "short": "📘指导+"},
    303: {"icon": "🔥", "name": "省体I", "short": "🔥省体"},
    304: {"icon": "🔥", "name": "省体II", "short": "🔥省体+"},
    205: {"icon": "🔰", "name": "神之庇佑", "short": "🔰0失败"},
}

# 6号建筑·扭蛋机 (UI_Panel_Education_Travel_Gashapon) 奖池评分与效果字典
GASHAPON_CFG_NOTES = {
    # 0~20金 新手/特惠福利扭蛋
    20003: {"score": 98, "tag": "🌟T0特惠神蛋(弹跳+20/体能+20+全训主属性+2)", "short": "🌟T0特惠神蛋"},
    20002: {"score": 95, "tag": "🌟T0特惠扭蛋(10金高性价比)", "short": "🌟10金特惠蛋"},
    20001: {"score": 95, "tag": "🎁0金福利扭蛋", "short": "🎁0金福利蛋"},
    20013: {"score": 95, "tag": "🎁0金福利扭蛋", "short": "🎁0金扭蛋"},
    20022: {"score": 95, "tag": "🎁0金福利扭蛋", "short": "🎁0金扭蛋"},
    # 120金 彩蛋系列（严格区分：82读笔记T0神蛋 vs 113~118六维定向+100彩蛋 vs 76随机换位置主控0属性）
    82:  {"score": 98, "tag": "📜直接计算一次笔记(120金T0真神彩蛋)", "short": "📜计算一次笔记"},
    76:  {"score": 25, "tag": "🔄球员位置(随机得1新位置+队友大涨·主控0属性)", "short": "🔄随机换位置(主控0属性)"},
    113: {"score": 88, "stat": 101, "gain": 100, "tag": "🌈投射彩蛋(投射+100+队友大涨)", "short": "🌈投射+100彩蛋"},
    114: {"score": 92, "stat": 102, "gain": 100, "tag": "🌈弹跳彩蛋(弹跳+100+队友大涨)", "short": "🌈弹跳+100彩蛋"},
    115: {"score": 90, "stat": 103, "gain": 100, "tag": "🌈防守彩蛋(防守+100+队友大涨)", "short": "🌈防守+100彩蛋"},
    116: {"score": 86, "stat": 104, "gain": 100, "tag": "🌈体能彩蛋(体能+100+队友大涨)", "short": "🌈体能+100彩蛋"},
    117: {"score": 86, "stat": 105, "gain": 100, "tag": "🌈运控彩蛋(运控+100+队友大涨)", "short": "🌈运控+100彩蛋"},
    118: {"score": 91, "stat": 106, "gain": 100, "tag": "🌈意识彩蛋(意识+100+队友大涨)", "short": "🌈意识+100彩蛋"},
    # 精力与持续回体系列（107当场满体+上限+10；22/23/24为持续4天每天结束回10/20/30体）
    107: {"score": 92, "tag": "⚡T0破限满体蛋(体力上限+10且当场回满)", "short": "⚡满体+上限10", "ps_gain": 95},
    24:  {"score": 85, "tag": "⚡4天每日回30体(state:308,共+120体)", "short": "⚡4天日回30体", "ps_gain": 30, "daily_ps": 30},
    23:  {"score": 82, "tag": "⚡4天每日回20体(state:307,共+80体)", "short": "⚡4天日回20体", "ps_gain": 20, "daily_ps": 20},
    22:  {"score": 76, "tag": "⚡4天每日回10体(state:306,共+40体)", "short": "⚡4天日回10体", "ps_gain": 10, "daily_ps": 10},
    # 周六零用钱补助系列（58/59/60 -> state:322/323/492，越早买返钱越多，末周买0收益）
    60:  {"score": 90, "sat_gold": 110, "tag": "💰周六补助++(后续每周六+110金)", "short": "💰每周六+110金"},
    59:  {"score": 82, "sat_gold": 80,  "tag": "💰周六补助+(后续每周六+80金)", "short": "💰每周六+80金"},
    58:  {"score": 70, "sat_gold": 40,  "tag": "💰周六补助(后续每周六+40金)", "short": "💰每周六+40金"},
    # 器材熟练度与永久训练乘区系列
    100: {"score": 88, "tag": "🔥熟练度暴涨+(2器材满100+全训主属性+2)", "short": "🔥熟练度暴涨+"},
    99:  {"score": 82, "tag": "🔥熟练度暴涨(器材熟练提升+全训主属性+2)", "short": "🔥熟练度暴涨"},
    30:  {"score": 90, "tag": "🔥器材暴击++(永久器材暴击+20%+队友大涨)", "short": "🔥器材暴击++"},
    29:  {"score": 80, "tag": "🔥器材暴击+(永久器材暴击+10%+队友中涨)", "short": "🔥器材暴击+"},
    28:  {"score": 68, "tag": "🔥器材暴击(永久器材暴击+5%+队友小涨)", "short": "🔥器材暴击"},
    33:  {"score": 88, "tag": "💪训练增强++(永久每训+3主属性+队友大涨)", "short": "💪训练增强++"},
    32:  {"score": 76, "tag": "💪训练增强+(永久每训+2主属性+队友中涨)", "short": "💪训练增强+"},
    31:  {"score": 58, "tag": "💪训练增强(永久每训+1主属性+队友小涨)", "short": "💪训练增强"},
    # 延迟满足与小游戏强化
    98:  {"score": 90, "tag": "⏳蓄势待发+(5天后全队全属性+20)", "short": "⏳5天全维+20"},
    97:  {"score": 84, "tag": "⏳蓄势待发(5天后全队全属性+13)", "short": "⏳5天全维+13"},
    95:  {"score": 71, "tag": "🎮小游戏强化(后续每次小游戏+20体能)", "short": "🎮小游戏+20体能"},
    96:  {"score": 78, "tag": "🎮小游戏强化+(后续每次小游戏+30体能)", "short": "🎮小游戏+30体能"},
    # 训练设施等级提升 + 对应属性直加系列（40~45蓝档Lv+1/+20；46~51紫档Lv+2/+30；52~57金档Lv+3/+40，日志11_06实测核验）
    52:  {"score": 84, "stat": 101, "gain": 40, "foc_idx": 1, "foc_up": 3, "tag": "📈投射升级包++(1号Lv+3&投射+40)", "short": "📈1号投射Lv+3&+40"},
    53:  {"score": 88, "stat": 102, "gain": 40, "foc_idx": 2, "foc_up": 3, "tag": "📈弹跳升级包++(2号Lv+3&弹跳+40)", "short": "📈2号弹跳Lv+3&+40"},
    54:  {"score": 86, "stat": 103, "gain": 40, "foc_idx": 3, "foc_up": 3, "tag": "📈防守升级包++(3号Lv+3&防守+40)", "short": "📈3号防守Lv+3&+40"},
    55:  {"score": 83, "stat": 104, "gain": 40, "foc_idx": 4, "foc_up": 3, "tag": "📈体能升级包++(4号Lv+3&体能+40)", "short": "📈4号体能Lv+3&+40"},
    56:  {"score": 82, "stat": 105, "gain": 40, "foc_idx": 5, "foc_up": 3, "tag": "📈运控升级包++(5号Lv+3&运控+40)", "short": "📈5号运控Lv+3&+40"},
    57:  {"score": 87, "stat": 106, "gain": 40, "foc_idx": 6, "foc_up": 3, "tag": "📈意识升级包++(6号Lv+3&意识+40)", "short": "📈6号意识Lv+3&+40"},
    46:  {"score": 74, "stat": 101, "gain": 30, "foc_idx": 1, "foc_up": 2, "tag": "📈投射升级包+(1号Lv+2&投射+30)", "short": "📈1号投射Lv+2&+30"},
    47:  {"score": 77, "stat": 102, "gain": 30, "foc_idx": 2, "foc_up": 2, "tag": "📈弹跳升级包+(2号Lv+2&弹跳+30)", "short": "📈2号弹跳Lv+2&+30"},
    48:  {"score": 76, "stat": 103, "gain": 30, "foc_idx": 3, "foc_up": 2, "tag": "📈防守升级包+(3号Lv+2&防守+30)", "short": "📈3号防守Lv+2&+30"},
    49:  {"score": 74, "stat": 104, "gain": 30, "foc_idx": 4, "foc_up": 2, "tag": "📈体能升级包+(4号Lv+2&体能+30)", "short": "📈4号体能Lv+2&+30"},
    50:  {"score": 73, "stat": 105, "gain": 30, "foc_idx": 5, "foc_up": 2, "tag": "📈运控升级包+(5号Lv+2&运控+30)", "short": "📈5号运控Lv+2&+30"},
    51:  {"score": 76, "stat": 106, "gain": 30, "foc_idx": 6, "foc_up": 2, "tag": "📈意识升级包+(6号Lv+2&意识+30)", "short": "📈6号意识Lv+2&+30"},
    40:  {"score": 65, "stat": 101, "gain": 20, "foc_idx": 1, "foc_up": 1, "tag": "📈投射升级包(1号Lv+1&投射+20)", "short": "📈1号投射Lv+1&+20"},
    41:  {"score": 68, "stat": 102, "gain": 20, "foc_idx": 2, "foc_up": 1, "tag": "📈弹跳升级包(2号Lv+1&弹跳+20)", "short": "📈2号弹跳Lv+1&+20"},
    42:  {"score": 67, "stat": 103, "gain": 20, "foc_idx": 3, "foc_up": 1, "tag": "📈防守升级包(3号Lv+1&防守+20)", "short": "📈3号防守Lv+1&+20"},
    43:  {"score": 65, "stat": 104, "gain": 20, "foc_idx": 4, "foc_up": 1, "tag": "📈体能升级包(4号Lv+1&体能+20)", "short": "📈4号体能Lv+1&+20"},
    44:  {"score": 64, "stat": 105, "gain": 20, "foc_idx": 5, "foc_up": 1, "tag": "📈运控升级包(5号Lv+1&运控+20)", "short": "📈5号运控Lv+1&+20"},
    45:  {"score": 67, "stat": 106, "gain": 20, "foc_idx": 6, "foc_up": 1, "tag": "📈意识升级包(6号Lv+1&意识+20)", "short": "📈6号意识Lv+1&+20"},
    # 六维单属性直加与高爆发锁训系列（场外满级实测：16~21金档+72；10~15紫档+48；4~9蓝档+30；106金档随机单维+80；104/105为+250但封锁后续训练获取）
    106: {"score": 82, "stat": 0,   "gain": 80,  "orig_price": 100, "tag": "✨随机单属性+80(100金)", "short": "✨随机单维+80"},
    105: {"score": 84, "stat": 103, "gain": 250, "lock_train_stat": 103, "orig_price": 100, "tag": "⚡防守暴涨(防守+250但主力不再能从训练获得防守+队友大涨)", "short": "⚡防守+250(锁训)"},
    104: {"score": 84, "stat": 106, "gain": 250, "lock_train_stat": 106, "orig_price": 100, "tag": "⚡意识暴涨(意识+250但主力不再能从训练获得意识+队友大涨)", "short": "⚡意识+250(锁训)"},
    16:  {"score": 80, "stat": 101, "gain": 72, "orig_price": 100, "tag": "✨投射大幅提升(投射+72)", "short": "✨投射+72"},
    17:  {"score": 84, "stat": 102, "gain": 72, "orig_price": 100, "tag": "✨弹跳大幅提升(弹跳+72)", "short": "✨弹跳+72"},
    18:  {"score": 82, "stat": 103, "gain": 72, "orig_price": 100, "tag": "✨防守大幅提升(防守+72)", "short": "✨防守+72"},
    19:  {"score": 81, "stat": 104, "gain": 72, "orig_price": 100, "tag": "✨体能大幅提升(体能+72)", "short": "✨体能+72"},
    20:  {"score": 79, "stat": 105, "gain": 72, "orig_price": 100, "tag": "✨运控大幅提升(运控+72)", "short": "✨运控+72"},
    21:  {"score": 83, "stat": 106, "gain": 72, "orig_price": 100, "tag": "✨意识大幅提升(意识+72)", "short": "✨意识+72"},
    10:  {"score": 72, "stat": 101, "gain": 48, "orig_price": 80,  "tag": "🟣投射中幅提升(投射+48)", "short": "🟣投射+48"},
    11:  {"score": 75, "stat": 102, "gain": 48, "orig_price": 80,  "tag": "🟣弹跳中幅提升(弹跳+48)", "short": "🟣弹跳+48"},
    12:  {"score": 74, "stat": 103, "gain": 48, "orig_price": 80,  "tag": "🟣防守中幅提升(防守+48)", "short": "🟣防守+48"},
    13:  {"score": 73, "stat": 104, "gain": 48, "orig_price": 80,  "tag": "🟣体能中幅提升(体能+48)", "short": "🟣体能+48"},
    14:  {"score": 72, "stat": 105, "gain": 48, "orig_price": 80,  "tag": "🟣运控中幅提升(运控+48)", "short": "🟣运控+48"},
    15:  {"score": 74, "stat": 106, "gain": 48, "orig_price": 80,  "tag": "🟣意识中幅提升(意识+48)", "short": "🟣意识+48"},
    4:   {"score": 62, "stat": 101, "gain": 30, "orig_price": 60,  "tag": "🔵投射增加(投射+30)", "short": "🔵投射+30"},
    5:   {"score": 65, "stat": 102, "gain": 30, "orig_price": 60,  "tag": "🔵弹跳增加(弹跳+30)", "short": "🔵弹跳+30"},
    6:   {"score": 64, "stat": 103, "gain": 30, "orig_price": 60,  "tag": "🔵防守增加(防守+30)", "short": "🔵防守+30"},
    7:   {"score": 63, "stat": 104, "gain": 30, "orig_price": 60,  "tag": "🔵体能增加(体能+30)", "short": "🔵体能+30"},
    8:   {"score": 62, "stat": 105, "gain": 30, "orig_price": 60,  "tag": "🔵运控增加(运控+30)", "short": "🔵运控+30"},
    9:   {"score": 64, "stat": 106, "gain": 30, "orig_price": 60,  "tag": "🔵意识增加(意识+30)", "short": "🔵意识+30"},
    1:   {"score": 64, "stat": 0,   "gain": 35, "tag": "🔵多维属性小幅提升", "short": "🔵多维+35"},
    2:   {"score": 70, "stat": 0,   "gain": 50, "tag": "🟣多维属性中幅提升", "short": "🟣多维+50"},
    3:   {"score": 78, "stat": 0,   "gain": 65, "tag": "✨多维属性+省体Buff", "short": "✨多维+65"},
    25:  {"score": 74, "trammel_all": 10, "tag": "🔥器材熟练度(全器材熟练度+10)", "short": "🔥全器材熟练+10"},
    26:  {"score": 76, "trammel_gain": 30, "trammel_rand_n": 2, "tag": "🔥器材熟练度+(随机2件器材熟练度+30)", "short": "🔥随机2器材熟练+30"},
    27:  {"score": 92, "trammel_all": 30, "tag": "🔥器材熟练度++(全器材熟练度+30)", "short": "🔥全器材熟练+30"},
    109: {"score": 63, "stat": 0,   "gain": 32, "tag": "🔵基础属性扭蛋(60金)", "short": "🔵属性+32"},
    110: {"score": 63, "stat": 0,   "gain": 32, "tag": "🔵基础属性扭蛋(60金)", "short": "🔵属性+32"},
    111: {"score": 63, "stat": 0,   "gain": 32, "tag": "🔵基础属性扭蛋(60金)", "short": "🔵属性+32"},
    112: {"score": 63, "stat": 0,   "gain": 32, "tag": "🔵基础属性扭蛋(60金)", "short": "🔵属性+32"},
    # 带负面代价的特训/惩罚类扭蛋（1119~1124扣大量体力升训练等级；10151永久打工收益-60）
    1119: {"score": 45, "foc_idx": 1, "foc_up": 1, "ps_gain": -45, "tag": "⚠️耗体特训(1号投射Lv+1但扣45~60体)", "short": "⚠️1号Lv+1(扣45体)"},
    1120: {"score": 45, "foc_idx": 2, "foc_up": 1, "ps_gain": -40, "tag": "⚠️耗体特训(2号弹跳Lv+1但扣35~45体)", "short": "⚠️2号Lv+1(扣40体)"},
    1121: {"score": 45, "foc_idx": 3, "foc_up": 1, "ps_gain": -40, "tag": "⚠️耗体特训(3号防守Lv+1但扣体)", "short": "⚠️3号Lv+1(扣40体)"},
    1122: {"score": 45, "foc_idx": 4, "foc_up": 1, "ps_gain": -40, "tag": "⚠️耗体特训(4号体能Lv+1但扣体)", "short": "⚠️4号Lv+1(扣40体)"},
    1123: {"score": 45, "foc_idx": 5, "foc_up": 1, "ps_gain": -40, "tag": "⚠️耗体特训(5号运控Lv+1但扣体)", "short": "⚠️5号Lv+1(扣40体)"},
    1124: {"score": 45, "foc_idx": 6, "foc_up": 1, "ps_gain": -40, "tag": "⚠️耗体特训(6号意识Lv+1但扣体)", "short": "⚠️6号Lv+1(扣40体)"},
    1125: {"score": 55, "stat": 0, "gain": 35, "tag": "🔵特训扭蛋(60金)", "short": "🔵特训+35"},
    1126: {"score": 66, "stat": 0, "gain": 50, "tag": "🟣特训扭蛋(80金)", "short": "🟣特训+50"},
    1127: {"score": 76, "stat": 0, "gain": 75, "tag": "✨双维特训扭蛋(100金)", "short": "✨双维+75"},
    10143: {"score": 45, "stat": 0, "gain": 50, "tag": "⚠️带负面状态属性扭蛋(100金)", "short": "⚠️属性+50(带负面)"},
    10144: {"score": 45, "stat": 0, "gain": 50, "tag": "⚠️带负面状态属性扭蛋(100金)", "short": "⚠️属性+50(带负面)"},
    10151: {"score": 40, "instant_gold": 300, "tag": "💰预支300金+永久打工金币-60(state:447)", "short": "💰得300金(打工-60)"},
}

# 3号建筑·电玩小游戏 (edu_mini_games_state_data) 全量配置字典（场外升级后自动结算必拿满额：简单60/普通70/困难100/特级全维各+25共150）
MINIGAME_CFG_CATALOG = {
    1:  {"stat": 101, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "投射·简单"},
    2:  {"stat": 101, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "投射·普通"},
    3:  {"stat": 101, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "投射·困难"},
    4:  {"stat": 0,   "tier": "特级", "range": "全维+25",   "base_gain": 150, "orig_price": 150, "short": "🌟全维·特级"},
    7:  {"stat": 102, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "弹跳·简单"},
    8:  {"stat": 102, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "弹跳·普通"},
    9:  {"stat": 102, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "弹跳·困难"},
    10: {"stat": 103, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "防守·简单"},
    11: {"stat": 103, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "防守·普通"},
    12: {"stat": 103, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "防守·困难"},
    19: {"stat": 106, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "意识·简单"},
    20: {"stat": 106, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "意识·普通"},
    21: {"stat": 106, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "意识·困难"},
    31: {"stat": 104, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "体能·简单"},
    32: {"stat": 104, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "体能·普通"},
    33: {"stat": 104, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "体能·困难"},
    34: {"stat": 105, "tier": "简单", "range": "60",        "base_gain": 60,  "orig_price": 60,  "short": "运控·简单"},
    35: {"stat": 105, "tier": "普通", "range": "70",        "base_gain": 70,  "orig_price": 80,  "short": "运控·普通"},
    36: {"stat": 105, "tier": "困难", "range": "100",       "base_gain": 100, "orig_price": 100, "short": "运控·困难"},
}


def load_strategy(name="training_flow"):
    path = os.path.join(STRATEGY_DIR, f"{name}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_event_db():
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_event_db(event_db: dict):
    try:
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(event_db, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


_EQUIP_POOL_CACHE = {"mtime": 0.0, "whistle_awake": 1}


def _get_whistle_awake_level() -> int:
    """读取玩家自有/助战『支援口哨(11004)』的觉醒星级（≥1星解锁器材事件上限突破总计 80+130=210 点）。"""
    pool_path = os.path.join(BASE_DIR, "data", "equip_pool.json")
    try:
        if os.path.exists(pool_path):
            mt = os.path.getmtime(pool_path)
            if mt != _EQUIP_POOL_CACHE["mtime"]:
                with open(pool_path, "r", encoding="utf-8") as f:
                    ep = json.load(f)
                best_aw = -1
                for sec in ("owned_cards", "borrowed_cards"):
                    c11004 = (ep.get(sec) or {}).get("11004")
                    if isinstance(c11004, dict):
                        best_aw = max(best_aw, int(c11004.get("awake_level", 0) or 0))
                if best_aw >= 0:
                    _EQUIP_POOL_CACHE["whistle_awake"] = best_aw
                _EQUIP_POOL_CACHE["mtime"] = mt
    except Exception:
        pass
    return _EQUIP_POOL_CACHE["whistle_awake"]


# 100% 由通用模板托管、无需按角色重复落盘至 data/event_choices.json 的事件类别
SHARED_TEMPLATE_NO_PERSIST_TYPES = frozenset({"camp_r10", "wish_r36", "skill_event"})


def classify_shared_story_event(ev_id: str, ev_name: str, state: dict = None) -> Optional[str]:
    """
    将剧情事件统一归类到「通用日程/事件模板」或「角色专属心愿链」：
    - 优先级 1：91xxxx / 910xxxx 严格识别为角色专属心愿链 ("char_wish")，绝不误判为集训/许愿；
    - 优先级 2：全角色完全一致的固定日程三选一（R10 集训 "camp_r10"、R36 许愿 "wish_r36"）；
    - 优先级 3：全角色统一结构的通用事件模板（角色/经理技能事件 "skill_event"、
                加练事件 "extra_train"、器材冒头事件 "equip_burst"、球星卡日常 "card_daily"）。
    """
    eid_str = str(ev_id or "").strip()
    eid_num = int(eid_str) if eid_str.isdigit() else 0
    ename = str(ev_name or "")

    # 1. 角色专属心愿剧情链（91xxxx / 910xxxx）：不同角色差异化核心，严禁与集训/许愿混淆
    if (910000 <= eid_num <= 919999) or (9100000 <= eid_num <= 9199999):
        return "char_wish"

    # 2. R10 集训三选一（全角色固定同一天出现，条件与奖励完全一致，仅剧情文案不同）
    if "集训" in ename or (81001 <= eid_num <= 81034) or eid_num in (5200182, 5200186):
        return "camp_r10"

    # 3. R36 许愿三选一（全角色固定同一天出现，奖励统一为 双推荐+20 / +40体 / +50金）
    if "许愿" in ename or (81035 <= eid_num <= 81065) or eid_num in (5200190, 5200192):
        return "wish_r36"

    # 4. 角色/经理技能事件（统一 1=婉拒+10体 / 2=答应得随机比赛技能）
    if "技能事件" in ename or (5300220 <= eid_num <= 5300299):
        return "skill_event"

    # 5. 加练事件（注意：老角色多为 1=婉拒+10体/2=陪练，20090+ 新角色部分反序，由对偶补全保序）
    if "加练事件" in ename or ("失败" not in ename and ((15000 <= eid_num <= 15099) or (20080 <= eid_num <= 20150))):
        return "extra_train"

    # 6. 器材冒头事件（1=获得属性 / 2=获得属性上限）
    if (31000000 <= eid_num <= 31999999) or ("一砖入魂" in ename) or ("起跳之星" in ename):
        return "equip_burst"

    # 7. 日常球星卡事件（100001..100200）
    if 100001 <= eid_num <= 100200:
        return "card_daily"

    return None


def _infer_event_branches(ev_id: str, ev_name: str, event_db: dict, state: dict = None) -> dict:
    """获取或按通用模板 / 事件 ID 段自动推导事件各选项的基础属性/精力/金币/上限收益结构。"""
    eid_num = int(ev_id) if str(ev_id).isdigit() else 0
    existing = json.loads(json.dumps(event_db.get(str(ev_id), {}).get("branches", {})))
    shared_kind = classify_shared_story_event(str(ev_id), ev_name, state=state)

    if shared_kind == "camp_r10":
        existing.setdefault("1", {"count": 0, "effect_id": 400011, "ps": -20, "coins": 0, "props": {}, "max_props": {}})
        existing["1"]["ps"] = -20
        existing["1"]["coins"] = 0
        existing["1"]["props"] = {}
        existing["1"]["max_props"] = {}
        existing["1"]["label"] = "和其他学校比赛的机会"
        existing["1"]["est_stat_ev"] = 32.0

        existing.setdefault("2", {"count": 0, "effect_id": None, "ps": 30, "coins": 0, "props": {}, "max_props": {}})
        existing["2"]["ps"] = 30
        existing["2"]["coins"] = 0
        existing["2"]["props"] = {}
        existing["2"]["max_props"] = {}
        existing["2"]["label"] = "全队出游的休息日(⚡+30体)"

        # 实证修正：26/26局日志证实 R36『许愿事件』为第6周周一 100% 必出固定事件（即使 R10 集训选2回+30体也必出许愿），选3不再虚增开启许愿的 chain_ev
        existing.setdefault("3", {"count": 0, "effect_id": None, "ps": 0, "coins": 30, "props": {}, "max_props": {}})
        existing["3"]["ps"] = 0
        existing["3"]["coins"] = 30
        existing["3"]["props"] = {}
        existing["3"]["max_props"] = {}
        existing["3"]["label"] = "满足队友的愿望(💰+30金)"
        existing["3"].pop("chain_ev", None)
    elif shared_kind == "wish_r36":
        rec_stats = get_char_rec_stats(state or {})
        rec_props = {str(st_id): 20 for st_id in rec_stats}
        rec_names_str = "/".join(STAT_NAMES.get(st_id, str(st_id)) for st_id in rec_stats)
        existing.setdefault("1", {"count": 0, "effect_id": None, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["1"]["ps"] = 0
        existing["1"]["coins"] = 0
        existing["1"]["props"] = rec_props
        existing["1"]["max_props"] = {}
        existing["1"].pop("est_stat_ev", None)
        existing["1"]["label"] = f"选项1(推荐属性{rec_names_str}各+20)"

        existing.setdefault("2", {"count": 0, "effect_id": None, "ps": 40, "coins": 0, "props": {}, "max_props": {}})
        existing["2"]["ps"] = 40
        existing["2"]["coins"] = 0
        existing["2"]["props"] = {}
        existing["2"]["max_props"] = {}
        existing["2"]["label"] = "选项2(大回体⚡+40)"

        existing.setdefault("3", {"count": 0, "effect_id": None, "ps": 0, "coins": 50, "props": {}, "max_props": {}})
        existing["3"]["ps"] = 0
        existing["3"]["coins"] = 50
        existing["3"]["props"] = {}
        existing["3"]["max_props"] = {}
        existing["3"]["label"] = "选项3(金币💰+50)"
    elif shared_kind == "skill_event":
        is_mgr_skill = (eid_num == 5300226) or ("经理" in str(ev_name or ""))
        existing.setdefault("1", {"count": 0, "effect_id": None, "ps": 10, "coins": 0, "props": {}, "max_props": {}})
        existing["1"].update({
            "ps": 10,
            "coins": 0,
            "props": {},
            "max_props": {},
            "label": "选项1(维持原计划·⚡精力+10)" if is_mgr_skill else "选项1(婉拒邀请·⚡精力+10)",
        })
        existing["1"].pop("est_stat_ev", None)

        existing.setdefault("2", {"count": 0, "effect_id": None, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["2"].update({
            "ps": 0,
            "coins": 0,
            "props": {},
            "max_props": {},
            "est_stat_ev": 17.5,
            "label": "选项2(临时改练法·获得随机比赛技能)" if is_mgr_skill else "选项2(热情答应·获得随机比赛技能)",
        })
    elif shared_kind == "extra_train":
        # 加练事件实测规律（18/18 角色全命中）：
        # - ID <= 20089（15001~15046 及 20082~20088）：选项 1=婉拒加练(⚡精力+10, eff=392)，选项 2=陪同加练(💪推荐属性+10, eff=439)；
        # - ID >= 20090（先斗/真崎/降旗/希雅等新角色）：选项 1/2 严格反序（1=陪同加练，2=婉拒加练）。
        # 若库里已观测到任意一侧分支，优先按实测分支对称补全；两侧均未收录时按 20090 分界线自动模板化，新角色 0 接管。
        def _is_decline(br: dict) -> bool:
            return int(br.get("effect_id") or 0) == 392 or int(br.get("ps") or 0) >= 10

        def _is_accept(br: dict) -> bool:
            return int(br.get("effect_id") or 0) == 439 or (
                int(br.get("ps") or 0) == 0 and (bool(br.get("props")) or float(br.get("est_stat_ev") or 0) > 0)
            )

        b1 = existing.get("1")
        b2 = existing.get("2")
        if not b1 and not b2 and eid_num > 0:
            reversed_order = (eid_num >= 20090)
            decline_key, accept_key = ("2", "1") if reversed_order else ("1", "2")
            existing[decline_key] = {
                "count": 0,
                "effect_id": 392,
                "ps": 10,
                "coins": 0,
                "props": {},
                "max_props": {},
                "label": f"选项{decline_key}(婉拒加练·⚡精力+10)",
            }
            existing[accept_key] = {
                "count": 0,
                "effect_id": 439,
                "ps": 0,
                "coins": 0,
                "props": {},
                "max_props": {},
                "est_stat_ev": 18.0,
                "label": f"选项{accept_key}(陪同加练·💪属性+5~10+队友成长)",
            }
            b1 = existing.get("1")
            b2 = existing.get("2")
        if b1 and not b2:
            if _is_decline(b1):
                existing["2"] = {
                    "count": 0,
                    "effect_id": 439,
                    "ps": 0,
                    "coins": 0,
                    "props": {},
                    "max_props": {},
                    "est_stat_ev": 18.0,
                    "label": "选项2(陪同加练·💪属性+5~10+队友成长)",
                }
            elif _is_accept(b1):
                existing["2"] = {
                    "count": 0,
                    "effect_id": 392,
                    "ps": 10,
                    "coins": 0,
                    "props": {},
                    "max_props": {},
                    "label": "选项2(婉拒加练·⚡精力+10)",
                }
        elif b2 and not b1:
            if _is_decline(b2):
                existing["1"] = {
                    "count": 0,
                    "effect_id": 439,
                    "ps": 0,
                    "coins": 0,
                    "props": {},
                    "max_props": {},
                    "est_stat_ev": 18.0,
                    "label": "选项1(陪同加练·💪属性+5~10+队友成长)",
                }
            elif _is_accept(b2):
                existing["1"] = {
                    "count": 0,
                    "effect_id": 392,
                    "ps": 10,
                    "coins": 0,
                    "props": {},
                    "max_props": {},
                    "label": "选项1(婉拒加练·⚡精力+10)",
                }
        for bk in ("1", "2"):
            br = existing.get(bk)
            if not br:
                continue
            if _is_decline(br):
                br["ps"] = max(10, int(br.get("ps") or 0))
                br.setdefault("label", f"选项{bk}(婉拒加练·⚡精力+10)")
                if not br.get("label"):
                    br["label"] = f"选项{bk}(婉拒加练·⚡精力+10)"
            elif _is_accept(br):
                if not br.get("props") and not br.get("max_props") and not br.get("est_stat_ev"):
                    br["est_stat_ev"] = 18.0
                if not br.get("label"):
                    br["label"] = f"选项{bk}(陪同加练·💪属性+5~10+队友成长)"
    elif shared_kind == "equip_burst":
        # 器材冒头二选一（如一砖入魂 101投射 / 起跳之星 102弹跳）：1=加属性(+40)，2=加上限(+40~70)
        stat_k = None
        for br in existing.values():
            for d_key in ("props", "max_props"):
                d_map = br.get(d_key) or {}
                if d_map:
                    stat_k = str(next(iter(d_map.keys())))
                    break
            if stat_k:
                break
        if not stat_k:
            if "一砖入魂" in str(ev_name or "") or str(ev_id).startswith("3100301") and eid_num <= 31003012:
                stat_k = "101"
            elif "起跳之星" in str(ev_name or "") or eid_num == 31003013:
                stat_k = "102"
        if stat_k and stat_k.isdigit():
            st_nm = STAT_NAMES.get(int(stat_k), stat_k)
            if "1" not in existing:
                existing["1"] = {
                    "count": 0,
                    "effect_id": 1,
                    "ps": 0,
                    "coins": 0,
                    "props": {stat_k: 40},
                    "max_props": {},
                    "label": f"选项1(获得{st_nm}属性+40)",
                }
            elif not existing["1"].get("label"):
                v1 = (existing["1"].get("props") or {}).get(stat_k, 40)
                existing["1"]["label"] = f"选项1(获得{st_nm}属性+{v1})"

            if "2" not in existing:
                existing["2"] = {
                    "count": 0,
                    "effect_id": 2,
                    "ps": 0,
                    "coins": 0,
                    "props": {},
                    "max_props": {stat_k: 40},
                    "label": f"选项2(获得{st_nm}上限+40)",
                }
            elif not existing["2"].get("label"):
                v2 = (existing["2"].get("max_props") or {}).get(stat_k, 40)
                existing["2"]["label"] = f"选项2(获得{st_nm}上限+{v2})"
    elif eid_num == 70 or "70熟练度" in ev_name:
        cap_70 = 80
        existing.setdefault("1", {"count": 0, "effect_id": 41004181, "ps": 40, "coins": 0, "props": {}, "max_props": {}})
        existing["1"]["ps"] = 40
        existing["1"]["coins"] = 0
        existing["1"]["props"] = {}
        existing["1"]["max_props"] = {}
        existing["1"]["label"] = "选项1(大回体⚡+40)"

        # 实证修正：选项2为六维随机1项属性上限+80，强制清空历史按字典累加的多维 max_props 脏数据
        existing.setdefault("2", {"count": 0, "effect_id": 41004182, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["2"]["ps"] = 0
        existing["2"]["coins"] = 0
        existing["2"]["props"] = {}
        existing["2"]["max_props"] = {}
        existing["2"]["random_max_prop"] = cap_70
        existing["2"]["label"] = f"选项2(随机1项上限📈+{cap_70})"

        existing.setdefault("3", {"count": 0, "effect_id": 41004183, "ps": 0, "coins": 40, "props": {}, "max_props": {}})
        existing["3"]["ps"] = 0
        existing["3"]["coins"] = 40
        existing["3"]["props"] = {}
        existing["3"]["max_props"] = {}
        existing["3"]["label"] = "选项3(金币💰+40)"
    elif eid_num == 71 or "100熟练度" in ev_name:
        # 支援口哨 1 星觉醒词条(effect 2206)将 70/100 熟上限事件升级至合计 210 点（80 + 130 = 210；0觉为 +100）
        cap_71 = 130 if _get_whistle_awake_level() >= 1 else 100
        existing.setdefault("1", {"count": 0, "effect_id": 41004184, "ps": 70, "coins": 0, "props": {}, "max_props": {}})
        existing["1"]["ps"] = 70
        existing["1"]["coins"] = 0
        existing["1"]["props"] = {}
        existing["1"]["max_props"] = {}
        existing["1"]["label"] = "选项1(超大幅回体⚡+70)"

        # 实证修正：选项2为六维随机1项属性上限+130/+100，严禁把多次随机命中的不同属性叠成五维齐加
        existing.setdefault("2", {"count": 0, "effect_id": 41004185, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["2"]["ps"] = 0
        existing["2"]["coins"] = 0
        existing["2"]["props"] = {}
        existing["2"]["max_props"] = {}
        existing["2"]["random_max_prop"] = cap_71
        existing["2"]["label"] = f"选项2(随机1项上限📈+{cap_71})"

        existing.setdefault("3", {"count": 0, "effect_id": 41004186, "ps": 0, "coins": 50, "props": {}, "max_props": {}})
        existing["3"]["ps"] = 0
        existing["3"]["coins"] = 50
        existing["3"]["props"] = {}
        existing["3"]["max_props"] = {}
        existing["3"]["label"] = "选项3(金币💰+50)"
    elif eid_num == 400105 or "理事长的神秘理疗" in ev_name:
        # 实证修正：选项1稳定+10体(eff=401)；选项2(eff=402/403)与选项3(eff=404/405)均为成功给随机1项属性~+30、失败扣10体的概率赌博
        existing.setdefault("1", {"count": 0, "effect_id": 401, "ps": 10, "coins": 0, "props": {}, "max_props": {}})
        existing["1"].update({"ps": 10, "coins": 0, "props": {}, "max_props": {}, "label": "选项1(稳健回体⚡+10)"})

        for b_k, eff_k, r_gain in (("2", 402, 30), ("3", 404, 31)):
            existing.setdefault(b_k, {"count": 0, "effect_id": eff_k, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
            existing[b_k].update({
                "ps": 0,
                "coins": 0,
                "props": {},
                "max_props": {},
                "random_prop": r_gain,
                "win_rate": 0.50,
                "fail_ps": -10,
                "label": f"选项{b_k}(🎲50%随机1项+{r_gain}/50%⚡-10)",
            })
            existing[b_k].pop("est_stat_ev", None)
    elif eid_num == 5200094 or "神秘的梦" in ev_name:
        # 实证修正：选项1稳定+10体(eff=417)；选项3大成功(eff=420, 3/5)真·全六维各+10(≥1200折半+5)，失败(eff=418, 2/5)精力-10且0属性
        existing.setdefault("1", {"count": 0, "effect_id": 417, "ps": 10, "coins": 0, "props": {}, "max_props": {}})
        existing["1"].update({"ps": 10, "coins": 0, "props": {}, "max_props": {}, "label": "选项1(稳回精力⚡+10)"})

        existing.setdefault("2", {"count": 0, "effect_id": 419, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["2"].update({
            "ps": 0,
            "coins": 0,
            "props": {},
            "max_props": {},
            "random_prop": 30,
            "win_rate": 0.50,
            "fail_ps": -10,
            "label": "选项2(🎲50%随机1项+30/50%⚡-10)",
        })
        existing["2"].pop("est_stat_ev", None)

        all6_ten = {str(k): 10 for k in (101, 102, 103, 104, 105, 106)}
        existing.setdefault("3", {"count": 0, "effect_id": 420, "ps": 0, "coins": 0, "props": all6_ten, "max_props": {}})
        existing["3"].update({
            "ps": 0,
            "coins": 0,
            "props": all6_ten,
            "max_props": {},
            "win_rate": 0.55,
            "fail_ps": -10,
            "label": "选项3(🎲55%全六维各+10/45%⚡-10)",
        })
        existing["3"].pop("est_stat_ev", None)
    elif eid_num == 5100005 or "海边的波子汽水" in ev_name:
        # 海滩合宿周三(R24)单节点自动随机奖励：服务器在 +20/+30/+60体 与 全六维各+10 之间随机发一种，并非同时获得
        existing.setdefault("1", {"count": 0, "effect_id": None, "ps": 35, "coins": 0, "props": {}, "max_props": {}})
        existing["1"].update({
            "ps": 35,
            "coins": 0,
            "props": {},
            "max_props": {},
            "label": "海滩自动随机奖励(⚡+20~60体 或 全六维各+10)",
        })
    elif shared_kind == "card_daily":
        existing.setdefault("1", {"count": 0, "effect_id": 7000, "ps": 10, "coins": 0, "props": {}, "max_props": {}})
        existing["1"]["ps"] = 10
        existing["1"].setdefault("label", "选项1(上/左·稳回精力)")

        existing.setdefault("2", {"count": 0, "effect_id": eid_num - 92922, "ps": 0, "coins": 0, "props": {}, "max_props": {}})
        existing["2"].setdefault("label", "选项2(下/右·加属性或上限)")
        if not existing["2"].get("props") and not existing["2"].get("max_props"):
            existing["2"]["est_stat_ev"] = 15.5

    return existing


def has_doumeki_in_lineup(state: dict) -> bool:
    """判定 5 人阵容中是否包含百目鬼 剑 (1013，解锁每周六『周末加训』weekend_train=true)。"""
    if state.get("weekend_train"):
        return True
    if state.get("main_char_id") == 1013:
        return True
    return 1013 in (state.get("teammate_char_ids") or [])


def get_stamina_unit_ev(state: dict, ps_override: int = None) -> float:
    """
    全局统一的 1 点不溢出精力（体力）边际有效属性当量 (EV)：
    - 每 25 点精力约等于 0.9~1.0 个完整训练回合门票（含主副属性、队友合训乘区、器材熟练度、经理吹哨能量与设施升级复利），
      或周末免去寝室休息从而腾出 1 动抽扭蛋/急招打工；
    - 第 3 周（海滩合宿备战周）与第 4 周（海滩合宿全 Lv5 满级训练周）享 1.15x 黄金周精力溢价；
    - 毕业前最后 1 训后精力价值归零（含百目鬼阵容延后至 R48 周六加训后归零）。
    """
    ps = int(ps_override if ps_override is not None else state.get("ps", 130))
    exp_r = int(state.get("exp_round", 0) or 0)
    round_num = int(state.get("round", 1) or 1)
    if has_doumeki_in_lineup(state) and exp_r > 0:
        if exp_r >= 48:
            return 0.15
        if exp_r >= 46:
            return 0.45 if ps <= 55 else 0.18
    elif exp_r >= 46 if exp_r > 0 else round_num >= 28:
        return 0.15

    week_no = (exp_r - 1) // 7 + 1 if exp_r > 0 else ((round_num - 1) // 4 + 1)
    beach_mult = 1.15 if (16 <= exp_r <= 26 or week_no in (3, 4)) else 1.0

    if ps <= 55:
        base_u = 1.65
    elif ps <= 85:
        base_u = 1.45
    elif ps <= 102:
        base_u = 1.15
    elif ps <= 115:
        base_u = 0.72
    else:
        base_u = 0.40
    return round(base_u * beach_mult, 2)


def evaluate_story_event(ev_id: str, ev_name: str, state: dict, strategy: dict, event_db: dict):
    """
    当触发 [EDU_MODEL]_LoadStory + [SCENARIO]进入选择节点 时，
    使用与训练/周末完全统一的【有效属性当量 EV】对每个事件选项动态算分并排序推荐。
    """
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    w = strategy["weights"]["stat_base_value"]
    focus_st, focus_bonus = get_active_focus(strategy)
    rem_trains = get_remaining_trains(state)
    eid_num = int(ev_id) if str(ev_id).isdigit() else 0

    # 找出当前最缺的短板属性与最核心的高权重未满属性
    sorted_by_low = sorted(props.items(), key=lambda x: x[1] / max(1, prop_max.get(x[0], 1600)))
    lowest_two_ids = {k for k, _ in sorted_by_low[:2]}
    lowest_two = [f"{STAT_NAMES[k]}({v})" for k, v in sorted_by_low[:2]]

    def _ev_stat_w(k_id: int) -> float:
        bw_k = float(w.get(str(k_id), 1.0))
        if focus_st and k_id == focus_st:
            return bw_k * focus_bonus
        return bw_k

    core_prio = sorted(
        [k for k in (102, 103, 104, 106, 101, 105) if props.get(k, 200) < prop_max.get(k, 1600) * (0.94 if focus_st == k else 0.92)],
        key=lambda k: (-_ev_stat_w(k), props.get(k, 200)),
    )
    prio_names = " > ".join(STAT_NAMES[k] for k in core_prio[:4])

    # 使用全局统一的精力边际价值标尺（低体高溢价、海滩周提权、高体防溢出）
    ps_unit_ev = get_stamina_unit_ev(state, ps_override=ps)

    # 龙宫寺心愿攒钱挑战 (task 113~116: 200/350/500/650金，固定于 R31 第5周周三验收) 激活且未达标时：
    # 卡片事件(+10精力)与日常回体享有额外续航溢价，避免周末被迫寝室休息而丢掉打工位；直接给金币也按高折现计价
    _gt_ev = state.get("active_gold_task") or {}
    _er_ev = int(state.get("exp_round", 1) or 1)
    _gold_ev = float(state.get("gold", 0) or 0)
    _gt_deadline = int(_gt_ev.get("deadline_round", 31) or 31)
    gold_task_active = bool(
        (
            _gt_ev.get("status") == 1
            and _er_ev <= _gt_deadline
            and _gold_ev < float(_gt_ev.get("target", 650))
        )
        or (int(state.get("main_char_id") or 0) == 1002 and _er_ev <= 3)
    )

    branches = _infer_event_branches(str(ev_id), ev_name, event_db, state=state)
    scored_options = []

    for b_idx in sorted(branches.keys(), key=lambda x: int(x) if str(x).isdigit() else 99):
        b_info = branches[b_idx]
        b_ps = int(b_info.get("ps", 0) or 0)
        b_coins = int(b_info.get("coins", 0) or 0)
        b_props = b_info.get("props", {}) or {}
        b_max = b_info.get("max_props", {}) or {}
        est_stat_ev = float(b_info.get("est_stat_ev", 0.0) or 0.0)
        chain_ev = float(b_info.get("chain_ev", 0.0) or 0.0)
        win_rate = float(b_info.get("win_rate", 1.0) or 1.0)
        fail_ps = int(b_info.get("fail_ps", 0) or 0)
        cnt = int(b_info.get("count", 0) or 0)
        label = b_info.get("label", f"选项 {b_idx}")

        ev_score = 0.0
        eff_parts = []

        # 1. 精力收益/消耗 EV（自动扣除超过 ps_max 上限的溢出浪费，并计入省下休息回合带来的训练器材/设施复利）
        if b_ps > 0:
            eff_ps = min(b_ps, max(0, ps_cap - ps))
            over_ps = b_ps - eff_ps
            # 大额回体（如+30/+40/+70）跨多个体力区间时，按起止水位均值平滑计价
            mid_ps = ps + eff_ps * 0.45
            eff_unit = get_stamina_unit_ev(state, ps_override=int(mid_ps))
            train_ticket_compound = (2.5 if (100001 <= eid_num <= 100200 and over_ps == 0 and rem_trains >= 6) else 0.0)
            # 龙宫寺攒钱心愿期：未溢出的回体享有续航溢价，优先选 +10 精力保住周末打工位
            gold_task_ps_bonus = (eff_ps * 0.95) if (gold_task_active and eff_ps > 0) else 0.0
            ev_score += eff_ps * eff_unit + train_ticket_compound + gold_task_ps_bonus
            if over_ps > 0:
                eff_parts.append(f"⚡精力+{b_ps}(实回+{eff_ps},溢出{over_ps}!)")
            else:
                eff_parts.append(f"⚡精力+{b_ps}(➔{ps + eff_ps})")
        elif b_ps < 0:
            cost_unit = 1.65 if (ps + b_ps < 50) else max(1.20, ps_unit_ev)
            ev_score += b_ps * cost_unit
            eff_parts.append(f"⚡精力{b_ps}(➔{max(0, ps + b_ps)})")

        # 2. 精确六维属性收益 EV（计入属性权重、1200折半、当前属性上限硬截断、概率分支 win_rate）
        is_all6_uniform = (
            len(b_props) == 6
            and len({int(v) for v in b_props.values()}) == 1
            and next(iter(int(v) for v in b_props.values())) > 0
        )
        all6_real_total = 0
        for st_k, st_v in sorted(b_props.items(), key=lambda x: int(x[0])):
            kid = int(st_k)
            val = int(st_v)
            if val == 0:
                continue
            is_focus_k = bool(focus_st and kid == focus_st)
            base_w = _ev_stat_w(kid)
            cur_v = props.get(kid, 200)
            max_v = prop_max.get(kid, 1600)
            st_nm = STAT_NAMES.get(kid, str(kid))
            if val > 0:
                real_v = calc_real_stat_gain(cur_v, max_v, val, apply_1200_discount=(val >= 10))
                all6_real_total += real_v
                ratio = cur_v / max(1, max_v)
                if real_v <= 0:
                    mult = 0.0
                elif ratio >= (0.94 if is_focus_k else 0.90):
                    mult = 0.62
                elif kid in lowest_two_ids and ratio < 0.55 and not (100001 <= eid_num <= 100200) and not is_all6_uniform:
                    mult = 0.94  # 大额短板事件保留高价值
                else:
                    # 剧情白板固定加点不提供器材熟练度/吹哨/设施升级复利，且会提早撞上 1200 折半墙，按 0.72 折现
                    mult = 0.72 if rem_trains >= 6 else 0.85
                ev_score += real_v * base_w * mult * win_rate
                if not is_all6_uniform:
                    if real_v <= 0:
                        eff_parts.append(f"💪{st_nm}+{val}(⚠️已满+0)")
                    elif real_v < val:
                        eff_parts.append(f"💪{st_nm}+{val}(实+{real_v})")
                    else:
                        eff_parts.append(f"💪{st_nm}+{val}")
            else:
                ev_score += val * base_w * win_rate
                eff_parts.append(f"💪{st_nm}{val}")
        if is_all6_uniform:
            u_val = next(iter(int(v) for v in b_props.values()))
            if win_rate < 1.0:
                eff_parts.append(f"🎲{int(round(win_rate * 100))}%全六维各+{u_val}(实拿共+{all6_real_total})")
            else:
                eff_parts.append(f"🌟全六维各+{u_val}(实拿共+{all6_real_total})")

        # 2.4 六维随机1项固定属性收益 EV (random_prop，按 1/6 数学期望 × win_rate 折算)
        b_rand_prop = int(b_info.get("random_prop", 0) or 0)
        if b_rand_prop > 0 and not b_props:
            rand_p_evs = []
            rand_real_vals = []
            for kid in (101, 102, 103, 104, 105, 106):
                base_w = float(w.get(str(kid), 1.0))
                cur_v = props.get(kid, 200)
                max_v = prop_max.get(kid, 1600)
                real_v = calc_real_stat_gain(cur_v, max_v, b_rand_prop, apply_1200_discount=(b_rand_prop >= 10))
                rand_real_vals.append(real_v)
                ratio = cur_v / max(1, max_v)
                mult = 0.0 if real_v <= 0 else (0.62 if ratio >= 0.90 else (0.72 if rem_trains >= 6 else 0.85))
                rand_p_evs.append(real_v * base_w * mult)
            ev_score += (sum(rand_p_evs) / 6.0) * win_rate
            avg_real = sum(rand_real_vals) / 6.0
            real_note = f"(均实+{avg_real:.0f})" if avg_real < b_rand_prop - 1.5 else ""
            if win_rate < 1.0:
                eff_parts.append(f"🎲{int(round(win_rate * 100))}%随机1项+{b_rand_prop}{real_note}")
            else:
                eff_parts.append(f"🎲随机1项属性+{b_rand_prop}{real_note}")

        # 2.5 概率分支失败扣体期望与残血防爆惩罚 (fail_ps < 0, 如 400105 理疗2/3 与 5200094 神秘的梦2/3)
        if win_rate < 1.0 and fail_ps < 0:
            fail_prob = max(0.0, 1.0 - win_rate)
            cost_unit = 1.65 if (ps + fail_ps < 55) else max(1.25, ps_unit_ev)
            low_ps_risk = 8.0 if (ps + fail_ps < 58) else (3.5 if ps < 75 else 0.0)
            ev_score += fail_prob * (fail_ps * cost_unit) - low_ps_risk
            eff_parts.append(f"{int(round(fail_prob * 100))}%失败⚡{fail_ps}(➔{max(0, ps + fail_ps)})")

        def _calc_single_cap_ev(kid: int, val: int):
            base_w = float(w.get(str(kid), 1.0))
            cur_v = props.get(kid, 200)
            max_v = prop_max.get(kid, 1600)
            ratio = cur_v / max(1, max_v)
            headroom = max(0, max_v - cur_v)
            if ratio >= 0.88 or headroom <= 180:
                return val * base_w * 0.72, True
            elif ratio >= 0.72 or (max_v <= 1650 and eid_num in (70, 71)):
                return val * base_w * 0.42, True
            elif ratio >= 0.58 and max_v <= 1750:
                return val * base_w * 0.22, True
            return val * base_w * 0.08, False

        # 3. 六维属性上限突破 EV (property_max_tbl 确定单项 vs random_max_prop 六维随机1项期望)
        # 凹 9500 分神局视角：若主属性初始上限偏低(<=1650)或中后期已冲高，突破上限是冲 9500 分的关键天花板；反之若上限已高(>1850)且属性尚低则价值极小。
        b_rand_max = int(b_info.get("random_max_prop", 0) or 0)
        if b_rand_max > 0:
            rand_cap_evs = []
            tight_names = []
            for kid in (101, 102, 103, 104, 105, 106):
                c_ev, is_tight = _calc_single_cap_ev(kid, b_rand_max)
                rand_cap_evs.append(c_ev)
                if is_tight:
                    tight_names.append(STAT_NAMES.get(kid, str(kid)))
            ev_score += sum(rand_cap_evs) / 6.0
            if tight_names:
                eff_parts.append(f"🎲随机1项上限+{b_rand_max}(缺上限{len(tight_names)}/6:{'/'.join(tight_names)})")
            else:
                eff_parts.append(f"🎲随机1项上限+{b_rand_max}(当前上限均充裕)")
        else:
            for st_k, st_v in sorted(b_max.items(), key=lambda x: int(x[0])):
                kid = int(st_k)
                val = int(st_v)
                if val <= 0:
                    continue
                cap_ev, _ = _calc_single_cap_ev(kid, val)
                ev_score += cap_ev
                eff_parts.append(f"📈{STAT_NAMES.get(kid, str(kid))}上限+{val}")

        # 4. 尚未观测到具体单项但已知加属性/上限/技能的分支期望 EV
        if est_stat_ev > 0 and not b_props and not b_max and not b_rand_max and not b_rand_prop:
            ev_score += est_stat_ev
            if 100001 <= (int(ev_id) if str(ev_id).isdigit() else 0) <= 100200:
                eff_parts.append("💪单维属性+20(或上限+15)")
            elif "技能" in label or classify_shared_story_event(str(ev_id), ev_name, state=state) == "skill_event":
                eff_parts.append(f"🎯获得随机比赛技能(约+{est_stat_ev:.1f}EV)")
            elif b_ps < 0:
                eff_parts.append(f"💪比赛/属性收益(约+{est_stat_ev:.0f}EV)")
            else:
                eff_parts.append(f"💪属性/技能增益(约+{est_stat_ev:.0f}EV)")

        # 5. 金币收益 EV（常规按商店买药/抽扭蛋折算率 1金 ≈ 0.38 EV；龙宫寺攒钱心愿未达标期提至 1.00 EV/金）
        if b_coins != 0:
            coin_unit = 1.00 if (gold_task_active and b_coins > 0) else 0.38
            ev_score += b_coins * coin_unit
            sign = "+" if b_coins > 0 else ""
            eff_parts.append(f"💰金币{sign}{b_coins}")

        # 6. 连锁后续事件收益（如集训事件选3开启后续许愿事件）
        if chain_ev > 0:
            ev_score += chain_ev
            eff_parts.append("🌟开启后续许愿(+40体/+50金)")

        if not eff_parts:
            eff_parts.append(f"历史选过 {cnt} 次(技能/剧情分支)")

        if not label.startswith("选项"):
            title_str = f"选项{b_idx}【{label}】"
        else:
            title_str = label

        scored_options.append({
            "b_idx": str(b_idx),
            "title": title_str,
            "ev": round(ev_score, 1),
            "effects": "，".join(eff_parts),
            "count": cnt,
        })

    # 仅当至少有 2 个可比分支（或单分支有明确非零 EV）时输出 EV 排序榜单
    has_meaningful_ev = len(scored_options) >= 2 or (len(scored_options) == 1 and scored_options[0]["ev"] > 0)
    opt_lines = []

    if has_meaningful_ev:
        scored_options.sort(key=lambda x: x["ev"], reverse=True)
        best = scored_options[0]
        for rank_i, item in enumerate(scored_options):
            badge = "👑首选" if rank_i == 0 else (f"🥈备选" if rank_i == 1 else f"#{rank_i+1}备选")
            opt_lines.append(f"{badge} 👉 {item['title']} (EV: {item['ev']:.1f}) — {item['effects']}")

        if len(scored_options) >= 2:
            diff = best["ev"] - scored_options[1]["ev"]
            recommended_choice = (
                f"👑 推荐点【{best['title']}】(EV: {best['ev']:.1f}，领先次选 +{diff:.1f} EV)！\n"
                f"   └ 理由：{best['effects']}（当前精力 {ps}/130 | 短板：{'、'.join(lowest_two)}）"
            )
        else:
            recommended_choice = f"👑 推荐点【{best['title']}】(EV: {best['ev']:.1f}) — {best['effects']}"
    else:
        for item in scored_options:
            opt_lines.append(f"• {item['title']}：{item['effects']}")
        if ps >= 115:
            recommended_choice = (
                f"👑 【满精力防溢出】当前精力 {ps}/130 接近满值，禁选『+精力』，优先选【加属性/技能】\n"
                f"   └ 属性优先级：{prio_names}（当前短板：{'、'.join(lowest_two)}）"
            )
        elif ps <= 60:
            recommended_choice = (
                f"👑 【低精力续航】当前精力仅 {ps}/130，若有『⚡精力+15~30』优先回体；否则选：{prio_names}"
            )
        else:
            recommended_choice = (
                f"👑 【属性/技能优先】当前精力 {ps}/130 充足 👉 优先选：{prio_names}（短板：{'、'.join(lowest_two)}）"
            )

    return {
        "ev_id": ev_id,
        "ev_name": ev_name,
        "scored_options": scored_options if has_meaningful_ev else [],
        "opt_lines": opt_lines,
        "recommendation": recommended_choice,
        "prio_names": prio_names,
        "lowest_two": lowest_two,
    }


def get_whistle_step(state: dict) -> tuple[int, str]:
    """
    根据本局携带的器材卡 (11004 支援口哨) 的星级 (awake_level)、突破等级 (break_level) 与实时熟练度 (trammel)，
    返回【哨子与经理同框时】的充能档位标识 (1=无哨+1, 2=固定+2, 3=满熟4破50%概率+3/50%概率+2，期望+2.5) 及 HUD 状态标签。
    注意（equipments.json 词条配表 + 日志实证）：
    - 0破基础词条 (11004151, probability_add_manager_exp=[100])：与经理同训 100% 额外 +1 经验（即同框保底 +2/次，单经理无哨子同框仅 +1/次）
    - 满破词条 (11004154, probability_add_manager_exp={"2": 50})：满 100 熟练度后，同框 50% 概率额外 +2 经验（共 +3/次）、50% 概率仍为 +2/次（期望 +2.5/次）
    - state_id=465 为老虎机器材暴击主属性+3，并非哨子状态，切勿混淆。
    """
    sup_trammel = state.get("supactor_trammel", {})
    card_to_cfg = state.get("supactor_card_to_cfg", {})
    card_trammel = state.get("supactor_card_trammel", {})
    w_effects = state.get("whistle_effects", set())

    has_whistle = (11004 in sup_trammel) or (11004 in card_to_cfg.values()) or bool(w_effects)
    if not has_whistle:
        return 1, "无哨子(经理+1/次)"

    w_tram = int(sup_trammel.get(11004, 0))
    for cid, cfg_id in card_to_cfg.items():
        if cfg_id == 11004:
            w_tram = max(w_tram, int(card_trammel.get(cid, 0)))
    if state.get("fired_71", False):
        w_tram = 100
    elif state.get("fired_70", False):
        w_tram = max(w_tram, 70)

    w_star = int(state.get("whistle_awake_level", 3))
    w_break = int(state.get("whistle_break_level", 4))
    has_triple_effect = (11004154 in w_effects) or (w_break >= 4)

    if w_tram >= 100 and has_triple_effect:
        return 3, f"🎺{w_star}星满熟哨(同框+2~3/次)"
    if w_tram >= 100:
        return 2, f"🎺{w_star}星满熟哨(同框+2/次)"
    return 2, f"🎺{w_star}星哨·熟{w_tram}/100(同框+2/次)"


SHORT_STAT_NAMES = {101: "投", 102: "弹", 103: "防", 104: "体", 105: "运", 106: "意"}


def format_hud_props_bar(state: dict) -> str:
    """
    生成 HUD 紧凑染色六维仪表盘：
    - ≥上限: 红色⚠️满顶 | ≥92%上限: 橙色!近顶 | ≥1200折半区: 青色½ | 中后期短板: 黄色📉 | 主属性甜区: 翠绿粗体
    - 尾部同步展示当前六维总和 ∑ 与实时面板评价分 ⭐
    """
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    rec_set = set(get_char_rec_stats(state))
    r_num = int(state.get("round", 1) or 1)
    min_val = min((int(props.get(k, 200)) for k in range(101, 107)), default=200)
    parts = []
    total_sum = 0
    for k in range(101, 107):
        v = int(props.get(k, 200))
        cap = int(prop_max.get(k, 1600))
        total_sum += v
        sn = SHORT_STAT_NAMES.get(k, str(k))
        ratio = v / max(1, cap)
        if v >= cap:
            parts.append(f"<span foreground='#f87171' weight='bold'>{sn}{v}/{cap}⚠️</span>")
        elif ratio >= 0.92:
            parts.append(f"<span foreground='#fb923c'>{sn}<b>{v}</b>/{cap}!</span>")
        elif v >= 1200:
            parts.append(f"<span foreground='#38bdf8'>{sn}<b>{v}</b>/{cap}½</span>")
        elif r_num >= 12 and v == min_val and v < 850:
            parts.append(f"<span foreground='#facc15'>{sn}<b>{v}</b>/{cap}📉</span>")
        elif k in rec_set:
            parts.append(f"<span foreground='#a7f3d0'><b>{sn}{v}</b>/{cap}</span>")
        else:
            parts.append(f"<b>{sn}</b>{v}/{cap}")
    rank_v = int(state.get("rank", 0) or 0)
    tail = f" <span foreground='#fbbf24'>∑<b>{total_sum}</b></span>"
    if rank_v > 0:
        r_col = "#f43f5e" if rank_v >= 9500 else ("#34d399" if rank_v >= 8500 else "#fde047")
        tail += f" <span foreground='{r_col}'>⭐<b>{rank_v}</b></span>"
    return " ".join(parts) + tail


def infer_exp_round(state: dict) -> int:
    """
    获取或从当前已完成训练次数推断 1..49 的日程回合 (experience_round)：
    - 第1周(R01-07): 5训(R01-05, 周一无比赛) + 周末(R06-07)
    - 第2周(R08-14): R08周一比赛(+70金) + 4训(R09-12, 其中R10必出集训三选一) + 周末(R13-14)
    - 第3周(R15-21): R15周一比赛(+70金) + 4训(R16-19) + 周末(R20-21)
    - 第4周(R22-28 海边合宿): 5训(R22-26, 周一无比赛+0金, R24必出合宿回体活动) + 周末(R27-28)
    - 第5周(R29-35): R29周一比赛(+70金) + 4训(R30-33) + 周末(R34-35)
    - 第6周(R36-42): R36必出许愿三选一+周一比赛(+70金, 无训练) + 4训(R37-40) + 周末(R41-42)
    - 第7周(R43-49 决赛周): R43比赛(+70金) + R44第27训 + R45比赛(+70金) + R46第28训 + R47比赛(+70金) + R48最后周末 + R49毕业
    """
    exp_r = int(state.get("exp_round", 0) or 0)
    if 1 <= exp_r <= 49:
        return exp_r
    t_no = max(1, int(state.get("round", 1)))
    rt = int(state.get("round_type", 1))
    wk_day = int(state.get("weekend_day", 1) or 1)
    if rt == 2:
        # 根据已完成训练数 state['round']-1 映射所属周末
        done = max(0, t_no - 1)
        if done <= 6:
            return 5 + wk_day
        elif done <= 10:
            return 12 + wk_day
        elif done <= 14:
            return 19 + wk_day
        elif done <= 19:
            return 26 + wk_day
        elif done <= 23:
            return 33 + wk_day
        elif done <= 26:
            return 40 + wk_day
        else:
            return 48
    train_to_exp = {
        1: 1, 2: 2, 3: 3, 4: 4, 5: 5,
        6: 9, 7: 10, 8: 11, 9: 12,
        10: 16, 11: 17, 12: 18, 13: 19,
        14: 22, 15: 23, 16: 24, 17: 25, 18: 26,
        19: 30, 20: 31, 21: 32, 22: 33,
        23: 37, 24: 38, 25: 39, 26: 40,
        27: 44, 28: 46,
    }
    return train_to_exp.get(min(28, t_no), 46)


def get_remaining_trains(state: dict) -> int:
    """根据当前真实日程回合 experience_round (1..49) 精确返回本局剩余的期望训练总次数（百目鬼阵容计入周六加训窗口）。"""
    exp_r = infer_exp_round(state)
    if exp_r <= 5:
        base_rem = 28 - (exp_r - 1)
    elif exp_r <= 7:
        base_rem = 23
    elif exp_r <= 12:
        base_rem = 23 - max(0, exp_r - 9)
    elif exp_r <= 14:
        base_rem = 19
    elif exp_r <= 19:
        base_rem = 19 - max(0, exp_r - 16)
    elif exp_r <= 21:
        base_rem = 15
    elif exp_r <= 26:
        base_rem = 15 - max(0, exp_r - 22)
    elif exp_r <= 28:
        base_rem = 10
    elif exp_r <= 33:
        base_rem = 10 - max(0, exp_r - 30)
    elif exp_r <= 35:
        base_rem = 6
    elif exp_r <= 40:
        base_rem = 6 - max(0, exp_r - 37)
    elif exp_r <= 42:
        base_rem = 2
    elif exp_r <= 44:
        base_rem = 2
    elif exp_r <= 46:
        base_rem = 1
    else:
        base_rem = 0

    if has_doumeki_in_lineup(state):
        cur_is_sat = (exp_r in (6, 13, 20, 27, 34, 41, 48)) and (
            bool(state.get("weekend_train"))
            or (state.get("round_type") == 2 and state.get("weekend_day", 1) == 1)
        )
        future_sats = sum(1 for sr in (6, 13, 20, 27, 34, 41, 48) if sr > exp_r)
        sat_equiv = (1 if cur_is_sat else 0) + (
            1 if (exp_r >= 46 and future_sats >= 1) else int(round(future_sats * 0.75))
        )
        return base_rem + sat_equiv
    return base_rem


def get_upcoming_schedule_context(state: dict, extra_whistle_gain: int = 0) -> dict:
    """
    全周期固定事件与三选一回体窗口前瞻分析器：
    识别即将到来的固定回体事件（R10集训三选一+30体、R24合宿全员活动+25体、R36许愿三选一+40体）
    以及哨子(11004)熟练度跨过 70/100 后排队必出的 ID 70(+40体)/ID 71(+70体) 三选一事件。
    日志实证铁律：
    - 海滩合宿周(R22~R26)中，R22(周一开场)、R24(周三全员活动)、R25(周四落幕烟花)被固定剧情 100% 占满，绝不弹出 ID 70/71；
    - 海滩期间仅 R23(周二·第15训前) 与 R26(周五·第18训前) 可弹出 ID 70/71，且单日最多弹出 1 个（若70与100同时待弹，先弹ID70，ID71顺延至下一空闲窗口）。
    """
    exp_r = infer_exp_round(state)
    week_no = (exp_r - 1) // 7 + 1
    sup_tram = state.get("supactor_trammel", {})
    whistle_tram = min(100, int(sup_tram.get(11004, 0)) + extra_whistle_gain)
    fired_70 = bool(state.get("fired_70", False))
    fired_71 = bool(state.get("fired_71", False))

    # 判定当前是否有待弹出的 ID 70 / ID 71（注意：若 ID 70 尚未弹出，即便熟练度已满 100，下一个窗口也必定先弹 ID 70！）
    queued_id70 = (whistle_tram >= 70) and (not fired_70)
    queued_id71 = (whistle_tram >= 100) and (not fired_71)
    next_whistle_event = "id70" if queued_id70 else ("id71" if queued_id71 else None)

    # 决定周末结束时的买药目标水位 target_pot_ps、低体风险起算线 safe_ps_floor、下周训练数与前瞻提示
    # 注意：高配阵容在爆出多人合训+彩圈且设施 Lv3~Lv5 时，单训耗体高达 -26 ~ -45 体（常规4训周连爆3训即耗 80~95 体）；
    # 要保证下周第4训（或决赛周第2训）进场仍有 >=68 体(0%失败率)而无需在平日被迫休息，必须留足高爆发余量！
    target_pot_ps = 124
    safe_ps_floor = 88
    next_week_trains = 4
    lookahead_notes = []
    weekday_next_turn_heal = 0
    weekday_heal_note = ""

    # 平日训练前瞻：检查下一训之前是否有必出的回体事件（严格区分海滩固定剧情占位日 R22/R24/R25 与空闲可弹日 R23/R26）
    if state.get("round_type", 1) == 1:
        # 哨子事件仅在『下一训前无强制全屏剧情锁死』时才能在下一训前立刻弹出
        can_pop_whistle_next_turn = exp_r not in (9, 13, 19, 23, 24, 26, 33, 40, 44, 46)
        if exp_r == 9:
            weekday_next_turn_heal = 30
            weekday_heal_note = "📅下训前必出集训(+30体/+30金)"
        elif exp_r == 23:
            weekday_next_turn_heal = 25
            weekday_heal_note = "🏖️下训前合宿活动自动回+20~60体"
        elif exp_r == 33:
            weekday_next_turn_heal = 40
            weekday_heal_note = "🌠下周一R36必出许愿(+40体/+50金)"
        elif exp_r == 44:
            weekday_heal_note = (
                "🏆决赛周剩2次平训 + R48周六可加训"
                if has_doumeki_in_lineup(state)
                else "🏆决赛周仅剩最后2训"
            )
        elif exp_r >= 46:
            weekday_heal_note = (
                "🔥最后1次平训！(R48周六仍可加训·耗体-80%)"
                if has_doumeki_in_lineup(state)
                else "🔥最后1训！0%失败率即可打空精力"
            )

        if next_whistle_event == "id70":
            if can_pop_whistle_next_turn:
                weekday_next_turn_heal = max(weekday_next_turn_heal, 40)
                weekday_heal_note = "🎁哨子达70：下训前必弹ID70(+40体/上限+80)"
            elif exp_r in (23, 24):
                weekday_heal_note = (weekday_heal_note + " | ⏳ID70将于周五R26弹出").strip(" | ")
        elif next_whistle_event == "id71":
            if can_pop_whistle_next_turn:
                weekday_next_turn_heal = max(weekday_next_turn_heal, 70)
                weekday_heal_note = "🎁哨子满100：下训前必弹ID71(+70体/上限+100)"
            elif exp_r in (23, 24):
                weekday_heal_note = (weekday_heal_note + " | ⏳ID71被合宿剧情顺延至周五R26弹出").strip(" | ")

    # 周末规划前瞻 (按所属周次 week_no = 1..7)
    hud_lookahead = ""
    if week_no == 1:
        # 下周仅周二(R09) 1 训后即到周三(R10)必出集训(+30体)，但若R09~R12连爆4次耗体训练需耗 ~95 体，周日留 75~105 体最稳
        target_pot_ps = 106
        safe_ps_floor = 68
        lookahead_notes.append("📅 下周三(R10)第2训前必出集训(+30体/+30金)，周日建议保持 75~105 体以防连爆4训断体")
        hud_lookahead = "📅 下周三集训+30体 · 周日留 75~105 体即可"
    elif week_no == 2:
        # 第3周(R16~R19)共 4 训且无固定回体事件！连爆 3 训耗体约 78，需 115~125 体进周才不会在周五(T13)被迫休息
        target_pot_ps = 124
        safe_ps_floor = 88
        lookahead_notes.append("⚠️ 下周(第3周)4训无固定回体！连续彩圈耗体极快，周日务必补至 95~124 体防周五断体休息")
        hud_lookahead = "⚠️ 下周4训无固定回体 · 周日建议补至 ≥95 体"
    elif week_no == 3:
        next_week_trains = 5
        # 海滩合宿周(R22~R26)全员 Lv5 满级训练共 5 训，多人彩圈单训耗体高达 -33~-45，必须满体进海滩！
        target_pot_ps = 128
        safe_ps_floor = 98
        lookahead_notes.append("🏖️ 下周海滩5训全Lv5(单训耗体-30~-45)！周日务必靠买药/散心补至 105~130 满体进场")
        hud_lookahead = "🏖️ 下周海滩5训全Lv5 · 周日务必补满体力"
    elif week_no == 4:
        # 第5周(R30~R33)高设施等级 4 训且周中无固定回体，单训耗体 -28~-37，需 115~126 体进场防第4训断体
        target_pot_ps = 126
        safe_ps_floor = 90
        lookahead_notes.append("⚠️ 下周(第5周)高Lv 4训无固定回体(单训-28~-37体)，周日建议补至 100~126 体防周中休息")
        hud_lookahead = "⚠️ 下周高Lv 4训无回体 · 周日建议补至 ≥100 体"
    elif week_no == 5:
        # 下周一(R36)无训练必出许愿(+40体)，但第6周 4 训全是 Lv4~5 大耗体(-28~-35/训)，周日需留 75~95 体才能在许愿后满体(120+)打满4训！
        target_pot_ps = 105
        safe_ps_floor = 74
        lookahead_notes.append("🌠 下周一(R36)必出许愿(+40体)，但第6周高Lv连爆耗体超100，周日需留 75~95 体(许愿后满体)防断体")
        hud_lookahead = "🌠 下周一许愿+40体 · 周日留 75~95 体即可"
    elif week_no == 6:
        # 下周决赛 2 训(R44+R46)，首训扣 -25~-32 体后，需保证最后1训(R46)仍有 >=70 体(0%失败率)，故周日需 95~108 体！
        next_week_trains = 2
        target_pot_ps = 108
        safe_ps_floor = 82
        lookahead_notes.append("🏆 下周决赛 2 训，为保最后一训(R46)仍有 70+ 体(0%失败率)，周日需补至 90~108 体")
        hud_lookahead = "🏆 下周决赛仅2训 · 周日补至 90~108 体保0%失败"
    elif week_no >= 7 or exp_r >= 48:
        next_week_trains = 0
        target_pot_ps = 0
        safe_ps_floor = 0
        if has_doumeki_in_lineup(state):
            lookahead_notes.append("🎓 毕业前最后周末(仅周六1动·无周日)：可⚡周六加训或外出，余钱全梭哈商店属性，禁买药/打工！")
            hud_lookahead = "🎓 最后周末(仅1动) · 可⚡加训或外出，余钱梭哈属性"
        else:
            lookahead_notes.append("🎓 毕业前最后周末(仅周六1动·无周日)：禁买药/休息/打工，全金币当场梭哈属性！")
            hud_lookahead = "🎓 最后周末(仅1动) · 全金币梭哈属性，禁买药/打工"

    # 叠加排队中的 ID 70 (+40体) / ID 71 (+70体) 器材熟练度三选一前瞻（打足余量，确保吃完事件后接近 120~130 满体！）
    if next_week_trains > 0:
        if next_whistle_event == "id71":
            if week_no == 3:
                target_pot_ps = min(target_pot_ps, 108)
                safe_ps_floor = min(safe_ps_floor, 80)
                lookahead_notes.append("🎁 哨子满100：海滩周二(R23)必弹ID71(+70体)，周日保持 85~108 体即可在R23回满130体")
                hud_lookahead = "🎁 海滩周二必弹哨子+70体 · 周日留 85~108 体"
            else:
                target_pot_ps = min(target_pot_ps, 92)
                safe_ps_floor = min(safe_ps_floor, 60)
                lookahead_notes.append("🎁 哨子满100：下周初必弹ID71(+70体/上限+100)，周日留 60~90 体即可满体开局")
                hud_lookahead = "🎁 下周初必弹哨子+70体 · 周日留 60~90 体即可"
        elif next_whistle_event == "id70":
            if week_no == 3:
                target_pot_ps = min(target_pot_ps, 116)
                safe_ps_floor = min(safe_ps_floor, 88)
                lookahead_notes.append(f"🎁 哨子已{whistle_tram}(≥70)：海滩周二(R23)必弹ID70(+40体)，周日保持 90~115 体进海滩")
                hud_lookahead = "🎁 海滩周二必弹哨子+40体 · 周日留 90~115 体"
            else:
                target_pot_ps = min(target_pot_ps, 106)
                safe_ps_floor = min(safe_ps_floor, 74)
                lookahead_notes.append(f"🎁 哨子已{whistle_tram}(≥70)：下周初必弹ID70(+40体)，周日需留 75~100 体才能在+40后满体撑完4训")
                hud_lookahead = "🎁 下周初必弹哨子+40体 · 周日留 75~100 体即可"

    # 若阵容中含有望月透(1012，精力>=59额外+5%全表情Bonus)或克拉丽丝(1009，精力>=48~55额外+4%Bonus)，周末留体底线额外上浮 +6 体防下周断被动
    lineup_cids = set(state.get("teammate_char_ids", []))
    if state.get("main_char_id"):
        lineup_cids.add(state.get("main_char_id"))
    if (1012 in lineup_cids or 1009 in lineup_cids) and next_week_trains >= 2 and not queued_id71:
        ps_cap_ctx = get_ps_max(state)
        safe_ps_floor = min(115, safe_ps_floor + 6)
        target_pot_ps = min(ps_cap_ctx, target_pot_ps + 6)

    return {
        "exp_round": exp_r,
        "week_no": week_no,
        "whistle_tram": whistle_tram,
        "queued_id70": queued_id70,
        "queued_id71": queued_id71,
        "next_whistle_event": next_whistle_event,
        "target_pot_ps": target_pot_ps,
        "safe_ps_floor": safe_ps_floor,
        "next_week_trains": next_week_trains,
        "lookahead_notes": lookahead_notes,
        "hud_lookahead": hud_lookahead,
        "weekday_next_turn_heal": weekday_next_turn_heal,
        "weekday_heal_note": weekday_heal_note,
    }


def format_hud_turn_header(state: dict) -> str:
    """生成极简平日训练顶栏：只保留训练进度与真实星期/赛程阶段，剔除游戏画面已有的体力/金币/经理/哨子星级。"""
    t_no = max(1, int(state.get("round", 1)))
    exp_r = infer_exp_round(state)
    has_dm = has_doumeki_in_lineup(state)
    if has_dm:
        if exp_r >= 48:
            phase_str = "最终周末·加训"
            phase_col = "#fbbf24"
        elif exp_r >= 46:
            phase_str = "决赛周五(周六可加训)"
            phase_col = "#fbbf24"
        elif exp_r >= 43:
            phase_str = "决赛周·倒数2平训"
            phase_col = "#fbbf24"
        else:
            week_no = (exp_r - 1) // 7 + 1
            day_idx = min(4, max(0, (exp_r - 1) % 7))
            wk_lbl = "海滩" if week_no == 4 else f"第{week_no}周"
            day_lbl = ("周一", "周二", "周三", "周四", "周五")[day_idx]
            if day_idx == 4:
                phase_str = f"{wk_lbl}·{day_lbl}(明日可加训)"
                phase_col = "#fcd34d"
            else:
                phase_str = f"{wk_lbl}·{day_lbl}"
                phase_col = "#94a3b8"
        turn_badge = f"🏀 第{t_no:02d}训"
    else:
        if exp_r >= 46 or t_no >= 28:
            phase_str = "最终训"
            phase_col = "#fbbf24"
        elif exp_r >= 43 or t_no == 27:
            phase_str = "决赛周·倒数2训"
            phase_col = "#fbbf24"
        else:
            week_no = (exp_r - 1) // 7 + 1
            day_idx = min(4, max(0, (exp_r - 1) % 7))
            wk_lbl = "海滩" if week_no == 4 else f"第{week_no}周"
            day_lbl = ("周一", "周二", "周三", "周四", "周五")[day_idx]
            if day_idx == 4:
                phase_str = f"{wk_lbl}·{day_lbl}(末训)"
                phase_col = "#fcd34d"
            else:
                phase_str = f"{wk_lbl}·{day_lbl}"
                phase_col = "#94a3b8"
        turn_badge = f"🏀 第{t_no:02d}/28训"
    return (
        f"<span weight='bold' foreground='#fbbf24'>{turn_badge}</span>  "
        f"<span size='small' foreground='{phase_col}'>{phase_str}</span>"
    )


def get_char_short_name(cid: int) -> str:
    ch = CHARACTER_DB.get(cid)
    return ch.get("short_name", str(cid)) if ch else str(cid)


def format_lineup_summary(state: dict) -> str:
    main_id = state.get("main_char_id")
    tm_ids = [c for c in state.get("teammate_char_ids", []) if c != main_id]
    if not main_id and not tm_ids:
        return ""
    main_str = f"⭐{get_char_short_name(main_id)}" if main_id else ""
    tm_str = "/".join(get_char_short_name(c) for c in tm_ids[:4])
    if main_str and tm_str:
        return f"{main_str}+{tm_str}"
    return main_str or tm_str


def get_lineup_ps_threshold(state: dict) -> tuple[int, str]:
    """
    检查本局 5 人阵容中是否存在依赖精力水位触发的核心育成天赋（如望月透 1012 要求精力 >=59，克拉丽丝 1009 要求精力 >=48~55）：
    返回 (阈值体力, 提示标签)；若无则返回 (0, "")。
    """
    lineup_set = set(state.get("teammate_char_ids", []))
    if state.get("main_char_id"):
        lineup_set.add(state.get("main_char_id"))
    char_talents = state.get("char_talents", {})
    if 1012 in lineup_set:
        lv_1012 = int(char_talents.get(1012, {}).get(101201, 4))
        th = max(55, 65 - 2 * (lv_1012 - 1))  # Lv4 为 59 体，Lv5 为 57 体
        return th, f"望月{th}体线"
    if 1009 in lineup_set:
        lv_1009 = int(char_talents.get(1009, {}).get(100901, 4))
        th = max(45, 60 - 3 * (lv_1009 - 1))
        return th, f"克拉丽丝{th}体线"
    return 0, ""


def detect_lineup_archetype(state: dict, strategy: Optional[dict] = None) -> dict:
    """
    自动识别本局 5 人阵容（1主控 + 4陪练）所属的育成流派核（源自 2026-11-23 贴吧实测 × characters.json edu_stream 交叉验证）：
    - 商店流 (store)：含白神(1045)且商店流 ≥2 人，或商店流 ≥3 人 → 周末除 #82 笔记彩蛋与 0~20 金特惠蛋外重度倾斜打工清商店；
    - 扭蛋流 (gacha)：含野上(1037)+羽濑川(1029)双核，或扭蛋流 ≥3 人 → 强化周末高阶扭蛋期望与 120 金彩蛋留钱底仓；
    - 小游戏流 (minigame)：小游戏流 ≥3 人 → 强化 3 号街机小游戏估值；
    - 训练流 (training)：默认/经典训练核/尚未解析出阵容 → 全部系数保持中性 (1.0 / 0.0)，对既有训练流模型 0 副作用。
    """
    neutral = {
        "archetype": "training",
        "label": "训练流",
        "badge": "",
        "core_names": [],
        "stream_counts": {},
        "work_ev_bonus": 0.0,
        "gasha_ev_mult": 1.0,
        "minigame_ev_mult": 1.0,
        "shop_stat_weekend_buy": False,
        "keep_gold_mult": 1.0,
        "note": "",
    }
    main_id = state.get("main_char_id")
    tm_ids = [int(c) for c in (state.get("teammate_char_ids") or []) if c]
    lineup_set = set(tm_ids)
    if main_id:
        lineup_set.add(int(main_id))

    stream_counts: dict[str, int] = {}
    for cid in lineup_set:
        ch = CHARACTER_DB.get(cid)
        st_name = (ch.get("edu_stream") or "").strip() if ch else ""
        if st_name:
            stream_counts[st_name] = stream_counts.get(st_name, 0) + 1

    store_cnt = stream_counts.get("商店流", 0)
    gacha_cnt = stream_counts.get("扭蛋流", 0)
    mg_cnt = stream_counts.get("小游戏流", 0)
    has_shirakami = 1045 in lineup_set
    has_nogami = 1037 in lineup_set
    has_hasegawa = 1029 in lineup_set

    strat_name = str((strategy or {}).get("name") or "")
    archetype = "training"
    core_names: list[str] = []

    if (has_shirakami and store_cnt >= 2) or store_cnt >= 3:
        archetype = "store"
        core_names = [get_char_short_name(c) for c in (1045, 1037, 1011, 1023, 1006, 1019, 1040) if c in lineup_set][:3]
    elif (has_nogami and has_hasegawa) or gacha_cnt >= 3:
        archetype = "gacha"
        core_names = [get_char_short_name(c) for c in (1037, 1029, 1021, 1032, 1033, 1004) if c in lineup_set][:3]
    elif mg_cnt >= 3:
        archetype = "minigame"
        core_names = [get_char_short_name(c) for c in (1015, 1017, 1018, 1022, 1028, 1039, 1042) if c in lineup_set][:3]
    elif not lineup_set:
        if "商店流" in strat_name or "Store Flow" in strat_name:
            archetype = "store"
        elif "扭蛋" in strat_name or "Gacha" in strat_name:
            archetype = "gacha"

    if archetype == "store":
        core_tag = "+".join(core_names) if core_names else "商店核"
        return {
            "archetype": "store",
            "label": "商店流",
            "badge": f"🛒商店流({core_tag})",
            "core_names": core_names,
            "stream_counts": stream_counts,
            "work_ev_bonus": 14.0,
            "gasha_ev_mult": 0.85,
            "minigame_ev_mult": 0.94,
            "shop_stat_weekend_buy": True,
            "keep_gold_mult": 1.28,
            "note": f"🛒 商店流({core_tag})：周末除 #82 彩蛋笔记外优先打工攒金、尽量清空商店高优属性/道具",
        }
    if archetype == "gacha":
        core_tag = "+".join(core_names) if core_names else "扭蛋核"
        return {
            "archetype": "gacha",
            "label": "扭蛋流",
            "badge": f"🎰扭蛋流({core_tag})",
            "core_names": core_names,
            "stream_counts": stream_counts,
            "work_ev_bonus": 4.0,
            "gasha_ev_mult": 1.12,
            "minigame_ev_mult": 0.96,
            "shop_stat_weekend_buy": False,
            "keep_gold_mult": 1.15,
            "note": f"🎰 扭蛋流({core_tag})：提高周末高阶扭蛋权重，优先保足 120 金防踏空彩蛋",
        }
    if archetype == "minigame":
        core_tag = "+".join(core_names) if core_names else "街机核"
        return {
            "archetype": "minigame",
            "label": "小游戏流",
            "badge": f"🎮小游戏流({core_tag})",
            "core_names": core_names,
            "stream_counts": stream_counts,
            "work_ev_bonus": 2.0,
            "gasha_ev_mult": 0.96,
            "minigame_ev_mult": 1.15,
            "shop_stat_weekend_buy": False,
            "keep_gold_mult": 1.05,
            "note": f"🎮 小游戏流({core_tag})：提升 3 号电玩城小游戏周末优先级",
        }

    neutral["stream_counts"] = stream_counts
    return neutral


def calc_lineup_teammate_ev(
    state: dict,
    cmd: dict,
    stat_ev: float,
    idx: int,
    props: dict,
    prop_max: dict,
    growth_rates: dict,
    w: dict,
) -> tuple[float, str]:
    """
    根据本局所选 5 名角色（1名主控 initactor + 4名陪练队友 team_mate_map）及其实际天赋/心结等级，
    动态计算『队友训练』（在场同伴数 head_cnt = mate_cnt + is_mgr >= 1）期望收益。
    ⚠️ 核心机制（日志 sc_train_result_end 实测验证）：
    1) 经理在场 (is_mgr=True) 在训练结算中完全视作 1 名在场同伴（与主控组成 >=2 人合训，头顶自带表情气泡）！
       即使按钮上 0 名队友、仅站了经理 (mate_cnt=0, is_mgr=True)，也 100% 触发『队友训练』、希雅拉人（此时场下 4 名队友全空可拉）、
       佐久间主副属性固定加点与全员同表情共鸣 (Bingo)；当 1 名队友 + 经理同框时，直接视为 2 名同伴在场！
    2) 只要触发『队友训练』，5人阵容 (lineup_set) 中所有角色的育成天赋与心结均全局无差别生效
       （主控 100%，陪练队友按 as_teammate_ratio 70%~90% 折算），无需该角色本人站在此训练按钮上 (mate_ids)！
    - 希雅(1046) / 出羽(1035) 在阵容中：任意队友/经理训练均可触发额外拉入 1~3 名场下队友（扩编人数 + 触发多人表情共鸣）
    - 佐久间(1020) / 龙宫寺(1002) 在阵容中：任意队友/经理训练额外增加训练主属性 (+2~3，且 40%~55% 概率再+2) 与副属性底数（进乘区放大）
    - 望月(1012) / 克拉丽丝(1009) 在阵容中：全部表情基础 Bonus 提升 (+3%~+8%/人) 及望月★2基础值+2
    - 闪堂(1007) 在阵容中：全部表情一致触发概率提升 (+15%~+25%) 及哭脸/平常脸升级
    - 真崎(1030) / 阵野(1031) 在阵容中：全部表情一致时的额外独立乘区倍率 (+25%~+45%)
    - 澪奈(1001) 在阵容中：队友训练时 Bonus 最终值放大 (+20%~+30%)
    """
    mate_cnt = cmd["mate_count"]
    is_mgr = bool(cmd.get("is_manager", False))
    head_cnt = mate_cnt + (1 if is_mgr else 0)
    mate_ids = cmd.get("mate_ids", [])
    main_cid = state.get("main_char_id")
    tm_cids = [c for c in state.get("teammate_char_ids", []) if c != main_cid]
    char_talents = state.get("char_talents", {})

    lineup_set = set(tm_cids)
    if main_cid:
        lineup_set.add(main_cid)

    # 若尚未从日志解析出 5 人阵容，则回退到校准后的基础人数模型
    has_tongxin = 420 in state.get("active_states", set())
    if not lineup_set:
        mult_map = w.get("teammate_skill_mult_by_count", {"1": 1.15, "2": 1.75, "3": 2.45, "4": 2.85})
        skill_mult = float(mult_map.get(str(min(4, max(1, head_cnt))), 2.45)) + (0.05 * head_cnt if has_tongxin else 0.0)
        mate_skill_ev = stat_ev * skill_mult
        synergy = w.get("teammate_3plus_synergy_bonus", 0.0) if head_cnt >= 3 else 0.0
        mate_growth_ev = mate_cnt * float(w.get("teammate_head_bonus", 0.0)) + synergy
        est_extra_stat = int(round(cmd["total_stat_gain"] * skill_mult))
        tx_tag = "·🧧同心" if has_tongxin else ""
        lbl = "经理合训" if (mate_cnt == 0 and is_mgr) else f"队友×{head_cnt}"
        return mate_skill_ev + mate_growth_ev, f"👥{lbl}(+{est_extra_stat}{tx_tag})"

    ps = state.get("ps", 130)
    if mate_ids:
        on_btn_names = [get_char_short_name(mid) for mid in mate_ids]
        if is_mgr:
            on_btn_names.append("经理")
    elif mate_cnt > 0:
        on_btn_names = [f"队友×{mate_cnt}" + ("+经理" if is_mgr else "")]
    else:
        on_btn_names = ["经理合训"]
    tag_notes = []

    def _get_talent_lv(cid: int, tid: int, default: int = 5) -> int:
        return int(char_talents.get(cid, {}).get(tid, default))

    def _has_hk(cid: int, hk_idx: int) -> bool:
        tmap = char_talents.get(cid, {})
        # 5位 ID (如 10461, 10462) 为星级心结
        return tmap.get(cid * 10 + hk_idx, 0) > 0

    def _role_scale(cid: int) -> float:
        if cid == main_cid:
            return 1.0
        # 陪练位置严格按 characters.json 的 as_teammate_ratio [70, 75, 80, 85, 90] 折算，与是否在按钮上无关
        lv = _get_talent_lv(cid, cid * 100 + 1, 5)
        return {1: 0.70, 2: 0.75, 3: 0.80, 4: 0.85, 5: 0.90}.get(min(5, max(1, lv)), 0.85)

    # 1. 希雅(1046) / 出羽(1035) 在 5 人阵容中时，任意有队友或经理的训练均可触发技能额外拉场下队友进场
    # 注意：仅站经理 (mate_cnt=0, is_mgr=True) 时，场下 4 名队友全部空闲 (bench_avail=4)，拉人空间最大！
    bench_avail = max(0, 4 - mate_cnt)
    exp_pulled = 0.0
    prob_pull_any = 0.0
    prob_reach_3plus = 1.0 if head_cnt >= 3 else 0.0
    extra_pull_bonus_pct = 0.0

    if 1046 in lineup_set and bench_avail > 0:
        lv_1046 = _get_talent_lv(1046, 104601, 5)
        scale_1046 = _role_scale(1046)
        # Lv1~Lv7 总触发率 50%~70%（拆分为拉 1 人与拉 2+ 人）
        p1_table = {1: 0.30, 2: 0.33, 3: 0.35, 4: 0.40, 5: 0.43, 6: 0.45, 7: 0.50}
        p1 = p1_table.get(lv_1046, 0.43) * scale_1046
        p2 = 0.20 * scale_1046
        hk1_1046 = _has_hk(1046, 1)
        hk2_1046 = _has_hk(1046, 2)
        if head_cnt == 1 and hk1_1046:
            # ★1 心结：如果训练中只有 1 个同伴，最少会额外增加 1 个队友
            p1_eff = max(p1, 0.65 * scale_1046 / 0.85 if scale_1046 < 1.0 else 0.75)
            p2_eff = p2
        else:
            p1_eff, p2_eff = p1, p2

        prob_pull_any = min(1.0, p1_eff + p2_eff)
        max_pull_cap = 3 if hk2_1046 else 2
        raw_pulled = p1_eff * min(1, bench_avail) + p2_eff * min(max_pull_cap, bench_avail)
        exp_pulled += raw_pulled
        if head_cnt == 2:
            prob_reach_3plus = max(prob_reach_3plus, prob_pull_any)
        elif head_cnt == 1:
            prob_reach_3plus = max(prob_reach_3plus, p2_eff)

        if hk1_1046:
            extra_pull_bonus_pct += exp_pulled * 0.10 * scale_1046
        if hk2_1046:
            extra_pull_bonus_pct += (head_cnt + exp_pulled) * 0.05 * scale_1046
        tag_notes.append(f"希雅拉人(期望{head_cnt}➔{head_cnt+exp_pulled:.1f}人)")

    if 1035 in lineup_set and bench_avail > 0:
        lv_1035 = _get_talent_lv(1035, 103501, 5)
        scale_1035 = _role_scale(1035)
        p_1035 = (0.50 + 0.05 * (min(5, max(1, lv_1035)) - 1)) * scale_1035
        pulled_1035 = min(float(bench_avail) - exp_pulled, p_1035 * min(1.6, float(bench_avail)))
        if pulled_1035 > 0:
            exp_pulled += pulled_1035
            prob_pull_any = max(prob_pull_any, min(1.0, p_1035))
            if head_cnt == 2:
                prob_reach_3plus = max(prob_reach_3plus, min(1.0, p_1035))
            elif head_cnt == 1:
                prob_reach_3plus = max(prob_reach_3plus, min(1.0, p_1035 * 0.45))
            tag_notes.append(f"出羽拉人(+{pulled_1035:.1f}人)")

    eff_mates = min(4.5, head_cnt + exp_pulled)

    # 2. 队友训练专属『主/副属性基础加值』（佐久间 1020 / 龙宫寺 1002 / 望月 1012 全局天赋 + 在场同伴头顶表情基础加值 flat_property_add_by_emoji）
    main_st_id = 100 + idx
    sub_map = {1: 105, 2: 106, 3: 101, 4: 102, 5: 103, 6: 104}
    sub_st_id = sub_map.get(idx, 104)

    def _unit_stat_ev(st_id: int) -> float:
        bw = w["stat_base_value"].get(str(st_id), 1.0)
        cur_v = props.get(st_id, 200)
        max_v = prop_max.get(st_id, 1600)
        ratio = cur_v / max(1, max_v)
        if ratio >= 1.0:
            return 0.0
        g_rate = growth_rates.get(st_id, 0) / 10000.0
        g_mult = 1.0 + g_rate * float(w.get("growth_rate_synergy_scale", 0.35))
        if ratio >= w["cap_proximity_decay_start_ratio"]:
            m = 0.65
        elif ratio < 0.55 and state.get("round", 1) > 12:
            m = w["under_cap_catchup_bonus"]
        else:
            m = 1.0
        return bw * m * g_mult

    # teammate_bonus_rule: 每名在场同伴头顶冒表情时自带基础属性加值 (emoji_1 笑脸 +2~4, emoji_2 平常脸 +1~2)
    high_bonus_on_btn = sum(1 for mid in mate_ids if mid in (1007, 1013, 1020))
    emoji_flat_per_mate = 1.05 if eff_mates < 1.8 else 1.25
    flat_main_add = eff_mates * emoji_flat_per_mate + high_bonus_on_btn * 0.60
    flat_sub_add = eff_mates * (0.28 if head_cnt == 1 else 0.45)
    if 1020 in lineup_set:
        scale_1020 = _role_scale(1020)
        lv_1020 = _get_talent_lv(1020, 102001, 5)
        prob_extra2 = 0.40 + 0.025 * (min(7, max(1, lv_1020)) - 1)
        flat_main_add += (3.0 + prob_extra2 * 2.0) * scale_1020
        if _has_hk(1020, 2):
            flat_sub_add += (0.50 * 2.0) * scale_1020
    if 1002 in lineup_set:
        scale_1002 = _role_scale(1002)
        lv_1002 = _get_talent_lv(1002, 100201, 5)
        prob_extra2_1002 = 0.40 + 0.025 * (min(7, max(1, lv_1002)) - 1)
        flat_main_add += (2.0 + prob_extra2_1002 * 2.0) * scale_1002
    if 1012 in lineup_set and ps >= 55 and _has_hk(1012, 2):
        flat_main_add += 2.0 * _role_scale(1012)

    flat_talent_ev = flat_main_add * _unit_stat_ev(main_st_id) + flat_sub_add * _unit_stat_ev(sub_st_id)

    # 3. 计算表情基础 Bonus 与『全部表情一致』(Bingo) 独立总乘区期望倍率（v0.10.0 按 4 份实机日志 sc_train_result_end.total_bonus 1-to-1 校准）：
    # 实机底层结算铁律：total_bonus = (1 + base_emoji_bonus) × (bingo_mult if Bingo else 1.0)
    # 此前旧版误将 bingo_mult 仅乘在 base_emoji_bonus 上而漏乘底数 1.0，导致 1/2/3 同伴倍率系统性低估 80~120 pp！
    per_mate_bonus = 0.165 + (0.05 if has_tongxin else 0.0)
    if 1012 in lineup_set:
        scale_1012 = _role_scale(1012)
        lv_1012 = _get_talent_lv(1012, 101201, 4)
        ps_thresh = max(55, 65 - 2 * (lv_1012 - 1))
        wangyue_add = (0.025 + (0.045 if ps >= ps_thresh else 0.0)) * scale_1012
        per_mate_bonus += wangyue_add
    if 1009 in lineup_set:
        lv_1009 = _get_talent_lv(1009, 100901, 5)
        ps_thresh_1009 = max(45, 60 - 3 * (lv_1009 - 1))
        if ps >= ps_thresh_1009:
            per_mate_bonus += 0.04 * _role_scale(1009)
    if 1013 in lineup_set and (state.get("weekend_train") or state.get("round_type") == 2) and _has_hk(1013, 2):
        # 百目鬼 ★2 心结：在周六加训中，全部表情属性加成额外 +5%（★1/★2 的固定加点已包含在面板基础值中）
        per_mate_bonus += 0.05 * _role_scale(1013)

    base_emoji_bonus = 0.06 + eff_mates * per_mate_bonus + extra_pull_bonus_pct + high_bonus_on_btn * 0.02

    # 『全部表情一致』(Bingo)：主控 + 至少 1 名同伴（队友或经理，总人数 >= 2）即可触发全员同表情共鸣
    if head_cnt == 1:
        base_align_prob = 0.31
    elif head_cnt == 2:
        base_align_prob = 0.29
    else:
        base_align_prob = 0.42
    if 1007 in lineup_set:
        scale_1007 = _role_scale(1007)
        lv_1007 = _get_talent_lv(1007, 100701, 5)
        shantang_prob = (0.20 + 0.025 * (min(7, max(1, lv_1007)) - 1)) * scale_1007
        if _has_hk(1007, 1):
            shantang_prob += 0.06 * scale_1007
        base_align_prob = min(0.85, base_align_prob + shantang_prob)
        tag_notes.append("闪堂锁同表情")

    # 实机 multi = 225/245/265/285 对应随人数递增的 Bingo 总乘区，叠加真崎(1030)/阵野(1031)
    bingo_mult = 1.65 + 0.17 * max(0.0, eff_mates - 1.0)
    if 1030 in lineup_set:
        scale_1030 = _role_scale(1030)
        lv_1030 = _get_talent_lv(1030, 103001, 5)
        masaki_mult = (0.22 + 0.025 * (min(7, max(1, lv_1030)) - 1) + (0.10 if _has_hk(1030, 2) else 0.0)) * scale_1030
        bingo_mult += masaki_mult
        tag_notes.append("真崎高倍率")
    if 1031 in lineup_set:
        scale_1031 = _role_scale(1031)
        lv_1031 = _get_talent_lv(1031, 103101, 5)
        bingo_mult += (0.22 + 0.025 * (min(7, max(1, lv_1031)) - 1)) * scale_1031

    exp_total_mult = (1.0 + base_emoji_bonus) * (1.0 + base_align_prob * (bingo_mult - 1.0))
    skill_mult = max(0.0, exp_total_mult - 1.0)

    if 1001 in lineup_set:
        lv_1001 = _get_talent_lv(1001, 100101, 5)
        skill_mult *= 1.0 + (0.20 + 0.025 * (min(5, max(1, lv_1001)) - 1)) * _role_scale(1001)

    # 佐久间/望月/表情固定加点属于训练基础值 (train_main_property_add)，同样吃 total_bonus 乘区放大！
    mate_skill_ev = (stat_ev + flat_talent_ev * 0.85) * skill_mult + flat_talent_ev

    # 4. 陪练队友自身涨属性不计入主控评价分（百分百通关前提下权重归零，默认 0.0）
    head_bonus = float(w.get("teammate_head_bonus", 0.0))
    synergy_3plus = float(w.get("teammate_3plus_synergy_bonus", 0.0)) * prob_reach_3plus
    effective_growth_mates = mate_cnt + exp_pulled * 0.85
    mate_growth_ev = effective_growth_mates * head_bonus + synergy_3plus

    total_mate_ev = mate_skill_ev + mate_growth_ev
    est_extra_stat = int(round((cmd["total_stat_gain"] + (flat_main_add + flat_sub_add) * 0.85) * skill_mult + flat_main_add + flat_sub_add))
    names_str = "/".join(on_btn_names)
    pull_badge = "·拉人" if exp_pulled > 0 else ""
    tx_badge = "·🧧同心" if has_tongxin else ""
    return (
        total_mate_ev,
        f"👥{names_str}(+{est_extra_stat}{pull_badge}{tx_badge})",
    )


def calc_equip_trammel_option_ev(
    cur_t: int,
    step_tram: int,
    rem_after: int,
    w: dict,
    already_maxed_crit_cnt: int = 0,
    *,
    fractional_step: bool = False,
) -> dict:
    """
    可暴击器材「本次合训 +13(+20) 熟练度」或「商店/扭蛋 +gain 熟练度」的彩圈/金圈期权价值（v0.10.1）：
    - 按新队友乘区实测单彩爆净超额温和校准 Δ_rb=56.0 / Δ_gd=3.5；
    - 采用轻度风险折现 turn_disc=0.98，兼顾 cur_t=0..91 严格单调与前期 0~39 熟器材合理起步估值；
    - 引入 already_maxed_crit_cnt 多满彩卡竞争边际递减（0/1/2/3 张 -> 1.00 / 0.88 / 0.76 / 0.65）；
    - fractional_step=True 供商店/扭蛋非 13 整数倍熟练度道具平滑插值对齐同口径期权模型。
    """
    q = min(0.95, max(0.05, float(w.get("equip_card_click_rate", 0.26))))
    p = float(w.get("equip_rainbow_harvest_prob", 0.22))
    delta_rb = float(w.get("equip_rainbow_realized_delta", 56.0))
    delta_gd = float(w.get("equip_gold_realized_delta", 3.5))
    turn_disc = min(1.0, max(0.80, float(w.get("equip_option_turn_discount", 0.98))))
    step = max(1, int(step_tram))
    rem = max(0, int(rem_after))
    cur = int(cur_t)
    if rem <= 0 or cur >= 100:
        return {"ev": 0.0, "rb_ev": 0.0, "gd_ev": 0.0, "n_after": 0, "grad": False}

    maxed_cnt = max(0, int(already_maxed_crit_cnt or 0))
    _comp_tbl = {0: 1.00, 1: 0.88, 2: 0.76, 3: 0.65}
    comp_mult = _comp_tbl.get(maxed_cnt, max(0.50, 0.65 - 0.08 * (maxed_cnt - 3)))

    def _exp_harvest_turns_int(n: int) -> float:
        """带逐回合时间折现的期望可收割回合数：sum_{s=0..rem-1} turn_disc^s * P(Binomial(s, q) >= n)。"""
        if n <= 0:
            return sum(turn_disc ** s for s in range(rem))
        if n > rem:
            return 0.0
        dp = [0.0] * (n + 1)
        dp[0] = 1.0
        tot = 0.0
        for s in range(rem):
            tot += dp[n] * (turn_disc ** s)
            if s + 1 < rem:
                dp[n] = dp[n] + dp[n - 1] * q
                for j in range(n - 1, 0, -1):
                    dp[j] = dp[j] * (1.0 - q) + dp[j - 1] * q
                dp[0] = dp[0] * (1.0 - q)
        return tot

    def _exp_harvest_turns_frac(n_f: float) -> float:
        if n_f <= 0.0:
            return _exp_harvest_turns_int(0)
        lo = int(n_f)
        frac = n_f - lo
        if frac <= 1e-9:
            return _exp_harvest_turns_int(lo)
        return (1.0 - frac) * _exp_harvest_turns_int(lo) + frac * _exp_harvest_turns_int(lo + 1)

    def _n_need(threshold: int, base: int) -> int:
        return -(-max(0, threshold - base) // step)

    def _value(base: int) -> tuple[float, float]:
        if fractional_step:
            unit_step = 13.0
            n100_f = 0.0 if base >= 100 else max(0.0, (100.0 - base) / unit_step)
            n70_f = 0.0 if base >= 70 else max(0.0, (70.0 - base) / unit_step)
            h100 = _exp_harvest_turns_frac(n100_f)
            h70 = _exp_harvest_turns_frac(n70_f)
        else:
            h100 = _exp_harvest_turns_int(_n_need(100, base))
            h70 = _exp_harvest_turns_int(_n_need(70, base))
        return p * delta_rb * comp_mult * h100, p * delta_gd * comp_mult * max(0.0, h70 - h100)

    next_t = min(100, cur + step)
    rb_with, gd_with = _value(next_t)
    rb_without, gd_without = _value(cur)
    rb_ev = rb_with - rb_without
    gd_ev = gd_with - gd_without
    return {
        "ev": round(max(0.0, rb_ev + gd_ev), 1),
        "rb_ev": round(rb_ev, 1),
        "gd_ev": round(gd_ev, 1),
        "n_after": _n_need(100, next_t),
        "grad": cur + step >= 100,
    }


# ---------------------------------------------------------------------------
# P5 训练设施等级同口径复利模型 (v0.9.0)
# ---------------------------------------------------------------------------
FACILITY_UPGRADE_STEPS = {1: 3, 2: 4, 3: 5, 4: 5}

# 各级跃升 (Lv1->2, Lv2->3, Lv3->4, Lv4->5) 裸面板主/副属性增量与体力变化
FACILITY_MAIN_DELTA_NORMAL = {1: 3.0, 2: 3.2, 3: 3.4, 4: 2.9}
FACILITY_SUB_DELTA_NORMAL = {1: 1.1, 2: 1.1, 3: 1.1, 4: 0.9}
FACILITY_PS_COST_DELTA_NORMAL = {1: 2.0, 2: 2.5, 3: 2.2, 4: 2.2}

FACILITY_MAIN_DELTA_IDX6 = {1: 3.0, 2: 2.9, 3: 2.4, 4: 2.1}
FACILITY_SUB_DELTA_IDX6 = {1: 1.0, 2: 0.9, 3: 0.8, 4: 0.7}
# 实测 6 号意识回体：Lv1(+4) -> Lv2(+4) -> Lv3(+5) -> Lv4(+5) -> Lv5(+6)
FACILITY_PS_HEAL_DELTA_IDX6 = {1: 0.0, 2: 1.0, 3: 0.0, 4: 1.0}

FACILITY_STAT_TOPOLOGY = {
    1: (101, 105),  # 1号投射: 主投射, 副运控（循环A: 101->105->103->101）
    2: (102, 106),  # 2号弹跳: 主弹跳, 副意识（循环B: 102->106->104->102，301/301次实机train_cmds实锤）
    3: (103, 101),  # 3号防守: 主防守, 副投射（循环A）
    4: (104, 102),  # 4号体能: 主体能, 副弹跳（循环B）
    5: (105, 103),  # 5号运控: 主运控, 副防守（循环A）
    6: (106, 104),  # 6号意识: 主意识, 副体能（循环B）
}

DEFAULT_P5_FACILITY_WEIGHTS = {
    "facility_base_click_rate": 0.167,
    "facility_click_rate_min": 0.10,
    "facility_click_rate_max": 0.25,
    "facility_click_prior_turns": 15.0,
    "facility_harvest_bonus_mult": 1.75,
    "facility_stamina_opp_cost": 1.05,
    "facility_idx6_heal_ev_per_ps": 1.15,
    "facility_max_lookahead_levels": 2,
}


def _resolve_effective_focus_levels(state: dict) -> dict:
    """获取当前用于海滩后复利评估的真实基础设施等级（海滩周 R22~R26 全锁 Lv5 时优先回退至 base_focus_levels）。"""
    focus_lvs = state.get("focus_levels", {}) or {}
    base_lvs = state.get("base_focus_levels", {}) or {}
    er = infer_exp_round(state)
    in_beach = (22 <= er <= 26) and not bool(state.get("weekend_train"))
    if (
        in_beach
        and focus_lvs
        and all(int(focus_lvs.get(k, 1)) == 5 for k in range(1, 7))
        and base_lvs
        and any(int(base_lvs.get(k, 1)) < 5 for k in range(1, 7))
    ):
        return base_lvs
    return focus_lvs


def build_future_train_harvest_mask(state: dict) -> list[int]:
    """
    构造「本训之后」未来每个训练回合是否吃常规设施等级差的 0/1 序列 harvest_mask：
    - 海滩合宿周 R22~R26 (平日 5 训) 强制临时锁 Lv5，常规等级不产生面板差值 → 记 0
    - 其余常规平日训及百目鬼周六加训均按常规设施等级结算 → 记 1
    """
    exp_r = infer_exp_round(state)
    has_dm = has_doumeki_in_lineup(state)

    weekday_train_rounds = [
        1, 2, 3, 4, 5,
        9, 10, 11, 12,
        16, 17, 18, 19,
        22, 23, 24, 25, 26,
        30, 31, 32, 33,
        37, 38, 39, 40,
        44, 46,
    ]
    sat_rounds = [6, 13, 20, 27, 34, 41, 48]

    future_events: list[tuple[int, int]] = []
    for r in weekday_train_rounds:
        if r > exp_r:
            is_beach = 1 if (22 <= r <= 26) else 0
            future_events.append((r, 0 if is_beach else 1))

    if has_dm:
        future_sats = [sr for sr in sat_rounds if sr > exp_r]
        sat_equiv = 1 if (exp_r >= 46 and future_sats) else int(round(len(future_sats) * 0.75))
        if sat_equiv > 0 and future_sats:
            step = len(future_sats) / sat_equiv
            picked_sats = [future_sats[min(len(future_sats) - 1, int(i * step))] for i in range(sat_equiv)]
            for sr in picked_sats:
                future_events.append((sr, 1))

    future_events.sort(key=lambda x: x[0])
    return [h for _, h in future_events]


def expected_harvest_turns_with_mask(n_clicks: int, harvest_mask: list[int], q: float) -> float:
    """
    精确计算：在未来回合序列 harvest_mask (1=可收割, 0=海滩锁Lv5) 中，
    当还需 n_clicks 次命中（每回合独立概率 q）即可达标时，达标后的期望可收割回合数：
        H(n) = sum_{s=1..M} harvest_mask[s-1] * P(Binomial(s-1, q) >= n)
    无海滩掩码时与负二项分布期望 E[(M - T_n)+] 严格恒等。
    """
    m = len(harvest_mask)
    if m <= 0:
        return 0.0
    if n_clicks <= 0:
        return float(sum(harvest_mask))
    if n_clicks > m:
        return 0.0

    q_clamped = min(0.95, max(0.02, float(q)))
    dp = [0.0] * (n_clicks + 1)
    dp[0] = 1.0
    tot = 0.0

    for s_idx in range(m):
        if harvest_mask[s_idx]:
            tot += dp[n_clicks]
        if s_idx + 1 < m:
            dp[n_clicks] = dp[n_clicks] + dp[n_clicks - 1] * q_clamped
            for j in range(n_clicks - 1, 0, -1):
                dp[j] = dp[j] * (1.0 - q_clamped) + dp[j - 1] * q_clamped
            dp[0] = dp[0] * (1.0 - q_clamped)

    return tot


def _facility_stat_cap_room_factor(st_id: int, state: dict, strategy: dict) -> float:
    """连续平滑防触顶系数：属性接近上限时设施升级长期收益平滑归零。"""
    props = state.get("props", {}) or {}
    prop_max = state.get("prop_max", {}) or {}
    cur_v = int(props.get(st_id, 200))
    max_v = max(1, int(prop_max.get(st_id, 1600)))
    if cur_v >= max_v:
        return 0.0
    ratio = cur_v / max_v

    focus_st, _ = get_active_focus(strategy)
    is_focus = bool(focus_st and st_id == focus_st)

    if is_focus:
        start_r, end_r = 0.88, 0.97
    else:
        start_r, end_r = 0.84, 0.96

    if ratio <= start_r:
        return 1.0
    if ratio >= end_r:
        return 0.0
    return max(0.0, (end_r - ratio) / (end_r - start_r))


def estimate_facility_click_rate(idx: int, state: dict, strategy: dict, p5_cfg: dict | None = None) -> float:
    """估计设施 idx 在未来非毕业回合中被选中的自然点击概率 q_f（贝叶斯平滑）。"""
    w = (strategy or {}).get("weights", {})
    cfg = {**DEFAULT_P5_FACILITY_WEIGHTS, **{k: w[k] for k in DEFAULT_P5_FACILITY_WEIGHTS if k in w}, **(p5_cfg or {})}

    q_base = float(cfg["facility_base_click_rate"])
    q_min = float(cfg["facility_click_rate_min"])
    q_max = float(cfg["facility_click_rate_max"])
    prior_n = float(cfg["facility_click_prior_turns"])

    train_counts = state.get("train_counts", {}) or {}
    focus_lvs = _resolve_effective_focus_levels(state)
    round_done = max(0, int(state.get("round", 1)) - 1)

    cur_cnt = int(train_counts.get(idx, 0))
    cur_lv = int(focus_lvs.get(idx, 1))
    er = infer_exp_round(state)
    in_beach = (22 <= er <= 26) and not bool(state.get("weekend_train"))
    if in_beach and all(int(focus_lvs.get(k, 1)) == 5 for k in range(1, 7)):
        est_clicks = cur_cnt
    else:
        prior_lv_clicks = sum(FACILITY_UPGRADE_STEPS.get(lv, 4) for lv in range(1, min(cur_lv, 5)))
        est_clicks = min(round_done, prior_lv_clicks + cur_cnt)

    q_est = (q_base * prior_n + est_clicks) / (prior_n + max(0, round_done))

    main_st = 100 + idx
    cap_room = _facility_stat_cap_room_factor(main_st, state, strategy)
    prop_max = state.get("prop_max", {}) or {}
    if int(prop_max.get(main_st, 1600)) >= 1950:
        q_est *= 1.12

    focus_st, focus_bonus = get_active_focus(strategy)
    if focus_st == main_st:
        q_est *= (1.0 + (focus_bonus - 1.0) * 0.45)

    q_est *= (0.45 + 0.55 * cap_room)
    return round(min(q_max, max(q_min, q_est)), 4)


def calc_facility_level_step_net_ev(
    idx: int,
    from_lv: int,
    state: dict,
    strategy: dict,
    p5_cfg: dict | None = None,
) -> float:
    """计算设施 idx 从 from_lv 升至 from_lv + 1 后，未来单次收割时的净 EV 增量 (Δ_fac_net)。"""
    if from_lv < 1 or from_lv >= 5 or idx not in FACILITY_STAT_TOPOLOGY:
        return 0.0

    w = (strategy or {}).get("weights", {})
    cfg = {**DEFAULT_P5_FACILITY_WEIGHTS, **{k: w[k] for k in DEFAULT_P5_FACILITY_WEIGHTS if k in w}, **(p5_cfg or {})}

    main_st, sub_st = FACILITY_STAT_TOPOLOGY[idx]
    stat_base_w = w.get("stat_base_value", {})
    growth_rates = state.get("growth_rates", {}) or {}
    growth_scale = float(w.get("growth_rate_synergy_scale", 0.35))
    focus_st, focus_bonus = get_active_focus(strategy)

    def _eff_stat_w(st_id: int) -> float:
        bw = float(stat_base_w.get(str(st_id), 1.0))
        if focus_st and st_id == focus_st:
            bw = bw * (1.0 + (focus_bonus - 1.0) * 0.70)
        g_mult = 1.0 + (float(growth_rates.get(st_id, 0)) / 10000.0) * growth_scale
        cap_f = _facility_stat_cap_room_factor(st_id, state, strategy)
        return bw * g_mult * cap_f

    if idx == 6:
        d_main = FACILITY_MAIN_DELTA_IDX6.get(from_lv, 2.8)
        d_sub = FACILITY_SUB_DELTA_IDX6.get(from_lv, 0.8)
    else:
        d_main = FACILITY_MAIN_DELTA_NORMAL.get(from_lv, 3.0)
        d_sub = FACILITY_SUB_DELTA_NORMAL.get(from_lv, 1.0)

    bonus_mult = float(cfg["facility_harvest_bonus_mult"])
    gross_stat_ev = (d_main * _eff_stat_w(main_st) + d_sub * _eff_stat_w(sub_st)) * bonus_mult

    if idx == 6:
        heal_delta = FACILITY_PS_HEAL_DELTA_IDX6.get(from_lv, 0.0)
        ps_net_ev = heal_delta * float(cfg["facility_idx6_heal_ev_per_ps"])
    else:
        ps_delta = FACILITY_PS_COST_DELTA_NORMAL.get(from_lv, 2.2)
        dm_discount = 0.86 if has_doumeki_in_lineup(state) else 1.0
        ps_net_ev = -ps_delta * dm_discount * float(cfg["facility_stamina_opp_cost"])

    return max(0.0, gross_stat_ev + ps_net_ev)


def calc_facility_compound_option_ev(
    idx: int,
    state: dict,
    strategy: dict,
    *,
    p5_cfg: dict | None = None,
    base_focus_lv_override: int | None = None,
) -> dict:
    """
    P5 核心入口：计算本回合点击设施 idx 带来的设施等级复利边际 EV。
    与 calc_equip_trammel_option_ev 严格同构：
        EV_fac = q_f * sum_{step k} Δ_net(L+k-1 -> L+k) * [H_mask(n_k - 1) - H_mask(n_k)]
    """
    w = (strategy or {}).get("weights", {})
    cfg = {**DEFAULT_P5_FACILITY_WEIGHTS, **{k: w[k] for k in DEFAULT_P5_FACILITY_WEIGHTS if k in w}, **(p5_cfg or {})}

    if idx not in FACILITY_STAT_TOPOLOGY:
        return {"ev": 0.0, "cur_lv": 5, "rem_to_up": 0, "q_f": 0.0, "delta_net": 0.0, "delta_hits": 0.0, "rem_non_beach": 0, "tag": "", "hud_tag": "", "breakdown": []}

    er = infer_exp_round(state)
    has_dm = has_doumeki_in_lineup(state)
    focus_lvs = _resolve_effective_focus_levels(state)
    if (has_dm and er >= 48) or (not has_dm and er >= 46):
        return {"ev": 0.0, "cur_lv": int(focus_lvs.get(idx, 1)), "rem_to_up": 0, "q_f": 0.0, "delta_net": 0.0, "delta_hits": 0.0, "rem_non_beach": 0, "tag": "", "hud_tag": "", "breakdown": []}

    train_counts = state.get("train_counts", {}) or {}
    train_up_need = state.get("train_up_need", {}) or {}

    cur_cnt = int(train_counts.get(idx, 0))
    if base_focus_lv_override is not None:
        cur_lv = int(base_focus_lv_override)
        step_need = max(1, int(train_up_need.get(idx, FACILITY_UPGRADE_STEPS.get(cur_lv, 3))))
        rem_to_up = max(1, step_need - cur_cnt)
    elif idx in focus_lvs:
        cur_lv = int(focus_lvs[idx])
        step_need = max(1, int(train_up_need.get(idx, FACILITY_UPGRADE_STEPS.get(cur_lv, 3))))
        rem_to_up = max(1, step_need - cur_cnt)
    else:
        step_need = max(1, int(train_up_need.get(idx, 3)))
        cur_lv = min(5, 1 + cur_cnt // step_need)
        rem_to_up = step_need - (cur_cnt % step_need)

    harvest_mask = build_future_train_harvest_mask(state)
    rem_non_beach = int(sum(harvest_mask))

    if cur_lv >= 5 or rem_non_beach <= 0:
        return {
            "ev": 0.0,
            "cur_lv": cur_lv,
            "rem_to_up": 0,
            "q_f": 0.0,
            "delta_net": 0.0,
            "delta_hits": 0.0,
            "rem_non_beach": rem_non_beach,
            "tag": "",
            "hud_tag": "",
            "breakdown": [],
        }

    q_f = estimate_facility_click_rate(idx, state, strategy, p5_cfg=cfg)
    max_look = max(1, int(cfg["facility_max_lookahead_levels"]))

    total_ev = 0.0
    total_delta_hits = 0.0
    first_delta_net = 0.0
    breakdown = []

    cum_clicks_needed = 0
    for offset in range(min(max_look, 5 - cur_lv)):
        from_lv = cur_lv + offset
        if offset == 0:
            cum_clicks_needed = rem_to_up
        else:
            cum_clicks_needed += FACILITY_UPGRADE_STEPS.get(from_lv, 5)

        delta_net = calc_facility_level_step_net_ev(idx, from_lv, state, strategy, p5_cfg=cfg)
        if offset == 0:
            first_delta_net = delta_net
        if delta_net <= 0.0:
            continue

        h_with = expected_harvest_turns_with_mask(cum_clicks_needed - 1, harvest_mask, q_f)
        h_without = expected_harvest_turns_with_mask(cum_clicks_needed, harvest_mask, q_f)
        delta_harvest_turns = max(0.0, h_with - h_without)
        delta_hits = q_f * delta_harvest_turns
        step_ev = delta_net * delta_hits
        total_ev += step_ev
        total_delta_hits += delta_hits
        breakdown.append({
            "transition": f"Lv{from_lv}->Lv{from_lv+1}",
            "clicks_before": cum_clicks_needed,
            "clicks_after": cum_clicks_needed - 1,
            "delta_net": round(delta_net, 2),
            "delta_hits": round(delta_hits, 3),
            "ev": round(step_ev, 2),
        })

    ev_rounded = round(max(0.0, total_ev), 1)
    tag = ""
    hud_tag = ""
    if ev_rounded >= 3.0:
        if rem_to_up == 1:
            tag = f"📈差1升Lv{cur_lv+1}(+{ev_rounded:.0f})"
            hud_tag = f"差1升Lv{cur_lv+1}"
        elif ev_rounded >= 4.5:
            tag = f"🏗️设施推进({cur_cnt}/{step_need}→Lv{cur_lv+1}·+{ev_rounded:.0f})"
            hud_tag = f"推Lv{cur_lv+1}"

    return {
        "ev": ev_rounded,
        "cur_lv": cur_lv,
        "rem_to_up": rem_to_up,
        "q_f": q_f,
        "delta_net": round(first_delta_net, 2),
        "delta_hits": round(total_delta_hits, 3),
        "rem_non_beach": rem_non_beach,
        "tag": tag,
        "hud_tag": hud_tag,
        "breakdown": breakdown,
    }


def calc_shop_facility_book_p5_ev(
    iid: int,
    state: dict,
    strategy: dict,
    *,
    p5_cfg: dict | None = None,
    lv_gain_override: int | None = None,
    train_idx_override: int | None = None,
) -> dict:
    """
    商店设施直升书 I/II (+1/+2级) 与扭蛋设施升级包同口径复利估值：
    - 精确扣除海滩 5 训（R22~R26 不吃常规等级）
    - 精确扣除不买书时未来自然点击升级的重叠收割时段
    """
    meta = STORE_ITEM_CATALOG.get(iid, {})
    t_idx = int(train_idx_override if train_idx_override is not None else meta.get("train_idx", 1))
    lv_gain = int(lv_gain_override if lv_gain_override is not None else (2 if (iid % 100 == 20) else 1))

    focus_lvs = _resolve_effective_focus_levels(state)
    train_counts = state.get("train_counts", {}) or {}
    train_up_need = state.get("train_up_need", {}) or {}

    cur_lv = int(focus_lvs.get(t_idx, 1))
    cur_cnt = int(train_counts.get(t_idx, 0))
    step_need = max(1, int(train_up_need.get(t_idx, FACILITY_UPGRADE_STEPS.get(cur_lv, 3))))
    eff_lv = max(0, min(lv_gain, 5 - cur_lv))

    harvest_mask = build_future_train_harvest_mask(state)
    rem_non_beach = int(sum(harvest_mask))
    if eff_lv <= 0 or rem_non_beach <= 0:
        return {"ev": 0.0, "eff_lv": 0, "rem_non_beach": rem_non_beach, "tag": "已满Lv5" if cur_lv >= 5 else "无剩余非海滩训"}

    q_f = estimate_facility_click_rate(t_idx, state, strategy, p5_cfg=p5_cfg)
    total_ev = 0.0
    rem_to_up_base = max(1, step_need - cur_cnt)

    def _clicks_to_reach(start_lv: int, target_lv: int, first_rem: int) -> int:
        if target_lv <= start_lv:
            return 0
        tot = first_rem
        for lv in range(start_lv + 1, target_lv):
            tot += FACILITY_UPGRADE_STEPS.get(lv, 5)
        return tot

    new_lv = cur_lv + eff_lv
    new_first_rem = FACILITY_UPGRADE_STEPS.get(new_lv, 5)

    for target_lv in range(cur_lv + 1, 6):
        delta_net = calc_facility_level_step_net_ev(t_idx, target_lv - 1, state, strategy, p5_cfg=p5_cfg)
        if delta_net <= 0.0:
            continue
        n_before = _clicks_to_reach(cur_lv, target_lv, rem_to_up_base)
        n_after = _clicks_to_reach(new_lv, target_lv, new_first_rem)
        h_before = expected_harvest_turns_with_mask(n_before, harvest_mask, q_f)
        h_after = expected_harvest_turns_with_mask(n_after, harvest_mask, q_f)
        total_ev += delta_net * q_f * max(0.0, h_after - h_before)

    return {
        "ev": round(total_ev, 1),
        "eff_lv": eff_lv,
        "q_f": q_f,
        "rem_non_beach": rem_non_beach,
        "tag": f"Lv{cur_lv}➔Lv{new_lv}(非海滩余{rem_non_beach}训)",
    }


def score_training_turn(state: dict, cmds: list, strategy: dict):
    """
    结合赛马娘经典 EV 模型 + 篮球少女专属实证机制：
    - 线性 Rank 水桶底盘 + 六维主副属性拓扑修正：106(意识)为无副属性带动的孤岛属性，给防掉队加成；104(体能)被2号弹跳与6号意识双路带动，提前至85%上限衰减防后期溢出
    - 角色原生成长率感知 (property_growth_rate_tbl)：顺应球员自带的 +5%/+8.5% 属性成长率加成
    - 训练设施等级与差 1 次升级补刀 (train_counts / train_up_need_counts=3)：前中期 2/3、5/6 等差 1 次升设施等级时享长期复利补刀加成，Lv1 零点击滞后设施享破冰加成
    - 器材面板去重：面板 before_bonus_property_tbl 已包含彩圈(value=2)属性加成；已满100熟练的彩圈器材不再重复计入涨熟练人头分
    - v0.8.0 器材彩圈期权：可暴击器材涨熟按「满彩日期提前 × 剩余可收割训数」计价（calc_equip_trammel_option_ev），
      实测 total_bonus 几乎只随在场人数上涨(1人232%/2人276%/3人320%)，器材彩圈抬底数、队友抬倍率，二者乘法叠加 ⇒ 前期先养底数
    - 队友合训技能倍率：预览面板 add_property_tbl 恒为空，结算时 1/2/3/4 名队友分别提供平均 +12%/+35%/+55%/+65% 额外属性倍率 + 队友自身经验/六维成长
    - 经理能量与哨子器材精确识别：区分无哨子(+1/次)、常规哨子(11004/465, +2/次)、双哨满配(+3/次)及11次爆发硬上限
    """
    w = strategy["weights"]
    rc = strategy["risk_control"]
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    growth_rates = state.get("growth_rates", {})
    train_counts = state.get("train_counts", {})
    train_up_need = state.get("train_up_need", {})
    round_num = state.get("round", 1)
    mgr_e = state.get("mgr_energy", 0)
    mgr_ex = state.get("mgr_explosive", 0)
    mgr_max_ex = state.get("mgr_max_explosive", 11)

    whistle_step, _ = get_whistle_step(state)
    has_dm = has_doumeki_in_lineup(state)
    is_sat_train = (state.get("round_type") == 2) or bool(state.get("weekend_train"))
    exp_r_top = infer_exp_round(state) if has_dm else 0

    is_early = (exp_r_top <= 18) if has_dm else (round_num <= 12)
    rem_turns_factor = (
        max(0.0, min(1.0, (get_remaining_trains(state) - 2) / 20.0))
        if has_dm
        else max(0.0, min(1.0, (26 - round_num) / 18.0))
    )
    stamina_104_decay_ratio = float(w.get("stamina_104_early_decay_ratio", w.get("cap_proximity_decay_start_ratio", 0.92)))
    iso_106_mult = float(w.get("isolated_106_catchup_extra_mult", 1.0))
    growth_scale = float(w.get("growth_rate_synergy_scale", 0.35))
    focus_st, focus_bonus = get_active_focus(strategy)
    focus_cur_ratio = (
        props.get(focus_st, 200) / max(1, prop_max.get(focus_st, 1600))
        if focus_st
        else 1.0
    )
    # 预先统计当前卡组已满 100 熟的可暴击器材数量，供白圈期权多满彩竞争边际递减使用
    _card_tram_all = state.get("supactor_card_trammel", {}) or {}
    _card_to_cfg_all = state.get("supactor_card_to_cfg", {}) or {}
    _sup_tram_cfg_all = state.get("supactor_trammel", {}) or {}
    if _card_tram_all:
        already_maxed_crit_cnt = sum(
            1
            for cid_k, tv in _card_tram_all.items()
            if int(tv or 0) >= 100 and _card_to_cfg_all.get(cid_k, cid_k) not in NON_CRIT_EQUIPMENT_IDS
        )
    else:
        already_maxed_crit_cnt = sum(
            1
            for cfg_k, tv in _sup_tram_cfg_all.items()
            if int(tv or 0) >= 100 and int(cfg_k) not in NON_CRIT_EQUIPMENT_IDS
        )
    scored = []

    for cmd in cmds:
        idx = cmd["idx"]
        gains = cmd["gains"]
        sup_cnt = cmd["sup_count"]
        gold_cnt = cmd.get("gold_count", 0)
        rainbow_cnt = cmd["rainbow_count"]
        mate_cnt = cmd["mate_count"]
        is_mgr = cmd["is_manager"]
        fail_rate = cmd["fail_rate"]
        ps_cost = cmd["ps"]

        score = 0.0
        reasons = []
        hud_tags = []

        # 1. 属性收益计算（考虑属性上限硬截断防溢出 + 六维等权底盘 + 角色原生成长率 + 短板追赶 + 专项特化偏向）
        stat_ev = 0.0
        real_total_gain = 0
        for st_id, val in gains.items():
            is_focus_stat = bool(focus_st and st_id == focus_st)
            base_w = float(w["stat_base_value"].get(str(st_id), 1.0))
            if is_focus_stat:
                base_w = base_w * focus_bonus
            cur_v = props.get(st_id, 200)
            max_v = prop_max.get(st_id, 1600)
            room = max(0, max_v - cur_v)
            real_val = min(int(val), room)
            real_total_gain += real_val
            ratio = cur_v / max(1, max_v)
            st_name = STAT_NAMES.get(st_id, str(st_id))

            # 角色原生成长率加成（如 value=500 即 +5.0%）
            g_rate = growth_rates.get(st_id, 0) / 10000.0
            growth_mult = 1.0 + g_rate * growth_scale
            cap_decay_start = max(float(w["cap_proximity_decay_start_ratio"]), 0.94) if is_focus_stat else float(w["cap_proximity_decay_start_ratio"])

            if room <= 0:
                mult = 0.0
                if st_id == 100 + idx:
                    reasons.append(f"⚠️{st_name}已满(+0)")
                    hud_tags.append(f"⚠️{st_name}已满")
            elif real_val < val:
                mult = 0.72
                if st_id == 100 + idx:
                    reasons.append(f"⚠️{st_name}触顶(实+{real_val})")
                    hud_tags.append(f"⚠️{st_name}触顶(实+{real_val})")
            elif ratio >= cap_decay_start:
                mult = 0.65
            elif st_id == 104 and not is_focus_stat and stamina_104_decay_ratio < cap_decay_start and ratio >= stamina_104_decay_ratio:
                mult = 0.72
            elif ratio < 0.55 and not is_early:
                mult = w["under_cap_catchup_bonus"] * (iso_106_mult if (st_id == 106 and iso_106_mult > 1.0) else 1.0)
                reasons.append(f"{st_name}补短")
                if st_id == 100 + idx:
                    hud_tags.append(f"补{st_name}")
            elif max_v >= 1950 and ratio < 0.85 and not is_early:
                # 中后期主控高上限核心属性（上限 >=1950 且未满 85%）享主攻倾斜，避免被低上限副训补短板抢走大面板回合
                mult = 1.08
            elif st_id == 106 and iso_106_mult > 1.0 and ratio < 0.72:
                mult = iso_106_mult
            else:
                mult = 1.0

            stat_ev += real_val * base_w * mult * growth_mult

        score += stat_ev
        if real_total_gain < cmd["total_stat_gain"]:
            reasons.append(f"+{cmd['total_stat_gain']}(实+{real_total_gain})")
        else:
            reasons.append(f"+{cmd['total_stat_gain']}")

        # 1.5 P5 训练设施等级同口径复利模型（连续级内推进 + 海滩 5 训扣减 + 6 号意识免费回体优势 + 贝叶斯点击率）
        p5_fac_info = calc_facility_compound_option_ev(idx, state, strategy)
        p5_fac_ev = p5_fac_info["ev"]
        if p5_fac_ev > 0.0:
            score += p5_fac_ev
            if p5_fac_info["tag"]:
                reasons.append(p5_fac_info["tag"])
            if p5_fac_info["hud_tag"]:
                hud_tags.append(p5_fac_info["hud_tag"])

        # 2. 器材熟练度与金爆/彩爆加成（利用 card_id 与 val=0/1/2 精确区分「100熟彩爆(val=2)」「70~99熟金爆+继续涨熟(val=1)」「未满100白圈涨熟(val=0)」，并感知哨子跨 70/100 触发三选一）
        if sup_cnt > 0:
            card_tram = state.get("supactor_card_trammel", {})
            card_to_cfg = state.get("supactor_card_to_cfg", {})
            sup_tram_cfg = state.get("supactor_trammel", {})
            sup_cards = cmd.get("sup_cards")
            has_tutorial_421 = 421 in state.get("active_states", set())
            step_tram = 20 if has_tutorial_421 else 13
            whistle_milestone_bonus = 0.0
            whistle_milestone_tag = ""
            hud_whistle_tag = ""
            grad_100_cnt = 0
            # v0.8.0：可暴击器材改用彩圈/金圈期权 EV（按距 100 差几次 + 满彩后剩余可收割训数连续折算）；
            # 哨子/吱神等不可暴击器材(NON_CRIT_EQUIPMENT_IDS)不进期权，仅保留平滑人头分(+哨子里程碑)。
            rem_after_sup = max(0, get_remaining_trains(state) - 1)
            option_ev_total = 0.0
            option_cnt = 0
            noncrit_unmaxed_cnt = 0
            use_option_model = bool(sup_cards and card_tram)
            if sup_cards and card_tram:
                unmaxed_sup_cnt = 0
                maxed_non_rainbow_cnt = 0
                for sc in sup_cards:
                    cid = sc["card_id"]
                    cfg_id = card_to_cfg.get(cid, cid)
                    cur_t = card_tram.get(cid, sup_tram_cfg.get(cfg_id, 0))
                    # 仅 val >= 2 (100熟彩爆) 已满熟练度；val == 1 (70~99熟金爆) 点击后照常 +13/+20 熟练度冲 100！
                    if sc["val"] >= 2:
                        continue
                    if cur_t >= 100:
                        maxed_non_rainbow_cnt += 1
                    else:
                        unmaxed_sup_cnt += 1
                        if cfg_id in NON_CRIT_EQUIPMENT_IDS:
                            noncrit_unmaxed_cnt += 1
                        else:
                            opt = calc_equip_trammel_option_ev(
                                cur_t, step_tram, rem_after_sup, w,
                                already_maxed_crit_cnt=already_maxed_crit_cnt,
                            )
                            opt_ev = opt["ev"]
                            if focus_st and focus_cur_ratio < 0.94 and _get_equip_pref_stat(cfg_id) == focus_st:
                                opt_ev = round(opt_ev * (1.0 + (focus_bonus - 1.0) * 0.45), 1)
                            option_ev_total += opt_ev
                            option_cnt += 1
                            if opt["grad"] and opt_ev > 0:
                                grad_100_cnt += 1
                        # 哨子(11004)每次合训 +13(+20) 熟练度：
                        # 1) 满100熟后解锁『同框+3格必爆』，且跨70/100分别必弹 ID70(+40体)/ID71(+70体) 三选一；
                        # 2) 即使当回合未直接跨线(如 0->13, 39->52, 78->91)，未满100熟的哨子单走也享有高优先级充能推进分，防止像 8571 局因差 3~4 EV 漏点哨子导致第3周断体！
                        if cfg_id == 11004:
                            if (70 - step_tram) <= cur_t < 70 and not state.get("fired_70", False):
                                whistle_milestone_bonus = 15.0
                                whistle_milestone_tag = "🎯哨冲70"
                                hud_whistle_tag = "哨冲70(+40体)"
                            elif (100 - step_tram) <= cur_t < 100 and not state.get("fired_71", False):
                                whistle_milestone_bonus = 18.0
                                whistle_milestone_tag = "🎯哨冲100"
                                hud_whistle_tag = "哨冲100(+70体)"
                            elif cur_t < 70:
                                whistle_milestone_bonus = 9.5
                                whistle_milestone_tag = f"🎺哨涨熟({cur_t}➔{min(100, cur_t + step_tram)})"
                                hud_whistle_tag = "养哨子"
                            elif cur_t < 100:
                                whistle_milestone_bonus = 11.0
                                whistle_milestone_tag = f"🎺哨冲满({cur_t}➔{min(100, cur_t + step_tram)})"
                                hud_whistle_tag = "养哨子"
            else:
                unmaxed_sup_cnt = max(0, sup_cnt - rainbow_cnt)
                maxed_non_rainbow_cnt = 0

            if has_dm and exp_r_top >= 48:
                # 毕业周六最后 1 动加训后直接进入结算，涨熟练度与哨子冲线均无后续训练或事件弹出窗口
                unmaxed_sup_cnt = 0
                noncrit_unmaxed_cnt = 0
                option_ev_total = 0.0
                option_cnt = 0
                grad_100_cnt = 0
                whistle_milestone_bonus = 0.0
                whistle_milestone_tag = ""
                hud_whistle_tag = ""

            # 平滑人头分（去掉 is_early 台阶）：剩余训练 >=22 时取前期权重，<=10 时取中后期权重，中间线性过渡
            head_early = float(w["supactor_head_early"])
            head_late = float(w["supactor_head_mid_late"])
            rem_now_sup = get_remaining_trains(state)
            head_blend = max(0.0, min(1.0, (rem_now_sup - 10) / 12.0))
            head_w = head_late + (head_early - head_late) * head_blend
            gold_w = float(w.get("supactor_gold_bonus", 0.5))
            rainbow_w = float(w.get("supactor_rainbow_bonus", 2.0))
            if use_option_model:
                # 可暴击器材 → 期权 EV；不可暴击器材(哨子等) → 平滑人头分 × noncrit 系数（哨子另享里程碑分）
                noncrit_scale = float(w.get("supactor_noncrit_head_scale", 1.0))
                trammel_score = option_ev_total + noncrit_unmaxed_cnt * head_w * noncrit_scale
            else:
                # 日志尚未解析出器材熟练度时回退旧人头模型
                grad_bonus = grad_100_cnt * (3.5 if rem_turns_factor > 0.25 else 1.5)
                trammel_score = unmaxed_sup_cnt * head_w + grad_bonus
            sup_score = (
                trammel_score
                + gold_cnt * gold_w
                + rainbow_cnt * rainbow_w
                + whistle_milestone_bonus
            )
            score += sup_score
            sup_parts = []
            if rainbow_cnt > 0:
                sup_parts.append(f"🌈彩×{rainbow_cnt}")
            if gold_cnt > 0:
                sup_parts.append(f"✨金爆×{gold_cnt}")
            if unmaxed_sup_cnt > 0:
                opt_s = f"·养{option_ev_total:.0f}" if (use_option_model and option_cnt > 0) else ""
                if grad_100_cnt > 0:
                    sup_parts.append(f"涨熟×{unmaxed_sup_cnt}(满彩×{grad_100_cnt}){opt_s}")
                else:
                    sup_parts.append(f"涨熟×{unmaxed_sup_cnt}{opt_s}")
            if whistle_milestone_tag:
                sup_parts.append(whistle_milestone_tag)
            if sup_parts:
                reasons.append("+".join(sup_parts))
            if hud_whistle_tag:
                hud_tags.append(hud_whistle_tag)
            elif grad_100_cnt > 0:
                hud_tags.append(f"满彩×{grad_100_cnt}")

        # 3. 队友/经理合训加成（基于 hoopgirls.live 角色配表，结合本局 5 人阵容的主控/队友天赋等级、心结与在场同伴动态精算；
        #    日志 sc_train_result_end 实证：即使 mate_cnt == 0 仅站了经理 is_mgr == True，也 100% 触发队友训练、希雅拉人与佐久间加点！）
        if mate_cnt > 0 or is_mgr:
            mate_total_ev, mate_reason_str = calc_lineup_teammate_ev(
                state=state,
                cmd=cmd,
                stat_ev=stat_ev,
                idx=idx,
                props=props,
                prop_max=prop_max,
                growth_rates=growth_rates,
                w=w,
            )
            score += mate_total_ev
            reasons.append(mate_reason_str)

        # 4. 经理在场 + 哨子同框判定(+1 / +2 / 满熟50%概率+3) + 3/6/9爆阶段性全局被动里程碑 + 溢出保留（无11爆上限，11爆后照常触发老虎机！）
        # 核心机制实证：
        # - 每次经理满 3 格触发爆气事件 (event 1212) 与免费老虎机 (slot_id=63)，max_explosive_cnt=100011 代表全场无爆发次数上限（实测12爆+照常给老虎机属性）；
        # - 满 100 熟 4 破哨子 (词条 11004154, params={"2":50}) 与经理同框时：50% 概率 +3 格、50% 概率 +2 格（期望 +2.5 格）：
        #   * 当 mgr_e == 0 时：50% 概率直爆 (0➔3🔥)，50% 概率停在 2/3 格；
        #   * 当 mgr_e == 1 时：100% 必爆，且 50% 概率余 1 格保留至下一管；
        #   * 当 mgr_e == 2 时：100% 必爆，且 50% 概率余 1 格、50% 概率余 2 格（期望余 1.5 格）。
        # - 阶段性全局被动里程碑（越早达成，后续吃满暴击增益的训练回合越多！）：
        #   * 第 3 爆：额外令随机 2 项训练设施等级直接 +1；
        #   * 第 6~7 爆：永久挂载 state_id=465（训练有金圈/彩圈时，训练主属性基础加值 +3）；
        #   * 第 9 爆：永久挂载 state_id=466（训练有金圈/彩圈时，器材陪伴加成额外 +15%）。
        if is_mgr:
            card_to_cfg = state.get("supactor_card_to_cfg", {})
            whistle_on_cmd = any(
                card_to_cfg.get(sc["card_id"], sc["card_id"]) == 11004
                for sc in (cmd.get("sup_cards") or [])
            )
            eff_mgr_step = whistle_step if whistle_on_cmd else 1
            rem_trains_now = get_remaining_trains(state)
            rem_after = max(0, rem_trains_now - 1)

            # 爆气概率：eff_mgr_step>=3 时为 50% +3 格 / 50% +2 格；eff_mgr_step==2 固定 +2 格；无哨同框固定 +1 格
            if whistle_on_cmd and eff_mgr_step >= 3:
                burst_prob = 0.5 if mgr_e == 0 else 1.0
            elif whistle_on_cmd and eff_mgr_step == 2:
                burst_prob = 1.0 if (mgr_e + 2) >= 3 else 0.0
            else:
                burst_prob = 1.0 if (mgr_e + 1) >= 3 else 0.0

            # 3爆(双设施+1) / 6爆(金彩圈主属+3) / 9爆(金彩圈陪伴+15%) 阶段性全局被动里程碑的前中期复利（按本训之后的剩余训练数折算，第28训rem_after=0自动归零）
            early_boom_comp = min(1.0, max(0.0, rem_after / 19.0))
            hud_ms_short = ""
            if mgr_ex < 3:
                per_bar_milestone = 4.5 * early_boom_comp
                ms_early_unit = (10.0 * early_boom_comp) if mgr_ex == 2 else 0.0
                ms_tag = "·冲3爆升设施" if (mgr_ex == 2 and burst_prob > 0 and rem_after > 0) else ""
                hud_ms_short = "·冲3爆" if ms_tag else ""
            elif mgr_ex < 6:
                per_bar_milestone = 4.0 * early_boom_comp
                ms_early_unit = (10.0 * early_boom_comp) if mgr_ex == 5 else 0.0
                ms_tag = "·冲6爆主属+3" if (mgr_ex == 5 and burst_prob > 0 and rem_after > 0) else ""
                hud_ms_short = "·冲6爆" if ms_tag else ""
            elif mgr_ex < 9:
                per_bar_milestone = 3.0 * early_boom_comp
                ms_early_unit = (8.0 * early_boom_comp) if mgr_ex == 8 else 0.0
                ms_tag = "·冲9爆暴击+15%" if (mgr_ex == 8 and burst_prob > 0 and rem_after > 0) else ""
                hud_ms_short = "·冲9爆" if ms_tag else ""
            else:
                per_bar_milestone = 0.0
                ms_early_unit = 0.0
                ms_tag = ""

            # 每格能量边际守恒势函数：
            # - 前中期 (rem_after >= 5)：溢出能量 100% 保留且后续必能凑满，每 1 格价值严格恒定为 bar_ev，
            #   故 0➔1、1➔2、普通 2➔3 完全平权，仅跨 3/6/9 爆享 ms_early_unit 早鸟被动复利，同框享省回合压缩效率奖；
            # - 收官期 (rem_after < 5)：未凑满 3 格的留存散装能量面临毕业清零风险（第28训 rem_after=0 时未爆能量严格归零，2➔3 救活沉没格数兑现整管老虎机）。
            unit_bar_ev = float(w.get("manager_unit_bar_ev", w.get("manager_no_whistle_0_1_bonus", 22.5)))
            if whistle_step == 1 and not whistle_on_cmd:
                unit_bar_ev += 2.5  # 全卡组无哨子时单格保底微调
            bar_ev = unit_bar_ev + per_bar_milestone
            full_burst_ev = 3.0 * unit_bar_ev + 3.0 * per_bar_milestone + ms_early_unit

            def _stored_bars_potential(bars: int, rem_t: int) -> float:
                b = max(0, min(2, int(bars)))
                if b == 0 or rem_t <= 0:
                    return 0.0
                if rem_t >= 5:
                    return b * bar_ev
                # 末 1~4 训留存 1 格 / 2 格的毕业变现期望系数（按剩余训数内再遇经理/哨子同框凑满 3 格的概率校准）
                reten_tbl = {
                    1: (0.18, 0.65),
                    2: (0.42, 0.82),
                    3: (0.62, 0.92),
                    4: (0.78, 0.97),
                }
                r1, r2 = reten_tbl.get(rem_t, (1.0, 1.0))
                return (1.0 * r1 * bar_ev) if b == 1 else (2.0 * r2 * bar_ev)

            def _marginal_gain_for_step(step_bars: int) -> float:
                tot = mgr_e + step_bars
                base_pot = _stored_bars_potential(mgr_e, rem_after)
                if tot >= 3:
                    return full_burst_ev + _stored_bars_potential(tot - 3, rem_after) - base_pot
                return _stored_bars_potential(tot, rem_after) - base_pot

            save_turn_scale = min(1.0, max(0.0, rem_after / 10.0))
            if whistle_on_cmd and eff_mgr_step >= 2:
                if eff_mgr_step >= 3:
                    sync_save_ev = float(w.get("manager_sync_3bar_save_turn_bonus", 15.0)) * save_turn_scale
                    mgr_w = 0.5 * _marginal_gain_for_step(2) + 0.5 * _marginal_gain_for_step(3) + sync_save_ev
                    if mgr_e == 0:
                        reasons.append(f"🎺👩‍💼同框+2~3(50%🔥爆|0➔2~3){ms_tag}")
                        hud_tags.append(f"同框50%爆{hud_ms_short}")
                    elif mgr_e == 1:
                        reasons.append(f"🎺👩‍💼同框+2~3🔥必爆!(50%余1){ms_tag}")
                        hud_tags.append(f"同框必爆{hud_ms_short}")
                    else:
                        reasons.append(f"🎺👩‍💼同框+2~3🔥必爆!(余1~2){ms_tag}")
                        hud_tags.append(f"同框必爆{hud_ms_short}")
                else:
                    sync_save_ev = float(w.get("manager_sync_2bar_save_turn_bonus", 9.0)) * save_turn_scale
                    mgr_w = _marginal_gain_for_step(2) + sync_save_ev
                    if (mgr_e + 2) >= 3:
                        ov = (mgr_e + 2) - 3
                        ov_s = f"(余{ov})" if ov > 0 else ""
                        reasons.append(f"🎺👩‍💼同框+2🔥必爆!{ov_s}{ms_tag}")
                        hud_tags.append(f"同框必爆{hud_ms_short}")
                    else:
                        reasons.append(f"🎺👩‍💼同框{mgr_e}➔{mgr_e + 2}/3{ms_tag}")
                        hud_tags.append("同框充能")
                score += mgr_w
            else:
                mgr_w = _marginal_gain_for_step(1)
                if mgr_e + 1 >= 3:
                    tag_p = "👩‍💼无哨" if whistle_step >= 2 else "👩‍💼"
                    reasons.append(f"{tag_p}2➔3🔥爆(+1格{ms_tag})")
                    if ms_tag or rem_after <= 4:
                        hud_tags.append(f"满格爆气{hud_ms_short}")
                else:
                    next_e = mgr_e + 1
                    if whistle_step >= 2:
                        reasons.append(f"👩‍💼无哨{mgr_e}➔{next_e}/3")
                    else:
                        reasons.append(f"👩‍💼{mgr_e}➔{next_e}/3")
                score += mgr_w

        # 5. 体力消耗与失败率惩罚（结合全局精力边际标尺、角色59体力被动门槛保护、6号意识满体防溢出与高上限神局博弈模型）
        pre_ps_score = score
        sched_ctx = get_upcoming_schedule_context(state)
        next_heal = sched_ctx.get("weekday_next_turn_heal", 0)
        exp_r_now = sched_ctx.get("exp_round", 1)
        cur_ps = state.get("ps", 130)
        is_last_train = (exp_r_now >= 48) if has_dm else (exp_r_now >= 46 if exp_r_now > 0 else round_num >= 28)
        is_fri_before_sat_dm = (not is_sat_train) and has_dm and (exp_r_now in (5, 12, 19, 26, 33, 40))
        is_week_last_train = True if is_sat_train else (
            (exp_r_now == 46) if has_dm else (exp_r_now in (5, 12, 19, 26, 33, 40, 46))
        )
        lineup_ps_thresh, thresh_label = get_lineup_ps_threshold(state)
        if ps_cost < 0:
            cost_mult = float(w.get("stamina_cost_penalty_per_ps", 0.65))
            if is_last_train:
                cost_mult *= 0.12  # 最后一训后无任何训练，只要不失败即可打空精力
            elif next_heal >= 25 and fail_rate == 0 and (cur_ps + ps_cost + next_heal) >= 85:
                cost_mult *= 0.45  # 下回合训练前必有 +25~+70 回体事件且耗体有助于防下回合回体溢出！
            elif cur_ps >= 118 and fail_rate == 0:
                cost_mult *= 0.72  # 满精力时主动耗体训练把精力槽转化为实打实的属性
            score += ps_cost * cost_mult
            if is_sat_train:
                reasons.append(f"省体{ps_cost}")
            # 若本训扣体后会导致下一训跌破望月透(59体)/克拉丽丝的精力被动门槛，计入下训全队表情Bonus断档损失
            next_ps_est = cur_ps + ps_cost + next_heal
            safe_pots_avail = [
                it for it in state.get("store_items", [])
                if it["id"] in (1001, 1002, 1003, 1201, 1202) and it["num"] > 0 and state.get("gold", 0) >= it["price"]
            ]
            has_safe_pot = bool(safe_pots_avail)
            if lineup_ps_thresh > 0 and not is_week_last_train and cur_ps >= lineup_ps_thresh and next_ps_est < lineup_ps_thresh:
                score -= (4.5 if has_safe_pot else 10.5)
                if not has_safe_pot:
                    hud_tags.append(f"破{lineup_ps_thresh}体被动")
            # 百目鬼周六加训前瞻（纯 EV 计价，绝不写死硬拦）：
            # 实机 19 组低体周六加训样本证实：周六加训耗体 -80%(-4~-6体)，49+体基本 0% 失败，43~48体均值仅 ~7% 失败；
            # 且放弃周六加训改外出/6号意识的净机会成本约为 ~160 EV（非归零 280 EV）。
            # 若商店此刻有买得起的稳定精力药或神之庇佑(1131/1132/1133，2~4天锁0%失败)，风险封顶为对应金币机会成本。
            if is_fri_before_sat_dm and next_ps_est < 49:
                if next_ps_est <= 25:
                    sat_fail_p = 0.55
                elif next_ps_est <= 35:
                    sat_fail_p = 0.35
                elif next_ps_est <= 42:
                    sat_fail_p = 0.18
                else:
                    sat_fail_p = 0.07
                sat_risk_ev = min(95.0, 160.0 * sat_fail_p)
                safe_shields_avail = [
                    it for it in state.get("store_items", [])
                    if it["id"] in (1131, 1132, 1133) and it["num"] > 0 and state.get("gold", 0) >= it["price"]
                ]
                sat_mitigators = safe_pots_avail + safe_shields_avail
                if sat_mitigators:
                    cheapest_mit = min(sat_mitigators, key=lambda it: it["price"])
                    g_now = float(state.get("gold", 0) or 0)
                    mit_opp = max(8.0, calc_keep_gold_ev(g_now, state) - calc_keep_gold_ev(max(0.0, g_now - cheapest_mit["price"]), state))
                    sat_risk_ev = min(sat_risk_ev, mit_opp)
                if sat_risk_ev >= 8.0:
                    score -= sat_risk_ev
                    reasons.append(f"周六加训低体风险-{sat_risk_ev:.0f}")
                    if sat_risk_ev >= 45.0:
                        hud_tags.append(f"周六险-{sat_risk_ev:.0f}")
        elif ps_cost > 0:
            # 6号·意识训练：严格按 ps_max - cur_ps 截断溢出回体！
            ps_cap_tr = get_ps_max(state)
            eff_heal = min(ps_cost, max(0, ps_cap_tr - cur_ps))
            if is_last_train:
                score += eff_heal * 0.10
            elif is_sat_train:
                unit_h = get_stamina_unit_ev(state, ps_override=cur_ps)
                score += eff_heal * unit_h
            else:
                unit_h = get_stamina_unit_ev(state, ps_override=cur_ps)
                score += eff_heal * unit_h
                # 若当前已满体(>=116)或下回合有大回体事件即将溢出，点不耗体的6号意识等于白白憋死体力槽，扣除憋体机会成本；
                # 若当前恰处于 55~58 体(低于望月59体线)，点6号意识(+4体)可直接把下训拉回 >=59 体重新激活望月+5%表情被动！
                if cur_ps >= 118 or (next_heal >= 30 and cur_ps + next_heal >= 128):
                    score -= 16.0
                elif cur_ps <= 43 and next_heal == 0 and not is_week_last_train:
                    score -= 10.0
                elif lineup_ps_thresh > 0 and cur_ps < lineup_ps_thresh <= (cur_ps + eff_heal) and next_heal == 0 and not is_week_last_train:
                    score += 11.0
                    hud_tags.append(f"回{lineup_ps_thresh}体保被动")
                elif 44 <= cur_ps <= 58 and next_heal == 0:
                    score += 6.0
            if eff_heal < ps_cost:
                reasons.append(f"回体+{ps_cost}(实+{eff_heal})")
                hud_tags.append(f"实回+{eff_heal}体")
            else:
                reasons.append(f"回体+{ps_cost}")

        raw_zero_fail_score = round(score, 1)
        non_ps_score = pre_ps_score
        if fail_rate > 0:
            p_fail = min(1.0, fail_rate / 100.0)
            if fail_rate >= rc["hard_reject_fail_rate"] or fail_rate >= 35:
                # 高危失败率 (>=35%，尤其 80%~100% 必败局)：按真实失败率折算并硬封顶一票否决，绝不让 100% 失败的大彩圈排第一！
                score = min(15.0, score * max(0.0, 1.0 - p_fail) - 120.0 - fail_rate * 1.5)
                non_ps_score = min(15.0, non_ps_score * max(0.0, 1.0 - p_fail) - 120.0 - fail_rate * 1.5)
                reasons.append(f"⚠️高危失败率{fail_rate}%")
                hud_tags.append(f"⚠️高危{fail_rate}%")
            elif fail_rate > rc["max_acceptable_fail_rate"]:
                # 中度失败率 (16%~34%)：160~190 线性平滑过渡，保留足够连锁续航惩罚防止低体强赌大彩圈翻车
                hr_blend = max(0.0, min(1.0, (score - 160.0) / 30.0)) if fail_rate <= 26 else 0.0
                loss_mult = 0.95 - 0.20 * hr_blend
                base_flat = float(rc["fail_rate_penalty_per_pct"])
                flat_rate = base_flat - (base_flat - 2.2) * hr_blend
                expected_loss = score * p_fail * loss_mult
                flat_pen = fail_rate * flat_rate
                score -= (expected_loss + flat_pen)
                non_ps_score -= (non_ps_score * p_fail * loss_mult + flat_pen)
                is_high_roll_tag = hr_blend >= 0.5
                reasons.append(f"🎲博上限失败率{fail_rate}%" if is_high_roll_tag else f"⚠️失败率{fail_rate}%")
                hud_tags.append(f"🎲博上限{fail_rate}%" if is_high_roll_tag else f"⚠️失败{fail_rate}%")
            else:
                hr_blend = max(0.0, min(1.0, (score - 160.0) / 30.0))
                flat_rate = 1.40 - 0.35 * hr_blend
                loss_mult = 0.65 - 0.15 * hr_blend
                pen = fail_rate * flat_rate + score * p_fail * loss_mult
                score -= pen
                non_ps_score -= (fail_rate * flat_rate + non_ps_score * p_fail * loss_mult)
                reasons.append(f"失败率{fail_rate}%")

        scored.append({
            "idx": idx,
            "name": TRAIN_NAMES.get(idx, f"{idx}号训练"),
            "score": round(score, 1),
            "non_ps_score": round(non_ps_score, 1),
            "raw_zero_fail_score": raw_zero_fail_score,
            "p5_fac_ev": p5_fac_ev,
            "cmd": cmd,
            "reasons": " | ".join(reasons),
            "hud_tags": hud_tags[:4],
        })

    # 6. 评估『💤平时休息(退回主界面点休息)』(education.cs_decide {decide_type = 2}) 作为第 7 个同台候选项（仅平时训练可用）
    if state.get("rest_status", True) and not is_sat_train:
        ps = state.get("ps", 130)
        sched_ctx = get_upcoming_schedule_context(state)
        next_heal = sched_ctx.get("weekday_next_turn_heal", 0)
        exp_r_now = sched_ctx.get("exp_round", 1)
        exp_gain = int(w.get("weekday_rest_expected_ps_gain", 75))
        ps_unit = float(w.get("weekday_rest_ps_unit_ev", 0.80))
        # 若下回合训练前必有固定/排队回体事件(如R10集训+30、R24合宿+25、ID70+40、ID71+70)，平时休息的有效不溢出体力需扣除即将到来的免费/三选一回体！
        ps_cap_rest = get_ps_max(state)
        effective_cap = max(65, ps_cap_rest - int(next_heal * 0.75))
        eff_ps = min(exp_gain, max(0, effective_cap - ps))
        over_ps = max(0, exp_gain - eff_ps)
        rest_score = eff_ps * ps_unit
        is_week_last_r = exp_r_now in (5, 12, 19, 26, 33, 40, 46)
        high_fail_cnt = sum(1 for c in cmds if c["idx"] != 6 and c["fail_rate"] >= 15)
        is_last_r = (exp_r_now >= 48) if has_dm else (exp_r_now >= 46 if exp_r_now > 0 else round_num >= 28)
        if is_last_r:
            rest_score = -99.0  # 最后一训绝不休息
        elif ps >= 75:
            # 精力充足(>=75，训练0%失败率)时休息会白白浪费一回合且严重溢出，施加高体机会成本扣分
            rest_score -= (ps - 70) * 0.85
        elif ps <= 44 and next_heal == 0 and not is_week_last_r and high_fail_cnt >= 4:
            # 周中残体(<=44体，1~5号全红高危)且非本周最后一训：及时休息一次回满，防止硬点6号意识(+4体)导致次日依然无法点彩圈/同框
            rest_score += 9.0

        best_train = max(scored, key=lambda x: x["score"]) if scored else None
        rest_reasons = [f"⚡精力+70~80({ps}➔{min(ps_cap_rest, ps + exp_gain)})"]
        rest_hud_tags = [f"有效+{eff_ps}体" if (over_ps > 0 and ps < 85) else f"回体+{exp_gain}"]
        if next_heal > 0:
            rest_reasons.append(f"下回合有+{next_heal}体事件，无需提前休息")
            rest_hud_tags.append(f"下训有+{next_heal}体")
        elif over_ps > 0 and ps < 85:
            rest_reasons.append(f"有效+{eff_ps}体")
        if high_fail_cnt >= 3 and best_train and best_train["score"] < rest_score:
            rest_reasons.append(f"1~5号高危失败率，攒体下回合爆发优于低效{best_train['idx']}号({best_train['score']:.1f})")
            rest_hud_tags.append("攒体防炸")
        elif ps <= 55 and next_heal == 0:
            rest_reasons.append("低体安全蓄力防炸训练")
        else:
            rest_reasons.append("当前精力充足无需休息")

        scored.append({
            "idx": 0,
            "name": TRAIN_NAMES[0],
            "score": round(rest_score, 1),
            "cmd": {"idx": 0, "gains": {}, "total_stat_gain": 0, "ps": exp_gain, "fail_rate": 0},
            "reasons": " | ".join(rest_reasons),
            "hud_tags": rest_hud_tags[:3],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # 专项特化双轨对照：计算纯总评基准分，若特化改写了 #1 推荐，透明标注总评 EV 让利与原总评 #1
    if focus_st and not (isinstance(strategy, dict) and "_focus_override" in strategy):
        base_strat = dict(strategy)
        base_strat["_focus_override"] = (None, 1.0)
        base_scored = score_training_turn(state, cmds, base_strat)
        base_map = {item["idx"]: item["score"] for item in base_scored}
        for item in scored:
            b_sc = base_map.get(item["idx"], item["score"])
            item["base_score"] = b_sc
            item["focus_delta"] = round(item["score"] - b_sc, 1)
        if scored and base_scored and scored[0]["idx"] != base_scored[0]["idx"]:
            f_short = STAT_NAMES.get(focus_st, "特化")
            top_item = scored[0]
            orig_top = base_scored[0]
            base_loss = max(0.0, round(orig_top["score"] - base_map.get(top_item["idx"], 0.0), 1))
            f_tag = f"🎯{f_short}特化(评-{base_loss:.0f})" if base_loss >= 1.0 else f"🎯{f_short}优先"
            top_item["focus_promoted"] = True
            top_item["focus_base_loss"] = base_loss
            top_item["hud_tags"] = ([f_tag] + list(top_item.get("hud_tags") or []))[:4]
            top_item["reasons"] = f"{f_tag} | {top_item['reasons']}"
            for cand in scored[1:]:
                if cand["idx"] == orig_top["idx"]:
                    cand["is_orig_top"] = True
                    cand["hud_tags"] = (["原评#1"] + list(cand.get("hud_tags") or []))[:4]
                    cand["reasons"] = f"原评#1 | {cand['reasons']}"
                    break

    return scored


def make_initial_state():
    return {
        "props": {101: 200, 102: 200, 103: 200, 104: 200, 105: 200, 106: 200},
        "prop_max": {101: 1575, 102: 1800, 103: 1575, 104: 1625, 105: 1615, 106: 1575},
        "growth_rates": {101: 0, 102: 0, 103: 0, 104: 0, 105: 0, 106: 0},
        "focus_levels": {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1},
        "base_focus_levels": {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1},
        "train_counts": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
        "train_up_need": {1: 3, 2: 3, 3: 3, 4: 3, 5: 3, 6: 3},
        "rank": 0,
        "ps": 130,
        "ps_max": 130,
        "gold": 0,
        "round": 1,
        "exp_round": 1,
        "left_round": 7,
        "round_type": 1,
        "rest_status": True,
        "weekend_day": 0,  # 0=平时, 1=周六(周末第1动), 2=周日(周末第2动)
        "fired_70": False,
        "fired_71": False,
        "mgr_energy": 0,
        "mgr_explosive": 0,
        "mgr_max_explosive": 99,
        "active_states": set(),
        "state_rounds": {},
        "active_gold_task": None,
        "cur_story": ("unknown", "未知事件"),
        "store_open": False,
        "store_items": [],
        "weekend_selections": {},
        "weekend_teammates": {},
        "weekend_special": {},
        "gashapon_list": [],
        "gasha_weights": {1: 39, 2: 30, 3: 25, 4: 5},
        "supactor_trammel": {},
        "supactor_card_trammel": {},
        "supactor_card_to_cfg": {},
        "whistle_awake_level": 3,
        "whistle_break_level": 4,
        "whistle_effects": set(),
        "note_stat_sum": 175,
        "note_cap_sum": 161,
        "minigame_list": [],
        "main_char_id": None,
        "teammate_char_ids": [],
        "char_talents": {},
        "train_with_team_cnt": 0,
        "weekend_train": False,
        "weekend_train_cmds": [],
        # 原生 UI 场景与叠层视图状态机字段
        "ycjm_scene": "",
        "is_beach_scene": False,
        "active_ui_views": set(),
        "in_story_choice": False,
        "cur_skill_choices": [],
        "learned_skills": set(),
        "train_anim_lock": False,
        "edu_state_id": 0,
        "weekend_map_open": False,
        "travel_sub_scene": "",
        "pre_edu_char_id": None,
    }


def clone_state_snapshot(state: dict) -> dict:
    """深拷贝当前育成状态快照，确保平时训练、周末出行与剧情事件复盘拥有 100% 完整的上下文。"""
    return {
        "props": dict(state.get("props", {})),
        "prop_max": dict(state.get("prop_max", {})),
        "growth_rates": dict(state.get("growth_rates", {})),
        "focus_levels": dict(state.get("focus_levels", {})),
        "base_focus_levels": dict(state.get("base_focus_levels", state.get("focus_levels", {}))),
        "train_counts": dict(state.get("train_counts", {})),
        "train_up_need": dict(state.get("train_up_need", {})),
        "rank": state.get("rank", 0),
        "ps": state.get("ps", 130),
        "ps_max": get_ps_max(state),
        "gold": state.get("gold", 0),
        "round": state.get("round", 1),
        "exp_round": state.get("exp_round", 1),
        "left_round": state.get("left_round", 7),
        "round_type": state.get("round_type", 1),
        "rest_status": state.get("rest_status", True),
        "weekend_day": state.get("weekend_day", 0),
        "fired_70": state.get("fired_70", False),
        "fired_71": state.get("fired_71", False),
        "mgr_energy": state.get("mgr_energy", 0),
        "mgr_explosive": state.get("mgr_explosive", 0),
        "mgr_max_explosive": state.get("mgr_max_explosive", 99),
        "active_states": set(state.get("active_states", set())),
        "state_rounds": dict(state.get("state_rounds", {})),
        "cur_story": state.get("cur_story", ("unknown", "未知事件")),
        "store_open": state.get("store_open", False),
        "store_items": [dict(x) for x in state.get("store_items", [])],
        "weekend_selections": {k: dict(v) for k, v in state.get("weekend_selections", {}).items()},
        "weekend_teammates": dict(state.get("weekend_teammates", {})),
        "weekend_special": dict(state.get("weekend_special", {})),
        "gashapon_list": [dict(x) for x in state.get("gashapon_list", [])],
        "gasha_weights": dict(state.get("gasha_weights", {})),
        "supactor_trammel": dict(state.get("supactor_trammel", {})),
        "supactor_card_trammel": dict(state.get("supactor_card_trammel", {})),
        "supactor_card_to_cfg": dict(state.get("supactor_card_to_cfg", {})),
        "whistle_awake_level": state.get("whistle_awake_level", 3),
        "whistle_break_level": state.get("whistle_break_level", 4),
        "whistle_effects": set(state.get("whistle_effects", set())),
        "note_stat_sum": state.get("note_stat_sum", 175),
        "note_cap_sum": state.get("note_cap_sum", 161),
        "minigame_list": [dict(x) for x in state.get("minigame_list", [])],
        "main_char_id": state.get("main_char_id"),
        "teammate_char_ids": list(state.get("teammate_char_ids", [])),
        "char_talents": {k: dict(v) for k, v in state.get("char_talents", {}).items()},
        "train_with_team_cnt": state.get("train_with_team_cnt", 0),
        "weekend_train": bool(state.get("weekend_train", False)),
        "weekend_train_cmds": [
            {
                **c,
                "gains": dict(c.get("gains", {})),
                "sup_cards": [dict(sc) for sc in c.get("sup_cards", [])],
                "mate_ids": list(c.get("mate_ids", [])),
            }
            for c in state.get("weekend_train_cmds", [])
        ],
        "ycjm_scene": state.get("ycjm_scene", ""),
        "is_beach_scene": bool(state.get("is_beach_scene", False)),
        "active_ui_views": set(state.get("active_ui_views", set())),
        "in_story_choice": bool(state.get("in_story_choice", False)),
        "cur_skill_choices": [dict(x) for x in state.get("cur_skill_choices", [])],
        "learned_skills": set(state.get("learned_skills", set())),
        "train_anim_lock": bool(state.get("train_anim_lock", False)),
        "edu_state_id": int(state.get("edu_state_id", 0) or 0),
        "weekend_map_open": bool(state.get("weekend_map_open", False)),
        "travel_sub_scene": state.get("travel_sub_scene", ""),
        "pre_edu_char_id": state.get("pre_edu_char_id"),
    }


def update_state_from_token(state: dict, tok: str):
    """从单个 EDU_MODEL 更新育成数据块中提取最新属性、成长率、设施训练次数、经理能量、金币、支援卡熟练度(trammel)、笔记词条、商店货架与周末建筑/扭蛋数据。"""
    em = re.search(
        r"edu_manager\s*=\s*\{energy_cnt\s*=\s*(\d+),explosive_cnt\s*=\s*(\d+),max_explosive_cnt\s*=\s*(\d+)\}",
        tok,
    )
    if em:
        state["mgr_energy"] = int(em.group(1))
        state["mgr_explosive"] = int(em.group(2))
        raw_max = int(em.group(3))
        state["mgr_max_explosive"] = 99 if raw_max >= 1000 else raw_max

    for sid_m in re.finditer(r"operating_status\s*=\s*(\d+),\s*round_num\s*=\s*(\d+),\s*state_id\s*=\s*(\d+)", tok):
        op_st, r_num, sid = int(sid_m.group(1)), int(sid_m.group(2)), int(sid_m.group(3))
        if r_num > 0 and op_st in (1, 3):
            state["active_states"].add(sid)
            state.setdefault("state_rounds", {})[sid] = r_num
        else:
            state["active_states"].discard(sid)
            state.get("state_rounds", {}).pop(sid, None)

    # 提取角色心愿攒钱挑战任务 (龙宫寺心叶 task_cfg_id: 113=200金, 114=350金, 115=500金, 116=650金；固定于 R31 第5周周三验收)
    if "task_list" in tok:
        if re.search(r"task_list\s*=\s*\{\s*\}", tok):
            state["active_gold_task"] = None
        else:
            _gold_task_targets = {113: 200, 114: 350, 115: 500, 116: 650}
            _tm = re.search(
                r"left_round\s*=\s*(\d+),\s*task_cfg_id\s*=\s*(\d+),\s*task_progress\s*=\s*(\d+),\s*task_status\s*=\s*(\d+)",
                tok,
            )
            if _tm:
                _lr, _tid, _tprog, _tst = int(_tm.group(1)), int(_tm.group(2)), int(_tm.group(3)), int(_tm.group(4))
                if _tid in _gold_task_targets:
                    if _tst == 1 and _lr > 0:
                        state["active_gold_task"] = {
                            "id": _tid,
                            "target": _gold_task_targets[_tid],
                            "progress": _tprog,
                            "left_round": _lr,
                            "deadline_round": 31,
                            "status": _tst,
                        }
                    else:
                        state["active_gold_task"] = None

    # 提取 6 项训练当前设施等级 (focus_levels)、级内次数 (train_counts) 与升下一级步长 (train_up_need_counts)
    flm = re.search(r"focus_levels\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", tok, re.S)
    if flm:
        _erm_fl = re.search(r"\bexperience_round\s*=\s*(\d+)", tok)
        _er_fl = int(_erm_fl.group(1)) if _erm_fl else infer_exp_round(state)
        _in_beach_fl = 22 <= _er_fl <= 26
        parsed_fl = {}
        for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", flm.group(1)):
            k, v = int(kv.group(1)), int(kv.group(2))
            if 1 <= k <= 6 and 1 <= v <= 5:
                state["focus_levels"][k] = v
                parsed_fl[k] = v
        # 非海滩周同步记录真实基础设施等级，供海滩 R22~R26 评估海滩后常规收割复利
        if parsed_fl and not _in_beach_fl:
            base_fl = state.setdefault("base_focus_levels", {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1})
            for k, v in parsed_fl.items():
                base_fl[k] = v

    tcm = re.search(r"train_counts\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", tok, re.S)
    if tcm:
        has_fl = bool(flm) or any(lv > 1 for lv in state.get("focus_levels", {}).values())
        for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", tcm.group(1)):
            k, v = int(kv.group(1)), int(kv.group(2))
            if 1 <= k <= 6 and 0 <= v <= 60:
                if has_fl:
                    state["train_counts"][k] = v
                else:
                    state["train_counts"][k] = max(state["train_counts"].get(k, 0), v)

    tum = re.search(r"train_up_need_counts\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", tok, re.S)
    if tum:
        for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", tum.group(1)):
            k, v = int(kv.group(1)), int(kv.group(2))
            if 1 <= k <= 6 and 1 <= v <= 10:
                state["train_up_need"][k] = v

    # 提取支援卡/器材熟练度与星级/突破 (supactor_dict / supactor_borrow_info / edu_common_effect_info 双重映射，兼容增量更新与中途续玩)
    if "supactor_dict" in tok or "supactor_borrow_info" in tok or "supactor_data" in tok:
        for awk_m in re.finditer(
            r"awake_level\s*=\s*(\d+),\s*break_level\s*=\s*(\d+),\s*card_id\s*=\s*(\d+),\s*cfg_id\s*=\s*(\d+)",
            tok,
        ):
            awk_lv, brk_lv, cid, cfg_id = (
                int(awk_m.group(1)),
                int(awk_m.group(2)),
                int(awk_m.group(3)),
                int(awk_m.group(4)),
            )
            state["supactor_card_to_cfg"][cid] = cfg_id
            if cfg_id == 11004:
                state["whistle_awake_level"] = awk_lv
                state["whistle_break_level"] = brk_lv
                state["supactor_trammel"].setdefault(11004, 0)
        for cm_cfg in re.finditer(r"card_id\s*=\s*(\d+),\s*cfg_id\s*=\s*(\d+)", tok):
            cid, cfg_id = int(cm_cfg.group(1)), int(cm_cfg.group(2))
            state["supactor_card_to_cfg"][cid] = cfg_id
            if cfg_id == 11004:
                state["supactor_trammel"].setdefault(11004, 0)
        for sm_c in re.finditer(r"card_id\s*=\s*(\d+),(?:[^{}]|\{[^{}]*\})*?trammel\s*=\s*(\d+)", tok, re.S):
            card_id, tram = int(sm_c.group(1)), int(sm_c.group(2))
            state["supactor_card_trammel"][card_id] = tram
            cfg_id = state["supactor_card_to_cfg"].get(card_id, card_id)
            state["supactor_trammel"][cfg_id] = tram

    # 从 edu_common_effect_info 中反推哨子(11004)的 card_id、觉醒星级(10109..10111)与满破三倍经验词条(11004154)
    if "edu_common_effect_info" in tok and ("11004" in tok or "10109" in tok or "10110" in tok or "10111" in tok):
        for ef_m in re.finditer(
            r"effect_cfg_id\s*=\s*(11004\d+|10109|10110|10111)\b.*?source_data\s*=\s*\{source_id\s*=\s*(\d+),\s*source_type\s*=\s*1\}",
            tok,
            re.S,
        ):
            ef_id, src_cid = int(ef_m.group(1)), int(ef_m.group(2))
            state["supactor_card_to_cfg"][src_cid] = 11004
            state.setdefault("whistle_effects", set()).add(ef_id)
            if src_cid in state["supactor_card_trammel"]:
                state["supactor_trammel"][11004] = max(
                    state["supactor_trammel"].get(11004, 0),
                    state["supactor_card_trammel"][src_cid],
                )
            else:
                state["supactor_trammel"].setdefault(11004, 0)
            if ef_id == 10111:
                state["whistle_awake_level"] = 3
            elif ef_id == 10110:
                state["whistle_awake_level"] = max(state.get("whistle_awake_level", 0), 2)
            elif ef_id == 10109:
                state["whistle_awake_level"] = max(state.get("whistle_awake_level", 0), 1)
            elif ef_id == 11004154:
                state["whistle_break_level"] = 4

    if "company_supactor_tbl" in tok:
        for sup_blk in re.finditer(r"company_supactor_tbl\s*=\s*\{([^}]*)\}", tok):
            for r_m in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*([1-9]\d*)", sup_blk.group(1)):
                cid_r = int(r_m.group(1))
                val_r = int(r_m.group(2))
                cfg_id_r = state["supactor_card_to_cfg"].get(cid_r, cid_r)
                if val_r >= 2:
                    # value=2 为 100 满熟练度彩爆
                    state["supactor_card_trammel"][cid_r] = 100
                    state["supactor_trammel"][cfg_id_r] = 100
                elif val_r == 1:
                    # value=1 为 70~99 熟练度金爆（绝不可覆写为 100，仅在中途续玩未知熟练度时兜底 >=70）
                    cur_c = state["supactor_card_trammel"].get(cid_r, 0)
                    if cur_c < 70:
                        state["supactor_card_trammel"][cid_r] = 70
                    if cfg_id_r in state["supactor_trammel"] and state["supactor_trammel"][cfg_id_r] < 70:
                        state["supactor_trammel"][cfg_id_r] = 70

    # 提取开局携带的 3 本笔记属性与上限词条总和 (用于精确计算 ID:82『直接计算一次笔记』彩蛋的本局真值)
    if "edu_note_list" in tok and "entry_type" in tok:
        st_sum = 0
        cap_sum = 0
        for em_n in re.finditer(
            r"entry_type\s*=\s*([12]),\s*entry_value\s*=\s*\{property_id\s*=\s*\d+,\s*property_value\s*=\s*(\d+)\}",
            tok,
        ):
            etype, eval_v = int(em_n.group(1)), int(em_n.group(2))
            if etype == 1:
                st_sum += eval_v
            elif etype == 2:
                cap_sum += eval_v
        if st_sum > 0 or cap_sum > 0:
            state["note_stat_sum"] = st_sum
            state["note_cap_sum"] = cap_sum

    # 提取主角角色 ID、天赋等级、六维属性与原生属性成长率（兼容 6 维多行全量刷新与单维局部刷新，排除队友的 team_mate_map）
    im = re.search(r"(?:^|\s)initactor\s*=\s*\{(.*?)(?=\n  \w+\s*=|\n\})", tok, re.M | re.S)
    if im:
        init_blk = im.group(1)
        cid_m = re.search(r"\bcfg_id\s*=\s*(\d+)", init_blk)
        if cid_m:
            mcid = int(cid_m.group(1))
            if mcid in CHARACTER_DB or mcid >= 1000:
                state["main_char_id"] = mcid
                t_m = re.search(r"talent_tbl\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", init_blk, re.S)
                if t_m:
                    tmap = state["char_talents"].setdefault(mcid, {})
                    for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", t_m.group(1)):
                        tmap[int(kv.group(1))] = int(kv.group(2))

    # 提取 4 名陪练队友的角色 ID 与各自天赋/心结等级 (team_mate_map)
    if "team_mate_map" in tok and "team_mate_map = {}" not in tok:
        tm_m = re.search(r"team_mate_map\s*=\s*\{(.*?)\n    \}", tok, re.S)
        if tm_m:
            tm_blk = tm_m.group(1)
            found_tm_ids = []
            for entry_m in re.finditer(
                r"cfg_id\s*=\s*(\d+).*?talent_tbl\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}",
                tm_blk,
                re.S,
            ):
                tcid = int(entry_m.group(1))
                if tcid not in found_tm_ids:
                    found_tm_ids.append(tcid)
                tmap = state["char_talents"].setdefault(tcid, {})
                for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", entry_m.group(2)):
                    tmap[int(kv.group(1))] = int(kv.group(2))
            if found_tm_ids:
                state["teammate_char_ids"] = found_tm_ids

    twm = re.search(r"\btrain_with_team_cnt\s*=\s*(\d+)", tok)
    if twm:
        state["train_with_team_cnt"] = int(twm.group(1))

    sub = im.group(1) if im else (tok if "team_mate_map" not in tok and "company_teammate_tbl" not in tok else "")
    if sub:
        ptm = re.search(r"property_tbl\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", sub, re.S)
        if ptm:
            for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", ptm.group(1)):
                k, v = int(kv.group(1)), int(kv.group(2))
                if 101 <= k <= 106:
                    state["props"][k] = v

        grm = re.search(r"property_growth_rate_tbl\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", sub, re.S)
        if grm:
            for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", grm.group(1)):
                k, v = int(kv.group(1)), int(kv.group(2))
                if 101 <= k <= 106:
                    state["growth_rates"][k] = v

    mm = re.search(r"property_max_tbl\s*=\s*\{((?:\s*(?:\[\d+\]\s*=\s*)?\{[^}]*\},?)+)\s*\}", tok, re.S)
    if mm:
        for kv in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", mm.group(1)):
            k, v = int(kv.group(1)), int(kv.group(2))
            if 101 <= k <= 106:
                state["prop_max"][k] = v

    rm = re.search(r"\brank\s*=\s*(\d+)", tok)
    if rm and "team_mate_map" not in tok:
        state["rank"] = int(rm.group(1))

    # 匹配独立 ps 更新（如 {ps = 120} 或多行块顶层 2 空格缩进的 ps = 65），排除 train_cmds 内部的 6 空格 ps = 4
    psm = re.search(r"(?:^  ps\s*=\s*(\d+)|\{\s*ps\s*=\s*(\d+)\s*\})", tok, re.M)
    if psm:
        val_str = psm.group(1) or psm.group(2)
        state["ps"] = int(val_str)

    psmax_m = re.search(r"\bps_max\s*=\s*(\d+)", tok)
    if psmax_m:
        pm_val = int(psmax_m.group(1))
        if 100 <= pm_val <= 300:
            state["ps_max"] = pm_val

    # 提取 1..49 真实日程回合 (experience_round) 与距下一场比赛剩余回合 (left_round)
    erm = re.search(r"\bexperience_round\s*=\s*(\d+)", tok)
    if erm:
        exp_r_new = int(erm.group(1))
        state["exp_round"] = exp_r_new
        if exp_r_new >= 49:
            # 毕业决赛日 (R49)：强制清除末周周六(R48，含百目鬼周六加训)残留的周末出行状态
            state["weekend_day"] = 0
            state["weekend_train"] = False
            state["weekend_train_cmds"] = []
            state["weekend_map_open"] = False
            state["travel_sub_scene"] = ""
            if state.get("round_type") == 2:
                state["round_type"] = 0
        elif state.get("round_type") == 2 and exp_r_new > 0 and (exp_r_new % 7 in (0, 6)):
            state["weekend_day"] = 2 if (exp_r_new % 7 == 0) else 1
            if state["weekend_day"] == 2:
                state["weekend_train"] = False
                state["weekend_train_cmds"] = []

    lrm = re.search(r"\bleft_round\s*=\s*(\d+)", tok)
    if lrm:
        state["left_round"] = int(lrm.group(1))
        if state["left_round"] == 0 and int(state.get("exp_round", 0) or 0) >= 49:
            state["weekend_train"] = False
            state["weekend_train_cmds"] = []
            if state.get("round_type") == 2:
                state["round_type"] = 0

    # 提取当前持有金币 (currency_list 中 key = 1)
    cm = re.search(r"currency_list\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}", tok)
    if cm:
        state["gold"] = int(cm.group(1))

    rsm = re.search(r"\brest_status\s*=\s*(true|false)", tok)
    if rsm:
        state["rest_status"] = rsm.group(1) == "true"

    rtm = re.search(r"\bround_type\s*=\s*(\d+)", tok)
    if rtm:
        rt_val = int(rtm.group(1))
        state["round_type"] = rt_val
        if rt_val == 1:
            state["weekend_day"] = 0
            state["weekend_train"] = False
            state["weekend_train_cmds"] = []
        elif rt_val == 2:
            exp_r_cur = int(state.get("exp_round", 0) or 0)
            if exp_r_cur > 0 and (exp_r_cur % 7 in (0, 6)):
                state["weekend_day"] = 2 if (exp_r_cur % 7 == 0) else 1
            elif "building_selection_list" in tok and "building_selection_list = {}" not in tok:
                state["weekend_day"] = min(2, state.get("weekend_day", 0) + 1)
            elif state.get("weekend_day", 0) == 0:
                state["weekend_day"] = 1
            if state.get("weekend_day") == 2:
                state["weekend_train"] = False
                state["weekend_train_cmds"] = []

    # 提取商店货架商品 (edu_store_info)
    if "edu_store_info" in tok and "items_data" in tok:
        op_m = re.search(r"edu_store_open_status\s*=\s*(true|false)", tok)
        if op_m:
            state["store_open"] = op_m.group(1) == "true"
        items_m = re.search(r"items_data\s*=\s*\{(.*?)\}\s*,\s*(?:store_common_effect_list|weight_distribution)", tok, re.S)
        if not items_m:
            items_m = re.search(r"items_data\s*=\s*\{(.*?)\n    \}", tok, re.S)
        if items_m:
            store_items = []
            for im_item in re.finditer(
                r"edu_item_id\s*=\s*(\d+),\s*edu_item_num\s*=\s*(\d+),\s*edu_item_price\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}",
                items_m.group(1),
            ):
                iid, inum, iprice = int(im_item.group(1)), int(im_item.group(2)), int(im_item.group(3))
                store_items.append({"id": iid, "num": inum, "price": iprice})
            state["store_items"] = store_items

    # 提取周末队友驻留分布 (weekend_teammate_activity)
    if "weekend_teammate_activity" in tok and "weekend_teammate_activity = {}" not in tok:
        wt_m = re.search(r"weekend_teammate_activity\s*=\s*\{(.*?)\}\s*,\s*work_cnt", tok, re.S)
        if wt_m:
            w_mates = {}
            for blk in re.finditer(r"selection_cfg_id\s*=\s*(\d+),\s*teammate_data_list\s*=\s*\{(.*?)\}\s*\}", wt_m.group(1), re.S):
                sel_id = int(blk.group(1))
                cnt = len(re.findall(r"teammate_cfg_id\s*=\s*\d+", blk.group(2)))
                if cnt > 0:
                    w_mates[sel_id] = cnt
            state["weekend_teammates"] = w_mates
    elif "weekend_teammate_activity = {}" in tok:
        state["weekend_teammates"] = {}

    # 提取周末建筑分支详情 (building_selection_list) 与周末单次特殊活动 (edu_activity_data)
    if "building_selection_list" in tok and "building_selection_list = {}" not in tok:
        if "team_building_list" not in tok:
            state["gashapon_list"] = []
        if "edu_mini_games_state_data" not in tok:
            state["minigame_list"] = []
        bs_m = re.search(r"building_selection_list\s*=\s*\{(.*?)\}\s*,\s*edu_activity_data", tok, re.S)
        if bs_m:
            sels = {}
            for sm_blk in re.finditer(
                r"selection_consume_ps\s*=\s*(-?\d+).*?selection_id\s*=\s*(\d+),\s*selection_status\s*=\s*(\d+),\s*work_data\s*=\s*\{(.*?)\n      \}",
                bs_m.group(1),
                re.S,
            ):
                ps_cost = int(sm_blk.group(1))
                sel_id = int(sm_blk.group(2))
                status = int(sm_blk.group(3))
                w_raw = sm_blk.group(4) or ""
                base_gold = 0
                tips_gold = 0
                w_lvl = 1
                bm_g = re.search(r"all_work_currency\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}", w_raw)
                if bm_g:
                    base_gold = int(bm_g.group(1))
                tm_g = re.search(r"work_tips\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}", w_raw)
                if tm_g:
                    tips_gold = int(tm_g.group(1))
                lm_g = re.search(r"work_level\s*=\s*(\d+)", w_raw)
                if lm_g:
                    w_lvl = int(lm_g.group(1))
                # status=1 为常规可用，status=2(8折特价)/3(特级机台或散心双倍)/4(打工急招2倍) 均为周末单次特殊活动激活态！
                if status > 0:
                    sels[sel_id] = {
                        "sel_id": sel_id,
                        "status": status,
                        "is_urgent": status == 4,
                        "ps_cost": ps_cost,
                        "base_gold": base_gold,
                        "tips_gold": tips_gold,
                        "total_gold": base_gold + tips_gold,
                        "work_level": w_lvl,
                    }
            state["weekend_selections"] = sels

    if "edu_activity_data" in tok:
        ead_m = re.search(
            r"edu_activity_data\s*=\s*\{\s*edu_common_effect_list\s*=\s*\{.*?\}(?:,\s*selection_cfg_id\s*=\s*(\d+),\s*special_activity_id\s*=\s*(\d+))?\s*\}",
            tok,
            re.S,
        )
        if ead_m:
            if ead_m.group(1) and ead_m.group(2):
                state["weekend_special"] = {
                    "sel_id": int(ead_m.group(1)),
                    "act_id": int(ead_m.group(2)),
                }
            else:
                state["weekend_special"] = {}

    # 提取周末 6号建筑·扭蛋机三选一池与档位刷新权重 (edu_team_building_data，精准锚定 team_building_refresh_cnt 后的 weight_distribution，防与商店权重混淆)
    if "team_building_list" in tok:
        wd_m = re.search(
            r"team_building_refresh_cnt\s*=\s*\d+,\s*weight_distribution\s*=\s*\{((?:\s*\[\d+\]\s*=\s*\{[^}]*\},?)+)\s*\}",
            tok,
            re.S,
        )
        if wd_m:
            w_dist = {}
            for km in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", wd_m.group(1)):
                k_w, v_w = int(km.group(1)), int(km.group(2))
                if 1 <= k_w <= 4:
                    w_dist[k_w] = v_w
            if w_dist:
                state["gasha_weights"] = w_dist

        tb_m = re.search(r"team_building_list\s*=\s*\{(.*?)\}\s*,\s*team_building_refresh_cnt", tok, re.S)
        if tb_m:
            g_list = []
            for idx_i, gm in enumerate(
                re.finditer(
                    r"price\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}\s*\},\s*team_building_cfg_id\s*=\s*(\d+),\s*teammate_data_list\s*=\s*\{(.*?)\}",
                    tb_m.group(1),
                    re.S,
                ),
                1,
            ):
                price = int(gm.group(1))
                cid = int(gm.group(2))
                mates = len(re.findall(r"teammate_cfg_id\s*=\s*\d+", gm.group(3)))
                g_list.append({"idx": idx_i, "cfg_id": cid, "price": price, "mates": mates})
            if g_list:
                state["gashapon_list"] = g_list

    # 提取百目鬼『周末加训』标识 (weekend_train) 与周六 6 大训练面板 (weekend_train_cmds)
    wt_flag_m = re.search(r"\bweekend_train\s*=\s*(true|false)", tok)
    if wt_flag_m:
        is_wt = wt_flag_m.group(1) == "true"
        state["weekend_train"] = is_wt
        if not is_wt:
            state["weekend_train_cmds"] = []
    if "train_cmds" in tok and "train_cmds = {}" not in tok:
        if state.get("round_type") == 2 or state.get("weekend_train"):
            wt_cmds = parse_train_cmd_block(tok)
            if len(wt_cmds) == 6:
                state["weekend_train_cmds"] = wt_cmds
                if state.get("weekend_day", 1) == 1:
                    state["weekend_train"] = True
    elif "train_cmds = {}" in tok and state.get("round_type") == 2:
        state["weekend_train_cmds"] = []

    # 提取已学技能列表 (available_skills = { 6800103, ... } 或增量 [idx] = skill_id)
    if "available_skills" in tok:
        learned = state.setdefault("learned_skills", set())
        for ask_m in re.finditer(r"available_skills\s*=\s*\{([^}]*)\}", tok):
            cleaned_sk = re.sub(r"\[\d+\]\s*=\s*", " ", ask_m.group(1))
            for sid_str in re.findall(r"\b(\d{5,9})\b", cleaned_sk):
                sid_i = int(sid_str)
                if sid_i > 0:
                    learned.add(sid_i)

    # 当下发新一轮 6 项训练面板或新周末建筑/扭蛋面板时，解除训练过渡动画锁；日期推进时收起旧周末地图标记
    if "train_cmds" in tok and "train_cmds = {}" not in tok:
        state["train_anim_lock"] = False
    if erm:
        state["weekend_map_open"] = False
        state["travel_sub_scene"] = ""

    # 提取周末 3号建筑·电玩小游戏三选一池 (edu_mini_games_state_data -> games_list)
    if "edu_mini_games_state_data" in tok and "games_list" in tok:
        mg_m = re.search(r"games_list\s*=\s*\{(.*?)\}\s*,\s*mini_game_params", tok, re.S)
        if mg_m:
            m_list = []
            for idx_i, mm_g in enumerate(
                re.finditer(
                    r"game_cfg_id\s*=\s*(\d+),\s*price\s*=\s*\{\s*(?:\[\d+\]\s*=\s*)?\{key\s*=\s*1,\s*value\s*=\s*(\d+)\}\s*\},\s*teammate_data_list\s*=\s*\{(.*?)\}",
                    mg_m.group(1),
                    re.S,
                ),
                1,
            ):
                gid = int(mm_g.group(1))
                price = int(mm_g.group(2))
                mates = len(re.findall(r"teammate_cfg_id\s*=\s*\d+", mm_g.group(3)))
                m_list.append({"idx": idx_i, "game_id": gid, "price": price, "mates": mates})
            if m_list:
                state["minigame_list"] = m_list


# =====================================================================
# 原生 UI 场景状态机 & 叠层模式裁决器 (Diegetic HUD Scene State Machine)
# =====================================================================

_OCCLUDING_UI_VIEW_SUFFIXES = (
    "edu_train_loading_view",
    "common_loading_view",
    "slot_machine_view",
    "effect_animation_popup_view",
    "education_match_prepare_view_v4",
    "match_result_main_view",
    "education_manager_transition_view",
    "story_end_transition_view",
    "edu_item_select_result_view",
    "education_skill_choice_result",
    "shoot_ball_qmodel_view",
    "puzzle_qmodel_view",
    "edu_agenda_week_summarize_view",
    "education_final_settlement_view",
    "edu_settlement_view",
)

# 部分瞬态动画/剧情/小游戏弹窗被引擎直接销毁时不打印 CloseUIView；回到主待命状态(1/104)或切入训练场景时主动回收防永久锁死隐藏
_STALE_MODAL_CLEANUP_SUFFIXES = (
    "effect_animation_popup_view",
    "education_match_prepare_view_v4",
    "match_result_main_view",
    "education_manager_transition_view",
    "story_end_transition_view",
    "story_view",
    "story_popup_view",
    "edu_item_select_result_view",
    "education_skill_choice_result",
    "shoot_ball_qmodel_view",
    "puzzle_qmodel_view",
    "edu_agenda_week_summarize_view",
    "education_match_reward_pop_view",
    "education_target_success_view",
    "task_success_view",
    "education_note_effect_view",
    "education_event_result_pop_view_v2",
    "lottery_result_view",
    "mechanism_close_view",
)


def _ui_views_has(active_views: set, *suffixes: str) -> bool:
    if not active_views:
        return False
    for v in active_views:
        vs = str(v).strip()
        for suf in suffixes:
            if vs == suf or vs.endswith("." + suf):
                return True
    return False


def _ui_views_discard_suffix(active_views: set, *suffixes: str) -> bool:
    removed = False
    for v in list(active_views):
        vs = str(v).strip()
        if any(vs == suf or vs.endswith("." + suf) for suf in suffixes):
            active_views.discard(v)
            removed = True
    return removed


def parse_skill_state_block(block_text: str) -> list[dict]:
    """解析 [EDU_MODEL]_SwitchState 进入状态: 22 的 skill_list 三张候选卡字典。"""
    if not block_text or "skill_id" not in block_text:
        return []
    sl_m = re.search(r"skill_list\s*=\s*\{(.*)\}", block_text, re.S)
    body = sl_m.group(1) if sl_m else block_text
    cards = []
    for item_m in re.finditer(r"\{([^{}]*\bskill_id\s*=\s*\d+[^{}]*)\}", body):
        raw = item_m.group(1)
        kv_map = {}
        for kv in re.finditer(r"(\w+)\s*=\s*(-?\d+)", raw):
            kv_map[kv.group(1)] = int(kv.group(2))
        sid = kv_map.get("skill_id", 0)
        if sid > 0:
            cards.append({
                "note_id": kv_map.get("note_id", 0),
                "refresh_cnt": kv_map.get("refresh_cnt", 0),
                "skill_id": sid,
                "skill_lib_type": kv_map.get("skill_lib_type", 0),
                "supactor_card_id": kv_map.get("supactor_card_id", 0),
            })
    return cards


def update_ui_state_from_line(state: dict, line: str, skill_buf: Optional[list] = None) -> dict:
    """
    增量维护育成 UI 场景、打开的 UIView 集合、剧情选择节点、选技能三选一池与动画遮挡锁。
    skill_buf: 可选单元素列表 [""]，用于跨行拼接 _SwitchState 进入状态: 22 { ... }。
    返回 {"changed": bool, "skill_popup_ready": bool}。
    """
    prev_mode = compute_active_hud_view_mode(state)
    skill_popup_ready = False
    views = state.setdefault("active_ui_views", set())
    learned = state.setdefault("learned_skills", set())

    # 0. 处理跨行的 _SwitchState 进入状态: 22 { skill_list = ... }
    if skill_buf is not None and skill_buf[0]:
        skill_buf[0] += line
        if skill_buf[0].count("{") > 0 and skill_buf[0].count("}") >= skill_buf[0].count("{"):
            parsed_cards = parse_skill_state_block(skill_buf[0])
            skill_buf[0] = ""
            if parsed_cards:
                state["cur_skill_choices"] = parsed_cards
                skill_popup_ready = True
        elif len(skill_buf[0]) > 16384:
            skill_buf[0] = ""

    # 1. [SceneMgr] 场景激活切换：ycjm01(一级主界面) <-> ycjm02(二级6项训练界面) & hbyc01(海滩合宿夏日特训单场景)
    if "[SceneMgr]" in line:
        if "SetActiveScene:" in line:
            if "ycjm01" in line:
                state["ycjm_scene"] = "ycjm01"
                state["is_beach_scene"] = False
            elif "ycjm02" in line:
                state["ycjm_scene"] = "ycjm02"
                state["is_beach_scene"] = False
                state["weekend_map_open"] = False
                state["in_story_choice"] = False
                _ui_views_discard_suffix(
                    views,
                    *_STALE_MODAL_CLEANUP_SUFFIXES,
                    "edu_train_loading_view",
                    "common_loading_view",
                    "edu_store_view",
                )
                state["store_open"] = False
            elif "hbyc01" in line or "夏日特训" in line:
                state["ycjm_scene"] = "ycjm01"
                state["is_beach_scene"] = True
                state["weekend_map_open"] = False
            elif "主界面场景" in line or "大厅" in line or "bsjm" in line:
                state["ycjm_scene"] = ""
                state["is_beach_scene"] = False
        elif "UnloadSceneFinish:" in line:
            if "hbyc01" in line or "夏日特训" in line:
                state["is_beach_scene"] = False
                if not _ui_views_has(views, "education_train_view_v3"):
                    state["ycjm_scene"] = ""
            elif not state.get("is_beach_scene"):
                if "ycjm01" in line and state.get("ycjm_scene") == "ycjm01":
                    state["ycjm_scene"] = ""
                elif "ycjm02" in line and state.get("ycjm_scene") == "ycjm02":
                    state["ycjm_scene"] = ""

    # 1b. 海滩合宿(hbyc01)无独立 ycjm02 场景，点『训练』进入6按钮面板时会触发 Toggle 状态切换 _s_set_isOn
    if (
        state.get("is_beach_scene")
        and "UnityEngineUIToggleWrap:_s_set_isOn" in line
        and not state.get("train_anim_lock")
        and (_ui_views_has(views, "education_train_view_v3") or state.get("ycjm_scene") == "ycjm01")
    ):
        state["ycjm_scene"] = "ycjm02"
        _ui_views_discard_suffix(views, *_STALE_MODAL_CLEANUP_SUFFIXES, "edu_train_loading_view", "common_loading_view")

    # 2. [UIManager] OpenUIView / CloseUIView / HideUIView / CloseAll 精确入栈出栈（兼容带 <color=cyan> 与纯文本）
    if "[UIManager]" in line and "CloseAll" in line:
        views.clear()
        state["in_story_choice"] = False
        state["store_open"] = False
        state["cur_skill_choices"] = []
        state["travel_sub_scene"] = ""
        if not state.get("ycjm_scene"):
            state["pre_edu_char_id"] = None
    elif "[UIManager]" in line and ("OpenUIView" in line or "CloseUIView" in line or "HideUIView" in line):
        um = re.search(
            r"(OpenUIView|CloseUIView|HideUIView)(?:</color>|:)?\s+(?:\d+\s+(?:(?:true|false)\s+)?)?((?:ui\.view\.|mini_game\.)[^\s,;\]]+)",
            line,
        )
        if not um:
            um = re.search(r"(OpenUIView|CloseUIView|HideUIView)(?:</color>|:)?\s+([a-zA-Z0-9_.]+)", line)
        if um:
            act_op, vname = um.group(1), um.group(2).strip()
            if act_op == "OpenUIView":
                views.add(vname)
                if vname.endswith("edu_store_view"):
                    state["store_open"] = True
                elif vname.endswith("education_train_view_v3"):
                    if int(state.get("edu_state_id", 0) or 0) in (0, 1, 104):
                        state["train_anim_lock"] = False
                        _ui_views_discard_suffix(views, *_STALE_MODAL_CLEANUP_SUFFIXES, "edu_train_loading_view", "common_loading_view")
                    if state.get("is_beach_scene"):
                        state["ycjm_scene"] = "ycjm01"
                elif vname.endswith("education_note_effect_view"):
                    _ui_views_discard_suffix(views, "edu_train_loading_view", "common_loading_view")
                elif vname.endswith("education_skill_choice_view_v2") and state.get("cur_skill_choices"):
                    skill_popup_ready = True
                elif "ui_education_formation_main_player_panel" in vname and not state.get("ycjm_scene"):
                    pass
            else:
                views.discard(vname)
                short_v = vname.rsplit(".", 1)[-1]
                _ui_views_discard_suffix(views, short_v)
                if short_v == "story_view":
                    state["in_story_choice"] = False
                elif short_v == "education_skill_choice_view_v2":
                    state["cur_skill_choices"] = []
                    if state.get("edu_state_id") == 22:
                        state["edu_state_id"] = 1
                elif short_v == "edu_store_view":
                    state["store_open"] = False
                    if state.get("is_beach_scene"):
                        state["ycjm_scene"] = "ycjm01"
                elif short_v == "edu_train_loading_view":
                    if state.get("is_beach_scene"):
                        state["ycjm_scene"] = "ycjm01"
                elif short_v == "edu_travel_selection_info_view":
                    state["travel_sub_scene"] = ""
                elif "ui_education_formation_main_player_panel" in short_v:
                    if not any("education.formation" in x for x in views):
                        state["pre_edu_char_id"] = None

    # 3. 音频环境辅助定位周末子面板（公园 / 餐厅 / 返回周末一级界面）
    if "PostAMBEvent:" in line:
        if "育成训练-公园" in line:
            state["travel_sub_scene"] = "park"
        elif "育成训练-餐厅" in line:
            state["travel_sub_scene"] = "restaurant"
        elif "育成训练-周末主界面" in line:
            state["weekend_map_open"] = False
            state["travel_sub_scene"] = ""

    # 4. 剧情分支节点进出
    if "[SCENARIO]进入选择节点" in line:
        state["in_story_choice"] = True
    if "request_call:" in line and ("education.cs_event_branch" in line or "education.cs_event_finish" in line):
        state["in_story_choice"] = False
        if "education.cs_event_finish" in line:
            _ui_views_discard_suffix(views, "story_view", "story_popup_view", "story_end_transition_view")

    # 5. [EDU_MODEL]_SwitchState 状态机（22=选技能，104=周末外出大地图，11=扭蛋三选一，14=街机三选一，105=建筑确认框）
    if "[EDU_MODEL]_SwitchState" in line:
        enter_m = re.search(r"进入状态:\s*(\d+)", line)
        leave_m = re.search(r"离开状态:\s*(\d+)", line)
        if enter_m:
            st_id = int(enter_m.group(1))
            state["edu_state_id"] = st_id
            if st_id in (1, 104):
                state["in_story_choice"] = False
                _ui_views_discard_suffix(views, *_STALE_MODAL_CLEANUP_SUFFIXES, "edu_train_loading_view", "common_loading_view")
                if st_id == 104:
                    state["weekend_map_open"] = True
                    state["travel_sub_scene"] = ""
            elif st_id == 15:
                _ui_views_discard_suffix(views, "edu_train_loading_view", "common_loading_view")
            elif st_id in (2, 16, 101, 110):
                state["store_open"] = False
                _ui_views_discard_suffix(views, "edu_store_view", "puzzle_qmodel_view", "shoot_ball_qmodel_view")
                if st_id == 16:
                    # 比赛日待开赛态（每周周一比赛日 & 决赛日 R49）：清除周末残留与训练过场锁，防误判为周末外出
                    state["train_anim_lock"] = False
                    state["in_story_choice"] = False
                    state["weekend_day"] = 0
                    state["weekend_train"] = False
                    state["weekend_train_cmds"] = []
                    state["weekend_map_open"] = False
                    state["travel_sub_scene"] = ""
                    if state.get("round_type") == 2:
                        state["round_type"] = 0
                    _ui_views_discard_suffix(views, *_STALE_MODAL_CLEANUP_SUFFIXES, "edu_train_loading_view", "common_loading_view")
            elif st_id == 5:
                state["ycjm_scene"] = ""
                state["is_beach_scene"] = False
                state["train_anim_lock"] = False
            elif st_id == 105:
                if "building_id = 1" in line:
                    state["travel_sub_scene"] = "park"
                elif "building_id = 8" in line:
                    state["travel_sub_scene"] = "restaurant"
            elif st_id == 22:
                if "skill_id" in line and line.count("}") >= line.count("{") and line.count("{") > 0:
                    parsed_cards = parse_skill_state_block(line)
                    if parsed_cards:
                        state["cur_skill_choices"] = parsed_cards
                        skill_popup_ready = True
                elif skill_buf is not None:
                    skill_buf[0] = line
        elif leave_m:
            lv_id = int(leave_m.group(1))
            if lv_id == 2:
                state["in_story_choice"] = False
                _ui_views_discard_suffix(views, "story_view", "story_popup_view", "story_end_transition_view")
            elif lv_id == 22:
                state["cur_skill_choices"] = []
                _ui_views_discard_suffix(views, "education_skill_choice_view_v2")
            if state.get("edu_state_id") == lv_id:
                state["edu_state_id"] = 0

    # 6. 网络请求与数据锁（训练点击锁、选技能提交、选人配装、开局/结束重置）
    if "[EDU_MODEL]UnlockData" in line:
        state["train_anim_lock"] = False

    if "request_call:" in line:
        if "education.cs_edu_initactor_record" in line:
            pm = re.search(r"initactor_cfg_id\s*=\s*(\d+)", line)
            if pm:
                state["pre_edu_char_id"] = int(pm.group(1))
        elif "education.cs_match" in line:
            state["store_open"] = False
            _ui_views_discard_suffix(views, "edu_store_view")
        elif "education.cs_start_education" in line or "education.cs_restore_education" in line:
            state["pre_edu_char_id"] = None
            state["train_anim_lock"] = False
            state["in_story_choice"] = False
            state["cur_skill_choices"] = []
            _ui_views_discard_suffix(views, *_OCCLUDING_UI_VIEW_SUFFIXES)
        elif "education.cs_end_education" in line:
            state["pre_edu_char_id"] = None
            state["ycjm_scene"] = ""
            state["is_beach_scene"] = False
            state["in_story_choice"] = False
            state["cur_skill_choices"] = []
            views.clear()
        elif "education.cs_train" in line or "education.cs_decide" in line:
            state["train_anim_lock"] = True
        elif "education.cs_edu_choice_skill" in line:
            sk_m = re.search(r"skill_id\s*=\s*(\d+)", line)
            if sk_m:
                sid_chosen = int(sk_m.group(1))
                if sid_chosen > 0:
                    learned.add(sid_chosen)
            state["cur_skill_choices"] = []
            _ui_views_discard_suffix(views, "education_skill_choice_view_v2")
            if state.get("edu_state_id") == 22:
                state["edu_state_id"] = 1
        elif "education.cs_select_team_building" in line or "education.cs_start_games" in line:
            state["train_anim_lock"] = True
            _ui_views_discard_suffix(views, "edu_item_select_view", "mini_game_choose_view")
        elif "education.cs_select_edu_activity" in line:
            # 选餐厅打工或公园出行直接发起结算；选6号扭蛋或3号街机会紧接着弹子面板
            if "building_cfg_id = 6" not in line and "building_cfg_id = 3" not in line:
                state["train_anim_lock"] = True
                state["weekend_map_open"] = False
                state["travel_sub_scene"] = ""
                _ui_views_discard_suffix(views, "edu_travel_selection_info_view")

    new_mode = compute_active_hud_view_mode(state)
    return {
        "changed": (new_mode != prev_mode) or skill_popup_ready,
        "skill_popup_ready": skill_popup_ready,
        "mode": new_mode,
    }


def replay_ui_state_from_text(state: dict, text: str) -> None:
    """对给定日志片段逐行回放 UI 场景与叠层状态机。"""
    if not text:
        return
    sk_buf = [""]
    for ln in text.splitlines(True):
        update_ui_state_from_line(state, ln, sk_buf)


def compute_active_hud_view_mode(state: dict) -> str:
    """
    纯函数：根据当前 state 裁决 HUD 应该呈现的精确视图模式。
    返回值枚举：
      - "hidden"             : 过场动画/加载/老虎机/比赛/纯对话剧情(无选项)/训练结算锁/大厅闲置（100% 零遮挡）
      - "skill_choice"       : 育成选技能三选一弹窗 (education_skill_choice_view_v2)
      - "story_choice"       : 剧情事件二选一/三选一分支节点 (story_view + in_story_choice)
      - "store"              : 育成商店内部 (edu_store_view)
      - "initactor"          : 开局选人编队笔记与本命器材配装界面
      - "weekend_gashapon"   : 周末 6号扭蛋机三选一界面
      - "weekend_minigame"   : 周末 3号街机小游戏三选一界面
      - "weekend_park"       : 周末 1号公园散步/划船/野餐三选一界面
      - "weekend_restaurant" : 周末 8号餐厅轻度/重度打工二选一界面
      - "ycjm02"             : 平日或周六加训二级面板（右侧 1~6 号训练按钮界面）
      - "weekend_map"        : 周末出行大地图（餐厅/街机/扭蛋/公园四大建筑）
      - "weekend_main"       : 周末一级主界面（右下外出/加训入口 + 右上商店入口）
      - "match_ready"        : 比赛日待开赛主界面（周一目标赛 & 末周决赛 R49，右下开赛 + 右上商店）
      - "ycjm01"             : 平日一级主界面（右下训练入口 + 右侧休息入口 + 右上商店入口）
    """
    if not isinstance(state, dict):
        return "hidden"
    views = state.get("active_ui_views") or set()

    # 1. 强遮挡过场/动画/加载/老虎机/比赛界面 -> 100% 隐藏
    if _ui_views_has(views, *_OCCLUDING_UI_VIEW_SUFFIXES):
        return "hidden"

    # 2. 选技能三选一弹窗（优先级高于栈底的 story_view 与 train_anim_lock）
    if _ui_views_has(views, "education_skill_choice_view_v2") or (
        int(state.get("edu_state_id", 0) or 0) == 22 and bool(state.get("cur_skill_choices"))
    ):
        return "skill_choice"

    # 3. 剧情视图：仅在进入选择节点时显示选项角标，纯剧情对话 100% 隐藏防挡台词
    if _ui_views_has(views, "story_view"):
        return "story_choice" if state.get("in_story_choice") else "hidden"
    if state.get("in_story_choice"):
        return "story_choice"

    # 4. 商店弹窗打开中
    if _ui_views_has(views, "edu_store_view"):
        return "store"

    # 5. 开局选人编队界面
    if state.get("pre_edu_char_id") and not state.get("ycjm_scene"):
        return "initactor"

    # 6. 点击训练/休息/建筑后至下一回合数据解锁前的过场过渡锁
    if state.get("train_anim_lock"):
        return "hidden"

    # 7. 周末出行子面板（扭蛋 / 街机 / 公园 / 餐厅）
    if _ui_views_has(views, "edu_item_select_view", "gashapon_screen", "edu_gashapon_view") or (
        state.get("round_type") == 2 and int(state.get("edu_state_id", 0) or 0) == 11 and state.get("gashapon_list")
    ):
        return "weekend_gashapon"

    if _ui_views_has(views, "mini_game_choose_view", "minigame_screen") or (
        state.get("round_type") == 2 and int(state.get("edu_state_id", 0) or 0) == 14 and state.get("minigame_list")
    ):
        return "weekend_minigame"

    if _ui_views_has(views, "park_screen", "edu_park_view"):
        return "weekend_park"
    if _ui_views_has(views, "edu_travel_selection_info_view"):
        sub_sc = state.get("travel_sub_scene", "")
        if sub_sc == "park":
            return "weekend_park"
        if sub_sc == "restaurant":
            return "weekend_restaurant"
        return "weekend_park"

    # 8. 育成一级/二级主场景、比赛日与周末地图
    ycjm = state.get("ycjm_scene", "")
    in_edu_main = bool(ycjm in ("ycjm01", "ycjm02") or _ui_views_has(views, "education_train_view_v3"))
    if not in_edu_main and not _ui_views_has(views, "weekend_map"):
        return "hidden"

    # 比赛日待开赛主界面（edu_state_id == 16 或 R49 决赛且 left_round == 0）：
    # 优先级高于 round_type == 2，彻底杜绝末周带百目鬼周六加训后或周日跨周一开赛时误判为周末出行
    if int(state.get("edu_state_id", 0) or 0) == 16 or (
        int(state.get("exp_round", 0) or 0) >= 49 and int(state.get("left_round", 1)) == 0
    ):
        return "match_ready"

    if int(state.get("round_type", 1) or 1) == 2:
        if ycjm == "ycjm02" and state.get("weekend_train"):
            return "ycjm02"
        if _ui_views_has(views, "weekend_map") or state.get("weekend_map_open") or int(state.get("edu_state_id", 0) or 0) == 104:
            return "weekend_map"
        return "weekend_main"

    if ycjm == "ycjm02":
        return "ycjm02"
    return "ycjm01"


def _count_maxed_crit_cards(raw_map: dict, card_to_cfg: dict) -> int:
    """统计已满 100 熟的可暴击器材数量（排除 NON_CRIT_EQUIPMENT_IDS）。"""
    cnt = 0
    for k_id, cur_t in (raw_map or {}).items():
        cfg_k = card_to_cfg.get(k_id, k_id)
        if int(cfg_k) not in NON_CRIT_EQUIPMENT_IDS and int(cur_t or 0) >= 100:
            cnt += 1
    return cnt


def _eval_single_card_trammel_gain_ev(
    cfg_id: int,
    cur_v: int,
    eff_g: int,
    rem_turns: int,
    state: dict,
    w: dict,
    already_maxed_crit_cnt: int = 0,
    whistle_70_ev: float = 10.0,
    whistle_100_ev: float = 12.0,
) -> tuple[float, str]:
    """
    统一商店/扭蛋单张器材熟练度增量估值（与训练期权 calc_equip_trammel_option_ev 严格同口径）：
    - 不可暴击器材 (NON_CRIT_EQUIPMENT_IDS)：
      * 11004 支援口哨：单独走 70/100 里程碑事件与推线进度估值；
      * 其余不可暴击器材 (如 11005)：无法触发彩圈暴击，记 0.0 EV；
    - 可暴击器材：调用 calc_equip_trammel_option_ev(..., fractional_step=True) 精确计价，
      剩余回合来不及满彩时自动归零，并受多满彩竞争递减约束。
    """
    if eff_g <= 0 or rem_turns <= 0 or cur_v >= 100:
        return 0.0, ""
    new_v = min(100, cur_v + eff_g)
    if int(cfg_id) in NON_CRIT_EQUIPMENT_IDS:
        if int(cfg_id) == 11004:
            rem_ratio = max(0.0, min(1.0, rem_turns / 20.0))
            ev = 0.0
            tag = ""
            if cur_v < 100 and new_v >= 100 and not state.get("fired_71", False):
                ev += whistle_100_ev
                tag = "哨➔100"
            elif cur_v < 70 and new_v >= 70 and not state.get("fired_70", False):
                ev += whistle_70_ev
                tag = "哨➔70"
            # 推哨子跨线进度价值（仅在剩余训练 >= 3 时计入，防止末周虚高）
            if rem_turns >= 3:
                unit_prog = 0.48 if cur_v < 70 else 0.56
                ev += eff_g * unit_prog * rem_ratio
            return round(ev, 1), tag
        return 0.0, ""

    opt = calc_equip_trammel_option_ev(
        cur_v,
        eff_g,
        rem_turns,
        w,
        already_maxed_crit_cnt=already_maxed_crit_cnt,
        fractional_step=True,
    )
    return float(opt["ev"]), ""


def calc_gashapon_dynamic_ev(
    cid: int,
    price: int,
    mates: int,
    state: dict,
    strategy: Optional[dict] = None,
) -> tuple[float, str, int]:
    """
    基于 EDU_MODEL 实时数据与历史日志结算实证，按每颗扭蛋的具体效果计算真实有效属性当量 (EV)：
    - 1 EV = 1 点有效属性当量
    - 1 点不溢出的有效精力 = 动态边际体力价值 get_stamina_unit_ev (1.18~1.55 EV/ps)
    - 1 名驻留队友 (👥×1) = +10 点随机单属性（受 >=1200 折半影响，前期约 +11 EV，后期高维折半约 +6~8.5 EV）
    - 移除旧版虚高的固定队友战力补贴 (mate_boost)，严格以主角六维有效属性 + 前中期复利雪球上限 (High-Roll) 计分：
      * 82 (直接计算一次笔记): T0真神彩蛋 (~200~260 EV)
      * 100 / 99 (熟练度暴涨+ / 暴涨) & 25/26/27: 区分 NON_CRIT_EQUIPMENT_IDS 并对齐彩圈期权模型
      * 40~57 / 1119~1124 (设施升级包): 接入 P5 设施复利模型 calc_shop_facility_book_p5_ev()
      * 30 / 29 (器材暴击++ / +) & 33 / 32 (训练增强++ / +): 按剩余训练回合数复利计分，末期自动衰减
      * 10151 (预支300金+打工-60) & 58/59/60 (周六补助): 前期周转可用，后期/决赛前因金币饱和大幅衰减
    """
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    round_num = state.get("round", 1)
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    trammel_map = state.get("supactor_trammel", {})
    note_stat = state.get("note_stat_sum", 0)
    note_cap = state.get("note_cap_sum", 0)

    g_meta = GASHAPON_CFG_NOTES.get(cid)
    short_tag = g_meta["short"] if g_meta else (
        f"🌈未知彩蛋#{cid}" if price >= 115 else ("✨金蛋" if price >= 95 else ("🟣紫蛋" if price >= 75 else "🔵蓝蛋"))
    )
    ps_gain = g_meta.get("ps_gain", 0) if g_meta else 0
    rem_turns = get_remaining_trains(state)
    rem_ratio = max(0.15, min(1.0, rem_turns / 24.0))
    ps_unit = get_stamina_unit_ev(state, ps_override=ps)
    exp_r = infer_exp_round(state)
    week_no = min(7, (exp_r - 1) // 7 + 1)
    wk_day = state.get("weekend_day", 1) or 1
    w_strat = (strategy or {}).get("weights") or {}
    strat_stat_raw = w_strat.get("stat_base_value") or {}
    stat_base_w = {
        k: float(strat_stat_raw.get(str(k), FIXED_STAT_BASE_W.get(k, 1.0)))
        for k in range(101, 107)
    }
    g_strat = strategy if isinstance(strategy, dict) and "weights" in strategy else {
        "weights": {"stat_base_value": {str(k): v for k, v in stat_base_w.items()}}
    }

    if cid in (100, 99):
        # 熟练度暴涨+ (100: 2器材满100+全训主属+2) / 熟练度暴涨 (99: 1器材满100+全训主属+2)：
        # 区分 NON_CRIT_EQUIPMENT_IDS 并对齐彩圈期权模型；剩训不足时自动衰减
        raw_map = trammel_map or state.get("supactor_card_trammel") or {}
        card_to_cfg = state.get("supactor_card_to_cfg", {})
        perm_train_ev = rem_turns * 2.0 * 1.45 if rem_turns > 0 else 0.0
        if raw_map:
            maxed_crit_cnt = _count_maxed_crit_cards(raw_map, card_to_cfg)
            card_evs = []
            for k_id, cur_t in raw_map.items():
                cfg_k = card_to_cfg.get(k_id, k_id)
                cur_v = min(100, max(0, int(cur_t)))
                if cur_v >= 98:
                    continue
                ev_c, _ = _eval_single_card_trammel_gain_ev(
                    cfg_k, cur_v, 100 - cur_v, rem_turns, state, w_strat,
                    already_maxed_crit_cnt=maxed_crit_cnt,
                    whistle_70_ev=10.0, whistle_100_ev=14.0,
                )
                card_evs.append(ev_c)
            if card_evs:
                n_picks = min(2 if cid == 100 else 1, len(card_evs))
                avg_card_ev = sum(card_evs) / len(card_evs)
                base_ev = round(n_picks * avg_card_ev + perm_train_ev + 3.0, 1)
            else:
                base_ev = round(perm_train_ev + 3.0, 1)
        else:
            # 无实时字典时按期权近似（假设典型中前期 cur_t=40）
            approx_one = calc_equip_trammel_option_ev(40, 60, rem_turns, w_strat, fractional_step=True)["ev"]
            n_picks = 2 if cid == 100 else 1
            base_ev = round(n_picks * approx_one * 0.85 + perm_train_ev + 3.0, 1)
    elif cid in (25, 26, 27):
        # 器材熟练度 (25,60金,全队+10) / 器材熟练度+ (26,80金,随机2件+30) / 器材熟练度++ (27,100金,全队+30)：
        # 逐卡区分 NON_CRIT_EQUIPMENT_IDS 并与 calc_equip_trammel_option_ev 对齐，剩训来不及满彩自动归零！
        t_gain = int(g_meta.get("trammel_gain", g_meta.get("trammel_all", 30 if cid != 25 else 10)))
        rand_n = int(g_meta.get("trammel_rand_n", 0))
        raw_map = trammel_map or state.get("supactor_card_trammel") or {}
        card_to_cfg = state.get("supactor_card_to_cfg", {})
        if raw_map:
            pick_p = min(1.0, rand_n / len(raw_map)) if rand_n > 0 else 1.0
            maxed_crit_cnt = _count_maxed_crit_cards(raw_map, card_to_cfg)
            eff_total = 0
            maxed_new = 0
            ev_sum = 0.0
            whistle_tag = ""
            for k_id, cur_t in raw_map.items():
                cfg_k = card_to_cfg.get(k_id, k_id)
                cur_v = min(100, max(0, int(cur_t)))
                if cur_v >= 100:
                    continue
                eff_g = min(t_gain, 100 - cur_v)
                eff_total += eff_g
                new_v = cur_v + eff_g
                if new_v >= 100 and int(cfg_k) not in NON_CRIT_EQUIPMENT_IDS:
                    maxed_new += 1
                ev_k, w_tag_k = _eval_single_card_trammel_gain_ev(
                    cfg_k, cur_v, eff_g, rem_turns, state, w_strat,
                    already_maxed_crit_cnt=maxed_crit_cnt,
                    whistle_70_ev=10.0, whistle_100_ev=12.0,
                )
                ev_sum += ev_k
                if w_tag_k:
                    whistle_tag = w_tag_k
            base_ev = round(ev_sum * pick_p + (3.0 if eff_total > 0 else 0.0), 1)
            if eff_total <= 0:
                short_tag = f"{short_tag}(⚠️已全满)"
                base_ev = 4.0
            elif rand_n > 0:
                w_sfx = f"·{whistle_tag}?" if whistle_tag else ""
                short_tag = f"{short_tag}(期望+{eff_total * pick_p:.0f}熟{w_sfx})"
            else:
                w_sfx = f"·{whistle_tag}" if whistle_tag else (f"·满{maxed_new}张" if maxed_new > 0 else "")
                short_tag = f"{short_tag}(实+{eff_total}熟{w_sfx})"
        else:
            approx_one = calc_equip_trammel_option_ev(45, t_gain, rem_turns, w_strat, fractional_step=True)["ev"]
            eff_cards = (rand_n * 0.8) if rand_n > 0 else 4.0
            base_ev = round(eff_cards * approx_one + 3.0, 1)
    elif cid == 82:
        # 120金T0真神彩蛋：直接计算一次笔记（7局实测均值 +175~300属性 / +161~240上限，上限神局核心胜负手）
        high_roll_note_bonus = 18.0 if rem_turns >= 12 else (10.0 if rem_turns >= 5 else 4.0)
        if note_stat > 0 or note_cap > 0:
            base_ev = max(195.0, note_stat * 1.15 + note_cap * 0.22) + high_roll_note_bonus
        else:
            base_ev = 215.0 + high_roll_note_bonus
    elif cid == 76:
        # 120金功能型彩蛋：随机获得1个新的球员位置 + 队友属性提升（主控六维属性+0、上限+0）
        base_ev = 12.0
    elif cid in (20003, 20002):
        base_ev = 165.0 if cid == 20003 else 142.0
    elif cid in (20001, 20013, 20022):
        base_ev = 92.0
    elif cid == 107:
        # 100金破限满体蛋：体力上限+10 且 当场回满体力（按 ps_cap + 10 算真实回满量！）
        eff_ps = max(0, (ps_cap + 10) - ps)
        ps_gain = eff_ps
        cap_10_bonus = 28.0 * rem_ratio + 6.0
        base_ev = eff_ps * ps_unit + cap_10_bonus
    elif cid in (22, 23, 24):
        # 持续4天每天结束回 10/20/30 体 (state_id: 306/307/308)：
        # 注意：周六/周日购买后，周六、周日、周一(比赛日)均会消耗天数或直接溢出；
        # 且若下周初必出固定/排队回体事件（第5周R36许愿+40体、第1周R10集训+30体、哨子ID70+40体/ID71+70体），持续回体将大面积溢出浪费！
        daily_ps = g_meta.get("daily_ps", 20) if g_meta else 20
        sched_c = get_upcoming_schedule_context(state)
        day1_eff_ps = min(daily_ps, max(0, ps_cap - ps))
        future_days = min(3, max(0, rem_turns))
        overlap_discount = 0.72
        if sched_c.get("queued_id71"):
            overlap_discount = 0.22
        elif week_no == 5 or sched_c.get("queued_id70"):
            overlap_discount = 0.34
        elif week_no == 1 or week_no >= 6:
            overlap_discount = 0.45
        if ps >= 80:
            overlap_discount *= 0.65
        future_eff_ps = future_days * daily_ps * overlap_discount
        base_ev = (day1_eff_ps * 0.55 + future_eff_ps) * (ps_unit * 0.85) + 3.0
    elif cid == 10151:
        # 100金预支300金（净+200金）+ 器材指导书+(state:302) + 永久打工金币-60(state:447)：
        # 前期缺钱买商店T0道具时可应急，但亏损1次周末行动机会成本与后续打工收益；第5~7周因决赛+210金饱和彻底贬值
        if week_no <= 2:
            base_ev = 82.0
        elif week_no == 3:
            base_ev = 68.0
        elif week_no == 4:
            base_ev = 52.0
        elif week_no == 5:
            base_ev = 32.0
        else:
            base_ev = 12.0
        short_tag = f"{short_tag}(净+200金)"
    elif g_meta and "sat_gold" in g_meta:
        # 周六零用钱补助 (58:+40金, 59:+80金, 60:+110金, state_id: 322/323/492)：
        # 前2周买入可连续白嫖 4~5 个周六（共 320~550 金），是支撑中后期连抽高阶扭蛋与买商店神器的长期发动机！第5~6周后迅速贬值
        sat_gold = int(g_meta["sat_gold"])
        rem_saturdays = max(0, 6 - week_no)
        eff_gold_unit = 0.42 if week_no <= 2 else (0.30 if week_no == 3 else (0.15 if week_no == 4 else 0.08))
        early_engine_bonus = (12.0 if sat_gold >= 80 else 6.0) if week_no <= 2 else 0.0
        base_ev = round(rem_saturdays * sat_gold * eff_gold_unit + early_engine_bonus + 2.0, 1)
        short_tag = f"{short_tag}(剩{rem_saturdays}周)"
    elif cid in (28, 29, 30):
        # 器材暴击(28,60金) / 器材暴击+(29,80金) / 器材暴击++(30,100金) [state_id: 310/311/312]：
        # 凹上限神局核心长期乘区！前中期买入覆盖 15~23 训彩圈合训与海滩满级暴击，复利远超单次属性街机
        if trammel_map:
            maxed_cnt = sum(1 for v in trammel_map.values() if v >= 100)
            avg_tram = sum(trammel_map.values()) / max(1, len(trammel_map))
            cur_mult = min(1.48, max(0.65, 0.65 + 0.14 * maxed_cnt + 0.35 * (avg_tram / 100.0)))
            crit_mult = max(1.18, cur_mult) if rem_turns >= 14 else (max(1.0, cur_mult) if rem_turns >= 8 else cur_mult)
        else:
            crit_mult = 1.18 if rem_turns >= 14 else 1.0
        per_turn_crit_ev = 4.8 if cid == 30 else (3.4 if cid == 29 else 2.1)
        high_roll_crit_bonus = (18.0 if cid == 30 else (11.0 if cid == 29 else 5.0)) if rem_turns >= 14 else (
            (10.0 if cid == 30 else (6.0 if cid == 29 else 2.5)) if rem_turns >= 10 else 0.0
        )
        base_ev = rem_turns * per_turn_crit_ev * crit_mult + high_roll_crit_bonus + 4.0
    elif cid in (31, 32, 33):
        # 训练增强(31,60金,+1基础主属性/训) / +(32,80金,+2) / ++(33,100金,+3) [state_id: 313/314/315]：
        # +1/+2/+3 直接加在训练底层基础主属性上，后续吃器材彩圈、队友合训、成长率与吹哨乘区，且不受 >=1200 属性折半限制！
        stat_per_turn = 3 if cid == 33 else (2 if cid == 32 else 1)
        high_roll_train_bonus = (16.0 if cid == 33 else (10.0 if cid == 32 else 5.0)) if rem_turns >= 14 else (
            (10.0 if cid == 33 else (6.0 if cid == 32 else 2.5)) if rem_turns >= 10 else 0.0
        )
        base_ev = rem_turns * (stat_per_turn * 2.85) + high_roll_train_bonus + 4.0
    elif cid in (97, 98):
        # 延迟满足(97,80金: 5天后全队全属性+13 => 主角6维各+13) / 98(100金: 5天后全维各+20)
        # 注意：5天后结算时属于固定属性加点(sub_type=20)，当时已 >=1200（或当前 >=1020 5天后必破1200）的维度会被折半！
        if rem_turns >= 4:
            per_d = 13.0 if cid == 97 else 20.0
            stat_ev_97 = 0.0
            # 5天（含1场周一比赛+4次训练）全六维合计约涨 360~420 点，分摊到单维的期望增量约为 +65 点（旧版写 +180 相当于假设4训涨1080点，导致1050+的属性被全部误判折半）
            proj_delta = 65 if rem_turns >= 6 else 35
            for k in range(101, 107):
                proj_cur = int(props.get(k, 200)) + proj_delta
                proj_props = dict(props)
                proj_props[k] = min(int(prop_max.get(k, 1600)), proj_cur)
                ev_k, _ = calc_stat_gain_ev(
                    k, per_d, proj_props, prop_max, g_strat, round_num=round_num, apply_1200_discount=True, rem_trains=rem_turns
                )
                stat_ev_97 += ev_k
            base_ev = round(stat_ev_97 + 4.0, 1)
        else:
            base_ev = 12.0  # 剩余不足4训时可能毕业前无法触发5天结算
    elif cid in (95, 96):
        # 小游戏强化（后续每次小游戏+20/+30体能，训练流路线小游戏次数有限，按实效保守估值）
        base_ev = (22.0 if cid == 95 else 32.0) * rem_ratio + 4.0
    elif g_meta and ("stat" in g_meta or "foc_idx" in g_meta):
        st_id = int(g_meta.get("stat", 0))
        gain = float(g_meta.get("gain", 0))
        stat_ev = 0.0
        if 101 <= st_id <= 106 and gain > 0:
            stat_ev, real_g = calc_stat_gain_ev(
                st_id, gain, props, prop_max, g_strat, round_num=round_num, apply_1200_discount=True, rem_trains=rem_turns
            )
            if real_g <= 0:
                short_tag = f"{short_tag}(⚠️已满+0)"
            elif real_g < gain:
                short_tag = f"{short_tag}(实+{real_g})"
        elif st_id == 0 and gain > 0:
            per_dim = gain / 6.0
            real_tot = 0
            for k in range(101, 107):
                ev_k, rk = calc_stat_gain_ev(
                    k, per_dim, props, prop_max, g_strat, round_num=round_num, apply_1200_discount=True, rem_trains=rem_turns
                )
                stat_ev += ev_k
                real_tot += rk
            if real_tot < int(round(gain)):
                short_tag = f"{short_tag}(实+{real_tot})"
            if cid == 3:
                stat_ev += 14.0 * rem_ratio  # 3号金蛋附带 state:300 训练省体 Buff

        fac_ev = 0.0
        if "foc_idx" in g_meta:
            foc_idx = int(g_meta["foc_idx"])
            foc_up = int(g_meta.get("foc_up", 1))
            eff_focus_lvs = _resolve_effective_focus_levels(state)
            cur_lv = int(eff_focus_lvs.get(foc_idx, 1))
            eff_lv = max(0, min(foc_up, 5 - cur_lv))
            p5_fac = calc_shop_facility_book_p5_ev(
                0,
                state,
                g_strat,
                lv_gain_override=foc_up,
                train_idx_override=foc_idx,
            )
            fac_ev = float(p5_fac.get("ev", 0.0))
            if eff_lv == 0:
                short_tag = f"{short_tag}(已Lv5)"
            else:
                short_tag = f"{short_tag}(Lv{cur_lv}➔{cur_lv + eff_lv})"

        ps_bonus_ev = 0.0
        penalty_ev = 0.0
        if ps_gain > 0:
            eff_ps_g = min(ps_gain, max(0, ps_cap - ps))
            ps_bonus_ev = eff_ps_g * (ps_unit * 0.90)
        elif ps_gain < 0:
            penalty_ev += abs(ps_gain) * (ps_unit * 1.15 if ps + ps_gain < 45 else ps_unit * 0.95)
        if cid in (10143, 10144):
            penalty_ev += 18.0 * rem_ratio
        if g_meta and g_meta.get("lock_train_stat"):
            # 104(意识暴涨+250/state:344) / 105(防守暴涨+250/state:345)：主力永久不再能从训练中获得该维属性
            # 后期(剩训少)或拿完已接近上限时几乎零代价血赚；前中期且该维成长空间很大(尤其为核心推荐/专注维)时扣减锁训机会成本
            lock_st = int(g_meta["lock_train_stat"])
            lock_real_g = calc_real_stat_gain(
                props.get(lock_st, 200), prop_max.get(lock_st, 1600), gain, apply_1200_discount=True
            )
            headroom_after = max(0, int(prop_max.get(lock_st, 1600)) - (int(props.get(lock_st, 200)) + lock_real_g))
            focus_st_l, _ = get_active_focus()
            rec_stats_l = get_char_rec_stats(state)
            is_key_lock = (focus_st_l == lock_st) or (lock_st in rec_stats_l)
            per_turn_lock = 11.5 if is_key_lock else 7.5
            lock_loss = min(headroom_after * 0.75, rem_turns * per_turn_lock * min(1.0, headroom_after / 450.0))
            penalty_ev += lock_loss
            st_nm_l = STAT_NAMES.get(lock_st, str(lock_st))
            if lock_real_g <= 0:
                short_tag = f"⚡{st_nm_l}+250(⚠️已满+0)"
            elif rem_turns <= 0 or lock_loss < 8.0:
                real_part = f"实+{lock_real_g}·" if lock_real_g < int(gain) else ""
                short_tag = f"⚡{st_nm_l}+250({real_part}无负)"
            else:
                real_part = f"实+{lock_real_g}·" if lock_real_g < int(gain) else ""
                short_tag = f"⚡{st_nm_l}+250({real_part}锁训-{lock_loss:.0f})"

        # 仅保留极小的主角外队友微调分(2~4 EV)，彻底剔除旧版 +14~24 EV 的虚高队友水分（按原档位判定，避免5折/8折时金档被降分）
        orig_p = int(g_meta.get("orig_price", price)) if g_meta else price
        is_gold_tier = orig_p >= 95 or cid in (3, 16, 17, 18, 19, 20, 21, 52, 53, 54, 55, 56, 57, 104, 105, 106, 1127)
        tie_mate_boost = 4.0 if (orig_p >= 115 or cid in (113, 114, 115, 116, 117, 118)) else (3.0 if is_gold_tier else 2.0)
        base_ev = max(5.0, stat_ev + fac_ev + ps_bonus_ev + tie_mate_boost - penalty_ev)
    else:
        if price >= 115:
            base_ev = 90.0
        elif price >= 95:
            base_ev = 74.0
        elif price >= 75:
            base_ev = 54.0
        else:
            base_ev = 34.0

    under_1200_cnt = sum(1 for k in range(101, 107) if props.get(k, 200) < 1200)
    mate_unit_ev = round(5.5 + 5.5 * (under_1200_cnt / 6.0) + 1.0, 1)
    total_ev = round(base_ev + mates * mate_unit_ev, 1)
    return total_ev, short_tag, ps_gain


def calc_minigame_dynamic_ev(gid: int, price: int, mates: int, state: dict, strategy: dict) -> tuple[float, str, str]:
    """
    计算 3号建筑·电玩小游戏的真实有效属性当量 (EV)（场外升级后自动结算必拿满额）：
    - 简单(60金/打折30~48金): 必拿主属性 +60
    - 普通(80金/打折40~64金): 必拿主属性 +70
    - 困难(100金/打折50~80金): 必拿主属性 +100
    - 特级(cfg_id=4, 150金/打折75~120金): 全六维各 +25 (合计 +150)！
    - 驻留队友 (👥×1): 触发 teammate_effect 给主角随机推荐/单维 +10 属性 + 默契提升（前期约 +12 EV/人）
    """
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    growth_rates = state.get("growth_rates", {})
    active_states = state.get("active_states", set())
    round_num = state.get("round", 1)
    is_early = round_num <= 12
    w = strategy["weights"]
    base_w_map = w["stat_base_value"]
    growth_scale = float(w.get("growth_rate_synergy_scale", 0.35))
    iso_106_mult = float(w.get("isolated_106_catchup_extra_mult", 1.08))
    stamina_104_decay = float(w.get("stamina_104_early_decay_ratio", 0.85))

    meta = MINIGAME_CFG_CATALOG.get(gid)
    if not meta:
        tier_s = "困难" if price >= 80 else ("普通" if price >= 60 else "简单")
        base_g = 100 if price >= 80 else (80 if price >= 60 else 60)
        meta = {"stat": 0, "tier": tier_s, "range": str(base_g), "base_gain": base_g, "orig_price": price, "short": f"小游戏#{gid}"}

    st_id = meta["stat"]
    base_gain = float(meta["base_gain"])
    orig_price = int(meta["orig_price"])
    short_name = meta["short"]

    rem_trains = get_remaining_trains(state)
    if st_id == 0 and gid == 4:
        # 全维特级小游戏：按六维分别计算扣除 >=1200 折半与上限截断后的真实有效属性当量
        per_dim = base_gain / 6.0
        stat_ev = 0.0
        real_tot = 0
        for k in range(101, 107):
            ev_k, rk = calc_stat_gain_ev(
                k, per_dim, props, prop_max, strategy, round_num=round_num, apply_1200_discount=True, rem_trains=rem_trains
            )
            stat_ev += ev_k
            real_tot += rk
        if real_tot < int(round(base_gain)):
            short_name = f"{short_name}(实+{real_tot})"
    elif 101 <= st_id <= 106:
        stat_ev, real_g = calc_stat_gain_ev(
            st_id, base_gain, props, prop_max, strategy, round_num=round_num, apply_1200_discount=True, rem_trains=rem_trains
        )
        if real_g <= 0:
            short_name = f"{short_name}(⚠️已满+0)"
        elif real_g < int(round(base_gain)):
            short_name = f"{short_name}(实+{real_g})"
    else:
        stat_ev = base_gain

    # 若持有扭蛋『小游戏强化/+』对应真实状态（state_id 338:+20体能 / 339:+30体能 / 367:+60体能）
    extra_mg_sta = (
        (20 if 338 in active_states else 0)
        + (30 if 339 in active_states else 0)
        + (60 if 367 in active_states else 0)
    )
    if extra_mg_sta > 0:
        ev_mg_sta, _ = calc_stat_gain_ev(
            104, extra_mg_sta, props, prop_max, strategy, round_num=round_num, apply_1200_discount=True, rem_trains=rem_trains
        )
        stat_ev += ev_mg_sta

    # 街机驻留队友每人提供推荐/单维 +10 属性（若该维 >=1200 则折半为 +5）+ 队友默契提升
    under_1200_cnt = sum(1 for k in range(101, 107) if props.get(k, 200) < 1200)
    mate_unit_ev = round(6.0 + 5.5 * (under_1200_cnt / 6.0) + 1.0, 1)
    mate_ev = mates * mate_unit_ev
    # 前后期侧重：前2~3周纯属性小游戏无任何长期复利，且过早堆高单项固定属性(sub_type=20)会提前撞上 1200 折半墙，
    # 故前中期未打折的常规纯属性街机(gid != 4)适度折现，把周六金币留给周日博取长期增益扭蛋(训练增强/器材暴击/周六补助/#82读笔记/设施升级)！
    exp_r_mg = infer_exp_round(state)
    week_no_mg = min(7, (exp_r_mg - 1) // 7 + 1)
    focus_st_mg, _ = get_active_focus(strategy)
    if gid != 4:
        if week_no_mg <= 2:
            early_mg_scale = 0.86 if price < orig_price else 0.78
        elif week_no_mg == 3:
            early_mg_scale = 0.92 if price < orig_price else 0.85
        else:
            early_mg_scale = 1.0
        if focus_st_mg and st_id == focus_st_mg:
            early_mg_scale = min(1.0, early_mg_scale + 0.08)
        stat_ev *= early_mg_scale
    total_ev = round(stat_ev + mate_ev, 1)

    disc_tag = f"折{price}金" if price < orig_price else f"{price}金"
    return total_ev, short_name, disc_tag


def calc_shop_bond_item_ev(
    iid: int,
    price: int,
    state: dict,
    trammel_override: dict = None,
    strategy: Optional[dict] = None,
) -> dict:
    """
    基于 supactor_dict 实时器材熟练度 (0..100) 与本局剩余训练回合数，
    精确计算商店『全器材熟练+5/+10/+15』(1113/1111/1112) 的动态有效属性当量 (EV)（v0.10.0 与彩圈期权严格对齐）：
    - 区分 NON_CRIT_EQUIPMENT_IDS（哨子 11004 单独走 70/100 里程碑；其余不可暴击器材计 0 EV）；
    - 可暴击器材逐卡调用 calc_equip_trammel_option_ev(..., fractional_step=True) 计价，剩余回合来不及满彩自动归零；
    - 已满 100 熟练的器材溢出部分计 0 EV。
    """
    meta = STORE_ITEM_CATALOG.get(iid, {})
    gain = int(meta.get("trammel_gain", 5))
    rem_turns = get_remaining_trains(state)
    if rem_turns <= 0:
        return {"ev": 0.0, "eff_total": 0, "maxed_new": 0, "w_gain": 0, "tag": "末周0训"}

    w_strat = (strategy or {}).get("weights") or {}
    raw_map = trammel_override if trammel_override is not None else (
        state.get("supactor_trammel") or state.get("supactor_card_trammel") or {}
    )
    card_to_cfg = state.get("supactor_card_to_cfg", {})

    if raw_map:
        maxed_crit_cnt = _count_maxed_crit_cards(raw_map, card_to_cfg)
        eff_total = 0
        maxed_new = 0
        w_gain = 0
        ev_sum = 0.0
        whistle_tag = ""
        for k_id, cur_t in raw_map.items():
            cfg_id = card_to_cfg.get(k_id, k_id)
            cur_v = min(100, max(0, int(cur_t)))
            if cur_v >= 100:
                continue
            eff_g = min(gain, 100 - cur_v)
            eff_total += eff_g
            new_v = cur_v + eff_g
            if new_v >= 100 and int(cfg_id) not in NON_CRIT_EQUIPMENT_IDS:
                maxed_new += 1
            if int(cfg_id) == 11004:
                w_gain = eff_g
            ev_k, w_tag_k = _eval_single_card_trammel_gain_ev(
                cfg_id, cur_v, eff_g, rem_turns, state, w_strat,
                already_maxed_crit_cnt=maxed_crit_cnt,
                whistle_70_ev=8.0, whistle_100_ev=10.0,
            )
            ev_sum += ev_k
            if w_tag_k:
                whistle_tag = w_tag_k

        ev = round(ev_sum, 1)
        tag_parts = [f"+{eff_total}熟"]
        if whistle_tag:
            tag_parts.append(whistle_tag)
        elif maxed_new > 0:
            tag_parts.append(f"满{maxed_new}张")
        return {
            "ev": ev,
            "eff_total": eff_total,
            "maxed_new": maxed_new,
            "w_gain": w_gain,
            "tag": "·".join(tag_parts),
        }

    approx_one = calc_equip_trammel_option_ev(45, gain, rem_turns, w_strat, fractional_step=True)["ev"]
    eff_total = 5 * gain
    ev = round(4.0 * approx_one + gain * 0.45 * min(1.0, rem_turns / 20.0), 1)
    return {"ev": ev, "eff_total": eff_total, "maxed_new": 0, "w_gain": gain, "tag": f"约+{eff_total}熟"}


def calc_shop_facility_item_ev(iid: int, price: int, state: dict, strategy: dict) -> dict:
    """计算商店『训练设施直升书I/II』(13010..18020, +1/+2级) 的 P5 同口径动态复利 EV。"""
    return calc_shop_facility_book_p5_ev(iid, state, strategy)


def calc_keep_gold_ev(rem_gold: float, state: dict, is_final_single_weekend: bool = False) -> float:
    """
    计算周末结束带入下周的留存金币真实期望价值（围绕「下周六保120金抽T0彩蛋笔记#82」与平日商店周转金定价）：
    - 毕业前最后周末 (R48, is_final_single_weekend=True)：后续0回合，留存金币价值为 0；
    - 周一比赛固定 +70 金规律（除第1周 R01 与第4周海滩合宿 R22 无比赛外，第2/3/5/6周的周一 R08/R15/R29/R36 均有比赛固定 +70 金）：
      * 第1、2、4、5周周末结束：下周一比赛保底 +70 金（另加周六补助 state 322/323/492）。
        因此周日结束留足 `need_120 = max(0, 120 - 70 - sat_allow)`（无补助时即 **50金**）即可保底凑够下周六 **120金** 买彩蛋笔记；
        `50~120金` 区间则提供平日商店买药/买T0器材书的周转缓冲（或下周六抽完80金蛋后周日靠打工再凑120金）。
      * 第3周周末结束 (R21 ➔ 进入第4周海滩合宿 R22)：**海滩周一 R22 无比赛（+0金）**！
        因此必须在第3周周日结束时直接留足 **120金**（减去周六补助），才能在第4周周六 (R27) 握有 120 金防踏空彩蛋笔记！
    - 第6周周末 (R41-42)：下周固定3场比赛保底 +210 金，叠加周六补助后被动进账高达 210~320 金，按分段饱和递减计价。
    """
    if is_final_single_weekend or rem_gold <= 0:
        return 0.0
    g = float(rem_gold)
    gold_task = state.get("active_gold_task")
    exp_r_now = int(state.get("exp_round", 1) or 1)
    deadline_r = int((gold_task or {}).get("deadline_round", 31) or 31)
    act_st = state.get("active_states", set())
    sat_allow = 110.0 if 492 in act_st else (80.0 if 323 in act_st else (40.0 if 322 in act_st else 0.0))
    if (
        gold_task
        and gold_task.get("status") == 1
        and exp_r_now <= deadline_r
    ):
        target = float(gold_task.get("target", 650))
        # 固定于 R31(第5周周三)验收：仅扣减 R31 前必拿的周一比赛 +70 金 (R08/R15/R29，海滩 R22 无比赛) 与周六补助 (R06/13/20/27)
        passive_match = sum(70.0 for mr in (8, 15, 29) if exp_r_now < mr <= deadline_r)
        passive_sat = sum(sat_allow for sr in (6, 13, 20, 27) if exp_r_now < sr <= deadline_r) if sat_allow > 0 else 0.0
        eff_target = max(0.0, target - passive_match - passive_sat)
        if eff_target > 0:
            g_toward = min(g, eff_target)
            g_excess = max(0.0, g - eff_target)
            # 达标 R31 奖励 4 天『龙宫寺集团买单』(商店/扭蛋/小游戏全 0 金，综合约 +350~500 EV)：
            # - 常规 W1~W4 缺口按 1.65 EV/金：周日打工稳压 100 金非必拿蛋与公园划船，同时周六百目鬼高分加训(250~420+ EV)仍自然胜出；
            # - 若进入第 4 周 (exp_r>=22) 且距 eff_target 缺口仍 >140 金（单靠最后 1 个周日普通打工难以达标），升至 2.40 紧急保底 650 金！
            cur_g_now = float(state.get("gold", 0) or 0)
            urgent_deficit = (exp_r_now >= 22) and ((eff_target - cur_g_now) > 140.0)
            rate_toward = 2.40 if urgent_deficit else 1.65
            return round(g_toward * rate_toward + g_excess * 0.25, 1)
    exp_r = exp_r_now
    week_no = min(7, (exp_r - 1) // 7 + 1)
    arch = detect_lineup_archetype(state)
    arch_gold_mult = float(arch.get("keep_gold_mult", 1.0))
    if week_no >= 6:
        passive_w7 = 210.0 + sat_allow  # 下周3场比赛(3×70=210金) + R48周六补助
        extra_store_cap = 140.0 if arch.get("archetype") == "store" else 0.0
        tier1_cap = max(0.0, 260.0 + extra_store_cap - passive_w7)  # 刚需缺口（凑够R48的120金扭蛋/街机 + 商店高优属性道具）
        tier2_cap = max(0.0, 390.0 + extra_store_cap - passive_w7)  # 次级商店属性书消化上限
        g1 = min(g, tier1_cap)
        g2 = max(0.0, min(g, tier2_cap) - tier1_cap)
        g3 = max(0.0, g - max(tier1_cap, tier2_cap))
        return round((g1 * 0.28 + g2 * 0.16 + g3 * 0.08) * (1.0 + (arch_gold_mult - 1.0) * 0.5), 1)
    if week_no == 3:
        # 第3周周日(R21)特例：下周是第4周海滩合宿(R22)，周一无比赛(+0金)！必须直接带足 120 金出周日，下周六才有 120 金买彩蛋笔记
        need_120 = max(0.0, 120.0 - sat_allow)
        buf_cap = need_120 + 60.0
        g0 = min(g, need_120)
        g1 = max(0.0, min(g, buf_cap) - need_120)
        g2 = max(0.0, min(g, 240.0) - buf_cap)
        g3 = max(0.0, g - max(240.0, buf_cap))
        return round((g0 * 0.56 + g1 * 0.38 + g2 * 0.26 + g3 * 0.18) * arch_gold_mult, 1)
    # 第1、2、4、5周：下周一均有比赛固定 +70 金！留 50 金即可在下周六凑满 120 金（50+70=120），50~120 金为平日买药/T0道具及连抽缓冲
    passive_next = 70.0 + sat_allow
    need_120 = max(0.0, 120.0 - passive_next)  # 无周六补助时为 50 金；有 80/110 金周六补助时为 0 金！
    buf_cap = need_120 + (45.0 if (sat_allow >= 80.0 and week_no >= 4) else 70.0)
    g0 = min(g, need_120)
    g1 = max(0.0, min(g, buf_cap) - need_120)
    g2 = max(0.0, min(g, 200.0) - buf_cap)
    g3 = max(0.0, g - max(200.0, buf_cap))
    if sat_allow >= 80.0 and week_no >= 4:
        # 已持有『每周六+80/+110金』印钞机且进入第4~5周：后续被动金币高达 370~600 金，严防继续打工屯钱导致 R48 爆仓作废
        return round((g0 * 0.50 + g1 * 0.24 + g2 * 0.12 + g3 * 0.05) * arch_gold_mult, 1)
    w_mult = 1.0 if week_no <= 4 else 0.88
    # 第5周：>200 金的富余部分边际价值显著下降（实测第6周周末常攒 500+、R48 余 700+ 金花不完），g3 系数 0.18 ➔ 0.11（折后约 0.10/金）
    g3_rate = 0.18 if week_no <= 4 else 0.11
    return round((g0 * 0.62 + g1 * 0.40 + g2 * 0.28 + g3 * g3_rate) * w_mult * arch_gold_mult, 1)


def calc_sunday_blind_gasha_ev(
    budget: int,
    state: dict,
    sunday_pivot: dict = None,
    gasha_discount: bool = False,
) -> dict:
    """
    基于 edu_team_building_data.weight_distribution (如场外升满后 {1:35, 2:30, 3:25, 4:9})，
    精确遍历周日 3 台扭蛋机 4^3 = 64 种档位组合，计算在给定金币预算 budget 下的：
    - 3选1最优机台期望 EV (machine_ev)
    - 含剩余金币留存价值 (calc_keep_gold_ev 分段饱和折现)、『周日明牌期权托底（若刷出普通蛋则改买高EV商店道具+轻度打工）』
      以及『凹上限神局期权溢价（保留 >=100/120 金进周日博取 82彩色笔记 / 100熟练暴涨+ / 30器材暴击++ 的上分位爆发价值）』的综合期望 (total_ev)
    """
    w = state.get("gasha_weights") or {1: 35, 2: 30, 3: 25, 4: 9}
    tot_w = max(1, sum(w.values()))
    probs = {k: w.get(k, 0) / tot_w for k in (1, 2, 3, 4)}

    rem_turns = get_remaining_trains(state)
    note_stat = state.get("note_stat_sum", 0)
    note_cap = state.get("note_cap_sum", 0)
    note_ev = max(195.0, note_stat * 1.15 + note_cap * 0.22) if (note_stat > 0 or note_cap > 0) else 215.0
    # 凹上限神局视角：前中期周日刷出彩蛋(120金)/金蛋(100金)/紫蛋(80金)时，玩家可挑走 82读笔记 / 30器材暴击++ / 33训练增强++ / 59&60周六补助 / Lv+2&+3升级包，
    # 其长期复利价值显著高于前期单次纯属性小游戏，故前3周保留 >=80~120 金进周日享有长期增益期权溢价！
    high_roll_t4 = 28.0 if rem_turns >= 14 else (18.0 if rem_turns >= 10 else (10.0 if rem_turns >= 4 else 0.0))
    high_roll_t3 = 20.0 if rem_turns >= 14 else (12.0 if rem_turns >= 10 else (5.0 if rem_turns >= 4 else 0.0))
    high_roll_t2 = 12.0 if rem_turns >= 14 else (6.0 if rem_turns >= 10 else 0.0)
    arch_g_mult = float(detect_lineup_archetype(state).get("gasha_ev_mult", 1.0))
    # 82 号彩蛋笔记在任何流派都保底 100% 原值，仅对非笔记扭蛋池按流派微调
    tier4_ev = round(0.16 * note_ev + (0.74 * 112.0 + 0.10 * 12.0 + 5.6 + high_roll_t4) * arch_g_mult, 1)

    c1, c2, c3, c4 = (30, 40, 50, 60) if gasha_discount else (60, 80, 100, 120)
    tier_specs = {
        1: {"cost": c1, "ev": round(40.0 * arch_g_mult, 1)},
        2: {"cost": c2, "ev": round((62.0 + high_roll_t2) * arch_g_mult, 1)},
        3: {"cost": c3, "ev": round((90.0 + high_roll_t3) * arch_g_mult, 1)},
        4: {"cost": c4, "ev": tier4_ev},
    }

    pivot_ev = float(sunday_pivot["sunday_ev"]) if sunday_pivot else 0.0
    after_gasha_shop = sunday_pivot.get("after_gasha_shop", {}) if sunday_pivot else {}
    keep_budget_ev = calc_keep_gold_ev(budget, state, is_final_single_weekend=False)

    exp_total = 0.0
    exp_machine_ev = 0.0
    exp_left_gold = 0.0
    pivot_prob = 0.0

    for t1 in (1, 2, 3, 4):
        for t2 in (1, 2, 3, 4):
            for t3 in (1, 2, 3, 4):
                p_comb = probs[t1] * probs[t2] * probs[t3]
                if p_comb <= 0:
                    continue
                best_comb_tot = max(keep_budget_ev, pivot_ev)
                used_pivot = pivot_ev > keep_budget_ev
                best_m_ev = 0.0
                best_rem = budget
                for t in (t1, t2, t3):
                    spec = tier_specs[t]
                    if spec["cost"] <= budget:
                        rem = budget - spec["cost"]
                        rem_keep_ev = calc_keep_gold_ev(rem, state, is_final_single_weekend=False)
                        extra_shop = after_gasha_shop.get(rem)
                        if extra_shop:
                            after_shop_keep = calc_keep_gold_ev(rem - extra_shop["cost"], state, is_final_single_weekend=False)
                            shop_net = extra_shop["ev"] - (rem_keep_ev - after_shop_keep)
                        else:
                            shop_net = 0.0
                        cand_tot = spec["ev"] + rem_keep_ev + max(0.0, shop_net)
                        if cand_tot > best_comb_tot:
                            best_comb_tot = cand_tot
                            best_m_ev = spec["ev"]
                            best_rem = rem - (extra_shop["cost"] if extra_shop and shop_net > 0 else 0)
                            used_pivot = False
                if used_pivot:
                    pivot_prob += p_comb
                exp_total += p_comb * best_comb_tot
                exp_machine_ev += p_comb * best_m_ev
                exp_left_gold += p_comb * best_rem

    p4 = probs[4]
    p_rainbow_pct = round((1.0 - (1.0 - p4) ** 3) * 100.0, 1) if budget >= c4 else 0.0
    return {
        "total_ev": round(exp_total, 1),
        "machine_ev": round(exp_machine_ev, 1),
        "left_gold": round(exp_left_gold, 0),
        "p_rainbow_pct": p_rainbow_pct,
        "tier4_ev": tier4_ev,
        "pivot_prob_pct": round(pivot_prob * 100.0, 0),
    }


def evaluate_weekend_and_shop(state: dict, strategy: dict):
    """
    周末出行 (round_type = 2) 参谋（统一有效属性当量 EV + 商店道具机会成本 & 周日明牌期权联合 DP）：
    严格区分【周六（第1/2动，行动后扭蛋与街机会刷新，但商店货架周六➔周日不刷新）】与【周日（第2/2动，周末最后1动）】，
    结合当前器材熟练度缺口、下周必出事件（R10集训+30体、R24合宿+25体、R36许愿+40体、ID70/71器材三选一+40/+70体、决赛周2训/毕业周0训），
    对【周六动作 + 周日动作 + 商店买药/买熟练度/买设施书/买属性】统一计算包含机会成本 (Opportunity Cost) 的总 EV 并给出 Top 方案。
    """
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    gold = state.get("gold", 0)
    props = state.get("props", {})
    prop_max = state.get("prop_max", {})
    round_num = state.get("round", 1)
    wk_day = state.get("weekend_day", 1) or 1  # 1=周六(第1/2动), 2=周日(最后1动)

    sched_ctx = get_upcoming_schedule_context(state)
    arch = detect_lineup_archetype(state, strategy)
    arch_work_bonus = float(arch.get("work_ev_bonus", 0.0))
    arch_gasha_mult = float(arch.get("gasha_ev_mult", 1.0))
    arch_mg_mult = float(arch.get("minigame_ev_mult", 1.0))
    arch_shop_stat_buy = bool(arch.get("shop_stat_weekend_buy", False))
    week_no = sched_ctx["week_no"]
    next_week_trains = sched_ctx["next_week_trains"]
    lookahead_notes = sched_ctx["lookahead_notes"]
    is_final_single_weekend = next_week_trains == 0
    has_weekend_train = (
        wk_day == 1
        and bool(state.get("weekend_train"))
        and len(state.get("weekend_train_cmds") or []) == 6
    )
    if is_final_single_weekend:
        day_base = "周六(加训/外出·仅1动)" if has_weekend_train else "周六(仅1动·无周日)"
        day_tag = f"第{week_no}周(末周)·{day_base}"
    else:
        day_base = ("周六(1/2·可加训)" if has_weekend_train else "周六(1/2)") if wk_day == 1 else "周日(2/2)"
        day_tag = f"第{week_no}周·{day_base}"
    if arch.get("badge"):
        day_tag = f"{day_tag} · {arch['badge']}"
    cur_label = "周六" if wk_day == 1 else "周日"
    gold_keep_unit_ev = 0.0 if is_final_single_weekend else (0.36 if week_no <= 4 else 0.30)
    scored_sat_trains = (
        score_training_turn(state, state["weekend_train_cmds"], strategy)
        if has_weekend_train
        else []
    )

    def keep_gold_ev(rem_g: float) -> float:
        return calc_keep_gold_ev(rem_g, state, is_final_single_weekend=is_final_single_weekend)

    def _v_ps_single(fin_ps: int, extra_w_gain: int = 0, cap_override: Optional[int] = None) -> float:
        """
        v0.10.0 单套严格凹形终态体力价值函数 V_ps(fin_ps)（彻底根除正收益+负惩罚双重计价）：
        - 毕业前最后周末 (next_week_trains == 0)：恒返回 0.0；
        - 严格非递增边际斜率（低体有效单价 1.60~1.80 EV/体；海滩前夕 week_no==3 为 1.80~2.00 EV/体）：
          * [0, floor_ps - 18]        : 1.80 EV/体（海滩前夕 2.00）
          * [floor_ps - 18, floor_ps] : 1.60 EV/体（海滩前夕 1.80）
          * [floor_ps, target_ps]     : 0.95 EV/体（海滩前夕 1.15）
          * [target_ps, cap]          : 0.22~0.45 EV/体（按事件替代程度递减）
          * > cap                     : 0.0 EV/体（溢出归零）
        """
        if next_week_trains == 0:
            return 0.0
        ctx = get_upcoming_schedule_context(state, extra_whistle_gain=extra_w_gain) if extra_w_gain > 0 else sched_ctx
        cap = max(ps_cap, int(cap_override or ps_cap))
        x = max(0, min(cap, int(fin_ps)))
        floor_ps = min(cap, int(ctx["safe_ps_floor"]))
        target_ps = min(cap, max(floor_ps, int(ctx["target_pot_ps"])))
        deep_ps = max(0, floor_ps - 18)

        s_deep = 2.00 if week_no == 3 else 1.80
        s_low = 1.80 if week_no == 3 else 1.60
        s_mid = 1.15 if week_no == 3 else 0.95
        if ctx["queued_id71"]:
            s_hi = 0.25
        elif week_no == 5 or ctx["queued_id70"]:
            s_hi = 0.32
        elif week_no == 1:
            s_hi = 0.30
        elif week_no == 3:
            s_hi = 0.45
        elif week_no == 6:
            s_hi = 0.22
        else:
            s_hi = 0.38

        v = min(x, deep_ps) * s_deep
        if x > deep_ps:
            v += (min(x, floor_ps) - deep_ps) * s_low
        if x > floor_ps:
            v += (min(x, target_ps) - floor_ps) * s_mid
        if x > target_ps:
            v += (x - target_ps) * s_hi
        return round(v, 2)

    def _v_ps_dist(ps_dist: dict[int, float], extra_w_gain: int = 0, cap_override: Optional[int] = None) -> float:
        if next_week_trains == 0:
            return 0.0
        return sum(prob * _v_ps_single(p_v, extra_w_gain=extra_w_gain, cap_override=cap_override) for p_v, prob in ps_dist.items())

    def v_ps_delta(start_p: int, end_p: int, extra_w_gain: int = 0, cap_override: Optional[int] = None) -> float:
        """单套凹势函数的严格单步增量 V_ps(end_p) - V_ps(start_p)（可正可负，跨步严格 Telescoping 零重复计价）。"""
        if next_week_trains == 0:
            return 0.0
        return round(
            _v_ps_single(end_p, extra_w_gain=extra_w_gain, cap_override=cap_override)
            - _v_ps_single(start_p, extra_w_gain=extra_w_gain, cap_override=cap_override),
            1,
        )

    store_items = [it for it in state.get("store_items", []) if it["num"] > 0]
    w_mates = state.get("weekend_teammates", {})
    sels = state.get("weekend_selections", {})
    gashapons = state.get("gashapon_list", [])
    minigames = state.get("minigame_list", [])

    # 1. 盘点商店道具并计算每件道具的动态 EV：0金白嫖、体力药水、全器材熟练度、训练设施书、直接加属性道具
    free_items = []
    stamina_items = []
    value_items = []
    stat_shop_items = []
    inv_candidates = []  # 可参与周末金币预算分配与机会成本PK的商店投资项 (bond / facility / 高优stat)

    w_cfg = strategy["weights"]
    base_w_map = w_cfg["stat_base_value"]
    growth_scale = float(w_cfg.get("growth_rate_synergy_scale", 0.35))
    iso_106_mult = float(w_cfg.get("isolated_106_catchup_extra_mult", 1.08))
    stamina_104_decay = float(w_cfg.get("stamina_104_early_decay_ratio", 0.85))
    growth_rates = state.get("growth_rates", {})

    sorted_by_low = sorted(props.items(), key=lambda x: x[1] / max(1, prop_max.get(x[0], 1600)))
    short_stats = {k for k, _ in sorted_by_low[:2]}
    rec_stats = get_char_rec_stats(state)
    rem_trains_now = get_remaining_trains(state)

    def eval_shop_stat_item(iid: int, cnt: int, cur_props: dict) -> dict:
        """
        基于当前六维属性 cur_props 与上限 prop_max，计算购买 cnt 件商店属性道具 iid 的：
        - 真实入账属性点 real_gain（扣除 >=1200 折半与触顶归零）
        - 动态有效属性当量 ev
        - 购买后的递推属性字典 next_props
        - 精确展示标签 tag
        """
        meta = STORE_ITEM_CATALOG[iid]
        mode = meta.get("mode", "single")
        st_id = int(meta.get("stat", 0))
        raw_per = int(meta.get("gain", 20))
        per_stat = int(meta.get("per_stat", raw_per))
        sim_p = dict(cur_props)
        tot_ev = 0.0
        tot_real = 0.0

        for _ in range(max(1, cnt)):
            if mode == "all_6":
                for k in range(101, 107):
                    ev_k, real_k = calc_stat_gain_ev(
                        k, per_stat, sim_p, prop_max, strategy, round_num, True, rem_trains_now
                    )
                    tot_ev += ev_k
                    tot_real += real_k
                    sim_p[k] = min(prop_max.get(k, 1600), sim_p.get(k, 200) + real_k)
            elif mode == "rec_2":
                for k in rec_stats:
                    ev_k, real_k = calc_stat_gain_ev(
                        k, per_stat, sim_p, prop_max, strategy, round_num, True, rem_trains_now
                    )
                    tot_ev += ev_k
                    tot_real += real_k
                    sim_p[k] = min(prop_max.get(k, 1600), sim_p.get(k, 200) + real_k)
            elif mode == "rec_1":
                ev_sum = 0.0
                real_sum = 0.0
                step_reals = {}
                for k in rec_stats:
                    ev_k, real_k = calc_stat_gain_ev(
                        k, raw_per, sim_p, prop_max, strategy, round_num, True, rem_trains_now
                    )
                    ev_sum += 0.5 * ev_k
                    real_sum += 0.5 * real_k
                    step_reals[k] = real_k
                tot_ev += ev_sum
                tot_real += real_sum
                for k, rk in step_reals.items():
                    sim_p[k] = min(prop_max.get(k, 1600), int(round(sim_p.get(k, 200) + 0.5 * rk)))
            elif mode == "rand_1":
                ev_sum = 0.0
                real_sum = 0.0
                for k in range(101, 107):
                    ev_k, real_k = calc_stat_gain_ev(
                        k, raw_per, sim_p, prop_max, strategy, round_num, True, rem_trains_now
                    )
                    ev_sum += ev_k / 6.0
                    real_sum += real_k / 6.0
                tot_ev += ev_sum
                tot_real += real_sum
            else:
                ev_k, real_k = calc_stat_gain_ev(
                    st_id, raw_per, sim_p, prop_max, strategy, round_num, True, rem_trains_now
                )
                tot_ev += ev_k
                tot_real += real_k
                sim_p[st_id] = min(prop_max.get(st_id, 1600), sim_p.get(st_id, 200) + real_k)

        raw_tot = raw_per * max(1, cnt)
        real_disp = int(round(tot_real)) if abs(tot_real - round(tot_real)) < 0.05 else round(tot_real, 1)
        if mode == "all_6":
            p_tot = per_stat * max(1, cnt)
            if real_disp <= 0:
                tag = f"全属性+{p_tot}·⚠️已满+0"
            elif real_disp < raw_tot:
                tag = f"全属性+{p_tot}·实+{real_disp}"
            else:
                tag = f"全属性+{p_tot}·共+{raw_tot}"
        elif mode == "rec_1":
            full_recs = [STAT_NAMES.get(k, str(k)) for k in rec_stats if cur_props.get(k, 200) >= prop_max.get(k, 1600)]
            if real_disp <= 0:
                tag = f"随机推+{raw_tot}·⚠️双推已满+0"
            elif full_recs:
                tag = f"随机推+{raw_tot}·⚠️{full_recs[0]}满·实均+{real_disp}"
            elif real_disp < raw_tot:
                tag = f"随机推+{raw_tot}·实均+{real_disp}"
            else:
                rec_s = "/".join(STAT_NAMES.get(k, str(k)) for k in rec_stats)
                tag = f"随机推+{raw_tot}·{rec_s}"
        elif mode == "rec_2":
            if real_disp <= 0:
                tag = f"双推+{raw_tot}·⚠️已满+0"
            elif real_disp < raw_tot:
                tag = f"双推+{raw_tot}·实+{real_disp}"
            else:
                tag = f"双推+{raw_tot}"
        elif mode == "rand_1":
            tag = f"随机+{raw_tot}·实均+{real_disp}" if real_disp < raw_tot else f"随机+{raw_tot}"
        else:
            st_nm = STAT_NAMES.get(st_id, "属性")
            if real_disp <= 0:
                tag = f"+{raw_tot}{st_nm}·⚠️已满+0"
            elif real_disp < raw_tot:
                tag = f"+{raw_tot}{st_nm}·实+{real_disp}"
            else:
                tag = f"+{raw_tot}{st_nm}"

        return {
            "ev": round(tot_ev, 1),
            "real_gain": tot_real,
            "raw_gain": raw_tot,
            "next_props": sim_p,
            "tag": tag,
        }

    for it in store_items:
        iid, num, price = it["id"], it["num"], it["price"]
        meta = STORE_ITEM_CATALOG.get(iid)
        if price == 0 or (meta and meta["cat"] == "free"):
            name = meta["name"] if meta else f"免费道具#{iid}"
            free_items.append({"id": iid, "name": name, "num": num, "price": 0})
            continue

        if meta:
            clean_desc = re.sub(r"\([^)]*金[^)]*\)", "", meta["desc"]).strip()
            if meta["cat"] == "stamina":
                ps_gain = meta["ps"]
                is_gamble = bool(meta.get("is_gamble", False))
                max_ps = int(meta.get("max_ps", ps_gain))
                cost_per_ps = round(price / max(1, ps_gain), 2)
                stamina_items.append({
                    "id": iid,
                    "name": meta["name"],
                    "num": num,
                    "price": price,
                    "ps": ps_gain,
                    "max_ps": max_ps,
                    "is_gamble": is_gamble,
                    "cost_per_ps": cost_per_ps,
                })
            elif meta["cat"] == "bond" and next_week_trains > 0:
                b_info = calc_shop_bond_item_ev(iid, price, state, strategy=strategy)
                b_ev = b_info["ev"]
                net_over_keep = round(b_ev - price * gold_keep_unit_ev, 1)
                value_items.append({
                    "id": iid,
                    "name": meta["name"],
                    "price": price,
                    "ev": b_ev,
                    "tag": b_info["tag"],
                    "prio": round(50.0 + net_over_keep, 1),
                    "desc": clean_desc,
                })
                if b_ev > price * gold_keep_unit_ev + 8.0 and (b_ev / max(1, price)) >= 0.55:
                    inv_candidates.append({
                        "id": iid,
                        "cat": "bond",
                        "name": meta["name"],
                        "num": num,
                        "price": price,
                        "unit_ev": b_ev,
                        "w_gain": b_info["w_gain"],
                        "tag": b_info["tag"],
                    })
            elif meta["cat"] == "buff" and next_week_trains > 0:
                # 持续回合 Buff 在周六/周日购买会空转周六、周日、下周一比赛日（浪费2~3天），仅提醒留到下周二训练前再买
                days = meta.get("days", 3)
                subcat = meta.get("subcat", "")
                if subcat in ("equip_boost_ps", "equip_boost_free"):
                    crit_ready_cnt, tracked_cnt = get_crit_ready_equipment_count(state)
                    if tracked_cnt > 0 and crit_ready_cnt == 0:
                        value_items.append({
                            "id": iid,
                            "name": f"{meta['name']}(无暴击器材暂缓)",
                            "price": price,
                            "ev": 0.0,
                            "tag": "",
                            "prio": 15,
                            "desc": clean_desc,
                        })
                        continue
                value_items.append({
                    "id": iid,
                    "name": f"{meta['name']}(⏳周二买覆{min(4, days)}训)",
                    "price": price,
                    "ev": 0.0,
                    "tag": "",
                    "prio": 42 if meta["tier"] == "T0" else 35,
                    "desc": clean_desc,
                })
            elif meta["cat"] == "facility" and next_week_trains > 0:
                f_info = calc_shop_facility_item_ev(iid, price, state, strategy)
                f_ev = f_info["ev"]
                net_over_keep = round(f_ev - price * gold_keep_unit_ev, 1)
                value_items.append({
                    "id": iid,
                    "name": meta["name"],
                    "price": price,
                    "ev": f_ev,
                    "tag": f_info["tag"],
                    "prio": round(45.0 + net_over_keep, 1),
                    "desc": clean_desc,
                })
                if f_ev > price * gold_keep_unit_ev + 10.0 and (f_ev / max(1, price)) >= 0.52:
                    inv_candidates.append({
                        "id": iid,
                        "cat": "facility",
                        "name": meta["name"],
                        "num": num,
                        "price": price,
                        "unit_ev": f_ev,
                        "w_gain": 0,
                        "tag": f_info["tag"],
                    })
            elif meta["cat"] == "stat":
                st_id = meta.get("stat", 0)
                gain_v = int(meta.get("gain", 20))
                eval_1 = eval_shop_stat_item(iid, 1, props)
                unit_ev = eval_1["ev"]
                if unit_ev > 0:
                    stat_shop_items.append({
                        "id": iid,
                        "name": meta["name"],
                        "num": num,
                        "price": price,
                        "stat": st_id,
                        "gain": gain_v,
                        "unit_ev": unit_ev,
                        "tag_1": eval_1["tag"],
                    })
                    # 商店流(白神核等)：常规周末也允许把高性价比未折半的属性秘籍纳入商店清仓候选
                    if (
                        arch_shop_stat_buy
                        and next_week_trains > 0
                        and unit_ev > price * gold_keep_unit_ev + 6.0
                        and (unit_ev / max(1, price)) >= 0.50
                    ):
                        inv_candidates.append({
                            "id": iid,
                            "cat": "stat",
                            "name": meta["name"],
                            "num": 1,
                            "price": price,
                            "unit_ev": unit_ev,
                            "w_gain": 0,
                            "tag": eval_1["tag"],
                        })
                if st_id in short_stats or st_id == 0 or gain_v >= 35 or next_week_trains == 0:
                    eff_cost_w = 0.12 if next_week_trains == 0 else gold_keep_unit_ev
                    net_s = round(unit_ev - price * eff_cost_w, 1)
                    value_items.append({
                        "id": iid,
                        "name": meta["name"],
                        "price": price,
                        "ev": unit_ev,
                        "tag": eval_1["tag"],
                        "prio": (round(60.0 + net_s, 1) if next_week_trains == 0 else round(38.0 + net_s, 1)) if unit_ev > 0 else -10.0,
                        "desc": clean_desc,
                    })

    # 稳定必回体药水 (is_gamble=False: 1201/1202/1001~1003) 绝对优先于 50% 赌狗特价饮 (1161~1163)
    stamina_items.sort(key=lambda x: (x.get("is_gamble", False), x["cost_per_ps"], x["price"]))
    value_items.sort(key=lambda x: -x["prio"])
    stat_shop_items.sort(key=lambda x: -(x["unit_ev"] / max(1, x["price"])))
    inv_candidates.sort(key=lambda x: -((x["unit_ev"] - x["price"] * gold_keep_unit_ev) / max(1, x["price"])))

    def plan_final_shop_stats(avail_gold: int, init_props: dict = None) -> dict:
        """毕业前最后周末(R48, 仅周六1动)：在给定剩余金币预算 avail_gold 下，动态递推属性上限与1200折半求商店加属性道具的最优购买组合"""
        if not is_final_single_weekend or avail_gold <= 0 or not stat_shop_items:
            return {"str": "", "cost": 0, "ev": 0.0, "items": []}
        start_props = dict(init_props if init_props is not None else props)
        best_combo = (0.0, 0, [], [])  # (ev, -cost, picked_list, items_list)

        def _dfs(idx: int, rem_g: int, cur_p: dict, acc_ev: float, spent_g: int, picked: list, picked_items: list):
            nonlocal best_combo
            if (acc_ev, -spent_g) > (best_combo[0], best_combo[1]):
                best_combo = (acc_ev, -spent_g, list(picked), list(picked_items))
            if idx >= len(stat_shop_items):
                return
            it = stat_shop_items[idx]
            # 0 件分支
            _dfs(idx + 1, rem_g, cur_p, acc_ev, spent_g, picked, picked_items)
            for cnt in range(1, it["num"] + 1):
                cost_c = cnt * it["price"]
                if cost_c <= rem_g:
                    res_c = eval_shop_stat_item(it["id"], cnt, cur_p)
                    ev_c = res_c["ev"]
                    if ev_c <= 0:
                        break
                    cnt_s = f"×{cnt}" if cnt > 1 else ""
                    picked.append(f"{it['name']}{cnt_s}({res_c['tag']})")
                    picked_items.append({
                        "id": it["id"],
                        "count": cnt,
                        "cat": "stat",
                        "reason": f"末周清仓({res_c['tag']})",
                    })
                    _dfs(idx + 1, rem_g - cost_c, res_c["next_props"], round(acc_ev + ev_c, 1), spent_g + cost_c, picked, picked_items)
                    picked.pop()
                    picked_items.pop()

        _dfs(0, avail_gold, start_props, 0.0, 0, [], [])
        return {
            "str": "+".join(best_combo[2]),
            "cost": -best_combo[1],
            "ev": round(best_combo[0], 1),
            "items": best_combo[3],
        }

    def plan_shop_investments(avail_gold: int) -> dict:
        """
        在常规周末给定可用金币预算 avail_gold 下，求商店『全器材熟练度(1113/1111/1112) / 训练设施书』的最优投资组合：
        - 严格核算机会成本：仅当买入道具的动态 EV 显著高于留存金币成本 (price * gold_keep_unit_ev) 时才买入；
        - 若同时买入多件器材熟练度道具，动态递推熟练度上限(100)防重复高估溢出部分。
        """
        if is_final_single_weekend or avail_gold <= 0 or not inv_candidates:
            return {"str": "", "cost": 0, "ev": 0.0, "net_ev": 0.0, "w_gain": 0, "items": []}

        base_tram = dict(state.get("supactor_trammel") or state.get("supactor_card_trammel") or {})
        best_res = {"str": "", "cost": 0, "ev": 0.0, "net_ev": 0.0, "w_gain": 0, "items": []}

        def _dfs_inv(idx: int, rem_g: int, cur_tram: dict, acc_ev: float, spent_g: int, acc_w: int, picked: list, picked_items: list):
            nonlocal best_res
            net_now = round(acc_ev - spent_g * gold_keep_unit_ev, 1)
            if (net_now, -spent_g) > (best_res["net_ev"], -best_res["cost"]):
                best_res = {
                    "str": "+".join(picked),
                    "cost": spent_g,
                    "ev": round(acc_ev, 1),
                    "net_ev": net_now,
                    "w_gain": acc_w,
                    "items": list(picked_items),
                }
            if idx >= len(inv_candidates):
                return
            it = inv_candidates[idx]
            # 0 件分支
            _dfs_inv(idx + 1, rem_g, cur_tram, acc_ev, spent_g, acc_w, picked, picked_items)
            # 购买 1 件分支 (bond / facility 均限购 1 件)
            if it["price"] <= rem_g:
                if it["cat"] == "bond":
                    b_res = calc_shop_bond_item_ev(it["id"], it["price"], state, trammel_override=cur_tram, strategy=strategy)
                    item_ev = b_res["ev"]
                    w_add = b_res["w_gain"]
                    tag_s = b_res["tag"]
                    gain_v = STORE_ITEM_CATALOG[it["id"]]["trammel_gain"]
                    next_tram = {k: min(100, v + gain_v) for k, v in cur_tram.items()} if cur_tram else {}
                else:
                    item_ev = it["unit_ev"]
                    w_add = 0
                    tag_s = it["tag"]
                    next_tram = cur_tram
                if item_ev > it["price"] * gold_keep_unit_ev + 6.0:
                    picked.append(f"{it['name']}({it['price']}金·{tag_s})")
                    picked_items.append({
                        "id": it["id"],
                        "count": 1,
                        "cat": it["cat"],
                        "reason": f"周末投资({tag_s})",
                    })
                    _dfs_inv(
                        idx + 1,
                        rem_g - it["price"],
                        next_tram,
                        acc_ev + item_ev,
                        spent_g + it["price"],
                        acc_w + w_add,
                        picked,
                        picked_items,
                    )
                    picked.pop()
                    picked_items.pop()

        _dfs_inv(0, avail_gold, base_tram, 0.0, 0, 0, [], [])
        return best_res

    def plan_shop_potions(
        cur_ps: int,
        avail_gold: int,
        extra_w_gain: int = 0,
        cap_override: Optional[int] = None,
    ):
        """
        在给定当前体力 cur_ps 与可用预算 avail_gold 下，使用单套凹形势函数 V_ps 规划不浪费的体力药：
        - 稳定药水：直接转移 ps -> min(cap, ps + gain)；
        - 50% 赌狗药 (is_gamble=True: 1161/1162/1163)：严格按两点真实分布
          0.5 * V_ps(min(cap, p + max_ps)) + 0.5 * V_ps(p) 计算真实期望增量，消除线性代入 Jensen 偏差！
        """
        cap = max(ps_cap, int(cap_override or ps_cap))
        start_p = max(0, min(cap, int(cur_ps)))
        if next_week_trains == 0 or avail_gold <= 0 or not stamina_items:
            return {"str": "", "ps_gain": 0, "cost": 0, "ev": 0.0, "ps_dist": {start_p: 1.0}, "items": []}

        ctx = get_upcoming_schedule_context(state, extra_whistle_gain=extra_w_gain) if extra_w_gain > 0 else sched_ctx
        target_p = min(cap, int(ctx["target_pot_ps"]))
        floor_p = min(cap, int(ctx["safe_ps_floor"]))
        ps_dist: dict[int, float] = {start_p: 1.0}
        cur_v = _v_ps_dist(ps_dist, extra_w_gain=extra_w_gain, cap_override=cap)
        base_v = cur_v

        picked = []
        picked_items = []
        cost_g = 0
        for st_it in stamina_items:
            take_n = 0
            is_gamb = bool(st_it.get("is_gamble", False))
            step_max_p = int(st_it.get("max_ps", st_it["ps"]))
            step_exp_p = int(st_it["ps"])
            for _ in range(st_it["num"]):
                mean_p = sum(p_k * pr for p_k, pr in ps_dist.items())
                if mean_p >= target_p or (cost_g + st_it["price"] > avail_gold):
                    break
                if mean_p >= floor_p and (mean_p + step_exp_p - target_p) > 25:
                    break
                next_dist: dict[int, float] = {}
                if is_gamb:
                    for p_k, pr in ps_dist.items():
                        p_win = min(cap, p_k + step_max_p)
                        next_dist[p_win] = next_dist.get(p_win, 0.0) + 0.5 * pr
                        next_dist[p_k] = next_dist.get(p_k, 0.0) + 0.5 * pr
                else:
                    for p_k, pr in ps_dist.items():
                        p_nxt = min(cap, p_k + step_exp_p)
                        next_dist[p_nxt] = next_dist.get(p_nxt, 0.0) + pr
                next_v = _v_ps_dist(next_dist, extra_w_gain=extra_w_gain, cap_override=cap)
                marg_v = next_v - cur_v
                # 仅当真实两点期望收益高于金币机会成本门槛时才买入（防止高体溢出或赌狗药亏本强买）
                min_worth = st_it["price"] * max(0.32, gold_keep_unit_ev)
                if marg_v < min_worth:
                    break
                ps_dist = next_dist
                cur_v = next_v
                take_n += 1
                cost_g += st_it["price"]
            if take_n > 0:
                cnt_s = f"×{take_n}" if take_n > 1 else ""
                if is_gamb:
                    picked.append(f"{st_it['name']}{cnt_s}(🎲赌+{step_max_p*take_n}/0·均+{step_exp_p*take_n}体)")
                else:
                    picked.append(f"{st_it['name']}{cnt_s}(+{step_exp_p*take_n}体)")
                picked_items.append({
                    "id": st_it["id"],
                    "count": take_n,
                    "cat": "stamina",
                    "reason": f"周末回体({st_it['name']}{cnt_s})",
                })
        exp_end_p = sum(p_k * pr for p_k, pr in ps_dist.items())
        eff_gain = max(0, int(round(exp_end_p - start_p)))
        return {
            "str": "+".join(picked),
            "ps_gain": eff_gain,
            "cost": cost_g,
            "ev": round(max(0.0, cur_v - base_v), 1),
            "ps_dist": ps_dist,
            "items": picked_items,
        }

    # 2. 评估今日 6号建筑·扭蛋机 (#1~#3) 的精确动态 EV（按扣除金币机会成本后的净收益 net_ev 优选）
    # 单卡净 EV 的金币机会成本：与路线总分共用 keep_gold_ev 分段曲线的边际差（不再用固定 0.38/0.28 单价，避免与路线总分口径打架）
    def gold_opp_cost(price: int) -> float:
        if is_final_single_weekend or price <= 0:
            return 0.0
        g_now = max(float(gold), float(price))
        return round(keep_gold_ev(g_now) - keep_gold_ev(g_now - price), 1)

    ps_unit_now = get_stamina_unit_ev(state, ps_override=ps)
    scored_gashapon = []
    for g in gashapons:
        cid = g["cfg_id"]
        price = g["price"]
        total_mates = g["mates"]
        g_ev, short_tag, g_ps = calc_gashapon_dynamic_ev(cid, price, total_mates, state, strategy=strategy)
        g_cap_after = (ps_cap + 10) if cid == 107 else ps_cap
        # 若扭蛋带回体效果，用单套 V_ps 势函数增量统一口径（#107 按 ps_cap + 10 算回满！）
        if g_ps > 0:
            raw_mult = 1.0 if cid == 107 else 0.90
            raw_ps_ev = min(g_ps, max(0, g_cap_after - ps)) * (ps_unit_now * raw_mult)
            adj_ps_ev = v_ps_delta(ps, min(g_cap_after, ps + g_ps), cap_override=g_cap_after)
            g_ev = round(max(10.0, g_ev - raw_ps_ev + adj_ps_ev), 1)
        elif next_week_trains == 0 and cid in (97, 98, 99, 100, 28, 29, 30, 31, 32, 33, 58, 59, 60, 95, 96, 10151):
            # 毕业前最后周末(R48)：5天后生效的延迟满足(97/98)、金币蛋与后续训练乘区类扭蛋仅剩当场队友加点价值
            g_ev = round(6.0 + total_mates * 11.0, 1)
        # 流派感知微调：#82 笔记彩蛋与 0~20 金特惠神蛋在任何流派都保持 100% 必选，仅常规付费扭蛋按流派加权
        if arch_gasha_mult != 1.0 and cid != 82 and price > 20 and next_week_trains > 0:
            g_ev = round(g_ev * arch_gasha_mult, 1)
        net_g_ev = round(g_ev - gold_opp_cost(price), 1)
        mate_s = f"·👥{total_mates}" if total_mates > 0 else ""
        scored_gashapon.append({
            "idx": g["idx"],
            "cfg_id": cid,
            "price": price,
            "score": g_ev,
            "ev": g_ev,
            "net_ev": net_g_ev,
            "ps_gain": g_ps,
            "ps_cap_after": g_cap_after,
            "mates": total_mates,
            "short_tag": short_tag,
            "short": f"#{g['idx']} {short_tag}{mate_s}({price}金 EV:{g_ev:.0f}·净{net_g_ev:.0f})",
            "desc": f"#{g['idx']}【{short_tag}{mate_s}】({price}金)",
        })
    scored_gashapon.sort(key=lambda x: (x["net_ev"], x["ev"]), reverse=True)
    afford_gashas = [x for x in scored_gashapon if gold >= x["price"]]
    best_gasha = afford_gashas[0] if afford_gashas else (scored_gashapon[0] if scored_gashapon else None)
    gasha_cost = best_gasha["price"] if best_gasha else 60
    gasha_today_str = f"🎰6号{best_gasha['desc']}" if best_gasha else "🎰6号扭蛋"
    best_gasha_w_gain = 35 if (best_gasha and best_gasha["cfg_id"] in (100, 99)) else 0

    # 3. 评估今日 3号建筑·街机小游戏 (#1~#3) 的精确动态 EV（按净收益 net_ev 排序，确保 60金+1队友 优于 80金无队友）
    # ⚠️ 实机 UI 排布（2026-11-17 实机截图对照日志 games_list=[35(40金), 21(50金), 34(30金)] 修正）：
    #    服务端下发的 games_list 顺序随机，客户端 mini_game_choose_view 画面从左到右固定按难度/价格升序（简单→普通→困难→特级）排布
    mg_ui_order = sorted(
        minigames,
        key=lambda m: (
            int(m.get("price", 0)),
            int(MINIGAME_CFG_CATALOG.get(int(m["game_id"]), {}).get("base_gain", 0)),
            int(m.get("idx", 0)),
        ),
    )
    mg_idx_to_ui_slot = {int(m["idx"]): slot_i for slot_i, m in enumerate(mg_ui_order, 1)}
    scored_minigame = []
    for mg in minigames:
        gid = mg["game_id"]
        price = mg["price"]
        total_mates = mg["mates"]
        ui_slot = mg_idx_to_ui_slot.get(int(mg["idx"]), int(mg["idx"]))
        mg_ev, mg_name, disc_tag = calc_minigame_dynamic_ev(gid, price, total_mates, state, strategy)
        if arch_mg_mult != 1.0 and next_week_trains > 0:
            mg_ev = round(mg_ev * arch_mg_mult, 1)
        net_mg_ev = round(mg_ev - gold_opp_cost(price), 1)
        mate_s = f"·👥{total_mates}" if total_mates > 0 else ""
        scored_minigame.append({
            "idx": mg["idx"],
            "ui_slot": ui_slot,
            "game_id": gid,
            "price": price,
            "mates": total_mates,
            "score": mg_ev,
            "ev": mg_ev,
            "net_ev": net_mg_ev,
            "short_tag": mg_name,
            "short": f"【{mg_name}】{mate_s}({disc_tag} EV:{mg_ev:.0f}·净{net_mg_ev:.0f})",
            "desc": f"【{mg_name}{mate_s}】({disc_tag})",
        })
    scored_minigame.sort(key=lambda x: (x["net_ev"], x["ev"]), reverse=True)
    afford_mgs = [x for x in scored_minigame if gold >= x["price"]]
    best_mg = afford_mgs[0] if afford_mgs else (scored_minigame[0] if scored_minigame else None)
    gasha_disc_active = (sels.get(17, {}).get("status", 1) == 2)
    mg_disc_active = (sels.get(6, {}).get("status", 1) == 2)
    mg_spec_active = (sels.get(6, {}).get("status", 1) == 3)
    out_spec_active = (sels.get(13, {}).get("status", 1) == 3)

    gasha_disc_tag = "🏷️5折" if (gasha_disc_active and any(g["price"] in (30, 40, 50) for g in gashapons)) else "🏷️特价"
    mg_disc_tag = "🏷️5折" if (mg_disc_active and any(m["price"] in (30, 40, 50) for m in minigames)) else "🏷️特价"

    if gasha_disc_active and best_gasha:
        gasha_today_str = f"🎰6号{gasha_disc_tag}{best_gasha['desc']}"
    mg_cost = best_mg["price"] if best_mg else 60
    mg_prefix = "🎮3号🔥特级" if mg_spec_active else (f"🎮3号{mg_disc_tag}" if mg_disc_active else "🎮3号")
    mg_today_str = f"{mg_prefix}{best_mg['desc']}" if best_mg else "🎮3号街机"

    # 4. 评估 8号打工 与 1号休息/散心 的精确 EV（含急招2倍、散心特活及单周末生效一次的状态转移）
    work_light = sels.get(19) or sels.get(3) or {
        "sel_id": 19, "ps_cost": 0, "base_gold": 60, "tips_gold": 20, "total_gold": 80, "status": 1
    }
    work_heavy = sels.get(20) or sels.get(5) or {
        "sel_id": 20, "ps_cost": -25, "base_gold": 110, "tips_gold": 30, "total_gold": 140, "status": 1
    }
    wl_urgent = (work_light.get("status", 1) == 4)
    wh_urgent = (work_heavy.get("status", 1) == 4)
    wl_mates = w_mates.get(19, 0) + w_mates.get(3, 0)
    wh_mates = w_mates.get(20, 0) + w_mates.get(5, 0)
    out_mates = w_mates.get(13, 0) + w_mates.get(1, 0)
    rest_mates = w_mates.get(14, 0) + w_mates.get(2, 0)

    wl_base = int(work_light.get("base_gold", 0) or max(60, work_light.get("total_gold", 80) - 20))
    wh_base = int(work_heavy.get("base_gold", 0) or max(110, work_heavy.get("total_gold", 140) - 30))
    wl_tips = int(work_light.get("tips_gold", 20) or 20)
    wh_tips = int(work_heavy.get("tips_gold", 30) or 30)

    # 当日保底到手金币 = 面板基础工资(急招已翻倍) + 驻留队友每人固定 +10 金；随机小费与急招暴富期权计入直接 EV
    # 注意：打工驻留队友的 +10 金已计入 wl_today_gold/wh_today_gold，额外仅触发队友自身默契提升(sub_type=29, 约+4.0 EV/人)
    mate_10_pairs = [
        calc_stat_gain_ev(k, 10, props, prop_max, strategy, round_num, True, rem_trains_now)
        for k in range(101, 107)
    ]
    mate_10_ev_stat = sum(p[0] for p in mate_10_pairs) / 6.0
    mate_10_real = sum(p[1] for p in mate_10_pairs) / 6.0
    mate_stat_10_ev = round(mate_10_ev_stat + 1.0, 1)
    wl_today_gold = wl_base + wl_mates * 10
    wh_today_gold = wh_base + wh_mates * 10
    wl_tip_ev = round(wl_tips * 0.6 * gold_keep_unit_ev + (8.0 if (wl_urgent and next_week_trains > 0) else 0.0), 1)
    wh_tip_ev = round(wh_tips * 0.6 * gold_keep_unit_ev + (12.0 if (wh_urgent and next_week_trains > 0) else 0.0), 1)

    work_arch_add = arch_work_bonus if next_week_trains > 0 else 0.0
    wl_ev_direct = round(wl_mates * 4.0 + (0.0 if next_week_trains == 0 else 4.5) + wl_tip_ev + work_arch_add, 1)

    def _calc_wh_dir_ev(mates_cnt: int, is_urg: bool, extra_w_gain: int = 0, start_ps: int = ps, cap_override: Optional[int] = None) -> float:
        ps_ev_d = 0.0 if next_week_trains == 0 else v_ps_delta(start_ps, max(0, start_ps - 25), extra_w_gain=extra_w_gain, cap_override=cap_override)
        t_ev = round(wh_tips * 0.6 * gold_keep_unit_ev + (12.0 if (is_urg and next_week_trains > 0) else 0.0), 1)
        return round(mates_cnt * 4.0 + (0.0 if next_week_trains == 0 else 4.5) + t_ev + work_arch_add + ps_ev_d, 1)

    wh_ev_direct = _calc_wh_dir_ev(wh_mates, wh_urgent, extra_w_gain=0, start_ps=ps)

    wl_short = f"💼8号{'🔥急招' if wl_urgent else ''}轻打工(+{wl_today_gold}金" + (f"·👥{wl_mates})" if wl_mates else ")")
    wh_short = f"🔨8号{'🔥急招' if wh_urgent else ''}重打工(-25体+{wh_today_gold}金" + (f"·👥{wh_mates})" if wh_mates else ")")

    # 周六未打工时，急招状态保留到周日；周六已打工时，周日急招已消耗恢复 1 倍基础工资
    sun_wl_keep_gold = wl_base
    sun_wh_keep_gold = wh_base
    sun_wl_keep_short = f"💼8号{'🔥急招' if wl_urgent else ''}轻打工(+{sun_wl_keep_gold}金)"
    sun_wh_keep_short = f"🔨8号{'🔥急招' if wh_urgent else ''}重打工(-25体+{sun_wh_keep_gold}金)"
    sun_wl_keep_ev = round((0.0 if next_week_trains == 0 else 4.5) + wl_tip_ev + work_arch_add, 1)
    sun_wl_after_work_gold = (wl_base // 2) if wl_urgent else wl_base

    rest_est_gain = 70 + rest_mates * 20
    rest_short = f"🧺1号外出野餐(+{rest_est_gain}体" + (f"·👥{rest_mates})" if rest_mates else ")")
    out_20_pairs = [
        calc_stat_gain_ev(k, 20, props, prop_max, strategy, round_num, True, rem_trains_now)
        for k in range(101, 107)
    ]
    out_20_ev = sum(p[0] for p in out_20_pairs) / 6.0
    out_20_real = sum(p[1] for p in out_20_pairs) / 6.0
    out_mult = 2 if out_spec_active else 1
    out_base_raw = 20 * out_mult
    out_base_stat_ev = round(out_20_ev * out_mult, 1)
    out_mate_raw = out_mates * 10
    out_raw_total = out_base_raw + out_mate_raw
    out_real_total = int(round(out_20_real * out_mult + out_mates * mate_10_real))
    out_stat_str = f"+{out_base_raw}+{out_mate_raw}属" if out_mates > 0 else f"+{out_base_raw}属"
    if out_real_total < out_raw_total:
        out_stat_str += f"·实+{out_real_total}"
    out_short = (
        f"🚣1号{'🔥划船特活' if out_spec_active else '公园划船'}(+30体{out_stat_str}"
        + (f"·👥{out_mates})" if out_mates else ")")
    )
    out_no_mate_real = int(round(out_20_real * out_mult))
    out_no_mate_stat_str = f"+{out_base_raw}属" + (f"·实+{out_no_mate_real}" if out_no_mate_real < out_base_raw else "")
    sun_out_no_mate_short = f"🚣1号{'🔥划船特活' if out_spec_active else '公园划船'}(+30体{out_no_mate_stat_str})"
    # 仅当体力真正低于下周安全底线 safe_ps_floor 时才将外出野餐纳入备选，杜绝中高体力(如70~100体)纯回体空过
    rest_check_ps = sched_ctx["safe_ps_floor"]
    # 外出野餐的驻留队友 +20体/人 已计入 rest_est_gain (70 + 20 * rest_mates)，不再重复加 +10~14 属性分；且周末纯回体亏损发育/生钱行动轮次
    rest_mate_bond_ev = 2.5 if week_no <= 4 else 0.0
    # 机会成本已自然体现在「未执行扭蛋/街机就没拿到其 EV」的直接对比中，不再额外重复倒扣 -14.0
    weekend_rest_opp_pen = 0.0

    def _build_sunday_pivot_ctx(
        sun_morning_gold: int,
        base_w_gain: int = 0,
        sun_work_gold: Optional[int] = None,
        gasha_disc: bool = False,
    ) -> dict:
        """
        构造周日明牌期权上下文：
        因为商店货架在周六➔周日不刷新（下周一才刷新），若周日早上持有 sun_morning_gold：
        - 若周日刷出极品彩蛋/金蛋，则直接抽蛋（若有余钱再顺手买商店高优道具）；
        - 若周日扭蛋池平庸，则可在周日早上用 sun_morning_gold 先买商店高优道具 (如全器材熟练+15)，再点周日 8号轻度打工 托底！
        - 即使商店无高优投资道具 (inv_p["cost"] <= 0)，周日放弃烂池改点轻度打工的保底收益也必须计入托底期权！
        """
        if sun_work_gold is None:
            sun_work_gold = sun_wl_keep_gold
        inv_p = plan_shop_investments(sun_morning_gold)
        after_map = {}
        tier_costs = (30, 40, 50, 60) if gasha_disc else (60, 80, 100, 120)
        for c_tier in tier_costs:
            if sun_morning_gold >= c_tier:
                rem_after_g = sun_morning_gold - c_tier
                inv_rem = plan_shop_investments(rem_after_g)
                if inv_rem["cost"] > 0:
                    after_map[rem_after_g] = inv_rem
        sun_wl_base = sun_wl_keep_ev if next_week_trains > 0 else 0.0
        if inv_p["cost"] <= 0:
            rem_no_inv = sun_morning_gold + sun_work_gold
            return {
                "sunday_ev": round(sun_wl_base + keep_gold_ev(rem_no_inv), 1),
                "inv": inv_p,
                "after_gasha_shop": after_map,
            }
        rem_end = sun_morning_gold - inv_p["cost"] + sun_work_gold
        sunday_ev = round(inv_p["ev"] + sun_wl_base + keep_gold_ev(rem_end), 1)
        return {"sunday_ev": sunday_ev, "inv": inv_p, "after_gasha_shop": after_map}

    # 5. 生成基于统一 EV 的极简决策路线（去掉标题与分步的重复啰嗦，直接单行呈现①周六➔②周日）
    plan_steps = []
    if free_items:
        f_names = "、".join(x["name"] for x in free_items)
        plan_steps.append(f"🎁 先领商店 0 金【{f_names}】")

    if wk_day == 1 and not is_final_single_weekend:
        routes = []

        def _append_sunday_work_routes(
            sat_key_prefix: str,
            sat_family: str,
            sat_str: str,
            sat_ev: float,
            ps_sun_start: int,
            gold_sun_start: int,
            base_w_gain: int = 0,
            cap_after_sat: Optional[int] = None,
        ):
            """当周六未执行打工时，周日仍保留轻度打工及（若有急招双倍）重度打工选项。"""
            cap_eff = max(ps_cap, int(cap_after_sat or ps_cap))
            inv_sw = plan_shop_investments(gold_sun_start)
            tot_w_sw = base_w_gain + inv_sw["w_gain"]
            gold_for_pot_sw = gold_sun_start - inv_sw["cost"]

            # 选项 1：周日轻度打工（若周六有轻打工急招，周日仍在）
            pot_sw_l = plan_shop_potions(ps_sun_start, gold_for_pot_sw, extra_w_gain=tot_w_sw, cap_override=cap_eff)
            rem_gold_l = gold_for_pot_sw - pot_sw_l["cost"] + sun_wl_keep_gold
            ev_sw_l = round(
                sat_ev + inv_sw["ev"] + sun_wl_keep_ev + pot_sw_l["ev"] + keep_gold_ev(rem_gold_l),
                1,
            )
            shop_l_parts = [x for x in (inv_sw["str"], pot_sw_l["str"]) if x]
            shop_s_l = f"🛒{'+'.join(shop_l_parts)} + " if shop_l_parts else ""
            routes.append({
                "key": f"{sat_key_prefix}_sun_work_light",
                "sat_family": sat_family,
                "total_ev": ev_sw_l,
                "compact": f"①周六 {sat_str} ➔ ②周日 {shop_s_l}{sun_wl_keep_short}",
            })

            # 选项 2：若重度打工处于急招 2 倍状态（周六未打工则周日仍在），评估周日急招重打工
            if wh_urgent and ps_sun_start >= 40:
                ps_after_sun_wh = ps_sun_start - 25
                pot_sw_h = plan_shop_potions(ps_after_sun_wh, gold_for_pot_sw, extra_w_gain=tot_w_sw, cap_override=cap_eff)
                rem_gold_h = gold_for_pot_sw - pot_sw_h["cost"] + sun_wh_keep_gold
                sun_wh_dir_ev = _calc_wh_dir_ev(0, True, extra_w_gain=tot_w_sw, start_ps=ps_sun_start, cap_override=cap_eff)
                ev_sw_h = round(
                    sat_ev + inv_sw["ev"] + sun_wh_dir_ev + pot_sw_h["ev"] + keep_gold_ev(rem_gold_h),
                    1,
                )
                shop_h_parts = [x for x in (inv_sw["str"], pot_sw_h["str"]) if x]
                shop_s_h = f"🛒{'+'.join(shop_h_parts)} + " if shop_h_parts else ""
                routes.append({
                    "key": f"{sat_key_prefix}_sun_work_heavy",
                    "sat_family": sat_family,
                    "total_ev": ev_sw_h,
                    "compact": f"①周六 {sat_str} ➔ ②周日 {shop_s_h}{sun_wh_keep_short}",
                })

        # 路线族 A：周六先拿今日最优扭蛋 best_gasha（防止周日被刷新覆盖）
        if best_gasha and gold >= gasha_cost:
            g_cap_sat = max(ps_cap, int(best_gasha.get("ps_cap_after", ps_cap)))
            ps_after_sat = min(g_cap_sat, ps + best_gasha["ps_gain"])
            gold_after_sat = gold - gasha_cost

            # A1: 周六抽扭蛋 ➔ 周日(先用周日早上现钱买商店高优道具 +) 打工(含周六保留下来的急招双倍！) + 体力药
            _append_sunday_work_routes(
                "sat_gasha", "sat_gasha", gasha_today_str, best_gasha["ev"],
                ps_after_sat, gold_after_sat, base_w_gain=best_gasha_w_gain, cap_after_sat=g_cap_sat,
            )

            # A2: 周六抽扭蛋 ➔ 周日外出野餐/公园划船 (+ 可选商店高优道具；周六未划船则划船特活保留至周日)
            if (ps_after_sat < rest_check_ps or out_spec_active) and next_week_trains > 0:
                inv_a2 = plan_shop_investments(gold_after_sat)
                tot_w_a2 = best_gasha_w_gain + inv_a2["w_gain"]
                rest_gain = min(70, max(0, g_cap_sat - ps_after_sat))
                rest_ev = round(v_ps_delta(ps_after_sat, min(g_cap_sat, ps_after_sat + rest_gain), extra_w_gain=tot_w_a2, cap_override=g_cap_sat) - weekend_rest_opp_pen, 1)
                out_gain = min(30, max(0, g_cap_sat - ps_after_sat))
                out_ev = round(
                    v_ps_delta(ps_after_sat, min(g_cap_sat, ps_after_sat + out_gain), extra_w_gain=tot_w_a2, cap_override=g_cap_sat) + out_base_stat_ev,
                    1,
                )
                if out_spec_active and (ps_after_sat + 30 >= sched_ctx["safe_ps_floor"]) and out_ev >= rest_ev:
                    sun_act_str = sun_out_no_mate_short
                    sun_r_ev = out_ev
                else:
                    sun_act_str = "🧺1号外出野餐(+70体)"
                    sun_r_ev = rest_ev
                rem_g_a2 = gold_after_sat - inv_a2["cost"]
                ev_a2 = round(best_gasha["ev"] + inv_a2["ev"] + sun_r_ev + keep_gold_ev(rem_g_a2), 1)
                inv_pre_a2 = f"🛒{inv_a2['str']} + " if inv_a2["str"] else ""
                routes.append({
                    "key": "sat_gasha_sun_rest",
                    "sat_family": "sat_gasha",
                    "total_ev": ev_a2,
                    "compact": f"①周六 {gasha_today_str} ➔ ②周日 {inv_pre_a2}{sun_act_str}",
                })

            # A3: 资金充裕时，周六抽今日扭蛋 ➔ 周日留钱看池抽盲盒（周六已抽过扭蛋，故周六若有扭蛋8折特价已在周六消耗，周日恢复原价）
            pot_a3 = plan_shop_potions(ps_after_sat, gold_after_sat, extra_w_gain=best_gasha_w_gain, cap_override=g_cap_sat)
            rem_for_sun_gasha = gold_after_sat - pot_a3["cost"]
            if gold_after_sat >= 60:
                best_ev_a3 = -1e9
                best_compact_a3 = ""
                if rem_for_sun_gasha >= 60:
                    piv_a3 = _build_sunday_pivot_ctx(rem_for_sun_gasha, best_gasha_w_gain, sun_work_gold=sun_wl_keep_gold, gasha_disc=False)
                    blind_a3 = calc_sunday_blind_gasha_ev(rem_for_sun_gasha, state, sunday_pivot=piv_a3, gasha_discount=False)
                    best_ev_a3 = round(best_gasha["ev"] + pot_a3["ev"] + blind_a3["total_ev"], 1)
                    pot_pre_a3 = f"🛒{pot_a3['str']} + " if pot_a3["str"] else ""
                    piv_str_a3 = f"(普通池则🛒{piv_a3['inv']['str'].split('(')[0]}+💼打工)" if piv_a3["inv"]["str"] else ""
                    best_compact_a3 = f"①周六 {gasha_today_str} ➔ ②周日 {pot_pre_a3}🎰抽盲盒{piv_str_a3}"
                # 若周六余钱原本够抽 100/120 金扭蛋，但前置买药会卡死高阶扭蛋预算，则同时评估『周日先看扭蛋池（60金池或普通池再顺手买药）』
                if pot_a3["cost"] > 0 and gold_after_sat >= 100 > rem_for_sun_gasha:
                    piv_a3_def = _build_sunday_pivot_ctx(gold_after_sat, best_gasha_w_gain, sun_work_gold=sun_wl_keep_gold, gasha_disc=False)
                    if gold_after_sat - 60 >= pot_a3["cost"]:
                        piv_a3_def.setdefault("after_gasha_shop", {})[gold_after_sat - 60] = {"cost": pot_a3["cost"], "ev": pot_a3["ev"]}
                    blind_a3_def = calc_sunday_blind_gasha_ev(gold_after_sat, state, sunday_pivot=piv_a3_def, gasha_discount=False)
                    ev_a3_def = round(best_gasha["ev"] + blind_a3_def["total_ev"], 1)
                    if ev_a3_def > best_ev_a3:
                        best_ev_a3 = ev_a3_def
                        best_compact_a3 = f"①周六 {gasha_today_str} ➔ ②周日 🎰先看池抽盲盒(余钱/普通池再🛒{pot_a3['str']}+💼打工)"
                if best_ev_a3 > -1e8:
                    routes.append({
                        "key": "sat_gasha_sun_blind",
                        "sat_family": "sat_gasha",
                        "total_ev": best_ev_a3,
                        "compact": best_compact_a3,
                    })

            # A4: 若周六有街机特级机台或街机特价（周六抽扭蛋未碰街机，特活保留至周日！），评估周日玩街机（注意：普通街机三选一与队友周日会刷新，仅特级机台cfg_id=4保留）
            if best_mg and (mg_spec_active or mg_disc_active):
                sun_mg_cost_a4 = mg_cost if mg_spec_active else (45 if mg_disc_active else 90)
                if gold_after_sat >= sun_mg_cost_a4:
                    if mg_spec_active:
                        sun_mg_ev_a4 = best_mg["ev"]
                    else:
                        avg_hard = sum(calc_minigame_dynamic_ev(gid_k, 100, 0, state, strategy)[0] for gid_k in (3, 9, 12, 21, 33, 36)) / 6.0
                        avg_norm = sum(calc_minigame_dynamic_ev(gid_k, 80, 0, state, strategy)[0] for gid_k in (2, 8, 11, 20, 32, 35)) / 6.0
                        sun_mg_ev_a4 = round(0.50 * avg_hard + 0.50 * avg_norm + 3.2, 1)
                    sun_mg_lbl_a4 = mg_today_str if mg_spec_active else (f"🎮3号{mg_disc_tag}周日街机" if mg_disc_active else "🎮3号周日街机")
                    rem_after_sun_mg = gold_after_sat - sun_mg_cost_a4
                    inv_a4 = plan_shop_investments(rem_after_sun_mg)
                    tot_w_a4 = best_gasha_w_gain + inv_a4["w_gain"]
                    pot_a4 = plan_shop_potions(ps_after_sat, rem_after_sun_mg - inv_a4["cost"], extra_w_gain=tot_w_a4, cap_override=g_cap_sat)
                    rem_g_a4 = rem_after_sun_mg - inv_a4["cost"] - pot_a4["cost"]
                    ev_a4 = round(
                        best_gasha["ev"] + sun_mg_ev_a4 + inv_a4["ev"] + pot_a4["ev"] + keep_gold_ev(rem_g_a4),
                        1,
                    )
                    shop_a4_parts = [x for x in (inv_a4["str"], pot_a4["str"]) if x]
                    shop_s_a4 = f"🛒{'+'.join(shop_a4_parts)} + " if shop_a4_parts else ""
                    routes.append({
                        "key": "sat_gasha_sun_mg",
                        "sat_family": "sat_gasha",
                        "total_ev": ev_a4,
                        "compact": f"①周六 {gasha_today_str} ➔ ②周日 {shop_s_a4}{sun_mg_lbl_a4}",
                    })

        # 路线族 C：周六先玩今日最优 3号街机小游戏 best_mg
        if best_mg and gold >= mg_cost:
            gold_after_mg = gold - mg_cost
            # C1: 周六玩街机 ➔ 周日先买商店高优道具(如全器材熟练+15) + 打工(周六未打工，故急招2倍保留至周日！)
            _append_sunday_work_routes(
                "sat_mg", "sat_minigame", mg_today_str, best_mg["ev"],
                ps, gold_after_mg, base_w_gain=0,
            )

            # C2: 周六玩街机(未碰扭蛋，若周六有扭蛋特价则保留至周日！) ➔ 周日看明牌抽盲盒，普通池则买商店道具+打工
            pot_c2 = plan_shop_potions(ps, gold_after_mg)
            rem_for_sun_g = gold_after_mg - pot_c2["cost"]
            min_gasha_c2 = 30 if gasha_disc_active else 60
            high_gasha_c2 = 50 if gasha_disc_active else 100
            if gold_after_mg >= min_gasha_c2:
                best_ev_c2 = -1e9
                best_compact_c2 = ""
                blind_lbl_c2 = f"🎰{gasha_disc_tag}抽盲盒" if gasha_disc_active else "🎰抽盲盒"
                if rem_for_sun_g >= min_gasha_c2:
                    piv_c2 = _build_sunday_pivot_ctx(rem_for_sun_g, 0, sun_work_gold=sun_wl_keep_gold, gasha_disc=gasha_disc_active)
                    blind_c2 = calc_sunday_blind_gasha_ev(rem_for_sun_g, state, sunday_pivot=piv_c2, gasha_discount=gasha_disc_active)
                    best_ev_c2 = round(best_mg["ev"] + pot_c2["ev"] + blind_c2["total_ev"], 1)
                    pot_pre_c2 = f"🛒{pot_c2['str']} + " if pot_c2["str"] else ""
                    piv_str_c2 = f"(或🛒{piv_c2['inv']['str'].split('(')[0]}+💼打工)" if piv_c2["inv"]["str"] else ""
                    best_compact_c2 = f"①周六 {mg_today_str} ➔ ②周日 {pot_pre_c2}{blind_lbl_c2}{piv_str_c2}"
                # 若周六玩完街机后余钱 >= 100/120金（够抽周日高阶金蛋/彩蛋），但前置买药会卡死高阶扭蛋预算，则同时评估『周日先看池抽盲盒（60金池或普通池再买药）』
                if pot_c2["cost"] > 0 and gold_after_mg >= high_gasha_c2 > rem_for_sun_g:
                    piv_c2_def = _build_sunday_pivot_ctx(gold_after_mg, 0, sun_work_gold=sun_wl_keep_gold, gasha_disc=gasha_disc_active)
                    if gold_after_mg - min_gasha_c2 >= pot_c2["cost"]:
                        piv_c2_def.setdefault("after_gasha_shop", {})[gold_after_mg - min_gasha_c2] = {
                            "cost": pot_c2["cost"],
                            "ev": pot_c2["ev"],
                        }
                    blind_c2_def = calc_sunday_blind_gasha_ev(gold_after_mg, state, sunday_pivot=piv_c2_def, gasha_discount=gasha_disc_active)
                    ev_c2_def = round(best_mg["ev"] + blind_c2_def["total_ev"], 1)
                    if ev_c2_def > best_ev_c2:
                        best_ev_c2 = ev_c2_def
                        best_compact_c2 = f"①周六 {mg_today_str} ➔ ②周日 {blind_lbl_c2}(余钱/普通池再🛒{pot_c2['str']}+💼打工)"
                if best_ev_c2 > -1e8:
                    routes.append({
                        "key": "sat_mg_sun_blind",
                        "sat_family": "sat_minigame",
                        "total_ev": best_ev_c2,
                        "compact": best_compact_c2,
                    })

            if (ps < rest_check_ps or out_spec_active) and next_week_trains > 0:
                inv_c3 = plan_shop_investments(gold_after_mg)
                w_c3 = inv_c3["w_gain"]
                rest_gain_c3 = min(70, max(0, ps_cap - ps))
                rest_ev_c3 = round(v_ps_delta(ps, min(ps_cap, ps + rest_gain_c3), extra_w_gain=w_c3) - weekend_rest_opp_pen, 1)
                out_gain_c3 = min(30, max(0, ps_cap - ps))
                out_ev_c3 = round(v_ps_delta(ps, min(ps_cap, ps + out_gain_c3), extra_w_gain=w_c3) + out_base_stat_ev, 1)
                if out_spec_active and (ps + 30 >= sched_ctx["safe_ps_floor"]) and out_ev_c3 >= rest_ev_c3:
                    sun_act_c3 = sun_out_no_mate_short
                    sun_ev_c3 = out_ev_c3
                else:
                    sun_act_c3 = "🧺1号外出野餐(+70体)"
                    sun_ev_c3 = rest_ev_c3
                rem_g_c3 = gold_after_mg - inv_c3["cost"]
                ev_c3 = round(best_mg["ev"] + inv_c3["ev"] + sun_ev_c3 + keep_gold_ev(rem_g_c3), 1)
                inv_pre_c3 = f"🛒{inv_c3['str']} + " if inv_c3["str"] else ""
                routes.append({
                    "key": "sat_mg_sun_rest",
                    "sat_family": "sat_minigame",
                    "total_ev": ev_c3,
                    "compact": f"①周六 {mg_today_str} ➔ ②周日 {inv_pre_c3}{sun_act_c3}",
                })

        # 路线族 B：周六先打工攒钱 ➔ 周日(买高优商店道具 +) 抽盲盒扭蛋 / 玩特活街机 / 寝室休息
        for w_mode, w_info, w_today_g, w_dir_ev_base, w_lbl, w_is_urg in (
            ("light", work_light, wl_today_gold, wl_ev_direct, wl_short, wl_urgent),
            ("heavy", work_heavy, wh_today_gold, wh_ev_direct, wh_short, wh_urgent),
        ):
            ps_after_w = ps + w_info["ps_cost"]
            # 仅重度打工(-25体)要求剩余体力 >= 15；轻度打工(0体)任何体力均可执行！
            if w_info["ps_cost"] < 0 and ps_after_w < 15:
                continue
            gold_after_w = gold + w_today_g
            pot_b = plan_shop_potions(ps_after_w, gold_after_w)
            fin_ps_b = min(ps_cap, ps_after_w + pot_b["ps_gain"])
            # 普通重度打工(-25体)若买完药后仍低于安全高体线 max(72, safe_ps_floor+8)，则优先用 0 耗体的轻度打工，不硬扣 25 体！
            if w_mode == "heavy" and not w_is_urg and fin_ps_b < max(72, sched_ctx["safe_ps_floor"] + 8):
                continue
            budget_sun = gold_after_w - pot_b["cost"]
            # 周六已打工，故周日若托底再打工则恢复 1 倍基础工资；若周六有扭蛋8折特价（周六未抽扭蛋），则8折保留至周日！
            piv_b = _build_sunday_pivot_ctx(budget_sun, 0, sun_work_gold=sun_wl_after_work_gold, gasha_disc=gasha_disc_active)
            blind_b = calc_sunday_blind_gasha_ev(budget_sun, state, sunday_pivot=piv_b, gasha_discount=gasha_disc_active)
            ev_b = round(w_dir_ev_base + pot_b["ev"] + blind_b["total_ev"], 1)
            pot_part = f"🛒{pot_b['str']} + " if pot_b["str"] else ""
            # 若周六打工后资金充裕(买完商店道具仍够抽周日盲盒)，显式对比『周日先买商店道具(如全器材熟练+15凑满哨子回体)再抽盲盒』
            inv_b_pre = plan_shop_investments(budget_sun)
            min_gasha_b = 30 if gasha_disc_active else 60
            if inv_b_pre["cost"] > 0 and (budget_sun - inv_b_pre["cost"]) >= min_gasha_b:
                w_gain_bi = inv_b_pre["w_gain"]
                pot_bi = plan_shop_potions(ps_after_w, gold_after_w - inv_b_pre["cost"], extra_w_gain=w_gain_bi)
                rem_after_inv_pot = gold_after_w - inv_b_pre["cost"] - pot_bi["cost"]
                if rem_after_inv_pot >= min_gasha_b:
                    blind_b_inv = calc_sunday_blind_gasha_ev(rem_after_inv_pot, state, gasha_discount=gasha_disc_active)
                    w_dir_ev_bi = (
                        _calc_wh_dir_ev(wh_mates, wh_urgent, extra_w_gain=w_gain_bi, start_ps=ps)
                        if w_mode == "heavy" else w_dir_ev_base
                    )
                    ev_b_inv = round(
                        w_dir_ev_bi + pot_bi["ev"] + inv_b_pre["ev"] + blind_b_inv["total_ev"],
                        1,
                    )
                    if ev_b_inv >= ev_b:
                        ev_b = ev_b_inv
                        pot_part = f"🛒{'+'.join(x for x in (pot_bi['str'], inv_b_pre['str']) if x)} + "

            blind_lbl_b = f"🎰{gasha_disc_tag}抽盲盒" if gasha_disc_active else "🎰抽盲盒"
            routes.append({
                "key": f"sat_work_{w_mode}_sun_blind",
                "sat_family": "sat_work",
                "total_ev": ev_b,
                "compact": f"①周六 {w_lbl} ➔ ②周日 {pot_part}{blind_lbl_b}",
            })

            # B3: 周六打工攒钱 ➔ 周日玩 3号街机（注意：普通街机三选一与驻留队友在周日会重新随机刷新，仅特级机台 status=3 保留；无街机特活/打折时不靠盲盒街机虚高抢盲盒扭蛋路线）
            if (best_mg and mg_spec_active) or mg_disc_active:
                sun_mg_cost_b3 = mg_cost if (best_mg and mg_spec_active) else 45
                if gold_after_w >= sun_mg_cost_b3:
                    if best_mg and mg_spec_active:
                        sun_mg_ev_b3 = best_mg["ev"]
                    else:
                        avg_hard_b3 = sum(calc_minigame_dynamic_ev(gid_k, 100, 0, state, strategy)[0] for gid_k in (3, 9, 12, 21, 33, 36)) / 6.0
                        avg_norm_b3 = sum(calc_minigame_dynamic_ev(gid_k, 80, 0, state, strategy)[0] for gid_k in (2, 8, 11, 20, 32, 35)) / 6.0
                        sun_mg_ev_b3 = round(0.50 * avg_hard_b3 + 0.50 * avg_norm_b3 + 3.2, 1)
                    sun_mg_lbl_b3 = mg_today_str if (best_mg and mg_spec_active) else f"🎮3号{mg_disc_tag}周日街机(约45金)"
                    rem_after_mg_b3 = gold_after_w - sun_mg_cost_b3
                    inv_b3 = plan_shop_investments(rem_after_mg_b3)
                    w_b3 = inv_b3["w_gain"]
                    pot_b3 = plan_shop_potions(ps_after_w, rem_after_mg_b3 - inv_b3["cost"], extra_w_gain=w_b3)
                    rem_g_b3 = rem_after_mg_b3 - inv_b3["cost"] - pot_b3["cost"]
                    w_dir_ev_b3 = (
                        _calc_wh_dir_ev(wh_mates, wh_urgent, extra_w_gain=w_b3, start_ps=ps)
                        if w_mode == "heavy" else w_dir_ev_base
                    )
                    ev_b3 = round(
                        w_dir_ev_b3 + sun_mg_ev_b3 + inv_b3["ev"] + pot_b3["ev"] + keep_gold_ev(rem_g_b3),
                        1,
                    )
                    shop_b3_parts = [x for x in (inv_b3["str"], pot_b3["str"]) if x]
                    shop_s_b3 = f"🛒{'+'.join(shop_b3_parts)} + " if shop_b3_parts else ""
                    routes.append({
                        "key": f"sat_work_{w_mode}_sun_mg",
                        "sat_family": "sat_work",
                        "total_ev": ev_b3,
                        "compact": f"①周六 {w_lbl} ➔ ②周日 {shop_s_b3}{sun_mg_lbl_b3}",
                    })

            # B2: 周六打工攒钱 ➔ 周日(买商店高优道具 +) 外出野餐/划船特活补体
            if (ps_after_w < rest_check_ps or out_spec_active) and next_week_trains > 0:
                inv_b2 = plan_shop_investments(gold_after_w)
                w_b2 = inv_b2["w_gain"]
                w_dir_ev_b2 = (
                    _calc_wh_dir_ev(wh_mates, wh_urgent, extra_w_gain=w_b2, start_ps=ps)
                    if w_mode == "heavy" else w_dir_ev_base
                )
                sun_rest_gain = min(70, max(0, ps_cap - ps_after_w))
                sun_rest_ev = v_ps_delta(ps_after_w, min(ps_cap, ps_after_w + sun_rest_gain), extra_w_gain=w_b2) - weekend_rest_opp_pen
                sun_out_gain = min(30, max(0, ps_cap - ps_after_w))
                sun_out_ev = v_ps_delta(ps_after_w, min(ps_cap, ps_after_w + sun_out_gain), extra_w_gain=w_b2) + out_base_stat_ev
                if out_spec_active and (ps_after_w + 30 >= sched_ctx["safe_ps_floor"]) and sun_out_ev >= sun_rest_ev:
                    sun_act_b2 = sun_out_no_mate_short
                    sun_r_ev_b2 = sun_out_ev
                else:
                    sun_act_b2 = "🧺1号外出野餐(+70体)"
                    sun_r_ev_b2 = sun_rest_ev
                rem_g_b2 = gold_after_w - inv_b2["cost"]
                ev_b2 = round(w_dir_ev_b2 + inv_b2["ev"] + sun_r_ev_b2 + keep_gold_ev(rem_g_b2), 1)
                inv_pre_b2 = f"🛒{inv_b2['str']} + " if inv_b2["str"] else ""
                routes.append({
                    "key": f"sat_work_{w_mode}_sun_rest",
                    "sat_family": "sat_work",
                    "total_ev": ev_b2,
                    "compact": f"①周六 {w_lbl} ➔ ②周日 {inv_pre_b2}{sun_act_b2}",
                })

        # 路线族 D：周六公园划船特活/多人划船（或极低体无药时的外出野餐）➔ 周日打工或抽盲盒
        # 注意：『①周六 🧺外出野餐 ➔ ②周日 💼打工』在博弈上恒被『①周六 💼打工 ➔ ②周日 看池机动/野餐』严格支配（周六打工能让周日早上多握60~90金看新扭蛋池！）
        if (ps < rest_check_ps or out_spec_active) and next_week_trains > 0:
            sat_rest_candidates = []
            ps_cap_sat = max(ps_cap, int(ps))
            if ps < rest_check_ps:
                sat_rest_candidates.append(
                    ("rest", rest_short, min(70 + rest_mates * 20, max(0, ps_cap_sat - ps)), rest_mates * rest_mate_bond_ev - weekend_rest_opp_pen)
                )
            if out_mates > 0 or out_spec_active:
                sat_rest_candidates.append(
                    ("out", out_short, min(30, max(0, ps_cap_sat - ps)), out_base_stat_ev + out_mates * mate_stat_10_ev)
                )
            for r_key, r_lbl, r_gain, r_mate_ev in sat_rest_candidates:
                ps_after_r = min(ps_cap_sat, ps + r_gain)
                r_ps_ev = v_ps_delta(ps, ps_after_r)
                sat_r_ev = round(r_ps_ev + r_mate_ev, 1)

                # D1: 仅外出散心允许接周日打工（周六寝室休息接周日打工恒不如周六先打工拿钱看周日池）
                if r_key != "rest":
                    _append_sunday_work_routes(
                        f"sat_{r_key}", "sat_rest", r_lbl, sat_r_ev,
                        ps_after_r, gold, base_w_gain=0,
                    )

                # D2: 周六散心/休息 ➔ 周日抽盲盒扭蛋（周六未抽扭蛋，若有特价则保留至周日）
                min_gasha_d2 = 30 if gasha_disc_active else 60
                if gold >= min_gasha_d2:
                    pot_d2 = plan_shop_potions(ps_after_r, gold)
                    rem_g_d2 = gold - pot_d2["cost"]
                    if rem_g_d2 >= min_gasha_d2:
                        piv_d2 = _build_sunday_pivot_ctx(rem_g_d2, 0, sun_work_gold=sun_wl_keep_gold, gasha_disc=gasha_disc_active)
                        blind_d2 = calc_sunday_blind_gasha_ev(rem_g_d2, state, sunday_pivot=piv_d2, gasha_discount=gasha_disc_active)
                        ev_d2 = round(sat_r_ev + pot_d2["ev"] + blind_d2["total_ev"], 1)
                        pot_pre_d2 = f"🛒{pot_d2['str']} + " if pot_d2["str"] else ""
                        blind_lbl_d2 = f"🎰{gasha_disc_tag}抽盲盒" if gasha_disc_active else "🎰抽盲盒"
                        routes.append({
                            "key": f"sat_{r_key}_sun_blind",
                            "sat_family": "sat_rest",
                            "total_ev": ev_d2,
                            "compact": f"①周六 {r_lbl} ➔ ②周日 {pot_pre_d2}{blind_lbl_d2}",
                        })

        # 路线族 E：百目鬼『周末加训』(weekend_train=true) — 周六 0 金执行加训(耗体-80%)，且 100% 保留周六全部外出特活/特价/急招至周日！
        if has_weekend_train and scored_sat_trains:
            card_to_cfg_wk = state.get("supactor_card_to_cfg", {})
            cur_w_tram = int(state.get("supactor_trammel", {}).get(11004, 0))
            step_w_tram = 20 if 421 in state.get("active_states", set()) else 13

            for t_item in scored_sat_trains:
                t_idx = t_item["idx"]
                t_cmd = t_item["cmd"]
                ps_delta = int(t_cmd.get("ps", 0))
                ps_after_t = max(0, min(ps_cap, ps + ps_delta))
                gold_after_t = gold
                whistle_unmaxed_on_t = (
                    cur_w_tram < 100
                    and any(
                        card_to_cfg_wk.get(sc["card_id"], sc["card_id"]) == 11004 and sc.get("val", 0) < 2
                        for sc in (t_cmd.get("sup_cards") or [])
                    )
                )
                t_w_gain = step_w_tram if whistle_unmaxed_on_t else 0
                sat_ps_ev = v_ps_delta(ps, ps_after_t, extra_w_gain=t_w_gain)
                sat_t_ev = round(float(t_item.get("non_ps_score", t_item["score"])) + sat_ps_ev, 1)
                ps_lbl_t = f"+{ps_delta}体" if ps_delta > 0 else f"{ps_delta}体"
                sat_t_str = f"🏋️加训【{t_item['name']}】({ps_lbl_t})"
                sat_fam_key = f"sat_train_{t_idx}"

                # E1: 周六加训 ➔ 周日(买商店高优道具 +) 轻度打工 / 急招重度打工(周六未打工，急招2倍100%保留！) + 体力药
                _append_sunday_work_routes(
                    sat_fam_key,
                    sat_fam_key,
                    sat_t_str,
                    sat_t_ev,
                    ps_after_t,
                    gold_after_t,
                    base_w_gain=t_w_gain,
                )

                # E2: 周六加训(0金消耗，且扭蛋5折/8折特价100%保留至周日！) ➔ 周日看明牌抽盲盒(普通池则转买商店道具+打工)
                pot_e2 = plan_shop_potions(ps_after_t, gold_after_t, extra_w_gain=t_w_gain)
                rem_for_sun_e2 = gold_after_t - pot_e2["cost"]
                min_gasha_e2 = 30 if gasha_disc_active else 60
                high_gasha_e2 = 50 if gasha_disc_active else 100
                if gold_after_t >= min_gasha_e2:
                    best_ev_e2 = -1e9
                    best_compact_e2 = ""
                    blind_lbl_e2 = f"🎰{gasha_disc_tag}抽盲盒" if gasha_disc_active else "🎰抽盲盒"
                    if rem_for_sun_e2 >= min_gasha_e2:
                        piv_e2 = _build_sunday_pivot_ctx(
                            rem_for_sun_e2, t_w_gain, sun_work_gold=sun_wl_keep_gold, gasha_disc=gasha_disc_active
                        )
                        blind_e2 = calc_sunday_blind_gasha_ev(
                            rem_for_sun_e2, state, sunday_pivot=piv_e2, gasha_discount=gasha_disc_active
                        )
                        best_ev_e2 = round(sat_t_ev + pot_e2["ev"] + blind_e2["total_ev"], 1)
                        pot_pre_e2 = f"🛒{pot_e2['str']} + " if pot_e2["str"] else ""
                        piv_str_e2 = f"(或🛒{piv_e2['inv']['str'].split('(')[0]}+💼打工)" if piv_e2["inv"]["str"] else ""
                        best_compact_e2 = f"①周六 {sat_t_str} ➔ ②周日 {pot_pre_e2}{blind_lbl_e2}{piv_str_e2}"

                        # 资金宽裕时，同步对比『先买商店高优投资(如全器材熟练度)再抽周日盲盒』
                        inv_e2_pre = plan_shop_investments(rem_for_sun_e2)
                        if inv_e2_pre["cost"] > 0 and (rem_for_sun_e2 - inv_e2_pre["cost"]) >= min_gasha_e2:
                            tot_w_e2i = t_w_gain + inv_e2_pre["w_gain"]
                            pot_e2i = plan_shop_potions(
                                ps_after_t, gold_after_t - inv_e2_pre["cost"], extra_w_gain=tot_w_e2i
                            )
                            rem_after_e2i = gold_after_t - inv_e2_pre["cost"] - pot_e2i["cost"]
                            if rem_after_e2i >= min_gasha_e2:
                                blind_e2i = calc_sunday_blind_gasha_ev(
                                    rem_after_e2i, state, gasha_discount=gasha_disc_active
                                )
                                sat_ps_e2i = v_ps_delta(ps, ps_after_t, extra_w_gain=tot_w_e2i)
                                sat_t_ev_e2i = round(
                                    float(t_item.get("non_ps_score", t_item["score"])) + sat_ps_e2i, 1
                                )
                                ev_e2i = round(
                                    sat_t_ev_e2i
                                    + pot_e2i["ev"]
                                    + inv_e2_pre["ev"]
                                    + blind_e2i["total_ev"],
                                    1,
                                )
                                if ev_e2i >= best_ev_e2:
                                    best_ev_e2 = ev_e2i
                                    shop_e2i = "+".join(x for x in (pot_e2i["str"], inv_e2_pre["str"]) if x)
                                    best_compact_e2 = f"①周六 {sat_t_str} ➔ ②周日 🛒{shop_e2i} + {blind_lbl_e2}"

                    if pot_e2["cost"] > 0 and gold_after_t >= high_gasha_e2 > rem_for_sun_e2:
                        piv_e2_def = _build_sunday_pivot_ctx(
                            gold_after_t, t_w_gain, sun_work_gold=sun_wl_keep_gold, gasha_disc=gasha_disc_active
                        )
                        if gold_after_t - min_gasha_e2 >= pot_e2["cost"]:
                            piv_e2_def.setdefault("after_gasha_shop", {})[gold_after_t - min_gasha_e2] = {
                                "cost": pot_e2["cost"],
                                "ev": pot_e2["ev"],
                            }
                        blind_e2_def = calc_sunday_blind_gasha_ev(
                            gold_after_t, state, sunday_pivot=piv_e2_def, gasha_discount=gasha_disc_active
                        )
                        ev_e2_def = round(sat_t_ev + blind_e2_def["total_ev"], 1)
                        if ev_e2_def > best_ev_e2:
                            best_ev_e2 = ev_e2_def
                            best_compact_e2 = (
                                f"①周六 {sat_t_str} ➔ ②周日 {blind_lbl_e2}(余钱/普通池再🛒{pot_e2['str']}+💼打工)"
                            )
                    if best_ev_e2 > -1e8:
                        routes.append({
                            "key": f"{sat_fam_key}_sun_blind",
                            "sat_family": sat_fam_key,
                            "total_ev": best_ev_e2,
                            "compact": best_compact_e2,
                        })

                # E3: 周六加训(街机特级机台/5折特价100%保留至周日！) ➔ 周日玩特活/打折街机
                if (best_mg and mg_spec_active) or mg_disc_active:
                    sun_mg_cost_e3 = mg_cost if (best_mg and mg_spec_active) else 45
                    if gold_after_t >= sun_mg_cost_e3:
                        if best_mg and mg_spec_active:
                            sun_mg_ev_e3 = best_mg["ev"]
                        else:
                            avg_hard_e3 = sum(
                                calc_minigame_dynamic_ev(gid_k, 100, 0, state, strategy)[0]
                                for gid_k in (3, 9, 12, 21, 33, 36)
                            ) / 6.0
                            avg_norm_e3 = sum(
                                calc_minigame_dynamic_ev(gid_k, 80, 0, state, strategy)[0]
                                for gid_k in (2, 8, 11, 20, 32, 35)
                            ) / 6.0
                            sun_mg_ev_e3 = round(0.50 * avg_hard_e3 + 0.50 * avg_norm_e3 + 3.2, 1)
                        sun_mg_lbl_e3 = (
                            mg_today_str if (best_mg and mg_spec_active) else f"🎮3号{mg_disc_tag}周日街机(约45金)"
                        )
                        rem_after_mg_e3 = gold_after_t - sun_mg_cost_e3
                        inv_e3 = plan_shop_investments(rem_after_mg_e3)
                        tot_w_e3 = t_w_gain + inv_e3["w_gain"]
                        pot_e3 = plan_shop_potions(
                            ps_after_t, rem_after_mg_e3 - inv_e3["cost"], extra_w_gain=tot_w_e3
                        )
                        rem_g_e3 = rem_after_mg_e3 - inv_e3["cost"] - pot_e3["cost"]
                        ev_e3 = round(
                            sat_t_ev
                            + sun_mg_ev_e3
                            + inv_e3["ev"]
                            + pot_e3["ev"]
                            + keep_gold_ev(rem_g_e3),
                            1,
                        )
                        shop_e3_parts = [x for x in (inv_e3["str"], pot_e3["str"]) if x]
                        shop_s_e3 = f"🛒{'+'.join(shop_e3_parts)} + " if shop_e3_parts else ""
                        routes.append({
                            "key": f"{sat_fam_key}_sun_mg",
                            "sat_family": sat_fam_key,
                            "total_ev": ev_e3,
                            "compact": f"①周六 {sat_t_str} ➔ ②周日 {shop_s_e3}{sun_mg_lbl_e3}",
                        })

                # E4: 周六加训(划船特活100%保留至周日！) ➔ 周日外出野餐/划船特活补体
                if (ps_after_t < rest_check_ps or out_spec_active) and next_week_trains > 0:
                    inv_e4 = plan_shop_investments(gold_after_t)
                    tot_w_e4 = t_w_gain + inv_e4["w_gain"]
                    rest_gain_e4 = min(70, max(0, ps_cap - ps_after_t))
                    rest_ev_e4 = round(
                        v_ps_delta(ps_after_t, min(ps_cap, ps_after_t + rest_gain_e4), extra_w_gain=tot_w_e4) - weekend_rest_opp_pen,
                        1,
                    )
                    out_gain_e4 = min(30, max(0, ps_cap - ps_after_t))
                    out_ev_e4 = round(
                        v_ps_delta(ps_after_t, min(ps_cap, ps_after_t + out_gain_e4), extra_w_gain=tot_w_e4) + out_base_stat_ev,
                        1,
                    )
                    if out_spec_active and (ps_after_t + 30 >= sched_ctx["safe_ps_floor"]) and out_ev_e4 >= rest_ev_e4:
                        sun_act_e4 = sun_out_no_mate_short
                        sun_ev_e4 = out_ev_e4
                    else:
                        sun_act_e4 = "🧺1号外出野餐(+70体)"
                        sun_ev_e4 = rest_ev_e4
                    rem_g_e4 = gold_after_t - inv_e4["cost"]
                    ev_e4 = round(
                        sat_t_ev + inv_e4["ev"] + sun_ev_e4 + keep_gold_ev(rem_g_e4),
                        1,
                    )
                    inv_pre_e4 = f"🛒{inv_e4['str']} + " if inv_e4["str"] else ""
                    routes.append({
                        "key": f"{sat_fam_key}_sun_rest",
                        "sat_family": sat_fam_key,
                        "total_ev": ev_e4,
                        "compact": f"①周六 {sat_t_str} ➔ ②周日 {inv_pre_e4}{sun_act_e4}",
                    })

        if not routes:
            routes.append({
                "key": "sat_fallback_work",
                "sat_family": "sat_work",
                "total_ev": round(wl_ev_direct + keep_gold_ev(gold + wl_today_gold), 1),
                "compact": f"①周六 {wl_short} ➔ ②周日 视刷出池子行动",
            })

        routes.sort(key=lambda r: r["total_ev"], reverse=True)
        top_route = routes[0]
        # 备选优先展示不同周六起手动作（例如首选是周六街机➔周日急招打工，备选展示周六直接急招打工➔周日抽盲盒/街机），若无不同家族则取次优
        alt_route = next((r for r in routes[1:] if r["sat_family"] != top_route["sat_family"]), None)
        if alt_route is None and len(routes) > 1:
            alt_route = routes[1]

        headline = f"👑 首选(EV:{top_route['total_ev']:.0f})：{top_route['compact']}"
        alt_compact = ""
        alt_diff_ev = 0.0
        if alt_route:
            diff_ev = top_route["total_ev"] - alt_route["total_ev"]
            alt_compact = alt_route["compact"]
            alt_diff_ev = round(diff_ev, 1)
            plan_steps.append(
                f"🥈 备选(EV:{alt_route['total_ev']:.0f}, -{diff_ev:.0f}{'·≈打平,按喜好选' if diff_ev <= 2.0 else ''})：{alt_route['compact']}"
            )

        def _route_to_act(r_item: dict) -> tuple[int, int]:
            fam = r_item.get("sat_family", "")
            rk = r_item.get("key", "")
            if fam.startswith("sat_train_"):
                return (0, int(fam.split("_")[-1]))
            if fam == "sat_gasha":
                return (6, 17)
            if fam == "sat_minigame":
                return (3, 6)
            if rk.startswith("sat_work_heavy"):
                return (8, 20)
            if fam == "sat_work" or rk.startswith("sat_work"):
                return (8, 19)
            if rk.startswith("sat_out"):
                return (1, 13)
            return (1, 14)

        act_ev_map = {}
        for r_item in routes:
            act_k = _route_to_act(r_item)
            if act_k not in act_ev_map or r_item["total_ev"] > act_ev_map[act_k]["total_ev"]:
                act_ev_map[act_k] = {"total_ev": r_item["total_ev"], "compact": r_item["compact"]}
        if (1, 13) in act_ev_map:
            act_ev_map[(1, 1)] = act_ev_map[(1, 13)]
        best_act = _route_to_act(top_route)
        best_ev = top_route["total_ev"]
        best_compact = top_route["compact"]
        # 周六(非末周)结构化推荐购物清单：先领 0 金白嫖；商店不刷新，常规药水/投资留到周日看新扭蛋池后买；
        # 仅当周六加训失败率 >=10% 时允许前置买入 1 瓶买得起的稳定体力药（严禁买赌狗药）急救
        recommended_shop_buys = [
            {"id": x["id"], "count": x["num"], "cat": "free", "reason": "0金免费领取"}
            for x in free_items
        ]
        if best_act[0] == 0 and scored_sat_trains:
            sat_pick = next((t for t in scored_sat_trains if t["idx"] == best_act[1]), None)
            if sat_pick and int((sat_pick.get("cmd") or {}).get("fail_rate", 0) or 0) >= 10:
                safe_sat_pots = [x for x in stamina_items if not x.get("is_gamble") and x["price"] <= gold]
                if safe_sat_pots:
                    sp = safe_sat_pots[0]
                    recommended_shop_buys.append({
                        "id": sp["id"],
                        "count": 1,
                        "cat": "stamina",
                        "reason": f"周六加训低体防炸({sp['name']})",
                    })
    else:
        # 周末最后1动（含常规周末的周日 2/2 动，以及毕业前最后周末 R48 的唯一周六 1/1 动）：
        # 直接对本动所有可执行单步方案 + 商店投资组合（熟练度/设施书/末周属性书）统一算总 EV 排序
        sun_options = []
        for g_item in scored_gashapon:
            if gold >= g_item["price"]:
                rem_g = gold - g_item["price"]
                w_gain_g = 35 if g_item["cfg_id"] in (100, 99) else 0
                g_cap_s = max(ps_cap, int(g_item.get("ps_cap_after", ps_cap)))
                ps_after_g = min(g_cap_s, ps + g_item["ps_gain"])
                inv_g = plan_shop_investments(rem_g)
                tot_w_g = w_gain_g + inv_g["w_gain"]
                pot_s = plan_shop_potions(ps_after_g, rem_g - inv_g["cost"], extra_w_gain=tot_w_g, cap_override=g_cap_s)
                left_g = rem_g - inv_g["cost"] - pot_s["cost"]
                stat_s = plan_final_shop_stats(left_g)
                left_g -= stat_s["cost"]
                tot_ev = round(
                    g_item["ev"] + inv_g["ev"] + pot_s["ev"] + stat_s["ev"] + keep_gold_ev(left_g),
                    1,
                )
                shop_pre = " + ".join(x for x in (inv_g["str"], pot_s["str"], stat_s["str"]) if x)
                pot_txt = f"🛒{shop_pre} + " if shop_pre else ""
                g_prefix = f"🎰6号{gasha_disc_tag}抽" if gasha_disc_active else "🎰6号抽"
                sun_options.append({
                    "act": (6, 17),
                    "total_ev": tot_ev,
                    "compact": f"{pot_txt}{g_prefix}{g_item['desc']}",
                    "shop_buys": list(inv_g["items"]) + list(pot_s["items"]) + list(stat_s["items"]),
                })

        # 本动玩 3号建筑·街机小游戏 (+ 可选商店高优投资/买药/末周买属性书)
        for mg_item in scored_minigame:
            if gold >= mg_item["price"]:
                rem_g_m = gold - mg_item["price"]
                inv_m = plan_shop_investments(rem_g_m)
                w_m = inv_m["w_gain"]
                pot_m = plan_shop_potions(ps, rem_g_m - inv_m["cost"], extra_w_gain=w_m)
                left_g_m = rem_g_m - inv_m["cost"] - pot_m["cost"]
                stat_m = plan_final_shop_stats(left_g_m)
                left_g_m -= stat_m["cost"]
                tot_ev_m = round(
                    mg_item["ev"] + inv_m["ev"] + pot_m["ev"] + stat_m["ev"] + keep_gold_ev(left_g_m),
                    1,
                )
                shop_pre_m = " + ".join(x for x in (inv_m["str"], pot_m["str"], stat_m["str"]) if x)
                pot_txt_m = f"🛒{shop_pre_m} + " if shop_pre_m else ""
                m_prefix = "🎮3号🔥特级玩" if mg_spec_active else (f"🎮3号{mg_disc_tag}玩" if mg_disc_active else "🎮3号玩")
                sun_options.append({
                    "act": (3, 6),
                    "total_ev": tot_ev_m,
                    "compact": f"{pot_txt_m}{m_prefix}{mg_item['desc']}",
                    "shop_buys": list(inv_m["items"]) + list(pot_m["items"]) + list(stat_m["items"]),
                })

        # 轻度打工与重度打工（注意：点完周日建筑会直接进周一刷新商店，因此周日商店购买必须使用点击打工前的现钱 gold！）
        if next_week_trains > 0 or wl_mates > 0:
            inv_w = plan_shop_investments(gold)
            w_w = inv_w["w_gain"]
            pot_w = plan_shop_potions(ps, gold - inv_w["cost"], extra_w_gain=w_w)
            left_g_w = gold - inv_w["cost"] - pot_w["cost"] + wl_today_gold
            stat_w = plan_final_shop_stats(gold - inv_w["cost"] - pot_w["cost"])
            left_g_w -= stat_w["cost"]
            ev_w = round(
                inv_w["ev"] + wl_ev_direct + pot_w["ev"] + stat_w["ev"] + keep_gold_ev(left_g_w),
                1,
            )
            shop_pre_w = " + ".join(x for x in (inv_w["str"], pot_w["str"], stat_w["str"]) if x)
            pot_txt_w = f"🛒{shop_pre_w} + " if shop_pre_w else ""
            sun_options.append({
                "act": (8, 19),
                "total_ev": ev_w,
                "compact": f"{pot_txt_w}{wl_short}",
                "shop_buys": list(inv_w["items"]) + list(pot_w["items"]) + list(stat_w["items"]),
            })

        if (next_week_trains > 0 or wh_mates > 0) and ps >= 40:
            inv_wh = plan_shop_investments(gold)
            w_wh = inv_wh["w_gain"]
            ps_after_wh = ps - 25
            pot_wh = plan_shop_potions(ps_after_wh, gold - inv_wh["cost"], extra_w_gain=w_wh)
            fin_ps_wh = min(ps_cap, ps_after_wh + pot_wh["ps_gain"])
            if wh_urgent or fin_ps_wh >= max(72, sched_ctx["safe_ps_floor"] + 8):
                left_g_wh = gold - inv_wh["cost"] - pot_wh["cost"] + wh_today_gold
                stat_wh = plan_final_shop_stats(gold - inv_wh["cost"] - pot_wh["cost"])
                left_g_wh -= stat_wh["cost"]
                wh_dir_ev_sun = _calc_wh_dir_ev(wh_mates, wh_urgent, extra_w_gain=w_wh, start_ps=ps)
                ev_wh = round(
                    inv_wh["ev"] + wh_dir_ev_sun + pot_wh["ev"] + stat_wh["ev"] + keep_gold_ev(left_g_wh),
                    1,
                )
                shop_pre_wh = " + ".join(x for x in (inv_wh["str"], pot_wh["str"], stat_wh["str"]) if x)
                pot_txt_wh = f"🛒{shop_pre_wh} + " if shop_pre_wh else ""
                sun_options.append({
                    "act": (8, 20),
                    "total_ev": ev_wh,
                    "compact": f"{pot_txt_wh}{wh_short}",
                    "shop_buys": list(inv_wh["items"]) + list(pot_wh["items"]) + list(stat_wh["items"]),
                })

        # 外出野餐 / 公园划船 (+ 可用现钱购买商店高优道具)
        # 注意：除第4周海滩合宿(week_no==3的下一周)周一无比赛外，其余周一比赛固定+70金；若周日结束现钱+下周一比赛金 < 120金且放弃打工，下周六将买不起120金彩蛋笔记
        mon_match_gold = 0 if week_no == 3 else (210 if week_no >= 6 else 70)
        act_st_now = state.get("active_states", set())
        sat_allow_now = 110 if 492 in act_st_now else (80 if 323 in act_st_now else (40 if 322 in act_st_now else 0))
        broke_floor_120 = max(0, 120 - mon_match_gold - sat_allow_now)  # W1/2/4/5为50金，W3(进海滩前)为120金
        no_work_broke_pen = (16.0 if (week_no <= 5 and gold < broke_floor_120) else 0.0)
        if next_week_trains > 0:
            inv_r = plan_shop_investments(gold)
            w_r = inv_r["w_gain"]
            ps_cap_sun = max(ps_cap, int(ps))
            rest_gain = min(70 + rest_mates * 20, max(0, ps_cap_sun - ps))
            rest_ps_ev = v_ps_delta(ps, min(ps_cap_sun, ps + rest_gain), extra_w_gain=w_r, cap_override=ps_cap_sun)
            ev_rest = round(
                inv_r["ev"] + rest_ps_ev + rest_mates * rest_mate_bond_ev - weekend_rest_opp_pen - no_work_broke_pen + keep_gold_ev(gold - inv_r["cost"]),
                1,
            )
            inv_pre_r = f"🛒{inv_r['str']} + " if inv_r["str"] else ""
            sun_options.append({
                "act": (1, 14),
                "total_ev": ev_rest,
                "compact": f"{inv_pre_r}{rest_short}(⚡➔{ps+rest_gain})",
                "shop_buys": list(inv_r["items"]),
            })
        if (out_mates > 0 or out_spec_active) and next_week_trains > 0:
            inv_o = plan_shop_investments(gold)
            w_o = inv_o["w_gain"]
            out_gain = min(30, max(0, ps_cap - ps))
            out_ps_ev = v_ps_delta(ps, min(ps_cap, ps + out_gain), extra_w_gain=w_o)
            stat_o = plan_final_shop_stats(gold - inv_o["cost"])
            rem_g_o = gold - inv_o["cost"] - stat_o["cost"]
            out_broke_pen = (0.0 if out_spec_active else no_work_broke_pen)
            ev_out = round(
                inv_o["ev"] + out_ps_ev + out_base_stat_ev + out_mates * mate_stat_10_ev + stat_o["ev"] + keep_gold_ev(rem_g_o) - out_broke_pen,
                1,
            )
            shop_pre_o = " + ".join(x for x in (inv_o["str"], stat_o["str"]) if x)
            stat_pre_o = f"🛒{shop_pre_o} + " if shop_pre_o else ""
            sun_options.append({
                "act": (1, 13),
                "total_ev": ev_out,
                "compact": f"{stat_pre_o}{out_short}(⚡➔{min(ps_cap, ps+out_gain)})",
                "shop_buys": list(inv_o["items"]) + list(stat_o["items"]),
            })

        # 若为毕业前最后周六(R48)且队伍含百目鬼(weekend_train=true)，评估『0金执行周六加训 + 现钱全扫商店属性书』
        if has_weekend_train and scored_sat_trains:
            stat_t_final = plan_final_shop_stats(gold)
            pot_txt_tf = f"🛒{stat_t_final['str']} + " if stat_t_final["str"] else ""
            for t_item in scored_sat_trains:
                t_idx = t_item["idx"]
                ps_delta = int(t_item["cmd"].get("ps", 0))
                ps_lbl_t = f"+{ps_delta}体" if ps_delta > 0 else f"{ps_delta}体"
                tot_ev_t = round(float(t_item.get("non_ps_score", t_item["score"])) + stat_t_final["ev"], 1)
                sun_options.append({
                    "act": (0, t_idx),
                    "total_ev": tot_ev_t,
                    "compact": f"{pot_txt_tf}🏋️加训【{t_item['name']}】({ps_lbl_t})",
                    "shop_buys": list(stat_t_final["items"]),
                })

        if not sun_options:
            stat_fb = plan_final_shop_stats(gold)
            fb_pre = f"🛒{stat_fb['str']} + " if stat_fb["str"] else ""
            sun_options.append({
                "act": (8, 19),
                "total_ev": round(wl_ev_direct + stat_fb["ev"], 1),
                "compact": f"{fb_pre}{wl_short}",
                "shop_buys": list(stat_fb["items"]),
            })

        sun_options.sort(key=lambda x: x["total_ev"], reverse=True)
        top_sun = sun_options[0]
        headline = f"👑 首选(EV:{top_sun['total_ev']:.0f})：{top_sun['compact']}"
        alt_compact = ""
        alt_diff_ev = 0.0
        if len(sun_options) > 1:
            alt_sun = sun_options[1]
            diff_s = top_sun["total_ev"] - alt_sun["total_ev"]
            alt_compact = alt_sun["compact"]
            alt_diff_ev = round(diff_s, 1)
            plan_steps.append(f"🥈 备选(EV:{alt_sun['total_ev']:.0f}, -{diff_s:.0f}{'·≈打平,按喜好选' if diff_s <= 2.0 else ''})：{alt_sun['compact']}")

        act_ev_map = {}
        for opt_item in sun_options:
            act_k = opt_item.get("act", (8, 19))
            if act_k not in act_ev_map or opt_item["total_ev"] > act_ev_map[act_k]["total_ev"]:
                act_ev_map[act_k] = {"total_ev": opt_item["total_ev"], "compact": opt_item["compact"]}
        if (1, 13) in act_ev_map:
            act_ev_map[(1, 1)] = act_ev_map[(1, 13)]
        best_act = top_sun.get("act", (8, 19))
        best_ev = top_sun["total_ev"]
        best_compact = top_sun["compact"]
        recommended_shop_buys = [
            {"id": x["id"], "count": x["num"], "cat": "free", "reason": "0金免费领取"}
            for x in free_items
        ] + list(top_sun.get("shop_buys") or [])

    best_train_opt = None
    second_train_item = None
    best_outing_opt = None
    second_outing_opt = None
    if has_weekend_train and scored_sat_trains:
        for t_item in scored_sat_trains:
            r_info = act_ev_map.get((0, t_item["idx"]))
            t_item["route_ev"] = r_info["total_ev"] if r_info else t_item["score"]
            t_item["route_compact"] = r_info["compact"] if r_info else f"🏋️加训【{t_item['name']}】"
        scored_sat_trains.sort(key=lambda x: (x["route_ev"], x["score"]), reverse=True)
        top_t = scored_sat_trains[0]
        best_train_opt = {
            "act": (0, top_t["idx"]),
            "total_ev": top_t["route_ev"],
            "compact": top_t["route_compact"],
            "train_item": top_t,
        }
        second_train_item = scored_sat_trains[1] if len(scored_sat_trains) > 1 else None

        outing_cands = [
            {"act": ak, "total_ev": av["total_ev"], "compact": av["compact"]}
            for ak, av in act_ev_map.items()
            if ak[0] != 0 and ak != (1, 1)
        ]
        outing_cands.sort(key=lambda x: x["total_ev"], reverse=True)
        best_outing_opt = outing_cands[0] if outing_cands else None
        second_outing_opt = outing_cands[1] if len(outing_cands) > 1 else None

        plan_steps = [s for s in plan_steps if not s.startswith("🥈 ")]
        if best_act[0] == 0:
            if second_train_item:
                diff_t2 = round(best_ev - second_train_item["route_ev"], 1)
                plan_steps.append(
                    f"🥈 加训次选(EV:{second_train_item['route_ev']:.0f}, -{diff_t2:.0f})：{second_train_item['route_compact']} [{second_train_item['reasons']}]"
                )
            if best_outing_opt:
                diff_out = round(best_ev - best_outing_opt["total_ev"], 1)
                alt_compact = best_outing_opt["compact"]
                alt_diff_ev = diff_out
                plan_steps.append(
                    f"🌴 外出最佳(EV:{best_outing_opt['total_ev']:.0f}, -{diff_out:.0f})：{best_outing_opt['compact']}"
                )
        else:
            diff_tr = round(best_ev - best_train_opt["total_ev"], 1)
            alt_compact = best_train_opt["compact"]
            alt_diff_ev = diff_tr
            plan_steps.append(
                f"🏋️ 加训最佳(EV:{best_train_opt['total_ev']:.0f}, -{diff_tr:.0f})：{best_train_opt['compact']} [{top_t['reasons']}]"
            )
            if second_outing_opt:
                diff_out2 = round(best_ev - second_outing_opt["total_ev"], 1)
                plan_steps.append(
                    f"🥈 外出次选(EV:{second_outing_opt['total_ev']:.0f}, -{diff_out2:.0f})：{second_outing_opt['compact']}"
                )

    # 汇总单行商店速览（不拆多行，直接标出每件道具的动态 EV 与熟练度收益标签）
    shop_parts = []
    if free_items:
        shop_parts.append("🎁0金:" + "/".join(x["name"] for x in free_items))
    if stamina_items:
        shop_parts.append("⚡" + "/".join(f"{x['name']}×{x['num']}({x['price']}金+{x['ps']}体)" for x in stamina_items[:2]))
    else:
        shop_parts.append("⚡无药")
    if value_items:
        v_strs = []
        for x in value_items[:2]:
            if x.get("ev", 0) > 0:
                tag_s = f"·{x['tag']}" if x.get("tag") else ""
                v_strs.append(f"{x['name']}({x['price']}金 EV:{x['ev']:.0f}{tag_s})")
            else:
                v_strs.append(f"{x['name']}({x['price']}金)")
        shop_parts.append("🌟" + "/".join(v_strs))
    shop_one_line = "🛒 商店: " + " | ".join(shop_parts)

    hud_free_tip = ("🎁 商店有 0金【" + " / ".join(x["name"] for x in free_items) + "】先领") if free_items else ""
    hud_shop_tip = ""
    for x in value_items[:2]:
        if x.get("ev", 0) >= 26 and gold >= x["price"] and x["name"] not in best_compact:
            tag_s = f"·{x['tag']}" if x.get("tag") else ""
            hud_shop_tip = f"🛒 商店好货：{x['name']}({x['price']}金{tag_s})"
            break

    mate_line = ""
    lookahead_parts = [lookahead_notes[0]] if lookahead_notes else []
    if arch.get("note"):
        lookahead_parts.append(arch["note"])
    lookahead_line = " | ".join(lookahead_parts)

    return {
        "ps": ps,
        "ps_max": ps_cap,
        "gold": gold,
        "wk_day": wk_day,
        "day_tag": day_tag,
        "lineup_archetype": arch,
        "headline": headline,
        "plan_steps": plan_steps,
        "shop_one_line": shop_one_line,
        "mate_line": mate_line,
        "lookahead_line": lookahead_line,
        "hud_lookahead": sched_ctx.get("hud_lookahead", ""),
        "hud_free_tip": hud_free_tip,
        "hud_shop_tip": hud_shop_tip,
        "scored_gashapon": scored_gashapon,
        "scored_minigame": scored_minigame,
        "has_weekend_train": has_weekend_train,
        "scored_sat_trains": scored_sat_trains,
        "best_train_opt": best_train_opt,
        "second_train_item": second_train_item,
        "best_outing_opt": best_outing_opt,
        "second_outing_opt": second_outing_opt,
        "best_act": best_act,
        "best_ev": best_ev,
        "best_compact": best_compact,
        "alt_compact": alt_compact,
        "alt_diff_ev": alt_diff_ev,
        "act_ev_map": act_ev_map,
        "recommended_shop_buys": recommended_shop_buys,
    }


def _apply_facility_book_purchase(state: dict, item_id: int):
    """当在商店购买训练设施直升书 (13010..18020) 时，同步增加对应训练项的等效累计次数（+1级=+3次，+2级=+6次）。"""
    meta = STORE_ITEM_CATALOG.get(item_id)
    if meta and meta.get("cat") == "facility" and meta.get("train_idx"):
        t_idx = int(meta["train_idx"])
        step_need = max(1, int(state.get("train_up_need", {}).get(t_idx, 3)))
        lv_gain = 2 if (item_id % 100 == 20) else 1
        cur = int(state.get("train_counts", {}).get(t_idx, 0))
        state.setdefault("train_counts", {})[t_idx] = cur + lv_gain * step_need


def count_training_days_in_window(exp_round: int, duration_days: int, has_sat_train: bool = False) -> int:
    """
    计算在当前阶段回合 exp_round 买入持续 duration_days 天的商店 Buff 道具，
    实际能覆盖多少个真正的训练回合（自动扣除周日、周一比赛日、R10/R24固定事件日；百目鬼阵容计入周六加训日）。
    """
    if exp_round <= 0:
        return min(3, duration_days)
    cnt = 0
    for r in range(exp_round, min(50, exp_round + duration_days)):
        if r <= 42:
            day_idx = (r - 1) % 7  # 0=周一, 1=周二, 2=周三, 3=周四, 4=周五, 5=周六, 6=周日
            if day_idx in (1, 2, 3, 4) and r not in (10, 24):
                cnt += 1
            elif has_sat_train and day_idx == 5:
                cnt += 1
        else:
            if r in (44, 46) or (has_sat_train and r == 48):
                cnt += 1
    return cnt


# 纯功能/友人型器材（无「金色友情训练属性加值」，不会触发金圈/彩圈暴击）：11004 支援口哨、11005 篮球吱神
NON_CRIT_EQUIPMENT_IDS = {11004, 11005}


def get_crit_ready_equipment_count(state: dict) -> tuple[int, int]:
    """
    统计当前携带的「可暴击器材」中已达暴击门槛（熟练度 >= 70）的张数，以及已识别到的可暴击器材总数。
    返回 (crit_ready_cnt, tracked_cnt)。
    """
    sup_trammel = state.get("supactor_trammel") or {}
    if sup_trammel:
        tracked = [
            int(v)
            for cfg_id, v in sup_trammel.items()
            if int(cfg_id) not in NON_CRIT_EQUIPMENT_IDS
        ]
        return sum(1 for v in tracked if v >= 70), len(tracked)

    card_trammel = state.get("supactor_card_trammel") or {}
    card_to_cfg = state.get("supactor_card_to_cfg") or {}
    tracked = []
    for cid, v in card_trammel.items():
        cfg_id = int(card_to_cfg.get(cid, cid))
        if cfg_id not in NON_CRIT_EQUIPMENT_IDS:
            tracked.append(int(v))
    return sum(1 for v in tracked if v >= 70), len(tracked)


def eval_equip_boost_shop_advice(
    subcat: str,
    meta: dict,
    price: int,
    state: dict,
    scored_cmds: list,
    exp_r: int,
    day_idx: int,
) -> dict:
    """
    统一裁决商店『器材指导教程/教程+(20010/20020, state=337)』与『器材指导书/书+(1181/1182, state=301/302)』的购买建议：
    - 据官方 states.json 实证，上述 Buff 全部绑定 condition_id=1158（训练有金圈），仅在暴击训练（金圈/彩圈 val>=1）时生效；
      且 state=337 无条件附带「所有训练精力消耗+10%」副作用。
    - 因此当卡组尚无任何可暴击器材（熟练度均 <70，排除口哨/吱神）或当回合最优训练本身无暴击圈（仅白圈合训 val=0）时，严禁推荐买入。
    """
    gold = state.get("gold", 0)
    ps = state.get("ps", 130)
    days = meta.get("days", 3)
    has_sat = has_doumeki_in_lineup(state)
    cov_trains = count_training_days_in_window(exp_r, days, has_sat_train=has_sat)

    best_train = next((x for x in (scored_cmds or []) if x.get("idx", 0) != 0), None)
    best_cmd = best_train["cmd"] if best_train else {}
    gold_cnt = int(best_cmd.get("gold_count", 0) or 0)
    rainbow_cnt = int(best_cmd.get("rainbow_count", 0) or 0)
    crit_cnt = gold_cnt + rainbow_cnt
    panel_gain = int(best_cmd.get("total_stat_gain", 0) or 0)

    any_cmd_crit = any(
        (int(x["cmd"].get("gold_count", 0) or 0) + int(x["cmd"].get("rainbow_count", 0) or 0)) >= 1
        for x in (scored_cmds or [])
        if x.get("idx", 0) != 0 and x.get("cmd")
    )
    crit_ready_cnt, tracked_cnt = get_crit_ready_equipment_count(state)
    # 若已追踪到可暴击器材熟练度但全部 < 70，且全面板也无任何暴击圈，说明暂无能暴击的器材
    has_crit_capable = (crit_ready_cnt > 0) or any_cmd_crit or (tracked_cnt == 0)

    res = {
        "recommend": False,
        "friday_trap": False,
        "cov_trains": cov_trains,
        "now_boost": 0,
        "level": "必买" if subcat == "equip_boost_ps" else "推荐",
        "icon": "💿" if subcat == "equip_boost_ps" else "📘",
    }
    if gold < price or not has_crit_capable:
        return res

    in_window = cov_trains >= 2 and (day_idx in (1, 2, 3) or has_sat)
    if not in_window:
        if day_idx == 4 and not has_sat and crit_cnt >= 1:
            res["friday_trap"] = True
        return res

    if subcat == "equip_boost_ps":
        if ps >= 78 and (rainbow_cnt >= 1 or (gold_cnt >= 1 and panel_gain >= 36)):
            res["recommend"] = True
            res["now_boost"] = max(6, int(round(panel_gain * 0.24)))
    elif subcat == "equip_boost_free":
        if crit_cnt >= 1:
            res["recommend"] = True
            res["now_boost"] = max(5, int(round(panel_gain * 0.14)))
    return res


def format_weekday_shop_tip(state: dict, best_ev: float = None, strategy: dict = None, scored_cmds: list = None):
    """在平时训练回合 (round_type = 1)，结合下一回合固定回体事件前瞻与商店0金福利/体力药/同心护符/器材指导教程/神之庇佑等道具生成参谋提醒。"""
    sched_ctx = get_upcoming_schedule_context(state)
    wk_heal_note = sched_ctx.get("weekday_heal_note", "")
    wk_next_heal = sched_ctx.get("weekday_next_turn_heal", 0)
    exp_r = sched_ctx.get("exp_round", state.get("exp_round", 1))
    day_idx = (exp_r - 1) % 7 if exp_r > 0 else 1  # 1=周二, 2=周三, 3=周四, 4=周五

    store_items = [it for it in state.get("store_items", []) if it["num"] > 0]
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    gold = state.get("gold", 0)
    active_states = state.get("active_states", set())
    state_rounds = state.get("state_rounds", {})

    best_train = next((x for x in (scored_cmds or []) if x.get("idx", 0) != 0), None)
    best_cmd = best_train["cmd"] if best_train else {}
    mate_cnt = best_cmd.get("mate_count", 0) + (1 if best_cmd.get("is_manager") else 0)
    mate_ids = best_cmd.get("mate_ids", [])
    lineup_all = set(state.get("teammate_char_ids", [])) | ({state["main_char_id"]} if state.get("main_char_id") else set())
    has_pull = (1046 in lineup_all) or (1035 in lineup_all) or (1046 in mate_ids) or (1035 in mate_ids)
    eff_mates_now = (mate_cnt + (0.9 if has_pull and best_cmd.get("mate_count", 0) < 4 else 0.0)) if mate_cnt > 0 else 0.0
    panel_gain = best_cmd.get("total_stat_gain", 0)
    max_top_fail = max(
        (x["cmd"].get("fail_rate", 0) for x in (scored_cmds or [])[:2] if x.get("idx", 0) != 0),
        default=0,
    )
    # 计算若使用【神之庇佑(1131/1132)】将全训练失败率强制归零后，能解封的最大潜能训练项
    best_safe_ev = max(
        (x["score"] for x in (scored_cmds or []) if (x.get("cmd") or {}).get("fail_rate", 0) <= 10),
        default=60.0,
    )
    best_zero_fail_unlock = None
    for x in (scored_cmds or []):
        if x.get("idx", 0) == 0:
            continue
        fr = x["cmd"].get("fail_rate", 0)
        if fr >= 12:
            # 直接读取未施加失败率惩罚前的真实 0% 失败率 EV（确保低体高危彩圈精准触发神之庇佑救场）
            zf_ev = round(float(x.get("raw_zero_fail_score", x["score"])), 1)
            if best_zero_fail_unlock is None or zf_ev > best_zero_fail_unlock["zf_ev"]:
                best_zero_fail_unlock = {
                    "name": x["name"],
                    "fail_rate": fr,
                    "zf_ev": zf_ev,
                    "net_gain": round(zf_ev - best_safe_ev, 1),
                }

    tips = []
    # 1. 显示当前已挂载的商店持续 Buff 及剩余天数
    active_buff_tags = []
    for sid, bmeta in SHOP_BUFF_STATE_META.items():
        if sid in active_states:
            rem_d = state_rounds.get(sid)
            d_str = f"({rem_d}天)" if rem_d else ""
            active_buff_tags.append(f"{bmeta['short']}{d_str}")
    if active_buff_tags:
        tips.append("✨Buff:" + "/".join(active_buff_tags))

    if wk_heal_note:
        tips.append(wk_heal_note)

    if store_items:
        free_cnt = sum(1 for it in store_items if it["price"] == 0 or it["id"] in (1151, 1152))
        if free_cnt > 0:
            tips.append("🎁商店有0金免费道具未领")

        # 2. 低体力药水与角色精力被动门槛（望月透59体/克拉丽丝）提醒
        lineup_th, th_lbl = get_lineup_ps_threshold(state)
        low_ps_thresh = 50 if wk_next_heal >= 30 else max(75, lineup_th + 20)
        if ps <= low_ps_thresh:
            st_its = [
                (
                    STORE_ITEM_CATALOG[it["id"]]["name"],
                    STORE_ITEM_CATALOG[it["id"]]["ps"],
                    it["price"],
                    bool(STORE_ITEM_CATALOG[it["id"]].get("is_gamble", False)),
                    int(STORE_ITEM_CATALOG[it["id"]].get("max_ps", STORE_ITEM_CATALOG[it["id"]]["ps"])),
                )
                for it in store_items
                if it["id"] in STORE_ITEM_CATALOG and STORE_ITEM_CATALOG[it["id"]]["cat"] == "stamina"
            ]
            if st_its:
                # 稳定必回体药水优先于 50% 赌狗特价饮
                best_st = min(st_its, key=lambda x: (x[3], x[2] / max(1, x[1])))
                st_gain_str = f"🎲50%赌+{best_st[4]}/0体" if best_st[3] else f"+{best_st[1]}体"
                tips.append(f"⚡低体可先买商店【{best_st[0]}({st_gain_str}/{best_st[2]}金)】防训练失败")

        # 3. 盘点商店中的持续回合增益 Buff（同心护符 / 器材指导教程+ / 器材指导书 / 省体护具 / 神之庇佑）
        late_buff_warn = []
        for it in store_items:
            iid, price = it["id"], it["price"]
            meta = STORE_ITEM_CATALOG.get(iid)
            if not meta or meta.get("cat") != "buff":
                continue
            subcat = meta.get("subcat", "")
            sid = meta.get("state_id")
            days = meta.get("days", 3)
            if sid and sid in active_states:
                continue  # 同类状态已生效中，不重复推荐

            has_sat = has_doumeki_in_lineup(state)
            cov_trains = count_training_days_in_window(exp_r, days, has_sat_train=has_sat)

            if subcat == "teammate_emoji":  # 21010 同心护符 (5天全队友表情+5%)
                now_bonus = max(3, int(round(panel_gain * 0.05 * eff_mates_now * (1.25 if eff_mates_now >= 1.8 else 1.0)))) if eff_mates_now >= 1 else 0
                est_ev = now_bonus * 1.15 + max(0, cov_trains - 1) * 11.5
                if gold >= price:
                    if cov_trains >= 3 and (eff_mates_now >= 1 or gold >= 160):
                        now_s = f"，本训即+{now_bonus}属性" if now_bonus > 0 else ""
                        tips.append(f"🛒必买【🧧{meta['name']}({price}金)】(5天覆{cov_trains}训全队友表情+5%{now_s}/总约+{est_ev:.0f}EV)")
                    elif cov_trains >= 2 and (day_idx != 4 or has_sat) and eff_mates_now >= 1.8 and gold >= 150:
                        tips.append(f"🛒推荐买【🧧{meta['name']}({price}金)】(覆{cov_trains}训多人表情+5%，本训+{now_bonus}属性)")
                    elif day_idx == 4 and not has_sat:
                        late_buff_warn.append(f"⏳{meta['name']}周五买空转周末3天")
            elif subcat in ("equip_boost_ps", "equip_boost_free"):  # 20010/20020 教程、1181/1182 指导书（仅对金/彩暴击生效）
                eb = eval_equip_boost_shop_advice(subcat, meta, price, state, scored_cmds, exp_r, day_idx)
                if eb["recommend"]:
                    if subcat == "equip_boost_ps":
                        tips.append(
                            f"🛒必买【{eb['icon']}{meta['name']}({price}金)】({days}天覆{eb['cov_trains']}训暴击加成，本训面板即+{eb['now_boost']})"
                        )
                    else:
                        tips.append(
                            f"🛒推荐买【{eb['icon']}{meta['name']}({price}金)】({days}天覆{eb['cov_trains']}训暴击+{eb['now_boost']}且不加耗体)"
                        )
                elif eb["friday_trap"]:
                    late_buff_warn.append(f"⏳{meta['name']}周五买空转周末")
            elif subcat == "ps_discount":  # 1191/1192 省体护具I/II (-40%训练耗体)
                if gold >= price and cov_trains >= 2 and day_idx in (1, 2, 3):
                    tips.append(f"🛒高性价比【🔥{meta['name']}({price}金)】(覆{cov_trains}训省40%体力≈+{cov_trains*9}体)")
            elif subcat == "zero_fail":  # 1131/1132 神之庇佑/庇佑+ (强制0%失败率)
                if gold >= price:
                    if best_zero_fail_unlock and best_zero_fail_unlock["net_gain"] >= 35.0:
                        u = best_zero_fail_unlock
                        tips.append(
                            f"🛒必买【🔰{meta['name']}({price}金)】(锁0%失败率解封【{u['name']}(EV:{u['zf_ev']:.0f})】，原失败{u['fail_rate']}%，净赚+{u['net_gain']:.0f}EV！)"
                        )
                    elif max_top_fail >= 12:
                        tips.append(f"🛒急救必买【🔰{meta['name']}({price}金)】(强制锁0%失败率，低体稳吃{max_top_fail}%高危训练！)")
                    elif day_idx == 4:
                        late_buff_warn.append(f"🚫{meta['name']}当前0%失败率勿买")

        if late_buff_warn and len(tips) < 3:
            tips.append(late_buff_warn[0])

        # 4. 全器材熟练度提升道具 (1113/1111/1112)：按动态 EV 与机会成本净值排序推荐
        bond_cands = []
        for it in store_items:
            if it["id"] in (1113, 1111, 1112) and gold >= it["price"]:
                b_info = calc_shop_bond_item_ev(it["id"], it["price"], state)
                if b_info["eff_total"] <= 0:
                    continue
                net_b = b_info["ev"] - it["price"] * 0.30
                bond_cands.append((net_b, it, b_info))
        if bond_cands:
            bond_cands.sort(key=lambda x: -x[0])
            best_net, best_it, best_b = bond_cands[0]
            meta = STORE_ITEM_CATALOG[best_it["id"]]
            if best_b["maxed_new"] > 0 or "哨➔" in best_b["tag"]:
                tips.append(f"🔥商店【{meta['name']}({best_it['price']}金 EV:{best_b['ev']:.0f}·{best_b['tag']})】建议买入")
            elif best_net >= 10.0:
                tips.append(f"🔧商店【{meta['name']}({best_it['price']}金 EV:{best_b['ev']:.0f}·{best_b['tag']})】高性价比")
    return ("💡 " + " | ".join(tips)) if tips else ""


def format_weekday_hud_tips(state: dict, scored_cmds: list = None, include_shop: bool = True) -> list[str]:
    """
    为清爽版 HUD 底部提示栏提炼极简『防忘与行动提醒』（平时没事时不显示，仅在有回体节点/必买道具/0金福利时出现）。
    返回带 Pango 颜色标签的单行短句列表。
    """
    sched_ctx = get_upcoming_schedule_context(state)
    wk_heal_note = sched_ctx.get("weekday_heal_note", "")
    wk_next_heal = sched_ctx.get("weekday_next_turn_heal", 0)
    exp_r = sched_ctx.get("exp_round", state.get("exp_round", 1))
    day_idx = (exp_r - 1) % 7 if exp_r > 0 else 1

    store_items = [it for it in state.get("store_items", []) if it["num"] > 0]
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    gold = state.get("gold", 0)
    active_states = state.get("active_states", set())
    state_rounds = state.get("state_rounds", {})

    best_train = next((x for x in (scored_cmds or []) if x.get("idx", 0) != 0), None)
    best_cmd = best_train["cmd"] if best_train else {}
    mate_cnt = best_cmd.get("mate_count", 0) + (1 if best_cmd.get("is_manager") else 0)
    mate_ids = best_cmd.get("mate_ids", [])
    lineup_all = set(state.get("teammate_char_ids", [])) | ({state["main_char_id"]} if state.get("main_char_id") else set())
    has_pull = (1046 in lineup_all) or (1035 in lineup_all) or (1046 in mate_ids) or (1035 in mate_ids)
    eff_mates_now = (mate_cnt + (0.9 if has_pull and best_cmd.get("mate_count", 0) < 4 else 0.0)) if mate_cnt > 0 else 0.0
    max_top_fail = max(
        (x["cmd"].get("fail_rate", 0) for x in (scored_cmds or [])[:2] if x.get("idx", 0) != 0),
        default=0,
    )
    best_safe_ev = max(
        (x["score"] for x in (scored_cmds or []) if (x.get("cmd") or {}).get("fail_rate", 0) <= 10),
        default=60.0,
    )
    best_zero_fail_unlock = None
    for x in (scored_cmds or []):
        if x.get("idx", 0) == 0:
            continue
        fr = x["cmd"].get("fail_rate", 0)
        if fr >= 12:
            zf_ev = round(float(x.get("raw_zero_fail_score", x["score"])), 1)
            if best_zero_fail_unlock is None or zf_ev > best_zero_fail_unlock["zf_ev"]:
                best_zero_fail_unlock = {
                    "name": x["name"],
                    "fail_rate": fr,
                    "zf_ev": zf_ev,
                    "net_gain": round(zf_ev - best_safe_ev, 1),
                }

    hud_lines = []

    # 第 1 类：赛程固定回体 / 哨子排队回体前瞻 + 当前激活 Buff 剩余天数（合并为 1 行清爽状态提示）
    status_parts = []
    if wk_heal_note:
        status_parts.append(f"<span foreground='#fcd34d' weight='bold'>{html.escape(wk_heal_note)}</span>")
    active_buff_tags = []
    for sid, bmeta in SHOP_BUFF_STATE_META.items():
        if sid in active_states:
            rem_d = state_rounds.get(sid)
            d_str = f"{rem_d}天" if rem_d else ""
            active_buff_tags.append(f"{bmeta['short']}{d_str}")
    if active_buff_tags:
        status_parts.append(f"<span foreground='#93c5fd'>{' / '.join(html.escape(t) for t in active_buff_tags)}</span>")
    if status_parts:
        hud_lines.append("  ·  ".join(status_parts))

    # 第 2 类：商店关键时机提醒（0金白嫖 / 低体急救药 / 高收益 Buff / 哨子冲线熟练度）
    shop_alerts = []
    if include_shop and store_items:
        free_cnt = sum(1 for it in store_items if it["price"] == 0 or it["id"] in (1151, 1152))
        if free_cnt > 0:
            shop_alerts.append("🎁 商店有 0金免费道具")

        lineup_th, _ = get_lineup_ps_threshold(state)
        low_ps_thresh = 50 if wk_next_heal >= 30 else max(72, lineup_th + 18)
        if ps <= low_ps_thresh and (max_top_fail >= 8 or ps <= 58):
            st_its = [
                (
                    STORE_ITEM_CATALOG[it["id"]]["name"],
                    STORE_ITEM_CATALOG[it["id"]]["ps"],
                    it["price"],
                    bool(STORE_ITEM_CATALOG[it["id"]].get("is_gamble", False)),
                    int(STORE_ITEM_CATALOG[it["id"]].get("max_ps", STORE_ITEM_CATALOG[it["id"]]["ps"])),
                )
                for it in store_items
                if it["id"] in STORE_ITEM_CATALOG and STORE_ITEM_CATALOG[it["id"]]["cat"] == "stamina" and gold >= it["price"]
            ]
            if st_its:
                best_st = min(st_its, key=lambda x: (x[3], x[2] / max(1, x[1])))
                st_gain_str = f"赌+{best_st[4]}体" if best_st[3] else f"+{best_st[1]}体"
                shop_alerts.append(f"⚡ 低体可买 {best_st[0]}({st_gain_str}/{best_st[2]}金)")

        friday_buff_trap = False
        for it in store_items:
            iid, price = it["id"], it["price"]
            meta = STORE_ITEM_CATALOG.get(iid)
            if not meta or meta.get("cat") != "buff" or gold < price:
                continue
            subcat = meta.get("subcat", "")
            sid = meta.get("state_id")
            days = meta.get("days", 3)
            if sid and sid in active_states:
                continue
            has_sat = has_doumeki_in_lineup(state)
            cov_trains = count_training_days_in_window(exp_r, days, has_sat_train=has_sat)
            if subcat == "teammate_emoji":
                if cov_trains >= 3 and (eff_mates_now >= 1 or gold >= 160):
                    shop_alerts.append(f"🛒 必买【{meta['name']} {price}金】覆{cov_trains}训")
                elif cov_trains >= 2 and (day_idx != 4 or has_sat) and eff_mates_now >= 1.8 and gold >= 150:
                    shop_alerts.append(f"🛒 推荐【{meta['name']} {price}金】覆{cov_trains}训")
                elif day_idx == 4 and not has_sat:
                    friday_buff_trap = True
            elif subcat in ("equip_boost_ps", "equip_boost_free"):
                eb = eval_equip_boost_shop_advice(subcat, meta, price, state, scored_cmds, exp_r, day_idx)
                if eb["recommend"]:
                    shop_alerts.append(f"🛒 {eb['level']}【{meta['name']} {price}金】覆{eb['cov_trains']}训")
                elif eb["friday_trap"]:
                    friday_buff_trap = True
            elif subcat == "ps_discount":
                if cov_trains >= 2 and day_idx in (1, 2, 3):
                    shop_alerts.append(f"🛒 推荐【{meta['name']} {price}金】省体覆{cov_trains}训")
            elif subcat == "zero_fail":
                if best_zero_fail_unlock and best_zero_fail_unlock["net_gain"] >= 35.0:
                    u = best_zero_fail_unlock
                    shop_alerts.append(f"🛒 必买【{meta['name']} {price}金】锁0%救{u['name']}")
                elif max_top_fail >= 12:
                    shop_alerts.append(f"🛒 急救【{meta['name']} {price}金】锁0%失败")

        bond_cands = []
        for it in store_items:
            if it["id"] in (1113, 1111, 1112) and gold >= it["price"]:
                b_info = calc_shop_bond_item_ev(it["id"], it["price"], state)
                if b_info["eff_total"] <= 0:
                    continue
                net_b = b_info["ev"] - it["price"] * 0.30
                bond_cands.append((net_b, it, b_info))
        if bond_cands:
            bond_cands.sort(key=lambda x: -x[0])
            best_net, best_it, best_b = bond_cands[0]
            meta = STORE_ITEM_CATALOG[best_it["id"]]
            if best_b["maxed_new"] > 0 or "哨➔" in best_b["tag"] or best_net >= 12.0:
                shop_alerts.append(f"🔥 商店【{meta['name']} {best_it['price']}金】({best_b['tag']})")

        if not shop_alerts and friday_buff_trap:
            shop_alerts.append("⏳ 周五勿买多日Buff(周末空转)")

    if shop_alerts:
        hud_lines.append(
            "<span foreground='#7dd3fc'>" + "  ·  ".join(html.escape(a) for a in shop_alerts[:2]) + "</span>"
        )

    return hud_lines


def extract_stitched_run_segments(log_dir: str = LOG_DIR) -> list[dict]:
    """
    按时间顺序扫描全部日志文件，以 cs_start_education 为新局起点，
    并将跨日志文件通过 cs_restore_education 续玩的半局自动拼接到上一局未结算的轨迹中。
    """
    logs = sorted(glob.glob(os.path.join(log_dir, "*.txt")))
    stitched = []

    for lp in logs:
        with open(lp, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if not txt:
            continue

        starts = [m.start() for m in re.finditer(r"request_call:\s+education\.cs_start_education", txt)]
        prefix = txt[:starts[0]] if starts else txt

        # 若文件开头（首个 cs_start_education 之前）存在续玩/训练记录，且上一局尚未出现结算 new_rank，则拼接到上一局
        if (
            prefix
            and stitched
            and not stitched[-1]["has_settled"]
            and ("cs_restore_education" in prefix or "education.cs_train" in prefix or "new_rank" in prefix)
        ):
            stitched[-1]["seg"] += "\n" + prefix
            stitched[-1]["end_log"] = os.path.basename(lp)
            if re.search(r"new_rank\s*=\s*(?:[3-9]\d{3}|1\d{4})\b", stitched[-1]["seg"]):
                stitched[-1]["has_settled"] = True

        for i, st in enumerate(starts):
            ed = starts[i + 1] if i + 1 < len(starts) else len(txt)
            seg = txt[st:ed]
            has_settled = bool(re.search(r"new_rank\s*=\s*(?:[3-9]\d{3}|1\d{4})\b", seg))
            stitched.append({
                "log": os.path.basename(lp),
                "end_log": os.path.basename(lp),
                "run_index": i + 1,
                "seg": seg,
                "has_settled": has_settled,
            })

    return stitched


def parse_state_and_turns_from_text(text: str, return_final_state: bool = False):
    state = make_initial_state()
    turns = []

    token_re = re.compile(
        r"(\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})|\[EDU_MODEL\]_LoadStory:\s+\d+\s+[^\s\t\r\n]+|request_call:\s+education\.(?:cs_train|cs_decide|cs_buy_edu_item)\s*\{[^\n]*\})",
        re.S,
    )
    last_cmds = None
    bought_in_weekday = []

    for m in token_re.finditer(text):
        tok = m.group(1)
        if tok.startswith("[EDU_MODEL]_LoadStory:"):
            sm = re.search(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", tok)
            if sm:
                ev_id = sm.group(1)
                state["cur_story"] = (ev_id, sm.group(2))
                if ev_id == "70":
                    state["fired_70"] = True
                elif ev_id == "71":
                    state["fired_71"] = True
            continue
        if tok.startswith("request_call:"):
            if "cs_buy_edu_item" in tok:
                bm = re.search(r"store_item_id\s*=\s*(\d+)", tok)
                if bm:
                    iid = int(bm.group(1))
                    _apply_facility_book_purchase(state, iid)
                    if state.get("round_type", 1) == 1:
                        inm = STORE_ITEM_CATALOG.get(iid, {}).get("name", f"道具#{iid}")
                        bought_in_weekday.append(f"{inm}({iid})")
                continue
            chosen_idx = None
            if "cs_decide" in tok and "decide_type = 2" in tok:
                chosen_idx = 0
            else:
                cm = re.search(r"cmd_idx\s*=\s*(\d+)", tok)
                if cm:
                    chosen_idx = int(cm.group(1))
            if chosen_idx is not None and last_cmds:
                snap = clone_state_snapshot(state)
                snap["round"] = len(turns) + 1
                turns.append({
                    "turn_no": len(turns) + 1,
                    "state": snap,
                    "cmds": last_cmds,
                    "chosen_idx": chosen_idx,
                    "is_weekend_train": bool(snap.get("round_type") == 2 or snap.get("weekend_train")),
                    "bought_items": list(bought_in_weekday),
                })
                if 1 <= chosen_idx <= 6:
                    state["train_counts"][chosen_idx] = state["train_counts"].get(chosen_idx, 0) + 1
                state["round"] = len(turns) + 1
                last_cmds = None
                bought_in_weekday = []
        else:
            update_state_from_token(state, tok)
            if state.get("round_type") == 2:
                bought_in_weekday = []
            if "train_cmds" in tok and "train_cmds = {}" not in tok:
                cmds = parse_train_cmd_block(tok)
                if len(cmds) == 6:
                    last_cmds = cmds
            elif "train_cmds = {}" in tok:
                last_cmds = None

    if return_final_state:
        return turns, clone_state_snapshot(state)
    return turns


# =====================================================================
# 自动代打 (Auto-Play via xdotool) & 坐标校准模块
# =====================================================================

def get_mouse_location():
    if IS_WINDOWS:
        return win32_get_mouse_location()
    out = subprocess.check_output(["xdotool", "getmouselocation", "--shell"], text=True)
    info = {}
    for line in out.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = int(v.strip())
    return info


def calibrate_buttons():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    print("=" * 72)
    print("🎯 【LQSNCoach 自动代打坐标校准向导】")
    if not IS_WINDOWS:
        print("提示：首次触发 xdotool 点击时，COSMIC 桌面可能会弹出一次『允许远程控制』确认框，点允许即可。")
    print("=" * 72)
    input("\n👉 第一步：请切到游戏训练界面，把鼠标悬停在最左边【1号·投射训练】按钮正中央，然后回终端按 [Enter]...")
    p1 = get_mouse_location()
    print(f"   ✅ 已记录 1 号键坐标: X={p1['X']}, Y={p1['Y']} (Window={p1.get('WINDOW', 0)})")

    input("\n👉 第二步：请把鼠标悬停在最右边【6号·意识训练】按钮正中央，然后回终端按 [Enter]...")
    p6 = get_mouse_location()
    print(f"   ✅ 已记录 6 号键坐标: X={p6['X']}, Y={p6['Y']} (Window={p6.get('WINDOW', 0)})")

    double_click_ans = input("\n👉 游戏里点训练键需要【连点两下】（第一下预览、第二下执行）吗？[Y/n，默认 Y]: ").strip().lower()
    double_click = double_click_ans not in ("n", "no")

    buttons = {}
    for idx in range(1, 7):
        ratio = (idx - 1) / 5.0
        bx = int(round(p1["X"] + (p6["X"] - p1["X"]) * ratio))
        by = int(round(p1["Y"] + (p6["Y"] - p1["Y"]) * ratio))
        buttons[str(idx)] = {"x": bx, "y": by}

    cfg = {
        "window_id": p6.get("WINDOW", 0),
        "double_click_to_confirm": double_click,
        "click_interval_sec": 0.35,
        "pre_action_delay_sec": 1.2,
        "train_buttons": buttons,
    }
    with open(COORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print("\n🎉 校准完成！6 个训练键坐标已保存至:", COORDS_FILE)
    for idx in range(1, 7):
        b = buttons[str(idx)]
        print(f"   - {TRAIN_NAMES[idx]}: ({b['x']}, {b['y']})")


def auto_click_training(idx: int, coords_cfg: dict):
    btn = coords_cfg.get("train_buttons", {}).get(str(idx))
    if not btn:
        print(f"⚠️ 未找到 {idx} 号训练键坐标，请先运行 --calibrate！")
        return

    delay = coords_cfg.get("pre_action_delay_sec", 1.2)
    print(f"🤖 [星奈代打] 等待 {delay:.1f}s 动画稳定后，自动点击 👉 【{TRAIN_NAMES.get(idx)}】 ({btn['x']}, {btn['y']})...")
    time.sleep(delay)

    if IS_WINDOWS:
        win32_click_at(
            int(btn["x"]),
            int(btn["y"]),
            double_click=coords_cfg.get("double_click_to_confirm", True),
            interval_sec=coords_cfg.get("click_interval_sec", 0.35),
        )
        return

    subprocess.run(["xdotool", "mousemove", "--sync", str(btn["x"]), str(btn["y"])], check=False)
    subprocess.run(["xdotool", "click", "1"], check=False)

    if coords_cfg.get("double_click_to_confirm", True):
        time.sleep(coords_cfg.get("click_interval_sec", 0.35))
        subprocess.run(["xdotool", "click", "1"], check=False)


def print_weekend_advice(state: dict, wk: dict, title_prefix: str = "🌴 【周末出行 & 商店参谋】"):
    print("\n" + "=" * 80)
    print(f"{title_prefix} [{wk['day_tag']}] ⚡精力: {wk['ps']}/130 | 💰金币: {wk['gold']} 金")
    print("-" * 80)
    if wk.get("lookahead_line"):
        print(f"  {wk['lookahead_line']}")
    print(f"  {wk['headline']}")
    for step_line in wk["plan_steps"]:
        print(f"    • {step_line}")
    print("-" * 80)
    print(f"  {wk['shop_one_line']}")
    if wk.get("has_weekend_train") and wk.get("scored_sat_trains"):
        tr_strs = " | ".join(
            f"{'👑' if i == 0 else ''}{t['name']}(双日EV:{t.get('route_ev', t['score']):.0f}·{t['reasons']})"
            for i, t in enumerate(wk["scored_sat_trains"][:3])
        )
        print(f"  🏋️ 周六加训(耗体-80%): {tr_strs}")
    if wk["scored_gashapon"]:
        g_strs = " | ".join(
            f"{'👑' if i == 0 else ''}{g['short']}" for i, g in enumerate(wk["scored_gashapon"])
        )
        print(f"  🎰 今日扭蛋: {g_strs}")
    if wk.get("scored_minigame"):
        mg_strs = " | ".join(
            f"{'👑' if i == 0 else ''}{m['short']}" for i, m in enumerate(wk["scored_minigame"])
        )
        print(f"  🎮 今日街机: {mg_strs}")
    if wk.get("mate_line"):
        print(f"  {wk['mate_line']}")
    print("=" * 80)


def parse_weekend_turns_from_text(text: str):
    state = make_initial_state()
    weekend_turns = []
    token_re = re.compile(
        r"(\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})|\[EDU_MODEL\]_LoadStory:\s+\d+\s+[^\s\t\r\n]+|request_call:\s+education\.(?:cs_train|cs_decide|cs_select_edu_activity|cs_buy_edu_item|cs_exit_state)\s*\{[^\n]*\})",
        re.S,
    )
    pending_weekend_snapshot = None
    pre_shop_snapshot = None
    bought_in_turn = []

    for m in token_re.finditer(text):
        tok = m.group(1)
        if tok.startswith("[EDU_MODEL]_LoadStory:"):
            sm = re.search(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", tok)
            if sm:
                ev_id = sm.group(1)
                state["cur_story"] = (ev_id, sm.group(2))
                if ev_id == "70":
                    state["fired_70"] = True
                elif ev_id == "71":
                    state["fired_71"] = True
            continue
        if tok.startswith("request_call:"):
            if "cs_train" in tok:
                cm = re.search(r"cmd_idx\s*=\s*(\d+)", tok)
                c_idx = int(cm.group(1)) if cm else None
                if c_idx and 1 <= c_idx <= 6:
                    state["train_counts"][c_idx] = state["train_counts"].get(c_idx, 0) + 1
                if state.get("round_type") == 2 and pending_weekend_snapshot and c_idx:
                    weekend_turns.append({
                        "wk_no": len(weekend_turns) + 1,
                        "state": pending_weekend_snapshot,
                        "pre_shop_state": pre_shop_snapshot or pending_weekend_snapshot,
                        "chosen_act": (0, c_idx),
                        "chosen_gasha_idx": None,
                        "bought_items": list(bought_in_turn),
                    })
                    pending_weekend_snapshot = None
                    pre_shop_snapshot = None
                state["round"] = state.get("round", 1) + 1
                bought_in_turn = []
            elif "cs_decide" in tok and "decide_type = 2" in tok:
                state["round"] = state.get("round", 1) + 1
                bought_in_turn = []
            elif "cs_buy_edu_item" in tok:
                bm = re.search(r"store_item_id\s*=\s*(\d+)", tok)
                if bm:
                    iid = int(bm.group(1))
                    _apply_facility_book_purchase(state, iid)
                    if state.get("round_type") == 2:
                        inm = STORE_ITEM_CATALOG.get(iid, {}).get("name", f"道具#{iid}")
                        bought_in_turn.append(f"{inm}({iid})")
            elif "cs_select_edu_activity" in tok and pending_weekend_snapshot:
                am = re.search(r"building_cfg_id\s*=\s*(\d+),selection_cfg_id\s*=\s*(\d+)", tok)
                if am:
                    b_id, s_id = int(am.group(1)), int(am.group(2))
                    # 检查是否只是点进 6号扭蛋/3号街机看一眼就 cs_exit_state 秒退
                    after_s = text[m.end():m.end() + 12000]
                    nxt_c = re.search(
                        r"request_call:\s+education\.(cs_train|cs_select_edu_activity|cs_exit_state)",
                        after_s,
                    )
                    if nxt_c and "cs_exit_state" in nxt_c.group(0):
                        continue
                    chosen_gasha_idx = None
                    if b_id == 6:
                        bound_end = nxt_c.start() if nxt_c else len(after_s)
                        gbm = re.search(
                            r"request_call:\s+education\.cs_select_team_building\s*\{\s*index\s*=\s*(\d+)",
                            after_s[:bound_end],
                        )
                        if gbm:
                            chosen_gasha_idx = int(gbm.group(1))
                    weekend_turns.append({
                        "wk_no": len(weekend_turns) + 1,
                        "state": pending_weekend_snapshot,
                        "pre_shop_state": pre_shop_snapshot or pending_weekend_snapshot,
                        "chosen_act": (b_id, s_id),
                        "chosen_gasha_idx": chosen_gasha_idx,
                        "bought_items": list(bought_in_turn),
                    })
                    pending_weekend_snapshot = None
                    pre_shop_snapshot = None
                    bought_in_turn = []
        else:
            update_state_from_token(state, tok)
            if state.get("round_type") == 1:
                pending_weekend_snapshot = None
                pre_shop_snapshot = None
                bought_in_turn = []
            elif state.get("round_type") == 2:
                # v0.10.0 修复买药后采样失真：
                # 首次收到 team_building_list 记录买前 pre_shop_snapshot；
                # 后续（包括买药/赌狗药结算后）持续刷新 pending_weekend_snapshot，确保 chosen_act 对照执行动作前真实状态！
                if "team_building_list" in tok and pre_shop_snapshot is None:
                    pre_snap = clone_state_snapshot(state)
                    pre_snap["round_type"] = 2
                    pre_snap["weekend_day"] = state.get("weekend_day", 1) or 1
                    pre_shop_snapshot = pre_snap
                if "team_building_list" in tok or pending_weekend_snapshot is not None:
                    snap = clone_state_snapshot(state)
                    snap["round_type"] = 2
                    snap["weekend_day"] = state.get("weekend_day", 1) or 1
                    pending_weekend_snapshot = snap
    return weekend_turns


def parse_story_turns_from_text(text: str):
    """提取一局中触发的带选项节点 ([SCENARIO]进入选择节点 + cs_event_branch) 及其当时状态快照。"""
    state = make_initial_state()
    story_turns = []
    token_re = re.compile(
        r"(\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})|\[EDU_MODEL\]_LoadStory:\s+\d+\s+[^\s\t\r\n]+|\[SCENARIO\]进入选择节点|request_call:\s+education\.(?:cs_train|cs_decide|cs_buy_edu_item|cs_event_branch)\s*\{[^\n]*\})",
        re.S,
    )
    pending_story_snap = None

    for m in token_re.finditer(text):
        tok = m.group(1)
        if tok.startswith("[EDU_MODEL]_LoadStory:"):
            sm = re.search(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", tok)
            if sm:
                ev_id = sm.group(1)
                state["cur_story"] = (ev_id, sm.group(2))
                if ev_id == "70":
                    state["fired_70"] = True
                elif ev_id == "71":
                    state["fired_71"] = True
            pending_story_snap = None
            continue
        if tok == "[SCENARIO]进入选择节点":
            pending_story_snap = clone_state_snapshot(state)
            continue
        if tok.startswith("request_call:"):
            if "cs_train" in tok:
                cm = re.search(r"cmd_idx\s*=\s*(\d+)", tok)
                if cm:
                    c_idx = int(cm.group(1))
                    if 1 <= c_idx <= 6:
                        state["train_counts"][c_idx] = state["train_counts"].get(c_idx, 0) + 1
                state["round"] = state.get("round", 1) + 1
            elif "cs_decide" in tok and "decide_type = 2" in tok:
                state["round"] = state.get("round", 1) + 1
            elif "cs_buy_edu_item" in tok:
                bm = re.search(r"store_item_id\s*=\s*(\d+)", tok)
                if bm:
                    _apply_facility_book_purchase(state, int(bm.group(1)))
            elif "cs_event_branch" in tok:
                bm = re.search(r"branch_idx\s*=\s*(\d+)", tok)
                if bm and state.get("cur_story"):
                    snap = pending_story_snap or clone_state_snapshot(state)
                    story_turns.append({
                        "ev_id": str(state["cur_story"][0]),
                        "ev_name": state["cur_story"][1],
                        "chosen_branch": str(bm.group(1)),
                        "state": snap,
                    })
                    pending_story_snap = None
        else:
            update_state_from_token(state, tok)
    return story_turns


def replay_last_run(strategy_name="training_flow", run_offset: int = 1):
    strategy = load_strategy(strategy_name)
    event_db = load_event_db()
    stitched = [r for r in extract_stitched_run_segments(LOG_DIR) if "education.cs_train" in r["seg"]]
    if not stitched:
        print("未找到包含训练记录的育成对局。")
        return

    idx = max(0, len(stitched) - max(1, int(run_offset)))
    target = stitched[idx]
    seg = target["seg"]
    log_label = (
        f"{target['log']} ➔ {target['end_log']}"
        if target["end_log"] != target["log"]
        else f"{target['log']}#{target['run_index']}"
    )

    turns, final_st = parse_state_and_turns_from_text(seg, return_final_state=True)
    wk_turns = parse_weekend_turns_from_text(seg)
    story_turns = parse_story_turns_from_text(seg)

    res_ranks = [int(x) for x in re.findall(r"new_rank\s*=\s*(\d+)", seg) if 3000 <= int(x) <= 20000]
    final_rank = res_ranks[-1] if res_ranks else final_st.get("rank", 0)
    lineup_str = format_lineup_summary(final_st) or "未知阵容"
    _, w3_final = get_whistle_step(final_st)

    # 1. 结算体检与水桶底盘诊断
    f_props = final_st.get("props", {})
    f_max = final_st.get("prop_max", {})
    stat_sum = sum(f_props.values())
    capped_list = []
    short_list = []
    stat_cells = []
    for k in range(101, 107):
        v = f_props.get(k, 0)
        mv = f_max.get(k, 1600)
        pct = v / max(1, mv) * 100
        nm = STAT_NAMES[k]
        flag = ""
        if v >= mv:
            flag = "⚠️满顶"
            capped_list.append(f"{nm}({v}/{mv})")
        elif pct >= 94:
            flag = "🔥准满"
        elif v < 1150:
            flag = "📉短板"
            short_list.append(f"{nm}({v}/{mv})")
        stat_cells.append(f"{nm}:{v}/{mv}({pct:.0f}%{flag})")

    fac_strs = []
    click_dist = {k: 0 for k in range(0, 7)}
    for t in turns:
        click_dist[t["chosen_idx"]] = click_dist.get(t["chosen_idx"], 0) + 1
    for k in range(1, 7):
        lv = final_st.get("focus_levels", {}).get(k, 1)
        fac_strs.append(f"{k}号({STAT_NAMES[100+k]}):Lv{lv}[点{click_dist.get(k, 0)}次]")

    print("=" * 88)
    print(
        f"🏀 星奈 v0.7.0 单局深度体检报告 | 对局: {log_label} (倒数第{run_offset}局) | 策略: {strategy['name']}"
    )
    print("=" * 88)
    print(
        f"📌 【一、结算总览】最终评分: {final_rank} 分 | 六维总和: {stat_sum} | 5人阵容: {lineup_str}"
    )
    sat_train_cnt = sum(1 for t in turns if t.get("is_weekend_train"))
    weekday_train_cnt = len(turns) - sat_train_cnt
    train_summary_str = (
        f"训练总计: {len(turns)} 训 (平时 {weekday_train_cnt} + ⚡周六加训 {sat_train_cnt}, 休息 {click_dist.get(0, 0)} 次)"
        if sat_train_cnt > 0
        else f"平时训练: {len(turns)} 训 (休息 {click_dist.get(0, 0)} 次)"
    )
    print(
        f"   • 经理爆发: {final_st['mgr_explosive']} 次 | 哨子状态: {w3_final} | {train_summary_str}"
    )
    print(f"   • 期末六维: {' | '.join(stat_cells[:3])}")
    print(f"              {' | '.join(stat_cells[3:])}")
    print(f"   • 设施等级: {' | '.join(fac_strs)}")
    if capped_list or short_list:
        diag_parts = []
        if capped_list:
            diag_parts.append(f"⚠️ 触顶溢出项: {'、'.join(capped_list)}（后期继续点主训收益被硬截断归零）")
        if short_list:
            diag_parts.append(f"📉 未满短板项: {'、'.join(short_list)}（分流 2~3 训补短板可显著抬升结算 Rank）")
        for dp in diag_parts:
            print(f"   👉 {dp}")

    # 2. 平时 28 训决策对比 & 商店联动抓虫
    match_count = 0
    top2_count = 0
    total_ev_loss = 0.0
    suboptimal_turns = []

    for t in turns:
        scored = score_training_turn(t["state"], t["cmds"], strategy)
        best = scored[0]
        chosen = next((x for x in scored if x["idx"] == t["chosen_idx"]), None)
        if best["idx"] == t["chosen_idx"]:
            match_count += 1
        if t["chosen_idx"] in (scored[0]["idx"], scored[1]["idx"]):
            top2_count += 1
        diff = (best["score"] - chosen["score"]) if chosen else 0.0
        if diff > 0:
            total_ev_loss += diff
        if chosen and diff >= 12.0:
            shop_tip = format_weekday_shop_tip(
                t["state"], best_ev=best["score"], strategy=strategy, scored_cmds=scored
            )
            suboptimal_turns.append((t, best, chosen, diff, shop_tip))

    suboptimal_turns.sort(key=lambda x: x[3], reverse=True)
    avg_loss = total_ev_loss / max(1, len(turns))
    print("\n" + "-" * 88)
    print(
        f"🏋️ 【二、平时训练决策复盘】首选一致率: {match_count}/{len(turns)} ({match_count / max(1, len(turns)) * 100:.1f}%) "
        f"| 前二命中率: {top2_count}/{len(turns)} ({top2_count / max(1, len(turns)) * 100:.1f}%) "
        f"| 平均每训EV偏离: -{avg_loss:.1f} EV"
    )
    print("-" * 88)
    if suboptimal_turns:
        print(f"💡 抓出 {len(suboptimal_turns)} 个高分差分歧回合（按亏损 EV 从大到小展示 Top 5）：")
        for t, best, chosen, diff, shop_tip in suboptimal_turns[:5]:
            st = t["state"]
            props_str = " ".join(f"{STAT_NAMES[k]}:{v}" for k, v in sorted(st["props"].items()))
            _, w3_str = get_whistle_step(st)
            bought_str = f" | 已买:{'、'.join(t['bought_items'])}" if t.get("bought_items") else ""
            sat_badge = " · ⚡周六加训" if t.get("is_weekend_train") else ""
            print(
                f"  • [第 {t['turn_no']:02d} 训{sat_badge} | ⚡{st['ps']} 💰{st['gold']} | 经理:{st['mgr_energy']}/3 爆{st['mgr_explosive']} ({w3_str}){bought_str}] ({props_str})"
            )
            print(f"    ❌ 实操选了 [{chosen['name']}] (EV {chosen['score']:.1f}): {chosen['reasons']}")
            print(f"    👑 星奈首选 [{best['name']}] (EV {best['score']:.1f}, 差 -{diff:.1f} EV): {best['reasons']}")
            if shop_tip and ("🛒必买" in shop_tip or "🎁" in shop_tip or "⚡低体" in shop_tip or "🔥商店" in shop_tip):
                print(f"    🛒 当回合商店联动: {shop_tip}")
    else:
        print("  🎉 太强了！全周期平时训练零重大失误（所有回合与星奈首选 EV 差均 < 12）！")

    # 3. 周末 13 动路线 & 扭蛋/街机/商店复盘
    if wk_turns:
        act_names = {
            **{(0, k): f"🏋️周六加训·{TRAIN_NAMES[k]}" for k in range(1, 7)},
            (6, 17): "🎰6号·抽扭蛋",
            (3, 6): "🎮3号·玩街机",
            (8, 19): "💼8号·散步/轻打工(0体)",
            (8, 20): "🔨8号·重度打工(-25体)",
            (1, 14): "🧺1号·外出野餐(+70体)",
            (1, 13): "🚣1号·公园划船(+30体)",
            (1, 1): "🚣1号·公园划船(+30体)",
        }
        wk_match_cnt = 0
        gasha_total_cnt = 0
        gasha_top1_cnt = 0
        wk_divergences = []
        wk_highlights = []

        for wt in wk_turns:
            wk_adv = evaluate_weekend_and_shop(wt["state"], strategy)
            c_act = wt["chosen_act"]
            best_act = wk_adv.get("best_act")
            best_ev = wk_adv.get("best_ev", 0.0)
            act_map = wk_adv.get("act_ev_map", {})
            chosen_route_info = act_map.get(c_act)
            chosen_ev = chosen_route_info["total_ev"] if chosen_route_info else (best_ev - 25.0)
            ev_gap = max(0.0, best_ev - chosen_ev)

            # 检查若进了 6号扭蛋，三选一机台是否选了当日最高 EV 那台
            gasha_machine_note = ""
            gasha_machine_loss = 0.0
            if c_act == (6, 17) and wk_adv.get("scored_gashapon"):
                gasha_total_cnt += 1
                sg = wk_adv["scored_gashapon"]
                top_g = sg[0]
                c_g_idx = wt.get("chosen_gasha_idx")
                if c_g_idx is not None:
                    picked_g = next((x for x in sg if x["idx"] == c_g_idx), None)
                    if picked_g:
                        if picked_g["cfg_id"] == top_g["cfg_id"] or abs(picked_g["ev"] - top_g["ev"]) <= 3.0:
                            gasha_top1_cnt += 1
                            gasha_machine_note = f"🎯选机命中👑[{picked_g['short']}]"
                        else:
                            gasha_machine_loss = round(top_g["ev"] - picked_g["ev"], 1)
                            gasha_machine_note = (
                                f"⚠️三选一抽了[{picked_g['short']}]，漏掉👑[{top_g['short']}](亏-{gasha_machine_loss:.0f}EV)"
                            )

            is_route_match = (c_act == best_act) or (ev_gap <= 6.0)
            if is_route_match:
                wk_match_cnt += 1

            total_wk_loss = round(ev_gap + gasha_machine_loss, 1)
            item_record = (wt, wk_adv, c_act, total_wk_loss, gasha_machine_note)
            if total_wk_loss >= 10.0:
                wk_divergences.append(item_record)
            else:
                wk_highlights.append(item_record)

        wk_divergences.sort(key=lambda x: x[3], reverse=True)
        print("\n" + "-" * 88)
        gasha_acc_str = (
            f" | 🎰扭蛋选机命中: {gasha_top1_cnt}/{gasha_total_cnt}"
            if gasha_total_cnt > 0
            else ""
        )
        print(
            f"🌴 【三、周末出行 & 商店/扭蛋复盘】周末路线一致率: {wk_match_cnt}/{len(wk_turns)} ({wk_match_cnt / max(1, len(wk_turns)) * 100:.1f}%)"
            f"{gasha_acc_str}"
        )
        print("-" * 88)
        if wk_divergences:
            print(f"💡 发现 {len(wk_divergences)} 个可优化的周末决策节点（展示亏损最大的前 4 动）：")
            for wt, wk_adv, c_act, loss_v, gm_note in wk_divergences[:4]:
                act_str = act_names.get(c_act, f"建筑{c_act}")
                buy_str = "、".join(wt["bought_items"]) if wt["bought_items"] else "未买道具"
                gm_sfx = f" | {gm_note}" if gm_note else ""
                print(
                    f"  • [{wk_adv['day_tag']} (第{wt['wk_no']:02d}动) | ⚡{wk_adv['ps']}/130 💰{wk_adv['gold']}金 | 亏损: -{loss_v:.0f} EV]"
                )
                print(f"    ❌ 实操: {act_str} (🛒商店:{buy_str}){gm_sfx}")
                print(f"    {wk_adv['headline']}")
                if wk_adv.get("lookahead_line"):
                    print(f"    📅 前瞻依据: {wk_adv['lookahead_line']}")
                print(f"    {wk_adv['shop_one_line']}")
        else:
            print("  🎉 本局周末路线与扭蛋选机全部合拍！展示 2 个高光周末决策：")
            for wt, wk_adv, c_act, _, gm_note in wk_highlights[:2]:
                act_str = act_names.get(c_act, f"建筑{c_act}")
                buy_str = "、".join(wt["bought_items"]) if wt["bought_items"] else "未买道具"
                print(f"  • [{wk_adv['day_tag']} | 实操: {act_str} | 商店: {buy_str}] 👉 {wk_adv['headline']}")

    # 4. 关键三选一/剧情事件复盘（集训、许愿、哨子70/100熟练度、球星卡分支）
    eval_events = []
    ev_match_cnt = 0
    ev_suboptimal = []
    for st_ev in story_turns:
        adv = evaluate_story_event(st_ev["ev_id"], st_ev["ev_name"], st_ev["state"], strategy, event_db)
        s_opts = adv.get("scored_options") or []
        if len(s_opts) < 2:
            continue
        eval_events.append((st_ev, adv))
        best_opt = s_opts[0]
        chosen_opt = next((x for x in s_opts if x["b_idx"] == st_ev["chosen_branch"]), None)
        if best_opt["b_idx"] == st_ev["chosen_branch"] or (
            chosen_opt and best_opt["ev"] - chosen_opt["ev"] <= 3.0
        ):
            ev_match_cnt += 1
        elif chosen_opt and (best_opt["ev"] - chosen_opt["ev"]) >= 6.0:
            ev_suboptimal.append((st_ev, best_opt, chosen_opt, best_opt["ev"] - chosen_opt["ev"]))

    if eval_events:
        print("\n" + "-" * 88)
        print(
            f"📖 【四、关键事件三选一/二选一复盘】有效事件一致率: {ev_match_cnt}/{len(eval_events)} ({ev_match_cnt / max(1, len(eval_events)) * 100:.1f}%)"
        )
        print("-" * 88)
        if ev_suboptimal:
            for st_ev, best_o, chosen_o, diff_e in ev_suboptimal[:5]:
                snap = st_ev["state"]
                print(
                    f"  • 《{st_ev['ev_name']}》(ID:{st_ev['ev_id']} | 当时⚡精力:{snap['ps']}/130 💰金币:{snap['gold']})"
                )
                print(f"    ❌ 实操选了 {chosen_o['title']} (EV:{chosen_o['ev']:.1f}) — {chosen_o['effects']}")
                print(
                    f"    👑 星奈推荐 {best_o['title']} (EV:{best_o['ev']:.1f}, 差 -{diff_e:.1f} EV) — {best_o['effects']}"
                )
        else:
            print("  🎉 所有关键事件（集训/许愿/哨子70&100三选一/球星卡事件）均命中当时的最优分支！")
    print("=" * 88)


# =====================================================================
# 原生按钮贴身角标池 (BadgePillWindow · 风格B奶白斜切章) + 顶栏第4原生槽 HUD (FloatingHUD)
# =====================================================================

DEFAULT_DIEGETIC_ANCHORS = DEFAULT_HUD_ANCHORS


def _deep_merge_coords(base_d: dict, override_d: dict) -> dict:
    out = dict(base_d)
    for k, v in (override_d or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_coords(out[k], v)
        else:
            out[k] = v
    return out


def load_ui_anchor_coords() -> dict:
    """只读读取 config/button_coords.json 并与内置归一化锚点兜底合并（严禁写回改动配置文件）。"""
    merged = _deep_merge_coords({}, DEFAULT_DIEGETIC_ANCHORS)
    if os.path.exists(COORDS_FILE):
        try:
            with open(COORDS_FILE, "r", encoding="utf-8") as f:
                disk_cfg = json.load(f)
            if isinstance(disk_cfg, dict):
                merged = _deep_merge_coords(merged, disk_cfg)
        except Exception:
            pass
    return merged


HUD_RECOMMENDATION_SPECS = [
    ("train", "训练推荐", "平日/周六加训的一级/二级训练与休息角标及哨子体力提示", ("train", "training", "训练", "平日", "xl")),
    ("story", "剧情选项", "剧情二选一/三选一分支选项优先级角标与底部推荐", ("story", "event", "剧情", "事件", "选项", "jq")),
    ("weekend", "周末出行", "周末大地图/扭蛋/街机/公园/餐厅角标与路线", ("weekend", "wk", "outing", "周末", "出行", "扭蛋", "街机", "zm")),
    ("shop", "商店好货", "商店内部参谋、右上角🛒角标与顺路买药提醒", ("shop", "store", "商店", "买药", "好货", "sd")),
    ("skill", "选技能", "育成技能三选一契合度角标与底部推荐", ("skill", "skills", "技能", "选技能", "jn")),
    ("note", "开局选人", "开局选人编队界面笔记配对与本命器材条", ("note", "notes", "equip", "initactor", "选人", "笔记", "器材", "配装", "bj")),
    ("secondary", "次选优先级", "各界面 🥈/🥉 次选对比角标（关闭后仅保留 👑 首选与 ⚠️ 警告）", ("secondary", "second", "alt", "priority", "次选", "备选", "优先级", "cx")),
]

_HUD_REC_ALIAS_MAP = {}
for _rk, _rlabel, _rdesc, _raliases in HUD_RECOMMENDATION_SPECS:
    _HUD_REC_ALIAS_MAP[_rk] = _rk
    _HUD_REC_ALIAS_MAP[_rlabel] = _rk
    for _ra in _raliases:
        _HUD_REC_ALIAS_MAP[_ra.lower()] = _rk


def get_default_hud_config() -> dict:
    """返回默认 HUD 推荐开关与专项特化配置（默认关闭专项偏科，不改变任何原有评分）。"""
    return {
        "diegetic_mode": True,
        "auto_check_updates": True,
        "recommendations": {key: True for key, *_ in HUD_RECOMMENDATION_SPECS},
        "focus": {
            "stat": None,
            "bonus": DEFAULT_FOCUS_BONUS,
            "last_stat": DEFAULT_FOCUS_STAT,
        },
    }


def load_hud_config() -> dict:
    """读取 config/hud_config.json 并与默认开关合并；同时同步全局专项偏科状态。"""
    cfg = get_default_hud_config()
    if os.path.exists(HUD_CONFIG_FILE):
        try:
            with open(HUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                if "diegetic_mode" in disk:
                    cfg["diegetic_mode"] = bool(disk["diegetic_mode"])
                if "auto_check_updates" in disk:
                    cfg["auto_check_updates"] = bool(disk["auto_check_updates"])
                recs = disk.get("recommendations", disk)
                if isinstance(recs, dict):
                    for key, *_ in HUD_RECOMMENDATION_SPECS:
                        if key in recs:
                            cfg["recommendations"][key] = bool(recs[key])
                if "focus" in disk:
                    cfg["focus"] = _normalize_focus_dict(disk.get("focus"))
        except Exception:
            pass
    _sync_active_focus_from_cfg(cfg)
    return cfg


def save_hud_config(cfg: dict) -> dict:
    """规范化并持久化 HUD 推荐开关与专项偏科配置到 config/hud_config.json。"""
    normalized = get_default_hud_config()
    if isinstance(cfg, dict) and "diegetic_mode" in cfg:
        normalized["diegetic_mode"] = bool(cfg["diegetic_mode"])
    if isinstance(cfg, dict) and "auto_check_updates" in cfg:
        normalized["auto_check_updates"] = bool(cfg["auto_check_updates"])
    recs = cfg.get("recommendations", cfg) if isinstance(cfg, dict) else {}
    if isinstance(recs, dict):
        for key, *_ in HUD_RECOMMENDATION_SPECS:
            if key in recs:
                normalized["recommendations"][key] = bool(recs[key])
    if isinstance(cfg, dict) and "focus" in cfg:
        normalized["focus"] = _normalize_focus_dict(cfg.get("focus"))
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(HUD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _sync_active_focus_from_cfg(normalized)
    return normalized


def is_hud_rec_enabled(cfg: Optional[dict], key: str) -> bool:
    """判断指定类别的 HUD 推荐是否开启。"""
    if not isinstance(cfg, dict):
        return True
    recs = cfg.get("recommendations", cfg)
    if not isinstance(recs, dict):
        return True
    return bool(recs.get(key, True))


def resolve_hud_rec_keys(raw_items: list[str]) -> tuple[list[str], list[str]]:
    """把 CLI 传入的英文/中文/逗号分隔开关名解析为规范 key 列表，返回 (keys, unknown_tokens)。"""
    keys = []
    unknown = []
    for raw in raw_items or []:
        for tok in str(raw).replace("，", ",").split(","):
            t = tok.strip().lower()
            if not t:
                continue
            if t in ("all", "全部", "所有", "*"):
                for k, *_ in HUD_RECOMMENDATION_SPECS:
                    if k not in keys:
                        keys.append(k)
                continue
            mapped = _HUD_REC_ALIAS_MAP.get(t)
            if mapped:
                if mapped not in keys:
                    keys.append(mapped)
            else:
                unknown.append(tok.strip())
    return keys, unknown


def print_hud_config_summary(cfg: Optional[dict] = None, header_note: str = ""):
    """以清爽表格打印当前 HUD 推荐开关配置与 CLI 快捷用法。"""
    cur = cfg if isinstance(cfg, dict) else load_hud_config()
    if header_note:
        print(header_note)
    print(f"⚙️ 星奈 HUD 推荐开关配置 ({HUD_CONFIG_FILE})：")
    for key, label, desc, _ in HUD_RECOMMENDATION_SPECS:
        enabled = is_hud_rec_enabled(cur, key)
        mark = "✅ 开" if enabled else "⛔ 关"
        print(f"  {mark}  {key:<10} [{label}]  — {desc}")
    fc = _normalize_focus_dict(cur.get("focus"))
    if fc["stat"] is not None:
        st_nm = STAT_NAMES.get(fc["stat"], str(fc["stat"]))
        print(f"\n🎯 专项偏科模式：✅ 开启【{st_nm}特化】 (倍率 ×{fc['bonus']:.2f} · 局内可点 HUD 顶栏小开关随时切换)")
    else:
        last_nm = STAT_NAMES.get(fc["last_stat"], "投射")
        print(f"\n🎯 专项偏科模式：⚪ 关闭（纯评分模式 · 默认记忆目标: {last_nm}，倍率 ×{fc['bonus']:.2f}）")
    print("\n💡 快捷切换（后台运行中的 HUD 会自动热刷新，无需重启）：")
    print("  hud focus 投射      开启投射专项特化（支持六维任意属性，如 投射/弹跳/防守/体能/运控/意识）")
    print("  hud focus off       关闭专项特化，切回纯评分模式")
    print("  hud on <项...>      开启指定推荐（如: hud on story / hud on all）")
    print("  hud off <项...>     关闭指定推荐（如: hud off story / hud off 剧情 次选）")
    print("  hud toggle <项...>  反转开关状态（如: hud toggle story shop）")
    print("  hud only <项...>    仅开启指定几项、其余关闭（如: hud only train skill）")
    print("  hud cfg reset       全部恢复默认开启（并关闭专项特化）")


def apply_hud_focus_cli_action(raw_args: Optional[list[str]] = None) -> int:
    """处理 CLI 专项偏科设置（如 hud focus 投射 / hud focus off / hud focus 投射 1.4）。"""
    cfg = load_hud_config()
    fc = _normalize_focus_dict(cfg.get("focus"))
    args = [str(a).strip() for a in (raw_args or []) if str(a).strip()]

    if not args or args[0].lower() in ("show", "status", "ls"):
        if fc["stat"] is not None:
            st_nm = STAT_NAMES.get(fc["stat"], str(fc["stat"]))
            print(f"🎯 当前专项特化：✅ 开启【{st_nm}】 (倍率 ×{fc['bonus']:.2f})")
        else:
            last_nm = STAT_NAMES.get(fc["last_stat"], "投射")
            print(f"🎯 当前专项特化：⚪ 关闭（纯评分模式 · 默认一键开启目标: {last_nm}，倍率 ×{fc['bonus']:.2f}）")
        print("💡 用法: hud focus 投射 | hud focus off | hud focus 投射 1.4")
        return 0

    tok0 = args[0].lower()
    if tok0 in ("off", "none", "0", "关", "关闭", "清空", "reset", "clear"):
        fc["stat"] = None
        if len(args) >= 2:
            try:
                bf = float(args[1])
                if 1.05 <= bf <= 2.0:
                    fc["bonus"] = round(bf, 2)
            except Exception:
                pass
        cfg["focus"] = fc
        save_hud_config(cfg)
        print("⚪ 已关闭专项偏科模式，切回纯总评最优推荐（HUD 自动热刷新）。")
        return 0

    if tok0 in ("on", "开", "开启"):
        fc["stat"] = fc["last_stat"] or DEFAULT_FOCUS_STAT
    elif tok0 in ("toggle", "sw", "切换"):
        fc["stat"] = None if fc["stat"] is not None else (fc["last_stat"] or DEFAULT_FOCUS_STAT)
    elif tok0 in FOCUS_STAT_ALIASES:
        fc["stat"] = FOCUS_STAT_ALIASES[tok0]
        fc["last_stat"] = fc["stat"]
    else:
        print(f"❌ 无法识别的属性名: {args[0]}")
        print("   可用属性: 投射 / 弹跳 / 防守 / 体能 / 运控 / 意识（或 off 关闭）")
        return 1

    if len(args) >= 2:
        try:
            bf = float(args[1])
            if not (1.05 <= bf <= 2.0):
                print("❌ 倍率需在 1.05 ~ 2.00 之间（推荐默认 1.35）。")
                return 1
            fc["bonus"] = round(bf, 2)
        except Exception:
            print(f"❌ 无效倍率参数: {args[1]}（请输入 1.05 ~ 2.00 之间的数字）")
            return 1

    cfg["focus"] = fc
    save_hud_config(cfg)
    if fc["stat"] is not None:
        st_nm = STAT_NAMES.get(fc["stat"], str(fc["stat"]))
        print(f"🎯 已开启【{st_nm}特化】模式（倍率 ×{fc['bonus']:.2f}，HUD 自动热刷新并重算当局角标）。")
    else:
        print("⚪ 已关闭专项偏科模式，切回纯总评模式（HUD 自动热刷新）。")
    return 0


def apply_hud_config_cli_action(action: str, items: Optional[list[str]] = None) -> int:
    """处理 CLI 对 HUD 推荐开关的查询或修改请求，返回进程退出码。"""
    if action == "show":
        print_hud_config_summary(load_hud_config())
        return 0
    if action == "reset":
        saved = save_hud_config(get_default_hud_config())
        print_hud_config_summary(saved, header_note="♻️ 已重置全部 HUD 推荐开关为默认开启。")
        return 0

    keys, unknown = resolve_hud_rec_keys(items or [])
    if unknown:
        valid = ", ".join(f"{k}({lbl})" for k, lbl, *_ in HUD_RECOMMENDATION_SPECS)
        print(f"❌ 未知推荐项: {', '.join(unknown)}")
        print(f"   可用项: {valid} 或 all")
        return 1
    if not keys:
        print("❌ 请至少指定一个推荐项（例如: story / train / weekend / shop / skill / note / secondary / all）。")
        return 1

    cfg = load_hud_config()
    recs = cfg["recommendations"]
    if action == "on":
        for k in keys:
            recs[k] = True
        note = f"✅ 已开启: {', '.join(keys)}"
    elif action == "off":
        for k in keys:
            recs[k] = False
        note = f"⛔ 已关闭: {', '.join(keys)}"
    elif action == "toggle":
        for k in keys:
            recs[k] = not bool(recs.get(k, True))
        note = f"🔄 已切换: {', '.join(keys)}"
    elif action == "only":
        key_set = set(keys)
        for k, *_ in HUD_RECOMMENDATION_SPECS:
            recs[k] = k in key_set
        note = f"🎯 已设为仅开启: {', '.join(keys)}"
    else:
        print(f"❌ 不支持的操作: {action}")
        return 1

    saved = save_hud_config(cfg)
    print_hud_config_summary(saved, header_note=note)
    return 0


def query_game_window_display_and_xid() -> tuple[str, str]:
    """通过 xwininfo 只读获取当前可见游戏窗口 (display, XID)，优先自动适配独立 Xwayland 沙盒（如 :20）。"""
    displays: list[str] = []
    env_disp = (os.environ.get("LQSN_DISPLAY") or "").strip()
    if env_disp:
        displays.append(env_disp if env_disp.startswith(":") else f":{env_disp}")
    sandbox_nums: list[int] = []
    try:
        for entry in os.listdir("/tmp/.X11-unix"):
            if entry.startswith("X") and entry[1:].isdigit():
                num = int(entry[1:])
                if num not in (0, 1):
                    sandbox_nums.append(num)
    except Exception:
        pass
    for num in sorted(sandbox_nums):
        d = f":{num}"
        if d not in displays:
            displays.append(d)
    for d in (os.environ.get("DISPLAY") or ":1", ":1", ":0"):
        if d and d not in displays:
            displays.append(d)

    for disp in displays:
        try:
            out = subprocess.check_output(
                ["env", f"DISPLAY={disp}", "xwininfo", "-name", "篮球少女：飞跃巅峰"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.25,
            )
            if "IsViewable" not in out:
                continue
            m = re.search(r"Window id:\s+(0x[0-9a-fA-F]+|\d+)", out)
            if m:
                return disp, m.group(1)
        except Exception:
            continue
    return "", ""


def query_game_window_xid() -> str:
    """通过 xwininfo 只读获取当前可见游戏窗口 XID（供选技能视觉自学习小图抓取使用）。"""
    _, wid = query_game_window_display_and_xid()
    return wid


def extract_weekday_shop_badge(state: dict, scored_cmds: list = None) -> str:
    """
    提取平日一级界面右上角『商店』按钮的精简好货角标（无必买/高优道具时返回空串，绝不误报打扰）。
    """
    store_items = [it for it in state.get("store_items", []) if it.get("num", 0) > 0]
    if not store_items:
        return ""
    if any(it["price"] == 0 or it["id"] in (1151, 1152) for it in store_items):
        return "🎁 0金免费领"

    sched_ctx = get_upcoming_schedule_context(state)
    wk_next_heal = sched_ctx.get("weekday_next_turn_heal", 0)
    exp_r = sched_ctx.get("exp_round", state.get("exp_round", 1))
    day_idx = (exp_r - 1) % 7 if exp_r > 0 else 1
    ps = state.get("ps", 130)
    ps_cap = get_ps_max(state)
    gold = state.get("gold", 0)
    active_states = state.get("active_states", set())

    best_train = next((x for x in (scored_cmds or []) if x.get("idx", 0) != 0), None)
    best_cmd = best_train["cmd"] if best_train else {}
    mate_cnt = best_cmd.get("mate_count", 0) + (1 if best_cmd.get("is_manager") else 0)
    mate_ids = best_cmd.get("mate_ids", [])
    lineup_all = set(state.get("teammate_char_ids", [])) | (
        {state["main_char_id"]} if state.get("main_char_id") else set()
    )
    has_pull = (1046 in lineup_all) or (1035 in lineup_all) or (1046 in mate_ids) or (1035 in mate_ids)
    eff_mates_now = (mate_cnt + (0.9 if has_pull and best_cmd.get("mate_count", 0) < 4 else 0.0)) if mate_cnt > 0 else 0.0
    max_top_fail = max(
        (x["cmd"].get("fail_rate", 0) for x in (scored_cmds or [])[:2] if x.get("idx", 0) != 0),
        default=0,
    )
    best_safe_ev = max(
        (x["score"] for x in (scored_cmds or []) if (x.get("cmd") or {}).get("fail_rate", 0) <= 10),
        default=60.0,
    )
    best_zf_gain = 0.0
    for x in (scored_cmds or []):
        if x.get("idx", 0) != 0 and x["cmd"].get("fail_rate", 0) >= 12:
            zf_ev = float(x.get("raw_zero_fail_score", x["score"]))
            best_zf_gain = max(best_zf_gain, zf_ev - best_safe_ev)

    # 低体买药预警
    lineup_th, _ = get_lineup_ps_threshold(state)
    low_ps_thresh = 50 if wk_next_heal >= 30 else max(72, lineup_th + 18)
    if ps <= low_ps_thresh and (max_top_fail >= 8 or ps <= 58):
        st_its = [
            STORE_ITEM_CATALOG[it["id"]]["name"]
            for it in store_items
            if it["id"] in STORE_ITEM_CATALOG
            and STORE_ITEM_CATALOG[it["id"]]["cat"] == "stamina"
            and gold >= it["price"]
        ]
        if st_its:
            return f"🛒 低体买{st_its[0][:4]}"

    # 必买/高优持续 Buff
    has_sat = has_doumeki_in_lineup(state)
    for it in store_items:
        iid, price = it["id"], it["price"]
        meta = STORE_ITEM_CATALOG.get(iid)
        if not meta or meta.get("cat") != "buff" or gold < price:
            continue
        sid = meta.get("state_id")
        if sid and sid in active_states:
            continue
        subcat = meta.get("subcat", "")
        days = meta.get("days", 3)
        cov_trains = count_training_days_in_window(exp_r, days, has_sat_train=has_sat)
        if subcat == "zero_fail" and (best_zf_gain >= 35.0 or max_top_fail >= 12):
            return f"🛒 必买{meta['name']}"
        if subcat == "teammate_emoji" and (
            (cov_trains >= 3 and (eff_mates_now >= 1 or gold >= 160))
            or (cov_trains >= 2 and (day_idx != 4 or has_sat) and eff_mates_now >= 1.8 and gold >= 150)
        ):
            return f"🛒 必买{meta['name']}"
        if subcat in ("equip_boost_ps", "equip_boost_free"):
            eb = eval_equip_boost_shop_advice(subcat, meta, price, state, scored_cmds, exp_r, day_idx)
            if eb["recommend"]:
                return f"🛒 {eb['level']}{meta['name']}"
        if subcat == "ps_discount" and cov_trains >= 2 and day_idx in (1, 2, 3):
            return f"🛒 推荐{meta['name']}"

    # 冲哨/满彩全器材熟练度道具
    for it in store_items:
        if it["id"] in (1113, 1111, 1112) and gold >= it["price"]:
            b_info = calc_shop_bond_item_ev(it["id"], it["price"], state)
            net_b = b_info["ev"] - it["price"] * 0.30
            if b_info["eff_total"] > 0 and (b_info["maxed_new"] > 0 or "哨➔" in b_info["tag"] or net_b >= 14.0):
                return f"🛒 {STORE_ITEM_CATALOG[it['id']]['name'][:5]}"

    # 特调专属饮料+ (1152) 满额不折半高优提醒
    for it in store_items:
        if it["id"] == 1152 and gold >= it["price"] and state.get("round", 1) >= 20:
            return "🛒 特调专属饮+"

    return ""


class BadgePillWindow:
    """
    轻量级单枚按钮贴身徽章微窗 (Gtk.WindowType.POPUP · 风格B 清爽奶白斜切章)：
    - 复刻游戏左上角『7天 | 赢得比赛』与『休息』按钮视觉语言：奶白底板 #F8FCFF + 左侧斜切色块 + 深藏青粗黑体字 + 可选底部指向尖角
    - 自适应紧贴文字尺寸（绝不创建全屏 RGBA 大窗，100% 保护 275Hz Direct Scanout 帧率）
    - 空 cairo.Region 实现 100% 鼠标点击穿透
    """

    ACCENT_PALETTES = SHARED_ACCENT_PALETTES

    def __init__(self, Gtk, Gdk, cairo, Pango, gdk_display):
        import gi
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import PangoCairo

        self.Gtk = Gtk
        self.Gdk = Gdk
        self.cairo = cairo
        self.Pango = Pango
        self.PangoCairo = PangoCairo
        self._gdk_display = gdk_display
        self.style_key = "best"
        self._left_markup = ""
        self._right_markup = ""
        self._with_tail = False
        self._font_pt = 13.5
        self._card_geom = (120, 32, 48, 20, 20, 0)
        self._render_sig = None
        self._visible = False
        self._closing = False

        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_title("LQSNBadgePill")
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_app_paintable(True)

        screen = self.win.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual and screen.is_composited():
            self.win.set_visual(visual)

        self.canvas = Gtk.DrawingArea()
        self.canvas.show()
        self.win.add(self.canvas)

        self.win.connect("draw", self._on_draw)
        self.win.connect("delete-event", lambda *_a: True)
        self.win.connect("realize", self._apply_click_through)
        self.win.connect("size-allocate", self._apply_click_through)
        self.Gtk.Widget.set_opacity(self.win, 0.0)
        self.win.move(-8192, -8192)

    def _apply_click_through(self, *_args):
        if self._closing:
            return
        gdk_win = self.win.get_window()
        if not gdk_win:
            return
        empty_reg = self.cairo.Region(self.cairo.RectangleInt(0, 0, 0, 0))
        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            gdk_win.input_shape_combine_region(empty_reg, 0, 0)
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()

    def _measure_and_update_size(self) -> tuple[int, int]:
        surf = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 8, 8)
        cr = self.cairo.Context(surf)
        weight_l = "Black" if self.style_key == "best" else "Bold"
        weight_r = "Black" if self.style_key == "best" else "Bold"

        lay_l = self.PangoCairo.create_layout(cr)
        lay_l.set_font_description(self.Pango.FontDescription(f"Noto Sans CJK SC {weight_l} {self._font_pt:.1f}"))
        lay_l.set_markup(self._left_markup, -1)
        _, log_l = lay_l.get_pixel_extents()

        has_right = bool(self._right_markup.strip())
        if has_right:
            lay_r = self.PangoCairo.create_layout(cr)
            lay_r.set_font_description(self.Pango.FontDescription(f"Noto Sans CJK SC {weight_r} {self._font_pt:.1f}"))
            lay_r.set_markup(self._right_markup, -1)
            _, log_r = lay_r.get_pixel_extents()
            rw, rh = log_r.width, log_r.height
        else:
            rw, rh = 0, 0

        lw, lh = log_l.width, log_l.height
        pad_l_x = max(10, round(self._font_pt * 0.82))
        slant = max(8, round(self._font_pt * 0.65)) if has_right else 0
        pad_r_x = max(11, round(self._font_pt * 0.92)) if has_right else pad_l_x
        pad_y = max(5, round(self._font_pt * 0.38))

        card_h = max(lh, rh) + pad_y * 2
        if has_right:
            left_w = int(lw + pad_l_x * 2 + slant * 0.5)
            card_w = int(left_w + rw + pad_r_x + 5)
        else:
            left_w = int(lw + pad_l_x * 2)
            card_w = left_w

        tail_h = 6 if self._with_tail else 0
        # 预留四周透明描边 + 2px 轻硬偏移阴影余量，确保圆角与底部尖角 100% 不被窗口裁剪
        win_w = max(40, card_w + 8)
        win_h = max(24, card_h + tail_h + 8)
        self._card_geom = (card_w, card_h, left_w, lh, rh, tail_h, pad_l_x, slant, has_right)
        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            self.canvas.set_size_request(win_w, win_h)
            self.win.resize(win_w, win_h)
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()
        return win_w, win_h

    def _on_draw(self, widget, cr):
        cr.set_operator(self.cairo.OPERATOR_CLEAR)
        cr.paint()
        if self._closing or not self._visible:
            return True
        cr.set_operator(self.cairo.OPERATOR_OVER)
        import math

        card_w, card_h, left_w, lh, rh, tail_h, pad_l_x, slant, has_right = self._card_geom
        x0, y0 = 2.5, 2.5
        rad = min(9.5, card_h / 2.0)
        tail_w = 12.0
        tail_x = x0 + card_w * 0.5

        def _card_path(ox: float, oy: float):
            r = min(rad, card_w / 2.0, card_h / 2.0)
            cr.new_sub_path()
            cr.arc(ox + card_w - r, oy + r, r, -math.pi / 2, 0)
            cr.arc(ox + card_w - r, oy + card_h - r, r, 0, math.pi / 2)
            if tail_h > 0:
                tx0 = max(ox + r + 3, min(ox + card_w - r - tail_w - 3, ox + card_w * 0.5 - tail_w / 2.0))
                tx1 = tx0 + tail_w
                cr.line_to(tx1, oy + card_h)
                cr.line_to((tx0 + tx1) * 0.5, oy + card_h + tail_h)
                cr.line_to(tx0, oy + card_h)
            cr.arc(ox + r, oy + card_h - r, r, math.pi / 2, math.pi)
            cr.arc(ox + r, oy + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        pal = self.ACCENT_PALETTES.get(self.style_key, self.ACCENT_PALETTES["second"])
        g_top, g_bot, border_rgba, _, _ = pal

        # 1. 轻巧 2px 硬偏移漫画贴纸阴影 (#142342)
        cr.save()
        _card_path(x0 + 2.0, y0 + 2.0)
        cr.set_source_rgba(*HUD_SHADOW_RGBA)
        cr.fill()
        cr.restore()

        # 2. 画纸奶白主体底板 (#F8FCFF)
        _card_path(x0, y0)
        cr.set_source_rgba(HUD_PAPER_RGB[0], HUD_PAPER_RGB[1], HUD_PAPER_RGB[2], 0.99)
        cr.fill_preserve()

        # 3. 左侧热血斜切渐变色块 + 冠军金/天蓝斜切饰条 + 右端极淡斜向速度线
        cr.save()
        cr.clip()
        cr.new_path()
        slant_top_x = x0 + left_w + slant * 0.5
        slant_bot_x = x0 + left_w - slant * 0.5
        if has_right:
            cr.move_to(x0 - 2, y0 - 2)
            cr.line_to(slant_top_x, y0 - 2)
            cr.line_to(slant_bot_x, y0 + card_h + tail_h + 2)
            cr.line_to(x0 - 2, y0 + card_h + tail_h + 2)
        else:
            cr.rectangle(x0 - 2, y0 - 2, card_w + 4, card_h + tail_h + 4)
        cr.close_path()
        grad = self.cairo.LinearGradient(0, y0, 0, y0 + card_h)
        grad.add_color_stop_rgb(0.0, *g_top)
        grad.add_color_stop_rgb(1.0, *g_bot)
        cr.set_source(grad)
        cr.fill()

        if has_right and self.style_key in ("best", "second"):
            trim_w = 3.2 if self.style_key == "best" else 2.0
            trim_rgb = HUD_CHAMPION_GOLD_RGB if self.style_key == "best" else HUD_SKY_BLUE_RGB
            cr.new_path()
            cr.move_to(slant_top_x - 0.5, y0 - 2)
            cr.line_to(slant_top_x + trim_w, y0 - 2)
            cr.line_to(slant_bot_x + trim_w, y0 + card_h + tail_h + 2)
            cr.line_to(slant_bot_x - 0.5, y0 + card_h + tail_h + 2)
            cr.close_path()
            cr.set_source_rgba(trim_rgb[0], trim_rgb[1], trim_rgb[2], 0.96)
            cr.fill()

        if has_right:
            slope = max(6.0, slant * 0.85)
            right_edge = x0 + card_w
            for offset_r, thick, alpha in ((5.5, 3.8, 0.11), (13.0, 2.4, 0.08), (19.5, 1.5, 0.055)):
                tx_s = right_edge - offset_r + slope * 0.35
                bx_s = right_edge - offset_r - slope * 0.65
                cr.new_path()
                cr.move_to(tx_s, y0 - 2)
                cr.line_to(tx_s + thick, y0 - 2)
                cr.line_to(bx_s + thick, y0 + card_h + 2)
                cr.line_to(bx_s, y0 + card_h + 2)
                cr.close_path()
                cr.set_source_rgba(g_top[0], g_top[1], g_top[2], alpha)
                cr.fill()
        cr.restore()

        # 4. 1.5px 深墨蓝细描边 (#142342)
        _card_path(x0, y0)
        cr.set_source_rgba(*border_rgba)
        cr.set_line_width(1.5)
        cr.set_line_join(self.cairo.LINE_JOIN_ROUND)
        cr.stroke()

        # 5. 左右分区粗黑体文字
        weight_c = "Black" if self.style_key == "best" else "Bold"
        fdesc = self.Pango.FontDescription(f"Noto Sans CJK SC {weight_c} {self._font_pt:.1f}")

        lay_l = self.PangoCairo.create_layout(cr)
        lay_l.set_font_description(fdesc)
        lay_l.set_markup(self._left_markup, -1)
        cr.move_to(x0 + pad_l_x, y0 + (card_h - lh) * 0.5 - 0.5)
        self.PangoCairo.show_layout(cr, lay_l)

        if has_right:
            lay_r = self.PangoCairo.create_layout(cr)
            lay_r.set_font_description(fdesc)
            lay_r.set_markup(self._right_markup, -1)
            extra_gap = 7 if self.style_key == "best" else 5
            cr.move_to(x0 + left_w + extra_gap, y0 + (card_h - rh) * 0.5 - 0.5)
            self.PangoCairo.show_layout(cr, lay_r)
        return True

    @classmethod
    def _split_legacy_markup(cls, markup: str, style_key: str) -> tuple[str, str]:
        return split_legacy_markup(markup, style_key)

    def show_at(
        self,
        markup: str = "",
        rect: tuple = None,
        rx: float = 0.5,
        ry: float = 0.5,
        anchor: str = "right",
        style_key: str = "best",
        dx_px: int = 0,
        dy_px: int = 0,
        left_markup: str = "",
        right_markup: str = "",
        with_tail: bool = False,
        font_scale: float = 1.0,
        left_of_box: tuple = None,
    ) -> Optional[tuple[int, int, int, int]]:
        if self._closing or not rect:
            return None
        content_rect = compute_game_content_box(rect) or rect
        gx, gy, gw, gh = content_rect
        lm, rm = resolve_badge_markups(
            left_markup=left_markup,
            right_markup=right_markup,
            markup=markup,
            style_key=style_key,
        )

        base_pt = 14.2 if style_key == "best" else 13.0
        scale = max(0.82, min(1.32, (gh / 1080.0) * font_scale))
        f_pt = round(base_pt * scale, 1)

        sig = (lm, rm, style_key, bool(with_tail), f_pt)
        need_redraw = sig != self._render_sig
        if need_redraw:
            self._render_sig = sig
            self.style_key = style_key
            self._left_markup = lm
            self._right_markup = rm
            self._with_tail = bool(with_tail)
            self._font_pt = f_pt
            pw, ph = self._measure_and_update_size()
        else:
            pw = max(40, self._card_geom[0] + 8)
            ph = max(24, self._card_geom[1] + self._card_geom[5] + 8)

        tx, ty, pw, ph = compute_badge_position(
            content_rect,
            pw=pw,
            ph=ph,
            rx=rx,
            ry=ry,
            anchor=anchor,
            dx_px=dx_px,
            dy_px=dy_px,
            left_of_box=left_of_box,
            min_top_y=30,
        )

        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            # 先移动到目标坐标再映射/显形，杜绝在 (-8192, -8192) 处触发 XMapWindow 导致 cosmic-comp 丢窗
            cur_x, cur_y = self.win.get_position()
            if abs(cur_x - tx) > 1 or abs(cur_y - ty) > 1 or need_redraw or not self._visible:
                self.win.move(tx, ty)
            if not self._visible or not self.win.get_visible():
                self.win.show_all()
            self._visible = True
            self.Gtk.Widget.set_opacity(self.win, 1.0)
            if need_redraw:
                self.canvas.queue_draw()
                self.win.queue_draw()
            gdk_win = self.win.get_window()
            if gdk_win:
                gdk_win.raise_()
            self._apply_click_through()
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()
        return (tx, ty, pw, ph)

    def hide_pill(self):
        if self._closing or not self._visible:
            return
        self._visible = False
        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            # 保持映射并透明移至屏外 + 置空点击区，避免高频 XUnmapWindow/XMapWindow 引发 cosmic-comp 丢窗
            self.Gtk.Widget.set_opacity(self.win, 0.0)
            self.win.move(-8192, -8192)
            self.win.queue_draw()
            self._apply_click_through()
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()

    def destroy_cleanly(self):
        if self._closing:
            return
        self._closing = True
        self._visible = False
        try:
            self.win.queue_draw()
            self.win.resize(1, 1)
            self.win.move(-4096, -4096)
            gdk_win = self.win.get_window()
            if gdk_win:
                gdk_win.hide()
            self.win.hide()
            self.win.destroy()
        except Exception:
            pass


class FloatingHUD:
    BADGE_POOL_SIZE = 8

    def __init__(self, strategy: dict, auto_play: bool, hud_config: Optional[dict] = None):
        os.environ["GDK_BACKEND"] = "x11"
        if "DISPLAY" not in os.environ:
            os.environ["DISPLAY"] = ":1"
        import cairo
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango

        self.cairo = cairo
        self.Gtk = Gtk
        self.Gdk = Gdk
        self.GdkPixbuf = GdkPixbuf
        self.GLib = GLib
        self.Pango = Pango
        from collections import OrderedDict as _OrderedDict
        self._pixbuf_cache = {}
        self._q_avatar_pixbuf_cache = _OrderedDict()
        self._active_char_id = None
        self.equip_icons_data = []
        self.strategy = strategy
        self.auto_play = auto_play
        self.hud_config = hud_config if hud_config is not None else load_hud_config()
        self.anchors = load_ui_anchor_coords()

        # 默认开启「原生按钮贴身角标 + 底部深海蓝槽迷你防忘条」模式；右键点击左侧 🏀 小把手可一键热切换经典卡片模式
        self.diegetic_mode = True
        self._scene_hidden = False
        self._active_view_mode = "ycjm01"

        # 缓存最近一次各场景决策结果，确保在 ycjm01 <-> ycjm02 / 商店 / 剧情 / 模式热切换时 0 延迟重绘角标
        self._last_state = None
        self._last_turn_scored = None
        self._last_turn_extra_tip = ""
        self._last_story_adv = None
        self._last_wk_adv = None
        self._last_skill_evals = None
        self._last_note_adv = None
        self._last_note_scan_badge = ""
        self._last_note_scan_status = ""

        # 主窗口：POPUP (override_redirect=True)，在角标模式下作为底部深海蓝槽迷你条，经典模式下作为右上角紧凑卡片
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.win.set_title("🏀 星奈育成军师 HUD")
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_app_paintable(True)

        screen = self.win.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual and screen.is_composited():
            self.win.set_visual(visual)

        self.win.connect("draw", self._on_draw_bg)
        self.win.connect("delete-event", lambda *_a: True)

        self.collapsed = False
        self.full_markup = ""
        self.tip_markup = ""
        self.trough_markup = "待命｜进入选人或育成自动贴标"
        self.mini_markup = "<span foreground='#FFB703' weight='bold'>★ 星奈待命</span>"
        self.custom_offset = None          # 经典卡片模式相对游戏左上角偏移
        self.trough_custom_offset = None   # 底部深海蓝槽模式相对游戏左上角偏移
        self.manual_pos = None
        self.drag_active = False
        self.drag_start_root = (0, 0)
        self.drag_start_win = (0, 0)
        self.last_game_rect = None
        self.on_auto_scan_cb = None
        self.on_focus_changed_cb = None
        self._prev_btn1 = False
        self._prev_btn2 = False
        self._prev_btn3 = False
        self._last_btn1_press_ts = 0.0
        self._last_btn1_press_pos = (0, 0)
        self._last_toggle_ts = 0.0
        self._last_scan_ts = 0.0
        self._last_focus_toggle_ts = 0.0
        self._closing = False
        self._last_rect_query_ts = 0.0
        self._gdk_display = self.win.get_display()
        # 常驻开启根级 GDK X11 异步错误陷阱（深度始终 >= 1），防止 cosmic-comp Xwayland 在高频 POPUP 显隐/改尺寸时
        # 偶发异步 BadWindow/BadDrawable 落入 GTK 内部帧循环触发 C 层 _exit(1) 导致 HUD 莫名静默退出
        self._root_x11_trap_active = False
        if self._gdk_display:
            self._gdk_display.error_trap_push()
            self._root_x11_trap_active = True
        self._gdk_root = screen.get_root_window() if screen else None
        seat = self._gdk_display.get_default_seat() if self._gdk_display else None
        self._gdk_pointer = seat.get_pointer() if seat else None

        # 预分配 8 枚点击穿透微窗池
        self.badge_pool = [
            BadgePillWindow(Gtk, Gdk, cairo, Pango, self._gdk_display)
            for _ in range(self.BADGE_POOL_SIZE)
        ]
        self._active_badge_specs = []

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        drag_mask = (
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
        )
        self.header_ev = Gtk.EventBox()
        self.header_ev.set_visible_window(False)
        self.header_ev.set_size_request(-1, 26)
        self.header_ev.add_events(drag_mask)
        self.header_ev.connect("button-press-event", self._on_header_press)
        self.header_ev.connect("button-release-event", self._on_header_release)
        self.header_ev.connect("motion-notify-event", self._on_header_motion)

        header_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_hbox.set_margin_start(6)
        header_hbox.set_margin_end(8)
        header_hbox.set_margin_top(4)
        header_hbox.set_margin_bottom(4)

        self.header_avatar = Gtk.Image()
        self.header_avatar.set_no_show_all(True)
        header_hbox.pack_start(self.header_avatar, False, False, 0)

        self.slot_tag_label = Gtk.Label()
        self.slot_tag_label.set_no_show_all(True)
        self.slot_tag_label.set_xalign(0.5)
        self.slot_tag_label.set_yalign(0.5)
        self.slot_tag_label.set_margin_start(9)
        self.slot_tag_label.set_margin_end(9)
        self.slot_tag_label.set_margin_top(2)
        self.slot_tag_label.set_margin_bottom(2)
        header_hbox.pack_start(self.slot_tag_label, False, False, 0)

        self.header_label = Gtk.Label()
        self.header_label.set_xalign(0.0)
        self.header_label.set_yalign(0.5)
        header_hbox.pack_start(self.header_label, True, True, 0)

        self.focus_btn_label = Gtk.Label()
        self.focus_btn_label.set_no_show_all(True)
        self.focus_btn_label.set_xalign(0.5)
        self.focus_btn_label.set_yalign(0.5)
        self.focus_btn_label.set_margin_start(8)
        self.focus_btn_label.set_margin_end(8)
        self.focus_btn_label.set_margin_top(2)
        self.focus_btn_label.set_margin_bottom(2)
        header_hbox.pack_end(self.focus_btn_label, False, False, 0)

        self.header_ev.add(header_hbox)
        vbox.pack_start(self.header_ev, False, False, 0)

        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.rows_box.set_margin_start(12)
        self.rows_box.set_margin_end(14)
        self.rows_box.set_margin_top(5)
        self.rows_box.set_margin_bottom(6)
        vbox.pack_start(self.rows_box, False, False, 0)

        self.equip_strip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.equip_strip_box.set_no_show_all(True)
        self.equip_strip_box.set_margin_start(12)
        self.equip_strip_box.set_margin_end(14)
        self.equip_strip_box.set_margin_top(0)
        self.equip_strip_box.set_margin_bottom(6)
        vbox.pack_start(self.equip_strip_box, False, False, 0)

        self.tip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.tip_box.set_no_show_all(True)
        self.tip_box.set_margin_start(12)
        self.tip_box.set_margin_end(14)
        self.tip_box.set_margin_top(4)
        self.tip_box.set_margin_bottom(6)
        self.tip_label = Gtk.Label()
        self.tip_label.set_xalign(0.0)
        self.tip_label.set_yalign(0.5)
        self.tip_label.set_line_wrap(True)
        self.tip_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.tip_label.set_max_width_chars(56)
        self.tip_box.pack_start(self.tip_label, False, False, 0)
        vbox.pack_start(self.tip_box, False, False, 0)

        self.win.add(vbox)
        self.win.connect("size-allocate", self._on_size_allocate)

        self.slot_tag_text = "星奈"
        mode_str = "🤖自动代打" if auto_play else "实时军师"
        self._update_header(
            f"<span weight='bold' foreground='#fbbf24'>🏀 星奈 · {mode_str}</span>  "
            f"<span size='x-small' foreground='#7dd3fc'>右键切原生贴标</span>"
        )
        init_body = "<span foreground='#cbd5e1'>⏳ 监听中，进入选人或育成自动贴标</span>"
        init_tip = "<span foreground='#94a3b8'>右键左侧标签热切『原生贴身角标 / 经典卡片』</span>"
        self.set_markup(
            init_body,
            mini_summary="🏀 星奈待命",
            tip_markup=init_tip,
            trough_summary="待命｜进入选人或育成自动贴标",
        )

        self.win.move(1280, 72)
        self.win.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration()
        self.sync_position_and_raise(force=True)
        while Gtk.events_pending():
            Gtk.main_iteration()
        GLib.timeout_add(400, self.sync_position_and_raise)
        if self._gdk_display:
            GLib.timeout_add_seconds(30, self._rotate_root_x11_trap)
        if self._gdk_root and self._gdk_pointer:
            GLib.timeout_add(25, self._poll_x11_pointer)

    def _rotate_root_x11_trap(self):
        """无缝轮换根级 X11 错误陷阱（先 push 新陷阱再 pop_ignored 旧陷阱，确保陷阱深度任何瞬间绝不归零）。"""
        if getattr(self, "_closing", False) or not self._gdk_display:
            return False
        try:
            self._gdk_display.error_trap_push()
            if self._root_x11_trap_active:
                self._gdk_display.error_trap_pop_ignored()
            self._root_x11_trap_active = True
        except Exception:
            pass
        return True

    def hide_all_badges(self):
        self._active_badge_specs = []
        for pill in self.badge_pool:
            pill.hide_pill()

    def _apply_badge_specs(self, specs: list):
        """将计算出的风格B角标规格投射到微窗复用池（支持 left_of_prev 自动无重叠并排）。"""
        self._active_badge_specs = list(specs or [])
        if getattr(self, "_closing", False) or not self.diegetic_mode or self._scene_hidden:
            for pill in self.badge_pool:
                pill.hide_pill()
            return
        rect = self.last_game_rect or self._query_game_rect()
        if not rect:
            for pill in self.badge_pool:
                pill.hide_pill()
            return
        self.last_game_rect = rect
        prev_box = None
        for idx, pill in enumerate(self.badge_pool):
            if idx < len(self._active_badge_specs):
                sp = self._active_badge_specs[idx]
                prev_box = pill.show_at(
                    markup=sp.get("markup", ""),
                    rect=rect,
                    rx=sp.get("rx", 0.5),
                    ry=sp.get("ry", 0.5),
                    anchor=sp.get("anchor", "right"),
                    style_key=sp.get("style", "best"),
                    dx_px=sp.get("dx", 0),
                    dy_px=sp.get("dy", 0),
                    left_markup=sp.get("left", ""),
                    right_markup=sp.get("right", ""),
                    with_tail=bool(sp.get("tail", False)),
                    font_scale=float(sp.get("font_scale", 1.0)),
                    left_of_box=(prev_box if sp.get("left_of_prev") else None),
                )
            else:
                pill.hide_pill()

    def _uses_bottom_trough(self) -> bool:
        """选技能与开局选人界面无顶部白栏、且底部深海蓝栏留空，自动落入底部槽；其余主界面/训练/周末均嵌于顶栏第4原生槽。"""
        return self._active_view_mode in ("initactor", "skill_choice")

    def _on_draw_bg(self, widget, cr):
        if getattr(self, "_closing", False) or self._scene_hidden:
            cr.set_operator(self.cairo.OPERATOR_CLEAR)
            cr.paint()
            return True
        import math

        cr.set_operator(self.cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(self.cairo.OPERATOR_OVER)

        alloc = widget.get_allocation()
        w, h = max(4, alloc.width), max(4, alloc.height)
        is_initactor_card = self.diegetic_mode and (self._active_view_mode == "initactor") and bool(
            self.equip_icons_data or self.full_markup
        )
        is_native_slot = self.diegetic_mode and not is_initactor_card

        def _rounded_rect_path(x0, y0, rw, rh, rad):
            rad = min(rad, rw / 2.0, rh / 2.0)
            cr.new_sub_path()
            cr.arc(x0 + rw - rad, y0 + rad, rad, -math.pi / 2, 0)
            cr.arc(x0 + rw - rad, y0 + rh - rad, rad, 0, math.pi / 2)
            cr.arc(x0 + rad, y0 + rh - rad, rad, math.pi / 2, math.pi)
            cr.arc(x0 + rad, y0 + rad, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()

        if is_native_slot:
            # 顶栏漫画贴纸胶囊（与 Windows Pillow 同源：奶白底 #F8FCFF + 1.5px #142342 描边 + 2px 硬阴影 + 速度线）
            pw = max(4.0, w - 4.0)
            ph = max(4.0, h - 4.0)
            r = min(12.0, ph / 2.0)

            # 1. 轻巧 2px 硬偏移阴影
            _rounded_rect_path(3.0, 3.0, pw, ph, r)
            cr.set_source_rgba(*HUD_SHADOW_RGBA)
            cr.fill()

            # 2. 画纸奶白主体底板 (#F8FCFF / 底槽 #F5F8FF)
            _rounded_rect_path(1.0, 1.0, pw, ph, r)
            if self._uses_bottom_trough():
                cr.set_source_rgba(245 / 255.0, 248 / 255.0, 255 / 255.0, 0.98)
            else:
                cr.set_source_rgba(HUD_PAPER_RGB[0], HUD_PAPER_RGB[1], HUD_PAPER_RGB[2], 0.99)
            cr.fill_preserve()

            # 3. 右端极淡斜向漫画速度线
            cr.save()
            cr.clip()
            slope = 7.0
            right_edge = 1.0 + pw
            for offset_r, thick, alpha in ((5.5, 3.6, 0.10), (13.0, 2.2, 0.075), (19.5, 1.4, 0.05)):
                tx_s = right_edge - offset_r + slope * 0.35
                bx_s = right_edge - offset_r - slope * 0.65
                cr.new_path()
                cr.move_to(tx_s, 0.0)
                cr.line_to(tx_s + thick, 0.0)
                cr.line_to(bx_s + thick, ph + 2.0)
                cr.line_to(bx_s, ph + 2.0)
                cr.close_path()
                cr.set_source_rgba(27 / 255.0, 91 / 255.0, 255 / 255.0, alpha)
                cr.fill()
            cr.restore()

            # 4. 1.5px 深墨蓝细描边 (#142342)
            _rounded_rect_path(1.0, 1.0, pw, ph, r)
            cr.set_source_rgba(*HUD_INK_STROKE_RGBA)
            cr.set_line_width(1.5)
            cr.set_line_join(self.cairo.LINE_JOIN_ROUND)
            cr.stroke()

            if self.slot_tag_label.get_visible():
                ta = self.slot_tag_label.get_allocation()
                tx = max(4.0, ta.x - 7.0)
                ty = 4.5
                tw = max(36.0, ta.width + 15.0)
                th = max(16.0, ph - 7.0)
                slant_t = 6.0
                cr.save()
                cr.new_path()
                t_r = min(6.5, th / 2.0)
                cr.arc(tx + t_r, ty + t_r, t_r, math.pi, 3 * math.pi / 2)
                cr.line_to(tx + tw, ty)
                cr.line_to(tx + tw - slant_t, ty + th)
                cr.arc(tx + t_r, ty + th - t_r, t_r, math.pi / 2, math.pi)
                cr.close_path()
                grad_t = self.cairo.LinearGradient(0, ty, 0, ty + th)
                grad_t.add_color_stop_rgb(0.0, 27 / 255.0, 91 / 255.0, 255 / 255.0)
                grad_t.add_color_stop_rgb(1.0, 17 / 255.0, 57 / 255.0, 166 / 255.0)
                cr.set_source(grad_t)
                cr.fill()
                cr.restore()
            if self.focus_btn_label.get_visible():
                fa = self.focus_btn_label.get_allocation()
                if fa.width > 6:
                    fc = _normalize_focus_dict((self.hud_config or {}).get("focus"))
                    is_on = fc["stat"] is not None
                    fx = max(4.0, fa.x - 6.0)
                    fy = 4.5
                    fw = min(pw - fx - 3.0, max(32.0, fa.width + 12.0))
                    fh = max(16.0, ph - 7.0)
                    _rounded_rect_path(fx, fy, fw, fh, min(7.5, fh / 2.0))
                    if is_on:
                        grad_f = self.cairo.LinearGradient(0, fy, 0, fy + fh)
                        grad_f.add_color_stop_rgb(0.0, 255 / 255.0, 81 / 255.0, 38 / 255.0)
                        grad_f.add_color_stop_rgb(1.0, 224 / 255.0, 58 / 255.0, 18 / 255.0)
                        cr.set_source(grad_f)
                        cr.fill_preserve()
                        cr.set_source_rgba(*HUD_INK_STROKE_RGBA)
                        cr.set_line_width(1.1)
                        cr.stroke()
                    else:
                        cr.set_source_rgba(234 / 255.0, 241 / 255.0, 255 / 255.0, 0.92)
                        cr.fill_preserve()
                        cr.set_source_rgba(20 / 255.0, 35 / 255.0, 66 / 255.0, 0.45)
                        cr.set_line_width(1.0)
                        cr.stroke()
            return False

        # 经典紧凑悬浮卡片模式 & 选人编队多行展开面板
        r = 10.5
        _rounded_rect_path(0.6, 0.6, max(2, w - 1.2), max(2, h - 1.2), r)
        grad = self.cairo.LinearGradient(0, 0, 0, h)
        grad.add_color_stop_rgba(0.0, 26 / 255.0, 46 / 255.0, 74 / 255.0, 0.92)
        grad.add_color_stop_rgba(1.0, 34 / 255.0, 58 / 255.0, 92 / 255.0, 0.94)
        cr.set_source(grad)
        cr.fill_preserve()

        cr.save()
        cr.clip()
        if (not self.collapsed or is_initactor_card) and h > 30:
            h_alloc = self.header_ev.get_allocation()
            hdr_h = max(26, h_alloc.height if h_alloc.height > 1 else 26)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.055)
            cr.rectangle(0, 0, w, hdr_h)
            cr.fill()

            cr.set_source_rgba(125 / 255.0, 211 / 255.0, 252 / 255.0, 0.18)
            cr.set_line_width(1.0)
            cr.move_to(0, hdr_h + 0.5)
            cr.line_to(w, hdr_h + 0.5)
            cr.stroke()

            if self.tip_box.get_visible():
                t_alloc = self.tip_box.get_allocation()
                if t_alloc.y > hdr_h + 4:
                    div_y = t_alloc.y - 4
                    cr.set_source_rgba(56 / 255.0, 189 / 255.0, 248 / 255.0, 0.075)
                    cr.rectangle(0, div_y, w, h - div_y)
                    cr.fill()
                    cr.set_source_rgba(125 / 255.0, 211 / 255.0, 252 / 255.0, 0.18)
                    cr.set_line_width(1.0)
                    cr.move_to(10, div_y + 0.5)
                    cr.line_to(w - 10, div_y + 0.5)
                    cr.stroke()
        cr.restore()

        _rounded_rect_path(0.6, 0.6, max(2, w - 1.2), max(2, h - 1.2), r)
        cr.set_source_rgba(125 / 255.0, 211 / 255.0, 252 / 255.0, 0.46)
        cr.set_line_width(1.15)
        cr.stroke()

        if self.focus_btn_label.get_visible():
            fa = self.focus_btn_label.get_allocation()
            if fa.width > 6:
                fc = _normalize_focus_dict((self.hud_config or {}).get("focus"))
                is_on = fc["stat"] is not None
                fx = max(4.0, fa.x - 5.0)
                fy = max(3.0, fa.y - 2.0)
                fw = min(w - fx - 5.0, max(30.0, fa.width + 10.0))
                fh = max(16.0, fa.height + 4.0)
                _rounded_rect_path(fx, fy, fw, fh, fh / 2.0)
                if is_on:
                    grad_f = self.cairo.LinearGradient(0, fy, 0, fy + fh)
                    grad_f.add_color_stop_rgb(0.0, 245 / 255.0, 158 / 255.0, 11 / 255.0)
                    grad_f.add_color_stop_rgb(1.0, 217 / 255.0, 119 / 255.0, 6 / 255.0)
                    cr.set_source(grad_f)
                    cr.fill()
                else:
                    cr.set_source_rgba(148 / 255.0, 163 / 255.0, 184 / 255.0, 0.16)
                    cr.fill_preserve()
                    cr.set_source_rgba(148 / 255.0, 163 / 255.0, 184 / 255.0, 0.36)
                    cr.set_line_width(0.9)
                    cr.stroke()
        return False

    def _get_diegetic_handle_width(self, alloc) -> int:
        if self.slot_tag_label.get_visible():
            ta = self.slot_tag_label.get_allocation()
            if ta.width > 8:
                return min(alloc.width, max(54, ta.x + ta.width + 10))
        return min(68, max(36, alloc.width))

    def _get_focus_btn_rect(self, alloc) -> Optional[tuple[int, int, int, int]]:
        """返回右侧专项偏科小开关在主窗体内的可点击矩形 (x, y, w, h)。"""
        if not self.focus_btn_label.get_visible():
            return None
        fa = self.focus_btn_label.get_allocation()
        if fa.width <= 4 or fa.height <= 4:
            return None
        fx = max(0, int(fa.x) - 7)
        fy = 0 if self.diegetic_mode else max(0, int(fa.y) - 3)
        fw = min(max(1, alloc.width - fx), int(fa.width) + 14)
        fh = alloc.height if self.diegetic_mode else min(max(1, alloc.height - fy), int(fa.height) + 6)
        return (fx, fy, fw, fh)

    def _on_size_allocate(self, widget, alloc):
        """
        动态裁剪鼠标可点击输入区域：
        - 原生贴身角标模式 (diegetic_mode=True)：左侧天蓝小胶囊标签 + 右侧🎯特化小开关接收鼠标事件，中间文字 100% 穿透！
        - 经典紧凑卡片模式 (diegetic_mode=False)：仅顶部标题栏（含右侧🎯小开关）可点击/拖拽，正文区 100% 穿透！
        """
        if getattr(self, "_closing", False):
            return
        gdk_win = self.win.get_window()
        if not gdk_win:
            return
        if self._scene_hidden:
            reg = self.cairo.Region(self.cairo.RectangleInt(0, 0, 0, 0))
        elif self.diegetic_mode:
            handle_w = self._get_diegetic_handle_width(alloc)
            reg = self.cairo.Region(self.cairo.RectangleInt(0, 0, handle_w, alloc.height))
            f_rect = self._get_focus_btn_rect(alloc)
            if f_rect:
                fx, fy, fw, fh = f_rect
                reg.union(self.cairo.RectangleInt(fx, fy, fw, fh))
        else:
            h_alloc = self.header_ev.get_allocation()
            raw_hdr_h = max(30, h_alloc.height if h_alloc.height > 1 else 30)
            header_h = alloc.height if self.collapsed else min(raw_hdr_h, alloc.height)
            reg = self.cairo.Region(self.cairo.RectangleInt(0, 0, alloc.width, header_h))
        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            gdk_win.input_shape_combine_region(reg, 0, 0)
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()

    def close_cleanly(self, quit_main_loop: bool = True):
        """
        优雅卸载主窗口与全部 8 枚卫星徽章微窗：
        显式执行透明重绘 + move(-4096,-4096) + XUnmapWindow(gdk_win.hide) + GDK flush/sync，
        确保 275Hz cosmic-comp 合成器图层 100% 零残影卸载。
        """
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            if self._gdk_display:
                self._gdk_display.error_trap_push()
            for pill in getattr(self, "badge_pool", ()):
                pill.destroy_cleanly()
            self.win.queue_draw()
            self.win.resize(1, 1)
            self.win.move(-4096, -4096)
            while self.Gtk.events_pending():
                self.Gtk.main_iteration_do(False)
            gdk_win = self.win.get_window()
            if gdk_win:
                gdk_win.hide()
            self.win.hide()
            if self._gdk_display:
                self._gdk_display.flush()
                self._gdk_display.sync()
            time.sleep(0.06)
            while self.Gtk.events_pending():
                self.Gtk.main_iteration_do(False)
            self.win.destroy()
            if self._gdk_display:
                self._gdk_display.flush()
                self._gdk_display.sync()
                self._gdk_display.error_trap_pop_ignored()
                if getattr(self, "_root_x11_trap_active", False):
                    self._root_x11_trap_active = False
                    self._gdk_display.error_trap_pop_ignored()
        except Exception:
            pass
        if quit_main_loop:
            try:
                if self.Gtk.main_level() > 0:
                    self.Gtk.main_quit()
            except Exception:
                pass

    def toggle_diegetic_mode_debounced(self):
        """右键点击 🏀 小把手：在『原生贴身角标 + 底部槽迷你条』与『经典紧凑悬浮卡片』间一键热切。"""
        now = time.time()
        if now - self._last_toggle_ts < 0.25:
            return
        self._last_toggle_ts = now
        self.diegetic_mode = not self.diegetic_mode
        if not self.diegetic_mode:
            self.collapsed = False
            self.hide_all_badges()
        if self._active_view_mode == "initactor" and self._last_note_adv:
            self.render_note_selection(self._last_note_adv, self._last_note_scan_badge, self._last_note_scan_status)
        elif self._last_state is not None:
            self.refresh_scene_overlay(self._last_state)
        else:
            self._apply_current_markup()

    def _toggle_collapse_debounced(self):
        now = time.time()
        if now - self._last_toggle_ts < 0.25:
            return
        self._last_toggle_ts = now
        self.collapsed = not self.collapsed
        self._apply_current_markup()

    def _trigger_scan_debounced(self):
        now = time.time()
        if now - self._last_scan_ts < 1.0:
            return
        self._last_scan_ts = now
        if self.on_auto_scan_cb:
            self.on_auto_scan_cb()

    def _toggle_focus_debounced(self, cycle: bool = False):
        """点击 HUD 顶栏右侧 🎯 小开关：左键一键开/关特化（默认投射），右键六维循环切换，实时落盘并 0 延迟重算角标。"""
        now = time.time()
        if now - self._last_focus_toggle_ts < 0.28:
            return
        self._last_focus_toggle_ts = now
        cfg = load_hud_config()
        fc = _normalize_focus_dict(cfg.get("focus"))
        if cycle:
            cur_st = fc["stat"]
            if cur_st is None:
                fc["stat"] = fc["last_stat"] or DEFAULT_FOCUS_STAT
            elif cur_st >= 106:
                fc["stat"] = None
            else:
                fc["stat"] = cur_st + 1
            if fc["stat"] is not None:
                fc["last_stat"] = fc["stat"]
        else:
            if fc["stat"] is not None:
                fc["stat"] = None
            else:
                fc["stat"] = fc["last_stat"] or DEFAULT_FOCUS_STAT
        cfg["focus"] = fc
        saved = save_hud_config(cfg)
        self.hud_config = saved
        if fc["stat"] is not None:
            st_nm = STAT_NAMES.get(fc["stat"], str(fc["stat"]))
            print(f"\n🎯 [HUD小开关] 已开启【{st_nm}特化】 (倍率 ×{fc['bonus']:.2f})，正在重算当前界面推荐...")
        else:
            print("\n⚪ [HUD小开关] 已关闭专项特化，切回纯评分模式，正在重算当前界面推荐...")
        if self.on_focus_changed_cb:
            self.on_focus_changed_cb(saved)
        else:
            self.set_hud_config(saved, state=self._last_state)

    def _update_focus_btn_label(self):
        """刷新顶栏右侧 🎯 专项特化小开关文案与配色。"""
        fc = _normalize_focus_dict((self.hud_config or {}).get("focus"))
        is_on = fc["stat"] is not None
        st_id = fc["stat"] if is_on else (fc["last_stat"] or DEFAULT_FOCUS_STAT)
        short_1 = SHORT_STAT_NAMES.get(st_id, "投")
        full_2 = STAT_NAMES.get(st_id, "投射")
        is_initactor = self.diegetic_mode and (self._active_view_mode == "initactor") and bool(
            self.equip_icons_data or self.full_markup
        )
        is_native_slot = self.diegetic_mode and not is_initactor
        if is_native_slot:
            gh = self.last_game_rect[3] if self.last_game_rect else 1080
            scale = max(0.84, min(1.30, gh / 1080.0))
            pt_f = round(10.8 * scale, 1)
            if is_on:
                mk = f"<span font_desc='Noto Sans CJK SC Black {pt_f}' foreground='#FFFFFF'>◆ {html.escape(full_2)}</span>"
            else:
                mk = f"<span font_desc='Noto Sans CJK SC Bold {pt_f}' foreground='#435370'>◆ {html.escape(short_1)}</span>"
        else:
            if is_on:
                mk = f"<span size='x-small' weight='bold' foreground='#FFFFFF'>◆ {html.escape(full_2)}</span>"
            else:
                mk = f"<span size='x-small' foreground='#94A3B8'>◆ {html.escape(short_1)}关</span>"
        self.focus_btn_label.set_markup(mk)
        self.focus_btn_label.show()

    def _poll_x11_pointer(self):
        """25ms GDK X11 根窗口指针轮询：支持全屏游戏下 🏀 把手拖拽、右侧🎯小开关热切、左键双击扫笔记、右键热切角标/卡片模式。"""
        if getattr(self, "_closing", False) or not self._gdk_root or not self._gdk_pointer:
            return False
        if self._scene_hidden:
            return True
        try:
            if self._gdk_display:
                self._gdk_display.error_trap_push()
            try:
                _, px, py, mask = self._gdk_root.get_device_position(self._gdk_pointer)
                mval = int(mask)
                btn1 = bool(mval & int(self.Gdk.ModifierType.BUTTON1_MASK))
                btn2 = bool(mval & int(self.Gdk.ModifierType.BUTTON2_MASK))
                btn3 = bool(mval & int(self.Gdk.ModifierType.BUTTON3_MASK))

                wx, wy = self.win.get_position()
                alloc = self.win.get_allocation()
                if self.diegetic_mode:
                    hit_w = self._get_diegetic_handle_width(alloc)
                    hit_h = alloc.height
                else:
                    h_alloc = self.header_ev.get_allocation()
                    hit_w = alloc.width
                    hit_h = alloc.height if self.collapsed else max(30, h_alloc.height if h_alloc.height > 1 else 30)

                f_rect = self._get_focus_btn_rect(alloc)
                in_focus_btn = bool(
                    f_rect
                    and (wx + f_rect[0] - 2 <= px <= wx + f_rect[0] + f_rect[2] + 2)
                    and (wy + f_rect[1] - 2 <= py <= wy + f_rect[1] + f_rect[3] + 2)
                )
                in_interactive = (not in_focus_btn) and (wx - 3 <= px <= wx + hit_w + 3) and (wy - 3 <= py <= wy + hit_h + 3)
                in_left_ball = (not in_focus_btn) and (wx - 3 <= px <= wx + max(56, hit_w)) and (wy - 3 <= py <= wy + hit_h + 3)

                if btn1 and not self._prev_btn1:
                    if in_focus_btn:
                        self.drag_active = False
                        self._toggle_focus_debounced(cycle=False)
                    elif in_interactive:
                        now = time.time()
                        if (
                            now - self._last_btn1_press_ts <= 0.40
                            and abs(px - self._last_btn1_press_pos[0]) <= 8
                            and abs(py - self._last_btn1_press_pos[1]) <= 8
                        ):
                            self.drag_active = False
                            self._last_btn1_press_ts = 0.0
                            self._trigger_scan_debounced()
                        else:
                            self._last_btn1_press_ts = now
                            self._last_btn1_press_pos = (px, py)
                            self.drag_active = True
                            self.drag_start_root = (px, py)
                            self.drag_start_win = (wx, wy)
                elif btn1 and self.drag_active:
                    dx = px - self.drag_start_root[0]
                    dy = py - self.drag_start_root[1]
                    nx = max(0, self.drag_start_win[0] + dx)
                    ny = max(0, self.drag_start_win[1] + dy)
                    if nx != wx or ny != wy:
                        self.win.move(nx, ny)
                        self.manual_pos = (nx, ny)
                        if self.last_game_rect:
                            gx, gy, _, _ = self.last_game_rect
                            if self.diegetic_mode:
                                if self._uses_bottom_trough():
                                    self.trough_custom_offset = (nx - gx, ny - gy)
                                else:
                                    self._top_slot_custom_offset = (nx - gx, ny - gy)
                            else:
                                self.custom_offset = (nx - gx, ny - gy)
                elif not btn1 and self.drag_active:
                    self.drag_active = False
                    wx, wy = self.win.get_position()
                    self.manual_pos = (wx, wy)
                    if self.last_game_rect:
                        gx, gy, _, _ = self.last_game_rect
                        if self.diegetic_mode:
                            if self._uses_bottom_trough():
                                self.trough_custom_offset = (wx - gx, wy - gy)
                            else:
                                self._top_slot_custom_offset = (wx - gx, wy - gy)
                        else:
                            self.custom_offset = (wx - gx, wy - gy)
            finally:
                if self._gdk_display:
                    self._gdk_display.error_trap_pop_ignored()

            if btn3 and not self._prev_btn3:
                if in_focus_btn:
                    self._toggle_focus_debounced(cycle=True)
                elif in_interactive:
                    if self.diegetic_mode or in_left_ball:
                        self.toggle_diegetic_mode_debounced()
                    else:
                        self._toggle_collapse_debounced()

            if btn2 and not self._prev_btn2 and in_interactive:
                self._trigger_scan_debounced()

            self._prev_btn1 = btn1
            self._prev_btn2 = btn2
            self._prev_btn3 = btn3
        except Exception:
            pass
        return True

    def _on_header_press(self, widget, event):
        alloc = self.win.get_allocation()
        f_rect = self._get_focus_btn_rect(alloc)
        in_focus = bool(
            f_rect
            and (f_rect[0] - 2 <= event.x <= f_rect[0] + f_rect[2] + 2)
            and (f_rect[1] - 2 <= event.y <= f_rect[1] + f_rect[3] + 2)
        )
        if in_focus:
            if event.button == 1:
                self._toggle_focus_debounced(cycle=False)
                return True
            elif event.button == 3:
                self._toggle_focus_debounced(cycle=True)
                return True
        if event.button == 2 or (event.button == 1 and event.type == self.Gdk.EventType._2BUTTON_PRESS):
            self.drag_active = False
            self._trigger_scan_debounced()
            return True
        if event.button == 1:
            self.drag_active = True
            self.drag_start_root = (int(event.x_root), int(event.y_root))
            wx, wy = self.win.get_position()
            self.drag_start_win = (wx, wy)
            return True
        elif event.button == 3:
            if self.diegetic_mode or event.x <= 68:
                self.toggle_diegetic_mode_debounced()
            else:
                self._toggle_collapse_debounced()
            return True
        return False

    def _on_header_release(self, widget, event):
        if event.button == 1 and self.drag_active:
            self.drag_active = False
            wx, wy = self.win.get_position()
            self.manual_pos = (wx, wy)
            if self.last_game_rect:
                gx, gy, _, _ = self.last_game_rect
                if self.diegetic_mode:
                    if self._uses_bottom_trough():
                        self.trough_custom_offset = (wx - gx, wy - gy)
                    else:
                        self._top_slot_custom_offset = (wx - gx, wy - gy)
                else:
                    self.custom_offset = (wx - gx, wy - gy)
            return True
        return False

    def _on_header_motion(self, widget, event):
        if self.drag_active:
            dx = int(event.x_root) - self.drag_start_root[0]
            dy = int(event.y_root) - self.drag_start_root[1]
            nx = max(0, self.drag_start_win[0] + dx)
            ny = max(0, self.drag_start_win[1] + dy)
            self.win.move(nx, ny)
            self.manual_pos = (nx, ny)
            if self.last_game_rect:
                gx, gy, _, _ = self.last_game_rect
                if self.diegetic_mode:
                    if self._uses_bottom_trough():
                        self.trough_custom_offset = (nx - gx, ny - gy)
                    else:
                        self._top_slot_custom_offset = (nx - gx, ny - gy)
                else:
                    self.custom_offset = (nx - gx, ny - gy)
            return True
        return False

    def _query_game_rect(self):
        try:
            out = subprocess.check_output(
                ["xwininfo", "-name", "篮球少女：飞跃巅峰"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.35,
            )
            if "IsViewable" not in out:
                return None
            mx = re.search(r"Absolute upper-left X:\s+(-?\d+)", out)
            my = re.search(r"Absolute upper-left Y:\s+(-?\d+)", out)
            mw = re.search(r"Width:\s+(\d+)", out)
            mh = re.search(r"Height:\s+(\d+)", out)
            if mx and my and mw and mh:
                return (int(mx.group(1)), int(my.group(1)), int(mw.group(1)), int(mh.group(1)))
        except subprocess.TimeoutExpired:
            return self.last_game_rect
        except Exception:
            pass
        return self.last_game_rect

    def sync_position_and_raise(self, force=False):
        if getattr(self, "_closing", False):
            return False
        try:
            # 400ms 守护自愈：若底层 state 真实视图模式与当前 HUD 缓存模式/显隐失步（如跨行批次切换后），立即自动重刷对齐
            if (
                not force
                and not getattr(self, "_in_scene_refresh", False)
                and self._last_state is not None
            ):
                live_mode = compute_active_hud_view_mode(self._last_state)
                if live_mode != self._active_view_mode or (live_mode == "hidden" and not self._scene_hidden):
                    self.refresh_scene_overlay(self._last_state)
                    return True
            if self._scene_hidden:
                if self._gdk_display:
                    self._gdk_display.error_trap_push()
                try:
                    self.Gtk.Widget.set_opacity(self.win, 0.0)
                    cur_x, cur_y = self.win.get_position()
                    if cur_x > -4000 or cur_y > -4000:
                        self.win.move(-8192, -8192)
                finally:
                    if self._gdk_display:
                        self._gdk_display.error_trap_pop_ignored()
                return True
            if self.drag_active:
                return True
            now = time.time()
            prev_rect = self.last_game_rect
            if force or (now - self._last_rect_query_ts >= 1.2):
                self._last_rect_query_ts = now
                rect = self._query_game_rect()
            else:
                rect = self.last_game_rect
            try:
                _, nat_req = self.win.get_preferred_size()
                pref_w, pref_h = nat_req.width, nat_req.height
            except Exception:
                pref_w, pref_h = 0, 0
            alloc = self.win.get_allocation()
            hud_w = pref_w if pref_w > 40 else (alloc.width if alloc.width > 40 else 280)
            hud_h = pref_h if pref_h > 18 else (alloc.height if alloc.height > 18 else 36)

            if self._gdk_display:
                self._gdk_display.error_trap_push()
            try:
                if rect:
                    self.last_game_rect = rect
                    content_rect = compute_game_content_box(rect) or rect
                    gx, gy, gw, gh = content_rect
                    if self.diegetic_mode:
                        use_bot = self._uses_bottom_trough()
                        cust_off = self.trough_custom_offset if use_bot else getattr(self, "_top_slot_custom_offset", None)
                        if cust_off is not None:
                            dx, dy = cust_off
                            target_x = max(0, gx + dx)
                            target_y = max(30, gy + dy)
                        elif use_bot:
                            tr_cfg = self.anchors.get("bottom_trough", {"rx": 0.530, "ry": 0.915})
                            cx = int(gx + gw * float(tr_cfg.get("rx", 0.530)))
                            cy = int(gy + gh * float(tr_cfg.get("ry", 0.915)))
                            target_x = max(gx + 8, min(gx + gw - hud_w - 8, cx - hud_w // 2))
                            target_y = max(34, min(gy + gh - hud_h - 4, cy - hud_h // 2))
                        else:
                            # 嵌入顶部纯白状态栏『¥ 120』与右侧『3x』之间的天然空槽 (rx=0.665, ry=0.042)
                            ts_cfg = self.anchors.get("top_bar_slot", {"rx": 0.665, "ry": 0.042})
                            cx = int(gx + gw * float(ts_cfg.get("rx", 0.665)))
                            cy = int(gy + gh * float(ts_cfg.get("ry", 0.042)))
                            min_x = int(gx + gw * 0.582)
                            max_x = max(min_x, int(gx + gw * 0.748) - hud_w)
                            target_x = max(min_x, min(max_x, cx - hud_w // 2))
                            target_y = max(30, min(gy + gh - hud_h - 4, cy - hud_h // 2))
                    else:
                        if self.custom_offset is not None:
                            dx, dy = self.custom_offset
                            target_x = max(0, gx + dx)
                            target_y = max(38, gy + dy)
                        else:
                            margin_r, margin_t = 14, 10
                            target_x = max(0, gx + gw - hud_w - margin_r)
                            target_y = max(38, gy + margin_t)
                    cur_x, cur_y = self.win.get_position()
                    if force or abs(cur_x - target_x) > 2 or abs(cur_y - target_y) > 2:
                        self.win.move(target_x, target_y)
                    if self.diegetic_mode and self._active_badge_specs and (force or rect != prev_rect):
                        self._apply_badge_specs(self._active_badge_specs)
                elif self.manual_pos is not None:
                    cur_x, cur_y = self.win.get_position()
                    tx, ty = self.manual_pos
                    if force or abs(cur_x - tx) > 2 or abs(cur_y - ty) > 2:
                        self.win.move(tx, ty)
                elif force:
                    if self.diegetic_mode:
                        self.win.move(1280, 72)
                    else:
                        self.win.move(1980, 40)

                if not self.win.get_visible():
                    self.win.show()
                self.Gtk.Widget.set_opacity(self.win, 1.0)
                gdk_win = self.win.get_window()
                if gdk_win:
                    if hasattr(gdk_win, "is_viewable") and not gdk_win.is_viewable():
                        self.win.show()
                    gdk_win.raise_()
                if self.diegetic_mode:
                    for pill in self.badge_pool[: len(self._active_badge_specs)]:
                        if pill._visible and not pill._closing:
                            if not pill.win.get_visible():
                                pill.win.show_all()
                            self.Gtk.Widget.set_opacity(pill.win, 1.0)
                            pw_gdk = pill.win.get_window()
                            if pw_gdk:
                                if hasattr(pw_gdk, "is_viewable") and not pw_gdk.is_viewable():
                                    pill.win.show_all()
                                pw_gdk.raise_()
            finally:
                if self._gdk_display:
                    self._gdk_display.error_trap_pop_ignored()
        except Exception:
            pass
        return True

    def _load_scaled_pixbuf(self, path: str, size: int = 22):
        if not path or not os.path.exists(path):
            return None
        key = (path, size)
        if key not in self._pixbuf_cache:
            try:
                self._pixbuf_cache[key] = self.GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
            except Exception:
                self._pixbuf_cache[key] = None
        return self._pixbuf_cache[key]

    def _build_q_avatar_pixbuf(self, char_id: Optional[int], size: int = 24):
        """懒加载并 LRU 缓存带 1.5px #142342 描边环的 24×24 圆形 Q 版小头像，缺失时平滑降级几何星徽。"""
        import math

        sz = max(18, int(size))
        norm_cid = None
        if char_id is not None:
            try:
                v = int(char_id)
                if v > 0:
                    norm_cid = v
            except (TypeError, ValueError):
                norm_cid = None
        key = (norm_cid, sz)
        cached = self._q_avatar_pixbuf_cache.get(key)
        if cached is not None:
            self._q_avatar_pixbuf_cache.move_to_end(key)
            return cached

        pb_out = None
        try:
            surf = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, sz, sz)
            cr = self.cairo.Context(surf)
            cx, cy = sz * 0.5, sz * 0.5
            r_in = max(4.0, sz * 0.5 - 1.2)
            path = resolve_q_avatar_path(norm_cid)
            drawn_img = False
            if path and os.path.isfile(path):
                try:
                    raw_pb = self.GdkPixbuf.Pixbuf.new_from_file_at_scale(path, sz, sz, False)
                    if raw_pb is not None:
                        cr.save()
                        cr.arc(cx, cy, r_in, 0, 2 * math.pi)
                        cr.clip()
                        cr.set_source_rgb(*HUD_PAPER_RGB)
                        cr.paint()
                        self.Gdk.cairo_set_source_pixbuf(cr, raw_pb, 0, 0)
                        cr.paint()
                        cr.restore()
                        drawn_img = True
                except Exception:
                    drawn_img = False
            if not drawn_img:
                cr.save()
                cr.arc(cx, cy, r_in, 0, 2 * math.pi)
                cr.clip()
                grad = self.cairo.LinearGradient(0, 0, 0, sz)
                grad.add_color_stop_rgb(0.0, 27 / 255.0, 91 / 255.0, 255 / 255.0)
                grad.add_color_stop_rgb(1.0, 17 / 255.0, 57 / 255.0, 166 / 255.0)
                cr.set_source(grad)
                cr.paint()
                cr.move_to(sz * 0.58, 0)
                cr.line_to(sz, 0)
                cr.line_to(sz * 0.72, sz)
                cr.line_to(sz * 0.34, sz)
                cr.close_path()
                cr.set_source_rgba(255 / 255.0, 81 / 255.0, 38 / 255.0, 0.70)
                cr.fill()
                # 冠军金五角星
                r_o, r_s = sz * 0.31, sz * 0.135
                cr.new_path()
                for i in range(10):
                    ang = -math.pi * 0.5 + i * (math.pi / 5.0)
                    rr = r_o if (i % 2 == 0) else r_s
                    px, py = cx + rr * math.cos(ang), cy + rr * math.sin(ang)
                    if i == 0:
                        cr.move_to(px, py)
                    else:
                        cr.line_to(px, py)
                cr.close_path()
                cr.set_source_rgb(*HUD_CHAMPION_GOLD_RGB)
                cr.fill()
                cr.restore()
            # 1.5px 深墨蓝细描边环 (#142342)
            cr.arc(cx, cy, sz * 0.5 - 0.85, 0, 2 * math.pi)
            cr.set_source_rgba(*HUD_INK_STROKE_RGBA)
            cr.set_line_width(1.5)
            cr.stroke()
            pb_out = self.Gdk.pixbuf_get_from_surface(surf, 0, 0, sz, sz)
        except Exception:
            pb_out = None

        if pb_out is not None:
            self._q_avatar_pixbuf_cache[key] = pb_out
            if len(self._q_avatar_pixbuf_cache) > 64:
                self._q_avatar_pixbuf_cache.popitem(last=False)
        return pb_out

    def _set_header_avatar_by_char(self, char_id: Optional[int]):
        self._active_char_id = char_id
        try:
            is_initactor = self.diegetic_mode and (self._active_view_mode == "initactor") and bool(
                self.equip_icons_data or self.full_markup
            )
            if self.diegetic_mode and not is_initactor:
                pb = self._build_q_avatar_pixbuf(char_id, size=24)
                if pb is not None:
                    self.header_avatar.set_from_pixbuf(pb)
                    self.header_avatar.show()
                    return
        except Exception:
            pass
        self.header_avatar.hide()

    @staticmethod
    def _format_hud_tag_spans(tags: list, muted: bool = False) -> str:
        if not tags:
            return ""
        if muted:
            joined = " · ".join(html.escape(t) for t in tags[:2])
            return f"  <span size='x-small' foreground='#64748b'>{joined}</span>"
        spans = []
        for t in tags[:4]:
            esc = html.escape(t)
            if t.startswith(("⚠️", "🚨")):
                spans.append(f"<span foreground='#fca5a5'>{esc}</span>")
            elif "特化" in t or "优先" in t and t.startswith("🎯"):
                spans.append(f"<span foreground='#fbbf24' weight='bold'>{esc}</span>")
            elif t == "原评#1":
                spans.append(f"<span foreground='#38bdf8' weight='bold'>{esc}</span>")
            elif any(k in t for k in ("哨冲", "必爆", "满格爆气", "满彩")):
                spans.append(f"<span foreground='#fde047'>{esc}</span>")
            elif any(k in t for k in ("差1升", "50%爆", "冲3爆", "冲6爆", "冲9爆", "突破")):
                spans.append(f"<span foreground='#93c5fd'>{esc}</span>")
            else:
                spans.append(f"<span foreground='#cbd5e1'>{esc}</span>")
        sep = "<span foreground='#475569'> · </span>"
        return f"  <span size='small'>{sep.join(spans)}</span>"

    @staticmethod
    def _clean_train_highlight_tag(tags: list) -> str:
        """角标在专项特化改写排名、或出现极高价值关键节点（哨冲/必爆/满彩）时附加简短后缀，常规回合只净显所练属性。"""
        for t in tags or ():
            clean = t
            for ic in ("👑", "🥈", "🥉", "🔥", "✨", "⚡", "🎺", "⚠️", "🚨", "🎯", "*"):
                clean = clean.replace(ic, "")
            clean = clean.strip()
            if "特化" in clean or clean == "原评#1":
                return clean[:12]
        for t in tags or ():
            clean = t
            for ic in ("👑", "🥈", "🥉", "🔥", "✨", "⚡", "🎺", "⚠️", "🚨", "*"):
                clean = clean.replace(ic, "")
            clean = re.sub(r"\([^)]*\)", "", clean).strip()
            if any(k in clean for k in ("哨冲", "必爆", "满格", "满彩")):
                return clean[:6]
        return ""

    @classmethod
    def _format_train_subject_markup(
        cls,
        item: dict,
        state: Optional[dict] = None,
        style_key: str = "best",
        include_tag: bool = True,
        prefix: str = "",
        suffix_warn: str = "",
    ) -> str:
        """
        格式化训练按钮右侧说明：直接干脆显示当前所练单项主属性（如 防守、意识、弹跳），
        不显示副属性，也不再展示冗余的『差 XX 分』或『合训≈+XX』。
        """
        idx = int((item or {}).get("idx", 0) or 0)
        if idx == 0:
            return html.escape(f"{prefix}休息回体")
        props = (state or {}).get("props") or {}
        prop_max = (state or {}).get("prop_max") or {}

        main_id = 100 + idx
        main_name = STAT_NAMES.get(main_id, cls._short_cmd_label(item.get("name", "")))
        if prop_max and (int(prop_max.get(main_id, 9999)) - int(props.get(main_id, 0))) <= 0:
            main_part = f"{html.escape(main_name)}(满)"
        else:
            main_part = html.escape(main_name)

        pref_part = html.escape(prefix) if prefix else ""
        base_mk = f"{pref_part}{main_part}"

        if suffix_warn:
            return f"{base_mk} · {html.escape(suffix_warn)}"

        if include_tag:
            key_tag = cls._clean_train_highlight_tag((item or {}).get("hud_tags") or [])
            if key_tag:
                tag_fg = "#B45309" if style_key == "best" else "#0369A1"
                return f"{base_mk}<span size='88%' foreground='{tag_fg}'> · {html.escape(key_tag)}</span>"
        return base_mk

    @staticmethod
    def _clean_plain_pill_reason(tags: list, max_items: int = 1) -> str:
        """提取适合风格B奶白右侧色块的干净人话词条（去除冗余 Emoji 与杂乱星号）。"""
        if not tags:
            return ""
        picked = []
        for t in tags:
            clean = t
            for ic in ("👑", "🥈", "🥉", "🔥", "✨", "⚡", "🎺", "⚠️", "🚨", "*"):
                clean = clean.replace(ic, "")
            clean = clean.strip()
            if not clean or "合训" in clean:
                continue
            if any(k in clean for k in ("哨冲", "必爆", "满格", "满彩", "彩×", "差1升", "50%爆", "同框")):
                picked.append(clean)
            if len(picked) >= max_items:
                break
        if not picked and tags:
            for t0 in tags:
                c0 = t0
                for ic in ("👑", "🥈", "🥉", "🔥", "✨", "⚡", "🎺", "*"):
                    c0 = c0.replace(ic, "")
                if c0.strip() and "合训" not in c0:
                    picked.append(c0.strip())
                    break
        return " · ".join(picked[:max_items])

    @staticmethod
    def _strip_markup_to_clean_text(raw_markup: str) -> str:
        txt = re.sub(r"<[^>]+>", "", raw_markup or "")
        txt = html.unescape(txt)
        for ic in ("🏀", "👑", "🥈", "🥉", "🛒", "🎁", "⚡", "💰", "🎺", "🌴", "📖", "🎯"):
            txt = txt.replace(ic, "")
        return re.sub(r"\s+", " ", txt).strip(" ·|")

    def _rebuild_rows_box(self, max_chars: int = 46):
        for child in list(self.rows_box.get_children()):
            self.rows_box.remove(child)
        row_lines = [ln for ln in (self.full_markup or "").splitlines() if ln.strip()]
        for ln in row_lines:
            lbl = self.Gtk.Label()
            lbl.set_xalign(0.0)
            lbl.set_yalign(0.5)
            lbl.set_line_wrap(True)
            lbl.set_line_wrap_mode(self.Pango.WrapMode.WORD_CHAR)
            lbl.set_max_width_chars(max_chars)
            lbl.set_markup(ln)
            lbl.show()
            self.rows_box.pack_start(lbl, False, False, 0)

    def _rebuild_equip_strip(self):
        for child in list(self.equip_strip_box.get_children()):
            self.equip_strip_box.remove(child)
        has_tip = bool((self.tip_markup or "").strip()) and not self.diegetic_mode
        if (not self.diegetic_mode and self.collapsed) or not self.equip_icons_data:
            self.equip_strip_box.hide()
            self.rows_box.set_margin_bottom(5 if has_tip else 7)
            return
        shown = 0
        for item in self.equip_icons_data[:5]:
            chip = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=3)
            pb = self._load_scaled_pixbuf(item.get("icon_path", ""), size=22)
            if pb is not None:
                img = self.Gtk.Image.new_from_pixbuf(pb)
                chip.pack_start(img, False, False, 0)
            lbl = self.Gtk.Label()
            nm = html.escape(item.get("name", "")[:4])
            pref = html.escape(item.get("pref", "")[:1])
            if item.get("is_borrow"):
                lbl.set_markup(f"<span size='x-small' foreground='#34d399'><b>借{nm}</b></span>")
            elif item.get("active", True):
                lbl.set_markup(f"<span size='x-small' foreground='#e2e8f0'><b>{nm}</b>({pref})</span>")
            else:
                lbl.set_markup(f"<span size='x-small' foreground='#94a3b8'>{nm}[白]</span>")
            chip.pack_start(lbl, False, False, 0)
            chip.show_all()
            self.equip_strip_box.pack_start(chip, False, False, 0)
            shown += 1
        if shown > 0:
            self.rows_box.set_margin_bottom(3)
            self.equip_strip_box.set_margin_bottom(5 if has_tip else 6)
            self.equip_strip_box.show()
        else:
            self.rows_box.set_margin_bottom(5 if has_tip else 7)
            self.equip_strip_box.hide()

    def _update_header(self, header_markup: str):
        self._last_header = header_markup
        if not self.diegetic_mode and not self.collapsed:
            self.header_label.set_markup(header_markup)

    def _set_scene_hidden(self, hidden: bool):
        if self._scene_hidden == hidden:
            if hidden:
                self.hide_all_badges()
            return
        self._scene_hidden = hidden
        if hidden:
            self.drag_active = False
            self.hide_all_badges()
            if self._gdk_display:
                self._gdk_display.error_trap_push()
            try:
                # 保持窗口映射并透明移至屏外 + 置空点击区，避免 cosmic-comp 高频 unmap/map 导致丢窗
                self.Gtk.Widget.set_opacity(self.win, 0.0)
                self.win.move(-8192, -8192)
                gdk_win = self.win.get_window()
                if gdk_win:
                    empty_reg = self.cairo.Region(self.cairo.RectangleInt(0, 0, 0, 0))
                    gdk_win.input_shape_combine_region(empty_reg, 0, 0)
                self.win.queue_draw()
            finally:
                if self._gdk_display:
                    self._gdk_display.error_trap_pop_ignored()
        else:
            self._apply_current_markup()
            if self._gdk_display:
                self._gdk_display.error_trap_push()
            try:
                if not self.win.get_visible():
                    self.win.show()
                self.Gtk.Widget.set_opacity(self.win, 1.0)
                self._on_size_allocate(self.win, self.win.get_allocation())
                gdk_win = self.win.get_window()
                if gdk_win:
                    gdk_win.raise_()
            finally:
                if self._gdk_display:
                    self._gdk_display.error_trap_pop_ignored()

    def _format_native_slot_labels(self, raw_trough: str):
        """将『标签｜正文』格式化进顶栏浅冰灰原生第4槽（自动按游戏窗口高度缩放字号）。"""
        gh = self.last_game_rect[3] if self.last_game_rect else 1080
        scale = max(0.84, min(1.30, gh / 1080.0))
        pt_tag = round(12.6 * scale, 1)
        pt_body = round(12.8 * scale, 1)

        if "｜" in (raw_trough or ""):
            tag_s, body_s = raw_trough.split("｜", 1)
        else:
            plain = self._strip_markup_to_clean_text(raw_trough)
            tag_s, body_s = "星奈", (plain or "军师待命")

        tag_clean = self._strip_markup_to_clean_text(tag_s)[:8] or "星奈"
        self.slot_tag_label.set_markup(
            f"<span font_desc='Noto Sans CJK SC Black {pt_tag}' foreground='#FFFFFF'>{html.escape(tag_clean)}</span>"
        )
        self.slot_tag_label.show()

        # 若 body_s 已带显式深色系 span 则直接包裹字号，否则自动着深墨蓝并高亮关键数字
        if "<span" in body_s and any(
            c in body_s for c in (HUD_TEXT_INK, "#1B5BFF", "#E03A12", "#F03E5A", "#253B52", "#1E88E5", "#C2410C", "#B91C1C")
        ):
            body_mk = f"<span font_desc='Noto Sans CJK SC Bold {pt_body}'>{body_s}</span>"
        else:
            b_clean = self._strip_markup_to_clean_text(body_s)[:44]
            b_esc = html.escape(b_clean)
            b_esc = re.sub(
                r"(\d+/\d+|EV\s*\d+|\+\d+)",
                r"<span foreground='#1B5BFF'>\1</span>",
                b_esc,
            )
            fg_main = "#F03E5A" if ("⚠️" in body_s or "高危" in b_clean or "空转" in b_clean) else HUD_TEXT_INK
            body_mk = f"<span font_desc='Noto Sans CJK SC Bold {pt_body}' foreground='{fg_main}'>{b_esc}</span>"
        self.header_label.set_markup(body_mk)

    def _apply_current_markup(self):
        if getattr(self, "_closing", False):
            return
        if self._scene_hidden:
            return

        if self.diegetic_mode:
            is_initactor = (self._active_view_mode == "initactor") and bool(self.equip_icons_data or self.full_markup)
            if is_initactor:
                self.slot_tag_label.hide()
                self.header_label.set_markup(self.trough_markup)
                self._rebuild_rows_box(max_chars=66)
                self.rows_box.show()
                self._rebuild_equip_strip()
                self.tip_box.hide()
            else:
                self._format_native_slot_labels(self.trough_markup)
                self.rows_box.hide()
                self.equip_strip_box.hide()
                self.tip_box.hide()
        else:
            self.slot_tag_label.hide()
            if self.collapsed:
                self.header_label.set_markup(
                    f"{self.mini_markup}  <span size='x-small' foreground='#7dd3fc'>▸</span>"
                )
                self.rows_box.hide()
                self.equip_strip_box.hide()
                self.tip_box.hide()
            else:
                self.header_label.set_markup(getattr(self, "_last_header", "🏀 星奈 · 实时军师"))
                self._rebuild_rows_box(max_chars=46)
                self.rows_box.show()
                self._rebuild_equip_strip()
                tip_clean = (self.tip_markup or "").strip()
                if tip_clean:
                    self.tip_label.set_markup(tip_clean)
                    self.tip_label.show()
                    self.tip_box.show()
                else:
                    self.tip_box.hide()

        self._update_focus_btn_label()

        if self._gdk_display:
            self._gdk_display.error_trap_push()
        try:
            _, nat_req = self.win.get_preferred_size()
            target_w = max(40, int(nat_req.width))
            target_h = max(20, int(nat_req.height))
            cur_w, cur_h = self.win.get_size()
            if abs(cur_w - target_w) > 1 or abs(cur_h - target_h) > 1:
                self.win.resize(target_w, target_h)
            self.win.queue_draw()
        finally:
            if self._gdk_display:
                self._gdk_display.error_trap_pop_ignored()
        self.sync_position_and_raise(force=True)
        self.GLib.idle_add(self._idle_redraw_and_raise)

    def _idle_redraw_and_raise(self):
        if getattr(self, "_closing", False):
            return False
        try:
            if not self._scene_hidden:
                self.win.queue_draw()
                self.sync_position_and_raise(force=True)
        except Exception:
            pass
        return False

    def set_markup(
        self,
        markup_str: str,
        mini_summary: str = None,
        equip_icons: list = None,
        tip_markup: str = "",
        trough_summary: str = None,
    ):
        self.full_markup = markup_str
        self.tip_markup = tip_markup or ""
        self.equip_icons_data = list(equip_icons) if equip_icons else []
        if mini_summary:
            self.mini_markup = mini_summary
        if trough_summary:
            self.trough_markup = trough_summary
        elif mini_summary:
            self.trough_markup = mini_summary
        self._apply_current_markup()

    def _rec_enabled(self, key: str) -> bool:
        return is_hud_rec_enabled(self.hud_config, key)

    def set_hud_config(self, new_cfg: dict, state: Optional[dict] = None):
        """热更新 HUD 推荐开关配置并立即重绘当前界面角标与顶栏原生槽。"""
        self.hud_config = new_cfg
        if self._active_view_mode == "initactor" and self._last_note_adv:
            self.render_note_selection(self._last_note_adv, self._last_note_scan_badge, self._last_note_scan_status)
        elif state is not None:
            self.refresh_scene_overlay(state)
        elif self._last_state is not None:
            self.refresh_scene_overlay(self._last_state)
        else:
            self._apply_current_markup()

    # -----------------------------------------------------------------
    # 6 大场景贴身角标计算与一键联动刷新 (refresh_scene_overlay · 风格B)
    # -----------------------------------------------------------------
    def _build_weekday_trough_line(self, state: dict, scored: list, include_shop: Optional[bool] = None) -> str:
        """构建顶栏第4原生槽精简情报（不重复抄写顶栏已有的体力与金币，仅展示哨子熟练度与关键防忘提醒）。"""
        use_shop = self._rec_enabled("shop") if include_shop is None else bool(include_shop)
        raw_tips = format_weekday_hud_tips(state, scored, include_shop=use_shop)
        tag = f"{int(state.get('round', 1)):02d}训"

        if int(state.get("round", 0)) == 1 and state.get("main_char_id"):
            cfg_ids = sorted(set(state.get("supactor_card_to_cfg", {}).values()))
            syn_tips = check_lineup_and_equip_synergy(
                int(state["main_char_id"]),
                state.get("teammate_char_ids", []),
                cfg_ids,
            )
            if syn_tips:
                clean_syn = self._strip_markup_to_clean_text(syn_tips[0])[:38]
                return f"{tag}｜<span foreground='#C2410C'>{html.escape(clean_syn)}</span>"

        if raw_tips:
            clean_t = self._strip_markup_to_clean_text(raw_tips[0])[:38]
            color = "#C2410C" if any(k in clean_t for k in ("必买", "免费", "哨", "集训", "海滩", "许愿")) else "#253B52"
            return f"{tag}｜<span foreground='{color}'>{html.escape(clean_t)}</span>"

        _, w3_badge = get_whistle_step(state)
        w_clean = self._strip_markup_to_clean_text(w3_badge)
        # 精简文案如："3星哨 熟0/100(同框+2/次)" -> "哨子 0/100 · 同框+2"
        w_clean = re.sub(r"^\d星哨\s*熟?", "哨子 ", w_clean)
        w_clean = w_clean.replace("/次", "")
        w_clean = re.sub(r"\(([^)]+)\)", r" · \1", w_clean)
        w_esc = html.escape(w_clean)
        w_esc = re.sub(r"(\d+/\d+)", r"<span foreground='#1E88E5'>\1</span>", w_esc)
        w_esc = w_esc.replace(" · ", "<span foreground='#607D94'> · </span>")
        return f"{tag}｜<span foreground='#253B52'>{w_esc}</span>"

    @staticmethod
    def _short_cmd_label(name: str) -> str:
        """从 '2号·弹跳' 提炼短名 '弹跳' 用于近分并排副章。"""
        if "·" in (name or ""):
            return name.split("·", 1)[1].strip()
        return (name or "").strip()

    def _build_badges_for_mode(self, mode: str, state: dict) -> tuple[list, str]:
        """根据当前视图模式与 hud_config 开关返回风格B (badge_specs, trough_markup_override)。"""
        return shared_build_badges_for_mode(
            mode,
            state,
            hud_config=self.hud_config,
            anchors=self.anchors,
            strategy=self.strategy,
            last_turn_scored=self._last_turn_scored,
            last_wk_adv=self._last_wk_adv,
            last_story_adv=self._last_story_adv,
            last_skill_evals=self._last_skill_evals,
            stat_names=STAT_NAMES,
            store_item_catalog=STORE_ITEM_CATALOG,
            extract_weekday_shop_badge_fn=extract_weekday_shop_badge,
            format_weekday_hud_tips_fn=format_weekday_hud_tips,
            format_weekday_shop_tip_fn=format_weekday_shop_tip,
            get_whistle_step_fn=get_whistle_step,
            check_lineup_and_equip_synergy_fn=check_lineup_and_equip_synergy,
            get_char_role_tags_fn=get_char_role_tags,
        )

    def refresh_scene_overlay(self, state: dict):
        """当日志 UI 场景切换（一级<->二级、进出商店、剧情对话/选择、过场锁）或配置开关变化时即时同步角标与底部槽显隐。"""
        if getattr(self, "_closing", False) or state is None:
            return
        if getattr(self, "_in_scene_refresh", False):
            return
        self._in_scene_refresh = True
        try:
            self._last_state = state
            mode = compute_active_hud_view_mode(state)
            self._active_view_mode = mode

            if mode == "hidden":
                self._set_scene_hidden(True)
                return

            if mode == "initactor":
                if not self._rec_enabled("note"):
                    self._set_scene_hidden(True)
                    return
                self._set_scene_hidden(False)
                self.hide_all_badges()
                self._apply_current_markup()
                return

            specs, trough_line = self._build_badges_for_mode(mode, state)
            if not specs and not trough_line:
                self._set_scene_hidden(True)
                return

            self._set_scene_hidden(False)
            self._set_header_avatar_by_char(extract_active_char_id(state))
            if not self.diegetic_mode:
                self.hide_all_badges()
                self._apply_current_markup()
                return

            if trough_line:
                self.trough_markup = trough_line
            self._apply_current_markup()
            self._apply_badge_specs(specs)
        finally:
            self._in_scene_refresh = False

    def render_skill_choice(self, state: dict, skill_evals: list):
        """渲染育成选技能三选一界面（贴身三卡角标 + 经典卡片兜底）。"""
        self._last_state = state
        self._last_skill_evals = list(skill_evals or [])
        self._set_header_avatar_by_char(extract_active_char_id(state))
        roles = sorted(get_char_role_tags(int(state.get("main_char_id") or 0)) - {"通用"})
        role_str = "/".join(roles) if roles else "通用"
        self._update_header(
            f"<span weight='bold' foreground='#38bdf8'>🎯 技能三选一 · 职责[{html.escape(role_str)}]</span>"
        )
        lines = []
        slot_names = ("左卡", "中卡", "右卡")
        show_sec = self._rec_enabled("secondary")
        for item in self._last_skill_evals[:3]:
            st_k = item.get("style_key", "second")
            if not show_sec and st_k == "second":
                continue
            s_lbl = slot_names[item["slot_idx"]] if 0 <= item["slot_idx"] < 3 else f"#{item['slot_idx']+1}"
            btxt = html.escape(item.get("badge_text", ""))
            snm = html.escape(item.get("skill_name", ""))
            rsn = html.escape(item.get("reason", ""))
            color = "#4ade80" if st_k == "best" else ("#fca5a5" if st_k == "warn" else ("#94a3b8" if st_k == "dup" else "#7dd3fc"))
            lines.append(
                f"<span foreground='{color}' weight='bold'>{s_lbl} {btxt}</span>  "
                f"<span foreground='#f8fafc' weight='bold'>{snm}</span>  "
                f"<span size='small' foreground='#cbd5e1'>{rsn}</span>"
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
        self._set_header_avatar_by_char(extract_active_char_id(state))
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
                        f"<span size='small' foreground='#86efac'>EV {item['ev']:.0f}</span>  "
                        f"<span size='small' foreground='#e2e8f0'>{e_esc}</span>"
                    )
                else:
                    diff = top_ev - item["ev"]
                    badge = "🥈" if i == 1 else "🥉"
                    if diff >= 35:
                        lines.append(
                            f"<span size='small' foreground='#64748b'>{badge} {t_esc}  -{diff:.0f} EV  {e_esc}</span>"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#38bdf8' weight='bold'>{badge} {t_esc}</span> "
                            f"<span size='small' foreground='#94a3b8'>-{diff:.0f} EV</span>  "
                            f"<span size='small' foreground='#cbd5e1'>{e_esc}</span>"
                        )
        else:
            for ol in ev_advice["opt_lines"]:
                lines.append(f"<span size='small' foreground='#e2e8f0'>{html.escape(ol)}</span>")
            if ev_advice.get("recommendation"):
                tip_str = f"<span size='small' foreground='#fde047'>{html.escape(ev_advice['recommendation'])}</span>"
        first_rec_line = ev_advice["recommendation"].splitlines()[0]
        self.set_markup(
            "\n".join(lines),
            mini_summary=f"<span foreground='#38bdf8' weight='bold'>📖 {ev_title}</span> <span foreground='#4ade80' weight='bold'>{html.escape(first_rec_line)}</span>",
            tip_markup=tip_str,
        )
        self.refresh_scene_overlay(state)

    def render_weekend_advice(self, state: dict, wk: dict):
        self._last_state = state
        self._last_wk_adv = wk
        self._set_header_avatar_by_char(extract_active_char_id(state))
        day_esc = html.escape(wk["day_tag"])
        has_wt = bool(wk.get("has_weekend_train") and wk.get("best_train_opt"))
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
        show_sec = self._rec_enabled("secondary")

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
                    f"<span size='small' foreground='#86efac'>EV {best_ev:.0f}</span>  "
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
                            f"<span size='small' foreground='#64748b'>🥈 次训  -{diff_t2:.0f} EV  {t2_esc}</span>{tag_html_2}"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#38bdf8' weight='bold'>🥈 次训</span> "
                            f"<span size='small' foreground='#94a3b8'>-{diff_t2:.0f} EV</span>  "
                            f"<span size='small' foreground='#cbd5e1'>{t2_esc}</span>{tag_html_2}"
                        )
                if out_opt:
                    diff_out = max(0.0, best_ev - float(out_opt["total_ev"]))
                    out_c = html.escape(out_opt["compact"])
                    if diff_out >= 35.0:
                        lines.append(
                            f"<span size='small' foreground='#94a3b8'>🌴 外出  -{diff_out:.0f} EV  {out_c}</span>"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#c084fc' weight='bold'>🌴 外出</span> "
                            f"<span size='small' foreground='#d8b4fe'>-{diff_out:.0f} EV</span>  "
                            f"<span size='small' foreground='#e2e8f0'>{out_c}</span>"
                        )
            else:
                lines.append(
                    f"<span foreground='#4ade80' weight='bold'>👑 外出</span> "
                    f"<span size='small' foreground='#86efac'>EV {best_ev:.0f}</span>  "
                    f"<span foreground='#f8fafc' weight='bold'>{best_c}</span>"
                )
                diff_tr = max(0.0, best_ev - float(tr_opt["total_ev"]))
                tr_c = html.escape(tr_opt["compact"])
                tag_html_tr = self._format_hud_tag_spans(top_t.get("hud_tags") or [], muted=(diff_tr >= 35.0))
                if diff_tr >= 35.0:
                    lines.append(
                        f"<span size='small' foreground='#94a3b8'>🏋️ 加训  -{diff_tr:.0f} EV  {tr_c}</span>{tag_html_tr}"
                    )
                else:
                    lines.append(
                        f"<span foreground='#fbbf24' weight='bold'>🏋️ 加训</span> "
                        f"<span size='small' foreground='#fde68a'>-{diff_tr:.0f} EV</span>  "
                        f"<span size='small' foreground='#e2e8f0'>{tr_c}</span>{tag_html_tr}"
                    )
                if show_sec and out2_opt:
                    diff_o2 = max(0.0, best_ev - float(out2_opt["total_ev"]))
                    out2_c = html.escape(out2_opt["compact"])
                    if diff_o2 >= 35.0:
                        lines.append(
                            f"<span size='small' foreground='#64748b'>🥈 外出次选  -{diff_o2:.0f} EV  {out2_c}</span>"
                        )
                    else:
                        lines.append(
                            f"<span foreground='#38bdf8' weight='bold'>🥈 外出次选</span> "
                            f"<span size='small' foreground='#94a3b8'>-{diff_o2:.0f} EV</span>  "
                            f"<span size='small' foreground='#cbd5e1'>{out2_c}</span>"
                        )
        else:
            lines = [
                f"<span foreground='#4ade80' weight='bold'>👑 首选</span> "
                f"<span size='small' foreground='#86efac'>EV {best_ev:.0f}</span>  "
                f"<span foreground='#f8fafc' weight='bold'>{best_c}</span>"
            ]
            if show_sec and alt_raw:
                alt_c = html.escape(alt_raw)
                diff_ev = float(wk.get("alt_diff_ev", 0.0))
                if diff_ev >= 35.0:
                    lines.append(
                        f"<span size='small' foreground='#64748b'>🥈 次选  -{diff_ev:.0f} EV  {alt_c}</span>"
                    )
                else:
                    lines.append(
                        f"<span foreground='#38bdf8' weight='bold'>🥈 次选</span> "
                        f"<span size='small' foreground='#94a3b8'>-{diff_ev:.0f} EV{'·≈打平' if diff_ev <= 2.0 else ''}</span>  "
                        f"<span size='small' foreground='#cbd5e1'>{alt_c}</span>"
                    )

        tip_lines = []
        if has_wt and best_act[0] != 0 and wk.get("second_train_item"):
            t2_i = wk["second_train_item"]
            d_t2 = max(0.0, best_ev - float(t2_i.get("route_ev", t2_i["score"])))
            tip_lines.append(
                f"<span size='small' foreground='#fde68a'>🏋️ 次优加训：{html.escape(t2_i['name'])}(EV {t2_i.get('route_ev', t2_i['score']):.0f}, -{d_t2:.0f})</span>"
            )
        if wk.get("hud_lookahead"):
            tip_lines.append(
                f"<span size='small' foreground='#fcd34d' weight='bold'>{html.escape(wk['hud_lookahead'])}</span>"
            )
        shop_extra = (
            [x for x in (wk.get("hud_free_tip", ""), wk.get("hud_shop_tip", "")) if x]
            if self._rec_enabled("shop")
            else []
        )
        if shop_extra:
            tip_lines.append(
                "<span size='small' foreground='#7dd3fc'>"
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
                f"<span size='small' foreground='#f9a8d4'>🎰 扭蛋近分同档：{html.escape(g_list[1]['short'])}</span>"
            )

        mini_hdr = (
            f"<span foreground='#fbbf24' weight='bold'>⚡{day_esc}</span>"
            if has_wt
            else f"<span foreground='#c084fc' weight='bold'>🌴{day_esc}</span>"
        )
        mini = f"{mini_hdr}  <span foreground='#4ade80' weight='bold'>👑{best_c}</span>"
        self.set_markup("\n".join(lines), mini_summary=mini, tip_markup="\n".join(tip_lines))
        self.refresh_scene_overlay(state)

    def render_turn(self, state: dict, scored: list, extra_tip: str = ""):
        self._last_state = state
        self._last_turn_scored = list(scored or [])
        self._last_turn_extra_tip = extra_tip
        self._set_header_avatar_by_char(extract_active_char_id(state))
        self._update_header(format_hud_turn_header(state))

        lines = []
        top_ev = scored[0]["score"] if scored else 0.0
        show_sec = self._rec_enabled("secondary")
        show_shop = self._rec_enabled("shop")
        for i, item in enumerate(scored[: (2 if show_sec else 1)]):
            name_esc = html.escape(item["name"])
            tags = item.get("hud_tags") or []
            if i == 0:
                tag_html = self._format_hud_tag_spans(tags, muted=False)
                lines.append(
                    f"<span foreground='#4ade80' weight='bold'>👑 {name_esc}</span> "
                    f"<span size='small' foreground='#86efac'>EV {item['score']:.0f}</span>"
                    f"{tag_html}"
                )
            else:
                diff = top_ev - item["score"]
                if diff >= 35.0:
                    tag_html = self._format_hud_tag_spans(tags, muted=True)
                    lines.append(
                        f"<span size='small' foreground='#64748b'>🥈 {name_esc}  -{diff:.0f} EV</span>{tag_html}"
                    )
                else:
                    tag_html = self._format_hud_tag_spans(tags, muted=False)
                    lines.append(
                        f"<span foreground='#38bdf8' weight='bold'>🥈 {name_esc}</span> "
                        f"<span size='small' foreground='#94a3b8'>-{diff:.0f} EV</span>"
                        f"{tag_html}"
                    )

        raw_tips = format_weekday_hud_tips(state, scored, include_shop=show_shop)
        tip_lines = [f"<span size='small'>{ln}</span>" for ln in raw_tips]

        if int(state.get("round", 0)) == 1 and state.get("main_char_id"):
            cfg_ids = sorted(set(state.get("supactor_card_to_cfg", {}).values()))
            syn_tips = check_lineup_and_equip_synergy(
                int(state["main_char_id"]),
                state.get("teammate_char_ids", []),
                cfg_ids,
            )
            for st_line in syn_tips[:2]:
                tip_lines.append(f"<span size='x-small' foreground='#fde047'>{html.escape(st_line)}</span>")

        if scored:
            best = scored[0]
            mini = (
                f"<span foreground='#fbbf24' weight='bold'>🏀{state['round']:02d}训</span>  "
                f"<span foreground='#4ade80' weight='bold'>👑{html.escape(best['name'])} (EV {best['score']:.0f})</span>"
            )
        else:
            mini = f"<span foreground='#fbbf24' weight='bold'>🏀{state['round']:02d}训</span>"
        trough_s = self._build_weekday_trough_line(state, scored, include_shop=show_shop)
        self.set_markup(
            "\n".join(lines),
            mini_summary=mini,
            tip_markup="\n".join(tip_lines),
            trough_summary=trough_s,
        )
        self.refresh_scene_overlay(state)

    def render_note_selection(self, adv: dict, scan_badge: str = "", scan_status_line: str = ""):
        self._last_note_adv = adv
        self._last_note_scan_badge = scan_badge
        self._last_note_scan_status = scan_status_line
        self._active_view_mode = "initactor"
        if not self._rec_enabled("note"):
            self._set_scene_hidden(True)
            return
        self._set_scene_hidden(False)
        self.hide_all_badges()

        c = adv["character"]
        self._set_header_avatar_by_char(c.get("id"))
        cname = html.escape(c.get("short_name", ""))
        pos = html.escape(c.get("best_pos", ""))
        badge_html = f"  <span size='x-small' foreground='#4ade80'>{html.escape(scan_badge)}</span>" if scan_badge else ""
        hdr = (
            f"<span weight='bold' foreground='#38bdf8'>🏀 📒 选人配装</span>  "
            f"<span foreground='#f8fafc' weight='bold'>{cname}</span>"
            f"<span size='small' foreground='#94a3b8'>({pos})</span>"
            f"{badge_html}  <span size='x-small' foreground='#7dd3fc'>双击🏀扫笔记 · 右键切卡片</span>"
        )
        self._update_header(hdr)
        markup = format_character_note_advice_hud_markup(adv, scan_status_line=scan_status_line)
        top_b = adv["borrow_combos"][0] if adv.get("borrow_combos") else None
        top_o = adv["pure_own_combos"][0] if adv.get("pure_own_combos") else None
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
        else:
            mini = f"<span foreground='#38bdf8' weight='bold'>📒{cname} 配装推荐</span>"
        self.set_markup(
            markup,
            mini_summary=mini,
            equip_icons=adv.get("equip_icon_items") or [],
            tip_markup=adv.get("hud_tip_markup", ""),
            trough_summary=hdr,
        )


def _collect_current_run_segments(logs: list[str], latest_text: str) -> tuple[str, str, bool]:
    """
    为实时监听提取当前正在进行的育成局片段：
    返回 (history_prefix, cur_seg, found_start)。
    - 若本局在 latest_text 内通过 cs_start_education 开局且未中断，history_prefix 为空；
    - 若经历过同日志断线重连或重启游戏续玩 (cs_restore_education)，先在本文件内往前找最近的
      cs_start_education，若仍未找到则倒序回溯最多 5 份旧日志拼接同局历史，避免丢失
      _LoadStory: 70/71（哨子事件）等跨进程累计状态。
    """
    start_pat = re.compile(r"request_call:\s+education\.cs_start_education")
    restore_pat = re.compile(r"request_call:\s+education\.cs_restore_education")
    end_pat = re.compile(r"request_call:\s+education\.cs_end_education")

    start_ms = list(start_pat.finditer(latest_text))
    restore_ms = list(restore_pat.finditer(latest_text))
    last_start = start_ms[-1].start() if start_ms else -1
    last_restore = restore_ms[-1].start() if restore_ms else -1

    # 正常从头开局（或尚未遇到 restore）
    if last_start >= last_restore:
        cur_seg = latest_text[last_start:] if last_start >= 0 else latest_text
        return "", cur_seg, last_start >= 0

    # 遇到 cs_restore_education：cur_seg 从最后一次 restore 开始，保证 UI 状态干净
    cur_seg = latest_text[last_restore:]

    # 1. 先检查同一份日志内 last_restore 之前是否有同局 cs_start_education（中间未 end）
    if last_start >= 0:
        between = latest_text[last_start:last_restore]
        if not end_pat.search(between):
            return between, cur_seg, True
        return "", cur_seg, False

    # 2. 当前日志里没有 cs_start_education（重启游戏续玩）：倒序回溯最多 5 份旧日志
    prefix_parts = [latest_text[:last_restore]]
    found_start = False
    for old_path in reversed(logs[:-1][-5:]):
        try:
            with open(old_path, "r", encoding="utf-8", errors="ignore") as _of:
                old_txt = _of.read()
        except Exception:
            continue
        if not old_txt:
            continue

        old_starts = list(start_pat.finditer(old_txt))
        old_ends = list(end_pat.finditer(old_txt))
        last_old_start = old_starts[-1].start() if old_starts else -1
        last_old_end = old_ends[-1].start() if old_ends else -1

        if last_old_end > last_old_start:
            # 旧日志最后一局已正常结算，不再往前追溯
            break
        if last_old_start >= 0:
            prefix_parts.append(old_txt[last_old_start:])
            found_start = True
            break
        prefix_parts.append(old_txt)

    history_prefix = "\n".join(reversed(prefix_parts))
    return history_prefix, cur_seg, found_start


def watch_live(strategy_name="training_flow", auto_play=False, use_gui=True):
    if not IS_WINDOWS:
        try:
            faulthandler.enable(all_threads=True)
        except Exception:
            pass
        sys.stdout = _SafeOutputStream(sys.stdout, HUD_FALLBACK_LOG)
        sys.stderr = _SafeOutputStream(sys.stderr, HUD_FALLBACK_LOG)
        for _ign_sig in ("SIGHUP", "SIGPIPE"):
            if hasattr(signal, _ign_sig):
                try:
                    signal.signal(getattr(signal, _ign_sig), signal.SIG_IGN)
                except Exception:
                    pass
        try:
            with open(HUD_PID_FILE, "w", encoding="utf-8") as _pf:
                _pf.write(f"{os.getpid()}\n")
        except Exception:
            pass

        def _cleanup_pid_file():
            try:
                if os.path.exists(HUD_PID_FILE):
                    with open(HUD_PID_FILE, "r", encoding="utf-8") as _pf:
                        if _pf.read().strip() == str(os.getpid()):
                            os.remove(HUD_PID_FILE)
            except Exception:
                pass

        atexit.register(_cleanup_pid_file)

    strategy = load_strategy(strategy_name)
    event_db = load_event_db()
    hud_cfg = load_hud_config()
    hud_cfg_mtime = os.path.getmtime(HUD_CONFIG_FILE) if os.path.exists(HUD_CONFIG_FILE) else 0.0
    coords_cfg = None
    if auto_play:
        if not os.path.exists(COORDS_FILE):
            print("⚠️ 开启 --auto 自动代打前需要先校准一次按钮坐标！正在启动 10 秒校准向导...")
            calibrate_buttons()
        with open(COORDS_FILE, "r", encoding="utf-8") as f:
            coords_cfg = json.load(f)

    logs = sorted(glob.glob(os.path.join(LOG_DIR, "*.txt")))
    if not logs:
        print(f"未找到游戏日志（当前查找目录: {LOG_DIR}）。请先启动游戏或检查 config/local_paths.json。")
        return
    latest_log = logs[-1]
    mode_tag = "🤖 自动代打模式 (--auto)" if auto_play else "📡 实时军师 HUD 模式"
    print(f"{mode_tag} 已启动！当前流派：【{strategy['name']}】")
    disabled_init = [k for k, *_ in HUD_RECOMMENDATION_SPECS if not is_hud_rec_enabled(hud_cfg, k)]
    if disabled_init:
        print(f"⚙️ 当前已关闭的 HUD 推荐：{', '.join(disabled_init)}（可用 hud on/off/cfg 随时切换）")
    print(f"📂 正在监听最新日志：{os.path.basename(latest_log)}（原生贴身角标 + 底部深海蓝槽 HUD 已就绪）...")

    state = make_initial_state()

    hud = None
    if use_gui:
        try:
            if IS_WINDOWS:
                hud = WindowsFloatingHUD(
                    strategy,
                    auto_play,
                    STAT_NAMES,
                    get_whistle_step,
                    format_character_note_advice_hud_markup,
                    hud_config=hud_cfg,
                )
            else:
                hud = FloatingHUD(strategy, auto_play, hud_config=hud_cfg)
        except Exception as e:
            print(f"⚠️ GUI 悬浮窗初始化失败 ({e})，回退为纯终端模式。")
            hud = None

    f = open(latest_log, "r", encoding="utf-8", errors="ignore")
    tail_text = f.read()
    history_prefix, cur_seg, found_start = _collect_current_run_segments(logs, tail_text)
    full_run_seg = (history_prefix + "\n" + cur_seg) if history_prefix else cur_seg

    last_init_cmds = None
    for m in re.finditer(
        r"(\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})|request_call:\s+education\.(?:cs_train|cs_buy_edu_item)\s*\{[^\n]*\})",
        cur_seg,
        re.S,
    ):
        tok = m.group(0)
        if tok.startswith("request_call:"):
            if "cs_train" in tok:
                cm = re.search(r"cmd_idx\s*=\s*(\d+)", tok)
                if cm:
                    c_idx = int(cm.group(1))
                    if 1 <= c_idx <= 6:
                        state["train_counts"][c_idx] = state["train_counts"].get(c_idx, 0) + 1
            elif "cs_buy_edu_item" in tok:
                bm = re.search(r"store_item_id\s*=\s*(\d+)", tok)
                if bm:
                    _apply_facility_book_purchase(state, int(bm.group(1)))
            continue
        update_state_from_token(state, tok)
        if "train_cmds" in tok and "train_cmds = {}" not in tok:
            c = parse_train_cmd_block(tok)
            if len(c) == 6:
                last_init_cmds = c

    for sm in re.finditer(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", full_run_seg):
        ev_id = sm.group(1)
        state["cur_story"] = (ev_id, sm.group(2))
        if ev_id == "70":
            state["fired_70"] = True
        elif ev_id == "71":
            state["fired_71"] = True

    # 计算已完成平时行动总次数（兼顾从头开局与中途 cs_restore_education 续玩恢复的局，含平时休息 cs_decide）
    level_base_clicks = {1: 0, 2: 3, 3: 7, 4: 12, 5: 18}
    fac_total_clicks = sum(
        level_base_clicks.get(state["focus_levels"].get(k, 1), 0) + state["train_counts"].get(k, 0)
        for k in range(1, 7)
    )
    log_train_clicks = len(re.findall(r"request_call:\s+education\.cs_train", full_run_seg))
    log_rest_clicks = len(re.findall(r"request_call:\s+education\.cs_decide\s*\{decide_type\s*=\s*2\}", full_run_seg))
    if found_start:
        state["round"] = log_train_clicks + log_rest_clicks + 1
    else:
        state["round"] = max(log_train_clicks + log_rest_clicks, fac_total_clicks + log_rest_clicks) + 1

    # 检查启动瞬间是否正停留在「开局选人编队界面」、「事件二选一」、「选技能三选一」、「周末出行界面」或「训练界面」
    note_chars, note_rules, note_rel_scores, note_entries_cat = load_note_static_db()
    note_pool = sync_note_and_actor_pool(note_chars, note_entries_cat)

    pre_matches = list(
        re.finditer(
            r"request_call:\s+education\.cs_edu_initactor_record\s*\{initactor_cfg_id\s*=\s*(\d+)\}",
            tail_text,
        )
    )
    last_pre_edu_pos = pre_matches[-1].start() if pre_matches else -1
    last_start_edu_pos = max(
        tail_text.rfind("education.cs_start_education"),
        tail_text.rfind("education.cs_restore_education"),
    )
    last_end_edu_pos = tail_text.rfind("education.cs_end_education")

    # 回放当前育成段（或选人段）的精确 UI 场景栈，恢复 ycjm01/ycjm02/商店/选技能/过场锁状态
    if last_pre_edu_pos > max(last_start_edu_pos, last_end_edu_pos):
        replay_ui_state_from_text(state, tail_text[max(0, last_pre_edu_pos - 4096):])
    else:
        replay_ui_state_from_text(state, cur_seg)
        if last_init_cmds and cur_seg.rfind("train_cmds = {") > max(
            cur_seg.rfind("education.cs_train"), cur_seg.rfind("education.cs_decide")
        ):
            state["train_anim_lock"] = False

    last_choice_pos = cur_seg.rfind("[SCENARIO]进入选择节点")
    last_branch_req_pos = cur_seg.rfind("education.cs_event_branch")
    last_train_req_pos = max(cur_seg.rfind("education.cs_train"), cur_seg.rfind("education.cs_decide"))
    last_cmds_pos = cur_seg.rfind("train_cmds = {")
    last_wk_pos = cur_seg.rfind("team_building_list = {")

    cur_pre_edu_cid = None
    auto_scan_running = False
    last_rendered_skill_sig = None
    skill_state_buf = [""]
    note_pool_mtime = os.path.getmtime(os.path.join(BASE_DIR, "data", "note_pool.json")) if os.path.exists(os.path.join(BASE_DIR, "data", "note_pool.json")) else 0.0

    def trigger_skill_choice_advice(force: bool = False):
        nonlocal last_rendered_skill_sig
        sk_list = state.get("cur_skill_choices") or []
        if not sk_list:
            return
        sig = tuple(int(c.get("skill_id", 0)) for c in sk_list)
        if not force and sig == last_rendered_skill_sig:
            if hud and hasattr(hud, "refresh_scene_overlay"):
                hud.refresh_scene_overlay(state)
            return
        last_rendered_skill_sig = sig
        cid = int(state.get("main_char_id") or 0)
        evals = evaluate_skill_choices(
            cid,
            sk_list,
            learned_skills=state.get("learned_skills", set()),
            note_pool=note_pool,
        )
        roles = sorted(get_char_role_tags(cid) - {"通用"})
        role_lbl = "/".join(roles) if roles else "通用"
        print("\n" + "-" * 78)
        print(f"🎯 【育成技能三选一参谋 · 球员职责[{role_lbl}]】")
        for it in evals:
            print(
                f"  槽位#{it['slot_idx']+1} {it['badge_text']} 👉 【{it['skill_name']}】(ID:{it['skill_id']}, 评分:{it['score']}) — {it['reason']}"
            )
        print("-" * 78)
        if hud and hasattr(hud, "render_skill_choice"):
            hud.render_skill_choice(state, evals)

        if has_unknown_skills(sk_list):
            disp, wid = query_game_window_display_and_xid()
            if wid:
                def _on_skill_learned(_learned_map):
                    def _ui_refresh():
                        cur_sk = state.get("cur_skill_choices") or []
                        if not cur_sk:
                            return False
                        new_evals = evaluate_skill_choices(
                            int(state.get("main_char_id") or 0),
                            cur_sk,
                            learned_skills=state.get("learned_skills", set()),
                            note_pool=note_pool,
                        )
                        print("✨ [技能视觉自学习] 新技能已识别入库，实时热刷新三选一角标：")
                        for nit in new_evals:
                            print(
                                f"  槽位#{nit['slot_idx']+1} {nit['badge_text']} 👉 【{nit['skill_name']}】 — {nit['reason']}"
                            )
                        if hud and hasattr(hud, "render_skill_choice"):
                            hud.render_skill_choice(state, new_evals)
                        return False

                    if hud and hasattr(hud, "GLib"):
                        hud.GLib.idle_add(_ui_refresh)
                    else:
                        _ui_refresh()

                learn_unknown_skills_from_window_async(wid, sk_list, on_learned_cb=_on_skill_learned, display=disp or None)

    def trigger_hud_auto_scan():
        nonlocal auto_scan_running, note_pool, note_pool_mtime
        if auto_scan_running:
            return
        auto_scan_running = True

        def _bg():
            nonlocal auto_scan_running, note_pool, note_pool_mtime
            try:
                if hud:
                    hud.GLib.idle_add(
                        hud._update_header,
                        "<span weight='bold' foreground='#fde047'>🤖 正在自动切换【我的/好友/协会】归零并翻到底抓屏（请勿动鼠标）...</span>",
                    )
                res = scan_notes_scrolling(
                    10,
                    note_chars,
                    note_entries_cat,
                    auto_tabs=True,
                    countdown_sec=0.2,
                )
                note_pool = sync_note_and_actor_pool(note_chars, note_entries_cat)
                pool_f = os.path.join(BASE_DIR, "data", "note_pool.json")
                if os.path.exists(pool_f):
                    note_pool_mtime = os.path.getmtime(pool_f)
                if cur_pre_edu_cid and hud:
                    adv_n = recommend_notes_for_character(
                        cur_pre_edu_cid, note_chars, note_rules, note_rel_scores, note_pool
                    )
                    badge = f"✅+{res.get('updated_count', 0)}本" if res.get("ok") else "⚠️中止"
                    st_line = res.get("summary") if res.get("ok") else f"⚠️ {res.get('error', '')}"
                    hud.GLib.idle_add(hud.render_note_selection, adv_n, badge, st_line)
            finally:
                auto_scan_running = False

        threading.Thread(target=_bg, daemon=True).start()

    last_train_cmds = last_init_cmds

    def recompute_current_scene_for_config(new_cfg: dict):
        nonlocal hud_cfg, hud_cfg_mtime
        hud_cfg = new_cfg
        if os.path.exists(HUD_CONFIG_FILE):
            try:
                hud_cfg_mtime = os.path.getmtime(HUD_CONFIG_FILE)
            except Exception:
                pass
        if hud:
            hud.hud_config = hud_cfg
        try:
            cur_mode = compute_active_hud_view_mode(state)
            if cur_mode == "story_choice" and state.get("cur_story") and state["cur_story"][0]:
                ev_id_c, ev_name_c = state["cur_story"]
                adv_c = evaluate_story_event(ev_id_c, ev_name_c, state, strategy, event_db)
                if hud:
                    hud.render_story_choice(state, adv_c)
                return
            if state.get("round_type") == 2 and (state.get("gashapon_list") or state.get("mini_game_list") or state.get("weekend_train_cmds")):
                wk_c = evaluate_weekend_and_shop(state, strategy)
                if hud:
                    hud.render_weekend_advice(state, wk_c)
                return
            if state.get("round_type", 1) == 1 and last_train_cmds and len(last_train_cmds) == 6:
                scored_c = score_training_turn(state, last_train_cmds, strategy)
                shop_tip_c = format_weekday_shop_tip(
                    state, best_ev=scored_c[0]["score"], strategy=strategy, scored_cmds=scored_c
                )
                if hud:
                    hud.render_turn(state, scored_c, shop_tip_c)
                return
        except Exception as rec_err:
            print(f"⚠️ [热切换重算异常] {rec_err}")
        if hud and hasattr(hud, "set_hud_config"):
            hud.set_hud_config(hud_cfg, state=state)

    if hud:
        hud.on_auto_scan_cb = trigger_hud_auto_scan
        hud.on_focus_changed_cb = recompute_current_scene_for_config

    # 预先填充当前回合的底层训练/周末缓存（即使此刻叠加打开了商店或选技能弹窗，关弹窗瞬间也有完整数据可回显）
    try:
        if state.get("round_type") == 2 and last_wk_pos >= 0:
            wk_init = evaluate_weekend_and_shop(state, strategy)
            if hud and hasattr(hud, "_last_wk_adv"):
                hud._last_wk_adv = wk_init
        elif last_init_cmds and last_cmds_pos > last_train_req_pos:
            scored_init = score_training_turn(state, last_init_cmds, strategy)
            if hud and hasattr(hud, "_last_turn_scored"):
                hud._last_turn_scored = scored_init

        init_mode = compute_active_hud_view_mode(state)
        if last_pre_edu_pos > max(last_start_edu_pos, last_end_edu_pos) and pre_matches and state.get("pre_edu_char_id"):
            pre_cid = int(pre_matches[-1].group(1))
            cur_pre_edu_cid = pre_cid
            adv_note = recommend_notes_for_character(pre_cid, note_chars, note_rules, note_rel_scores, note_pool)
            print("\n" + format_character_note_advice_terminal(adv_note))
            if hud:
                hud.render_note_selection(adv_note, "", "")
        elif init_mode == "skill_choice" and state.get("cur_skill_choices"):
            trigger_skill_choice_advice(force=True)
        elif init_mode == "story_choice" or (last_choice_pos > max(last_branch_req_pos, last_cmds_pos, last_wk_pos) and state.get("in_story_choice")):
            ev_id, ev_name = state["cur_story"]
            adv = evaluate_story_event(ev_id, ev_name, state, strategy, event_db)
            print(f"\n📖 【事件选项决策：《{ev_name}》(ID:{ev_id})】当前精力: {state['ps']}/130")
            for ol in adv["opt_lines"]:
                print(f"  {ol}")
            print(f"  {adv['recommendation']}\n")
            if hud:
                hud.render_story_choice(state, adv)
        elif state.get("round_type") == 2 and last_wk_pos >= 0:
            wk_adv = evaluate_weekend_and_shop(state, strategy)
            print_weekend_advice(state, wk_adv)
            if hud:
                hud.render_weekend_advice(state, wk_adv)
        elif last_init_cmds and last_cmds_pos > last_train_req_pos:
            scored = score_training_turn(state, last_init_cmds, strategy)
            shop_tip = format_weekday_shop_tip(state, best_ev=scored[0]["score"], strategy=strategy, scored_cmds=scored)
            if hud:
                hud.render_turn(state, scored, shop_tip)
        elif hud and hasattr(hud, "refresh_scene_overlay"):
            hud.refresh_scene_overlay(state)
    except Exception as init_err:
        print(f"⚠️ [星奈军师] 启动初始面板恢复异常（已忽略并继续监听）: {init_err}")

    buf = ""
    pending_event_learn = None  # {ev_id, ev_name, br_idx, snap_ps, snap_gold, snap_props, snap_max, eff_id}

    def finalize_event_learn():
        nonlocal pending_event_learn
        if not pending_event_learn:
            return
        info = pending_event_learn
        pending_event_learn = None
        ev_id = str(info["ev_id"])
        ev_name = info["ev_name"]
        # R10 集训、R36 许愿、角色/经理技能事件 100% 由通用模板托管，禁止按角色重复回写脏条目
        if classify_shared_story_event(ev_id, ev_name, state=state) in SHARED_TEMPLATE_NO_PERSIST_TYPES:
            return
        br_idx = str(info["br_idx"])
        d_ps = state["ps"] - info["snap_ps"]
        d_gold = state.get("gold", 0) - info["snap_gold"]
        d_props = {
            str(k): state["props"][k] - info["snap_props"].get(k, 0)
            for k in state["props"]
            if 0 < abs(state["props"][k] - info["snap_props"].get(k, 0)) < 250
        }
        d_max = {
            str(k): state["prop_max"][k] - info["snap_max"].get(k, 0)
            for k in state["prop_max"]
            if 0 < abs(state["prop_max"][k] - info["snap_max"].get(k, 0)) < 250
        }
        rec = event_db.setdefault(ev_id, {"name": ev_name, "branches": {}})
        rec["name"] = ev_name
        br_entry = rec["branches"].setdefault(
            br_idx,
            {"count": 0, "effect_id": info.get("eff_id"), "ps": 0, "coins": 0, "props": {}, "max_props": {}},
        )
        br_entry["count"] = int(br_entry.get("count", 0)) + 1
        if info.get("eff_id") is not None:
            br_entry["effect_id"] = info["eff_id"]
        has_any_gain = bool(
            br_entry.get("props")
            or br_entry.get("random_prop")
            or br_entry.get("max_props")
            or br_entry.get("random_max_prop")
        )
        if d_ps < 0 and not d_props and not d_max and has_any_gain:
            br_entry["fail_ps"] = min(int(br_entry.get("fail_ps", 0) or 0), d_ps)
            br_entry["ps"] = 0
            br_entry.setdefault("win_rate", 0.50)
        elif d_ps == 0 and (d_props or d_max) and int(br_entry.get("ps", 0) or 0) < 0 and not br_entry.get("props") and not br_entry.get("random_prop"):
            br_entry["fail_ps"] = int(br_entry["ps"])
            br_entry["ps"] = 0
            br_entry.setdefault("win_rate", 0.50)
        elif d_ps != 0 and abs(d_ps) > abs(br_entry.get("ps", 0)) and not br_entry.get("fail_ps"):
            br_entry["ps"] = d_ps
        if d_gold != 0 and abs(d_gold) > abs(br_entry.get("coins", 0)):
            br_entry["coins"] = d_gold
        if len(d_props) == 1 and "许愿" not in ev_name:
            pk, pv = next(iter(d_props.items()))
            if pv > 0 and (br_entry.get("random_prop") or (br_entry.get("props") and pk not in br_entry["props"])):
                prev_p = max((abs(int(v)) for v in br_entry.get("props", {}).values()), default=0)
                br_entry["random_prop"] = max(int(br_entry.get("random_prop", 0) or 0), prev_p, pv)
                br_entry["props"] = {}
                br_entry.pop("est_stat_ev", None)
            elif abs(pv) > abs(br_entry.setdefault("props", {}).get(pk, 0)):
                br_entry["props"][pk] = pv
                br_entry.pop("est_stat_ev", None)
        else:
            for pk, pv in d_props.items():
                if abs(pv) > abs(br_entry.setdefault("props", {}).get(pk, 0)):
                    br_entry["props"][pk] = pv
                    br_entry.pop("est_stat_ev", None)

        if len(d_max) == 1:
            mk, mv = next(iter(d_max.items()))
            eid_int = int(ev_id) if ev_id.isdigit() else 0
            if mv > 0 and (
                eid_int in (70, 71)
                or br_entry.get("random_max_prop")
                or (br_entry.get("max_props") and mk not in br_entry["max_props"])
            ):
                prev_m = max((abs(int(v)) for v in br_entry.get("max_props", {}).values()), default=0)
                br_entry["random_max_prop"] = max(int(br_entry.get("random_max_prop", 0) or 0), prev_m, mv)
                br_entry["max_props"] = {}
                br_entry.pop("est_stat_ev", None)
            elif abs(mv) > abs(br_entry.setdefault("max_props", {}).get(mk, 0)):
                br_entry["max_props"][mk] = mv
                br_entry.pop("est_stat_ev", None)
        else:
            for mk, mv in d_max.items():
                if abs(mv) > abs(br_entry.setdefault("max_props", {}).get(mk, 0)):
                    br_entry["max_props"][mk] = mv
                    br_entry.pop("est_stat_ev", None)
        save_event_db(event_db)

    def poll_log():
        nonlocal f, latest_log, buf, pending_event_learn
        nonlocal cur_pre_edu_cid, note_pool, note_pool_mtime, last_rendered_skill_sig
        nonlocal hud_cfg, hud_cfg_mtime, last_train_cmds
        if hud and getattr(hud, "_closing", False):
            return False
        try:
            cur_cfg_mtime = os.path.getmtime(HUD_CONFIG_FILE) if os.path.exists(HUD_CONFIG_FILE) else 0.0
            if cur_cfg_mtime != hud_cfg_mtime:
                hud_cfg_mtime = cur_cfg_mtime
                hud_cfg = load_hud_config()
                disabled_k = [k for k, *_ in HUD_RECOMMENDATION_SPECS if not is_hud_rec_enabled(hud_cfg, k)]
                fc_now = _normalize_focus_dict(hud_cfg.get("focus"))
                fc_desc = f"🎯{STAT_NAMES.get(fc_now['stat'], fc_now['stat'])}特化(×{fc_now['bonus']:.2f})" if fc_now["stat"] else "纯评分模式"
                print(f"\n⚙️ [HUD配置热更新] 模式: {fc_desc} | 已关闭推荐: {', '.join(disabled_k) if disabled_k else '无（全部开启）'}")
                recompute_current_scene_for_config(hud_cfg)
            newest_logs = sorted(glob.glob(os.path.join(LOG_DIR, "*.txt")))
            if newest_logs and newest_logs[-1] != latest_log:
                latest_log = newest_logs[-1]
                print(f"\n🔄 检测到新开启的游戏日志：{os.path.basename(latest_log)}，已自动切换监听！")
                f.close()
                f = open(latest_log, "r", encoding="utf-8", errors="ignore")
                buf = ""
                skill_state_buf[0] = ""

            ui_mode_dirty = False

            while True:
                line = f.readline()
                if not line:
                    break
                if "[EDU_MODEL] 更新育成数据" in line:
                    buf = line
                elif buf:
                    buf += line
                    if len(buf) > 250000:
                        buf = ""

                ui_ev = update_ui_state_from_line(state, line, skill_state_buf)
                if ui_ev["changed"]:
                    ui_mode_dirty = True
                if not state.get("cur_skill_choices"):
                    last_rendered_skill_sig = None
                elif ui_ev["skill_popup_ready"] and compute_active_hud_view_mode(state) == "skill_choice":
                    trigger_skill_choice_advice(force=False)
                    ui_mode_dirty = False

                if "request_call:" in line and "education.cs_edu_initactor_record" in line:
                    pre_m = re.search(r"initactor_cfg_id\s*=\s*(\d+)", line)
                    if pre_m:
                        pre_cid = int(pre_m.group(1))
                        cur_pre_edu_cid = pre_cid
                        state["pre_edu_char_id"] = pre_cid
                        note_pool = sync_note_and_actor_pool(note_chars, note_entries_cat)
                        pool_f = os.path.join(BASE_DIR, "data", "note_pool.json")
                        if os.path.exists(pool_f):
                            note_pool_mtime = os.path.getmtime(pool_f)
                        adv_note = recommend_notes_for_character(pre_cid, note_chars, note_rules, note_rel_scores, note_pool)
                        print("\n" + format_character_note_advice_terminal(adv_note))
                        if hud:
                            hud.render_note_selection(adv_note, "", "")
                            ui_mode_dirty = False

                if "request_call:" in line and "education.cs_start_education" in line:
                    cur_pre_edu_cid = None
                    pending_event_learn = None
                    last_rendered_skill_sig = None
                    skill_state_buf[0] = ""
                    fresh = make_initial_state()
                    state.clear()
                    state.update(fresh)
                    print("\n🎉 检测到新的一局育成开始！状态已重置。")
                    if hud:
                        if hasattr(hud, "hide_all_badges"):
                            hud.hide_all_badges()
                        hud._update_header(
                            "<span weight='bold' foreground='#fbbf24'>🏀 星奈 · 实时军师</span>"
                        )
                        hud.set_markup(
                            "<span foreground='#4ade80' weight='bold'>🎉 新局开启，等待第 01 训面板...</span>",
                            mini_summary="🏀 新局待命",
                        )
                        if hasattr(hud, "refresh_scene_overlay"):
                            hud.refresh_scene_overlay(state)
                        ui_mode_dirty = False

                if ("request_call:" in line and "education.cs_end_education" in line) or (
                    "new_rank =" in line and re.search(r"new_rank\s*=\s*(?:[3-9]\d{3}|1\d{4})\b", line)
                ):
                    rk_m = re.search(r"new_rank\s*=\s*(\d+)", line)
                    if rk_m and not state.get("_settled_archived"):
                        state["_settled_archived"] = True
                        settled_rk = int(rk_m.group(1))
                        def _bg_archive(rk_val=settled_rk):
                            try:
                                import analyzer
                                runs = analyzer.extract_all_runs(strategy_name=strategy_name, force_rebuild=False)
                                latest_r = max(runs, key=lambda x: x.get("seq_no", 0)) if runs else None
                                r_id = latest_r.get("run_id", "") if latest_r else ""
                                print(
                                    f"\n💾 [战绩自动归档] 本局（最终评分 {rk_val}）已自动存入 data/runs/archive/{r_id}.json 及 detailed_runs.json！"
                                )
                            except Exception as ex:
                                print(f"⚠️ [战绩自动归档] 归档异常: {ex}")
                        threading.Thread(target=_bg_archive, daemon=True).start()

                if "request_call:" in line and "education.cs_train" in line:
                    cm_t = re.search(r"cmd_idx\s*=\s*(\d+)", line)
                    if cm_t:
                        c_idx = int(cm_t.group(1))
                        if 1 <= c_idx <= 6:
                            state["train_counts"][c_idx] = state["train_counts"].get(c_idx, 0) + 1
                    state["round"] = state.get("round", 1) + 1

                if "request_call:" in line and "education.cs_decide" in line and "decide_type = 2" in line:
                    state["round"] = state.get("round", 1) + 1

                if "request_call:" in line and "education.cs_buy_edu_item" in line:
                    bm_i = re.search(r"store_item_id\s*=\s*(\d+)", line)
                    if bm_i:
                        _apply_facility_book_purchase(state, int(bm_i.group(1)))

                if "[EDU_MODEL]_LoadStory:" in line:
                    finalize_event_learn()
                    sm = re.search(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", line)
                    if sm:
                        ev_id = sm.group(1)
                        state["cur_story"] = (ev_id, sm.group(2))
                        if ev_id == "70":
                            state["fired_70"] = True
                        elif ev_id == "71":
                            state["fired_71"] = True

                if "request_call:" in line and "education.cs_event_branch" in line:
                    bm = re.search(r"branch_idx\s*=\s*(\d+)", line)
                    if bm and state.get("cur_story"):
                        pending_event_learn = {
                            "ev_id": state["cur_story"][0],
                            "ev_name": state["cur_story"][1],
                            "br_idx": bm.group(1),
                            "snap_ps": state["ps"],
                            "snap_gold": state.get("gold", 0),
                            "snap_props": dict(state["props"]),
                            "snap_max": dict(state["prop_max"]),
                            "eff_id": None,
                        }

                if "[SCENARIO]触发事件效果:" in line and pending_event_learn:
                    efm = re.search(r"\[SCENARIO\]触发事件效果:\s*(\d+)", line)
                    if efm:
                        pending_event_learn["eff_id"] = int(efm.group(1))

                if "[EDU_MODEL]_SwitchState 离开状态:" in line and re.search(r"离开状态:\s+2\b", line):
                    finalize_event_learn()

                if "[SCENARIO]进入选择节点" in line:
                    ev_id, ev_name = state["cur_story"]
                    adv = evaluate_story_event(ev_id, ev_name, state, strategy, event_db)
                    print("\n" + "-" * 78)
                    print(f"📖 【事件选项决策：《{ev_name}》(ID:{ev_id})】当前精力: {state['ps']}/130")
                    for ol in adv["opt_lines"]:
                        print(f"  {ol}")
                    print(f"  {adv['recommendation']}")
                    print("-" * 78)
                    if hud:
                        hud.render_story_choice(state, adv)
                        ui_mode_dirty = False

                em_match = (
                    re.search(r"\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})\s*$", buf, re.S)
                    if (buf and line.rstrip().endswith("}"))
                    else None
                )
                if em_match:
                    tok = em_match.group(0)
                    buf = ""
                    prev_m_tok = compute_active_hud_view_mode(state)
                    update_state_from_token(state, tok)
                    if compute_active_hud_view_mode(state) != prev_m_tok:
                        ui_mode_dirty = True

                    # 周末出行回合：当收到扭蛋/街机池下发，或在周末期间购买了商店道具引起货架/体力/金币/周六加训面板变化时，实时刷新周末参谋面板
                    if state.get("round_type") == 2 and (
                        "team_building_list" in tok
                        or "edu_mini_games_state_data" in tok
                        or ("edu_store_info" in tok and "items_data" in tok)
                        or ("train_cmds" in tok and "train_cmds = {}" not in tok and bool(state.get("gashapon_list")))
                    ):
                        wk_adv = evaluate_weekend_and_shop(state, strategy)
                        title_p = (
                            "🛒 【周末商店购买后 · 实时重算参谋】"
                            if "edu_store_info" in tok and "team_building_list" not in tok
                            else ("⚡ 【周六加训 / 外出 & 商店全局参谋】" if wk_adv.get("has_weekend_train") else "🌴 【周末出行 & 商店全局参谋】")
                        )
                        print_weekend_advice(state, wk_adv, title_prefix=title_p)
                        if hud:
                            hud.render_weekend_advice(state, wk_adv)
                            ui_mode_dirty = False

                    if state.get("round_type", 1) == 1 and "train_cmds" in tok and "train_cmds = {}" not in tok:
                        cmds = parse_train_cmd_block(tok)
                        if len(cmds) == 6:
                            last_train_cmds = cmds
                            is_shop_refresh = "edu_store_info" in tok and "round_type" not in tok
                            scored = score_training_turn(state, cmds, strategy)
                            shop_tip = format_weekday_shop_tip(
                                state, best_ev=scored[0]["score"], strategy=strategy, scored_cmds=scored
                            )
                            _, w3_badge = get_whistle_step(state)
                            props_str = " ".join(f"{STAT_NAMES[k]}:{v}" for k, v in sorted(state["props"].items()))
                            tag_prefix = "🛒 【商店购买后面板重算 · " if is_shop_refresh else "🏀 【"
                            print("\n" + "=" * 84)
                            print(
                                f"{tag_prefix}第 {state['round']:02d} 训】体力:{state['ps']} | 金币:{state.get('gold', 0)} | 经理:{state['mgr_energy']}/3格 已爆:{state['mgr_explosive']}次 ({w3_badge})"
                            )
                            print(f"📊 当前六维：{props_str}")
                            for rank_i, item in enumerate(scored[:3], 1):
                                mark = "👑 首选" if rank_i == 1 else f"#{rank_i} 备选"
                                print(f"  {mark} 👉 【{item['name']}】 (EV: {item['score']}) — {item['reasons']}")
                            if shop_tip:
                                print(f"  {shop_tip}")
                            print("=" * 84)

                            if hud:
                                hud.render_turn(state, scored, shop_tip)
                                ui_mode_dirty = False

                            if auto_play and coords_cfg and not is_shop_refresh:
                                auto_click_training(scored[0]["idx"], coords_cfg)

            if hud and hasattr(hud, "refresh_scene_overlay"):
                final_batch_mode = compute_active_hud_view_mode(state)
                mode_desynced = final_batch_mode != getattr(hud, "_active_view_mode", None)
                if ui_mode_dirty or mode_desynced:
                    if final_batch_mode == "skill_choice" and state.get("cur_skill_choices"):
                        trigger_skill_choice_advice(force=False)
                    else:
                        hud.refresh_scene_overlay(state)

            if cur_pre_edu_cid:
                pool_f = os.path.join(BASE_DIR, "data", "note_pool.json")
                if os.path.exists(pool_f):
                    cur_mtime = os.path.getmtime(pool_f)
                    if cur_mtime > note_pool_mtime + 0.05:
                        note_pool_mtime = cur_mtime
                        note_pool = sync_note_and_actor_pool(note_chars, note_entries_cat)
                        adv_note = recommend_notes_for_character(
                            cur_pre_edu_cid, note_chars, note_rules, note_rel_scores, note_pool
                        )
                        print("\n" + format_character_note_advice_terminal(adv_note))
                        if hud:
                            hud.render_note_selection(adv_note, "✅库存已刷新", "")
        except Exception as e:
            print(f"⚠️ [星奈军师] 实时日志解析异常（已自动恢复继续监听）: {e}")
        return True

    if hud:
        def _graceful_quit(*_args):
            print(f"\n🛑 [{time.strftime('%H:%M:%S')}] 收到退出信号，正在优雅卸载星奈 HUD...")
            if hud:
                hud.close_cleanly(quit_main_loop=True)
            return False

        atexit.register(lambda: hud.close_cleanly(quit_main_loop=False))
        if not IS_WINDOWS and hasattr(hud.GLib, "unix_signal_add"):
            for _qsig in (signal.SIGTERM, signal.SIGINT):
                try:
                    hud.GLib.unix_signal_add(hud.GLib.PRIORITY_HIGH, int(_qsig), _graceful_quit)
                except Exception:
                    signal.signal(_qsig, _graceful_quit)
        else:
            for _qsig in (signal.SIGTERM, signal.SIGINT):
                try:
                    signal.signal(_qsig, _graceful_quit)
                except Exception:
                    pass

        hud.GLib.timeout_add(100, poll_log)
        try:
            hud.Gtk.main()
        except KeyboardInterrupt:
            pass
        except Exception as main_err:
            import traceback
            print(f"\n❌ [{time.strftime('%H:%M:%S')}] Gtk.main 异常退出: {main_err}")
            traceback.print_exc()
        finally:
            hud.close_cleanly(quit_main_loop=False)
            try:
                f.close()
            except Exception:
                pass
    else:
        stop_loop = False

        def _stop_cli(*_args):
            nonlocal stop_loop
            stop_loop = True

        for _qsig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(_qsig, _stop_cli)
            except Exception:
                pass
        try:
            while not stop_loop:
                poll_log()
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="training_flow", help="流派策略文件名（不含.json）")
    parser.add_argument("--replay-latest", nargs="?", const=1, type=int, metavar="N", help="复盘倒数第 N 局育成决策（默认 1 为最近一局）")
    parser.add_argument("--calibrate", action="store_true", help="10秒校准游戏窗口中 1~6 号训练键的屏幕坐标")
    parser.add_argument("--auto", action="store_true", help="开启自动代打模式（由星奈通过 xdotool 自动点击最优训练键）")
    parser.add_argument("--no-gui", action="store_true", help="仅使用终端输出，不弹出屏幕置顶悬浮窗")
    parser.add_argument("--config", action="store_true", help="查看当前 HUD 推荐开关状态")
    parser.add_argument("--on", nargs="*", metavar="ITEM", help="开启指定 HUD 推荐项并保存（支持中英文名或 all）")
    parser.add_argument("--off", nargs="*", metavar="ITEM", help="关闭指定 HUD 推荐项并保存（支持中英文名或 all）")
    parser.add_argument("--toggle", nargs="*", metavar="ITEM", help="反转指定 HUD 推荐项开关并保存")
    parser.add_argument("--only", nargs="*", metavar="ITEM", help="仅开启指定 HUD 推荐项、其余全部关闭并保存")
    parser.add_argument("--focus", nargs="*", metavar="STAT", help="开启/关闭专项属性特化模式（如: --focus 投射 或 --focus off）")
    parser.add_argument("--reset-config", action="store_true", help="重置 HUD 推荐开关为全部开启")
    args = parser.parse_args()

    if args.reset_config:
        sys.exit(apply_hud_config_cli_action("reset"))
    elif args.focus is not None:
        sys.exit(apply_hud_focus_cli_action(args.focus))
    elif args.on is not None:
        sys.exit(apply_hud_config_cli_action("on", args.on))
    elif args.off is not None:
        sys.exit(apply_hud_config_cli_action("off", args.off))
    elif args.toggle is not None:
        sys.exit(apply_hud_config_cli_action("toggle", args.toggle))
    elif args.only is not None:
        sys.exit(apply_hud_config_cli_action("only", args.only))
    elif args.config:
        sys.exit(apply_hud_config_cli_action("show"))
    elif args.calibrate:
        calibrate_buttons()
    elif args.replay_latest is not None:
        replay_last_run(args.strategy, run_offset=args.replay_latest)
    else:
        watch_live(args.strategy, auto_play=args.auto, use_gui=not args.no_gui)
