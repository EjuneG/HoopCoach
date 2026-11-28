#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v0120_stranger_friendly.py — v0.12.0 陌生人友好体验、快捷自动报错与作战室改版回归测试
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import bootstrap
import issue_reporter
import live_coach as lc
import platform_compat as pc
import win_launcher


class _DummyVar:
    def __init__(self, value="") -> None:
        self._val = value

    def get(self):
        return self._val

    def set(self, val) -> None:
        self._val = val


class _DummyRoot:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []

    def after(self, ms, cb):
        self.after_calls.append((ms, cb))
        return f"after_{len(self.after_calls)}"


class TestAutoErrorReporting(unittest.TestCase):
    def test_auto_error_payload_sanitizes_paths_and_tokens(self) -> None:
        raw_tb = (
            'Traceback (most recent call last):\n'
            '  File "C:\\Users\\Alice\\Desktop\\HoopCoach\\src\\win_launcher.py", line 12\n'
            'RuntimeError: failed with ghp_1234567890abcdef123456 and /home/bob/secret\n'
        )
        payload = issue_reporter.build_auto_error_payload(
            "读取 C:\\Users\\Alice\\Logs",
            RuntimeError("bad token sk-1234567890abcdef123456"),
            traceback_text=raw_tb,
        )
        self.assertEqual(payload["category"], "crash")
        combined = payload["title"] + "\n" + payload["description"]
        self.assertNotIn("Alice", combined)
        self.assertNotIn("bob", combined)
        self.assertNotIn("ghp_1234567890", combined)
        self.assertNotIn("sk-1234567890", combined)
        self.assertIn("<USER>", combined)
        self.assertIn("<REDACTED_", combined)

    def test_issue_markdown_includes_reporter_note_and_crash_category(self) -> None:
        md = issue_reporter.build_issue_markdown(
            "crash",
            "测试自动报错",
            "堆栈摘要",
            diag_info={"version": "v0.12.0", "os": "Windows 11"},
            reporter_note="QQ: 12345 / 昵称测试",
        )
        self.assertIn("程序异常自动上报", md)
        self.assertIn("联系方式 / 昵称", md)
        self.assertIn("昵称测试", md)

    def test_bootstrap_fatal_report_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
            with open(os.path.join(tmp, "config", "app_meta.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "app_name": "HoopCoach",
                        "version": "v0.12.0",
                        "github_repo": "EjuneG/HoopCoach",
                        "feedback_worker_url": "https://example.workers.dev",
                    },
                    f,
                )
            rep = bootstrap.build_fatal_error_report(
                tmp,
                "HoopCoach 启动失败",
                "File C:\\Users\\Tester\\HoopCoach\\src\\win_launcher.py\nImportError: boom ghp_abcdef1234567890",
            )
            self.assertEqual(rep["category"], "crash")
            self.assertNotIn("Tester", rep["sanitized_detail"])
            self.assertNotIn("ghp_abcdef", rep["sanitized_detail"])
            self.assertIn("<USER>", rep["description"])
            self.assertIn("<REDACTED_TOKEN>", rep["description"])

    def _make_stub_app(self) -> win_launcher.LQSNCoachDesktopApp:
        app = object.__new__(win_launcher.LQSNCoachDesktopApp)
        app._closing = False
        app._logged_runtime_errors = set()
        app._mute_runtime_error_prompts = False
        app._last_error_prompt_ts = 0.0
        app._error_prompt_cooldown_sec = 60.0
        app._active_runtime_error = None
        app._error_banner_frame = None
        app.var_error_banner = _DummyVar("")
        app.var_live_preview = _DummyVar("")
        app.root = _DummyRoot()
        return app

    def test_runtime_error_dedup_cooldown_foreground_and_mute(self) -> None:
        app = self._make_stub_app()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pc, "BASE_DIR", tmp):
            # 1. 游戏在前台：亮红条但不主动弹窗
            with mock.patch.object(app, "_is_game_in_foreground", return_value=True):
                app._record_runtime_error("场景渲染", ValueError("boom1"))
                self.assertEqual(len(app.root.after_calls), 0)
                self.assertIn("场景渲染", app.var_error_banner.get())

            # 2. 相同异常去重，不重复弹窗也不重复写入
            with mock.patch.object(app, "_is_game_in_foreground", return_value=False):
                app._record_runtime_error("场景渲染", ValueError("boom1"))
                self.assertEqual(len(app.root.after_calls), 0)

                # 3. 新异常且游戏不在前台：触发轻弹
                app._record_runtime_error("日志轮询", KeyError("k2"))
                self.assertEqual(len(app.root.after_calls), 1)

                # 4. 冷却时间内（<60s）再出新异常：仅更新横幅，不重复弹窗
                app._record_runtime_error("笔记解析", RuntimeError("r3"))
                self.assertEqual(len(app.root.after_calls), 1)

                # 5. 勾选「本次不再提示」后即使过冷却也不弹且不亮横幅
                app._last_error_prompt_ts = 0.0
                app._on_mute_runtime_error_prompts(True)
                app.var_error_banner.set("")
                app._record_runtime_error("技能解析", TypeError("t4"))
                self.assertEqual(len(app.root.after_calls), 1)
                self.assertEqual(app.var_error_banner.get(), "")


class TestLauncherOnboardingAndPersistence(unittest.TestCase):
    def test_hud_config_persists_strategy_and_follow_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig_cfg_dir = lc.CONFIG_DIR
            orig_cfg_file = lc.HUD_CONFIG_FILE
            lc.CONFIG_DIR = tmp
            lc.HUD_CONFIG_FILE = os.path.join(tmp, "hud_config.json")
            try:
                cfg = lc.get_default_hud_config()
                self.assertEqual(cfg["strategy_name"], "training_flow")
                self.assertTrue(cfg["follow_game_focus"])

                cfg["strategy_name"] = "store_flow"
                cfg["follow_game_focus"] = False
                lc.save_hud_config(cfg)

                reloaded = lc.load_hud_config()
                self.assertEqual(reloaded["strategy_name"], "store_flow")
                self.assertFalse(reloaded["follow_game_focus"])
            finally:
                lc.CONFIG_DIR = orig_cfg_dir
                lc.HUD_CONFIG_FILE = orig_cfg_file

    def test_wrong_dir_shows_guidance_not_silent(self) -> None:
        app = object.__new__(win_launcher.LQSNCoachDesktopApp)
        app.root = None
        app.var_scan_status = _DummyVar("")
        app.docs_dir = ""
        app.log_dir = ""

        warn_calls = []
        with mock.patch.object(
            pc,
            "choose_game_dir_with_status",
            return_value=(None, "/tmp/wrong_folder", "未在所选目录中找到游戏日志文件夹"),
        ), mock.patch.object(win_launcher, "messagebox") as mb:
            mb.showwarning.side_effect = lambda title, msg, **kw: warn_calls.append((title, msg))
            app._on_choose_game_dir()

        self.assertEqual(len(warn_calls), 1)
        self.assertIn("打开文件所在的位置", warn_calls[0][1])
        self.assertIn("无效", app.var_scan_status.get())

    def test_first_bind_auto_triggers_background_scan(self) -> None:
        app = object.__new__(win_launcher.LQSNCoachDesktopApp)
        app._closing = False
        app._scanning_runs = False
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "Logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "11_28___10_00_00___log.txt"), "w", encoding="utf-8") as f:
                f.write("[EDU_MODEL] dummy\n")
            app.log_dir = log_dir
            app.docs_dir = tmp
            scan_called = []
            with mock.patch.object(pc, "BASE_DIR", tmp), mock.patch.object(
                app, "_on_scan_runs", side_effect=lambda auto_triggered=False: scan_called.append(auto_triggered)
            ):
                app._maybe_auto_scan_on_first_bind(force=False)
            self.assertEqual(scan_called, [True])

    def test_brand_titles_unified_to_hoopcoach(self) -> None:
        self.assertIn("HoopCoach", bootstrap.MAIN_WINDOW_TITLE)
        self.assertNotIn("星奈", bootstrap.MAIN_WINDOW_TITLE)
        with open(os.path.join(SRC_DIR, "win_launcher.py"), "r", encoding="utf-8") as f:
            wl_src = f.read()
        self.assertNotIn("星奈", wl_src)
        self.assertIn("HoopCoach · 实时军师", wl_src)
        self.assertIn("上手与角标图例", wl_src)


class TestDashboardV0120Revamp(unittest.TestCase):
    def test_dashboard_cold_start_and_scan_endpoint(self) -> None:
        import urllib.request
        import dashboard_server
        import equipment_advisor as ea

        with tempfile.TemporaryDirectory() as tmp:
            orig_attrs = {
                "DETAILED_RUNS_FILE": dashboard_server.DETAILED_RUNS_FILE,
                "NOTE_POOL_FILE": dashboard_server.NOTE_POOL_FILE,
                "EQUIP_POOL_FILE": dashboard_server.EQUIP_POOL_FILE,
                "DAILY_QUEUE_FILE": dashboard_server.DAILY_QUEUE_FILE,
                "ACCOUNT_STATE_MD": dashboard_server.ACCOUNT_STATE_MD,
                "DOCS_DIR": dashboard_server.DOCS_DIR,
            }
            orig_ea_pool = ea.EQUIP_POOL_FILE
            try:
                docs_dir = os.path.join(tmp, "docs")
                os.makedirs(docs_dir, exist_ok=True)
                dashboard_server.DETAILED_RUNS_FILE = os.path.join(tmp, "detailed_runs.json")
                dashboard_server.NOTE_POOL_FILE = os.path.join(tmp, "note_pool.json")
                dashboard_server.EQUIP_POOL_FILE = os.path.join(tmp, "equip_pool.json")
                dashboard_server.DAILY_QUEUE_FILE = os.path.join(tmp, "daily_queue.json")
                dashboard_server.ACCOUNT_STATE_MD = os.path.join(tmp, "account_state.md")
                dashboard_server.DOCS_DIR = docs_dir
                ea.EQUIP_POOL_FILE = os.path.join(tmp, "equip_pool.json")
                ea.load_equip_data(force_reload=True)

                srv, url, engine, _mon = dashboard_server.start_background_server(
                    host="127.0.0.1", port=0, log_dir=os.path.join(tmp, "no_logs")
                )
                try:
                    with urllib.request.urlopen(f"{url}/api/overview", timeout=5) as r:
                        ov = json.loads(r.read().decode("utf-8"))
                    self.assertIn("sync_status", ov)
                    self.assertIn("char_records", ov)
                    self.assertEqual(ov["sync_status"]["runs_count"], 0)
                    self.assertEqual(ov["char_records"], [])

                    with urllib.request.urlopen(f"{url}/api/plans", timeout=5) as r:
                        pl = json.loads(r.read().decode("utf-8"))
                    # 即使是空 docs 目录，也应至少回退加载 quickstart_guide.md（或公开仓导出后的 docs/quickstart_guide.md）
                    plan_ids = [p["id"] for p in pl.get("plans", [])]
                    self.assertIn("quickstart_guide", plan_ids)

                    req = urllib.request.Request(f"{url}/api/scan", data=b"", method="POST")
                    with urllib.request.urlopen(req, timeout=5) as r:
                        scan_res = json.loads(r.read().decode("utf-8"))
                    self.assertIn("sync_status", scan_res)
                    self.assertIn("message", scan_res)
                finally:
                    srv.shutdown()
                    srv.server_close()
            finally:
                for k, v in orig_attrs.items():
                    setattr(dashboard_server, k, v)
                ea.EQUIP_POOL_FILE = orig_ea_pool
                ea.load_equip_data(force_reload=True)

    def test_dashboard_full_data_char_records_sorted(self) -> None:
        import dashboard_server

        engine = dashboard_server.DashboardEngine()
        ov = engine.get_overview()
        self.assertIn("sync_status", ov)
        self.assertIn("char_records", ov)
        recs = ov["char_records"]
        if len(recs) >= 2:
            self.assertGreaterEqual(recs[0]["best_rank"], recs[1]["best_rank"])
            self.assertIn("avg_rank", recs[0])
            self.assertIn("has_rainbow_note", recs[0])

    def test_public_docs_and_pages_polish(self) -> None:
        pages_path = os.path.join(ROOT_DIR, "packaging", "pages_index.html")
        if os.path.isfile(pages_path):
            with open(pages_path, "r", encoding="utf-8") as f:
                html_txt = f.read()
            self.assertIn("ghfast.top", html_txt)
            self.assertIn("data-dl-mirror", html_txt)
            self.assertNotIn("星奈", html_txt)

        readme_path = os.path.join(ROOT_DIR, "packaging", "README_PUBLIC.md")
        if os.path.isfile(readme_path):
            with open(readme_path, "r", encoding="utf-8") as f:
                rd_txt = f.read()
            self.assertIn("ghfast.top", rd_txt)
            self.assertIn("AGENTS.md", rd_txt)


if __name__ == "__main__":
    unittest.main()
