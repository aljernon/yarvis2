import datetime
import logging
import pathlib
from inspect import cleandoc

from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE
from yarvis_ptb.settings.main import SUBAGENT_DEFAULT_MODEL, SUBAGENT_MODEL_MAP
from yarvis_ptb.storage import DbMessage, create_agent, save_message
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

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
            description=cleandoc("""
                Delegates a task to a subagent — a separate Claude conversation that runs
                autonomously with its own tool access. The subagent completes the task and
                returns its findings. Its messages are stored separately and don't pollute
                the main conversation context.

                Use this for:
                - Research tasks that require multiple tool calls
                - Tasks that would consume too much main context
                - Independent subtasks that can run in isolation

                The subagent has no access to the main conversation history or memory.
                It receives only the task description you provide.
                """),
            args=[
                ArgSpec(
                    name="task",
                    type=str,
                    description="The task/question for the subagent. Be specific and self-contained — the subagent has no access to the main conversation.",
                    is_required=True,
                ),
                ArgSpec(
                    name="tools",
                    type=str,
                    description="Comma-separated tool names the subagent should have access to. Default: python_repl,bash_run,editor",
                    is_required=False,
                ),
                ArgSpec(
                    name="model",
                    type=str,
                    description="Model to use: haiku, sonnet, or opus. Default: haiku",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        task: str,
        tools: str | None = None,
        model: str | None = None,
        **kwargs,
    ) -> ToolResult:
        from yarvis_ptb.tool_sampler import process_subagent_query

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
            meta={"task": task[:500], "tools": tools, "model": model_short},
        )
        logger.info(f"Created subagent {agent_id} for chat {self._chat_id}")

        # 2. Build system prompt
        system = _load_subagent_system_prompt()

        # 3. Build messages — single user message with the task
        messages = [{"role": "user", "content": task}]

        # 4. Parse tool names
        if tools:
            tool_names = [t.strip() for t in tools.split(",") if t.strip()]
        else:
            tool_names = ["python_repl", "bash_run", "editor"]

        # 5. Run the subagent query (inherit parent's interruption scope if available)
        from yarvis_ptb.interruption_scope_internal import INTERRUPTABLES

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
            logger.exception(f"Subagent {agent_id} failed: {e}")
            return ToolResult.error(f"Subagent failed: {e}")

        # Build cost info for the parent invocation to aggregate
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

        # 6. Send subagent activity to debug chat
        from yarvis_ptb.debug_chat import add_debug_message_to_queue

        add_debug_message_to_queue(f"**SUBAGENT #{agent_id}** (task: {task[:100]})")
        if message_params:
            add_debug_message_to_queue(message_params)

        # 7. Save all message_params to DB with agent_id
        now = datetime.datetime.now(DEFAULT_TIMEZONE)
        save_message(
            self._curr,
            DbMessage(
                created_at=now,
                chat_id=self._chat_id,
                user_id=1,  # User message (the task)
                message=task,
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

        # 8. Extract final text response
        final_text = _extract_final_text(message_params)
        if not final_text:
            return ToolResult.error("Subagent produced no text response")

        if subagent_usage:
            self.subagent_usages.append(subagent_usage)

        return ToolResult.success(f"[Subagent #{agent_id} result]\n{final_text}")


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


def _extract_final_text(message_params: list[dict]) -> str | None:
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
