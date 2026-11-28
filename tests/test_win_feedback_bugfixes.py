#!/usr/bin/env python3
"""Regression tests for Windows/cross-platform bugfixes reported from real-world usage:
1. Settlement rank extraction ignores card.sc_team_rank_reward & pvp.cs_challenge_pvp new_rank.
2. LiveLogMirror resets state cleanly on cs_start_education, advances round on train/rest,
   and does not leak previous run's equipment into [01训] synergy warning.
3. Weekend HUD & advisor labels do not expose internal building IDs ('6号扭蛋', '3号街机', etc.).
4. LiveLogMirror marks fired_70/fired_71 on LoadStory 70/71, and weekday trough line never lets
   whistle-70 event tips hide actionable shop alerts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in (SRC_DIR, TESTS_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import analyzer as az  # noqa: E402
import hud_badges as hb  # noqa: E402
import live_coach as lc  # noqa: E402
from log_mirror import LiveLogMirror  # noqa: E402


def _make_edu_block(
    *,
    exp_round: int = 1,
    main_char_id: int = 1020,
    supactors: list[tuple[int, int, int]] | None = None,
) -> str:
    supactors = supactors or [(90001, 11004, 0), (90002, 11002, 20), (90003, 32003, 25)]
    sup_str = ", ".join(
        f"[{cid}] = {{awake_level = 1,break_level = 4,card_id = {cid},cfg_id = {cfg},event_progress = 0,level = 50,trammel = {tr}}}"
        for cid, cfg, tr in supactors
    )
    return (
        "[EDU_MODEL] 更新育成数据 {\n"
        f"  experience_round = {exp_round},\n"
        "  round_type = 1,\n"
        "  ps = 120,\n"
        "  ps_max = 130,\n"
        "  currency_list = { {key = 1,value = 180} },\n"
        f"  initactor = {{ cfg_id = {main_char_id} }},\n"
        f"  supactor_dict = {{ {sup_str} }}\n"
        "}\n"
    )


class TestReportedBugfixes(unittest.TestCase):
    def test_bug1_extract_education_result_ignores_team_rank_and_pvp_new_rank(self):
        seg = (
            "request_call:    education.cs_start_education    {initactor_cfg_id = 1020}\n"
            "[11-27 02:46:47]:[INFO]    [NETWORK]    dispatch:    education.sc_education_result    {\n"
            "  favor_exp_change = {...},\n"
            "  new_rank = 10420,\n"
            "  new_record = true\n"
            "}\n"
            "[11-27 02:47:01]:[INFO]    [NETWORK]    dispatch:    card.sc_team_rank_reward    {\n"
            "  new_rank = 19865,\n"
            "  old_rank = 19120,\n"
            "  reward_idx_list = {...}\n"
            "}\n"
            "[11-27 02:49:10]:[INFO]    [NETWORK]    dispatch:    pvp.cs_challenge_pvp    {\n"
            "  home_pvp_score = 4659,\n"
            "  new_rank = 14212,\n"
            "  ok = true,\n"
            "  old_rank = 14500\n"
            "}\n"
        )
        self.assertEqual(lc.extract_education_result_ranks(seg), [10420])
        self.assertTrue(lc.has_education_result(seg))

        unsettled_with_pvp = (
            "request_call:    education.cs_start_education    {initactor_cfg_id = 1020}\n"
            "[11-27 02:47:01]:[INFO]    [NETWORK]    dispatch:    card.sc_team_rank_reward    {\n"
            "  new_rank = 18562,\n"
            "  old_rank = 18100,\n"
            "  reward_idx_list = {...}\n"
            "}\n"
            "[11-27 02:49:10]:[INFO]    [NETWORK]    dispatch:    pvp.cs_challenge_pvp    {\n"
            "  home_pvp_score = 4659,\n"
            "  new_rank = 17046,\n"
            "  ok = true,\n"
            "  old_rank = 17200\n"
            "}\n"
        )
        self.assertEqual(lc.extract_education_result_ranks(unsettled_with_pvp), [])
        self.assertFalse(lc.has_education_result(unsettled_with_pvp))

    def test_bug1_schema_v4_auto_rebuilds_stale_v3_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            os.makedirs(log_dir)
            os.makedirs(arch_dir)

            log_fp = os.path.join(log_dir, "11_27___10_00_00___log.txt")
            from test_analyzer_regression import _make_train_cmds_block, _make_opening_notes_block

            parts = [
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1020}\n",
                _make_opening_notes_block((620, 610, 600)),
            ]
            for turn in range(1, 17):
                parts.append(_make_train_cmds_block(exp_round=turn, highlight_idx=1))
                parts.append("request_call:    education.cs_train    {cmd_idx = 1}\n")
            parts.append(
                "[11-27 10:20:00]:[INFO]    [NETWORK]    dispatch:    education.sc_education_result    {\n"
                "  new_rank = 10388,\n"
                "  new_record = true\n"
                "}\n"
                "[11-27 10:20:05]:[INFO]    [NETWORK]    dispatch:    card.sc_team_rank_reward    {\n"
                "  new_rank = 19865,\n"
                "  old_rank = 19000,\n"
                "  reward_idx_list = {...}\n"
                "}\n"
            )
            with open(log_fp, "w", encoding="utf-8") as f:
                f.write("".join(parts))

            st = os.stat(log_fp)
            manifest_path = os.path.join(runs_dir, "archive_manifest.json")
            out_path = os.path.join(runs_dir, "detailed_runs.json")
            # 模拟旧版 v3 manifest 已缓存了错误的 19865 分数
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 3,
                        "log_meta": {
                            "11_27___10_00_00___log.txt": {"mtime": round(st.st_mtime, 2), "size": st.st_size}
                        },
                        "runs_order": ["11_27___10_00_00_r1"],
                        "runs_meta": [
                            {
                                "run_id": "11_27___10_00_00_r1",
                                "log": "11_27___10_00_00___log.txt",
                                "end_log": "11_27___10_00_00___log.txt",
                            }
                        ],
                    },
                    f,
                )
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump([{"run_id": "11_27___10_00_00_r1", "seq_no": 1, "final_rank": 19865}], f)

            with patch.object(az, "LOG_DIR", log_dir), \
                 patch.object(az, "RUNS_DIR", runs_dir), \
                 patch.object(az, "ARCHIVE_DIR", arch_dir), \
                 patch.object(az, "MANIFEST_PATH", manifest_path), \
                 patch.object(az, "OUT_PATH", out_path):
                runs = az.extract_all_runs(force_rebuild=False)
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["final_rank"], 10388)
                with open(manifest_path, "r", encoding="utf-8") as mf:
                    m_after = json.load(mf)
                self.assertEqual(m_after["schema_version"], az.MANIFEST_SCHEMA_VERSION)

    def test_bug2_and_bug4_log_mirror_resets_run_advances_round_and_marks_fired_70(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_fp = os.path.join(tmp, "11_27___12_00_00___log.txt")
            # 模拟同一日志文件（超过 1.5MB 触发快速回放）包含：
            # 第 1 局带了组织者戒指(如 36001)且触发了哨子 70 事件；
            # 第 2 局开新局练佐久间(1020)，只带 11004/11002/32003（没带戒指），并在第 2 训后触发 LoadStory 70。
            pad = ("# padding line\n" * 110000)  # ~1.65 MB
            run1 = (
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1009}\n"
                + _make_edu_block(
                    exp_round=1,
                    main_char_id=1009,
                    supactors=[(10001, 11004, 75), (10002, 36001, 40), (10003, 11002, 20)],
                )
                + pad
                + "[EDU_MODEL]_LoadStory:    70    器材加成-70熟练度    0\n"
            )
            run2 = (
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1020}\n"
                + _make_edu_block(
                    exp_round=1,
                    main_char_id=1020,
                    supactors=[(20001, 11004, 72), (20002, 11002, 20), (20003, 32003, 25)],
                )
            )
            with open(log_fp, "w", encoding="utf-8") as f:
                f.write(run1 + run2)

            mirror = LiveLogMirror(tmp)
            try:
                self.assertTrue(mirror.attach_latest())
                # 新局不应携带上局的 36001 器材，且 fired_70 已重置为 False、round == 1
                self.assertEqual(set(mirror.state["supactor_card_to_cfg"].values()), {11004, 11002, 32003})
                self.assertEqual(mirror.state["round"], 1)
                self.assertFalse(mirror.state["fired_70"])
                ctx_before = lc.get_upcoming_schedule_context(mirror.state)
                self.assertTrue(ctx_before["queued_id70"])

                # 模拟进行 1 次训练 + 触发 LoadStory 70
                with open(log_fp, "a", encoding="utf-8") as f:
                    f.write("request_call:    education.cs_train    {cmd_idx = 1}\n")
                    f.write("[EDU_MODEL]_LoadStory:    70    器材加成-70熟练度    0\n")
                mirror.poll()

                self.assertEqual(mirror.state["round"], 2)
                self.assertTrue(mirror.state["fired_70"])
                ctx_after = lc.get_upcoming_schedule_context(mirror.state)
                self.assertFalse(ctx_after["queued_id70"])
                self.assertNotIn("ID70", ctx_after["weekday_heal_note"])
            finally:
                mirror.close()

    def test_bug3_no_internal_building_numbers_in_weekend_hud_and_advisor(self):
        state = lc.make_initial_state()
        state["round_type"] = 2
        state["exp_round"] = 6
        state["ps"] = 100
        state["gold"] = 160
        state["gashapon_list"] = [
            {"idx": 1, "cfg_id": 20002, "price": 10, "mates": 0},
            {"idx": 2, "cfg_id": 100, "price": 100, "mates": 1},
        ]
        state["minigame_list"] = [
            {"idx": 1, "game_id": 1, "price": 60, "mates": 0},
        ]
        strat = lc.load_strategy("training_flow")
        wk = lc.evaluate_weekend_and_shop(state, strat)
        for text in (wk.get("best_compact", ""), wk.get("headline", "")):
            for bad in ("6号", "3号街机", "8号", "1号外出", "1号公园"):
                self.assertNotIn(bad, text)

        specs_main, _ = hb.build_badges_for_mode("weekend_main", state, last_wk_adv=wk)
        specs_map, _ = hb.build_badges_for_mode("weekend_map", state, last_wk_adv=wk)
        for sp in specs_main + specs_map:
            right_s = str(sp.get("right") or "")
            self.assertNotIn("6号", right_s)
            self.assertNotIn("3号", right_s)

    def test_bug4_weekday_trough_does_not_hide_shop_alert_under_whistle_70(self):
        state = lc.make_initial_state()
        state["round"] = 6
        state["exp_round"] = 8
        state["round_type"] = 1
        state["ps"] = 100
        state["gold"] = 180
        state["supactor_trammel"] = {11004: 75}
        state["fired_70"] = False
        state["store_items"] = [{"id": 1151, "price": 0, "num": 1}]

        trough = hb.build_weekday_trough_line(state, [], include_shop=True)
        clean = hb.strip_markup_to_clean_text(trough)
        self.assertIn("0金免费", clean)
        self.assertIn("哨70", clean)

        # 触发 ID70 后，哨子 70 提示立即消失，仅保留商店提示
        state["fired_70"] = True
        trough_after = hb.build_weekday_trough_line(state, [], include_shop=True)
        clean_after = hb.strip_markup_to_clean_text(trough_after)
        self.assertIn("0金免费", clean_after)
        self.assertNotIn("哨70", clean_after)
        self.assertNotIn("ID70", clean_after)

    def test_feedback_super_diag_zip_compact_log_and_worker_relay(self):
        import io
        import zipfile
        import issue_reporter as ir
        import dashboard_server as ds

        with tempfile.TemporaryDirectory() as tmp:
            log_fp = os.path.join(tmp, "11_27___12_30_00___log.txt")
            sample_log = (
                "UnityEngine.Logger:Log(LogType, Object)\n"
                "XLua.DelegateBridge:PCall(IntPtr, Int32, Int32, Int32)\n"
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1020}\n"
                "request_call:    cs_heartbeat    {}\n"
                r"[11-27 12:31:00]:[INFO] path=C:\Users\SecretPlayer\Desktop\LQSN" + "\n"
                "[EDU_MODEL]_LoadStory:    70    器材加成-70熟练度    0\n"
                "[11-27 12:35:00]:[INFO]    [NETWORK]    dispatch:    education.sc_education_result    {\n"
                "  new_rank = 10520,\n"
                "  new_record = true\n"
                "}\n"
            )
            with open(log_fp, "w", encoding="utf-8") as f:
                f.write(sample_log)

            compact = ir.extract_compact_run_log(log_fp, max_chars=8000)
            self.assertIn("education.cs_start_education", compact)
            self.assertIn("器材加成-70熟练度", compact)
            self.assertIn("new_rank = 10520", compact)
            self.assertNotIn("UnityEngine.Logger", compact)
            self.assertNotIn("cs_heartbeat", compact)
            self.assertNotIn("SecretPlayer", compact)

            st = lc.make_initial_state()
            st["round"] = 5
            st["fired_70"] = True
            snap = ir.build_state_snapshot(st)
            self.assertEqual(snap["round"], 5)
            self.assertTrue(snap["fired_70"])

            diag = ir.collect_diagnostic_info({"active_view_mode": "ycjm01", "round": 5})
            zpath = ir.export_diagnostic_zip(
                diag,
                log_path=log_fp,
                out_dir=tmp,
                hud_config={"diegetic_mode": True},
                state_snapshot=snap,
            )
            with zipfile.ZipFile(zpath, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("diagnostic_env.json", names)
                self.assertIn("state_snapshot.json", names)
                self.assertIn("edu_log_compact.txt", names)
                self.assertIn("log_tail_sanitized.txt", names)

            # 验证 Worker 直投封装（mock urllib.request.urlopen）
            captured = {}

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(
                        {"ok": True, "issue_number": 42, "issue_url": "https://github.com/EjuneG/HoopCoach/issues/42"}
                    ).encode("utf-8")

            def _fake_urlopen(req, timeout=15.0):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.header_items())
                captured["body"] = json.loads(req.data.decode("utf-8"))
                return _FakeResp()

            with patch.object(ir.urllib.request, "urlopen", side_effect=_fake_urlopen):
                ok, res = ir.submit_feedback_via_worker(
                    "https://hoopcoach.example.workers.dev",
                    "bug",
                    "测试标题",
                    "测试描述",
                    diag,
                    include_diag=True,
                    state_snapshot=snap,
                    log_path=log_fp,
                    app_key="demo-key",
                )
            self.assertTrue(ok)
            self.assertEqual(res["issue_number"], 42)
            self.assertEqual(captured["url"], "https://hoopcoach.example.workers.dev/api/feedback")
            self.assertEqual(captured["headers"].get("X-app-key"), "demo-key")
            self.assertIn("education.cs_start_education", captured["body"]["compact_log"])
            self.assertIn('"fired_70": true', captured["body"]["state_snapshot"])

            # 验证作战室诊断包导出
            engine = ds.DashboardEngine()
            zbytes, zname = ds.build_dashboard_diagnostic_zip_bytes(engine, live_monitor=None)
            self.assertTrue(zname.startswith("hoopcoach_dashboard_diag_"))
            with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
                self.assertIn("diagnostic_env.json", zf.namelist())
                self.assertIn("detailed_runs_sanitized.json", zf.namelist())


    def test_skill_choice_blue_rarity_and_unknown_slot_guard(self):
        import skill_advisor as sa

        sa.reload_skill_catalog()
        # 1. 已知蓝卡（210308 快速启动）应排在白卡（210408 后段稳定）左侧、金卡（210418 后段稳固）右侧
        known_cards = [
            {"skill_id": 210408},  # 白
            {"skill_id": 210308},  # 蓝
            {"skill_id": 210418},  # 金
        ]
        evals_known = sa.evaluate_skill_choices(1001, known_cards)
        self.assertEqual([x["skill_id"] for x in evals_known], [210418, 210308, 210408])
        self.assertFalse(any(x.get("slot_uncertain") for x in evals_known))
        specs_k, trough_k = hb.build_badges_for_mode("skill_choice", {"main_char_id": 1001}, last_skill_evals=evals_known)
        self.assertGreaterEqual(len(specs_k), 1)
        self.assertIn("后段稳固", trough_k)

        # 2. 含未收录技能时标记 slot_uncertain=True，隐藏可能贴错槽位的卡顶角标，仅在底槽按名称提示已知优选
        mixed_cards = [
            {"skill_id": 219999},  # 未收录
            {"skill_id": 210418},  # 金 · 后段稳固
            {"skill_id": 210408},  # 白 · 后段稳定
        ]
        evals_mixed = sa.evaluate_skill_choices(1001, mixed_cards)
        self.assertTrue(all(x.get("slot_uncertain") for x in evals_mixed))
        specs_m, trough_m = hb.build_badges_for_mode("skill_choice", {"main_char_id": 1001}, last_skill_evals=evals_mixed)
        self.assertEqual(specs_m, [])
        self.assertIn("后段稳固", trough_m)
        self.assertIn("免贴错位", trough_m)

if __name__ == "__main__":
    unittest.main()
