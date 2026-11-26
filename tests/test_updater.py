#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_updater.py — HoopCoach 壳肉分离 + 轻量热更新全链路回归测试

覆盖：
1. SemVer 版本号解析与比较；
2. 多线路优先级排序与回退记忆；
3. 受保护路径与 Zip Slip 安全防护；
4. 端到端本地 HTTP 模拟 Release：
   - build_update_bundle 生成真实 patch zip + assets_manifest + manifest；
   - check_for_updates 精准走 patch 模式并只识别本地缺失的新增 PNG；
   - stage_update 下载、SHA256 校验、解压与补拉缺图；
   - bootstrap.apply_staged_patch_update 原子覆盖并 100% 保留用户个人数据；
5. SHA256 篡改拒绝；
6. 写入异常自动回滚；
7. runtime_version 升级自动切换 full 整包模式；
8. 公开源码依赖静态审计（防止壳肉分离后在 Windows 冻结环境缺标准库）。
"""

from __future__ import annotations

import ast
import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT_DIR, os.path.join(ROOT_DIR, "src"), os.path.join(ROOT_DIR, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bootstrap
import build_update_bundle
import updater


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return


def _start_local_http_server(directory: str):
    handler = functools.partial(_QuietHandler, directory=directory)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


class TestUpdaterUnit(unittest.TestCase):
    def test_version_comparison(self):
        self.assertTrue(updater.is_newer_version("v0.10.2", "v0.10.1"))
        self.assertTrue(updater.is_newer_version("v0.11.0", "v0.10.9"))
        self.assertTrue(updater.is_newer_version("v1.0.0", "v0.99.9"))
        self.assertTrue(updater.is_newer_version("v0.10.2", "v0.10.2-beta.1"))
        self.assertTrue(updater.is_newer_version("v0.10.2-beta.2", "v0.10.2-beta.1"))
        self.assertFalse(updater.is_newer_version("v0.10.1", "v0.10.1"))
        self.assertFalse(updater.is_newer_version("v0.10.0", "v0.10.1"))
        self.assertFalse(updater.is_newer_version("", "v0.10.1"))

    def test_candidate_urls_priority(self):
        updater._LAST_GOOD_PREFIX = None
        cands = updater.build_candidate_urls(
            "https://github.com/EjuneG/HoopCoach/releases/latest/download/manifest.json",
            custom_mirrors=["https://cf.example.com/"],
        )
        self.assertEqual(cands[0][0], "https://cf.example.com/")
        self.assertTrue(cands[0][1].startswith("https://cf.example.com/https://github.com/"))
        self.assertEqual(cands[1][0], "")
        self.assertEqual(cands[1][1], "https://github.com/EjuneG/HoopCoach/releases/latest/download/manifest.json")
        self.assertGreaterEqual(len(cands), 4)

    def test_protected_paths_and_zip_slip(self):
        self.assertTrue(updater.is_protected_relpath("config/hud_config.json"))
        self.assertTrue(updater.is_protected_relpath("config/local_paths.json"))
        self.assertTrue(updater.is_protected_relpath("data/runs/detailed_runs.json"))
        self.assertTrue(bootstrap.is_protected_relpath("data/runs/archive/run1.json"))
        self.assertFalse(updater.is_protected_relpath("config/app_meta.json"))
        self.assertFalse(updater.is_protected_relpath("src/win_launcher.py"))

        with tempfile.TemporaryDirectory() as td:
            bad_zip = os.path.join(td, "bad.zip")
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.txt", "boom")
            with self.assertRaises(ValueError):
                updater._safe_extract_zip(bad_zip, os.path.join(td, "out"))


class TestUpdaterEndToEnd(unittest.TestCase):
    def setUp(self):
        updater._LAST_GOOD_PREFIX = None

    def _make_fake_repo(self, root: str, version: str, extra_png: bool = False):
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        os.makedirs(os.path.join(root, "config"), exist_ok=True)
        os.makedirs(os.path.join(root, "dashboard"), exist_ok=True)
        os.makedirs(os.path.join(root, "strategies"), exist_ok=True)
        os.makedirs(os.path.join(root, "data", "hoopgirls", "heads"), exist_ok=True)

        with open(os.path.join(root, "src", "win_launcher.py"), "w", encoding="utf-8") as f:
            f.write(f'VERSION_TAG = "{version}"\n')
        with open(os.path.join(root, "dashboard", "index.html"), "w", encoding="utf-8") as f:
            f.write(f"<h1>HoopCoach {version}</h1>")
        with open(os.path.join(root, "strategies", "training_flow.json"), "w", encoding="utf-8") as f:
            f.write('{"name": "training_flow"}')
        with open(os.path.join(root, "data", "verified_mechanics.json"), "w", encoding="utf-8") as f:
            f.write('{"version": "' + version + '"}')
        with open(os.path.join(root, "data", "hoopgirls", "heads", "char_1001.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nOLD_CHAR_IMAGE_BYTES")
        if extra_png:
            with open(os.path.join(root, "data", "hoopgirls", "heads", "char_9999.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nNEW_CHAR_IMAGE_BYTES_9999")

    def test_full_patch_and_incremental_asset_flow(self):
        with tempfile.TemporaryDirectory() as td:
            repo_v2 = os.path.join(td, "repo_v2")
            srv_root = os.path.join(td, "http_root")
            client_dir = os.path.join(td, "client")

            # 1. 准备新版仓库（含 1 张旧图 + 1 张新增角色图）
            self._make_fake_repo(repo_v2, "v0.10.2", extra_png=True)

            # 2. 准备本地客户端旧版目录（含用户自定义配置、游戏路径记忆、历史战绩和旧图）
            self._make_fake_repo(client_dir, "v0.10.1", extra_png=False)
            with open(os.path.join(client_dir, "config", "app_meta.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "app_name": "HoopCoach",
                        "version": "v0.10.1",
                        "runtime_version": "rt1",
                        "github_repo": "EjuneG/HoopCoach",
                        "custom_mirrors": [],
                    },
                    f,
                )
            with open(os.path.join(client_dir, "config", "hud_config.json"), "w", encoding="utf-8") as f:
                json.dump({"diegetic_mode": False, "focus": {"stat": 103, "bonus": 1.3}}, f)
            with open(os.path.join(client_dir, "config", "local_paths.json"), "w", encoding="utf-8") as f:
                json.dump({"log_dir": "D:/Games/LQSN/_game_data_/Documents/Logs"}, f)
            os.makedirs(os.path.join(client_dir, "data", "runs"), exist_ok=True)
            with open(os.path.join(client_dir, "data", "runs", "detailed_runs.json"), "w", encoding="utf-8") as f:
                f.write('[{"run_id": "keep_me_forever"}]')

            # 3. 构造本地 HTTP Release 托管目录
            rel_latest_dir = os.path.join(srv_root, "EjuneG", "HoopCoach", "releases", "latest", "download")
            rel_v2_dir = os.path.join(srv_root, "EjuneG", "HoopCoach", "releases", "download", "v0.10.2")
            raw_v2_dir = os.path.join(srv_root, "raw", "EjuneG", "HoopCoach", "v0.10.2")
            os.makedirs(rel_latest_dir, exist_ok=True)
            os.makedirs(rel_v2_dir, exist_ok=True)
            shutil.copytree(os.path.join(repo_v2, "data"), os.path.join(raw_v2_dir, "data"))

            built = build_update_bundle.build_all(
                repo_root=repo_v2,
                dist_dir=rel_v2_dir,
                version="v0.10.2",
                repo="EjuneG/HoopCoach",
                runtime_version="rt1",
                notes="测试热更新：新增角色 9999 头像与新版代码",
            )
            self.assertTrue(os.path.isfile(built["release_body_path"]))
            with open(built["release_body_path"], "r", encoding="utf-8") as f:
                rel_body = f.read()
            self.assertIn(
                "https://github.com/EjuneG/HoopCoach/releases/download/v0.10.2/HoopCoach-Windows-v0.10.2.zip",
                rel_body,
            )
            self.assertIn("`manifest.json`", rel_body)
            self.assertIn("测试热更新：新增角色 9999 头像与新版代码", rel_body)

            srv, base_url = _start_local_http_server(srv_root)
            try:
                # 改写 manifest/assets_manifest 中的 URL 指向本地 HTTP 测试服务
                with open(built["assets_manifest_path"], "r", encoding="utf-8") as f:
                    am = json.load(f)
                am["raw_base_url"] = f"{base_url}/raw/EjuneG/HoopCoach/v0.10.2"
                with open(built["assets_manifest_path"], "w", encoding="utf-8") as f:
                    json.dump(am, f)
                am_sha = build_update_bundle.sha256_file(built["assets_manifest_path"])

                with open(built["manifest_path"], "r", encoding="utf-8") as f:
                    mf = json.load(f)
                mf["patch"]["url"] = f"{base_url}/EjuneG/HoopCoach/releases/download/v0.10.2/{mf['patch']['filename']}"
                mf["assets_manifest"]["url"] = f"{base_url}/EjuneG/HoopCoach/releases/download/v0.10.2/assets_manifest.json"
                mf["assets_manifest"]["sha256"] = am_sha
                with open(built["manifest_path"], "w", encoding="utf-8") as f:
                    json.dump(mf, f)
                shutil.copy2(built["manifest_path"], os.path.join(rel_latest_dir, "manifest.json"))

                # 将 manifest.json 请求路由到我们的本地测试服务器
                orig_build_cands = updater.build_candidate_urls

                def _local_cands(raw_url, custom_mirrors=None, include_public_mirrors=True):
                    if raw_url.startswith("https://github.com/"):
                        mapped = raw_url.replace("https://github.com/", base_url + "/")
                        return [("", mapped, "本地测试服")]
                    return orig_build_cands(raw_url, custom_mirrors=custom_mirrors, include_public_mirrors=False)

                with mock.patch.object(updater, "build_candidate_urls", side_effect=_local_cands):
                    # Step A: 检查更新
                    res = updater.check_for_updates(base_dir=client_dir)
                    self.assertTrue(res.ok, res.error)
                    self.assertTrue(res.has_update)
                    self.assertEqual(res.mode, "patch")
                    self.assertEqual(res.latest_version, "v0.10.2")
                    # 必须仅拉取缺失的那 1 张新图 char_9999.png，不重复下 char_1001.png
                    self.assertEqual([a.rel_path for a in res.missing_assets], ["data/hoopgirls/heads/char_9999.png"])
                    self.assertEqual(res.total_download_bytes, res.patch_bytes + res.missing_assets_bytes)

                    # Step B: 下载并暂存
                    progress_events = []
                    updater.stage_update(
                        res,
                        base_dir=client_dir,
                        progress_cb=lambda d, t, msg: progress_events.append((d, t, msg)),
                    )
                    self.assertTrue(progress_events)
                    self.assertTrue(os.path.isfile(os.path.join(client_dir, "_update_staging", "ready.json")))

                # Step C: bootstrap 原子应用热更
                ok, msg = bootstrap.apply_staged_patch_update(client_dir)
                self.assertTrue(ok, msg)

                # 验证新版代码、版本元数据、新图片与 assets_state 已落地
                with open(os.path.join(client_dir, "src", "win_launcher.py"), "r", encoding="utf-8") as f:
                    self.assertIn("v0.10.2", f.read())
                new_meta = updater.load_app_meta(client_dir)
                self.assertEqual(new_meta["version"], "v0.10.2")
                self.assertTrue(os.path.isfile(os.path.join(client_dir, "data", "hoopgirls", "heads", "char_9999.png")))
                self.assertTrue(os.path.isfile(os.path.join(client_dir, "config", "assets_state.json")))

                # 验证用户配置与历史战绩 100% 完好保留
                with open(os.path.join(client_dir, "config", "hud_config.json"), "r", encoding="utf-8") as f:
                    hud_cfg = json.load(f)
                self.assertEqual(hud_cfg["diegetic_mode"], False)
                self.assertEqual(hud_cfg["focus"]["stat"], 103)
                with open(os.path.join(client_dir, "config", "local_paths.json"), "r", encoding="utf-8") as f:
                    self.assertIn("D:/Games/LQSN", f.read())
                with open(os.path.join(client_dir, "data", "runs", "detailed_runs.json"), "r", encoding="utf-8") as f:
                    self.assertIn("keep_me_forever", f.read())

                # 再次检查更新应返回已是最新
                with mock.patch.object(updater, "build_candidate_urls", side_effect=_local_cands):
                    res2 = updater.check_for_updates(base_dir=client_dir)
                    self.assertTrue(res2.ok)
                    self.assertFalse(res2.has_update)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_sha256_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "file.zip"), "wb") as f:
                f.write(b"corrupted content")
            srv, base_url = _start_local_http_server(td)
            try:
                dst = os.path.join(td, "dl.zip")
                with self.assertRaises(RuntimeError):
                    updater._http_download_file(
                        f"{base_url}/file.zip",
                        dst,
                        expected_sha256="0" * 64,
                    )
                self.assertFalse(os.path.exists(dst))
            finally:
                srv.shutdown()
                srv.server_close()

    def test_rollback_on_apply_failure(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "src"), exist_ok=True)
            with open(os.path.join(td, "src", "a.py"), "w", encoding="utf-8") as f:
                f.write("ORIGINAL_A")
            with open(os.path.join(td, "src", "b.py"), "w", encoding="utf-8") as f:
                f.write("ORIGINAL_B")

            payload = os.path.join(td, "_update_staging", "payload", "src")
            os.makedirs(payload, exist_ok=True)
            with open(os.path.join(payload, "a.py"), "w", encoding="utf-8") as f:
                f.write("NEW_A")
            with open(os.path.join(payload, "b.py"), "w", encoding="utf-8") as f:
                f.write("NEW_B")
            with open(os.path.join(td, "_update_staging", "ready.json"), "w", encoding="utf-8") as f:
                json.dump({"mode": "patch", "target_version": "v9.9.9"}, f)

            real_copy2 = shutil.copy2
            calls = {"n": 0}

            def _flaky_copy2(src, dst, *a, **kw):
                # 备份两次 + 覆盖 a.py 成功后，在覆盖 b.py 时模拟磁盘/权限异常
                if dst.endswith(os.path.join("src", "b.py")) and "_update_backup" not in dst:
                    raise PermissionError("simulated lock on b.py")
                calls["n"] += 1
                return real_copy2(src, dst, *a, **kw)

            with mock.patch.object(bootstrap.shutil, "copy2", side_effect=_flaky_copy2):
                ok, msg = bootstrap.apply_staged_patch_update(td)
            self.assertFalse(ok)
            self.assertIn("回滚", msg)
            with open(os.path.join(td, "src", "a.py"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "ORIGINAL_A")
            with open(os.path.join(td, "src", "b.py"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "ORIGINAL_B")

    def test_runtime_version_bump_switches_to_full_mode(self):
        with tempfile.TemporaryDirectory() as td:
            mf = {
                "schema": 1,
                "version": "v0.11.0",
                "runtime_version": "rt2",
                "notes": "升级底层 Python 运行库",
                "patch": {"filename": "p.zip", "size": 500000, "sha256": "a" * 64, "url": "https://example.com/p.zip"},
                "full": {"filename": "f.zip", "size": 32000000, "sha256": "b" * 64, "url": "https://example.com/f.zip"},
            }
            raw_bytes = json.dumps(mf).encode("utf-8")
            with mock.patch.object(updater, "_http_fetch_bytes", return_value=(raw_bytes, "mock")):
                res = updater.check_for_updates(
                    meta={
                        "version": "v0.10.2",
                        "runtime_version": "rt1",
                        "github_repo": "EjuneG/HoopCoach",
                        "custom_mirrors": [],
                    },
                    base_dir=td,
                )
            self.assertTrue(res.ok)
            self.assertTrue(res.has_update)
            self.assertEqual(res.mode, "full")
            self.assertEqual(res.total_download_bytes, 32000000)

    def test_spec_covers_public_imports(self):
        """确保壳肉分离后，公开业务模块用到的第三方/标准库及 PIL/tkinter 子模块均未遗漏在 bootstrap.py 与 spec 中。"""
        with open(os.path.join(ROOT_DIR, "src", "bootstrap.py"), "r", encoding="utf-8") as f:
            boot_src = f.read()
        with open(os.path.join(ROOT_DIR, "packaging", "lqsncoach.spec"), "r", encoding="utf-8") as f:
            spec_src = f.read()

        def _collect_imports(tree: ast.AST) -> tuple[set[str], set[str]]:
            top_mods: set[str] = set()
            sub_mods: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        top_mods.add(a.name.split(".")[0])
                        if "." in a.name and a.name.split(".")[0] in ("PIL", "tkinter"):
                            sub_mods.add(a.name)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    m0 = node.module.split(".")[0]
                    top_mods.add(m0)
                    if m0 in ("PIL", "tkinter"):
                        if "." in node.module:
                            sub_mods.add(node.module)
                        else:
                            for a in node.names:
                                if a.name != "*":
                                    sub_mods.add(f"{m0}.{a.name}")
            return top_mods, sub_mods

        boot_imports, boot_subs = _collect_imports(ast.parse(boot_src))

        # 始终内建于 Python 解释器或 Linux 专属可选模块
        ignore = {
            "__future__", "argparse", "atexit", "cairo", "collections", "dataclasses",
            "datetime", "decision_bridge", "faulthandler", "gi", "io", "json", "os",
            "shutil", "src", "sys", "time", "traceback", "typing",
        }
        local_mods = {os.path.splitext(os.path.basename(p))[0] for p in build_update_bundle.PATCH_SRC_FILES}

        missing = set()
        missing_subs_boot = set()
        missing_subs_spec = set()
        for rel in build_update_bundle.PATCH_SRC_FILES:
            full = os.path.join(ROOT_DIR, rel)
            with open(full, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), rel)
            top_mods, sub_mods = _collect_imports(tree)
            for m0 in top_mods:
                if m0 not in local_mods and m0 not in ignore and m0 not in boot_imports:
                    missing.add(m0)
            for sm in sub_mods:
                if sm not in boot_subs:
                    missing_subs_boot.add(sm)
                if f'"{sm}"' not in spec_src and f"'{sm}'" not in spec_src:
                    missing_subs_spec.add(sm)

        self.assertEqual(missing, set(), f"以下模块未在 src/bootstrap.py 显式引入，可能导致冻结壳缺包: {missing}")
        self.assertEqual(missing_subs_boot, set(), f"以下 PIL/tkinter 子模块未在 src/bootstrap.py 引入: {missing_subs_boot}")
        self.assertEqual(missing_subs_spec, set(), f"以下 PIL/tkinter 子模块未在 lqsncoach.spec hiddenimports 声明: {missing_subs_spec}")


if __name__ == "__main__":
    unittest.main()
