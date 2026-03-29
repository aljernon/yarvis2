# Code Conventions

- When writing prompts/instructions for Yarvis agents, prefer minimal guidance — say what to check, not what to do. Avoid prescriptive wording.
- Don't speculate about runtime behavior — trace the code to verify before claiming how something works
- Before adding retry/error-handling logic, grep for existing patterns in the codebase (e.g. tenacity in tool_sampler.py) and match the established style
- When adding a library or pattern, grep for existing usage first and follow the same approach rather than writing from scratch
- When passing a single boolean `True`/`False` as an argument, always use the keyword form (e.g. `get_timezone(complex_chat=True)` not `get_timezone(True)`)
- Always use top-level imports, never local/deferred imports unless needed to break a circular dependency
- All imports must be at the top of the file, never scattered between function definitions
- After making functional changes, test with `cli_prompt.py` before declaring done
- Use `client.messages.count_tokens()` for token budget enforcement, not character-count heuristics
- Never pass pytz timezones to datetime constructor; always use `tz.localize(dt)`. Prefer `America/Los_Angeles` over `US/Pacific`
- When referencing code locations, use relative paths from project root (e.g. `yarvis_ptb/yarvis_ptb/file.py:42`), not bare filenames. Never put punctuation (`.`, `,`) immediately after a `file:line` reference — it breaks clickability
- For DB access, use `from yarvis_ptb.storage import connect` — don't re-search for the pattern. `connect()` returns a psycopg2 connection context manager
- Don't add back-compat aliases for renames — update all references directly
- For Yarvis-visible information (errors, status, events), put it in the notification/message text — yarvis doesn't see server logs, only what's stored in DB messages
- When committing, only include files directly related to the requested change — don't bundle unrelated edits
- Never `git push` unless explicitly asked — pushes trigger a Heroku rebuild
- When editing workspace files, read the full file first to avoid duplicating existing content
- Before exploring a subdirectory (dashboard/, signal_accumulator/, etc.), check for a local CLAUDE.md in that directory first
