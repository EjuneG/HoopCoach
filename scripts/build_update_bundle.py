#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_update_bundle.py — 生成 HoopCoach 轻量热更包、图片增量清单与 manifest.json

由 GitHub Actions（packaging/build-windows.yml）在构建 Windows 发布包时调用，
也可在本地直接运行验证热更包结构与体积。

输出产物（位于 --dist-dir，默认 dist/）：
1. HoopCoach-Patch-<ver>.zip      （仅含平铺 src/*.py、dashboard、strategies、data/*.json、app_meta.json，约 0.6 MB）
2. assets_manifest.json           （data/hoopgirls/ 下全部图片 PNG 的 rel_path / size / sha256 清单）
3. manifest.json                  （约 1.5 KB 轻清单，供客户端启动时静默检查更新）
4. （可选）同步将 `assets_state.json` 写入 `--portable-dir/config/assets_state.json`
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import zipfile


# 允许进入轻量热更包的纯只读业务源码白名单
PATCH_SRC_FILES = (
    "src/analyzer.py",
    "src/bootstrap.py",
    "src/dashboard_server.py",
    "src/equipment_advisor.py",
    "src/hud_badges.py",
    "src/issue_reporter.py",
    "src/live_coach.py",
    "src/log_mirror.py",
    "src/note_advisor.py",
    "src/platform_compat.py",
    "src/skill_advisor.py",
    "src/updater.py",
    "src/win_diegetic_hud.py",
    "src/win_launcher.py",
)

PATCH_STATIC_DATA_FILES = (
    "data/equipment_guide_db.json",
    "data/event_choices.json",
    "data/skill_catalog.json",
    "data/verified_mechanics.json",
)

PATCH_HOOPGIRLS_JSON = (
    "data/hoopgirls/characters.json",
    "data/hoopgirls/equipments.json",
    "data/hoopgirls/guides.json",
    "data/hoopgirls/inheritance.json",
    "data/hoopgirls/inheritance_rules.json",
    "data/hoopgirls/prototypes.json",
    "data/hoopgirls/states.json",
)

ASSET_SUBDIRS = (
    "data/hoopgirls/heads",
    "data/hoopgirls/equipment_icons",
)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def build_assets_manifest(repo_root: str, repo: str, version: str) -> tuple[dict, str]:
    """扫描 data/hoopgirls 下的 PNG/WebP 图片，计算单文件哈希与整体 assets_hash。"""
    files_map: dict[str, dict] = {}
    for sub in ASSET_SUBDIRS:
        abs_sub = os.path.join(repo_root, sub)
        if not os.path.isdir(abs_sub):
            continue
        for cur_root, _dirs, files in os.walk(abs_sub):
            for fn in sorted(files):
                if not fn.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                    continue
                abs_p = os.path.join(cur_root, fn)
                rel = os.path.relpath(abs_p, repo_root).replace("\\", "/")
                files_map[rel] = {
                    "size": os.path.getsize(abs_p),
                    "sha256": sha256_file(abs_p),
                }

    summary_h = hashlib.sha256()
    for rel in sorted(files_map.keys()):
        item = files_map[rel]
        summary_h.update(f"{rel}:{item['size']}:{item['sha256']}\n".encode("utf-8"))
    assets_hash = summary_h.hexdigest()[:16]

    manifest = {
        "schema": 1,
        "version": version,
        "assets_hash": assets_hash,
        "raw_base_url": f"https://raw.githubusercontent.com/{repo}/{version}",
        "count": len(files_map),
        "files": files_map,
    }
    return manifest, assets_hash


def build_patch_zip(
    repo_root: str,
    patch_zip_path: str,
    app_meta: dict,
) -> tuple[int, str, int]:
    """打包纯代码 + 前端 + 策略 + 静态 JSON（不含 exe、_internal、图片与用户配置）。"""
    os.makedirs(os.path.dirname(os.path.abspath(patch_zip_path)), exist_ok=True)
    file_count = 0

    with zipfile.ZipFile(patch_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        def _add_file(src_rel: str, arc_rel: str | None = None) -> None:
            nonlocal file_count
            abs_p = os.path.join(repo_root, src_rel)
            if os.path.isfile(abs_p):
                zf.write(abs_p, arc_rel or src_rel)
                file_count += 1

        def _add_dir(src_rel: str, skip_dirs: tuple[str, ...] = ("__pycache__", "data")) -> None:
            nonlocal file_count
            abs_d = os.path.join(repo_root, src_rel)
            if not os.path.isdir(abs_d):
                return
            for cur_root, dirs, files in os.walk(abs_d):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
                for fn in sorted(files):
                    abs_p = os.path.join(cur_root, fn)
                    rel = os.path.relpath(abs_p, repo_root).replace("\\", "/")
                    zf.write(abs_p, rel)
                    file_count += 1

        for rel in PATCH_SRC_FILES:
            _add_file(rel)
        for rel in PATCH_STATIC_DATA_FILES:
            _add_file(rel)
        for rel in PATCH_HOOPGIRLS_JSON:
            _add_file(rel)

        _add_dir("strategies", skip_dirs=("__pycache__",))
        _add_dir("dashboard", skip_dirs=("__pycache__", "data"))
        _add_file("packaging/assets/hoopcoach.ico")

        if os.path.isfile(os.path.join(repo_root, "README.md")):
            _add_file("README.md", "使用说明.txt")

        # 写入携带精确版本号的 config/app_meta.json（绝不包含 hud_config.json / local_paths.json）
        meta_bytes = (json.dumps(app_meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        zf.writestr("config/app_meta.json", meta_bytes)
        file_count += 1

    size = os.path.getsize(patch_zip_path)
    digest = sha256_file(patch_zip_path)
    return size, digest, file_count


def build_all(
    repo_root: str,
    dist_dir: str,
    version: str,
    repo: str,
    runtime_version: str = "rt1",
    full_zip_path: str = "",
    notes: str = "",
    portable_dir: str = "",
) -> dict:
    os.makedirs(dist_dir, exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 合并现有 config/app_meta.json 中的 custom_mirrors 等配置
    app_meta = {
        "app_name": "HoopCoach",
        "version": version,
        "runtime_version": runtime_version,
        "github_repo": repo,
        "custom_mirrors": [],
    }
    src_meta = os.path.join(repo_root, "config", "app_meta.json")
    if os.path.isfile(src_meta):
        try:
            with open(src_meta, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                if isinstance(disk.get("custom_mirrors"), list):
                    app_meta["custom_mirrors"] = disk["custom_mirrors"]
                if not runtime_version and isinstance(disk.get("runtime_version"), str):
                    app_meta["runtime_version"] = disk["runtime_version"].strip() or "rt1"
                if not repo and isinstance(disk.get("github_repo"), str):
                    app_meta["github_repo"] = disk["github_repo"].strip()
        except Exception:
            pass
    app_meta["version"] = version
    if repo:
        app_meta["github_repo"] = repo
    if runtime_version:
        app_meta["runtime_version"] = runtime_version

    # 1. 生成图片增量清单 assets_manifest.json
    assets_manifest, assets_hash = build_assets_manifest(repo_root, app_meta["github_repo"], version)
    assets_manifest_path = os.path.join(dist_dir, "assets_manifest.json")
    with open(assets_manifest_path, "w", encoding="utf-8") as f:
        json.dump(assets_manifest, f, ensure_ascii=False, separators=(",", ":"))
    am_size = os.path.getsize(assets_manifest_path)
    am_sha = sha256_file(assets_manifest_path)

    # 若指定了 portable_dir（构建整包阶段），同步写入精确版本号的 app_meta.json 与初始 assets_state.json
    if portable_dir and os.path.isdir(portable_dir):
        cfg_dir = os.path.join(portable_dir, "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "app_meta.json"), "w", encoding="utf-8") as f:
            json.dump(app_meta, f, indent=2, ensure_ascii=False)
            f.write("\n")
        with open(os.path.join(cfg_dir, "assets_state.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"assets_hash": assets_hash, "updated_at": now_iso},
                f,
                indent=2,
                ensure_ascii=False,
            )
            f.write("\n")

    # 2. 生成轻量热更包 HoopCoach-Patch-<ver>.zip
    patch_name = f"HoopCoach-Patch-{version}.zip"
    patch_path = os.path.join(dist_dir, patch_name)
    patch_size, patch_sha, patch_files = build_patch_zip(repo_root, patch_path, app_meta)

    dl_base = f"https://github.com/{app_meta['github_repo']}/releases/download/{version}"

    # 3. 完整整包元数据（若已生成）
    full_info: dict = {}
    if full_zip_path and os.path.isfile(full_zip_path):
        full_name = os.path.basename(full_zip_path)
        full_info = {
            "filename": full_name,
            "size": os.path.getsize(full_zip_path),
            "sha256": sha256_file(full_zip_path),
            "url": f"{dl_base}/{full_name}",
        }

    # 4. 生成轻清单 manifest.json（约 1.5 KB）
    manifest = {
        "schema": 1,
        "app_name": app_meta["app_name"],
        "version": version,
        "runtime_version": app_meta["runtime_version"],
        "published_at": now_iso,
        "notes": notes.strip() or f"HoopCoach {version} 更新发布",
        "patch": {
            "filename": patch_name,
            "size": patch_size,
            "sha256": patch_sha,
            "url": f"{dl_base}/{patch_name}",
        },
        "full": full_info,
        "assets_hash": assets_hash,
        "assets_manifest": {
            "filename": "assets_manifest.json",
            "size": am_size,
            "sha256": am_sha,
            "url": f"{dl_base}/assets_manifest.json",
        },
    }
    manifest_path = os.path.join(dist_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return {
        "manifest_path": manifest_path,
        "assets_manifest_path": assets_manifest_path,
        "patch_path": patch_path,
        "patch_size": patch_size,
        "patch_files": patch_files,
        "assets_count": assets_manifest["count"],
        "assets_hash": assets_hash,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 HoopCoach 轻量热更包与 manifest.json")
    parser.add_argument("--repo-root", default=".", help="公开仓库根目录")
    parser.add_argument("--dist-dir", default="dist", help="产物输出目录")
    parser.add_argument("--version", required=True, help="发布版本号，例如 v0.10.2")
    parser.add_argument("--repo", default="EjuneG/HoopCoach", help="GitHub 仓库 owner/repo")
    parser.add_argument("--runtime-version", default="rt1", help="底层运行时壳版本，例如 rt1")
    parser.add_argument("--full-zip", default="", help="已打好的 Windows 整包 zip 路径（可选）")
    parser.add_argument("--portable-dir", default="", help="整包解压组装目录（写入 app_meta 与 assets_state）")
    parser.add_argument("--notes", default="", help="发布更新说明")
    parser.add_argument("--notes-file", default="", help="从文件读取发布更新说明（优先于 --notes）")
    args = parser.parse_args()

    notes = args.notes
    if args.notes_file and os.path.isfile(args.notes_file):
        with open(args.notes_file, "r", encoding="utf-8") as f:
            notes = f.read().strip()

    res = build_all(
        repo_root=os.path.abspath(args.repo_root),
        dist_dir=os.path.abspath(args.dist_dir),
        version=args.version.strip(),
        repo=args.repo.strip().strip("/"),
        runtime_version=args.runtime_version.strip(),
        full_zip_path=os.path.abspath(args.full_zip) if args.full_zip else "",
        notes=notes,
        portable_dir=os.path.abspath(args.portable_dir) if args.portable_dir else "",
    )
    print(
        f"✅ 热更产物已生成: patch={os.path.basename(res['patch_path'])} "
        f"({res['patch_size'] / 1024:.1f} KB, {res['patch_files']} 文件) | "
        f"assets={res['assets_count']} 张 (hash={res['assets_hash']}) | "
        f"manifest={os.path.basename(res['manifest_path'])}"
    )


if __name__ == "__main__":
    main()
