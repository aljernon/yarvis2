---
description: Reflect on recent Claude Code sessions to find improvements for CLAUDE.md and rules
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, Agent
---

Reflect on recent Claude Code sessions for this project to identify workflow improvements. Analyze conversations for mistakes, user corrections, inefficiencies, and missed patterns — then propose concrete improvements to CLAUDE.md or .claude/rules/*.md files.

Arguments: $ARGUMENTS

## Step 1: Gather session transcripts

**If arguments contain "current" or "this":** Reflect on the current session only. The current session is the most recent JSONL file:

```bash
ls -t ~/.claude/projects/-Users-anton-projects-yarvis2/*.jsonl | head -1
```

**Otherwise (default):** Find the 6 most recent session JSONL files (by modification time). The first one is usually the current `/reflect` session and should be skipped — but if `/reflect` was invoked mid-session (i.e. that file has substantive pre-`/reflect` content), include it too, since it's the richest source of recent learnings.

```bash
ls -t ~/.claude/projects/-Users-anton-projects-yarvis2/*.jsonl | head -6 | tail -5
# Then decide whether to also include the current session:
CURRENT=$(ls -t ~/.claude/projects/-Users-anton-projects-yarvis2/*.jsonl | head -1)
# Count non-/reflect user turns; include the file if >20
jq -r 'select(.type=="user" and .message.role=="user") | .message.content | if type=="string" then . elif type=="array" then [.[]|select(.type=="text")|.text]|join("\n") else empty end' "$CURRENT" 2>/dev/null | grep -vcE '(^$|<command-name>/reflect|<local-command-caveat|<command-message>|<command-args>)'
```

If that count is >20, include `$CURRENT` in the analysis set too (but exclude its `/reflect` invocation itself).

Read each session file. Sessions are very large JSONL with noisy metadata (base64 signatures, tool IDs, etc.). **Always preprocess with jq first** to extract just the useful content, then **filter out frame noise** (system reminders, command sentinels, request-interrupted markers, task-notification blocks):

```bash
# Extract user messages, drop frame noise. Run per session.
jq -r 'select(.type=="user" and .message.role=="user") | .message.content | if type=="string" then . elif type=="array" then [.[]|select(.type=="text")|.text]|join("\n") else empty end' "$SESSION_FILE" \
  | grep -vE '^(\[Request|<command-|<local-command|<system-reminder|<task-notification|<task-id|<tool-use-id|<output-file|<status|<summary|</task-notification|$)'

# Extract assistant text + tool calls
jq -r '
  select(.type == "assistant") |
    .message.content[]? |
    if .type == "text" then "ASSISTANT: " + .text
    elif .type == "tool_use" then "TOOL: " + .name
    else empty end
' "$SESSION_FILE"
```

**Before reading, check per-file size** so you can decide whether to read inline or delegate:

```bash
for f in $(ls -t ~/.claude/projects/-Users-anton-projects-yarvis2/*.jsonl | head -6); do
  chars=$(jq -r 'select(.type=="user" and .message.role=="user") | .message.content | if type=="string" then . elif type=="array" then [.[]|select(.type=="text")|.text]|join("\n") else empty end' "$f" 2>/dev/null \
    | grep -vcE '^(\[Request|<command-|<local-command|<system-reminder|<task-notification|<task-id|<tool-use-id|<output-file|<status|<summary|</task-notification|$)' )
  echo "$(basename $f): $chars filtered lines"
done
```

If any file exceeds ~20KB of filtered user content (≈ a long session like a379e690 in this project's history), **delegate that file's analysis to an Explore subagent** rather than reading it inline — otherwise you flood your context with low-signal text and produce a thin reflection table. Read smaller sessions directly. For inline reads of the current session, use `head -500` and `tail -500` only as a last resort; the negative-grep filter is usually enough.

## Step 2: Analyze sessions for improvement signals

For each session, look for these patterns:

**User corrections & feedback:**
- User saying "no", "wrong", "that's not right", "don't do that", "I said..."
- User repeating instructions that Claude should have followed
- User providing the same guidance they've given in prior sessions

**Tool misuse:**
- Using Bash when a dedicated tool (Read, Grep, Glob, Edit) should have been used
- Running `cat`, `grep`, `find`, `sed` via Bash instead of dedicated tools
- Unnecessary tool calls or roundabout approaches
- Failed tool calls that required retries

**Inefficiencies:**
- Claude asking questions it could have answered by reading existing docs
- Exploring code that's already documented in CLAUDE.md
- Missing context that led to wrong assumptions
- Repeated patterns that could be codified as rules

**Missed conventions:**
- Code style violations that were corrected
- Project-specific patterns Claude didn't follow
- Environment setup steps Claude got wrong

** Unclear things in the code:**
- Missing CLAUDE.md/rules files that could reduce research time next time CC handles similar task

## Step 3: Read the reflect command itself

Read this command file:
```
.claude/commands/reflect.md
```

Consider whether the reflect command itself could be improved based on how this session is going. If so, include it as an improvement item.

## Step 4: Read current CLAUDE.md and rules

Read the existing CLAUDE.md and any files in .claude/rules/ to understand what's already documented. Don't propose duplicates.

## Step 5: Present improvement candidates

Create a numbered table of proposed improvements. For each item include:

| # | Category | Issue found | Proposed fix | Target file |
|---|----------|------------|-------------|-------------|
| 1 | User correction | "User corrected X in session Y" | Add rule: "..." | CLAUDE.md |
| 2 | Tool misuse | "Used bash grep instead of Grep tool" | Add rule: "..." | .claude/rules/tools.md |
| ... | ... | ... | ... | ... |

Categories: `User correction`, `Tool misuse`, `Inefficiency`, `Convention`, `Reflect improvement`

Keep the "Proposed fix" column concise — just the gist of what would be added.

## Step 6: Present and STOP

Present the table to the user. Do NOT use AskUserQuestion — just show the table and ask "Which items should I implement? (list numbers)" as plain text. Then STOP and wait for the user to reply. Do NOT proceed to step 7 until the user has explicitly told you which items to implement.

## Step 7: Implement chosen items (only after user replies)

ONLY after the user has replied with their chosen item numbers in a follow-up message, implement those items:
- Edit the target file (CLAUDE.md or the appropriate .claude/rules/*.md file)
- Keep additions concise — one line per concept where possible
- Group related additions together under appropriate headers
- For new rules files, create them with a clear header comment explaining the rule category

Show a summary of all changes made.
