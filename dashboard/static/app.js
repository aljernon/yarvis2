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
      el.textContent = `${data.total_messages} msgs | ${data.visible_messages} visible | ${data.active_invocations} active inv | ${data.unique_chats} chats`;
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
      const singleStr = keys.length === 1 && typeof block.input[keys[0]] === "string";
      const inputStr = singleStr ? block.input[keys[0]] : JSON.stringify(block.input, null, 2);
      const headerLabel = singleStr ? `${block.name}/${keys[0]}` : block.name;
      const startOpen = block.name === "send_message";
      const bytes = new Blob([inputStr]).size;
      html += `<div class="tool-use-block"><div class="tool-use-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow${startOpen ? " open" : ""}" id="arrow-${id}">&#9654;</span> <strong>Tool:</strong> ${escapeHtml(headerLabel)} <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content${startOpen ? " open" : ""}" id="${id}">${escapeHtml(inputStr)}</div></div>`;
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
      html += `<div class="tool-result-block"><div class="tool-result-header${errClass}" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>${block.is_error ? "Error" : "Result"}</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(truncated)}</div></div>`;
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
    card.className = "turn-card";

    let badges = "";
    if (msg.has_image) badges += `<span class="badge image">Image</span>`;
    if (msg.marked_for_archive) badges += `<span class="badge archived">Archived</span>`;

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

    // Show per-message counts in the first block header of each api-message
    const body = document.getElementById(turnId + "-rendered");
    if (body) {
      const apiMsgs = body.querySelectorAll(".api-message");
      for (const el of apiMsgs) {
        const idx = parseInt(el.dataset.msgIdx);
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
    }
  } catch (e) {
    if (btn) btn.textContent = "err";
  }
}

// ── Invocations page ─────────────────────────────────────────────────────────

async function loadInvocations() {
  const resp = await fetch("/api/invocations");
  const data = await resp.json();
  const container = document.getElementById("invocations-container");
  if (!container) return;

  let html = `<table class="inv-table">
    <thead><tr>
      <th>Status</th><th>ID</th><th>Chat</th><th>Scheduled</th>
      <th>Recurring</th><th>Reason</th><th>Meta</th>
    </tr></thead><tbody>`;

  for (const inv of data.invocations) {
    const cls = inv.is_active ? "active" : "inactive";
    const dot = `<span class="status-dot ${cls}"></span>${inv.is_active ? "Active" : "Inactive"}`;
    html += `<tr class="${cls}">
      <td>${dot}</td>
      <td>${inv.id}</td>
      <td>${inv.chat_id}</td>
      <td>${formatTimestamp(inv.scheduled_at)}<br><span class="relative-time">${relativeTime(inv.scheduled_at)}</span></td>
      <td>${inv.is_recurring ? "Yes" : "No"}</td>
      <td>${escapeHtml(inv.reason)}</td>
      <td><div class="meta-json">${escapeHtml(JSON.stringify(inv.meta, null, 2))}</div></td>
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

  // Split into parts: text before first skill, then each skill until next
  const parts = [];
  if (matches[0].index > 0) {
    parts.push({ type: "text", content: text.slice(0, matches[0].index) });
  }
  for (let i = 0; i < matches.length; i++) {
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    parts.push({ type: "skill", name: matches[i].name, content: text.slice(matches[i].index, end) });
  }

  let html = `<div class="turn-card"><div class="turn-header" id="system-prompt-header"><span class="sender system">System Prompt</span><span class="turn-bytes">${sysBytes}b</span><span class="toggle-arrow open" id="arrow-${outerId}" onclick="toggleCollapsible('${outerId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${outerId}" style="max-height:none">`;

  for (const part of parts) {
    if (part.type === "skill") {
      const id = "skill-" + uid();
      const bytes = new Blob([part.content]).size;
      html += `<div class="tool-use-block"><div class="tool-use-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>Skill: ${escapeHtml(part.name)}</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(part.content)}</div></div>`;
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
      span.textContent = `${data.system_tokens} tok`;
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
            span.textContent = `${turnTotal} tok`;
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

  if (document.getElementById("invocations-container")) {
    loadInvocations();
  }

  if (document.getElementById("agent-container")) {
    loadAgentView();
  }
});
