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

**Otherwise (default):** Find the 6 most recent session JSONL files (by modification time), then **skip the first one** (it's the current `/reflect` session):

```bash
ls -t ~/.claude/projects/-Users-anton-projects-yarvis2/*.jsonl | head -6 | tail -5
```

Read each session file. Sessions can be large — for files over 200KB, read the first 500 and last 500 lines to capture the start and end of the conversation.

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
