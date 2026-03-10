"""TodoWrite tool — tracks progress on multi-step tasks within a single invocation."""

import json
import logging

from yarvis_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class TodoWriteTool(LocalTool):
    """In-memory todo list for tracking progress on complex, multi-step tasks.

    State persists across tool calls within a single invocation (process_query loop),
    matching the Claude Code TodoWrite semantics.
    """

    def __init__(self):
        self._todos: list[dict] = []

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="todo_write",
            description=(
                "Use this tool to create and manage a todo list for tracking "
                "progress on multi-step tasks. Each call replaces the entire "
                "todo list with the provided items.\n\n"
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
                                "content": {
                                    "type": "string",
                                    "description": "Imperative description of the task (e.g. 'Fix authentication bug')",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current status of the task",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        todos: list[dict] = kwargs.pop("todos")
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        old_todos = self._todos
        self._todos = todos

        result = {
            "oldTodos": old_todos,
            "newTodos": todos,
        }
        return ToolResult.success(json.dumps(result))


def build_todo_tools() -> list[LocalTool]:
    return [TodoWriteTool()]
