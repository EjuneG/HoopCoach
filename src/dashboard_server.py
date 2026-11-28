#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LQSNCoach 私人作战室本地后端服务与静态快照导出器 (src/dashboard_server.py)

定位：
- 仅服务于本地个人账号的战绩复盘、实盘笔记/器材配对与后台挂机实时监控；
- 严格只读访问本地归档与游戏明文日志（零内存注入、零进程侵入）；
- 复盘与配对 100% 复用 note_advisor / equipment_advisor / live_coach / auto_driver 真值引擎。

用法：
  python3 src/dashboard_server.py                 # 启动本地服务 http://127.0.0.1:8765
  python3 src/dashboard_server.py --port 8765     # 指定端口
  python3 src/dashboard_server.py --open          # 启动后自动打开浏览器
  python3 src/dashboard_server.py --export-static # 仅导出离线快照 dashboard/data/snapshot.js
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import equipment_advisor
import live_coach as lc
import note_advisor
import platform_compat
from log_mirror import LiveLogMirror

try:
    import decision_bridge
except ImportError:
    decision_bridge = None

BASE_DIR = platform_compat.BASE_DIR
DATA_DIR = os.path.join(BASE_DIR, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
ARCHIVE_DIR = os.path.join(RUNS_DIR, "archive")
HEADS_DIR = os.path.join(DATA_DIR, "hoopgirls", "heads")
EQUIP_ICONS_DIR = os.path.join(DATA_DIR, "hoopgirls", "equipment_icons")
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")
DASHBOARD_DATA_DIR = os.path.join(DASHBOARD_DIR, "data")
DOCS_DIR = os.path.join(BASE_DIR, "docs")

DETAILED_RUNS_FILE = os.path.join(RUNS_DIR, "detailed_runs.json")
NOTE_POOL_FILE = os.path.join(DATA_DIR, "note_pool.json")
EQUIP_POOL_FILE = os.path.join(DATA_DIR, "equip_pool.json")
DAILY_QUEUE_FILE = os.path.join(DATA_DIR, "daily_queue.json")
ACCOUNT_SNAPSHOT_FILE = os.path.join(DATA_DIR, "account_snapshot.json")
ACCOUNT_STATE_MD = os.path.join(BASE_DIR, "ai", "account_state.md")
HUD_CONFIG_FILE = os.path.join(BASE_DIR, "config", "hud_config.json")

STAT_IDS = [101, 102, 103, 104, 105, 106]
STAT_NAMES = {
    101: "投射",
    102: "弹跳",
    103: "防守",
    104: "体能",
    105: "运控",
    106: "意识",
}
POS_ORDER = ["C", "PF", "SF", "SG", "PG"]
POS_CN = {"C": "中锋", "PF": "大前", "SF": "小前", "SG": "分卫", "PG": "控卫"}

# 全 38 名实装球员的拼音/首字母极速检索词（零第三方依赖）
CHAR_PINYIN_HINTS: dict[int, str] = {
    1001: "lingnai ln jianisi jns beikong bk",
    1002: "longgongsi lgs xinye xy beikong bk",
    1003: "jinjiang jj zou z beikong bk",
    1004: "hequan hq xiangzhi xz beikong bk",
    1005: "yalisha yls jiasimin jsm beikong bk",
    1006: "mingyao my yicheng yc",
    1007: "shantang st shantangdian std beikong bk",
    1009: "kelalisi klls boluo bl yicheng yc",
    1010: "zaolai zl qianqiu qq yicheng yc",
    1011: "yutian yt zhuzhixiang zzx yicheng yc",
    1012: "wangyue wy tou t yicheng yc",
    1013: "baimugui bmg jian j yinghai yh",
    1014: "tengsong ts jiulian jl yinghai yh",
    1015: "lvchuan lc luchuan bumeng bm yinghai yh",
    1016: "yusen ys liuli ll cangqi cq",
    1017: "jiechuan jc shi s mingriye mry",
    1018: "dudao dd daiye dy mingriye mry",
    1019: "mingxue mx shaye sy mingriye mry",
    1020: "zuojiujian zjj yunai yn mingriye mry",
    1021: "qiansheng qs beiersi bes mingriye mry",
    1022: "daoye dy xingli xl yinghai yh",
    1023: "qingshui qs wanli wl shengfeiluosi sfls",
    1024: "jiaben jb chunna cn shengfeiluosi sfls",
    1025: "chuncheng cc yongyuan yy shengfeiluosi sfls",
    1026: "sentian st lili ll beikong bk",
    1027: "meijia mj weilai wl beikong bk",
    1028: "wushilan wsl shiyin sy beikong bk",
    1029: "yulaichuan ylc yusechuan ysc cou c shengfeiluosi sfls",
    1030: "zhenqi zq huishi hs shengfeiluosi sfls",
    1032: "xiandou xd yueqi yq cangqi cq",
    1033: "woyun wy zhenzhou zz cangqi cq",
    1037: "yeshang ys long l shengshan ss",
    1038: "jiangqi jq qianhongsha qhs yeming ym",
    1039: "linyuan ly shuli sl qinglan ql",
    1040: "meixing mx liqi lq qinglan ql",
    1042: "yuanye yy tong t hongmeng hm",
    1045: "baishen bs shijiu sj shengshan ss",
    1046: "xiya xy aodaila adl shengshan ss",
}


def _safe_read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _build_head_map() -> dict[int, str]:
    mapping: dict[int, str] = {}
    if not os.path.isdir(HEADS_DIR):
        return mapping
    for fn in os.listdir(HEADS_DIR):
        m = re.match(r"portrait_head_(\d+)_.*\.png$", fn)
        if m:
            mapping[int(m.group(1))] = fn
    return mapping


def _build_equip_icon_map() -> dict[int, str]:
    mapping: dict[int, str] = {}
    if not os.path.isdir(EQUIP_ICONS_DIR):
        return mapping
    for fn in os.listdir(EQUIP_ICONS_DIR):
        m = re.match(r"(\d+)_.*\.png$", fn)
        if m:
            mapping[int(m.group(1))] = fn
    return mapping


def _head_url(char_id: Optional[int], head_map: dict[int, str]) -> str:
    if not char_id:
        return ""
    fn = head_map.get(int(char_id))
    return f"/assets/heads/{urllib.parse.quote(fn)}" if fn else ""


def _equip_icon_url(cfg_id: Optional[int], icon_map: dict[int, str], icon_file: str = "") -> str:
    fn = icon_file or (icon_map.get(int(cfg_id), "") if cfg_id else "")
    return f"/assets/equips/{urllib.parse.quote(fn)}" if fn else ""


def _parse_account_diamonds() -> dict[str, str]:
    res = {"diamonds": "", "trust_level": "10 级", "updated_at": ""}
    if not os.path.exists(ACCOUNT_STATE_MD):
        return res
    try:
        with open(ACCOUNT_STATE_MD, encoding="utf-8") as f:
            text = f.read()
        m_dia = re.search(r"\*\*钻石\*\*\s*\|\s*\*\*([0-9,]+)\*\*", text)
        if m_dia:
            res["diamonds"] = m_dia.group(1)
        m_upd = re.search(r"\*\*最后更新\*\*[：:]\s*([0-9\-]+)", text)
        if m_upd:
            res["updated_at"] = m_upd.group(1)
    except Exception:
        pass
    return res


def _parse_run_date_label(run_id: str, log_name: str = "") -> str:
    src = run_id or log_name
    m = re.match(r"(\d{2})_(\d{2})___(\d{2})_(\d{2})", src)
    if m:
        return f"{m.group(1)}-{m.group(2)} {m.group(3)}:{m.group(4)}"
    return src[:16]


def _parse_run_sort_key(run_id: str, log_name: str = "", run_index: int = 0) -> tuple:
    src = run_id or log_name
    m = re.match(r"(\d{2})_(\d{2})___(\d{2})_(\d{2})_(\d{2})(?:_r(\d+))?", src)
    if m:
        mo, dy, hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        ridx = int(m.group(6)) if m.group(6) else int(run_index or 0)
        # 兼容跨年（若出现 01~03 月接在 10~12 月之后，这里统一按 10~12 -> 01~09 序）
        yr_ord = 0 if mo >= 9 else 1
        return (yr_ord, mo, dy, hh, mm, ss, ridx, src)
    return (9, 99, 99, 99, 99, 99, int(run_index or 0), src)


def _extract_main_name(lineup: str) -> str:
    if not lineup:
        return "未知"
    s = lineup.strip()
    m = re.match(r"⭐?\s*([^+/\s(（]+)", s)
    return m.group(1) if m else s.split("+")[0].replace("⭐", "").strip()


class DashboardEngine:
    """带文件 mtime 感知的本地作战室聚合引擎。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.head_map = _build_head_map()
        self.equip_icon_map = _build_equip_icon_map()
        self.chars, self.rules, self.rel_scores, self.entries_catalog = note_advisor.load_static_db()
        self.equip_db, _, _ = equipment_advisor.load_equip_data()

        self.name_to_char_id: dict[str, int] = {}
        for cid, c in self.chars.items():
            for alias in (
                c.get("short_name"),
                c.get("show_name"),
                *(c.get("aliases") or []),
            ):
                if alias:
                    self.name_to_char_id[str(alias).strip()] = cid

        self._runs_cache: list[dict] = []
        self._runs_mtime: float = -1.0
        self._archive_index: dict[str, str] = {}
        self._archive_dir_mtime: float = -1.0
        self._pool_sync_lock = threading.Lock()
        self.docs_dir: Optional[str] = None
        self.log_dir: Optional[str] = None
        self.strategy_name: str = "training_flow"

    def set_game_dirs(self, docs_dir: Optional[str] = None, log_dir: Optional[str] = None) -> None:
        if docs_dir:
            self.docs_dir = docs_dir
        if log_dir:
            self.log_dir = log_dir

    def _char_pos_code(self, cid: Optional[int]) -> str:
        if not cid or cid not in self.chars:
            return ""
        bp = str(self.chars[cid].get("best_pos") or "").strip()
        return bp.split()[0] if bp else ""

    def _resolve_char_id(self, name: str) -> Optional[int]:
        if not name:
            return None
        if name in self.name_to_char_id:
            return self.name_to_char_id[name]
        for k, cid in self.name_to_char_id.items():
            if name in k or k in name:
                return cid
        return None

    def _get_runs_enriched(self) -> list[dict]:
        mt = _file_mtime(DETAILED_RUNS_FILE)
        with self._lock:
            if mt == self._runs_mtime and self._runs_cache:
                return self._runs_cache

            raw = _safe_read_json(DETAILED_RUNS_FILE, [])
            if not isinstance(raw, list):
                raw = []

            enriched = []
            for idx, r in enumerate(raw):
                main_name = _extract_main_name(str(r.get("lineup") or ""))
                cid = self._resolve_char_id(main_name)
                pos = self._char_pos_code(cid)
                run_id = str(r.get("run_id") or f"run_{idx}")
                fstats = r.get("final_stats") or {}
                pmax = r.get("prop_max") or {}
                stats_clean = {str(sid): int(fstats.get(str(sid), fstats.get(sid, 0)) or 0) for sid in STAT_IDS}
                caps_clean = {str(sid): int(pmax.get(str(sid), pmax.get(sid, 1500)) or 1500) for sid in STAT_IDS}

                enriched.append({
                    "seq_no": int(r.get("seq_no") or (idx + 1)),
                    "run_id": run_id,
                    "date_label": _parse_run_date_label(run_id, str(r.get("log") or "")),
                    "log": r.get("log", ""),
                    "final_rank": int(r.get("final_rank") or 0),
                    "lineup": r.get("lineup", ""),
                    "main_name": main_name,
                    "main_char_id": cid,
                    "main_pos": pos,
                    "main_head": _head_url(cid, self.head_map),
                    "final_stats": stats_clean,
                    "prop_max": caps_clean,
                    "stat_sum": int(r.get("stat_sum") or sum(stats_clean.values())),
                    "min_stat": int(r.get("min_stat") or (min(stats_clean.values()) if stats_clean else 0)),
                    "max_stat": int(r.get("max_stat") or (max(stats_clean.values()) if stats_clean else 0)),
                    "capped_stats": list(r.get("capped_stats") or []),
                    "short_stats": list(r.get("short_stats") or []),
                    "mgr_explosive": int(r.get("mgr_explosive") or 0),
                    "mgr_max_explosive": int(r.get("mgr_max_explosive") or 0),
                    "equip_max_turns": r.get("equip_max_turns") or {},
                    "crit_equip_total": int(r.get("crit_equip_total") or 0),
                    "crit_maxed_total": int(r.get("crit_maxed_total") or 0),
                    "crit_maxed_by_t12": int(r.get("crit_maxed_by_t12") or 0),
                    "crit_maxed_by_t15": int(r.get("crit_maxed_by_t15") or 0),
                    "focus_levels": r.get("focus_levels") or {},
                    "train_count": int(r.get("train_count") or 0),
                    "coach_match_pct": float(r.get("coach_match_pct") or 0.0),
                    "coach_top2_pct": float(r.get("coach_top2_pct") or 0.0),
                    "avg_turn_ev_loss": float(r.get("avg_turn_ev_loss") or 0.0),
                    "weekend_count": int(r.get("weekend_count") or 0),
                    "weekend_match_pct": float(r.get("weekend_match_pct") or 0.0),
                    "store_buys": int(r.get("store_buys") or 0),
                    "slot_machines": int(r.get("slot_machines") or 0),
                    "note_scores": list(r.get("note_scores") or []),
                })

            # 统一按真实对局时间升序排列（修复历史补扫日志 seq_no 乱序），并重编展示序号
            enriched.sort(key=lambda x: _parse_run_sort_key(x["run_id"], x.get("log", "")))
            for i, item in enumerate(enriched, start=1):
                item["seq_no"] = i
            self._runs_cache = enriched
            self._runs_mtime = mt
            return enriched

    def _get_archive_index(self) -> dict[str, str]:
        mt = _file_mtime(ARCHIVE_DIR)
        with self._lock:
            if mt == self._archive_dir_mtime and self._archive_index:
                return self._archive_index
            idx: dict[str, str] = {}
            if os.path.isdir(ARCHIVE_DIR):
                for fp in glob.glob(os.path.join(ARCHIVE_DIR, "*.json")):
                    base = os.path.splitext(os.path.basename(fp))[0]
                    idx[base] = fp
                    try:
                        with open(fp, encoding="utf-8") as f:
                            head_txt = f.read(512)
                        m = re.search(r'"run_id"\s*:\s*"([^"]+)"', head_txt)
                        if m:
                            idx[m.group(1)] = fp
                    except Exception:
                        pass
            self._archive_index = idx
            self._archive_dir_mtime = mt
            return idx

    def invalidate_caches(self) -> None:
        with self._lock:
            self._runs_mtime = -1.0
            self._archive_dir_mtime = -1.0
            self.equip_db, _, _ = equipment_advisor.load_equip_data(force_reload=True)

    def sync_local_pools(
        self,
        docs_dir: Optional[str] = None,
        log_dir: Optional[str] = None,
        force_rescan: bool = False,
    ) -> dict[str, Any]:
        """从本地游戏存档 (ob_*) 与近期日志增量同步已有角色、笔记池与器材池。"""
        with self._pool_sync_lock:
            res: dict[str, Any] = {"ok": False, "owned_actors": 0, "own_notes": 0, "owned_equips": 0}
            try:
                note_pool = note_advisor.sync_note_and_actor_pool(
                    self.chars,
                    self.entries_catalog,
                    force_rescan=force_rescan,
                    docs_dir=docs_dir,
                    log_dir=log_dir,
                )
                res["owned_actors"] = len(note_pool.get("owned_actors") or [])
                res["own_notes"] = len(note_pool.get("own_notes") or {})
                res["ok"] = True
            except Exception as e:
                res["note_error"] = str(e)

            try:
                equip_pool = equipment_advisor.sync_equip_pool(
                    docs_dir=docs_dir,
                    log_dir=log_dir,
                    force_rescan=force_rescan,
                )
                res["owned_equips"] = len(equip_pool.get("owned_cards") or {})
            except Exception as e:
                res["equip_error"] = str(e)

            self.invalidate_caches()
            return res

    def refresh_all(
        self,
        docs_dir: Optional[str] = None,
        log_dir: Optional[str] = None,
        strategy_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """网页端『重新加载』完整刷新：增量扫描新完赛战绩 + 同步角色/笔记/器材池 + 刷新缓存与离线快照。"""
        active_docs = docs_dir or self.docs_dir
        active_logs = log_dir or self.log_dir
        if not active_docs or not active_logs:
            try:
                r_docs, r_logs = platform_compat.resolve_game_dirs(interactive_on_missing=False)
                if not active_docs and r_docs:
                    active_docs = r_docs
                if not active_logs and r_logs:
                    active_logs = r_logs
                self.set_game_dirs(active_docs, active_logs)
            except Exception:
                pass

        prev_count = len(self._get_runs_enriched())
        runs_scanned = False
        runs_total = prev_count
        new_runs = 0
        runs_error = ""

        if active_logs and os.path.isdir(active_logs):
            try:
                import analyzer as _az

                _az.LOG_DIR = active_logs
                runs = _az.extract_all_runs(
                    strategy_name=(strategy_name or self.strategy_name or "training_flow"),
                    force_rebuild=False,
                    log_dir=active_logs,
                )
                runs_scanned = True
                runs_total = len(runs or [])
                new_runs = max(0, runs_total - prev_count)
            except Exception as e:
                runs_error = str(e)

        sync_info = self.sync_local_pools(docs_dir=active_docs, log_dir=active_logs)
        if not runs_scanned:
            runs_total = len(self._get_runs_enriched())

        try:
            export_static_snapshot(self)
        except Exception:
            pass

        out: dict[str, Any] = {
            "ok": True,
            "refreshed_at": time.strftime("%H:%M:%S"),
            "runs_scanned": runs_scanned,
            "runs_total": runs_total,
            "new_runs": new_runs,
            "sync": sync_info,
        }
        if runs_error:
            out["runs_error"] = runs_error
        return out

    def get_overview(self) -> dict:
        runs = self._get_runs_enriched()
        note_pool = _safe_read_json(NOTE_POOL_FILE, {})
        daily_queue = _safe_read_json(DAILY_QUEUE_FILE, {})
        account_meta = _parse_account_diamonds()

        # 1. 五位置最高评分底座（从全部真实归档实时汇总）
        pos_best: dict[str, dict] = {
            p: {
                "pos": p,
                "pos_cn": POS_CN[p],
                "best_rank": 0,
                "best_char": "",
                "best_char_id": None,
                "best_head": "",
                "best_run_id": "",
                "auto_est": 0,
                "is_10k": False,
                "run_count": 0,
            }
            for p in POS_ORDER
        }
        char_best: dict[int, dict] = {}
        for r in runs:
            pos = r["main_pos"]
            rk = r["final_rank"]
            cid = r["main_char_id"]
            if pos in pos_best:
                pos_best[pos]["run_count"] += 1
                if rk > pos_best[pos]["best_rank"]:
                    pos_best[pos].update({
                        "best_rank": rk,
                        "best_char": r["main_name"],
                        "best_char_id": cid,
                        "best_head": r["main_head"],
                        "best_run_id": r["run_id"],
                        "auto_est": int(round(rk * 0.97)),
                        "is_10k": rk >= 10000,
                    })
            if cid:
                if cid not in char_best or rk > char_best[cid]["best_rank"]:
                    char_best[cid] = {
                        "best_rank": rk,
                        "best_run_id": r["run_id"],
                        "run_count": char_best.get(cid, {}).get("run_count", 0) + 1,
                    }
                else:
                    char_best[cid]["run_count"] += 1

        # 2. 核心统计 KPI
        total_runs = len(runs)
        ranks = [r["final_rank"] for r in runs if r["final_rank"] > 0]
        over_10k = sum(1 for x in ranks if x >= 10000)
        recent10 = ranks[-10:] if ranks else []
        prev10 = ranks[-20:-10] if len(ranks) > 10 else []

        best_run = max((r for r in runs if r["final_rank"] > 0), key=lambda r: r["final_rank"], default=None)
        covered_positions = [p for p in pos_best.values() if p["best_rank"] > 0]
        pos_avg_best_rank = (
            round(sum(p["best_rank"] for p in covered_positions) / len(covered_positions))
            if covered_positions else 0
        )
        pos_avg_auto_est = (
            round(sum(p["auto_est"] for p in covered_positions) / len(covered_positions))
            if covered_positions else 0
        )
        weakest_pos = min(covered_positions, key=lambda p: p["best_rank"], default=None)

        own_notes_dict = note_pool.get("own_notes") or {}
        rainbow_own_notes = [n for n in own_notes_dict.values() if int(n.get("rank") or 0) >= 7]
        rainbow_chars = sorted({int(n.get("actor_id") or 0) for n in rainbow_own_notes if n.get("actor_id")})

        # 3. 养成队列与瓶颈摘要（丰富头像与最新历史最高分）
        bottlenecks = []
        for b in daily_queue.get("bottlenecks") or []:
            cid = b.get("char_id")
            live_best = char_best.get(cid, {}).get("best_rank", 0) if cid else 0
            cur_best = max(int(b.get("current_best_rank") or 0), live_best)
            target_rk = int(b.get("target_rank") or 0)
            bottlenecks.append({
                "char_id": cid,
                "char_name": b.get("char_name", ""),
                "full_name": b.get("full_name", ""),
                "role": b.get("role", ""),
                "priority": int(b.get("priority") or 99),
                "focus_stat": b.get("focus_stat", ""),
                "current_best_rank": cur_best,
                "target_rank": target_rk,
                "rank_gap": max(0, target_rk - cur_best),
                "has_rainbow_note": cid in rainbow_chars,
                "primary_bottleneck": b.get("primary_bottleneck", ""),
                "advice": b.get("advice", ""),
                "head": _head_url(cid, self.head_map),
            })

        queue_slots = []
        for q in daily_queue.get("queue") or []:
            cid = q.get("char_id")
            queue_slots.append({
                "slot": q.get("slot"),
                "char_id": cid,
                "char_name": q.get("char_name", ""),
                "head": _head_url(cid, self.head_map),
                "purpose": q.get("purpose", ""),
                "focus_stat": q.get("focus_stat", ""),
                "strategy": q.get("strategy", ""),
                "notes": q.get("notes") or {},
                "equipments": q.get("equipments") or {},
                "reason": q.get("reason", ""),
            })

        # 4. 筛选下拉所需的已出场角色列表
        played_chars = []
        seen_c = set()
        for r in reversed(runs):
            mn = r["main_name"]
            if mn and mn not in seen_c:
                seen_c.add(mn)
                played_chars.append({
                    "name": mn,
                    "char_id": r["main_char_id"],
                    "pos": r["main_pos"],
                    "head": r["main_head"],
                })

        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kpi": {
                "total_runs": total_runs,
                "best_rank": best_run["final_rank"] if best_run else 0,
                "best_char": best_run["main_name"] if best_run else "",
                "best_pos": best_run["main_pos"] if best_run else "",
                "best_run_id": best_run["run_id"] if best_run else "",
                "avg_rank_all": round(sum(ranks) / len(ranks), 1) if ranks else 0.0,
                "avg_rank_recent10": round(sum(recent10) / len(recent10), 1) if recent10 else 0.0,
                "avg_rank_prev10": round(sum(prev10) / len(prev10), 1) if prev10 else 0.0,
                "over_10k_count": over_10k,
                "pos_10k_count": sum(1 for p in pos_best.values() if p["is_10k"]),
                "pos_covered_count": len(covered_positions),
                "pos_avg_best_rank": pos_avg_best_rank,
                "pos_avg_auto_est": pos_avg_auto_est,
                "pos_min_pos": weakest_pos["pos"] if weakest_pos else "",
                "pos_min_rank": weakest_pos["best_rank"] if weakest_pos else 0,
                "own_notes_total": len(own_notes_dict),
                "rainbow_notes_count": len(rainbow_own_notes),
                "rainbow_chars_count": len(rainbow_chars),
                "borrowed_notes_total": len(note_pool.get("borrowed_notes") or {}),
                "diamonds": account_meta["diamonds"],
            },
            "positions": [pos_best[p] for p in POS_ORDER],
            "bottlenecks": bottlenecks,
            "queue": queue_slots,
            "goal_warnings": daily_queue.get("goal_warnings") or [],
            "played_chars": played_chars,
            "runs": runs,
        }

    def get_run_detail(self, run_id: str) -> Optional[dict]:
        runs = self._get_runs_enriched()
        summary = next((r for r in runs if r["run_id"] == run_id), None)
        idx = self._get_archive_index()
        fp = idx.get(run_id)
        if not fp and summary:
            # 兼容跨日志命名
            for k, path in idx.items():
                if run_id in k or (summary.get("log") and summary["log"].replace("___log.txt", "") in k):
                    fp = path
                    break

        detail = _safe_read_json(fp, None) if fp else None
        if not detail and not summary:
            return None

        merged = dict(summary or {})
        if detail:
            # 补充逐回合精细复盘字段
            fstats = detail.get("final_stats") or merged.get("final_stats") or {}
            pmax = detail.get("prop_max") or merged.get("prop_max") or {}
            merged["final_stats"] = {str(s): int(fstats.get(str(s), fstats.get(s, 0)) or 0) for s in STAT_IDS}
            merged["prop_max"] = {str(s): int(pmax.get(str(s), pmax.get(s, 1500)) or 1500) for s in STAT_IDS}
            merged["has_archive"] = True
            merged["weekday_timeline"] = detail.get("weekday_timeline") or []
            merged["weekend_timeline"] = detail.get("weekend_timeline") or []
            merged["top_suboptimal_turns"] = detail.get("top_suboptimal_turns") or []
            merged["story_choices"] = detail.get("story_choices") or []
            merged["equip_max_turns"] = detail.get("equip_max_turns") or merged.get("equip_max_turns") or {}
            merged["equip_max_rounds"] = detail.get("equip_max_rounds") or {}
            merged["building_dist"] = detail.get("building_dist") or {}
            merged["team_building_choices"] = detail.get("team_building_choices") or []
            merged["train_dist"] = detail.get("train_dist") or {}
            merged["weekday_train_dist"] = detail.get("weekday_train_dist") or {}
        else:
            merged["has_archive"] = False
            merged["weekday_timeline"] = []
            merged["weekend_timeline"] = []
            merged["top_suboptimal_turns"] = []
            merged["story_choices"] = []

        # 补充器材名称，便于前端直接展示「哪张卡第几回合满级」
        eq_names = {}
        for eid_str in (merged.get("equip_max_turns") or {}).keys():
            eq = self.equip_db.get(int(eid_str)) if str(eid_str).isdigit() else None
            eq_names[str(eid_str)] = {
                "name": eq.get("name", f"器材#{eid_str}") if eq else f"器材#{eid_str}",
                "icon": _equip_icon_url(int(eid_str) if str(eid_str).isdigit() else None, self.equip_icon_map, eq.get("icon_file", "") if eq else ""),
                "turn": merged["equip_max_turns"][eid_str],
            }
        merged["equip_max_named"] = eq_names
        return merged

    def get_box(self) -> dict:
        note_pool = _safe_read_json(NOTE_POOL_FILE, {})
        equip_pool = _safe_read_json(EQUIP_POOL_FILE, {})
        runs = self._get_runs_enriched()

        owned_actor_ids = set(int(x) for x in (note_pool.get("owned_actors") or []))
        own_notes_raw = note_pool.get("own_notes") or {}
        bor_notes_raw = note_pool.get("borrowed_notes") or {}

        # 每位角色历史最高分与局数
        run_stats_by_cid: dict[int, dict] = {}
        for r in runs:
            cid = r.get("main_char_id")
            if not cid:
                continue
            st = run_stats_by_cid.setdefault(cid, {"best_rank": 0, "run_count": 0})
            st["run_count"] += 1
            if r["final_rank"] > st["best_rank"]:
                st["best_rank"] = r["final_rank"]

        # 归类自备笔记与好友笔记
        def _serialize_note(n: dict, source: str) -> dict:
            aid = int(n.get("actor_id") or 0)
            cobj = self.chars.get(aid, {})
            highlights = note_advisor.format_note_highlights(n, note_advisor.get_char_rec_stat_ids(cobj) if cobj else None)
            harmony_str = note_advisor.format_harmony_targets(n, self.chars) if n.get("harmony_targets") else ""
            return {
                "note_id": str(n.get("note_id") or ""),
                "actor_id": aid,
                "actor_name": n.get("actor_name") or cobj.get("short_name") or str(aid),
                "pos": self._char_pos_code(aid),
                "head": _head_url(aid, self.head_map),
                "rank": int(n.get("rank") or 0),
                "is_rainbow": int(n.get("rank") or 0) >= 7,
                "score": int(n.get("score") or 0),
                "entry_num": int(n.get("entry_num") or 0),
                "source": source,
                "friend": n.get("friend", ""),
                "last_seen": n.get("last_seen", ""),
                "highlights": highlights,
                "harmony_str": harmony_str,
                "harmony_targets": {str(k): int(v) for k, v in (n.get("harmony_targets") or {}).items() if int(v) > 0},
                "harmony_star": int(n.get("harmony_star") or 0),
                "stat_bonuses": n.get("stat_bonuses") or {},
                "cap_bonuses": n.get("cap_bonuses") or {},
            }

        own_notes = [_serialize_note(n, "own") for n in own_notes_raw.values()]
        own_notes.sort(key=lambda x: (-x["rank"], -x["score"], x["actor_id"]))

        bor_notes = [_serialize_note(n, "borrow") for n in bor_notes_raw.values()]
        bor_notes.sort(key=lambda x: (-x["rank"], -x["score"], x["actor_id"]))

        # 按角色统计自备最高笔记
        own_best_by_cid: dict[int, dict] = {}
        for n in own_notes:
            aid = n["actor_id"]
            cur = own_best_by_cid.get(aid)
            if not cur or (n["rank"], n["score"]) > (cur["rank"], cur["score"]):
                own_best_by_cid[aid] = {"rank": n["rank"], "score": n["score"], "count": 0}
            own_best_by_cid[aid]["count"] = own_best_by_cid[aid].get("count", 0) + 1

        # 角色清单（仅已实装 38 人）
        characters = []
        for cid, c in sorted(self.chars.items()):
            if not c.get("released", False):
                continue
            rst = run_stats_by_cid.get(cid, {"best_rank": 0, "run_count": 0})
            nb = own_best_by_cid.get(cid, {"rank": 0, "score": 0, "count": 0})
            characters.append({
                "id": cid,
                "short_name": c.get("short_name", ""),
                "show_name": c.get("show_name", ""),
                "pos": self._char_pos_code(cid),
                "best_pos": c.get("best_pos", ""),
                "quality_name": c.get("quality_name", ""),
                "role_type": list(c.get("role_type") or []),
                "recommend_train": list(c.get("recommend_train") or []),
                "head": _head_url(cid, self.head_map),
                "owned": cid in owned_actor_ids,
                "best_rank": rst["best_rank"],
                "run_count": rst["run_count"],
                "own_note_count": nb["count"],
                "best_own_note_rank": nb["rank"],
                "best_own_note_score": nb["score"],
                "has_rainbow_note": nb["rank"] >= 7,
            })

        # 自有与常用借卡器材池
        def _serialize_equip_card(cid_str: str, info: dict, is_borrow: bool) -> dict:
            cfg_id = int(info.get("cfg_id") or cid_str)
            meta = self.equip_db.get(cfg_id, {})
            return {
                "cfg_id": cfg_id,
                "name": info.get("name") or meta.get("name") or str(cfg_id),
                "icon": _equip_icon_url(cfg_id, self.equip_icon_map, meta.get("icon_file", "")),
                "level": int(info.get("level") or 0),
                "break_level": int(info.get("break_level") or 0),
                "awake_level": int(info.get("awake_level") or 0),
                "preferred_training": meta.get("preferred_training", ""),
                "official_tag": meta.get("official_tag", ""),
                "stat_tier": meta.get("stat_tier", ""),
                "community_tier": meta.get("community_tier", ""),
                "skill_name": meta.get("skill_name", ""),
                "skill_summary": meta.get("skill_summary", ""),
                "is_borrow": is_borrow,
            }

        owned_equips = [
            _serialize_equip_card(k, v, False)
            for k, v in (equip_pool.get("owned_cards") or {}).items()
        ]
        owned_equips.sort(key=lambda x: (-x["awake_level"], -x["break_level"], x["cfg_id"]))

        borrowed_equips = [
            _serialize_equip_card(k, v, True)
            for k, v in (equip_pool.get("borrowed_cards") or {}).items()
        ]

        return {
            "characters": characters,
            "own_notes": own_notes,
            "borrowed_notes": bor_notes,
            "owned_equips": owned_equips,
            "borrowed_equips": borrowed_equips,
        }

    def get_planner(self, char_id: int, mode: str = "score", focus_stat: str = "") -> dict:
        if char_id not in self.chars:
            raise ValueError(f"未知角色 ID: {char_id}")
        note_pool = _safe_read_json(NOTE_POOL_FILE, {})
        cobj = self.chars[char_id]
        rec_ids = note_advisor.get_char_rec_stat_ids(cobj)

        # 1. 真实笔记配对建议（100% 复用 note_advisor 真值引擎）
        note_adv = note_advisor.recommend_notes_for_character(
            char_id, self.chars, self.rules, self.rel_scores, note_pool
        )

        def _fmt_combo(c: dict) -> dict:
            n1 = c["note1"]
            n2 = c["note2"]
            a1 = int(n1.get("actor_id") or 0)
            a2 = int(n2.get("actor_id") or 0)
            return {
                "total_compat": c["total_compat"],
                "relation_total": c["relation_total"],
                "harmony_bonus": c["harmony_bonus"],
                "rank_name": c["rank_name"],
                "level": c["level"],
                "learn_times": c["learn_times"],
                "trig_min": c["trig_min"],
                "trig_max": c["trig_max"],
                "expected_triggers": round(float(c["expected_triggers"]), 1),
                "compat_tier_note": c.get("compat_tier_note", ""),
                "avg_note_score": round(float(c["avg_note_score"]), 1),
                "matched_cap_gain": int(c.get("matched_cap_gain") or 0),
                "total_cap_gain": int(c.get("total_cap_gain") or 0),
                "eval_score": round(float(c["eval_score"]), 1),
                "note1": {
                    "actor_id": a1,
                    "actor_name": n1.get("actor_name", ""),
                    "head": _head_url(a1, self.head_map),
                    "rank": int(n1.get("rank") or 0),
                    "score": int(n1.get("score") or 0),
                    "friend": n1.get("friend", ""),
                    "highlights": note_advisor.format_note_highlights(n1, rec_ids),
                    "harmony_bonus": int(c.get("b1") or 0),
                    "rel": int(c.get("r1") or 0),
                },
                "note2": {
                    "actor_id": a2,
                    "actor_name": n2.get("actor_name", ""),
                    "head": _head_url(a2, self.head_map),
                    "rank": int(n2.get("rank") or 0),
                    "score": int(n2.get("score") or 0),
                    "friend": n2.get("friend", ""),
                    "highlights": note_advisor.format_note_highlights(n2, rec_ids),
                    "harmony_bonus": int(c.get("b2") or 0),
                    "rel": int(c.get("r2") or 0),
                },
            }

        # 2. 真实器材配装建议（100% 复用 equipment_advisor 真值引擎）
        focus_id: Any = None
        if focus_stat:
            for sid, sname in STAT_NAMES.items():
                if focus_stat in (str(sid), sname):
                    focus_id = sid
                    break
        eq_adv = equipment_advisor.recommend_equipments_for_character(
            char_id, mode=mode if mode in ("score", "pvp_22", "pvp_31") else "score", focus_stat=focus_id
        )

        def _fmt_loadout(items: list[dict]) -> list[dict]:
            out = []
            for it in items or []:
                eq = it.get("equip") or {}
                cid_eq = int(eq.get("id") or 0)
                se = it.get("skill_eval") or {}
                oi = it.get("owned_info") or {}
                out.append({
                    "id": cid_eq,
                    "name": eq.get("name", ""),
                    "icon": _equip_icon_url(cid_eq, self.equip_icon_map, eq.get("icon_file", "")),
                    "preferred_training": eq.get("preferred_training", ""),
                    "official_tag": eq.get("official_tag", ""),
                    "stat_tier": eq.get("stat_tier", ""),
                    "community_tier": eq.get("community_tier", ""),
                    "skill_name": eq.get("skill_name", ""),
                    "skill_summary": eq.get("skill_summary", ""),
                    "skill_ok": bool(se.get("effective", True)),
                    "skill_reason": se.get("reason", ""),
                    "train_match": it.get("train_match", ""),
                    "total_score": round(float(it.get("total_score") or 0), 1),
                    "is_borrow": bool(it.get("is_borrow", False)),
                    "break_level": int(it.get("break_level") or oi.get("break_level") or 0),
                    "awake_level": int(oi.get("awake_level") or (3 if it.get("is_borrow") else 0)),
                })
            return out

        ab = note_adv.get("auto_breeders") or {}
        for k in ("same_pos_owned", "same_pos_all"):
            for item in ab.get(k) or []:
                item["head"] = _head_url(item.get("actor_id"), self.head_map)

        return {
            "character": {
                "id": char_id,
                "short_name": cobj.get("short_name", ""),
                "show_name": cobj.get("show_name", ""),
                "best_pos": cobj.get("best_pos", ""),
                "pos": self._char_pos_code(char_id),
                "head": _head_url(char_id, self.head_map),
                "roles_str": eq_adv.get("roles_str", ""),
                "recommend_train": list(cobj.get("recommend_train") or []),
                "rec_stats_str": note_adv.get("rec_stats_str", ""),
            },
            "notes": {
                "is_empty_pool": bool(note_adv.get("is_empty_pool", False)),
                "borrow_combos": [_fmt_combo(c) for c in (note_adv.get("borrow_combos") or [])[:8]],
                "pure_own_combos": [_fmt_combo(c) for c in (note_adv.get("pure_own_combos") or [])[:5]],
                "generic_combos": [_fmt_combo(c) for c in (note_adv.get("generic_combos") or [])[:5]],
                "auto_breeders": ab,
                "revision_warning": note_adv.get("revision_warning", ""),
            },
            "equipments": {
                "mode": eq_adv.get("mode", mode),
                "owned_pattern": eq_adv.get("owned_pattern") or {},
                "owned_loadout": _fmt_loadout(eq_adv.get("owned_loadout") or []),
                "owned_benched": _fmt_loadout(eq_adv.get("owned_benched") or []),
                "bis_pattern": eq_adv.get("bis_pattern") or {},
                "bis_loadout": _fmt_loadout(eq_adv.get("bis_loadout") or []),
                "pvp_22_pattern": eq_adv.get("pvp_22_pattern") or {},
                "pvp_22_loadout": _fmt_loadout(eq_adv.get("pvp_22_loadout") or []),
                "pvp_31_pattern": eq_adv.get("pvp_31_pattern") or {},
                "pvp_31_loadout": _fmt_loadout(eq_adv.get("pvp_31_loadout") or []),
                "owned_pvp_note": eq_adv.get("owned_pvp_note", ""),
                "wish_headline": eq_adv.get("wish_headline", ""),
            },
        }

    def get_compatibility_data(self) -> dict:
        """
        返回笔记相性查询全量数据包：
        - characters: 全 38 名已实装球员（含拼音首字母检索词、学校、位置、已拥有状态、自备最高/好友最高笔记摘要）
        - rel_scores: 38×38 精确角色基础关系分矩阵（源自 inheritance_rules.json）
        - rules: 档位门槛、单次触发词条区间与专属契合星级加分真值
        - own_notes / borrowed_notes: 完整个人与好友笔记（含 harmony_targets）
        - breeding: 黄金三角互哺圈、刷笔记枢纽榜与两步铺路反推链
        """
        note_pool = _safe_read_json(NOTE_POOL_FILE, {})
        box = self.get_box()
        own_notes = box.get("own_notes") or []
        bor_notes = box.get("borrowed_notes") or []

        best_bor_by_cid: dict[int, dict] = {}
        for n in bor_notes:
            aid = int(n.get("actor_id") or 0)
            cur = best_bor_by_cid.get(aid)
            if not cur or (n["rank"], n["score"]) > (cur["rank"], cur["score"]):
                best_bor_by_cid[aid] = {
                    "rank": n["rank"],
                    "score": n["score"],
                    "friend": n.get("friend", ""),
                    "count": (cur["count"] if cur else 0) + 1,
                }
            else:
                cur["count"] += 1

        released_ids: list[int] = []
        characters = []
        for c in box.get("characters") or []:
            cid = int(c["id"])
            released_ids.append(cid)
            raw_c = self.chars.get(cid, {})
            bor = best_bor_by_cid.get(cid) or {}
            rec_ids = note_advisor.get_char_rec_stat_ids(raw_c)
            characters.append({
                **c,
                "en_name": raw_c.get("en_name", ""),
                "aliases": list(raw_c.get("aliases") or []),
                "school": raw_c.get("school", ""),
                "pinyin": CHAR_PINYIN_HINTS.get(cid, ""),
                "rec_stats": [int(s) for s in rec_ids],
                "rec_stats_names": [STAT_NAMES.get(int(s), str(s)) for s in rec_ids],
                "best_borrow_rank": int(bor.get("rank") or 0),
                "best_borrow_score": int(bor.get("score") or 0),
                "best_borrow_friend": bor.get("friend", ""),
                "borrow_note_count": int(bor.get("count") or 0),
            })

        rel_matrix: dict[str, dict[str, int]] = {}
        for cid in released_ids:
            rmap = self.rel_scores.get(cid, {})
            rel_matrix[str(cid)] = {
                str(pid): (0 if pid == cid else int(rmap.get(pid, 0)))
                for pid in released_ids
            }

        c_cfg = self.rules.get("compatibility_and_learn_times") or {}
        harmony_levels = c_cfg.get("harmony_levels") or [
            {"level": 1, "rank_name": "无", "min_compatibility_score": 0, "inheritance_learn_times": 1},
            {"level": 2, "rank_name": "差", "min_compatibility_score": 1, "inheritance_learn_times": 2},
            {"level": 3, "rank_name": "一般", "min_compatibility_score": 10, "inheritance_learn_times": 3},
            {"level": 4, "rank_name": "良", "min_compatibility_score": 20, "inheritance_learn_times": 4},
            {"level": 5, "rank_name": "优", "min_compatibility_score": 30, "inheritance_learn_times": 5},
            {"level": 6, "rank_name": "极", "min_compatibility_score": 45, "inheritance_learn_times": 5},
        ]
        trigger_tiers = c_cfg.get("entry_trigger_count_by_compatibility") or [
            {"required_compatibility_score": 0, "min_triggered_entries": 2, "max_triggered_entries": 4},
            {"required_compatibility_score": 5, "min_triggered_entries": 3, "max_triggered_entries": 5},
            {"required_compatibility_score": 10, "min_triggered_entries": 4, "max_triggered_entries": 6},
            {"required_compatibility_score": 15, "min_triggered_entries": 4, "max_triggered_entries": 7},
            {"required_compatibility_score": 20, "min_triggered_entries": 4, "max_triggered_entries": 8},
            {"required_compatibility_score": 40, "min_triggered_entries": 5, "max_triggered_entries": 10},
            {"required_compatibility_score": 60, "min_triggered_entries": 5, "max_triggered_entries": 11},
        ]

        breeding = note_advisor.compute_breeding_cycle_data(
            self.chars, self.rules, self.rel_scores, note_pool
        )
        for item in breeding.get("hub_ranking") or []:
            item["head"] = _head_url(item.get("actor_id"), self.head_map)
        for tri in breeding.get("triangles") or []:
            for mem in tri.get("members") or []:
                mem["head"] = _head_url(mem.get("actor_id"), self.head_map)
        for st in breeding.get("stepping_stones") or []:
            st["target_head"] = _head_url(st.get("target_id"), self.head_map)
            st["bridge_head"] = _head_url(st.get("bridge_id"), self.head_map)

        return {
            "characters": characters,
            "rel_scores": rel_matrix,
            "rules": {
                "harmony_bonus_by_star": {str(k): v for k, v in note_advisor.HARMONY_BONUS_BY_STAR.items()},
                "harmony_levels": harmony_levels,
                "entry_triggers": [t for t in trigger_tiers if int(t.get("required_compatibility_score", 0)) <= 100],
            },
            "own_notes": own_notes,
            "borrowed_notes": bor_notes,
            "breeding": breeding,
            "revision_warning": note_advisor.NOTE_REVISION_WARNING,
        }

    def get_plans(self) -> dict:
        """扫描 docs/*_plan.md 与 docs/*_guide.md，汇总个人养成计划与攻略文档。"""
        items: list[dict] = []
        if not os.path.isdir(DOCS_DIR):
            return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "plans": items}

        char_db = self.chars
        head_map = self.head_map
        candidates = sorted(
            set(glob.glob(os.path.join(DOCS_DIR, "*_plan.md")))
            | set(glob.glob(os.path.join(DOCS_DIR, "*_guide.md")))
        )
        for path in candidates:
            fname = os.path.basename(path)
            slug = os.path.splitext(fname)[0]
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            # 解析标题（首个 # 标题）
            m_title = re.search(r"(?m)^#\s+(.+?)\s*$", content)
            title = m_title.group(1).strip() if m_title else slug

            # 解析可选 HTML 注释元信息（plan_tag / plan_status / plan_chars / plan_summary）
            meta: dict[str, str] = {}
            m_comment = re.search(r"<!--(.*?)-->", content, flags=re.S)
            if m_comment:
                for line in m_comment.group(1).splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip().lower()] = v.strip()

            is_plan = slug.endswith("_plan")
            tag = meta.get("plan_tag") or ("养成计划" if is_plan else "专题攻略")
            status = meta.get("plan_status") or ("进行中" if is_plan else "常驻参考")
            summary = meta.get("plan_summary") or ""
            if not summary:
                for line in content.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#") and not s.startswith("<!--") and not s.startswith("-->") and not s.startswith("---"):
                        summary = re.sub(r"^[>\-\*\s]+", "", s)
                        summary = re.sub(r"\*\*(.+?)\*\*", r"\1", summary)
                        summary = summary[:110]
                        break

            related_chars = []
            raw_cids = meta.get("plan_chars", "")
            for tok in re.split(r"[,\s]+", raw_cids):
                if tok.isdigit():
                    cid = int(tok)
                    cinfo = char_db.get(cid) or {}
                    head_fn = head_map.get(cid)
                    related_chars.append({
                        "char_id": cid,
                        "name": cinfo.get("short_name") or cinfo.get("show_name") or str(cid),
                        "position": self._char_pos_code(cid) or str(cinfo.get("best_pos") or ""),
                        "head_url": f"/assets/heads/{head_fn}" if head_fn else "",
                    })

            mtime = _file_mtime(path)
            items.append({
                "id": slug,
                "filename": fname,
                "rel_path": f"docs/{fname}",
                "kind": "plan" if is_plan else "guide",
                "title": title,
                "tag": tag,
                "status": status,
                "summary": summary,
                "related_chars": related_chars,
                "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "",
                "mtime": mtime,
                "markdown": content,
            })

        # *_plan.md 排在前面，其次按更新时间倒序
        items.sort(key=lambda x: (0 if x["kind"] == "plan" else 1, -x["mtime"]))
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "plans": items,
        }

    def get_note_activation(
        self,
        note_id: str = "",
        actor_id: Optional[int] = None,
        harmony_star: int = 0,
    ) -> dict:
        """单本笔记/单角色反向激活真值表（100% 复用 note_advisor.evaluate_compatibility）。"""
        note_pool = _safe_read_json(NOTE_POOL_FILE, {})
        res = note_advisor.compute_single_note_activation_data(
            note_id,
            self.chars,
            self.rules,
            self.rel_scores,
            note_pool,
            actor_id=actor_id,
            harmony_star=harmony_star,
        )
        if not res.get("ok"):
            return res
        tn = res.get("target_note") or {}
        tn["head"] = _head_url(tn.get("actor_id"), self.head_map)
        for key in ("own_activations", "borrow_activations"):
            for item in res.get(key) or []:
                item["head"] = _head_url(item.get("char_id"), self.head_map)
                if item.get("partner"):
                    item["partner"]["head"] = _head_url(item["partner"].get("actor_id"), self.head_map)
        for item in res.get("static_rankings") or []:
            item["head"] = _head_url(item.get("char_id"), self.head_map)
        return res


class LiveMonitor:
    """复用 log_mirror.LiveLogMirror 的只读实时监控器（零侵入、增量读取）。"""

    def __init__(self, engine: DashboardEngine) -> None:
        self.engine = engine
        self._lock = threading.Lock()
        _, self.log_dir = platform_compat.resolve_game_dirs(interactive_on_missing=False)
        self.tailer: Optional[LiveLogMirror] = None
        self.attached = False
        self.last_error = ""
        self.coords_cfg = {}
        try:
            coords_p = os.path.join(BASE_DIR, "config", "button_coords.json")
            if os.path.exists(coords_p):
                with open(coords_p, "r", encoding="utf-8") as cf:
                    self.coords_cfg = json.load(cf)
        except Exception:
            self.coords_cfg = {}
        try:
            self.strategy = lc.load_strategy("training_flow")
        except Exception:
            self.strategy = None

    def set_log_dir(self, log_dir: str) -> None:
        with self._lock:
            if log_dir and log_dir != self.log_dir:
                self.log_dir = log_dir
                if self.tailer is not None:
                    self.tailer.close()
                self.tailer = None
                self.attached = False

    def _ensure_attached(self) -> None:
        if not self.log_dir or not os.path.isdir(self.log_dir):
            self.last_error = f"未找到游戏日志目录: {self.log_dir}"
            return
        if self.tailer is None:
            self.tailer = LiveLogMirror(self.log_dir)
        if not self.attached:
            self.attached = self.tailer.attach_latest()
        else:
            self.tailer.poll()

    @staticmethod
    def _detect_processes() -> dict[str, bool]:
        out = {"game_running": False, "driver_running": False, "hud_running": False}
        if platform_compat.IS_WINDOWS:
            try:
                out["game_running"] = platform_compat.win32_query_game_rect() is not None
                out["hud_running"] = True
            except Exception:
                pass
            return out
        try:
            ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=1.5)
            txt = ps.stdout or ""
            out["game_running"] = ("LQSN" in txt and (".exe" in txt or "wine" in txt or "proton" in txt.lower()))
            out["driver_running"] = ("auto_driver.py" in txt)
            out["hud_running"] = ("live_coach.py" in txt)
        except Exception:
            pass
        return out

    def get_snapshot(self) -> dict:
        with self._lock:
            try:
                self._ensure_attached()
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)

            procs = self._detect_processes()
            if not self.tailer or not self.tailer.log_path:
                return {
                    "ok": False,
                    "status": "offline",
                    "status_label": "未检测到游戏日志",
                    "error": self.last_error,
                    "processes": procs,
                    "timestamp": time.strftime("%H:%M:%S"),
                }

            st = self.tailer.state
            log_path = self.tailer.log_path
            log_mtime = _file_mtime(log_path)
            idle_sec = max(0, int(time.time() - log_mtime)) if log_mtime > 0 else 99999

            exp_round = int(st.get("exp_round") or 0)
            left_round = int(st.get("left_round") or 0)
            round_type = int(st.get("round_type") or 1)
            weekend_day = int(st.get("weekend_day") or 0)
            active_views = sorted(list(st.get("active_ui_views") or []))

            in_end_view = any("end_reward" in v or "edu_settlement" in v for v in active_views)
            in_edu = (exp_round > 0 and not in_end_view)

            if not procs["game_running"] and idle_sec > 180:
                status = "offline"
                status_label = f"游戏离线（上次日志 {idle_sec // 60} 分钟前）"
            elif in_end_view:
                status = "settlement"
                status_label = "停在育成结算页"
            elif in_edu:
                status = "running_edu"
                status_label = "全自动托管中" if procs["driver_running"] else "育成对局进行中"
            else:
                status = "lobby"
                status_label = "大厅 / 准备开局"

            # 主力与队友信息
            main_cid = st.get("main_char_id") or st.get("pre_edu_char_id")
            main_obj = self.engine.chars.get(int(main_cid), {}) if main_cid else {}
            teammates = []
            for tcid in st.get("teammate_char_ids") or []:
                tc = self.engine.chars.get(int(tcid), {})
                teammates.append({
                    "id": int(tcid),
                    "name": tc.get("short_name") or str(tcid),
                    "head": _head_url(int(tcid), self.engine.head_map),
                })

            # 六维与属性上限
            props = st.get("props") or {}
            pmax = st.get("prop_max") or {}
            stats_list = []
            for sid in STAT_IDS:
                val = int(props.get(sid, props.get(str(sid), 0)) or 0)
                cap = int(pmax.get(sid, pmax.get(str(sid), 1500)) or 1500)
                lvl = int((st.get("focus_levels") or {}).get(sid - 100, 1) or 1)
                stats_list.append({
                    "id": sid,
                    "name": STAT_NAMES[sid],
                    "val": val,
                    "cap": cap,
                    "level": lvl,
                    "pct": round(min(100.0, (val / cap * 100.0) if cap > 0 else 0.0), 1),
                })

            # 器材熟度进度
            card_to_cfg = st.get("supactor_card_to_cfg") or {}
            card_trammel = st.get("supactor_card_trammel") or {}
            equips_progress = []
            for card_id, tram in card_trammel.items():
                cfg_id = card_to_cfg.get(card_id) or card_to_cfg.get(str(card_id))
                eq = self.engine.equip_db.get(int(cfg_id), {}) if cfg_id else {}
                equips_progress.append({
                    "cfg_id": int(cfg_id) if cfg_id else 0,
                    "name": eq.get("name", f"器材#{cfg_id or card_id}"),
                    "icon": _equip_icon_url(int(cfg_id) if cfg_id else None, self.engine.equip_icon_map, eq.get("icon_file", "")),
                    "trammel": int(tram or 0),
                    "is_max": int(tram or 0) >= 100,
                })
            equips_progress.sort(key=lambda x: (-x["trammel"], x["cfg_id"]))

            # 实时训练评分 Top 3 与当前军师建议
            train_top = []
            next_action_summary = ""
            try:
                strat = self.strategy
                if strat and self.tailer.last_train_cmds and len(self.tailer.last_train_cmds) == 6:
                    scored = lc.score_training_turn(st, self.tailer.last_train_cmds, strat)
                    for item in (scored or [])[:3]:
                        train_top.append({
                            "idx": int(item.get("idx", 0)),
                            "name": item.get("name", ""),
                            "ev": round(float(item.get("ev", 0.0)), 1),
                            "fail_rate": int(item.get("fail_rate", 0)),
                            "ps_cost": int(item.get("ps_cost", 0)),
                            "tags": list(item.get("tags") or [])[:4],
                        })
                if strat and decision_bridge is not None and hasattr(decision_bridge, "decide_next_action"):
                    dec = decision_bridge.decide_next_action(
                        st, self.tailer.last_train_cmds, strat, self.coords_cfg
                    )
                    if isinstance(dec, dict):
                        next_action_summary = dec.get("reason") or dec.get("label") or dec.get("action") or ""
                elif train_top:
                    top0 = train_top[0]
                    next_action_summary = f"推荐训练：{top0['name']} (EV {top0['ev']:.0f})"
                elif round_type == 2 and strat and hasattr(lc, "evaluate_weekend_and_shop"):
                    wk = lc.evaluate_weekend_and_shop(st, strat)
                    if isinstance(wk, dict):
                        next_action_summary = wk.get("best_compact") or wk.get("headline") or ""
            except Exception:
                pass

            week_no = ((exp_round - 1) // 7 + 1) if exp_round > 0 else 0
            day_in_week = ((exp_round - 1) % 7 + 1) if exp_round > 0 else 0
            day_names = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

            return {
                "ok": True,
                "status": status,
                "status_label": status_label,
                "timestamp": time.strftime("%H:%M:%S"),
                "log_file": os.path.basename(log_path),
                "idle_sec": idle_sec,
                "processes": procs,
                "edu": {
                    "main_char_id": main_cid,
                    "main_name": main_obj.get("short_name", "未识别"),
                    "main_pos": self.engine._char_pos_code(int(main_cid)) if main_cid else "",
                    "main_head": _head_url(int(main_cid), self.engine.head_map) if main_cid else "",
                    "teammates": teammates,
                    "rank": int(st.get("rank") or 0),
                    "exp_round": exp_round,
                    "left_round": left_round,
                    "week_no": week_no,
                    "day_label": day_names.get(day_in_week, "—"),
                    "round_type": "周末" if round_type == 2 else "平日",
                    "ps": int(st.get("ps") or 0),
                    "ps_max": int(st.get("ps_max") or 130),
                    "gold": int(st.get("gold") or 0),
                    "mgr_energy": int(st.get("mgr_energy") or 0),
                    "mgr_explosive": int(st.get("mgr_explosive") or 0),
                    "mgr_max_explosive": int(st.get("mgr_max_explosive") or 0),
                    "is_beach": bool(st.get("is_beach_scene", False)),
                    "store_open": bool(st.get("store_open", False)),
                    "cur_story": list(st.get("cur_story") or []),
                    "active_views": active_views[-6:],
                    "stats": stats_list,
                    "equips": equips_progress,
                    "train_top": train_top,
                    "next_action": next_action_summary,
                },
                "recent_lines": list(self.tailer.recent_lines)[-18:],
            }


def export_static_snapshot(engine: DashboardEngine) -> str:
    os.makedirs(DASHBOARD_DATA_DIR, exist_ok=True)
    overview = engine.get_overview()
    box = engine.get_box()
    compatibility = engine.get_compatibility_data()
    plans = engine.get_plans()
    # 静态离线模式下把图片路径改写为相对项目根目录路径，便于直接 file:// 双击预览
    def _rewrite_assets(obj: Any) -> Any:
        if isinstance(obj, str):
            if obj.startswith("/assets/heads/"):
                return "../data/hoopgirls/heads/" + obj[len("/assets/heads/"):]
            if obj.startswith("/assets/equips/"):
                return "../data/hoopgirls/equipment_icons/" + obj[len("/assets/equips/"):]
            return obj
        if isinstance(obj, list):
            return [_rewrite_assets(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _rewrite_assets(v) for k, v in obj.items()}
        return obj

    payload = _rewrite_assets({
        "overview": overview,
        "box": box,
        "compatibility": compatibility,
        "plans": plans,
    })
    out_js = os.path.join(DASHBOARD_DATA_DIR, "snapshot.js")
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("// Auto-generated personal snapshot (gitignored)\n")
        f.write("window.LQSN_DASHBOARD_SNAPSHOT = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    return out_js


def make_handler(engine: DashboardEngine, live_monitor: LiveMonitor):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # 保持终端安静，只在出错时打印
            if args and len(args) >= 2 and str(args[1]).startswith(("4", "5")):
                sys.stderr.write(f"[Dashboard] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, base_dir: str, rel_path: str) -> None:
            rel_clean = urllib.parse.unquote(rel_path).lstrip("/")
            full = os.path.realpath(os.path.join(base_dir, rel_clean))
            base_real = os.path.realpath(base_dir)
            if not (full == base_real or full.startswith(base_real + os.sep)) or not os.path.isfile(full):
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
                return
            ctype, _ = mimetypes.guess_type(full)
            if not ctype:
                ctype = "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                raw = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache" if base_dir == DASHBOARD_DIR else "public, max-age=3600")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/refresh":
                res = engine.refresh_all(log_dir=getattr(live_monitor, "log_dir", None))
                self._send_json(res)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if path == "/api/overview":
                    self._send_json(engine.get_overview())
                    return
                if path == "/api/box":
                    self._send_json(engine.get_box())
                    return
                if path.startswith("/api/run/"):
                    run_id = urllib.parse.unquote(path[len("/api/run/"):]).strip("/")
                    detail = engine.get_run_detail(run_id)
                    if detail is None:
                        self._send_json({"error": f"未找到对局 {run_id}"}, HTTPStatus.NOT_FOUND)
                    else:
                        self._send_json(detail)
                    return
                if path == "/api/planner":
                    cid_str = (qs.get("char_id") or [""])[0]
                    if not cid_str.isdigit():
                        self._send_json({"error": "缺少有效 char_id"}, HTTPStatus.BAD_REQUEST)
                        return
                    mode = (qs.get("mode") or ["score"])[0]
                    focus = (qs.get("focus") or [""])[0]
                    self._send_json(engine.get_planner(int(cid_str), mode=mode, focus_stat=focus))
                    return
                if path == "/api/compatibility":
                    self._send_json(engine.get_compatibility_data())
                    return
                if path == "/api/plans":
                    self._send_json(engine.get_plans())
                    return
                if path == "/api/compatibility/note_activation":
                    note_id = (qs.get("note_id") or [""])[0]
                    aid_str = (qs.get("actor_id") or [""])[0]
                    h_str = (qs.get("harmony_star") or ["0"])[0]
                    aid = int(aid_str) if aid_str.isdigit() else None
                    h_star = int(h_str) if h_str.isdigit() else 0
                    self._send_json(engine.get_note_activation(note_id=note_id, actor_id=aid, harmony_star=h_star))
                    return
                if path == "/api/live":
                    self._send_json(live_monitor.get_snapshot())
                    return
                if path == "/api/export-diag":
                    zbytes, fname = build_dashboard_diagnostic_zip_bytes(engine, live_monitor)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(zbytes)))
                    self.end_headers()
                    self.wfile.write(zbytes)
                    return

                # 静态头像与器材图标
                if path.startswith("/assets/heads/"):
                    self._send_file(HEADS_DIR, path[len("/assets/heads/"):])
                    return
                if path.startswith("/assets/equips/"):
                    self._send_file(EQUIP_ICONS_DIR, path[len("/assets/equips/"):])
                    return

                # 前端静态文件
                if path in ("/", "/index.html"):
                    self._send_file(DASHBOARD_DIR, "index.html")
                    return
                self._send_file(DASHBOARD_DIR, path)
            except Exception as e:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return DashboardHandler


def build_dashboard_diagnostic_zip_bytes(
    engine: DashboardEngine,
    live_monitor: Optional[LiveMonitor] = None,
) -> tuple[bytes, str]:
    """构建作战室脱敏诊断 zip 包（供 /api/export-diag 下载，专门排查战绩/评分/日志解析异常）。"""
    import issue_reporter as ir

    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = f"hoopcoach_dashboard_diag_{ts}.zip"
    buf = io.BytesIO()
    diag_env = ir.collect_diagnostic_info({"active_view_mode": "dashboard"})
    runs = engine._get_runs_enriched()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostic_env.json", json.dumps(diag_env, indent=2, ensure_ascii=False) + "\n")
        # 脱敏战绩摘要表（不含个人路径）
        safe_runs = ir._sanitize_recursive(runs)
        zf.writestr("detailed_runs_sanitized.json", json.dumps(safe_runs, indent=2, ensure_ascii=False) + "\n")

        manifest_fp = os.path.join(RUNS_DIR, "archive_manifest.json")
        if os.path.isfile(manifest_fp):
            try:
                with open(manifest_fp, "r", encoding="utf-8") as mf:
                    m_txt = ir.sanitize_text(mf.read())
                zf.writestr("archive_manifest_sanitized.json", m_txt)
            except Exception:
                pass

        # 附带最高分 2 局 + 最新 2 局的精简归档，方便定位榜单异常局
        pick_ids: list[str] = []
        for r in sorted(runs, key=lambda x: x.get("final_rank", 0), reverse=True)[:2]:
            rid = r.get("run_id")
            if rid and rid not in pick_ids:
                pick_ids.append(rid)
        for r in sorted(runs, key=lambda x: x.get("seq_no", 0), reverse=True)[:2]:
            rid = r.get("run_id")
            if rid and rid not in pick_ids:
                pick_ids.append(rid)
        for rid in pick_ids:
            detail = engine.get_run_detail(rid)
            if detail:
                zf.writestr(
                    f"sample_archives/{rid}.json",
                    json.dumps(ir._sanitize_recursive(detail), indent=2, ensure_ascii=False) + "\n",
                )

        # 提取最新 2 份日志中所有 new_rank 周围协议上下文（快速核对是否混入非育成协议）
        log_dir_use = getattr(live_monitor, "log_dir", None) or ""
        if not log_dir_use:
            try:
                _, log_dir_use = resolve_game_dirs(interactive_on_missing=False)
            except Exception:
                log_dir_use = ""
        if log_dir_use and os.path.isdir(log_dir_use):
            log_files = sorted(glob.glob(os.path.join(log_dir_use, "*.txt")), key=os.path.getmtime, reverse=True)[:2]
            rank_snips: list[str] = []
            for lp in log_files:
                bn = os.path.basename(lp)
                try:
                    with open(lp, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    for idx_l, ln in enumerate(lines):
                        if "new_rank" in ln or "education.sc_education_result" in ln:
                            ctx = "".join(lines[max(0, idx_l - 2) : min(len(lines), idx_l + 3)])
                            rank_snips.append(f"[{bn}:{idx_l + 1}]\n{ir.sanitize_text(ctx.strip())}\n")
                            if len(rank_snips) >= 40:
                                break
                except Exception:
                    continue
            if rank_snips:
                zf.writestr("recent_rank_protocols_sanitized.txt", "\n---\n".join(rank_snips) + "\n")

            if log_files:
                compact = ir.extract_compact_run_log(log_files[0], max_chars=24000)
                if compact:
                    zf.writestr("latest_run_compact_log.txt", compact)

        err_txt = ir._read_sanitized_runtime_error_log(max_chars=8000)
        if err_txt:
            zf.writestr("runtime_error_sanitized.log", err_txt)

    return buf.getvalue(), fname


def start_background_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    log_dir: Optional[str] = None,
    docs_dir: Optional[str] = None,
) -> tuple[ThreadingHTTPServer, str, DashboardEngine, LiveMonitor]:
    """供桌面控制台内嵌拉起本地作战室服务，自动尝试空闲端口。"""
    engine = DashboardEngine()
    engine.set_game_dirs(docs_dir=docs_dir, log_dir=log_dir)
    if (docs_dir and os.path.isdir(docs_dir)) or (log_dir and os.path.isdir(log_dir)):
        try:
            engine.sync_local_pools(docs_dir=docs_dir, log_dir=log_dir)
        except Exception:
            pass
    try:
        export_static_snapshot(engine)
    except Exception:
        pass
    live_monitor = LiveMonitor(engine)
    if log_dir:
        live_monitor.set_log_dir(log_dir)
    threading.Thread(target=live_monitor.get_snapshot, daemon=True).start()

    # 后台静默增量同步完赛战绩（含旧版 schema 归档自动重算自愈）
    if log_dir and os.path.isdir(log_dir):
        def _auto_upgrade_stale_runs() -> None:
            try:
                import analyzer as _az
                _az.LOG_DIR = log_dir
                _az.extract_all_runs(
                    strategy_name=(engine.strategy_name or "training_flow"),
                    force_rebuild=False,
                    log_dir=log_dir,
                )
                engine.invalidate_caches()
                try:
                    export_static_snapshot(engine)
                except Exception:
                    pass
            except Exception:
                pass
        threading.Thread(target=_auto_upgrade_stale_runs, daemon=True).start()

    server = None
    actual_port = port
    for candidate_port in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate_port), make_handler(engine, live_monitor))
            actual_port = int(server.server_address[1])
            break
        except OSError:
            continue
    if server is None:
        server = ThreadingHTTPServer((host, 0), make_handler(engine, live_monitor))
        actual_port = int(server.server_address[1])

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{actual_port}", engine, live_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="LQSNCoach 私人作战室本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机访问）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开默认浏览器")
    parser.add_argument("--export-static", action="store_true", help="仅导出离线快照 dashboard/data/snapshot.js 后退出")
    args = parser.parse_args()

    engine = DashboardEngine()
    d_dir, l_dir = "", ""
    try:
        d_dir, l_dir = platform_compat.resolve_game_dirs(interactive_on_missing=False)
        engine.set_game_dirs(docs_dir=d_dir, log_dir=l_dir)
        if (d_dir and os.path.isdir(d_dir)) or (l_dir and os.path.isdir(l_dir)):
            engine.sync_local_pools(docs_dir=d_dir, log_dir=l_dir)
    except Exception:
        pass
    snap_path = export_static_snapshot(engine)

    if args.export_static:
        print(f"✅ 已导出离线快照：{snap_path}")
        return

    live_monitor = LiveMonitor(engine)
    if l_dir:
        live_monitor.set_log_dir(l_dir)
    # 后台预热实时日志 tailer，避免首次点击监控页等待
    threading.Thread(target=live_monitor.get_snapshot, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine, live_monitor))
    url = f"http://{args.host}:{args.port}"
    print(f"🏀 LQSNCoach 私人作战室已启动：{url}")
    print(f"   离线快照已同步：{snap_path}")
    print("   按 Ctrl+C 停止服务。")

    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止私人作战室服务。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
