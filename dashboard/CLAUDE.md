# Dashboard

Local-only Flask app for inspecting Yarvis conversation history and the full agent context window.

## Running

```bash
SETTINGS_NAME=anton conda run -n clam python dashboard/app.py
# Runs on http://localhost:5001
```

## Architecture

- **Backend**: `app.py` — Flask app with JSON API endpoints, uses the same DB and `yarvis_ptb` modules as the bot
- **Frontend**: Vanilla JS (`static/app.js`) + CSS (`static/style.css`) + Jinja templates (`templates/`)
- No build step, no npm, no bundler

## Pages

| Route | Template | Description |
|-------|----------|-------------|
| `/` / `/messages` | `messages.html` | Browse conversation history, paginated, with search and byte-size filters |
| `/agent` | `agent.html` | POV: Full agent context window: system prompt + message history as Claude sees it |
| `/agents` | `agents.html` | List all agents with their fields, type, and message counts |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/chats` | List chats with message counts |
| `GET /api/messages?page=&chat_id=&search=&min_bytes=` | Paginated messages for a chat |
| `GET /api/turn/<id>/tokens` | Token count breakdown for a single DB turn |
| `GET /api/agents` | List all agents with metadata and message counts |
| `GET /api/agent-view` | Full agent context (system prompt + history) |
| `GET /api/agent-view/tokens` | Token counts for the full agent context |
| `GET /api/stats` | Summary stats shown in nav bar |

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

- `BOT_USER_ID = -1`, `SYSTEM_USER_ID = -2`, `TOOL_CALL_USER_ID = -3`
- `PER_PAGE = 500`
- `DEFAULT_CHAT_ID = ROOT_USER_ID` (Anton's Telegram ID)
- Token counting model: `claude-sonnet-4-20250514`
