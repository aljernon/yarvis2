import contextlib
import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import psycopg2

from yarvis_ptb.queries import (
    INIT_AGENTS_QUERY,
    INIT_INVOCATIONS_QUERY,
    INIT_MEMORY_QUERY,
    INIT_MESSAGES_QUERY,
    INIT_VARIABLES_QUERY,
    INIT_VECTOR,
    MIGRATE_MESSAGES_AGENT_ID,
)
from yarvis_ptb.settings import (
    BOT_USER_ID,
    DEFAULT_TIMEZONE,
)

logger = logging.getLogger(__name__)

# read database connection url from the enivron variable we just set.
DATABASE_URL = os.environ.get("DATABASE_URL")


IMAGE_B64_META_FIELD = "image_b64"


class MemoryType(TypedDict):
    id: str
    content: str
    created_at: str
    meta: dict[str, Any]


@dataclass
class DbMessage:
    created_at: datetime.datetime
    chat_id: int
    user_id: int
    message: str
    marked_for_archive: bool = False
    meta: dict | None = None
    message_id: int | None = None
    agent_id: int | None = None

    def is_bot(self):
        return self.user_id == BOT_USER_ID


@dataclass
class DbScheduledInvocation:
    scheduled_at: datetime.datetime
    is_recurring: bool
    chat_id: int
    reason: str
    is_active: bool = True
    meta: dict = field(default_factory=dict)
    scheduled_id: int | None = None


@dataclass
class Invocation:
    invocation_type: Literal["reply", "schedule", "context_overflow", "reply_timeout"]
    db_invocation: DbScheduledInvocation | None = None
    reply_to_message_id: int | None = None


@dataclass
class MemoryUpdateRequest:
    new_memories: list[MemoryType] = field(default_factory=list)
    deleted_memory_ids: list[str] = field(default_factory=list)


@dataclass
class InvokeUpdateRequest:
    new_invoke: datetime.datetime | None = None


class VariablesForChat:
    KILL_SWITCH = "KILL_SWITCH"

    def __init__(self, curr, chat_id: int):
        self.chat_id = chat_id
        self.curr = curr
        self._read_all()

    def _read_all(self):
        self.variables = {}
        self.curr.execute(
            """
            SELECT name, value, datatype
            FROM chat_variables
            WHERE (chat_id = %s OR chat_id IS NULL) and datatype != %s
            ORDER BY chat_id NULLS FIRST
            """,
            (self.chat_id, "none"),
        )
        for row in self.curr.fetchall():
            name, value, type_str = row
            if type_str == "str":
                self.variables[name] = str(value)
            elif type_str == "bool":
                self.variables[name] = {"true": True, "false": False}[value]
            elif type_str == "int":
                self.variables[name] = int(value)
            elif type_str == "datetime":
                self.variables[name] = datetime.datetime.fromisoformat(
                    value
                ).astimezone(DEFAULT_TIMEZONE)
            else:
                raise ValueError(f"Unknown variable type: {type_str}")

    def _prepare_value_for_set(self, value: Any) -> tuple[str, Any]:
        if isinstance(value, str):
            type_str = "str"
            db_value = value
        elif isinstance(value, bool):
            type_str = "bool"
            db_value = "true" if value else "false"
        elif isinstance(value, int):
            type_str = "int"
            db_value = str(value)
        elif isinstance(value, datetime.datetime):
            type_str = "datetime"
            assert value.tzinfo is not None, "Datetime must be localized"
            db_value = value.isoformat()
        elif value is None:
            type_str = "none"
            db_value = "none"
        else:
            raise ValueError(f"Unsupported variable type: {type(value)}")
        return type_str, db_value

    def get(self, variable_name: str, default_value: Any = None):
        return self.variables.get(variable_name, default_value)

    def set(self, variable_name: str, value: Any):
        type_str, db_value = self._prepare_value_for_set(value)
        self.curr.execute(
            """
            INSERT INTO chat_variables (chat_id, name, value, datatype)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chat_id, name)
            DO UPDATE SET value = EXCLUDED.value,
                           datatype = EXCLUDED.datatype
        """,
            (self.chat_id, variable_name, db_value, type_str),
        )
        self.variables[variable_name] = value

    def set_global(self, variable_name: str, value: Any):
        type_str, db_value = self._prepare_value_for_set(value)
        self.curr.execute(
            """
            INSERT INTO chat_variables (chat_id, name, value, datatype)
            VALUES (NULL, %s, %s, %s)
            ON CONFLICT (chat_id, name)
            DO UPDATE SET value = EXCLUDED.value,
                       datatype = EXCLUDED.datatype
        """,
            (variable_name, db_value, type_str),
        )
        # Just re-read as we don't know about priorities local vs NULL.'
        self._read_all()


@contextlib.contextmanager
def connect():
    assert DATABASE_URL is not None
    con = None
    try:
        # create a new database connection by calling the connect() function
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True

        #  create a new cursor
        with conn.cursor() as cur:
            # execute an SQL statement to get the HerokuPostgres database version
            cur.execute("SELECT version()")
            db_version = cur.fetchone()
            logger.info("Server version: %s", db_version)

        yield conn

    finally:
        # close the communication with the database server by calling the close()
        if con is not None:
            con.close()
            print("Database connection closed.")


def get_messages(
    curr, chat_id: int, limit: int | None = None, agent_id: int | None = None
) -> list[DbMessage]:
    """Gets last limit messages from the DB for the chat sorted ASC by created_at.

    agent_id=None (default) returns only live agent messages (WHERE agent_id IS NULL).
    agent_id=N returns only subagent messages for that agent.
    """
    if limit is None:
        limit = 100000000
    if agent_id is None:
        agent_filter = "AND agent_id IS NULL"
        params = (chat_id, limit)
    else:
        agent_filter = "AND agent_id = %s"
        params = (chat_id, agent_id, limit)
    curr.execute(
        f"""
        SELECT created_at, chat_id, user_id, message, meta, id, marked_for_archive, agent_id
        FROM messages
        WHERE chat_id = %s
        AND is_visible = true
        {agent_filter}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        params,
    )
    rows = curr.fetchall()
    messages = []
    for row in list(rows)[::-1]:
        messages.append(
            DbMessage(
                created_at=row[0].astimezone(DEFAULT_TIMEZONE),
                chat_id=row[1],
                user_id=row[2],
                message=row[3],
                meta=row[4],
                message_id=row[5],
                marked_for_archive=row[6],
                agent_id=row[7],
            )
        )
    return messages


def save_message(curr, message: DbMessage, *, is_visible: bool = True):
    """Save message to the dbwith connect() as curr"""
    assert message.message_id is None, "Will be auto-generated"
    curr.execute(
        """
            INSERT INTO messages (created_at, chat_id, user_id, message, meta, marked_for_archive, agent_id, is_visible)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
        (
            message.created_at,
            message.chat_id,
            message.user_id,
            message.message,
            json.dumps(message.meta),
            message.marked_for_archive,
            message.agent_id,
            is_visible,
        ),
    )


def mark_message_for_archive(curr, chat_id: int, message_id: int):
    curr.execute(
        """
        UPDATE messages
        SET marked_for_archive = true
        WHERE chat_id = %s AND id = %s
        """,
        (chat_id, message_id),
    )


def archive_marked_messages(curr, chat_id: int):
    curr.execute(
        """
        UPDATE messages
        SET is_visible = false
        WHERE chat_id = %s AND marked_for_archive = true
        """,
        (chat_id,),
    )


def hide_single_message(curr, chat_id: int, message_id: int):
    """Sets is_visible=false for athe message"""
    curr.execute(
        """
        UPDATE messages
        SET is_visible = false
        WHERE chat_id = %s AND id = %s
        """,
        (chat_id, message_id),
    )


def hide_message_history(curr, chat_id: int):
    """Sets is_visible=false for all messages in the specified chat"""
    curr.execute(
        """
            UPDATE messages
            SET is_visible = false
            WHERE chat_id = %s
            """,
        (chat_id,),
    )


def get_memories(curr, chat_id: int) -> list[MemoryType]:
    curr.execute(
        """
        SELECT created_at, chat_id, mem_id, content, extra
        FROM memories
        WHERE chat_id = %s AND active=true
        ORDER BY created_at ASC
        """,
        (chat_id,),
    )
    memories = []
    for row in curr.fetchall():
        memories.append(
            dict(
                created_at=str(row[0]),
                chat_id=row[1],
                id=row[2],
                content=row[3],
                meta=row[4],
            )
        )
    return memories


def update_memory(curr, chat_id: int, update: MemoryUpdateRequest):
    for delete_id in update.deleted_memory_ids:
        curr.execute(
            "UPDATE memories SET active=false WHERE mem_id=%s and chat_id=%s",
            (delete_id, chat_id),
        )
    for mem in update.new_memories:
        curr.execute(
            """
            INSERT INTO memories (created_at, chat_id, mem_id, content, extra)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                mem["created_at"],
                chat_id,
                mem["id"],
                mem["content"],
                json.dumps(mem["meta"]),
            ),
        )


def update_single_memory(curr, chat_id: int, memory_id: str, content: str) -> bool:
    """Updates a memory. Returns True if memory exists and was updated"""
    curr.execute(
        """
        UPDATE memories
        SET content = %s
        WHERE chat_id = %s AND mem_id = %s AND active=true
        """,
        (content, chat_id, memory_id),
    )
    return curr.rowcount == 1


def sync_memory_db_with_dump(curr, chat_id: int, dump: list[dict]) -> str:
    """Loads memory from a dump, returns string describing what was updated.

    The dump should be a list of dicts, each dict containing memory fields.
    New memory ids are treated as an error. Existing memories will be updated.
    Memory not in the dump will be marked as non-active."""

    # Get current memory state
    curr_memories = get_memories(curr, chat_id)
    curr_ids = {m["id"] for m in curr_memories}
    dump_ids = {m["id"] for m in dump}

    # Check for new IDs - that would be an error
    if not dump_ids.issubset(curr_ids):
        new_ids = dump_ids - curr_ids
        logger.error(f"New memory IDs found in dump: {new_ids}")
        return f"Error: Found new memory IDs in dump that don't exist: {new_ids}"

    # Update all existing memories in dump that differ
    updated_ids = []
    curr_memories_dict = {m["id"]: m for m in curr_memories}

    for memory in dump:
        curr_memory = curr_memories_dict[memory["id"]]
        if (
            curr_memory["content"] != memory["content"]
            or curr_memory["meta"] != memory["meta"]
            or curr_memory["created_at"] != memory["created_at"]
        ):
            curr.execute(
                """
                UPDATE memories
                SET content = %s,
                    extra = %s,
                    created_at = %s
                WHERE mem_id = %s AND chat_id = %s
                """,
                (
                    memory["content"],
                    json.dumps(memory["meta"]),
                    datetime.datetime.fromisoformat(memory["created_at"]).astimezone(
                        DEFAULT_TIMEZONE
                    ),
                    memory["id"],
                    chat_id,
                ),
            )
            updated_ids.append(memory["id"])

    # Mark memories not in dump as non-active
    ids_to_deactivate = curr_ids - dump_ids
    if ids_to_deactivate:
        curr.execute(
            """
            UPDATE memories
            SET active = false
            WHERE mem_id = ANY(%s) AND chat_id = %s
            """,
            (list(ids_to_deactivate), chat_id),
        )

    return (
        f"Updated memories with IDs: {updated_ids if updated_ids else 'none'}. "
        f"Deactivated memories with IDs: {list(ids_to_deactivate) if ids_to_deactivate else 'none'}"
    )


def get_scheduled_invocations(
    curr, chat_id: int | None = None
) -> list[DbScheduledInvocation]:
    """Gets active invocations from the DB for the chat"""
    if chat_id is None:
        curr.execute(
            """
        SELECT scheduled_at, chat_id, is_active, reason, meta, id, is_recurring
        FROM invocations
        WHERE is_active = true
        ORDER BY scheduled_at ASC
        """
        )
    else:
        curr.execute(
            """
        SELECT scheduled_at, chat_id, is_active, reason, meta, id, is_recurring
        FROM invocations
        WHERE chat_id = %s AND is_active = true
        ORDER BY scheduled_at ASC
        """,
            (chat_id,),
        )
    rows = curr.fetchall()
    invocations = []
    for row in list(rows):
        invocations.append(
            DbScheduledInvocation(
                scheduled_at=row[0].astimezone(DEFAULT_TIMEZONE),
                chat_id=row[1],
                is_active=row[2],
                reason=row[3],
                meta=row[4],
                scheduled_id=row[5],
                is_recurring=row[6],
            )
        )
    return invocations


def save_invocation(curr, invocation: DbScheduledInvocation) -> int:
    """Save invocation to the db"""
    assert invocation.scheduled_id is None, "Will be auto-generated"
    assert invocation.is_active, "Only active invocations can be saved"
    curr.execute(
        """
       INSERT INTO invocations (created_at, scheduled_at, chat_id, is_active, reason, meta, is_recurring)
       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
       """,
        (
            datetime.datetime.now(DEFAULT_TIMEZONE),
            invocation.scheduled_at,
            invocation.chat_id,
            invocation.is_active,
            invocation.reason,
            json.dumps(invocation.meta),
            invocation.is_recurring,
        ),
    )
    return curr.fetchone()[0]


def bump_recurring_invocation(curr, invocation: DbScheduledInvocation):
    """Set invocation as non-active"""
    assert invocation.scheduled_id is not None, "Should be saved first"
    curr.execute(
        """
       UPDATE invocations
       SET scheduled_at = %s
       WHERE chat_id = %s AND id = %s
       """,
        (
            invocation.scheduled_at + datetime.timedelta(days=1),
            invocation.chat_id,
            invocation.scheduled_id,
        ),
    )


def set_non_active_invocation(curr, invocation: DbScheduledInvocation):
    """Set invocation as non-active"""
    assert invocation.scheduled_id is not None, "Should be saved first"
    curr.execute(
        """
       UPDATE invocations
       SET is_active = false
       WHERE chat_id = %s AND id = %s
       """,
        (invocation.chat_id, invocation.scheduled_id),
    )


def test_messages():
    from yarvis_ptb.prompting import convert_db_messages_to_claude_messages

    test_chat_id = 123123123
    with connect() as conn, conn.cursor() as curr:
        try:
            # Create a test message
            test_message = DbMessage(
                created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                chat_id=test_chat_id,
                user_id=456,
                message="Test message",
                meta={"test": "metadata"},
            )

            # Save message
            save_message(curr, test_message)

            # Read messages
            messages = get_messages(curr, chat_id=test_chat_id)
            assert len(messages) == 1, messages
            for msg in messages:
                print(f"Retrieved: {msg}")

            # Save bot message
            save_message(
                curr,
                DbMessage(
                    created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                    chat_id=test_chat_id,
                    user_id=BOT_USER_ID,
                    message="From BOT",
                    meta={"test": "metadata"},
                ),
            )

            convert_db_messages_to_claude_messages(
                get_messages(curr, chat_id=test_chat_id)
            )

        finally:
            # Delete everything
            curr.execute("DELETE FROM messages WHERE chat_id=123123123")

        # Verify deletion
        messages = get_messages(curr, chat_id=test_chat_id)
        print(f"Messages after deletion: {messages}")


def test_memories():
    fake_chat_id = 12345
    with connect() as conn, conn.cursor() as curr:
        try:
            # Create a test memory
            test_memory: MemoryType = {
                "id": "aaa",
                "content": "test",
                "meta": {"some_random_field": 11},
                "created_at": datetime.datetime.now(DEFAULT_TIMEZONE).isoformat(),
            }

            update_memory(
                curr,
                fake_chat_id,
                MemoryUpdateRequest(new_memories=[test_memory], deleted_memory_ids=[]),
            )

            memories = get_memories(curr, chat_id=fake_chat_id)
            assert len(memories) == 1, memories
            [the_memory] = memories
            print(f"Retrieved: {the_memory}")
            for k, v in test_memory.items():
                if k != "created_at":
                    assert v == the_memory.get(k), f"{k=}: {v} != {the_memory[k]=}"

            update_memory(
                curr,
                fake_chat_id,
                MemoryUpdateRequest(
                    new_memories=[], deleted_memory_ids=[the_memory["id"]]
                ),
            )

            memories = get_memories(curr, chat_id=fake_chat_id)
            assert len(memories) == 0, memories

        finally:
            curr.execute(f"DELETE FROM memories WHERE chat_id={fake_chat_id}")


def test_variables():
    fake_chat_id = 12345
    fake_var_name = "qwerty"
    with connect() as conn, conn.cursor() as curr:
        try:
            # Test setting variable value and getting by default.
            variables = VariablesForChat(curr, fake_chat_id)
            print("VARS", variables.variables)
            assert variables.get(fake_var_name) is None

            # Test global value
            variables.set_global(fake_var_name, "global")
            assert variables.get(fake_var_name) == "global"

            variables.set(fake_var_name, "test1")
            # Overwrites global value
            assert variables.get(fake_var_name) == "test1"
            assert variables.get("nonexistent", "default") == "default"

            # Test boolean type
            variables.set(fake_var_name, True)
            assert variables.get(fake_var_name) is True

            # Test datetime type
            now = datetime.datetime.now(DEFAULT_TIMEZONE)
            variables.set(fake_var_name, now)
            assert variables.get(fake_var_name) == now
            variables = VariablesForChat(curr, fake_chat_id)
            assert variables.get(fake_var_name) == now
            assert (variables.get(fake_var_name) - now).total_seconds() == 0
            variables._read_all()
            assert variables.get(fake_var_name) == now

        finally:
            curr.execute("DELETE FROM chat_variables WHERE name=%s", (fake_var_name,))


def test_invocations():
    fake_chat_id = 12345
    with connect() as conn, conn.cursor() as curr:
        try:
            # Create a test invocation
            test_invocation = DbScheduledInvocation(
                scheduled_at=datetime.datetime.now(DEFAULT_TIMEZONE),
                chat_id=fake_chat_id,
                is_active=True,
                is_recurring=False,
                reason="test reason",
                meta={"test_field": "test_value"},
            )

            save_invocation(curr, test_invocation)

            invocations = get_scheduled_invocations(curr, chat_id=fake_chat_id)
            invocations_all = get_scheduled_invocations(curr)
            assert len(invocations) == 1, invocations
            assert len([x for x in invocations_all if x.chat_id == fake_chat_id]) == 1
            [the_invocation] = invocations
            print(f"Retrieved: {the_invocation}")

            assert test_invocation.chat_id == the_invocation.chat_id
            assert test_invocation.is_active == the_invocation.is_active
            assert test_invocation.scheduled_at == the_invocation.scheduled_at
            assert test_invocation.reason == the_invocation.reason
            assert test_invocation.meta == the_invocation.meta

            assert the_invocation.scheduled_id is not None, the_invocation
            set_non_active_invocation(curr, the_invocation)

            invocations = get_scheduled_invocations(curr, chat_id=fake_chat_id)
            assert (
                len(invocations) == 0
            ), invocations  # Should be empty since we only get active ones

        finally:
            curr.execute(f"DELETE FROM invocations WHERE chat_id={fake_chat_id}")


def update_agent_meta(curr, agent_id: int, meta: dict) -> None:
    """Merges keys into the agent's existing meta JSON."""
    curr.execute(
        """
        UPDATE agents
        SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb
        WHERE id = %s
        """,
        (json.dumps(meta), agent_id),
    )


def get_agent_meta(curr, agent_id: int) -> dict | None:
    """Returns agent meta dict, or None if the agent doesn't exist."""
    curr.execute("SELECT meta FROM agents WHERE id = %s", (agent_id,))
    row = curr.fetchone()
    return row[0] if row else None


def create_agent(curr, chat_id: int, meta: dict | None = None) -> int:
    """Creates an agent record in the DB. Returns the agent id."""
    curr.execute(
        """
        INSERT INTO agents (chat_id, created_at, meta)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (
            chat_id,
            datetime.datetime.now(DEFAULT_TIMEZONE),
            json.dumps(meta) if meta else None,
        ),
    )
    return curr.fetchone()[0]


def craete_all():
    with connect() as conn, conn.cursor() as curr:
        logger.info("Init DB")
        curr.execute(INIT_VECTOR)
        curr.execute(INIT_MEMORY_QUERY)
        curr.execute(INIT_MESSAGES_QUERY)
        curr.execute(INIT_VARIABLES_QUERY)
        curr.execute(INIT_INVOCATIONS_QUERY)
        curr.execute(INIT_AGENTS_QUERY)
        curr.execute(MIGRATE_MESSAGES_AGENT_ID)
        logger.info("Init DB done")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    craete_all()
    test_invocations()
    test_variables()
    test_messages()
    test_memories()
