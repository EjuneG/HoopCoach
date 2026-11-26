#!/usr/bin/env python3
"""
LQSNCoach — 开局笔记最优配对参谋 & 种马养成循环规划器 (Note Advisor & Breeding Cycle Planner)

功能一（开局选人阶段实时/手动调用）：
  根据当前选中的育成球员，结合 data/hoopgirls/inheritance_rules.json 的相性矩阵与传承档位公式，
  从玩家「自备笔记池」与「好友/社团借用笔记池」中自动检索综合期望最高（契合度档位 + 笔记评分 + 核心属性/上限因子）的配对方案。

功能二（独立战略规划功能 —— 下一位练哪个角色刷笔记最好）：
  基于「为了练 A 需要先练高契合度的 B 和 C，而 B/C 又有各自契合度」的循环养成机制，
  结合当前已拥有球员名册与现有自备笔记池，计算：
  1. 当下最值得练的「刷笔记枢纽角色」排行（下游辐射广度 × 当前笔记可喂饱度 × 边际提升）
  2. 已拥有球员中的「黄金三角互哺圈」（A ⇄ B ⇄ C 永动互刷高分红/彩笔记）
  3. 任意目标主力的「两步铺路反推链」（现有笔记 ➔ 先练 B ➔ 毕业 A）

用法：
  python3 src/note_advisor.py --auto-scan         # 【每日开局前一键扫库】自动切「我的/好友/协会」标签并平滑滚动翻页扫描入库
  python3 src/note_advisor.py --plan              # 运行「下一位练谁（为了笔记）」养成循环规划器
  python3 src/note_advisor.py --char 早濑         # 查询指定角色（名称或ID）的开局最优笔记组合与铺路方案
  python3 src/note_advisor.py --pool              # 查看当前已同步的自备笔记池与好友借用笔记池明细
"""

import argparse
import glob
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from platform_compat import BASE_DIR, resolve_game_dirs

_CACHE_LOCK = threading.Lock()

DOCS_DIR, LOG_DIR = resolve_game_dirs(interactive_on_missing=False)
CHAR_DB_FILE = os.path.join(BASE_DIR, "data", "hoopgirls", "characters.json")
INHERIT_RULES_FILE = os.path.join(BASE_DIR, "data", "hoopgirls", "inheritance_rules.json")
NOTE_POOL_CACHE_FILE = os.path.join(BASE_DIR, "data", "note_pool.json")
NOTE_VISUAL_CACHE_FILE = os.path.join(BASE_DIR, "data", "note_visual_cache.json")
GEMINI_API_BIN = os.path.expanduser("~/.local/bin/gemini-api")

STAT_NAMES = {
    101: "投射",
    102: "弹跳",
    103: "防守",
    104: "体能",
    105: "运控",
    106: "意识",
}
STAT_NAME_TO_ID = {v: k for k, v in STAT_NAMES.items()}


def get_char_rec_stat_ids(char_obj: dict) -> list[int]:
    rec_raw = char_obj.get("rec_stats") or char_obj.get("recommend_base_attr") or []
    res = []
    for item in rec_raw:
        if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
            res.append(int(item))
        elif item in STAT_NAME_TO_ID:
            res.append(STAT_NAME_TO_ID[item])
    return res

QUALITY_NAMES = {
    1: "1阶白",
    2: "2阶绿",
    3: "3阶蓝",
    4: "4阶紫",
    5: "5阶金",
    6: "6阶红",
    7: "7阶彩",
}

HARMONY_BONUS_BY_STAR = {0: 0, 1: 2, 2: 5, 3: 10}

# 已在 11-04~11-05 笔记合成/分解中淘汰的旧日志自备笔记 ID，以及已更换的旧好友助战笔记 ID
ARCHIVED_OWN_NOTE_IDS = {
    "474989023295193",  # 10-27 澪奈 4阶 209分
    "393075407721204",  # 10-27 闪堂 3阶 142分
    "617925536388348",  # 11-01 闪堂 6阶 322分
    "655308932773230",  # 11-01 毒岛 4阶 206分
    "576144096727089",  # 11-01 闪堂 6阶 355分
    "584940190218732",  # 11-01 真崎 6阶 351分
    "655308932801510",  # 11-02 望月 6阶 427分 (已迭代为 473分 2★)
    "608579692064024",  # 11-03 闪堂 5阶 362分 (已合成淘汰，现用 365分 6阶红)
}

ARCHIVED_BORROW_NOTE_IDS = {
    "404620280668031",  # 10-29 勤俊 闪堂 358分
    "623972849034529",  # 11-01 落翼之秋 望月 505分
    "548106549421347",  # 11-01 菲菲回来了 远野 364分 (已换名雪 506分)
    "628370897558430",  # 11-01 Sinlem 闪堂 516分 (已换卧云 479分)
    "655858690790763",  # 11-02 紫金星空 佐久间 427分
    "598684083073614",  # 11-02 期期 林原 419分
    "641565037563228",  # 11-03 Motto 野上 468分
    "559101663176432",  # 11-03 黎明的裤角 千圣 415分
    "547007035775032",  # 11-03 Epi 和泉 398分
}

MANUAL_BORROW_OVERRIDES = {
    "900000000001033": {
        "note_id": 900000000001033,
        "actor_id": 1033,
        "actor_name": "卧云",
        "rank": 6,
        "score": 479,
        "entry_num": 13,
        "friend": "Sinlem",
        "last_seen": "manual_confirmed",
        "harmony_star": 0,
        "harmony_bonus": 0,
        "stat_bonuses": {},
        "cap_bonuses": {},
        "stat_stars": {},
        "cap_stars": {},
        "special_entries": [],
    }
}


def load_static_db():
    chars = {}
    if os.path.exists(CHAR_DB_FILE):
        with open(CHAR_DB_FILE, "r", encoding="utf-8") as f:
            for c in json.load(f):
                if c.get("kind") == "player" and "id" in c:
                    chars[int(c["id"])] = c

    rules = {}
    rel_scores = {}
    entries_catalog = {}
    if os.path.exists(INHERIT_RULES_FILE):
        with open(INHERIT_RULES_FILE, "r", encoding="utf-8") as f:
            rules = json.load(f)
        for cid_str, obj in rules.get("character_relationship_scores", {}).items():
            rel_scores[int(cid_str)] = {int(k): int(v) for k, v in obj.get("scores", {}).items()}
        for e in rules.get("entries", []):
            entries_catalog[int(e["entry_id"])] = e

    return chars, rules, rel_scores, entries_catalog


def _parse_entries(entries_raw: str, entries_catalog: dict, chars: Optional[dict] = None) -> dict:
    """解析单本笔记中的所有词条，提取按目标角色绑定的契合度字典(harmony_targets)、属性加点、属性上限加点与特殊词条。"""
    harmony_targets = {}   # {str(target_actor_id): star}
    stat_bonuses = {}      # {101..106: sum_val}
    cap_bonuses = {}       # {101..106: sum_val}
    stat_stars = {}        # {101..106: sum_star}
    cap_stars = {}         # {101..106: sum_star}
    special_entries = []   # [(name, star, type_id)]

    item_re = re.compile(
        r"entry_id\s*=\s*(\d+),\s*entry_star_id\s*=\s*(\d+),\s*entry_type\s*=\s*(\d+),\s*entry_value\s*=\s*\{(.*?)\}",
        re.S,
    )
    for m in item_re.finditer(entries_raw):
        eid = int(m.group(1))
        star = int(m.group(2))
        etype = int(m.group(3))
        val_raw = m.group(4)

        if eid == 501:
            hm = re.search(r"harmony_initactor\s*=\s*(\d+)", val_raw)
            if hm:
                t_aid = str(int(hm.group(1)))
                harmony_targets[t_aid] = max(harmony_targets.get(t_aid, 0), star)
            continue

        pm = re.search(r"property_id\s*=\s*(\d+),\s*property_value\s*=\s*(-?\d+)", val_raw)
        if etype == 1 and pm:
            pid, pval = int(pm.group(1)), int(pm.group(2))
            stat_bonuses[pid] = stat_bonuses.get(pid, 0) + pval
            stat_stars[pid] = stat_stars.get(pid, 0) + star
        elif etype == 2 and pm:
            pid, pval = int(pm.group(1)), int(pm.group(2))
            cap_bonuses[pid] = cap_bonuses.get(pid, 0) + pval
            cap_stars[pid] = cap_stars.get(pid, 0) + star
        else:
            ecat = entries_catalog.get(eid, {})
            ename = ecat.get("name", f"词条#{eid}").replace("${role_name}", "").replace("${skill_name}", "技能")
            special_entries.append({"entry_id": eid, "name": ename, "star": star, "type": etype})

    max_h_star = max(harmony_targets.values()) if harmony_targets else 0
    return {
        "harmony_targets": harmony_targets,
        "harmony_star": max_h_star,
        "harmony_bonus": HARMONY_BONUS_BY_STAR.get(max_h_star, 0),
        "stat_bonuses": {str(k): v for k, v in sorted(stat_bonuses.items())},
        "cap_bonuses": {str(k): v for k, v in sorted(cap_bonuses.items())},
        "stat_stars": {str(k): v for k, v in sorted(stat_stars.items())},
        "cap_stars": {str(k): v for k, v in sorted(cap_stars.items())},
        "special_entries": special_entries,
    }


def format_harmony_targets(note: dict, chars: dict, active_core_id: Optional[int] = None) -> str:
    """将笔记携带的专属契合词条格式化为可读文本（如『闪堂契合2★(+5)/佐久间契合1★(+2)』）。"""
    ht = note.get("harmony_targets", {})
    if not ht:
        return "无契合词条"
    parts = []
    for tid_str, star in sorted(ht.items(), key=lambda kv: -int(kv[1])):
        tid = int(tid_str)
        tname = chars.get(tid, {}).get("short_name", str(tid))
        bonus = HARMONY_BONUS_BY_STAR.get(int(star), 0)
        mark = "🎯" if (active_core_id is not None and tid == int(active_core_id)) else ""
        parts.append(f"{mark}{tname}契合{star}★(+{bonus})")
    return "/".join(parts)


def format_note_highlights(note: dict, target_rec_stats: Optional[list] = None) -> str:
    """将笔记的核心亮点（上限因子、属性因子、高星剧本因子）浓缩为简短可读标签。"""
    parts = []
    rec_set = set(int(x) for x in (target_rec_stats or []))

    cap_b = {int(k): int(v) for k, v in note.get("cap_bonuses", {}).items()}
    if cap_b:
        cap_items = sorted(cap_b.items(), key=lambda kv: (kv[0] not in rec_set, -kv[1]))
        cap_str = "/".join(
            f"{'★' if k in rec_set else ''}{STAT_NAMES.get(k, str(k))}上限+{v}"
            for k, v in cap_items[:2]
        )
        parts.append(cap_str)

    stat_b = {int(k): int(v) for k, v in note.get("stat_bonuses", {}).items()}
    if stat_b:
        stat_items = sorted(stat_b.items(), key=lambda kv: (kv[0] not in rec_set, -kv[1]))
        stat_str = "/".join(
            f"{'★' if k in rec_set else ''}{STAT_NAMES.get(k, str(k))}+{v}"
            for k, v in stat_items[:2]
        )
        parts.append(stat_str)

    sp = note.get("special_entries", [])
    high_sp = [x for x in sp if x.get("type") in (101, 102, 103, 3) or x.get("star", 1) >= 2]
    if high_sp:
        parts.append(f"{high_sp[0]['name']}{high_sp[0]['star']}★")

    return " · ".join(parts) if parts else f"共{note.get('entry_num', 0)}条因子"


def sync_note_and_actor_pool(chars: dict, entries_catalog: dict, force_rescan: bool = False) -> dict:
    """
    从本地存档目录 (ob_*) 与历史日志 (Logs/*.txt) 同步：
    - owned_actors: 玩家已解锁的球员 ID 列表
    - own_notes: 玩家自备笔记字典 {note_id_str: note_obj}
    - borrowed_notes: 好友/社团助战借用过的笔记字典 {note_id_str: note_obj}
    - min_kept_rank: 自动回收设置保留的最低品阶（默认 5 阶金）
    按日志 (mtime, size) 增量缓存，避免重复全盘读取历史大日志，并与 data/note_pool.json 持久化。
    """
    cached = {"owned_actors": [], "own_notes": {}, "borrowed_notes": {}, "min_kept_rank": 5, "archived_ids": [], "scanned_logs": {}}
    if os.path.exists(NOTE_POOL_CACHE_FILE):
        try:
            with open(NOTE_POOL_CACHE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cached = loaded
        except Exception:
            pass

    archived_ids = set(str(x) for x in cached.get("archived_ids", [])) | ARCHIVED_OWN_NOTE_IDS | ARCHIVED_BORROW_NOTE_IDS
    scanned_logs = {} if force_rescan else dict(cached.get("scanned_logs") or {})
    owned_set = set() if force_rescan else set(int(x) for x in cached.get("owned_actors", []) if int(x) in chars)
    # 仅从缓存加载非视觉临时键 (非 v_ 开头) 的日志真值笔记，避免旧的误识视觉键残留污染
    own_notes = {} if force_rescan else {
        str(k): v
        for k, v in cached.get("own_notes", {}).items()
        if str(k) not in archived_ids and not str(k).startswith("v_")
    }
    borrowed_notes = {} if force_rescan else {
        str(k): v
        for k, v in cached.get("borrowed_notes", {}).items()
        if str(k) not in archived_ids and not str(k).startswith("v_")
    }
    for k, v in MANUAL_BORROW_OVERRIDES.items():
        if k not in archived_ids and k not in borrowed_notes:
            borrowed_notes[k] = dict(v)
    min_kept_rank = int(cached.get("min_kept_rank", 5))

    # 1. 扫描 Documents/ob_*/ 下的 new_initactor.json 与 edu_recycle_note_quality_pref.json
    for ob_dir in sorted(glob.glob(os.path.join(DOCS_DIR, "ob_*"))):
        if not os.path.isdir(ob_dir):
            continue
        init_file = os.path.join(ob_dir, "new_initactor.json")
        if os.path.exists(init_file):
            try:
                with open(init_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                for k in d.keys():
                    cid = int(k)
                    if cid in chars:
                        owned_set.add(cid)
            except Exception:
                pass

        recycle_file = os.path.join(ob_dir, "edu_recycle_note_quality_pref.json")
        if os.path.exists(recycle_file):
            try:
                with open(recycle_file, "r", encoding="utf-8") as f:
                    pref = json.load(f)
                # pref 为长度 7 的布尔数组，true 表示自动分解，首个 false 对应保留的最低品阶
                for idx, is_recycled in enumerate(pref):
                    if not is_recycled:
                        min_kept_rank = idx + 1
                        break
            except Exception:
                pass

    # 2. 扫描 Logs/*.txt 提取完整笔记结构与玩家选人记录
    note_block_re = re.compile(
        r"note_data\s*=\s*\{\s*entry\s*=\s*\{(.*?)\},\s*entry_num\s*=\s*(\d+),\s*initactor_id\s*=\s*(\d+),\s*is_locked\s*=\s*(true|false),\s*mark_type\s*=\s*(\d+),\s*note_id\s*=\s*(\d+),\s*note_rank\s*=\s*(\d+),\s*note_score\s*=\s*(\d+)",
        re.S,
    )

    logs = sorted(glob.glob(os.path.join(LOG_DIR, "*.txt")), key=os.path.getmtime)
    for lp in logs:
        log_name = os.path.basename(lp)
        try:
            st = os.stat(lp)
            log_sig = {"mtime": round(st.st_mtime, 6), "size": int(st.st_size)}
        except OSError:
            continue
        if not force_rescan and scanned_logs.get(log_name) == log_sig:
            continue
        try:
            with open(lp, "r", encoding="utf-8", errors="ignore") as f:
                t = f.read()
        except Exception:
            continue
        scanned_logs[log_name] = log_sig

        # 玩家在开局选人界面点选过的角色必为已拥有角色
        for m in re.finditer(r"education\.cs_edu_initactor_record\s*\{initactor_cfg_id\s*=\s*(\d+)\}", t):
            cid = int(m.group(1))
            if cid in chars:
                owned_set.add(cid)

        # 先提取 borrow_note_info 块中的助战借用笔记（严格限制在 edu_note_list 之前，防空借用跨界吞自备笔记）
        for pos in [m.start() for m in re.finditer(r"borrow_note_info\s*=\s*\{", t)]:
            end_cands = [
                p for p in (t.find("burn_state =", pos), t.find("edu_note_list =", pos)) if p != -1 and p > pos
            ]
            end_pos = min(end_cands) if end_cands else -1
            if end_pos != -1 and end_pos - pos < 15000:
                btxt = t[pos:end_pos]
                nm = note_block_re.search(btxt)
                if nm:
                    entries_raw, enum, aid, is_locked, mark_type, nid, nrank, nscore = nm.groups()
                    nid_int = int(nid)
                    if nid_int > 0:
                        name_m = re.search(r'name\s*=\s*"([^"]+)"', btxt)
                        friend_name = name_m.group(1) if name_m else "好友助战"
                        parsed = _parse_entries(entries_raw, entries_catalog)
                        aid_int = int(aid)
                        borrowed_notes[str(nid_int)] = {
                            "note_id": nid_int,
                            "actor_id": aid_int,
                            "actor_name": chars.get(aid_int, {}).get("short_name", str(aid_int)),
                            "rank": int(nrank),
                            "score": int(nscore),
                            "entry_num": int(enum),
                            "friend": friend_name,
                            "last_seen": log_name,
                            **parsed,
                        }

        # 再提取所有 note_data 块，排除借用笔记后即为玩家自备笔记
        for m in note_block_re.finditer(t):
            entries_raw, enum, aid, is_locked, mark_type, nid, nrank, nscore = m.groups()
            nid_int = int(nid)
            if nid_int <= 0 or str(nid_int) in borrowed_notes:
                continue
            parsed = _parse_entries(entries_raw, entries_catalog)
            aid_int = int(aid)
            if aid_int in chars:
                owned_set.add(aid_int)
            own_notes[str(nid_int)] = {
                "note_id": nid_int,
                "actor_id": aid_int,
                "actor_name": chars.get(aid_int, {}).get("short_name", str(aid_int)),
                "rank": int(nrank),
                "score": int(nscore),
                "entry_num": int(enum),
                "is_locked": is_locked == "true",
                "last_seen": log_name,
                **parsed,
            }

    # 确保借用笔记不混入自备笔记，且剔除已归档/已合成的旧笔记及被同一好友新助战覆盖的旧助战
    for nid_str in list(own_notes.keys()):
        if nid_str in borrowed_notes or nid_str in archived_ids:
            del own_notes[nid_str]
    for nid_str in list(borrowed_notes.keys()):
        if nid_str in archived_ids:
            del borrowed_notes[nid_str]

    # 同一好友若有更新的助战笔记，自动淘汰该好友的旧助战记录
    by_friend = {}
    for nid_str, nb in borrowed_notes.items():
        fr = nb.get("friend", "")
        if not fr or fr == "好友助战":
            continue
        by_friend.setdefault(fr, []).append((nb.get("last_seen", ""), nid_str))
    for fr, items in by_friend.items():
        if len(items) > 1:
            items.sort()
            for _, old_nid in items[:-1]:
                borrowed_notes.pop(old_nid, None)

    # 叠加通过游戏画面视觉扫描 (data/note_visual_cache.json) 捕获的最新自备与好友借用笔记
    _apply_visual_cache_to_pool(own_notes, borrowed_notes, chars)

    result = {
        "owned_actors": sorted(owned_set),
        "own_notes": own_notes,
        "borrowed_notes": borrowed_notes,
        "min_kept_rank": min_kept_rank,
        "archived_ids": sorted(archived_ids),
        "scanned_logs": scanned_logs,
    }
    try:
        os.makedirs(os.path.dirname(NOTE_POOL_CACHE_FILE), exist_ok=True)
        with open(NOTE_POOL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return result


def _load_visual_cache() -> dict:
    if os.path.exists(NOTE_VISUAL_CACHE_FILE):
        try:
            with open(NOTE_VISUAL_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"own_notes": {}, "borrowed_notes": {}, "archived_own_keys": [], "updated_at": ""}


def _save_visual_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(NOTE_VISUAL_CACHE_FILE), exist_ok=True)
        with open(NOTE_VISUAL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _merge_note_details(existing: dict, incoming: dict) -> dict:
    """
    将视觉扫描到的词条/契合详情合并到已有笔记记录中（保留更丰富的字段）。
    ⚠️ 铁律：若 existing 为日志真值记录（非 v_ 开头的数字 note_id），绝不允许视觉扫描覆盖其 actor_id / actor_name / score / rank！
    """
    merged = dict(existing)
    is_ground_truth = bool(merged.get("note_id")) and not str(merged.get("note_id")).startswith("v_")
    has_actor = bool(merged.get("actor_id"))
    mutable_keys = ("friend", "last_seen") if (is_ground_truth or has_actor) else ("rank", "score", "actor_id", "actor_name", "friend", "last_seen")
    for k in mutable_keys:
        if incoming.get(k):
            merged[k] = incoming[k]
    if incoming.get("harmony_targets"):
        ht = dict(merged.get("harmony_targets") or {})
        ht.update({str(tk): int(tv) for tk, tv in incoming["harmony_targets"].items() if int(tv) > 0})
        merged["harmony_targets"] = ht
        merged["harmony_star"] = max(ht.values()) if ht else int(merged.get("harmony_star", 0))
    for dict_key in ("stat_bonuses", "cap_bonuses"):
        if incoming.get(dict_key):
            d = dict(merged.get(dict_key) or {})
            for sk, sv in incoming[dict_key].items():
                if int(sv) > 0:
                    d[str(sk)] = max(int(d.get(str(sk), 0)), int(sv))
            merged[dict_key] = d
    if incoming.get("skill_entries"):
        existing_skills = {s["name"]: s for s in merged.get("skill_entries", [])}
        for s in incoming["skill_entries"]:
            existing_skills[s["name"]] = s
        merged["skill_entries"] = list(existing_skills.values())
    if incoming.get("top_entries_str") and (not merged.get("top_entries_str") or "视觉" in str(merged.get("top_entries_str"))):
        merged["top_entries_str"] = incoming["top_entries_str"]
    return merged


def _note_stat_fingerprint(n: dict) -> tuple:
    """生成笔记的『评分 + 基础属性加点 + 属性上限加点』唯一数值指纹，用于跨帧拦截同一张卡被认错头像产生的克隆分身。"""
    sc = int(n.get("score") or 0)
    sb = tuple(sorted((int(k), int(v)) for k, v in (n.get("stat_bonuses") or {}).items() if int(v) > 0))
    cb = tuple(sorted((int(k), int(v)) for k, v in (n.get("cap_bonuses") or {}).items() if int(v) > 0))
    return (sc, sb, cb)


def _apply_visual_cache_to_pool(own_notes: dict, borrowed_notes: dict, chars: dict) -> None:
    vcache = _load_visual_cache()
    arch_keys = set(vcache.get("archived_own_keys") or [])
    if arch_keys:
        for nid_str, n in list(own_notes.items()):
            key = f"{n.get('actor_id')}:{n.get('score')}"
            if key in arch_keys or nid_str in arch_keys:
                own_notes.pop(nid_str, None)

    for vkey, vn in (vcache.get("own_notes") or {}).items():
        aid = int(vn.get("actor_id", 0))
        score = int(vn.get("score", 0))
        # 自动过滤 <250 分的低阶蓝/绿/紫过渡笔记
        if not aid or score < 250:
            continue
        # 强制按分数段校准品阶，防止旧缓存中紫卡被误标为 7 阶彩
        vn["rank"] = 7 if score >= 510 else (6 if score >= 340 else 5)
        fp_in = _note_stat_fingerprint(vn)
        matched_nid = None
        # 1) 优先匹配相同角色 + 相近分数
        for nid_str, n in own_notes.items():
            if int(n.get("actor_id", 0)) == aid and abs(int(n.get("score", 0)) - score) <= 1:
                matched_nid = nid_str
                break
        # 2) 若池中已有完全同分+同属性数值指纹的笔记（或唯一同分日志真值），必为同一张物理卡片，直接合并绝不克隆！
        if not matched_nid:
            for nid_str, n in own_notes.items():
                if _note_stat_fingerprint(n) == fp_in and (fp_in[1] or fp_in[2]):
                    matched_nid = nid_str
                    break
        if not matched_nid:
            same_score_gt = [
                nid_str for nid_str, n in own_notes.items()
                if not str(nid_str).startswith("v_") and abs(int(n.get("score", 0)) - score) <= 1
            ]
            if len(same_score_gt) == 1:
                matched_nid = same_score_gt[0]
        if matched_nid:
            own_notes[matched_nid] = _merge_note_details(own_notes[matched_nid], vn)
        else:
            nid_str = str(vn.get("note_id") or f"v_own_{aid}_{score}")
            vn["note_id"] = nid_str
            own_notes[nid_str] = vn

    for vkey, vn in (vcache.get("borrowed_notes") or {}).items():
        aid = int(vn.get("actor_id", 0))
        score = int(vn.get("score", 0))
        friend = str(vn.get("friend") or "").strip()
        if not aid or score < 250 or not friend or friend in ("好友助战", "好友", "助战", "协会", "COACH"):
            continue
        vn["rank"] = 7 if score >= 510 else (6 if score >= 340 else 5)
        matched_nid = None
        if friend and friend != "好友助战":
            for nid_str, nb in list(borrowed_notes.items()):
                if nb.get("friend") == friend and abs(int(nb.get("score", 0)) - score) <= 1:
                    matched_nid = nid_str
                    break
            # 禁止因同好友名无条件 pop 删除已由日志实锤的真值笔记；仅在双方均为 source=="ocr" 且显式确认覆盖时更新移除旧 OCR 项
            if not matched_nid and vn.get("source") == "ocr" and bool(vn.get("confirm_override")):
                for nid_str, nb in list(borrowed_notes.items()):
                    if nb.get("friend") == friend and nb.get("source") == "ocr":
                        borrowed_notes.pop(nid_str, None)
        if not matched_nid:
            for nid_str, nb in borrowed_notes.items():
                if int(nb.get("actor_id", 0)) == aid and abs(int(nb.get("score", 0)) - score) <= 1 and nb.get("friend") == friend:
                    matched_nid = nid_str
                    break
        if matched_nid:
            borrowed_notes[matched_nid] = _merge_note_details(borrowed_notes[matched_nid], vn)
        else:
            nid_str = str(vn.get("note_id") or f"v_borrow_{friend}_{aid}_{score}")
            vn["note_id"] = nid_str
            borrowed_notes[nid_str] = vn


CARD_AVATAR_DIR = os.path.join(BASE_DIR, "data", "hoopgirls", "card_avatars")
COMFY_PYTHON_BIN = os.path.expanduser("~/ComfyUI/.venv/bin/python")

# 38位已实装球员在笔记卡片左上角的「标准正面半身像」显著视觉特征速查表
CHAR_VISUAL_HINTS = (
    "• 1001 澪奈：金黄马尾辫+红色发带，白底红蓝边北空球衣\n"
    "• 1002 龙宫寺：深紫黑高马尾，白底北空球衣\n"
    "• 1003 近江：黑色短发+黑/红框眼镜，白底红字KITAKOU球衣\n"
    "• 1004 和泉：粉红色双马尾+白色花朵发饰，白底北空球衣\n"
    "• 1005 亚理纱：小麦肤色+深棕大波浪卷发，白底北空球衣\n"
    "• 1006 洺瑶：棕色短发+青色发箍，白红球衣\n"
    "• 1007 闪堂：银灰双马尾+红领结蓝灰校服/白球衣\n"
    "• 1009 克拉丽丝：金黄色高马尾+蓝色发绳，白红伊澄球衣\n"
    "• 1010 早濑：黑色短发+脸颊创可贴，白红伊澄球衣\n"
    "• 1011 羽田：银白短发+头顶橙色蝴蝶结，白红伊澄球衣\n"
    "• 1012 望月：浅亚麻灰金微卷短发，白红伊澄球衣\n"
    "• 1013 百目鬼：小麦肤色+黑发挑染金色长发，深蓝樱海球衣\n"
    "• 1014 藤松：橘红色长发，深蓝樱海球衣\n"
    "• 1015 绿川：小麦肤色+灰白挑染黑短发，深蓝樱海球衣\n"
    "• 1016 雨森：墨绿色齐肩发+白色发箍，黑白球衣\n"
    "• 1017 皆川：深棕色中长发+呆毛，紫白明日叶球衣\n"
    "• 1018 毒岛：金棕色短发+发梢挑染，紫白明日叶球衣\n"
    "• 1019 名雪：浅灰棕色齐刘海短发，紫白明日叶球衣\n"
    "• 1020 佐久间：天蓝色短发+方框眼镜，紫白明日叶球衣\n"
    "• 1021 千圣：黑色高马尾长发+小麦肤色，紫色明日叶球衣（注意与闪堂/卧云区分！）\n"
    "• 1022 稻叶：浅金灰齐肩发+左侧黑色一字发夹，蓝黄樱海球衣\n"
    "• 1023 清水：金色波浪长发+紫色发饰，黑红圣菲洛斯球衣\n"
    "• 1024 榎本：小麦肤色+浅金短发丸子头，黑红圣菲洛斯球衣\n"
    "• 1025 春澄：深蓝色短发，黑红圣菲洛斯球衣\n"
    "• 1026 森田：浅灰棕双马尾，白橙球衣\n"
    "• 1027 美伽：浅棕色短发，黑红球衣\n"
    "• 1028 五十岚：深棕波波头短发，白红球衣\n"
    "• 1029 羽濑川：黑紫挑染短发+红色挑染，黑红圣菲洛斯球衣\n"
    "• 1030 真崎：棕色长发+红色圆框眼镜，黑蓝球衣\n"
    "• 1032 先斗：青绿色双马尾，黑白苍骑球衣\n"
    "• 1033 卧云：银灰紫长发+黑色发带，黑白苍骑球衣\n"
    "• 1037 野上：浅金粉白长发，白色圣山球衣\n"
    "• 1038 降旗：酒红色短发+黑色交叉发夹，黑蓝球衣\n"
    "• 1039 林原：灰褐色双马尾/长发，绿白青岚球衣\n"
    "• 1040 美星：深紫蓝色长发，绿白青岚球衣\n"
    "• 1042 远野：金橙色蓬松短发，白红球衣\n"
    "• 1045 白神：浅紫色双麻花辫，白紫圣山球衣\n"
    "• 1046 希雅：蓝灰色长卷发+齐刘海，白蓝圣山球衣"
)


def _pre_annotate_card_avatars(img_path: str) -> str:
    """
    使用本地 OpenCV 模板匹配 (data/hoopgirls/card_avatars/*.png + portrait_head_1007_闪堂.png)，
    在截图中的笔记卡片左上角立绘处进行像素级匹配（相关系数 >= 0.82）。
    若命中已知球员模板，直接在卡片左上角打上醒目的黑底黄字标签『【角色:千圣】』，生成临时标注图供 gemini-api 100% 零误差读取。
    """
    if not os.path.exists(COMFY_PYTHON_BIN) or not os.path.exists(img_path):
        return img_path
    out_annotated = f"{img_path}_annotated.jpg"
    py_code = r"""
import cv2, glob, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

cv2.setNumThreads(2)
img_path, av_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
scr = cv2.imread(img_path)
if scr is None:
    sys.exit(1)

templates = []
for p in sorted(glob.glob(os.path.join(av_dir, "*.png"))):
    base = os.path.basename(p)[:-4]
    if "_" not in base:
        continue
    cid_s, sname = base.split("_", 1)
    tpl = cv2.imread(p)
    if tpl is not None and tpl.shape[0] >= 30 and tpl.shape[1] >= 30:
        # 裁取中间偏上核心人脸立绘区域，避开底部属性文字与右上角『租借』角标
        h, w = tpl.shape[:2]
        crop = tpl[int(h*0.06):int(h*0.76), int(w*0.08):int(w*0.84)]
        templates.append((int(cid_s), sname, crop))

if not templates:
    sys.exit(1)

H, W = scr.shape[:2]
hits = []
for cid, sname, crop in templates:
    ch, cw = crop.shape[:2]
    for scale in (0.72, 0.78, 0.85, 0.92, 1.0, 1.08):
        tw, th = max(16, int(cw * scale)), max(16, int(ch * scale))
        t_res = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(scr, t_res, cv2.TM_CCOEFF_NORMED)
        loc = np.where(res >= 0.80)
        for pt in zip(*loc[::-1]):
            val = float(res[pt[1], pt[0]])
            hits.append((val, cid, sname, int(pt[0]), int(pt[1])))

hits.sort(reverse=True)
kept = []
for val, cid, sname, x, y in hits:
    if all(abs(x - kx) > 45 or abs(y - ky) > 45 for _, _, _, kx, ky in kept):
        kept.append((val, cid, sname, x, y))

if not kept:
    sys.exit(1)

pil_im = Image.open(img_path).convert("RGB")
draw = ImageDraw.Draw(pil_im)
font = None
for fp in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"]:
    if os.path.exists(fp):
        font = ImageFont.truetype(fp, 15)
        break
if not font:
    font = ImageFont.load_default()

for val, cid, sname, x, y in kept:
    tag = f"【{sname}】"
    bx0, by0 = max(0, x - 4), max(0, y - 18)
    draw.rectangle([bx0, by0, bx0 + 68, by0 + 20], fill=(15, 23, 42), outline=(250, 204, 21), width=1)
    draw.text((bx0 + 3, by0 + 1), tag, fill=(254, 240, 138), font=font)

pil_im.save(out_path, quality=88)
"""
    try:
        r = subprocess.run(
            [COMFY_PYTHON_BIN, "-c", py_code, img_path, CARD_AVATAR_DIR, out_annotated],
            capture_output=True,
            timeout=12.0,
        )
        if r.returncode == 0 and os.path.exists(out_annotated):
            return out_annotated
    except Exception:
        pass
    return img_path


def _discover_x11_displays() -> list[str]:
    ordered: list[str] = []
    env_disp = (os.environ.get("LQSN_DISPLAY") or "").strip()
    if env_disp:
        ordered.append(env_disp if env_disp.startswith(":") else f":{env_disp}")
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
        if d not in ordered:
            ordered.append(d)
    for d in (":1", ":0"):
        if d not in ordered:
            ordered.append(d)
    return ordered


def find_game_x11_window() -> Optional[tuple[str, str]]:
    """自动在独立 Xwayland 沙盒（如 :20）与主桌面 (:1 / :0) 上定位《篮球少女：飞跃巅峰》主窗口 (返回 (display, window_id))。"""
    for disp in _discover_x11_displays():
        try:
            res = subprocess.run(
                ["env", f"DISPLAY={disp}", "xwininfo", "-root", "-tree"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except Exception:
            continue
        candidates = []
        for line in res.stdout.splitlines():
            if "LQSNlauncher" in line or "Weixin" in line or "Steam" in line:
                continue
            if "篮球少女" in line or "steam_proton" in line:
                m = re.search(r"(0x[0-9a-f]+)\s+(\"[^\"]*\"|\(has no name\)):\s+\([^)]*\)\s+(\d+)x(\d+)", line)
                if m:
                    wid, name, w, h = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
                    if w >= 1000 and h >= 600:
                        priority = 0 if "篮球少女" in name else 1
                        candidates.append((priority, wid))
        candidates.sort()
        for _, wid in candidates:
            try:
                st = subprocess.run(
                    ["env", f"DISPLAY={disp}", "xwininfo", "-id", wid, "-stats"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                ).stdout
                if "IsViewable" in st:
                    return disp, wid
            except Exception:
                continue
    return None


def capture_game_screenshot(out_path: str = "/tmp/lqsn_note_scan.jpg") -> tuple[bool, Optional[list[int]]]:
    """
    通过 X11 import 零侵入截取游戏窗口画面 (<200KB)，并生成 16x16 灰度指纹用于画面变化检测。
    """
    loc = find_game_x11_window()
    if not loc:
        return False, None
    disp, wid = loc
    try:
        r = subprocess.run(
            ["env", f"DISPLAY={disp}", "import", "-window", wid, "-resize", "1366x", "-quality", "85", out_path],
            capture_output=True,
            timeout=3.0,
        )
        if r.returncode != 0 or not os.path.exists(out_path):
            return False, None
        # 计算右侧笔记列表核心区域 (在 1366x768 截图中为 460x360+885+155) 的 16x16 灰度指纹
        # 注意 ImageMagick 的 +X+Y 偏移量必须用像素值（不能加 %，否则会被当成 +55px 裁到左上角静态背景！）
        sig_proc = subprocess.run(
            [
                "convert",
                out_path,
                "-crop",
                "460x360+885+155",
                "+repage",
                "-colorspace",
                "Gray",
                "-resize",
                "16x16!",
                "-depth",
                "8",
                "txt:-",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        sig = []
        for line in sig_proc.stdout.splitlines():
            m = re.search(r"\((\d+)(?:,\d+)*\)", line)
            if m:
                sig.append(int(m.group(1)))
        return True, (sig if len(sig) == 256 else None)
    except Exception:
        return False, None


def screenshot_diff_score(sig1: Optional[list[int]], sig2: Optional[list[int]]) -> float:
    """计算两帧 16x16 灰度指纹的平均绝对像素差 (0~255)。若 < 2.2 说明画面基本静止。"""
    if not sig1 or not sig2 or len(sig1) != len(sig2):
        return 999.0
    return sum(abs(a - b) for a, b in zip(sig1, sig2)) / float(len(sig1))


def scan_notes_from_screen(
    img_path: Optional[str] = None,
    chars: Optional[dict] = None,
    entries_catalog: Optional[dict] = None,
    default_source: Optional[str] = None,
) -> dict:
    """
    截取或读取游戏画面，先经本地 OpenCV 头像模板预标注，再调用 gemini-api 提取画面中的自备/借用笔记与详情词条，
    自动写入 data/note_visual_cache.json 并刷新 data/note_pool.json。
    """
    if chars is None or entries_catalog is None:
        chars, _, _, entries_catalog = load_static_db()
    if not img_path:
        img_path = "/tmp/lqsn_note_scan.jpg"
        ok, _ = capture_game_screenshot(img_path)
        if not ok:
            return {"ok": False, "error": "未找到可见的《篮球少女》游戏窗口", "updated_count": 0}

    annotated_img_path = _pre_annotate_card_avatars(img_path)
    char_names_list = "、".join(f"{c['short_name']}({cid})" for cid, c in sorted(chars.items()))
    prompt = (
        "你是《篮球少女：飞跃巅峰》笔记画面识别器。请读取这张游戏截图，判断当前是否处于「选择训练笔记(我的笔记/好友借用/协会借用)」、「好友列表(含出借笔记)」、「笔记详情弹窗」或「笔记背包」界面。\n"
        f"已知全部球员简称与ID：{char_names_list}。\n"
        "⚠️ 角色头像识别铁律（必须严格遵守，防止张冠李戴）：\n"
        "1. 若某张笔记卡片左上角已经被预标注了黑底黄字标签（如『【千圣】』『【和泉】』『【近江】』），**直接采用该标签作为 char_name**！\n"
        "2. 在好友/协会借用列表或好友列表中，每个条目包含玩家个人头像与笔记卡片：**只有彩色长方形笔记卡片左上角的半身像**才是笔记所属球员；**圆形玩家头像框内是好友玩家的个人头像，严禁将其当作笔记角色**！\n"
        "3. 对于未预标注的卡片，请严格对照以下《卡片左上角正面半身像特征表》确认角色（切勿把黑高马尾紫球衣的『千圣』误认为灰双马尾的『闪堂』）：\n"
        f"{CHAR_VISUAL_HINTS}\n"
        "请提取画面中所有能看清『评分』数字的笔记卡片（底部被截断看不到评分数字的卡片请忽略），以及右侧或弹窗中展示的完整词条详情（特别注意『XX契合』词条及其星级1~3★、属性上限如『意识上限+165』、基础属性如『弹跳+72』、技能词条）。\n"
        "判断笔记来源 source：\n"
        "- 注意『选择笔记』画面左侧槽位中已装备的 1~2 张无好友昵称卡片是玩家自己的自备笔记，若顶部无『租借』红标则标为 source=\"self\"；\n"
        "- 带有好友/协会成员昵称（如 haruto、九曜、tes、小羊爱吃鱼、不解语、落翼之秋 等）或『租借』角标的卡片必须标为 source=\"borrow\" 并填写 friend_name；\n"
        "- 若处于『我的』列表或笔记背包且无好友昵称，则 source=\"self\"。\n"
        "只输出严格合法的 JSON（不要附加解释文字），格式如下：\n"
        "{\n"
        "  \"screen_type\": \"note_select_borrow | note_select_own | friend_list | note_detail_popup | note_bag | other\",\n"
        "  \"notes\": [\n"
        "    {\n"
        "      \"char_name\": \"千圣\",\n"
        "      \"score\": 607,\n"
        "      \"rank\": 7,\n"
        "      \"source\": \"borrow\",\n"
        "      \"friend_name\": \"九曜\",\n"
        "      \"entries\": [\n"
        "        {\"name\": \"投射\", \"star\": 3, \"value\": 75},\n"
        "        {\"name\": \"体能上限\", \"star\": 3, \"value\": 166}\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )
    try:
        proc = subprocess.run(
            [GEMINI_API_BIN, "analyze", "-i", annotated_img_path, prompt],
            capture_output=True,
            text=True,
            timeout=25.0,
        )
        out = proc.stdout
    except Exception as e:
        return {"ok": False, "error": f"gemini-api 调用失败: {e}", "updated_count": 0}

    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return {"ok": False, "error": "未解析出 JSON", "updated_count": 0}
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析错误: {e}", "updated_count": 0}

    screen_type = parsed.get("screen_type", "other")
    raw_notes = parsed.get("notes") or []
    if screen_type == "other" or not raw_notes:
        return {"ok": True, "screen_type": screen_type, "updated_count": 0, "summary": "当前画面无笔记数据"}

    with _CACHE_LOCK:
        # 先加载一次已有笔记池，用已有记录防止把同分自备笔记在不同滚动帧里重复克隆成多个角色
        gt_pool = sync_note_and_actor_pool(chars, entries_catalog)
        gt_own_by_score = {}
        for nid_s, n_obj in gt_pool.get("own_notes", {}).items():
            if not str(nid_s).startswith("v_"):
                gt_own_by_score.setdefault(int(n_obj.get("score", 0)), []).append(n_obj)
        for nid_s, n_obj in gt_pool.get("own_notes", {}).items():
            sc_k = int(n_obj.get("score", 0))
            if sc_k not in gt_own_by_score:
                gt_own_by_score.setdefault(sc_k, []).append(n_obj)
        gt_borrow_by_friend_score = {}
        for nid_s, nb_obj in gt_pool.get("borrowed_notes", {}).items():
            if not str(nid_s).startswith("v_") and nb_obj.get("friend"):
                gt_borrow_by_friend_score[(nb_obj["friend"], int(nb_obj.get("score", 0)))] = nb_obj

        vcache = _load_visual_cache()
        v_own = vcache.setdefault("own_notes", {})
        v_borrow = vcache.setdefault("borrowed_notes", {})
        now_tag = time.strftime("visual_%m_%d_%H_%M")
        updated_items = []

        for rn in raw_notes:
            cname = str(rn.get("char_name") or "").strip().strip("【】")
            score = int(rn.get("score") or 0)
            # 忽略 <250 分的低阶蓝/绿/紫凑数过渡笔记（5阶金起评分为 250+）
            if not cname or score < 250:
                continue
            aid = resolve_character_id(cname, chars)
            if not aid:
                continue
            friend = str(rn.get("friend_name") or "").strip()
            if friend in ("好友", "好友助战", "助战", "协会", "COACH"):
                friend = ""
            src = str(rn.get("source") or default_source or ("borrow" if friend else "self")).lower()
            if friend:
                src = "borrow"
            elif src == "borrow" and not friend:
                # 借用列表左侧槽位展示的是已装备的自备笔记（无好友昵称），不混入好友助战池
                src = "self"

            # 若与日志真值同分（自备同分，或同好友+同分），强制采用日志真值的 actor_id
            if src == "self" and score in gt_own_by_score and len(gt_own_by_score[score]) == 1:
                aid = int(gt_own_by_score[score][0]["actor_id"])
            elif src == "borrow" and (friend, score) in gt_borrow_by_friend_score:
                aid = int(gt_borrow_by_friend_score[(friend, score)]["actor_id"])

            short_name = chars[aid]["short_name"]
            # 严格按分数段校准笔记品阶（防止视觉模型把紫色边框误判为 7 阶彩）
            if score >= 510:
                rank = 7
            elif score >= 340:
                rank = 6
            elif score >= 250:
                rank = 5
            else:
                rank = 4

            harmony_targets = {}
            stat_bonuses = {}
            cap_bonuses = {}
            skill_entries = []
            for ent in rn.get("entries") or []:
                ename = str(ent.get("name") or "").strip()
                estar = max(1, min(3, int(ent.get("star") or 1)))
                eval_num = int(ent.get("value") or 0)
                if not ename:
                    continue
                if "契合" in ename:
                    target_prefix = ename.replace("契合", "").strip()
                    tid = resolve_character_id(target_prefix, chars)
                    if tid:
                        harmony_targets[str(tid)] = max(harmony_targets.get(str(tid), 0), estar)
                elif "上限" in ename:
                    for sid, sname in STAT_NAMES.items():
                        if sname in ename:
                            cap_bonuses[str(sid)] = eval_num if eval_num > 0 else {1: 45, 2: 95, 3: 155}.get(estar, 60)
                            break
                else:
                    matched_stat = False
                    for sid, sname in STAT_NAMES.items():
                        if ename == sname or ename.startswith(sname + "+"):
                            stat_bonuses[str(sid)] = eval_num if eval_num > 0 else {1: 20, 2: 45, 3: 70}.get(estar, 35)
                            matched_stat = True
                            break
                    if not matched_stat:
                        skill_entries.append({"id": 0, "name": ename, "star": estar, "type": 4})

            note_obj = {
                "note_id": f"v_{src}_{friend or 'self'}_{aid}_{score}",
                "actor_id": aid,
                "actor_name": short_name,
                "rank": rank,
                "score": score,
                "entry_num": len(rn.get("entries") or []),
                "last_seen": now_tag,
                "source": "ocr",
                "harmony_star": max(harmony_targets.values()) if harmony_targets else 0,
                "harmony_targets": harmony_targets,
                "stat_bonuses": stat_bonuses,
                "cap_bonuses": cap_bonuses,
                "skill_entries": skill_entries,
                "top_entries_str": "视觉扫描同步",
            }

            if src == "borrow":
                note_obj["friend"] = friend or "好友助战"
                # 若同一好友已有缓存键，移除旧键后写入最新记录
                if friend:
                    for old_k, old_v in list(v_borrow.items()):
                        if old_v.get("friend") == friend and old_k != f"{friend}_{aid}_{score}":
                            v_borrow.pop(old_k, None)
                vkey = f"{friend or 'borrow'}_{aid}_{score}"
                v_borrow[vkey] = _merge_note_details(v_borrow.get(vkey, {}), note_obj)
                updated_items.append(f"借:{note_obj['friend']} {short_name}({score}分)")
            else:
                vkey = f"own_{aid}_{score}"
                v_own[vkey] = _merge_note_details(v_own.get(vkey, {}), note_obj)
                updated_items.append(f"自备:{short_name}({score}分)")

        vcache["updated_at"] = now_tag
        _save_visual_cache(vcache)
        sync_note_and_actor_pool(chars, entries_catalog)
    return {
        "ok": True,
        "screen_type": screen_type,
        "updated_count": len(updated_items),
        "updated_items": updated_items,
        "summary": f"📸 画面同步 {len(updated_items)} 本笔记：{' / '.join(updated_items[:6])}",
    }


def _detect_current_note_ui_mode() -> str:
    """
    通过最新日志尾部判断当前打开的是哪个笔记相关界面：
    - 'formation_select': 开局「选择笔记」界面（含『我的 / 好友 / 协会』三个标签页）
    - 'note_bag': 主界面「训练笔记」背包界面（仅自备笔记列表）
    - 'friend_list': 主界面「好友列表」界面（可扫好友出借笔记）
    """
    logs = sorted(glob.glob(os.path.join(LOG_DIR, "*.txt")))
    if not logs:
        return "formation_select"
    try:
        lp = logs[-1]
        with open(lp, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(max(0, os.path.getsize(lp) - 250000))
            tail = f.read()
        pos_sel_open = tail.rfind("OpenUIView: formation_train_note_select_view")
        pos_sel_close = tail.rfind("CloseUIView: formation_train_note_select_view")
        if pos_sel_open > pos_sel_close:
            return "formation_select"

        pos_bag_open = tail.rfind("OpenUIView: train_note_main_view_v2")
        pos_bag_close = tail.rfind("CloseUIView: train_note_main_view_v2")
        if pos_bag_open > pos_bag_close:
            return "note_bag"

        pos_fr_open = tail.rfind("friend.cs_get_friend_tbl")
        if pos_fr_open != -1 and len(tail) - pos_fr_open < 60000:
            return "friend_list"
    except Exception:
        pass
    return "formation_select"


def _click_game_rel(
    disp: str, wid: str, gx: int, gy: int, gw: int, gh: int, rx: float, ry: float, wait_s: float = 0.55
) -> None:
    """先聚焦游戏窗口 (windowfocus)，再在相对坐标 (rx, ry) 处点击鼠标左键。"""
    tx = gx + int(gw * rx)
    ty = gy + int(gh * ry)
    subprocess.run(
        [
            "env",
            f"DISPLAY={disp}",
            "xdotool",
            "windowfocus",
            wid,
            "mousemove",
            str(tx),
            str(ty),
            "sleep",
            "0.08",
            "click",
            "1",
        ],
        capture_output=True,
        timeout=2.5,
    )
    if wait_s > 0:
        time.sleep(wait_s)


def _is_tab_pill_blue(img_path: str, check_rx: float, check_ry: float = 0.080) -> bool:
    """
    检查「选择笔记」右上角指定标签页胶囊按钮（check_rx: 我的=0.680, 好友=0.785, 协会=0.885）是否处于亮蓝色激活态。
    实测激活态 RGB=(14, 144, 230) [B - R > 100]，未激活态 RGB=(185, 199, 202) [B - R < 30]。
    """
    if not os.path.exists(img_path):
        return False
    try:
        out = subprocess.check_output(
            [
                "convert",
                img_path,
                "-format",
                "%[fx:int(w*" + f"{check_rx:.4f}" + ")],%[fx:int(h*" + f"{check_ry:.4f}" + ")]",
                "info:",
            ],
            text=True,
            timeout=1.5,
        ).strip()
        px, py = [int(x) for x in out.split(",")]
        txt = subprocess.check_output(
            ["convert", img_path, "-crop", f"1x1+{px}+{py}", "+repage", "-depth", "8", "txt:-"],
            text=True,
            timeout=1.5,
        )
        m = re.search(r"\((\d+),(\d+),(\d+)", txt)
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return (b - r) > 100 and b > 170
    except Exception:
        pass
    return False


def _ensure_tab_selected(
    disp: str,
    wid: str,
    gx: int,
    gy: int,
    gw: int,
    gh: int,
    t_label: str,
    click_rx: float,
    click_ry: float,
    check_rx: float,
    max_attempts: int = 12,
) -> bool:
    """
    闭环切换标签页：点击目标标签并校验按钮像素是否变为亮蓝色激活态。
    若因窗口刚切回前台吞掉首次点击，自动每隔 0.45s 重试点击，直到 100% 确认切入该栏。
    """
    probe_img = "/tmp/lqsn_tab_probe.jpg"
    for attempt in range(max_attempts):
        if attempt == 0:
            ok, _ = capture_game_screenshot(probe_img)
            if ok and _is_tab_pill_blue(probe_img, check_rx, click_ry):
                return True
        _click_game_rel(disp, wid, gx, gy, gw, gh, click_rx, click_ry, wait_s=0.40)
        ok, _ = capture_game_screenshot(probe_img)
        if ok and _is_tab_pill_blue(probe_img, check_rx, click_ry):
            return True
        print(f"  🔄 [{t_label}] 尚未激活（等待窗口焦点或重试试点 {attempt + 1}/{max_attempts}）...")
    return False


def _scroll_game_list_down(
    disp: str,
    wid: str,
    gx: int,
    gy: int,
    gw: int,
    gh: int,
    rx: float = 0.818,
    ry_from: float = 0.60,
    ry_to: float = 0.23,
) -> None:
    """
    在游戏列表区域通过「鼠标左键按住平滑上拉拖拽 + 末端悬停 0.18s 消惯性」精准向下翻两整行（Unity ScrollRect 专用）。
    rx=0.818 位于笔记列表两列之间的中缝，不会误触任何卡片或详情按钮。
    """
    sx = gx + int(gw * rx)
    y1 = gy + int(gh * ry_from)
    y2 = gy + int(gh * ry_to)
    steps = 10
    cmd = [
        "env",
        f"DISPLAY={disp}",
        "xdotool",
        "windowfocus",
        wid,
        "mousemove",
        str(sx),
        str(y1),
        "sleep",
        "0.08",
        "mousedown",
        "1",
        "sleep",
        "0.05",
    ]
    for i in range(1, steps + 1):
        yi = int(y1 + (y2 - y1) * (i / float(steps)))
        cmd.extend(["mousemove", str(sx), str(yi), "sleep", "0.025"])
    cmd.extend(["sleep", "0.18", "mouseup", "1"])
    try:
        subprocess.run(cmd, capture_output=True, timeout=3.5)
        time.sleep(0.45)
    except Exception:
        pass


def _scroll_game_list_to_top(
    disp: str,
    wid: str,
    gx: int,
    gy: int,
    gw: int,
    gh: int,
    rx: float = 0.818,
    max_pulls: int = 10,
) -> bool:
    """
    闭环归零：在列表区域平滑向下拉动（从 ry=0.25 拉到 ry=0.64），每拉一次检测列表画面差分，
    直到连续两帧差分 < 1.8（100% 确认已回到第 1 页起跑线）才返回。
    返回 True 表示期间画面发生过滚动，False 表示原本就在最顶部（或窗口被遮挡）。
    """
    sx = gx + int(gw * rx)
    y1 = gy + int(gh * 0.25)
    y2 = gy + int(gh * 0.64)
    _, last_sig = capture_game_screenshot("/tmp/lqsn_top_check.jpg")
    moved_any = False
    for _ in range(max_pulls):
        cmd = [
            "env",
            f"DISPLAY={disp}",
            "xdotool",
            "windowfocus",
            wid,
            "mousemove",
            str(sx),
            str(y1),
            "sleep",
            "0.05",
            "mousedown",
            "1",
            "sleep",
            "0.04",
        ]
        steps = 8
        for s in range(1, steps + 1):
            yi = int(y1 + (y2 - y1) * (s / float(steps)))
            cmd.extend(["mousemove", str(sx), str(yi), "sleep", "0.02"])
        cmd.extend(["sleep", "0.14", "mouseup", "1"])
        try:
            subprocess.run(cmd, capture_output=True, timeout=3.0)
            time.sleep(0.35)
        except Exception:
            break
        ok, sig = capture_game_screenshot("/tmp/lqsn_top_check.jpg")
        if not ok or not sig:
            break
        diff_v = screenshot_diff_score(sig, last_sig) if last_sig is not None else 0.0
        last_sig = sig
        if diff_v >= 1.8:
            moved_any = True
        else:
            # 列表已不再向上移动，确认已回到起跑线顶部！
            break
    return moved_any


def scan_notes_scrolling(
    pages: int = 10,
    chars: Optional[dict] = None,
    entries_catalog: Optional[dict] = None,
    auto_tabs: bool = False,
    countdown_sec: float = 0.0,
    progress_cb=None,
) -> dict:
    """
    自动在游戏笔记列表先归零到顶部起跑线，再向下滚动翻页到底并连续截取多页合并扫描。
    当 auto_tabs=True 且处于「选择笔记」界面时，自动依次点击『我的』➔『好友』➔『协会』三个标签页，
    每一栏都先闭环归零到第 1 行，再一路翻到底截屏，随后统一在后台并发识别入库。
    """
    if chars is None or entries_catalog is None:
        chars, _, _, entries_catalog = load_static_db()
    loc = find_game_x11_window()
    if not loc:
        return {"ok": False, "error": "未找到可见的《篮球少女》游戏窗口", "updated_count": 0}
    disp, wid = loc

    if countdown_sec > 0:
        msg = f"请在 {countdown_sec:.0f} 秒内确保《篮球少女》窗口在前台未被网页遮挡，即将自动翻页扫笔记！"
        print(f"⏳ {msg}")
        try:
            subprocess.run(["notify-send", "-t", "3000", "🏀 星奈军师 · 自动扫笔记", msg], capture_output=True)
        except Exception:
            pass
        time.sleep(countdown_sec)

    gx, gy, gw, gh = 0, 0, 1920, 1080
    try:
        st = subprocess.check_output(["env", f"DISPLAY={disp}", "xwininfo", "-id", wid], text=True, timeout=1.5)
        mx = re.search(r"Absolute upper-left X:\s+(-?\d+)", st)
        my = re.search(r"Absolute upper-left Y:\s+(-?\d+)", st)
        mw = re.search(r"Width:\s+(\d+)", st)
        mh = re.search(r"Height:\s+(\d+)", st)
        if mx and my and mw and mh:
            gx, gy, gw, gh = int(mx.group(1)), int(my.group(1)), int(mw.group(1)), int(mh.group(1))
    except Exception:
        pass

    ui_mode = _detect_current_note_ui_mode()
    captured_Queue = []  # [(img_path, default_source, tab_label)]

    if auto_tabs and ui_mode == "formation_select":
        max_cap_pages = max(10, pages)
        # (标签名, 点击中心rx, 点击中心ry, 蓝色胶囊校验点rx, 默认来源, 最大页数)
        tab_plan = [
            ("我的自备", 0.714, 0.080, 0.680, "self", max_cap_pages),
            ("好友助战", 0.816, 0.080, 0.785, "borrow", max_cap_pages),
            ("协会助战", 0.916, 0.080, 0.885, "borrow", max_cap_pages),
        ]
        print("🤖 正在游戏「选择笔记」界面自动切换【我的 ➔ 好友 ➔ 协会】，每栏闭环确认变蓝激活 ➔ 归零到顶 ➔ 一路翻到底抓屏...")
        for t_idx, (t_label, trx, try_, chk_rx, def_src, max_p) in enumerate(tab_plan):
            if progress_cb:
                progress_cb(f"⏳ 正在切到【{t_label}】并归零到第1页起跑线...")
            if not _ensure_tab_selected(disp, wid, gx, gy, gw, gh, t_label, trx, try_, chk_rx, max_attempts=12):
                return {
                    "ok": False,
                    "error": f"未能切换到【{t_label}】标签页（游戏窗口可能被浏览器等前台窗口遮挡），已安全中止！",
                    "updated_count": 0,
                }
            print(f"  🔵 [{t_label}] 标签已确认激活（亮蓝），正在归零到第 1 页起跑线...")
            _scroll_game_list_to_top(disp, wid, gx, gy, gw, gh, rx=0.818, max_pulls=10)
            last_sig = None
            for p_idx in range(max_p):
                if progress_cb:
                    progress_cb(f"📸 正在抓取【{t_label}】第 {p_idx + 1} 页...")
                img_p = f"/tmp/lqsn_auto_scan_t{t_idx}_p{p_idx + 1}.jpg"
                ok, sig = capture_game_screenshot(img_p)
                if not ok:
                    break
                diff_v = screenshot_diff_score(sig, last_sig) if last_sig is not None else 999.0
                if last_sig is not None and diff_v < 1.8:
                    print(f"  ✔ [{t_label}] 已检测到底部（共抓 {p_idx} 页，触底差分={diff_v:.2f}），切换下一栏！")
                    break
                last_sig = sig
                captured_Queue.append((img_p, def_src, f"{t_label}第{p_idx + 1}页"))
                if p_idx + 1 < max_p:
                    _scroll_game_list_down(disp, wid, gx, gy, gw, gh, rx=0.818, ry_from=0.60, ry_to=0.23)
        # 抓屏完成后切回「我的」标签页并回到顶部起跑线，方便玩家后续直接选笔记
        _ensure_tab_selected(disp, wid, gx, gy, gw, gh, "我的自备", 0.714, 0.080, 0.680, max_attempts=4)
        _scroll_game_list_to_top(disp, wid, gx, gy, gw, gh, rx=0.818, max_pulls=10)
    else:
        rx_scroll = 0.60 if ui_mode == "friend_list" else 0.818
        ry_from = 0.72 if ui_mode == "friend_list" else 0.60
        ry_to = 0.28 if ui_mode == "friend_list" else 0.23
        def_src = "borrow" if ui_mode == "friend_list" else None
        print(f"🤖 正在当前列表先归零到顶，再向下拖拽翻页抓屏（最多 {max(1, pages)} 页，期间请勿移动鼠标）...")
        _scroll_game_list_to_top(disp, wid, gx, gy, gw, gh, rx=rx_scroll, max_pulls=10)
        last_sig = None
        for p_idx in range(max(1, pages)):
            img_p = f"/tmp/lqsn_note_scan_p{p_idx + 1}.jpg"
            ok, sig = capture_game_screenshot(img_p)
            if not ok:
                break
            if last_sig is not None and screenshot_diff_score(sig, last_sig) < 1.8:
                print(f"  ✔ 当前列表已检测到底部（共抓 {p_idx} 页）！")
                break
            last_sig = sig
            captured_Queue.append((img_p, def_src, f"第{p_idx + 1}页"))
            if p_idx + 1 < pages:
                _scroll_game_list_down(disp, wid, gx, gy, gw, gh, rx=rx_scroll, ry_from=ry_from, ry_to=ry_to)

    print(f"✅ 画面抓取完毕（共 {len(captured_Queue)} 屏），鼠标已释放！正在后台并发识别笔记入库...")
    from concurrent.futures import ThreadPoolExecutor

    def _scan_worker(item):
        idx, (img_p, def_src, page_tag) = item
        print(f"  ⏳ [{idx}/{len(captured_Queue)}] 正在解析 {page_tag}...")
        return scan_notes_from_screen(img_p, chars, entries_catalog, default_source=def_src)

    all_updated = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(_scan_worker, enumerate(captured_Queue, 1)))
    for res in results:
        if res.get("ok") and res.get("updated_items"):
            for it in res["updated_items"]:
                if it not in all_updated:
                    all_updated.append(it)

    return {
        "ok": True,
        "updated_count": len(all_updated),
        "updated_items": all_updated,
        "summary": f"📸 自动翻页共扫入/更新 {len(all_updated)} 本笔记：{' / '.join(all_updated[:10])}",
    }


def evaluate_compatibility(core_id: int, note1: dict, note2: dict, rel_scores: dict, rules: dict, rec_stats: Optional[list] = None) -> dict:
    """
    精确计算主力球员 core_id 携带 note1 + note2 时的：
    - 基础角色关系分 (同名记 0 分)
    - 契合度词条加成 (1★+2 / 2★+5 / 3★+10)
    - 总相性分、相性等级 (无/差/一般/良/优/极)、开局传承学习次数 (1~5次)
    - 单次触发词条数区间 (min~max) 与全场期望触发总词条数
    - 综合传承期望评分 (eval_score)：兼顾档位阶跃、笔记评分与核心属性/上限因子契合度
    """
    a1 = int(note1["actor_id"])
    a2 = int(note2["actor_id"])
    rel_map = rel_scores.get(int(core_id), {})
    r1 = 0 if a1 == core_id else int(rel_map.get(a1, 0))
    r2 = 0 if a2 == core_id else int(rel_map.get(a2, 0))
    s1 = int(note1.get("harmony_targets", {}).get(str(core_id), 0))
    s2 = int(note2.get("harmony_targets", {}).get(str(core_id), 0))
    b1 = HARMONY_BONUS_BY_STAR.get(s1, 0)
    b2 = HARMONY_BONUS_BY_STAR.get(s2, 0)

    relation_total = r1 + r2
    harmony_bonus = b1 + b2
    total_compat = relation_total + harmony_bonus

    c_cfg = rules.get("compatibility_and_learn_times", {})
    levels = c_cfg.get("harmony_levels", [
        {"level": 1, "rank_name": "无", "min_compatibility_score": 0, "inheritance_learn_times": 1},
        {"level": 2, "rank_name": "差", "min_compatibility_score": 1, "inheritance_learn_times": 2},
        {"level": 3, "rank_name": "一般", "min_compatibility_score": 10, "inheritance_learn_times": 3},
        {"level": 4, "rank_name": "良", "min_compatibility_score": 20, "inheritance_learn_times": 4},
        {"level": 5, "rank_name": "优", "min_compatibility_score": 30, "inheritance_learn_times": 5},
        {"level": 6, "rank_name": "极", "min_compatibility_score": 45, "inheritance_learn_times": 5},
    ])
    triggers = c_cfg.get("entry_trigger_count_by_compatibility", [
        {"required_compatibility_score": 0, "min_triggered_entries": 2, "max_triggered_entries": 4},
        {"required_compatibility_score": 5, "min_triggered_entries": 3, "max_triggered_entries": 5},
        {"required_compatibility_score": 10, "min_triggered_entries": 4, "max_triggered_entries": 6},
        {"required_compatibility_score": 15, "min_triggered_entries": 4, "max_triggered_entries": 7},
        {"required_compatibility_score": 20, "min_triggered_entries": 4, "max_triggered_entries": 8},
        {"required_compatibility_score": 40, "min_triggered_entries": 5, "max_triggered_entries": 10},
        {"required_compatibility_score": 60, "min_triggered_entries": 5, "max_triggered_entries": 11},
    ])

    cur_level = levels[0]
    next_level = None
    for idx, lv in enumerate(levels):
        if total_compat >= int(lv["min_compatibility_score"]):
            cur_level = lv
            next_level = levels[idx + 1] if idx + 1 < len(levels) else None

    cur_trig = triggers[0]
    for tr in triggers:
        if total_compat >= int(tr["required_compatibility_score"]):
            cur_trig = tr

    learn_times = int(cur_level["inheritance_learn_times"])
    avg_trig = (int(cur_trig["min_triggered_entries"]) + int(cur_trig["max_triggered_entries"])) / 2.0
    expected_total_triggers = learn_times * avg_trig

    # 计算目标角色的核心属性与上限命中加分
    rec_set = set(int(x) for x in (rec_stats or []))
    matched_cap_gain = 0
    total_cap_gain = 0
    matched_stat_gain = 0
    for n in (note1, note2):
        for k_str, v in n.get("cap_bonuses", {}).items():
            k, val = int(k_str), int(v)
            total_cap_gain += val
            if k in rec_set:
                matched_cap_gain += val
        for k_str, v in n.get("stat_bonuses", {}).items():
            k, val = int(k_str), int(v)
            if k in rec_set:
                matched_stat_gain += val

    avg_note_score = (int(note1["score"]) + int(note2["score"])) / 2.0
    # 综合评分（实证校准）：
    # - 30分（优）：学习次数达 5 次封顶（单次 4~9 词条）
    # - 40分（优高段）与 45~59分（极）：同为 5 次学习 × 单次 5~10 词条！45分『极』仅为视觉档位，无额外触发词条，
    #   故 40~59 区间锁平阶跃奖励（仅留 0.15/分微弱平局决胜），严防 55 分低质种马反超 42~46 分三金上限神种马！
    # - 60分（隐藏极高段）：单次词条上限跃升至 5~11 条，给予真实阶跃加分。
    if total_compat >= 60:
        tier_bonus = 64.0 + (total_compat - 60) * 0.15
        compat_tier_note = "🔥60隐藏满档(5~11条)"
    elif total_compat >= 40:
        tier_bonus = 54.0 + (total_compat - 40) * 0.15
        compat_tier_note = "⚡40档词条已满(=极收益)" if total_compat < 45 else "⚡满档收益"
    elif total_compat >= 30:
        tier_bonus = 30.0 + (total_compat - 30) * 1.8
        compat_tier_note = f"差{40 - total_compat}分进40满词条档"
    elif total_compat >= 15:
        tier_bonus = 10.0 + (total_compat - 15) * 1.1
        compat_tier_note = f"差{30 - total_compat}分满5次学习" if total_compat >= 24 else ""
    else:
        tier_bonus = total_compat * 0.6
        compat_tier_note = ""

    eval_score = (
        expected_total_triggers * 8.0
        + tier_bonus
        + avg_note_score * 0.35
        + matched_cap_gain * 0.12
        + total_cap_gain * 0.05
        + matched_stat_gain * 0.10
    )

    return {
        "core_id": core_id,
        "note1": note1,
        "note2": note2,
        "r1": r1,
        "r2": r2,
        "s1": s1,
        "s2": s2,
        "b1": b1,
        "b2": b2,
        "relation_total": relation_total,
        "harmony_bonus": harmony_bonus,
        "total_compat": total_compat,
        "rank_name": cur_level["rank_name"],
        "level": int(cur_level["level"]),
        "learn_times": learn_times,
        "trig_min": int(cur_trig["min_triggered_entries"]),
        "trig_max": int(cur_trig["max_triggered_entries"]),
        "expected_triggers": round(expected_total_triggers, 1),
        "compat_tier_note": compat_tier_note,
        "next_rank_name": next_level["rank_name"] if next_level else None,
        "next_rank_need": (int(next_level["min_compatibility_score"]) - total_compat) if next_level else 0,
        "avg_note_score": round(avg_note_score, 1),
        "matched_cap_gain": matched_cap_gain,
        "total_cap_gain": total_cap_gain,
        "eval_score": round(eval_score, 1),
    }


NOTE_REVISION_WARNING = (
    "⚠️ 笔记修订避坑：两本修订【仅保留共同拥有的词条】，其余全部纯随机且【星级不叠加】（2★+1★可能降为1★，位置适性也会洗没）！"
    "严禁拿高分/三星词条好笔记修订，仅限两本废笔记赌狗。"
)


def recommend_auto_train_breeders(
    core_id: int,
    chars: dict,
    rel_scores: dict,
    pool: Optional[dict] = None,
    top_k: int = 5,
) -> dict:
    """
    基于社区实测铁律「自动训练不会产出第二位置词条」，结合全量 rel_scores 相性矩阵，
    为目标核心球员 core_id 找出最适合每天自动训练 5 把定向刷 7 阶彩笔记的「原生同位置 + 最高契合度」种马。
    """
    char_obj = chars.get(int(core_id), {})
    best_pos = str(char_obj.get("best_pos") or "").strip()
    pos_code = best_pos.split()[0] if best_pos else ""
    rel_map = rel_scores.get(int(core_id), {})
    owned_ids = set(int(x) for x in (pool or {}).get("owned_actors", []))

    best_own_by_actor: dict[int, dict] = {}
    for n in (pool or {}).get("own_notes", {}).values():
        aid = int(n.get("actor_id", 0))
        sc = int(n.get("score", 0))
        if aid not in best_own_by_actor or sc > int(best_own_by_actor[aid].get("score", 0)):
            best_own_by_actor[aid] = n

    same_pos_candidates = []
    for cid, cinfo in chars.items():
        if int(cid) == int(core_id) or not cinfo.get("released", True):
            continue
        c_pos = str(cinfo.get("best_pos") or "").strip()
        c_code = c_pos.split()[0] if c_pos else ""
        if not pos_code or c_code != pos_code:
            continue
        r_to_core = int(rel_map.get(int(cid), 0))
        r_from_core = int(rel_scores.get(int(cid), {}).get(int(core_id), 0))
        own_note = best_own_by_actor.get(int(cid))
        same_pos_candidates.append({
            "actor_id": int(cid),
            "actor_name": cinfo.get("short_name", str(cid)),
            "show_name": cinfo.get("show_name", cinfo.get("short_name", str(cid))),
            "best_pos": c_pos,
            "rel_to_core": r_to_core,
            "rel_from_core": r_from_core,
            "is_owned": int(cid) in owned_ids,
            "best_own_score": int(own_note.get("score", 0)) if own_note else 0,
            "best_own_rank": int(own_note.get("rank", 0)) if own_note else 0,
            "rec_train": list(cinfo.get("recommend_train") or []),
        })

    same_pos_all = sorted(
        same_pos_candidates,
        key=lambda x: (-x["rel_to_core"], -int(x["is_owned"]), -x["rel_from_core"], x["actor_id"]),
    )
    same_pos_owned = [x for x in same_pos_all if x["is_owned"]]
    return {
        "core_id": int(core_id),
        "core_name": char_obj.get("short_name", f"球员{core_id}"),
        "best_pos": best_pos,
        "pos_code": pos_code,
        "same_pos_owned": same_pos_owned[:top_k],
        "same_pos_all": same_pos_all[:top_k],
        "tip": (
            f"自动训练不出第二位置词条；每天自动 5 把原生同位({pos_code or '同位置'})最高契合种马，单卡 8500+ 记录成绩约 5% 出彩且天然免转位！"
        ),
    }


def format_auto_breeder_terminal(core_id: int, chars: dict, rel_scores: dict, pool: Optional[dict] = None) -> str:
    """独立终端报告：自动训练刷彩笔记最佳原生同位种马榜 + 笔记修订避坑提示。"""
    ab = recommend_auto_train_breeders(core_id, chars, rel_scores, pool, top_k=6)
    lines = [
        "=" * 90,
        f"🤖 【自动 5 把刷彩笔记 · 原生同位置最优种马榜】目标核心：{ab['core_name']} (ID:{ab['core_id']} · {ab['best_pos']})",
        f"📌 机制依据：{ab['tip']}",
        "-" * 90,
    ]
    if ab["same_pos_owned"]:
        lines.append("✅ 一、账号已解锁原生同位种马排行（按 rel_scores 严格验算，优先挂机刷彩）：")
        for idx, it in enumerate(ab["same_pos_owned"], 1):
            own_s = (
                f"现有自备 {QUALITY_NAMES.get(it['best_own_rank'], '')} {it['best_own_score']}分"
                if it["best_own_score"] > 0
                else "尚无高分自备"
            )
            lines.append(
                f"  #{idx} 【{it['actor_name']}({it['actor_id']}·{it['best_pos']})】 "
                f"给{ab['core_name']}天生相性 +{it['rel_to_core']} (反向+{it['rel_from_core']}) | 主练:{'/'.join(it['rec_train'])} | {own_s}"
            )
    else:
        lines.append("⚠️ 当前已解锁池中暂无同位置其他已实装球员，请参考下方全图鉴同位榜：")

    lines.append("\n🌐 二、全图鉴原生同位最高契合 Top 6（抽卡/借好友同位彩笔记参考）：")
    for idx, it in enumerate(ab["same_pos_all"], 1):
        mark = "✅已解锁" if it["is_owned"] else "🔒未解锁"
        lines.append(
            f"  #{idx} 【{it['actor_name']}({it['actor_id']}·{it['best_pos']})】[{mark}] "
            f"天生相性 +{it['rel_to_core']} (反向+{it['rel_from_core']}) | 主练:{'/'.join(it['rec_train'])}"
        )
    lines.append(f"\n{NOTE_REVISION_WARNING}")
    lines.append("💡 补充规则：彩位置适性实为 +10%（金位置 +5%）；笔升至 5~6 级后单卡 8500+ 分记录成绩约 5% 概率产出 7 阶彩笔记。")
    lines.append("=" * 90)
    return "\n".join(lines)


def recommend_notes_for_character(core_id: int, chars: dict, rules: dict, rel_scores: dict, pool: dict) -> dict:
    """为指定球员生成「1自备+1好友借用」、「2纯自备」及「助战列表定向淘金」三大最优方案。"""
    char_obj = chars.get(int(core_id), {"id": core_id, "short_name": f"球员{core_id}", "recommend_base_attr": []})
    rec_stats = get_char_rec_stat_ids(char_obj)
    min_rank = int(pool.get("min_kept_rank", 5))

    own_list = [
        n for n in pool.get("own_notes", {}).values()
        if int(n.get("rank", 0)) >= min_rank
    ]
    if len(own_list) < 2:
        own_list = list(pool.get("own_notes", {}).values())

    borrow_list = list(pool.get("borrowed_notes", {}).values())

    # 1. 纯自备组合 (2 Own Notes，且两本笔记原主人不可相同)
    pure_own_combos = []
    for i in range(len(own_list)):
        n1 = own_list[i]
        for j in range(i + 1, len(own_list)):
            n2 = own_list[j]
            if int(n1["actor_id"]) == int(n2["actor_id"]):
                continue
            ev = evaluate_compatibility(core_id, n1, n2, rel_scores, rules, rec_stats)
            pure_own_combos.append(ev)
    pure_own_combos.sort(key=lambda x: (-x["eval_score"], -x["total_compat"], -x["avg_note_score"]))

    # 2. 1自备 + 1已知好友借用组合
    borrow_combos = []
    for n1 in own_list:
        for nb in borrow_list:
            if int(n1["actor_id"]) == int(nb["actor_id"]):
                continue
            ev = evaluate_compatibility(core_id, n1, nb, rel_scores, rules, rec_stats)
            borrow_combos.append(ev)
    borrow_combos.sort(key=lambda x: (-x["eval_score"], -x["total_compat"], -x["avg_note_score"]))

    # 3. 助战淘金指南：固定手头最契合的 1 本自备笔记，推荐去好友/社团列表里搜哪几位高相性球员
    rel_map = rel_scores.get(int(core_id), {})
    best_single_own = None
    best_single_val = -1
    for n1 in own_list:
        aid = int(n1["actor_id"])
        r1 = 0 if aid == core_id else int(rel_map.get(aid, 0))
        s1 = int(n1.get("harmony_targets", {}).get(str(core_id), 0))
        b1 = HARMONY_BONUS_BY_STAR.get(s1, 0)
        v = (r1 + b1) * 15 + int(n1["score"]) * 0.2
        if v > best_single_val:
            best_single_val = v
            best_single_own = n1

    scout_targets = []
    if best_single_own:
        own_aid = int(best_single_own["actor_id"])
        s1_own = int(best_single_own.get("harmony_targets", {}).get(str(core_id), 0))
        b1_own = HARMONY_BONUS_BY_STAR.get(s1_own, 0)
        own_contrib = (0 if own_aid == core_id else int(rel_map.get(own_aid, 0))) + b1_own
        candidates = []
        for cid, cinfo in chars.items():
            if cid in (core_id, own_aid):
                continue
            if not cinfo.get("released", True):
                continue
            r_score = int(rel_map.get(cid, 0))
            candidates.append((r_score, cid, cinfo["short_name"]))
        candidates.sort(reverse=True)
        for r_score, cid, cname in candidates[:4]:
            base_total = own_contrib + r_score
            scout_targets.append({
                "actor_id": cid,
                "actor_name": cname,
                "rel_score": r_score,
                "own_note_actor": best_single_own["actor_name"],
                "own_note_score": best_single_own["score"],
                "own_contrib": own_contrib,
                "total_0star": base_total,
                "total_2star": base_total + 5,
                "total_3star": base_total + 10,
            })

    auto_breeders = recommend_auto_train_breeders(core_id, chars, rel_scores, pool, top_k=5)

    return {
        "character": char_obj,
        "rec_stats": rec_stats,
        "rec_stats_str": "/".join(STAT_NAMES.get(int(s), str(s)) for s in rec_stats),
        "own_count": len(own_list),
        "borrow_count": len(borrow_list),
        "borrow_combos": borrow_combos[:4],
        "pure_own_combos": pure_own_combos[:4],
        "scout_targets": scout_targets,
        "auto_breeders": auto_breeders,
        "revision_warning": NOTE_REVISION_WARNING,
    }


def format_character_note_advice_terminal(adv: dict) -> str:
    """生成终端高亮显示的「开局选人 · 最优笔记配对参谋」报告。"""
    c = adv["character"]
    cname = c.get("show_name") or c.get("short_name")
    pos = c.get("best_pos", "")
    rec_stats = adv["rec_stats"]
    rec_str = adv["rec_stats_str"]

    lines = [
        "=" * 88,
        f"📒 【开局选人 · 笔记最优配对参谋】目标球员：{cname} (ID:{c['id']} · {pos}) | 核心主属性：{rec_str}",
        "-" * 88,
    ]

    if adv["borrow_combos"]:
        lines.append("🔥 一、顶配借用方案（1自备 + 1已记录好友助战，冲高分首选）：")
        for idx, cb in enumerate(adv["borrow_combos"][:3], 1):
            n1, nb = cb["note1"], cb["note2"]
            h1 = f"专属契合{cb['s1']}★(+{cb['b1']})" if cb.get("b1") else "专属契合+0"
            h2 = f"专属契合{cb['s2']}★(+{cb['b2']})" if cb.get("b2") else "专属契合+0"
            hl1 = format_note_highlights(n1, rec_stats)
            hl2 = format_note_highlights(nb, rec_stats)
            mark = "👑 首选" if idx == 1 else f"#{idx} 备选"
            tier_note = f" | {cb['compat_tier_note']}" if cb.get("compat_tier_note") else ""
            lines.append(
                f"  {mark} 👉 【契合 {cb['total_compat']}分 · {cb['rank_name']}(Lv.{cb['level']}) | {cb['learn_times']}次学习 × 单次{cb['trig_min']}~{cb['trig_max']}词条 (期望{cb['expected_triggers']}条){tier_note}】"
            )
            lines.append(
                f"     ├─ 📗 自备：{n1['actor_name']} ({QUALITY_NAMES.get(n1['rank'], '')} {n1['score']}分 | 关系+{cb['r1']} {h1}) ➔ {hl1}"
            )
            lines.append(
                f"     └─ 🤝 借用：{nb['actor_name']} [好友:{nb.get('friend', '助战')}] ({QUALITY_NAMES.get(nb['rank'], '')} {nb['score']}分 | 关系+{cb['r2']} {h2}) ➔ {hl2}"
            )

    if adv["pure_own_combos"]:
        lines.append("\n💎 二、纯自备省额度方案（2本自备笔记，不耗每日3次借用额度）：")
        for idx, cb in enumerate(adv["pure_own_combos"][:3], 1):
            n1, n2 = cb["note1"], cb["note2"]
            h1 = f"专属契合{cb['s1']}★(+{cb['b1']})" if cb.get("b1") else "专属契合+0"
            h2 = f"专属契合{cb['s2']}★(+{cb['b2']})" if cb.get("b2") else "专属契合+0"
            hl1 = format_note_highlights(n1, rec_stats)
            hl2 = format_note_highlights(n2, rec_stats)
            mark = "⭐ 首选" if idx == 1 else f"#{idx} 备选"
            tier_note = f" | {cb['compat_tier_note']}" if cb.get("compat_tier_note") else ""
            lines.append(
                f"  {mark} 👉 【契合 {cb['total_compat']}分 · {cb['rank_name']}(Lv.{cb['level']}) | {cb['learn_times']}次学习 × 单次{cb['trig_min']}~{cb['trig_max']}词条 (期望{cb['expected_triggers']}条){tier_note}】"
            )
            lines.append(
                f"     ├─ 📗 自备1：{n1['actor_name']} ({QUALITY_NAMES.get(n1['rank'], '')} {n1['score']}分 | 关系+{cb['r1']} {h1}) ➔ {hl1}"
            )
            lines.append(
                f"     └─ 📘 自备2：{n2['actor_name']} ({QUALITY_NAMES.get(n2['rank'], '')} {n2['score']}分 | 关系+{cb['r2']} {h2}) ➔ {hl2}"
            )

    if adv["scout_targets"]:
        st0 = adv["scout_targets"][0]
        scout_str = " / ".join(
            f"搜【{st['actor_name']}】(关系+{st['rel_score']} ➔ 0★即{st['total_0star']}分, 2★达{st['total_2star']}分)"
            for st in adv["scout_targets"][:3]
        )
        lines.append(
            f"\n🔍 三、去助战列表淘金：固定自备【{st0['own_note_actor']}({st0['own_note_score']}分, 贡献+{st0['own_contrib']})】时，优先 {scout_str}"
        )

    ab = adv.get("auto_breeders") or {}
    ab_owned = ab.get("same_pos_owned") or []
    ab_all = ab.get("same_pos_all") or []
    if ab_owned or ab_all:
        lines.append(f"\n🤖 四、自动训练 5 把刷彩笔记最佳种马（原生同位 {ab.get('pos_code', '')} · 自动训练不出第二位置词条，免洗转位）：")
        if ab_owned:
            own_s = " / ".join(
                f"【{x['actor_name']}】(关系+{x['rel_to_core']}"
                + (f", 现有{x['best_own_score']}分)" if x["best_own_score"] > 0 else ", 尚缺高分)")
                for x in ab_owned[:4]
            )
            lines.append(f"  ✅ 已解锁首选：{own_s}")
        if ab_all:
            all_s = " / ".join(
                f"{x['actor_name']}(+{x['rel_to_core']}{'·已拥有' if x['is_owned'] else '·未解锁'})"
                for x in ab_all[:4]
            )
            lines.append(f"  🌐 全图鉴同位榜：{all_s}")
        lines.append("  💡 实操节奏：笔 5~6 级后单卡 8500+ 记录成绩约 5% 出 7 阶彩；40~59 分词条期望完全一致(5~10条)，勿为凑 45 虚分牺牲主属性上限。")

    lines.append(f"\n{adv.get('revision_warning', NOTE_REVISION_WARNING)}")
    lines.append("=" * 88)
    return "\n".join(lines)


def format_character_note_advice_hud_markup(adv: dict, scan_status_line: str = "") -> str:
    """生成用于右上角置顶悬浮窗 FloatingHUD 的 Pango Markup 富文本。"""
    c = adv["character"]
    rec_stats = adv["rec_stats"]
    lines = []

    if adv["borrow_combos"]:
        cb = adv["borrow_combos"][0]
        n1, nb = cb["note1"], cb["note2"]
        hl1 = html.escape(format_note_highlights(n1, rec_stats))
        hl2 = html.escape(format_note_highlights(nb, rec_stats))
        badge_color = "#4ade80" if cb["total_compat"] < 42 else "#34d399"
        friend_s = html.escape(nb.get("friend", "助战"))
        lines.append(
            f"<span weight='bold' foreground='{badge_color}'>👑 借用 {cb['total_compat']}分·{html.escape(cb['rank_name'])}</span>  "
            f"<span foreground='#f8fafc'>自备<b>{html.escape(n1['actor_name'])}</b>({n1['score']}) + 借<b>{html.escape(nb['actor_name'])}</b>[{friend_s}]({nb['score']})</span>"
        )
        lines.append(
            f"<span size='small' foreground='#94a3b8'>   {hl1} · {hl2}</span>"
        )

    if adv["pure_own_combos"]:
        cb = adv["pure_own_combos"][0]
        n1, n2 = cb["note1"], cb["note2"]
        lines.append(
            f"<span weight='bold' foreground='#fbbf24'>💎 自备 {cb['total_compat']}分·{html.escape(cb['rank_name'])}</span>  "
            f"<span foreground='#cbd5e1'><b>{html.escape(n1['actor_name'])}</b>({n1['score']}) + <b>{html.escape(n2['actor_name'])}</b>({n2['score']})</span>"
        )

    # 嵌入实机校准版本命器材 5选4 图标条与精简避坑提示
    eq_tips = []
    try:
        from equipment_advisor import format_equip_advice_hud_markup
        eq_markup, eq_icons = format_equip_advice_hud_markup(int(c.get("id", 0)))
        adv["equip_icon_items"] = eq_icons
        if eq_markup:
            eq_tips.extend(eq_markup.splitlines())
    except Exception:
        pass

    if scan_status_line and not scan_status_line.startswith("💡"):
        eq_tips.insert(0, f"<span size='small' foreground='#34d399'>{html.escape(scan_status_line)}</span>")
    adv["hud_tip_markup"] = "\n".join(eq_tips)

    return "\n".join(lines)


def analyze_breeding_cycle(chars: dict, rules: dict, rel_scores: dict, pool: dict) -> str:
    """
    独立战略功能：分析当前账号「下一位练哪个角色（为了刷笔记）最好」的养成循环规划器。
    """
    owned_ids = [
        int(x) for x in pool.get("owned_actors", [])
        if int(x) in chars and chars[int(x)].get("released", True)
    ]
    if not owned_ids:
        owned_ids = sorted(k for k, v in chars.items() if v.get("released", True))

    min_rank = int(pool.get("min_kept_rank", 5))
    active_own = [
        n for n in pool.get("own_notes", {}).values()
        if int(n.get("rank", 0)) >= min_rank
    ]
    borrow_list = list(pool.get("borrowed_notes", {}).values())

    # 每个角色当前最好的自备笔记
    best_own_by_char = {}
    for n in active_own:
        aid = int(n["actor_id"])
        val = int(n["score"]) + int(n.get("harmony_bonus", 0)) * 18
        if aid not in best_own_by_char:
            best_own_by_char[aid] = n
        else:
            old = best_own_by_char[aid]
            old_val = int(old["score"]) + int(old.get("harmony_bonus", 0)) * 18
            if val > old_val:
                best_own_by_char[aid] = n

    lines = [
        "=" * 94,
        "🧬 【篮球少女 · 种马/刷笔记养成循环战略规划器 (Note Breeding Cycle Planner)】",
        f"📊 已解锁球员：{len(owned_ids)} 人 | 现有保留自备金/红/彩笔记：{len(active_own)} 本（覆盖 {len(best_own_by_char)} 名球员，尚缺 {len(owned_ids) - len(best_own_by_char)} 人）",
        "=" * 94,
    ]

    # 一、现有自备核心笔记池盘点
    lines.append("\n📦 一、当前自备高阶笔记池家底（金/红/彩）：")
    for aid, n in sorted(best_own_by_char.items(), key=lambda kv: (-int(kv[1].get("score", 0)), -int(kv[1].get("harmony_bonus", 0)))):
        h_str = format_harmony_targets(n, chars)
        hl = format_note_highlights(n, get_char_rec_stat_ids(chars.get(aid, {})))
        status = "✅ 毕业级基石" if int(n.get("score", 0)) >= 450 else ("🟡 可用过渡(建议后续迭代)" if int(n.get("score", 0)) >= 340 else "🔴 评分偏低急需重刷")
        lines.append(
            f"  • {n['actor_name']}({aid})：{QUALITY_NAMES.get(n['rank'], '')} {n['score']}分 | {h_str} | {hl}  [{status}]"
        )

    # 二、评估每位已拥有角色作为「下一位刷笔记目标」的综合战略价值
    # 综合价值 = 下游枢纽辐射价值 (Downstream Hub) × 上游当前可启动性 (Upstream Readiness) × 边际紧迫度 (Marginal Need)
    ranking = []
    for x in owned_ids:
        adv_x = recommend_notes_for_character(x, chars, rules, rel_scores, pool)
        best_2own = adv_x["pure_own_combos"][0] if adv_x["pure_own_combos"] else None
        best_1b = adv_x["borrow_combos"][0] if adv_x["borrow_combos"] else None

        c_own = best_2own["total_compat"] if best_2own else 0
        c_bor = best_1b["total_compat"] if best_1b else 0

        # 下游辐射：在已拥有的其他球员 Y 中，rel(Y, X) 的分布
        rels_to_x = sorted(
            [(int(rel_scores.get(y, {}).get(x, 0)), y) for y in owned_ids if y != x],
            reverse=True,
        )
        top8_avg = sum(r for r, _ in rels_to_x[:8]) / max(1, len(rels_to_x[:8]))
        high_15_targets = [(r, y) for r, y in rels_to_x if r >= 15]

        # 模拟：如果今天练出一本 X 的 6阶红笔记 (假设带 1★~2★契合，即 +2~+5)，能让多少原本自备相性不足 (<38分) 的已拥有球员获得相性跃升？
        upgraded_targets = []
        for r_yx, y in rels_to_x[:12]:
            if r_yx < 14:
                continue
            # Y 目前最好的单本自备笔记（排除 Y 自己和 X）
            best_other_for_y = 0
            for aid_o, n_o in best_own_by_char.items():
                if aid_o in (y, x):
                    continue
                s_o = int(n_o.get("harmony_targets", {}).get(str(y), 0))
                v_o = int(rel_scores.get(y, {}).get(aid_o, 0)) + HARMONY_BONUS_BY_STAR.get(s_o, 0)
                if v_o > best_other_for_y:
                    best_other_for_y = v_o
            new_y_compat = best_other_for_y + r_yx + 2  # 保守按 1★ 契合 (+2) 估算新刷出的 X 笔记
            if new_y_compat >= 35:
                upgraded_targets.append((chars[y]["short_name"], r_yx, new_y_compat))

        # 边际紧迫度：目前完全没有金/红笔记的权重 1.0；仅有 <300 分笔记的 0.90；300~380 分的 0.72；已有 >450 分高分笔记的 0.35
        cur_own = best_own_by_char.get(x)
        cur_h_star = cur_own.get("harmony_star", max(cur_own.get("harmony_targets", {}).values(), default=0)) if cur_own else 0
        if not cur_own:
            marginal_mult = 1.00
            own_tag = "❌ 尚无金/红笔记"
        elif int(cur_own["score"]) < 300:
            marginal_mult = 0.90
            own_tag = f"🔴 现有仅{cur_own['score']}分({cur_h_star}★)"
        elif int(cur_own["score"]) < 400:
            marginal_mult = 0.72
            own_tag = f"🟡 现有{cur_own['score']}分({cur_h_star}★)"
        else:
            marginal_mult = 0.35
            own_tag = f"✅ 已有{cur_own['score']}分({cur_h_star}★)"

        # 上游可启动分：当前就能用自备/借用凑出 ≥40 分或 ≥30 分的角色，更容易一次育成直接刷出 6阶红/7阶彩高分笔记
        readiness_score = max(c_own, c_bor * 0.95)
        hub_score = top8_avg * 3.2 + len(high_15_targets) * 2.5 + len(upgraded_targets) * 1.8
        priority_score = (hub_score + readiness_score * 0.85) * marginal_mult

        ranking.append({
            "actor_id": x,
            "name": chars[x]["short_name"],
            "pos": chars[x].get("best_pos", ""),
            "own_tag": own_tag,
            "priority_score": round(priority_score, 1),
            "top8_avg": round(top8_avg, 1),
            "high_15_cnt": len(high_15_targets),
            "top_targets": rels_to_x[:5],
            "upgraded_targets": upgraded_targets[:5],
            "best_2own": best_2own,
            "best_1b": best_1b,
        })

    ranking.sort(key=lambda r: -r["priority_score"])

    lines.append("\n🏆 二、当下最优先练的「刷笔记枢纽角色」Top 8 推荐榜（综合 下游辐射广度 × 当前可喂饱度 × 笔记缺口）：")
    for idx, item in enumerate(ranking[:8], 1):
        b2 = item["best_2own"]
        b1 = item["best_1b"]
        p2_str = (
            f"契合{b2['total_compat']}分【{b2['rank_name']}·{b2['learn_times']}次】({b2['note1']['actor_name']}+{b2['note2']['actor_name']})"
            if b2 else "无"
        )
        p1_str = (
            f"契合{b1['total_compat']}分【{b1['rank_name']}·{b1['learn_times']}次】({b1['note1']['actor_name']}+借{b1['note2']['actor_name']}[{b1['note2'].get('friend', '')}])"
            if b1 else "无"
        )
        t_str = "、".join(f"{chars[y]['short_name']}(+{r})" for r, y in item["top_targets"])
        lines.append(
            f"  #{idx} 👑 【{item['name']}({item['actor_id']}·{item['pos']})】 战略优先级：{item['priority_score']} | 状态：{item['own_tag']}"
        )
        lines.append(
            f"     ├─ 🎯 为什么练她（下游辐射）：Top8均相性 {item['top8_avg']}（≥15分高相性有 {item['high_15_cnt']} 人：{t_str}）"
        )
        lines.append(
            f"     └─ 🚀 现在怎么练她（上游喂养）：💎纯自备 {p2_str} ｜ 🔥借用 {p1_str}"
        )

    # 三、黄金三角互哺圈（A ⇄ B ⇄ C 永动刷因子闭环）
    triangles = []
    for i, a in enumerate(owned_ids):
        for j in range(i + 1, len(owned_ids)):
            b = owned_ids[j]
            for k in range(j + 1, len(owned_ids)):
                c = owned_ids[k]
                sa = int(rel_scores.get(a, {}).get(b, 0)) + int(rel_scores.get(a, {}).get(c, 0))
                sb = int(rel_scores.get(b, {}).get(a, 0)) + int(rel_scores.get(b, {}).get(c, 0))
                sc = int(rel_scores.get(c, {}).get(a, 0)) + int(rel_scores.get(c, {}).get(b, 0))
                min_s = min(sa, sb, sc)
                avg_s = (sa + sb + sc) / 3.0
                if min_s >= 32:
                    triangles.append((min_s, avg_s, a, b, c, sa, sb, sc))
    triangles.sort(key=lambda x: (-x[0], -x[1]))

    lines.append("\n♻️ 三、已拥有球员「黄金三角互哺圈」（3人天生相性保底 ≥32分，凑齐后可无限互刷 40~45+【极】笔记）：")
    shown_tri = 0
    seen_sets = []
    for min_s, avg_s, a, b, c, sa, sb, sc in triangles:
        # 避免展示过度重叠的三角
        if sum(1 for s in seen_sets if len(s & {a, b, c}) >= 2) >= 2:
            continue
        seen_sets.append({a, b, c})
        shown_tri += 1
        na, nb, nc = chars[a]["short_name"], chars[b]["short_name"], chars[c]["short_name"]
        missing = [chars[x]["short_name"] for x in (a, b, c) if x not in best_own_by_char]
        have = [f"{chars[x]['short_name']}({best_own_by_char[x]['score']}分)" for x in (a, b, c) if x in best_own_by_char]
        act_tip = (
            f"⚡ 仅差【{'、'.join(missing)}】即可闭环！（已有：{'、'.join(have)}）"
            if missing and have
            else (f"🔨 待开拓圈（先用过渡笔记练出【{na}】启动）" if len(missing) == 3 else "✅ 已完全闭环，可随时互刷冲7阶彩！")
        )
        lines.append(
            f"  🔄 黄金圈{shown_tri}：【{na} ⇄ {nb} ⇄ {nc}】 天生保底 {min_s}分 / 均分 {avg_s:.1f}分 (练{na}:{sa} | 练{nb}:{sb} | 练{nc}:{sc})"
        )
        lines.append(f"     └─ {act_tip}")
        if shown_tri >= 5:
            break

    # 四、卡手主力「两步铺路反推链」（当前纯自备相性 <35 分的角色，先练谁能一步救活？）
    lines.append("\n🗺️ 四、卡手角色「两步铺路反推链」（当前纯自备相性 <35 分的球员，先练哪位种马最赚？）：")
    stepping_stones = []
    for target in owned_ids:
        adv_t = recommend_notes_for_character(target, chars, rules, rel_scores, pool)
        cur_c = adv_t["pure_own_combos"][0]["total_compat"] if adv_t["pure_own_combos"] else 0
        if cur_c >= 35:
            continue
        # 寻找一个目前尚未拥有高分自备笔记的桥梁角色 B (bridge)，满足：
        # 1) B 现在就能用现有自备笔记以 ≥35 分高相性练出来
        # 2) B 练出后（假设带 2★契合 +5），搭配现有最佳单本自备笔记 A_own，能让 target 的自备相性大幅跃升
        best_bridge = None
        for bridge in owned_ids:
            if bridge == target or bridge in best_own_by_char:
                continue
            r_tb = int(rel_scores.get(target, {}).get(bridge, 0))
            if r_tb < 15:
                continue
            adv_b = recommend_notes_for_character(bridge, chars, rules, rel_scores, pool)
            b_own_c = adv_b["pure_own_combos"][0]["total_compat"] if adv_b["pure_own_combos"] else 0
            if b_own_c < 34:
                continue
            # 找现有自备笔记里与 target 最搭的另一本 partner_note
            best_partner_c = 0
            best_partner_name = ""
            for aid_p, n_p in best_own_by_char.items():
                if aid_p in (target, bridge):
                    continue
                s_p = int(n_p.get("harmony_targets", {}).get(str(target), 0))
                c_p = int(rel_scores.get(target, {}).get(aid_p, 0)) + HARMONY_BONUS_BY_STAR.get(s_p, 0)
                if c_p > best_partner_c:
                    best_partner_c = c_p
                    best_partner_name = chars[aid_p]["short_name"]
            proj_c_0star = best_partner_c + r_tb
            proj_c_2star = proj_c_0star + 5
            gain = proj_c_2star - cur_c
            if best_bridge is None or proj_c_2star > best_bridge["proj_2star"]:
                best_bridge = {
                    "bridge_id": bridge,
                    "bridge_name": chars[bridge]["short_name"],
                    "bridge_now_compat": b_own_c,
                    "bridge_now_pair": f"{adv_b['pure_own_combos'][0]['note1']['actor_name']}+{adv_b['pure_own_combos'][0]['note2']['actor_name']}",
                    "partner_name": best_partner_name,
                    "r_tb": r_tb,
                    "proj_0star": proj_c_0star,
                    "proj_2star": proj_c_2star,
                    "gain": gain,
                }
        if best_bridge and best_bridge["proj_2star"] >= 35:
            stepping_stones.append((best_bridge["gain"], target, cur_c, best_bridge))

    stepping_stones.sort(key=lambda x: -x[0])
    for _, target, cur_c, br in stepping_stones[:6]:
        tname = chars[target]["short_name"]
        lines.append(
            f"  • 想练【{tname}({target})】(现自备仅 {cur_c}分) ➔ 第1步：先用现有({br['bridge_now_pair']} · {br['bridge_now_compat']}分)练出【{br['bridge_name']}】"
            f" ➔ 第2步：用【{br['bridge_name']}(+{br['r_tb']}) + 现有{br['partner_name']}】直接把 {tname} 拉升至 {br['proj_0star']}~{br['proj_2star']}分！"
        )

    lines.append("\n🤖 五、自动刷彩种马铁律 & 修订避坑提示：")
    lines.append("  • 自动训练不出第二位置词条：定向给主力刷 7 阶彩笔记时，优先自动 5 把【原生同位置 + 高契合度】种马（可用 `--auto-breeder <角色名>` 一键查榜）。")
    lines.append(f"  • {NOTE_REVISION_WARNING}")
    lines.append("=" * 94)
    return "\n".join(lines)


def analyze_single_note_activation(query: str, chars: dict, rules: dict, rel_scores: dict, pool: dict) -> str:
    """
    针对单本新出炉/新借用的笔记（按分数、note_id 或角色名匹配），严格调用 evaluate_compatibility()
    计算它与现有自备池/助战池搭配时，能把哪些角色推上 ≥30 分【优】/ ≥40 分 / ≥45 分【极】。
    彻底杜绝凭印象心算角色基础相性分 (rel_scores) 导致的幻觉！
    """
    q = query.strip()
    all_notes = []
    for n in pool.get("own_notes", {}).values():
        all_notes.append(("自备", n))
    for n in pool.get("borrowed_notes", {}).values():
        all_notes.append((f"借:{n.get('friend', '助战')}", n))

    matched = []
    for src_tag, n in all_notes:
        if (
            q == str(n.get("note_id", ""))
            or q == str(n.get("score", ""))
            or q in str(n.get("actor_name", ""))
            or f"{n.get('actor_name', '')}{n.get('score', '')}" == q
        ):
            matched.append((src_tag, n))
    if not matched:
        return f"⚠️ 在当前笔记池中未找到匹配『{query}』的笔记（支持按分数如 511/666、角色名如 毒岛、或 note_id 查询）。"

    matched.sort(key=lambda x: -int(x[1].get("score", 0)))
    src_tag, target_note = matched[0]
    t_aid = int(target_note["actor_id"])
    t_name = target_note.get("actor_name", str(t_aid))
    t_score = int(target_note.get("score", 0))
    is_target_own = src_tag == "自备"
    owned_ids = set(int(x) for x in pool.get("owned_actors", []))

    ht_str = format_harmony_targets(target_note, chars)
    hl_str = format_note_highlights(target_note)
    lines = [
        "=" * 94,
        f"🔍 【单笔记反向激活真值表】目标笔记：[{src_tag}] {t_name}({t_aid}) | {QUALITY_NAMES.get(target_note.get('rank', 6), '')} {t_score}分",
        f"   ├─ 专属契合词条：{ht_str}",
        f"   └─ 核心因子亮点：{hl_str}",
        "-" * 94,
    ]

    min_rank = int(pool.get("min_kept_rank", 5))
    own_partners = [
        n for n in pool.get("own_notes", {}).values()
        if int(n.get("rank", 0)) >= min_rank and int(n["actor_id"]) != t_aid and int(n.get("score", 0)) >= 340
    ]
    borrow_partners = [
        n for n in pool.get("borrowed_notes", {}).values()
        if int(n["actor_id"]) != t_aid and int(n.get("score", 0)) >= 400
    ]

    # 1. 搭配自备高分笔记 (≥340分) 能达到 ≥30分【优】的角色
    own_activations = []
    for cid, c in chars.items():
        if cid == t_aid:
            continue
        rec_stats = get_char_rec_stat_ids(c)
        best_ev = None
        for p_note in own_partners:
            if int(p_note["actor_id"]) == cid:
                continue
            ev = evaluate_compatibility(cid, target_note, p_note, rel_scores, rules, rec_stats)
            if best_ev is None or (ev["total_compat"], ev["eval_score"]) > (best_ev["total_compat"], best_ev["eval_score"]):
                best_ev = ev
        if best_ev and best_ev["total_compat"] >= 28:
            own_activations.append((best_ev["total_compat"], best_ev["eval_score"], cid, best_ev))

    own_activations.sort(key=lambda x: (-x[0], -x[1]))
    hdr_1 = "📗 一、搭配【其他自备红/金笔记】真实相性排行（≥28分，由 evaluate_compatibility 严格验算）："
    lines.append(hdr_1)
    for compat, _, cid, ev in own_activations[:15]:
        cname = chars[cid]["short_name"]
        pos = chars[cid].get("best_pos", "")
        own_mark = "✅已拥有" if cid in owned_ids else "🔒未解锁"
        p_note = ev["note2"]
        b1_str = f"+契合{ev['b1']}" if ev["b1"] else ""
        b2_str = f"+契合{ev['b2']}" if ev["b2"] else ""
        tier_mark = "🔥" if compat >= 30 else "⚠️差一点(良)"
        lines.append(
            f"  {tier_mark} {cname}({cid}·{pos}) [{own_mark}]：总相性 {compat}分【{ev['rank_name']}·{ev['learn_times']}次×{ev['trig_min']}~{ev['trig_max']}条】"
            f" = {t_name}{t_score}(关系+{ev['r1']}{b1_str}) + 自备{p_note['actor_name']}{p_note['score']}(关系+{ev['r2']}{b2_str}) | 均分{ev['avg_note_score']}"
        )

    # 2. 若目标笔记本身是自备笔记，额外展示它搭配【好友高分助战笔记】能激活的角色
    if is_target_own and borrow_partners:
        bor_activations = []
        for cid, c in chars.items():
            if cid == t_aid:
                continue
            rec_stats = get_char_rec_stat_ids(c)
            best_ev = None
            for b_note in borrow_partners:
                if int(b_note["actor_id"]) == cid:
                    continue
                ev = evaluate_compatibility(cid, target_note, b_note, rel_scores, rules, rec_stats)
                if best_ev is None or (ev["total_compat"], ev["eval_score"]) > (best_ev["total_compat"], best_ev["eval_score"]):
                    best_ev = ev
            if best_ev and best_ev["total_compat"] >= 30:
                bor_activations.append((best_ev["total_compat"], best_ev["eval_score"], cid, best_ev))
        bor_activations.sort(key=lambda x: (-x[0], -x[1]))
        lines.append("\n🤝 二、搭配【好友高分助战笔记 (≥400分)】直达 ≥30分【优】排行：")
        for compat, _, cid, ev in bor_activations[:12]:
            cname = chars[cid]["short_name"]
            pos = chars[cid].get("best_pos", "")
            own_mark = "✅已拥有" if cid in owned_ids else "🔒未解锁"
            b_note = ev["note2"]
            b1_str = f"+契合{ev['b1']}" if ev["b1"] else ""
            b2_str = f"+契合{ev['b2']}" if ev["b2"] else ""
            lines.append(
                f"  🚀 {cname}({cid}·{pos}) [{own_mark}]：总相性 {compat}分【{ev['rank_name']}·{ev['learn_times']}次】"
                f" = 自备{t_name}{t_score}(+{ev['r1']}{b1_str}) + 借{b_note['actor_name']}[{b_note.get('friend', '')}]{b_note['score']}(+{ev['r2']}{b2_str}) | 均分{ev['avg_note_score']}"
            )

    lines.append("=" * 94)
    return "\n".join(lines)


def resolve_character_id(query: str, chars: dict) -> Optional[int]:
    q = query.strip()
    if q.isdigit() and int(q) in chars:
        return int(q)
    for cid, c in chars.items():
        if q in (c.get("short_name", ""), c.get("show_name", ""), c.get("en_name", "")):
            return cid
        for al in c.get("aliases", []):
            if q == al or q in al:
                return cid
    for cid, c in chars.items():
        if q in c.get("short_name", "") or q in c.get("show_name", ""):
            return cid
    return None


def main():
    parser = argparse.ArgumentParser(description="LQSNCoach — 开局笔记最优配对参谋 & 种马养成循环规划器")
    parser.add_argument("--char", "-c", type=str, help="查询指定球员（名字或ID）的开局最优笔记组合")
    parser.add_argument("--auto-breeder", "-b", type=str, help="查询指定目标球员最适合自动训练 5 把刷彩笔记的「原生同位置+最高契合」种马榜")
    parser.add_argument("--note", "-n", type=str, help="查询指定单本笔记（按分数如511/666、角色名或ID）能激活的所有角色真值表")
    parser.add_argument("--plan", "-p", action="store_true", help="运行「下一位练哪个角色（为了刷笔记）」养成循环战略规划器")
    parser.add_argument("--pool", action="store_true", help="查看当前已同步的自备笔记池与好友借用笔记池明细")
    parser.add_argument("--scan", "-s", nargs="?", const="", default=None, help="直接截取当前游戏画面（或指定图片路径）识别自备/借用笔记并更新笔记池")
    parser.add_argument("--scan-scroll", type=int, nargs="?", const=10, default=None, metavar="PAGES", help="在当前打开的列表先归零到顶，再向下自动拖拽翻页到底连续扫描（默认最多 10 页）")
    parser.add_argument("--auto-scan", "-a", type=int, nargs="?", const=10, default=None, metavar="PAGES", help="【每日一键扫库】自动切换『我的/好友/协会』标签，每栏先归零到顶再一路翻到底扫描入库")
    parser.add_argument("--delay", type=float, default=2.5, help="运行自动翻页前的准备倒计时秒数（默认 2.5s，留出切回游戏窗口的时间）")
    args = parser.parse_args()

    chars, rules, rel_scores, entries_catalog = load_static_db()

    if args.auto_scan is not None:
        res = scan_notes_scrolling(args.auto_scan, chars, entries_catalog, auto_tabs=True, countdown_sec=args.delay)
        if not res.get("ok"):
            print(f"⚠️ 一键自动扫库失败：{res.get('error')}")
        else:
            print(f"✅ {res.get('summary')}")
        return

    if args.scan_scroll is not None:
        res = scan_notes_scrolling(args.scan_scroll, chars, entries_catalog, auto_tabs=False, countdown_sec=args.delay)
        if not res.get("ok"):
            print(f"⚠️ 翻页扫描失败：{res.get('error')}")
        else:
            print(f"✅ {res.get('summary')}")
        return

    if args.scan is not None:
        img_p = args.scan if args.scan else None
        res = scan_notes_from_screen(img_p, chars, entries_catalog)
        if not res.get("ok"):
            print(f"⚠️ 画面扫描失败：{res.get('error')}")
        else:
            print(f"✅ [{res.get('screen_type')}] {res.get('summary')}")
        return

    pool = sync_note_and_actor_pool(chars, entries_catalog)

    if args.pool:
        print("=" * 92)
        print(f"📗 自备笔记池（共 {len(pool['own_notes'])} 本，自动回收阈值：保留 ≥{pool['min_kept_rank']}阶）：")
        for n in sorted(pool["own_notes"].values(), key=lambda x: -int(x["score"])):
            hl = format_note_highlights(n, get_char_rec_stat_ids(chars.get(int(n["actor_id"]), {})))
            ht_str = format_harmony_targets(n, chars)
            print(
                f"  [自备] {n['actor_name']}({n['actor_id']}) | {QUALITY_NAMES.get(n['rank'], '')} {n['score']}分 | {ht_str} | {hl} (nid:{n['note_id']})"
            )
        print("-" * 92)
        print(f"🤝 助战/好友借用笔记池（共 {len(pool['borrowed_notes'])} 本）：")
        for n in sorted(pool["borrowed_notes"].values(), key=lambda x: -int(x["score"])):
            hl = format_note_highlights(n, get_char_rec_stat_ids(chars.get(int(n["actor_id"]), {})))
            ht_str = format_harmony_targets(n, chars)
            print(
                f"  [借:{n.get('friend', '好友')}] {n['actor_name']}({n['actor_id']}) | {QUALITY_NAMES.get(n['rank'], '')} {n['score']}分 | {ht_str} | {hl} (nid:{n['note_id']})"
            )
        print("=" * 92)
        return

    if args.note:
        print(analyze_single_note_activation(args.note, chars, rules, rel_scores, pool))
        return

    if args.auto_breeder:
        cid = resolve_character_id(args.auto_breeder, chars)
        if not cid:
            print(f"⚠️ 未找到匹配的球员：{args.auto_breeder}")
            return
        print(format_auto_breeder_terminal(cid, chars, rel_scores, pool))
        return

    if args.char:
        cid = resolve_character_id(args.char, chars)
        if not cid:
            print(f"⚠️ 未找到匹配的球员：{args.char}")
            return
        adv = recommend_notes_for_character(cid, chars, rules, rel_scores, pool)
        print(format_character_note_advice_terminal(adv))
        return

    # 默认或 --plan 时输出养成循环规划器；若在最近日志中检测到最后选中的角色，也顺带附上该角色的开局配对建议
    print(analyze_breeding_cycle(chars, rules, rel_scores, pool))


if __name__ == "__main__":
    main()
