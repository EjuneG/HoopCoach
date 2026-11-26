#!/usr/bin/env python3
"""
《篮球少女：飞跃巅峰》育成选技能参谋与视觉自学习库 (src/skill_advisor.py)
- 加载 data/skill_catalog.json（已核实白技真值 + characters.json 专属传承被动技 + 实机自学习条目）
- 提取当前球员职责标签集合（得分手/防守者/组织者/篮板手/通用），裁决左中右三张卡职责契合度、已学重复与省 PT 折算
- 遇未知 skill_id 时后台抓 ~45KB 窗口小图调 gemini-api 识别三卡并原子写入 data/skill_catalog.json 后回调刷新 HUD
- 严禁将 supactor_card_id 当作器材专属金技硬绑（skill_lib_type=3 常为通用白技）
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(BASE_DIR, "data", "skill_catalog.json")
CHARS_PATH = os.path.join(BASE_DIR, "data", "hoopgirls", "characters.json")
NOTE_POOL_PATH = os.path.join(BASE_DIR, "data", "note_pool.json")
GEMINI_API_BIN = os.path.expanduser("~/.local/bin/gemini-api")
PROBE_IMAGE_PATH = "/tmp/lqsn_skill_probe.jpg"

VALID_ROLE_TAGS = ("得分手", "防守者", "组织者", "篮板手", "通用")
VALID_RARITIES = ("彩", "金", "银", "白")

RARITY_BASE_SCORE = {
    "彩": 112.0,
    "SSR": 112.0,
    "金": 94.0,
    "SR": 94.0,
    "银": 74.0,
    "白": 60.0,
    "未知": 50.0,
}

# 实战核心白技/通用技微调权重（仅对已核实技能生效）
SKILL_PRACTICAL_BONUS = {
    "速度优势": 6.0,
    "力量优势": 6.0,
    "前期加持": 2.0,
}

_CATALOG_LOCK = threading.RLock()
_CACHED_CATALOG: dict[str, Any] | None = None
_CACHED_CATALOG_MTIME: float = -1.0
_CACHED_CHARS_BY_ID: dict[int, dict[str, Any]] | None = None
_NOTE_POOL_LOCK = threading.RLock()
_CACHED_NOTE_POOL: dict[str, Any] | None = None
_CACHED_NOTE_POOL_MTIME: float = -1.0

_INFLIGHT_LOCK = threading.Lock()
_IS_LEARNING: bool = False
_RECENT_ATTEMPTS: dict[tuple[int, ...], float] = {}
_ATTEMPT_COOLDOWN_SEC = 15.0


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _load_chars_by_id() -> dict[int, dict[str, Any]]:
    global _CACHED_CHARS_BY_ID
    if _CACHED_CHARS_BY_ID is not None:
        return _CACHED_CHARS_BY_ID
    chars_by_id: dict[int, dict[str, Any]] = {}
    if os.path.exists(CHARS_PATH):
        with open(CHARS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = raw.values() if isinstance(raw, dict) else raw
        for c in items:
            if isinstance(c, dict) and c.get("id") is not None:
                try:
                    chars_by_id[int(c["id"])] = c
                except (TypeError, ValueError):
                    pass
    _CACHED_CHARS_BY_ID = chars_by_id
    return chars_by_id


def _load_default_note_pool() -> dict[str, Any]:
    global _CACHED_NOTE_POOL, _CACHED_NOTE_POOL_MTIME
    with _NOTE_POOL_LOCK:
        if os.path.exists(NOTE_POOL_PATH):
            try:
                cur_mtime = os.path.getmtime(NOTE_POOL_PATH)
            except OSError:
                cur_mtime = -1.0
            if _CACHED_NOTE_POOL is not None and cur_mtime == _CACHED_NOTE_POOL_MTIME:
                return _CACHED_NOTE_POOL
            try:
                with open(NOTE_POOL_PATH, "r", encoding="utf-8") as f:
                    _CACHED_NOTE_POOL = json.load(f)
                    _CACHED_NOTE_POOL_MTIME = cur_mtime
                    return _CACHED_NOTE_POOL
            except Exception:
                pass
        elif _CACHED_NOTE_POOL is not None and _CACHED_NOTE_POOL_MTIME == -1.0:
            return _CACHED_NOTE_POOL
        _CACHED_NOTE_POOL = {}
        _CACHED_NOTE_POOL_MTIME = -1.0
        return _CACHED_NOTE_POOL


def reload_skill_catalog() -> dict[str, Any]:
    """强制从磁盘重新加载 data/skill_catalog.json 并刷新内存缓存。"""
    global _CACHED_CATALOG, _CACHED_CATALOG_MTIME
    with _CATALOG_LOCK:
        if os.path.exists(CATALOG_PATH):
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _CACHED_CATALOG_MTIME = os.path.getmtime(CATALOG_PATH)
        else:
            data = {"version": 1, "updated_at": _now_iso(), "skills": {}}
            _CACHED_CATALOG_MTIME = -1.0
        if not isinstance(data.get("skills"), dict):
            data["skills"] = {}
        _CACHED_CATALOG = data
        return data


def _get_catalog() -> dict[str, Any]:
    with _CATALOG_LOCK:
        if _CACHED_CATALOG is not None:
            if os.path.exists(CATALOG_PATH):
                try:
                    if os.path.getmtime(CATALOG_PATH) == _CACHED_CATALOG_MTIME:
                        return _CACHED_CATALOG
                except OSError:
                    return _CACHED_CATALOG
            else:
                return _CACHED_CATALOG
        return reload_skill_catalog()


def get_skill_entry(skill_id: int) -> dict[str, Any] | None:
    """查询已知技能条目，未收录返回 None。"""
    try:
        sid_key = str(int(skill_id))
    except (TypeError, ValueError):
        return None
    catalog = _get_catalog()
    entry = catalog.get("skills", {}).get(sid_key)
    return dict(entry) if isinstance(entry, dict) else None


def get_char_role_tags(char_id: int) -> set[str]:
    """提取球员生效的职责流派标签集合（固定包含 '通用'）。"""
    tags: set[str] = {"通用"}
    try:
        cid = int(char_id)
    except (TypeError, ValueError):
        return tags
    char_obj = _load_chars_by_id().get(cid)
    if not char_obj:
        return tags
    for role in char_obj.get("role_type") or ():
        r = str(role).strip()
        if r and r != "陪练NPC":
            tags.add(r)
    return tags


def _normalize_learned_set(learned_skills: Any) -> set[int]:
    out: set[int] = set()
    if not learned_skills:
        return out
    items = learned_skills.keys() if isinstance(learned_skills, dict) else learned_skills
    for item in items:
        try:
            sid = int(item.get("skill_id") if isinstance(item, dict) else item)
            if sid > 0:
                out.add(sid)
        except (TypeError, ValueError):
            continue
    return out


def _lookup_note_skill_star(note_id: int, note_pool: list | dict | None) -> int:
    """查询 note_id 对应的笔记技能省 PT 星级（未命中或无星级信息时返回 1）。"""
    if note_id <= 0:
        return 0
    pool = _load_default_note_pool() if note_pool is None else note_pool
    nid_str = str(note_id)
    note_obj = None

    if isinstance(pool, dict):
        if nid_str in pool and isinstance(pool[nid_str], dict):
            note_obj = pool[nid_str]
        else:
            for sec in ("own_notes", "borrowed_notes", "borrow_notes"):
                sec_map = pool.get(sec)
                if isinstance(sec_map, dict) and nid_str in sec_map:
                    note_obj = sec_map[nid_str]
                    break
    elif isinstance(pool, list):
        for item in pool:
            if isinstance(item, dict) and str(item.get("note_id", "")) == nid_str:
                note_obj = item
                break

    if not isinstance(note_obj, dict):
        return 1

    for e in note_obj.get("special_entries") or ():
        if isinstance(e, dict) and int(e.get("type") or 0) == 6:
            return max(1, int(e.get("star") or 1))
    for e in note_obj.get("entries") or ():
        if isinstance(e, dict) and int(e.get("entry_type") or 0) == 6:
            return max(1, int(e.get("entry_star_id") or e.get("star") or 1))
    return 1


def _split_role_tags(raw_tag: str) -> set[str]:
    parts = [p.strip() for p in re.split(r"[/、,，\s]+", str(raw_tag or "")) if p.strip()]
    return set(parts) if parts else {"通用"}


def _is_role_matched(role_tag: str, char_roles: set[str]) -> bool:
    tag_parts = _split_role_tags(role_tag)
    if "通用" in tag_parts:
        return True
    return bool(tag_parts & (char_roles - {"通用"}))


def _score_single_card(
    slot_idx: int,
    card: dict[str, Any],
    char_roles: set[str],
    learned_set: set[int],
    note_pool: list | dict | None,
) -> dict[str, Any]:
    skill_id = int(card.get("skill_id") or 0)
    note_id = int(card.get("note_id") or 0)
    skill_lib_type = int(card.get("skill_lib_type") or 0)

    entry = get_skill_entry(skill_id)
    is_unknown = entry is None
    if entry is not None:
        skill_name = str(entry.get("name") or f"技能#{skill_id}")
        role_tag = str(entry.get("role_tag") or "通用")
        rarity = str(entry.get("rarity") or "白")
        desc = str(entry.get("desc") or "")
    else:
        skill_name = f"技能#{skill_id}" if skill_id > 0 else "未知技能"
        role_tag = "未知"
        rarity = "未知"
        desc = ""

    is_learned = skill_id > 0 and skill_id in learned_set
    has_note_discount = note_id > 0
    note_star = _lookup_note_skill_star(note_id, note_pool) if has_note_discount else 0
    role_match = True if is_unknown else _is_role_matched(role_tag, char_roles)

    base = RARITY_BASE_SCORE.get(rarity, 55.0)
    note_bonus = (6.0 + note_star * 2.0) if has_note_discount else 0.0

    if is_learned:
        priority_tier = 0
        score = -200.0 + base * 0.1
        reason = "已学习该技能，重复选择收益极低"
    elif not role_match:
        priority_tier = 1
        score = -60.0 + base * 0.1 + note_bonus * 0.2
        reason = f"技能限定[{role_tag}]，当前球员职责不符将空转"
    elif is_unknown:
        priority_tier = 2
        lib_bonus = 6.0 if skill_lib_type == 1 else 0.0
        score = base + lib_bonus + note_bonus
        parts = ["未收录待识别"]
        if skill_lib_type == 1:
            parts.append("固有/传承池")
        if has_note_discount:
            parts.append(f"笔记★{note_star}省PT")
        reason = " · ".join(parts)
    else:
        priority_tier = 3
        is_specific_match = role_tag != "通用" and _is_role_matched(role_tag, char_roles)
        role_bonus = 18.0 if is_specific_match else 8.0
        practical_bonus = SKILL_PRACTICAL_BONUS.get(skill_name, 0.0)
        score = base + role_bonus + practical_bonus + note_bonus
        parts = [f"契合[{role_tag}]" if is_specific_match else "通用泛用"]
        if rarity in ("彩", "金"):
            parts.append(f"{rarity}级核心")
        elif practical_bonus > 0:
            parts.append("实战高优白技")
        if has_note_discount:
            parts.append(f"笔记★{note_star}省PT")
        reason = " · ".join(parts)

    return {
        "slot_idx": slot_idx,
        "skill_id": skill_id,
        "skill_name": skill_name,
        "role_tag": role_tag,
        "rarity": rarity,
        "desc": desc,
        "score": round(score, 2),
        "rank": 0,
        "badge_text": "",
        "style_key": "second",
        "role_match": role_match,
        "is_learned": is_learned,
        "has_note_discount": has_note_discount,
        "note_star": note_star,
        "is_unknown": is_unknown,
        "reason": reason,
        "_priority_tier": priority_tier,
    }


_UI_RARITY_ORDER = {"彩": 4, "SSR": 4, "金": 3, "SR": 3, "银": 2, "白": 1, "未知": 0}


def sort_skill_choices_for_ui(skill_list: list[dict]) -> list[dict]:
    """
    按实机三选一界面真实展示顺序重排技能卡（2026-11-27 R36/R43 实机截图验证）：
    游戏 UI 会将高稀有度卡（彩 > 金 > 银 > 白）排在左侧槽位，同稀有度保持服务器下发相对顺序。
    """
    indexed = []
    for raw_idx, card in enumerate(skill_list or ()):
        c = card or {}
        sid = int(c.get("skill_id") or 0)
        entry = get_skill_entry(sid) if sid > 0 else None
        rar = str((entry or {}).get("rarity") or "未知")
        indexed.append((-_UI_RARITY_ORDER.get(rar, 0), raw_idx, c))
    indexed.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in indexed]


def evaluate_skill_choices(
    char_id: int,
    skill_list: list[dict],
    learned_skills: set[int] | list[int] = (),
    note_pool: list | dict | None = None,
) -> list[dict]:
    """
    评估选技能左中右候选卡，按实机 UI 展示顺序 (slot_idx=0..N-1) 返回完整评分、排名、短角标与样式 key。
    """
    if not skill_list:
        return []
    char_roles = get_char_role_tags(char_id)
    learned_set = _normalize_learned_set(learned_skills)
    ui_cards = sort_skill_choices_for_ui(skill_list)

    results = [
        _score_single_card(idx, card or {}, char_roles, learned_set, note_pool)
        for idx, card in enumerate(ui_cards)
    ]

    ordered = sorted(
        results,
        key=lambda r: (-r["_priority_tier"], -r["score"], r["slot_idx"]),
    )
    for rank_idx, item in enumerate(ordered, start=1):
        item["rank"] = rank_idx
        pt_suffix = "·省PT" if item["has_note_discount"] else ""
        if item["is_learned"]:
            item["style_key"] = "dup"
            item["badge_text"] = "♻️ 已学重复"
        elif not item["role_match"]:
            item["style_key"] = "warn"
            item["badge_text"] = "⚠️ 职责不符空转"
        elif item["is_unknown"]:
            item["style_key"] = "unknown"
            item["badge_text"] = ("🔍 识别中" if rank_idx == 1 else "🔍 待识别") + pt_suffix
        elif rank_idx == 1:
            item["style_key"] = "best"
            item["badge_text"] = "👑 契合首选" + pt_suffix
        elif rank_idx == 2:
            item["style_key"] = "second"
            item["badge_text"] = "🥈 备选" + pt_suffix
        else:
            item["style_key"] = "second"
            item["badge_text"] = "🥉 末选" + pt_suffix

    for item in results:
        item.pop("_priority_tier", None)
    return results


def has_unknown_skills(skill_list: list[dict]) -> bool:
    """判断候选列表中是否存在未收录于 data/skill_catalog.json 的 skill_id。"""
    for card in skill_list or ():
        try:
            sid = int((card or {}).get("skill_id") or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0 and get_skill_entry(sid) is None:
            return True
    return False


def _normalize_role_tag(raw: str) -> str:
    s = str(raw or "").strip()
    for tag in VALID_ROLE_TAGS:
        if tag in s:
            return tag
    return "通用"


def _normalize_rarity(raw: str) -> str:
    s = str(raw or "").strip().upper()
    if "彩" in s or "SSR" in s or "UR" in s:
        return "彩"
    if "金" in s or "SR" in s:
        return "金"
    if "银" in s or "R" == s:
        return "银"
    return "白"


def _persist_learned_skills(new_entries: dict[str, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """原子合并写入 data/skill_catalog.json 并刷新内存缓存。"""
    global _CACHED_CATALOG, _CACHED_CATALOG_MTIME
    if not new_entries:
        return {}
    written: dict[int, dict[str, Any]] = {}
    with _CATALOG_LOCK:
        catalog = reload_skill_catalog()
        skills_map = catalog.setdefault("skills", {})
        for sid_str, info in new_entries.items():
            existing = skills_map.get(sid_str)
            # 不覆盖已人工核实或静态库真值
            if isinstance(existing, dict) and existing.get("source", "").startswith(
                ("verified_", "characters.json")
            ):
                continue
            skills_map[sid_str] = info
            written[int(sid_str)] = dict(info)

        if not written:
            return {}

        seeds = catalog.get("observed_log_seeds")
        if isinstance(seeds, dict) and isinstance(seeds.get("unknown_210xxx_ids"), list):
            seeds["unknown_210xxx_ids"] = [
                x for x in seeds["unknown_210xxx_ids"] if str(x) not in skills_map
            ]

        catalog["updated_at"] = _now_iso()
        catalog["skills"] = dict(sorted(skills_map.items(), key=lambda kv: int(kv[0])))

        os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
        tmp_path = f"{CATALOG_PATH}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CATALOG_PATH)
        _CACHED_CATALOG = catalog
        _CACHED_CATALOG_MTIME = os.path.getmtime(CATALOG_PATH)
    return written


def _resolve_capture_display(display: str | None = None) -> str:
    """解析截图使用的 DISPLAY：显式参数 > live_coach.query_game_window_display_and_xid() > 环境变量 DISPLAY。"""
    if display:
        return str(display)
    try:
        import live_coach as _lc

        disp, _ = _lc.query_game_window_display_and_xid()
        if disp:
            return str(disp)
    except Exception:
        pass
    return str(os.environ.get("DISPLAY") or "").strip()


def _probe_and_learn_sync(
    wid: str,
    skill_snapshot: list[dict[str, Any]],
    unknown_key: tuple[int, ...],
    on_learned_cb: Callable[[dict[int, dict[str, Any]]], None] | None,
    display: str | None = None,
) -> None:
    global _IS_LEARNING
    try:
        # 稍等弹窗淡入动画稳定
        time.sleep(0.25)
        env = os.environ.copy()
        resolved_display = _resolve_capture_display(display)
        if resolved_display:
            env["DISPLAY"] = resolved_display
        elif not env.get("DISPLAY"):
            env.pop("DISPLAY", None)

        subprocess.run(
            ["import", "-window", wid, "-resize", "960x540", "-quality", "75", PROBE_IMAGE_PATH],
            env=env,
            capture_output=True,
            timeout=8.0,
            check=True,
        )
        if not os.path.exists(PROBE_IMAGE_PATH) or os.path.getsize(PROBE_IMAGE_PATH) < 1024:
            return

        prompt = (
            "你是《篮球少女：飞跃巅峰》「选择技能」界面卡片识别器。\n"
            "请读取截图中自左向右排列的 3 张技能卡（slot=0 左、slot=1 中、slot=2 右），提取每张卡的信息：\n"
            "- slot: 0 / 1 / 2\n"
            "- name: 技能名称（纯中文名，不要包含职责标签或后缀）\n"
            "- role_tag: 卡片上标示的生效职责，只能从 [\"得分手\", \"防守者\", \"组织者\", \"篮板手\", \"通用\"] 中选一（若无专属职责限定则填 \"通用\"）\n"
            "- rarity: 卡片品质底色，只能从 [\"彩\", \"金\", \"银\", \"白\"] 中选一\n"
            "- desc: 技能效果描述全文\n"
            "仅输出严格合法的 JSON（不要附加 Markdown 或解释），格式：\n"
            "{\"cards\": [{\"slot\": 0, \"name\": \"...\", \"role_tag\": \"通用\", \"rarity\": \"白\", \"desc\": \"...\"}]}"
        )
        proc = subprocess.run(
            [GEMINI_API_BIN, "analyze", "-i", PROBE_IMAGE_PATH, prompt],
            capture_output=True,
            text=True,
            timeout=25.0,
        )
        if proc.returncode != 0 or not proc.stdout:
            return

        m = re.search(r"\{[\s\S]*\}", proc.stdout)
        if not m:
            return
        parsed = json.loads(m.group(0))
        cards = parsed.get("cards") or []
        if not isinstance(cards, list):
            return

        new_entries: dict[str, dict[str, Any]] = {}
        for item in cards:
            if not isinstance(item, dict):
                continue
            try:
                slot = int(item.get("slot"))
            except (TypeError, ValueError):
                continue
            if not (0 <= slot < len(skill_snapshot)):
                continue
            sid = int((skill_snapshot[slot] or {}).get("skill_id") or 0)
            name = str(item.get("name") or "").strip()
            if sid <= 0 or not name or get_skill_entry(sid) is not None:
                continue
            new_entries[str(sid)] = {
                "skill_id": sid,
                "name": name,
                "role_tag": _normalize_role_tag(item.get("role_tag", "通用")),
                "rarity": _normalize_rarity(item.get("rarity", "白")),
                "desc": str(item.get("desc") or "").strip(),
                "source": "gemini_vision",
                "learned_at": _now_iso(),
            }

        learned_map = _persist_learned_skills(new_entries)
        if learned_map and on_learned_cb is not None:
            try:
                on_learned_cb(learned_map)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        with _INFLIGHT_LOCK:
            _IS_LEARNING = False
            _RECENT_ATTEMPTS[unknown_key] = time.monotonic()


def learn_unknown_skills_from_window_async(
    wid: int | str,
    skill_list: list[dict],
    on_learned_cb: Callable[[dict[int, dict[str, Any]]], None] | None = None,
    display: str | None = None,
) -> bool:
    """
    当 skill_list 存在未收录 skill_id 时，后台防重入抓取窗口小图并调用 gemini-api 识别入库。
    成功启动后台识别任务返回 True；无需识别、窗口无效或正在识别中返回 False。
    """
    global _IS_LEARNING
    wid_str = str(wid).strip() if wid is not None else ""
    if not wid_str or wid_str == "0" or not skill_list:
        return False

    unknown_ids: list[int] = []
    for card in skill_list:
        try:
            sid = int((card or {}).get("skill_id") or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0 and get_skill_entry(sid) is None:
            unknown_ids.append(sid)

    if not unknown_ids:
        return False

    unknown_key = tuple(sorted(set(unknown_ids)))
    now = time.monotonic()
    with _INFLIGHT_LOCK:
        if _IS_LEARNING:
            return False
        last_try = _RECENT_ATTEMPTS.get(unknown_key, 0.0)
        if last_try > 0.0 and (now - last_try) < _ATTEMPT_COOLDOWN_SEC:
            return False
        _IS_LEARNING = True

    snapshot = [dict(c) if isinstance(c, dict) else {} for c in skill_list]
    t = threading.Thread(
        target=_probe_and_learn_sync,
        args=(wid_str, snapshot, unknown_key, on_learned_cb, display),
        daemon=True,
    )
    t.start()
    return True
