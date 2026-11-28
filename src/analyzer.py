#!/usr/bin/env python3
"""
LQSNCoach — 历史战绩增量归档与 v0.7.0 自学习诊断引擎 (Incremental Run Archiver & Replay Analyzer)
- 自动将每局完赛育成浓缩为 8~12 KB 的结构化 JSON 存入 data/runs/archive/<run_id>.json
- 通过 data/runs/archive_manifest.json 记录已归档日志的 (mtime, size)，日常增量更新无需重扫历史大日志
- 支持 --rebuild 在军师估值模型大版本升级后一键重算全部历史归档
"""

import argparse
import glob
import json
import os
import re
import sys
import threading
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from platform_compat import BASE_DIR, resolve_game_dirs

_, LOG_DIR = resolve_game_dirs(interactive_on_missing=True)
_INITIAL_LOG_DIR = LOG_DIR
_EXTRACT_LOCK = threading.Lock()
RUNS_DIR = os.path.join(BASE_DIR, "data", "runs")
ARCHIVE_DIR = os.path.join(RUNS_DIR, "archive")
MANIFEST_PATH = os.path.join(RUNS_DIR, "archive_manifest.json")
OUT_PATH = os.path.join(RUNS_DIR, "detailed_runs.json")

BUILDING_NAMES = {
    0: "周六加训",
    1: "寝室休息",
    2: "器材室",
    3: "街机厅",
    4: "散心/神社",
    5: "体育馆",
    6: "扭蛋机",
    7: "商店",
    8: "打工",
}


def parse_train_cmd_block(block_text: str):
    """
    从包含 train_cmds 的 [EDU_MODEL] 更新育成数据 块中提取 6 个训练按钮的完整面板数据。
    使用按 train_type = 1..6 切分的方式，避免嵌套花括号导致正则提前截断。
    """
    tc_pos = block_text.find("train_cmds = {")
    if tc_pos == -1:
        return []
    sub = block_text[tc_pos:]

    item_re = re.compile(
        r"before_bonus_property_tbl\s*=\s*\{(.*?)\},\s*company_supactor_tbl\s*=\s*\{(.*?)\},\s*company_teammate_tbl\s*=\s*\{(.*?)\},\s*fail_rate\s*=\s*(\d+),\s*idx\s*=\s*(\d+),\s*is_edu_manager\s*=\s*(true|false),\s*ps\s*=\s*(-?\d+),\s*pt\s*=\s*(-?\d+),\s*train_type\s*=\s*(\d+)",
        re.S,
    )
    cmds = []
    for m in item_re.finditer(sub):
        props_raw = m.group(1)
        sups_raw = m.group(2)
        mates_raw = m.group(3)
        fail_rate = int(m.group(4))
        idx = int(m.group(5))
        is_mgr = m.group(6) == "true"
        ps_cost = int(m.group(7))
        pt_gain = int(m.group(8))

        gains = {}
        for pm in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(-?\d+)", props_raw):
            gains[int(pm.group(1))] = int(pm.group(2))

        sup_cards = [
            {"card_id": int(km.group(1)), "val": int(km.group(2))}
            for km in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(\d+)", sups_raw)
        ]
        sup_count = len(sup_cards)
        gold_count = sum(1 for sc in sup_cards if sc["val"] == 1)
        rainbow_count = sum(1 for sc in sup_cards if sc["val"] >= 2)
        mate_ids = [int(x) for x in re.findall(r"teammate_id\s*=\s*(\d+)", mates_raw)]
        mate_count = len(mate_ids)

        mate_extra_gains = 0
        for pm in re.finditer(r"key\s*=\s*(\d+),\s*value\s*=\s*(-?\d+)", mates_raw):
            k, v = int(pm.group(1)), int(pm.group(2))
            if 101 <= k <= 106:
                gains[k] = gains.get(k, 0) + v
                mate_extra_gains += v

        cmds.append({
            "idx": idx,
            "gains": gains,
            "total_stat_gain": sum(gains.values()),
            "mate_extra_gains": mate_extra_gains,
            "sup_cards": sup_cards,
            "sup_count": sup_count,
            "gold_count": gold_count,
            "rainbow_count": rainbow_count,
            "mate_ids": mate_ids,
            "mate_count": mate_count,
            "is_manager": is_mgr,
            "fail_rate": fail_rate,
            "ps": ps_cost,
            "pt": pt_gain,
        })
    return cmds


def _extract_opening_note_scores(seg: str) -> list[int]:
    """定位当局首个含 edu_note_list 的 [EDU_MODEL] 块，提取 note_score（避免 45KB 截断与重复计数）。"""
    for m in re.finditer(r"\[EDU_MODEL\][^\n]*\{", seg):
        brace_start = m.end() - 1
        depth = 0
        end_pos = -1
        limit = min(len(seg), brace_start + 300000)
        for i in range(brace_start, limit):
            ch = seg[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i + 1
                    break
        if end_pos == -1:
            continue
        blk = seg[brace_start:end_pos]
        if ("edu_note_list" in blk or "borrow_note_info" in blk) and "note_score" in blk:
            return [int(x) for x in re.findall(r"note_score\s*=\s*(\d+)", blk)]
    return []


def sanitize_run_id(log_tag: str) -> str:
    """将 log_tag 转换为安全紧凑的文件名 ID。"""
    s = log_tag.replace("___log.txt", "").replace(".txt", "").replace("➔", "__to__").replace("#", "_r")
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", s)


def build_single_run_archive(item: dict, seq_no: int, lc, strategy: dict) -> dict | None:
    """从单局拼接日志文本 seg 中提取汇总指标与完整逐回合时间线。"""
    seg = item["seg"]
    res = lc.extract_education_result_ranks(seg)
    if not res:
        return None
    final_rank = res[-1]

    turns, final_st = lc.parse_state_and_turns_from_text(seg, return_final_state=True)
    if len(turns) < 15:
        return None
    wk_turns = lc.parse_weekend_turns_from_text(seg)
    story_turns = lc.parse_story_turns_from_text(seg)

    # 精确结算六维与上限
    final_stats = {int(k): int(v) for k, v in final_st.get("props", {}).items()}
    if sum(final_stats.values()) < 3000:
        props_all = re.findall(
            r"property_tbl\s*=\s*\{\s*"
            r"\[1\]\s*=\s*\{key\s*=\s*101,value\s*=\s*(\d+)\},\s*"
            r"\[2\]\s*=\s*\{key\s*=\s*102,value\s*=\s*(\d+)\},\s*"
            r"\[3\]\s*=\s*\{key\s*=\s*103,value\s*=\s*(\d+)\},\s*"
            r"\[4\]\s*=\s*\{key\s*=\s*104,value\s*=\s*(\d+)\},\s*"
            r"\[5\]\s*=\s*\{key\s*=\s*105,value\s*=\s*(\d+)\},\s*"
            r"\[6\]\s*=\s*\{key\s*=\s*106,value\s*=\s*(\d+)\}",
            seg,
        )
        best_tuple = max(props_all, key=lambda t: sum(int(x) for x in t)) if props_all else ()
        if best_tuple:
            final_stats = {101 + k: int(v) for k, v in enumerate(best_tuple)}

    prop_max = {int(k): int(v) for k, v in final_st.get("prop_max", {}).items()}
    capped_stats = [
        lc.STAT_NAMES[k]
        for k in range(101, 107)
        if final_stats.get(k, 0) >= prop_max.get(k, 1600)
    ]
    short_stats = [
        f"{lc.STAT_NAMES[k]}({final_stats.get(k, 0)})"
        for k in range(101, 107)
        if final_stats.get(k, 0) < 1150
    ]

    # 1. 平时训练逐回合轨迹与 EV 损耗（周六加训仅在 weekend_timeline 计入匹配率，避免与平日 coach_match_pct 双计）
    t_match = 0
    t_top2 = 0
    weekday_only_count = 0
    weekend_train_count = 0
    total_ev_loss = 0.0
    train_dist = Counter()
    weekday_train_dist = Counter()
    weekday_timeline = []
    suboptimal_list = []

    for t in turns:
        c_idx = t["chosen_idx"]
        st = t["state"]
        is_wk_train = bool(t.get("is_weekend_train"))
        train_dist[c_idx] += 1
        scored = lc.score_training_turn(st, t["cmds"], strategy)
        best = scored[0]
        chosen = next((x for x in scored if x["idx"] == c_idx), None)
        diff = round(best["score"] - chosen["score"], 1) if (chosen and best["score"] > chosen["score"]) else 0.0
        if is_wk_train:
            weekend_train_count += 1
        else:
            weekday_only_count += 1
            weekday_train_dist[c_idx] += 1
            if best["idx"] == c_idx:
                t_match += 1
            if len(scored) >= 2 and c_idx in (scored[0]["idx"], scored[1]["idx"]):
                t_top2 += 1
            total_ev_loss += diff
        _, w3_str = lc.get_whistle_step(st)

        turn_rec = {
            "turn_no": t["turn_no"],
            "exp_round": st.get("exp_round", 0),
            "is_weekend_train": is_wk_train,
            "ps": st.get("ps", 0),
            "gold": st.get("gold", 0),
            "mgr_energy": st.get("mgr_energy", 0),
            "mgr_explosive": st.get("mgr_explosive", 0),
            "whistle": w3_str,
            "focus_levels": {str(k): v for k, v in st.get("focus_levels", {}).items()},
            "props": {str(k): v for k, v in st.get("props", {}).items()},
            "chosen_idx": c_idx,
            "chosen_name": chosen["name"] if chosen else f"#{c_idx}",
            "chosen_ev": round(chosen["score"], 1) if chosen else 0.0,
            "best_idx": best["idx"],
            "best_name": best["name"],
            "best_ev": round(best["score"], 1),
            "ev_loss": diff,
            "bought_items": t.get("bought_items", []),
        }
        weekday_timeline.append(turn_rec)

        if not is_wk_train and chosen and diff >= 12.0:
            shop_tip = lc.format_weekday_shop_tip(
                st, best_ev=best["score"], strategy=strategy, scored_cmds=scored
            )
            suboptimal_list.append({
                "turn_no": t["turn_no"],
                "exp_round": st.get("exp_round", 0),
                "ps": st.get("ps", 0),
                "gold": st.get("gold", 0),
                "whistle": w3_str,
                "chosen_name": chosen["name"],
                "chosen_ev": round(chosen["score"], 1),
                "chosen_reasons": chosen.get("reasons", ""),
                "best_name": best["name"],
                "best_ev": round(best["score"], 1),
                "best_reasons": best.get("reasons", ""),
                "ev_loss": diff,
                "shop_tip": shop_tip,
            })

    suboptimal_list.sort(key=lambda x: x["ev_loss"], reverse=True)

    # 2. 周末出行逐回合轨迹与商店/扭蛋明细
    bldg_dist = Counter()
    wk_match = 0
    weekend_timeline = []

    for wt in wk_turns:
        b_id, s_id = wt["chosen_act"]
        st = wt["state"]
        bldg_dist[b_id] += 1
        wk_adv = lc.evaluate_weekend_and_shop(st, strategy)
        best_act = wk_adv.get("best_act")
        best_ev = round(wk_adv.get("best_ev", 0.0), 1)
        chosen_info = wk_adv.get("act_ev_map", {}).get((b_id, s_id))
        chosen_ev = round(chosen_info["total_ev"], 1) if chosen_info else round(best_ev - 25.0, 1)
        is_match = (b_id, s_id) == best_act or (best_ev - chosen_ev) <= 6.0
        if is_match:
            wk_match += 1

        gasha_picked = None
        if b_id == 6 and wt.get("chosen_gasha_idx"):
            g_idx = wt["chosen_gasha_idx"]
            g_pool = st.get("gashapon_list", [])
            if 1 <= g_idx <= len(g_pool):
                g_cfg = g_pool[g_idx - 1].get("cfg_id")
                g_note = lc.GASHAPON_CFG_NOTES.get(g_cfg, f"扭蛋#{g_cfg}")
                gasha_picked = {"index": g_idx, "cfg_id": g_cfg, "desc": g_note}

        chosen_bldg_str = (
            f"周六加训·{lc.TRAIN_NAMES.get(s_id, str(s_id))}"
            if b_id == 0
            else BUILDING_NAMES.get(b_id, f"建筑#{b_id}")
        )
        weekend_timeline.append({
            "wk_no": wt["wk_no"],
            "exp_round": st.get("exp_round", 0),
            "day_tag": wk_adv.get("day_tag", ""),
            "ps": st.get("ps", 0),
            "gold": st.get("gold", 0),
            "chosen_act": [b_id, s_id],
            "chosen_bldg_name": chosen_bldg_str,
            "chosen_ev": chosen_ev,
            "best_act": list(best_act) if best_act else None,
            "best_headline": wk_adv.get("headline", ""),
            "best_ev": best_ev,
            "is_match": is_match,
            "gasha_picked": gasha_picked,
            "bought_items": wt.get("bought_items", []),
        })

    # 3. 其它全局统计（老虎机、社团三选一、开局笔记、剧情分支）
    blds = [int(x) for x in re.findall(r"request_call:\s+education\.cs_select_team_building\s*\{\s*index\s*=\s*(\d+)", seg)]
    buys = len(re.findall(r"request_call:\s+education\.cs_buy_edu_item", seg))
    slot_picks = [
        int(x)
        for x in re.findall(
            r"request_call:\s+education\.cs_select_edu_slot_machines\s*\{[^}]*(?:slot_id|slot_machine_id)\s*=\s*(\d+)",
            seg,
        )
    ]
    slots = len(re.findall(r"request_call:\s+education\.cs_select_edu_slot_machines", seg))
    note_scores = _extract_opening_note_scores(seg)
    lineup_str = lc.format_lineup_summary(final_st) or "未知阵容"
    log_tag = (
        f"{item['log']}➔{item['end_log']}"
        if item["end_log"] != item["log"]
        else f"{item['log']}#{item['run_index']}"
    )
    run_id = sanitize_run_id(log_tag)

    # v0.8.0 北极星指标：每张可暴击器材首次达到 100 熟练度（满彩）的训次（归属到触发满 100 的那一训，消除晚记 1 训偏移）
    non_crit = getattr(lc, "NON_CRIT_EQUIPMENT_IDS", {11004, 11005})
    equip_max_turns = {}
    equip_max_rounds = {}
    crit_cfg_seen = set()
    prev_turn = None
    for t in turns:
        ct = t["state"].get("supactor_card_trammel", {}) or {}
        c2c = t["state"].get("supactor_card_to_cfg", {}) or {}
        trigger_t = prev_turn if prev_turn is not None else t
        for cid, v in ct.items():
            cfg = int(c2c.get(cid, cid))
            if cfg in non_crit:
                continue
            crit_cfg_seen.add(cfg)
            if int(v) >= 100 and str(cfg) not in equip_max_turns:
                equip_max_turns[str(cfg)] = trigger_t["turn_no"]
                equip_max_rounds[str(cfg)] = trigger_t["state"].get("exp_round", 0)
        prev_turn = t
    for cid, v in (final_st.get("supactor_card_trammel", {}) or {}).items():
        cfg = int((final_st.get("supactor_card_to_cfg", {}) or {}).get(cid, cid))
        if cfg not in non_crit:
            crit_cfg_seen.add(cfg)
            if int(v) >= 100 and str(cfg) not in equip_max_turns:
                last_t = turns[-1] if turns else None
                equip_max_turns[str(cfg)] = last_t["turn_no"] if last_t else 1
                equip_max_rounds[str(cfg)] = last_t["state"].get("exp_round", 0) if last_t else 0
    crit_maxed_by_t12 = sum(1 for tn in equip_max_turns.values() if tn <= 12)
    crit_maxed_by_t15 = sum(1 for tn in equip_max_turns.values() if tn <= 15)

    summary = {
        "seq_no": seq_no,
        "run_id": run_id,
        "log": item["log"],
        "end_log": item["end_log"],
        "log_tag": log_tag,
        "run_index": item["run_index"],
        "final_rank": final_rank,
        "lineup": lineup_str,
        "final_stats": {str(k): v for k, v in final_stats.items()},
        "prop_max": {str(k): v for k, v in prop_max.items()},
        "stat_sum": sum(final_stats.values()),
        "min_stat": min(final_stats.values()) if final_stats else 0,
        "max_stat": max(final_stats.values()) if final_stats else 0,
        "capped_stats": capped_stats,
        "short_stats": short_stats,
        "mgr_explosive": final_st.get("mgr_explosive", 0),
        "mgr_max_explosive": final_st.get("mgr_max_explosive", 99),
        "equip_max_turns": equip_max_turns,
        "equip_max_rounds": equip_max_rounds,
        "crit_equip_total": len(crit_cfg_seen),
        "crit_maxed_total": len(equip_max_turns),
        "crit_maxed_by_t12": crit_maxed_by_t12,
        "crit_maxed_by_t15": crit_maxed_by_t15,
        "focus_levels": {str(k): v for k, v in final_st.get("focus_levels", {}).items()},
        "train_count": len(turns),
        "weekday_train_count": weekday_only_count,
        "weekend_train_count": weekend_train_count,
        "train_dist": {str(k): v for k, v in train_dist.items()},
        "weekday_train_dist": {str(k): v for k, v in weekday_train_dist.items()},
        "coach_match_pct": round(t_match / max(1, weekday_only_count) * 100, 1),
        "coach_top2_pct": round(t_top2 / max(1, weekday_only_count) * 100, 1),
        "avg_turn_ev_loss": round(total_ev_loss / max(1, weekday_only_count), 1),
        "weekend_count": len(wk_turns),
        "weekend_match_pct": round(wk_match / max(1, len(wk_turns)) * 100, 1),
        "building_dist": {str(k): v for k, v in bldg_dist.items()},
        "team_building_choices": blds,
        "store_buys": buys,
        "slot_machines": slots,
        "slot_machine_ids": slot_picks,
        "note_scores": note_scores[:3],
    }

    detail = dict(summary)
    detail["weekday_timeline"] = weekday_timeline
    detail["weekend_timeline"] = weekend_timeline
    detail["top_suboptimal_turns"] = suboptimal_list[:8]
    detail["story_choices"] = [
        {"ev_id": s["ev_id"], "ev_name": s["ev_name"], "chosen_branch": s["chosen_branch"]}
        for s in story_turns
    ]
    return {"summary": summary, "detail": detail}


MANIFEST_SCHEMA_VERSION = 5


def _parse_archive_sort_key(run_id: str, log_name: str = "") -> tuple:
    src = run_id or log_name or ""
    m = re.match(r"(\d{2})_(\d{2})___(\d{2})_(\d{2})_(\d{2})(?:_r(\d+))?", src)
    if m:
        mo, dy, hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        ridx = int(m.group(6)) if m.group(6) else 0
        yr_ord = 0 if mo >= 9 else 1
        return (yr_ord, mo, dy, hh, mm, ss, ridx, src)
    return (9, 99, 99, 99, 99, 99, 0, src)


def _is_plausible_archived_run(arch_data: dict) -> bool:
    """校验原始日志已清理的历史归档是否合理，过滤旧版 v3 误采队伍总评价/PVP 的假分。"""
    if not isinstance(arch_data, dict):
        return False
    try:
        rk = int(arch_data.get("final_rank") or 0)
    except Exception:
        return False
    if not (1000 <= rk <= 20000):
        return False
    if not str(arch_data.get("lineup") or "").strip():
        return False
    fstats = arch_data.get("final_stats")
    if isinstance(fstats, dict) and fstats:
        try:
            stat_sum = sum(int(v or 0) for v in fstats.values())
        except Exception:
            stat_sum = 0
        # 真实育成 (final_rank - stat_sum) 稳定在 +250~+425；放宽至 [-500, +2500] 既不误杀真局，
        # 又能拦截旧版 v3 误采的队伍总评/PVP 排名假分（如 14000~19999 或异常低分）
        if stat_sum >= 4000 and not (stat_sum - 500 <= rk <= stat_sum + 2500):
            return False
    return True


def _load_manifest() -> dict:
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "log_meta": {}, "runs_order": [], "runs_meta": []}


def _resolve_active_log_dir(log_dir: str | None = None) -> str:
    """动态解析有效日志目录：显式传参 > 外部改写/已存在的模块 LOG_DIR > 实时重新探测。"""
    global LOG_DIR
    if log_dir is not None:
        return str(log_dir).strip()
    if LOG_DIR != _INITIAL_LOG_DIR:
        return str(LOG_DIR or "").strip()
    if LOG_DIR and os.path.isdir(LOG_DIR):
        return LOG_DIR
    _, fresh_log_dir = resolve_game_dirs(interactive_on_missing=False)
    if fresh_log_dir:
        LOG_DIR = fresh_log_dir
    return str(LOG_DIR or "").strip()


def extract_all_runs(
    strategy_name: str = "training_flow",
    force_rebuild: bool = False,
    log_dir: str | None = None,
) -> list[dict]:
    """
    真·按文件增量同步全部已完赛对局：
    - 对 (mtime, size) 未变化的历史日志，连 open().read() 都不调用，直接从 data/runs/archive/<run_id>.json 秒读
    - 仅当存在新日志或正在写入的最新日志时，只读取从最后一个未完结/变更日志起的尾部 1~2 个文件
    """
    with _EXTRACT_LOCK:
        return _extract_all_runs_locked(
            strategy_name=strategy_name,
            force_rebuild=force_rebuild,
            log_dir=log_dir,
        )


def _extract_all_runs_locked(
    strategy_name: str = "training_flow",
    force_rebuild: bool = False,
    log_dir: str | None = None,
) -> list[dict]:
    active_log_dir = _resolve_active_log_dir(log_dir)
    if not active_log_dir or not os.path.isdir(active_log_dir):
        raise FileNotFoundError(
            f"游戏日志目录无效或未绑定：{active_log_dir!r}。请先在桌面控制台选择《篮球少女》游戏目录。"
        )
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    manifest = _load_manifest()
    prev_runs_order = list(manifest.get("runs_order") or [])
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        # 协议解析/归档规则升级：清空 log_meta 强制重算当前现存日志，
        # 同时保留原有 runs_meta 并由下文兜底召回原始日志已被清理的历史归档，不丢失老战绩
        manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
        manifest["log_meta"] = {}
        force_rebuild = True
    elif force_rebuild:
        # 强制重建：仅清空日志指纹让现存日志全部重算；保留 runs_meta，
        # 使原始日志已被清理的历史老局（archive/*.json 仍在）照常并入战绩总表，不被 --rebuild 丢弃
        manifest["log_meta"] = {}

    logs = sorted(
        glob.glob(os.path.join(active_log_dir, "*.txt")),
        key=lambda p: (os.path.getmtime(p), os.path.basename(p)),
    )
    cur_log_meta = {}
    changed_logs = set()
    for lp in logs:
        bn = os.path.basename(lp)
        st = os.stat(lp)
        meta = {"mtime": round(st.st_mtime, 2), "size": st.st_size}
        cur_log_meta[bn] = meta
        old_m = manifest.get("log_meta", {}).get(bn)
        if not old_m or old_m.get("mtime") != meta["mtime"] or old_m.get("size") != meta["size"]:
            changed_logs.add(bn)

    # 快速通道：无任何日志变更时直接返回 detailed_runs.json
    if not changed_logs and not force_rebuild and os.path.exists(OUT_PATH) and manifest.get("runs_order"):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                cached_runs = json.load(f)
            if len(cached_runs) == len(manifest["runs_order"]):
                return sorted(cached_runs, key=lambda r: r.get("seq_no", 0))
        except Exception:
            pass

    import live_coach as lc
    strategy = lc.load_strategy(strategy_name)

    # 计算需要真正打开读取的最小日志集合：支持跨 3+ 文件续玩拼接，确保不丢弃前驱开局或中间段
    log_basenames = [os.path.basename(lp) for lp in logs]
    bn_to_idx = {bn: i for i, bn in enumerate(log_basenames)}
    if not force_rebuild and manifest.get("runs_meta"):
        first_dirty_idx = len(logs)
        for idx_l, bn in enumerate(log_basenames):
            if bn in changed_logs:
                first_dirty_idx = min(first_dirty_idx, max(0, idx_l - 1))
                break

        if first_dirty_idx < len(logs):
            def _inspect_log_boundary(lp: str) -> tuple[bool, bool]:
                try:
                    with open(lp, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                except Exception:
                    return False, False
                m_start = re.search(r"request_call:\s+education\.cs_start_education", txt)
                prefix = txt[:m_start.start()] if m_start else txt
                has_cont = any(
                    k in prefix
                    for k in ("cs_restore_education", "education.cs_", "education.sc_education_result", "[EDU_MODEL]")
                )
                has_settled = lc.has_education_result(prefix)
                return (m_start is not None), (has_cont and not has_settled)

            need_back = False
            for check_i in range(first_dirty_idx, len(logs)):
                if check_i == first_dirty_idx or log_basenames[check_i] in changed_logs:
                    _, orphan_cont = _inspect_log_boundary(logs[check_i])
                    if orphan_cont:
                        need_back = True
                        break
            if need_back:
                while first_dirty_idx > 0:
                    first_dirty_idx -= 1
                    has_start, orphan_cont = _inspect_log_boundary(logs[first_dirty_idx])
                    if has_start and not orphan_cont:
                        break

            # 若已有跨多文件归档局的结尾落入脏区，同步把起局日志拉入脏区
            for rm in manifest.get("runs_meta", []):
                end_i = bn_to_idx.get(rm.get("end_log", ""))
                start_i = bn_to_idx.get(rm.get("log", ""))
                if end_i is not None and end_i >= first_dirty_idx and start_i is not None:
                    first_dirty_idx = min(first_dirty_idx, start_i)

        dirty_log_set = set(log_basenames[first_dirty_idx:])
    else:
        dirty_log_set = set(log_basenames)

    all_summaries = []
    runs_order = []
    runs_meta = []

    # 1. 先直接复用 dirty_log_set 之前所有完全静止的历史归档（零大文件 IO）
    for rm in manifest.get("runs_meta", []):
        if rm["log"] not in dirty_log_set and rm["end_log"] not in dirty_log_set:
            arch_file = os.path.join(ARCHIVE_DIR, f"{rm['run_id']}.json")
            if os.path.exists(arch_file):
                try:
                    with open(arch_file, "r", encoding="utf-8") as af:
                        arch_data = json.load(af)
                    if not _is_plausible_archived_run(arch_data):
                        continue
                    summ = {
                        k: v
                        for k, v in arch_data.items()
                        if k not in ("weekday_timeline", "weekend_timeline", "top_suboptimal_turns", "story_choices")
                    }
                    summ["seq_no"] = len(all_summaries) + 1
                    all_summaries.append(summ)
                    runs_order.append(rm["run_id"])
                    runs_meta.append(rm)
                except Exception:
                    pass

    # 2. 仅对尾部 dirty_log_set 中的日志文件执行拼接与解析
    dirty_stitched = []
    for lp in logs:
        bn = os.path.basename(lp)
        if bn not in dirty_log_set:
            continue
        with open(lp, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if not txt:
            continue
        starts = [m.start() for m in re.finditer(r"request_call:\s+education\.cs_start_education", txt)]
        prefix = txt[:starts[0]] if starts else txt
        if (
            prefix
            and dirty_stitched
            and not dirty_stitched[-1]["has_settled"]
            and (
                "cs_restore_education" in prefix
                or "education.cs_" in prefix
                or "education.sc_education_result" in prefix
                or "new_rank" in prefix
                or "[EDU_MODEL]" in prefix
            )
        ):
            dirty_stitched[-1]["seg"] += "\n" + prefix
            dirty_stitched[-1]["end_log"] = bn
            if lc.has_education_result(dirty_stitched[-1]["seg"]):
                dirty_stitched[-1]["has_settled"] = True

        for i, st in enumerate(starts):
            ed = starts[i + 1] if i + 1 < len(starts) else len(txt)
            seg = txt[st:ed]
            has_settled = lc.has_education_result(seg)
            dirty_stitched.append({
                "log": bn,
                "end_log": bn,
                "run_index": i + 1,
                "seg": seg,
                "has_settled": has_settled,
            })

    already_kept_ids = set(runs_order)
    for item in dirty_stitched:
        log_tag = (
            f"{item['log']}➔{item['end_log']}"
            if item["end_log"] != item["log"]
            else f"{item['log']}#{item['run_index']}"
        )
        run_id = sanitize_run_id(log_tag)
        if run_id in already_kept_ids:
            continue
        arch_file = os.path.join(ARCHIVE_DIR, f"{run_id}.json")
        involved_changed = (item["log"] in changed_logs) or (item["end_log"] in changed_logs)
        if not force_rebuild and not involved_changed and os.path.exists(arch_file):
            try:
                with open(arch_file, "r", encoding="utf-8") as af:
                    arch_data = json.load(af)
                summ = {
                    k: v
                    for k, v in arch_data.items()
                    if k not in ("weekday_timeline", "weekend_timeline", "top_suboptimal_turns", "story_choices")
                }
                summ["seq_no"] = len(all_summaries) + 1
                all_summaries.append(summ)
                runs_order.append(run_id)
                runs_meta.append({"run_id": run_id, "log": item["log"], "end_log": item["end_log"]})
                continue
            except Exception:
                pass

        seq_no = len(all_summaries) + 1
        res_built = build_single_run_archive(item, seq_no, lc, strategy)
        if not res_built:
            continue

        summ = res_built["summary"]
        detail = res_built["detail"]
        with open(arch_file, "w", encoding="utf-8") as af:
            json.dump(detail, af, indent=2, ensure_ascii=False)
        all_summaries.append(summ)
        runs_order.append(run_id)
        runs_meta.append({"run_id": run_id, "log": item["log"], "end_log": item["end_log"]})

    # 3. 兜底召回：扫描 ARCHIVE_DIR 中尚未登记、且原始日志已不在当前 Logs/ 目录下的合法历史归档
    #    （修复旧版 schema v4 升级清空 runs_meta 或跨机迁入 archive/ 导致的老战绩丢失；
    #      若原始日志仍在当前 Logs/ 目录，则严格以 Step 2 最新解析结果为准，不复活被淘汰的假局）
    cur_log_names = set(log_basenames)
    seen_ids = set(runs_order)
    for arch_fp in sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.json"))):
        try:
            with open(arch_fp, "r", encoding="utf-8") as af:
                arch_data = json.load(af)
        except Exception:
            continue
        rid = str(arch_data.get("run_id") or os.path.splitext(os.path.basename(arch_fp))[0])
        if not rid or rid in seen_ids:
            continue
        arch_log = str(arch_data.get("log") or "")
        arch_end_log = str(arch_data.get("end_log") or arch_log)
        if (arch_log and arch_log in cur_log_names) or (arch_end_log and arch_end_log in cur_log_names):
            continue
        if not _is_plausible_archived_run(arch_data):
            continue
        summ = {
            k: v
            for k, v in arch_data.items()
            if k not in ("weekday_timeline", "weekend_timeline", "top_suboptimal_turns", "story_choices")
        }
        summ["run_id"] = rid
        all_summaries.append(summ)
        runs_order.append(rid)
        runs_meta.append({"run_id": rid, "log": arch_log, "end_log": arch_end_log})
        seen_ids.add(rid)

    # 统一按真实对局时间升序重排并重编 seq_no，保证历史召回局与新解析局时间线一致
    if all_summaries:
        combined = list(zip(all_summaries, runs_order, runs_meta))
        combined.sort(key=lambda t: _parse_archive_sort_key(t[1], t[2].get("log", "")))
        all_summaries = []
        runs_order = []
        runs_meta = []
        for idx_s, (summ, rid, rm) in enumerate(combined, start=1):
            summ["seq_no"] = idx_s
            all_summaries.append(summ)
            runs_order.append(rid)
            runs_meta.append(rm)

    # 防护：schema 升级/全量重算若意外得到 0 局，但磁盘上原先已有归档，
    # 禁止用空结果覆盖，避免并发扫描或日志目录异常把战绩表清空。
    if not all_summaries and prev_runs_order and force_rebuild:
        raise RuntimeError(
            f"全量重算未解析到任何完赛局（日志目录共 {len(logs)} 个文件），已保留原有 {len(prev_runs_order)} 局归档未覆盖。"
        )

    sorted_by_rank = sorted(all_summaries, key=lambda r: r["final_rank"], reverse=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted_by_rank, f, indent=2, ensure_ascii=False)

    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["log_meta"] = cur_log_meta
    manifest["runs_order"] = runs_order
    manifest["runs_meta"] = runs_meta
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return all_summaries


def load_archived_run(run_offset: int = 1) -> dict | None:
    """直接从增量归档库读取倒数第 run_offset 局的完整明细（自动触发毫秒级增量同步）。"""
    runs = extract_all_runs()
    if not runs:
        return None
    by_time = sorted(runs, key=lambda r: r["seq_no"])
    idx = max(0, len(by_time) - max(1, int(run_offset)))
    target = by_time[idx]
    run_id = target.get("run_id") or sanitize_run_id(target["log_tag"])
    arch_file = os.path.join(ARCHIVE_DIR, f"{run_id}.json")
    if os.path.exists(arch_file):
        with open(arch_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return target


def print_learning_report(runs: list[dict]):
    if not runs:
        print("未找到任何已结算的完整育成记录。")
        return

    by_time = sorted(runs, key=lambda r: r["seq_no"])
    by_rank = sorted(runs, key=lambda r: r["final_rank"], reverse=True)
    recent_runs = by_time[-5:]
    top_runs = by_rank[:5]
    mid_runs = [r for r in by_rank if 8000 <= r["final_rank"] < 8600]

    print("=" * 92)
    print(f"🏀 LQSNCoach v0.7.0 历史战绩全维复盘报告（共归档 {len(runs)} 局完整育成，单局明细已存 data/runs/archive/）")
    print("=" * 92)

    print("\n🕒 【最近 5 局实战成绩单（按时间从近到远）】")
    for r in reversed(recent_runs):
        cap_s = f"⚠️触顶:{'/'.join(r['capped_stats'])}" if r["capped_stats"] else "✅无溢出"
        short_s = f"📉短板:{'/'.join(r['short_stats'])}" if r["short_stats"] else "✅全维>=1150"
        print(
            f"  • [{r['log_tag']}] (ID: {r.get('run_id', '')}) 评分 {r['final_rank']} | 六维和 {r['stat_sum']} (低{r['min_stat']}/高{r['max_stat']}) "
            f"| 爆 {r['mgr_explosive']}次 | 阵容: {r['lineup']}"
        )
        print(
            f"    └ 军师首选一致: {r['coach_match_pct']}% (前二 {r['coach_top2_pct']}%, 均亏 -{r['avg_turn_ev_loss']} EV/训) "
            f"| 周末一致: {r['weekend_match_pct']}% | {cap_s} | {short_s}"
        )
        if "crit_maxed_total" in r:
            emt = r.get("equip_max_turns", {}) or {}
            emt_s = "/".join(f"T{tn}" for tn in sorted(emt.values())) or "无"
            print(
                f"    └ 🌈暴击器材满彩: {r['crit_maxed_total']}/{r.get('crit_equip_total', '?')} 张 "
                f"(T12前 {r.get('crit_maxed_by_t12', 0)} · T15前 {r.get('crit_maxed_by_t15', 0)}) | 满彩训次 {emt_s}"
            )

    print("\n🏆 【历史 Top 5 高分神局标杆】")
    for r in top_runs:
        b_dist = {int(k): v for k, v in r["building_dist"].items()}
        b_str = f"🎰扭蛋{b_dist.get(6,0)}/🎮街机{b_dist.get(3,0)}/💼打工{b_dist.get(8,0)}/💤休息{b_dist.get(1,0)}"
        print(
            f"  • 评分 {r['final_rank']} | 六维和 {r['stat_sum']} (最低 {r['min_stat']}) | 爆 {r['mgr_explosive']}次 "
            f"| 军师前二命中 {r['coach_top2_pct']}% (均亏 -{r['avg_turn_ev_loss']} EV) | 周末[{b_str}] ({r['log_tag']})"
        )

    def _avg(lst, key_fn):
        return sum(key_fn(x) for x in lst) / max(1, len(lst))

    if mid_runs:
        print("\n📊 【高分局 (Top 5) vs 8000~8599 瓶颈局 实证数据对比】")
        print(
            f"  1. 六维水桶底盘：Top 5 平均六维总和 {_avg(top_runs, lambda x: x['stat_sum']):.0f}（最低单项 {_avg(top_runs, lambda x: x['min_stat']):.0f}）"
            f" vs 瓶颈局 {_avg(mid_runs, lambda x: x['stat_sum']):.0f}（最低单项 {_avg(mid_runs, lambda x: x['min_stat']):.0f}）"
        )
        print(
            f"  2. 平时训练损耗：Top 5 平均每训偏离最优 -{_avg(top_runs, lambda x: x['avg_turn_ev_loss']):.1f} EV（前二命中 {_avg(top_runs, lambda x: x['coach_top2_pct']):.1f}%）"
            f" vs 瓶颈局 -{_avg(mid_runs, lambda x: x['avg_turn_ev_loss']):.1f} EV（前二命中 {_avg(mid_runs, lambda x: x['coach_top2_pct']):.1f}%）"
        )
        print(
            f"  3. 周末资源转化：Top 5 平均（🎰扭蛋 {_avg(top_runs, lambda x: int(x['building_dist'].get('6', x['building_dist'].get(6, 0)))):.1f} 次 + 🎮街机 {_avg(top_runs, lambda x: int(x['building_dist'].get('3', x['building_dist'].get(3, 0)))):.1f} 次，"
            f"💤睡觉仅 {_avg(top_runs, lambda x: int(x['building_dist'].get('1', x['building_dist'].get(1, 0)))):.1f} 次）"
            f" vs 瓶颈局（🎰扭蛋 {_avg(mid_runs, lambda x: int(x['building_dist'].get('6', x['building_dist'].get(6, 0)))):.1f} 次 + 🎮街机 {_avg(mid_runs, lambda x: int(x['building_dist'].get('3', x['building_dist'].get(3, 0)))):.1f} 次，"
            f"💤睡觉 {_avg(mid_runs, lambda x: int(x['building_dist'].get('1', x['building_dist'].get(1, 0)))):.1f} 次）"
        )

    latest = by_time[-1]
    print(f"\n🔍 【最新一局 ({latest['log_tag']} · {latest['final_rank']}分) 自动病因诊断】")
    findings = []
    if latest["capped_stats"] and latest["short_stats"]:
        findings.append(
            f"⚠️ 偏科触顶与短板并存：{'/'.join(latest['capped_stats'])} 已达 100% 属性上限发生硬截断浪费，而 {'/'.join(latest['short_stats'])} 严重偏低；中后期应少点已超 90% 上限的主训，分流 2~3 训给短板。"
        )
    elif latest["short_stats"]:
        findings.append(
            f"📉 存在明显短板属性：{'/'.join(latest['short_stats'])} 未满 1150，拉低了整体设施均衡度与基础评价。"
        )
    if latest["mgr_explosive"] < 10:
        findings.append(
            f"🎺 经理爆发次数偏少：本局仅触发 {latest['mgr_explosive']} 次经理爆发，前期需优先抓哨子合训冲满 100 熟练度解锁同框 +2~3 格。"
        )
    if latest["avg_turn_ev_loss"] >= 10.0:
        findings.append(
            f"🏋️ 平时训练存在高分差分歧：本局平均每训偏离军师首选 -{latest['avg_turn_ev_loss']:.1f} EV（可直接读 data/runs/archive/{latest.get('run_id', '')}.json 的 top_suboptimal_turns）。"
        )
    if latest["weekend_match_pct"] < 70.0:
        findings.append(
            f"🌴 周末出行路线有优化空间：周末路线一致率为 {latest['weekend_match_pct']:.1f}%，注意结合 R10集训 / R24合宿 / R36许愿 / 哨子70&100三选一前瞻控体，减少无谓寝室休息与过度打工。"
        )
    if not findings:
        findings.append("🎉 本局整体节奏极佳！水桶均衡度、经理爆发与周末转化均处于高分标杆区间。")
    for idx_f, fd in enumerate(findings, 1):
        print(f"  {idx_f}. {fd}")
    print("\n💡 提示：输入 `python3 src/analyzer.py` 秒读增量战绩库；加 `--rebuild` 可在估值公式大改后重算全部历史归档。")
    print("=" * 92)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LQSNCoach 历史战绩增量归档与复盘分析器")
    parser.add_argument("--strategy", default="training_flow", help="策略名称")
    parser.add_argument("--rebuild", action="store_true", help="强制重新扫描全部原始日志并重建 archive/")
    args = parser.parse_args()
    runs = extract_all_runs(strategy_name=args.strategy, force_rebuild=args.rebuild)
    print_learning_report(runs)
