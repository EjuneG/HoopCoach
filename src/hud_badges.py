#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hud_badges.py — 跨平台共享原生贴身角标规格引擎（零 GTK / 零 Tkinter / 零 Win32 依赖）

职责：
1. 统一定义风格 B「奶白斜切章」配色表 (ACCENT_PALETTES) 与默认归一化 UI 锚点表 (DEFAULT_HUD_ANCHORS)；
2. 提供 16:9 有效内容盒换算 compute_game_content_box(rect)：
   当玩家使用 21:9 带鱼屏（左右黑边）或 4:3 / 16:10（上下黑边）时，自动算出居中的 16:9 有效画面区域，
   保证所有归一化 (rx, ry) 坐标在任意分辨率与屏幕比例下 100% 精准贴合游戏按钮；
3. 提供纯函数 build_badges_for_mode(...) 与 compute_badge_position(...)，
   同时供 Linux GTK FloatingHUD 与 Windows 原生分层窗 WindowsFloatingHUD 共用。
"""

from __future__ import annotations

import html
import re
from typing import Callable, Optional


# style_key: (grad_top_rgb, grad_bot_rgb, border_rgba, default_left_fg, default_right_fg)
ACCENT_PALETTES = {
    "best": (
        (255 / 255.0, 198 / 255.0, 38 / 255.0),
        (255 / 255.0, 168 / 255.0, 24 / 255.0),
        (1.0, 1.0, 1.0, 0.97),
        "#3B2500",
        "#1E3246",
    ),
    "second": (
        (66 / 255.0, 180 / 255.0, 248 / 255.0),
        (42 / 255.0, 152 / 255.0, 235 / 255.0),
        (186 / 255.0, 218 / 255.0, 242 / 255.0, 0.96),
        "#FFFFFF",
        "#334E68",
    ),
    "shop": (
        (42 / 255.0, 198 / 255.0, 126 / 255.0),
        (24 / 255.0, 168 / 255.0, 102 / 255.0),
        (167 / 255.0, 243 / 255.0, 208 / 255.0, 0.96),
        "#FFFFFF",
        "#1E3246",
    ),
    "warn": (
        (251 / 255.0, 113 / 255.0, 133 / 255.0),
        (225 / 255.0, 29 / 255.0, 72 / 255.0),
        (254 / 255.0, 205 / 255.0, 211 / 255.0, 0.96),
        "#FFFFFF",
        "#881337",
    ),
    "dup": (
        (148 / 255.0, 163 / 255.0, 184 / 255.0),
        (100 / 255.0, 116 / 255.0, 139 / 255.0),
        (203 / 255.0, 213 / 255.0, 225 / 255.0, 0.92),
        "#FFFFFF",
        "#64748B",
    ),
    "unknown": (
        (66 / 255.0, 180 / 255.0, 248 / 255.0),
        (42 / 255.0, 152 / 255.0, 235 / 255.0),
        (186 / 255.0, 218 / 255.0, 242 / 255.0, 0.94),
        "#FFFFFF",
        "#334E68",
    ),
    "muted": (
        (148 / 255.0, 163 / 255.0, 184 / 255.0),
        (115 / 255.0, 130 / 255.0, 150 / 255.0),
        (203 / 255.0, 213 / 255.0, 225 / 255.0, 0.90),
        "#FFFFFF",
        "#64748B",
    ),
}


DEFAULT_HUD_ANCHORS = {
    "main_screen": {
        "train_entry": {"rx": 0.840, "ry": 0.868, "top_edge_ry": 0.790},
        "rest_entry": {"rx": 0.880, "ry": 0.648, "top_edge_ry": 0.614},
        "shop_entry": {"rx": 0.920, "ry": 0.165, "badge_rx": 0.862, "badge_ry": 0.215},
    },
    "train_screen": {
        "buttons": {
            "1": {"rx": 0.840, "ry": 0.220},
            "2": {"rx": 0.850, "ry": 0.342},
            "3": {"rx": 0.850, "ry": 0.464},
            "4": {"rx": 0.850, "ry": 0.586},
            "5": {"rx": 0.850, "ry": 0.708},
            "6": {"rx": 0.832, "ry": 0.832},
        },
        "badge_right_rx": 0.742,
    },
    "story_screen": {
        "two_options": {
            "1": {"rx": 0.354, "ry": 0.870},
            "2": {"rx": 0.646, "ry": 0.870},
        },
        "three_options": {
            "1": {"rx": 0.220, "ry": 0.870},
            "2": {"rx": 0.500, "ry": 0.870},
            "3": {"rx": 0.780, "ry": 0.870},
        },
    },
    "weekend_map": {
        "entry_btn": {"rx": 0.837, "ry": 0.856, "top_edge_ry": 0.790},
        "restaurant_work": {"rx": 0.178, "ry": 0.595},
        "arcade_game": {"rx": 0.465, "ry": 0.360},
        "street_gashapon": {"rx": 0.560, "ry": 0.785},
        "park_outing": {"rx": 0.820, "ry": 0.500},
    },
    "gashapon_screen": {
        "options": {
            "1": {"rx": 0.265, "ry": 0.500, "top_edge_ry": 0.270},
            "2": {"rx": 0.500, "ry": 0.500, "top_edge_ry": 0.270},
            "3": {"rx": 0.738, "ry": 0.500, "top_edge_ry": 0.270},
        }
    },
    "minigame_screen": {
        "options": {
            "1": {"rx": 0.272, "ry": 0.500, "top_edge_ry": 0.270},
            "2": {"rx": 0.500, "ry": 0.500, "top_edge_ry": 0.270},
            "3": {"rx": 0.728, "ry": 0.500, "top_edge_ry": 0.270},
        }
    },
    "park_screen": {
        "walk": {"rx": 0.295, "ry": 0.452},
        "boat": {"rx": 0.501, "ry": 0.451},
        "picnic": {"rx": 0.705, "ry": 0.452},
    },
    "restaurant_screen": {
        "light": {"rx": 0.365, "ry": 0.452},
        "heavy": {"rx": 0.635, "ry": 0.452},
    },
    "skill_choice_screen": {
        "options": {
            "1": {"rx": 0.207, "ry": 0.466},
            "2": {"rx": 0.501, "ry": 0.473},
            "3": {"rx": 0.794, "ry": 0.473},
        }
    },
    "top_bar_slot": {"rx": 0.665, "ry": 0.042},
    "bottom_trough": {"rx": 0.530, "ry": 0.915},
}

# 向后兼容别名
DEFAULT_DIEGETIC_ANCHORS = DEFAULT_HUD_ANCHORS

DEFAULT_STAT_NAMES = {
    101: "投射",
    102: "弹跳",
    103: "防守",
    104: "体能",
    105: "运控",
    106: "意识",
}


def compute_game_content_box(
    rect: Optional[tuple[int, int, int, int]],
    target_aspect: float = 16.0 / 9.0,
    tolerance: float = 0.035,
) -> Optional[tuple[int, int, int, int]]:
    """
    将游戏客户区矩形 (gx, gy, gw, gh) 换算为居中的 16:9 有效游戏画面盒 (cx, cy, cw, ch)：
    - 16:9 常规比例（误差 <= tolerance）：直接原样返回；
    - 21:9 / 32:9 超宽带鱼屏（左右黑边）：保留全高 gh，居中截取宽 cw = round(gh * 16/9)；
    - 4:3 / 16:10 / 竖屏（上下黑边）：保留全宽 gw，居中截取高 ch = round(gw / (16/9))。
    """
    if not rect or len(rect) != 4:
        return None
    gx, gy, gw, gh = int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
    if gw <= 0 or gh <= 0:
        return (gx, gy, gw, gh)

    cur_aspect = gw / float(gh)
    rel_diff = abs(cur_aspect - target_aspect) / target_aspect
    if rel_diff <= tolerance:
        return (gx, gy, gw, gh)

    if cur_aspect > target_aspect:
        # 偏宽（例如 21:9 带鱼屏）：左右留黑边
        cw = max(1, int(round(gh * target_aspect)))
        cx = gx + (gw - cw) // 2
        return (cx, gy, cw, gh)

    # 偏窄（例如 4:3 或 16:10）：上下留黑边
    ch = max(1, int(round(gw / target_aspect)))
    cy = gy + (gh - ch) // 2
    return (gx, cy, gw, ch)


def compute_badge_position(
    content_rect: tuple[int, int, int, int],
    *,
    pw: int,
    ph: int,
    rx: float = 0.5,
    ry: float = 0.5,
    anchor: str = "right",
    dx_px: int = 0,
    dy_px: int = 0,
    left_of_box: Optional[tuple[int, int, int, int]] = None,
    min_top_y: int = 0,
) -> tuple[int, int, int, int]:
    """计算单枚角标的屏幕像素矩形 (tx, ty, pw, ph)，自动防重叠与夹紧在游戏内容盒内。"""
    gx, gy, gw, gh = content_rect
    if left_of_box is not None:
        bx, by, _, bh = left_of_box
        tx = bx - pw - 6
        ty = by + (bh - ph) // 2
    else:
        ax = int(gx + gw * rx) + int(dx_px)
        ay = int(gy + gh * ry) + int(dy_px)
        if anchor == "bottom_center":
            tx = ax - pw // 2
            ty = ay - ph + 2
        elif anchor == "left":
            tx = ax
            ty = ay - ph // 2
        else:  # "right": 右边缘贴紧目标点左侧
            tx = ax - pw
            ty = ay - ph // 2

    tx = max(gx + 6, min(gx + gw - pw - 6, tx))
    ty = max(max(min_top_y, gy + 4), min(gy + gh - ph - 4, ty))
    return (int(tx), int(ty), int(pw), int(ph))


def strip_markup_to_clean_text(raw_markup: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw_markup or "")
    txt = html.unescape(txt)
    for ic in ("🏀", "👑", "🥈", "🥉", "🛒", "🎁", "⚡", "💰", "🎺", "🌴", "📖", "🎯"):
        txt = txt.replace(ic, "")
    return re.sub(r"\s+", " ", txt).strip(" ·|")


def short_cmd_label(name: str) -> str:
    """从 '2号·弹跳' 提炼短名 '弹跳' 用于近分并排副章。"""
    if "·" in (name or ""):
        return name.split("·", 1)[1].strip()
    return (name or "").strip()


def clean_train_highlight_tag(tags: list) -> str:
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


def format_train_subject_markup(
    item: dict,
    state: Optional[dict] = None,
    *,
    stat_names: Optional[dict] = None,
    style_key: str = "best",
    include_tag: bool = True,
    prefix: str = "",
    suffix_warn: str = "",
) -> str:
    """格式化训练按钮右侧说明：直接显示所练单项主属性，出现特化/必爆/满彩等关键节点时附加简短后缀。"""
    idx = int((item or {}).get("idx", 0) or 0)
    if idx == 0:
        return html.escape(f"{prefix}休息回体")
    props = (state or {}).get("props") or {}
    prop_max = (state or {}).get("prop_max") or {}
    snames = stat_names or DEFAULT_STAT_NAMES

    main_id = 100 + idx
    main_name = snames.get(main_id, short_cmd_label(item.get("name", "")))
    if prop_max and (int(prop_max.get(main_id, 9999)) - int(props.get(main_id, 0))) <= 0:
        main_part = f"{html.escape(main_name)}(满)"
    else:
        main_part = html.escape(main_name)

    pref_part = html.escape(prefix) if prefix else ""
    base_mk = f"{pref_part}{main_part}"

    if suffix_warn:
        return f"{base_mk} · {html.escape(suffix_warn)}"

    if include_tag:
        key_tag = clean_train_highlight_tag((item or {}).get("hud_tags") or [])
        if key_tag:
            tag_fg = "#B45309" if style_key == "best" else "#0369A1"
            return f"{base_mk}<span size='88%' foreground='{tag_fg}'> · {html.escape(key_tag)}</span>"
    return base_mk


def split_legacy_markup(markup: str, style_key: str) -> tuple[str, str]:
    """把旧式单段 markup 拆分为 (left_markup, right_markup) 风格 B 双区文本。"""
    plain = re.sub(r"<[^>]+>", "", markup or "").strip()
    plain = html.unescape(plain)
    for ic in ("👑", "🥈", "🥉", "🛒", "🎁", "⚠️", "💰", "🎯", "📖"):
        plain = plain.replace(ic, "").strip()
    pal = ACCENT_PALETTES.get(style_key, ACCENT_PALETTES["second"])
    l_fg, r_fg = pal[3], pal[4]
    parts = [p.strip() for p in re.split(r"\s{2,}|\s*·\s*", plain, maxsplit=1) if p.strip()]
    if len(parts) == 2:
        lp, rp = parts[0], parts[1]
    else:
        m_ev = re.search(r"([+-]?\d+\s*EV|EV\s*\d+)", plain)
        if m_ev:
            ev_tok = m_ev.group(1).strip()
            rest = (plain[: m_ev.start()] + " " + plain[m_ev.end() :]).strip(" ·-")
            lp, rp = (f"★ {ev_tok}" if style_key == "best" else ev_tok), rest
        else:
            lp, rp = plain, ""
    l_mk = f"<span foreground='{l_fg}'>{html.escape(lp)}</span>"
    r_mk = f"<span foreground='{r_fg}'>{html.escape(rp)}</span>" if rp else ""
    return l_mk, r_mk


def resolve_badge_markups(
    *,
    left_markup: str = "",
    right_markup: str = "",
    markup: str = "",
    style_key: str = "best",
) -> tuple[str, str]:
    """统一将 spec 中的 left/right/markup 标准化为带默认前景色的 (lm, rm)。"""
    pal = ACCENT_PALETTES.get(style_key, ACCENT_PALETTES["second"])
    l_fg, r_fg = pal[3], pal[4]
    if left_markup or right_markup:
        lm = (
            f"<span foreground='{l_fg}'>{left_markup}</span>"
            if "<span" in left_markup
            else f"<span foreground='{l_fg}'>{html.escape(left_markup)}</span>"
        )
        rm = (
            (
                f"<span foreground='{r_fg}'>{right_markup}</span>"
                if "<span" in right_markup
                else f"<span foreground='{r_fg}'>{html.escape(right_markup)}</span>"
            )
            if right_markup
            else ""
        )
        return lm, rm
    return split_legacy_markup(markup, style_key)


def _is_rec_enabled(hud_config: Optional[dict], key: str) -> bool:
    if not isinstance(hud_config, dict):
        return True
    recs = hud_config.get("recommendations", hud_config)
    if not isinstance(recs, dict):
        return True
    return bool(recs.get(key, True))


def build_weekday_trough_line(
    state: dict,
    scored: list,
    *,
    include_shop: bool = True,
    format_weekday_hud_tips_fn: Optional[Callable] = None,
    get_whistle_step_fn: Optional[Callable] = None,
    check_lineup_and_equip_synergy_fn: Optional[Callable] = None,
) -> str:
    """构建顶栏第 4 原生槽精简情报（不重复抄写顶栏已有的体力与金币，仅展示哨子熟练度与关键防忘提醒）。"""
    if format_weekday_hud_tips_fn is None or get_whistle_step_fn is None:
        try:
            import live_coach as _lc

            if format_weekday_hud_tips_fn is None:
                format_weekday_hud_tips_fn = _lc.format_weekday_hud_tips
            if get_whistle_step_fn is None:
                get_whistle_step_fn = _lc.get_whistle_step
        except Exception:
            pass
    if check_lineup_and_equip_synergy_fn is None:
        try:
            from equipment_advisor import check_lineup_and_equip_synergy as _chk_syn

            check_lineup_and_equip_synergy_fn = _chk_syn
        except Exception:
            pass

    raw_tips = format_weekday_hud_tips_fn(state, scored, include_shop=include_shop) if format_weekday_hud_tips_fn else []
    tag = f"{int(state.get('round', 1)):02d}训"

    if int(state.get("round", 0)) == 1 and state.get("main_char_id") and check_lineup_and_equip_synergy_fn:
        try:
            cfg_ids = sorted(set((state.get("supactor_card_to_cfg") or {}).values()))
            syn_tips = check_lineup_and_equip_synergy_fn(
                int(state["main_char_id"]),
                state.get("teammate_char_ids", []),
                cfg_ids,
            )
            if syn_tips:
                clean_syn = strip_markup_to_clean_text(syn_tips[0])[:38]
                return f"{tag}｜<span foreground='#C2410C'>{html.escape(clean_syn)}</span>"
        except Exception:
            pass

    if raw_tips:
        clean_t = strip_markup_to_clean_text(raw_tips[0])[:38]
        color = "#C2410C" if any(k in clean_t for k in ("必买", "免费", "哨", "集训", "海滩", "许愿")) else "#253B52"
        return f"{tag}｜<span foreground='{color}'>{html.escape(clean_t)}</span>"

    if get_whistle_step_fn:
        _, w3_badge = get_whistle_step_fn(state)
        w_clean = strip_markup_to_clean_text(w3_badge)
        w_clean = re.sub(r"^\d星哨\s*熟?", "哨子 ", w_clean)
        w_clean = w_clean.replace("/次", "")
        w_clean = re.sub(r"\(([^)]+)\)", r" · \1", w_clean)
        w_esc = html.escape(w_clean)
        w_esc = re.sub(r"(\d+/\d+)", r"<span foreground='#1E88E5'>\1</span>", w_esc)
        w_esc = w_esc.replace(" · ", "<span foreground='#607D94'> · </span>")
        return f"{tag}｜<span foreground='#253B52'>{w_esc}</span>"

    return f"{tag}｜<span foreground='#253B52'>实时参谋就绪</span>"


def build_badges_for_mode(
    mode: str,
    state: dict,
    *,
    hud_config: Optional[dict] = None,
    anchors: Optional[dict] = None,
    strategy: Optional[dict] = None,
    last_turn_scored: Optional[list] = None,
    last_wk_adv: Optional[dict] = None,
    last_story_adv: Optional[dict] = None,
    last_skill_evals: Optional[list] = None,
    stat_names: Optional[dict] = None,
    store_item_catalog: Optional[dict] = None,
    extract_weekday_shop_badge_fn: Optional[Callable] = None,
    format_weekday_hud_tips_fn: Optional[Callable] = None,
    format_weekday_shop_tip_fn: Optional[Callable] = None,
    get_whistle_step_fn: Optional[Callable] = None,
    check_lineup_and_equip_synergy_fn: Optional[Callable] = None,
    get_char_role_tags_fn: Optional[Callable] = None,
) -> tuple[list[dict], str]:
    """
    纯函数：根据当前视图模式与 hud_config 开关返回风格 B (badge_specs, trough_markup_override)。
    """
    if (
        stat_names is None
        or store_item_catalog is None
        or extract_weekday_shop_badge_fn is None
        or format_weekday_hud_tips_fn is None
        or format_weekday_shop_tip_fn is None
        or get_whistle_step_fn is None
    ):
        try:
            import live_coach as _lc

            if stat_names is None:
                stat_names = _lc.STAT_NAMES
            if store_item_catalog is None:
                store_item_catalog = _lc.STORE_ITEM_CATALOG
            if extract_weekday_shop_badge_fn is None:
                extract_weekday_shop_badge_fn = _lc.extract_weekday_shop_badge
            if format_weekday_hud_tips_fn is None:
                format_weekday_hud_tips_fn = _lc.format_weekday_hud_tips
            if format_weekday_shop_tip_fn is None:
                format_weekday_shop_tip_fn = _lc.format_weekday_shop_tip
            if get_whistle_step_fn is None:
                get_whistle_step_fn = _lc.get_whistle_step
        except Exception:
            pass
    if get_char_role_tags_fn is None:
        try:
            from skill_advisor import get_char_role_tags as _get_roles

            get_char_role_tags_fn = _get_roles
        except Exception:
            get_char_role_tags_fn = lambda _cid: {"通用"}

    snames = stat_names or DEFAULT_STAT_NAMES
    catalog = store_item_catalog or {}

    specs: list[dict] = []
    trough_line = ""
    anc = anchors or DEFAULT_HUD_ANCHORS
    show_train = _is_rec_enabled(hud_config, "train")
    show_story = _is_rec_enabled(hud_config, "story")
    show_weekend = _is_rec_enabled(hud_config, "weekend")
    show_shop = _is_rec_enabled(hud_config, "shop")
    show_skill = _is_rec_enabled(hud_config, "skill")
    show_sec = _is_rec_enabled(hud_config, "secondary")

    if mode == "ycjm01":
        scored = last_turn_scored or []
        main_a = anc.get("main_screen", {})
        tr_btn = main_a.get("train_entry", {"rx": 0.840, "ry": 0.868, "top_edge_ry": 0.790})
        rs_btn = main_a.get("rest_entry", {"rx": 0.880, "ry": 0.648, "top_edge_ry": 0.614})
        sh_btn = main_a.get("shop_entry", {"rx": 0.920, "ry": 0.165, "badge_rx": 0.862, "badge_ry": 0.215})
        tr_top_ry = float(tr_btn.get("top_edge_ry", 0.790))
        rs_top_ry = float(rs_btn.get("top_edge_ry", 0.614))

        if show_train and scored:
            best = scored[0]
            if best.get("idx", 0) == 0:
                specs.append({
                    "left": f"★ EV {best['score']:.0f}",
                    "right": "休息回体",
                    "rx": rs_btn["rx"] - 0.015,
                    "ry": rs_top_ry,
                    "anchor": "bottom_center",
                    "style": "best",
                    "tail": True,
                })
                if show_sec and len(scored) > 1:
                    s2 = scored[1]
                    diff = max(0.0, best["score"] - s2["score"])
                    if diff <= 30.0:
                        specs.append({
                            "left": f"EV {s2['score']:.0f}",
                            "right": format_train_subject_markup(
                                s2, state, stat_names=snames, style_key="second", include_tag=False
                            ),
                            "rx": tr_btn["rx"],
                            "ry": tr_top_ry,
                            "anchor": "bottom_center",
                            "style": "second",
                            "tail": True,
                        })
            else:
                train_alts = [x for x in scored[1:] if 1 <= int(x.get("idx", 0)) <= 6]
                close_t2 = (
                    train_alts[0]
                    if (show_sec and train_alts and (best["score"] - train_alts[0]["score"]) <= 22.0)
                    else None
                )
                right_txt = format_train_subject_markup(
                    best, state, stat_names=snames, style_key="best", include_tag=True
                )
                primary_rx = (tr_btn["rx"] + 0.032) if close_t2 else tr_btn["rx"]
                specs.append({
                    "left": f"★ EV {best['score']:.0f}",
                    "right": right_txt,
                    "rx": primary_rx,
                    "ry": tr_top_ry,
                    "anchor": "bottom_center",
                    "style": "best",
                    "tail": True,
                })
                if close_t2:
                    specs.append({
                        "left": f"EV {close_t2['score']:.0f}",
                        "right": format_train_subject_markup(
                            close_t2, state, stat_names=snames, style_key="second", include_tag=False
                        ),
                        "style": "second",
                        "tail": False,
                        "font_scale": 0.92,
                        "left_of_prev": True,
                    })
                rest_item = next((x for x in scored[1:] if x.get("idx", 0) == 0), None)
                if show_sec and rest_item and (best["score"] - rest_item["score"]) <= 22.0:
                    specs.append({
                        "left": f"EV {rest_item['score']:.0f}",
                        "right": "休息回体",
                        "rx": rs_btn["rx"] - 0.015,
                        "ry": rs_top_ry,
                        "anchor": "bottom_center",
                        "style": "second",
                        "tail": True,
                        "font_scale": 0.92,
                    })

        shop_badge = (
            extract_weekday_shop_badge_fn(state, scored)
            if (show_shop and extract_weekday_shop_badge_fn)
            else ""
        )
        if shop_badge:
            clean_sh = strip_markup_to_clean_text(shop_badge)
            specs.append({
                "left": "商店",
                "right": clean_sh,
                "rx": float(sh_btn.get("badge_rx", sh_btn["rx"] - 0.058)),
                "ry": float(sh_btn.get("badge_ry", sh_btn["ry"] + 0.045)),
                "anchor": "right",
                "style": "shop",
                "tail": False,
            })
        if show_train:
            trough_line = build_weekday_trough_line(
                state,
                scored,
                include_shop=show_shop,
                format_weekday_hud_tips_fn=format_weekday_hud_tips_fn,
                get_whistle_step_fn=get_whistle_step_fn,
                check_lineup_and_equip_synergy_fn=check_lineup_and_equip_synergy_fn,
            )
        elif shop_badge:
            trough_line = f"商店｜<span foreground='#C2410C'>{html.escape(strip_markup_to_clean_text(shop_badge))}</span>"

    elif mode == "ycjm02":
        if not show_train:
            return [], ""
        tr_a = anc.get("train_screen", {})
        btns = tr_a.get("buttons", {})
        right_rx = float(tr_a.get("badge_right_rx", 0.742))

        if int(state.get("round_type", 1) or 1) == 2 and last_wk_adv and last_wk_adv.get("scored_sat_trains"):
            wk = last_wk_adv
            sat_trains = wk.get("scored_sat_trains") or []
            top_ev = float(wk.get("best_ev", sat_trains[0].get("route_ev", sat_trains[0]["score"])))
            max_n = 3 if show_sec else 1
            for rank_i, t_item in enumerate(sat_trains[:max_n]):
                b_coord = btns.get(str(t_item["idx"]))
                if not b_coord:
                    continue
                t_ev = float(t_item.get("route_ev", t_item["score"]))
                if rank_i == 0:
                    r_txt = format_train_subject_markup(
                        t_item, state, stat_names=snames, style_key="best", include_tag=True
                    )
                    specs.append({
                        "left": f"★ EV {t_ev:.0f}",
                        "right": r_txt,
                        "rx": right_rx,
                        "ry": b_coord["ry"],
                        "anchor": "right",
                        "style": "best",
                    })
                elif show_sec:
                    diff = max(0.0, top_ev - t_ev)
                    if diff <= 32.0:
                        r_txt = format_train_subject_markup(
                            t_item, state, stat_names=snames, style_key="second", include_tag=True
                        )
                        specs.append({
                            "left": f"EV {t_ev:.0f}",
                            "right": r_txt,
                            "rx": right_rx,
                            "ry": b_coord["ry"],
                            "anchor": "right",
                            "style": "second",
                        })
            trough_line = f"{wk.get('day_tag', '周六')}｜{strip_markup_to_clean_text(wk.get('best_compact', ''))}"
        else:
            scored = last_turn_scored or []
            train_only = [x for x in scored if 1 <= int(x.get("idx", 0)) <= 6]
            rest_top = bool(scored and scored[0].get("idx", 0) == 0)
            ref_top_ev = scored[0]["score"] if scored else (train_only[0]["score"] if train_only else 0.0)
            max_n = 3 if show_sec else 1
            for rank_i, item in enumerate(train_only[:max_n]):
                b_coord = btns.get(str(item["idx"]))
                if not b_coord:
                    continue
                if rank_i == 0 and not rest_top:
                    r_txt = format_train_subject_markup(
                        item, state, stat_names=snames, style_key="best", include_tag=True
                    )
                    specs.append({
                        "left": f"★ EV {item['score']:.0f}",
                        "right": r_txt,
                        "rx": right_rx,
                        "ry": b_coord["ry"],
                        "anchor": "right",
                        "style": "best",
                    })
                else:
                    diff = max(0.0, ref_top_ev - item["score"])
                    if rank_i == 0 and rest_top:
                        specs.append({
                            "left": f"EV {item['score']:.0f}",
                            "right": format_train_subject_markup(
                                item,
                                state,
                                stat_names=snames,
                                style_key="warn",
                                include_tag=False,
                                suffix_warn="退回休息更赚",
                            ),
                            "rx": right_rx,
                            "ry": b_coord["ry"],
                            "anchor": "right",
                            "style": "warn",
                        })
                    elif show_sec and diff <= 32.0:
                        r_txt = format_train_subject_markup(
                            item, state, stat_names=snames, style_key="second", include_tag=True
                        )
                        specs.append({
                            "left": f"EV {item['score']:.0f}",
                            "right": r_txt,
                            "rx": right_rx,
                            "ry": b_coord["ry"],
                            "anchor": "right",
                            "style": "second",
                        })
            if rest_top and scored:
                trough_line = (
                    f"{int(state.get('round', 1)):02d}训｜"
                    f"<span foreground='#B91C1C'>低体高危：建议退回点休息 (EV {scored[0]['score']:.0f})</span>"
                )
            else:
                trough_line = build_weekday_trough_line(
                    state,
                    scored,
                    include_shop=show_shop,
                    format_weekday_hud_tips_fn=format_weekday_hud_tips_fn,
                    get_whistle_step_fn=get_whistle_step_fn,
                    check_lineup_and_equip_synergy_fn=check_lineup_and_equip_synergy_fn,
                )

    elif mode == "story_choice":
        if not show_story:
            return [], ""
        adv = last_story_adv
        if adv:
            s_opts = adv.get("scored_options") or []
            st_a = anc.get("story_screen", {})
            n_opts = max(len(s_opts), len(adv.get("opt_lines") or []))
            opt_map = st_a.get("three_options", {}) if n_opts >= 3 else st_a.get("two_options", {})
            if s_opts:
                top_ev = s_opts[0]["ev"]
                for rank_i, item in enumerate(s_opts[:3]):
                    b_key = str(item.get("b_idx", rank_i + 1))
                    oc = opt_map.get(b_key) or opt_map.get(str(rank_i + 1))
                    if not oc:
                        continue
                    eff_short = strip_markup_to_clean_text(item.get("effects") or "")[:16]
                    if rank_i == 0:
                        specs.append({
                            "left": f"★ EV {item['ev']:.0f}",
                            "right": eff_short or "推荐选项",
                            "rx": oc["rx"],
                            "ry": oc["ry"] - 0.045,
                            "anchor": "bottom_center",
                            "style": "best",
                            "tail": True,
                        })
                    elif show_sec:
                        diff = max(0.0, top_ev - item["ev"])
                        specs.append({
                            "left": f"EV {item['ev']:.0f}",
                            "right": eff_short or "备选选项",
                            "rx": oc["rx"],
                            "ry": oc["ry"] - 0.045,
                            "anchor": "bottom_center",
                            "style": "second" if diff < 35.0 else "muted",
                            "tail": True,
                            "font_scale": 0.92,
                        })
            ev_t = strip_markup_to_clean_text(adv.get("ev_name", "事件"))[:6]
            rec_1 = strip_markup_to_clean_text(
                (adv.get("recommendation") or "").splitlines()[0] if adv.get("recommendation") else ""
            )
            trough_line = f"{ev_t}｜{rec_1}"

    elif mode in (
        "weekend_main",
        "weekend_map",
        "weekend_gashapon",
        "weekend_minigame",
        "weekend_park",
        "weekend_restaurant",
    ):
        if mode not in ("weekend_main", "weekend_map") and not show_weekend:
            return [], ""
        wk = last_wk_adv
        if wk:
            best_ev = float(wk.get("best_ev", 0.0))
            best_act = wk.get("best_act") or (8, 19)
            act_ev_map = wk.get("act_ev_map") or {}
            sh_btn = anc.get("main_screen", {}).get(
                "shop_entry", {"rx": 0.920, "ry": 0.165, "badge_rx": 0.862, "badge_ry": 0.215}
            )

            if show_shop and mode in ("weekend_main", "weekend_map"):
                if wk.get("hud_free_tip"):
                    specs.append({
                        "left": "商店",
                        "right": "0金免费领",
                        "rx": float(sh_btn.get("badge_rx", 0.862)),
                        "ry": float(sh_btn.get("badge_ry", 0.215)),
                        "anchor": "right",
                        "style": "shop",
                    })
                elif "🛒" in (wk.get("best_compact") or "") or wk.get("hud_shop_tip"):
                    specs.append({
                        "left": "商店",
                        "right": "先买商店好货",
                        "rx": float(sh_btn.get("badge_rx", 0.862)),
                        "ry": float(sh_btn.get("badge_ry", 0.215)),
                        "anchor": "right",
                        "style": "shop",
                    })

            if mode in ("weekend_main", "weekend_map") and not show_weekend:
                if specs:
                    shop_msg = wk.get("hud_free_tip") or wk.get("hud_shop_tip") or "右上角商店有可买好货"
                    return specs, f"商店｜<span foreground='#C2410C'>{html.escape(strip_markup_to_clean_text(shop_msg))}</span>"
                return [], ""

            if mode == "weekend_main":
                wm_a = anc.get("weekend_map", {})
                entry_btn = wm_a.get("entry_btn", {"rx": 0.837, "ry": 0.856, "top_edge_ry": 0.790})
                tr_btn = anc.get("main_screen", {}).get(
                    "train_entry", {"rx": 0.840, "ry": 0.868, "top_edge_ry": 0.790}
                )
                has_wt = bool(wk.get("has_weekend_train") and wk.get("best_train_opt"))
                if has_wt and best_act[0] == 0:
                    top_t = wk["best_train_opt"]["train_item"]
                    specs.append({
                        "left": f"★ EV {best_ev:.0f}",
                        "right": format_train_subject_markup(
                            top_t, state, stat_names=snames, style_key="best", include_tag=False, prefix="加训·"
                        ),
                        "rx": tr_btn["rx"],
                        "ry": float(tr_btn.get("top_edge_ry", 0.790)),
                        "anchor": "bottom_center",
                        "style": "best",
                        "tail": True,
                    })
                else:
                    act_lbl_map = {
                        6: "6号扭蛋",
                        3: "3号街机",
                        8: "餐厅打工",
                        1: "公园出行",
                    }
                    act_lbl = act_lbl_map.get(best_act[0], "周末外出")
                    specs.append({
                        "left": f"★ EV {best_ev:.0f}",
                        "right": f"外出·{act_lbl}",
                        "rx": entry_btn["rx"],
                        "ry": float(entry_btn.get("top_edge_ry", 0.790)),
                        "anchor": "bottom_center",
                        "style": "best",
                        "tail": True,
                    })

            elif mode == "weekend_map":
                wm_a = anc.get("weekend_map", {})
                bldg_cands = []
                w_opts = [(k, act_ev_map[k]) for k in ((8, 19), (8, 20)) if k in act_ev_map]
                if w_opts:
                    bk, bv = max(w_opts, key=lambda x: x[1]["total_ev"])
                    lbl = "重度打工" if bk[1] == 20 else "轻度打工"
                    bldg_cands.append(("restaurant_work", lbl, bv["total_ev"]))
                if (3, 6) in act_ev_map:
                    bldg_cands.append(("arcade_game", "街机小游戏", act_ev_map[(3, 6)]["total_ev"]))
                if (6, 17) in act_ev_map:
                    bldg_cands.append(("street_gashapon", "6号扭蛋机", act_ev_map[(6, 17)]["total_ev"]))
                p_opts = [(k, act_ev_map[k]) for k in ((1, 14), (1, 13)) if k in act_ev_map]
                if p_opts:
                    pk, pv = max(p_opts, key=lambda x: x[1]["total_ev"])
                    lbl_p = "野餐回体" if pk[1] == 14 else "划船散心"
                    bldg_cands.append(("park_outing", lbl_p, pv["total_ev"]))

                bldg_cands.sort(key=lambda x: x[2], reverse=True)
                if bldg_cands:
                    top_b_ev = bldg_cands[0][2]
                    for r_i, (b_key, b_lbl, b_ev) in enumerate(bldg_cands[:2]):
                        bc = wm_a.get(b_key)
                        if not bc:
                            continue
                        if r_i == 0:
                            specs.append({
                                "left": f"★ EV {b_ev:.0f}",
                                "right": b_lbl,
                                "rx": bc["rx"],
                                "ry": bc["ry"] - 0.068,
                                "anchor": "bottom_center",
                                "style": "best",
                                "tail": True,
                            })
                        elif show_sec:
                            diff_b = max(0.0, top_b_ev - b_ev)
                            if diff_b < 45.0:
                                specs.append({
                                    "left": f"EV {b_ev:.0f}",
                                    "right": b_lbl,
                                    "rx": bc["rx"],
                                    "ry": bc["ry"] - 0.068,
                                    "anchor": "bottom_center",
                                    "style": "second",
                                    "tail": True,
                                })

            elif mode == "weekend_gashapon":
                g_opts = anc.get("gashapon_screen", {}).get("options", {})
                g_list = wk.get("scored_gashapon") or []
                gold_now = int(state.get("gold", 0))
                afford = [x for x in g_list if gold_now >= x["price"]]
                ranked_g = afford + [x for x in g_list if gold_now < x["price"]]
                if ranked_g:
                    top_gev = ranked_g[0]["ev"]
                    for r_i, g_it in enumerate(ranked_g[: (3 if show_sec else 1)]):
                        gc = g_opts.get(str(g_it["idx"]))
                        if not gc:
                            continue
                        g_top_ry = float(gc.get("top_edge_ry", gc["ry"] - 0.230))
                        can_buy = gold_now >= g_it["price"]
                        stag = strip_markup_to_clean_text(g_it.get("short_tag", ""))[:12]
                        if r_i == 0 and can_buy:
                            specs.append({
                                "left": f"★ EV {g_it['ev']:.0f}·净{g_it.get('net_ev', g_it['ev']):.0f}",
                                "right": stag or "首选扭蛋",
                                "rx": gc["rx"],
                                "ry": g_top_ry,
                                "anchor": "bottom_center",
                                "style": "best",
                                "tail": True,
                            })
                        elif not can_buy and (r_i == 0 or show_sec):
                            specs.append({
                                "left": f"需{g_it['price']}金",
                                "right": stag or "金币不足",
                                "rx": gc["rx"],
                                "ry": g_top_ry,
                                "anchor": "bottom_center",
                                "style": "warn",
                                "tail": True,
                                "font_scale": 0.90,
                            })
                        elif can_buy and show_sec:
                            diff_g = max(0.0, top_gev - g_it["ev"])
                            if r_i == 1 or diff_g <= 25.0:
                                specs.append({
                                    "left": f"EV {g_it['ev']:.0f}·净{g_it.get('net_ev', g_it['ev']):.0f}",
                                    "right": stag or "备选扭蛋",
                                    "rx": gc["rx"],
                                    "ry": g_top_ry,
                                    "anchor": "bottom_center",
                                    "style": "second",
                                    "tail": True,
                                })

            elif mode == "weekend_minigame":
                m_opts = anc.get("minigame_screen", anc.get("gashapon_screen", {})).get("options", {})
                m_list = wk.get("scored_minigame") or []
                gold_now = int(state.get("gold", 0))
                afford_m = [x for x in m_list if gold_now >= x["price"]]
                ranked_m = afford_m + [x for x in m_list if gold_now < x["price"]]
                if ranked_m:
                    top_mev = ranked_m[0]["ev"]
                    for r_i, m_it in enumerate(ranked_m[: (3 if show_sec else 1)]):
                        ui_slot = int(m_it.get("ui_slot") or m_it["idx"])
                        mc = m_opts.get(str(ui_slot))
                        if not mc:
                            continue
                        m_top_ry = float(mc.get("top_edge_ry", mc["ry"] - 0.230))
                        can_buy_m = gold_now >= m_it["price"]
                        stag = strip_markup_to_clean_text(m_it.get("short_tag", ""))[:12]
                        if r_i == 0 and can_buy_m:
                            specs.append({
                                "left": f"★ EV {m_it['ev']:.0f}·净{m_it.get('net_ev', m_it['ev']):.0f}",
                                "right": stag or "首选街机",
                                "rx": mc["rx"],
                                "ry": m_top_ry,
                                "anchor": "bottom_center",
                                "style": "best",
                                "tail": True,
                            })
                        elif not can_buy_m and (r_i == 0 or show_sec):
                            specs.append({
                                "left": f"需{m_it['price']}金",
                                "right": stag or "金币不足",
                                "rx": mc["rx"],
                                "ry": m_top_ry,
                                "anchor": "bottom_center",
                                "style": "warn",
                                "tail": True,
                                "font_scale": 0.90,
                            })
                        elif can_buy_m and show_sec:
                            diff_m = max(0.0, top_mev - m_it["ev"])
                            if r_i == 1 or diff_m <= 25.0:
                                specs.append({
                                    "left": f"EV {m_it['ev']:.0f}·净{m_it.get('net_ev', m_it['ev']):.0f}",
                                    "right": stag or "备选街机",
                                    "rx": mc["rx"],
                                    "ry": m_top_ry,
                                    "anchor": "bottom_center",
                                    "style": "second",
                                    "tail": True,
                                })

            elif mode == "weekend_park":
                pk_a = anc.get("park_screen", {})
                pk_cands = []
                if (1, 14) in act_ev_map:
                    pk_cands.append(("picnic", "野餐回体", act_ev_map[(1, 14)]["total_ev"]))
                if (1, 13) in act_ev_map:
                    pk_cands.append(("boat", "划船散心", act_ev_map[(1, 13)]["total_ev"]))
                pk_cands.sort(key=lambda x: x[2], reverse=True)
                if pk_cands:
                    for r_i, (pk_k, pk_lbl, pk_ev) in enumerate(pk_cands[:2]):
                        pc = pk_a.get(pk_k)
                        if not pc:
                            continue
                        if r_i == 0:
                            specs.append({
                                "left": f"★ EV {pk_ev:.0f}",
                                "right": pk_lbl,
                                "rx": pc["rx"],
                                "ry": pc["ry"] - 0.115,
                                "anchor": "bottom_center",
                                "style": "best",
                                "tail": True,
                            })
                        elif show_sec:
                            specs.append({
                                "left": f"EV {pk_ev:.0f}",
                                "right": pk_lbl,
                                "rx": pc["rx"],
                                "ry": pc["ry"] - 0.115,
                                "anchor": "bottom_center",
                                "style": "second",
                                "tail": True,
                            })

            elif mode == "weekend_restaurant":
                rs_a = anc.get("restaurant_screen", {})
                rs_cands = []
                if (8, 19) in act_ev_map:
                    rs_cands.append(("light", "轻度打工", act_ev_map[(8, 19)]["total_ev"]))
                if (8, 20) in act_ev_map:
                    rs_cands.append(("heavy", "重度打工", act_ev_map[(8, 20)]["total_ev"]))
                rs_cands.sort(key=lambda x: x[2], reverse=True)
                if rs_cands:
                    for r_i, (rk_k, rk_lbl, rk_ev) in enumerate(rs_cands[:2]):
                        rc = rs_a.get(rk_k)
                        if not rc:
                            continue
                        if r_i == 0:
                            specs.append({
                                "left": f"★ EV {rk_ev:.0f}",
                                "right": rk_lbl,
                                "rx": rc["rx"],
                                "ry": rc["ry"] - 0.115,
                                "anchor": "bottom_center",
                                "style": "best",
                                "tail": True,
                            })
                        elif show_sec:
                            specs.append({
                                "left": f"EV {rk_ev:.0f}",
                                "right": rk_lbl,
                                "rx": rc["rx"],
                                "ry": rc["ry"] - 0.115,
                                "anchor": "bottom_center",
                                "style": "second",
                                "tail": True,
                            })

            day_s = strip_markup_to_clean_text(wk.get("day_tag", "周末")).split("(")[0][:6]
            best_c = strip_markup_to_clean_text(wk.get("best_compact") or "")[:36]
            trough_line = f"{day_s}｜{best_c}"

    elif mode == "skill_choice":
        if not show_skill:
            return [], ""
        sk_evals = last_skill_evals or []
        sk_opts = anc.get("skill_choice_screen", {}).get("options", {})
        for item in sk_evals[:3]:
            s_idx = int(item.get("slot_idx", 0))
            sc = sk_opts.get(str(s_idx + 1))
            if not sc:
                continue
            st_key = item.get("style_key") or "second"
            if not show_sec and st_key == "second":
                continue
            btxt = strip_markup_to_clean_text(item.get("badge_text") or "")
            sname = strip_markup_to_clean_text(item.get("skill_name") or "")
            l_txt = f"★ {btxt or '首选'}" if st_key == "best" else (btxt or "备选")
            r_txt = (
                sname
                if (sname and not item.get("is_unknown"))
                else strip_markup_to_clean_text(item.get("reason") or "")[:10]
            )
            specs.append({
                "left": l_txt,
                "right": r_txt,
                "rx": sc["rx"],
                "ry": sc["ry"] - 0.195,
                "anchor": "bottom_center",
                "style": st_key,
                "tail": True,
            })
        roles = sorted(get_char_role_tags_fn(int(state.get("main_char_id") or 0)) - {"通用"})
        role_str = "/".join(roles) if roles else "通用"
        top_sk = next((x for x in sk_evals if x.get("rank") == 1), None)
        if top_sk:
            trough_line = (
                f"选技({role_str})｜首选：{top_sk['skill_name']} · "
                f"{strip_markup_to_clean_text(top_sk.get('reason', ''))[:24]}"
            )
        else:
            trough_line = f"选技({role_str})｜识别比对中"

    elif mode == "match_ready":
        main_a = anc.get("main_screen", {})
        tr_btn = main_a.get("train_entry", {"rx": 0.840, "ry": 0.868, "top_edge_ry": 0.790})
        sh_btn = main_a.get("shop_entry", {"rx": 0.920, "ry": 0.165, "badge_rx": 0.862, "badge_ry": 0.215})
        is_final_match = int(state.get("exp_round", 0) or 0) >= 49
        match_tag = "决赛日" if is_final_match else "比赛日"

        gold_now = int(state.get("gold", 0) or 0)
        s_items = state.get("store_items") or []
        buyable_final = []
        free_items = []
        for it in s_items:
            iid, inum, iprice = int(it.get("id", 0)), int(it.get("num", 0)), int(it.get("price", 0))
            if inum <= 0:
                continue
            if iprice == 0:
                free_items.append(catalog.get(iid, {}).get("short", f"#{iid}"))
            elif is_final_match and iprice <= gold_now:
                meta_i = catalog.get(iid, {})
                if meta_i.get("type") in ("stat", "stat_special"):
                    buyable_final.append((meta_i.get("short", f"#{iid}"), iprice))

        need_shop = False
        if show_shop and free_items:
            need_shop = True
            specs.append({
                "left": "商店",
                "right": f"0金领{free_items[0]}",
                "rx": float(sh_btn.get("badge_rx", 0.862)),
                "ry": float(sh_btn.get("badge_ry", 0.215)),
                "anchor": "right",
                "style": "shop",
            })
        elif show_shop and is_final_match and buyable_final:
            need_shop = True
            specs.append({
                "left": "决赛清仓",
                "right": f"花光{gold_now}金买属性",
                "rx": float(sh_btn.get("badge_rx", 0.862)),
                "ry": float(sh_btn.get("badge_ry", 0.215)),
                "anchor": "right",
                "style": "shop",
            })

        specs.append({
            "left": f"★ {match_tag}",
            "right": "扫完商店➔开赛" if need_shop else "直接开赛",
            "rx": tr_btn["rx"],
            "ry": float(tr_btn.get("top_edge_ry", 0.790)),
            "anchor": "bottom_center",
            "style": "best",
            "tail": True,
        })
        trough_line = (
            f"{match_tag}｜先清商店花光{gold_now}金再开赛"
            if (is_final_match and need_shop)
            else (f"{match_tag}｜0金免费领完直接开赛" if need_shop else f"{match_tag}｜点击右下角开始比赛")
        )

    elif mode == "store":
        if not show_shop:
            return [], ""
        if int(state.get("exp_round", 0) or 0) >= 49:
            gold_now = int(state.get("gold", 0) or 0)
            trough_line = f"决赛前清仓｜余{gold_now}金优先全买属性书/特调"
        elif int(state.get("round_type", 1) or 1) == 2 and last_wk_adv:
            wk = last_wk_adv
            shop_s = strip_markup_to_clean_text(wk.get("shop_one_line", "按路线预算选购"))[:40]
            trough_line = f"商店｜{shop_s}"
        else:
            scored = last_turn_scored or []
            shop_tip_s = (
                format_weekday_shop_tip_fn(
                    state,
                    best_ev=(scored[0]["score"] if scored else None),
                    strategy=strategy,
                    scored_cmds=scored,
                )
                if format_weekday_shop_tip_fn
                else ""
            )
            clean_s = strip_markup_to_clean_text(shop_tip_s)[:40] if shop_tip_s else "当前无必买道具，省钱备战周末扭蛋"
            trough_line = f"商店｜{clean_s}"

    return specs, trough_line
