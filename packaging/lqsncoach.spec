# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller --onedir 绿色目录包构建配置（Windows 纯只读版 HoopCoach）

打包后目录结构（解压即用、无黑框、零 Python 安装门槛）：
  HoopCoach/
  ├── HoopCoach.exe      # 双击启动桌面小控制台 + 游戏内悬浮 HUD
  ├── _internal/         # Python 运行时与标准库
  ├── config/            # 用户热配置（hud_config.json / local_paths.json / app_meta.json）
  ├── dashboard/         # 本地作战室前端页面
  ├── data/              # 机制库、器材库、技能库、角色数据（运行时在此生成 runs/）
  └── strategies/        # 训练流 / 商店流 / 扭蛋流权重配置

说明：本 spec 显式排除一切自动化点击驱动模块，确保产物 100% 纯只读零侵入。
"""

import os
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT_DIR = os.path.dirname(SPEC_DIR)
SRC_DIR = os.path.join(ROOT_DIR, "src")

# 纯只读核心模块
hiddenimports = [
    "PIL",
    "PIL.Image",
    "PIL.ImageChops",
    "PIL.ImageDraw",
    "PIL.ImageFilter",
    "PIL.ImageFont",
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
    "win_diegetic_hud",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

# 显式排除所有自动化点击 / 私有挂机模块，确保 0 字节自动化代码进入 exe
excludes = [
    "auto_driver",
    "lobby_driver",
    "decision_bridge",
    "strategy_planner",
    "box_scanner",
    "gi",
    "cairo",
    "pytest",
    "unittest",
]

a = Analysis(
    [os.path.join(SRC_DIR, "win_launcher.py")],
    pathex=[SRC_DIR],
    binaries=[],
    datas=[],  # 静态数据目录由构建脚本直接平铺拷贝至 LQSNCoach.exe 同级目录，便于读写配置与归档
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
