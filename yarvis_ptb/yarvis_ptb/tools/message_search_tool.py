import datetime
import json
import textwrap

from yarvis_ptb.message_search import (
    SENTENCE_TRANSFORMER_AVAILABLE,
    InMemoryMessageGroupStore,
    get_in_memory_message_store,
)
from yarvis_ptb.settings import DEFAULT_TIMEZONE
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

DEFAULT_LIMIT = 5


class SearchMessagesTool(LocalTool):
    def __init__(self, store: InMemoryMessageGroupStore):
        self.store = store

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_message_history",
            description=textwrap.dedent("""
            Searches through history of messages using vector embeddings.

            The vector database consists of documents, where each document is
            time-bucketed groups of messages from user and assistant. Tool calls
            and thinking blocks were removed from the messages before creating a dcument.

            Each document is represented by a vector embedding, which is used to
            do semantic search of document that is the closst to the query.
            Query will also be transformed into a vector before performing th
            search

            Returns a list of message groups sorted by relevance, where each group contains:
            - Text content of the grouped messages
            - Start and end timestamps (min and max) of messages in the group
            - (If query provided) Score indicating how well the group matches the query when 1.0 is the maximum score.

            Search can be optionally constrained by start/end dates.
            """),
            args=[
                ArgSpec(
                    name="query",
                    type=str,
                    description="Text to search for in message history",
                    is_required=True,
                ),
                ArgSpec(
                    name="limit",
                    type=int,
                    description=f"Maximum number of messages to return (default {DEFAULT_LIMIT})",
                    is_required=False,
                ),
                ArgSpec(
                    name="start_date",
                    type=str,
                    description="Start date/datetime in ISO format",
                    is_required=False,
                ),
                ArgSpec(
                    name="end_date",
                    type=str,
                    description="End date/datetime in ISO format",
                    is_required=False,
                ),
            ],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        query = kwargs.pop("query")
        limit = kwargs.pop("limit", 5)
        if start_date := kwargs.pop("start_date", None):
            start_date = datetime.datetime.fromisoformat(start_date).astimezone(
                DEFAULT_TIMEZONE
            )
        if end_date := kwargs.pop("end_date", None):
            end_date = datetime.datetime.fromisoformat(end_date).astimezone(
                DEFAULT_TIMEZONE
            )

        assert not kwargs, f"Unexpected extra kwargs {kwargs}"
        results = self.store.search(
            query, limit=limit, start_date=start_date, end_date=end_date
        )
        output = []
        for msg_group, score in results:
            block: dict = dict(
                start_date=msg_group.start_date.isoformat(),
                end_date=msg_group.end_date.isoformat(),
                content=msg_group.content_for_search,
            )
            if score is not None:
                block["score"] = float(score)
            output.append(block)

        return ToolResult(
            text=json.dumps(output) if output else "No matching messages found"
        )


def build_message_search_tools(chat_id) -> list[LocalTool]:
    if not SENTENCE_TRANSFORMER_AVAILABLE:
        return []
    tools = []
    store = get_in_memory_message_store(chat_id)
    if store is not None:
        tools.append(SearchMessagesTool(store))
    return tools
