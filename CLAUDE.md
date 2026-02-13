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
