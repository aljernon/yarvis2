from __future__ import annotations

import datetime
import logging
import pathlib
from inspect import cleandoc
from typing import TYPE_CHECKING

from yarvis_ptb.on_disk_memory import load_skills_by_name
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE
from yarvis_ptb.settings.main import SUBAGENT_DEFAULT_MODEL, SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    create_agent,
    get_agent_meta,
    get_messages,
    save_message,
    update_agent_meta,
)
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

if TYPE_CHECKING:
    from anthropic.types import MessageParam

    from yarvis_ptb.tool_sampler import ClaudeCallInfo

logger = logging.getLogger(__name__)

# Maximum context length for agent conversations (in estimated tokens).
# When an agent's history exceeds this, it becomes "frozen" — new messages
# are saved but marked hidden and won't appear in future agent calls.
MAX_AGENT_CONTEXT_TOKENS = 50_000

SUBAGENT_SYSTEM_PROMPT_PATH = (
    pathlib.Path(__file__).parent.parent.parent.parent
    / "core_knowledge"
    / "subagent-usage"
    / "SYSTEM_PROMPT.md"
)


class RunSubagentTool(LocalTool):
    def __init__(self, curr, chat_id: int, bot):
        self._curr = curr
        self._chat_id = chat_id
        self._bot = bot
        self.subagent_usages: list[dict] = []

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_subagent",
            description=cleandoc(f"""
                Runs a message in an agent context. Agents are separate Claude conversations
                with their own tool access and history, stored separately from the main
                conversation.

                - Omit agent_id to create a new agent (returns its ID in the result)
                - Pass agent_id to continue an existing agent's conversation

                Agents have a context limit of {MAX_AGENT_CONTEXT_TOKENS} tokens. Once
                exceeded, the agent becomes "frozen": it still responds, but the exchange
                is not added to its persistent history. Create a new agent to continue.

                Use for: research, multi-step computation, context-heavy work, any task
                that doesn't need main conversation history.
                """),
            args=[
                ArgSpec(
                    name="message",
                    type=str,
                    description="The message to send to the agent. For new agents, this should be self-contained since the agent has no access to the main conversation.",
                    is_required=True,
                ),
                ArgSpec(
                    name="agent_id",
                    type=int,
                    description="Continue a previous agent's conversation. The agent retains its full history.",
                    is_required=False,
                ),
                ArgSpec(
                    name="tools",
                    type=str,
                    description="Comma-separated tool names the agent should have access to. Default: python_repl,bash_run,editor",
                    is_required=False,
                ),
                ArgSpec(
                    name="model",
                    type=str,
                    description="Model to use: haiku, sonnet, or opus. Default: haiku",
                    is_required=False,
                ),
                ArgSpec(
                    name="skills",
                    type=str,
                    description="Comma-separated skill names from the Core Knowledge Repository to include in the agent's system prompt. Only allowed when creating a new agent (no agent_id).",
                    is_required=False,
                ),
            ],
        )

    # pyre-ignore[14]: Named params intentionally narrow **kwargs from base class
    async def _execute(
        self,
        *,
        message: str,
        agent_id: int | None = None,
        tools: str | None = None,
        model: str | None = None,
        skills: str | None = None,
        **kwargs: object,
    ) -> ToolResult:
        if agent_id is not None:
            if skills is not None:
                return ToolResult.error(
                    "The 'skills' parameter can only be used when creating a new agent (without agent_id)."
                )
            return await self._execute_resume(
                agent_id=agent_id, message=message, tools=tools, model=model
            )
        return await self._execute_new(
            message=message, tools=tools, model=model, skills=skills
        )

    async def _execute_new(
        self,
        *,
        message: str,
        tools: str | None = None,
        model: str | None = None,
        skills: str | None = None,
    ) -> ToolResult:
        # 0. Resolve model
        model_short = model or SUBAGENT_DEFAULT_MODEL
        model_id = SUBAGENT_MODEL_MAP.get(model_short)
        if model_id is None:
            return ToolResult.error(
                f"Unknown model '{model_short}'. Use: haiku, sonnet, or opus"
            )

        # 1. Create agent record
        agent_id = create_agent(
            self._curr,
            self._chat_id,
            meta={"task": message[:500], "tools": tools, "model": model_short},
        )
        logger.info(f"Created agent {agent_id} for chat {self._chat_id}")

        # 2. Build system prompt
        system = _load_subagent_system_prompt()

        # 2a. Inject requested skills into system prompt
        if skills:
            skill_names = [s.strip() for s in skills.split(",") if s.strip()]
            skill_content, missing = load_skills_by_name(skill_names)
            if missing:
                return ToolResult.error(f"Unknown skill(s): {', '.join(missing)}")
            if skill_content:
                system = f"{system}\n\n=== Reference Knowledge ===\nThe following skill files were provided to help you with this task.\n\n{skill_content}"

        # 3. Build messages — single user message
        messages: list[MessageParam] = [{"role": "user", "content": message}]

        # 4. Parse tool names
        tool_names = _parse_tool_names(tools)

        # 5. Run the agent query
        try:
            message_params, claude_calls, model_id = await self._run_agent(
                system=system,
                messages=messages,
                tool_names=tool_names,
                agent_id=agent_id,
                model_id=model_id,
            )
        except Exception as e:
            return ToolResult.error(f"Agent failed with {type(e).__name__}: {e}")

        # 6. Save messages and return
        return self._finalize(
            agent_id=agent_id,
            message=message,
            message_params=message_params,
            claude_calls=claude_calls,
            model_id=model_id,
        )

    async def _execute_resume(
        self,
        *,
        agent_id: int,
        message: str,
        tools: str | None = None,
        model: str | None = None,
    ) -> ToolResult:
        # 1. Load agent meta
        agent_meta = get_agent_meta(self._curr, agent_id)
        if agent_meta is None:
            return ToolResult.error(
                f"Agent #{agent_id} not found. Use run_subagent without agent_id to create a new one."
            )
        logger.info(f"Resuming agent {agent_id} for chat {self._chat_id}")

        # 2. Resolve model — args override, else fall back to agent's original
        model_short = model or agent_meta.get("model") or SUBAGENT_DEFAULT_MODEL
        model_id = SUBAGENT_MODEL_MAP.get(model_short)
        if model_id is None:
            return ToolResult.error(
                f"Unknown model '{model_short}'. Use: haiku, sonnet, or opus"
            )

        # 3. Resolve tools — args override, else fall back to agent's original
        effective_tools = tools if tools is not None else agent_meta.get("tools")
        tool_names = _parse_tool_names(effective_tools)

        # 4. Check if agent is frozen (context too large from previous run)
        frozen = (agent_meta.get("last_prompt_tokens") or 0) >= MAX_AGENT_CONTEXT_TOKENS

        # 5. Rebuild conversation history from DB
        db_msgs = get_messages(self._curr, self._chat_id, agent_id=agent_id)
        messages: list[MessageParam] = []
        for msg in db_msgs:
            if msg.user_id == BOT_USER_ID:
                # Bot message — expand message_params into the conversation
                if msg.meta and "message_params" in msg.meta:
                    messages.extend(msg.meta["message_params"])
            else:
                # User message
                messages.append({"role": "user", "content": msg.message})

        # 6. Append new user message
        messages.append({"role": "user", "content": message})

        # 7. Build system prompt and run
        system = _load_subagent_system_prompt()
        try:
            message_params, claude_calls, model_id = await self._run_agent(
                system=system,
                messages=messages,
                tool_names=tool_names,
                agent_id=agent_id,
                model_id=model_id,
            )
        except Exception as e:
            return ToolResult.error(
                f"Agent #{agent_id} failed with {type(e).__name__}: {e}"
            )

        # 8. Save and return (hidden if frozen)
        return self._finalize(
            agent_id=agent_id,
            message=message,
            message_params=message_params,
            claude_calls=claude_calls,
            model_id=model_id,
            frozen=frozen,
        )

    async def _run_agent(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        tool_names: list[str],
        agent_id: int,
        model_id: str,
    ) -> tuple[list[MessageParam], list[ClaudeCallInfo], str]:
        """Run the agent query. Returns (message_params, claude_calls, model_id)."""
        from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES
        from yarvis_ptb.tool_sampler import process_subagent_query

        parent_scope = None
        for s in reversed(INTERRUPTABLES):
            if s.chat_id == self._chat_id:
                parent_scope = s
                break

        try:
            message_params, claude_calls = await process_subagent_query(
                system=system,
                messages=messages,
                tool_names=tool_names,
                chat_id=self._chat_id,
                agent_id=agent_id,
                curr=self._curr,
                bot=self._bot,
                scope=parent_scope,
                model_name=model_id,
            )
        except Exception as e:
            logger.exception(f"Agent {agent_id} failed: {e}")
            raise

        return message_params, claude_calls, model_id

    def _finalize(
        self,
        *,
        agent_id: int,
        message: str,
        message_params: list[MessageParam],
        claude_calls: list[ClaudeCallInfo],
        model_id: str,
        frozen: bool = False,
    ) -> ToolResult:
        """Build cost info, save to DB, extract final text, return result."""
        from yarvis_ptb.debug_chat import add_debug_message_to_queue
        from yarvis_ptb.tool_sampler import (
            MODEL_PRICING,
            cost_breakdown,
            estimate_cost,
        )

        subagent_usage = None
        if claude_calls:
            pricing = MODEL_PRICING.get(model_id)
            subagent_usage = {
                "model": model_id,
                "calls": [c.to_usage_dict(pricing) for c in claude_calls],
                "estimated_cost_usd": estimate_cost(claude_calls, model_id),
                "cost_breakdown_usd": cost_breakdown(claude_calls, model_id),
            }

        # Track last prompt tokens in agent meta for frozen detection
        if claude_calls:
            last_prompt_tokens = max(c.num_prompt_tokens for c in claude_calls)
            update_agent_meta(
                self._curr, agent_id, {"last_prompt_tokens": last_prompt_tokens}
            )

        # Debug chat
        add_debug_message_to_queue(f"**AGENT #{agent_id}** (msg: {message[:100]})")
        if message_params:
            add_debug_message_to_queue(message_params)

        # Save new user message + bot response to DB
        # If frozen, save as hidden (is_visible=False) so they won't be loaded on
        # future resumes — the agent's persistent context stays fixed.
        is_visible = not frozen
        now = datetime.datetime.now(DEFAULT_TIMEZONE)
        save_message(
            self._curr,
            DbMessage(
                created_at=now,
                chat_id=self._chat_id,
                user_id=1,  # User message
                message=message,
                agent_id=agent_id,
            ),
            is_visible=is_visible,
        )
        if message_params:
            bot_meta: dict = {"message_params": message_params}
            if subagent_usage:
                bot_meta["usage"] = subagent_usage
            save_message(
                self._curr,
                DbMessage(
                    created_at=now,
                    chat_id=self._chat_id,
                    user_id=BOT_USER_ID,
                    message="USE_CONTENT_FROM_META",
                    meta=bot_meta,
                    agent_id=agent_id,
                ),
                is_visible=is_visible,
            )

        # Extract final text response
        final_text = _extract_final_text(message_params)
        if not final_text:
            return ToolResult.error("Agent produced no text response")

        if subagent_usage:
            self.subagent_usages.append(subagent_usage)

        result = f"[Agent #{agent_id} result]\n{final_text}"
        if frozen:
            result += (
                f"\n\n⚠️ Agent #{agent_id} is FROZEN: its context exceeded "
                f"{MAX_AGENT_CONTEXT_TOKENS} tokens. This response was still generated "
                f"but the exchange was not added to the agent's persistent history. "
                f"Future calls will see the same context as before this call. "
                f"Consider creating a new agent for continued work."
            )
        return ToolResult.success(result)


def _parse_tool_names(tools: str | None) -> list[str]:
    """Parse comma-separated tool names, or return defaults."""
    if tools:
        return [t.strip() for t in tools.split(",") if t.strip()]
    return ["python_repl", "bash_run", "editor"]


def _load_subagent_system_prompt() -> str:
    """Load the subagent system prompt from the core_knowledge file."""
    try:
        prompt = SUBAGENT_SYSTEM_PROMPT_PATH.read_text()
    except FileNotFoundError:
        logger.warning(
            f"Subagent system prompt not found at {SUBAGENT_SYSTEM_PROMPT_PATH}, using default"
        )
        prompt = "You are a subagent. Complete the given task and return your findings concisely."

    now = datetime.datetime.now(DEFAULT_TIMEZONE)
    prompt += f"\n\nCurrent date and time: {now.strftime('%Y-%m-%d %H:%M %Z')}"
    return prompt


def _extract_final_text(message_params: list[MessageParam]) -> str | None:
    """Extract the final text from message_params (last assistant turn)."""
    for msg in reversed(message_params):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, str):
                return content
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            if texts:
                return "\n".join(texts)
    return None


def build_subagent_tools(curr, chat_id: int, bot) -> list[LocalTool]:
    return [RunSubagentTool(curr, chat_id, bot)]


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    from yarvis_ptb.settings import load_env

    load_env()

    from yarvis_ptb.storage import (
        connect,
        craete_all,
        get_messages,
    )

    # ── Unit test: _extract_final_text with synthetic data ──

    def test_extract_final_text():
        print("\n=== Testing _extract_final_text ===")

        # String content
        assert (
            _extract_final_text([{"role": "assistant", "content": "hello"}]) == "hello"
        )

        # Block content
        params = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "python_repl",
                        "input": {"code": "2+2"},
                        "id": "t1",
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "4"}],
                        "tool_use_id": "t1",
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "The answer is 4."},
                ],
            },
        ]
        assert _extract_final_text(params) == "The answer is 4."

        # Multiple text blocks
        params2 = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Line 1"},
                    {"type": "text", "text": "Line 2"},
                ],
            }
        ]
        assert _extract_final_text(params2) == "Line 1\nLine 2"

        # Empty
        assert _extract_final_text([]) is None
        assert _extract_final_text([{"role": "user", "content": "hi"}]) is None

        print("All _extract_final_text tests passed!")

    test_extract_final_text()

    # ── Integration test: full subagent flow ──

    TEST_CHAT_ID = -999999  # unlikely to collide

    async def test_subagent_integration():
        print("\n=== Integration test: subagent flow ===")
        from yarvis_ptb.tool_sampler import process_subagent_query

        craete_all()

        with connect() as conn, conn.cursor() as curr:
            # 1. Create agent
            from yarvis_ptb.storage import create_agent

            agent_id = create_agent(curr, TEST_CHAT_ID, meta={"test": True})
            print(f"Created agent_id={agent_id}")

            # 2. Run subagent query
            system = "You are a test subagent. Complete the task concisely."
            messages = [
                {
                    "role": "user",
                    "content": "What is 2+2? Use python_repl to compute it.",
                }
            ]

            message_params, _claude_calls = await process_subagent_query(
                system=system,
                messages=messages,
                tool_names=["python_repl"],
                chat_id=TEST_CHAT_ID,
                agent_id=agent_id,
                curr=curr,
                bot=None,
            )

            # 3. Verify message_params non-empty
            assert message_params, "message_params should not be empty"
            print(f"Got {len(message_params)} message param entries")

            # 4. Verify final text extractable
            final_text = _extract_final_text(message_params)
            assert final_text, "Should extract final text"
            print(f"Final text: {final_text[:200]}")

            # 5. Save messages to DB (mimic what _execute does)
            now = datetime.datetime.now(DEFAULT_TIMEZONE)
            save_message(
                curr,
                DbMessage(
                    created_at=now,
                    chat_id=TEST_CHAT_ID,
                    user_id=1,
                    message="What is 2+2? Use python_repl to compute it.",
                    agent_id=agent_id,
                ),
            )
            save_message(
                curr,
                DbMessage(
                    created_at=now,
                    chat_id=TEST_CHAT_ID,
                    user_id=BOT_USER_ID,
                    message="USE_CONTENT_FROM_META",
                    meta={"message_params": message_params},
                    agent_id=agent_id,
                ),
            )

            # 6. Verify messages retrievable with agent_id
            agent_msgs = get_messages(curr, TEST_CHAT_ID, agent_id=agent_id)
            assert (
                len(agent_msgs) >= 2
            ), f"Expected >=2 agent messages, got {len(agent_msgs)}"
            print(
                f"get_messages(agent_id={agent_id}) returned {len(agent_msgs)} messages"
            )

            # 7. Verify get_messages(agent_id=None) does NOT return them
            main_msgs = get_messages(curr, TEST_CHAT_ID, agent_id=None)
            agent_msg_ids = {m.agent_id for m in main_msgs}
            assert (
                agent_id not in agent_msg_ids
            ), "Main messages should not include subagent messages"
            print("get_messages(agent_id=None) correctly excludes subagent messages")

            # 8. Cleanup
            curr.execute(
                "DELETE FROM messages WHERE chat_id = %s AND agent_id = %s",
                (TEST_CHAT_ID, agent_id),
            )
            curr.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
            print(f"Cleaned up agent {agent_id} and its messages")

            print("\n=== Integration test PASSED ===")

    asyncio.run(test_subagent_integration())
