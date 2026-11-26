#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_icon.py — 可复现生成 HoopCoach 专属篮球主题多尺寸程序图标 (.ico)

输出：
  packaging/assets/hoopcoach.ico（含 16 / 32 / 48 / 64 / 128 / 256 px 六档尺寸）
"""

from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw, ImageFilter

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_ICO = os.path.join(ROOT_DIR, "packaging", "assets", "hoopcoach.ico")


def _draw_star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r_outer: float, r_inner: float, fill: tuple) -> None:
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = r_outer if i % 2 == 0 else r_inner
        pts.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r))
    draw.polygon(pts, fill=fill)


def render_icon_master(size: int = 1024) -> Image.Image:
    """4x 超采样渲染清爽高辨识度的深海蓝徽章 + 暖橙篮球 + 星奈金星图标。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(size * 0.055)
    radius = int(size * 0.225)
    box = [pad, pad, size - pad, size - pad]

    # 1. 圆角深海蓝底板（纵向微渐变）
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)

    grad = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    top_col = (15, 23, 42)      # #0F172A
    mid_col = (23, 37, 84)      # #172554
    bot_col = (12, 74, 110)     # #0C4A6E
    for y in range(pad, size - pad + 1):
        t = (y - pad) / max(1, (size - 2 * pad))
        if t < 0.55:
            k = t / 0.55
            c = tuple(int(top_col[i] + (mid_col[i] - top_col[i]) * k) for i in range(3))
        else:
            k = (t - 0.55) / 0.45
            c = tuple(int(mid_col[i] + (bot_col[i] - mid_col[i]) * k) for i in range(3))
        gdraw.line([(pad, y), (size - pad, y)], fill=(*c, 255))

    img.paste(grad, (0, 0), mask)

    # 2. 徽章外圈细描边（青蓝微光）
    border_w = max(4, int(size * 0.022))
    draw.rounded_rectangle(box, radius=radius, outline=(56, 189, 248, 220), width=border_w)

    # 3. 篮球主体投影
    cx = size * 0.48
    cy = size * 0.53
    br = size * 0.325
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    sdraw.ellipse(
        [cx - br, cy - br + size * 0.028, cx + br, cy + br + size * 0.028],
        fill=(2, 6, 23, 150),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=size * 0.026))
    img = Image.alpha_composite(img, shadow_layer)

    # 4. 篮球球体（径向光照渐变：左上高光暖橙 -> 右下深橙）
    ball_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ball_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(ball_mask).ellipse([cx - br, cy - br, cx + br, cy + br], fill=255)

    # 使用同心椭圆近似平滑径向高光（纯 Pillow 极速无像素循环）
    bdraw = ImageDraw.Draw(ball_layer)
    bdraw.ellipse([cx - br, cy - br, cx + br, cy + br], fill=(217, 78, 18, 255))
    hl_cx = cx - br * 0.28
    hl_cy = cy - br * 0.30
    steps = 28
    for i in range(steps):
        frac = 1.0 - i / steps
        r = br * (1.28 * frac)
        t = i / (steps - 1)
        cr = int(217 + (253 - 217) * t)
        cg = int(78 + (186 - 78) * t)
        cb = int(18 + (116 - 18) * t)
        bdraw.ellipse([hl_cx - r, hl_cy - r, hl_cx + r, hl_cy + r], fill=(cr, cg, cb, 255))

    # 5. 篮球经典黑色筋条（裁剪在球体内部）
    seam_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    se_draw = ImageDraw.Draw(seam_layer)
    seam_col = (41, 18, 8, 235)
    sw = max(6, int(size * 0.028))

    # 轻微倾斜角度感：横筋 + 竖筋 + 左右弧筋
    se_draw.line([(cx - br * 1.1, cy), (cx + br * 1.1, cy)], fill=seam_col, width=sw)
    se_draw.line([(cx, cy - br * 1.1), (cx, cy + br * 1.1)], fill=seam_col, width=sw)

    # 左侧弧线（向右凸的经典篮球侧筋，与中竖线留出饱满间距）
    arc_r = br * 0.80
    left_arc_cx = cx - br * 1.24
    se_draw.ellipse(
        [left_arc_cx - arc_r, cy - arc_r, left_arc_cx + arc_r, cy + arc_r],
        outline=seam_col,
        width=sw,
    )
    # 右侧弧线（向左凸的经典篮球侧筋，与左侧完全对称）
    right_arc_cx = cx + br * 1.24
    se_draw.ellipse(
        [right_arc_cx - arc_r, cy - arc_r, right_arc_cx + arc_r, cy + arc_r],
        outline=seam_col,
        width=sw,
    )

    ball_layer = Image.alpha_composite(ball_layer, seam_layer)
    # 外轮廓清晰描边，保证 16px 小尺寸下同样锐利
    bdraw = ImageDraw.Draw(ball_layer)
    bdraw.ellipse(
        [cx - br, cy - br, cx + br, cy + br],
        outline=(67, 20, 7, 240),
        width=max(5, int(size * 0.022)),
    )

    img.paste(ball_layer, (0, 0), ball_mask)

    # 6. 右上角「星奈」闪亮金星徽记（带微光）
    star_cx = size * 0.77
    star_cy = size * 0.24
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _draw_star(ImageDraw.Draw(glow), star_cx, star_cy, size * 0.155, size * 0.068, (250, 204, 21, 180))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size * 0.024))
    img = Image.alpha_composite(img, glow)

    draw_top = ImageDraw.Draw(img)
    _draw_star(draw_top, star_cx, star_cy, size * 0.135, size * 0.058, (254, 240, 138, 255))
    _draw_star(draw_top, star_cx, star_cy, size * 0.095, size * 0.042, (255, 251, 235, 255))

    return img


def generate_ico(out_path: str = OUT_ICO) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    master = render_icon_master(1024)
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(out_path, format="ICO", sizes=sizes)
    return out_path


if __name__ == "__main__":
    p = generate_ico()
    print(f"✅ 已生成多尺寸程序图标: {p} ({os.path.getsize(p)} bytes)")
