#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
log_mirror.py — LQSNCoach 纯只读实时日志状态镜像器（零侵入、零输入模拟）

与 live_coach 状态机 100% 同源：
- 仅通过只读打开游戏明文日志（_game_data_/Documents/Logs/*.txt）恢复当前育成状态；
- 支持大日志冷启动反向锚点快进（education.cs_start_education）与增量 poll；
- 供实时桌面 HUD、作战室 Dashboard 及其他扩展模块共享复用。
"""

from __future__ import annotations

import glob
import os
import re
import time
from collections import deque
from typing import Optional

import live_coach as lc


class LiveLogMirror:
    """加载最新游戏日志，快速回放到当前进度，随后支持增量 poll 与状态事件监听。"""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.log_path: Optional[str] = None
        self.fp = None
        self.state = lc.make_initial_state()
        self.last_train_cmds: list = []
        self._skill_buf = [""]
        self._edu_buf = ""
        self.recent_lines: deque[str] = deque(maxlen=45)
        self.recent_events: deque[dict] = deque(maxlen=60)

    def close(self) -> None:
        if self.fp is not None:
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None

    def _find_latest_log(self) -> Optional[str]:
        if not self.log_dir or not os.path.isdir(self.log_dir):
            return None
        files = glob.glob(os.path.join(self.log_dir, "*.txt"))
        if not files:
            return None
        return max(files, key=os.path.getmtime)

    _FAST_SKIP_PREFIXES = (
        "\t",
        "UnityEngine.",
        "XLua.",
        "P3Client.",
        "Animancer.",
        "DG.Tweening.",
        "System.",
    )

    @staticmethod
    def _find_fast_replay_offset(path: str, min_size_bytes: int = 1_500_000) -> int:
        """大日志冷启动加速：反向定位最后一次育成开局锚点 (education.cs_start_education) 所在行首，避免吃进上一局残留状态。"""
        try:
            fsize = os.path.getsize(path)
            if fsize < min_size_bytes:
                return 0
            anchor = b"request_call:    education.cs_start_education"
            chunk_size = 512 * 1024
            pos = fsize
            tail_overlap = b""
            with open(path, "rb") as bf:
                while pos > 0:
                    read_sz = min(chunk_size, pos)
                    pos -= read_sz
                    bf.seek(pos)
                    buf = bf.read(read_sz) + tail_overlap
                    idx = buf.rfind(anchor)
                    if idx != -1:
                        abs_anchor = pos + idx
                        line_start = buf.rfind(b"\n", 0, idx)
                        if line_start != -1:
                            return pos + line_start + 1
                        return max(0, abs_anchor - 256)
                    tail_overlap = buf[: len(anchor) + 64]
        except Exception:
            return 0
        return 0

    def _reset_for_new_run(self) -> None:
        """开新局 (education.cs_start_education) 时彻底清空上一局残留状态（器材映射/哨子事件标记/训次/商店等）。"""
        self.state = lc.make_initial_state()
        self.last_train_cmds = []
        self._skill_buf = [""]
        self._edu_buf = ""

    def attach_latest(self) -> bool:
        latest = self._find_latest_log()
        if not latest:
            return False
        if self.log_path == latest and self.fp is not None:
            return True
        self.close()
        self.log_path = latest
        self._reset_for_new_run()
        self.recent_lines.clear()
        self.recent_events.clear()
        start_off = self._find_fast_replay_offset(latest)
        self.fp = open(latest, "r", encoding="utf-8", errors="ignore")
        if start_off > 0:
            self.fp.seek(start_off)
            if start_off > 1:
                # 若落在行中则对齐到下一完整行首；若恰好紧跟 '\n' 则不丢弃锚点行本身
                self.fp.seek(start_off - 1)
                if self.fp.read(1) != "\n":
                    self.fp.readline()
        # 一次性快进读取历史行，精准恢复当前育成状态（快进时跳过纯 Unity/XLua 堆栈噪声行）
        skip_pfx = self._FAST_SKIP_PREFIXES
        for line in self.fp:
            if not self._edu_buf and not self._skill_buf[0]:
                if not line or line.startswith(skip_pfx) or "cs_heartbeat" in line:
                    continue
            self._feed_line(line, record_events=False)
        # 冷启动中途续玩兜底：若日志前半段未记录开局，基于 train_counts/exp_round 防止 round 卡在 1
        if int(self.state.get("round", 1) or 1) == 1:
            tc_sum = sum(int(v or 0) for v in (self.state.get("train_counts") or {}).values())
            exp_r = int(self.state.get("exp_round", 0) or 0)
            if tc_sum > 0:
                self.state["round"] = tc_sum + 1
            elif exp_r > 2:
                self.state["round"] = max(2, exp_r - ((exp_r - 1) // 7) * 2)
        return True

    def poll(self) -> list[dict]:
        """增量读取日志新行（并自动感知新开游戏产生的新日志文件），返回本批捕获的关键事件列表。"""
        latest = self._find_latest_log()
        if latest and latest != self.log_path:
            self.attach_latest()
        if not self.fp:
            return []
        batch_events: list[dict] = []
        while True:
            line = self.fp.readline()
            if not line:
                break
            evs = self._feed_line(line, record_events=True)
            if evs:
                batch_events.extend(evs)
        return batch_events

    def _feed_line(self, line: str, record_events: bool = True) -> list[dict]:
        events: list[dict] = []
        stripped = line.strip()
        if stripped and any(
            k in stripped
            for k in (
                "[EDU_MODEL]",
                "[UIManager]",
                "[SceneMgr]",
                "[SCENARIO]",
                "request_call:    education.",
                "dispatch:    education.",
            )
        ):
            self.recent_lines.append(stripped[:220])

        # 跟踪剧情加载事件 ID 与名称（同步标记哨子 70/100 熟练度事件已触发，避免触发后永久残留提示）
        if "[EDU_MODEL]_LoadStory:" in line:
            sm = re.search(r"\[EDU_MODEL\]_LoadStory:\s+(\d+)\s+([^\s\t\r\n]+)", line)
            if sm:
                ev_id = sm.group(1)
                self.state["cur_story"] = (ev_id, sm.group(2))
                if ev_id == "70":
                    self.state["fired_70"] = True
                elif ev_id == "71":
                    self.state["fired_71"] = True

        # 跟踪商店购买、新局重置、平日训练/休息训次推进与关键 RPC 生命周期
        if "request_call:" in line and "education." in line:
            rm_all = re.search(r"(education\.cs_[a-z0-9_]+)", line)
            if rm_all:
                rpc_name = rm_all.group(1)
                if rpc_name == "education.cs_start_education":
                    self._reset_for_new_run()
                elif rpc_name == "education.cs_buy_edu_item":
                    self.state["store_buy_pending"] = True
                    bm = re.search(r"buy_num\s*=\s*(\d+),\s*store_item_id\s*=\s*(\d+)", line)
                    if bm:
                        iid = int(bm.group(2))
                        self.state["last_store_buy_req"] = {
                            "buy_num": int(bm.group(1)),
                            "store_item_id": iid,
                        }
                        lc._apply_facility_book_purchase(self.state, iid)
                    else:
                        bm_i = re.search(r"store_item_id\s*=\s*(\d+)", line)
                        if bm_i:
                            lc._apply_facility_book_purchase(self.state, int(bm_i.group(1)))
                elif rpc_name == "education.cs_train":
                    self.state["store_buy_pending"] = False
                    self.state["slot_machine_pending"] = False
                    cm_t = re.search(r"cmd_idx\s*=\s*(\d+)", line)
                    if cm_t:
                        c_idx = int(cm_t.group(1))
                        if 1 <= c_idx <= 6:
                            self.state["train_counts"][c_idx] = self.state["train_counts"].get(c_idx, 0) + 1
                    self.state["round"] = int(self.state.get("round", 1) or 1) + 1
                elif rpc_name == "education.cs_decide":
                    self.state["store_buy_pending"] = False
                    self.state["slot_machine_pending"] = False
                    if re.search(r"decide_type\s*=\s*2\b", line):
                        self.state["round"] = int(self.state.get("round", 1) or 1) + 1
                elif rpc_name == "education.cs_select_edu_slot_machines":
                    self.state["slot_machine_pending"] = True
                elif rpc_name in (
                    "education.cs_match",
                    "education.cs_select_edu_activity",
                    "education.cs_end_education",
                    "education.cs_restore_education",
                ):
                    self.state["store_buy_pending"] = False
                    self.state["slot_machine_pending"] = False
                if record_events:
                    events.append({"kind": "rpc", "name": rpc_name})

        if "[UIManager]BlockInput:" in line:
            bm = re.search(r"BlockInput:\s+(true|false)", line)
            if bm:
                if bm.group(1) == "true":
                    self.state["block_input"] = True
                    self.state["block_input_ts"] = time.time()
                else:
                    self.state["block_input"] = False
                    self.state["block_input_ts"] = 0.0

        if "[UIManager]" in line and "CloseAll" in line:
            self.state["store_buy_pending"] = False
            self.state["slot_machine_pending"] = False
            self.state["block_input"] = False
            self.state["block_input_ts"] = 0.0
        elif "[UIManager]" in line and ("OpenUIView" in line or "CloseUIView" in line or "HideUIView" in line):
            um = re.search(
                r"(OpenUIView|CloseUIView|HideUIView)(?:</color>|:)?\s+(?:\d+\s+(?:(?:true|false)\s+)?)?((?:ui\.view\.|mini_game\.)[^\s,;\]]+)",
                line,
            )
            if not um:
                um = re.search(r"(OpenUIView|CloseUIView|HideUIView)(?:</color>|:)?\s+([a-zA-Z0-9_.]+)", line)
            if um:
                op, vn = um.group(1), um.group(2).rsplit(".", 1)[-1]
                if vn == "edu_store_view" and op in ("OpenUIView", "CloseUIView", "HideUIView"):
                    self.state["store_buy_pending"] = False
                if vn == "slot_machine_view" and op in ("CloseUIView", "HideUIView"):
                    self.state["slot_machine_pending"] = False
                if record_events:
                    events.append({"kind": "view_open" if op == "OpenUIView" else "view_close", "name": vn})
        if "[EDU_MODEL]_SwitchState" in line:
            em = re.search(r"进入状态:\s*(\d+)", line)
            if em:
                st_val = int(em.group(1))
                self.state["store_buy_pending"] = False
                if st_val != 21:
                    self.state["slot_machine_pending"] = False
                if record_events:
                    events.append({"kind": "state_id", "value": st_val})

        if "[EDU_MODEL] 更新育成数据" in line:
            self._edu_buf = line
        elif self._edu_buf:
            self._edu_buf += line
            if len(self._edu_buf) > 120000:
                self._edu_buf = ""

        ui_res = lc.update_ui_state_from_line(self.state, line, self._skill_buf)
        token_updated = False

        if self._edu_buf and line.rstrip().endswith("}"):
            em = re.search(r"\[EDU_MODEL\] 更新育成数据\s*\{(?:\n.*?\n\}|[^\n]*\})\s*$", self._edu_buf, re.S)
            if em:
                tok = em.group(0)
                self._edu_buf = ""
                lc.update_state_from_token(self.state, tok)
                token_updated = True
                if "edu_store_info" in tok and self.state.get("store_open") is False:
                    self.state["store_buy_pending"] = False
                if "train_cmds = {}" in tok:
                    self.last_train_cmds = []
                elif self.state.get("round_type", 1) == 1 and "train_cmds" in tok:
                    cmds = lc.parse_train_cmd_block(tok)
                    if len(cmds) == 6:
                        self.last_train_cmds = cmds

        if record_events and (ui_res.get("changed") or ui_res.get("skill_popup_ready") or token_updated):
            events.append({"kind": "state_changed", "mode": ui_res.get("mode", "")})

        if record_events and events:
            for ev in events:
                self.recent_events.append(ev)
        return events
