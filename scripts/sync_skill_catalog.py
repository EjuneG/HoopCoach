#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_skill_catalog.py — 从公开配表数据源同步并构建 data/skill_catalog.json

功能说明：
1. 拉取 `https://hoopgirls.live/data/skills.json`（或读取本地 `--input` JSON 文件）；
2. 结合 `data/hoopgirls/characters.json` 关联角色专属被动（2001001 等）；
3. 解析品质（彩/金/蓝/白）、游戏原生 quality(1~4)、评分 rank、消耗 pt_cost、
   上下位覆盖链（replaces_skill_ids / replaced_by_skill_ids / net_score_delta_on_upgrade）、
   硬性职责/位置限定（role_tag / required_positions）与柔性契合偏好（preferred_roles / bonus_positions / preferred_style / preferred_attrs）；
4. 原子写入 `data/skill_catalog.json`（默认过滤掉内部测试/关卡干预/附属子 Buff，保持热更包轻巧）。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "https://hoopgirls.live/data/skills.json"
DEFAULT_CHARS_PATH = os.path.join(REPO_ROOT, "data", "hoopgirls", "characters.json")
DEFAULT_OUT_PATH = os.path.join(REPO_ROOT, "data", "skill_catalog.json")

POS_CN_TO_CODE = {
    "控卫": "PG",
    "分卫": "SG",
    "小前": "SF",
    "大前": "PF",
    "中锋": "C",
}

ATTR_NAMES = ("投射", "弹跳", "防守", "体能", "运控", "意识")


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _load_owner_map(chars_path: str) -> dict[int, tuple[int, str]]:
    """从 characters.json 提取 {skill_id: (char_id, short_name)} 映射。"""
    owners: dict[int, tuple[int, str]] = {}
    if not os.path.isfile(chars_path):
        return owners
    with open(chars_path, "r", encoding="utf-8-sig") as f:
        chars = json.load(f)
    items = chars.values() if isinstance(chars, dict) else chars
    for ch in items:
        if not isinstance(ch, dict) or ch.get("id") is None:
            continue
        cid = int(ch["id"])
        cname = str(ch.get("short_name") or ch.get("show_name") or cid)
        ult = ch.get("ultimate_skill") or {}
        if isinstance(ult, dict):
            for lv_item in ult.get("levels") or ():
                if isinstance(lv_item, dict) and lv_item.get("id"):
                    owners[int(lv_item["id"])] = (cid, cname)
            if ult.get("id"):
                owners[int(ult["id"])] = (cid, cname)
            for ext in ult.get("attached_extra_skills") or ():
                if isinstance(ext, dict):
                    if ext.get("id"):
                        owners[int(ext["id"])] = (cid, cname)
                    for lv_item in ext.get("levels") or ():
                        if isinstance(lv_item, dict) and lv_item.get("id"):
                            owners[int(lv_item["id"])] = (cid, cname)
        for ext in ch.get("extra_skill_details") or ():
            if isinstance(ext, dict):
                if ext.get("id"):
                    owners[int(ext["id"])] = (cid, cname)
                for lv_item in ext.get("levels") or ():
                    if isinstance(lv_item, dict) and lv_item.get("id"):
                        owners[int(lv_item["id"])] = (cid, cname)
    return owners


def _extract_positions_from_clause(clause: str) -> list[str]:
    found: list[str] = []
    for cn, code in POS_CN_TO_CODE.items():
        if cn in clause and code not in found:
            found.append(code)
    return found


def _infer_role_and_affinity(
    sid: int,
    name: str,
    desc: str,
    short_desc: str,
    is_action_card: bool,
) -> dict[str, Any]:
    full = f"{name} {desc} {short_desc}"

    # 1. 硬性职责天赋锁（如「受组织者天赋加成」）
    role_tag = "通用"
    for r_name in ("得分手", "防守者", "组织者", "篮板手"):
        if f"受{r_name}天赋加成" in full or f"限{r_name}" in full:
            role_tag = r_name
            break

    # 2. 硬性位置锁 vs 额外位置加成
    required_positions: list[str] = []
    bonus_positions: list[str] = []

    # 形如 "(控卫和分卫额外小幅提升)" / "（小前额外中幅提升）" 为额外加成，所有位置本身均生效
    for m in re.finditer(r"[（(]([^）)]*?)额外[^）)]*[）)]", desc):
        for code in _extract_positions_from_clause(m.group(1)):
            if code not in bonus_positions:
                bonus_positions.append(code)

    # 形如 "中锋更擅长抢篮板" 或 "我方中锋和大前 抢篮板时"（仅当不是全队光环时）
    if re.match(r"^(控卫|分卫|小前|大前|中锋)更擅长", desc.strip()):
        required_positions = _extract_positions_from_clause(desc)

    # 3. 柔性职责偏好（不判死刑空转，仅用于同品质择优加分）
    preferred_roles: list[str] = []
    if role_tag != "通用":
        preferred_roles.append(role_tag)
    else:
        if any(k in full for k in ("助攻", "传球", "接球", "空位队友", "挡拆", "传切", "突分", "快攻")):
            preferred_roles.append("组织者")
        if any(k in full for k in ("抢篮板", "进攻篮板", "防守篮板", "补篮", "补扣", "篮板")):
            preferred_roles.append("篮板手")
        if any(k in full for k in ("抢断", "盖帽", "看破", "盯防", "协防", "干扰对手", "被突破", "防守回合", "防守属性", "防守擅长", "防守精通", "防守熟练")):
            preferred_roles.append("防守者")
        if any(
            k in full
            for k in (
                "三分",
                "中投",
                "远投",
                "近投",
                "上篮",
                "扣篮",
                "勾手",
                "背打",
                "后撤",
                "横撤",
                "突破",
                "得分",
                "出手",
                "进攻速卡",
                "进攻力卡",
                "进攻技卡",
                "进攻威力",
                "选卡威力",
            )
        ):
            preferred_roles.append("得分手")

    # 4. 球风流派偏好（速度 / 力量 / 技巧）
    preferred_style = ""
    if any(k in full for k in ("速卡", "速度优势", "速度强势", "快速启动", "电光石火", "速克制")):
        preferred_style = "速度"
    elif any(k in full for k in ("力卡", "力量优势", "力量强势", "力克制")):
        preferred_style = "力量"
    elif any(k in full for k in ("技卡", "技术优势", "稳步精进", "千锤百炼", "技克制")):
        preferred_style = "技巧"

    # 5. 关联六维属性偏好
    preferred_attrs = [attr for attr in ATTR_NAMES if attr in name or attr in short_desc]

    return {
        "role_tag": role_tag,
        "preferred_roles": preferred_roles,
        "required_positions": required_positions,
        "bonus_positions": bonus_positions,
        "preferred_style": preferred_style,
        "preferred_attrs": preferred_attrs,
    }


def _map_rarity_and_quality(raw_item: dict[str, Any], sid: int, is_owner_skill: bool = False) -> tuple[str, int, bool]:
    q = int(raw_item.get("quality") or 0)
    cls = int(raw_item.get("class") or 0)
    desc = str(raw_item.get("desc") or raw_item.get("skill_desc") or "").strip()
    short_desc = str(raw_item.get("skill_desc_short") or "").strip()

    is_action_card = bool(
        200000 <= sid < 300000
        and q in (1, 2, 4)
        and desc == ""
        and short_desc == ""
    )

    if q >= 4:
        return "彩", q, is_action_card
    if q == 3 or cls in (1, 3) or is_owner_skill:
        return "金", max(q, 3), is_action_card
    if q == 2:
        return "金", 2, is_action_card
    if q == 1:
        if is_action_card:
            return "蓝", 1, True
        return "白", 1, False
    return "白", 0, False


def build_catalog_from_raw(
    raw_skills_data: dict[str, Any],
    owner_map: dict[int, tuple[int, str]],
) -> dict[str, Any]:
    smap = raw_skills_data.get("skills_by_id") or {}
    skills_out: dict[str, dict[str, Any]] = {}

    for sid_str, item in sorted(smap.items(), key=lambda kv: int(kv[0])):
        if not isinstance(item, dict):
            continue
        sid = int(item.get("skill_id") or sid_str)
        if sid < 100000:
            continue
        q_raw = int(item.get("quality") or 0)
        cls_raw = int(item.get("class") or 0)
        is_owner = sid in owner_map
        if q_raw <= 0 and cls_raw not in (1, 3) and not is_owner:
            continue
        if bool(item.get("ignore_show", False)) and q_raw <= 0 and not is_owner:
            continue

        name = str(item.get("show_name") or item.get("name") or "").lstrip("*").strip()
        if not name:
            continue
        desc = str(item.get("desc") or item.get("skill_desc") or "").strip()
        short_desc = str(item.get("skill_desc_short") or "").strip()
        rarity, quality, is_action_card = _map_rarity_and_quality(item, sid, is_owner_skill=is_owner)
        aff = _infer_role_and_affinity(sid, name, desc, short_desc, is_action_card)

        replaces_ids = [int(x) for x in (item.get("replaces_skill_ids") or ()) if int(x) > 0]
        replaced_by_ids = [int(x) for x in (item.get("replaced_by_skill_ids") or ()) if int(x) > 0]
        rank_val = int(item.get("rank") or 0)
        pt_cost = int(item.get("pt_cost") or 0)
        net_delta = int(item.get("net_score_delta_on_upgrade") or rank_val)

        entry: dict[str, Any] = {
            "skill_id": sid,
            "name": name,
            "rarity": rarity,
            "quality": quality,
            "role_tag": aff["role_tag"],
            "desc": desc or (f"获得动作卡【{name}】" if is_action_card else ""),
            "source": "hoopgirls.live:skills.json",
        }
        if short_desc:
            entry["short_desc"] = short_desc
        if is_action_card:
            entry["is_action_card"] = True
        if rank_val > 0:
            entry["rank"] = rank_val
        if pt_cost > 0:
            entry["pt_cost"] = pt_cost
        if int(item.get("level") or 0) > 0:
            entry["level"] = int(item["level"])
        if int(item.get("skill_group") or 0) > 0:
            entry["skill_group"] = int(item["skill_group"])
        if replaces_ids:
            entry["replaces_skill_ids"] = replaces_ids
            entry["net_score_delta_on_upgrade"] = net_delta
        if replaced_by_ids:
            entry["replaced_by_skill_ids"] = replaced_by_ids
        if aff["preferred_roles"]:
            entry["preferred_roles"] = aff["preferred_roles"]
        if aff["required_positions"]:
            entry["required_positions"] = aff["required_positions"]
        if aff["bonus_positions"]:
            entry["bonus_positions"] = aff["bonus_positions"]
        if aff["preferred_style"]:
            entry["preferred_style"] = aff["preferred_style"]
        if aff["preferred_attrs"]:
            entry["preferred_attrs"] = aff["preferred_attrs"]
        if sid in owner_map:
            cid, cname = owner_map[sid]
            entry["owner_char_id"] = cid
            entry["source"] = f"hoopgirls.live:skills.json+characters.json:{cid}({cname})"

        skills_out[str(sid)] = entry

    return {
        "version": 2,
        "updated_at": _now_iso(),
        "notes": (
            "《篮球少女：飞跃巅峰》全量技能真值库（同步自 hoopgirls.live/data/skills.json 并关联 characters.json）。"
            "包含普通技/动作蓝卡/高级金技/红色进化彩技/角色固有与专属被动，附带上下位覆盖链与职责位置偏好。"
        ),
        "skills": skills_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="同步并生成 data/skill_catalog.json")
    parser.add_argument("--input", default="", help="可选本地 skills.json 路径；未指定时从 hoopgirls.live 拉取")
    parser.add_argument("--url", default=DEFAULT_URL, help="远端 skills.json URL")
    parser.add_argument("--chars", default=DEFAULT_CHARS_PATH, help="characters.json 路径")
    parser.add_argument("--output", default=DEFAULT_OUT_PATH, help="输出 skill_catalog.json 路径")
    args = parser.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8-sig") as f:
            raw_data = json.load(f)
    else:
        req = urllib.request.Request(args.url, headers={"User-Agent": "Mozilla/5.0 (HoopCoach SkillSync)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_data = json.loads(resp.read().decode("utf-8"))

    owner_map = _load_owner_map(args.chars)
    catalog = build_catalog_from_raw(raw_data, owner_map)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    tmp_path = f"{args.output}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, args.output)

    skills = catalog["skills"]
    by_rarity: dict[str, int] = {}
    for v in skills.values():
        r = v.get("rarity", "?")
        by_rarity[r] = by_rarity.get(r, 0) + 1
    print(f"Synced {len(skills)} skills -> {args.output} | by rarity: {by_rarity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
