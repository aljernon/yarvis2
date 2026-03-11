"""Todo tools — persistent per-agent todo list, stored in CKR."""

import json
import logging
import pathlib

from yarvis_ptb.on_disk_memory import MEMORY_PATH
from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

TODOS_DIR = MEMORY_PATH / "todos"


def _todos_path(agent_slug: str) -> pathlib.Path:
    return TODOS_DIR / f"{agent_slug}.json"


def read_todos(agent_slug: str) -> list[dict]:
    """Read todos for a given agent. Public for use in context rendering."""
    try:
        data = json.loads(_todos_path(agent_slug).read_text())
        if not isinstance(data, list):
            logger.warning("todos/%s.json is not a list, ignoring", agent_slug)
            return []
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_todos(agent_slug: str, todos: list[dict]) -> None:
    TODOS_DIR.mkdir(exist_ok=True)
    _todos_path(agent_slug).write_text(json.dumps(todos, indent=2) + "\n")


class TodoReadTool(LocalTool):
    """Read the current todo list."""

    def __init__(self, agent_slug: str):
        self._agent_slug = agent_slug

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="todo_read",
            description="Read the current todo list. Returns all todos with their id, content, status, and priority.",
            args={
                "type": "object",
                "properties": {},
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        todos = read_todos(self._agent_slug)
        if not todos:
            return ToolResult.success("No todos.")
        return ToolResult.success(json.dumps({"todos": todos}, indent=2))


class TodoWriteTool(LocalTool):
    """Write/replace the entire todo list. Persisted to CKR across invocations."""

    def __init__(self, agent_slug: str):
        self._agent_slug = agent_slug

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="todo_write",
            description=(
                "Create and manage a todo list for tracking progress on multi-step tasks. "
                "Each call replaces the entire todo list. Persists across invocations.\n\n"
                "Best practices:\n"
                "- Use for complex tasks with 3+ steps\n"
                "- Exactly ONE task should be in_progress at any time\n"
                "- Mark tasks completed IMMEDIATELY after finishing\n"
                "- Break complex tasks into smaller, actionable items\n"
                "- Only mark a task completed when FULLY accomplished"
            ),
            args={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The complete todo list (replaces any existing list)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Unique identifier for the task (e.g. '1', 'auth-fix')",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Imperative description of the task",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current status of the task",
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Priority level",
                                },
                            },
                            "required": ["id", "content", "status", "priority"],
                        },
                    }
                },
                "required": ["todos"],
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        todos: list[dict] = kwargs.pop("todos")
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        old_todos = read_todos(self._agent_slug)
        _write_todos(self._agent_slug, todos)

        return ToolResult.success(
            json.dumps({"oldTodos": old_todos, "newTodos": todos})
        )


def build_todo_tools(agent_slug: str) -> list[LocalTool]:
    return [TodoReadTool(agent_slug), TodoWriteTool(agent_slug)]
