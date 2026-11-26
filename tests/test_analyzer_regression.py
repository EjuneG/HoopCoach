#!/usr/bin/env python3
"""Regression tests for analyzer.py data accuracy and multi-file log stitching."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import analyzer as az  # noqa: E402
import live_coach as lc  # noqa: E402


def _make_train_cmds_block(
    *,
    exp_round: int = 1,
    round_type: int = 1,
    weekend_train: bool = False,
    trammel_map: dict[int, int] | None = None,
    highlight_idx: int = 1,
) -> str:
    """Construct a compact [EDU_MODEL] block with 6 train_cmds and optional card trammel."""
    trammel_map = trammel_map or {5001: 40, 5002: 40}
    sup_dict_entries = ", ".join(
        f"[{i}] = {{awake_level = 1,break_level = 4,card_id = {cid},cfg_id = {16000 + i},trammel = {val}}}"
        for i, (cid, val) in enumerate(trammel_map.items(), 1)
    )
    cmds_lines = []
    for idx in range(1, 7):
        base_v = 30 if idx == highlight_idx else 10
        stat_id = 100 + idx
        cmds_lines.append(
            f"[{idx}] = {{"
            f"before_bonus_property_tbl = {{ [1] = {{key = {stat_id},value = {base_v}}} }},"
            f"company_supactor_tbl = {{}},"
            f"company_teammate_tbl = {{}},"
            f"fail_rate = 0,"
            f"idx = {idx},"
            f"is_edu_manager = false,"
            f"ps = -15,"
            f"pt = 10,"
            f"train_type = {idx}"
            f"}}"
        )
    wt_flag = "true" if weekend_train else "false"
    extra_train_cmds = (
        f"  train_cmds = {{ {', '.join(cmds_lines)} }},\n"
        + ("  team_building_list = { {building_cfg_id = 1} },\n" if round_type == 2 else "")
    )
    return (
        "[EDU_MODEL] 更新育成数据 {\n"
        f"  experience_round = {exp_round},\n"
        f"  round_type = {round_type},\n"
        f"  weekend_train = {wt_flag},\n"
        "  ps = 110,\n"
        "  ps_max = 130,\n"
        "  currency_list = { {key = 1,value = 200} },\n"
        "  initactor = {\n"
        "    cfg_id = 1046,\n"
        "    property_tbl = {\n"
        "      [1] = {key = 101,value = 500},\n"
        "      [2] = {key = 102,value = 600},\n"
        "      [3] = {key = 103,value = 400},\n"
        "      [4] = {key = 104,value = 450},\n"
        "      [5] = {key = 105,value = 420},\n"
        "      [6] = {key = 106,value = 410}\n"
        "    },\n"
        "    property_max_tbl = {\n"
        "      [1] = {key = 101,value = 1800},\n"
        "      [2] = {key = 102,value = 1800},\n"
        "      [3] = {key = 103,value = 1800},\n"
        "      [4] = {key = 104,value = 1800},\n"
        "      [5] = {key = 105,value = 1800},\n"
        "      [6] = {key = 106,value = 1800}\n"
        "    }\n"
        "  },\n"
        f"  supactor_dict = {{ {sup_dict_entries} }},\n"
        f"{extra_train_cmds}"
        "}\n"
    )


def _make_opening_notes_block(scores: tuple[int, int, int] = (690, 606, 690)) -> str:
    return (
        "[EDU_MODEL] 更新育成数据 {\n"
        "  borrow_note_info = {\n"
        f"    note_data = {{ note_score = {scores[0]} }}\n"
        "  },\n"
        "  edu_note_list = {\n"
        f"    [1] = {{ note_data = {{ note_score = {scores[1]} }} }},\n"
        f"    [2] = {{ note_data = {{ note_score = {scores[2]} }} }}\n"
        "  }\n"
        "}\n"
    )


def _make_synthetic_run_text() -> str:
    parts = ["request_call:    education.cs_start_education    {initactor_cfg_id = 1046}\n"]
    # 超过 45KB 前置噪声，验证 note_scores 不被 seg[:45000] 截断
    parts.append("#" + ("x" * 52000) + "\n")
    parts.append(_make_opening_notes_block((690, 606, 685)))
    # 后续另一个 [EDU_MODEL] 笔记快照不应导致 note_scores 重复
    parts.append(_make_opening_notes_block((999, 999, 999)))

    # 16 个训练回合：第 1~14 训为平日（100% 跟随首选 highlight_idx），
    # 第 15~16 训为周六加训（weekend_train=True，故意选非最优按钮 6 号，验证不污染平日 coach_match_pct）
    # 其中第 3 训把 5001(16001) 从 85 推至 100（在第 4 训前快照体现），
    # 第 16 训（最后一训）把 5002(16002) 从 90 推至 100（在 final_st 体现）。
    for turn in range(1, 17):
        if turn <= 14:
            tram_5001 = 100 if turn >= 4 else 85
            tram_5002 = 90
            hi = (turn % 5) + 1
            parts.append(
                _make_train_cmds_block(
                    exp_round=turn,
                    round_type=1,
                    weekend_train=False,
                    trammel_map={5001: tram_5001, 5002: tram_5002},
                    highlight_idx=hi,
                )
            )
            parts.append(f"request_call:    education.cs_train    {{cmd_idx = {hi}}}\n")
        else:
            parts.append(
                _make_train_cmds_block(
                    exp_round=13 if turn == 15 else 20,
                    round_type=2,
                    weekend_train=True,
                    trammel_map={5001: 100, 5002: 90},
                    highlight_idx=1,
                )
            )
            parts.append("request_call:    education.cs_train    {cmd_idx = 6}\n")

    # 老虎机（同时覆盖 slot_id 与 slot_machine_id）
    parts.append("request_call:    education.cs_select_edu_slot_machines    {slot_id = 63}\n")
    parts.append("request_call:    education.cs_select_edu_slot_machines    {slot_machine_id = 61}\n")

    # 终局快照：5002 在第 16 训后满 100，并结算 new_rank
    parts.append(
        _make_train_cmds_block(
            exp_round=49,
            round_type=1,
            weekend_train=False,
            trammel_map={5001: 100, 5002: 100},
            highlight_idx=1,
        )
    )
    parts.append("new_rank = 9650\n")
    return "".join(parts)


class TestAnalyzerRegression(unittest.TestCase):
    def test_parse_train_cmd_block_single_definition_and_importable(self):
        self.assertTrue(hasattr(az, "parse_train_cmd_block"))
        self.assertIs(lc.parse_train_cmd_block, az.parse_train_cmd_block)
        cmds = az.parse_train_cmd_block(_make_train_cmds_block(highlight_idx=2))
        self.assertEqual(len(cmds), 6)
        self.assertEqual(cmds[1]["total_stat_gain"], 30)

    def test_analyzer_fixes_slot_notes_trammel_and_weekend_split(self):
        seg = _make_synthetic_run_text()
        strat = lc.load_strategy("training_flow")
        res = az.build_single_run_archive(
            {"log": "test.txt", "end_log": "test.txt", "run_index": 1, "seg": seg},
            seq_no=1,
            lc=lc,
            strategy=strat,
        )
        self.assertIsNotNone(res)
        s = res["summary"]
        d = res["detail"]

        # 1. slot_machine_ids 兼容 slot_id 与 slot_machine_id
        self.assertEqual(s["slot_machine_ids"], [63, 61])
        self.assertEqual(s["slot_machines"], 2)

        # 2. note_scores 不受 45KB 截断且只采首块
        self.assertEqual(s["note_scores"], [690, 606, 685])

        # 3. equip_max_turns 消除晚记 1 训偏移，且记录 equip_max_rounds
        self.assertEqual(s["equip_max_turns"]["16001"], 3)
        self.assertEqual(s["equip_max_turns"]["16002"], 16)
        self.assertEqual(s["equip_max_rounds"]["16001"], 3)
        self.assertEqual(s["equip_max_rounds"]["16002"], 20)

        # 4. 周六加训不污染平日 coach_match_pct，且完整保留 train_count 兼容与拆分字段
        self.assertEqual(s["train_count"], 16)
        self.assertEqual(s["weekday_train_count"], 14)
        self.assertEqual(s["weekend_train_count"], 2)
        self.assertEqual(s["coach_match_pct"], 100.0)
        self.assertEqual(s["weekend_count"], 2)
        self.assertEqual(sum(1 for r in d["weekday_timeline"] if r["is_weekend_train"]), 2)
        self.assertTrue(all(sub["turn_no"] not in (15, 16) for sub in d["top_suboptimal_turns"]))

    def test_multifile_mtime_sort_and_three_file_incremental_stitch(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            os.makedirs(log_dir)
            os.makedirs(arch_dir)

            f1 = os.path.join(log_dir, "12_31___23_50_00___log.txt")
            f2 = os.path.join(log_dir, "01_01___00_10_00___log.txt")
            f3 = os.path.join(log_dir, "01_01___01_00_00___log.txt")

            seg1 = [
                "request_call:    education.cs_start_education    {initactor_cfg_id = 1046}\n",
                _make_opening_notes_block((680, 610, 620)),
            ]
            for turn in range(1, 7):
                seg1.append(_make_train_cmds_block(exp_round=turn, trammel_map={5001: 50, 5002: 50}, highlight_idx=1))
                seg1.append("request_call:    education.cs_train    {cmd_idx = 1}\n")

            seg2 = ["request_call:    education.cs_restore_education    {}\n"]
            for turn in range(7, 13):
                seg2.append(_make_train_cmds_block(exp_round=turn, trammel_map={5001: 80, 5002: 80}, highlight_idx=2))
                seg2.append("request_call:    education.cs_train    {cmd_idx = 2}\n")

            seg3 = ["request_call:    education.cs_restore_education    {}\n"]
            for turn in range(13, 17):
                seg3.append(_make_train_cmds_block(exp_round=turn, trammel_map={5001: 100, 5002: 95}, highlight_idx=3))
                seg3.append("request_call:    education.cs_train    {cmd_idx = 3}\n")
            seg3.append("request_call:    education.cs_select_edu_slot_machines    {slot_id = 62}\n")
            seg3.append("new_rank = 9123\n")

            with patch.object(az, "LOG_DIR", log_dir), \
                 patch.object(az, "RUNS_DIR", runs_dir), \
                 patch.object(az, "ARCHIVE_DIR", arch_dir), \
                 patch.object(az, "MANIFEST_PATH", os.path.join(runs_dir, "archive_manifest.json")), \
                 patch.object(az, "OUT_PATH", os.path.join(runs_dir, "detailed_runs.json")):
                with open(f1, "w", encoding="utf-8") as fp:
                    fp.write("".join(seg1))
                with open(f2, "w", encoding="utf-8") as fp:
                    fp.write("".join(seg2))
                os.utime(f1, (1000, 1000))
                os.utime(f2, (2000, 2000))
                runs_before = az.extract_all_runs()
                self.assertEqual(runs_before, [])

                with open(f3, "w", encoding="utf-8") as fp:
                    fp.write("".join(seg3))
                os.utime(f3, (3000, 3000))
                runs_after = az.extract_all_runs()
                self.assertEqual(len(runs_after), 1)
                run = runs_after[0]
                self.assertEqual(run["log"], "12_31___23_50_00___log.txt")
                self.assertEqual(run["end_log"], "01_01___01_00_00___log.txt")
                self.assertEqual(run["train_count"], 16)
                self.assertEqual(run["final_rank"], 9123)
                self.assertEqual(run["note_scores"], [680, 610, 620])
                self.assertEqual(run["slot_machine_ids"], [62])


if __name__ == "__main__":
    unittest.main()
