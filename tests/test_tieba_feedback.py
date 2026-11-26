#!/usr/bin/env python3
"""
回归测试：2026-11-23 贴吧社区情报 Fact-Check 4 项反哺升级验证
覆盖：
1. note_advisor：自动 5 把刷彩笔记「原生同位置+最高契合」种马推荐 + 修订避坑铁律
2. equipment_advisor：冲评分向 vs PVP 实战偏科集中 (2+2 / 3+1) 双视角与阵型诊断
3. live_coach：5 人阵容流派感知 (训练流零副作用 / 白神商店流 / 野上+羽濑川扭蛋流 / 小游戏流)
4. verified_mechanics：build_mechanics_db 新机制入库验证
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

import equipment_advisor as eq_adv
import live_coach as lc
import note_advisor as na
import skill_advisor as sa


class TestTiebaFeedbackUpgrades(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chars, cls.rules, cls.rel_scores, cls.entries_catalog = na.load_static_db()
        cls.strategy = lc.load_strategy("training_flow")

    def test_note_auto_breeder_same_pos_and_revision_warning(self):
        """验证自动训练刷彩种马严格筛选原生同位置、数值严格匹配 rel_scores，并带修订避坑提示。"""
        pool = {
            "owned_actors": [1002, 1020, 1010, 1022, 1030, 1007],
            "own_notes": {},
            "borrowed_notes": {},
            "min_kept_rank": 5,
        }
        # 龙宫寺 1002 是 PF 大前
        ab = na.recommend_auto_train_breeders(1002, self.chars, self.rel_scores, pool, top_k=5)
        self.assertEqual(ab["pos_code"], "PF")
        self.assertTrue(len(ab["same_pos_all"]) >= 3)
        self.assertTrue(len(ab["same_pos_owned"]) >= 2)
        for item in ab["same_pos_all"]:
            self.assertNotEqual(item["actor_id"], 1002)
            self.assertTrue(item["best_pos"].startswith("PF"))
            expected_rel = int(self.rel_scores[1002].get(item["actor_id"], 0))
            self.assertEqual(item["rel_to_core"], expected_rel)

        # 确保排序按 rel_to_core 非递增
        rels = [x["rel_to_core"] for x in ab["same_pos_all"]]
        self.assertEqual(rels, sorted(rels, reverse=True))

        # recommend_notes_for_character 与终端报告均携带自动刷彩与修订避坑
        adv = na.recommend_notes_for_character(1002, self.chars, self.rules, self.rel_scores, pool)
        self.assertIn("auto_breeders", adv)
        self.assertIn("仅保留共同拥有的词条", adv["revision_warning"])
        txt = na.format_character_note_advice_terminal(adv)
        self.assertIn("自动训练 5 把刷彩笔记最佳种马", txt)
        self.assertIn("笔记修订避坑", txt)

    def test_equipment_dual_perspective_score_vs_pvp(self):
        """验证器材参谋冲评分默认模式不变，且能给出 2+2 / 3+1 实战偏科方案与阵型诊断。"""
        rec_score = eq_adv.recommend_equipments_for_character(1002, mode="score")
        rec_22 = eq_adv.recommend_equipments_for_character(1002, mode="pvp_22")
        rec_31 = eq_adv.recommend_equipments_for_character(1002, mode="pvp_31")

        self.assertEqual(rec_score["mode"], "score")
        self.assertEqual(len(rec_score["bis_loadout"]), 5)
        self.assertEqual(int(rec_score["bis_loadout"][0]["equip"]["id"]), 11004)

        # 2+2 与 3+1 方案必须均为 5 卡（含哨子）且金技能全部生效
        for rec in (rec_22, rec_31):
            self.assertEqual(len(rec["bis_loadout"]), 5)
            self.assertEqual(int(rec["bis_loadout"][0]["equip"]["id"]), 11004)
            for card in rec["bis_loadout"][1:]:
                self.assertTrue(card["skill_eval"]["active"])

        self.assertEqual(rec_22["pvp_22_pattern"]["archetype"], "pvp_22")
        self.assertEqual(rec_31["pvp_31_pattern"]["archetype"], "pvp_31")

        report = eq_adv.format_terminal_report(1002)
        self.assertIn("PVP 实战偏科集中配装参考", report)
        self.assertIn("2+2 双核专精", report)
        self.assertIn("3+1 极攻暴击", report)
        self.assertNotIn("<span", report)

        hud_markup, icons = eq_adv.format_equip_advice_hud_markup(1002)
        self.assertIn("阵型：", hud_markup)
        self.assertTrue(len(icons) >= 4)

    def _make_weekend_state(self, main_id: int, teammates: list[int], gold: int = 160, gashapons=None, store_items=None):
        st = lc.make_initial_state()
        st["main_char_id"] = main_id
        st["teammate_char_ids"] = list(teammates)
        st["round"] = 13
        st["exp_round"] = 13  # 第2周周六
        st["weekend_day"] = 1
        st["ps"] = 110
        st["ps_max"] = 130
        st["gold"] = gold
        st["props"] = {101: 600, 102: 600, 103: 600, 104: 600, 105: 600, 106: 600}
        st["prop_max"] = {101: 1650, 102: 1650, 103: 1650, 104: 1650, 105: 1650, 106: 1650}
        st["gashapon_list"] = gashapons or [
            {"idx": 1, "cfg_id": 1001, "price": 80, "mates": 0},
            {"idx": 2, "cfg_id": 1002, "price": 60, "mates": 0},
            {"idx": 3, "cfg_id": 1003, "price": 60, "mates": 0},
        ]
        st["minigame_list"] = [
            {"idx": 1, "game_id": 2, "price": 80, "mates": 0},
            {"idx": 2, "game_id": 3, "price": 100, "mates": 0},
            {"idx": 3, "game_id": 1, "price": 60, "mates": 0},
        ]
        st["store_items"] = store_items or [
            {"id": 1101, "num": 1, "price": 40},  # 属性书 (+20投射)
        ]
        st["weekend_selections"] = {
            19: {"sel_id": 19, "ps_cost": 0, "base_gold": 70, "tips_gold": 20, "total_gold": 90, "status": 1},
            20: {"sel_id": 20, "ps_cost": -25, "base_gold": 120, "tips_gold": 30, "total_gold": 150, "status": 1},
            17: {"sel_id": 17, "status": 1},
            6: {"sel_id": 6, "status": 1},
            13: {"sel_id": 13, "status": 1},
            14: {"sel_id": 14, "status": 1},
        }
        return st

    def test_live_coach_archetype_detection_and_weekend_tuning(self):
        """验证流派自动识别（训练流中性零副作用 / 白神商店流 / 野上+羽濑川扭蛋流 / 小游戏流）与周末评估联动。"""
        # 1. 经典训练流（真崎/闪堂/百目鬼/龙宫寺/希雅）-> 全中性
        st_train = self._make_weekend_state(1002, [1030, 1007, 1013, 1046])
        arch_train = lc.detect_lineup_archetype(st_train, self.strategy)
        self.assertEqual(arch_train["archetype"], "training")
        self.assertEqual(arch_train["keep_gold_mult"], 1.0)
        self.assertEqual(arch_train["work_ev_bonus"], 0.0)
        self.assertEqual(arch_train["gasha_ev_mult"], 1.0)
        self.assertFalse(arch_train["shop_stat_weekend_buy"])

        # 2. 白神商店流（白神1045 + 羽田1011 + 清水1023 + 野上1037，练龙宫寺1002）
        st_store = self._make_weekend_state(1002, [1045, 1011, 1023, 1037])
        arch_store = lc.detect_lineup_archetype(st_store, self.strategy)
        self.assertEqual(arch_store["archetype"], "store")
        self.assertTrue(arch_store["shop_stat_weekend_buy"])
        self.assertGreater(arch_store["keep_gold_mult"], 1.2)
        self.assertGreater(arch_store["work_ev_bonus"], 10.0)
        self.assertLess(arch_store["gasha_ev_mult"], 1.0)

        # 3. 野上+羽濑川扭蛋流（野上1037 + 羽濑川1029 + 千圣1021 + 先斗1032）
        st_gacha = self._make_weekend_state(1002, [1037, 1029, 1021, 1032])
        arch_gacha = lc.detect_lineup_archetype(st_gacha, self.strategy)
        self.assertEqual(arch_gacha["archetype"], "gacha")
        self.assertGreater(arch_gacha["gasha_ev_mult"], 1.05)
        self.assertGreater(arch_gacha["keep_gold_mult"], 1.1)

        # 4. 商店流留钱边际价值 > 训练流；商店流在普通池中对打工权重更高
        self.assertGreater(lc.calc_keep_gold_ev(100, st_store), lc.calc_keep_gold_ev(100, st_train))
        res_store = lc.evaluate_weekend_and_shop(st_store, self.strategy)
        res_gacha = lc.evaluate_weekend_and_shop(st_gacha, self.strategy)
        self.assertEqual(res_store["lineup_archetype"]["archetype"], "store")
        self.assertIn("商店流", res_store["day_tag"])
        self.assertEqual(res_gacha["lineup_archetype"]["archetype"], "gacha")
        self.assertIn("扭蛋流", res_gacha["day_tag"])

        # 5. 商店流遇到 #82 彩蛋笔记时依旧 100% 必拿，不被降温系数误伤
        st_store_82 = self._make_weekend_state(
            1002,
            [1045, 1011, 1023, 1037],
            gold=140,
            gashapons=[
                {"idx": 1, "cfg_id": 82, "price": 120, "mates": 0},
                {"idx": 2, "cfg_id": 1001, "price": 60, "mates": 0},
                {"idx": 3, "cfg_id": 1002, "price": 60, "mates": 0},
            ],
        )
        res_store_82 = lc.evaluate_weekend_and_shop(st_store_82, self.strategy)
        self.assertEqual(res_store_82["best_act"], (6, 17))
        self.assertIn("笔记", res_store_82["best_compact"])

    def test_verified_mechanics_db_contains_tieba_entries(self):
        """验证已验证机制库 JSON & Markdown 均已同步收录本次贴吧 Fact-Check 新条目。"""
        db_path = os.path.join(BASE_DIR, "data", "verified_mechanics.json")
        md_path = os.path.join(BASE_DIR, "docs", "verified_mechanics.md")
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
        titles = " ".join(m["title"] for m in db.get("core_mechanics", []))
        self.assertIn("一砖入魂", titles)
        self.assertIn("笔记实操铁律", titles)
        self.assertIn("四大育成流派生态定位", titles)

        with open(md_path, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("自动训练不会产出“第二位置”词条", md)
        self.assertIn("白神(1045)", md)

    def test_equipment_advisor_mtime_cache_and_explicit_focus_stat(self):
        """验证 equip_pool.json mtime 自动失效刷新与显式 focus_stat 绕开全局 hud_config.json。"""
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = os.path.join(tmp, "equip_pool.json")
            hud_path = os.path.join(tmp, "hud_config.json")
            with open(pool_path, "w", encoding="utf-8") as f:
                json.dump({"owned_cards": {"11002": {"cfg_id": 11002, "level": 50, "break_level": 4, "awake_level": 1}}}, f)
            with open(hud_path, "w", encoding="utf-8") as f:
                json.dump({"focus": {"stat": 101, "bonus": 1.15}}, f)
            os.utime(pool_path, (1000.0, 1000.0))

            with (
                mock.patch.object(eq_adv, "EQUIP_POOL_FILE", pool_path),
                mock.patch.object(eq_adv, "HUD_CONFIG_FILE", hud_path),
                mock.patch.object(eq_adv, "_CACHED_DATA", None),
                mock.patch.object(eq_adv, "_CACHED_MTIMES", None),
            ):
                _, pool1, _ = eq_adv.load_equip_data()
                self.assertIn("11002", pool1["owned_cards"])
                self.assertNotIn("32003", pool1["owned_cards"])

                # 外部更新 equip_pool.json 且推进 mtime 后，无需 force_reload 自动刷新
                with open(pool_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "owned_cards": {
                                "11002": {"cfg_id": 11002, "level": 50, "break_level": 4, "awake_level": 1},
                                "32003": {"cfg_id": 32003, "level": 50, "break_level": 4, "awake_level": 0},
                            }
                        },
                        f,
                    )
                os.utime(pool_path, (2000.0, 2000.0))
                _, pool2, _ = eq_adv.load_equip_data()
                self.assertIn("32003", pool2["owned_cards"])

                # 显式传入 focus_stat=None 绕开 hud_config.json 中的 stat=101(投射)
                rec_default = eq_adv.recommend_equipments_for_character(1002)
                rec_none = eq_adv.recommend_equipments_for_character(1002, focus_stat=None)
                rec_jump = eq_adv.recommend_equipments_for_character(1002, focus_stat=102)
                score_default_11002 = next(x for x in rec_default["owned_loadout"] if int(x["equip"]["id"]) == 11002)
                score_none_11002 = next(x for x in rec_none["owned_loadout"] if int(x["equip"]["id"]) == 11002)
                score_jump_32003 = next(x for x in rec_jump["owned_loadout"] if int(x["equip"]["id"]) == 32003)
                self.assertIn("投射特化", score_default_11002["train_match"])
                self.assertNotIn("特化", score_none_11002["train_match"])
                self.assertIn("弹跳特化", score_jump_32003["train_match"])

    def test_equipment_advisor_owned_max_whistle_allows_borrow_missing_ssr(self):
        """验证自有 11004 满破(break>=4, awake>=1)时走「自有口哨+自有3+借1缺失SSR」，未满破仍必借 11004。"""
        with tempfile.TemporaryDirectory() as tmp:
            pool_path = os.path.join(tmp, "equip_pool.json")
            base_owned = {
                "11002": {"cfg_id": 11002, "level": 50, "break_level": 4, "awake_level": 1},
                "16002": {"cfg_id": 16002, "level": 50, "break_level": 4, "awake_level": 1},
                "16003": {"cfg_id": 16003, "level": 50, "break_level": 4, "awake_level": 1},
                "16004": {"cfg_id": 16004, "level": 50, "break_level": 4, "awake_level": 1},
            }
            with (
                mock.patch.object(eq_adv, "EQUIP_POOL_FILE", pool_path),
                mock.patch.object(eq_adv, "_CACHED_DATA", None),
                mock.patch.object(eq_adv, "_CACHED_MTIMES", None),
            ):
                # 1. 自有 11004 仅 0 觉未满破 -> 仍必借满破 11004
                owned_unmax = dict(base_owned)
                owned_unmax["11004"] = {"cfg_id": 11004, "level": 50, "break_level": 4, "awake_level": 0}
                with open(pool_path, "w", encoding="utf-8") as f:
                    json.dump({"owned_cards": owned_unmax}, f)
                os.utime(pool_path, (1000.0, 1000.0))
                rec_unmax = eq_adv.recommend_equipments_for_character(1002, focus_stat=None)
                whistle_unmax = rec_unmax["owned_loadout"][0]
                self.assertEqual(int(whistle_unmax["equip"]["id"]), 11004)
                self.assertTrue(whistle_unmax["is_borrow"])
                self.assertIsNone(whistle_unmax.get("owned_info"))
                self.assertEqual(sum(1 for x in rec_unmax["owned_loadout"] if x.get("is_borrow")), 1)

                # 2. 自有 11004 已满破 (break>=4 且 awake>=1) -> 自有口哨 + 自有 3 + 借 1 缺失 SSR
                owned_max = dict(base_owned)
                owned_max["11004"] = {"cfg_id": 11004, "level": 50, "break_level": 4, "awake_level": 1}
                with open(pool_path, "w", encoding="utf-8") as f:
                    json.dump({"owned_cards": owned_max}, f)
                os.utime(pool_path, (2000.0, 2000.0))
                rec_max = eq_adv.recommend_equipments_for_character(1002, focus_stat=None)
                self.assertEqual(len(rec_max["owned_loadout"]), 5)
                whistle_max = rec_max["owned_loadout"][0]
                self.assertEqual(int(whistle_max["equip"]["id"]), 11004)
                self.assertFalse(whistle_max["is_borrow"])
                self.assertIsNotNone(whistle_max.get("owned_info"))
                borrowed_cards = [x for x in rec_max["owned_loadout"] if x.get("is_borrow")]
                self.assertEqual(len(borrowed_cards), 1)
                borrowed_id = str(borrowed_cards[0]["equip"]["id"])
                self.assertNotIn(borrowed_id, owned_max)
                self.assertTrue(borrowed_cards[0]["skill_eval"]["active"])

    def test_skill_advisor_note_pool_mtime_and_display_resolution(self):
        """验证 skill_advisor note_pool mtime 自动刷新与 display 检测优先级（不再硬编码回退 :1）。"""
        with tempfile.TemporaryDirectory() as tmp:
            np_path = os.path.join(tmp, "note_pool.json")
            with open(np_path, "w", encoding="utf-8") as f:
                json.dump({"own_notes": {"99001": {"note_id": 99001, "special_entries": [{"type": 6, "star": 1}]}}}, f)
            os.utime(np_path, (1000.0, 1000.0))

            with (
                mock.patch.object(sa, "NOTE_POOL_PATH", np_path),
                mock.patch.object(sa, "_CACHED_NOTE_POOL", None),
                mock.patch.object(sa, "_CACHED_NOTE_POOL_MTIME", -1.0),
            ):
                self.assertEqual(sa._lookup_note_skill_star(99001, None), 1)
                with open(np_path, "w", encoding="utf-8") as f:
                    json.dump({"own_notes": {"99001": {"note_id": 99001, "special_entries": [{"type": 6, "star": 3}]}}}, f)
                os.utime(np_path, (2000.0, 2000.0))
                self.assertEqual(sa._lookup_note_skill_star(99001, None), 3)

        # display 优先级：显式参数 > live_coach.query_game_window_display_and_xid() > 环境变量 DISPLAY（不再硬编码 :1）
        with (
            mock.patch.object(lc, "query_game_window_display_and_xid", return_value=(":20", "0xabc")),
            mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
        ):
            self.assertEqual(sa._resolve_capture_display(":9"), ":9")
            self.assertEqual(sa._resolve_capture_display(None), ":20")

        with (
            mock.patch.object(lc, "query_game_window_display_and_xid", return_value=("", "")),
            mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
        ):
            self.assertEqual(sa._resolve_capture_display(None), ":0")

        captured_envs = []

        def fake_run(cmd, env=None, **_kwargs):
            captured_envs.append(dict(env or {}))
            raise sa.subprocess.CalledProcessError(1, cmd)

        with (
            mock.patch.object(lc, "query_game_window_display_and_xid", return_value=("", "")),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sa.time, "sleep", return_value=None),
            mock.patch.object(sa.subprocess, "run", side_effect=fake_run),
        ):
            self.assertEqual(sa._resolve_capture_display(None), "")
            sa._probe_and_learn_sync("0x1", [], (1,), None, display=None)
            self.assertEqual(len(captured_envs), 1)
            self.assertNotIn("DISPLAY", captured_envs[0])


if __name__ == "__main__":
    unittest.main()
