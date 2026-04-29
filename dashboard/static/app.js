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

function formatToolInput(input) {
  const keys = Object.keys(input || {});
  if (keys.length === 0) return "";
  const parts = [];
  for (const key of keys) {
    const val = input[key];
    const strVal = typeof val === "string" ? val : JSON.stringify(val, null, 2);
    const isLong = strVal.length > 80 || strVal.includes("\n");
    if (isLong) {
      parts.push(`<span class="tool-arg-name">${escapeHtml(key)}</span>\n${escapeHtml(strVal)}`);
    } else {
      parts.push(`<span class="tool-arg-name">${escapeHtml(key)}:</span> ${escapeHtml(strVal)}`);
    }
  }
  return parts.join("\n");
}

function renderContentBlocks(blocks, dbMsgId) {
  let html = "";
  for (const block of blocks) {
    if (block.type === "image") {
      if (dbMsgId) {
        html += `<div class="image-block"><a href="/api/message/${dbMsgId}/image" target="_blank"><img src="/api/message/${dbMsgId}/image" alt="Image" loading="lazy"></a></div>`;
      } else {
        html += `<div class="image-block" style="color:red;font-weight:bold">[BUG: image block without dbMsgId]</div>`;
      }
      continue;
    }
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
      const startOpen = block.name === "send_message";
      const skillAttr = block.name === "read_skill" && block.input && block.input.name ? ` data-skill-name="${escapeHtml(block.input.name)}"` : "";
      let headerLabel, bodyHtml, inputStr;
      if (singleStr) {
        // Single string arg: arg name in header, plain value in body
        inputStr = isSendMessageFinal ? block.input.message : block.input[keys[0]];
        headerLabel = isSendMessageFinal ? `${block.name}/message/final` : `${block.name}/${keys[0]}`;
        bodyHtml = escapeHtml(inputStr);
      } else {
        // Multiple args: custom rendering with bold arg names
        headerLabel = block.name;
        bodyHtml = formatToolInput(block.input);
        inputStr = JSON.stringify(block.input);
      }
      const bytes = new Blob([inputStr]).size;
      html += `<div class="tool-use-block"${skillAttr}><div class="tool-use-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow${startOpen ? " open" : ""}" id="arrow-${id}">&#9654;</span> <strong>Tool:</strong> ${escapeHtml(headerLabel)} <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content${startOpen ? " open" : ""}" id="${id}">${bodyHtml}</div></div>`;
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

function renderSubagentGroup(group) {
  const collapseId = "subagent-" + uid();
  const slug = escapeHtml(group.agent_slug || `Agent #${group.agent_id}`);
  const timeRange = `${formatTimestamp(group.first_time)} – ${formatTimestamp(group.last_time)}`;

  let bodyHtml = "";
  // Group subagent messages into turn cards (same logic as main view)
  let si = 0;
  while (si < group.history.length) {
    const startIdx = si;
    const firstRole = group.history[si].role;
    let endIdx = si + 1;
    if (firstRole === "assistant") {
      while (endIdx < group.history.length) {
        const msg = group.history[endIdx];
        if (msg.role === "user") {
          const content = msg.content;
          const isToolResult = Array.isArray(content) && content.length > 0 &&
            content.some(b => b.type === "tool_result");
          if (isToolResult) { endIdx++; continue; }
        }
        if (msg.role === "assistant" && endIdx > startIdx + 1) { endIdx++; continue; }
        break;
      }
    }
    const sc = firstRole === "assistant" ? "bot" : "";
    let turnBody = "";
    for (let j = startIdx; j < endIdx; j++) {
      const msg = group.history[j];
      let msgBody = "";
      if (typeof msg.content === "string") {
        const id = "txt-" + uid();
        msgBody = `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(msg.content)}</div></div>`;
      } else if (Array.isArray(msg.content)) {
        msgBody = renderContentBlocks(msg.content);
      }
      if (msgBody) turnBody += `<div class="api-message">${msgBody}</div>`;
    }
    if (turnBody) {
      const roleLabel = firstRole;
      const turnUsage = findUsageForRange(group.turn_usages || [], startIdx, endIdx);
      const usageBadge = turnUsage ? renderUsageBadge({usage: turnUsage}) : "";
      bodyHtml += `<div class="turn-card"><div class="turn-header"><span class="sender ${sc}">${escapeHtml(roleLabel)}</span>${usageBadge}</div><div class="turn-body">${turnBody}</div></div>`;
    }
    si = endIdx;
  }

  // Aggregate group-level cost: sum estimated_cost_usd across all turn_usages.
  const groupCost = (group.turn_usages || []).reduce(
    (s, tu) => s + (tu.usage && tu.usage.estimated_cost_usd ? tu.usage.estimated_cost_usd : 0),
    0,
  );
  const groupCostStr = groupCost > 0 ? ` <span class="block-size">$${groupCost.toFixed(4)}</span>` : "";

  const slugLink = `<a href="/agent?agent_id=${group.agent_id}" class="badge agent" onclick="event.stopPropagation()">${slug}</a>`;
  return `<div class="subagent-group"><div class="subagent-group-header" onclick="toggleCollapsible('${collapseId}')"><span class="toggle-arrow open" id="arrow-${collapseId}">&#9654;</span> ${slugLink} <span class="subagent-time">${timeRange}</span> <span class="block-size">(${group.num_db_turns} DB turns, ${group.num_messages} API msgs)</span>${groupCostStr}</div><div class="collapsible-content open" id="${collapseId}">${bodyHtml}</div></div>`;
}

function shortModelName(model) {
  return (model || "").replace(/^claude-/, "").replace(/-\d{8}$/, "");
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
  const model = shortModelName(usage.model);
  const modelStr = model ? ` ${model}` : "";
  const parts = [];
  if (totalUncached) parts.push(`${totalUncached} in`);
  if (totalCached) parts.push(`${totalCached} cached`);
  if (totalCacheCreate) parts.push(`${totalCacheCreate} cache_wr`);
  if (totalOut) parts.push(`${totalOut} out`);
  // Tooltip: model + per-category $ breakdown + per-call details
  const tooltipLines = [];
  if (usage.model) tooltipLines.push(`model: ${usage.model}`);
  const bd = usage.cost_breakdown_usd;
  if (bd) {
    if (usage.model) tooltipLines.push("");
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
      const sModel = shortModelName(s.model);
      const sCost = s.estimated_cost_usd != null ? `$${s.estimated_cost_usd.toFixed(4)}` : "";
      const nCalls = (s.calls || []).length;
      tooltipLines.push(`subagent ${i + 1} (${sModel}, ${nCalls} calls): ${sCost}`);
    }
  }
  const tooltip = tooltipLines.join("\n");
  return `<span class="badge usage" title="${escapeHtml(tooltip)}">${parts.join(" | ")}${costStr}${modelStr}</span>`;
}

function findUsageForRange(turnUsages, startIdx, endIdx) {
  if (!turnUsages) return null;
  for (const tu of turnUsages) {
    // Check if this turn's API range overlaps with the card's range
    if (tu.api_start < endIdx && tu.api_end > startIdx) return tu.usage;
  }
  return null;
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
    card.className = msg.is_hidden_auto_message ? "turn-card ghost-msg" : (msg.agent_id ? "turn-card subagent" : "turn-card");

    let badges = "";
    if (msg.agent_id) badges += `<a href="/agent?agent_id=${msg.agent_id}" class="badge agent" title="View agent config & history">${msg.agent_slug || 'Agent #' + msg.agent_id}</a>`;
    if (msg.has_image) badges += `<span class="badge image">Image</span>`;
    if (msg.marked_for_archive) badges += `<span class="badge archived">Archived</span>`;
    badges += renderUsageBadge(msg.meta);

    const sc = senderClass(msg);
    const turnTypeEmoji = {"notification": "\uD83D\uDD35", "schedule": "\uD83D\uDCC5", "reflection": "\uD83D\uDD04"};
    let senderLabel = msg.sender;
    if (msg.is_hidden_auto_message) {
      senderLabel = "\uD83D\uDC7B " + senderLabel;
    }
    if (msg.user_id === -2 && msg.meta?.turn_type) {
      senderLabel += " " + (turnTypeEmoji[msg.meta.turn_type] || "\uD83D\uDCCC " + msg.meta.turn_type);
    }
    const turnId = "turn-" + msg.id;

    let rendered;
    if (msg.has_message_params) {
      rendered = renderMessageParams(msg.meta, turnId);
    } else {
      // Non-bot messages: render from api_messages to include <meta> tags
      const apiMsg = msg.api_messages?.[0];
      const content = apiMsg?.content;
      if (Array.isArray(content)) {
        rendered = `<div class="api-message" data-turn-id="${turnId}" data-msg-idx="0">${renderContentBlocks(content, msg.id)}</div>`;
      } else if (typeof content === "string" && content.trim()) {
        const id = "txt-" + uid();
        rendered = `<div class="api-message" data-turn-id="${turnId}" data-msg-idx="0"><div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(content)}</div></div></div>`;
      } else {
        const id = "txt-" + uid();
        rendered = `<div class="api-message" data-turn-id="${turnId}" data-msg-idx="0"><div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(msg.message)}</div></div></div>`;
      }
    }
    const rawJson = escapeHtml(JSON.stringify(msg.api_messages, null, 2));

    card.innerHTML = `
      <div class="turn-header">
        <span class="timestamp">${formatTimestamp(msg.created_at)}</span>
        <span class="sender ${sc}">${escapeHtml(senderLabel)}</span>
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
      <th>Title</th><th>Subagent</th>
    </tr></thead><tbody>`;

  for (const s of data.schedules) {
    const cls = s.is_active ? "active" : "inactive";
    const dot = `<span class="status-dot ${cls}"></span>${s.is_active ? "Active" : "Inactive"}`;
    html += `<tr class="${cls} schedule-main">
      <td>${dot}</td>
      <td>${s.id}</td>
      <td>${escapeHtml(s.schedule_type)}</td>
      <td>${s.schedule_spec ? escapeHtml(s.schedule_spec) : "—"}</td>
      <td>${formatTimestamp(s.next_run_at)}<br><span class="relative-time">${relativeTime(s.next_run_at)}</span></td>
      <td>${escapeHtml(s.title)}</td>
      <td>${s.run_in_subagent ? "🧵" : "⚡"}</td>
    </tr>`;
    if (s.context) {
      html += `<tr class="${cls} schedule-context">
        <td colspan="7" style="white-space:pre-wrap;padding-left:2em;opacity:0.85">${escapeHtml(s.context).replace(/\\n/g, "\n")}</td>
      </tr>`;
    }
  }

  html += "</tbody></table>";
  container.innerHTML = html;
}

// ── Locations page ──────────────────────────────────────────────────────────

async function loadLocations() {
  const resp = await fetch("/api/locations");
  const data = await resp.json();
  const container = document.getElementById("locations-container");
  if (!container) return;

  let html = `<table class="inv-table">
    <thead><tr>
      <th>When</th><th>Location</th><th>Coords</th>
      <th>Acc (m)</th><th>Batt</th><th>Type/tid</th><th></th>
    </tr></thead><tbody>`;

  for (const l of data.locations) {
    const acc = l.acc != null ? l.acc.toFixed(0) : "—";
    const batt = l.batt != null ? l.batt.toFixed(0) + "%" : "—";
    html += `<tr id="loc-row-${l.id}">
      <td>${formatTimestamp(l.tst)}<br><span class="relative-time">${relativeTime(l.tst)}</span></td>
      <td style="white-space:pre-wrap">${escapeHtml(l.formatted || "")}</td>
      <td>${l.lat.toFixed(6)},<br>${l.lon.toFixed(6)}</td>
      <td>${acc}</td>
      <td>${batt}</td>
      <td>${escapeHtml(l.event_type || "")}/${escapeHtml(l.tid || "")}</td>
      <td><button class="resolve-btn" onclick="resolveLocation(${l.id})">raw</button></td>
    </tr>
    <tr id="loc-detail-${l.id}" style="display:none"><td colspan="7"><pre id="loc-detail-body-${l.id}" style="white-space:pre-wrap;max-height:400px;overflow:auto;margin:0;padding:0.5em;background:#f6f8fa;font-size:11px"></pre></td></tr>`;
  }

  html += "</tbody></table>";
  container.innerHTML = html;
}

async function resolveLocation(locId) {
  const row = document.getElementById(`loc-detail-${locId}`);
  const body = document.getElementById(`loc-detail-body-${locId}`);
  if (!row || !body) return;
  row.style.display = "";
  body.textContent = "…";
  try {
    const resp = await fetch(`/api/locations/${locId}/resolve`, { method: "POST" });
    const data = await resp.json();
    body.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    body.textContent = `⚠ ${e.message}`;
  }
}

// ── Collapsible toggle ──────────────────────────────────────────────────────

function toggleTurnView(turnId) {
  const rendered = document.getElementById(turnId + "-rendered");
  const raw = document.getElementById(turnId + "-raw");
  const full = document.getElementById(turnId + "-full");
  const card = rendered.parentElement;
  const btn = card.querySelector(".view-toggle:not(.full-toggle)");
  const fullBtn = card.querySelector(".full-toggle");
  if (raw.style.display === "none") {
    raw.style.display = "";
    rendered.style.display = "none";
    if (full) full.style.display = "none";
    btn.textContent = "json";
    if (fullBtn) fullBtn.textContent = "show full";
  } else {
    raw.style.display = "none";
    rendered.style.display = "";
    if (full) full.style.display = "none";
    btn.textContent = "rendered";
    if (fullBtn) fullBtn.textContent = "show full";
  }
}

function toggleTurnFull(turnId) {
  const filtered = document.getElementById(turnId + "-rendered");
  const full = document.getElementById(turnId + "-full");
  const raw = document.getElementById(turnId + "-raw");
  if (!filtered || !full) return;
  const card = filtered.parentElement;
  const btn = card.querySelector(".full-toggle");
  const viewBtn = card.querySelector(".view-toggle:not(.full-toggle)");
  const showingFull = full.style.display !== "none";
  if (showingFull) {
    full.style.display = "none";
    filtered.style.display = "";
    if (raw) raw.style.display = "none";
    if (btn) btn.textContent = "show full";
    if (viewBtn) viewBtn.textContent = "rendered";
  } else {
    full.style.display = "";
    filtered.style.display = "none";
    if (raw) raw.style.display = "none";
    if (btn) btn.textContent = "show filtered";
    if (viewBtn) viewBtn.textContent = "rendered";
  }
}

function renderApiMessagesBody(messages, dbId) {
  let bodyHtml = "";
  for (const msg of messages) {
    let msgBody = "";
    if (typeof msg.content === "string") {
      const id = "txt-" + uid();
      const bytes = new Blob([msg.content]).size;
      msgBody = `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content open" id="${id}">${escapeHtml(msg.content)}</div></div>`;
    } else if (Array.isArray(msg.content)) {
      msgBody = renderContentBlocks(msg.content, dbId);
      if (!msgBody && msg.content.length === 0) {
        msgBody = `<div class="text-block"><em>(empty content)</em></div>`;
      }
    }
    if (msgBody) {
      bodyHtml += `<div class="api-message">${msgBody}</div>`;
    }
  }
  return bodyHtml;
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

  // Find all === Section === boundaries (workspace includes)
  const sectionRegex = /^=== (.+?) ===$/mg;
  const matches = [];
  let m;
  while ((m = sectionRegex.exec(text)) !== null) {
    matches.push({ index: m.index, name: m[1] });
  }

  // If no sections found, render as single block
  if (matches.length === 0) {
    return `<div class="turn-card"><div class="turn-header" id="system-prompt-header"><span class="sender system">System Prompt</span><span class="turn-bytes">${sysBytes}b</span><span class="toggle-arrow open" id="arrow-${outerId}" onclick="toggleCollapsible('${outerId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${outerId}" style="max-height:800px">${escapeHtml(text)}</div></div>`;
  }

  // Split into parts: text before first section, then each section
  const parts = [];
  if (matches[0].index > 0) {
    parts.push({ type: "text", name: "Base Prompt", content: text.slice(0, matches[0].index) });
  }
  for (let i = 0; i < matches.length; i++) {
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    parts.push({ type: "section", name: matches[i].name, content: text.slice(matches[i].index, end) });
  }

  let html = `<div class="turn-card"><div class="turn-header" id="system-prompt-header"><span class="sender system">System Prompt</span><span class="turn-bytes">${sysBytes}b</span><span class="toggle-arrow open" id="arrow-${outerId}" onclick="toggleCollapsible('${outerId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${outerId}" style="max-height:none">`;

  for (const part of parts) {
    const id = (part.type === "section" ? "section-" : "systxt-") + uid();
    const bytes = new Blob([part.content]).size;
    const label = escapeHtml(part.name);
    html += `<div class="text-block" data-skill-name="${label}"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow" id="arrow-${id}">&#9654;</span> <strong>${label}</strong> <span class="block-size">(${bytes} bytes)</span></div><div class="collapsible-content" id="${id}">${escapeHtml(part.content)}</div></div>`;
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

let agentViewFull = false;
let currentSubagentId = null;

function toggleFullHistory() {
  agentViewFull = !agentViewFull;
  const btn = document.getElementById("agent-full-btn");
  if (btn) btn.textContent = agentViewFull ? "show with forget_above" : "show full history";
  if (currentSubagentId != null) {
    loadSubagentView(currentSubagentId);
  } else {
    loadAgentView();
  }
}

async function loadAgentView() {
  currentSubagentId = null;
  const container = document.getElementById("agent-container");
  const loading = document.getElementById("agent-loading");
  const btn = document.getElementById("load-agent-btn");
  if (!container) return;

  if (btn) btn.disabled = true;
  if (loading) loading.style.display = "";

  try {
    const url = agentViewFull ? "/api/agent-view?full=1" : "/api/agent-view";
    const resp = await fetch(url);
    const data = await resp.json();
    if (loading) loading.style.display = "none";

    // Set default model in dropdown
    const modelSelect = document.getElementById("chat-model");
    if (modelSelect) modelSelect.value = "opus";

    let html = "";

    // Stats
    html += `<div class="agent-stats">${data.num_db_turns} DB turns → ${data.num_messages} API messages</div>`;

    // System prompt — split into skill sub-blocks
    html += renderSystemPrompt(data.system_prompt);

    // Collect workspace includes from system prompt + on-demand skills from read_skill calls
    const workspaceIncludes = [];
    const sectionRegex2 = /^=== (.+?) ===$/mg;
    let sm;
    while ((sm = sectionRegex2.exec(data.system_prompt)) !== null) {
      workspaceIncludes.push(sm[1]);
    }
    const onDemandSkills = [];
    for (const msg of data.history) {
      if (!Array.isArray(msg.content)) continue;
      for (const block of msg.content) {
        if (block.type === "tool_use" && block.name === "read_skill" && block.input && block.input.name) {
          onDemandSkills.push(block.input.name);
        }
      }
    }
    if (workspaceIncludes.length > 0 || onDemandSkills.length > 0) {
      let skillsHtml = '<div class="skills-summary">';
      if (workspaceIncludes.length > 0) {
        skillsHtml += '<strong>Includes:</strong> ';
        for (const s of workspaceIncludes) {
          skillsHtml += `<span class="skill-badge autoload" onclick="scrollToSkill('${escapeHtml(s)}')">${escapeHtml(s)}</span> `;
        }
      }
      if (onDemandSkills.length > 0) {
        skillsHtml += '<strong>Skills:</strong> ';
        for (const s of onDemandSkills) {
          skillsHtml += `<span class="skill-badge on-demand" onclick="scrollToSkill('${escapeHtml(s)}')">${escapeHtml(s)}</span> `;
        }
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

    // Prepare subagent groups sorted by last_time for chronological interleaving
    // (position where the subagent finished, not where it started)
    const subagentGroups = (data.subagent_groups || []).slice().sort((a, b) =>
      a.last_time.localeCompare(b.last_time)
    );
    let nextSubagentIdx = 0;

    // Helper: render all subagent groups whose last_time <= given timestamp
    function flushSubagents(beforeTime) {
      let out = "";
      while (nextSubagentIdx < subagentGroups.length &&
             (!beforeTime || subagentGroups[nextSubagentIdx].last_time <= beforeTime)) {
        out += renderSubagentGroup(subagentGroups[nextSubagentIdx]);
        nextSubagentIdx++;
      }
      return out;
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
            // Check if it contains a tool_result (part of the same exchange)
            // Use some() not every() — system hints (text blocks) may accompany tool_results
            const content = msg.content;
            const isToolResult = Array.isArray(content) && content.length > 0 &&
              content.some(b => b.type === "tool_result");
            if (isToolResult) { endIdx++; continue; }
          }
          if (msg.role === "assistant" && endIdx > startIdx + 1) { endIdx++; continue; }
          break;
        }
      } else {
        endIdx = i + 1;
      }

      // Insert any subagent groups that occurred before this turn
      const turnTime = data.db_times ? data.db_times[startIdx] : null;
      if (turnTime) html += flushSubagents(turnTime);

      const isSystemMsg = firstRole === "user" && typeof data.history[startIdx].content === "string" && data.history[startIdx].content.startsWith("<system>System message");
      // Detect agent-to-agent messages: <meta type="message" sender_type="agent" sender_name="..." target="...">
      const firstContent = data.history[startIdx].content;
      const firstText = firstRole === "user" && Array.isArray(firstContent) && firstContent.length > 0 &&
        firstContent[0].type === "text" ? firstContent[0].text : "";
      const agentMetaMatch = firstText.match(/^<meta type="message" sender_type="agent" sender_name="([^"]+)"/);
      const agentTargetMatch = agentMetaMatch && firstText.match(/target="([^"]+)"/);

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
          const dbId = data.db_ids ? data.db_ids[j] : null;
          msgBody = renderContentBlocks(msg.content, dbId);
          if (!msgBody && msg.content.length === 0) {
            msgBody = `<div class="text-block"><em>(empty content)</em></div>`;
          }
        }
        if (msgBody) {
          bodyHtml += `<div class="api-message" data-agent-idx="${j}">${msgBody}</div>`;
        }
      }

      if (bodyHtml) {
        const rangeLabel = startIdx === endIdx - 1 ? `#${startIdx}` : `#${startIdx}-${endIdx - 1}`;
        // Show absolute DB message IDs if available
        let dbIdLabel = "";
        if (data.db_ids) {
          const dbIdsInRange = new Set();
          for (let j = startIdx; j < endIdx && j < data.db_ids.length; j++) {
            if (data.db_ids[j] != null) dbIdsInRange.add(data.db_ids[j]);
          }
          if (dbIdsInRange.size > 0) {
            const ids = [...dbIdsInRange];
            dbIdLabel = ids.length === 1 ? `db:${ids[0]}` : `db:${ids.join(",")}`;
          }
        }
        let roleLabel = isSystemMsg ? "system" : firstRole;
        let roleLabelHtml = escapeHtml(roleLabel);
        if (agentMetaMatch) {
          const senderSlug = agentMetaMatch[1];
          const targetSlug = agentTargetMatch ? agentTargetMatch[1] : null;
          const groups = data.subagent_groups || [];
          function agentBadge(slug) {
            const g = groups.find(g => g.agent_slug === slug);
            if (g) return `<a href="/agent?agent_id=${g.agent_id}" class="badge agent">${escapeHtml(slug)}</a>`;
            return `<span class="badge agent">${escapeHtml(slug)}</span>`;
          }
          roleLabelHtml = agentBadge(senderSlug);
          if (targetSlug) roleLabelHtml += ` → ${agentBadge(targetSlug)}`;
        }
        const turnUsage = findUsageForRange(data.turn_usages, startIdx, endIdx);
        const usageBadge = turnUsage ? renderUsageBadge({usage: turnUsage}) : "";
        const povTurnId = "pov-" + uid();
        const rawSlice = data.history.slice(startIdx, endIdx);
        const rawJson = escapeHtml(JSON.stringify(rawSlice, null, 2));

        // Detect a forget_above-affected BotTurn in this card and prebuild the full body.
        let fullBodyHtml = "";
        let fullToggleHtml = "";
        if (data.full_turns && data.db_ids) {
          const seen = new Set();
          for (let j = startIdx; j < endIdx && j < data.db_ids.length; j++) {
            const id = data.db_ids[j];
            if (id == null || seen.has(id)) continue;
            seen.add(id);
            const fullMsgs = data.full_turns[String(id)];
            if (fullMsgs) {
              fullBodyHtml = renderApiMessagesBody(fullMsgs, id);
              fullToggleHtml = `<button class="view-toggle full-toggle" onclick="toggleTurnFull('${povTurnId}')">show full</button>`;
              break;
            }
          }
        }

        const fullBodyDiv = fullBodyHtml ? `<div class="turn-body" id="${povTurnId}-full" style="display:none">${fullBodyHtml}</div>` : "";
        html += `<div class="turn-card" data-agent-idx="${startIdx}" data-agent-end="${endIdx}"><div class="turn-header"><span class="msg-id">${rangeLabel}</span>${dbIdLabel ? `<span class="msg-id">${dbIdLabel}</span>` : ""}<span class="sender ${sc}">${roleLabelHtml}</span>${usageBadge}${fullToggleHtml}<button class="view-toggle" onclick="toggleTurnView('${povTurnId}')">rendered</button></div><div class="turn-body" id="${povTurnId}-rendered">${bodyHtml}</div>${fullBodyDiv}<div class="turn-body turn-raw" id="${povTurnId}-raw" style="display:none"><pre>${rawJson}</pre></div></div>`;
      }
      i = endIdx;
    }

    // Flush any remaining subagent groups after all main turns
    html += flushSubagents(null);

    container.innerHTML = html;
  } catch (e) {
    if (loading) loading.textContent = "Error: " + e.message;
    console.error("Failed to load agent view:", e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Subagent view ────────────────────────────────────────────────────────────

async function loadSubagentView(agentId) {
  currentSubagentId = agentId;
  const container = document.getElementById("agent-container");
  const loading = document.getElementById("agent-loading");
  const subtitle = document.getElementById("agent-subtitle");
  const tokenBtn = document.getElementById("agent-token-btn");
  if (!container) return;

  if (subtitle) subtitle.textContent = `Loading agent #${agentId}...`;
  if (tokenBtn) tokenBtn.style.display = "none";
  if (loading) loading.style.display = "";

  try {
    const fullParam = agentViewFull ? "?full=1" : "";
    const resp = await fetch(`/api/subagent/${agentId}${fullParam}`);
    if (!resp.ok) {
      const err = await resp.json();
      if (loading) loading.textContent = `Error: ${err.error || resp.statusText}`;
      return;
    }
    const data = await resp.json();
    if (loading) loading.style.display = "none";

    // Set model dropdown from agent config
    const modelSelect = document.getElementById("chat-model");
    const agentModel = data.agent_config?.sampling?.model || "opus";
    if (modelSelect) modelSelect.value = agentModel;

    let html = "";

    // Agent info
    const agentLabel = data.agent_slug || `Agent #${data.agent_id}`;
    if (subtitle) subtitle.textContent = agentLabel;
    html += `<div class="agent-stats">${agentLabel} (#${data.agent_id}) | Chat ${data.chat_id} | Created ${formatTimestamp(data.created_at)} | ${data.num_db_turns} DB turns → ${data.num_messages} API messages</div>`;

    // Agent config
    if (data.agent_config && Object.keys(data.agent_config).length > 0) {
      const configId = "agentcfg-" + uid();
      const configStr = JSON.stringify(data.agent_config, null, 2);
      html += `<div class="turn-card"><div class="turn-header"><span class="sender system">Agent Config</span><span class="toggle-arrow open" id="arrow-${configId}" onclick="toggleCollapsible('${configId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content open" id="${configId}" style="max-height:800px"><pre><code class="language-json">${escapeHtml(configStr)}</code></pre></div></div>`;
    }

    // System prompt
    if (data.system_prompt) {
      html += renderSystemPrompt(data.system_prompt);
    }

    // Agent meta (extra fields beyond agent_config)
    const extraMeta = Object.fromEntries(
      Object.entries(data.agent_meta).filter(([k]) => k !== "agent_config")
    );
    if (Object.keys(extraMeta).length > 0) {
      const metaId = "agentmeta-" + uid();
      const metaStr = JSON.stringify(extraMeta, null, 2);
      html += `<div class="turn-card"><div class="turn-header"><span class="sender system">Agent Meta</span><span class="toggle-arrow" id="arrow-${metaId}" onclick="toggleCollapsible('${metaId}')" style="cursor:pointer">&#9654;</span></div><div class="collapsible-content" id="${metaId}"><pre><code class="language-json">${escapeHtml(metaStr)}</code></pre></div></div>`;
    }

    // Message history — same grouping as main agent view
    let i = 0;
    while (i < data.history.length) {
      const startIdx = i;
      const firstRole = data.history[i].role;
      let endIdx = i + 1;
      if (firstRole === "assistant") {
        while (endIdx < data.history.length) {
          const msg = data.history[endIdx];
          if (msg.role === "user") {
            const content = msg.content;
            const isToolResult = Array.isArray(content) && content.length > 0 &&
              content.some(b => b.type === "tool_result");
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
          if (!msgBody && msg.content.length === 0) {
            msgBody = `<div class="text-block"><em>(empty content)</em></div>`;
          }
        }
        if (msgBody) {
          bodyHtml += `<div class="api-message">${msgBody}</div>`;
        }
      }

      if (bodyHtml) {
        const rangeLabel = startIdx === endIdx - 1 ? `#${startIdx}` : `#${startIdx}-${endIdx - 1}`;
        let dbIdLabel = "";
        if (data.db_ids) {
          const dbIdsInRange = new Set();
          for (let j = startIdx; j < endIdx && j < data.db_ids.length; j++) {
            if (data.db_ids[j] != null) dbIdsInRange.add(data.db_ids[j]);
          }
          if (dbIdsInRange.size > 0) {
            const ids = [...dbIdsInRange];
            dbIdLabel = ids.length === 1 ? `db:${ids[0]}` : `db:${ids.join(",")}`;
          }
        }
        const roleLabel = isSystemMsg ? "system" : firstRole;
        const turnUsage = findUsageForRange(data.turn_usages, startIdx, endIdx);
        const usageBadge = turnUsage ? renderUsageBadge({usage: turnUsage}) : "";

        let fullBodyHtml = "";
        let fullToggleHtml = "";
        let fullBodyDiv = "";
        let renderedId = "";
        if (data.full_turns && data.db_ids) {
          const seen = new Set();
          for (let j = startIdx; j < endIdx && j < data.db_ids.length; j++) {
            const id = data.db_ids[j];
            if (id == null || seen.has(id)) continue;
            seen.add(id);
            const fullMsgs = data.full_turns[String(id)];
            if (fullMsgs) {
              const povTurnId = "sub-" + uid();
              fullBodyHtml = renderApiMessagesBody(fullMsgs, id);
              fullToggleHtml = `<button class="view-toggle full-toggle" onclick="toggleTurnFull('${povTurnId}')">show full</button>`;
              fullBodyDiv = `<div class="turn-body" id="${povTurnId}-full" style="display:none">${fullBodyHtml}</div>`;
              renderedId = ` id="${povTurnId}-rendered"`;
              break;
            }
          }
        }

        html += `<div class="turn-card"><div class="turn-header"><span class="msg-id">${rangeLabel}</span>${dbIdLabel ? `<span class="msg-id">${dbIdLabel}</span>` : ""}<span class="sender ${sc}">${escapeHtml(roleLabel)}</span>${usageBadge}${fullToggleHtml}</div><div class="turn-body"${renderedId}>${bodyHtml}</div>${fullBodyDiv}</div>`;
      }
      i = endIdx;
    }

    container.innerHTML = html;
    if (window.hljs) container.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
  } catch (e) {
    if (loading) loading.textContent = "Error: " + e.message;
    console.error("Failed to load subagent view:", e);
  }
}

// ── Agents list page ─────────────────────────────────────────────────────────

async function loadAgents() {
  const container = document.getElementById("agents-container");
  if (!container) return;

  container.innerHTML = `<div class="loading">Loading agents...</div>`;

  try {
    const resp = await fetch("/api/agents");
    const data = await resp.json();

    let html = `<table class="inv-table">
      <thead><tr>
        <th>ID</th><th>Slug</th><th>Type</th><th>Chat</th><th>Created</th>
        <th>Messages</th><th>Meta</th>
      </tr></thead><tbody>`;

    for (const a of data.agents) {
      const metaExtra = Object.fromEntries(
        Object.entries(a.meta).filter(([k]) => k !== "agent_config" && k !== "type")
      );
      const metaStr = Object.keys(metaExtra).length > 0
        ? escapeHtml(JSON.stringify(metaExtra, null, 2))
        : "";
      const typeLabel = a.type || "—";
      const slugLink = a.slug
        ? `<a href="/agent?agent_id=${a.id}" class="badge agent">${escapeHtml(a.slug)}</a>`
        : "—";

      html += `<tr>
        <td>${a.id}</td>
        <td>${slugLink}</td>
        <td>${escapeHtml(typeLabel)}</td>
        <td>${a.chat_id}</td>
        <td>${formatTimestamp(a.created_at)}<br><span class="relative-time">${relativeTime(a.created_at)}</span></td>
        <td>${a.msg_count}</td>
        <td><div class="meta-json">${metaStr}</div></td>
      </tr>`;
    }

    html += "</tbody></table>";
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="loading">Error: ${e.message}</div>`;
    console.error("Failed to load agents:", e);
  }
}

// ── Ephemeral agent chat ─────────────────────────────────────────────────────

async function sendAgentChat() {
  const input = document.getElementById("chat-input");
  const btn = document.getElementById("chat-send-btn");
  const status = document.getElementById("chat-status");
  const responseContainer = document.getElementById("chat-response-container");
  const message = input.value.trim();
  if (!message) return;

  // Get agent_id from URL if present
  const params = new URLSearchParams(window.location.search);
  const agentId = params.get("agent_id");

  btn.disabled = true;
  btn.textContent = "...";
  status.textContent = "Sending to Claude...";
  responseContainer.innerHTML = "";

  try {
    const body = { message };
    if (agentId) body.agent_id = parseInt(agentId);
    const model = document.getElementById("chat-model")?.value;
    if (model) body.model = model;

    const resp = await fetch("/api/agent-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (data.error) {
      status.textContent = "Error: " + data.error;
      return;
    }

    // Render usage
    let usageHtml = "";
    if (data.usage && data.usage.num_calls) {
      const u = data.usage;
      const costStr = u.cost_usd != null ? ` $${u.cost_usd.toFixed(4)}` : "";
      const model = (u.model || "").replace(/^claude-/, "").replace(/-\d{8}$/, "");
      usageHtml = `<span class="badge usage">${u.num_calls} call(s) | ${u.prompt_tokens} prompt | ${u.cached_tokens} cached | ${u.output_tokens} out${costStr} ${model}</span>`;
    }
    status.innerHTML = usageHtml || "Done";

    let html = "";

    // Render response message_params — group assistant + tool_result pairs
    const msgs = data.message_params || [];
    if (msgs.length > 0) {
      let i = 0;
      while (i < msgs.length) {
        const startIdx = i;
        const firstRole = msgs[i].role;
        let endIdx = i + 1;
        if (firstRole === "assistant") {
          while (endIdx < msgs.length) {
            const m = msgs[endIdx];
            if (m.role === "user") {
              const c = m.content;
              const isToolResult = Array.isArray(c) && c.length > 0 && c.some(b => b.type === "tool_result");
              if (isToolResult) { endIdx++; continue; }
            }
            if (m.role === "assistant" && endIdx > startIdx + 1) { endIdx++; continue; }
            break;
          }
        }

        let bodyHtml = "";
        for (let j = startIdx; j < endIdx; j++) {
          const m = msgs[j];
          let msgBody = "";
          if (typeof m.content === "string") {
            if (m.content.trim()) {
              const id = "txt-" + uid();
              msgBody = `<div class="text-block"><div class="text-block-header" onclick="toggleCollapsible('${id}')"><span class="toggle-arrow open" id="arrow-${id}">&#9654;</span> <strong>Text</strong></div><div class="collapsible-content open" id="${id}">${escapeHtml(m.content)}</div></div>`;
            }
          } else if (Array.isArray(m.content)) {
            msgBody = renderContentBlocks(m.content);
          }
          if (msgBody) bodyHtml += `<div class="api-message">${msgBody}</div>`;
        }

        if (bodyHtml) {
          const sc = firstRole === "assistant" ? "bot" : "";
          html += `<div class="turn-card"><div class="turn-header"><span class="sender ${sc}">${escapeHtml(firstRole)}</span></div><div class="turn-body">${bodyHtml}</div></div>`;
        }
        i = endIdx;
      }
    } else {
      html += `<div class="turn-card"><div class="turn-body"><em>(no response)</em></div></div>`;
    }

    responseContainer.innerHTML = html;
    input.value = "";
  } catch (e) {
    status.textContent = "Error: " + e.message;
    console.error("Agent chat failed:", e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
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

  if (document.getElementById("locations-container")) {
    loadLocations();
  }

  if (document.getElementById("agents-container")) {
    loadAgents();
  }

  if (document.getElementById("agent-container")) {
    const params = new URLSearchParams(window.location.search);
    const agentId = params.get("agent_id");
    if (agentId) {
      loadSubagentView(parseInt(agentId));
    } else {
      loadAgentView();
    }

    // Ctrl+Enter or Cmd+Enter to send
    const chatInput = document.getElementById("chat-input");
    if (chatInput) {
      chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          sendAgentChat();
        }
      });
    }
  }

  if (document.getElementById("workspace-container")) {
    const params = new URLSearchParams(window.location.search);
    const date = params.get("date");
    loadWorkspaceDiff(date);
  }
});


// ── Workspace Diff ─────────────────────────────────────────────────────────

async function loadWorkspaceDiff(date) {
  const container = document.getElementById("workspace-container");
  container.innerHTML = "<p>Loading...</p>";
  try {
    const url = date ? `/api/workspace-diff?date=${date}` : "/api/workspace-diff";
    const resp = await fetch(url);
    const data = await resp.json();

    const currentDate = data.date;
    const d = new Date(currentDate + "T12:00:00");
    const prevDate = new Date(d); prevDate.setDate(d.getDate() - 1);
    const nextDate = new Date(d); nextDate.setDate(d.getDate() + 1);
    const fmt = dt => `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,"0")}-${String(dt.getDate()).padStart(2,"0")}`;
    // Use local date, not UTC
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
    const isToday = currentDate === today;

    let html = `<div class="workspace-nav">`;
    if (!isToday) html += `<a href="/workspace?date=${fmt(nextDate)}">&larr; ${fmt(nextDate)}</a>`;
    else html += `<span></span>`;
    html += `<strong>${currentDate}${isToday ? " (today)" : ""}</strong>`;
    html += `<a href="/workspace?date=${fmt(prevDate)}">${fmt(prevDate)} &rarr;</a>`;
    html += `</div>`;

    html += renderRepoDiff("Workspace", data.workspace, {pullBtn: true});
    html += renderRepoDiff("Logseq (Yarvis only)", data.logseq);

    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<p>Error: ${escapeHtml(e.message)}</p>`;
  }
}

async function pullWorkspace(btn) {
  btn.disabled = true;
  btn.textContent = "pulling...";
  try {
    const resp = await fetch("/api/workspace-pull", {method: "POST"});
    const data = await resp.json();
    btn.textContent = data.ok ? "done!" : "error";
    if (data.ok && !data.output.includes("Already up to date")) {
      setTimeout(() => location.reload(), 500);
    } else {
      setTimeout(() => { btn.textContent = "git pull"; btn.disabled = false; }, 1500);
    }
  } catch (e) {
    btn.textContent = "error";
    setTimeout(() => { btn.textContent = "git pull"; btn.disabled = false; }, 1500);
  }
}

function renderRepoDiff(title, repoData, opts) {
  const collapseId = "repo-" + uid();
  let html = `<div class="repo-diff-section">`;
  const pullHtml = opts?.pullBtn ? ` <button class="pull-btn" onclick="event.stopPropagation(); pullWorkspace(this)">git pull</button>` : "";
  html += `<div class="repo-diff-header" onclick="toggleCollapsible('${collapseId}')"><span class="toggle-arrow open" id="arrow-${collapseId}">&#9654;</span> <strong>${escapeHtml(title)}</strong> <span class="workspace-info-inline">${repoData.commits} commit(s)</span>${pullHtml}</div>`;
  html += `<div class="collapsible-content open" id="${collapseId}">`;

  if (repoData.error) {
    html += `<div class="workspace-info">${escapeHtml(repoData.error)}</div>`;
  } else {
    if (repoData.stat) html += `<pre class="workspace-stat">${escapeHtml(repoData.stat)}</pre>`;
    if (repoData.diff) {
      html += `<pre class="workspace-diff">${colorDiff(repoData.diff)}</pre>`;
    } else {
      html += `<div class="workspace-info">No changes</div>`;
    }
  }
  html += `</div></div>`;
  return html;
}

function colorDiff(diff) {
  return diff.split("\n").map(line => {
    const esc = escapeHtml(line);
    if (line.startsWith("commit ")) return `<span class="diff-commit">${esc}</span>`;
    if (line.startsWith("Author:") || line.startsWith("Date:")) return `<span class="diff-commit-meta">${esc}</span>`;
    if (line.startsWith("+++") || line.startsWith("---")) return `<span class="diff-file">${esc}</span>`;
    if (line.startsWith("@@")) return `<span class="diff-hunk">${esc}</span>`;
    if (line.startsWith("+")) return `<span class="diff-add">${esc}</span>`;
    if (line.startsWith("-")) return `<span class="diff-del">${esc}</span>`;
    if (line.startsWith("diff ")) return `<span class="diff-file">${esc}</span>`;
    return esc;
  }).join("\n");
}
