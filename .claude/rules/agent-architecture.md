# Agent & Subagent Architecture

High-level overview of message flow and agent system. Read the actual files for details.

## Main message flow

Telegram → `handlers.py:handle_message` → `complex_chat.py:process_multi_message_claude_invocation` → `prompting.py:build_claude_input` (system prompt + history) → `tool_sampler.py:process_query` (Claude API + tool loop) → save bot response → `nudges.py:run_nudges` (post-reply self-checks)

## User IDs and turn types

| user_id | Constant | Turn class | Meaning |
|---------|----------|------------|---------|
| `-1` | `BOT_USER_ID` | `BotTurn` | Claude responses. `message="USE_CONTENT_FROM_META"`, actual content in `meta.message_params` |
| `-2` | `SYSTEM_USER_ID` | `SystemTurn` | Notifications, schedule markers, reflections. `meta.turn_type` determines rendering |
| `1` | `AGENT_TO_AGENT_USER_ID` | `InputMessageTurn` | Subagent → main agent messages. `meta.agent_slug` has sender, `meta.target_slug` has receiver |
| Real ID | — | `InputMessageTurn` | Human messages from Telegram |

## Subagent system

- **CreateSubagentTool**: Minimal prompt, limited tools. For isolated computation.
- **CreateYarvisSubagentTool**: Full Yarvis identity + workspace. For context-aware tasks.
- **Scheduled subagents** (`handlers.py:_run_schedule_in_subagent`): Created with `sched/` prefix slug. Output triggers `process_multi_message_claude_invocation` with `invocation_type="reply"` so main agent processes the result.
- `send_message` in subagents uses `CollectMessageTool` — returns text to parent, not Telegram.
- Agent messages scoped by `agent_id` column: `get_messages(curr, chat_id, agent_id=N)` for subagent, `agent_id=None` for main.

## Nudges (`nudges.py`)

Post-reply self-checks that run sequentially after each bot reply. Each `Nudge` has:
- `should_run(tool_names)` — condition based on tools used in the original reply
- `should_persist(nudge_tool_names)` — whether to keep the nudge visible in history
- `noop_send_message` — whether to block send_message during the nudge

## Auto-reflect (`daily_self_reflect.py`)

Runs as a subagent with `CollectMessageTool`. Any `send_message` calls become a summary in the main chat notification. Triggered by idle timeout or midnight cron.

## Telegram client (`telegram_client.py`)

Telegram messages are persistent on their servers — use Telethon to re-fetch any historical messages. The notification check window in `message_events.py` is just a polling interval, not a data retention limit. Running locally requires `set -a && source .env && set +a` for `TELEGRAM_ID`/`TELEGRAM_HASH`.

## Key files

| File | What it does |
|------|-------------|
| `handlers.py` | Telegram handlers, schedule execution, subagent scheduling |
| `complex_chat.py` | Main Claude invocation pipeline |
| `tool_sampler.py` | Tool resolution + Claude API loop |
| `prompting.py` | System prompt building, message conversion, context injection |
| `turns.py` | Turn types (BotTurn, SystemTurn, InputMessageTurn) and rendering to MessageParam |
| `agent_runner.py` | Subagent creation and execution |
| `nudges.py` | Post-reply self-check system |
| `agent_config.py` | AgentConfig (rendering + sampling), AgentMeta |
