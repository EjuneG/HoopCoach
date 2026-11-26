#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hud_render.py — 贴身角标 HUD 离线渲染、设计 Token 对齐与 Q 版头像懒加载/降级单测
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import hud_badges
import win_diegetic_hud


def _rgb_f_to_hex(rgb_f: tuple[float, float, float]) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        int(round(rgb_f[0] * 255)),
        int(round(rgb_f[1] * 255)),
        int(round(rgb_f[2] * 255)),
    )


class TestHudDesignTokensAndSpecs(unittest.TestCase):
    def test_shared_anime_palette_tokens(self) -> None:
        # 校验核心运动色板与共享 Design Tokens 精确对齐
        self.assertEqual(_rgb_f_to_hex(hud_badges.HUD_PAPER_RGB), "#F8FCFF")
        self.assertEqual(_rgb_f_to_hex(hud_badges.HUD_INK_STROKE_RGB), "#142342")
        self.assertEqual(_rgb_f_to_hex(hud_badges.HUD_CHAMPION_GOLD_RGB), "#FFB703")
        self.assertEqual(hud_badges.HUD_TEXT_INK, "#101B33")

        best = hud_badges.ACCENT_PALETTES["best"]
        second = hud_badges.ACCENT_PALETTES["second"]
        shop = hud_badges.ACCENT_PALETTES["shop"]
        warn = hud_badges.ACCENT_PALETTES["warn"]

        self.assertEqual(_rgb_f_to_hex(best[0]), "#FF5126")
        self.assertEqual(_rgb_f_to_hex(best[1]), "#E03A12")
        self.assertEqual(_rgb_f_to_hex(best[2][:3]), "#142342")
        self.assertEqual(best[4], "#101B33")

        self.assertEqual(_rgb_f_to_hex(second[0]), "#1B5BFF")
        self.assertEqual(_rgb_f_to_hex(second[1]), "#1139A6")
        self.assertEqual(_rgb_f_to_hex(second[2][:3]), "#142342")

        self.assertEqual(_rgb_f_to_hex(shop[0]), "#12B886")
        self.assertEqual(_rgb_f_to_hex(warn[0]), "#F03E5A")

    def test_extract_active_char_id_and_specs_carry_char_id(self) -> None:
        self.assertEqual(hud_badges.extract_active_char_id({"main_char_id": 1002}), 1002)
        self.assertEqual(hud_badges.extract_active_char_id({"pre_edu_char_id": "1046"}), 1046)
        self.assertIsNone(hud_badges.extract_active_char_id({"main_char_id": 0}))
        self.assertIsNone(hud_badges.extract_active_char_id(None))

        state = {
            "main_char_id": 1046,
            "round": 8,
            "exp_round": 15,
            "round_type": 1,
            "ps": 82,
            "gold": 120,
            "props": {101: 600, 102: 600, 103: 600, 104: 600, 105: 600, 106: 600},
            "prop_max": {101: 1600, 102: 1600, 103: 1600, 104: 1600, 105: 1600, 106: 1600},
        }
        scored = [
            {"idx": 1, "name": "1号·投射", "score": 165.0, "hud_tags": ["哨冲"], "cmd": {"ps": -18}},
            {"idx": 3, "name": "3号·防守", "score": 152.0, "hud_tags": [], "cmd": {"ps": -18}},
        ]
        specs, trough = hud_badges.build_badges_for_mode(
            "ycjm01",
            state,
            hud_config={"recommendations": {"train": True, "shop": False, "secondary": True}},
            last_turn_scored=scored,
        )
        self.assertGreaterEqual(len(specs), 2)
        for sp in specs:
            self.assertEqual(sp.get("char_id"), 1046)

        trough_spec = hud_badges.build_trough_spec(trough, char_id=1046, focus_badge="偏投射")
        self.assertEqual(trough_spec["char_id"], 1046)
        self.assertEqual(trough_spec["focus_badge"], "偏投射")

    def test_no_dev_jargon_in_match_or_trough_copy(self) -> None:
        forbidden = ("纯只读", "零侵入", "Pillow", "Win32", "免命令行", "➔", "▸")
        state = {
            "main_char_id": 1002,
            "round": 28,
            "exp_round": 50,
            "gold": 180,
            "store_items": [{"id": 1101, "num": 1, "price": 0}],
        }
        specs, trough = hud_badges.build_badges_for_mode(
            "match_ready",
            state,
            hud_config={"recommendations": {"train": True, "shop": True}},
        )
        combined = trough + " " + " ".join(f"{s.get('left', '')} {s.get('right', '')}" for s in specs)
        for bad in forbidden:
            self.assertNotIn(bad, combined)
        self.assertEqual(win_diegetic_hud._strip_unsupported_emoji("扫完商店 ▸ 开赛 ➔ 🎯"), "扫完商店 ▶ 开赛 → ")


@unittest.skipUnless(win_diegetic_hud.PIL_AVAILABLE, "当前环境未安装 Pillow")
class TestHudOfflineRenderingAndAvatars(unittest.TestCase):
    def setUp(self) -> None:
        win_diegetic_hud.clear_render_caches()

    def test_render_badge_rgba_all_styles_and_tail(self) -> None:
        cases = [
            {"left": "★ EV 168", "right": "投射<span size='88%' foreground='#E03A12'> · 哨冲</span>", "style": "best", "tail": True, "char_id": 1002},
            {"left": "EV 154", "right": "防守", "style": "second", "tail": True},
            {"left": "商店", "right": "0金免费领", "style": "shop", "tail": False},
            {"left": "控体", "right": "退回休息更赚", "style": "warn", "tail": True},
            {"left": "跳过", "right": "职责不符", "style": "muted", "tail": False},
            {"left": "单区高光", "right": "", "style": "best", "tail": False},
        ]
        for sp in cases:
            img = win_diegetic_hud.render_badge_rgba(sp, dpi_scale=1.0, gh=1080)
            self.assertEqual(img.mode, "RGBA")
            w, h = img.size
            self.assertGreater(w, 48)
            self.assertGreater(h, 24)
            # 左上角留白透明，中心不透明
            self.assertEqual(img.getpixel((0, 0))[3], 0)
            self.assertGreater(img.getpixel((w // 2, h // 2))[3], 230)

        # 验证相同 spec 命中内存 LRU 缓存
        im1 = win_diegetic_hud.render_badge_rgba(cases[0], dpi_scale=1.0, gh=1080)
        im2 = win_diegetic_hud.render_badge_rgba(cases[0], dpi_scale=1.0, gh=1080)
        self.assertIs(im1, im2)

        # 验证带底部尖角比无尖角略高，且尖角底部中点非透明
        no_tail = win_diegetic_hud.render_badge_rgba({"left": "★ 首选", "right": "投射", "style": "best", "tail": False})
        with_tail = win_diegetic_hud.render_badge_rgba({"left": "★ 首选", "right": "投射", "style": "best", "tail": True})
        self.assertGreater(with_tail.size[1], no_tail.size[1])
        tw, th = with_tail.size
        # 尖角尖端附近应存在实心像素
        tip_col_alphas = [with_tail.getpixel((tw // 2, y))[3] for y in range(th - 10, th)]
        self.assertGreater(max(tip_col_alphas), 180)

    def test_q_avatar_lazy_load_lru_cache_and_fallback(self) -> None:
        # 1. 仓库内置主练角色 1002（龙宫寺）与 1046（希雅）命中 data/hoopgirls/heads/q_head_s_<id>_*.png
        p1002 = hud_badges.resolve_q_avatar_path(1002)
        p1046 = hud_badges.resolve_q_avatar_path(1046)
        self.assertIsNotNone(p1002)
        self.assertIsNotNone(p1046)
        self.assertIn("q_head_s_1002", os.path.basename(p1002))
        self.assertIn("q_head_s_1046", os.path.basename(p1046))

        # 2. 未知角色 / 缺省 char_id 平滑降级到默认 1001（澪奈）
        p_def = hud_badges.resolve_q_avatar_path(None)
        p_unk = hud_badges.resolve_q_avatar_path(99999)
        self.assertIsNotNone(p_def)
        self.assertIn("1001", os.path.basename(p_def))
        self.assertEqual(p_def, p_unk)

        # 3. 懒加载 + LRU 缓存命中同一对象
        av1 = win_diegetic_hud.load_q_avatar_rgba(1002, size_px=48)
        av2 = win_diegetic_hud.load_q_avatar_rgba(1002, size_px=48)
        self.assertIsNotNone(av1)
        self.assertIs(av1, av2)
        self.assertEqual(av1.size, (48, 48))
        self.assertEqual(av1.getpixel((0, 0))[3], 0)
        self.assertGreater(av1.getpixel((24, 24))[3], 240)

        # 4. 仅存在 packaging/assets/site/q_<id>.png 时的二级回退路径
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix="hoopcoach_site_q_") as tmp_dir:
            site_dir = os.path.join(tmp_dir, "packaging", "assets", "site")
            os.makedirs(site_dir, exist_ok=True)
            site_png = os.path.join(site_dir, "q_1099.png")
            Image.new("RGBA", (64, 64), (255, 81, 38, 255)).save(site_png)
            resolved = hud_badges.resolve_q_avatar_path(1099, base_dir=tmp_dir, default_char_id=None)
            self.assertEqual(resolved, site_png)
            av_site = win_diegetic_hud.load_q_avatar_rgba(1099, size_px=48, base_dir=tmp_dir, default_char_id=None)
            self.assertIsNotNone(av_site)
            self.assertEqual(av_site.size, (48, 48))

        # 5. 全目录缺失或图片损坏时平滑降级为几何星徽，绝不抛异常
        with tempfile.TemporaryDirectory(prefix="hoopcoach_empty_av_") as empty_dir:
            self.assertIsNone(hud_badges.resolve_q_avatar_path(1002, base_dir=empty_dir))
            emblem = win_diegetic_hud.load_q_avatar_rgba(1002, size_px=48, base_dir=empty_dir)
            self.assertIsNotNone(emblem)
            self.assertEqual(emblem.size, (48, 48))
            self.assertGreater(emblem.getpixel((24, 24))[3], 240)

            # 损坏 PNG 文件也不崩溃
            bad_heads = os.path.join(empty_dir, "data", "hoopgirls", "heads")
            os.makedirs(bad_heads, exist_ok=True)
            with open(os.path.join(bad_heads, "q_head_s_1002_bad.png"), "wb") as f:
                f.write(b"not-a-valid-png")
            win_diegetic_hud.clear_render_caches()
            emblem_bad = win_diegetic_hud.load_q_avatar_rgba(1002, size_px=48, base_dir=empty_dir)
            self.assertIsNotNone(emblem_bad)
            self.assertEqual(emblem_bad.size, (48, 48))

    def test_load_q_avatar_no_pillow_graceful_none(self) -> None:
        orig = win_diegetic_hud.PIL_AVAILABLE
        win_diegetic_hud.PIL_AVAILABLE = False
        try:
            self.assertIsNone(win_diegetic_hud.load_q_avatar_rgba(1002, size_px=48))
        finally:
            win_diegetic_hud.PIL_AVAILABLE = orig

    def test_render_trough_pill_rgba_with_and_without_char_id(self) -> None:
        img_ryu = win_diegetic_hud.render_trough_pill_rgba(
            "14训｜哨子 <span foreground='#1B5BFF'>68/100</span> · 下回彩圈留体",
            char_id=1002,
            focus_badge="🎯偏投射×1.15",
        )
        img_xia = win_diegetic_hud.render_trough_pill_rgba(
            "选技(外线)｜首选：精准三分 · 契合度 S+",
            is_bottom=True,
            char_id=1046,
        )
        img_def = win_diegetic_hud.render_trough_pill_rgba(
            "待命｜进入选人或育成自动贴标",
            char_id=None,
        )
        for im in (img_ryu, img_xia, img_def):
            self.assertEqual(im.mode, "RGBA")
            self.assertGreater(im.size[0], 180)
            self.assertGreaterEqual(im.size[1], 32)
            self.assertEqual(im.getpixel((0, 0))[3], 0)
            self.assertGreater(im.getpixel((im.size[0] // 2, im.size[1] // 2))[3], 230)

        # 缺失素材目录也能无错渲染带几何星徽的胶囊
        with tempfile.TemporaryDirectory(prefix="hoopcoach_trough_fallback_") as empty_dir:
            img_fb = win_diegetic_hud.render_trough_pill_rgba(
                "01训｜开局参谋就绪",
                char_id=1002,
                avatar_base_dir=empty_dir,
            )
            self.assertEqual(img_fb.mode, "RGBA")
            self.assertGreater(img_fb.size[0], 150)


if __name__ == "__main__":
    unittest.main()
