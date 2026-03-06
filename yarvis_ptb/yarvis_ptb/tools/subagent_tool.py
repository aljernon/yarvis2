from __future__ import annotations

import datetime
import logging
from inspect import cleandoc
from typing import TYPE_CHECKING

from yarvis_ptb.agent_config import DEFAULT_SUBAGENT_TOOL_SUBSET, AgentConfig, AgentMeta
from yarvis_ptb.agent_slugs import generate_agent_slug
from yarvis_ptb.debug_chat import add_debug_message_to_queue
from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES
from yarvis_ptb.on_disk_memory import load_skills_by_name
from yarvis_ptb.prompting import (
    build_claude_input,
    convert_db_messages_to_claude_messages,
    render_mesage_param_exact,
)
from yarvis_ptb.ptb_util import InterruptionScope
from yarvis_ptb.rendering_config import RenderingConfig
from yarvis_ptb.sampling import NoOpHooks, SamplingConfig, SamplingResult
from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE
from yarvis_ptb.settings.main import SUBAGENT_DEFAULT_MODEL, SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import (
    DbMessage,
    create_agent,
    get_agent_by_slug,
    get_messages,
    save_message,
    update_agent_meta,
)
from yarvis_ptb.tools.collect_message_tool import CollectMessageTool
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

if TYPE_CHECKING:
    from anthropic.types import MessageParam


logger = logging.getLogger(__name__)

# Maximum context length for agent conversations (in estimated tokens).
# When an agent's history exceeds this, it becomes "frozen" — new messages
# are saved but marked hidden and won't appear in future agent calls.
MAX_AGENT_CONTEXT_TOKENS = 50_000


def _render_history_for_subagent(
    db_messages: list[DbMessage],
) -> str:
    """Render main conversation history as text for inclusion in a subagent prompt.

    Large tool results (>=10KB) are truncated using the standard compactification
    logic, with get_tool_output references so the subagent can retrieve them.
    """
    claude_messages = convert_db_messages_to_claude_messages(
        db_messages,
        tool_result_truncation_after_n_turns=0,
    )
    lines: list[str] = []
    for msg in claude_messages:
        lines.extend(render_mesage_param_exact(msg))
    return "\n".join(lines)


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

                - Omit agent to create a new agent (returns its slug in the result)
                - Pass agent slug to continue an existing agent's conversation

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
                    name="agent",
                    type=str,
                    description="Agent slug to resume (e.g. 'swift-pine' or 'archive-2026-03-04'). Omit to create a new agent.",
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
                    description="Comma-separated skill names from the Core Knowledge Repository to include in the agent's system prompt. Only allowed when creating a new agent (without agent).",
                    is_required=False,
                ),
                ArgSpec(
                    name="include_message_history",
                    type=bool,
                    description="If true, the agent will see the currently visible main conversation messages as a rendered text block (a system message before the main request). Only allowed when creating a new agent (without agent).",
                    is_required=False,
                ),
            ],
        )

    # pyre-ignore[14]: Named params intentionally narrow **kwargs from base class
    async def _execute(
        self,
        *,
        message: str,
        agent: str | None = None,
        tools: str | None = None,
        model: str | None = None,
        skills: str | None = None,
        include_message_history: bool = False,
        **kwargs: object,
    ) -> ToolResult:
        if agent is not None:
            if skills is not None:
                return ToolResult.error(
                    "The 'skills' parameter can only be used when creating a new agent (without agent)."
                )
            if include_message_history:
                return ToolResult.error(
                    "The 'include_message_history' parameter can only be used when creating a new agent (without agent)."
                )
        else:
            # Create new agent
            agent, error = self._create_agent(
                message=message,
                tools=tools,
                model=model,
                skills=skills,
                include_message_history=include_message_history,
            )
            if error is not None:
                return error

        return await self._run_agent_request(
            agent_slug=agent, message=message, tools=tools, model=model
        )

    def _create_agent(
        self,
        *,
        message: str,
        tools: str | None,
        model: str | None,
        skills: str | None,
        include_message_history: bool,
    ) -> tuple[str, ToolResult | None]:
        """Create a new agent record. Returns (slug, error_or_None)."""
        model_short = model or SUBAGENT_DEFAULT_MODEL
        if model_short not in SUBAGENT_MODEL_MAP:
            return "", ToolResult.error(
                f"Unknown model '{model_short}'. Use: haiku, sonnet, or opus"
            )

        skill_names: list[str] = []
        if skills:
            skill_names = [s.strip() for s in skills.split(",") if s.strip()]

        # Validate skills exist
        if skill_names:
            _, missing = load_skills_by_name(skill_names)
            if missing:
                return "", ToolResult.error(f"Unknown skill(s): {', '.join(missing)}")

        tool_subset: list[str] = list(DEFAULT_SUBAGENT_TOOL_SUBSET)
        if tools:
            tool_subset = [t.strip() for t in tools.split(",") if t.strip()]

        agent_config = AgentConfig(
            description=message[:500],
            rendering=RenderingConfig(
                prompt_name="subagent",
                autoload_memory_logic=skill_names,
                list_all_memories=False,
                tool_result_truncation_after_n_turns=0,
            ),
            sampling=SamplingConfig(
                model=model_short,
                tool_subset=tool_subset,
            ),
        )
        agent_meta = AgentMeta(agent_config=agent_config)

        slug = generate_agent_slug()
        agent_id = create_agent(
            self._curr,
            self._chat_id,
            meta=agent_meta.model_dump(),
            slug=slug,
        )
        logger.info(f"Created agent {slug} (id={agent_id}) for chat {self._chat_id}")

        # Save main conversation history as initial messages under the agent
        if include_message_history:
            db_messages = get_messages(self._curr, chat_id=self._chat_id)
            if db_messages:
                history_text = _render_history_for_subagent(db_messages)
                now = datetime.datetime.now(DEFAULT_TIMEZONE)
                save_message(
                    self._curr,
                    DbMessage(
                        created_at=now,
                        chat_id=self._chat_id,
                        user_id=1,
                        message=f"<main_conversation_history>\n{history_text}\n</main_conversation_history>",
                        agent_id=agent_id,
                    ),
                )
                save_message(
                    self._curr,
                    DbMessage(
                        created_at=now,
                        chat_id=self._chat_id,
                        user_id=BOT_USER_ID,
                        message="Understood, I've read the main conversation history. What would you like me to do?",
                        agent_id=agent_id,
                    ),
                )

        return slug, None

    async def _run_agent_request(
        self,
        *,
        agent_slug: str,
        message: str,
        tools: str | None = None,
        model: str | None = None,
    ) -> ToolResult:
        """Load agent, build prompt, run query, save results."""
        # 1. Load agent by slug
        result = get_agent_by_slug(self._curr, self._chat_id, agent_slug)
        if result is None:
            return ToolResult.error(
                f"Agent '{agent_slug}' not found. Use run_subagent without agent to create a new one."
            )
        agent_id, raw_meta = result
        logger.info(
            f"Running agent {agent_slug} (id={agent_id}) for chat {self._chat_id}"
        )
        agent_meta = AgentMeta.model_validate(raw_meta)
        agent_config = agent_meta.agent_config

        # 2. Resolve model — args override, else fall back to agent's original
        if model:
            agent_config.sampling.model = model
        model_id = SUBAGENT_MODEL_MAP.get(agent_config.sampling.model)
        if model_id is None:
            return ToolResult.error(
                f"Unknown model '{agent_config.sampling.model}'. Use: haiku, sonnet, or opus"
            )

        # 3. Resolve tools — args override, else fall back to agent's original
        if tools is not None:
            agent_config.sampling.tool_subset = [
                t.strip() for t in tools.split(",") if t.strip()
            ]

        # 4. Check if agent is frozen
        frozen = (
            agent_meta.is_frozen
            or (agent_meta.last_prompt_tokens or 0) >= MAX_AGENT_CONTEXT_TOKENS
        )

        # 5. Build system prompt + conversation history
        db_msgs = get_messages(self._curr, self._chat_id, agent_id=agent_id)
        system, messages = build_claude_input(db_msgs, agent_config.rendering)

        # 6. Append new user message
        messages.append({"role": "user", "content": message})

        # 7. Build extra tools for frozen agents
        extra_tools: list[LocalTool] = []
        if agent_meta.is_frozen:
            extra_tools.append(CollectMessageTool())

        # 8. Run
        try:
            result, model_id = await self._run_agent(
                system=system,
                messages=messages,
                agent_config=agent_config,
                agent_id=agent_id,
                extra_tools=extra_tools,
            )
        except Exception as e:
            return ToolResult.error(
                f"Agent {agent_slug} failed with {type(e).__name__}: {e}"
            )

        # 9. Save and return
        return self._finalize(
            agent_id=agent_id,
            slug=agent_slug,
            message=message,
            result=result,
            model_id=model_id,
            frozen=frozen,
        )

    async def _run_agent(
        self,
        *,
        system: str,
        messages: list[MessageParam],
        agent_config: AgentConfig,
        agent_id: int,
        extra_tools: list[LocalTool] | None = None,
    ) -> tuple[SamplingResult, str]:
        """Run the agent query. Returns (SamplingResult, model_id)."""
        # Deferred import: circular dependency (tool_sampler imports subagent_tool)
        from yarvis_ptb.tool_sampler import (
            _DummyJobQueue,
            get_tools_for_agent_config,
            process_query,
        )

        parent_scope = None
        for s in reversed(INTERRUPTABLES):
            if s.chat_id == self._chat_id:
                parent_scope = s
                break
        if parent_scope is None:
            parent_scope = InterruptionScope(chat_id=self._chat_id, message_id=None)

        tools = get_tools_for_agent_config(
            agent_config, self._curr, self._chat_id, self._bot
        )
        if extra_tools:
            # Extra tools override by name (e.g. CollectMessageTool replaces SendMessageTool)
            extra_names = {t.name for t in extra_tools}
            tools = [t for t in tools if t.name not in extra_names] + extra_tools

        try:
            result = await process_query(
                system=system,
                messages=messages,
                agent_config=agent_config,
                tools=tools,
                hooks=NoOpHooks(),
                job_queue=_DummyJobQueue(),
                scope=parent_scope,
            )
        except Exception as e:
            logger.exception(f"Agent {agent_id} failed: {e}")
            raise

        model_id = agent_config.sampling.resolve_model_name()
        return result, model_id

    def _finalize(
        self,
        *,
        agent_id: int,
        slug: str,
        message: str,
        result: SamplingResult,
        model_id: str,
        frozen: bool = False,
    ) -> ToolResult:
        """Build cost info, save to DB, extract final text, return result."""
        # Deferred import: circular dependency (tool_sampler imports subagent_tool)
        from yarvis_ptb.tool_sampler import (
            MODEL_PRICING,
            cost_breakdown,
            estimate_cost,
        )

        message_params = result.message_params
        claude_calls = result.claude_calls

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
        add_debug_message_to_queue(f"**AGENT {slug}** (msg: {message[:100]})")
        if message_params:
            add_debug_message_to_queue(message_params)

        # Save new user message + bot response to DB
        # Frozen agents: skip saving entirely — ephemeral queries only exist
        # in the caller's message_params.
        if not frozen:
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
                )

        # Extract final text — prefer agent_messages (from send_message calls),
        # fall back to final assistant text
        if result.agent_messages:
            final_text = "\n\n".join(result.agent_messages)
        else:
            final_text = _extract_final_text(message_params)
        if not final_text:
            return ToolResult.error("Agent produced no text response")

        if subagent_usage:
            self.subagent_usages.append(subagent_usage)

        result_text = f"[Agent {slug} result]\n{final_text}"
        if frozen:
            result_text += (
                f"\n\n⚠️ Agent {slug} is FROZEN. This response was still generated "
                f"but the exchange was not added to the agent's persistent history. "
                f"Future calls will see the same context as before this call."
            )
        return ToolResult.success(result_text)


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
        create_all,
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

        create_all()

        with connect() as conn, conn.cursor() as curr:
            # 1. Create agent
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

            agent_config = AgentConfig(
                sampling=SamplingConfig(tool_subset=["python_repl"]),
            )
            tools = get_tools_for_agent_config(agent_config, curr, TEST_CHAT_ID, None)
            scope = InterruptionScope(chat_id=TEST_CHAT_ID, message_id=None)
            result = await process_query(
                system=system,
                messages=messages,
                agent_config=agent_config,
                tools=tools,
                hooks=NoOpHooks(),
                job_queue=_DummyJobQueue(),
                scope=scope,
            )
            message_params = result.message_params

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
