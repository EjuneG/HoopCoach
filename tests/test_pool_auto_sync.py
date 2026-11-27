#!/usr/bin/env python3
"""
Windows / 跨平台新用户开箱自动同步角色 (★)、笔记池与自有器材池回归测试。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import analyzer
import dashboard_server
import equipment_advisor
import note_advisor


SAMPLE_LOG_CONTENT = """
[12:00:01] education.cs_edu_initactor_record {initactor_cfg_id = 1007}
[12:00:05] [EDU_MODEL] {
  borrow_note_info = {
    name = "TestFriend",
    note_data = {
      entry = {
        {entry_id = 501, entry_star_id = 2, entry_type = 5, entry_value = {harmony_initactor = 1002}},
        {entry_id = 101, entry_star_id = 3, entry_type = 2, entry_value = {property_id = 101, property_value = 40}}
      },
      entry_num = 2,
      initactor_id = 1030,
      is_locked = true,
      mark_type = 0,
      note_id = 888000111,
      note_rank = 7,
      note_score = 620
    }
  },
  edu_note_list = {
    {
      note_data = {
        entry = {
          {entry_id = 501, entry_star_id = 1, entry_type = 5, entry_value = {harmony_initactor = 1002}},
          {entry_id = 102, entry_star_id = 2, entry_type = 1, entry_value = {property_id = 102, property_value = 35}}
        },
        entry_num = 2,
        initactor_id = 1012,
        is_locked = false,
        mark_type = 0,
        note_id = 777000222,
        note_rank = 6,
        note_score = 510
      }
    }
  },
  supactor_dict = {
    [1] = {awake_level = 3, break_level = 4, card_id = 50001, cfg_id = 11002, level = 50},
    [2] = {awake_level = 3, break_level = 4, card_id = 90009, cfg_id = 11004, level = 50}
  },
  trammel_dict = {}
}
"""


class TestPoolAutoSync(unittest.TestCase):
    def setUp(self):
        self.chars, self.rules, self.rel_scores, self.entries_cat = note_advisor.load_static_db()

    def _create_mock_game_docs(self, base_dir: str) -> tuple[str, str]:
        docs_dir = os.path.join(base_dir, "Documents")
        log_dir = os.path.join(docs_dir, "Logs")
        ob_dir = os.path.join(docs_dir, "ob_123456")
        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(ob_dir, exist_ok=True)

        with open(os.path.join(ob_dir, "new_initactor.json"), "w", encoding="utf-8") as f:
            json.dump({"1002": True, "1046": True}, f)

        with open(os.path.join(ob_dir, "edu_recycle_note_quality_pref.json"), "w", encoding="utf-8") as f:
            json.dump([True, True, True, True, False, False, False], f)

        with open(os.path.join(ob_dir, "new_supactor.json"), "w", encoding="utf-8") as f:
            json.dump({"50001": True}, f)

        with open(os.path.join(ob_dir, "last_edu_supactor_1.json"), "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"id": 50001, "source_type": 1},
                    {"id": 90009, "source_type": 2},
                ],
                f,
            )

        with open(os.path.join(log_dir, "11_27___13_00_00___log.txt"), "w", encoding="utf-8") as f:
            f.write(SAMPLE_LOG_CONTENT)

        return docs_dir, log_dir

    def test_empty_dirs_cold_start_creates_no_stale_files_and_no_private_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            note_cache = os.path.join(tmp, "note_pool.json")
            equip_cache = os.path.join(tmp, "equip_pool.json")
            no_overrides = os.path.join(tmp, "missing_overrides.json")

            with (
                patch.object(note_advisor, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(note_advisor, "NOTE_VISUAL_CACHE_FILE", os.path.join(tmp, "missing_vis.json")),
                patch.object(note_advisor, "NOTE_OVERRIDES_FILE", no_overrides),
                patch.object(note_advisor, "resolve_game_dirs", return_value=("", "")),
                patch.object(equipment_advisor, "EQUIP_POOL_FILE", equip_cache),
                patch.object(equipment_advisor, "resolve_game_dirs", return_value=("", "")),
            ):
                np = note_advisor.sync_note_and_actor_pool(self.chars, self.entries_cat, docs_dir="", log_dir="")
                ep = equipment_advisor.sync_equip_pool(docs_dir="", log_dir="")

            self.assertEqual(np["owned_actors"], [])
            self.assertEqual(np["own_notes"], {})
            self.assertEqual(np["borrowed_notes"], {})
            self.assertEqual(ep["owned_cards"], {})
            self.assertFalse(os.path.exists(note_cache))
            self.assertFalse(os.path.exists(equip_cache))

    def test_windows_new_user_auto_sync_actors_notes_and_equips(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir, log_dir = self._create_mock_game_docs(tmp)
            note_cache = os.path.join(tmp, "note_pool.json")
            equip_cache = os.path.join(tmp, "equip_pool.json")
            no_overrides = os.path.join(tmp, "missing_overrides.json")

            with (
                patch.object(note_advisor, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(note_advisor, "NOTE_VISUAL_CACHE_FILE", os.path.join(tmp, "missing_vis.json")),
                patch.object(note_advisor, "NOTE_OVERRIDES_FILE", no_overrides),
                patch.object(equipment_advisor, "EQUIP_POOL_FILE", equip_cache),
            ):
                np = note_advisor.sync_note_and_actor_pool(
                    self.chars,
                    self.entries_cat,
                    docs_dir=docs_dir,
                    log_dir=log_dir,
                )
                ep = equipment_advisor.sync_equip_pool(docs_dir=docs_dir, log_dir=log_dir)

            # 1002 & 1046 来自 new_initactor.json，1007 来自选人记录，1012 来自自备笔记
            # 1030 仅在好友借用笔记里出现，不能误判为已拥有
            self.assertEqual(set(np["owned_actors"]), {1002, 1007, 1012, 1046})
            self.assertNotIn(1030, np["owned_actors"])
            self.assertIn("777000222", np["own_notes"])
            self.assertIn("888000111", np["borrowed_notes"])
            self.assertNotIn("900000000001033", np["borrowed_notes"])
            self.assertEqual(np["min_kept_rank"], 5)
            self.assertTrue(os.path.isfile(note_cache))

            # 50001 为自有高级智能篮球 (11002)，90009 为好友借用支援口哨 (11004)
            self.assertIn("11002", ep["owned_cards"])
            self.assertNotIn("11004", ep["owned_cards"])
            self.assertIn("11004", ep["borrowed_cards"])
            self.assertTrue(os.path.isfile(equip_cache))

    def test_dynamic_resolve_after_initial_empty_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir, log_dir = self._create_mock_game_docs(tmp)
            note_cache = os.path.join(tmp, "note_pool.json")
            no_overrides = os.path.join(tmp, "missing_overrides.json")

            with (
                patch.object(note_advisor, "DOCS_DIR", ""),
                patch.object(note_advisor, "LOG_DIR", ""),
                patch.object(note_advisor, "_INITIAL_DOCS_DIR", ""),
                patch.object(note_advisor, "_INITIAL_LOG_DIR", ""),
                patch.object(note_advisor, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(note_advisor, "NOTE_VISUAL_CACHE_FILE", os.path.join(tmp, "missing_vis.json")),
                patch.object(note_advisor, "NOTE_OVERRIDES_FILE", no_overrides),
                patch.object(note_advisor, "resolve_game_dirs", return_value=(docs_dir, log_dir)),
            ):
                np = note_advisor.sync_note_and_actor_pool(self.chars, self.entries_cat)

            self.assertIn(1002, np["owned_actors"])
            self.assertIn(1046, np["owned_actors"])

    def test_dashboard_engine_sync_local_pools_populates_box_stars(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir, log_dir = self._create_mock_game_docs(tmp)
            note_cache = os.path.join(tmp, "note_pool.json")
            equip_cache = os.path.join(tmp, "equip_pool.json")
            no_overrides = os.path.join(tmp, "missing_overrides.json")

            with (
                patch.object(note_advisor, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(note_advisor, "NOTE_VISUAL_CACHE_FILE", os.path.join(tmp, "missing_vis.json")),
                patch.object(note_advisor, "NOTE_OVERRIDES_FILE", no_overrides),
                patch.object(equipment_advisor, "EQUIP_POOL_FILE", equip_cache),
                patch.object(dashboard_server, "NOTE_POOL_FILE", note_cache),
                patch.object(dashboard_server, "EQUIP_POOL_FILE", equip_cache),
                patch.object(dashboard_server, "DETAILED_RUNS_FILE", os.path.join(tmp, "missing_runs.json")),
            ):
                eng = dashboard_server.DashboardEngine()
                # 同步前为干净空池
                box_before = eng.get_box()
                self.assertFalse(any(c["owned"] for c in box_before["characters"]))

                # 执行自动同步后，立即可见 ★ 已拥有角色、笔记与器材
                info = eng.sync_local_pools(docs_dir=docs_dir, log_dir=log_dir)
                self.assertTrue(info["ok"])
                self.assertEqual(info["owned_actors"], 4)
                self.assertEqual(info["owned_equips"], 1)

                box_after = eng.get_box()
                owned_ids = {c["id"] for c in box_after["characters"] if c["owned"]}
                self.assertEqual(owned_ids, {1002, 1007, 1012, 1046})
                self.assertEqual(len(box_after["own_notes"]), 1)
                self.assertEqual(len(box_after["borrowed_notes"]), 1)
                self.assertEqual(len(box_after["owned_equips"]), 1)

    def test_optional_local_note_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir, log_dir = self._create_mock_game_docs(tmp)
            note_cache = os.path.join(tmp, "note_pool.json")
            overrides_file = os.path.join(tmp, "note_overrides.json")
            with open(overrides_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "archived_own_note_ids": ["777000222"],
                        "manual_borrow_overrides": {
                            "9990001": {
                                "note_id": 9990001,
                                "actor_id": 1033,
                                "actor_name": "卧云",
                                "rank": 6,
                                "score": 480,
                                "entry_num": 10,
                                "friend": "CustomFriend",
                                "last_seen": "manual",
                            }
                        },
                    },
                    f,
                )

            with (
                patch.object(note_advisor, "NOTE_POOL_CACHE_FILE", note_cache),
                patch.object(note_advisor, "NOTE_VISUAL_CACHE_FILE", os.path.join(tmp, "missing_vis.json")),
                patch.object(note_advisor, "NOTE_OVERRIDES_FILE", overrides_file),
            ):
                np = note_advisor.sync_note_and_actor_pool(
                    self.chars,
                    self.entries_cat,
                    docs_dir=docs_dir,
                    log_dir=log_dir,
                )

            self.assertNotIn("777000222", np["own_notes"])
            self.assertIn("9990001", np["borrowed_notes"])

    def test_analyzer_rejects_invalid_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_dir = os.path.join(tmp, "nonexistent_logs")
            with (
                patch.object(analyzer, "LOG_DIR", ""),
                patch.object(analyzer, "_INITIAL_LOG_DIR", ""),
                patch.object(analyzer, "resolve_game_dirs", return_value=("", "")),
            ):
                with self.assertRaises(FileNotFoundError):
                    analyzer.extract_all_runs(log_dir=missing_dir)
                with self.assertRaises(FileNotFoundError):
                    analyzer.extract_all_runs()

    def test_analyzer_prevents_wiping_archive_on_empty_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_log_dir = os.path.join(tmp, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            manifest_path = os.path.join(runs_dir, "archive_manifest.json")
            out_path = os.path.join(runs_dir, "detailed_runs.json")
            os.makedirs(empty_log_dir, exist_ok=True)
            os.makedirs(arch_dir, exist_ok=True)

            existing_runs = [{"run_id": "old_run_1", "final_rank": 9888, "seq_no": 1}]
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(existing_runs, f)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 3,
                        "log_meta": {},
                        "runs_order": ["old_run_1"],
                        "runs_meta": [{"run_id": "old_run_1", "log": "old.txt", "end_log": "old.txt"}],
                    },
                    f,
                )

            with (
                patch.object(analyzer, "RUNS_DIR", runs_dir),
                patch.object(analyzer, "ARCHIVE_DIR", arch_dir),
                patch.object(analyzer, "MANIFEST_PATH", manifest_path),
                patch.object(analyzer, "OUT_PATH", out_path),
            ):
                with self.assertRaises(RuntimeError):
                    analyzer.extract_all_runs(force_rebuild=False, log_dir=empty_log_dir)

            with open(out_path, "r", encoding="utf-8") as f:
                preserved = json.load(f)
            self.assertEqual(preserved, existing_runs)

    def test_analyzer_explicit_log_dir_overrides_empty_module_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "Logs")
            runs_dir = os.path.join(tmp, "runs")
            arch_dir = os.path.join(runs_dir, "archive")
            manifest_path = os.path.join(runs_dir, "archive_manifest.json")
            out_path = os.path.join(runs_dir, "detailed_runs.json")
            os.makedirs(log_dir, exist_ok=True)

            with (
                patch.object(analyzer, "LOG_DIR", ""),
                patch.object(analyzer, "_INITIAL_LOG_DIR", ""),
                patch.object(analyzer, "RUNS_DIR", runs_dir),
                patch.object(analyzer, "ARCHIVE_DIR", arch_dir),
                patch.object(analyzer, "MANIFEST_PATH", manifest_path),
                patch.object(analyzer, "OUT_PATH", out_path),
            ):
                runs = analyzer.extract_all_runs(force_rebuild=False, log_dir=log_dir)
            self.assertEqual(runs, [])
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            self.assertEqual(manifest["schema_version"], analyzer.MANIFEST_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
