# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller --onedir 壳肉分离绿色目录包构建配置（Windows 纯只读版 HoopCoach）

打包后目录结构（解压即用、无黑框、零 Python 安装门槛、日常 ~0.6 MB 轻量热更新）：
  HoopCoach/
  ├── HoopCoach.exe      # 稳定运行时引导外壳（src/bootstrap.py）
  ├── _internal/         # Python 运行时、标准库、Tkinter、Pillow（壳不变则永不重下）
  ├── src/               # 平铺纯只读业务模块（日常热更新仅替换此处与静态数据）
  ├── config/            # 用户热配置（hud_config.json / local_paths.json 受保护；app_meta.json 随更）
  ├── dashboard/         # 本地作战室前端页面
  ├── data/              # 机制库、器材库、技能库、角色数据（运行时在此生成 runs/）
  └── strategies/        # 训练流 / 商店流 / 扭蛋流权重配置

说明：
1. 本 spec 仅将 `src/bootstrap.py` 与运行库打入 exe/_internal/，业务 `.py` 模块平铺在 `src/` 目录；
2. 显式排除一切自动化点击驱动模块，确保产物 100% 纯只读零侵入。
"""

import os
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT_DIR = os.path.dirname(SPEC_DIR)
SRC_DIR = os.path.join(ROOT_DIR, "src")
ICON_PATH = os.path.join(SPEC_DIR, "assets", "hoopcoach.ico")

# 运行时壳必须预装的标准库与第三方依赖（确保平铺的 src/*.py 零缺包）
hiddenimports = [
    "PIL",
    "PIL.Image",
    "PIL.ImageChops",
    "PIL.ImageDraw",
    "PIL.ImageFilter",
    "PIL.ImageFont",
    "PIL.ImageGrab",
    "PIL.ImageTk",
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "base64",
    "concurrent.futures",
    "ctypes",
    "ctypes.wintypes",
    "difflib",
    "getpass",
    "glob",
    "hashlib",
    "html",
    "http.server",
    "io",
    "mimetypes",
    "platform",
    "socket",
    "ssl",
    "tempfile",
    "urllib.error",
    "urllib.parse",
    "urllib.request",
    "webbrowser",
    "zipfile",
]

# 排除业务模块（使其平铺在外部 src/ 以支持 ~0.6 MB 轻量热更新）+ 排除一切私有自动化模块
excludes = [
    # 业务模块（平铺在目录外部 src/）
    "analyzer",
    "dashboard_server",
    "equipment_advisor",
    "hud_badges",
    "issue_reporter",
    "live_coach",
    "log_mirror",
    "note_advisor",
    "platform_compat",
    "skill_advisor",
    "updater",
    "win_diegetic_hud",
    "win_launcher",
    # 私有/自动化模块（严禁进入公开包）
    "auto_driver",
    "char_wishes",
    "lobby_driver",
    "decision_bridge",
    "strategy_planner",
    "box_scanner",
    # 非 Windows / 测试依赖
    "gi",
    "cairo",
    "pytest",
    "unittest",
]

a = Analysis(
    [os.path.join(SRC_DIR, "bootstrap.py")],
    pathex=[],  # 故意不加入 SRC_DIR，彻底防止业务模块被意外封死进 PYZ
    binaries=[],
    datas=[],  # 业务源码与静态数据由构建脚本平铺至 HoopCoach.exe 同级目录
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HoopCoach",
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 关闭 UPX 压缩，大幅降低 Windows Defender / 杀软误报率
    console=False,  # 无黑框控制台窗口
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HoopCoach",
)
