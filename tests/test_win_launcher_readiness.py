#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_win_launcher_readiness.py — Windows 首版发版前 P0/P1 成熟度与空池冷启动回归测试

覆盖范围：
1. AST 静态调用审计：校验 win_launcher.py 对 lc / analyzer / dashboard_server / pc / updater / issue_reporter / bootstrap 的全部属性调用均真实存在；
2. 零个人数据池（无 note_pool.json / equip_pool.json / runs/）端到端模拟渲染五大高频场景：
   - initactor 选人配装（笔记通用最优 + 毕业 5 器材优雅降级）
   - skill_choice 选技能三选一
   - story_choice 剧情分支
   - weekend 周末与商店
   - train 平日 6 选 1 与商店小贴士
   断言 0 异常、runtime_error.log 不产生、HUD 渲染回调与预览文案全部非空；
3. _record_runtime_error 去重写入 runtime_error.log 与状态栏提示；
4. bootstrap.detect_archive_temp_run 压缩包直跑防呆与正常路径放行；
5. packaging/assets/hoopcoach.ico 多尺寸完整性；
6. dashboard_server.DashboardEngine 在零战绩与空池下全部核心接口 0 异常且返回完整通用规划数据。
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import types
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import analyzer
import bootstrap
import dashboard_server
import equipment_advisor as ea
import issue_reporter
import live_coach as lc
import note_advisor as na
import platform_compat as pc
import updater
import win_launcher


class _DummyVar:
    def __init__(self, value="") -> None:
        self._val = value

    def get(self):
        return self._val

    def set(self, val) -> None:
        self._val = val


class _DummyHUD:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def render_note_selection(self, adv, scan_badge="", scan_status_line="") -> None:
        self.calls.append(("render_note_selection", (adv, scan_badge, scan_status_line)))

    def render_skill_choice(self, state, evals) -> None:
        self.calls.append(("render_skill_choice", (state, evals)))

    def render_story_choice(self, state, adv) -> None:
        self.calls.append(("render_story_choice", (state, adv)))

    def render_weekend_advice(self, state, wk) -> None:
        self.calls.append(("render_weekend_advice", (state, wk)))

    def render_turn(self, state, scored, shop_tip="") -> None:
        self.calls.append(("render_turn", (state, scored, shop_tip)))

    def refresh_scene_overlay(self, state) -> None:
        self.calls.append(("refresh_scene_overlay", (state,)))


def _make_sample_train_cmds() -> list[dict]:
    cmds = []
    for i in range(1, 7):
        gains = {100 + i: 42}
        cmds.append({
            "idx": i,
            "gains": gains,
            "total_stat_gain": 42,
            "mate_extra_gains": 8 if i == 1 else 0,
            "sup_cards": [],
            "sup_count": 0,
            "gold_count": 0,
            "rainbow_count": 0,
            "mate_ids": [1001] if i == 1 else [],
            "mate_count": 1 if i == 1 else 0,
            "is_manager": False,
            "fail_rate": 0,
            "ps": -20,
            "pt": 15,
        })
    return cmds


class TestWinLauncherReadiness(unittest.TestCase):
    def test_win_launcher_ast_module_calls_exist(self) -> None:
        src_path = os.path.join(SRC_DIR, "win_launcher.py")
        with open(src_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=src_path)

        module_map = {
            "lc": lc,
            "analyzer": analyzer,
            "dashboard_server": dashboard_server,
            "pc": pc,
            "updater": updater,
            "issue_reporter": issue_reporter,
            "bootstrap": bootstrap,
        }

        missing: list[str] = []
        checked = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                alias = node.value.id
                if alias in module_map:
                    checked += 1
                    if not hasattr(module_map[alias], node.attr):
                        missing.append(f"{alias}.{node.attr} (line {node.lineno})")

        self.assertGreater(checked, 20, "AST 审计应覆盖至少 20 处跨模块属性访问")
        self.assertEqual(missing, [], f"win_launcher.py 存在指向不存在属性的调用: {missing}")

    def test_render_all_five_scenes_empty_user_pool(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hoopcoach_empty_pool_") as tmp_dir:
            orig_pc_base = pc.BASE_DIR
            orig_ea_pool = ea.EQUIP_POOL_FILE
            pc.BASE_DIR = tmp_dir
            ea.EQUIP_POOL_FILE = os.path.join(tmp_dir, "data", "equip_pool.json")
            ea.load_equip_data(force_reload=True)
            try:
                app = object.__new__(win_launcher.LQSNCoachDesktopApp)
                app.strategy_name = "training_flow"
                app.strategy = lc.load_strategy("training_flow")
                app.hud_cfg = {"recommendations": {}, "focus": {"stat": None, "bonus": 1.15}}
                app.hud = _DummyHUD()
                app.var_live_preview = _DummyVar("")
                app._last_rendered_sig = ""
                app._note_static_db = None
                app._note_pool_cache = {}
                app._note_pool_mtime = -2.0
                app._event_db = None
                app._logged_runtime_errors = set()

                mirror = types.SimpleNamespace(state=lc.make_initial_state(), last_train_cmds=[])
                app.mirror = mirror

                # 1. initactor 选人配装（空笔记池 + 空器材池优雅降级）
                st1 = lc.make_initial_state()
                st1["active_ui_views"] = {"initactor"}
                st1["pre_edu_char_id"] = 1002
                mirror.state = st1
                app._render_current_state(force=True)

                note_calls = [c for c in app.hud.calls if c[0] == "render_note_selection"]
                self.assertEqual(len(note_calls), 1)
                adv_note = note_calls[0][1][0]
                self.assertTrue(adv_note.get("is_empty_pool"))
                self.assertGreaterEqual(len(adv_note.get("generic_combos") or []), 2)
                markup = na.format_character_note_advice_hud_markup(adv_note, "")
                self.assertIn("通用推荐", markup)
                self.assertIn("暂未同步个人笔记池", markup)
                self.assertEqual(len(adv_note.get("equip_icon_items") or []), 5)
                self.assertIn("通用毕业 5 卡推荐", adv_note.get("hud_tip_markup", ""))
                self.assertNotIn("空冲分均衡", adv_note.get("hud_tip_markup", ""))
                self.assertIn("选人配装", app.var_live_preview.get())

                # 2. skill_choice 选技能三选一
                app.hud.calls.clear()
                st2 = lc.make_initial_state()
                st2["main_char_id"] = 1002
                st2["exp_round"] = 5
                st2["round"] = 4
                st2["edu_state_id"] = 22
                st2["active_ui_views"] = {"education_skill_choice_view_v2"}
                st2["cur_skill_choices"] = [
                    {"id": 200101, "lv": 1},
                    {"id": 200201, "lv": 1},
                    {"id": 200301, "lv": 1},
                ]
                mirror.state = st2
                app._render_current_state(force=True)

                skill_calls = [c for c in app.hud.calls if c[0] == "render_skill_choice"]
                self.assertEqual(len(skill_calls), 1)
                evals = skill_calls[0][1][1]
                self.assertEqual(len(evals), 3)
                self.assertIn("技能三选一", app.var_live_preview.get())

                # 3. story_choice 剧情分支
                app.hud.calls.clear()
                st3 = lc.make_initial_state()
                st3["main_char_id"] = 1002
                st3["exp_round"] = 4
                st3["round"] = 3
                st3["ps"] = 85
                st3["gold"] = 120
                st3["in_story_choice"] = True
                st3["cur_story"] = ("40", "合宿事件")
                mirror.state = st3
                app._render_current_state(force=True)

                story_calls = [c for c in app.hud.calls if c[0] == "render_story_choice"]
                self.assertEqual(len(story_calls), 1)
                adv_story = story_calls[0][1][1]
                self.assertTrue(adv_story.get("recommendation"))
                self.assertIn("事件", app.var_live_preview.get())

                # 4. weekend 周末与商店
                app.hud.calls.clear()
                st4 = lc.make_initial_state()
                st4["main_char_id"] = 1002
                st4["exp_round"] = 6
                st4["round"] = 5
                st4["round_type"] = 2
                st4["weekend_day"] = 1
                st4["ps"] = 65
                st4["gold"] = 180
                st4["active_ui_views"] = {"weekend_map"}
                st4["gashapon_list"] = [{"idx": 1, "cfg_id": 82, "price": 20, "mates": 1}]
                mirror.state = st4
                app._render_current_state(force=True)

                wk_calls = [c for c in app.hud.calls if c[0] == "render_weekend_advice"]
                self.assertEqual(len(wk_calls), 1)
                self.assertIn("EV", app.var_live_preview.get())

                # 5. train 平日 6 选 1 + 平日商店提示
                app.hud.calls.clear()
                st5 = lc.make_initial_state()
                st5["main_char_id"] = 1002
                st5["exp_round"] = 3
                st5["round"] = 3
                st5["round_type"] = 1
                st5["ps"] = 100
                st5["gold"] = 160
                st5["active_ui_views"] = {"ycjm01"}
                st5["store_items"] = [{"id": 1101, "price": 0, "num": 1}]
                mirror.state = st5
                mirror.last_train_cmds = _make_sample_train_cmds()
                app._render_current_state(force=True)

                turn_calls = [c for c in app.hud.calls if c[0] == "render_turn"]
                self.assertEqual(len(turn_calls), 1)
                scored = turn_calls[0][1][1]
                self.assertGreaterEqual(len(scored), 6)
                self.assertIn("第03/28训", app.var_live_preview.get())

                # 全程 0 运行时异常，不生成 runtime_error.log
                self.assertEqual(app._logged_runtime_errors, set())
                self.assertFalse(os.path.exists(os.path.join(tmp_dir, "runtime_error.log")))
            finally:
                pc.BASE_DIR = orig_pc_base
                ea.EQUIP_POOL_FILE = orig_ea_pool
                ea.load_equip_data(force_reload=True)

    def test_runtime_error_logged_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hoopcoach_errlog_") as tmp_dir:
            orig_pc_base = pc.BASE_DIR
            pc.BASE_DIR = tmp_dir
            try:
                app = object.__new__(win_launcher.LQSNCoachDesktopApp)
                app.var_live_preview = _DummyVar("")
                app._logged_runtime_errors = set()

                for _ in range(3):
                    try:
                        raise ValueError("boom-once")
                    except ValueError as e:
                        app._record_runtime_error("测试分支", e)

                log_path = os.path.join(tmp_dir, "runtime_error.log")
                self.assertTrue(os.path.isfile(log_path))
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertEqual(content.count("[测试分支] ValueError: boom-once"), 1)
                self.assertIn("runtime_error.log", app.var_live_preview.get())
            finally:
                pc.BASE_DIR = orig_pc_base

    def test_detect_archive_temp_run(self) -> None:
        bad_paths = [
            r"C:\Users\Alice\AppData\Local\Temp\Rar$EXa12345.678\HoopCoach",
            r"C:\Users\Alice\AppData\Local\Temp\7zO9A8B1234\HoopCoach",
            r"C:\Users\Alice\AppData\Local\Temp\Temp1_HoopCoach-Windows-v1.0.0.zip\HoopCoach",
            r"C:\Users\Alice\AppData\Local\Temp\BNZ.65ab1234\HoopCoach",
        ]
        for p in bad_paths:
            hit, _ = bootstrap.detect_archive_temp_run(p, temp_root=r"C:\Users\Alice\AppData\Local\Temp")
            self.assertTrue(hit, f"应命中压缩包临时目录防呆: {p}")

        good_paths = [
            r"D:\Games\HoopCoach",
            r"C:\Users\Alice\Desktop\HoopCoach",
            "/home/user/Games/HoopCoach",
        ]
        for p in good_paths:
            hit, _ = bootstrap.detect_archive_temp_run(p, temp_root=r"C:\Users\Alice\AppData\Local\Temp")
            self.assertFalse(hit, f"普通解压路径不应误报: {p}")

    def test_hoopcoach_ico_has_multi_sizes(self) -> None:
        ico_path = os.path.join(ROOT_DIR, "packaging", "assets", "hoopcoach.ico")
        self.assertTrue(os.path.isfile(ico_path), "缺少程序图标 packaging/assets/hoopcoach.ico")
        self.assertGreater(os.path.getsize(ico_path), 1024)
        try:
            from PIL import Image
            with Image.open(ico_path) as im:
                sizes = set(im.info.get("sizes") or ())
            for expected in ((16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)):
                self.assertIn(expected, sizes)
        except ImportError:
            pass

    def test_dashboard_engine_empty_pool_zero_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hoopcoach_dash_empty_") as tmp_dir:
            orig_attrs = {
                "DETAILED_RUNS_FILE": dashboard_server.DETAILED_RUNS_FILE,
                "NOTE_POOL_FILE": dashboard_server.NOTE_POOL_FILE,
                "EQUIP_POOL_FILE": dashboard_server.EQUIP_POOL_FILE,
                "DAILY_QUEUE_FILE": dashboard_server.DAILY_QUEUE_FILE,
                "ARCHIVE_DIR": dashboard_server.ARCHIVE_DIR,
                "ACCOUNT_STATE_MD": dashboard_server.ACCOUNT_STATE_MD,
            }
            orig_ea_pool = ea.EQUIP_POOL_FILE
            dashboard_server.DETAILED_RUNS_FILE = os.path.join(tmp_dir, "detailed_runs.json")
            dashboard_server.NOTE_POOL_FILE = os.path.join(tmp_dir, "note_pool.json")
            dashboard_server.EQUIP_POOL_FILE = os.path.join(tmp_dir, "equip_pool.json")
            dashboard_server.DAILY_QUEUE_FILE = os.path.join(tmp_dir, "daily_queue.json")
            dashboard_server.ARCHIVE_DIR = os.path.join(tmp_dir, "archive")
            dashboard_server.ACCOUNT_STATE_MD = os.path.join(tmp_dir, "account_state.md")
            ea.EQUIP_POOL_FILE = os.path.join(tmp_dir, "equip_pool.json")
            ea.load_equip_data(force_reload=True)
            try:
                eng = dashboard_server.DashboardEngine()
                ov = eng.get_overview()
                self.assertEqual(ov["kpi"]["total_runs"], 0)
                self.assertEqual(len(ov["positions"]), 5)
                self.assertEqual(ov["runs"], [])

                box = eng.get_box()
                self.assertGreater(len(box["characters"]), 30)
                self.assertEqual(box["own_notes"], [])
                self.assertEqual(box["owned_equips"], [])

                plan = eng.get_planner(1002)
                self.assertTrue(plan["notes"]["is_empty_pool"])
                self.assertGreaterEqual(len(plan["notes"]["generic_combos"]), 3)
                self.assertEqual(len(plan["equipments"]["bis_loadout"]), 5)
                self.assertGreaterEqual(len(plan["equipments"]["pvp_22_loadout"]), 5)
            finally:
                for k, v in orig_attrs.items():
                    setattr(dashboard_server, k, v)
                ea.EQUIP_POOL_FILE = orig_ea_pool
                ea.load_equip_data(force_reload=True)


if __name__ == "__main__":
    unittest.main()
