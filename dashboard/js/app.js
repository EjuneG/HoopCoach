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
    runSortByRank: false,
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
    compat: null,
    compatCoreId: 1002,
    compatSlotA: null,
    compatSlotB: null,
    compatSubMode: "forward",
    compatFwdSearch: "",
    compatFwdPos: "ALL",
    compatFwdOnlyOwned: false,
    compatFwdOnlyHasNote: false,
    compatRevType: "note",
    compatRevNoteId: "",
    compatRevActorId: 1007,
    compatRevStar: 0,
    compatRevPos: "ALL",
    compatRevOnlyOwned: false,
    compatRevData: null,
    compatRevLoading: false,
    picker: {
      open: false,
      target: "core",
      source: "chars",
      search: "",
      pos: "ALL",
      onlyOwned: false,
      sortByRel: true,
      allowNotes: false,
      coreForRel: null,
      onPick: null,
    },
    livePollMs: 2500,
    liveTimer: null,
    liveLast: null,
    plans: [],
    selectedPlanId: null,
    refreshing: false,
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
    if (viewName === "compat") {
      ensureCompatReady();
    }
    if (viewName === "plans") {
      renderPlansView();
    }
    if (viewName === "live") {
      pollLiveNow();
      startLiveLoop();
    } else {
      stopLiveLoop();
    }
  }

  /* ==================== 视图 1：总览 ==================== */
  function computeRunRecords(ov) {
    const runs = (ov && ov.runs) || [];
    let bestRun = null;
    const posBestIds = new Set();
    const charBestMap = {};
    for (const r of runs) {
      if (!r || !(r.final_rank > 0)) continue;
      if (!bestRun || r.final_rank > bestRun.final_rank) bestRun = r;
      const key = r.main_char_id || r.main_name;
      if (key && (!charBestMap[key] || r.final_rank > charBestMap[key].final_rank)) {
        charBestMap[key] = r;
      }
    }
    for (const p of (ov && ov.positions) || []) {
      if (p && p.best_rank > 0 && p.best_run_id) posBestIds.add(p.best_run_id);
    }
    const charBestIds = new Set(Object.values(charBestMap).map((r) => r.run_id));
    return {
      globalBestId: bestRun ? bestRun.run_id : "",
      globalBestRun: bestRun,
      posBestIds,
      charBestIds,
    };
  }

  function renderOverview() {
    const ov = state.overview;
    if (!ov) return;
    const k = ov.kpi;
    const rec = computeRunRecords(ov);

    // 1. 4 张 KPI 卡片
    const delta10 = k.avg_rank_prev10 ? (k.avg_rank_recent10 - k.avg_rank_prev10).toFixed(0) : null;
    const deltaHtml = delta10 !== null
      ? `<span class="badge ${Number(delta10) >= 0 ? "badge-green" : "badge-red"}">${Number(delta10) >= 0 ? "+" : ""}${delta10} vs 前10局</span>`
      : "";

    const bestChar = k.best_char || (rec.globalBestRun ? rec.globalBestRun.main_name : "");
    const bestPos = k.best_pos || (rec.globalBestRun ? rec.globalBestRun.main_pos : "");
    const bestHolderBadge = bestChar
      ? `<span class="badge badge-champ">★ ${esc(bestChar)}${bestPos ? " · " + esc(bestPos) : ""}</span>`
      : "";

    const positions = ov.positions || [];
    const coveredPos = positions.filter((p) => p.best_rank > 0);
    const avgPosBest = k.pos_avg_best_rank || (coveredPos.length
      ? Math.round(coveredPos.reduce((s, p) => s + p.best_rank, 0) / coveredPos.length)
      : 0);
    const avgPosAuto = k.pos_avg_auto_est || (coveredPos.length
      ? Math.round(coveredPos.reduce((s, p) => s + (p.auto_est || Math.round(p.best_rank * 0.97)), 0) / coveredPos.length)
      : 0);
    const weakestPos = coveredPos.length
      ? coveredPos.reduce((m, p) => (p.best_rank < m.best_rank ? p : m), coveredPos[0])
      : null;
    const posKpiBadge = coveredPos.length === 5 && weakestPos
      ? `<span class="badge">短板 ${esc(weakestPos.pos)} ${weakestPos.best_rank.toLocaleString()}</span>`
      : `<span class="badge">已覆盖 ${coveredPos.length} / 5 位</span>`;

    $("overview-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">历史最高分</div>
        <div class="kpi-val kpi-val-gold">${k.best_rank ? k.best_rank.toLocaleString() : "—"}</div>
        <div class="kpi-foot"><span>累计归档 ${k.total_runs} 局</span>${bestHolderBadge}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">近 10 局均分</div>
        <div class="kpi-val">${k.avg_rank_recent10 ? Math.round(k.avg_rank_recent10).toLocaleString() : "—"}</div>
        <div class="kpi-foot"><span>全历史均分 ${k.avg_rank_all ? Math.round(k.avg_rank_all).toLocaleString() : "—"}</span>${deltaHtml}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">5 位置底座均分</div>
        <div class="kpi-val">${avgPosBest ? avgPosBest.toLocaleString() : "—"}</div>
        <div class="kpi-foot"><span>托管均估 ~${avgPosAuto ? avgPosAuto.toLocaleString() : "—"}</span>${posKpiBadge}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">自备 7 阶彩笔记</div>
        <div class="kpi-val kpi-val-purple">${k.rainbow_notes_count} <span style="font-size:14px;font-weight:600;font-style:normal;color:var(--text-muted)">(${k.rainbow_chars_count}人)</span></div>
        <div class="kpi-foot"><span>总笔记 ${k.own_notes_total} 自备 · ${k.borrowed_notes_total} 好友</span>${k.diamonds ? `<span class="badge badge-blue">◆ ${esc(k.diamonds)}</span>` : ""}</div>
      </div>
    `;

    // 2. 5 位置最高评分底座
    const posSummaryEl = $("pos-base-summary") || $("pos-10k-summary");
    if (posSummaryEl) {
      posSummaryEl.textContent = !coveredPos.length
        ? "暂无位置记录"
        : coveredPos.length === 5
        ? `5 位置均分 ${avgPosBest.toLocaleString()}`
        : `已覆盖 ${coveredPos.length} / 5 位 · 均分 ${avgPosBest.toLocaleString()}`;
    }
    const maxPosRank = Math.max(0, ...positions.map((p) => p.best_rank || 0));
    $("overview-positions").innerHTML = positions.map((p) => {
      const hasRank = p.best_rank > 0;
      const pct = maxPosRank > 0 && hasRank ? Math.min(100, Math.round((p.best_rank / maxPosRank) * 100)) : 0;
      const isBest = maxPosRank > 0 && p.best_rank === maxPosRank;
      const cardCls = !hasRank ? "is-empty" : isBest ? "is-best" : "";
      return `
        <div class="pos-card ${cardCls}" data-run-id="${esc(p.best_run_id)}">
          <div class="pos-top">
            <span class="pos-code">${esc(p.pos)} · ${esc(p.pos_cn)}</span>
          </div>
          <div class="pos-who">
            ${avatarHtml(p.best_head, p.best_char)}
            <div>
              <div class="pos-score num">${hasRank ? p.best_rank.toLocaleString() : "—"}</div>
              <div class="pos-name">${esc(p.best_char || "暂无记录")}</div>
            </div>
          </div>
          <div class="pos-bar"><div class="pos-bar-fill" style="width:${pct}%"></div></div>
          <div class="pos-foot">
            <span>托管估 ~${p.auto_est ? p.auto_est.toLocaleString() : "—"}</span>
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
      .map((w) => `<div class="warn-banner">▲ ${esc(w)}</div>`)
      .join("");

    const bList = (ov.bottlenecks || []).slice(0, 6);
    $("overview-bottlenecks").innerHTML = bList.length ? bList.map((b) => {
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
          <div class="pos-bar"><div class="pos-bar-fill" style="width:${pct}%;background:${b.rank_gap === 0 ? "var(--mint)" : "var(--accent)"}"></div></div>
          <div class="goal-note">${esc(b.primary_bottleneck || b.advice || "")}</div>
        </div>
      `;
    }).join("") : '<div class="empty-state">暂未配置养成队列或同步历史战绩。可先在桌面控制台点击「扫描历史战绩」，或切换到「笔记与配装」查看全角色通用方案。</div>';

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

  function getThemeChartPalette() {
    const cs = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (cs.getPropertyValue(name) || "").trim() || fallback;
    const isDark = (document.documentElement.getAttribute("data-theme") || "light") === "dark";
    return {
      isDark,
      grid: pick("--chart-grid", isDark ? "rgba(148,184,255,0.14)" : "rgba(16,27,51,0.11)"),
      axis: pick("--chart-axis", isDark ? "#98acd0" : "#526485"),
      line: pick("--chart-line", isDark ? "#4d8dff" : "#1b5bff"),
      gradTop: pick("--chart-grad-top", isDark ? "rgba(77,141,255,0.30)" : "rgba(27,92,255,0.22)"),
      gradBot: pick("--chart-grad-bot", isDark ? "rgba(77,141,255,0.02)" : "rgba(27,92,255,0.02)"),
      dotStroke: pick("--chart-dot-stroke", isDark ? "#131f38" : "#ffffff"),
      refLine: pick("--chart-ref-line", isDark ? "#ffb703" : "#e68a00"),
      refLabel: pick("--chart-ref-label", isDark ? "#ffc83d" : "#b86700"),
      refPillBg: pick("--chart-ref-pill-bg", isDark ? "rgba(255,183,3,0.18)" : "#fff4d1"),
      peakDot: pick("--chart-peak-dot", isDark ? "#ffb703" : "#ff8a00"),
      peakDotStroke: pick("--chart-peak-dot-stroke", isDark ? "#131f38" : "#101b33"),
    };
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

    const pal = getThemeChartPalette();
    const measuredW = host.clientWidth ? Math.round(host.clientWidth) : 1120;
    const W = Math.max(680, Math.min(1320, measuredW));
    const H = 252;
    const padL = 54;
    const padR = 24;
    const padT = 20;
    const padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const scores = runs.map((r) => r.final_rank);
    const minScore = Math.min(...scores);
    const maxScore = Math.max(...scores);
    const avgScore = Math.round(scores.reduce((a, b) => a + b, 0) / scores.length);
    let minY = Math.max(0, Math.floor((minScore - 200) / 500) * 500);
    let maxY = Math.ceil((maxScore + 200) / 500) * 500;
    if (maxY - minY < 500) {
      maxY = minY + 500;
    }
    const spanY = Math.max(500, maxY - minY);

    const xOf = (i) => padL + (runs.length === 1 ? plotW / 2 : (i / (runs.length - 1)) * plotW);
    const yOf = (v) => padT + plotH - ((v - minY) / spanY) * plotH;

    // 水平网格线
    const ticks = [];
    const step = spanY > 3500 ? 1000 : 500;
    for (let v = minY; v <= maxY; v += step) {
      ticks.push(v);
    }
    const gridSvg = ticks.map((v) => {
      const y = yOf(v);
      const yStr = y.toFixed(1);
      return `
        <line x1="${padL}" y1="${yStr}" x2="${W - padR}" y2="${yStr}"
              stroke="${pal.grid}" stroke-width="1" />
        <text x="${padL - 9}" y="${(y + 4).toFixed(1)}" text-anchor="end"
              fill="${pal.axis}" font-size="11" font-weight="600">${v.toLocaleString()}</text>
      `;
    }).join("");

    // 动态均分参考线（标签自动避让高分侧）
    const qLen = Math.max(1, Math.ceil(runs.length * 0.28));
    const leftAvg = scores.slice(0, qLen).reduce((a, b) => a + b, 0) / qLen;
    const rightAvg = scores.slice(-qLen).reduce((a, b) => a + b, 0) / qLen;
    const pillOnLeft = leftAvg <= rightAvg;
    const avgLabel = `均分 ${avgScore.toLocaleString()}`;
    const pillW = Math.max(84, avgLabel.length * 9 + 18);
    const pillX = pillOnLeft ? (padL + 10) : (W - padR - pillW - 4);
    const avgY = yOf(avgScore);
    const avgPillY = Math.max(padT + 2, avgY - 21);
    const avgRefSvg = `
      <line x1="${padL}" y1="${avgY.toFixed(1)}" x2="${W - padR}" y2="${avgY.toFixed(1)}"
            stroke="${pal.refLine}" stroke-dasharray="6 4" stroke-width="1.6" />
      <rect x="${pillX}" y="${avgPillY.toFixed(1)}" width="${pillW}" height="18" rx="9"
            fill="${pal.refPillBg}" stroke="${pal.refLine}" stroke-width="1.2" />
      <text x="${pillX + pillW / 2}" y="${(avgPillY + 12.5).toFixed(1)}" text-anchor="middle"
            fill="${pal.refLabel}" font-size="10.5" font-weight="800">${avgLabel}</text>
    `;

    // 折线与渐变填充
    const pts = runs.map((r, i) => `${xOf(i).toFixed(1)},${yOf(r.final_rank).toFixed(1)}`).join(" ");
    const areaPts = `${xOf(0).toFixed(1)},${(padT + plotH).toFixed(1)} ${pts} ${xOf(runs.length - 1).toFixed(1)},${(padT + plotH).toFixed(1)}`;

    // X 轴稀疏日期标签
    const labelCount = Math.min(7, runs.length);
    const xLabels = [];
    for (let k = 0; k < labelCount; k++) {
      const idx = labelCount === 1 ? 0 : Math.round((k / (labelCount - 1)) * (runs.length - 1));
      xLabels.push(`<text x="${xOf(idx).toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="${pal.axis}" font-size="11" font-weight="600">${esc(runs[idx].date_label.slice(0, 5))}</text>`);
    }

    // 识别本视图最高分与阶段刷新新高（PB）节点
    const pbFlags = [];
    let runningBest = -1;
    for (let i = 0; i < runs.length; i++) {
      const sc = runs[i].final_rank;
      const isPb = i > 0 && sc > runningBest;
      if (sc > runningBest) runningBest = sc;
      pbFlags.push(isPb);
    }

    // 节点圆点
    const baseRad = runs.length > 60 ? 3.6 : 4.5;
    const dots = runs.map((r, i) => {
      const cx = xOf(i).toFixed(1);
      const cy = yOf(r.final_rank).toFixed(1);
      const isPeak = r.final_rank === maxScore;
      const isPb = pbFlags[i];
      const highlight = isPeak || isPb;
      const col = highlight ? pal.peakDot : pal.line;
      const stroke = highlight ? pal.peakDotStroke : pal.dotStroke;
      const rad = isPeak ? baseRad + 1.6 : isPb ? baseRad + 0.8 : baseRad;
      return `<circle class="chart-dot" data-idx="${i}" cx="${cx}" cy="${cy}" r="${rad}" fill="${col}" stroke="${stroke}" stroke-width="${highlight ? 1.8 : 1.6}" style="cursor:pointer" />`;
    }).join("");

    const gradId = pal.isDark ? "trendGradDark" : "trendGradLight";
    host.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${pal.gradTop}" />
            <stop offset="100%" stop-color="${pal.gradBot}" />
          </linearGradient>
        </defs>
        ${gridSvg}
        ${avgRefSvg}
        <polygon points="${areaPts}" fill="url(#${gradId})" />
        <polyline points="${pts}" fill="none" stroke="${pal.line}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" />
        ${xLabels.join("")}
        ${dots}
      </svg>
    `;

    const tip = $("trend-tooltip");
    const wrap = $("trend-chart-wrap");
    host.querySelectorAll(".chart-dot").forEach((dot) => {
      dot.addEventListener("mouseenter", (e) => {
        const idx = Number(dot.dataset.idx);
        const r = runs[idx];
        const isPeak = r.final_rank === maxScore;
        const isPb = pbFlags[idx];
        const tag = isPeak ? " · ★ 最高" : isPb ? " · ▲ 新高" : "";
        tip.innerHTML = `
          <div style="font-weight:800;margin-bottom:3px">${esc(r.main_name)} · <span style="color:${(isPeak || isPb) ? "var(--gold-text)" : "var(--accent)"}">${r.final_rank.toLocaleString()}</span><span style="color:var(--gold-text);font-size:11px">${tag}</span></div>
          <div style="color:var(--text-muted)">${esc(r.date_label)} · #${r.seq_no}</div>
          <div style="color:var(--text-muted);margin-top:2px">吻合 ${r.coach_match_pct.toFixed(0)}% · 均亏 ${r.avg_turn_ev_loss.toFixed(1)} EV</div>
        `;
        tip.classList.remove("hidden");
        const rect = wrap.getBoundingClientRect();
        const left = Math.min(rect.width - 185, Math.max(8, e.clientX - rect.left + 12));
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
    const rec = computeRunRecords(ov);
    const kw = state.runSearch.trim().toLowerCase();
    const list = ov.runs
      .slice()
      .reverse()
      .filter((r) => {
        if (state.runChar && r.main_name !== state.runChar) return false;
        if (kw) {
          const hay = `${r.main_name} ${r.lineup} ${r.date_label} ${r.final_rank}`.toLowerCase();
          if (!hay.includes(kw)) return false;
        }
        return true;
      });
    if (state.runSortByRank) {
      list.sort((a, b) => (b.final_rank - a.final_rank) || (b.seq_no - a.seq_no));
    }

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
      const isRecord = rec.posBestIds.has(r.run_id) || rec.charBestIds.has(r.run_id);
      return `
        <div class="run-item ${isActive ? "active" : ""}" data-rid="${esc(r.run_id)}">
          <input type="checkbox" class="cmp-chk" data-rid="${esc(r.run_id)}" ${isChecked ? "checked" : ""} title="勾选加入双局对比" />
          ${avatarHtml(r.main_head, r.main_name, "avatar-sm")}
          <div class="run-item-info">
            <div class="run-item-top">
              <span class="run-item-name">${esc(r.main_name)} <span style="font-weight:400;color:var(--text-dim)">${esc(r.main_pos)}</span></span>
              <span class="run-item-score num ${isRecord ? "is-record" : ""}">${r.final_rank.toLocaleString()}</span>
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
      ? '<p class="panel-sub">离线快照仅包含摘要；启动本地服务后可查看完整逐回合失误分析。</p>'
      : subTurns.length === 0
      ? '<div class="badge badge-green" style="padding:6px 12px">★ 本局未出现显著 EV 失误回合，整体贴合军师推荐</div>'
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
    const rec = computeRunRecords(state.overview);
    let recordBadge = "";
    if (d.run_id && d.run_id === rec.globalBestId) {
      recordBadge = '<span class="badge badge-champ">★ 全号最高</span>';
    } else if (d.run_id && rec.posBestIds.has(d.run_id)) {
      recordBadge = `<span class="badge badge-champ">★ ${esc(d.main_pos || "同位")} 最高</span>`;
    } else if (d.run_id && rec.charBestIds.has(d.run_id)) {
      recordBadge = '<span class="badge badge-gold">★ 角色最高</span>';
    }
    const isRecordRun = Boolean(recordBadge);

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
            <div style="display:flex;align-items:center;justify-content:flex-end;gap:8px">
              ${recordBadge}
              <div class="run-hero-score num ${isRecordRun ? "is-record" : ""}">${d.final_rank.toLocaleString()}</div>
            </div>
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
      $("planner-notes-body").innerHTML = '<div class="empty-state">离线快照模式下不支持实时配对运算，请先启动本地作战室服务</div>';
      $("planner-equips-body").innerHTML = "";
      return;
    }
    $("planner-notes-body").innerHTML = '<div class="empty-state">正在计算最优笔记配对方案…</div>';
    $("planner-equips-body").innerHTML = '<div class="empty-state">正在计算最优器材配装方案…</div>';
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
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div class="panel-sub">${esc(p.equipments.owned_pvp_note || p.equipments.wish_headline || "")}</div>
        <button id="btn-jump-compat-core" class="action-btn action-btn-sm" type="button">▸ 查相性沙盒</button>
      </div>
    `;
    const jumpBtn = $("btn-jump-compat-core");
    if (jumpBtn) {
      jumpBtn.addEventListener("click", () => {
        openCompatForCore(c.id, true);
      });
    }
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
      const fallbackAll = ab.same_pos_all || [];
      const rows = owned.length ? owned : fallbackAll;
      const isAllFallback = !owned.length && fallbackAll.length > 0;
      host.innerHTML = rows.length ? `
        ${isAllFallback ? '<div class="panel-sub" style="margin-bottom:8px">◆ 暂未同步个人角色池，以下展示全图鉴原生同位置高相性种马推荐</div>' : ""}
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>${isAllFallback ? "同位置种马（全图鉴）" : "同位置种马（已拥有）"}</th><th>双边关系分</th><th>现有自备最高</th><th>主练</th></tr></thead>
            <tbody>
              ${rows.slice(0, 8).map((x) => `
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

    let combos = tab === "own" ? (p.notes.pure_own_combos || []) : (p.notes.borrow_combos || []);
    let isGenericFallback = false;
    if (!combos.length && (p.notes.generic_combos || []).length) {
      combos = p.notes.generic_combos;
      isGenericFallback = true;
    }
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
            ${n.rank ? `<span class="badge ${n.rank >= 7 ? "badge-purple" : ""}">${n.rank}阶 ${n.score}</span>` : '<span class="badge">通用搭档</span>'}
            ${n.friend ? `<span class="badge badge-gold">借:${esc(n.friend)}</span>` : `<span class="badge">${tag}</span>`}
          </div>
          <div class="note-mini-sub">相性 ${n.rel}${n.harmony_bonus ? `+${n.harmony_bonus}` : ""}${n.highlights ? ` · ${esc(n.highlights)}` : (isGenericFallback ? " · 基础关系分（带对应专属契合更高）" : " · 无高亮词条")}</div>
        </div>
      </div>
    `;

    const bannerHtml = isGenericFallback
      ? '<div class="panel-sub" style="margin-bottom:8px">◆ 暂未同步个人笔记池，以下基于全角色关系矩阵给出通用最高契合双搭档参考（优先挑选带对应主属性上限词条的高分笔记）</div>'
      : "";
    host.innerHTML = bannerHtml + `<div class="combo-list">${combos.slice(0, 5).map((cb, idx) => `
      <div class="combo-card">
        <div class="combo-head">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span class="badge ${idx === 0 ? "badge-green" : "badge-blue"}">#${idx + 1} 契合 ${cb.total_compat} (${esc(cb.rank_name)})</span>
            <span class="badge">${cb.learn_times}次学 · 期望 ${cb.expected_triggers} 词条</span>
          </div>
          <span class="panel-sub" style="margin:0">${isGenericFallback ? "全图鉴 0★ 基准相性" : `均分 ${cb.avg_note_score} · 匹配上限 +${cb.matched_cap_gain}`}</span>
        </div>
        <div class="combo-pair">
          ${renderMiniNote(cb.note1, isGenericFallback ? "搭档1" : "自备")}
          ${renderMiniNote(cb.note2, isGenericFallback ? "搭档2" : (tab === "own" ? "自备" : "借用"))}
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
    let ownedEmptyFallback = false;
    if (isOwned && list.length <= 1 && (eq.bis_loadout || []).length) {
      list = eq.bis_loadout;
      pat = eq.bis_pattern || {};
      ownedEmptyFallback = true;
    } else if (!isOwned) {
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
    const fallbackHint = ownedEmptyFallback
      ? '<div class="panel-sub" style="margin-bottom:8px">◆ 暂未同步个人自有器材池，以下自动展示全图鉴通用毕业 5 卡方案作为参考</div>'
      : "";
    const patHtml = pat && pat.tag ? `<div style="margin-bottom:10px"><span class="badge badge-blue">${esc(pat.tag)}</span></div>` : "";
    host.innerHTML = fallbackHint + patHtml + `<div class="equip-loadout-list">${list.map((it) => `
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
      const hasOwnedAny = box.characters.some((c) => c.owned);
      const visibleChars = box.characters.filter(
        (c) => (hasOwnedAny ? c.owned : true) && (!kw || `${c.short_name} ${c.show_name} ${c.pos}`.toLowerCase().includes(kw))
      );
      if (!visibleChars.length) {
        host.innerHTML = '<div class="empty-state">未找到匹配的角色</div>';
        return;
      }
      const headHint = !hasOwnedAny
        ? '<div class="panel-sub" style="margin-bottom:10px">◆ 暂未同步个人角色与笔记池，以下展示全图鉴角色；点击任意角色即可在上方查看通用笔记与毕业器材配装</div>'
        : "";
      host.innerHTML = headHint + POS_ORDER.map((pos) => {
        const items = visibleChars.filter((c) => c.pos === pos).sort((a, b) => b.best_rank - a.best_rank);
        if (!items.length) return "";
        return `
          <div class="gap-pos-group">
            <div class="gap-pos-label">${POS_LABEL[pos]}（${hasOwnedAny ? `${items.filter((x) => x.has_rainbow_note).length}/${items.length} 有 7 阶彩笔记` : `共 ${items.length} 人`}）</div>
            <div class="gap-grid">
              ${items.map((c) => `
                <div class="gap-card ${c.has_rainbow_note ? "has-rainbow" : c.own_note_count ? "" : "no-note"}" data-cid="${c.id}">
                  ${avatarHtml(c.head, c.short_name)}
                  <div style="min-width:0;flex:1">
                    <div style="font-weight:600;font-size:13px">${esc(c.short_name)}</div>
                    <div style="font-size:11px;color:var(--text-muted)">
                      ${c.has_rainbow_note ? `<span style="color:var(--purple);font-weight:600">7阶彩 ${c.best_own_note_score}</span>` :
                        c.best_own_note_rank ? `${c.best_own_note_rank}阶 ${c.best_own_note_score}` : "点击查通用配装"}
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
      if (!filtered.length) {
        host.innerHTML = `<div class="empty-state">${src.length ? "当前搜索无匹配笔记" : "暂未同步个人笔记数据；上方「配装规划器」已自动提供全角色通用高相性双搭档参考"}</div>`;
        return;
      }
      host.innerHTML = `
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr><th>球员</th><th>阶数</th><th>总分</th>${tab === "bor_notes" ? "<th>好友</th>" : ""}<th>词条亮点</th><th>最近同步</th><th>操作</th></tr>
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
                  <td><button class="action-btn action-btn-sm btn-note-rev" type="button" data-nid="${esc(n.note_id)}" data-aid="${n.actor_id}">▸ 反查</button></td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      `;
      host.querySelectorAll(".btn-note-rev").forEach((btn) => {
        btn.addEventListener("click", () => {
          openCompatReverseForNote(btn.dataset.nid, Number(btn.dataset.aid));
        });
      });
      return;
    }

    if (tab === "equips") {
      const allEq = [...(box.owned_equips || []), ...(box.borrowed_equips || [])].filter(
        (e) => !kw || `${e.name} ${e.preferred_training} ${e.skill_name} ${e.community_tier}`.toLowerCase().includes(kw)
      );
      if (!allEq.length) {
        host.innerHTML = '<div class="empty-state">暂未同步个人器材池；可在上方「配装规划器」直接查看任意角色的全图鉴毕业 5 卡与 PVP 阵型方案</div>';
        return;
      }
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
                <div style="font-size:17px;font-weight:800">${esc(e.main_name)} <span class="badge badge-blue">${esc(e.main_pos || "?")}</span></div>
                <div class="panel-sub">第 ${e.week_no || "—"} 周 · ${esc(e.day_label)} · 回合 ${e.exp_round}/49 ${e.is_beach ? "· ★ 海滩集训" : ""}</div>
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
              <div class="metric-box-val num" style="color:var(--gold-text)">${e.gold}</div>
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

  /* ==================== 视图 4：笔记相性查询与极速选人 ==================== */
  const HARMONY_BONUS_DEFAULT = { 0: 0, 1: 2, 2: 5, 3: 10 };

  function getCompatChar(cid) {
    if (!state.compat || !cid) return null;
    return (state.compat.characters || []).find((c) => Number(c.id) === Number(cid)) || null;
  }

  function getAllCompatNotes() {
    if (!state.compat) return [];
    return [...(state.compat.own_notes || []), ...(state.compat.borrowed_notes || [])];
  }

  function findCompatNoteById(noteId) {
    if (!noteId || !state.compat) return null;
    return getAllCompatNotes().find((n) => String(n.note_id) === String(noteId)) || null;
  }

  function getBestNoteForActor(actorId, preferOwn) {
    if (!state.compat) return null;
    const own = (state.compat.own_notes || []).filter((n) => Number(n.actor_id) === Number(actorId));
    const bor = (state.compat.borrowed_notes || []).filter((n) => Number(n.actor_id) === Number(actorId));
    if (preferOwn !== false && own.length) return own[0];
    if (bor.length) return bor[0];
    if (own.length) return own[0];
    return null;
  }

  function getRelScore(coreId, partnerId) {
    if (!state.compat || !coreId || !partnerId || Number(coreId) === Number(partnerId)) return 0;
    const row = (state.compat.rel_scores || {})[String(coreId)];
    if (!row) return 0;
    return Number(row[String(partnerId)] || 0);
  }

  function getHarmonyBonus(star) {
    const map = (state.compat && state.compat.rules && state.compat.rules.harmony_bonus_by_star) || HARMONY_BONUS_DEFAULT;
    return Number(map[String(star)] ?? HARMONY_BONUS_DEFAULT[Number(star)] ?? 0);
  }

  function charSearchHaystack(c) {
    if (!c) return "";
    return [
      c.short_name,
      c.show_name,
      c.en_name,
      ...(c.aliases || []),
      c.pinyin,
      c.pos,
      c.best_pos,
      c.school,
      ...(c.recommend_train || []),
      ...(c.rec_stats_names || []),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function charMatchesQuery(c, kw) {
    const q = (kw || "").trim().toLowerCase();
    if (!q) return true;
    const hay = charSearchHaystack(c);
    return q.split(/\s+/).every((tok) => hay.includes(tok));
  }

  function relBadgeClass(rel) {
    if (rel >= 18) return "rel-s";
    if (rel >= 14) return "rel-a";
    if (rel >= 10) return "rel-b";
    return "rel-c";
  }

  function getSlotContribution(coreId, slot) {
    if (!slot || !slot.actorId) {
      return { rel: 0, star: 0, bonus: 0, total: 0, isSameAsCore: false };
    }
    const aid = Number(slot.actorId);
    const isSame = Number(coreId) === aid;
    const rel = isSame ? 0 : getRelScore(coreId, aid);
    let star = 0;
    if (slot.mode === "note" && slot.note) {
      const ht = slot.note.harmony_targets || {};
      star = Number(ht[String(coreId)] || 0);
    } else {
      star = Number(slot.star || 0);
    }
    const bonus = getHarmonyBonus(star);
    return { rel, star, bonus, total: rel + bonus, isSameAsCore: isSame };
  }

  function evaluateCompatNumbers(totalCompat) {
    const rules = (state.compat && state.compat.rules) || {};
    const levels = rules.harmony_levels || [
      { level: 1, rank_name: "无", min_compatibility_score: 0, inheritance_learn_times: 1 },
      { level: 2, rank_name: "差", min_compatibility_score: 1, inheritance_learn_times: 2 },
      { level: 3, rank_name: "一般", min_compatibility_score: 10, inheritance_learn_times: 3 },
      { level: 4, rank_name: "良", min_compatibility_score: 20, inheritance_learn_times: 4 },
      { level: 5, rank_name: "优", min_compatibility_score: 30, inheritance_learn_times: 5 },
      { level: 6, rank_name: "极", min_compatibility_score: 45, inheritance_learn_times: 5 },
    ];
    const triggers = rules.entry_triggers || [
      { required_compatibility_score: 0, min_triggered_entries: 2, max_triggered_entries: 4 },
      { required_compatibility_score: 5, min_triggered_entries: 3, max_triggered_entries: 5 },
      { required_compatibility_score: 10, min_triggered_entries: 4, max_triggered_entries: 6 },
      { required_compatibility_score: 15, min_triggered_entries: 4, max_triggered_entries: 7 },
      { required_compatibility_score: 20, min_triggered_entries: 4, max_triggered_entries: 8 },
      { required_compatibility_score: 40, min_triggered_entries: 5, max_triggered_entries: 10 },
      { required_compatibility_score: 60, min_triggered_entries: 5, max_triggered_entries: 11 },
    ];

    let curLv = levels[0];
    let nextLv = null;
    for (let i = 0; i < levels.length; i++) {
      if (totalCompat >= Number(levels[i].min_compatibility_score)) {
        curLv = levels[i];
        nextLv = i + 1 < levels.length ? levels[i + 1] : null;
      }
    }
    let curTr = triggers[0];
    for (let i = 0; i < triggers.length; i++) {
      if (totalCompat >= Number(triggers[i].required_compatibility_score)) {
        curTr = triggers[i];
      }
    }
    const learnTimes = Number(curLv.inheritance_learn_times);
    const trigMin = Number(curTr.min_triggered_entries);
    const trigMax = Number(curTr.max_triggered_entries);
    const expected = Number((learnTimes * (trigMin + trigMax) / 2).toFixed(1));
    let tierCls = "tier-none";
    if (totalCompat >= 60) tierCls = "tier-max";
    else if (totalCompat >= 40) tierCls = "tier-great";
    else if (totalCompat >= 30) tierCls = "tier-good";
    else if (totalCompat >= 20) tierCls = "tier-mid";
    else if (totalCompat >= 1) tierCls = "tier-low";

    return {
      totalCompat,
      rankName: curLv.rank_name,
      level: Number(curLv.level),
      learnTimes,
      trigMin,
      trigMax,
      expected,
      nextLv,
      tierCls,
    };
  }

  function makeNoteSlot(noteObj) {
    if (!noteObj) return null;
    return {
      actorId: Number(noteObj.actor_id),
      mode: "note",
      star: 0,
      note: noteObj,
    };
  }

  function makeSimSlot(actorId, star) {
    return {
      actorId: Number(actorId),
      mode: "sim",
      star: Number(star || 0),
      note: null,
    };
  }

  function findBestAutoPairForCore(coreId) {
    if (!state.compat || !coreId) return [null, null];
    const own = (state.compat.own_notes || []).filter((n) => Number(n.actor_id) !== Number(coreId));
    const bor = (state.compat.borrowed_notes || []).filter((n) => Number(n.actor_id) !== Number(coreId));

    const scorePair = (n1, n2) => {
      const c1 = getSlotContribution(coreId, makeNoteSlot(n1));
      const c2 = getSlotContribution(coreId, makeNoteSlot(n2));
      const tot = c1.total + c2.total;
      const ev = evaluateCompatNumbers(tot);
      const tierBonus = tot >= 60 ? 64 + (tot - 60) * 0.15 : tot >= 40 ? 54 + (tot - 40) * 0.15 : tot >= 30 ? 30 + (tot - 30) * 1.8 : tot;
      const avgSc = (Number(n1.score || 0) + Number(n2.score || 0)) / 2;
      return ev.expected * 8 + tierBonus + avgSc * 0.35;
    };

    let best = null;
    let bestVal = -1;
    // 1. 优先尝试 1自备 + 1好友借用
    for (const n1 of own) {
      for (const n2 of bor) {
        if (Number(n1.actor_id) === Number(n2.actor_id)) continue;
        const v = scorePair(n1, n2);
        if (v > bestVal) {
          bestVal = v;
          best = [makeNoteSlot(n1), makeNoteSlot(n2)];
        }
      }
    }
    // 2. 同时对比 2纯自备，若更强或无借用则选纯自备
    for (let i = 0; i < own.length; i++) {
      for (let j = i + 1; j < own.length; j++) {
        if (Number(own[i].actor_id) === Number(own[j].actor_id)) continue;
        const v = scorePair(own[i], own[j]);
        if (v > bestVal) {
          bestVal = v;
          best = [makeNoteSlot(own[i]), makeNoteSlot(own[j])];
        }
      }
    }
    if (best) return best;

    // 3. 空池优雅降级：取全图鉴基础关系分最高的 2 位角色
    const chars = (state.compat.characters || [])
      .filter((c) => Number(c.id) !== Number(coreId))
      .sort((a, b) => getRelScore(coreId, b.id) - getRelScore(coreId, a.id));
    if (chars.length >= 2) {
      return [makeSimSlot(chars[0].id, 0), makeSimSlot(chars[1].id, 0)];
    }
    return [null, null];
  }

  function ensureCompatReady() {
    syncCompatModeSeg();
    if (!state.compat) {
      const loadingHtml = '<div class="empty-state">正在加载全角色相性矩阵…</div>';
      const slotsEl = $("compat-sandbox-slots");
      const revEl = $("compat-reverse-body");
      const brEl = $("compat-breeding-body");
      if (slotsEl) slotsEl.innerHTML = loadingHtml;
      if (revEl) revEl.innerHTML = loadingHtml;
      if (brEl) brEl.innerHTML = loadingHtml;
      return;
    }
    if (!getCompatChar(state.compatCoreId) && state.compat.characters.length) {
      state.compatCoreId = state.compat.characters[0].id;
    }
    if (!state.compatSlotA && !state.compatSlotB) {
      const [sA, sB] = findBestAutoPairForCore(state.compatCoreId);
      state.compatSlotA = sA;
      state.compatSlotB = sB;
    }
    // 初始化反查默认对象（优先取最高分自备彩笔记）
    if (!state.compatRevNoteId && state.compat.own_notes && state.compat.own_notes.length) {
      state.compatRevType = "note";
      state.compatRevNoteId = state.compat.own_notes[0].note_id;
      state.compatRevActorId = state.compat.own_notes[0].actor_id;
    } else if (!state.compatRevNoteId && state.compat.characters.length) {
      state.compatRevType = "actor";
      state.compatRevActorId = state.compat.characters[0].id;
    }
    renderCompatView();
  }

  function openCompatForCore(charId, autoFill) {
    state.compatCoreId = Number(charId);
    if (autoFill && state.compat) {
      const [sA, sB] = findBestAutoPairForCore(state.compatCoreId);
      state.compatSlotA = sA;
      state.compatSlotB = sB;
    }
    state.compatSubMode = "forward";
    syncCompatModeSeg();
    switchView("compat");
    renderCompatView();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function openCompatReverseForNote(noteId, actorId) {
    state.compatSubMode = "reverse";
    if (noteId) {
      state.compatRevType = "note";
      state.compatRevNoteId = String(noteId);
      if (actorId) state.compatRevActorId = Number(actorId);
    } else if (actorId) {
      state.compatRevType = "actor";
      state.compatRevNoteId = "";
      state.compatRevActorId = Number(actorId);
    }
    state.compatRevData = null;
    syncCompatModeSeg();
    switchView("compat");
    renderCompatView();
    loadCompatReverseData();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function syncCompatModeSeg() {
    const mode = state.compatSubMode || "forward";
    const seg = $("compat-mode-seg");
    if (seg) {
      seg.querySelectorAll("[data-cmode]").forEach((b) => {
        const isActive = b.dataset.cmode === mode;
        b.classList.toggle("active", isActive);
        b.setAttribute("aria-selected", isActive ? "true" : "false");
      });
    }
    const panes = {
      forward: $("compat-pane-forward"),
      reverse: $("compat-pane-reverse"),
      breeding: $("compat-pane-breeding"),
    };
    Object.entries(panes).forEach(([key, el]) => {
      if (el) el.classList.toggle("active", key === mode);
    });
  }

  function renderCompatView() {
    syncCompatModeSeg();
    if (!state.compat) return;
    if (state.compatSubMode === "forward") {
      renderCompatSandbox();
      renderCompatForward();
    } else if (state.compatSubMode === "reverse") {
      renderCompatReverse();
    } else {
      renderCompatBreeding();
    }
  }

  function renderCompatSandbox() {
    const core = getCompatChar(state.compatCoreId);
    const slotA = state.compatSlotA;
    const slotB = state.compatSlotB;
    const cA = getSlotContribution(state.compatCoreId, slotA);
    const cB = getSlotContribution(state.compatCoreId, slotB);
    const dupActors = Boolean(slotA && slotB && Number(slotA.actorId) === Number(slotB.actorId));

    const renderPartnerSlot = (which, slot, contrib) => {
      const label = which === "A" ? "笔记槽位 A" : "笔记槽位 B";
      if (!slot || !slot.actorId) {
        return `
          <div class="sandbox-slot is-empty" data-slot-open="${which}">
            <div class="slot-head">
              <span class="slot-label">${label}</span>
              <span class="badge">空槽</span>
            </div>
            <div class="slot-empty-cta">
              <strong>+ 点击极速选人 / 选笔记</strong>
              <span>支持拼音首字母、位置与真实笔记池</span>
            </div>
          </div>
        `;
      }
      const ch = getCompatChar(slot.actorId);
      const isNote = slot.mode === "note" && slot.note;
      const n = slot.note;
      const warnCls = (contrib.isSameAsCore || dupActors) ? "is-warn" : "";
      const noteTag = isNote
        ? (n.friend ? `<span class="badge badge-gold">借:${esc(n.friend)}</span>` : '<span class="badge badge-purple">自备</span>')
        : '<span class="badge">自由模拟</span>';
      const scoreTag = isNote ? `<span class="badge ${n.rank >= 7 ? "badge-purple" : ""}">${n.rank}阶 ${n.score}</span>` : "";

      const footerHtml = isNote
        ? `
          <div class="slot-footer">
            <span style="font-size:11.5px;color:${contrib.bonus > 0 ? "var(--gold-text)" : "var(--text-dim)"}">
              ${contrib.bonus > 0 ? `★ 命中主力契合 ${contrib.star}★ (+${contrib.bonus})` : "对当前主力无专属契合词条"}
            </span>
            <div style="display:flex;gap:4px">
              <button class="mini-btn" type="button" data-slot-tosim="${which}" title="切换为自由调节星级模拟">切模拟</button>
              <button class="mini-btn" type="button" data-slot-clear="${which}" title="清空该槽位">×</button>
            </div>
          </div>
        `
        : `
          <div class="slot-footer">
            <div class="star-pills">
              ${[0, 1, 2, 3].map((st) => {
                const b = getHarmonyBonus(st);
                return `<button type="button" class="star-pill ${Number(slot.star) === st ? "active" : ""}" data-slot-star="${which}:${st}">${st === 0 ? "0★" : `${st}★+${b}`}</button>`;
              }).join("")}
            </div>
            <button class="mini-btn" type="button" data-slot-clear="${which}" title="清空该槽位">×</button>
          </div>
        `;

      return `
        <div class="sandbox-slot ${warnCls}">
          <div class="slot-head">
            <span class="slot-label">${label}</span>
            <div style="display:flex;align-items:center;gap:6px">
              <span class="badge ${contrib.total >= 18 ? "badge-green" : contrib.total >= 14 ? "badge-blue" : ""}">贡献 +${contrib.total}</span>
              <button class="mini-btn" type="button" data-slot-open="${which}">换人/笔记</button>
            </div>
          </div>
          <div class="slot-main" data-slot-open="${which}">
            ${avatarHtml(ch ? ch.head : (n ? n.head : ""), ch ? ch.short_name : "", "avatar")}
            <div class="slot-main-info">
              <div class="slot-char-name">
                <span>${esc(ch ? ch.short_name : (n ? n.actor_name : slot.actorId))}</span>
                ${ch ? `<span class="badge">${esc(ch.pos)}</span>` : ""}
                ${noteTag}
                ${scoreTag}
              </div>
              <div class="slot-char-sub">
                ${contrib.isSameAsCore ? '<span style="color:var(--red)">▲ 与主力同名，基础关系分为 0</span>' : `基础关系 +${contrib.rel}${contrib.bonus ? ` · 契合 +${contrib.bonus}` : ""}`}
                ${isNote && n.highlights ? ` · ${esc(n.highlights)}` : ""}
              </div>
            </div>
          </div>
          ${footerHtml}
        </div>
      `;
    };

    const coreHtml = core ? `
      <div class="sandbox-slot is-core">
        <div class="slot-head">
          <span class="slot-label">育成主力 C</span>
          <button class="mini-btn active" type="button" data-slot-open="core">▸ 换主力</button>
        </div>
        <div class="slot-main" data-slot-open="core">
          ${avatarHtml(core.head, core.short_name, "avatar-lg")}
          <div class="slot-main-info">
            <div class="slot-char-name">
              <span>${esc(core.short_name)}</span>
              <span class="badge badge-blue">${esc(core.pos)}</span>
              ${core.owned ? '<span class="badge badge-green">已拥有</span>' : '<span class="badge">图鉴</span>'}
            </div>
            <div class="slot-char-sub">${esc(core.school || "—")} · 主练：${esc((core.recommend_train || []).join("/") || "—")}</div>
          </div>
        </div>
        <div class="slot-footer">
          <span style="font-size:11.5px;color:var(--text-muted)">历史最高 ${core.best_rank ? core.best_rank.toLocaleString() : "—"} · ${core.has_rainbow_note ? "已有7阶彩" : core.best_own_note_rank ? `${core.best_own_note_rank}阶 ${core.best_own_note_score}` : "无自备笔记"}</span>
          <div style="display:flex;gap:5px;flex-wrap:wrap">
            <button class="mini-btn" type="button" id="btn-compat-goto-reverse" title="切换到单卡反查，看 TA 的笔记能激活谁">反查 TA →</button>
            <button class="mini-btn" type="button" id="btn-compat-goto-planner">配装参谋 →</button>
          </div>
        </div>
      </div>
    ` : "";

    $("compat-sandbox-slots").innerHTML = `
      ${coreHtml}
      <div class="compat-arrow" aria-hidden="true">←</div>
      ${renderPartnerSlot("A", slotA, cA)}
      ${renderPartnerSlot("B", slotB, cB)}
    `;

    // 绑定沙盒槽位交互
    $("compat-sandbox-slots").querySelectorAll("[data-slot-open]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const target = el.dataset.slotOpen;
        if (target === "core") {
          openQuickPicker({
            target: "core",
            title: "选择育成主力 C",
            allowNotes: false,
            onPick: (picked) => {
              state.compatCoreId = Number(picked.char.id);
              renderCompatView();
            },
          });
        } else {
          openQuickPicker({
            target,
            title: `为【${core ? core.short_name : "主力"}】选择笔记槽位 ${target}`,
            allowNotes: true,
            coreForRel: state.compatCoreId,
            onPick: (picked) => {
              const newSlot = picked.type === "note" ? makeNoteSlot(picked.note) : makeSimSlot(picked.char.id, 0);
              if (target === "A") state.compatSlotA = newSlot;
              else state.compatSlotB = newSlot;
              renderCompatView();
            },
          });
        }
      });
    });

    $("compat-sandbox-slots").querySelectorAll("[data-slot-star]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const [which, stStr] = btn.dataset.slotStar.split(":");
        const st = Number(stStr);
        if (which === "A" && state.compatSlotA) state.compatSlotA.star = st;
        if (which === "B" && state.compatSlotB) state.compatSlotB.star = st;
        renderCompatView();
      });
    });

    $("compat-sandbox-slots").querySelectorAll("[data-slot-tosim]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const which = btn.dataset.slotTosim;
        const sl = which === "A" ? state.compatSlotA : state.compatSlotB;
        if (sl) {
          const curStar = getSlotContribution(state.compatCoreId, sl).star;
          const next = makeSimSlot(sl.actorId, curStar);
          if (which === "A") state.compatSlotA = next;
          else state.compatSlotB = next;
          renderCompatView();
        }
      });
    });

    $("compat-sandbox-slots").querySelectorAll("[data-slot-clear]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.dataset.slotClear === "A") state.compatSlotA = null;
        else state.compatSlotB = null;
        renderCompatView();
      });
    });

    const gotoReverse = $("btn-compat-goto-reverse");
    if (gotoReverse && core) {
      gotoReverse.addEventListener("click", (e) => {
        e.stopPropagation();
        const bestNote = getBestNoteForActor(core.id, true);
        openCompatReverseForNote(bestNote ? bestNote.note_id : "", core.id);
      });
    }

    const gotoPlanner = $("btn-compat-goto-planner");
    if (gotoPlanner && core) {
      gotoPlanner.addEventListener("click", (e) => {
        e.stopPropagation();
        state.plannerCharId = core.id;
        const sel = $("planner-char-select");
        if (sel) sel.value = String(core.id);
        switchView("box");
        loadPlanner();
      });
    }

    // 渲染实时计算结果仪表条
    const relTotal = cA.rel + cB.rel;
    const bonusTotal = cA.bonus + cB.bonus;
    const totalCompat = relTotal + bonusTotal;
    const ev = evaluateCompatNumbers(totalCompat);
    const pct = Math.min(100, Math.round((totalCompat / 65) * 100));

    const ticks = [
      { v: 10, lbl: "10 一般" },
      { v: 20, lbl: "20 良" },
      { v: 30, lbl: "30 优·5次" },
      { v: 40, lbl: "40 满词条" },
      { v: 45, lbl: "45 极" },
      { v: 60, lbl: "60 隐藏" },
    ];

    let hintCls = "";
    let hintText = "";
    if (dupActors) {
      hintCls = "is-warn";
      hintText = "▲ 槽位 A 与槽位 B 为同一名角色：游戏内开局不能同时装备两本同一角色的主笔记，请替换其中一个槽位。";
    } else if (cA.isSameAsCore || cB.isSameAsCore) {
      hintCls = "is-warn";
      hintText = "▲ 携带与育成主力同名的笔记时，该笔记的基础关系分按规则记为 0 分（仍可生效专属契合词条）。";
    } else if (!slotA || !slotB) {
      hintText = "◆ 还可以再从下方正查列表点击一名搭档补齐双笔记槽位，实时查看完整总相性与触发词条期望。";
    } else if (totalCompat >= 60) {
      hintCls = "is-max";
      hintText = `★ 已突破 60 分隐藏满档！每次学习触发 5~11 条词条（全场 5 次期望 ${ev.expected} 条），顶配起跑！`;
    } else if (totalCompat >= 40) {
      hintCls = "is-great";
      hintText = `◆ 已达 40+ 满词条档（40~59 分同为 5 次学习 × 单次 5~10 条词条；${totalCompat < 45 ? "与 45 分『极』实机词条收益完全一致，" : ""}不必为凑虚分牺牲主属性上限词条；距 60 隐藏档差 ${60 - totalCompat} 分）。`;
    } else if (totalCompat >= 30) {
      hintText = `◆ 已达 30 分【优】并拉满 5 次学习！再补 ${40 - totalCompat} 分即可进 40 分满词条档（单次 4~8 → 5~10 条，多出 1 张 2★/3★ 专属契合即可跨档）。`;
    } else {
      hintText = `▸ 距 30 分【优】（满 5 次学习）还差 ${30 - totalCompat} 分；建议在下方正查墙按关系分降序挑选高相性搭档。`;
    }

    $("compat-result-bar").innerHTML = `
      <div class="compat-result-top">
        <div class="compat-score-block">
          <span class="compat-score-num num">${totalCompat}</span>
          <span style="font-size:13px;color:var(--text-muted)">总相性分</span>
          <span class="compat-tier-badge ${ev.tierCls}">${esc(ev.rankName)} · Lv.${ev.level}</span>
          <span class="badge badge-blue">学习 ${ev.learnTimes} 次</span>
          <span class="badge">单次 ${ev.trigMin}~${ev.trigMax} 词条</span>
          <span class="badge badge-green">全场期望 ${ev.expected} 条</span>
        </div>
        <div class="compat-formula-row num">
          <span>关系分 <strong>${cA.rel} + ${cB.rel} = ${relTotal}</strong></span>
          <span>·</span>
          <span>专属契合 <strong>+${cA.bonus} + ${cB.bonus} = +${bonusTotal}</strong></span>
        </div>
      </div>

      <div class="compat-meter-wrap">
        <div class="compat-meter-track">
          <div class="compat-meter-fill" style="width:${pct}%"></div>
        </div>
        <div class="compat-meter-ticks">
          ${ticks.map((tk) => `
            <span class="compat-meter-tick ${totalCompat >= tk.v ? "reached" : ""}" style="left:${Math.min(96, Math.round((tk.v / 65) * 100))}%">${tk.lbl}</span>
          `).join("")}
        </div>
      </div>

      <div class="compat-hint-banner ${hintCls}">${hintText}</div>
    `;
  }

  function renderCompatForward() {
    const core = getCompatChar(state.compatCoreId);
    const host = $("compat-forward-body");
    const titleEl = $("compat-forward-title");
    const subEl = $("compat-forward-sub");
    if (!host) return;
    if (!core || !state.compat) {
      host.innerHTML = '<div class="empty-state">暂无角色数据</div>';
      return;
    }
    if (titleEl) titleEl.textContent = `谁最搭【${core.short_name}】`;
    if (subEl) subEl.textContent = `按对【${core.short_name}】的基础关系分降序，点击卡片智能填入上方槽位 A/B；自动训练刷彩优先选【同位刷彩】`;

    const posFilters = [
      { key: "ALL", label: "全部" },
      { key: "SAME", label: `★同位刷彩 (${core.pos})` },
      ...POS_ORDER.map((p) => ({ key: p, label: p })),
    ];

    const slotAId = state.compatSlotA ? Number(state.compatSlotA.actorId) : 0;
    const slotBId = state.compatSlotB ? Number(state.compatSlotB.actorId) : 0;

    const partners = (state.compat.characters || [])
      .filter((c) => Number(c.id) !== Number(core.id))
      .map((c) => ({
        ...c,
        rel: getRelScore(core.id, c.id),
        revRel: getRelScore(c.id, core.id),
        samePos: Boolean(core.pos && c.pos === core.pos),
        hasAnyNote: c.own_note_count > 0 || c.borrow_note_count > 0,
      }))
      .filter((c) => {
        if (state.compatFwdPos === "SAME" && !c.samePos) return false;
        if (state.compatFwdPos !== "ALL" && state.compatFwdPos !== "SAME" && c.pos !== state.compatFwdPos) return false;
        if (state.compatFwdOnlyOwned && !c.owned) return false;
        if (state.compatFwdOnlyHasNote && !c.hasAnyNote) return false;
        if (state.compatFwdSearch && !charMatchesQuery(c, state.compatFwdSearch)) return false;
        return true;
      })
      .sort((a, b) => b.rel - a.rel || Number(b.owned) - Number(a.owned) || b.best_own_note_score - a.best_own_note_score || a.id - b.id);

    host.innerHTML = `
      <div class="compat-filter-bar">
        <div class="pill-group">
          ${posFilters.map((f) => `<button type="button" class="filter-pill ${state.compatFwdPos === f.key ? "active" : ""}" data-fwd-pos="${f.key}">${esc(f.label)}</button>`).join("")}
        </div>
        <div class="head-controls">
          <input id="compat-fwd-search" class="ctrl-input" type="search" value="${esc(state.compatFwdSearch)}" placeholder="搜角色 / 拼音(lgs) / 学校…" />
          <label class="chk-pill"><input type="checkbox" id="compat-fwd-owned" ${state.compatFwdOnlyOwned ? "checked" : ""} /><span>仅已拥有</span></label>
          <label class="chk-pill"><input type="checkbox" id="compat-fwd-hasnote" ${state.compatFwdOnlyHasNote ? "checked" : ""} /><span>有自备/借用笔记</span></label>
        </div>
      </div>

      ${partners.length ? `
        <div class="compat-partner-grid">
          ${partners.map((p) => {
            const inA = p.id === slotAId;
            const inB = p.id === slotBId;
            const cardCls = inA ? "in-slot-a" : inB ? "in-slot-b" : "";
            const noteBadge = p.has_rainbow_note
              ? `<span class="badge badge-purple">彩 ${p.best_own_note_score}</span>`
              : p.best_own_note_rank
              ? `<span class="badge">自备 ${p.best_own_note_score}</span>`
              : "";
            const borBadge = p.best_borrow_score
              ? `<span class="badge badge-gold">借 ${p.best_borrow_score}</span>`
              : "";
            return `
              <div class="partner-card ${cardCls}" data-partner-cid="${p.id}">
                <div class="partner-card-top">
                  ${avatarHtml(p.head, p.short_name, "avatar")}
                  <div class="partner-card-meta">
                    <div class="partner-card-name">
                      <span>${esc(p.short_name)}</span>
                      <span class="badge">${esc(p.pos)}</span>
                      ${p.owned ? '<span style="color:var(--green);font-size:11px" title="已拥有角色">★</span>' : ""}
                    </div>
                    <div class="partner-card-sub">${esc(p.school || "—")} · 反向 +${p.revRel}</div>
                  </div>
                  <span class="partner-rel-badge ${relBadgeClass(p.rel)}" title="对${esc(core.short_name)}基础关系分">+${p.rel}</span>
                </div>
                <div class="partner-card-foot">
                  <div class="partner-badges">
                    ${p.samePos ? '<span class="badge badge-blue">同位刷彩</span>' : ""}
                    ${noteBadge}
                    ${borBadge}
                  </div>
                  <div class="partner-card-actions">
                    <button type="button" class="mini-btn ${inA ? "active" : ""}" data-put-slot="A:${p.id}">${inA ? "已在A" : "+槽A"}</button>
                    <button type="button" class="mini-btn ${inB ? "active" : ""}" data-put-slot="B:${p.id}">${inB ? "已在B" : "+槽B"}</button>
                  </div>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      ` : '<div class="empty-state">当前筛选条件下无匹配搭档，可清空搜索或取消勾选筛选</div>'}
    `;

    const sInput = $("compat-fwd-search");
    if (sInput) {
      sInput.addEventListener("input", (e) => {
        state.compatFwdSearch = e.target.value;
        const curPos = e.target.selectionStart;
        renderCompatForward();
        const nextInp = $("compat-fwd-search");
        if (nextInp) {
          nextInp.focus();
          if (curPos != null) nextInp.setSelectionRange(curPos, curPos);
        }
      });
    }
    host.querySelectorAll("[data-fwd-pos]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.compatFwdPos = btn.dataset.fwdPos;
        renderCompatForward();
      });
    });
    const ownChk = $("compat-fwd-owned");
    if (ownChk) {
      ownChk.addEventListener("change", (e) => {
        state.compatFwdOnlyOwned = e.target.checked;
        renderCompatForward();
      });
    }
    const noteChk = $("compat-fwd-hasnote");
    if (noteChk) {
      noteChk.addEventListener("change", (e) => {
        state.compatFwdOnlyHasNote = e.target.checked;
        renderCompatForward();
      });
    }

    const assignPartnerToSlot = (which, cid) => {
      const bestN = getBestNoteForActor(cid, which === "A");
      const newSlot = bestN ? makeNoteSlot(bestN) : makeSimSlot(cid, 0);
      if (which === "A") state.compatSlotA = newSlot;
      else state.compatSlotB = newSlot;
      renderCompatView();
    };

    host.querySelectorAll("[data-put-slot]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const [which, cidStr] = btn.dataset.putSlot.split(":");
        assignPartnerToSlot(which, Number(cidStr));
      });
    });

    host.querySelectorAll(".partner-card[data-partner-cid]").forEach((card) => {
      card.addEventListener("click", () => {
        const cid = Number(card.dataset.partnerCid);
        if (cid === slotAId || cid === slotBId) return;
        const target = !state.compatSlotA ? "A" : !state.compatSlotB ? "B" : "B";
        assignPartnerToSlot(target, cid);
      });
    });
  }

  function computeReverseActivationOffline() {
    if (!state.compat) return null;
    const isReal = state.compatRevType === "note" && state.compatRevNoteId;
    const realNote = isReal ? findCompatNoteById(state.compatRevNoteId) : null;
    const aid = realNote ? Number(realNote.actor_id) : Number(state.compatRevActorId);
    const ch = getCompatChar(aid);
    if (!ch) return null;

    const hStar = Number(state.compatRevStar || 0);
    const htMap = realNote ? (realNote.harmony_targets || {}) : {};
    const targetNote = realNote || {
      note_id: `virtual_${aid}_${hStar}`,
      actor_id: aid,
      actor_name: ch.short_name,
      pos: ch.pos,
      head: ch.head,
      rank: 0,
      score: 0,
      friend: "",
      harmony_targets: {},
      harmony_str: hStar > 0 ? `假设全角色 ${hStar}★(+${getHarmonyBonus(hStar)})` : "0★ 基础关系分",
      highlights: "离线虚拟角色单卡推演",
    };

    const isTargetOwn = !realNote || realNote.source === "own";
    const ownPartners = (state.compat.own_notes || []).filter((n) => Number(n.actor_id) !== aid);
    const borPartners = (state.compat.borrowed_notes || []).filter((n) => Number(n.actor_id) !== aid);

    const calcList = (partners, minScore) => {
      const out = [];
      for (const c of state.compat.characters || []) {
        if (Number(c.id) === aid) continue;
        let best = null;
        for (const p of partners) {
          if (Number(p.actor_id) === Number(c.id)) continue;
          const r1 = getRelScore(c.id, aid);
          const s1 = realNote ? Number(htMap[String(c.id)] || 0) : hStar;
          const b1 = getHarmonyBonus(s1);
          const r2 = getRelScore(c.id, p.actor_id);
          const s2 = Number((p.harmony_targets || {})[String(c.id)] || 0);
          const b2 = getHarmonyBonus(s2);
          const tot = r1 + b1 + r2 + b2;
          const avgSc = (Number(targetNote.score || 0) + Number(p.score || 0)) / 2;
          if (!best || tot > best.total_compat || (tot === best.total_compat && avgSc > best.avg_note_score)) {
            const ev = evaluateCompatNumbers(tot);
            best = {
              char_id: c.id,
              char_name: c.short_name,
              pos: c.pos,
              head: c.head,
              owned: c.owned,
              total_compat: tot,
              rank_name: ev.rankName,
              level: ev.level,
              learn_times: ev.learnTimes,
              trig_min: ev.trigMin,
              trig_max: ev.trigMax,
              expected_triggers: ev.expected,
              r1, s1, b1, r2, s2, b2,
              avg_note_score: Number(avgSc.toFixed(1)),
              partner: p,
            };
          }
        }
        if (best && best.total_compat >= minScore) out.push(best);
      }
      return out.sort((a, b) => b.total_compat - a.total_compat || b.avg_note_score - a.avg_note_score).slice(0, 20);
    };

    const staticRankings = (state.compat.characters || [])
      .filter((c) => Number(c.id) !== aid)
      .map((c) => {
        const rel = getRelScore(c.id, aid);
        const hs = realNote ? Number(htMap[String(c.id)] || 0) : hStar;
        const hb = getHarmonyBonus(hs);
        return {
          char_id: c.id,
          char_name: c.short_name,
          pos: c.pos,
          head: c.head,
          same_pos: Boolean(ch.pos && c.pos === ch.pos),
          owned: c.owned,
          rel,
          harmony_star: hs,
          harmony_bonus: hb,
          single_contrib: rel + hb,
        };
      })
      .sort((a, b) => b.single_contrib - a.single_contrib || b.rel - a.rel);

    return {
      ok: true,
      is_virtual: !realNote,
      source_tag: realNote ? (realNote.friend ? `借:${realNote.friend}` : "自备") : (hStar ? `角色推演 (${hStar}★)` : "角色基础推演"),
      is_target_own: isTargetOwn,
      target_note: targetNote,
      own_activations: calcList(ownPartners, 26),
      borrow_activations: isTargetOwn ? calcList(borPartners, 28) : [],
      static_rankings: staticRankings,
    };
  }

  async function loadCompatReverseData() {
    if (!state.compat) return;
    if (!state.online) {
      state.compatRevData = computeReverseActivationOffline();
      renderCompatReverse();
      return;
    }
    state.compatRevLoading = true;
    renderCompatReverse();
    try {
      const qs = new URLSearchParams();
      if (state.compatRevType === "note" && state.compatRevNoteId) {
        qs.set("note_id", state.compatRevNoteId);
      } else {
        qs.set("actor_id", String(state.compatRevActorId));
        qs.set("harmony_star", String(state.compatRevStar));
      }
      state.compatRevData = await apiGet("/api/compatibility/note_activation?" + qs.toString());
    } catch (_) {
      state.compatRevData = computeReverseActivationOffline();
    } finally {
      state.compatRevLoading = false;
      renderCompatReverse();
    }
  }

  function renderCompatReverse() {
    const host = $("compat-reverse-body");
    if (!state.compat || !host) return;
    if (!state.compatRevData && !state.compatRevLoading) {
      loadCompatReverseData();
      return;
    }

    const ownTop = (state.compat.own_notes || []).slice(0, 8);
    const borTop = (state.compat.borrowed_notes || []).slice(0, 4);
    const quickNotes = [...ownTop, ...borTop];

    const quickChipsHtml = quickNotes.length ? `
      <div class="reverse-quick-notes">
        <span style="font-size:12px;color:var(--text-muted)">快选本地笔记：</span>
        ${quickNotes.map((n) => {
          const isAct = state.compatRevType === "note" && String(state.compatRevNoteId) === String(n.note_id);
          return `
            <button type="button" class="quick-note-chip ${isAct ? "active" : ""}" data-qnote="${esc(n.note_id)}" data-qaid="${n.actor_id}">
              <span>${esc(n.actor_name)}</span>
              <span style="opacity:0.85">${n.rank}阶${n.score}</span>
              ${n.friend ? `<span style="color:var(--gold-text)">借</span>` : ""}
            </button>
          `;
        }).join("")}
      </div>
    ` : "";

    const d = state.compatRevData;
    const tn = d && d.target_note ? d.target_note : null;

    const starBarHtml = (state.compatRevType === "actor")
      ? `
        <div class="star-pills">
          ${[0, 1, 2, 3].map((st) => `<button type="button" class="star-pill ${state.compatRevStar === st ? "active" : ""}" data-rev-star="${st}">${st === 0 ? "0★基础" : `假设${st}★+${getHarmonyBonus(st)}`}</button>`).join("")}
        </div>
      `
      : "";

    const heroHtml = `
      <div class="reverse-hero">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          ${tn ? avatarHtml(tn.head, tn.actor_name, "avatar-lg") : ""}
          <div>
            <div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:15px;font-weight:700">
              <span>${tn ? esc(tn.actor_name) : "请选择笔记或角色"}</span>
              ${tn && tn.pos ? `<span class="badge">${esc(tn.pos)}</span>` : ""}
              ${d ? `<span class="badge ${d.is_virtual ? "badge-blue" : "badge-purple"}">${esc(d.source_tag)}</span>` : ""}
              ${tn && tn.score ? `<span class="badge badge-gold">${tn.rank}阶 ${tn.score}分</span>` : ""}
            </div>
            <div class="panel-sub" style="margin-top:3px">
              ${tn ? `专属契合：${esc(tn.harmony_str || "无")} · ${esc(tn.highlights || "")}` : "点右侧按钮打开极速选人 / 选笔记浮层"}
            </div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          ${starBarHtml}
          ${tn ? `<button id="btn-rev-as-core" class="action-btn" type="button" title="切换到配对试算，以 ${esc(tn.actor_name)} 为主力 C 查看最优搭配">以 TA 为主力试算 →</button>` : ""}
          <button id="btn-rev-pick" class="action-btn action-btn-primary" type="button">▸ 换笔记 / 换角色</button>
        </div>
      </div>
    `;

    if (state.compatRevLoading) {
      host.innerHTML = heroHtml + quickChipsHtml + '<div class="empty-state">正在精确验算相性反查结果…</div>';
      bindReverseControls(host);
      return;
    }

    if (!d || !d.ok) {
      host.innerHTML = heroHtml + quickChipsHtml + `<div class="empty-state">${esc((d && d.error) || "暂无反查数据")}</div>`;
      bindReverseControls(host);
      return;
    }

    const filterFn = (it) => {
      if (state.compatRevOnlyOwned && !it.owned) return false;
      if (state.compatRevPos !== "ALL" && it.pos !== state.compatRevPos) return false;
      return true;
    };

    const ownActs = (d.own_activations || []).filter(filterFn);
    const borActs = (d.borrow_activations || []).filter(filterFn);
    const statics = (d.static_rankings || []).filter(filterFn);

    const renderActTable = (title, items) => {
      if (!items.length) return "";
      return `
        <div>
          <h3 class="sub-title" style="margin-bottom:8px">${title}</h3>
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>目标主力</th><th>总相性</th><th>配方拆解</th><th>试算</th></tr></thead>
              <tbody>
                ${items.slice(0, 12).map((it) => `
                  <tr>
                    <td style="display:flex;align-items:center;gap:7px">
                      ${avatarHtml(it.head, it.char_name, "avatar-sm")}
                      <div>
                        <strong>${esc(it.char_name)}</strong> <span style="color:var(--text-dim);font-size:11px">${esc(it.pos)}</span>
                        ${it.owned ? '<span style="color:var(--green);font-size:11px">★</span>' : ""}
                      </div>
                    </td>
                    <td>
                      <span class="badge ${it.total_compat >= 40 ? "badge-green" : it.total_compat >= 30 ? "badge-blue" : "badge-gold"}">${it.total_compat} · ${esc(it.rank_name)}</span>
                    </td>
                    <td style="font-size:12px;color:var(--text-muted)">
                      本卡(+${it.r1}${it.b1 ? `+${it.b1}` : ""}) + ${esc(it.partner.actor_name)}${it.partner.friend ? `[${esc(it.partner.friend)}]` : ""}(+${it.r2}${it.b2 ? `+${it.b2}` : ""})
                    </td>
                    <td>
                      <button type="button" class="mini-btn btn-load-rev-pair"
                        data-core="${it.char_id}"
                        data-pnid="${esc(it.partner.note_id || "")}"
                        data-paid="${it.partner.actor_id}">进试算</button>
                    </td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        </div>
      `;
    };

    const actsHtml = (ownActs.length || borActs.length)
      ? `<div class="reverse-split ${ownActs.length && borActs.length ? "" : "is-single"}">
           ${renderActTable("◆ 搭配你的自备笔记直达高相性", ownActs)}
           ${renderActTable("★ 搭配好友助战笔记直达高相性", borActs)}
         </div>`
      : '<div class="panel-sub" style="margin-bottom:12px">◆ 当前池暂无可配对的其他高分笔记，以下展示全图鉴 37 名角色的单卡相性贡献排行：</div>';

    host.innerHTML = `
      ${heroHtml}
      ${quickChipsHtml}
      <div class="compat-filter-bar">
        <div class="pill-group">
          ${["ALL", ...POS_ORDER].map((p) => `<button type="button" class="filter-pill ${state.compatRevPos === p ? "active" : ""}" data-rev-pos="${p}">${p === "ALL" ? "全部位置" : p}</button>`).join("")}
        </div>
        <label class="chk-pill"><input type="checkbox" id="compat-rev-owned" ${state.compatRevOnlyOwned ? "checked" : ""} /><span>仅看已拥有角色</span></label>
      </div>
      ${actsHtml}
      <h3 class="sub-title" style="margin-bottom:10px">◆ 全图鉴单卡相性贡献排行（点任意卡片作为主力 C 进入配对试算）</h3>
      <div class="compat-partner-grid">
        ${statics.map((s) => `
          <div class="partner-card" data-rev-setcore="${s.char_id}">
            <div class="partner-card-top">
              ${avatarHtml(s.head, s.char_name, "avatar")}
              <div class="partner-card-meta">
                <div class="partner-card-name">
                  <span>${esc(s.char_name)}</span>
                  <span class="badge">${esc(s.pos)}</span>
                  ${s.owned ? '<span style="color:var(--green);font-size:11px">★</span>' : ""}
                </div>
                <div class="partner-card-sub">基础关系 +${s.rel}${s.harmony_bonus ? ` · ★专属 +${s.harmony_bonus}` : ""}</div>
              </div>
              <span class="partner-rel-badge ${relBadgeClass(s.single_contrib)}">+${s.single_contrib}</span>
            </div>
            <div class="partner-card-foot">
              <div class="partner-badges">
                ${s.same_pos ? '<span class="badge badge-blue">同位刷彩</span>' : ""}
                ${s.harmony_bonus ? `<span class="badge badge-gold">${s.harmony_star}★契合</span>` : ""}
              </div>
              <span style="font-size:11px;color:var(--accent)">进试算 →</span>
            </div>
          </div>
        `).join("")}
      </div>
    `;

    bindReverseControls(host);
  }

  function bindReverseControls(host) {
    const asCoreBtn = $("btn-rev-as-core");
    if (asCoreBtn) {
      asCoreBtn.addEventListener("click", () => {
        const d = state.compatRevData;
        const cid = d && d.target_note ? Number(d.target_note.actor_id) : Number(state.compatRevActorId);
        if (cid) openCompatForCore(cid, true);
      });
    }

    const pickBtn = $("btn-rev-pick");
    if (pickBtn) {
      pickBtn.addEventListener("click", () => {
        openQuickPicker({
          target: "reverse",
          title: "选择要反查的笔记或角色",
          allowNotes: true,
          defaultSource: (state.compat.own_notes || []).length ? "own" : "chars",
          onPick: (picked) => {
            if (picked.type === "note") {
              state.compatRevType = "note";
              state.compatRevNoteId = String(picked.note.note_id);
              state.compatRevActorId = Number(picked.note.actor_id);
            } else {
              state.compatRevType = "actor";
              state.compatRevNoteId = "";
              state.compatRevActorId = Number(picked.char.id);
            }
            state.compatRevData = null;
            loadCompatReverseData();
          },
        });
      });
    }

    host.querySelectorAll("[data-qnote]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.compatRevType = "note";
        state.compatRevNoteId = btn.dataset.qnote;
        state.compatRevActorId = Number(btn.dataset.qaid);
        state.compatRevData = null;
        loadCompatReverseData();
      });
    });

    host.querySelectorAll("[data-rev-star]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.compatRevStar = Number(btn.dataset.revStar);
        state.compatRevData = null;
        loadCompatReverseData();
      });
    });

    host.querySelectorAll("[data-rev-pos]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.compatRevPos = btn.dataset.revPos;
        renderCompatReverse();
      });
    });

    const ownChk = $("compat-rev-owned");
    if (ownChk) {
      ownChk.addEventListener("change", (e) => {
        state.compatRevOnlyOwned = e.target.checked;
        renderCompatReverse();
      });
    }

    host.querySelectorAll(".btn-load-rev-pair").forEach((btn) => {
      btn.addEventListener("click", () => {
        const coreId = Number(btn.dataset.core);
        const pnid = btn.dataset.pnid;
        const paid = Number(btn.dataset.paid);
        const curNote = state.compatRevType === "note" ? findCompatNoteById(state.compatRevNoteId) : null;
        const slotA = curNote ? makeNoteSlot(curNote) : makeSimSlot(state.compatRevActorId, state.compatRevStar);
        const pNote = findCompatNoteById(pnid);
        const slotB = pNote ? makeNoteSlot(pNote) : makeSimSlot(paid, 0);
        state.compatCoreId = coreId;
        state.compatSlotA = slotA;
        state.compatSlotB = slotB;
        state.compatSubMode = "forward";
        renderCompatView();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });

    host.querySelectorAll("[data-rev-setcore]").forEach((card) => {
      card.addEventListener("click", () => {
        const coreId = Number(card.dataset.revSetcore);
        const curNote = state.compatRevType === "note" ? findCompatNoteById(state.compatRevNoteId) : null;
        state.compatCoreId = coreId;
        state.compatSlotA = curNote ? makeNoteSlot(curNote) : makeSimSlot(state.compatRevActorId, state.compatRevStar);
        state.compatSlotB = null;
        state.compatSubMode = "forward";
        renderCompatView();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
  }

  function renderCompatBreeding() {
    const host = $("compat-breeding-body");
    if (!host) return;
    const br = state.compat && state.compat.breeding;
    if (!br) {
      host.innerHTML = '<div class="empty-state">暂无养成循环规划数据</div>';
      return;
    }

    const triHtml = (br.triangles || []).length ? `
      <h3 class="sub-title" style="margin-bottom:10px">★ 黄金三角互哺圈（3 人天生相性保底 ≥32 分，凑齐后可无限互刷高分彩笔记）</h3>
      <div class="triangle-grid">
        ${br.triangles.map((t, idx) => {
          const stBadge = t.status === "complete"
            ? '<span class="badge badge-green">已闭环</span>'
            : t.status === "near"
            ? '<span class="badge badge-gold">仅差1人</span>'
            : t.status === "reference"
            ? '<span class="badge badge-blue">全图鉴参考</span>'
            : '<span class="badge">待开拓</span>';
          return `
            <div class="triangle-card ${t.status === "complete" ? "is-complete" : ""}">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">
                <div style="font-weight:700;font-size:13.5px">黄金圈 #${idx + 1} · 保底 <span style="color:var(--green)">${t.min_compat}</span> / 均 ${t.avg_compat}</div>
                ${stBadge}
              </div>
              <div class="triangle-members">
                ${t.members.map((m, mi) => {
                  const others = t.members.filter((_, oi) => oi !== mi).map((o) => o.actor_id);
                  return `
                    <div class="triangle-member" data-tri-load="${m.actor_id}:${others[0]}:${others[1]}" title="点击以${esc(m.name)}为主力载入沙盒">
                      ${avatarHtml(m.head, m.name, "avatar-sm")}
                      <div style="font-weight:700;font-size:12.5px">${esc(m.name)}</div>
                      <div style="font-size:11px;color:var(--accent)">练她 +${m.compat_from_others}</div>
                      <div style="font-size:10.5px;color:${m.has_own ? "var(--purple)" : "var(--text-dim)"}">${m.has_own ? `${m.own_rank}阶 ${m.own_score}` : "缺自备"}</div>
                    </div>
                  `;
                }).join("")}
              </div>
              <div class="panel-sub" style="margin:0">${esc(t.tip)}</div>
            </div>
          `;
        }).join("")}
      </div>
    ` : "";

    const hubs = br.hub_ranking || [];
    const hubHtml = hubs.length ? `
      <h3 class="sub-title" style="margin-bottom:10px">★ 刷笔记枢纽角色 Top 10（综合 下游辐射广度 × 当前可喂饱度 × 笔记缺口）</h3>
      <div class="table-wrap" style="margin-bottom:18px">
        <table class="data-table">
          <thead><tr><th>#</th><th>枢纽角色</th><th>状态</th><th>下游辐射 (Top8均 / ≥15人数)</th><th>主要喂养对象</th><th>操作</th></tr></thead>
          <tbody>
            ${hubs.slice(0, 10).map((h, i) => `
              <tr>
                <td><span class="badge ${i < 3 ? "badge-green" : ""}">#${i + 1}</span></td>
                <td style="display:flex;align-items:center;gap:8px">${avatarHtml(h.head, h.name, "avatar-sm")}<strong>${esc(h.name)}</strong> <span style="color:var(--text-dim);font-size:11px">${esc(h.pos)}</span></td>
                <td><span class="badge ${h.own_status === "missing" ? "badge-red" : h.own_status === "good" ? "badge-green" : "badge-gold"}">${esc(h.own_tag)}</span></td>
                <td class="num"><strong>${h.top8_avg}</strong> / ${h.high_15_cnt}人</td>
                <td style="font-size:12px;color:var(--text-muted)">${(h.top_targets || []).slice(0, 4).map((x) => `${esc(x.actor_name)}(+${x.rel})`).join("、")}</td>
                <td><button type="button" class="mini-btn btn-hub-sandbox" data-cid="${h.actor_id}">进试算</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    ` : "";

    const steps = br.stepping_stones || [];
    const stepHtml = steps.length ? `
      <h3 class="sub-title" style="margin-bottom:6px">▸ 卡手角色「两步铺路反推链」（当前纯自备 &lt;35 分的主力，先练哪位种马最赚）</h3>
      <div class="stepping-list">
        ${steps.map((s) => `
          <div class="stepping-card">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
              ${avatarHtml(s.target_head, s.target_name, "avatar-sm")}
              <div style="font-size:13px">
                想练 <strong>${esc(s.target_name)}</strong>（现自备 ${s.cur_compat}分）
                ➔ 先练 <strong>${esc(s.bridge_name)}</strong>（现可 ${s.bridge_now_compat}分 靠 ${esc(s.bridge_now_n1)}+${esc(s.bridge_now_n2)}）
                ➔ 搭现有 ${esc(s.partner_name)} 可直拉 <strong style="color:var(--green)">${s.proj_0star}~${s.proj_2star}分</strong>
              </div>
            </div>
            <button type="button" class="mini-btn btn-hub-sandbox" data-cid="${s.bridge_id}">先练 ${esc(s.bridge_name)} →</button>
          </div>
        `).join("")}
      </div>
    ` : "";

    const warnHtml = state.compat.revision_warning
      ? `<div class="compat-hint-banner" style="margin-top:16px">${esc(state.compat.revision_warning)}</div>`
      : "";

    host.innerHTML = triHtml + hubHtml + stepHtml + warnHtml;

    host.querySelectorAll("[data-tri-load]").forEach((el) => {
      el.addEventListener("click", () => {
        const [cId, aId, bId] = el.dataset.triLoad.split(":").map(Number);
        state.compatCoreId = cId;
        const nA = getBestNoteForActor(aId, true);
        const nB = getBestNoteForActor(bId, true);
        state.compatSlotA = nA ? makeNoteSlot(nA) : makeSimSlot(aId, 0);
        state.compatSlotB = nB ? makeNoteSlot(nB) : makeSimSlot(bId, 0);
        state.compatSubMode = "forward";
        renderCompatView();
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });

    host.querySelectorAll(".btn-hub-sandbox").forEach((btn) => {
      btn.addEventListener("click", () => {
        openCompatForCore(Number(btn.dataset.cid), true);
      });
    });
  }

  /* ==================== 全局极速选人 / 选笔记浮层 ==================== */
  function openQuickPicker(opts) {
    if (!state.compat) return;
    const pk = state.picker;
    pk.open = true;
    pk.target = opts.target || "core";
    pk.allowNotes = Boolean(opts.allowNotes);
    pk.source = pk.allowNotes && opts.defaultSource ? opts.defaultSource : "chars";
    pk.search = "";
    pk.pos = "ALL";
    pk.onlyOwned = false;
    pk.coreForRel = opts.coreForRel ? Number(opts.coreForRel) : null;
    pk.sortByRel = Boolean(pk.coreForRel);
    pk.onPick = opts.onPick || null;

    $("picker-modal-title").textContent = opts.title || "极速选人";
    const srcSeg = $("picker-source-seg");
    srcSeg.classList.toggle("hidden", !pk.allowNotes);
    if (pk.allowNotes) {
      const ownCnt = (state.compat.own_notes || []).length;
      const borCnt = (state.compat.borrowed_notes || []).length;
      const btns = srcSeg.querySelectorAll(".seg-btn");
      if (btns[0]) btns[0].textContent = `全部角色 (${(state.compat.characters || []).length})`;
      if (btns[1]) btns[1].textContent = `我的自备 (${ownCnt})`;
      if (btns[2]) btns[2].textContent = `好友可借 (${borCnt})`;
      btns.forEach((b) => b.classList.toggle("active", b.dataset.psrc === pk.source));
    }

    const inp = $("picker-search-input");
    inp.value = "";
    $("compat-picker-modal").classList.remove("hidden");
    renderQuickPicker();
    setTimeout(() => inp.focus(), 20);
  }

  function closeQuickPicker() {
    state.picker.open = false;
    $("compat-picker-modal").classList.add("hidden");
  }

  function getPickerFilteredItems() {
    const pk = state.picker;
    if (!state.compat) return [];
    const kw = pk.search.trim().toLowerCase();
    const coreId = pk.coreForRel;

    if (pk.source === "chars") {
      let list = (state.compat.characters || []).map((c) => ({
        type: "char",
        char: c,
        rel: coreId ? getRelScore(coreId, c.id) : 0,
        isSameAsCore: Boolean(coreId && Number(coreId) === Number(c.id)),
      }));
      list = list.filter((it) => {
        if (pk.pos !== "ALL" && it.char.pos !== pk.pos) return false;
        if (pk.onlyOwned && !it.char.owned) return false;
        if (kw && !charMatchesQuery(it.char, kw)) return false;
        return true;
      });
      if (coreId && pk.sortByRel) {
        list.sort((a, b) => Number(a.isSameAsCore) - Number(b.isSameAsCore) || b.rel - a.rel || Number(b.char.owned) - Number(a.char.owned) || a.char.id - b.char.id);
      } else {
        list.sort((a, b) => Number(b.char.owned) - Number(a.char.owned) || POS_ORDER.indexOf(a.char.pos) - POS_ORDER.indexOf(b.char.pos) || b.char.best_rank - a.char.best_rank || a.char.id - b.char.id);
      }
      return list;
    }

    const srcNotes = pk.source === "own" ? (state.compat.own_notes || []) : (state.compat.borrowed_notes || []);
    let list = srcNotes.map((n) => {
      const ch = getCompatChar(n.actor_id);
      const rel = coreId ? getRelScore(coreId, n.actor_id) : 0;
      const hStar = coreId ? Number((n.harmony_targets || {})[String(coreId)] || 0) : 0;
      const hBonus = getHarmonyBonus(hStar);
      return {
        type: "note",
        note: n,
        char: ch,
        rel,
        hStar,
        hBonus,
        totalContrib: rel + hBonus,
      };
    });
    list = list.filter((it) => {
      if (pk.pos !== "ALL" && it.note.pos !== pk.pos) return false;
      if (kw) {
        const hay = `${charSearchHaystack(it.char)} ${it.note.actor_name} ${it.note.friend || ""} ${it.note.highlights || ""} ${it.note.score} ${it.note.rank}阶`.toLowerCase();
        if (!kw.split(/\s+/).every((tok) => hay.includes(tok))) return false;
      }
      return true;
    });
    if (coreId) {
      list.sort((a, b) => b.totalContrib - a.totalContrib || b.note.score - a.note.score);
    } else {
      list.sort((a, b) => b.note.rank - a.note.rank || b.note.score - a.note.score);
    }
    return list;
  }

  function renderQuickPicker() {
    const pk = state.picker;
    const filterRow = $("picker-filter-row");
    const posPills = ["ALL", ...POS_ORDER].map((p) =>
      `<button type="button" class="filter-pill ${pk.pos === p ? "active" : ""}" data-pk-pos="${p}">${p === "ALL" ? "全部位置" : p}</button>`
    ).join("");
    const rightCtrls = `
      <div class="head-controls">
        ${pk.source === "chars" ? `<label class="chk-pill"><input type="checkbox" id="pk-only-owned" ${pk.onlyOwned ? "checked" : ""} /><span>仅已拥有</span></label>` : ""}
        ${pk.source === "chars" && pk.coreForRel ? `<label class="chk-pill"><input type="checkbox" id="pk-sort-rel" ${pk.sortByRel ? "checked" : ""} /><span>按相性优先排序</span></label>` : ""}
      </div>
    `;
    filterRow.innerHTML = `<div class="pill-group">${posPills}</div>${rightCtrls}`;

    filterRow.querySelectorAll("[data-pk-pos]").forEach((btn) => {
      btn.addEventListener("click", () => {
        pk.pos = btn.dataset.pkPos;
        renderQuickPicker();
      });
    });
    const ownChk = $("pk-only-owned");
    if (ownChk) {
      ownChk.addEventListener("change", (e) => {
        pk.onlyOwned = e.target.checked;
        renderQuickPicker();
      });
    }
    const sortChk = $("pk-sort-rel");
    if (sortChk) {
      sortChk.addEventListener("change", (e) => {
        pk.sortByRel = e.target.checked;
        renderQuickPicker();
      });
    }

    const items = getPickerFilteredItems();
    const body = $("picker-body");
    if (!items.length) {
      body.innerHTML = `<div class="empty-state">${pk.source === "chars" ? "没有匹配的角色，试试输入拼音首字母或切换位置筛选" : "当前分类暂无笔记，可切到「全部角色」进行自由模拟"}</div>`;
      return;
    }

    if (pk.source === "chars") {
      body.innerHTML = `
        <div class="picker-char-grid">
          ${items.map((it, idx) => {
            const c = it.char;
            return `
              <div class="picker-char-card ${idx === 0 ? "is-first" : ""} ${it.isSameAsCore ? "is-disabled" : ""}" data-pk-idx="${idx}">
                ${avatarHtml(c.head, c.short_name, "avatar-sm")}
                <div style="min-width:0;flex:1">
                  <div style="font-weight:700;font-size:13px;display:flex;align-items:center;gap:4px">
                    <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.short_name)}</span>
                    ${c.owned ? '<span style="color:var(--green);font-size:10px">★</span>' : ""}
                  </div>
                  <div style="font-size:11px;color:var(--text-muted)">${esc(c.pos)} · ${esc((c.school || "").slice(0, 4))}</div>
                </div>
                ${pk.coreForRel ? `<span class="partner-rel-badge ${relBadgeClass(it.rel)}" style="font-size:12px;padding:2px 6px">+${it.rel}</span>` : ""}
              </div>
            `;
          }).join("")}
        </div>
      `;
    } else {
      body.innerHTML = `
        <div class="picker-note-list">
          ${items.map((it, idx) => {
            const n = it.note;
            return `
              <div class="picker-note-card ${idx === 0 ? "is-first" : ""}" data-pk-idx="${idx}">
                <div style="display:flex;align-items:center;gap:10px;min-width:0;flex:1">
                  ${avatarHtml(n.head, n.actor_name, "avatar")}
                  <div style="min-width:0;flex:1">
                    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:13.5px;font-weight:700">
                      <span>${esc(n.actor_name)}</span>
                      <span class="badge">${esc(n.pos)}</span>
                      <span class="badge ${n.rank >= 7 ? "badge-purple" : ""}">${n.rank}阶 ${n.score}分</span>
                      ${n.friend ? `<span class="badge badge-gold">借:${esc(n.friend)}</span>` : ""}
                      ${it.hBonus > 0 ? `<span class="badge badge-green">★契合${it.hStar}★(+${it.hBonus})</span>` : ""}
                    </div>
                    <div class="panel-sub" style="margin-top:2px">${esc(n.highlights || "—")}${n.harmony_str ? ` · ${esc(n.harmony_str)}` : ""}</div>
                  </div>
                </div>
                ${pk.coreForRel ? `<span class="partner-rel-badge ${relBadgeClass(it.totalContrib)}">+${it.totalContrib}</span>` : `<span class="badge">反查 →</span>`}
              </div>
            `;
          }).join("")}
        </div>
      `;
    }

    body.querySelectorAll("[data-pk-idx]").forEach((el) => {
      el.addEventListener("click", () => {
        const picked = items[Number(el.dataset.pkIdx)];
        if (!picked) return;
        const cb = pk.onPick;
        closeQuickPicker();
        if (cb) cb(picked);
      });
    });
  }

  /* ==================== 视图 4.5：养成计划与专题攻略 ==================== */
  function renderInlineMd(raw) {
    let s = esc(raw);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, text, href) => {
      const safe = /^https?:\/\//i.test(href) ? href : "#";
      return safe === "#" ? `<span>${text}</span>` : `<a href="${safe}" target="_blank" rel="noopener">${text}</a>`;
    });
    return s;
  }

  function renderMarkdown(mdText) {
    if (!mdText) return "";
    // 去掉 HTML 元信息注释块
    const cleaned = String(mdText).replace(/<!--[\s\S]*?-->/g, "").replace(/\r\n/g, "\n");
    const lines = cleaned.split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trim = line.trim();

      if (!trim) {
        i += 1;
        continue;
      }

      // 代码块
      if (trim.startsWith("```")) {
        const lang = trim.slice(3).trim();
        const buf = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith("```")) {
          buf.push(lines[i]);
          i += 1;
        }
        i += 1;
        out.push(`<pre class="md-pre"><code data-lang="${esc(lang)}">${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }

      // 分隔线
      if (/^-{3,}$/.test(trim) || /^\*{3,}$/.test(trim)) {
        out.push("<hr class=\"md-hr\" />");
        i += 1;
        continue;
      }

      // 标题
      const hm = trim.match(/^(#{1,4})\s+(.+)$/);
      if (hm) {
        const lvl = hm[1].length;
        out.push(`<h${lvl} class="md-h${lvl}">${renderInlineMd(hm[2])}</h${lvl}>`);
        i += 1;
        continue;
      }

      // 引用块
      if (trim.startsWith(">")) {
        const qbuf = [];
        while (i < lines.length && lines[i].trim().startsWith(">")) {
          qbuf.push(lines[i].trim().replace(/^>\s?/, ""));
          i += 1;
        }
        out.push(`<blockquote class="md-quote">${qbuf.map((x) => renderInlineMd(x)).join("<br/>")}</blockquote>`);
        continue;
      }

      // 表格
      if (trim.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:\-|]+\|[\s:\-|]*$/.test(lines[i + 1])) {
        const parseRow = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const headers = parseRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().includes("|") && lines[i].trim() !== "") {
          rows.push(parseRow(lines[i]));
          i += 1;
        }
        const thead = `<thead><tr>${headers.map((h) => `<th>${renderInlineMd(h)}</th>`).join("")}</tr></thead>`;
        const tbody = `<tbody>${rows.map((r) => `<tr>${headers.map((_, idx) => `<td>${renderInlineMd(r[idx] || "")}</td>`).join("")}</tr>`).join("")}</tbody>`;
        out.push(`<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`);
        continue;
      }

      // 无序 / 任务列表
      if (/^[-*+]\s+/.test(trim)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          const itemRaw = lines[i].replace(/^\s*[-*+]\s+/, "");
          const chk = itemRaw.match(/^\[( |x|X)\]\s+(.*)$/);
          if (chk) {
            const done = chk[1].toLowerCase() === "x";
            items.push(`<li class="md-task ${done ? "done" : ""}"><span class="md-chk">${done ? "☑" : "☐"}</span><span>${renderInlineMd(chk[2])}</span></li>`);
          } else {
            items.push(`<li>${renderInlineMd(itemRaw)}</li>`);
          }
          i += 1;
        }
        out.push(`<ul class="md-ul">${items.join("")}</ul>`);
        continue;
      }

      // 有序列表
      if (/^\d+\.\s+/.test(trim)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${renderInlineMd(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
          i += 1;
        }
        out.push(`<ol class="md-ol">${items.join("")}</ol>`);
        continue;
      }

      // 普通段落
      const pbuf = [trim];
      i += 1;
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,4}\s|```|>|[-*+]\s|\d+\.\s|---+$)/.test(lines[i].trim())
      ) {
        pbuf.push(lines[i].trim());
        i += 1;
      }
      out.push(`<p class="md-p">${renderInlineMd(pbuf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  function renderPlansView() {
    const listEl = $("plans-list");
    const detailEl = $("plans-detail-area");
    const pillEl = $("plans-count-pill");
    if (!listEl || !detailEl) return;

    const plans = state.plans || [];
    if (pillEl) pillEl.textContent = `${plans.length} 份`;

    if (!plans.length) {
      listEl.innerHTML = `<div class="empty-state">docs/ 下暂无 *_plan.md 或 *_guide.md 文档</div>`;
      detailEl.innerHTML = `<div class="panel empty-state">把计划写进 docs/*_plan.md 后点击右上角刷新即可显示</div>`;
      return;
    }

    if (!state.selectedPlanId || !plans.some((p) => p.id === state.selectedPlanId)) {
      state.selectedPlanId = plans[0].id;
    }

    listEl.innerHTML = plans.map((p) => {
      const active = p.id === state.selectedPlanId;
      const badgeCls = p.kind === "plan" ? "badge-accent" : "badge-gold";
      return `
        <div class="plan-card ${active ? "active" : ""}" data-plan-id="${esc(p.id)}">
          <div class="plan-card-top">
            <span class="badge ${badgeCls}">${esc(p.tag)}</span>
            <span class="plan-status">${esc(p.status)}</span>
          </div>
          <div class="plan-card-title">${esc(p.title)}</div>
          <div class="plan-card-summary">${esc(p.summary || "")}</div>
          <div class="plan-card-meta">
            <span>${esc(p.rel_path)}</span>
            <span>${esc(p.updated_at)}</span>
          </div>
        </div>
      `;
    }).join("");

    listEl.querySelectorAll("[data-plan-id]").forEach((el) => {
      el.addEventListener("click", () => {
        state.selectedPlanId = el.dataset.planId;
        renderPlansView();
      });
    });

    const cur = plans.find((p) => p.id === state.selectedPlanId) || plans[0];
    const charsHtml = (cur.related_chars || []).length
      ? `
        <div class="plan-chars-bar">
          <span class="panel-sub">快捷直达配装器：</span>
          ${cur.related_chars.map((c) => `
            <button type="button" class="plan-char-chip" data-jump-char="${c.char_id}">
              ${c.head_url ? `<img src="${esc(c.head_url)}" alt="${esc(c.name)}" />` : ""}
              <span>${esc(c.name)}</span>
              <span class="chip-pos">${esc(c.position)}</span>
            </button>
          `).join("")}
        </div>
      `
      : "";

    detailEl.innerHTML = `
      <article class="panel plan-doc-panel">
        <div class="plan-doc-head">
          <div class="plan-doc-badges">
            <span class="badge ${cur.kind === "plan" ? "badge-accent" : "badge-gold"}">${esc(cur.tag)}</span>
            <span class="badge badge-green">${esc(cur.status)}</span>
            <span class="panel-sub">文件：<code>${esc(cur.rel_path)}</code> · 更新：${esc(cur.updated_at)}</span>
          </div>
          ${charsHtml}
        </div>
        <div class="md-body">${renderMarkdown(cur.markdown)}</div>
      </article>
    `;

    detailEl.querySelectorAll("[data-jump-char]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const cid = Number(btn.dataset.jumpChar);
        if (!cid) return;
        state.plannerCharId = cid;
        const sel = $("planner-char-select");
        if (sel) sel.value = String(cid);
        switchView("box");
        loadPlanner();
      });
    });
  }

  /* ==================== 初始化与事件绑定 ==================== */
  async function loadCoreData() {
    try {
      const [ov, box, compat, plansRes] = await Promise.all([
        apiGet("/api/overview"),
        apiGet("/api/box"),
        apiGet("/api/compatibility"),
        apiGet("/api/plans").catch(() => ({ plans: [] })),
      ]);
      state.online = true;
      state.overview = ov;
      state.box = box;
      state.compat = compat;
      state.plans = (plansRes && plansRes.plans) || [];
      setConnBadge(true, "本地服务在线");
    } catch (_) {
      state.online = false;
      const snap = window.LQSN_DASHBOARD_SNAPSHOT;
      if (snap) {
        state.overview = snap.overview;
        state.box = snap.box;
        state.compat = snap.compatibility || null;
        state.plans = (snap.plans && snap.plans.plans) || [];
        setConnBadge(false, "离线快照模式");
      } else {
        setConnBadge(false, "未连接数据");
        return;
      }
    }

    renderOverview();
    renderRunsList();
    initBoxControls();
    if (state.view === "compat") {
      ensureCompatReady();
    }
    if (state.view === "plans") {
      renderPlansView();
    }

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

  const ICON_SUN_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  const ICON_MOON_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

  function updateThemeButton(theme) {
    const btn = $("btn-theme");
    if (!btn) return;
    const isDark = theme === "dark";
    btn.innerHTML = isDark ? ICON_SUN_SVG : ICON_MOON_SVG;
    btn.title = isDark ? "切换亮色校园主题" : "切换深色护眼主题";
    btn.setAttribute("aria-label", btn.title);
  }

  function bindEvents() {
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });

    $("btn-theme").addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("lqsn_dash_theme_v1", next); } catch (_) {}
      updateThemeButton(next);
      renderTrendChart();
    });

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      if (state.view !== "overview") return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => renderTrendChart(), 120);
    });

    const btnExportDiag = $("btn-export-diag");
    if (btnExportDiag) {
      btnExportDiag.addEventListener("click", () => {
        if (!state.online) {
          alert("当前为离线静态快照模式，请通过桌面控制台启动本地作战室后再导出诊断包。");
          return;
        }
        window.location.href = "/api/export-diag";
      });
    }

    $("btn-refresh-all").addEventListener("click", async () => {
      if (state.refreshing) return;
      const btn = $("btn-refresh-all");
      state.refreshing = true;
      if (btn) {
        btn.disabled = true;
        btn.classList.add("is-spinning");
      }
      let refreshInfo = null;
      try {
        if (state.online) {
          setConnBadge(true, "正在扫描战绩与同步归档…");
          try {
            const resp = await fetch("/api/refresh", { method: "POST", cache: "no-store" });
            if (resp.ok) {
              refreshInfo = await resp.json();
            }
          } catch (_) {}
        }
        state.runDetailCache = {};
        state.plannerData = null;
        state.compatRevData = null;
        await loadCoreData();
        if (state.view === "runs") {
          const allRuns = (state.overview && state.overview.runs) || [];
          if (state.compareIds.length === 2) {
            renderRunCompare(state.compareIds[0], state.compareIds[1]);
          } else if (refreshInfo && refreshInfo.new_runs > 0 && allRuns.length) {
            selectRun(allRuns[allRuns.length - 1].run_id);
          } else if (state.selectedRunId && allRuns.some((r) => r.run_id === state.selectedRunId)) {
            selectRun(state.selectedRunId);
          } else if (allRuns.length) {
            selectRun(allRuns[allRuns.length - 1].run_id);
          }
        }
        if (state.view === "box") await loadPlanner();
        if (state.view === "compat") ensureCompatReady();
        if (state.view === "live") pollLiveNow();

        if (refreshInfo && refreshInfo.runs_error) {
          setConnBadge(false, "扫描异常：" + refreshInfo.runs_error);
        } else if (refreshInfo && refreshInfo.new_runs > 0) {
          setConnBadge(true, `✓ 新增 ${refreshInfo.new_runs} 局 · 共 ${refreshInfo.runs_total} 局 (${refreshInfo.refreshed_at || ""})`);
        } else if (refreshInfo && refreshInfo.runs_scanned) {
          setConnBadge(true, `✓ 已同步 ${refreshInfo.runs_total} 局 (${refreshInfo.refreshed_at || ""})`);
        } else if (state.online) {
          setConnBadge(true, "✓ 已刷新归档与角色池");
        }
      } finally {
        if (btn) {
          btn.classList.remove("is-spinning");
          btn.disabled = false;
        }
        state.refreshing = false;
      }
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
    const sortRankEl = $("run-sort-rank");
    if (sortRankEl) {
      sortRankEl.addEventListener("change", (e) => {
        state.runSortByRank = e.target.checked;
        renderRunsList();
      });
    }
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
    const plannerQuickBtn = $("btn-planner-quick-pick");
    if (plannerQuickBtn) {
      plannerQuickBtn.addEventListener("click", () => {
        openQuickPicker({
          target: "planner",
          title: "极速选择配装目标角色",
          allowNotes: false,
          onPick: (picked) => {
            state.plannerCharId = Number(picked.char.id);
            $("planner-char-select").value = String(state.plannerCharId);
            loadPlanner();
          },
        });
      });
    }
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

    // 相性查询沙盒与工作台控制
    $("btn-compat-autofill").addEventListener("click", () => {
      const [sA, sB] = findBestAutoPairForCore(state.compatCoreId);
      state.compatSlotA = sA;
      state.compatSlotB = sB;
      renderCompatView();
    });
    $("btn-compat-swap").addEventListener("click", () => {
      const tmp = state.compatSlotA;
      state.compatSlotA = state.compatSlotB;
      state.compatSlotB = tmp;
      renderCompatView();
    });
    $("btn-compat-clear").addEventListener("click", () => {
      state.compatSlotA = null;
      state.compatSlotB = null;
      renderCompatView();
    });
    $("compat-mode-seg").querySelectorAll("[data-cmode]").forEach((b) => {
      b.addEventListener("click", () => {
        state.compatSubMode = b.dataset.cmode;
        renderCompatView();
      });
    });

    // 极速选人 / 选笔记浮层控制
    $("btn-picker-close").addEventListener("click", closeQuickPicker);
    $("compat-picker-modal").addEventListener("click", (e) => {
      if (e.target === $("compat-picker-modal")) closeQuickPicker();
    });
    $("picker-source-seg").querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        state.picker.source = b.dataset.psrc;
        $("picker-source-seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        renderQuickPicker();
        $("picker-search-input").focus();
      });
    });
    $("picker-search-input").addEventListener("input", (e) => {
      state.picker.search = e.target.value;
      renderQuickPicker();
    });
    $("picker-search-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const items = getPickerFilteredItems();
        if (items.length) {
          e.preventDefault();
          const picked = items[0];
          const cb = state.picker.onPick;
          closeQuickPicker();
          if (cb) cb(picked);
        }
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.picker.open) {
        closeQuickPicker();
      }
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
    const curTheme = document.documentElement.getAttribute("data-theme") || "light";
    document.documentElement.setAttribute("data-theme", curTheme);
    updateThemeButton(curTheme);
    bindEvents();
    loadCoreData();
  });
})();
