// ── Utilities ────────────────────────────────────────────────────────────────

function relativeTime(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  const now = new Date();
  const diffMs = d - now;
  const absDiff = Math.abs(diffMs);
  const seconds = Math.floor(absDiff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  let text;
  if (days > 0) text = `${days}d`;
  else if (hours > 0) text = `${hours}h`;
  else if (minutes > 0) text = `${minutes}m`;
  else text = `${seconds}s`;

  return diffMs > 0 ? `in ${text}` : `${text} ago`;
}

function escapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatTimestamp(isoStr) {
  if (!isoStr) return "";
  const d = new Date(isoStr);
  return d.toLocaleString();
}

function uid() {
  return Math.random().toString(36).slice(2, 9);
}

// ── Stats ────────────────────────────────────────────────────────────────────

async function loadStats() {
  try {
    const resp = await fetch("/api/stats");
    const data = await resp.json();
    const el = document.getElementById("nav-stats");
    if (el) {
      el.textContent = `${data.total_messages} msgs | ${data.visible_messages} visible | ${data.active_schedules} active sched | ${data.unique_chats} chats`;
    }
  } catch (e) {
    console.error("Failed to load stats:", e);
  }
}

// ── Messages page ────────────────────────────────────────────────────────────

let currentPage = 1;
let currentChatId = null;

function renderContentBlocks(blocks) {
  let html = "";
  for (const block of blocks) {
    if (block.type === "text") {
      if (!block.text || !block.text.trim()) continue;
      const id = "txt-" + uid();
      const bytes = new Blob([block.text]).size;
      html += `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content open" id="${id}">${escapeHtml(block.text)}</div></div>`;
    } else if (block.type === "tool_use") {
      const id = "tool-" + uid();
      const keys = Object.keys(block.input || {});
      const isSendMessageFinal = block.name === "send_message" && keys.length === 2 && block.input.final === true && typeof block.input.message === "string";
      const singleStr = isSendMessageFinal || (keys.length === 1 && typeof block.input[keys[0]] === "string");
      const inputStr = isSendMessageFinal ? block.input.message : (singleStr ? block.input[keys[0]] : JSON.stringify(block.input, null, 2));
      const headerLabel = isSendMessageFinal ? `${block.name}/message/final` : (singleStr ? `${block.name}/${keys[0]}` : block.name);
      const startOpen = block.name === "send_message";
      const bytes = new Blob([inputStr]).size;
      const skillAttr = block.name === "read_memory" && block.input && block.input.name ? ` data-skill-name="${escapeHtml(block.input.name)}"` : "";
      html += `<div class="tool-use-block"${skillAttr}><div class="tool-use-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow${startOpen ? " open" : ""}" id="arrow-${id}">&#9654;</span> <strong>Tool:</strong> ${escapeHtml(headerLabel)} <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content${startOpen ? " open" : ""}" id="${id}">${escapeHtml(inputStr)}</div></div>`;
    } else if (block.type === "tool_result") {
      const id = "tr-" + uid();
      let content = "";
      if (Array.isArray(block.content)) {
        content = block.content.map(c => c.text || JSON.stringify(c)).join("\n");
      } else {
        content = typeof block.content === "string" ? block.content : JSON.stringify(block.content, null, 2);
      }
      const errClass = block.is_error ? " tool-error" : "";
      const truncated = content;
      const bytes = new Blob([content]).size;
      const isTruncated = content.startsWith("Tool output truncated (");
      const truncLabel = isTruncated ? ' <span style="color:#e8a735;font-weight:bold">[TRUNCATED]</span>' : "";
      html += `<div class="tool-result-block"><div class="tool-result-header${errClass}" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>${block.is_error ? "Error" : "Result"}</strong> <span class="block-size">(${bytes} bytes)</span>${truncLabel}</div><div class="collapsible-content" id="${id}">${escapeHtml(truncated)}</div></div>`;
    } else if (block.type === "thinking") {
      const id = "think-" + uid();
      const text = block.thinking || "";
      const bytes = new Blob([text]).size;
      html += `<div class="thinking-block"><div class="thinking-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>Thinking</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(text)}</div></div>`;
    } else if (block.type === "redacted_thinking") {
      html += `<div class="thinking-block redacted"><strong>Redacted Thinking</strong></div>`;
    } else {
      html += `<div class="content-block">${escapeHtml(JSON.stringify(block))}</div>`;
    }
  }
  return html;
}

function renderMessageParams(meta, turnId) {
  const params = meta.message_params;
  if (!params || !Array.isArray(params)) return escapeHtml(JSON.stringify(meta));

  let html = "";
  let msgIdx = 0;
  for (const msg of params) {
    const content = msg.content;
    if (!content || (Array.isArray(content) && content.length === 0)) {
      msgIdx++;
      continue;
    }

    let bodyHtml = "";
    if (typeof content === "string") {
      if (content.trim()) {
        const id = "txt-" + uid();
        bodyHtml = `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(content)}</div></div>`;
      }
    } else if (Array.isArray(content)) {
      bodyHtml = renderContentBlocks(content);
    }

    if (bodyHtml) {
      html += `<div class="api-message" data-turn-id="${turnId}" data-msg-idx="${msgIdx}">`;
      html += bodyHtml;
      html += `</div>`;
    }
    msgIdx++;
  }
  return html;
}

function renderUsageBadge(meta) {
  const usage = meta && meta.usage;
  if (!usage) return "";
  const cost = usage.estimated_cost_usd;
  const calls = usage.calls;
  if (!calls || !calls.length) return "";
  const totalOut = calls.reduce((s, c) => s + (c.output || 0), 0);
  const totalCached = calls.reduce((s, c) => s + (c.cached_input || 0), 0);
  const totalUncached = calls.reduce((s, c) => s + (c.uncached_input || 0), 0);
  const totalCacheCreate = calls.reduce((s, c) => s + (c.cache_creation || 0), 0);
  const costStr = cost != null ? ` $${cost.toFixed(4)}` : "";
  const parts = [];
  if (totalUncached) parts.push(`${totalUncached} in`);
  if (totalCached) parts.push(`${totalCached} cached`);
  if (totalCacheCreate) parts.push(`${totalCacheCreate} cache_wr`);
  if (totalOut) parts.push(`${totalOut} out`);
  // Tooltip: per-category $ breakdown + per-call details
  const tooltipLines = [];
  const bd = usage.cost_breakdown_usd;
  if (bd) {
    tooltipLines.push(`in: ${totalUncached} tok  $${(bd.uncached_input || 0).toFixed(4)}`);
    tooltipLines.push(`cached: ${totalCached} tok  $${(bd.cached_input || 0).toFixed(4)}`);
    tooltipLines.push(`cache_wr: ${totalCacheCreate} tok  $${(bd.cache_creation || 0).toFixed(4)}`);
    tooltipLines.push(`out: ${totalOut} tok  $${(bd.output || 0).toFixed(4)}`);
  }
  if (calls.length > 1) {
    tooltipLines.push("");
    for (let i = 0; i < calls.length; i++) {
      const c = calls[i];
      const p = [];
      if (c.uncached_input) p.push(`in:${c.uncached_input}`);
      if (c.cached_input) p.push(`cached:${c.cached_input}`);
      if (c.cache_creation) p.push(`cache_wr:${c.cache_creation}`);
      if (c.output) p.push(`out:${c.output}`);
      if (c.cost_usd != null) p.push(`$${c.cost_usd.toFixed(4)}`);
      tooltipLines.push(`call ${i + 1}: ${p.join(", ")}`);
    }
  }
  const subs = usage.subagent_usages;
  if (subs && subs.length) {
    tooltipLines.push("");
    for (let i = 0; i < subs.length; i++) {
      const s = subs[i];
      const model = (s.model || "").replace(/^claude-/, "").replace(/-\d+$/, "");
      const sCost = s.estimated_cost_usd != null ? `$${s.estimated_cost_usd.toFixed(4)}` : "";
      const nCalls = (s.calls || []).length;
      tooltipLines.push(`subagent ${i + 1} (${model}, ${nCalls} calls): ${sCost}`);
    }
  }
  const tooltip = tooltipLines.join("\n");
  return `<span class="badge usage" title="${escapeHtml(tooltip)}">${parts.join(" | ")}${costStr}</span>`;
}

function senderClass(msg) {
  if (msg.user_id === -1) return "bot";
  if (msg.user_id === -2) return "system";
  if (msg.user_id === -3) return "tool";
  return "";
}

async function loadChats() {
  const resp = await fetch("/api/chats");
  const data = await resp.json();
  const select = document.getElementById("chat-filter");
  if (!select) return data.default_chat_id;

  select.innerHTML = "";
  for (const chat of data.chats) {
    const opt = document.createElement("option");
    opt.value = chat.chat_id;
    opt.textContent = `${chat.label} (${chat.msg_count} msgs)`;
    if (chat.chat_id === data.default_chat_id) opt.selected = true;
    select.appendChild(opt);
  }

  return data.default_chat_id;
}

async function loadMessages(page) {
  currentPage = page || 1;
  const search = document.getElementById("search-input")?.value || "";
  const chatId = document.getElementById("chat-filter")?.value || currentChatId;
  const minBytes = document.getElementById("min-bytes-input")?.value || "";

  const params = new URLSearchParams({ page: currentPage, chat_id: chatId });
  if (search) params.set("search", search);
  if (minBytes) params.set("min_bytes", minBytes);

  const container = document.getElementById("turns-container");
  if (!container) return;
  container.innerHTML = `<div class="loading">Loading...</div>`;

  const resp = await fetch("/api/messages?" + params);
  const data = await resp.json();
  container.innerHTML = "";

  for (const msg of data.messages) {
    const card = document.createElement("div");
    card.className = msg.agent_id ? "turn-card subagent" : "turn-card";

    let badges = "";
    if (msg.agent_id) badges += `<span class="badge agent">Agent #${msg.agent_id}</span>`;
    if (msg.has_image) badges += `<span class="badge image">Image</span>`;
    if (msg.marked_for_archive) badges += `<span class="badge archived">Archived</span>`;
    badges += renderUsageBadge(msg.meta);

    const sc = senderClass(msg);
    const turnId = "turn-" + msg.id;

    let rendered;
    if (msg.has_message_params) {
      rendered = renderMessageParams(msg.meta, turnId);
    } else {
      // Non-bot messages: wrap in a single api-message block
      const id = "txt-" + uid();
      rendered = `<div class="api-message" data-turn-id="${turnId}" data-msg-idx="0"><div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(msg.message)}</div></div></div>`;
    }
    const rawJson = escapeHtml(JSON.stringify(msg.api_messages, null, 2));

    card.innerHTML = `
      <div class="turn-header">
        <span class="timestamp">${formatTimestamp(msg.created_at)}</span>
        <span class="sender ${sc}">${escapeHtml(msg.sender)}</span>
        <span class="msg-id">#${msg.id}</span>
        <span class="turn-bytes">${msg.total_bytes}b</span>
        ${badges}
        <button class="view-toggle" onclick="toggleTurnView('${turnId}')">rendered</button>
        <button class="token-btn" id="${turnId}-token-btn" onclick="fetchTokens('${turnId}', ${msg.id})">tokens?</button>
        <span class="token-info" id="${turnId}-token-total"></span>
      </div>
      <div class="turn-body" id="${turnId}-rendered">${rendered}</div>
      <div class="turn-body turn-raw" id="${turnId}-raw" style="display:none">${rawJson}</div>
    `;
    container.appendChild(card);
  }

  let filterInfo = "";
  if (search) filterInfo += ` search="${search}"`;
  if (minBytes) filterInfo += ` min=${minBytes}b`;

  const pagHtml = `
    <button ${data.page <= 1 ? "disabled" : ""} onclick="loadMessages(${data.page - 1})">Prev</button>
    <span class="page-info">Page ${data.page} / ${data.total_pages} (${data.total} turns${filterInfo})</span>
    <button ${data.page >= data.total_pages ? "disabled" : ""} onclick="loadMessages(${data.page + 1})">Next</button>
  `;
  const pt = document.getElementById("pagination-top");
  const pb = document.getElementById("pagination-bottom");
  if (pt) pt.innerHTML = pagHtml;
  if (pb) pb.innerHTML = pagHtml;
}

async function fetchTokens(turnId, dbId) {
  const btn = document.getElementById(turnId + "-token-btn");
  if (btn) { btn.disabled = true; btn.textContent = "..."; }
  try {
    const resp = await fetch(`/api/turn/${dbId}/tokens`);
    const data = await resp.json();

    // Show total in header
    if (btn) btn.style.display = "none";
    const totalEl = document.getElementById(turnId + "-token-total");
    if (totalEl) totalEl.textContent = `${data.total_tokens} tok`;

    // Show per-message counts in block headers of each api-message
    const body = document.getElementById(turnId + "-rendered");
    if (body) {
      const apiMsgs = body.querySelectorAll(".api-message");
      for (const el of apiMsgs) {
        const idx = parseInt(el.dataset.msgIdx);
        const tokData = data.messages[idx];
        if (!tokData || tokData.tokens == null) continue;

        if (tokData.blocks) {
          // Per-block annotation for mixed assistant messages (text + tool_use)
          const headers = el.querySelectorAll(".tool-use-header, .tool-result-header, .text-block-header");
          for (let bi = 0; bi < headers.length && bi < tokData.blocks.length; bi++) {
            const b = tokData.blocks[bi];
            if (!b) continue;  // null = skipped (e.g. tool_use covered by call+result)
            const h = headers[bi];
            if (h.querySelector(".block-tokens")) continue;
            const span = document.createElement("span");
            span.className = "block-tokens";
            span.textContent = b.approx ? `~${b.tokens} tok` : `${b.tokens} tok`;
            h.appendChild(span);
          }
        } else {
          const header = el.querySelector(".tool-use-header, .tool-result-header, .text-block-header");
          if (header && !header.querySelector(".block-tokens")) {
            const span = document.createElement("span");
            span.className = "block-tokens";
            let label = `${tokData.tokens} tok`;
            if (tokData.approx) label = `~${tokData.tokens} tok`;
            if (tokData.pair) label = `${tokData.tokens} tok (call+result)`;
            span.textContent = label;
            header.appendChild(span);
          }
        }
      }
    }
  } catch (e) {
    if (btn) btn.textContent = "err";
  }
}

// ── Schedules page ───────────────────────────────────────────────────────────

async function loadSchedules() {
  const resp = await fetch("/api/schedules");
  const data = await resp.json();
  const container = document.getElementById("schedules-container");
  if (!container) return;

  let html = `<table class="inv-table">
    <thead><tr>
      <th>Status</th><th>ID</th><th>Type</th><th>Spec</th><th>Next Run</th>
      <th>Title</th><th>Context</th>
    </tr></thead><tbody>`;

  for (const s of data.schedules) {
    const cls = s.is_active ? "active" : "inactive";
    const dot = `<span class="status-dot ${cls}"></span>${s.is_active ? "Active" : "Inactive"}`;
    html += `<tr class="${cls}">
      <td>${dot}</td>
      <td>${s.id}</td>
      <td>${escapeHtml(s.schedule_type)}</td>
      <td>${s.schedule_spec ? escapeHtml(s.schedule_spec) : "—"}</td>
      <td>${formatTimestamp(s.next_run_at)}<br><span class="relative-time">${relativeTime(s.next_run_at)}</span></td>
      <td>${escapeHtml(s.title)}</td>
      <td>${s.context ? escapeHtml(s.context) : "—"}</td>
    </tr>`;
  }

  html += "</tbody></table>";
  container.innerHTML = html;
}

// ── Collapsible toggle ──────────────────────────────────────────────────────

function toggleTurnView(turnId) {
  const rendered = document.getElementById(turnId + "-rendered");
  const raw = document.getElementById(turnId + "-raw");
  const btn = rendered.parentElement.querySelector(".view-toggle");
  if (raw.style.display === "none") {
    raw.style.display = "";
    rendered.style.display = "none";
    btn.textContent = "json";
  } else {
    raw.style.display = "none";
    rendered.style.display = "";
    btn.textContent = "rendered";
  }
}

function toggleCollapsible(id) {
  const el = document.getElementById(id);
  const arrow = document.getElementById("arrow-" + id);
  if (el) el.classList.toggle("open");
  if (arrow) arrow.classList.toggle("open");
}

function scrollToSkill(name) {
  const el = document.querySelector(`[data-skill-name="${name}"]`);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.style.outline = "2px solid #e05068";
    setTimeout(() => el.style.outline = "", 1500);
  }
}

// ── Agent view page ─────────────────────────────────────────────────────────

function renderSystemPrompt(text) {
  const sysBytes = new Blob([text]).size;
  const outerId = "agent-system-" + uid();

  // Find all skill boundaries
  const skillRegex = /^Content of skill (\S+) - read from .+$/mg;
  const matches = [];
  let m;
  while ((m = skillRegex.exec(text)) !== null) {
    matches.push({ index: m.index, name: m[1] });
  }

  // If no skills found, render as single block
  if (matches.length === 0) {
    return `<div class="turn-card"><div class="turn-header" id="system-prompt-header"><span class="sender system">System Prompt</span><span class="turn-bytes">${sysBytes}b</span><span class="toggle-arrow open" id="arrow-${outerId}" onclick="toggleCollapsible('${outerId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${outerId}" style="max-height:800px">${escapeHtml(text)}</div></div>`;
  }

  // Find where skills section ends (e.g. "Available Knowledge Files" or other section headers)
  const lastSkillMatch = matches[matches.length - 1];
  const postSkillRegex = /^\n*===\s/m;
  const afterLastSkill = text.slice(lastSkillMatch.index);
  // Search for a section header after the first line of the last skill
  const firstNewline = afterLastSkill.indexOf("\n");
  const postMatch = firstNewline >= 0 ? postSkillRegex.exec(afterLastSkill.slice(firstNewline)) : null;
  const skillsEndIndex = postMatch ? lastSkillMatch.index + firstNewline + postMatch.index : text.length;

  // Split into parts: text before first skill, each skill, then trailing text
  const parts = [];
  if (matches[0].index > 0) {
    parts.push({ type: "text", content: text.slice(0, matches[0].index) });
  }
  for (let i = 0; i < matches.length; i++) {
    const end = i + 1 < matches.length ? matches[i + 1].index : skillsEndIndex;
    parts.push({ type: "skill", name: matches[i].name, content: text.slice(matches[i].index, end) });
  }
  if (skillsEndIndex < text.length) {
    parts.push({ type: "text", content: text.slice(skillsEndIndex) });
  }

  let html = `<div class="turn-card"><div class="turn-header" id="system-prompt-header"><span class="sender system">System Prompt</span><span class="turn-bytes">${sysBytes}b</span><span class="toggle-arrow open" id="arrow-${outerId}" onclick="toggleCollapsible('${outerId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${outerId}" style="max-height:none">`;

  for (const part of parts) {
    if (part.type === "skill") {
      const id = "skill-" + uid();
      const bytes = new Blob([part.content]).size;
      html += `<div class="tool-use-block" data-skill-name="${escapeHtml(part.name)}"><div class="tool-use-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>Skill: ${escapeHtml(part.name)}</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(part.content)}</div></div>`;
    } else {
      const id = "systxt-" + uid();
      const bytes = new Blob([part.content]).size;
      html += `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>Text</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(part.content)}</div></div>`;
    }
  }

  html += `</div></div>`;
  return html;
}

async function fetchAgentTokens() {
  const btn = document.getElementById("agent-token-btn");
  const info = document.getElementById("agent-token-info");
  if (btn) { btn.disabled = true; btn.textContent = "counting..."; }
  try {
    const resp = await fetch("/api/agent-view/tokens");
    const data = await resp.json();
    if (btn) btn.style.display = "none";
    if (info) info.textContent = `${data.total_tokens} tok`;

    // Show system tokens in the system prompt header
    const sysHeader = document.getElementById("system-prompt-header");
    if (sysHeader && !sysHeader.querySelector(".token-info")) {
      const span = document.createElement("span");
      span.className = "token-info";
      const toolInfo = data.tool_tokens ? ` + ${data.tool_tokens} tools (${data.num_tools})` : "";
      span.textContent = `${data.system_tokens} tok${toolInfo}`;
      sysHeader.appendChild(span);
    }

    // Annotate per-message token counts
    const container = document.getElementById("agent-container");
    if (container && data.messages) {
      const apiMsgs = container.querySelectorAll(".api-message[data-agent-idx]");
      for (const el of apiMsgs) {
        const idx = parseInt(el.dataset.agentIdx);
        const tokData = data.messages[idx];
        if (!tokData || tokData.tokens == null) continue;
        const header = el.querySelector(".tool-use-header, .tool-result-header, .text-block-header");
        if (header && !header.querySelector(".block-tokens")) {
          const span = document.createElement("span");
          span.className = "block-tokens";
          let label = `${tokData.tokens} tok`;
          if (tokData.approx) label = `~${tokData.tokens} tok`;
          if (tokData.pair) label = `${tokData.tokens} tok (call+result)`;
          span.textContent = label;
          header.appendChild(span);
        }
      }

      // Show turn totals in turn headers
      const turnCards = container.querySelectorAll(".turn-card[data-agent-end]");
      for (const card of turnCards) {
        const start = parseInt(card.dataset.agentIdx);
        const end = parseInt(card.dataset.agentEnd);
        let turnTotal = 0;
        let hasAny = false;
        for (let idx = start; idx < end; idx++) {
          const t = data.messages[idx];
          if (t && t.tokens != null) { turnTotal += t.tokens; hasAny = true; }
        }
        if (hasAny) {
          const header = card.querySelector(".turn-header");
          if (header && !header.querySelector(".token-info")) {
            const span = document.createElement("span");
            span.className = "token-info";
            span.textContent = `${turnTotal > 2000 ? "⚠️ " : ""}${turnTotal} tok`;
            header.appendChild(span);
          }
        }
      }
    }
  } catch (e) {
    if (btn) btn.textContent = "err";
    console.error("Failed to fetch agent tokens:", e);
  }
}

async function loadAgentView() {
  const container = document.getElementById("agent-container");
  const loading = document.getElementById("agent-loading");
  const btn = document.getElementById("load-agent-btn");
  if (!container) return;

  if (btn) btn.disabled = true;
  if (loading) loading.style.display = "";

  try {
    const resp = await fetch("/api/agent-view");
    const data = await resp.json();
    if (loading) loading.style.display = "none";

    let html = "";

    // Stats
    html += `<div class="agent-stats">${data.num_db_turns} DB turns → ${data.num_messages} API messages</div>`;

    // System prompt — split into skill sub-blocks
    html += renderSystemPrompt(data.system_prompt);

    // Collect loaded skills: autoloaded from system prompt + on-demand from read_memory calls
    const autoloadedSkills = [];
    const skillRegex2 = /^Content of skill (\S+) - read from .+$/mg;
    let sm;
    while ((sm = skillRegex2.exec(data.system_prompt)) !== null) {
      autoloadedSkills.push(sm[1]);
    }
    const onDemandSkills = [];
    for (const msg of data.history) {
      if (!Array.isArray(msg.content)) continue;
      for (const block of msg.content) {
        if (block.type === "tool_use" && block.name === "read_memory" && block.input && block.input.name) {
          onDemandSkills.push(block.input.name);
        }
      }
    }
    if (autoloadedSkills.length > 0 || onDemandSkills.length > 0) {
      let skillsHtml = '<div class="skills-summary"><strong>Skills:</strong> ';
      for (const s of autoloadedSkills) {
        skillsHtml += `<span class="skill-badge autoload" onclick="scrollToSkill('${escapeHtml(s)}')">${escapeHtml(s)}</span> `;
      }
      for (const s of onDemandSkills) {
        skillsHtml += `<span class="skill-badge on-demand" onclick="scrollToSkill('${escapeHtml(s)}')">${escapeHtml(s)}</span> `;
      }
      skillsHtml += '</div>';
      html += skillsHtml;
    }

    // Tools list
    if (data.tools && data.tools.length > 0) {
      let toolsHtml = '<div class="skills-summary"><strong>Tools (' + data.tools.length + '):</strong> ';
      for (const t of data.tools) {
        toolsHtml += `<span class="skill-badge tool">${escapeHtml(t)}</span> `;
      }
      toolsHtml += '</div>';
      html += toolsHtml;
    }

    // Message history — group into turn-cards like messages page
    // Each turn-card contains consecutive messages of compatible roles
    // (assistant messages until a user message, then user message)
    let i = 0;
    while (i < data.history.length) {
      const startIdx = i;
      const firstRole = data.history[i].role;
      // Collect consecutive messages that belong together
      // assistant + user(tool_result) pairs stay together, otherwise break on role change
      let endIdx = i + 1;
      if (firstRole === "assistant") {
        while (endIdx < data.history.length) {
          const msg = data.history[endIdx];
          if (msg.role === "user") {
            // Check if it's a tool_result (part of the same exchange)
            const content = msg.content;
            const isToolResult = Array.isArray(content) && content.length > 0 &&
              content.every(b => b.type === "tool_result");
            if (isToolResult) { endIdx++; continue; }
          }
          if (msg.role === "assistant" && endIdx > startIdx + 1) { endIdx++; continue; }
          break;
        }
      } else {
        endIdx = i + 1;
      }

      const isSystemMsg = firstRole === "user" && typeof data.history[startIdx].content === "string" && data.history[startIdx].content.startsWith("<system>System message");
      const sc = firstRole === "assistant" ? "bot" : (isSystemMsg ? "system" : "");
      let bodyHtml = "";
      for (let j = startIdx; j < endIdx; j++) {
        const msg = data.history[j];
        let msgBody = "";
        if (typeof msg.content === "string") {
          const id = "txt-" + uid();
          const bytes = new Blob([msg.content]).size;
          msgBody = `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content open" id="${id}">${escapeHtml(msg.content)}</div></div>`;
        } else if (Array.isArray(msg.content)) {
          msgBody = renderContentBlocks(msg.content);
        }
        if (msgBody) {
          bodyHtml += `<div class="api-message" data-agent-idx="${j}">${msgBody}</div>`;
        }
      }

      if (bodyHtml) {
        const rangeLabel = startIdx === endIdx - 1 ? `#${startIdx}` : `#${startIdx}-${endIdx - 1}`;
        const roleLabel = isSystemMsg ? "system" : firstRole;
        html += `<div class="turn-card" data-agent-idx="${startIdx}" data-agent-end="${endIdx}"><div class="turn-header"><span class="msg-id">${rangeLabel}</span><span class="sender ${sc}">${escapeHtml(roleLabel)}</span></div><div class="turn-body">${bodyHtml}</div></div>`;
      }
      i = endIdx;
    }

    container.innerHTML = html;
  } catch (e) {
    if (loading) loading.textContent = "Error: " + e.message;
    console.error("Failed to load agent view:", e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  loadStats();

  if (document.getElementById("turns-container")) {
    currentChatId = await loadChats();
    loadMessages(1);

    document.getElementById("search-btn")?.addEventListener("click", () => loadMessages(1));
    document.getElementById("search-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadMessages(1);
    });
    document.getElementById("min-bytes-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadMessages(1);
    });
    document.getElementById("chat-filter")?.addEventListener("change", () => loadMessages(1));
  }

  if (document.getElementById("schedules-container")) {
    loadSchedules();
  }

  if (document.getElementById("agent-container")) {
    loadAgentView();
  }
});
