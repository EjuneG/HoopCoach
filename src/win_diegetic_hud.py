#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
win_diegetic_hud.py — Windows 方案 A 抗锯齿贴身角标渲染器（Pillow 2× 超采样 + Win32 UpdateLayeredWindow）

核心特性：
1. 纯离线可测渲染层（零 Win32 依赖）：
   - `render_badge_rgba(spec, dpi_scale=1.0, gh=1080) -> PIL.Image.Image`
   - `render_trough_pill_rgba(trough_markup, dpi_scale=1.0, gh=1080, is_bottom=False) -> PIL.Image.Image`
   - 2× 超采样绘制奶白圆角主体 + 柔和投影 + 左侧斜切渐变色块 + 干净描边 + 可选底部尖角 + Pango 多色粗体字，
     经 LANCZOS 缩回 1× RGBA，得到商业级无锯齿半透明贴图，内置内存 LRU 缓存避免重复重绘；
2. Win32 原生 32-bit ARGB 分层穿透窗口复用池（`Win32LayeredBadgeWindow` / `Win32BadgePool`）：
   - 严格 64-bit 安全 `ctypes.windll.user32` / `gdi32` 接口声明；
   - `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST`；
   - 预乘 Alpha BGRA DIB Section + `UpdateLayeredWindow(..., ULW_ALPHA)` 原子提交：
     100% 真半透明抗锯齿、零闪烁、100% 鼠标点击穿透、绝不抢走游戏焦点；
   - 支持 `left_of_prev` 自动防重叠并排摆放，以及游戏切后台/最小化/过场秒隐身。
"""

from __future__ import annotations

from collections import OrderedDict
import ctypes
import html
import os
import re
import sys
from typing import Optional

from hud_badges import (
    ACCENT_PALETTES,
    compute_badge_position,
    compute_game_content_box,
    resolve_badge_markups,
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
    """剔除微软雅黑/NotoSansCJK 等 TrueType 字体不含字形的彩色 Emoji（完整保留 ★ ◆ ➔ · 等实心符号），杜绝豆腐块。"""
    if not text:
        return ""
    return _EMOJI_STRIP_RE.sub("", text)


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


# =====================================================================
# 方案 A 离屏 2× 超采样抗锯齿渲染（纯 Pillow，Linux / Windows 通用可测）
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
      - tail: bool，是否在底部绘制指向小尖角
      - font_scale: float，相对字号缩放倍率（默认 1.0）
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装，无法使用方案 A 渲染抗锯齿角标")

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

    cache_key = (lm, rm, style_key, with_tail, f_pt)
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
    pad_r_x = max(11 * ss, int(round(f_pt * 0.92 * ss))) if has_right else pad_l_x
    pad_y = max(5 * ss, int(round(f_pt * 0.40 * ss)))

    card_h = max(lh, rh, int(round(f_pt * 1.35 * ss))) + pad_y * 2
    if has_right:
        left_w = int(round(lw + pad_l_x * 2 + slant * 0.45))
        card_w = int(round(left_w + rw + pad_r_x + 6 * ss))
    else:
        left_w = int(round(lw + pad_l_x * 2))
        card_w = left_w

    tail_h = (6 * ss) if with_tail else 0
    tail_w = 12 * ss
    margin_x = 4 * ss
    margin_top = 3 * ss
    margin_bot = 6 * ss

    win_w2 = max(44 * ss, card_w + margin_x * 2)
    win_h2 = max(26 * ss, card_h + tail_h + margin_top + margin_bot)
    # 保证 2× 宽高为偶数，缩回 1× 无半像素抖动
    win_w2 += win_w2 % ss
    win_h2 += win_h2 % ss

    x0 = margin_x
    y0 = margin_top
    rad = min(int(round(10.5 * ss)), card_h // 2, card_w // 2)
    tail_cx = x0 + card_w // 2
    tx0 = max(x0 + rad + 3 * ss, min(x0 + card_w - rad - tail_w - 3 * ss, tail_cx - tail_w // 2))
    tx1 = tx0 + tail_w
    tx_mid = (tx0 + tx1) // 2

    def _draw_card_shape_mask(ox: int, oy: int) -> Image.Image:
        m = Image.new("L", (win_w2, win_h2), 0)
        md = ImageDraw.Draw(m)
        md.rounded_rectangle([ox, oy, ox + card_w, oy + card_h], radius=rad, fill=255)
        if tail_h > 0:
            md.polygon([(tx0, oy + card_h - 1), (tx_mid, oy + card_h + tail_h), (tx1, oy + card_h - 1)], fill=255)
        return m

    canvas = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))

    # 1. 柔和圆角底投影（轻微高斯模糊）
    shadow_mask = _draw_card_shape_mask(x0, y0 + int(round(2.8 * ss)))
    shadow_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    shadow_fill = Image.new("RGBA", (win_w2, win_h2), (20, 36, 58, 66))
    shadow_layer.paste(shadow_fill, (0, 0), shadow_mask)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=1.8 * ss))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    # 2. 清爽奶白底板 (#F8FCFF)
    body_mask = _draw_card_shape_mask(x0, y0)
    body_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    body_fill = Image.new("RGBA", (win_w2, win_h2), (248, 252, 255, 250))
    body_layer.paste(body_fill, (0, 0), body_mask)
    canvas = Image.alpha_composite(canvas, body_layer)

    # 3. 左侧斜切活力渐变色块（与主体圆角+尖角精确相交裁剪）
    grad_img = _make_vertical_gradient(win_w2, win_h2, y0, y0 + card_h, g_top, g_bot)
    slant_mask = Image.new("L", (win_w2, win_h2), 0)
    sdraw = ImageDraw.Draw(slant_mask)
    if has_right:
        sdraw.polygon(
            [
                (x0 - 2 * ss, y0 - 2 * ss),
                (x0 + left_w + slant * 0.5, y0 - 2 * ss),
                (x0 + left_w - slant * 0.5, y0 + card_h + tail_h + 2 * ss),
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

    # 4. 干净外描边（若带底部尖角，沿尖角开口做平滑 V 形闭合描边，绝不横切尖角）
    border_rgba = (
        int(round(border_rgba_f[0] * 255)),
        int(round(border_rgba_f[1] * 255)),
        int(round(border_rgba_f[2] * 255)),
        int(round(border_rgba_f[3] * 255)),
    )
    border_w = max(2, int(round(2.0 * ss)))
    border_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(border_layer)
    bdraw.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=rad, outline=border_rgba, width=border_w)
    if tail_h > 0:
        # 抹去尖角开口内侧的水平底边线段，再用平滑折线补齐尖角 V 形外轮廓并自然衔接左右底边
        bdraw.rectangle(
            [tx0 + border_w, y0 + card_h - border_w - 1, tx1 - border_w, y0 + card_h + 2],
            fill=(0, 0, 0, 0),
        )
        bdraw.line(
            [
                (tx0 - 1, y0 + card_h - border_w // 2),
                (tx_mid, y0 + card_h + tail_h),
                (tx1 + 1, y0 + card_h - border_w // 2),
            ],
            fill=border_rgba,
            width=border_w,
            joint="curve",
        )
    canvas = Image.alpha_composite(canvas, border_layer)

    # 5. 左右分区粗黑体文字
    tdraw = ImageDraw.Draw(canvas)
    _draw_runs(tdraw, x0 + pad_l_x, y0, card_h, left_runs, base_px, is_black, lh, ltop)
    if has_right:
        _draw_runs(tdraw, x0 + left_w + 5 * ss, y0, card_h, right_runs, base_px, is_black, rh, rtop)

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
):
    """
    2× 超采样离屏渲染顶栏第 4 原生冰灰状态胶囊（复刻 Linux FloatingHUD 原生顶栏槽），
    可用于离线预览验收或分层窗口展示。
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装")

    dpi = max(0.75, min(3.0, float(dpi_scale or 1.0)))
    scale = max(0.84, min(1.30, max(360, int(gh or 1080)) / 1080.0)) * dpi
    ss = 2
    tag_px = max(14, int(round(12.6 * scale * 1.28 * ss)))
    body_px = max(14, int(round(12.8 * scale * 1.28 * ss)))

    if "｜" in (trough_markup or ""):
        tag_s, body_s = trough_markup.split("｜", 1)
    else:
        plain = strip_markup_to_clean_text(trough_markup)
        tag_s, body_s = "星奈", (plain or "军师待命")
    tag_clean = _strip_unsupported_emoji(strip_markup_to_clean_text(tag_s))[:8] or "星奈"
    focus_badge = _strip_unsupported_emoji(focus_badge or "").strip()
    body_runs = parse_pango_runs(body_s, default_color="#253B52")
    if not body_runs:
        body_runs = [("军师待命", "#253B52", 1.0, True)]

    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(probe)
    tw, th, ttop = _measure_runs(pdraw, [(tag_clean, "#FFFFFF", 1.0, True)], tag_px, black=True)
    bw, bh, btop = _measure_runs(pdraw, body_runs, body_px, black=False)
    fw, fh, ftop = (
        _measure_runs(pdraw, [(focus_badge, "#FFFFFF", 0.92, True)], tag_px, black=True)
        if focus_badge
        else (0, 0, 0)
    )

    pill_h = max(th, bh, int(round(28 * scale * ss))) + 10 * ss
    tag_pad_x = 10 * ss
    tag_pill_w = tw + tag_pad_x * 2
    focus_pill_w = (fw + 16 * ss) if focus_badge else 0
    gap = 10 * ss
    pad_outer = 6 * ss
    pill_w = pad_outer + tag_pill_w + gap + bw + (gap + focus_pill_w if focus_badge else 0) + 14 * ss

    win_w2 = pill_w + 8 * ss
    win_h2 = pill_h + 8 * ss
    win_w2 += win_w2 % ss
    win_h2 += win_h2 % ss
    x0, y0 = 4 * ss, 4 * ss
    rad = pill_h // 2

    canvas = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    bg_rgba = (234, 243, 250, 245) if is_bottom else (216, 229, 237, 250)
    draw.rounded_rectangle(
        [x0, y0, x0 + pill_w, y0 + pill_h],
        radius=rad,
        fill=bg_rgba,
        outline=(165, 192, 210, 170),
        width=max(2, int(round(1.3 * ss))),
    )

    # 左侧天蓝小胶囊标签
    tx, ty = x0 + pad_outer, y0 + 4 * ss
    th_box = pill_h - 8 * ss
    tag_mask = Image.new("L", (win_w2, win_h2), 0)
    ImageDraw.Draw(tag_mask).rounded_rectangle([tx, ty, tx + tag_pill_w, ty + th_box], radius=th_box // 2, fill=255)
    t_grad = _make_vertical_gradient(
        win_w2,
        win_h2,
        ty,
        ty + th_box,
        (68 / 255.0, 178 / 255.0, 248 / 255.0),
        (45 / 255.0, 158 / 255.0, 236 / 255.0),
    )
    t_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
    t_layer.paste(t_grad, (0, 0), tag_mask)
    canvas = Image.alpha_composite(canvas, t_layer)

    # 可选右侧橙色偏科小胶囊
    if focus_badge:
        fx = x0 + pill_w - pad_outer - focus_pill_w
        f_mask = Image.new("L", (win_w2, win_h2), 0)
        ImageDraw.Draw(f_mask).rounded_rectangle([fx, ty, fx + focus_pill_w, ty + th_box], radius=th_box // 2, fill=255)
        f_grad = _make_vertical_gradient(
            win_w2,
            win_h2,
            ty,
            ty + th_box,
            (245 / 255.0, 158 / 255.0, 11 / 255.0),
            (217 / 255.0, 119 / 255.0, 6 / 255.0),
        )
        f_layer = Image.new("RGBA", (win_w2, win_h2), (0, 0, 0, 0))
        f_layer.paste(f_grad, (0, 0), f_mask)
        canvas = Image.alpha_composite(canvas, f_layer)

    tdraw = ImageDraw.Draw(canvas)
    _draw_runs(tdraw, tx + tag_pad_x, ty, th_box, [(tag_clean, "#FFFFFF", 1.0, True)], tag_px, True, th, ttop)
    _draw_runs(tdraw, tx + tag_pill_w + gap, y0, pill_h, body_runs, body_px, False, bh, btop)
    if focus_badge:
        fx = x0 + pill_w - pad_outer - focus_pill_w
        _draw_runs(tdraw, fx + 8 * ss, ty, th_box, [(focus_badge, "#FFFFFF", 0.92, True)], tag_px, True, fh, ftop)

    return canvas.resize((win_w2 // ss, win_h2 // ss), Image.Resampling.LANCZOS)


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
        sig = (id(img), tx, ty, pw, ph)
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
