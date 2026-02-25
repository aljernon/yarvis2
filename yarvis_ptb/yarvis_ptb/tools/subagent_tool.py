import datetime
import logging
import pathlib
from inspect import cleandoc

from yarvis_ptb.settings import BOT_USER_ID, DEFAULT_TIMEZONE
from yarvis_ptb.storage import DbMessage, create_agent, save_message
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

SUBAGENT_SYSTEM_PROMPT_PATH = (
    pathlib.Path(__file__).parent.parent.parent.parent
    / "core_knowledge"
    / "subagent-usage"
    / "SYSTEM_PROMPT.md"
)

SUBAGENT_MODEL = "claude-sonnet-4-5-20250929"


class RunSubagentTool(LocalTool):
    def __init__(self, curr, chat_id: int, bot):
        self._curr = curr
        self._chat_id = chat_id
        self._bot = bot

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
            ],
        )

    async def _execute(
        self, *, task: str, tools: str | None = None, **kwargs
    ) -> ToolResult:
        from yarvis_ptb.tool_sampler import process_subagent_query

        # 1. Create agent record
        agent_id = create_agent(
            self._curr,
            self._chat_id,
            meta={"task": task[:500], "tools": tools},
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
            message_params = await process_subagent_query(
                system=system,
                messages=messages,
                tool_names=tool_names,
                chat_id=self._chat_id,
                agent_id=agent_id,
                curr=self._curr,
                bot=self._bot,
                scope=parent_scope,
            )
        except Exception as e:
            logger.exception(f"Subagent {agent_id} failed: {e}")
            return ToolResult.error(f"Subagent failed: {e}")

        # 6. Save all message_params to DB with agent_id
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
            save_message(
                self._curr,
                DbMessage(
                    created_at=now,
                    chat_id=self._chat_id,
                    user_id=BOT_USER_ID,
                    message="USE_CONTENT_FROM_META",
                    meta={"message_params": message_params},
                    agent_id=agent_id,
                ),
            )

        # 7. Extract final text response
        final_text = _extract_final_text(message_params)
        if not final_text:
            return ToolResult.error("Subagent produced no text response")

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
