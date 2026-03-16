# Dashboard

Local-only Flask app for inspecting Yarvis conversation history and the full agent context window.

## Running

```bash
SETTINGS_NAME=anton conda run -n clam python dashboard/app.py
# or: ./launch_dashboard.sh
# Runs on http://localhost:5001
```

## Architecture

- **Backend**: Flask app with Blueprint-based route organization
- **Frontend**: Vanilla JS (`static/app.js`) + CSS (`static/style.css`) + Jinja templates (`templates/`)
- No build step, no npm, no bundler

### Backend structure

| File | Purpose |
|------|---------|
| `app.py` | Flask app creation, HTML routes, blueprint registration |
| `helpers.py` | Shared utilities: DB connection, turn conversion, image truncation, usage extraction |
| `token_counting.py` | Anthropic `count_tokens` with on-disk SHA-256 cache |
| `routes/messages.py` | `/api/chats`, `/api/messages`, `/api/turn/<id>/tokens`, `/api/stats` |
| `routes/agents.py` | `/api/agents`, `/api/subagent/<id>` |
| `routes/agent_view.py` | `/api/agent-view`, `/api/agent-view/tokens` |
| `routes/chat.py` | `/api/agent-chat` — ephemeral Claude execution (no DB save) |
| `routes/schedules.py` | `/api/schedules` |

## Pages

| Route | Template | Description |
|-------|----------|-------------|
| `/` / `/messages` | `messages.html` | Browse conversation history, paginated, with search and byte-size filters |
| `/agent` | `agent.html` | POV: Full agent context window + ephemeral chat input |
| `/agents` | `agents.html` | List all agents with their fields, type, and message counts |
| `/schedules` | `schedules.html` | Scheduled invocations |

## Ephemeral Chat

The agent view (`/agent`) has a message input at the bottom that sends a prompt to Claude with full tool access and renders the response inline. No messages are saved to the database. Model is selectable via dropdown (opus/sonnet/haiku).

## Frontend Rendering (`app.js`)

### Content block rendering (`renderContentBlocks`)

Handles Claude API content block types: `text`, `tool_use`, `tool_result`, `thinking`, `redacted_thinking`.

**Special cases for `tool_use` display:**
- If a tool call has exactly one string argument, the header shows `tool_name/arg_name` and the body shows just the string value (not JSON)
- If `send_message` has `message` (string) + `final: true`, the header shows `send_message/message/final` and body shows only the message text
- `send_message` blocks are expanded by default; all others are collapsed

### Token counting

Click "tokens?" on any turn to fetch per-block token counts via the API. Uses Anthropic's `count_tokens` endpoint with an on-disk SHA-256 cache (`.token_cache/` directory, gitignored).

## Key constants

- `BOT_USER_ID = -1`, `SYSTEM_USER_ID = -2`
- `PER_PAGE = 500`
- `DEFAULT_CHAT_ID = ROOT_USER_ID` (Anton's Telegram ID)
- Token counting model: `claude-sonnet-4-20250514`
