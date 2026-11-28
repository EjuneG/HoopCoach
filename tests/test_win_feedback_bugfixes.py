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

    def test_bug1_schema_upgrade_recovers_archived_runs_and_filters_v3_polluted_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            os.makedirs(log_dir)
            os.makedirs(arch_dir)

            # 现存新日志：1 局正常结算 (9603)
            log_fp = os.path.join(log_dir, "11_25___00_55_44___log.txt")
            from test_analyzer_regression import _make_train_cmds_block, _make_opening_notes_block

            parts = [
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1046}\n",
                _make_opening_notes_block((620, 610, 600)),
            ]
            for turn in range(1, 17):
                parts.append(_make_train_cmds_block(exp_round=turn, highlight_idx=1))
                parts.append("request_call:    education.cs_train    {cmd_idx = 1}\n")
            parts.append(
                "[11-25 01:10:00]:[INFO]    [NETWORK]    dispatch:    education.sc_education_result    {\n"
                "  new_rank = 9603,\n"
                "  new_record = true\n"
                "}\n"
            )
            with open(log_fp, "w", encoding="utf-8") as f:
                f.write("".join(parts))

            # 1) 原始日志已被游戏轮转清理的合法老归档（希雅 10247）：必须被召回
            old_good_arch = {
                "seq_no": 1,
                "run_id": "11_17___15_27_34_r1",
                "log": "11_17___15_27_34___log.txt",
                "end_log": "11_17___15_27_34___log.txt",
                "final_rank": 10247,
                "lineup": "⭐希雅+真崎/百目鬼/闪堂/望月",
                "final_stats": {"101": 1600, "102": 1650, "103": 1700, "104": 1650, "105": 1622, "106": 1650},
                "stat_sum": 9872,
            }
            with open(os.path.join(arch_dir, "11_17___15_27_34_r1.json"), "w", encoding="utf-8") as f:
                json.dump(old_good_arch, f, ensure_ascii=False)

            # 2) 原始日志已被清理但曾被旧版 v3 误采为队伍总评价 (19865) 的坏归档：必须被过滤
            old_bad_arch = {
                "seq_no": 2,
                "run_id": "11_16___12_00_00_r1",
                "log": "11_16___12_00_00___log.txt",
                "end_log": "11_16___12_00_00___log.txt",
                "final_rank": 19865,
                "lineup": "⭐希雅+真崎/百目鬼/闪堂/望月",
                "final_stats": {"101": 1500, "102": 1500, "103": 1500, "104": 1500, "105": 1500, "106": 1500},
                "stat_sum": 9000,
            }
            with open(os.path.join(arch_dir, "11_16___12_00_00_r1.json"), "w", encoding="utf-8") as f:
                json.dump(old_bad_arch, f, ensure_ascii=False)

            # 3) 对应现存日志但已被新解析器淘汰的幽灵假局归档：不得复活
            stale_current_ghost = {
                "seq_no": 3,
                "run_id": "11_25___00_55_44_r99",
                "log": "11_25___00_55_44___log.txt",
                "end_log": "11_25___00_55_44___log.txt",
                "final_rank": 10500,
                "lineup": "⭐希雅+真崎/百目鬼/闪堂/望月",
                "final_stats": {"101": 1700, "102": 1700, "103": 1700, "104": 1700, "105": 1700, "106": 1600},
                "stat_sum": 10100,
            }
            with open(os.path.join(arch_dir, "11_25___00_55_44_r99.json"), "w", encoding="utf-8") as f:
                json.dump(stale_current_ghost, f, ensure_ascii=False)

            # 模拟旧版 v4 manifest（已把老归档丢掉，仅登记了现存新日志的 1 局）
            st = os.stat(log_fp)
            manifest_path = os.path.join(runs_dir, "archive_manifest.json")
            out_path = os.path.join(runs_dir, "detailed_runs.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 4,
                        "log_meta": {
                            "11_25___00_55_44___log.txt": {"mtime": round(st.st_mtime, 2), "size": st.st_size}
                        },
                        "runs_order": ["11_25___00_55_44_r1"],
                        "runs_meta": [
                            {
                                "run_id": "11_25___00_55_44_r1",
                                "log": "11_25___00_55_44___log.txt",
                                "end_log": "11_25___00_55_44___log.txt",
                            }
                        ],
                    },
                    f,
                )

            with patch.object(az, "LOG_DIR", log_dir), \
                 patch.object(az, "RUNS_DIR", runs_dir), \
                 patch.object(az, "ARCHIVE_DIR", arch_dir), \
                 patch.object(az, "MANIFEST_PATH", manifest_path), \
                 patch.object(az, "OUT_PATH", out_path):
                runs = az.extract_all_runs(force_rebuild=False)
                run_ids = [r["run_id"] for r in runs]
                self.assertEqual(run_ids, ["11_17___15_27_34_r1", "11_25___00_55_44_r1"])
                self.assertEqual([r["final_rank"] for r in runs], [10247, 9603])
                self.assertEqual([r["seq_no"] for r in runs], [1, 2])

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
        # 1. 严格保留服务器下发原始槽位顺序（[0]左 / [1]中 / [2]右），不按稀有度擅自重排
        known_cards = [
            {"skill_id": 210408},  # slot 0 · 白 · 后段稳定
            {"skill_id": 210223},  # slot 1 · 蓝 · 灵巧背打（动作卡）
            {"skill_id": 210418},  # slot 2 · 金 · 后段稳固
        ]
        evals_known = sa.evaluate_skill_choices(1001, known_cards)
        self.assertEqual([x["skill_id"] for x in evals_known], [210408, 210223, 210418])
        self.assertEqual([x["slot_idx"] for x in evals_known], [0, 1, 2])
        self.assertFalse(any(x.get("slot_uncertain") for x in evals_known))
        specs_k, trough_k = hb.build_badges_for_mode("skill_choice", {"main_char_id": 1001}, last_skill_evals=evals_known)
        self.assertGreaterEqual(len(specs_k), 1)
        self.assertIn("后段稳固", trough_k)

        # 2. 混入未收录技能时，已知槽位仍精准对齐原下标，未收录卡本身标记 unknown
        mixed_cards = [
            {"skill_id": 219999},  # slot 0 · 未收录
            {"skill_id": 210418},  # slot 1 · 金 · 后段稳固
            {"skill_id": 210408},  # slot 2 · 白 · 后段稳定
        ]
        evals_mixed = sa.evaluate_skill_choices(1001, mixed_cards)
        self.assertEqual([x["skill_id"] for x in evals_mixed], [219999, 210418, 210408])
        self.assertTrue(evals_mixed[0]["is_unknown"])
        self.assertEqual(evals_mixed[0]["style_key"], "unknown")
        self.assertEqual(evals_mixed[1]["slot_idx"], 1)
        specs_m, trough_m = hb.build_badges_for_mode("skill_choice", {"main_char_id": 1001}, last_skill_evals=evals_mixed)
        self.assertIn("后段稳固", trough_m)

    def test_skill_catalog_full_coverage_and_upgrade_chain(self):
        import skill_advisor as sa

        cat = sa.reload_skill_catalog()
        skills = cat.get("skills") or {}
        self.assertGreaterEqual(len(skills), 600)
        # 校验真值纠偏：200101=投射擅长，210286=速度优势，210413=前期加持，2001001=澪奈被动斗志昂扬
        self.assertEqual(skills["200101"]["name"], "投射擅长")
        self.assertEqual(skills["210286"]["name"], "速度优势")
        self.assertEqual(skills["210413"]["name"], "前期加持")
        self.assertEqual(skills["2001001"]["name"], "斗志昂扬")
        self.assertEqual(skills["2001001"]["owner_char_id"], 1001)

        # 1) 已学上位金技 210418(后段稳固) 时，下位白技 210408(后段稳定) 自动识别为 ♻️ 已学上位
        ev_sup = sa.evaluate_skill_choices(
            1001,
            [{"skill_id": 210408}, {"skill_id": 210406}],
            learned_skills={210418},
        )
        by_sid = {x["skill_id"]: x for x in ev_sup}
        self.assertTrue(by_sid[210408]["is_learned"])
        self.assertTrue(by_sid[210408]["is_superseded"])
        self.assertIn("已学上位", by_sid[210408]["badge_text"])
        self.assertEqual(by_sid[210406]["rank"], 1)

        # 2) 已学下位白技 210408 时，覆盖升级金技 210418(净评+25) 优先级低于全新同档金技 210416(净评+50)
        ev_upg = sa.evaluate_skill_choices(
            1001,
            [{"skill_id": 210418}, {"skill_id": 210416}],
            learned_skills={210408},
        )
        by_upg = {x["skill_id"]: x for x in ev_upg}
        self.assertTrue(by_upg[210418]["upgrades_learned"])
        self.assertIn("覆盖下位", by_upg[210418]["reason"])
        self.assertEqual(by_upg[210416]["rank"], 1)
        self.assertGreater(by_upg[210416]["score"], by_upg[210418]["score"])

        # 3) 硬性限定（210171 快攻倾向 受组织者天赋加成；203201 篮板倾向 限中锋）对纯得分手小前 1001(澪奈) 判空转，
        #    但普通跨职责白技（210307 二次手感）不误判空转，仅评分低于契合得分手白技（210296 远投擅长）
        ev_roles = sa.evaluate_skill_choices(
            1001,
            [{"skill_id": 210171}, {"skill_id": 210307}, {"skill_id": 210296}],
        )
        by_r = {x["skill_id"]: x for x in ev_roles}
        self.assertFalse(by_r[210171]["role_match"])
        self.assertEqual(by_r[210171]["style_key"], "warn")
        self.assertTrue(by_r[210307]["role_match"])
        self.assertEqual(by_r[210296]["rank"], 1)
        self.assertGreater(by_r[210296]["score"], by_r[210307]["score"])

        ev_pos = sa.evaluate_skill_choices(1001, [{"skill_id": 203201}, {"skill_id": 210408}])
        by_p = {x["skill_id"]: x for x in ev_pos}
        self.assertFalse(by_p[203201]["role_match"])
        self.assertFalse(by_p[203201]["pos_match"])
        self.assertIn("位置不符", by_p[203201]["badge_text"])

    def test_cross_day_no_stale_train_cmds_or_sig_swallow_and_partial_line(self):
        import win_launcher as wl

        def _make_cmds_block(boost_idx: int, gain: int) -> str:
            parts = []
            for i in range(1, 7):
                g = gain if i == boost_idx else 10
                parts.append(
                    f"[{i}] = {{"
                    f"before_bonus_property_tbl = {{ [1] = {{key = {100 + i},value = {g}}} }},"
                    f"company_supactor_tbl = {{}},"
                    f"company_teammate_tbl = {{}},"
                    f"fail_rate = 0,"
                    f"idx = {i},"
                    f"is_edu_manager = false,"
                    f"ps = -15,"
                    f"pt = 10,"
                    f"train_type = {i}"
                    f"}}"
                )
            return "[EDU_MODEL] 更新育成数据 {\n  train_cmds = { " + ", ".join(parts) + " }\n}\n"

        with tempfile.TemporaryDirectory() as tmp:
            log_fp = os.path.join(tmp, "11_28___09_00_00___log.txt")
            day1 = (
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1002}\n"
                "[SceneMgr] SetActiveScene: ycjm02\n"
                "[EDU_MODEL] 更新育成数据    {\n"
                "  experience_round = 11,\n"
                "  round_type = 1,\n"
                "  ps = 100\n"
                "}\n"
                + _make_cmds_block(boost_idx=1, gain=90)
            )
            with open(log_fp, "w", encoding="utf-8") as f:
                f.write(day1)

            mirror = LiveLogMirror(tmp)
            try:
                self.assertTrue(mirror.attach_latest())
                self.assertEqual(len(mirror.last_train_cmds), 6)

                class _DummyVar:
                    def __init__(self, v=""):
                        self.v = v

                    def set(self, v):
                        self.v = str(v)

                    def get(self):
                        return self.v

                class _DummyHud:
                    def __init__(self):
                        self._last_turn_scored = None
                        self._last_wk_adv = None
                        self._last_story_adv = None
                        self._last_skill_evals = None
                        self.turn_renders = []

                    def render_turn(self, state, scored, extra_tip=""):
                        self._last_turn_scored = scored
                        self.turn_renders.append((int(state.get("exp_round") or 0), scored[0]["idx"]))

                    def refresh_scene_overlay(self, state):
                        pass

                app = wl.LQSNCoachDesktopApp.__new__(wl.LQSNCoachDesktopApp)
                app.mirror = mirror
                app.hud = _DummyHud()
                app.strategy_name = "training_flow"
                app.strategy = lc.load_strategy("training_flow")
                app.hud_cfg = {"focus": {"stat": None, "bonus": 1.15}}
                app._last_rendered_sig = ""
                app._logged_runtime_errors = set()
                app.var_live_preview = _DummyVar()

                app._render_current_state(force=False)
                self.assertEqual(app.hud.turn_renders[-1], (11, 1))

                # 玩家在 Day 11 点击训练 -> 立即清空旧训练面板与 HUD 缓存，绝不把 Day 11 提示带入 Day 12
                with open(log_fp, "a", encoding="utf-8") as f:
                    f.write("request_call:    education.cs_train    {cmd_idx = 1}\n")
                    f.write("[EDU_MODEL]UnlockData\n")
                    f.write("[EDU_MODEL] 更新育成数据    {\n  experience_round = 12,\n  round_type = 1,\n  ps = 130\n}\n")
                mirror.poll()
                self.assertEqual(mirror.last_train_cmds, [])
                app._render_current_state(force=False)
                self.assertIsNone(app.hud._last_turn_scored)

                # 先写入半截行（无换行符），poll 应暂存等待完整行，随后补完新一天 train_cmds（首选变为 4 号）
                day2_cmds = _make_cmds_block(boost_idx=4, gain=120)
                half = len(day2_cmds) // 2
                with open(log_fp, "a", encoding="utf-8") as f:
                    f.write(day2_cmds[:half])
                mirror.poll()
                self.assertEqual(mirror.last_train_cmds, [])

                with open(log_fp, "a", encoding="utf-8") as f:
                    f.write(day2_cmds[half:])
                evs = mirror.poll()
                self.assertTrue(evs)
                self.assertEqual(len(mirror.last_train_cmds), 6)

                # 即使 mode/exp_round/round/ps/gold 与上一包完全一致，也绝不吞掉新一天的 train_cmds
                app._render_current_state(force=False)
                self.assertEqual(app.hud.turn_renders[-1], (12, 4))
            finally:
                mirror.close()

    def test_dashboard_refresh_all_scans_new_completed_runs(self):
        import dashboard_server as ds
        import note_advisor as na
        import equipment_advisor as ea
        from test_analyzer_regression import _make_synthetic_run_text

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = os.path.join(tmp, "Documents")
            log_dir = os.path.join(docs_dir, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            os.makedirs(log_dir, exist_ok=True)
            os.makedirs(arch_dir, exist_ok=True)

            runs_json = os.path.join(runs_dir, "detailed_runs.json")
            manifest_json = os.path.join(runs_dir, "manifest.json")
            note_cache = os.path.join(tmp, "note_pool.json")
            equip_cache = os.path.join(tmp, "equip_pool.json")
            snap_dir = os.path.join(tmp, "dash_data")
            os.makedirs(snap_dir, exist_ok=True)
            snap_js = os.path.join(snap_dir, "snapshot.js")

            with (
                patch.object(az, "RUNS_DIR", runs_dir),
                patch.object(az, "ARCHIVE_DIR", arch_dir),
                patch.object(az, "OUT_PATH", runs_json),
                patch.object(az, "MANIFEST_PATH", manifest_json),
                patch.object(ds, "DETAILED_RUNS_FILE", runs_json),
                patch.object(ds, "ARCHIVE_DIR", arch_dir),
                patch.object(ds, "NOTE_POOL_FILE", note_cache),
                patch.object(ds, "EQUIP_POOL_FILE", equip_cache),
                patch.object(ds, "DASHBOARD_DATA_DIR", snap_dir),
                patch.object(na, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(na, "NOTE_OVERRIDES_FILE", os.path.join(tmp, "no_ov.json")),
                patch.object(ea, "EQUIP_POOL_FILE", equip_cache),
            ):
                eng = ds.DashboardEngine()
                eng.set_game_dirs(docs_dir=docs_dir, log_dir=log_dir)
                self.assertEqual(len(eng.get_overview()["runs"]), 0)

                # 模拟玩家刚打完一局新完赛日志，直接在网页点击『重新加载』(/api/refresh -> engine.refresh_all)
                log_fp = os.path.join(log_dir, "11_28___12_30_00___log.txt")
                with open(log_fp, "w", encoding="utf-8") as f:
                    f.write(_make_synthetic_run_text())

                res = eng.refresh_all()
                self.assertTrue(res["ok"])
                self.assertTrue(res["runs_scanned"])
                self.assertEqual(res["new_runs"], 1)
                self.assertEqual(res["runs_total"], 1)
                self.assertEqual(len(eng.get_overview()["runs"]), 1)
                self.assertTrue(os.path.isfile(snap_js))

    def test_win_launcher_initial_geometry_and_combobox_wheel_guard(self):
        import win_launcher as wl
        from unittest.mock import MagicMock

        if wl.tk is None:
            self.skipTest("tkinter unavailable")

        app = None
        try:
            with (
                patch.object(wl.pc, "resolve_game_dirs", return_value=("", "")),
                patch.object(wl.pc, "WindowsFloatingHUD", return_value=MagicMock(diegetic_mode=True)),
                patch.object(wl.LQSNCoachDesktopApp, "_prompt_first_time_dir", lambda self: None),
                patch.object(wl.LQSNCoachDesktopApp, "_start_check_updates", lambda self, silent=True: None),
            ):
                try:
                    app = wl.LQSNCoachDesktopApp()
                except Exception as exc:
                    if "display" in str(exc).lower() or "tclerror" in type(exc).__name__.lower():
                        self.skipTest(f"headless display unavailable: {exc}")
                    raise

                # 移到屏幕外完成真实布局测量与事件派发，避免测试闪烁抢焦点
                cur_size = app.root.geometry().split("+")[0]
                app.root.geometry(f"{cur_size}+10000+10000")
                app.root.update()

                avail_w, avail_h = app._get_screen_work_area()
                if avail_h >= 780:
                    self.assertFalse(
                        bool(app._scroll_bar.winfo_ismapped()),
                        "常规桌面分辨率下初始窗口应完整展示首页卡片，无需出现滚动条",
                    )
                    btn_bottom = (
                        app.btn_open_dashboard.winfo_rooty()
                        - app._scroll_canvas.winfo_rooty()
                        + app.btn_open_dashboard.winfo_height()
                    )
                    self.assertLessEqual(
                        btn_bottom,
                        app._scroll_canvas.winfo_height() + 2,
                        "『打开作战室网页』按钮在初始窗口中必须无需滚动直接可见",
                    )

                # 验证鼠标滚轮划过中间三个下拉框时绝不篡改当前选中项
                strat_before = app.var_strategy.get()
                fstat_before = app.var_focus_stat.get()
                fbonus_before = app.var_focus_bonus.get()
                for cb in (app.cb_strategy, app.cb_focus_stat, app.cb_focus_bonus):
                    self.assertIsNotNone(cb)
                    res = app._on_combobox_mousewheel(
                        type("Ev", (), {"widget": cb, "delta": -120, "num": None})()
                    )
                    self.assertEqual(res, "break")
                    cb.event_generate("<MouseWheel>", delta=-120)
                    cb.event_generate("<MouseWheel>", delta=120)
                    app.root.update()

                self.assertEqual(app.var_strategy.get(), strat_before)
                self.assertEqual(app.var_focus_stat.get(), fstat_before)
                self.assertEqual(app.var_focus_bonus.get(), fbonus_before)

                # 当窗口被手动压矮需要滚动时，在中间下拉框上滚轮应顺畅滚动外层页面且仍不改值
                app.root.minsize(468, 400)
                app.root.geometry("520x420+10000+10000")
                app.root.update()
                app._sync_scroll_layout()
                app.root.update()
                y0 = app._scroll_canvas.yview()[0]
                app._on_combobox_mousewheel(
                    type("Ev", (), {"widget": app.cb_strategy, "delta": -120, "num": None})()
                )
                y1 = app._scroll_canvas.yview()[0]
                self.assertGreater(y1, y0)
                self.assertEqual(app.var_strategy.get(), strat_before)
        finally:
            if app is not None:
                app._closing = True
                try:
                    app.root.destroy()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
