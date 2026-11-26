/* ==========================================================================
   LQSNCoach 私人作战室前端控制器 (dashboard/js/app.js)
   - 在线模式（本地 HTTP 服务）：四视图全功能 + 实时日志监控 + 任意单局复盘
   - 离线模式（直接双击 HTML）：优雅降级加载 dashboard/data/snapshot.js 静态快照
   ========================================================================== */

(function () {
  "use strict";

  const STAT_LIST = [
    { id: "101", name: "投射" },
    { id: "102", name: "弹跳" },
    { id: "103", name: "防守" },
    { id: "104", name: "体能" },
    { id: "105", name: "运控" },
    { id: "106", name: "意识" },
  ];
  const POS_ORDER = ["C", "PF", "SF", "SG", "PG"];
  const POS_LABEL = { C: "C · 中锋", PF: "PF · 大前", SF: "SF · 小前", SG: "SG · 分卫", PG: "PG · 控卫" };

  const state = {
    online: false,
    view: "overview",
    overview: null,
    box: null,
    trendChar: "",
    trendLimit: 0,
    runSearch: "",
    runChar: "",
    runOnly10k: false,
    selectedRunId: null,
    compareIds: [],
    runDetailCache: {},
    plannerCharId: 1002,
    plannerMode: "score",
    plannerFocus: "",
    plannerNoteTab: "borrow",
    plannerEquipTab: "owned",
    plannerData: null,
    boxSubTab: "gap",
    boxSearch: "",
    livePollMs: 2500,
    liveTimer: null,
    liveLast: null,
  };

  /* -------------------- 工具函数 -------------------- */
  function $(id) {
    return document.getElementById(id);
  }

  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function avatarHtml(src, name, cls) {
    const c = cls ? "avatar " + cls : "avatar";
    if (src) {
      return `<img class="${c}" src="${esc(src)}" alt="${esc(name || "")}" loading="lazy" onerror="this.style.display='none'" />`;
    }
    const ch = (name || "?").slice(0, 1);
    return `<span class="${c} avatar-fallback">${esc(ch)}</span>`;
  }

  function equipIcoHtml(src, name) {
    if (src) {
      return `<img class="equip-ico" src="${esc(src)}" alt="${esc(name || "")}" loading="lazy" onerror="this.style.display='none'" />`;
    }
    return `<span class="equip-ico avatar-fallback">${esc((name || "器").slice(0, 1))}</span>`;
  }

  async function apiGet(url) {
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    return resp.json();
  }

  function setConnBadge(online, text) {
    const el = $("conn-badge");
    if (!el) return;
    el.textContent = text;
    el.className = "conn-badge " + (online ? "ok" : "offline");
  }

  /* -------------------- 视图切换 -------------------- */
  function switchView(viewName) {
    state.view = viewName;
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      const active = btn.dataset.view === viewName;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".view-panel").forEach((p) => {
      p.classList.toggle("active", p.id === "view-" + viewName);
    });
    if (viewName === "runs" && !state.selectedRunId && state.overview && state.overview.runs.length) {
      const latest = state.overview.runs[state.overview.runs.length - 1];
      selectRun(latest.run_id);
    }
    if (viewName === "box" && !state.plannerData) {
      loadPlanner();
    }
    if (viewName === "live") {
      pollLiveNow();
      startLiveLoop();
    } else {
      stopLiveLoop();
    }
  }

  /* ==================== 视图 1：总览 ==================== */
  function renderOverview() {
    const ov = state.overview;
    if (!ov) return;
    const k = ov.kpi;

    // 1. 4 张 KPI 卡片
    const delta10 = k.avg_rank_prev10 ? (k.avg_rank_recent10 - k.avg_rank_prev10).toFixed(0) : null;
    const deltaHtml = delta10 !== null
      ? `<span class="badge ${Number(delta10) >= 0 ? "badge-green" : "badge-red"}">${Number(delta10) >= 0 ? "+" : ""}${delta10} vs 前10局</span>`
      : "";

    $("overview-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">历史最高分</div>
        <div class="kpi-val" style="color:var(--gold)">${k.best_rank.toLocaleString()}</div>
        <div class="kpi-foot"><span>破万 ${k.over_10k_count} / ${k.total_runs} 局</span><span class="badge badge-gold">${k.total_runs ? Math.round(k.over_10k_count / k.total_runs * 100) : 0}%</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">近 10 局均分</div>
        <div class="kpi-val">${Math.round(k.avg_rank_recent10).toLocaleString()}</div>
        <div class="kpi-foot"><span>全历史均分 ${Math.round(k.avg_rank_all).toLocaleString()}</span>${deltaHtml}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">5 位置破万进度</div>
        <div class="kpi-val">${k.pos_10k_count} / 5</div>
        <div class="kpi-foot"><span>10 级托管全位置保底底座</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">自备 7 阶彩笔记</div>
        <div class="kpi-val" style="color:var(--purple)">${k.rainbow_notes_count} <span style="font-size:14px;font-weight:500;color:var(--text-muted)">(${k.rainbow_chars_count}人)</span></div>
        <div class="kpi-foot"><span>总笔记 ${k.own_notes_total} 自备 · ${k.borrowed_notes_total} 好友</span>${k.diamonds ? `<span class="badge badge-blue">💎 ${esc(k.diamonds)}</span>` : ""}</div>
      </div>
    `;

    // 2. 5 位置底座
    $("pos-10k-summary").textContent = `${k.pos_10k_count} / 5 位置已破万`;
    $("overview-positions").innerHTML = ov.positions.map((p) => {
      const pct = Math.min(100, Math.round((p.best_rank / 10500) * 100));
      const gap = Math.max(0, 10000 - p.best_rank);
      return `
        <div class="pos-card ${p.is_10k ? "is-10k" : "need-10k"}" data-run-id="${esc(p.best_run_id)}">
          <div class="pos-top">
            <span class="pos-code">${esc(p.pos)} · ${esc(p.pos_cn)}</span>
            <span class="badge ${p.is_10k ? "badge-green" : "badge-gold"}">${p.is_10k ? "已破万" : "差 " + gap}</span>
          </div>
          <div class="pos-who">
            ${avatarHtml(p.best_head, p.best_char)}
            <div>
              <div class="pos-score num">${p.best_rank ? p.best_rank.toLocaleString() : "—"}</div>
              <div class="pos-name">${esc(p.best_char || "暂无记录")}</div>
            </div>
          </div>
          <div class="pos-bar"><div class="pos-bar-fill" style="width:${pct}%"></div></div>
          <div class="pos-foot">
            <span>托管估 ~${p.auto_est || "—"}</span>
            <span>${p.run_count} 局</span>
          </div>
        </div>
      `;
    }).join("");

    document.querySelectorAll(".pos-card[data-run-id]").forEach((el) => {
      el.addEventListener("click", () => {
        const rid = el.dataset.runId;
        if (rid) {
          switchView("runs");
          selectRun(rid);
        }
      });
    });

    // 3. 填充筛选下拉
    const fillCharSelect = (selId, currentVal) => {
      const sel = $(selId);
      if (!sel) return;
      const opts = ['<option value="">全部主力</option>'].concat(
        ov.played_chars.map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${esc(c.pos || "?")})</option>`)
      );
      sel.innerHTML = opts.join("");
      sel.value = currentVal || "";
    };
    fillCharSelect("trend-char-filter", state.trendChar);
    fillCharSelect("run-char-filter", state.runChar);

    // 4. 折线图
    renderTrendChart();

    // 5. 冲分队列 & 瓶颈
    $("overview-updated").textContent = `快照同步 ${ov.generated_at || ""}`;
    $("overview-warnings").innerHTML = (ov.goal_warnings || [])
      .map((w) => `<div class="warn-banner">⚠️ ${esc(w)}</div>`)
      .join("");

    $("overview-bottlenecks").innerHTML = (ov.bottlenecks || []).slice(0, 6).map((b) => {
      const pct = b.target_rank ? Math.min(100, Math.round((b.current_best_rank / b.target_rank) * 100)) : 0;
      return `
        <div class="goal-card" data-cid="${b.char_id || ""}">
          <div class="goal-head">
            ${avatarHtml(b.head, b.char_name)}
            <div style="min-width:0;flex:1">
              <div class="goal-title">
                <span class="badge badge-blue">P${b.priority}</span>
                <span>${esc(b.char_name)}</span>
                <span class="badge">${esc(b.role)}</span>
                ${b.has_rainbow_note ? '<span class="badge badge-purple">有彩笔记</span>' : '<span class="badge badge-gold">缺彩笔记</span>'}
              </div>
              <div class="goal-meta">专项：${esc(b.focus_stat || "均衡")}</div>
            </div>
          </div>
          <div class="goal-progress-row">
            <span class="num">当前 <strong>${b.current_best_rank.toLocaleString()}</strong> / 目标 ${b.target_rank.toLocaleString()}</span>
            <span class="${b.rank_gap === 0 ? "diff-pos" : ""}">${b.rank_gap === 0 ? "已达标" : "差 " + b.rank_gap}</span>
          </div>
          <div class="pos-bar"><div class="pos-bar-fill" style="width:${pct}%;background:${b.rank_gap === 0 ? "var(--green)" : "var(--accent)"}"></div></div>
          <div class="goal-note">${esc(b.primary_bottleneck || b.advice || "")}</div>
        </div>
      `;
    }).join("");

    document.querySelectorAll(".goal-card[data-cid]").forEach((el) => {
      el.addEventListener("click", () => {
        const cid = Number(el.dataset.cid);
        if (cid) {
          state.plannerCharId = cid;
          const sel = $("planner-char-select");
          if (sel) sel.value = String(cid);
          switchView("box");
          loadPlanner();
        }
      });
    });
  }

  function renderTrendChart() {
    const ov = state.overview;
    if (!ov) return;
    let runs = ov.runs.filter((r) => r.final_rank > 0);
    if (state.trendChar) {
      runs = runs.filter((r) => r.main_name === state.trendChar);
    }
    if (state.trendLimit > 0 && runs.length > state.trendLimit) {
      runs = runs.slice(-state.trendLimit);
    }
    const host = $("trend-chart");
    if (!runs.length) {
      host.innerHTML = '<div class="empty-state">该筛选条件下暂无战绩数据</div>';
      return;
    }

    const W = 960;
    const H = 245;
    const padL = 50;
    const padR = 20;
    const padT = 18;
    const padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const scores = runs.map((r) => r.final_rank);
    const minY = Math.floor((Math.min(...scores, 8500) - 200) / 500) * 500;
    const maxY = Math.ceil((Math.max(...scores, 10200) + 200) / 500) * 500;
    const spanY = Math.max(500, maxY - minY);

    const xOf = (i) => padL + (runs.length === 1 ? plotW / 2 : (i / (runs.length - 1)) * plotW);
    const yOf = (v) => padT + plotH - ((v - minY) / spanY) * plotH;

    // 水平参考线
    const ticks = [];
    const step = spanY > 3500 ? 1000 : 500;
    for (let v = minY; v <= maxY; v += step) {
      ticks.push(v);
    }
    const gridSvg = ticks.map((v) => {
      const y = yOf(v).toFixed(1);
      const is10k = v === 10000;
      return `
        <line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"
              stroke="${is10k ? "var(--gold)" : "var(--border)"}"
              stroke-dasharray="${is10k ? "5 4" : "none"}"
              stroke-width="${is10k ? 1.4 : 1}" />
        <text x="${padL - 8}" y="${Number(y) + 4}" text-anchor="end"
              fill="${is10k ? "var(--gold)" : "var(--text-dim)"}" font-size="11">${v}</text>
      `;
    }).join("");

    // 折线与渐变填充
    const pts = runs.map((r, i) => `${xOf(i).toFixed(1)},${yOf(r.final_rank).toFixed(1)}`).join(" ");
    const areaPts = `${xOf(0).toFixed(1)},${(padT + plotH).toFixed(1)} ${pts} ${xOf(runs.length - 1).toFixed(1)},${(padT + plotH).toFixed(1)}`;

    // X 轴稀疏日期标签
    const labelCount = Math.min(6, runs.length);
    const xLabels = [];
    for (let k = 0; k < labelCount; k++) {
      const idx = labelCount === 1 ? 0 : Math.round((k / (labelCount - 1)) * (runs.length - 1));
      xLabels.push(`<text x="${xOf(idx).toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--text-dim)" font-size="11">${esc(runs[idx].date_label.slice(0, 5))}</text>`);
    }

    // 节点圆点
    const dots = runs.map((r, i) => {
      const cx = xOf(i).toFixed(1);
      const cy = yOf(r.final_rank).toFixed(1);
      const col = r.final_rank >= 10000 ? "var(--gold)" : "var(--accent)";
      const rad = runs.length > 50 ? 3.5 : 4.5;
      return `<circle class="chart-dot" data-idx="${i}" cx="${cx}" cy="${cy}" r="${rad}" fill="${col}" stroke="var(--surface)" stroke-width="1.5" style="cursor:pointer" />`;
    }).join("");

    host.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <defs>
          <linearGradient id="trendGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.26" />
            <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.01" />
          </linearGradient>
        </defs>
        ${gridSvg}
        <polygon points="${areaPts}" fill="url(#trendGrad)" />
        <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
        ${xLabels.join("")}
        ${dots}
      </svg>
    `;

    const tip = $("trend-tooltip");
    const wrap = $("trend-chart-wrap");
    host.querySelectorAll(".chart-dot").forEach((dot) => {
      dot.addEventListener("mouseenter", (e) => {
        const r = runs[Number(dot.dataset.idx)];
        tip.innerHTML = `
          <div style="font-weight:700;margin-bottom:3px">${esc(r.main_name)} · <span style="color:${r.final_rank >= 10000 ? "var(--gold)" : "var(--accent)"}">${r.final_rank.toLocaleString()}</span></div>
          <div style="color:var(--text-muted)">${esc(r.date_label)} · #${r.seq_no}</div>
          <div style="color:var(--text-muted);margin-top:2px">吻合 ${r.coach_match_pct.toFixed(0)}% · 均亏 ${r.avg_turn_ev_loss.toFixed(1)} EV</div>
        `;
        tip.classList.remove("hidden");
        const rect = wrap.getBoundingClientRect();
        const left = Math.min(rect.width - 180, Math.max(8, e.clientX - rect.left + 12));
        const top = Math.max(8, e.clientY - rect.top - 64);
        tip.style.left = left + "px";
        tip.style.top = top + "px";
      });
      dot.addEventListener("mouseleave", () => tip.classList.add("hidden"));
      dot.addEventListener("click", () => {
        const r = runs[Number(dot.dataset.idx)];
        switchView("runs");
        selectRun(r.run_id);
      });
    });
  }

  /* ==================== 视图 2：单局复盘 & 双局对比 ==================== */
  function renderRunsList() {
    const ov = state.overview;
    if (!ov) return;
    const kw = state.runSearch.trim().toLowerCase();
    const list = ov.runs
      .slice()
      .reverse()
      .filter((r) => {
        if (state.runChar && r.main_name !== state.runChar) return false;
        if (state.runOnly10k && r.final_rank < 10000) return false;
        if (kw) {
          const hay = `${r.main_name} ${r.lineup} ${r.date_label} ${r.final_rank}`.toLowerCase();
          if (!hay.includes(kw)) return false;
        }
        return true;
      });

    // 更新对比状态栏
    const cLen = state.compareIds.length;
    $("compare-hint").textContent =
      cLen === 0 ? "勾选 2 局可开启左右对比" :
      cLen === 1 ? "已勾选 1 局，再勾 1 局即可对比" :
      "正在对比已选 2 局";
    $("btn-clear-compare").classList.toggle("hidden", cLen === 0);

    $("runs-list").innerHTML = list.length ? list.map((r) => {
      const isActive = state.compareIds.length < 2 && state.selectedRunId === r.run_id;
      const isChecked = state.compareIds.includes(r.run_id);
      return `
        <div class="run-item ${isActive ? "active" : ""}" data-rid="${esc(r.run_id)}">
          <input type="checkbox" class="cmp-chk" data-rid="${esc(r.run_id)}" ${isChecked ? "checked" : ""} title="勾选加入双局对比" />
          ${avatarHtml(r.main_head, r.main_name, "avatar-sm")}
          <div class="run-item-info">
            <div class="run-item-top">
              <span class="run-item-name">${esc(r.main_name)} <span style="font-weight:400;color:var(--text-dim)">${esc(r.main_pos)}</span></span>
              <span class="run-item-score num ${r.final_rank >= 10000 ? "is-10k" : ""}">${r.final_rank.toLocaleString()}</span>
            </div>
            <div class="run-item-sub">
              <span>${esc(r.date_label)}</span>
              <span>吻合 ${r.coach_match_pct.toFixed(0)}% · T15 ${r.crit_maxed_by_t15}/${r.crit_equip_total}</span>
            </div>
          </div>
        </div>
      `;
    }).join("") : '<div class="empty-state">无匹配对局</div>';

    $("runs-list").querySelectorAll(".run-item").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.classList.contains("cmp-chk")) return;
        const rid = el.dataset.rid;
        if (state.compareIds.length >= 2) {
          state.compareIds = [];
        }
        selectRun(rid);
      });
    });

    $("runs-list").querySelectorAll(".cmp-chk").forEach((chk) => {
      chk.addEventListener("change", (e) => {
        e.stopPropagation();
        const rid = chk.dataset.rid;
        if (chk.checked) {
          if (state.compareIds.length >= 2) {
            state.compareIds.shift();
          }
          if (!state.compareIds.includes(rid)) state.compareIds.push(rid);
        } else {
          state.compareIds = state.compareIds.filter((x) => x !== rid);
        }
        renderRunsList();
        if (state.compareIds.length === 2) {
          renderRunCompare(state.compareIds[0], state.compareIds[1]);
        } else if (state.selectedRunId) {
          selectRun(state.selectedRunId);
        }
      });
    });
  }

  async function fetchRunDetail(runId) {
    if (state.runDetailCache[runId]) return state.runDetailCache[runId];
    if (state.online) {
      const data = await apiGet("/api/run/" + encodeURIComponent(runId));
      state.runDetailCache[runId] = data;
      return data;
    }
    const fallback = (state.overview.runs || []).find((r) => r.run_id === runId);
    return fallback ? Object.assign({ has_archive: false }, fallback) : null;
  }

  async function selectRun(runId) {
    state.selectedRunId = runId;
    renderRunsList();
    const area = $("runs-detail-area");
    area.innerHTML = '<div class="panel empty-state">正在读取单局复盘数据…</div>';
    try {
      const d = await fetchRunDetail(runId);
      if (!d) {
        area.innerHTML = '<div class="panel empty-state">未找到该局记录</div>';
        return;
      }
      renderSingleRunDetail(d);
    } catch (err) {
      area.innerHTML = `<div class="panel empty-state">读取失败：${esc(err.message)}</div>`;
    }
  }

  function renderSingleRunDetail(d) {
    const area = $("runs-detail-area");
    const statsHtml = STAT_LIST.map((st) => {
      const val = Number((d.final_stats || {})[st.id] || 0);
      const cap = Number((d.prop_max || {})[st.id] || 1500);
      const pct = cap > 0 ? Math.min(100, Math.round((val / cap) * 100)) : 0;
      const isCap = val >= cap - 15;
      const isShort = val < 1050;
      const fillCls = isCap ? "capped" : isShort ? "short" : val >= 1600 ? "high" : "";
      return `
        <div class="stat-row">
          <div class="stat-row-top">
            <span class="stat-label">${st.name} ${isCap ? '<span class="badge badge-gold">撞顶</span>' : isShort ? '<span class="badge badge-red">短板</span>' : ""}</span>
            <span class="stat-num"><strong>${val}</strong> <span style="color:var(--text-dim)">/ ${cap}</span></span>
          </div>
          <div class="stat-track"><div class="stat-fill ${fillCls}" style="width:${pct}%"></div></div>
        </div>
      `;
    }).join("");

    // 器材满级节奏芯片
    const eqNamed = Object.values(d.equip_max_named || {}).sort((a, b) => (a.turn || 99) - (b.turn || 99));
    const eqHtml = eqNamed.length ? `
      <div class="equip-chip-row">
        ${eqNamed.map((eq) => `
          <div class="equip-chip">
            ${equipIcoHtml(eq.icon, eq.name)}
            <span>${esc(eq.name)}</span>
            <span class="badge ${eq.turn <= 15 ? "badge-green" : "badge-gold"}">T${eq.turn} 满</span>
          </div>
        `).join("")}
      </div>
    ` : "";

    // Top 失误回合
    const subTurns = d.top_suboptimal_turns || [];
    const subHtml = !d.has_archive
      ? '<p class="panel-sub">离线快照仅包含摘要；启动本地服务 <code>python3 src/dashboard_server.py</code> 可查看完整逐回合失误分析。</p>'
      : subTurns.length === 0
      ? '<div class="badge badge-green" style="padding:6px 12px">✅ 本局未出现显著 EV 失误回合，整体贴合军师推荐</div>'
      : `
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>回合</th><th>体力 / 金币</th><th>实际选择</th><th>军师首选</th><th>EV 亏损</th></tr>
            </thead>
            <tbody>
              ${subTurns.slice(0, 10).map((t) => `
                <tr>
                  <td>R${t.exp_round || t.turn_no || "?"}</td>
                  <td>${t.ps ?? "—"} 体 · ${t.gold ?? "—"} 金</td>
                  <td>${esc(t.chosen_name || t.chosen || "—")}</td>
                  <td style="color:var(--green)">${esc(t.best_name || t.best || "—")}</td>
                  <td class="diff-neg">-${Number(t.ev_loss || t.loss || 0).toFixed(1)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;

    // 折叠逐回合时间线
    const wkList = d.weekend_timeline || [];
    const wdList = d.weekday_timeline || [];
    const timelineHtml = d.has_archive ? `
      <details class="fold-block">
        <summary>展开 7 周周末决策记录（${wkList.length} 条）</summary>
        <div class="table-wrap" style="margin-top:10px">
          <table class="data-table">
            <thead><tr><th>周次</th><th>体力 / 金币</th><th>实际行动</th><th>吻合</th><th>军师首选</th></tr></thead>
            <tbody>
              ${wkList.map((w) => `
                <tr>
                  <td>${esc(w.day_tag || ("W" + w.wk_no))}</td>
                  <td>${w.ps ?? "—"} / ${w.gold ?? "—"}</td>
                  <td>${esc(w.chosen_bldg_name || "—")}</td>
                  <td>${w.is_match ? '<span class="badge badge-green">一致</span>' : '<span class="badge badge-gold">分歧</span>'}</td>
                  <td style="color:var(--text-muted);font-size:12px">${esc(w.best_headline || "—")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </details>
      <details class="fold-block">
        <summary>展开平日逐回合训练流（${wdList.length} 回合）</summary>
        <div class="table-wrap" style="margin-top:10px">
          <table class="data-table">
            <thead><tr><th>回合</th><th>体 / 金</th><th>选择训练</th><th>EV</th><th>军师首选</th><th>亏损</th></tr></thead>
            <tbody>
              ${wdList.map((t) => {
                const loss = Number(t.ev_loss || 0);
                return `
                  <tr>
                    <td>T${t.turn_no} (R${t.exp_round})</td>
                    <td>${t.ps} / ${t.gold}</td>
                    <td>${esc(t.chosen_name || "—")}</td>
                    <td>${t.chosen_ev != null ? Number(t.chosen_ev).toFixed(1) : "—"}</td>
                    <td style="color:var(--text-muted)">${esc(t.best_name || "—")}</td>
                    <td class="${loss >= 15 ? "diff-neg" : ""}">${loss > 0.5 ? "-" + loss.toFixed(1) : "0"}</td>
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        </div>
      </details>
    ` : "";

    const notesStr = (d.note_scores || []).length ? d.note_scores.join(" + ") : "—";

    area.innerHTML = `
      <section class="panel">
        <div class="run-hero">
          <div class="run-hero-left">
            ${avatarHtml(d.main_head, d.main_name, "avatar-lg")}
            <div>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <h2 class="panel-title" style="font-size:18px">${esc(d.main_name)}</h2>
                <span class="badge">${esc(d.main_pos || "?")}</span>
                <span class="panel-sub" style="margin:0">${esc(d.date_label)} · #${d.seq_no}</span>
              </div>
              <div class="panel-sub" style="margin-top:4px">${esc(d.lineup)}</div>
            </div>
          </div>
          <div style="text-align:right">
            <div class="run-hero-score num ${d.final_rank >= 10000 ? "is-10k" : ""}">${d.final_rank.toLocaleString()}</div>
            <div class="panel-sub">六维总和 ${d.stat_sum.toLocaleString()}</div>
          </div>
        </div>

        <div class="metric-strip">
          <div class="metric-box">
            <div class="metric-box-label">军师吻合率</div>
            <div class="metric-box-val num">${d.coach_match_pct.toFixed(1)}%</div>
          </div>
          <div class="metric-box">
            <div class="metric-box-label">回合平均 EV 亏</div>
            <div class="metric-box-val num">${d.avg_turn_ev_loss.toFixed(1)}</div>
          </div>
          <div class="metric-box">
            <div class="metric-box-label">器材 T12 / T15 满</div>
            <div class="metric-box-val num">${d.crit_maxed_by_t12} / ${d.crit_maxed_by_t15} <span style="font-size:12px;color:var(--text-dim)">(共${d.crit_equip_total})</span></div>
          </div>
          <div class="metric-box">
            <div class="metric-box-label">商店买 / 扭蛋次</div>
            <div class="metric-box-val num">${d.store_buys} / ${d.slot_machines}</div>
          </div>
          <div class="metric-box">
            <div class="metric-box-label">携带笔记总分</div>
            <div class="metric-box-val num">${esc(notesStr)}</div>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3 class="panel-title">六维实得 vs 属性上限</h3>
          <span class="panel-sub">爆发次数 ${d.mgr_explosive} · 最低属性 ${d.min_stat} · 最高 ${d.max_stat}</span>
        </div>
        <div class="stats-grid">${statsHtml}</div>
        ${eqHtml}
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3 class="panel-title">关键失误 / 分歧回合</h3>
          <span class="panel-sub">按单回合 EV 亏损降序</span>
        </div>
        ${subHtml}
        ${timelineHtml}
      </section>
    `;
  }

  async function renderRunCompare(idA, idB) {
    const area = $("runs-detail-area");
    area.innerHTML = '<div class="panel empty-state">正在加载双局对比…</div>';
    const [a, b] = await Promise.all([fetchRunDetail(idA), fetchRunDetail(idB)]);
    if (!a || !b) {
      area.innerHTML = '<div class="panel empty-state">无法加载对比数据</div>';
      return;
    }

    const diffSpan = (v, invert) => {
      const n = Number(v);
      if (Math.abs(n) < 0.05) return '<span style="color:var(--text-dim)">0</span>';
      const good = invert ? n < 0 : n > 0;
      const sign = n > 0 ? "+" : "";
      const txt = Number.isInteger(n) ? `${sign}${n}` : `${sign}${n.toFixed(1)}`;
      return `<span class="${good ? "diff-pos" : "diff-neg"}">${txt}</span>`;
    };

    const rows = [
      ["最终评分", a.final_rank, b.final_rank, b.final_rank - a.final_rank, false],
      ["六维总和", a.stat_sum, b.stat_sum, b.stat_sum - a.stat_sum, false],
      ...STAT_LIST.map((st) => {
        const va = Number((a.final_stats || {})[st.id] || 0);
        const vb = Number((b.final_stats || {})[st.id] || 0);
        return [`属性 · ${st.name}`, `${va} / ${(a.prop_max || {})[st.id] || "-"}`, `${vb} / ${(b.prop_max || {})[st.id] || "-"}`, vb - va, false];
      }),
      ["军师吻合率", a.coach_match_pct.toFixed(1) + "%", b.coach_match_pct.toFixed(1) + "%", b.coach_match_pct - a.coach_match_pct, false],
      ["回合平均 EV 亏", a.avg_turn_ev_loss.toFixed(1), b.avg_turn_ev_loss.toFixed(1), b.avg_turn_ev_loss - a.avg_turn_ev_loss, true],
      ["器材 T15 满级数", a.crit_maxed_by_t15, b.crit_maxed_by_t15, b.crit_maxed_by_t15 - a.crit_maxed_by_t15, false],
      ["商店购买数", a.store_buys, b.store_buys, b.store_buys - a.store_buys, false],
      ["扭蛋次数", a.slot_machines, b.slot_machines, b.slot_machines - a.slot_machines, false],
    ];

    area.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2 class="panel-title">双局对比（B 相对 A 差值）</h2>
            <p class="panel-sub">A：${esc(a.main_name)} ${a.final_rank} (${esc(a.date_label)}) &nbsp;vs&nbsp; B：${esc(b.main_name)} ${b.final_rank} (${esc(b.date_label)})</p>
          </div>
          <button type="button" class="action-btn" id="btn-exit-cmp">退出对比</button>
        </div>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>指标</th><th>A · ${esc(a.main_name)} (${a.final_rank})</th><th>B · ${esc(b.main_name)} (${b.final_rank})</th><th>B - A</th></tr>
            </thead>
            <tbody>
              ${rows.map(([label, va, vb, diff, inv]) => `
                <tr>
                  <td style="font-weight:600">${esc(label)}</td>
                  <td>${esc(va)}</td>
                  <td>${esc(vb)}</td>
                  <td>${diffSpan(diff, inv)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </section>
    `;

    $("btn-exit-cmp").addEventListener("click", () => {
      state.compareIds = [];
      renderRunsList();
      selectRun(b.run_id);
    });
  }

  /* ==================== 视图 3：笔记与配装 ==================== */
  function initBoxControls() {
    const box = state.box;
    if (!box) return;
    const sel = $("planner-char-select");
    const chars = box.characters.slice().sort((a, b) => Number(b.owned) - Number(a.owned) || b.best_rank - a.best_rank);
    sel.innerHTML = chars.map((c) =>
      `<option value="${c.id}">${c.owned ? "★ " : ""}${esc(c.short_name)} · ${esc(c.pos)}${c.best_rank ? " (" + c.best_rank + ")" : ""}</option>`
    ).join("");
    if (chars.some((c) => c.id === state.plannerCharId)) {
      sel.value = String(state.plannerCharId);
    } else if (chars.length) {
      state.plannerCharId = chars[0].id;
      sel.value = String(chars[0].id);
    }
    renderBoxSubTab();
  }

  async function loadPlanner() {
    if (!state.online) {
      $("planner-notes-body").innerHTML = '<div class="empty-state">离线快照模式下不支持实时配对运算，请运行 <code>python3 src/dashboard_server.py</code></div>';
      $("planner-equips-body").innerHTML = "";
      return;
    }
    $("planner-notes-body").innerHTML = '<div class="empty-state">正在调用 note_advisor 真值计算…</div>';
    $("planner-equips-body").innerHTML = '<div class="empty-state">正在调用 equipment_advisor 真值计算…</div>';
    try {
      const qs = new URLSearchParams({
        char_id: String(state.plannerCharId),
        mode: state.plannerMode,
        focus: state.plannerFocus,
      });
      state.plannerData = await apiGet("/api/planner?" + qs.toString());
      renderPlanner();
    } catch (err) {
      $("planner-notes-body").innerHTML = `<div class="empty-state">加载失败：${esc(err.message)}</div>`;
    }
  }

  function renderPlanner() {
    const p = state.plannerData;
    if (!p) return;
    const c = p.character;
    $("planner-char-banner").innerHTML = `
      <div class="char-banner-left">
        ${avatarHtml(c.head, c.short_name, "avatar-lg")}
        <div>
          <div style="font-size:16px;font-weight:700">${esc(c.show_name || c.short_name)} <span class="badge badge-blue">${esc(c.best_pos)}</span></div>
          <div class="panel-sub">定位：${esc(c.roles_str || "—")} · 主练：${esc((c.recommend_train || []).join(" / "))}</div>
        </div>
      </div>
      <div class="panel-sub">${esc(p.equipments.owned_pvp_note || p.equipments.wish_headline || "")}</div>
    `;
    renderPlannerNotes();
    renderPlannerEquips();
  }

  function renderPlannerNotes() {
    const p = state.plannerData;
    if (!p) return;
    const host = $("planner-notes-body");
    const tab = state.plannerNoteTab;

    if (tab === "breeder") {
      const ab = p.notes.auto_breeders || {};
      const owned = ab.same_pos_owned || [];
      host.innerHTML = owned.length ? `
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>同位置种马（已拥有）</th><th>双边关系分</th><th>现有自备最高</th><th>主练</th></tr></thead>
            <tbody>
              ${owned.slice(0, 8).map((x) => `
                <tr>
                  <td style="display:flex;align-items:center;gap:8px">${avatarHtml(x.head, x.actor_name, "avatar-sm")}<strong>${esc(x.actor_name)}</strong></td>
                  <td class="badge-blue" style="font-weight:700">${x.rel_to_core} / ${x.rel_from_core}</td>
                  <td>${x.best_own_rank ? `<span class="badge ${x.best_own_rank >= 7 ? "badge-purple" : ""}">${x.best_own_rank}阶 ${x.best_own_score}</span>` : '<span style="color:var(--text-dim)">暂无</span>'}</td>
                  <td style="color:var(--text-muted)">${esc((x.rec_train || []).join("/"))}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      ` : '<div class="empty-state">暂无同位置种马数据</div>';
      return;
    }

    const combos = tab === "own" ? (p.notes.pure_own_combos || []) : (p.notes.borrow_combos || []);
    if (!combos.length) {
      host.innerHTML = '<div class="empty-state">当前笔记池暂无可用配对</div>';
      return;
    }

    const renderMiniNote = (n, tag) => `
      <div class="note-mini">
        ${avatarHtml(n.head, n.actor_name, "avatar-sm")}
        <div class="note-mini-body">
          <div class="note-mini-title">
            <span>${esc(n.actor_name)}</span>
            <span class="badge ${n.rank >= 7 ? "badge-purple" : ""}">${n.rank}阶 ${n.score}</span>
            ${n.friend ? `<span class="badge badge-gold">借:${esc(n.friend)}</span>` : `<span class="badge">${tag}</span>`}
          </div>
          <div class="note-mini-sub">相性 ${n.rel}${n.harmony_bonus ? `+${n.harmony_bonus}` : ""} · ${esc(n.highlights || "无高亮词条")}</div>
        </div>
      </div>
    `;

    host.innerHTML = `<div class="combo-list">${combos.slice(0, 5).map((cb, idx) => `
      <div class="combo-card">
        <div class="combo-head">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span class="badge ${idx === 0 ? "badge-green" : "badge-blue"}">#${idx + 1} 契合 ${cb.total_compat} (${esc(cb.rank_name)})</span>
            <span class="badge">${cb.learn_times}次学 · 期望 ${cb.expected_triggers} 词条</span>
          </div>
          <span class="panel-sub" style="margin:0">均分 ${cb.avg_note_score} · 匹配上限 +${cb.matched_cap_gain}</span>
        </div>
        <div class="combo-pair">
          ${renderMiniNote(cb.note1, "自备")}
          ${renderMiniNote(cb.note2, tab === "own" ? "自备" : "借用")}
        </div>
      </div>
    `).join("")}</div>`;
  }

  function renderPlannerEquips() {
    const p = state.plannerData;
    if (!p) return;
    const eq = p.equipments;
    const isOwned = state.plannerEquipTab === "owned";
    let list = eq.owned_loadout || [];
    let pat = eq.owned_pattern || {};
    if (!isOwned) {
      if (state.plannerMode === "pvp_22") {
        list = eq.pvp_22_loadout || eq.bis_loadout || [];
        pat = eq.pvp_22_pattern || {};
      } else if (state.plannerMode === "pvp_31") {
        list = eq.pvp_31_loadout || eq.bis_loadout || [];
        pat = eq.pvp_31_pattern || {};
      } else {
        list = eq.bis_loadout || [];
        pat = eq.bis_pattern || {};
      }
    }

    const host = $("planner-equips-body");
    const patHtml = pat && pat.tag ? `<div style="margin-bottom:10px"><span class="badge badge-blue">${esc(pat.tag)}</span></div>` : "";
    host.innerHTML = patHtml + `<div class="equip-loadout-list">${list.map((it) => `
      <div class="equip-row-card ${it.is_borrow ? "borrow" : ""}">
        ${equipIcoHtml(it.icon, it.name)}
        <div class="equip-row-info">
          <div class="equip-row-title">
            <span>${esc(it.name)}</span>
            <span class="badge">${esc(it.preferred_training)}</span>
            ${it.is_borrow ? '<span class="badge badge-gold">借满破</span>' : `<span class="badge badge-blue">${it.awake_level}觉</span>`}
            <span class="badge ${it.skill_ok ? "badge-green" : "badge-red"}">${it.skill_ok ? "金技生效" : "金技空转"}</span>
          </div>
          <div class="equip-row-sub">${esc(it.skill_name || "")} · ${esc(it.train_match || "")} · ${esc(it.community_tier || it.stat_tier)}</div>
        </div>
      </div>
    `).join("")}</div>`;
  }

  function renderBoxSubTab() {
    const box = state.box;
    if (!box) return;
    const host = $("box-sub-body");
    const tab = state.boxSubTab;
    const kw = state.boxSearch.trim().toLowerCase();

    if (tab === "gap") {
      const owned = box.characters.filter((c) => c.owned && (!kw || `${c.short_name} ${c.show_name} ${c.pos}`.toLowerCase().includes(kw)));
      host.innerHTML = POS_ORDER.map((pos) => {
        const items = owned.filter((c) => c.pos === pos).sort((a, b) => b.best_rank - a.best_rank);
        if (!items.length) return "";
        return `
          <div class="gap-pos-group">
            <div class="gap-pos-label">${POS_LABEL[pos]}（${items.filter((x) => x.has_rainbow_note).length}/${items.length} 有 7 阶彩笔记）</div>
            <div class="gap-grid">
              ${items.map((c) => `
                <div class="gap-card ${c.has_rainbow_note ? "has-rainbow" : c.own_note_count ? "" : "no-note"}" data-cid="${c.id}">
                  ${avatarHtml(c.head, c.short_name)}
                  <div style="min-width:0;flex:1">
                    <div style="font-weight:600;font-size:13px">${esc(c.short_name)}</div>
                    <div style="font-size:11px;color:var(--text-muted)">
                      ${c.has_rainbow_note ? `<span style="color:var(--purple);font-weight:600">7阶彩 ${c.best_own_note_score}</span>` :
                        c.best_own_note_rank ? `${c.best_own_note_rank}阶 ${c.best_own_note_score}` : "无自备笔记"}
                    </div>
                    <div style="font-size:11px;color:var(--text-dim)">最高分 ${c.best_rank || "—"}</div>
                  </div>
                </div>
              `).join("")}
            </div>
          </div>
        `;
      }).join("");

      host.querySelectorAll(".gap-card[data-cid]").forEach((el) => {
        el.addEventListener("click", () => {
          state.plannerCharId = Number(el.dataset.cid);
          $("planner-char-select").value = String(state.plannerCharId);
          loadPlanner();
          window.scrollTo({ top: 0, behavior: "smooth" });
        });
      });
      return;
    }

    if (tab === "own_notes" || tab === "bor_notes") {
      const src = tab === "own_notes" ? box.own_notes : box.borrowed_notes;
      const filtered = src.filter((n) => !kw || `${n.actor_name} ${n.friend} ${n.highlights} ${n.rank}阶 ${n.score}`.toLowerCase().includes(kw));
      host.innerHTML = `
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>球员</th><th>阶数</th><th>总分</th>${tab === "bor_notes" ? "<th>好友</th>" : ""}<th>词条亮点</th><th>最近同步</th></tr>
            </thead>
            <tbody>
              ${filtered.slice(0, 60).map((n) => `
                <tr>
                  <td style="display:flex;align-items:center;gap:8px">${avatarHtml(n.head, n.actor_name, "avatar-sm")}<strong>${esc(n.actor_name)}</strong> <span style="color:var(--text-dim)">${esc(n.pos)}</span></td>
                  <td><span class="badge ${n.is_rainbow ? "badge-purple" : ""}">${n.rank}阶</span></td>
                  <td style="font-weight:700">${n.score}</td>
                  ${tab === "bor_notes" ? `<td><span class="badge badge-gold">${esc(n.friend || "好友")}</span></td>` : ""}
                  <td style="color:var(--text-muted);font-size:12px">${esc(n.highlights || "—")}</td>
                  <td style="color:var(--text-dim);font-size:11px">${esc(n.last_seen || "—")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
      return;
    }

    if (tab === "equips") {
      const allEq = [...(box.owned_equips || []), ...(box.borrowed_equips || [])].filter(
        (e) => !kw || `${e.name} ${e.preferred_training} ${e.skill_name} ${e.community_tier}`.toLowerCase().includes(kw)
      );
      host.innerHTML = `
        <div class="equip-loadout-list">
          ${allEq.map((e) => `
            <div class="equip-row-card ${e.is_borrow ? "borrow" : ""}">
              ${equipIcoHtml(e.icon, e.name)}
              <div class="equip-row-info">
                <div class="equip-row-title">
                  <span>${esc(e.name)}</span>
                  <span class="badge">${esc(e.preferred_training)}</span>
                  <span class="badge ${e.is_borrow ? "badge-gold" : "badge-blue"}">${e.is_borrow ? "常用借卡" : "自有"} · Lv.${e.level} · ${e.awake_level}觉</span>
                  <span class="badge">${esc(e.community_tier || e.stat_tier)}</span>
                </div>
                <div class="equip-row-sub">${esc(e.skill_name)}：${esc(e.skill_summary)}</div>
              </div>
            </div>
          `).join("")}
        </div>
      `;
    }
  }

  /* ==================== 视图 4：后台挂机实时监控 ==================== */
  function startLiveLoop() {
    stopLiveLoop();
    if (state.livePollMs > 0 && state.online) {
      state.liveTimer = setInterval(pollLiveNow, state.livePollMs);
    }
  }

  function stopLiveLoop() {
    if (state.liveTimer) {
      clearInterval(state.liveTimer);
      state.liveTimer = null;
    }
  }

  async function pollLiveNow() {
    if (!state.online) {
      $("live-status-label").textContent = "离线静态模式无法读取实时日志";
      $("live-status-meta").textContent = "在终端运行 python3 src/dashboard_server.py 后访问 http://127.0.0.1:8765 即可启用";
      $("live-main-body").innerHTML = '<div class="panel empty-state">请通过本地服务打开作战室以启用后台挂机监控</div>';
      return;
    }
    try {
      const data = await apiGet("/api/live");
      state.liveLast = data;
      renderLive(data);
    } catch (err) {
      $("live-status-label").textContent = "读取实时状态失败";
      $("live-status-meta").textContent = err.message;
    }
  }

  function renderLive(data) {
    const dot = $("live-status-dot");
    const navDot = $("nav-live-dot");
    const isRun = data.status === "running_edu";
    const isIdle = data.status === "lobby" || data.status === "settlement";

    dot.className = "status-dot " + (isRun ? "running" : isIdle ? "lobby" : "");
    navDot.className = "live-indicator " + (isRun ? "online" : isIdle ? "idle" : "");

    $("live-status-label").textContent = data.status_label || "未知状态";
    $("live-status-meta").textContent = data.log_file
      ? `监听日志：${data.log_file} · 距上次写入 ${data.idle_sec}s · 更新于 ${data.timestamp}`
      : (data.error || "暂无日志");

    const pr = data.processes || {};
    $("live-proc-badges").innerHTML = `
      <span class="badge ${pr.game_running ? "badge-green" : ""}">游戏 ${pr.game_running ? "运行中" : "未开"}</span>
      <span class="badge ${pr.driver_running ? "badge-blue" : ""}">托管 ${pr.driver_running ? "运行中" : "待机"}</span>
      <span class="badge ${pr.hud_running ? "badge-purple" : ""}">HUD ${pr.hud_running ? "开" : "关"}</span>
    `;

    const host = $("live-main-body");
    if (!data.ok || !data.edu) {
      host.innerHTML = `<div class="panel empty-state">${esc(data.error || "暂未捕获到育成状态")}</div>`;
      return;
    }

    const e = data.edu;
    const psPct = e.ps_max > 0 ? Math.min(100, Math.round((e.ps / e.ps_max) * 100)) : 0;
    const psCls = e.ps <= 35 ? "low" : e.ps <= 65 ? "warn" : "";

    const statsHtml = (e.stats || []).map((st) => `
      <div class="stat-row">
        <div class="stat-row-top">
          <span class="stat-label">${esc(st.name)} <span class="badge">Lv.${st.level}</span></span>
          <span class="stat-num"><strong>${st.val}</strong> <span style="color:var(--text-dim)">/ ${st.cap}</span></span>
        </div>
        <div class="stat-track"><div class="stat-fill ${st.pct >= 96 ? "capped" : ""}" style="width:${st.pct}%"></div></div>
      </div>
    `).join("");

    const equipsHtml = (e.equips || []).length ? `
      <div class="equip-chip-row">
        ${e.equips.map((eq) => `
          <div class="equip-chip">
            ${equipIcoHtml(eq.icon, eq.name)}
            <span>${esc(eq.name)}</span>
            <span class="badge ${eq.is_max ? "badge-green" : eq.trammel >= 70 ? "badge-gold" : ""}">${eq.trammel}/100</span>
          </div>
        `).join("")}
      </div>
    ` : '<div class="panel-sub">暂未读取到本局器材熟度</div>';

    const trainTopHtml = (e.train_top || []).length ? `
      <div class="table-wrap" style="margin-top:10px">
        <table class="data-table">
          <thead><tr><th>推荐顺位</th><th>训练项</th><th>EV</th><th>耗体</th><th>失败率</th></tr></thead>
          <tbody>
            ${e.train_top.map((t, i) => `
              <tr>
                <td><span class="badge ${i === 0 ? "badge-green" : ""}">#${i + 1}</span></td>
                <td><strong>${esc(t.name)}</strong></td>
                <td style="font-weight:700;color:${i === 0 ? "var(--green)" : "inherit"}">${t.ev}</td>
                <td>${t.ps_cost}</td>
                <td>${t.fail_rate}%</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    ` : "";

    host.innerHTML = `
      <div class="live-grid">
        <section class="panel">
          <div class="run-hero">
            <div class="run-hero-left">
              ${avatarHtml(e.main_head, e.main_name, "avatar-lg")}
              <div>
                <div style="font-size:17px;font-weight:700">${esc(e.main_name)} <span class="badge badge-blue">${esc(e.main_pos || "?")}</span></div>
                <div class="panel-sub">第 ${e.week_no || "—"} 周 · ${esc(e.day_label)} · 回合 ${e.exp_round}/49 ${e.is_beach ? "· 🏖️海滩集训" : ""}</div>
              </div>
            </div>
            <div style="text-align:right">
              <div class="run-hero-score num" style="font-size:26px">${e.rank ? e.rank.toLocaleString() : "—"}</div>
              <div class="panel-sub">当前预估分</div>
            </div>
          </div>

          <div style="margin-top:14px">
            <div style="display:flex;justify-content:space-between;font-size:13px">
              <span>当前体力</span>
              <strong class="num">${e.ps} / ${e.ps_max}</strong>
            </div>
            <div class="stamina-bar"><div class="stamina-fill ${psCls}" style="width:${psPct}%"></div></div>
          </div>

          <div class="metric-strip">
            <div class="metric-box">
              <div class="metric-box-label">金币</div>
              <div class="metric-box-val num" style="color:var(--gold)">${e.gold}</div>
            </div>
            <div class="metric-box">
              <div class="metric-box-label">经理能量 / 爆发</div>
              <div class="metric-box-val num">${e.mgr_energy}% · ${e.mgr_explosive}次</div>
            </div>
            <div class="metric-box">
              <div class="metric-box-label">剩余回合</div>
              <div class="metric-box-val num">${e.left_round}</div>
            </div>
          </div>

          <div style="margin-top:16px">
            <h3 class="sub-title" style="margin-bottom:10px">实时六维与设施等级</h3>
            <div class="stats-grid">${statsHtml}</div>
          </div>
        </section>

        <div style="display:flex;flex-direction:column;gap:14px">
          <section class="panel">
            <h3 class="panel-title" style="margin-bottom:10px">军师此刻判断</h3>
            <div class="advice-callout">
              ${esc(e.next_action || "等待下一回合状态更新…")}
            </div>
            ${e.active_views && e.active_views.length ? `<div class="panel-sub" style="margin-top:8px">当前界面：${esc(e.active_views.join(" / "))}</div>` : ""}
            ${trainTopHtml}
          </section>

          <section class="panel">
            <h3 class="panel-title" style="margin-bottom:4px">器材熟度进度</h3>
            ${equipsHtml}
          </section>
        </div>
      </div>

      <section class="panel" style="margin-top:14px">
        <div class="panel-head" style="margin-bottom:8px">
          <h3 class="panel-title">最新关键日志流</h3>
          <span class="panel-sub">最近 ${(data.recent_lines || []).length} 条</span>
        </div>
        <div class="log-console">${esc((data.recent_lines || []).join("\n") || "暂无新事件")}</div>
      </section>
    `;
  }

  /* ==================== 初始化与事件绑定 ==================== */
  async function loadCoreData() {
    try {
      const [ov, box] = await Promise.all([apiGet("/api/overview"), apiGet("/api/box")]);
      state.online = true;
      state.overview = ov;
      state.box = box;
      setConnBadge(true, "本地服务在线");
    } catch (_) {
      state.online = false;
      const snap = window.LQSN_DASHBOARD_SNAPSHOT;
      if (snap) {
        state.overview = snap.overview;
        state.box = snap.box;
        setConnBadge(false, "离线快照模式");
      } else {
        setConnBadge(false, "未连接数据");
        return;
      }
    }

    renderOverview();
    renderRunsList();
    initBoxControls();

    // 在线时静默探测一次 live 状态以点亮顶部指示灯
    if (state.online) {
      apiGet("/api/live").then((d) => {
        const navDot = $("nav-live-dot");
        if (navDot) {
          navDot.className = "live-indicator " + (d.status === "running_edu" ? "online" : (d.status === "lobby" || d.status === "settlement") ? "idle" : "");
        }
      }).catch(() => {});
    }
  }

  function bindEvents() {
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });

    $("btn-theme").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("lqsn_dash_theme_v1", next); } catch (_) {}
      $("btn-theme").textContent = next === "dark" ? "☀" : "🌙";
    });

    $("btn-refresh-all").addEventListener("click", async () => {
      if (state.online) {
        try { await fetch("/api/refresh", { method: "POST" }); } catch (_) {}
      }
      state.runDetailCache = {};
      state.plannerData = null;
      await loadCoreData();
      if (state.view === "box") loadPlanner();
      if (state.view === "live") pollLiveNow();
    });

    // 趋势图控制
    $("trend-char-filter").addEventListener("change", (e) => {
      state.trendChar = e.target.value;
      renderTrendChart();
    });
    $("trend-range-seg").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("trend-range-seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.trendLimit = Number(b.dataset.limit || 0);
        renderTrendChart();
      });
    });

    // 单局筛选
    $("run-search").addEventListener("input", (e) => {
      state.runSearch = e.target.value;
      renderRunsList();
    });
    $("run-char-filter").addEventListener("change", (e) => {
      state.runChar = e.target.value;
      renderRunsList();
    });
    $("run-only-10k").addEventListener("change", (e) => {
      state.runOnly10k = e.target.checked;
      renderRunsList();
    });
    $("btn-clear-compare").addEventListener("click", () => {
      state.compareIds = [];
      renderRunsList();
      if (state.selectedRunId) selectRun(state.selectedRunId);
    });

    // 配对器控制
    $("planner-char-select").addEventListener("change", (e) => {
      state.plannerCharId = Number(e.target.value);
      loadPlanner();
    });
    $("planner-focus-select").addEventListener("change", (e) => {
      state.plannerFocus = e.target.value;
      loadPlanner();
    });
    $("planner-mode-seg").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("planner-mode-seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.plannerMode = b.dataset.mode;
        loadPlanner();
      });
    });
    $("planner-note-tab").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("planner-note-tab").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.plannerNoteTab = b.dataset.ntab;
        renderPlannerNotes();
      });
    });
    $("planner-equip-tab").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("planner-equip-tab").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.plannerEquipTab = b.dataset.etab;
        renderPlannerEquips();
      });
    });

    // Box 下半区控制
    $("box-sub-seg").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("box-sub-seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.boxSubTab = b.dataset.btab;
        renderBoxSubTab();
      });
    });
    $("box-search").addEventListener("input", (e) => {
      state.boxSearch = e.target.value;
      renderBoxSubTab();
    });

    // Live 控制
    $("live-poll-seg").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        $("live-poll-seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.livePollMs = Number(b.dataset.ms || 0);
        startLiveLoop();
      });
    });
    $("btn-live-now").addEventListener("click", pollLiveNow);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const curTheme = document.documentElement.getAttribute("data-theme") || "dark";
    $("btn-theme").textContent = curTheme === "dark" ? "☀" : "🌙";
    bindEvents();
    loadCoreData();
  });
})();
