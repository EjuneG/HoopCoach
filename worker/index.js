/**
 * HoopCoach Feedback Relay — Cloudflare Worker (Zero-Dependency ESM)
 *
 * 功能：
 * 1. 让无 GitHub 账号或国内直连受限的玩家在客户端一键免登提交脱敏 Bug / 改进建议；
 * 2. 客户端绝不内置任何 GitHub Token，Token 仅加密保存在 Cloudflare Worker Secret (`GITHUB_TOKEN`)；
 * 3. 服务端强制白名单校验、体积限制、简易 IP 限流与二次敏感信息脱敏，自动在目标仓库创建结构化 Issue。
 *
 * 需要在 Cloudflare Worker -> Settings -> Variables and Secrets 中配置：
 * - GITHUB_TOKEN (Secret): Fine-grained PAT，仅授予目标仓库 Issues: Read and write 权限
 * - GITHUB_REPO  (Text)  : 目标公开仓库，如 "EjuneG/HoopCoach"
 * - APP_KEY      (Text, 可选): 若配置，则校验请求头 X-App-Key 防止无关扫描
 */

const MAX_PAYLOAD_BYTES = 120 * 1024;
const MAX_BODY_CHARS = 58000;
const RATE_WINDOW_MS = 60 * 1000;
const RATE_MAX_REQ = 5;

const ipBuckets = new Map();

const CATEGORY_META = {
  bug: { prefix: "[Bug]", label: "🐛 Bug 反馈", ghLabels: ["bug", "from-app"] },
  badge_offset: { prefix: "[角标偏移]", label: "📐 角标位置偏移", ghLabels: ["bug", "ui", "from-app"] },
  feature: { prefix: "[建议]", label: "💡 功能改进建议", ghLabels: ["enhancement", "from-app"] },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-App-Key",
  };
}

function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(),
    },
  });
}

function sanitizeText(text) {
  if (!text) return "";
  let out = String(text);
  out = out.replace(/([A-Za-z]:\\Users\\)([^\\/\r\n"'<>]+)/gi, "$1<USER>");
  out = out.replace(/([A-Za-z]:\/Users\/)([^\\/\r\n"'<>]+)/gi, "$1<USER>");
  out = out.replace(/(\/home\/)([^/\r\n"'<>:\s]+)/g, "$1<USER>");
  out = out.replace(/(\/Users\/)([^/\r\n"'<>:\s]+)/g, "$1<USER>");
  out = out.replace(/\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{12,}\b/g, "<REDACTED_TOKEN>");
  out = out.replace(/\bgithub_pat_[A-Za-z0-9_]{20,}\b/g, "<REDACTED_TOKEN>");
  out = out.replace(/\bsk-[A-Za-z0-9_\-]{16,}\b/g, "<REDACTED_KEY>");
  out = out.replace(/(Bearer\s+)[A-Za-z0-9._\-]{16,}/gi, "$1<REDACTED>");
  return out;
}

function clipText(val, maxLen) {
  const s = sanitizeText(val == null ? "" : String(val)).trim();
  if (s.length <= maxLen) return s;
  return `${s.slice(0, maxLen)}\n... (truncated ${s.length - maxLen} chars)`;
}

function checkRateLimit(ip) {
  const now = Date.now();
  const key = ip || "unknown";
  let arr = ipBuckets.get(key) || [];
  arr = arr.filter((ts) => now - ts < RATE_WINDOW_MS);
  if (arr.length >= RATE_MAX_REQ) {
    ipBuckets.set(key, arr);
    return false;
  }
  arr.push(now);
  ipBuckets.set(key, arr);
  if (ipBuckets.size > 500) {
    for (const [k, v] of ipBuckets.entries()) {
      if (!v.length || now - v[v.length - 1] > RATE_WINDOW_MS) {
        ipBuckets.delete(k);
      }
    }
  }
  return true;
}

function buildIssueBody(payload, catMeta) {
  const desc = clipText(payload.description, 4000) || "（未填写详细描述）";
  const envMd = clipText(payload.env_markdown, 4000);
  const stateSnap = clipText(payload.state_snapshot, 10000);
  const compactLog = clipText(payload.compact_log, 30000);
  const runtimeErr = clipText(payload.runtime_error, 6000);
  const contact = clipText(payload.reporter_note, 120);

  const sections = [
    `### 反馈类型\n${catMeta.label}（客户端一键直投）`,
    `### 问题描述 / 复现步骤\n${desc}`,
  ];

  if (contact) {
    sections.push(`### 反馈人备注\n${contact}`);
  }
  if (envMd) {
    sections.push(`### 自动采集的脱敏环境信息\n${envMd}`);
  }
  if (stateSnap) {
    sections.push(
      `<details>\n<summary>🧠 实时内存状态快照 (state_snapshot.json)</summary>\n\n\`\`\`json\n${stateSnap}\n\`\`\`\n</details>`
    );
  }
  if (runtimeErr) {
    sections.push(
      `<details>\n<summary>⚠️ 客户端运行异常日志 (runtime_error.log)</summary>\n\n\`\`\`text\n${runtimeErr}\n\`\`\`\n</details>`
    );
  }
  if (compactLog) {
    sections.push(
      `<details>\n<summary>📜 本局精简脱敏日志 (已剥离 Unity 堆栈)</summary>\n\n\`\`\`text\n${compactLog}\n\`\`\`\n</details>`
    );
  }

  let body = sections.join("\n\n") + "\n";
  if (body.length > MAX_BODY_CHARS) {
    body = `${body.slice(0, MAX_BODY_CHARS)}\n\n> ⚠️ 诊断附带内容已按上限自动截断。\n`;
  }
  return body;
}

async function createGitHubIssue(env, { title, body, labels }) {
  const repo = (env.GITHUB_REPO || "").trim().replace(/^\/+|\/+$/g, "");
  if (!/^[A-Za-z0-9_.\-]+\/[A-Za-z0-9_.\-]+$/.test(repo)) {
    return { ok: false, status: 500, error: "服务端未配置合法的 GITHUB_REPO" };
  }
  const token = (env.GITHUB_TOKEN || "").trim();
  if (!token) {
    return { ok: false, status: 500, error: "服务端未配置 GITHUB_TOKEN Secret" };
  }

  const url = `https://api.github.com/repos/${repo}/issues`;
  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "HoopCoach-Feedback-Worker/1.0",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
  };

  let resp = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify({ title, body, labels }),
  });

  // 若目标仓库尚未创建某些自定义 label 导致 422，自动降级为不带 labels 重试一次
  if (resp.status === 422 && labels && labels.length) {
    resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ title, body }),
    });
  }

  if (!resp.ok) {
    const errTxt = (await resp.text()).slice(0, 300);
    return { ok: false, status: 502, error: `GitHub API 返回 ${resp.status}: ${errTxt}` };
  }

  const created = await resp.json();
  return {
    ok: true,
    status: 201,
    issue_number: created.number,
    issue_url: created.html_url,
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method === "GET" && (path === "/" || path === "/health")) {
      return jsonResp({
        ok: true,
        service: "hoopcoach-feedback-relay",
        repo_configured: Boolean((env.GITHUB_REPO || "").trim()),
        token_configured: Boolean((env.GITHUB_TOKEN || "").trim()),
        app_key_required: Boolean((env.APP_KEY || "").trim()),
      });
    }

    if (request.method !== "POST" || (path !== "/" && path !== "/api/feedback")) {
      return jsonResp({ ok: false, error: "Not Found" }, 404);
    }

    const expectedKey = (env.APP_KEY || "").trim();
    if (expectedKey) {
      const gotKey = (request.headers.get("X-App-Key") || "").trim();
      if (gotKey !== expectedKey) {
        return jsonResp({ ok: false, error: "Unauthorized app key" }, 403);
      }
    }

    const clientIp = request.headers.get("CF-Connecting-IP") || request.headers.get("X-Forwarded-For") || "";
    if (!checkRateLimit(clientIp)) {
      return jsonResp({ ok: false, error: "提交过于频繁，请 1 分钟后再试" }, 429);
    }

    const contentLen = Number(request.headers.get("Content-Length") || "0");
    if (contentLen > MAX_PAYLOAD_BYTES) {
      return jsonResp({ ok: false, error: "提交数据超过大小上限 (120 KB)" }, 413);
    }

    let rawText = "";
    try {
      rawText = await request.text();
    } catch {
      return jsonResp({ ok: false, error: "无法读取请求正文" }, 400);
    }
    if (rawText.length > MAX_PAYLOAD_BYTES) {
      return jsonResp({ ok: false, error: "提交数据超过大小上限 (120 KB)" }, 413);
    }

    let payload;
    try {
      payload = JSON.parse(rawText);
    } catch {
      return jsonResp({ ok: false, error: "请求体不是合法 JSON" }, 400);
    }
    if (!payload || typeof payload !== "object") {
      return jsonResp({ ok: false, error: "无效的请求体" }, 400);
    }

    const catKey = CATEGORY_META[payload.category] ? payload.category : "bug";
    const catMeta = CATEGORY_META[catKey];
    let cleanTitle = clipText(payload.title, 120).replace(/[\r\n]+/g, " ").trim();
    if (!cleanTitle) {
      return jsonResp({ ok: false, error: "请填写简短标题后再提交" }, 400);
    }
    if (!cleanTitle.startsWith("[")) {
      cleanTitle = `${catMeta.prefix} ${cleanTitle}`;
    }

    const body = buildIssueBody(payload, catMeta);
    const res = await createGitHubIssue(env, {
      title: cleanTitle,
      body,
      labels: catMeta.ghLabels,
    });

    if (!res.ok) {
      return jsonResp({ ok: false, error: res.error }, res.status || 502);
    }
    return jsonResp(
      {
        ok: true,
        issue_number: res.issue_number,
        issue_url: res.issue_url,
      },
      201
    );
  },
};
