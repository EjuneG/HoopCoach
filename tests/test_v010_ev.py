#!/usr/bin/env python3
"""
EV v0.10 底层校准全量回归单测集 (tests/test_v010_ev.py)：
1. 六维双三元对称闭环拓扑 (FACILITY_STAT_TOPOLOGY[2] == (102, 106))、等权底盘与取消 106 孤岛/104 提前刹车；
2. calc_lineup_teammate_ev() 实收倍率校准（5 人训练队 head_cnt=1/2/3）与 ratio>=1.0 严格归零；
3. calc_equip_trammel_option_ev() Δ_rb 回调、cur_t 单调性修复（无 65>91 倒挂）、already_maxed_crit_cnt 递减与彩圈人头分去重；
4. 周末单套严格凹终态体力价值函数 V_ps(fin_ps)（无双重计价）、赌狗药两点真期望 Jensen 修正与 recommended_shop_buys 契约；
5. focus 专项倍率去除 max(bw, 1.15) 硬抬底（纯 bw * focus_bonus）与 cap_decay_start <= 0.94；
6. 扭蛋升级包接入 P5 设施模型、商店/扭蛋熟练度区分 NON_CRIT_EQUIPMENT_IDS 并与期权对齐、动态 ps_max=140 全链路生效（含 #107 按 ps_cap+10）；
7. parse_weekend_turns_from_text() 买药后采样修复（state 为买药后最新快照，保留 pre_shop_state）；
8. 已验证机制库 JSON & Markdown 同步收录 2 号弹跳副属性=106 意识与双三元闭环。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import live_coach as lc  # noqa: E402


def _make_base_state(
    exp_round: int = 10,
    round_no: int = 7,
    ps: int = 100,
    ps_max: int = 130,
    gold: int = 180,
) -> dict:
    st = lc.make_initial_state()
    st["exp_round"] = exp_round
    st["round"] = round_no
    st["ps"] = ps
    st["ps_max"] = ps_max
    st["gold"] = gold
    st["props"] = {101: 600, 102: 600, 103: 600, 104: 600, 105: 600, 106: 600}
    st["prop_max"] = {101: 1700, 102: 1700, 103: 1700, 104: 1700, 105: 1700, 106: 1700}
    st["focus_levels"] = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}
    st["base_focus_levels"] = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}
    st["train_counts"] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
    st["train_up_need"] = {1: 3, 2: 3, 3: 3, 4: 3, 5: 3, 6: 3}
    return st


def _make_cmd(idx: int, gains: dict, **overrides) -> dict:
    cmd = {
        "idx": idx,
        "gains": dict(gains),
        "total_stat_gain": sum(gains.values()),
        "mate_extra_gains": 0,
        "sup_cards": [],
        "sup_count": 0,
        "gold_count": 0,
        "rainbow_count": 0,
        "mate_ids": [],
        "mate_count": 0,
        "is_manager": False,
        "fail_rate": 0,
        "ps": 4 if idx == 6 else -20,
        "pt": 10,
    }
    cmd.update(overrides)
    return cmd


class TestV010EVCore(unittest.TestCase):
    def setUp(self):
        self.strategy = lc.load_strategy("training_flow")
        self.strategy["_focus_override"] = (None, 1.0)

    # ------------------------------------------------------------------
    # 1. 六维拓扑 & 等权底盘
    # ------------------------------------------------------------------
    def test_1_topology_and_equal_weights(self):
        # 双三元对称闭环：101->105->103->101 与 102->106->104->102
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[2], (102, 106))
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[4], (104, 102))
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[6], (106, 104))
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[1], (101, 105))
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[3], (103, 101))
        self.assertEqual(lc.FACILITY_STAT_TOPOLOGY[5], (105, 103))

        # strategies/training_flow.json 升至 0.10.0 且六维等权、取消 106 孤岛与 104 提前刹车
        self.assertEqual(self.strategy["version"], "0.10.0")
        w = self.strategy["weights"]
        for k in range(101, 107):
            self.assertAlmostEqual(w["stat_base_value"][str(k)], 1.0, places=4)
            self.assertAlmostEqual(lc.FIXED_STAT_BASE_W[k], 1.0, places=4)
        self.assertAlmostEqual(w["isolated_106_catchup_extra_mult"], 1.0, places=4)
        self.assertGreaterEqual(w["stamina_104_early_decay_ratio"], w["cap_proximity_decay_start_ratio"])

        # calc_stat_gain_ev 在同属性比例(86%)下，104 体能与 101 投射完全等权（不再 104 提前在 85% 刹车）
        props_86 = {k: 1462 for k in range(101, 107)}  # 1462/1700 = 0.86
        pmax = {k: 1700 for k in range(101, 107)}
        ev101, g101 = lc.calc_stat_gain_ev(101, 40, props_86, pmax, self.strategy, rem_trains=10)
        ev104, g104 = lc.calc_stat_gain_ev(104, 40, props_86, pmax, self.strategy, rem_trains=10)
        ev106, g106 = lc.calc_stat_gain_ev(106, 40, props_86, pmax, self.strategy, rem_trains=10)
        self.assertEqual((ev101, g101), (ev104, g104))
        self.assertEqual((ev101, g101), (ev106, g106))

        # calc_gashapon_dynamic_ev 读取 strategy 权重而非硬编码
        st = _make_base_state()
        custom_strat = json.loads(json.dumps(self.strategy))
        custom_strat["_focus_override"] = (None, 1.0)
        custom_strat["weights"]["stat_base_value"]["101"] = 1.50
        ev_def, _, _ = lc.calc_gashapon_dynamic_ev(16, 100, 0, st, strategy=self.strategy)
        ev_hi, _, _ = lc.calc_gashapon_dynamic_ev(16, 100, 0, st, strategy=custom_strat)
        self.assertGreater(ev_hi - ev_def, 30.0)

    # ------------------------------------------------------------------
    # 2. calc_lineup_teammate_ev 校准 & 满上限严格归零
    # ------------------------------------------------------------------
    def test_2_lineup_teammate_ev_calibration_and_cap_zero(self):
        st = _make_base_state()
        st["main_char_id"] = 1046
        st["teammate_char_ids"] = [1007, 1012, 1013, 1030]
        st["char_talents"] = {
            1046: {104601: 7},
            1007: {100701: 7},
            1012: {101201: 7},
            1013: {101301: 7},
            1030: {103001: 7},
        }
        w = self.strategy["weights"]

        # 5 人满配训练队 head_cnt = 1 / 2 / 3 预测倍率应贴近实测 250% / 310% / 385%
        mults = {}
        for hc, m_ids, is_mgr in (
            (1, [], True),
            (2, [1007], True),
            (3, [1007, 1030], True),
        ):
            cmd = _make_cmd(2, {102: 40, 106: 15}, mate_ids=m_ids, mate_count=len(m_ids), is_manager=is_mgr)
            ev_big, _ = lc.calc_lineup_teammate_ev(
                st, cmd, 10000.0, 2, st["props"], st["prop_max"], st["growth_rates"], w
            )
            mults[hc] = 100.0 * (1.0 + ev_big / 10000.0)
        self.assertGreaterEqual(mults[1], 225.0)
        self.assertLessEqual(mults[1], 285.0)
        self.assertGreaterEqual(mults[2], 275.0)
        self.assertLessEqual(mults[2], 345.0)
        self.assertGreaterEqual(mults[3], 355.0)
        self.assertLessEqual(mults[3], 430.0)
        self.assertLess(mults[1], mults[2])
        self.assertLess(mults[2], mults[3])

        # 满上限硬编码残留清理：102 弹跳和 106 意识全满时，stat_ev=0 且 flat_talent_ev 必须严格为 0.0
        st_cap = _make_base_state()
        st_cap["main_char_id"] = 1020
        st_cap["teammate_char_ids"] = [1007, 1013, 1030, 1046]
        st_cap["props"] = {k: 1700 for k in range(101, 107)}
        st_cap["prop_max"] = {k: 1700 for k in range(101, 107)}
        cmd_cap = _make_cmd(2, {102: 30, 106: 10}, mate_ids=[1007], mate_count=1, is_manager=False)
        ev_cap, _ = lc.calc_lineup_teammate_ev(
            st_cap, cmd_cap, 0.0, 2, st_cap["props"], st_cap["prop_max"], st_cap["growth_rates"], w
        )
        self.assertEqual(ev_cap, 0.0)

    # ------------------------------------------------------------------
    # 3. 器材期权回调、单调性、多满彩竞争递减与人头分去重
    # ------------------------------------------------------------------
    def test_3_equip_option_monotonicity_and_competition_decay(self):
        w = self.strategy["weights"]
        self.assertLessEqual(w["supactor_rainbow_bonus"], 3.0)
        self.assertLessEqual(w["supactor_gold_bonus"], 1.5)

        # 单调性检验：同等 rem_after 下，cur_t=0..91 严格不减，杜绝 cur_t=65 > cur_t=91 倒挂，且区间合理（前期有存在感、峰值<=45 EV）
        for rem in (28, 24, 18, 12, 6):
            evs = [lc.calc_equip_trammel_option_ev(ct, 13, rem, w)["ev"] for ct in (0, 13, 26, 39, 52, 65, 78, 91)]
            for i in range(len(evs) - 1):
                self.assertLessEqual(
                    evs[i], evs[i + 1],
                    f"rem={rem} 处发生单调性倒挂: {list(zip((0,13,26,39,52,65,78,91), evs))}",
                )
            self.assertLessEqual(max(evs), 45.0, f"单张白圈期权不应虚高超过 45 EV: {evs}")
        # 前期开局 rem=28 时，0 熟暴击卡应回弹至合理起步区间 (>=12 EV)，避免被 0.95 折现误杀
        ev_early_0 = lc.calc_equip_trammel_option_ev(0, 13, 28, w)["ev"]
        self.assertGreaterEqual(ev_early_0, 12.0)

        # 多满彩卡竞争边际递减：already_maxed_crit_cnt = 0 > 1 > 2 > 3
        c0 = lc.calc_equip_trammel_option_ev(65, 13, 20, w, already_maxed_crit_cnt=0)["ev"]
        c1 = lc.calc_equip_trammel_option_ev(65, 13, 20, w, already_maxed_crit_cnt=1)["ev"]
        c2 = lc.calc_equip_trammel_option_ev(65, 13, 20, w, already_maxed_crit_cnt=2)["ev"]
        c3 = lc.calc_equip_trammel_option_ev(65, 13, 20, w, already_maxed_crit_cnt=3)["ev"]
        self.assertGreater(c0, c1)
        self.assertGreater(c1, c2)
        self.assertGreater(c2, c3)

    # ------------------------------------------------------------------
    # 4. 周末单套凹势函数 V_ps + 赌狗药 Jensen 偏差 + recommended_shop_buys
    # ------------------------------------------------------------------
    def test_4_weekend_single_v_ps_gamble_jensen_and_shop_buys(self):
        # 4a. 海滩前夕 R21 (ps=64, gold=395) 寝室休息纯体力增益应在 ~95~115 EV（彻底消除旧版 200+ EV 双重计价）
        st_r21 = _make_base_state(exp_round=21, round_no=14, ps=64, gold=395)
        st_r21["round_type"] = 2
        st_r21["weekend_day"] = 2
        st_r21["gashapon_list"] = [
            {"idx": 1, "cfg_id": 82, "price": 120, "mates": 0},   # #82 笔记彩蛋
            {"idx": 2, "cfg_id": 4, "price": 60, "mates": 0},
            {"idx": 3, "cfg_id": 5, "price": 60, "mates": 0},
        ]
        st_r21["minigame_list"] = [
            {"idx": 1, "game_id": 3, "price": 100, "mates": 0},
            {"idx": 2, "game_id": 1, "price": 60, "mates": 0},
            {"idx": 3, "game_id": 2, "price": 80, "mates": 0},
        ]
        st_r21["store_items"] = [{"id": 1201, "num": 1, "price": 60}]  # 稳定+80体营养餐
        st_r21["weekend_selections"] = {
            19: {"sel_id": 19, "ps_cost": 0, "base_gold": 70, "tips_gold": 20, "total_gold": 90, "status": 1},
            20: {"sel_id": 20, "ps_cost": -25, "base_gold": 120, "tips_gold": 30, "total_gold": 150, "status": 1},
            17: {"sel_id": 17, "status": 1}, 6: {"sel_id": 6, "status": 1},
            13: {"sel_id": 13, "status": 1}, 14: {"sel_id": 14, "status": 1},
        }
        wk_r21 = lc.evaluate_weekend_and_shop(st_r21, self.strategy)
        keep_395 = lc.calc_keep_gold_ev(395, st_r21)
        rest_ps_gain_ev = wk_r21["act_ev_map"][(1, 14)]["total_ev"] - keep_395
        self.assertGreaterEqual(rest_ps_gain_ev, 85.0)
        self.assertLessEqual(rest_ps_gain_ev, 120.0)
        # 校验低体(ps=64 -> 130, 66体)单位有效价值处于合理非双重计价区间
        self.assertLessEqual(rest_ps_gain_ev / 66.0, 2.05)

        # 校验 recommended_shop_buys 契约及 scored_minigame 原有字段
        self.assertIn("recommended_shop_buys", wk_r21)
        self.assertIsInstance(wk_r21["recommended_shop_buys"], list)
        self.assertTrue(any(x["id"] == 1201 and x["cat"] == "stamina" for x in wk_r21["recommended_shop_buys"]))
        for b in wk_r21["recommended_shop_buys"]:
            self.assertTrue({"id", "count", "cat", "reason"}.issubset(b.keys()))
        for mg in wk_r21["scored_minigame"]:
            self.assertIn("ui_slot", mg)
            self.assertIn("price", mg)

        # 4b. 极低体 ps=45 无药面对普通蓝蛋/普通街机时，寝室休息仍稳居第一保底续航
        st_low = _make_base_state(exp_round=14, round_no=9, ps=45, gold=140)
        st_low["round_type"] = 2
        st_low["weekend_day"] = 2
        st_low["gashapon_list"] = [
            {"idx": 1, "cfg_id": 4, "price": 60, "mates": 0},
            {"idx": 2, "cfg_id": 5, "price": 60, "mates": 0},
            {"idx": 3, "cfg_id": 6, "price": 60, "mates": 0},
        ]
        st_low["minigame_list"] = [{"idx": 1, "game_id": 1, "price": 60, "mates": 0}]
        st_low["store_items"] = []
        st_low["weekend_selections"] = dict(st_r21["weekend_selections"])
        wk_low = lc.evaluate_weekend_and_shop(st_low, self.strategy)
        self.assertEqual(wk_low["best_act"], (1, 14))

        # 4c. 赌狗药 Jensen 不等式：同样期望 +40 体、同价格，100% 稳定 +40 (1002) 严格优于 50% 赌 +80/0 (1162)
        st_det = _make_base_state(exp_round=14, round_no=9, ps=70, gold=200)
        st_det["round_type"] = 2
        st_det["weekend_day"] = 2
        st_det["gashapon_list"] = [{"idx": 1, "cfg_id": 4, "price": 60, "mates": 0}]
        st_det["weekend_selections"] = dict(st_r21["weekend_selections"])
        st_det["store_items"] = [{"id": 1002, "num": 1, "price": 45}]  # 稳定 +40 体

        st_gam = lc.clone_state_snapshot(st_det)
        st_gam["store_items"] = [{"id": 1162, "num": 1, "price": 45}]  # 50% 赌 +80/0 (期望也是 +40 体)

        wk_det = lc.evaluate_weekend_and_shop(st_det, self.strategy)
        wk_gam = lc.evaluate_weekend_and_shop(st_gam, self.strategy)
        # 在相同(6, 17)路线上，因 V_ps 严格凹，赌狗药两点真期望严格低于稳定药
        self.assertGreater(wk_det["act_ev_map"][(6, 17)]["total_ev"], wk_gam["act_ev_map"][(6, 17)]["total_ev"])

    # ------------------------------------------------------------------
    # 5. focus 专项倍率去除 max(bw, 1.15) 双重放大
    # ------------------------------------------------------------------
    def test_5_focus_multiplier_no_double_amplification(self):
        st = _make_base_state(exp_round=10, round_no=7)
        st["props"] = {k: 900 for k in range(101, 107)}
        strat_base = json.loads(json.dumps(self.strategy))
        strat_base["_focus_override"] = (None, 1.0)
        strat_foc = json.loads(json.dumps(self.strategy))
        strat_foc["_focus_override"] = (101, 1.15)

        # 5a. calc_stat_gain_ev: 100 点 101 投射在 focus=1.15 下严格为 115.0（不再是 1.06*1.15=121.9）
        ev_b, _ = lc.calc_stat_gain_ev(101, 100, st["props"], st["prop_max"], strat_base, rem_trains=10)
        ev_f, _ = lc.calc_stat_gain_ev(101, 100, st["props"], st["prop_max"], strat_foc, rem_trains=10)
        self.assertAlmostEqual(ev_b, 100.0, places=1)
        self.assertAlmostEqual(ev_f, 115.0, places=1)

        # 5b. score_training_turn: 纯 101 投射增益在 focus=1.15 下放大比严格为 1.15（不再是 1.3225）
        cmd = _make_cmd(1, {101: 100}, ps=0)
        # 设为 Lv5 剥离设施升级复利，纯测属性加权
        st_lv5 = _make_base_state(exp_round=10, round_no=7)
        st_lv5["focus_levels"] = {k: 5 for k in range(1, 7)}
        st_lv5["base_focus_levels"] = {k: 5 for k in range(1, 7)}
        sc_b = next(x for x in lc.score_training_turn(st_lv5, [cmd], strat_base) if x["idx"] == 1)["score"]
        sc_f = next(x for x in lc.score_training_turn(st_lv5, [cmd], strat_foc) if x["idx"] == 1)["score"]
        self.assertAlmostEqual(sc_f / sc_b, 1.15, places=3)

        # 5c. cap_decay_start 最多放宽至 0.94：在 95% 上限处必须触发防溢出衰减
        st_95 = _make_base_state(exp_round=30, round_no=19)
        st_95["focus_levels"] = {k: 5 for k in range(1, 7)}
        st_95["base_focus_levels"] = {k: 5 for k in range(1, 7)}
        st_95["props"][101] = 1615  # 1615/1700 = 0.95 > 0.94
        cmd_small = _make_cmd(1, {101: 20}, ps=0)
        sc_95 = next(x for x in lc.score_training_turn(st_95, [cmd_small], strat_foc) if x["idx"] == 1)["score"]
        # 若未触发衰减会是 20*1.15=23.0；触发 0.65 衰减后为 20*1.15*0.65=14.95
        self.assertLess(sc_95, 17.0)

        # 5d. calc_facility_level_step_net_ev & evaluate_story_event 同样无 max(bw, 1.15) 硬抬底
        d_b = lc.calc_facility_level_step_net_ev(1, 1, st, strat_base)
        d_f = lc.calc_facility_level_step_net_ev(1, 1, st, strat_foc)
        # focus_bonus=1.15 -> 1 + 0.15*0.70 = 1.105 仅作用于主属性 101，不应被 1.15 硬抬底放大
        self.assertGreater(d_f, d_b)
        self.assertLess(d_f / max(0.1, d_b), 1.16)

    # ------------------------------------------------------------------
    # 6. 扭蛋升级包 P5 接入 + 熟练度区分非暴击卡 + 全量 ps_max=140
    # ------------------------------------------------------------------
    def test_6_gasha_p5_bond_option_and_dynamic_ps_max(self):
        # 6a. 扭蛋升级包 #52 (1号Lv+3 & 投射+40) 接入 P5 设施模型：Lv5 时设施分归零，R20 海滩前正确扣除海滩 5 训
        st_lv1 = _make_base_state(exp_round=20, round_no=14)
        st_lv5 = _make_base_state(exp_round=20, round_no=14)
        st_lv5["focus_levels"][1] = 5
        st_lv5["base_focus_levels"][1] = 5
        ev_lv1, _, _ = lc.calc_gashapon_dynamic_ev(52, 100, 0, st_lv1, strategy=self.strategy)
        ev_lv5, _, _ = lc.calc_gashapon_dynamic_ev(52, 100, 0, st_lv5, strategy=self.strategy)
        p5_ref = lc.calc_shop_facility_book_p5_ev(0, st_lv1, self.strategy, lv_gain_override=3, train_idx_override=1)["ev"]
        self.assertAlmostEqual(ev_lv1 - ev_lv5, p5_ref, delta=0.5)

        # 6b. 商店/扭蛋熟练度区分 NON_CRIT_EQUIPMENT_IDS 并与期权对齐
        st_nc = _make_base_state(exp_round=10, round_no=7)
        st_nc["supactor_trammel"] = {11005: 40}  # 不可暴击且非哨子
        res_nc = lc.calc_shop_bond_item_ev(1112, 110, st_nc, strategy=self.strategy)
        self.assertEqual(res_nc["ev"], 0.0)

        # 哨子跨 70/100 单独走里程碑
        st_w = _make_base_state(exp_round=10, round_no=7)
        st_w["supactor_trammel"] = {11004: 60}
        res_w = lc.calc_shop_bond_item_ev(1112, 110, st_w, strategy=self.strategy)
        self.assertGreater(res_w["ev"], 10.0)
        self.assertIn("哨➔70", res_w["tag"])

        # 后期剩余 2 训且卡仅 20 熟（来不及满彩）-> 期权 EV 自动归零
        st_late = _make_base_state(exp_round=42, round_no=26)
        st_late["supactor_trammel"] = {31001: 20, 31002: 20}
        res_late = lc.calc_shop_bond_item_ev(1112, 110, st_late, strategy=self.strategy)
        self.assertEqual(res_late["ev"], 0.0)

        # 6c. 动态 ps_max=140 全链路生效（含 update_state_from_token 与 #107 按 ps_cap+10=150 算回满）
        st_pm = lc.make_initial_state()
        lc.update_state_from_token(st_pm, "[EDU_MODEL] 更新育成数据 {\n  ps = 132,\n  ps_max = 140,\n}")
        self.assertEqual(st_pm["ps"], 132)
        self.assertEqual(st_pm["ps_max"], 140)
        self.assertEqual(lc.get_ps_max(st_pm), 140)

        # 6号意识在 ps=132, ps_max=140 时应实回 +4 体（不截断为 0）
        cmd6 = _make_cmd(6, {106: 20, 104: 8}, ps=4)
        sc6 = next(x for x in lc.score_training_turn(st_pm, [cmd6], self.strategy) if x["idx"] == 6)
        self.assertNotIn("实+0", sc6["reasons"])

        # 扭蛋 #107 在 ps=130, ps_max=140 时，新上限为 150，应回满 +20 体（而非 0 体）
        st_107 = _make_base_state(exp_round=13, round_no=9, ps=130, ps_max=140)
        ev_107, _, ps_g_107 = lc.calc_gashapon_dynamic_ev(107, 100, 0, st_107, strategy=self.strategy)
        self.assertEqual(ps_g_107, 20)
        self.assertGreater(ev_107, 25.0)

    # ------------------------------------------------------------------
    # 7. parse_weekend_turns_from_text 买药后采样修复
    # ------------------------------------------------------------------
    def test_7_parse_weekend_turns_post_shop_snapshot(self):
        fake_log = (
            "[EDU_MODEL] 更新育成数据 {\n"
            "  experience_round = 14,\n"
            "  round_type = 2,\n"
            "  ps = 50,\n"
            "  ps_max = 130,\n"
            "  currency_list = {[1] = {key = 1, value = 200}},\n"
            "  edu_team_building_data = {team_building_list = {[1] = {cfg_id = 4, price = 60, company_teammate_tbl = {}}}}\n"
            "}\n"
            "request_call: education.cs_buy_edu_item {store_item_id = 1201}\n"
            "[EDU_MODEL] 更新育成数据 {\n"
            "  ps = 130,\n"
            "  currency_list = {[1] = {key = 1, value = 140}}\n"
            "}\n"
            "request_call: education.cs_select_edu_activity {building_cfg_id = 6,selection_cfg_id = 17}\n"
            "request_call: education.cs_select_team_building {index = 1}\n"
        )
        wturns = lc.parse_weekend_turns_from_text(fake_log)
        self.assertEqual(len(wturns), 1)
        wt = wturns[0]
        self.assertEqual(wt["state"]["ps"], 130)
        self.assertEqual(wt["state"]["gold"], 140)
        self.assertEqual(wt["pre_shop_state"]["ps"], 50)
        self.assertEqual(wt["pre_shop_state"]["gold"], 200)
        self.assertEqual(wt["chosen_act"], (6, 17))
        self.assertEqual(wt["chosen_gasha_idx"], 1)
        self.assertEqual(len(wt["bought_items"]), 1)

    # ------------------------------------------------------------------
    # 8. 机制库同步校验
    # ------------------------------------------------------------------
    def test_8_verified_mechanics_topology_synced(self):
        with open(os.path.join(PROJECT_ROOT, "data", "verified_mechanics.json"), encoding="utf-8") as f:
            db = json.load(f)
        topo2 = next(t for t in db["train_topology"] if t["idx"] == 2)
        self.assertEqual(topo2["sub"], "106 意识")
        with open(os.path.join(PROJECT_ROOT, "docs", "verified_mechanics.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("双三元对称闭环", md)
        self.assertIn("102→106→104→102", md)

    # ------------------------------------------------------------------
    # 9. 周五→周六百目鬼加训低体风险校准 + 残局末 4 训经理 0→1 变现率校准
    # ------------------------------------------------------------------
    def test_9_friday_saturday_risk_and_endgame_manager_retention(self):
        # 复现 W6 周五 (ER=40, ps=68, gold=266, 经理 0/3, 剩余 5 训, 商店有 70 金神之庇佑 1131)：
        # #1 投射(1彩爆+希雅, +43/+17, ps=-23 -> 45体) 应稳压 #6 意识(单站经理, +21/+9, ps=+5)
        st = _make_base_state(exp_round=40, round_no=31, ps=68, gold=266)
        st["main_char_id"] = 1011
        st["teammate_char_ids"] = [1046, 1012, 1007, 1013]
        st["props"] = {101: 1796, 102: 1105, 103: 1257, 104: 1022, 105: 1564, 106: 935}
        st["prop_max"] = {101: 2304, 102: 1745, 103: 1670, 104: 1640, 105: 2285, 106: 1670}
        st["growth_rates"] = {101: 500, 102: 0, 103: 0, 104: 0, 105: 400, 106: 0}
        st["mgr_energy"] = 0
        st["mgr_explosive"] = 15
        st["whistle_awake_level"] = 3
        st["whistle_break_level"] = 4
        st["supactor_trammel"] = {11004: 100}
        st["focus_levels"] = {1: 2, 2: 3, 3: 2, 4: 2, 5: 2, 6: 3}
        st["train_counts"] = {1: 2, 2: 1, 3: 3, 4: 0, 5: 3, 6: 3}
        st["train_up_need"] = {1: 4, 2: 5, 3: 4, 4: 4, 5: 4, 6: 5}
        st["store_items"] = [{"id": 1131, "num": 1, "price": 70}]

        cmd_shoot = _make_cmd(
            1, {101: 43, 105: 17},
            sup_cards=[{"card_id": 1, "val": 2}], sup_count=1, rainbow_count=1,
            mate_ids=[1046], mate_count=1, is_manager=False, ps=-23, fail_rate=0,
        )
        cmd_iq = _make_cmd(
            6, {106: 21, 104: 9},
            sup_cards=[], sup_count=0, rainbow_count=0,
            mate_ids=[], mate_count=0, is_manager=True, ps=5, fail_rate=0,
        )
        scored = lc.score_training_turn(st, [cmd_shoot, cmd_iq], self.strategy)
        self.assertEqual(scored[0]["idx"], 1, f"W6周五彩爆+希雅应胜过单经理意识: {scored}")
        self.assertGreater(scored[0]["score"] - scored[1]["score"], 5.0)

        # 即使商店没有任何药水/神之庇佑，45体对应风险也仅 ~11 EV（非旧版虚高 -56 EV），投射依然应胜出
        st_empty = lc.clone_state_snapshot(st)
        st_empty["store_items"] = []
        scored_empty = lc.score_training_turn(st_empty, [cmd_shoot, cmd_iq], self.strategy)
        self.assertEqual(scored_empty[0]["idx"], 1, f"无神之庇佑时 45 体风险校准后投射仍应胜出: {scored_empty}")

        # 极低体 (next_ps_est=28, 无药无庇佑) 时周六加训风险仍应显著扣分 (>45 EV)
        st_low = lc.clone_state_snapshot(st_empty)
        st_low["ps"] = 51  # 51 - 23 = 28
        sc_low_shoot = next(x for x in lc.score_training_turn(st_low, [cmd_shoot, cmd_iq], self.strategy) if x["idx"] == 1)
        self.assertIn("周六加训低体风险", sc_low_shoot["reasons"])

    # ------------------------------------------------------------------
    # 10. 属性上限倍率溢出截断 & 触顶器材期权衰减
    # ------------------------------------------------------------------
    def test_10_cap_overflow_truncation_after_teammate_mult(self):
        st = _make_base_state(exp_round=30, round_no=19, ps=105, gold=160)
        st["main_char_id"] = 1046
        st["teammate_char_ids"] = [1007, 1012, 1013, 1030]
        st["char_talents"] = {
            1046: {104601: 7},
            1007: {100701: 7},
            1012: {101201: 5},
            1013: {101301: 5},
            1030: {103001: 7},
        }
        # 101 投射距离上限仅剩 50 点（2300/2350），102 弹跳空间充裕（1200/1900）
        st["props"] = {101: 2300, 102: 1200, 103: 1100, 104: 1100, 105: 1100, 106: 1100}
        st["prop_max"] = {101: 2350, 102: 1900, 103: 1700, 104: 1700, 105: 1700, 106: 1700}

        cmd_near_cap = _make_cmd(
            1, {101: 45, 105: 15},
            mate_ids=[1007, 1030], mate_count=2, is_manager=False, ps=-22, fail_rate=0,
        )
        cmd_open_room = _make_cmd(
            2, {102: 40, 106: 14},
            mate_ids=[1007, 1030], mate_count=2, is_manager=False, ps=-22, fail_rate=0,
        )
        scored = lc.score_training_turn(st, [cmd_near_cap, cmd_open_room], self.strategy)
        self.assertEqual(scored[0]["idx"], 2, f"倍率放大后会严重溢出的 101 不应击败空间充裕的 102: {scored}")
        sc1 = next(x for x in scored if x["idx"] == 1)
        self.assertIn("⚠️溢", sc1["reasons"])
        self.assertIn("⚠️倍率溢出", sc1["hud_tags"])

    # ------------------------------------------------------------------
    # 11. 望月体力线前置买药提醒（无需进商店 / 0% 失败率也亮灯）
    # ------------------------------------------------------------------
    def test_11_wangyue_ps_threshold_shop_alert_without_entering_shop(self):
        st = _make_base_state(exp_round=17, round_no=11, ps=58, gold=120)
        st["main_char_id"] = 1046
        st["teammate_char_ids"] = [1007, 1012, 1013, 1030]
        # 望月 Lv3 -> 60体线；当前 58 体已跌破但训练 0% 失败率
        st["char_talents"] = {1012: {101201: 3}}
        st["store_items"] = [{"id": 1001, "num": 1, "price": 30}]
        cmd_ok = _make_cmd(2, {102: 38, 106: 14}, mate_ids=[1007], mate_count=1, fail_rate=0, ps=-20)
        scored = lc.score_training_turn(st, [cmd_ok], self.strategy)

        badge = lc.extract_weekday_shop_badge(st, scored)
        self.assertIn("保望月", badge)
        hud_tips = lc.format_weekday_hud_tips(st, scored, include_shop=True)
        self.assertTrue(any("保望月" in line for line in hud_tips), f"HUD 未提示保望月买药: {hud_tips}")
        shop_tip = lc.format_weekday_shop_tip(st, scored_cmds=scored, strategy=self.strategy)
        self.assertIn("望月", shop_tip)

    # ------------------------------------------------------------------
    # 12. 望月无药烂回合提前休息 vs 有药不乱休 vs 好回合不乱休
    # ------------------------------------------------------------------
    def test_12_wangyue_early_rest_ev_decision(self):
        st = _make_base_state(exp_round=16, round_no=10, ps=54, gold=120)  # 周二，后面还有 3 训
        st["main_char_id"] = 1046
        st["teammate_char_ids"] = [1007, 1012, 1013, 1030]
        st["char_talents"] = {1012: {101201: 4, 10122: 1}}  # 59 体线 + ★2 心结
        st["store_items"] = []  # 商店无药

        # 场景 A：纯烂回合（无队友、无彩圈、无经理），提前休息应获得保望月被动挽救 EV 并胜出
        cmd_bad = _make_cmd(1, {101: 18, 105: 7}, mate_count=0, is_manager=False, fail_rate=10, ps=-20)
        sc_no_pot = lc.score_training_turn(st, [cmd_bad], self.strategy)
        rest_no_pot = next(x for x in sc_no_pot if x["idx"] == 0)
        self.assertEqual(sc_no_pot[0]["idx"], 0, f"无药+烂回合+连训日跌破望月线应推荐提前休息: {sc_no_pot}")
        self.assertIn("保望月被动", rest_no_pot["hud_tags"])

        # 场景 B：商店有稳定补体药时，不给休息加望月挽救 EV（优先引导买药而非浪费回合休息）
        st_with_pot = lc.clone_state_snapshot(st)
        st_with_pot["store_items"] = [{"id": 1001, "num": 1, "price": 30}]
        rest_with_pot = next(x for x in lc.score_training_turn(st_with_pot, [cmd_bad], self.strategy) if x["idx"] == 0)
        self.assertLess(rest_with_pot["score"], rest_no_pot["score"] - 15.0)

        # 场景 C：无药但当回合是多人大彩圈神局，绝不能为了望月提前休息
        cmd_god = _make_cmd(
            2, {102: 52, 106: 18},
            sup_cards=[{"card_id": 1, "val": 2}], sup_count=1, rainbow_count=1,
            mate_ids=[1007, 1030], mate_count=2, is_manager=False, fail_rate=5, ps=-22,
        )
        sc_god = lc.score_training_turn(st, [cmd_god], self.strategy)
        self.assertEqual(sc_god[0]["idx"], 2, f"大彩圈神局绝不应为了望月提前休息: {sc_god}")

    # ------------------------------------------------------------------
    # 13. 扭蛋 #25 (蓝档随机1件器材+30熟) 不得误算为全队 5 张器材
    # ------------------------------------------------------------------
    def test_13_gasha_25_random_single_card_trammel_ev(self):
        st = _make_base_state(exp_round=13, round_no=8, ps=90, gold=160)
        st["supactor_trammel"] = {
            11004: 30,
            11002: 30,
            31003: 30,
            32003: 30,
            35002: 30,
        }
        ev25, tag25, _ = lc.calc_gashapon_dynamic_ev(25, 60, 0, st, strategy=self.strategy)
        ev26, tag26, _ = lc.calc_gashapon_dynamic_ev(26, 80, 0, st, strategy=self.strategy)
        ev27, tag27, _ = lc.calc_gashapon_dynamic_ev(27, 100, 0, st, strategy=self.strategy)
        self.assertIn("随机1器材熟练+30", tag25)
        self.assertIn("期望+30熟", tag25)
        self.assertIn("随机2器材熟练+30", tag26)
        self.assertIn("期望+60熟", tag26)
        self.assertIn("全器材熟练+30", tag27)
        self.assertLess(ev25, ev26, f"蓝档随机1件(25)不应高于紫档随机2件(26): {ev25} vs {ev26}")
        self.assertLess(ev26, ev27, f"紫档随机2件(26)不应高于金档全队(27): {ev26} vs {ev27}")

    # ------------------------------------------------------------------
    # 14. 独立右上角商店微窗在 ycjm01/ycjm02 常驻 + 望月本训后破线外显 + 顶栏解耦防覆盖
    # ------------------------------------------------------------------
    def test_14_independent_shop_hud_ycjm02_and_wangyue_prebreak(self):
        import hud_badges as hb

        # 场景 A：望月 60 体线，当前 66 体（尚未破线，但本训 -20 体后下训将跌破 60 体，且下训无免费回体）
        st = _make_base_state(exp_round=17, round_no=11, ps=66, gold=140)
        st["main_char_id"] = 1046
        st["teammate_char_ids"] = [1007, 1012, 1013, 1030]
        st["char_talents"] = {1012: {101201: 3}}
        st["supactor_trammel"] = {11004: 50}
        st["active_states"] = {20102}  # 同时挂一个生效中的商店 Buff，验证顶栏与独立商店微窗解耦互不覆盖
        st["state_rounds"] = {20102: 2}
        st["store_items"] = [{"id": 1001, "num": 1, "price": 30}]
        cmd_ok = _make_cmd(2, {102: 38, 106: 14}, mate_ids=[1007], mate_count=1, fail_rate=0, ps=-20)
        scored = lc.score_training_turn(st, [cmd_ok], self.strategy)

        # 1) 不点进商店，badge 与 hud_tips 均应直接给出「保下训望月」提醒
        badge_txt = lc.extract_weekday_shop_badge(st, scored)
        self.assertIn("保下训望月", badge_txt)
        hud_tips = lc.format_weekday_hud_tips(st, scored, include_shop=True)
        self.assertTrue(any("防破望月" in t for t in hud_tips), f"外部 HUD 漏掉本训后破线提醒: {hud_tips}")

        # 2) ycjm02 训练界面必须出现独立薄荷绿商店微窗（位于右上角 ry=0.118，不与按钮冲突），且顶栏干净保留 Buff/哨子
        specs_02, trough_02 = hb.build_badges_for_mode("ycjm02", st, last_turn_scored=scored)
        shop_specs_02 = [sp for sp in specs_02 if sp.get("style") == "shop"]
        self.assertEqual(len(shop_specs_02), 1, f"ycjm02 缺少独立商店提示微窗: {specs_02}")
        self.assertIn("保下训望月", shop_specs_02[0]["right"])
        self.assertLess(shop_specs_02[0]["ry"], 0.16, "ycjm02 商店微窗应位于右上角留白区，避免与1号训练按钮重叠")
        self.assertNotIn("保下训望月", trough_02, "已有独立商店微窗时，顶栏第4槽应与商店提示解耦")

        # 3) 若回退单行顶栏模式（include_shop=True），望月「先买」关键字也不得被哨子70回体前瞻覆盖
        st_below = lc.clone_state_snapshot(st)
        st_below["ps"] = 56
        st_below["supactor_trammel"] = {11004: 75}
        trough_fallback = hb.build_weekday_trough_line(st_below, scored, include_shop=True)
        self.assertIn("保望月", trough_fallback, f"单行顶栏兜底模式望月买药被覆盖: {trough_fallback}")

        # 4) 复现周五 (ER=5, day_idx=4) ps=68、望月55体线、最优训练 -28体 (训完仅剩40体)，外部商店微窗必须主动提示买药
        st_fri = _make_base_state(exp_round=5, round_no=5, ps=68, gold=200)
        st_fri["main_char_id"] = 1007
        st_fri["teammate_char_ids"] = [1046, 1012, 1013, 1030]
        st_fri["char_talents"] = {1012: {101201: 7}}  # 望月 55 体线
        st_fri["store_items"] = [
            {"id": 1002, "num": 2, "price": 50},
            {"id": 1201, "num": 1, "price": 60},
        ]
        cmd_fri = _make_cmd(4, {104: 42, 102: 12}, mate_ids=[1046], mate_count=1, fail_rate=0, ps=-28)
        scored_fri = lc.score_training_turn(st_fri, [cmd_fri], self.strategy)
        badge_fri = lc.extract_weekday_shop_badge(st_fri, scored_fri)
        self.assertIn("保下训望月", badge_fri, f"周五训完剩40体未提示保望月买药: {badge_fri}")

        # 5) 无必买项时，独立商店微窗仍常驻显示「商店｜暂无必买」(style="muted")
        st_idle = _make_base_state(exp_round=2, round_no=2, ps=100, gold=120)
        st_idle["store_items"] = [{"id": 1002, "num": 1, "price": 50}]
        specs_idle_01, _ = hb.build_badges_for_mode("ycjm01", st_idle, last_turn_scored=scored)
        specs_idle_02, _ = hb.build_badges_for_mode("ycjm02", st_idle, last_turn_scored=scored)
        self.assertTrue(any(sp.get("left") == "商店" and sp.get("right") == "暂无必买" and sp.get("style") == "muted" for sp in specs_idle_01))
        self.assertTrue(any(sp.get("left") == "商店" and sp.get("right") == "暂无必买" and sp.get("style") == "muted" for sp in specs_idle_02))


if __name__ == "__main__":
    unittest.main(verbosity=2)
