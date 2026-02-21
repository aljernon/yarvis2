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

## Development
To work on this project locally:
1. Set up required environment variables (including API keys)
2. Install dependencies from requirements.txt
3. Run the bot using the launch.sh script
