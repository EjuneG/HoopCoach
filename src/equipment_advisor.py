#!/usr/bin/env python3
"""
《篮球少女：飞跃巅峰》本命器材配装参谋 & 阵容协同诊断器 (src/equipment_advisor.py)
- 融合实机校准版 data/equipment_guide_db.json (73件公测器材 + 官方定位标签 + 满级三维榜 + 社区实战五档榜)
- 融合玩家账号自有器材快照 data/equip_pool.json（自动给出「手头已练卡 5选4 实盘方案」与「全图鉴毕业顶配参考」）
- 提供 HUD 悬浮窗专用的 22×22 官方图标列表 + Pango 富文本摘要 + 开局陪练不叠加/星愿速查提示
"""

import argparse
import html
import json
import os
import re
import sys
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE_DB_FILE = os.path.join(BASE_DIR, "data", "equipment_guide_db.json")
EQUIP_POOL_FILE = os.path.join(BASE_DIR, "data", "equip_pool.json")
CHARS_FILE = os.path.join(BASE_DIR, "data", "hoopgirls", "characters.json")
ICONS_DIR = os.path.join(BASE_DIR, "data", "hoopgirls", "equipment_icons")

STAT_NAMES = {101: "投射", 102: "弹跳", 103: "防守", 104: "体能", 105: "运控", 106: "意识"}
TRAIN_NAME_TO_ID = {v: k for k, v in STAT_NAMES.items()}

COMMUNITY_TIER_SCORE = {
    "夯": 34.0,
    "顶级": 26.0,
    "人上人": 17.0,
    "未上榜": 14.0,
    "NPC": 9.0,
    "拉完了": 2.0,
}

STAT_TIER_SCORE = {
    "T0特例": 40.0,
    "S": 28.0,
    "A": 20.0,
    "B": 14.0,
    "C": 10.0,
    "D": 2.0,
}

_CACHED_DATA = None
_CACHED_MTIMES: tuple[float, float, float] | None = None
_UNSET = object()


def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path) if os.path.exists(path) else -1.0
    except OSError:
        return -1.0


def load_equip_data(force_reload: bool = False) -> tuple[dict, dict, dict]:
    """返回 (guide_db, equip_pool, chars_by_id)。"""
    global _CACHED_DATA, _CACHED_MTIMES
    cur_mtimes = (
        _file_mtime(GUIDE_DB_FILE),
        _file_mtime(EQUIP_POOL_FILE),
        _file_mtime(CHARS_FILE),
    )
    if _CACHED_DATA is not None and not force_reload and _CACHED_MTIMES == cur_mtimes:
        return _CACHED_DATA

    if os.path.exists(GUIDE_DB_FILE):
        with open(GUIDE_DB_FILE, encoding="utf-8") as f:
            guide_db = json.load(f)
    else:
        guide_db = {"equipments": {}}
    if os.path.exists(EQUIP_POOL_FILE):
        with open(EQUIP_POOL_FILE, encoding="utf-8") as f:
            equip_pool = json.load(f)
    else:
        equip_pool = {"owned_cards": {}, "borrowed_cards": {}}
    if os.path.exists(CHARS_FILE):
        with open(CHARS_FILE, encoding="utf-8") as f:
            raw_chars = json.load(f)
    else:
        raw_chars = []
    chars_list = raw_chars.values() if isinstance(raw_chars, dict) else raw_chars
    chars_by_id = {
        int(c["id"]): c
        for c in chars_list
        if isinstance(c, dict) and c.get("kind") == "player"
    }
    _CACHED_DATA = (guide_db, equip_pool, chars_by_id)
    _CACHED_MTIMES = cur_mtimes
    return _CACHED_DATA


def get_equip_icon_path(eq: dict) -> str:
    fn = eq.get("icon_file") or ""
    if fn:
        p = os.path.join(ICONS_DIR, fn)
        if os.path.exists(p):
            return p
    return ""


def evaluate_skill_match(eq: dict, char_obj: dict) -> dict:
    """
    判定某件器材的金/银比赛技能是否对该球员生效，以及风格/打法是否契合：
    返回 {"active": bool, "score": float, "badge": str, "warn": str}
    """
    eid = int(eq["id"])
    if eid == 11004:
        return {"active": True, "score": 60.0, "badge": "🎺T0必锁", "warn": ""}

    tag = eq.get("official_tag", "通用")
    roles = set(char_obj.get("role_type") or [])
    style = char_obj.get("style", "")
    name = eq.get("name", "")

    # 1. 通用类 / SR行为卡 / R过渡件 / 未分类基础件
    if tag.startswith("通用") or tag in ("行为卡(SR)", "过渡件(R)", "待确认"):
        rarity = eq.get("rarity", "SSR")
        if rarity == "SSR":
            bonus = 18.0
            badge = "✅通用生效"
        elif rarity == "SR":
            bonus = 6.0
            badge = "✅SR过渡生效"
        else:
            bonus = 2.0
            badge = "✅R基础过渡"
        # 特殊反协同检查：都大会冠军戒指(突分狂热) 给纯得分手会稀释自身球权
        if name == "都大会冠军戒指" and ("得分手" in roles and "组织者" not in roles):
            return {"active": True, "score": -8.0, "badge": "⚠️稀释主C球权", "warn": "突分战术会分走自得分主C球权"}
        return {"active": True, "score": bonus, "badge": badge, "warn": ""}

    # 2. 角色定位专属类
    base_role = tag.split("/")[0].split("+")[0]
    if base_role in roles:
        synergy_bonus = 24.0
        badge = f"✅{base_role}本命"
        # 细分卡色/打法微调
        if name == "高级智能篮球" and style in ("速度", "全能"):
            synergy_bonus += 6.0
            badge = "✅速卡得分绝配"
        elif name in ("智能跳绳", "高级跳绳A") and style in ("力量", "全能"):
            synergy_bonus += 6.0
            badge = "✅力卡得分绝配"
        elif name == "高级充气游泳池" and style in ("技巧", "全能"):
            synergy_bonus += 6.0
            badge = "✅技卡得分绝配"
        elif name == "高级登山机" and char_obj.get("id") in (1012, 1013, 1040):
            synergy_bonus += 8.0
            badge = "✅中投本命"
        elif name == "都大会冠军戒指" and "得分手" in roles:
            synergy_bonus -= 14.0
            badge = "⚠️慎带(分球权)"
        return {"active": True, "score": synergy_bonus, "badge": badge, "warn": ""}

    # 3. 跨定位部分可用特例（快攻戒指给防守/篮板手、挡拆戒指给高运控）
    if name == "地区预选冠军戒指" and (roles & {"防守者", "篮板手"}):
        return {"active": True, "score": 16.0, "badge": "✅防板快攻可用", "warn": ""}

    # 4. 定位不匹配 -> 金技能比赛空转！仅吃育成训练面板
    return {
        "active": False,
        "score": -22.0,
        "badge": f"⚠️需{base_role}(金技空转)",
        "warn": f"{name}金技限定『{base_role}』，{char_obj.get('short_name','该角色')}({'/'.join(roles) or '无'})带它仅吃训练白值",
    }


HUD_CONFIG_FILE = os.path.join(BASE_DIR, "config", "hud_config.json")


def _resolve_focus_stat_name(focus_stat: Any = _UNSET) -> str:
    """解析专项偏科属性名；显式传入 focus_stat 时绕开 hud_config.json（None/空串表示禁用）。"""
    if focus_stat is not _UNSET:
        if focus_stat is None:
            return ""
        if isinstance(focus_stat, int):
            return STAT_NAMES.get(focus_stat, "")
        raw = str(focus_stat).strip()
        if not raw or raw.lower() in ("none", "null", "off", "无", "均衡", "pure", "0"):
            return ""
        if raw.isdigit():
            return STAT_NAMES.get(int(raw), "")
        if raw in TRAIN_NAME_TO_ID:
            return raw
        return ""
    return _load_active_focus_stat_name()


def _load_active_focus_stat_name() -> str:
    """读取 config/hud_config.json 中当前开启的专项偏科属性名（关闭时返回空串）。"""
    try:
        if os.path.exists(HUD_CONFIG_FILE):
            with open(HUD_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            fc = cfg.get("focus") if isinstance(cfg, dict) else None
            if isinstance(fc, dict) and fc.get("stat") is not None:
                return STAT_NAMES.get(int(fc["stat"]), "")
    except Exception:
        pass
    return ""


def _score_equip_for_char(eq: dict, char_obj: dict, break_level: int = 4, focus_stat_name: str = "") -> dict:
    """综合评估单件器材对目标球员的「比赛技能适配 + 育成推荐训练契合 + 满级三维 + 专项特化联动」。"""
    eid = int(eq["id"])
    if eid == 11004:
        return {
            "equip": eq,
            "total_score": 999.0,
            "train_match": "协助核心",
            "skill_eval": evaluate_skill_match(eq, char_obj),
            "icon_path": get_equip_icon_path(eq),
        }

    rec_trains = set(char_obj.get("recommend_train") or [])
    rec_attrs = set(char_obj.get("recommend_base_attr") or [])
    pref = eq.get("preferred_training", "")

    # 训练属性契合度：命中角色双主练属性权重最高（既吃擅长率又吃推荐属性加成）
    if pref in rec_trains:
        train_score = 26.0
        train_match = f"🎯主练{pref}"
    elif pref in rec_attrs:
        train_score = 16.0
        train_match = f"👍副推{pref}"
    elif pref in ("弹跳", "体能", "防守"):
        train_score = 8.0
        train_match = f"辅练{pref}"
    else:
        train_score = 2.0
        train_match = f"错位{pref}"

    skill_ev = evaluate_skill_match(eq, char_obj)
    comm_sc = COMMUNITY_TIER_SCORE.get(eq.get("community_tier", "未上榜"), 12.0)
    # 若金技能根本不触发，社区实战榜高分（如进阶教练手册的夯档续航）不能虚加到比赛端，仅保留少量通用底分
    eff_comm_sc = comm_sc if skill_ev["active"] else 4.0

    stat_sc = STAT_TIER_SCORE.get(eq.get("stat_tier", "C"), 10.0) + eq.get("lv50_composite", 85) * 0.12
    # 突破等级折算（满破4破=1.0，3破45级≈0.88，2破40级≈0.74，0~1破明显打折）
    br_mult = {4: 1.0, 3: 0.89, 2: 0.76, 1: 0.64, 0: 0.52}.get(int(break_level), 1.0)

    # 若开启专项特化（如投射协会赛），给擅长目标训练且金技有效的器材温和提权
    focus_bonus = 0.0
    if focus_stat_name and pref == focus_stat_name:
        focus_bonus = 11.0 if skill_ev["active"] else 4.0
        train_match = f"🎯{focus_stat_name}特化·{train_match}"

    total = (train_score + stat_sc) * br_mult + skill_ev["score"] + eff_comm_sc + focus_bonus
    return {
        "equip": eq,
        "break_level": break_level,
        "total_score": round(total, 1),
        "train_match": train_match,
        "skill_eval": skill_ev,
        "icon_path": get_equip_icon_path(eq),
    }


def classify_loadout_pattern(loadout: list[dict], char_obj: dict) -> dict:
    """
    识别除 11004 支援口哨外的 4 张实战器材训练位集中度：
    - 3+1 极攻：主单维 3 张 + 1 张（实战极攻暴击，主属性易冲 2500~3400）
    - 2+2 专精：双属性各 2 张（双核暴击）
    - 2+1+1 轻度集中：主属性 2 张 + 2 张辅维
    - 1+1+1+1 均衡冲分：四维分散（偏水桶冲总评分）
    """
    non_whistle = [x for x in (loadout or []) if int(x["equip"]["id"]) != 11004]
    rec_trains = list(char_obj.get("recommend_train") or [])
    counts: dict[str, int] = {}
    for item in non_whistle:
        pt = item["equip"].get("preferred_training") or "其他"
        counts[pt] = counts.get(pt, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] not in rec_trains, kv[0]))
    shape = "+".join(str(v) for _, v in ordered) if ordered else "空"
    detail = "+".join(f"{k}×{v}" for k, v in ordered) if ordered else "无"
    top_cnt = ordered[0][1] if ordered else 0
    sec_cnt = ordered[1][1] if len(ordered) > 1 else 0
    has_defense = counts.get("防守", 0) > 0
    rec_hits = sum(counts.get(rt, 0) for rt in rec_trains)
    primary_stat = rec_trains[0] if rec_trains else (ordered[0][0] if ordered else "")
    primary_hits = counts.get(primary_stat, 0) if primary_stat else 0

    if top_cnt >= 3:
        style_tag = f"⚔️ {shape}极攻({detail})"
        archetype = "pvp_31"
    elif top_cnt == 2 and sec_cnt == 2:
        style_tag = f"⚔️ 2+2专精({detail})"
        archetype = "pvp_22"
    elif top_cnt == 2:
        style_tag = f"⚖️ {shape}半集中({detail})"
        archetype = "hybrid"
    else:
        style_tag = f"📊 {shape}冲分均衡({detail}·偏水桶)"
        archetype = "score_balanced"

    return {
        "shape": shape,
        "detail": detail,
        "counts": counts,
        "archetype": archetype,
        "tag": style_tag,
        "has_defense": has_defense,
        "rec_hits": rec_hits,
        "primary_stat": primary_stat,
        "primary_hits": primary_hits,
    }


def _build_specialized_loadout(
    candidates: list[dict],
    char_obj: dict,
    whistle_entry: Optional[dict],
    target_mode: str,
    focus_stat_name: str = "",
) -> list[dict]:
    """
    基于贴吧高玩实战共识（防 9000 水桶虚战），从金技生效候选中构造偏科集中方案：
    - target_mode == 'pvp_31'：哨子 + 3 张主属性(优先专项/第一主练) + 1 张防守(若主属性为防守则补副主练)
    - target_mode == 'pvp_22'：哨子 + 2 张主属性A + 2 张主属性B(若某主练卡数不足则以防守/副推补齐)
    """
    rec_trains = list(char_obj.get("recommend_train") or [])
    rec_attrs = list(char_obj.get("recommend_base_attr") or [])
    by_stat: dict[str, list[dict]] = {}
    for c in candidates:
        pt = c["equip"].get("preferred_training") or ""
        by_stat.setdefault(pt, []).append(c)

    picked: list[dict] = []

    def _pick_from(stat_name: str, need: int):
        for c in by_stat.get(stat_name, []):
            if len(picked) >= 4 or need <= 0:
                break
            if c in picked:
                continue
            picked.append(c)
            need -= 1

    if target_mode == "pvp_31":
        # 选择张数最充沛的核心主练属性（若开启专项且有 >=2 张则优先专项）
        primary_order = []
        if focus_stat_name:
            primary_order.append(focus_stat_name)
        for st in rec_trains + rec_attrs + ["弹跳", "投射", "防守"]:
            if st and st not in primary_order:
                primary_order.append(st)
        main_stat = max(
            primary_order[:3] or primary_order,
            key=lambda st: (min(3, len(by_stat.get(st, []))), st == focus_stat_name, st in rec_trains[:1]),
            default="",
        )
        _pick_from(main_stat, 3)
        # 第 4 张优先补「防守」（贴吧实测：防守在防守回合全卡生效，体能之下第一实战属性），主属性为防守时补副主练
        sub_order = (
            [s for s in rec_trains if s != main_stat] + ["弹跳", "体能"]
            if main_stat == "防守"
            else ["防守"] + [s for s in rec_trains if s != main_stat] + ["弹跳", "体能"]
        )
        for st in sub_order:
            if len(picked) >= 4:
                break
            _pick_from(st, 4 - len(picked))
    else:
        # pvp_22：优先取两个能凑出 >=2 张卡的推荐主练属性（不足时以「防守」/其他高分属性补足第二组）
        stat_priority = []
        if focus_stat_name:
            stat_priority.append(focus_stat_name)
        for st in rec_trains + ["防守"] + rec_attrs + ["弹跳", "投射", "运控", "体能", "意识"]:
            if st and st not in stat_priority:
                stat_priority.append(st)
        pair_stats = [st for st in stat_priority if len(by_stat.get(st, [])) >= 2][:2]
        if len(pair_stats) < 2:
            for st in stat_priority:
                if st not in pair_stats and by_stat.get(st):
                    pair_stats.append(st)
                if len(pair_stats) >= 2:
                    break
        for st in pair_stats[:2]:
            _pick_from(st, 2)

    # 兜底补齐至 4 张
    for c in candidates:
        if len(picked) >= 4:
            break
        if c not in picked:
            picked.append(c)

    return ([whistle_entry] if whistle_entry else []) + picked


def _pick_best_missing_ssr(
    all_candidates: list[dict],
    owned_raw: dict,
    exclude_ids: set[int],
    prefer_stat: str = "",
) -> Optional[dict]:
    """从全图鉴激活金技 SSR 中挑选 1 张高收益缺失（未拥有）器材作为好友借用卡。"""
    owned_keys = {str(k) for k in (owned_raw or {}).keys()}
    missing = [
        c for c in all_candidates
        if str(c["equip"]["id"]) not in owned_keys and int(c["equip"]["id"]) not in exclude_ids
    ]
    pool = missing if missing else [c for c in all_candidates if int(c["equip"]["id"]) not in exclude_ids]
    if not pool:
        return None
    if prefer_stat:
        preferred = [c for c in pool if c["equip"].get("preferred_training") == prefer_stat]
        if preferred:
            pool = preferred
    chosen = dict(pool[0])
    chosen["owned_info"] = None
    chosen["is_borrow"] = True
    return chosen


def recommend_equipments_for_character(
    core_id: int,
    force_reload: bool = False,
    mode: str = "score",
    focus_stat: Any = _UNSET,
) -> dict:
    """
    为指定球员生成：
    1. owned_loadout: 基于玩家已识别自有器材池 + 支援口哨（未满破必借满破 11004；自有已满破则走「自有口哨 + 自有 3 + 借 1 缺失 SSR」）
    2. bis_loadout: 全图鉴 73 件公测器材中的「本命毕业 5 卡方案」（mode='score' 默认冲评分均衡；'pvp_22'/'pvp_31' 切换实战偏科集中）
    3. pvp_22_loadout / pvp_31_loadout / owned_pvp_loadout: PVP 实战偏科集中双视角参考（防 9000 水桶虚战）
    4. wish_tip / non_stack_pairs: 角色专属星愿攻略与同名陪练防踩坑提示
    """
    norm_mode = mode if mode in ("score", "pvp_22", "pvp_31") else "score"
    guide_db, equip_pool, chars_by_id = load_equip_data(force_reload=force_reload)
    equips = guide_db.get("equipments", {})
    char_obj = chars_by_id.get(int(core_id), {
        "id": int(core_id),
        "short_name": f"球员{core_id}",
        "role_type": ["得分手"],
        "recommend_train": ["投射", "弹跳"],
        "style": "全能",
    })

    whistle_eq = equips.get("11004")
    focus_stat_name = _resolve_focus_stat_name(focus_stat)

    # 先构建全图鉴有效金技 SSR 候选列表，供毕业顶配与「自有满破口哨借缺失 SSR」复用
    all_candidates = []
    for eid_str, eq in equips.items():
        if eid_str == "11004" or not eq.get("released", False) or eq.get("rarity") != "SSR":
            continue
        sc = _score_equip_for_char(eq, char_obj, break_level=4, focus_stat_name=focus_stat_name)
        if not sc["skill_eval"]["active"]:
            continue
        all_candidates.append(sc)
    all_candidates.sort(key=lambda x: -x["total_score"])

    # 检测自有池是否已有满破（break_level >= 4 且 awake_level >= 1）11004 支援口哨
    owned_raw = equip_pool.get("owned_cards", {})
    owned_whistle_info = owned_raw.get("11004") or owned_raw.get(11004)
    has_max_owned_whistle = bool(
        isinstance(owned_whistle_info, dict)
        and int(owned_whistle_info.get("break_level", 0) or 0) >= 4
        and int(owned_whistle_info.get("awake_level", 0) or 0) >= 1
    )

    if whistle_eq:
        w_break = int(owned_whistle_info.get("break_level", 4)) if has_max_owned_whistle else 4
        whistle_entry = _score_equip_for_char(whistle_eq, char_obj, break_level=w_break, focus_stat_name=focus_stat_name)
        if has_max_owned_whistle:
            whistle_entry["owned_info"] = owned_whistle_info
            whistle_entry["is_borrow"] = False
        else:
            whistle_entry["is_borrow"] = True
    else:
        whistle_entry = None

    # 1. 玩家实盘自有池评估
    owned_scored = []
    for cfg_str, oinfo in owned_raw.items():
        if str(cfg_str) == "11004":
            continue
        eq = equips.get(str(cfg_str))
        if not eq or not eq.get("released", True):
            continue
        sc = _score_equip_for_char(eq, char_obj, break_level=int(oinfo.get("break_level", 4)), focus_stat_name=focus_stat_name)
        sc["owned_info"] = oinfo
        sc["is_borrow"] = False
        owned_scored.append(sc)
    owned_scored.sort(key=lambda x: -x["total_score"])

    if has_max_owned_whistle:
        owned_top3 = owned_scored[:3]
        picked_own_ids = {int(x["equip"]["id"]) for x in owned_top3}
        borrowed_ssr = _pick_best_missing_ssr(all_candidates, owned_raw, picked_own_ids)
        if borrowed_ssr is not None:
            owned_score_loadout = ([whistle_entry] if whistle_entry else []) + owned_top3 + [borrowed_ssr]
        else:
            owned_score_loadout = ([whistle_entry] if whistle_entry else []) + owned_scored[:4]
    else:
        owned_top4 = owned_scored[:4]
        owned_score_loadout = ([whistle_entry] if whistle_entry else []) + owned_top4

    # 评估玩家自有池能否凑出实战集中组合 (2+2 / 3+1 / 至少同主属性 2 张且金技有效)
    owned_active = [x for x in owned_scored if x["skill_eval"]["active"]]
    owned_pvp_target = "pvp_31" if norm_mode == "pvp_31" else "pvp_22"
    if has_max_owned_whistle:
        base_own_pool = owned_active or owned_scored
        best_pvp_loadout = None
        best_pvp_score = -1.0
        # 枚举每个属性最高分的缺失 SSR 作为单张借卡尝试凑阵型，确保至多 1 张借用卡
        seen_stats = set()
        borrow_candidates = []
        for c in all_candidates:
            if str(c["equip"]["id"]) in {str(k) for k in owned_raw.keys()}:
                continue
            pt = c["equip"].get("preferred_training") or ""
            if pt not in seen_stats:
                seen_stats.add(pt)
                cand_copy = dict(c)
                cand_copy["owned_info"] = None
                cand_copy["is_borrow"] = True
                borrow_candidates.append(cand_copy)
        for b_cand in borrow_candidates or [None]:
            trial_pool = list(base_own_pool)
            if b_cand is not None:
                trial_pool = sorted(trial_pool + [b_cand], key=lambda x: -x["total_score"])
            trial_loadout = _build_specialized_loadout(trial_pool, char_obj, whistle_entry, owned_pvp_target, focus_stat_name)
            # 严格保证非哨子位至多 1 张借卡
            non_w = [x for x in trial_loadout if int(x["equip"]["id"]) != 11004]
            if sum(1 for x in non_w if x.get("is_borrow")) > 1:
                continue
            pat = classify_loadout_pattern(trial_loadout, char_obj)
            if pat["archetype"] in ("pvp_31", "pvp_22"):
                sc_sum = sum(x["total_score"] for x in non_w)
                if best_pvp_loadout is None or sc_sum > best_pvp_score:
                    best_pvp_loadout = trial_loadout
                    best_pvp_score = sc_sum
        owned_pvp_loadout = best_pvp_loadout if best_pvp_loadout is not None else owned_score_loadout
    else:
        owned_pvp_loadout = _build_specialized_loadout(owned_active or owned_scored, char_obj, whistle_entry, owned_pvp_target, focus_stat_name)

    owned_pvp_pattern = classify_loadout_pattern(owned_pvp_loadout, char_obj)
    if owned_pvp_pattern["archetype"] in ("pvp_31", "pvp_22"):
        owned_pvp_note = f"✅ 自有池可组出实战集中阵型：{owned_pvp_pattern['tag']}"
    else:
        owned_pvp_note = (
            f"ℹ️ 自有已练池同属性 SSR 深度暂不足以凑满 2+2/3+1（当前最高集中度：{owned_pvp_pattern['tag']}），"
            "平民期建议先按实盘最优 5 选 4 过渡，后续抽卡可定向补齐主练双维/防守同属性卡。"
        )

    if norm_mode in ("pvp_22", "pvp_31") and owned_pvp_pattern["archetype"] in ("pvp_31", "pvp_22"):
        owned_loadout = owned_pvp_loadout
    else:
        owned_loadout = owned_score_loadout
    picked_ids = {int(x["equip"]["id"]) for x in owned_loadout}
    owned_benched = [x for x in owned_scored if int(x["equip"]["id"]) not in picked_ids]

    # 2. 全图鉴毕业顶配 5 卡（1 支援口哨 + 4 张金技能必生效的 SSR）

    # 2a. 冲评分向均衡方案（原有逻辑：尽量覆盖双主练属性且同属性不超 2 张）
    bis_four = []
    stat_counts = {}
    rec_trains = list(char_obj.get("recommend_train") or [])
    for rt in rec_trains[:2]:
        best_rt = next((c for c in all_candidates if c["equip"]["preferred_training"] == rt and c not in bis_four), None)
        if best_rt:
            bis_four.append(best_rt)
            stat_counts[rt] = stat_counts.get(rt, 0) + 1
    for c in all_candidates:
        if len(bis_four) >= 4:
            break
        if c in bis_four:
            continue
        pt = c["equip"]["preferred_training"]
        if stat_counts.get(pt, 0) >= 2:
            continue
        bis_four.append(c)
        stat_counts[pt] = stat_counts.get(pt, 0) + 1
    bis_score_loadout = ([whistle_entry] if whistle_entry else []) + bis_four

    # 2b. PVP 实战偏科集中方案（2+2 双核专精 & 3+1 极攻专精）
    pvp_22_loadout = _build_specialized_loadout(all_candidates, char_obj, whistle_entry, "pvp_22", focus_stat_name)
    pvp_31_loadout = _build_specialized_loadout(all_candidates, char_obj, whistle_entry, "pvp_31", focus_stat_name)

    if norm_mode == "pvp_22":
        bis_loadout = pvp_22_loadout
    elif norm_mode == "pvp_31":
        bis_loadout = pvp_31_loadout
    else:
        bis_loadout = bis_score_loadout

    owned_pattern = classify_loadout_pattern(owned_loadout, char_obj)
    bis_pattern = classify_loadout_pattern(bis_loadout, char_obj)
    pvp_22_pattern = classify_loadout_pattern(pvp_22_loadout, char_obj)
    pvp_31_pattern = classify_loadout_pattern(pvp_31_loadout, char_obj)

    wish_info = guide_db.get("star_wish_guides", {}).get(str(core_id))
    roles_str = "/".join(char_obj.get("role_type") or ["通用"])

    return {
        "character": char_obj,
        "mode": norm_mode,
        "roles_str": roles_str,
        "owned_loadout": owned_loadout,
        "owned_benched": owned_benched,
        "owned_pattern": owned_pattern,
        "bis_loadout": bis_loadout,
        "bis_pattern": bis_pattern,
        "bis_score_loadout": bis_score_loadout,
        "pvp_22_loadout": pvp_22_loadout,
        "pvp_22_pattern": pvp_22_pattern,
        "pvp_31_loadout": pvp_31_loadout,
        "pvp_31_pattern": pvp_31_pattern,
        "owned_pvp_loadout": owned_pvp_loadout,
        "owned_pvp_pattern": owned_pvp_pattern,
        "owned_pvp_note": owned_pvp_note,
        "wish_headline": wish_info["headline"] if wish_info else "",
        "non_stacking_rules": guide_db.get("non_stacking_talents", []),
    }


def validate_equip_loadout(
    equipped_cfg_ids: list[int],
    equip_pool: dict = None,
) -> dict:
    """
    编队器材硬防呆校验（防开局漏借 11004 支援口哨裸跑）：
    返回 {"ok": bool, "has_whistle": bool, "warnings": list[str]}。
    """
    ids = [int(x) for x in (equipped_cfg_ids or []) if x]
    if not ids:
        return {"ok": True, "has_whistle": False, "warnings": []}
    has_whistle = 11004 in ids
    warnings = []
    if not has_whistle:
        if equip_pool is None:
            _, equip_pool, _ = load_equip_data()
        owned = (equip_pool or {}).get("owned_cards") or {}
        if "11004" in owned or 11004 in owned:
            warnings.append("🚨 漏带核心【支援口哨(11004)】：无哨子将损失 +110 体力与约半数经理爆发，请立即补上！")
        else:
            warnings.append("🚨 漏借核心【支援口哨(11004)】：自有库无口哨，必须在助战槽借好友满破口哨（勿把助战槽错借自有卡）！")
    return {"ok": has_whistle, "has_whistle": has_whistle, "warnings": warnings}


def check_lineup_and_equip_synergy(
    core_id: int,
    teammate_ids: list[int] = None,
    equipped_cfg_ids: list[int] = None,
) -> list[str]:
    """
    用于第 01 训开局 HUD 体检：
    0. 防漏带/漏借 11004 支援口哨硬告警；
    1. 检测阵容中是否误带『羽濑川+春澄』或『羽田+清水』同名不叠加陪练；
    2. 检测本局实际携带的 5 张器材中是否有金技能角色定位不匹配（空转）的卡；
    3. 若主练角色为望月透/稻叶杏里/洺瑶，附带 1 行星愿顶配公式提醒。
    """
    if not core_id:
        return []
    guide_db, equip_pool, chars_by_id = load_equip_data()
    equips = guide_db.get("equipments", {})
    char_obj = chars_by_id.get(int(core_id), {})
    tips = []

    # 0. 防漏带/漏借 11004 支援口哨红灯告警
    if equipped_cfg_ids:
        v_res = validate_equip_loadout(equipped_cfg_ids, equip_pool=equip_pool)
        tips.extend(v_res["warnings"])

    # 1. 同名陪练天赋不叠加检查（陪练与陪练之间，或主力+陪练同名机制冲突）
    all_party = set(int(x) for x in (teammate_ids or []) if x)
    if core_id:
        all_party.add(int(core_id))
    for rule in guide_db.get("non_stacking_talents", []):
        pair = set(rule["actors"])
        if pair.issubset(all_party):
            tips.append(rule["warning"])

    # 2. 实带器材金技能空转检查
    idle_names = []
    for cfg in (equipped_cfg_ids or []):
        if int(cfg) == 11004:
            continue
        eq = equips.get(str(cfg))
        if eq and char_obj:
            ev = evaluate_skill_match(eq, char_obj)
            if not ev["active"]:
                req = eq.get("official_tag", "").split("/")[0].split("+")[0]
                idle_names.append(f"{eq['name']}[需{req}]")
    if idle_names:
        roles_s = "/".join(char_obj.get("role_type") or [])
        tips.append(f"🎒 器材提醒：{'、'.join(idle_names)} 对{char_obj.get('short_name','') }({roles_s})金技不生效，下局可换同定位/通用卡")

    # 3. 特殊星愿提醒
    wish = guide_db.get("star_wish_guides", {}).get(str(core_id))
    if wish:
        tips.append(wish["headline"])

    return tips


def format_equip_advice_hud_markup(core_id: int, focus_stat: Any = _UNSET) -> tuple[str, list[dict]]:
    """
    返回供选人界面 HUD 嵌入的 (pango_markup_block, icon_strip_items)：
    - pango_markup_block: 仅保留关键避坑/轮休换下/特殊星愿极简提醒（具体 5 张配卡直接由底部 22×22 图标条直观展示，不重复刷文字）
    - icon_strip_items: [{"name": ..., "icon_path": ..., "pref": ..., "is_borrow": ...}, ...] 供 GTK 渲染 22×22 真实器材图标条
    """
    rec = recommend_equipments_for_character(core_id, focus_stat=focus_stat)
    c = rec["character"]
    lines = []

    icon_items = []
    used_owned = bool(rec["owned_loadout"] and len(rec["owned_loadout"]) >= 2)
    if used_owned:
        for item in rec["owned_loadout"]:
            eq = item["equip"]
            icon_items.append({
                "id": eq["id"],
                "name": eq["name"],
                "pref": eq["preferred_training"],
                "icon_path": item["icon_path"],
                "active": item["skill_eval"]["active"],
                "is_borrow": bool(item.get("is_borrow", eq["id"] == 11004 and not item.get("owned_info"))),
            })

        if rec["owned_benched"]:
            b0 = rec["owned_benched"][0]
            if not b0["skill_eval"]["active"]:
                bnm = html.escape(b0["equip"]["name"])
                lines.append(
                    f"<span size='small' foreground='#fca5a5'>🎒 轮休：<b>{bnm}</b>(金技不适配)</span>"
                )

    if not icon_items and rec["bis_loadout"]:
        for item in rec["bis_loadout"][:5]:
            eq = item["equip"]
            icon_items.append({
                "id": eq["id"],
                "name": eq["name"],
                "pref": eq["preferred_training"],
                "icon_path": item["icon_path"],
                "active": True,
                "is_borrow": (eq["id"] == 11004),
            })

    if used_owned:
        active_pat = rec.get("owned_pattern") or {}
        if active_pat.get("tag"):
            pat_col = "#34d399" if active_pat.get("archetype") in ("pvp_22", "pvp_31") else "#94a3b8"
            lines.append(f"<span size='small' foreground='{pat_col}'>阵型：{html.escape(active_pat['tag'])}</span>")
    else:
        bis_pat = rec.get("bis_pattern") or {}
        pat_suffix = f" · 阵型：{html.escape(bis_pat['tag'])}" if bis_pat.get("tag") else ""
        lines.append(
            f"<span size='small' foreground='#7dd3fc'>🎒 通用毕业 5 卡推荐（暂未同步个人器材池）{pat_suffix}</span>"
        )

    if rec["wish_headline"]:
        wh_short = re.sub(r"（.*?）", "", rec["wish_headline"]).replace("星愿标签组合：首选", "星愿：").strip()
        lines.append(f"<span size='small' foreground='#f9a8d4'>{html.escape(wh_short)}</span>")

    cid = int(c.get("id", 0))
    for rule in rec["non_stacking_rules"]:
        if cid in rule["actors"]:
            other_idx = 1 if rule["actors"][0] == cid else 0
            other_name = rule["names"][other_idx]
            lines.append(
                f"<span size='small' foreground='#fde047'>⚠️ 避坑：与<b>{html.escape(other_name)}</b>『{html.escape(rule['talent'])}』不叠加</span>"
            )

    return "\n".join(lines), icon_items


def resolve_char_query(query: str, chars_by_id: dict) -> Optional[int]:
    q = query.strip()
    if q.isdigit() and int(q) in chars_by_id:
        return int(q)
    for cid, c in chars_by_id.items():
        names = [c.get("short_name", ""), c.get("show_name", "")] + list(c.get("aliases") or [])
        if any(q == n or q in n for n in names if n):
            return cid
    return None


def format_terminal_report(core_id: int, mode: str = "score", focus_stat: Any = _UNSET) -> str:
    rec = recommend_equipments_for_character(core_id, mode=mode, focus_stat=focus_stat)
    c = rec["character"]
    cname = c.get("show_name") or c.get("short_name")
    mode_lbl = {"score": "冲评分向(默认)", "pvp_22": "PVP实战·2+2双核专精", "pvp_31": "PVP实战·3+1极攻暴击"}.get(rec["mode"], "冲评分向")
    own_whistle = any(int(it["equip"]["id"]) == 11004 and it.get("owned_info") for it in rec["owned_loadout"])
    slot_title = (
        "💎 一、你的账号实盘最优 5 卡（自有满破支援口哨 + 自有 3 + 借 1 缺失 SSR）"
        if own_whistle
        else "💎 一、你的账号实盘最优 5 卡（1 好友满破支援口哨 + 自有高破 SSR 选 4）"
    )
    lines = [
        "=" * 92,
        f"🎒 【开局选人 · 本命器材配装参谋】目标球员：{cname} (ID:{c['id']} · {c.get('best_pos','')}) | 视角：{mode_lbl}",
        f"📌 球员定位：{rec['roles_str']} | 风格：{c.get('style','')} | 流派：{c.get('edu_stream','')} | 推荐主练：{'/'.join(c.get('recommend_train') or [])}",
        "-" * 92,
        f"{slot_title} | 阵型：{rec['owned_pattern']['tag']}",
    ]
    for i, item in enumerate(rec["owned_loadout"], 1):
        eq = item["equip"]
        oinfo = item.get("owned_info")
        lv_str = f"Lv.{oinfo['level']} {oinfo['break_level']}破" if oinfo else "好友满破3觉"
        sev = item["skill_eval"]
        lines.append(
            f"  #{i} [{lv_str}] {eq['name']} (主练:{eq['preferred_training']} | 满级三维:{eq['lv50_composite']}·{eq['stat_tier']}档 | 实战榜:{eq['community_tier']})"
        )
        lines.append(
            f"     └─ {sev['badge']} | 金技『{eq['skill_name']}』：{eq['skill_summary']}"
        )
    if rec["owned_benched"]:
        lines.append("  🚫 建议本局轮休换下：")
        for b in rec["owned_benched"]:
            eq = b["equip"]
            lines.append(f"     • {eq['name']} (主练:{eq['preferred_training']}) — {b['skill_eval']['warn'] or '综合契合度次于上方4张'}")
    lines.append(f"  {rec['owned_pvp_note']}")

    lines.append(f"\n🌟 二、全图鉴毕业顶配 5 卡参考（当前视角：{mode_lbl} | 阵型：{rec['bis_pattern']['tag']}）：")
    for i, item in enumerate(rec["bis_loadout"], 1):
        eq = item["equip"]
        sev = item["skill_eval"]
        lines.append(
            f"  👑 #{i} {eq['name']} (主练:{eq['preferred_training']} | 满级属性:+{eq['lv50_stat']} 综合{eq['lv50_composite']}·{eq['stat_tier']}档 | 实战:{eq['community_tier']})"
        )
        lines.append(
            f"     └─ {sev['badge']} | 金技『{eq['skill_name']}』：{eq['community_note']}"
        )

    def _fmt_compact_cards(loadout: list[dict]) -> str:
        return " + ".join(
            f"{it['equip']['name']}({it['equip']['preferred_training']})"
            for it in loadout
            if int(it["equip"]["id"]) != 11004
        )

    lines.append("\n⚔️ 三、PVP 实战偏科集中配装参考（高配防 9000 水桶虚战，主属性直冲 2500~3400）：")
    lines.append(f"  • 【2+2 双核专精】{rec['pvp_22_pattern']['tag']}：支援口哨 + {_fmt_compact_cards(rec['pvp_22_loadout'])}")
    lines.append(f"  • 【3+1 极攻暴击】{rec['pvp_31_pattern']['tag']}：支援口哨 + {_fmt_compact_cards(rec['pvp_31_loadout'])}")
    lines.append("  💡 实战铁律：防守属性在防守回合无论选哪张卡都会生效（体能之下第一属性）；3+1/2+2 高度依赖卡池深度，前期优先拉满主属性卡熟练度，后期死磕主维再补体能/意识。")

    if rec["wish_headline"]:
        lines.append(f"\n{rec['wish_headline']}")
    lines.append("=" * 92)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="篮球少女本命器材配装参谋")
    parser.add_argument("--char", "-c", type=str, default="", help="查询角色名称或 ID（如：望月、稻叶、克拉丽丝）")
    parser.add_argument("--mode", "-m", choices=("score", "pvp_22", "pvp_31"), default="score", help="配装视角：score(冲评分向默认) / pvp_22(实战2+2双核) / pvp_31(实战3+1极攻)")
    parser.add_argument("--pool", action="store_true", help="查看当前已识别的自有器材池与全图鉴 S/夯 榜")
    args = parser.parse_args()

    guide_db, equip_pool, chars_by_id = load_equip_data()
    if args.pool or not args.char:
        equips = guide_db.get("equipments", {})
        print("=" * 88)
        print(f"📦 【自有常驻主力器材池】（共识别 {len(equip_pool.get('owned_cards', {}))} 张实装上阵卡，缓存总卡号 {equip_pool.get('total_owned_card_ids_in_cache', 0)} 张）")
        print("-" * 88)
        for cfg, info in equip_pool.get("owned_cards", {}).items():
            eq = equips.get(str(cfg), {})
            print(
                f"  • {info['name']} (ID:{cfg}) — Lv.{info['level']} ({info['break_level']}破/{info['awake_level']}觉) "
                f"| 主练:{eq.get('preferred_training')} | 官方标签:{eq.get('official_tag')} | 三维:{eq.get('stat_tier')}({eq.get('lv50_composite')}) | 实战:{eq.get('community_tier')}"
            )
        if not args.char:
            print("\n💡 用法提示：输入 `hud equip 望月` 或 `python3 src/equipment_advisor.py -c 龙宫寺 --mode pvp_22` 查看专属 5 卡配装与 PVP 集中方案！")
            return

    cid = resolve_char_query(args.char, chars_by_id)
    if not cid:
        print(f"❌ 未找到匹配角色：{args.char}")
        sys.exit(1)
    print(format_terminal_report(cid, mode=args.mode))


if __name__ == "__main__":
    main()
