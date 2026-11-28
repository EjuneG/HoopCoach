#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win_diegetic_hud.py — 跨平台二次元漫画硬边贴纸角标渲染器（2× 超采样抗锯齿 + 分层透明叠加）

核心特性：
1. 离线可测渲染层（零 Win32 依赖）：
   - `render_badge_rgba(spec, dpi_scale=1.0, gh=1080) -> PIL.Image.Image`
   - `render_trough_pill_rgba(trough_markup, dpi_scale=1.0, gh=1080, is_bottom=False, focus_badge="", char_id=None) -> PIL.Image.Image`
   - `load_q_avatar_rgba(char_id=None, size_px=48, ...) -> Optional[PIL.Image.Image]`
   - 2× 超采样绘制奶白底 `#F8FCFF` + `1.5px` 深墨蓝细描边 `#142342` + 轻巧 `2px` 硬偏移阴影
     + 左侧斜切高饱和渐变色块 + 右端极淡斜向速度线 + 无缝底部尖角，经 LANCZOS 缩回 1× RGBA；
2. Windows 分层穿透窗口复用池（`Win32LayeredBadgeWindow` / `Win32BadgePool`）：
   - 严格 64-bit 安全 `ctypes.windll.user32` / `gdi32` 接口声明；
   - 预乘 Alpha BGRA DIB Section 原子提交，真半透明抗锯齿、100% 点击穿透、不抢游戏焦点；
   - 支持 `left_of_prev` 自动防重叠并排摆放，以及过场/切后台自动隐身。
"""

from __future__ import annotations

from collections import OrderedDict
import ctypes
import html
import math
import os
import re
import sys
from typing import Optional

from hud_badges import (
    ACCENT_PALETTES,
    DEFAULT_Q_AVATAR_CHAR_ID,
    HUD_CHAMPION_GOLD_RGB,
    HUD_INK_STROKE_RGB,
    HUD_INK_STROKE_RGBA,
    HUD_PAPER_RGB,
    HUD_SHADOW_RGBA,
    HUD_SKY_BLUE_RGB,
    HUD_TEXT_INK,
    compute_badge_position,
    compute_game_content_box,
    resolve_badge_markups,
    resolve_q_avatar_path,
    strip_markup_to_clean_text,
)

IS_WINDOWS = sys.platform == "win32"

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFilter = None
    ImageFont = None
    PIL_AVAILABLE = False


# =====================================================================
# 字体探测与 Pango 轻量标记解析
# =====================================================================

_FONT_PATH_CACHE: dict[bool, Optional[str]] = {}
_PIL_FONT_CACHE: OrderedDict[tuple[str, int], object] = OrderedDict()
_BADGE_IMG_CACHE: OrderedDict[tuple, object] = OrderedDict()
_BADGE_IMG_CACHE_MAX = 160
_AVATAR_IMG_CACHE: OrderedDict[tuple, object] = OrderedDict()
_AVATAR_IMG_CACHE_MAX = 64


def clear_render_caches() -> None:
    """清空内存级贴图与头像 LRU 缓存（供单测或热切换使用）。"""
    _BADGE_IMG_CACHE.clear()
    _AVATAR_IMG_CACHE.clear()


def _find_cjk_font_path(black: bool = False) -> Optional[str]:
    if black in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[black]

    candidates: list[str] = []
    if IS_WINDOWS:
        win_dir = os.environ.get("WINDIR", r"C:\Windows")
        fdir = os.path.join(win_dir, "Fonts")
        candidates.extend([
            os.path.join(fdir, "msyhbd.ttc"),
            os.path.join(fdir, "msyh.ttc"),
            os.path.join(fdir, "simhei.ttf"),
            os.path.join(fdir, "Dengb.ttf"),
            os.path.join(fdir, "seguisb.ttf"),
        ])
    else:
        home_fonts = os.path.expanduser("~/.local/share/fonts")
        if black:
            candidates.extend([
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Black.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                os.path.join(home_fonts, "SarasaUiSC/SarasaUiSC-Bold.ttf"),
                os.path.join(home_fonts, "source-han-ttf/SourceHanSansCN-Medium.ttf"),
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            ])
        else:
            candidates.extend([
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
                os.path.join(home_fonts, "SarasaUiSC/SarasaUiSC-Bold.ttf"),
                os.path.join(home_fonts, "SarasaUiSC/SarasaUiSC-SemiBold.ttf"),
                os.path.join(home_fonts, "source-han-ttf/SourceHanSansCN-Medium.ttf"),
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            ])

    found = None
    for path in candidates:
        if path and os.path.isfile(path):
            found = path
            break
    _FONT_PATH_CACHE[black] = found
    return found


def _load_pil_font(px_size: int, black: bool = False):
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装，无法渲染抗锯齿角标")
    px_size = max(10, int(px_size))
    fpath = _find_cjk_font_path(black=black) or ""
    key = (fpath, px_size)
    cached = _PIL_FONT_CACHE.get(key)
    if cached is not None:
        _PIL_FONT_CACHE.move_to_end(key)
        return cached
    try:
        if fpath:
            font = ImageFont.truetype(fpath, size=px_size)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    _PIL_FONT_CACHE[key] = font
    if len(_PIL_FONT_CACHE) > 64:
        _PIL_FONT_CACHE.popitem(last=False)
    return font


def _hex_to_rgb(hex_color: str, fallback: tuple[int, int, int] = (30, 50, 70)) -> tuple[int, int, int]:
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            pass
    elif len(s) == 3:
        try:
            return (int(s[0] * 2, 16), int(s[1] * 2, 16), int(s[2] * 2, 16))
        except ValueError:
            pass
    return fallback


_EMOJI_STRIP_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\u26A0\u26A1\u2705\u2728\u274C\u2757\u23F3\u23F8"
    "\uFE0F"
    "]+"
)


def _strip_unsupported_emoji(text: str) -> str:
    """剔除微软雅黑/NotoSansCJK 等 TrueType 字体不含字形的彩色 Emoji 与缺字箭头（完整保留 ★ ◆ ● ✓ → ▶ · 等符号），杜绝豆腐块。"""
    if not text:
        return ""
    cleaned = text.replace("➔", "→").replace("▸", "▶")
    return _EMOJI_STRIP_RE.sub("", cleaned)


def parse_pango_runs(markup: str, default_color: str = "#1E3246") -> list[tuple[str, str, float, bool]]:
    """
    轻量解析 Pango markup 中的 <span foreground='...' size='88%' weight='bold'> 与 <b>，
    返回 [(text, hex_color, size_scale, is_bold), ...]。
    """
    token_re = re.compile(r"(<span\b[^>]*>|</span>|<b>|</b>)", re.I)
    parts = token_re.split(markup or "")
    stack = [(default_color, 1.0, False)]
    runs: list[tuple[str, str, float, bool]] = []

    for part in parts:
        if not part:
            continue
        low = part.lower()
        if low.startswith("<span"):
            fg, sc, bd = stack[-1]
            m_fg = re.search(r"foreground=['\"]([^'\"]+)['\"]", part, re.I)
            if m_fg:
                fg = m_fg.group(1)
            m_sz = re.search(r"size=['\"]([^'\"]+)['\"]", part, re.I)
            if m_sz:
                raw_sz = m_sz.group(1).strip().lower()
                if raw_sz.endswith("%"):
                    try:
                        sc = max(0.65, min(1.35, float(raw_sz[:-1]) / 100.0))
                    except ValueError:
                        pass
                elif raw_sz == "small":
                    sc = 0.88
                elif raw_sz == "x-small":
                    sc = 0.78
            if "weight='bold'" in low or 'weight="bold"' in low or "weight='black'" in low:
                bd = True
            stack.append((fg, sc, bd))
        elif low == "</span>":
            if len(stack) > 1:
                stack.pop()
        elif low == "<b>":
            fg, sc, _ = stack[-1]
            stack.append((fg, sc, True))
        elif low == "</b>":
            if len(stack) > 1:
                stack.pop()
        else:
            txt = _strip_unsupported_emoji(html.unescape(part))
            if txt:
                fg, sc, bd = stack[-1]
                runs.append((txt, fg, sc, bd))
    return runs


def _measure_runs(
    draw,
    runs: list[tuple[str, str, float, bool]],
    base_px: int,
    black: bool,
) -> tuple[int, int, int]:
    """返回多段文字的 (total_width, ref_height, ref_top_offset)（均在当前画布像素空间下）。"""
    if not runs:
        return 0, 0, 0
    base_font = _load_pil_font(base_px, black=black)
    ref_bbox = draw.textbbox((0, 0), "★EV089防体", font=base_font)
    ref_top = int(ref_bbox[1])
    ref_h = max(1, int(ref_bbox[3] - ref_bbox[1]))

    total_w = 0.0
    for txt, _fg, sc, bd in runs:
        fnt = _load_pil_font(round(base_px * sc), black=(black or bd))
        if hasattr(draw, "textlength"):
            total_w += float(draw.textlength(txt, font=fnt))
        else:
            bb = draw.textbbox((0, 0), txt, font=fnt)
            total_w += float(bb[2] - bb[0])
    return max(1, int(round(total_w))), ref_h, ref_top


def _draw_runs(
    draw,
    x: float,
    card_y0: float,
    card_h: float,
    runs: list[tuple[str, str, float, bool]],
    base_px: int,
    black: bool,
    ref_h: int,
    ref_top: int,
) -> None:
    cur_x = float(x)
    base_y = card_y0 + (card_h - ref_h) * 0.5 - ref_top
    for txt, fg, sc, bd in runs:
        fnt = _load_pil_font(round(base_px * sc), black=(black or bd))
        rgb = _hex_to_rgb(fg)
        if abs(sc - 1.0) > 0.02:
            sub_bb = draw.textbbox((0, 0), "防0", font=fnt)
            sub_h = max(1, sub_bb[3] - sub_bb[1])
            y_pos = card_y0 + (card_h - sub_h) * 0.5 - sub_bb[1]
        else:
            y_pos = base_y
        draw.text((cur_x, y_pos), txt, font=fnt, fill=(rgb[0], rgb[1], rgb[2], 255))
        if hasattr(draw, "textlength"):
            cur_x += float(draw.textlength(txt, font=fnt))
        else:
            bb = draw.textbbox((0, 0), txt, font=fnt)
            cur_x += float(bb[2] - bb[0])


def _make_vertical_gradient(
    width: int,
    height: int,
    y_start: int,
    y_end: int,
    top_rgb_f: tuple[float, float, float],
    bot_rgb_f: tuple[float, float, float],
):
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r0, g0, b0 = top_rgb_f[0] * 255.0, top_rgb_f[1] * 255.0, top_rgb_f[2] * 255.0
    r1, g1, b1 = bot_rgb_f[0] * 255.0, bot_rgb_f[1] * 255.0, bot_rgb_f[2] * 255.0
    span = max(1, y_end - y_start)
    for y in range(height):
        t = max(0.0, min(1.0, (y - y_start) / float(span)))
        r = int(round(r0 + (r1 - r0) * t))
        g = int(round(g0 + (g1 - g0) * t))
        b = int(round(b0 + (b1 - b0) * t))
        draw.line([(0, y), (width - 1, y)], fill=(r, g, b, 255))
    return img


def _rgb_f_to_255(rgb_f: tuple[float, float, float]) -> tuple[int, int, int]:
    return (
        int(round(rgb_f[0] * 255.0)),
        int(round(rgb_f[1] * 255.0)),
        int(round(rgb_f[2] * 255.0)),
    )


def _rgba_f_to_255(rgba_f: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return (
        int(round(rgba_f[0] * 255.0)),
        int(round(rgba_f[1] * 255.0)),
        int(round(rgba_f[2] * 255.0)),
        int(round(rgba_f[3] * 255.0)),
    )


def _build_rounded_card_polygon(
    ox: float,
    oy: float,
    w: float,
    h: float,
    rad: float,
    *,
    tail_h: float = 0.0,
    tx0: float = 0.0,
    tx1: float = 0.0,
    arc_steps: int = 16,
) -> list[tuple[float, float]]:
    """生成圆角矩形 + 可选无缝底部尖角的连续多边形点集，确保底色与描边 100% 几何重合、零缺口。"""
    r = max(1.0, min(float(rad), float(w) * 0.5, float(h) * 0.5))
    pts: list[tuple[float, float]] = []

    def _add_arc(cx: float, cy: float, start_a: float, end_a: float) -> None:
        for i in range(arc_steps + 1):
            a = start_a + (end_a - start_a) * (i / float(arc_steps))
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    # 右上圆角 (-pi/2 -> 0)
    _add_arc(ox + w - r, oy + r, -math.pi * 0.5, 0.0)
    # 右下圆角 (0 -> pi/2)
    _add_arc(ox + w - r, oy + h - r, 0.0, math.pi * 0.5)
    # 可选底部指向尖角（无缝嵌入下底边）
    if tail_h > 0.0 and tx1 > tx0:
        pts.append((tx1, oy + h))
        pts.append(((tx0 + tx1) * 0.5, oy + h + tail_h))
        pts.append((tx0, oy + h))
    # 左下圆角 (pi/2 -> pi)
    _add_arc(ox + r, oy + h - r, math.pi * 0.5, math.pi)
    # 左上圆角 (pi -> 3*pi/2)
    _add_arc(ox + r, oy + r, math.pi, math.pi * 1.5)
    return pts


def _draw_right_speed_lines(
    canvas,
    clip_mask,
    x0: float,
    y0: float,
    card_w: float,
    card_h: float,
    slant: float,
    accent_rgb_f: tuple[float, float, float],
    ss: int,
):
    """在贴纸右端绘制极淡斜向漫画速度线（不干扰正文可读性，增强校园篮球番动感）。"""
    ar, ag, ab = _rgb_f_to_255(accent_rgb_f)
    line_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(line_layer)
    slope = max(6.0 * ss, slant * 0.85)
    right_edge = x0 + card_w
    # 3 条由右向左渐隐的斜向速度带
    stripes = (
        (5.5 * ss, 3.8 * ss, 28),
        (13.0 * ss, 2.4 * ss, 20),
        (19.5 * ss, 1.5 * ss, 14),
    )
    for offset_r, thick, alpha in stripes:
        top_x = right_edge - offset_r + slope * 0.35
        bot_x = right_edge - offset_r - slope * 0.65
        ldraw.polygon(
            [
                (top_x, y0 - 2 * ss),
                (top_x + thick, y0 - 2 * ss),
                (bot_x + thick, y0 + card_h + 2 * ss),
                (bot_x, y0 + card_h + 2 * ss),
            ],
            fill=(ar, ag, ab, alpha),
        )
    clipped_alpha = ImageChops.multiply(line_layer.split()[3], clip_mask)
    line_layer.putalpha(clipped_alpha)
    return Image.alpha_composite(canvas, line_layer)


def _render_geometric_emblem_rgba(size_px: int, ring_width_px: int = 3):
    """缺图或无角色上下文时的干净几何星徽降级贴图（零字体依赖，运动宝蓝 + 冠军金星 + 墨蓝描边）。"""
    sz = max(16, int(size_px))
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    # 圆形底蒙版
    circle_mask = Image.new("L", (sz, sz), 0)
    inset = max(1, ring_width_px // 2)
    ImageDraw.Draw(circle_mask).ellipse([inset, inset, sz - 1 - inset, sz - 1 - inset], fill=255)

    bg_grad = _make_vertical_gradient(
        sz,
        sz,
        0,
        sz,
        (27 / 255.0, 91 / 255.0, 255 / 255.0),
        (17 / 255.0, 57 / 255.0, 166 / 255.0),
    )
    bg_draw = ImageDraw.Draw(bg_grad)
    # 活力斜切橙红光带
    bg_draw.polygon(
        [
            (sz * 0.58, 0),
            (sz, 0),
            (sz * 0.72, sz),
            (sz * 0.34, sz),
        ],
        fill=(255, 81, 38, 175),
    )
    img.paste(bg_grad, (0, 0), circle_mask)

    # 中央冠军金五角星 (#FFB703)
    cx, cy = sz * 0.5, sz * 0.5
    r_out = sz * 0.31
    r_in = sz * 0.135
    star_pts = []
    for i in range(10):
        ang = -math.pi * 0.5 + i * (math.pi / 5.0)
        rad_i = r_out if (i % 2 == 0) else r_in
        star_pts.append((cx + rad_i * math.cos(ang), cy + rad_i * math.sin(ang)))
    sdraw = ImageDraw.Draw(img)
    sdraw.polygon(star_pts, fill=(255, 183, 3, 255))

    # 1.5px 深墨蓝外环 (#142342)
    ink_rgba = _rgba_f_to_255(HUD_INK_STROKE_RGBA)
    sdraw.ellipse([0, 0, sz - 1, sz - 1], outline=ink_rgba, width=max(2, int(ring_width_px)))
    return img


def load_q_avatar_rgba(
    char_id: Optional[int] = None,
    size_px: int = 48,
    *,
    base_dir: Optional[str] = None,
    default_char_id: Optional[int] = DEFAULT_Q_AVATAR_CHAR_ID,
    ring_width_px: int = 3,
):
    """
    懒加载并 LRU 缓存圆形 Q 版小头像（附带深墨蓝 #142342 细描边环）。
    - 优先读取 `data/hoopgirls/heads/q_head_s_<id>_*.png`，次选 `packaging/assets/site/q_<id>.png`；
    - 缺失或非训练态自动平滑降级为默认 `q_head_s_1001`（澪奈）或几何星徽；
    - 缺失 Pillow、文件损坏或无资源时绝不抛异常阻塞。
    """
    if not PIL_AVAILABLE:
        return None
    sz = max(16, int(size_px))
    rw = max(2, int(ring_width_px))
    norm_cid: Optional[int] = None
    if char_id is not None:
        try:
            v = int(char_id)
            if v > 0:
                norm_cid = v
        except (TypeError, ValueError):
            norm_cid = None
    cache_key = (norm_cid, sz, base_dir or "", default_char_id, rw)
    cached = _AVATAR_IMG_CACHE.get(cache_key)
    if cached is not None:
        _AVATAR_IMG_CACHE.move_to_end(cache_key)
        return cached

    img_out = None
    try:
        path = resolve_q_avatar_path(norm_cid, base_dir=base_dir, default_char_id=default_char_id)
        if path and os.path.isfile(path):
            with Image.open(path) as src:
                rgba = src.convert("RGBA")
                # 居中正方形裁剪并 LANCZOS 缩放到目标像素尺寸
                sw, sh = rgba.size
                if sw != sh:
                    side = min(sw, sh)
                    left = (sw - side) // 2
                    top = (sh - side) // 2
                    rgba = rgba.crop((left, top, left + side, top + side))
                inner_sz = max(8, sz - rw)
                scaled = rgba.resize((inner_sz, inner_sz), Image.Resampling.LANCZOS)

                out = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
                mask = Image.new("L", (inner_sz, inner_sz), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, inner_sz - 1, inner_sz - 1], fill=255)
                off = (sz - inner_sz) // 2
                # 奶白内底衬防止角色头像边缘半透明露底
                bg_disc = Image.new("RGBA", (inner_sz, inner_sz), (248, 252, 255, 255))
                bg_disc = Image.alpha_composite(bg_disc, scaled)
                out.paste(bg_disc, (off, off), mask)
                ink_rgba = _rgba_f_to_255(HUD_INK_STROKE_RGBA)
                ImageDraw.Draw(out).ellipse([0, 0, sz - 1, sz - 1], outline=ink_rgba, width=rw)
                img_out = out
    except Exception:
        img_out = None

    if img_out is None:
        img_out = _render_geometric_emblem_rgba(sz, ring_width_px=rw)

    _AVATAR_IMG_CACHE[cache_key] = img_out
    if len(_AVATAR_IMG_CACHE) > _AVATAR_IMG_CACHE_MAX:
        _AVATAR_IMG_CACHE.popitem(last=False)
    return img_out


# =====================================================================
# 离屏 2× 超采样抗锯齿漫画贴纸渲染（纯 Pillow，Linux / Windows 通用可测）
# =====================================================================

def render_badge_rgba(
    spec: dict,
    dpi_scale: float = 1.0,
    gh: int = 1080,
):
    """
    根据角标 spec 离屏 2× 超采样渲染无锯齿 32-bit RGBA 贴图（返回 PIL.Image.Image）。

    spec 字段：
      - left / right / markup: 文本或带轻量 Pango <span> 标记的文案
      - style: "best" | "second" | "shop" | "warn" | "dup" | "unknown" | "muted"
      - tail: bool，是否在底部绘制无缝指向小尖角
      - font_scale: float，相对字号缩放倍率（默认 1.0）
      - char_id: Optional[int]，可选训练角色 ID（透传上下文）
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装，无法渲染抗锯齿角标")

    style_key = str(spec.get("style") or "second")
    if style_key not in ACCENT_PALETTES:
        style_key = "second"
    with_tail = bool(spec.get("tail", False))
    font_scale = float(spec.get("font_scale", 1.0) or 1.0)
    dpi = max(0.75, min(3.0, float(dpi_scale or 1.0)))

    lm, rm = resolve_badge_markups(
        left_markup=str(spec.get("left", "") or ""),
        right_markup=str(spec.get("right", "") or ""),
        markup=str(spec.get("markup", "") or ""),
        style_key=style_key,
    )

    base_pt = 14.2 if style_key == "best" else 13.0
    scale = max(0.82, min(1.32, (max(360, int(gh or 1080)) / 1080.0) * font_scale)) * dpi
    f_pt = round(base_pt * scale, 1)

    cache_key = ("badge", lm, rm, style_key, with_tail, f_pt)
    cached = _BADGE_IMG_CACHE.get(cache_key)
    if cached is not None:
        _BADGE_IMG_CACHE.move_to_end(cache_key)
        return cached

    pal = ACCENT_PALETTES[style_key]
    g_top, g_bot, border_rgba_f, l_fg, r_fg = pal
    is_black = style_key == "best"

    ss = 2  # 2× 超采样抗锯齿倍率
    base_px = max(14, int(round(f_pt * 1.28 * ss)))

    left_runs = parse_pango_runs(lm, default_color=l_fg)
    right_runs = parse_pango_runs(rm, default_color=r_fg) if rm.strip() else []
    has_right = bool(right_runs)

    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(probe)
    lw, lh, ltop = _measure_runs(pdraw, left_runs, base_px, black=is_black)
    rw, rh, rtop = _measure_runs(pdraw, right_runs, base_px, black=is_black) if has_right else (0, 0, 0)

    pad_l_x = max(10 * ss, int(round(f_pt * 0.82 * ss)))
    slant = max(8 * ss, int(round(f_pt * 0.65 * ss))) if has_right else 0
    pad_r_x = max(12 * ss, int(round(f_pt * 0.96 * ss))) if has_right else pad_l_x
    pad_y = max(5 * ss, int(round(f_pt * 0.40 * ss)))

    card_h = max(lh, rh, int(round(f_pt * 1.35 * ss))) + pad_y * 2
    if has_right:
        left_w = int(round(lw + pad_l_x * 2 + slant * 0.45))
        card_w = int(round(left_w + rw + pad_r_x + 7 * ss))
    else:
        left_w = int(round(lw + pad_l_x * 2))
        card_w = left_w

    tail_h = (6 * ss) if with_tail else 0
    tail_w = 12 * ss
    shadow_dx = 2 * ss
    shadow_dy = 2 * ss
    margin_left = 3 * ss
    margin_right = 3 * ss + shadow_dx
    margin_top = 3 * ss
    margin_bot = 4 * ss + shadow_dy

    win_w2 = max(44 * ss, card_w + margin_left + margin_right)
    win_h2 = max(26 * ss, card_h + tail_h + margin_top + margin_bot)
    win_w2 += win_w2 % ss
    win_h2 += win_h2 % ss

    x0 = margin_left
    y0 = margin_top
    rad = min(int(round(9.5 * ss)), card_h // 2, card_w // 2)
    tail_cx = x0 + card_w // 2
    tx0 = max(x0 + rad + 3 * ss, min(x0 + card_w - rad - tail_w - 3 * ss, tail_cx - tail_w // 2))
    tx1 = tx0 + tail_w

    def _card_pts(ox: float, oy: float) -> list[tuple[float, float]]:
        return _build_rounded_card_polygon(
            ox,
            oy,
            card_w,
            card_h,
            rad,
            tail_h=tail_h,
            tx0=(tx0 + (ox - x0)),
            tx1=(tx1 + (ox - x0)),
        )

    def _draw_card_shape_mask(ox: float, oy: float) -> Image.Image:
        m = Image.new("L", (win_w2, win_h2), 0)
        ImageDraw.Draw(m).polygon(_card_pts(ox, oy), fill=255)
        return m

    canvas = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))

    # 1. 轻巧 2px 硬偏移漫画贴纸阴影（深墨蓝半透明，绝不发灰糊成厚黑块）
    shadow_mask = _draw_card_shape_mask(x0 + shadow_dx, y0 + shadow_dy)
    shadow_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    shadow_fill = Image.new("RGBA", (win_w2, win_h2), _rgba_f_to_255(HUD_SHADOW_RGBA))
    shadow_layer.paste(shadow_fill, (0, 0), shadow_mask)
    canvas = Image.alpha_composite(canvas, shadow_layer)

    # 2. 画纸奶白底板 (#F8FCFF)
    body_mask = _draw_card_shape_mask(x0, y0)
    pr, pg, pb = _rgb_f_to_255(HUD_PAPER_RGB)
    body_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    body_fill = Image.new("RGBA", (win_w2, win_h2), (pr, pg, pb, 252))
    body_layer.paste(body_fill, (0, 0), body_mask)
    canvas = Image.alpha_composite(canvas, body_layer)

    # 3. 左侧热血斜切渐变色块（与主体圆角+尖角精确相交裁剪）
    grad_img = _make_vertical_gradient(win_w2, win_h2, y0, y0 + card_h, g_top, g_bot)
    slant_mask = Image.new("L", (win_w2, win_h2), 0)
    sdraw = ImageDraw.Draw(slant_mask)
    slant_top_x = x0 + left_w + slant * 0.5
    slant_bot_x = x0 + left_w - slant * 0.5
    if has_right:
        sdraw.polygon(
            [
                (x0 - 2 * ss, y0 - 2 * ss),
                (slant_top_x, y0 - 2 * ss),
                (slant_bot_x, y0 + card_h + tail_h + 2 * ss),
                (x0 - 2 * ss, y0 + card_h + tail_h + 2 * ss),
            ],
            fill=255,
        )
    else:
        sdraw.rectangle([x0 - 2 * ss, y0 - 2 * ss, x0 + card_w + 2 * ss, y0 + card_h + tail_h + 2 * ss], fill=255)
    clipped_accent_mask = ImageChops.darker(body_mask, slant_mask)
    accent_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    accent_layer.paste(grad_img, (0, 0), clipped_accent_mask)
    canvas = Image.alpha_composite(canvas, accent_layer)

    # 3b. 首选炽橙搭配冠军金 (#FFB703) 斜切高光饰条；次选宝蓝配清透天蓝 (#22A7FF) 细饰条
    if has_right and style_key in ("best", "second"):
        trim_w = (3.2 * ss) if style_key == "best" else (2.0 * ss)
        trim_rgb = _rgb_f_to_255(HUD_CHAMPION_GOLD_RGB if style_key == "best" else HUD_SKY_BLUE_RGB)
        trim_mask = Image.new("L", (win_w2, win_h2), 0)
        ImageDraw.Draw(trim_mask).polygon(
            [
                (slant_top_x - 0.5 * ss, y0 - 2 * ss),
                (slant_top_x + trim_w, y0 - 2 * ss),
                (slant_bot_x + trim_w, y0 + card_h + tail_h + 2 * ss),
                (slant_bot_x - 0.5 * ss, y0 + card_h + tail_h + 2 * ss),
            ],
            fill=255,
        )
        clipped_trim = ImageChops.darker(body_mask, trim_mask)
        trim_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
        trim_fill = Image.new("RGBA", (win_w2, win_h2), (trim_rgb[0], trim_rgb[1], trim_rgb[2], 245))
        trim_layer.paste(trim_fill, (0, 0), clipped_trim)
        canvas = Image.alpha_composite(canvas, trim_layer)

    # 3c. 右端极淡斜向漫画速度线（仅作用于右侧奶白区）
    if has_right:
        right_zone_mask = ImageChops.subtract(body_mask, slant_mask)
        canvas = _draw_right_speed_lines(canvas, right_zone_mask, x0, y0, card_w, card_h, slant, g_top, ss)

    # 4. 1.5px 深墨蓝细描边 (#142342)（统一连续轮廓，尖角无缝闭合、零锯齿）
    border_rgba = _rgba_f_to_255(border_rgba_f)
    border_w = max(3, int(round(1.5 * ss)))
    border_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(border_layer)
    outline_pts = _card_pts(x0, y0)
    bdraw.line(outline_pts + [outline_pts[0], outline_pts[1]], fill=border_rgba, width=border_w, joint="curve")
    canvas = Image.alpha_composite(canvas, border_layer)

    # 5. 左右分区粗黑体文字
    tdraw = ImageDraw.Draw(canvas)
    _draw_runs(tdraw, x0 + pad_l_x, y0, card_h, left_runs, base_px, is_black, lh, ltop)
    if has_right:
        extra_gap = (7 * ss) if style_key == "best" else (5 * ss)
        _draw_runs(tdraw, x0 + left_w + extra_gap, y0, card_h, right_runs, base_px, is_black, rh, rtop)

    # 6. LANCZOS 缩回 1× RGBA
    out = canvas.resize((win_w2 // ss, win_h2 // ss), Image.Resampling.LANCZOS)
    _BADGE_IMG_CACHE[cache_key] = out
    if len(_BADGE_IMG_CACHE) > _BADGE_IMG_CACHE_MAX:
        _BADGE_IMG_CACHE.popitem(last=False)
    return out


def render_trough_pill_rgba(
    trough_markup: str,
    dpi_scale: float = 1.0,
    gh: int = 1080,
    is_bottom: bool = False,
    focus_badge: str = "",
    char_id: Optional[int] = None,
    show_avatar: bool = True,
    avatar_base_dir: Optional[str] = None,
):
    """
    2× 超采样离屏渲染顶栏/底栏漫画贴纸状态胶囊（与角标共享 #F8FCFF 奶白底 + 1.5px #142342 细描边
    + 2px 硬偏移阴影 + 左侧斜切宝蓝标签 + 右端极淡速度线 + 最左侧动态 24×24 Q 版小头像）。
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装")

    dpi = max(0.75, min(3.0, float(dpi_scale or 1.0)))
    scale = max(0.84, min(1.30, max(360, int(gh or 1080)) / 1080.0)) * dpi
    ss = 2

    if "｜" in (trough_markup or ""):
        tag_s, body_s = trough_markup.split("｜", 1)
    else:
        plain = strip_markup_to_clean_text(trough_markup)
        tag_s, body_s = "星奈", (plain or "军师待命")
    tag_clean = _strip_unsupported_emoji(strip_markup_to_clean_text(tag_s))[:8] or "星奈"
    raw_focus = (focus_badge or "").strip()
    clean_focus = _strip_unsupported_emoji(raw_focus).strip()
    if clean_focus and not clean_focus.startswith(("★", "◆", "▸", "●", "✓")):
        clean_focus = f"◆ {clean_focus}"
    body_runs = parse_pango_runs(body_s, default_color=HUD_TEXT_INK)
    if not body_runs:
        body_runs = [("军师待命", HUD_TEXT_INK, 1.0, True)]

    norm_cid: Optional[int] = None
    if char_id is not None:
        try:
            v = int(char_id)
            if v > 0:
                norm_cid = v
        except (TypeError, ValueError):
            norm_cid = None

    cache_key = (
        "trough",
        tag_clean,
        body_s,
        round(scale, 3),
        bool(is_bottom),
        clean_focus,
        norm_cid,
        bool(show_avatar),
        avatar_base_dir or "",
    )
    cached = _BADGE_IMG_CACHE.get(cache_key)
    if cached is not None:
        _BADGE_IMG_CACHE.move_to_end(cache_key)
        return cached

    tag_px = max(14, int(round(12.2 * scale * 1.28 * ss)))
    body_px = max(14, int(round(12.6 * scale * 1.28 * ss)))

    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(probe)
    tw, th, ttop = _measure_runs(pdraw, [(tag_clean, "#FFFFFF", 1.0, True)], tag_px, black=True)
    bw, bh, btop = _measure_runs(pdraw, body_runs, body_px, black=False)
    fw, fh, ftop = (
        _measure_runs(pdraw, [(clean_focus, "#FFFFFF", 0.90, True)], tag_px, black=True)
        if clean_focus
        else (0, 0, 0)
    )

    av_px = int(round(24 * scale * ss)) if show_avatar else 0
    av_ring_w = max(3, int(round(1.5 * ss)))
    pill_h = max(th + 10 * ss, bh + 10 * ss, av_px + 8 * ss, int(round(32 * scale * ss)))
    slant = max(6 * ss, int(round(7.5 * scale * ss)))
    tag_pad_l = 9 * ss
    tag_pad_r = 9 * ss
    tag_pill_w = tw + tag_pad_l + tag_pad_r + int(round(slant * 0.45))
    focus_pill_w = (fw + 16 * ss) if clean_focus else 0
    pad_outer = 6 * ss
    av_gap = 6 * ss if show_avatar else 0
    gap = 9 * ss
    right_pad = 15 * ss if not clean_focus else 8 * ss

    pill_w = (
        pad_outer
        + (av_px + av_gap if show_avatar else 0)
        + tag_pill_w
        + gap
        + bw
        + (gap + focus_pill_w if clean_focus else 0)
        + right_pad
    )

    shadow_dx = 2 * ss
    shadow_dy = 2 * ss
    x0 = 3 * ss
    y0 = 3 * ss
    win_w2 = pill_w + x0 + 3 * ss + shadow_dx
    win_h2 = pill_h + y0 + 3 * ss + shadow_dy
    win_w2 += win_w2 % ss
    win_h2 += win_h2 % ss
    rad = min(int(round(12.0 * scale * ss)), pill_h // 2)

    pill_pts = _build_rounded_card_polygon(x0, y0, pill_w, pill_h, rad)
    shadow_pts = _build_rounded_card_polygon(x0 + shadow_dx, y0 + shadow_dy, pill_w, pill_h, rad)

    canvas = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))

    # 1. 轻巧 2px 硬偏移阴影
    sh_mask = Image.new("L", (win_w2, win_h2), 0)
    ImageDraw.Draw(sh_mask).polygon(shadow_pts, fill=255)
    sh_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    sh_layer.paste(Image.new("RGBA", (win_w2, win_h2), _rgba_f_to_255(HUD_SHADOW_RGBA)), (0, 0), sh_mask)
    canvas = Image.alpha_composite(canvas, sh_layer)

    # 2. 画纸奶白底板 (#F8FCFF / 底槽 #F5F8FF)
    body_mask = Image.new("L", (win_w2, win_h2), 0)
    ImageDraw.Draw(body_mask).polygon(pill_pts, fill=255)
    bg_rgba = (245, 248, 255, 250) if is_bottom else (248, 252, 255, 252)
    bg_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    bg_layer.paste(Image.new("RGBA", (win_w2, win_h2), bg_rgba), (0, 0), body_mask)
    canvas = Image.alpha_composite(canvas, bg_layer)

    # 3. 右端极淡斜向速度线
    is_urgent_tag = any(k in tag_clean for k in ("决赛", "比赛", "高危"))
    tag_top = (255 / 255.0, 81 / 255.0, 38 / 255.0) if is_urgent_tag else (27 / 255.0, 91 / 255.0, 255 / 255.0)
    tag_bot = (224 / 255.0, 58 / 255.0, 18 / 255.0) if is_urgent_tag else (17 / 255.0, 57 / 255.0, 166 / 255.0)
    canvas = _draw_right_speed_lines(canvas, body_mask, x0, y0, pill_w, pill_h, slant, tag_top, ss)

    # 4. 左侧斜切热血标签块（宝蓝或炽橙）
    tx = x0 + pad_outer + (av_px + av_gap if show_avatar else 0)
    ty = y0 + 4 * ss
    th_box = pill_h - 8 * ss
    tag_mask = Image.new("L", (win_w2, win_h2), 0)
    t_rad = min(int(round(6.5 * ss)), th_box // 2)
    t_draw = ImageDraw.Draw(tag_mask)
    # 左侧圆角 + 右侧斜切
    t_draw.rounded_rectangle([tx, ty, tx + tag_pill_w - slant * 0.35, ty + th_box], radius=t_rad, fill=255)
    t_draw.polygon(
        [
            (tx + tag_pill_w * 0.5, ty),
            (tx + tag_pill_w, ty),
            (tx + tag_pill_w - slant * 0.85, ty + th_box),
            (tx + tag_pill_w * 0.5, ty + th_box),
        ],
        fill=255,
    )
    t_grad = _make_vertical_gradient(win_w2, win_h2, ty, ty + th_box, tag_top, tag_bot)
    t_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    t_layer.paste(t_grad, (0, 0), tag_mask)
    canvas = Image.alpha_composite(canvas, t_layer)

    # 5. 可选右侧炽橙特化小胶囊
    if clean_focus:
        fx = x0 + pill_w - pad_outer - focus_pill_w
        f_rad = min(int(round(7.5 * ss)), th_box // 2)
        f_mask = Image.new("L", (win_w2, win_h2), 0)
        ImageDraw.Draw(f_mask).rounded_rectangle([fx, ty, fx + focus_pill_w, ty + th_box], radius=f_rad, fill=255)
        f_grad = _make_vertical_gradient(
            win_w2,
            win_h2,
            ty,
            ty + th_box,
            (255 / 255.0, 81 / 255.0, 38 / 255.0),
            (224 / 255.0, 58 / 255.0, 18 / 255.0),
        )
        f_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
        f_layer.paste(f_grad, (0, 0), f_mask)
        ImageDraw.Draw(f_layer).rounded_rectangle(
            [fx, ty, fx + focus_pill_w, ty + th_box],
            radius=f_rad,
            outline=_rgba_f_to_255(HUD_INK_STROKE_RGBA),
            width=max(2, int(round(1.1 * ss))),
        )
        canvas = Image.alpha_composite(canvas, f_layer)

    # 6. 1.5px 深墨蓝外描边 (#142342)
    border_w = max(3, int(round(1.5 * ss)))
    border_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    ImageDraw.Draw(border_layer).line(
        pill_pts + [pill_pts[0], pill_pts[1]],
        fill=_rgba_f_to_255(HUD_INK_STROKE_RGBA),
        width=border_w,
        joint="curve",
    )
    canvas = Image.alpha_composite(canvas, border_layer)

    # 7. 最左侧动态 24×24 圆形 Q 版小头像（带 1.5px #142342 环）
    if show_avatar and av_px > 0:
        av_img = load_q_avatar_rgba(
            norm_cid,
            size_px=av_px,
            base_dir=avatar_base_dir,
            ring_width_px=av_ring_w,
        )
        if av_img is not None:
            av_x = int(round(x0 + pad_outer))
            av_y = int(round(y0 + (pill_h - av_px) * 0.5))
            av_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
            av_layer.paste(av_img, (av_x, av_y), av_img)
            canvas = Image.alpha_composite(canvas, av_layer)

    # 8. 文字绘制
    tdraw = ImageDraw.Draw(canvas)
    _draw_runs(tdraw, tx + tag_pad_l, ty, th_box, [(tag_clean, "#FFFFFF", 1.0, True)], tag_px, True, th, ttop)
    _draw_runs(tdraw, tx + tag_pill_w + gap, y0, pill_h, body_runs, body_px, False, bh, btop)
    if clean_focus:
        fx = x0 + pill_w - pad_outer - focus_pill_w
        _draw_runs(tdraw, fx + 8 * ss, ty, th_box, [(clean_focus, "#FFFFFF", 0.90, True)], tag_px, True, fh, ftop)

    out = canvas.resize((win_w2 // ss, win_h2 // ss), Image.Resampling.LANCZOS)
    _BADGE_IMG_CACHE[cache_key] = out
    if len(_BADGE_IMG_CACHE) > _BADGE_IMG_CACHE_MAX:
        _BADGE_IMG_CACHE.popitem(last=False)
    return out


def _rgba_image_to_premultiplied_bgra_bytes(img) -> bytes:
    """将 Pillow RGBA 图高速转为 Win32 UpdateLayeredWindow 所需的预乘 Alpha BGRA 字节流。"""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    r_p = ImageChops.multiply(r, a)
    g_p = ImageChops.multiply(g, a)
    b_p = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b_p, g_p, r_p, a)).tobytes()


# =====================================================================
# Win32 原生 32-bit ARGB 分层穿透窗口（UpdateLayeredWindow + ULW_ALPHA）
# =====================================================================

if IS_WINDOWS:
    from ctypes import wintypes

    _LRESULT = ctypes.c_ssize_t
    _WNDPROCTYPE = ctypes.WINFUNCTYPE(
        _LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROCTYPE),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", _BITMAPINFOHEADER),
            ("bmiColors", wintypes.DWORD * 3),
        ]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        ]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _kernel32 = ctypes.windll.kernel32

    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(_POINT),
        ctypes.POINTER(_SIZE),
        wintypes.HDC,
        ctypes.POINTER(_POINT),
        wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION),
        wintypes.DWORD,
    ]
    _user32.UpdateLayeredWindow.restype = wintypes.BOOL

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteObject.restype = wintypes.BOOL

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _WS_POPUP = 0x80000000
    _WS_EX_TOPMOST = 0x00000008
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_TOOLWINDOW = 0x00000080
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_NOACTIVATE = 0x08000000
    _SW_HIDE = 0
    _SW_SHOWNOACTIVATE = 4
    _HWND_TOPMOST = wintypes.HWND(-1)
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010
    _ULW_ALPHA = 0x00000002
    _AC_SRC_OVER = 0x00
    _AC_SRC_ALPHA = 0x01
    _DIB_RGB_COLORS = 0
    _BI_RGB = 0
    _WM_NCHITTEST = 0x0084
    _HTTRANSPARENT = -1

    _WND_CLASS_NAME = "LQSNCoachLayeredBadgeClass"
    _WND_CLASS_REGISTERED = False
    _WND_PROC_REF = None

    def _badge_wnd_proc(hwnd, msg, wparam, lparam):
        if msg == _WM_NCHITTEST:
            return _HTTRANSPARENT
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _ensure_badge_window_class() -> bool:
        global _WND_CLASS_REGISTERED, _WND_PROC_REF
        if _WND_CLASS_REGISTERED:
            return True
        try:
            _WND_PROC_REF = _WNDPROCTYPE(_badge_wnd_proc)
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.lpfnWndProc = _WND_PROC_REF
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            wc.lpszClassName = _WND_CLASS_NAME
            atom = _user32.RegisterClassExW(ctypes.byref(wc))
            # atom != 0 或类已注册均视为成功
            _WND_CLASS_REGISTERED = bool(atom) or (ctypes.get_last_error() == 1410)
            return True
        except Exception:
            return False


class Win32LayeredBadgeWindow:
    """
    单枚 Win32 原生 32-bit ARGB 分层穿透角标微窗：
    - 100% 真半透明抗锯齿（UpdateLayeredWindow + ULW_ALPHA）；
    - 100% 鼠标点击穿透（WS_EX_TRANSPARENT + WM_NCHITTEST -> HTTRANSPARENT）；
    - 绝不抢占游戏焦点、绝不出现在任务栏或 Alt+Tab 列表；
    - 非 Windows 平台自动以纯无头安全桩模式工作，便于跨平台单测。
    """

    def __init__(self) -> None:
        self.hwnd: int = 0
        self._visible: bool = False
        self._closing: bool = False
        self._last_sig: Optional[tuple] = None
        self._last_box: Optional[tuple[int, int, int, int]] = None

        if IS_WINDOWS and PIL_AVAILABLE and _ensure_badge_window_class():
            ex_style = (
                _WS_EX_LAYERED
                | _WS_EX_TRANSPARENT
                | _WS_EX_TOOLWINDOW
                | _WS_EX_NOACTIVATE
                | _WS_EX_TOPMOST
            )
            hinst = _kernel32.GetModuleHandleW(None)
            hwnd = _user32.CreateWindowExW(
                ex_style,
                _WND_CLASS_NAME,
                "LQSNBadge",
                _WS_POPUP,
                -8192,
                -8192,
                16,
                16,
                None,
                None,
                hinst,
                None,
            )
            self.hwnd = int(hwnd or 0)

    def _commit_rgba_image(self, img, x: int, y: int) -> None:
        if not IS_WINDOWS or not self.hwnd or self._closing:
            return
        w, h = img.size
        bgra_bytes = _rgba_image_to_premultiplied_bgra_bytes(img)

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 自上而下 DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = _BI_RGB

        screen_dc = _user32.GetDC(None)
        if not screen_dc:
            return
        mem_dc = None
        hbitmap = None
        old_obj = None
        try:
            mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
            bits_ptr = ctypes.c_void_p()
            hbitmap = _gdi32.CreateDIBSection(
                screen_dc,
                ctypes.byref(bmi),
                _DIB_RGB_COLORS,
                ctypes.byref(bits_ptr),
                None,
                0,
            )
            if not hbitmap or not bits_ptr.value:
                return
            ctypes.memmove(bits_ptr.value, bgra_bytes, len(bgra_bytes))
            old_obj = _gdi32.SelectObject(mem_dc, hbitmap)

            pt_dst = _POINT(int(x), int(y))
            sz = _SIZE(int(w), int(h))
            pt_src = _POINT(0, 0)
            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)

            _user32.UpdateLayeredWindow(
                self.hwnd,
                screen_dc,
                ctypes.byref(pt_dst),
                ctypes.byref(sz),
                mem_dc,
                ctypes.byref(pt_src),
                0,
                ctypes.byref(blend),
                _ULW_ALPHA,
            )
            if not self._visible:
                _user32.ShowWindow(self.hwnd, _SW_SHOWNOACTIVATE)
                self._visible = True
            _user32.SetWindowPos(
                self.hwnd,
                _HWND_TOPMOST,
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        finally:
            if old_obj and mem_dc:
                _gdi32.SelectObject(mem_dc, old_obj)
            if hbitmap:
                _gdi32.DeleteObject(hbitmap)
            if mem_dc:
                _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)

    def show_spec(
        self,
        spec: dict,
        content_rect: tuple[int, int, int, int],
        *,
        dpi_scale: float = 1.0,
        left_of_box: Optional[tuple[int, int, int, int]] = None,
    ) -> Optional[tuple[int, int, int, int]]:
        if self._closing or not PIL_AVAILABLE or not content_rect:
            return None
        _gx, _gy, _gw, gh = content_rect
        img = render_badge_rgba(spec, dpi_scale=dpi_scale, gh=gh)
        pw, ph = img.size
        tx, ty, pw, ph = compute_badge_position(
            content_rect,
            pw=pw,
            ph=ph,
            rx=float(spec.get("rx", 0.5)),
            ry=float(spec.get("ry", 0.5)),
            anchor=str(spec.get("anchor", "right")),
            dx_px=int(spec.get("dx", 0)),
            dy_px=int(spec.get("dy", 0)),
            left_of_box=left_of_box,
            min_top_y=0,
        )
        sig = (
            spec.get("left"),
            spec.get("right"),
            spec.get("style"),
            bool(spec.get("tail")),
            spec.get("font_scale"),
            spec.get("char_id"),
            tx,
            ty,
            pw,
            ph,
        )
        if sig != self._last_sig or not self._visible:
            self._last_sig = sig
            self._last_box = (tx, ty, pw, ph)
            self._commit_rgba_image(img, tx, ty)
            if not IS_WINDOWS:
                self._visible = True
        return (tx, ty, pw, ph)

    def hide(self) -> None:
        if self._closing or not self._visible:
            return
        self._visible = False
        self._last_sig = None
        if IS_WINDOWS and self.hwnd:
            try:
                _user32.ShowWindow(self.hwnd, _SW_HIDE)
            except Exception:
                pass

    def destroy(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._visible = False
        if IS_WINDOWS and self.hwnd:
            try:
                _user32.ShowWindow(self.hwnd, _SW_HIDE)
                _user32.DestroyWindow(self.hwnd)
            except Exception:
                pass
            self.hwnd = 0


class Win32BadgePool:
    """复用 10 枚 Win32LayeredBadgeWindow 的角标池，支持 left_of_prev 防重叠与一键全隐。"""

    def __init__(self, size: int = 10) -> None:
        self.size = max(1, int(size))
        self.windows: list[Win32LayeredBadgeWindow] = [
            Win32LayeredBadgeWindow() for _ in range(self.size)
        ]

    def apply_specs(
        self,
        specs: list[dict],
        game_rect: Optional[tuple[int, int, int, int]],
        dpi_scale: float = 1.0,
    ) -> list[tuple[int, int, int, int]]:
        if not specs or not game_rect or not PIL_AVAILABLE:
            self.hide_all()
            return []
        content_rect = compute_game_content_box(game_rect) or game_rect
        boxes: list[tuple[int, int, int, int]] = []
        prev_box: Optional[tuple[int, int, int, int]] = None
        for idx, win in enumerate(self.windows):
            if idx < len(specs):
                sp = specs[idx]
                box = win.show_spec(
                    sp,
                    content_rect,
                    dpi_scale=dpi_scale,
                    left_of_box=(prev_box if sp.get("left_of_prev") else None),
                )
                prev_box = box
                if box:
                    boxes.append(box)
            else:
                win.hide()
        return boxes

    def hide_all(self) -> None:
        for win in self.windows:
            win.hide()

    def destroy_all(self) -> None:
        for win in self.windows:
            win.destroy()

    def get_hwnds(self) -> set[int]:
        return {w.hwnd for w in self.windows if w.hwnd}
