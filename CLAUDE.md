# Yarvis: Yet Another JARVIS - Claude-Powered Telegram Bot

## Project Overview
Yarvis is a Telegram bot powered by Claude LLM that provides users with an AI assistant through the Telegram platform. The bot features rich functionality including tool usage, memory persistence, and scheduled task execution.

## Key Components

### Core Functionality
- **Anthropic's Claude Integration**: Uses the Anthropic API to interact with Claude models
- **Telegram Bot Framework**: Built on Python-Telegram-Bot (PTB) library for Telegram integration
- **PostgreSQL Database**: Stores conversation history, memories, and scheduled invocations
- **Tool Access**: Claude has access to various tools including Python execution, file access, and more
- **Memory System**: Persistent memory (Core Knowledge Repository) included in system prompts
- **Scheduling System**: Allows the bot to schedule future invocations for reminders and recurring tasks

### Conventions
- **Pydantic for structured data**: Use `pydantic.BaseModel` (not dataclasses) for config/data objects that need serialization. See `AgentConfig` in `agent_config.py` as the reference pattern. Use `model_dump()` / `model_validate()` for dict conversion.

### Key Files and Modules
- `launch.sh`: Top-level entrypoint for the Heroku deployment
- `yarvis_ptb/yarvis_ptb/complex_chat.py`: Contains the main conversational logic, Claude integration, and message handling
- `yarvis_ptb/yarvis_ptb/storage.py`: Manages database interactions for messages, memories, and scheduled invocations
- `yarvis_ptb/yarvis_ptb/prompt_consts.py`: Contains system prompts
- `yarvis_ptb/yarvis_ptb/agent_config.py`: `AgentConfig` (rendering + sampling + tool config), `AgentMeta` (typed wrapper for agents.meta JSONB)
- `yarvis_ptb/yarvis_ptb/rendering_config.py`: `RenderingConfig` — prompt name, memory loading, context placement
- `yarvis_ptb/yarvis_ptb/sampling.py`: `SamplingConfig` — model, max_tokens, output_mode
- `yarvis_ptb/yarvis_ptb/daily_agent_update.py`: DAU (Disjoint Agent Union) — daily session rotation
- `yarvis_ptb/yarvis_ptb/tools/`: Directory containing tool implementations

### Memory / Core knowledge repository
`core_knowledge` folder is a link to a separate memory repository. It contains .md files with info about both the user and the environment. Some of them are loaded into context all the time and some are loaded contextually

## Data Structure

### Database Tables
- `messages`: Stores chat history (user and bot messages)
- `invocations`: Tracks scheduled bot invocations
- `chat_variables`: Stores chat-specific variable values
- `memories`: Not used. Old implementation of memory. Currently memory is FS-based

### Message Storage Format
Messages in the `messages` table have a `message` text field and a `meta` JSONB field.

- **User messages**: `message` contains the text. `meta` may have `{"is_voice": true}` for voice messages, or `{"image_b64": "..."}` for image messages.
- **System messages**: `message` contains the text (e.g. restart notifications). `meta` is usually empty.
- **Bot messages** (user_id=-1): `message` is set to `"USE_CONTENT_FROM_META"`. The actual content is in `meta.message_params`, which is a list of Claude API `MessageParam` turns:
  ```
  [
    {"role": "assistant", "content": [{"type": "tool_use", "name": "bash_run", "input": {...}, "id": "..."}]},
    {"role": "user", "content": [{"type": "tool_result", "content": [{"type": "text", "text": "..."}], "is_error": false, "tool_use_id": "..."}]},
    {"role": "assistant", "content": [{"type": "text", "text": "The final response text"}]},
    ...
  ]
  ```
  The list alternates assistant turns (containing `tool_use` and/or `text` blocks) and user turns (containing `tool_result` blocks). The final assistant turn typically has the text response shown to the user. See `render_claude_response_short()` and `render_claude_response_verbose()` in `prompting.py` for rendering logic.
- **Tool call messages** (user_id=-3): Legacy format, `message` contains text.

Special user_id values: BOT_USER_ID=-1, SYSTEM_USER_ID=-2, TOOL_CALL_USER_ID=-3.

### Memory System
The Core Knowledge Repository is implemented as a collection of text files that get included in Claude's system prompt. This allows the bot to maintain persistent knowledge across conversations.

## Message Processing Flow
1. User sends message to Telegram
2. Message is processed by `handle_message_root_user_assistant()`
3. `process_multi_message_claude_invocation()` prepares the Claude API call
4. Previous messages are retrieved and context is built
5. Claude processes the input with tools via `tool_sampler.process_query()`
6. Response is formatted and sent back to the user
7. Messages are saved to the database for future context

## Scheduling System
The bot can schedule its own future invocations using the scheduling system:
- Invocations can be one-time or recurring
- Scheduled invocations are stored in the database
- Each invocation includes metadata, reason, and timing information

## DAU (Disjoint Agent Union)
Daily session rotation system. At 2am local time (`daily_agent_update.py`):
1. Creates a frozen archive agent (`archive-YYYY-MM-DD`) for the previous session
2. Reassigns main-chat messages (last 2 days, `agent_id IS NULL`) to the archive agent
3. Generates a haiku summary of the archived conversation
4. Inserts system messages (freeze notice in archive, new-session notice in main chat)
5. Triggers a proactive Claude invocation for the new session

Frozen archive agents are queryable via `run_subagent(agent="archive-2026-03-04", message="...")` — queries are ephemeral (not saved under the archive agent). Agent slugs use `coolname` for subagents and date-based format for archives (`agent_slugs.py`).

## Credentials & Environment Variables
- API credentials (client IDs, secrets) go in `.env` (gitignored) and must also be set on Heroku via `heroku config:set`
- OAuth token files (e.g. `whoop_token.json`, `nest_token.json`) are shipped to Heroku via `tokens_to_envs.sh`
- When adding a new integration, always add its env vars to both `.env` and Heroku

## Deployment
The project is hosted on Heroku with PostgreSQL database integration.
- Heroku app name: `claude-telegram-v2` (fork of original `claude-telegram`/yarvis)
- Both apps share the same Postgres database (`postgresql-shaped-06547`)
- **Never push to Heroku directly** (`git push heroku`). Deploys happen automatically after pushing to GitHub.
- `update_tokens.sh` must be run inside the conda env: `conda run -n clam ./update_tokens.sh`

## GCP Infrastructure

A GCP VM runs message accumulator services as Docker containers, accessible via Tailscale.

- **GCP project**: `signal-api-project`
- **VM**: `signal-api`, zone `us-central1-a`, `e2-micro` (free tier), Ubuntu 24.04 LTS
- **Tailscale IP**: `100.108.7.78`
- **Local gcloud**: `/opt/homebrew/share/google-cloud-sdk/bin/gcloud`
- **SSH**: `gcloud compute ssh signal-api --zone us-central1-a --tunnel-through-iap`
- **Heroku ↔ GCP**: Tailscale buildpack (`mvisonneau/heroku-buildpack-tailscale`), env vars: `TAILSCALE_AUTH_KEY`, `SIGNAL_API_URL`

### Services on the VM
- **Signal combined** (`signal_accumulator/`): signal-cli-rest-api + accumulator in one container (ports 8080+8081). See `signal_accumulator/CLAUDE.md`.
- **SMS accumulator** (`sms_accumulator/`): Go service, port 8082. See `sms_accumulator/CLAUDE.md`.

### VM Watchdog
Auto-restarts the VM when health checks fail. Runs in `callback_minute` on Heroku (`vm_watchdog.py`). After 3 consecutive failures (3 minutes), resets the VM via the GCP Compute API. 10-minute cooldown between resets.

**Service account setup** (already done, for reference):
```bash
gcloud iam service-accounts create vm-restarter \
  --project=signal-api-project --display-name="VM auto-restarter"
gcloud projects add-iam-policy-binding signal-api-project \
  --member="serviceAccount:vm-restarter@signal-api-project.iam.gserviceaccount.com" \
  --role="roles/compute.instanceAdmin.v1"
gcloud iam service-accounts keys create gcp_vm_key.json \
  --iam-account=vm-restarter@signal-api-project.iam.gserviceaccount.com
heroku config:set -a claude-telegram-v2 GCP_VM_KEY="$(cat gcp_vm_key.json)"
```

## Development
To work on this project locally:
1. Activate the conda environment: `conda activate clam`
2. Set up required environment variables (including API keys) — see `activate.zsh`
3. Install dependencies from requirements.txt
4. Run the bot using the launch.sh script

### Running Python
`activate.zsh` is the ground truth for environment setup. Source it or replicate its variables before running anything. Currently this means:
```
SETTINGS_NAME=anton conda run -n clam python -c "..."
```

### Testing changes
Use `cli_prompt.py` to test changes end-to-end. It loads full conversation history from DB, runs Claude with all tools, and prints the response to terminal — without saving anything back to DB or involving Telegram.
```
SETTINGS_NAME=anton conda run -n clam python cli_prompt.py "Your test prompt here"
SETTINGS_NAME=anton conda run -n clam python cli_prompt.py -v "Compute 2+2 in python"  # verbose: shows tool call details
```

### Dumping messages
Use `dump_messages.py` to dump recent conversation messages from the database to stdout in Claude MessageParam format. Useful for debugging message storage and rendering.
```
SETTINGS_NAME=anton conda run -n clam python dump_messages.py              # last 24h, up to 200 messages
SETTINGS_NAME=anton conda run -n clam python dump_messages.py -s 2026-02-27  # since a specific date
SETTINGS_NAME=anton conda run -n clam python dump_messages.py -n 50          # limit to 50 messages
SETTINGS_NAME=anton conda run -n clam python dump_messages.py --max-line-length 0  # no line truncation (default: 200 chars)
```

# Style guide

Use pyhton 3.10 syntax. For typing, don't use List or Optional
