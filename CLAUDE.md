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

### Key Files and Modules
- `launch.sh`: Top-level entrypoint for the Heroku deployment
- `yarvis_ptb/yarvis_ptb/complex_chat.py`: Contains the main conversational logic, Claude integration, and message handling
- `yarvis_ptb/yarvis_ptb/storage.py`: Manages database interactions for messages, memories, and scheduled invocations
- `yarvis_ptb/yarvis_ptb/prompt_consts.py`: Contains system prompts
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

## Deployment
The project is hosted on Heroku with PostgreSQL database integration.
- Heroku app name: `claude-telegram`

## GCP Infrastructure (Signal API)
A GCP VM runs `signal-cli-rest-api` to give Yarvis read access to Signal messages.

- **GCP project**: `signal-api-project`
- **VM name**: `signal-api`, zone `us-central1-a`, machine type `e2-micro` (free tier)
- **OS**: Ubuntu 24.04 LTS
- **Networking**: Tailscale private network (no public ports except SSH)
  - VM Tailscale IP: `100.108.7.78`
  - Signal API accessible at `http://100.108.7.78:8080`
- **Signal API**: `bbernhard/signal-cli-rest-api` Docker container, running in JSON-RPC mode
  - Bound to Tailscale IP only (`-p 100.108.7.78:8080:8080`)
  - Signal number: `+16506603785`
  - Data volume: `signal-cli-data`
- **Heroku ↔ GCP**: Tailscale buildpack (`mvisonneau/heroku-buildpack-tailscale`) on Heroku app
  - Env vars on Heroku: `TAILSCALE_AUTH_KEY`, `SIGNAL_API_URL=http://100.108.7.78:8080`
- **Local gcloud**: `/opt/homebrew/share/google-cloud-sdk/bin/gcloud`
- **SSH**: `gcloud compute ssh signal-api --zone us-central1-a --tunnel-through-iap`

### Signal Accumulator
Code lives in `signal_accumulator/` in this repo. It's a Flask app that listens to the Signal API websocket, stores messages in SQLite, and exposes a query API on port 8081.

**Deployment**: Code is committed here, then manually copied to the GCP VM and run as a Docker container:
```bash
# Copy files to VM
gcloud compute scp signal_accumulator/* signal-api:~/signal_accumulator/ --zone us-central1-a --tunnel-through-iap

# On the VM: build and run
cd ~/signal_accumulator
sudo docker build -t signal-accumulator .
sudo docker run -d --name signal-accumulator \
  --restart=unless-stopped \
  -p 100.108.7.78:8081:8081 \
  -v signal-accumulator-data:/data \
  -e SIGNAL_WS_URL=ws://100.108.7.78:8080 \
  signal-accumulator
```

### SMS Accumulator
Go service in `sms_accumulator/` — connects to Google Messages via `libgm` (web pairing protocol), captures all SMS/RCS messages (incoming + outgoing), stores in SQLite, exposes query API on port 8082. See `sms_accumulator/CLAUDE.md` for details.

- **API**: `GET /messages?hours=24&sender=...&limit=100`, `GET /health`
- **VM port**: `100.108.7.78:8082`
- **Docker volume**: `sms-accumulator-data` (holds `auth.json` + `sms_messages.db`)

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

### Go
Go is not installed locally. For Go services (e.g. `sms_accumulator/`), use Docker for builds — see the sub-project's own CLAUDE.md for build/deploy instructions.

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
