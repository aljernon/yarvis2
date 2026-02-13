import copy
import datetime
import functools
import re
from dataclasses import asdict, dataclass
from typing import Literal

import numpy as np
import termcolor
import tqdm
import typer
from anthropic.types.message_param import MessageParam

SENTENCE_TRANSFORMER_AVAILABLE = False

if SENTENCE_TRANSFORMER_AVAILABLE:
    from sentence_transformers import SentenceTransformer


from yarvis_ptb.settings import DEFAULT_TIMEZONE, ROOT_USER_ID
from yarvis_ptb.storage import DbMessage, connect, get_messages, save_message

app = typer.Typer()


if SENTENCE_TRANSFORMER_AVAILABLE:

    def get_model() -> SentenceTransformer:
        return SentenceTransformer("all-MiniLM-L6-v2")


def save_message_and_update_index(curr, message: DbMessage):
    save_message(curr, message)
    if (maybe_store := get_in_memory_message_store(message.chat_id)) is not None:
        maybe_store.add_message(message)


@functools.cache
def get_in_memory_message_store(chat_id: int) -> "InMemoryMessageGroupStore | None":
    if not SENTENCE_TRANSFORMER_AVAILABLE:
        return None
    if chat_id != ROOT_USER_ID:
        return None
    return InMemoryMessageGroupStore(chat_id)


class InMemoryMessageStore:
    def __init__(self, chat_id: int, min_message_length: int = 50):
        self.min_message_length = min_message_length
        with connect() as conn, conn.cursor() as curr:
            self.all_messages: list[DbMessage] = [
                x
                for x in get_messages(curr, chat_id)
                if len(x.message) >= min_message_length
            ]
        self.embeddings: np.ndarray = self.encode(
            self.all_messages, show_progress_bar=True
        )

    def encode(
        self, messages: list[DbMessage], show_progress_bar: bool = False
    ) -> np.ndarray:
        return get_model().encode(
            [x.message for x in messages],
            show_progress_bar=show_progress_bar,
            normalize_embeddings=True,
        )

    def add_message(self, message: DbMessage):
        if len(message.message) < self.min_message_length:
            return
        self.all_messages.append(message)
        self.embeddings = np.concat((self.embeddings, self.encode([message])), axis=0)

    def search(
        self,
        query: str | None = None,
        start_date: datetime.datetime | None = None,
        end_date: datetime.datetime | None = None,
        message_type: Literal["human", "assistant", "any"] = "any",
        limit: int = 10,
    ) -> list[tuple[DbMessage, float]] | list[tuple[DbMessage, None]]:
        # Finds the closeest messages by dot product among the ones that satisfy the condition
        # Prepares a mask for filtering messages based on message type and dates
        valid_mask = np.ones(len(self.all_messages), dtype=bool)

        if start_date is not None:
            valid_mask &= np.array(
                [x.created_at >= start_date for x in self.all_messages]
            )

        if end_date is not None:
            valid_mask &= np.array(
                [x.created_at <= end_date for x in self.all_messages]
            )

        if message_type == "human":
            valid_mask &= np.array([not x.is_bot() for x in self.all_messages])
        elif message_type == "assistant":
            valid_mask &= np.array([x.is_bot() for x in self.all_messages])

        # Get dot product similarity scores for query
        if query is not None:
            query_embedding = get_model().encode([query], normalize_embeddings=True)[0]
            scores = np.dot(self.embeddings[valid_mask], query_embedding)
            local_indices = np.argsort(-scores)[:limit]  # Get top k scores

            selected_scores = scores[local_indices]
            selected_global_indices = np.arange(len(valid_mask))[valid_mask][
                local_indices
            ]
            filtered_messages = [
                (self.all_messages[i], score)
                for i, score in zip(
                    selected_global_indices, selected_scores.tolist(), strict=True
                )
            ]
        else:
            # Return filtered messages
            valid_indices = np.where(valid_mask)[0]
            valid_indices = valid_indices[::-1][:limit]
            filtered_messages = [(self.all_messages[i], None) for i in valid_indices]
        return filtered_messages


def render_mesage_param_for_semantic_search(rec: MessageParam) -> str:
    parts = []
    rec = copy.deepcopy(rec)
    content = rec.pop("content")
    if isinstance(content, str):
        parts.append(content)
    else:
        for section in content:  # type: ignore
            section_content = section.pop("text", None)
            if section_content:
                parts.append(str(section_content) + "\n")
    text = "\n".join(parts)
    parts = re.split(r"(<thinking>.*?</thinking>)", text, flags=re.DOTALL)
    parts = [x if not x.startswith("<thinking>") else "" for x in parts]
    text = "\n".join(parts)
    parts = re.split(r"(<system>.*?</system>)", text, flags=re.DOTALL)
    parts = [x if not x.startswith("<system>") else "" for x in parts]
    text = "\n".join(parts)
    parts = [line.strip() for line in text.split("\n") if line.strip()]
    text = "\n".join(parts)
    if text:
        text = f"- {text}"
    return text


def _render_for_search(message: DbMessage) -> str:
    from yarvis_ptb.prompting import (
        convert_db_messages_to_claude_messages,
    )

    return "\n".join(
        render_mesage_param_for_semantic_search(x)
        for x in convert_db_messages_to_claude_messages([message])
    )


@dataclass
class MessageGroup:
    start_date: datetime.datetime
    end_date: datetime.datetime
    messages: list[DbMessage]
    _content_for_search: list[str]
    _search_content_len: int

    @property
    def content_for_search(self) -> str:
        return "\n".join(self._content_for_search)

    @classmethod
    def from_message(cls, message: DbMessage) -> "MessageGroup":
        group = cls(
            start_date=message.created_at,
            end_date=message.created_at,
            messages=[],
            _content_for_search=[],
            _search_content_len=0,
        )
        group._append_message_text(message)
        return group

    def _append_message_text(self, message: DbMessage):
        self.messages.append(message)
        self._content_for_search.append(_render_for_search(message))
        self._search_content_len += len(self._content_for_search[-1])

    def add_message(self, message: DbMessage):
        self.end_date = message.created_at
        self._append_message_text(message)

    def does_belong_to_group(
        self, message: DbMessage, timeout_secs: int, max_size_chars: int
    ) -> bool:
        if (message.created_at - self.end_date).total_seconds() > timeout_secs:
            return False
        if self._search_content_len + len(_render_for_search(message)) > max_size_chars:
            return False
        return True


class InMemoryMessageGroupStore:
    def __init__(
        self, chat_id: int, timeout_secs: int = 300, max_size_chars: int = 1024
    ):
        self.model = get_model()
        self.timeout_secs = timeout_secs
        self.max_size_chars = max_size_chars
        with connect() as conn, conn.cursor() as curr:
            all_messages: list[DbMessage] = get_messages(curr, chat_id)
        self.rebuild_all(all_messages)

    def rebuild_all(self, all_messages: list[DbMessage]):
        self.all_messages = all_messages
        self.message_groups: list[MessageGroup] = []
        for message in all_messages:
            if self.message_groups and self.message_groups[-1].does_belong_to_group(
                message,
                timeout_secs=self.timeout_secs,
                max_size_chars=self.max_size_chars,
            ):
                self.message_groups[-1].add_message(message)
            else:
                self.message_groups.append(MessageGroup.from_message(message))

        self.embeddings: np.ndarray = self.encode(
            self.message_groups, show_progress_bar=True
        )

    def encode(
        self, messages: list[MessageGroup], show_progress_bar: bool = False
    ) -> np.ndarray:
        return self.model.encode(
            [x.content_for_search for x in messages],
            show_progress_bar=show_progress_bar,
            normalize_embeddings=True,
        )

    def add_message(self, message: DbMessage):
        num_valid_embeddings = len(self.message_groups)
        if self.message_groups and self.message_groups[-1].does_belong_to_group(
            message,
            timeout_secs=self.timeout_secs,
            max_size_chars=self.max_size_chars,
        ):
            self.message_groups[-1].add_message(message)
            num_valid_embeddings -= 1
        else:
            self.message_groups.append(MessageGroup.from_message(message))

        new_embeddings = self.encode(self.message_groups[num_valid_embeddings:])
        self.embeddings = np.concatenate(
            (self.embeddings[:num_valid_embeddings], new_embeddings), axis=0
        )

    def search(
        self,
        query: str | None = None,
        start_date: datetime.datetime | None = None,
        end_date: datetime.datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[MessageGroup, float]] | list[tuple[MessageGroup, None]]:
        # Finds the closeest messages by dot product among the ones that satisfy the condition
        # Prepares a mask for filtering messages based on message type and dates
        valid_mask = np.ones(len(self.message_groups), dtype=bool)

        if start_date is not None:
            valid_mask &= np.array(
                [x.end_date >= start_date for x in self.message_groups]
            )

        if end_date is not None:
            valid_mask &= np.array(
                [x.start_date <= end_date for x in self.message_groups]
            )

        # Get dot product similarity scores for query
        if query is not None:
            query_embedding = get_model().encode([query], normalize_embeddings=True)[0]
            scores = np.dot(self.embeddings[valid_mask], query_embedding)
            local_indices = np.argsort(-scores)[:limit]  # Get top k scores

            selected_scores = scores[local_indices]
            selected_global_indices = np.arange(len(valid_mask))[valid_mask][
                local_indices
            ]
            filtered_messages = [
                (self.message_groups[i], score)
                for i, score in zip(
                    selected_global_indices, selected_scores.tolist(), strict=True
                )
            ]
        else:
            # Return filtered messages
            valid_indices = np.where(valid_mask)[0]
            valid_indices = valid_indices[::-1][:limit]
            filtered_messages = [(self.message_groups[i], None) for i in valid_indices]
        return filtered_messages


@app.command()
def populate_vectors():
    # Initialize the model
    model = get_model()

    with connect() as conn:
        cur = conn.cursor()

        # First make sure we have the vector extension
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

        # Add embedding column if it doesn't exist
        cur.execute("""
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS embedding vector(384)
        """)

        cur.execute("""
            DROP INDEX IF EXISTS messages_embedding_idx
        """)

        # Get messages without embeddings
        cur.execute("""
            SELECT id, message
            FROM messages
            WHERE embedding IS NULL
        """)

        # Process in batches for efficiency
        all_ids_messages = list(cur.fetchall())
        embeddings = model.encode(
            [x[1] for x in all_ids_messages],
            show_progress_bar=True,
            normalize_embeddings=True,
        )
        # Update database
        for (id, _), embedding in tqdm.tqdm(
            list(zip(all_ids_messages, embeddings, strict=True))
        ):
            cur.execute(
                "UPDATE messages SET embedding = %s WHERE id = %s",
                (embedding.tolist(), id),
            )

        # Create index if it doesn't exist
        cur.execute("""
            CREATE INDEX IF NOT EXISTS messages_embedding_idx
            ON messages USING ivfflat (embedding vector_ip_ops)
            WITH (lists = 100)
        """)

        conn.commit()
        cur.close()


MESSAGE_SEARCH_QUERY = """
SELECT message, created_at,
       1 - (embedding <=> query_embedding) as similarity
FROM messages
WHERE 1 - (embedding <=> query_embedding) > 0.7
ORDER BY similarity DESC
LIMIT 10;
"""


def find_close_messages(
    query: str, *, similarity_threshold: float = 0.7, limit: int = 10
) -> list[DbMessage]:
    model = get_model()

    query_embedding = model.encode(query, normalize_embeddings=True)

    with connect() as conn, conn.cursor() as cur:
        # Search using inner product (vectors are normalized)
        cur.execute(
            """
            SELECT message, created_at, 1 - (embedding <=> %s) as similarity
            FROM messages
            WHERE embedding IS NOT NULL
              AND 1 - (embedding <=> %s) > %s
            ORDER BY embedding <=> %s
            LIMIT %s
        """,
            (
                query_embedding,
                query_embedding,
                similarity_threshold,
                query_embedding,
                limit,
            ),
        )
        rows = cur.fetchall()
        return rows
        # return [DbMessage() for row in cur.fetchall()]


@app.command()
def query_model(query: str, limit: int = 10):
    store = InMemoryMessageStore(ROOT_USER_ID)
    messages = store.search(query=query, limit=limit)
    print("Num rows:", len(messages))
    for row, score in messages:
        print("===", score, {k: v for k, v in asdict(row).items() if k != "message"})
        print(row.message)


@app.command()
def test_add_group_store():
    # Create initial store
    store = InMemoryMessageGroupStore(ROOT_USER_ID)

    # Create test message
    new_message = DbMessage(
        chat_id=ROOT_USER_ID,
        created_at=datetime.datetime.now(DEFAULT_TIMEZONE),
        user_id=1234,
        message="Test message content",
    )

    # Get initial counts
    initial_groups = len(store.message_groups)
    initial_embeddings = store.embeddings.shape[0]

    # Add message
    store.add_message(new_message)

    # Verify changes
    assert (
        len(store.message_groups) == initial_groups + 1
    ), "Should have added new group"
    assert (
        store.embeddings.shape[0] == initial_embeddings + 1
    ), "Should have new embedding"
    assert (
        new_message in store.message_groups[-1].messages
    ), "New message should be in last group"

    # Test adding message close in time
    next_message = DbMessage(
        chat_id=ROOT_USER_ID,
        created_at=new_message.created_at + datetime.timedelta(seconds=10),
        user_id=1234,
        message="Follow up message",
    )

    store.add_message(next_message)

    assert len(store.message_groups) == initial_groups + 1, "Should use existing group"
    assert (
        store.embeddings.shape[0] == initial_embeddings + 1
    ), "Should have same embeddings"
    assert (
        next_message in store.message_groups[-1].messages
    ), "New message should be added to group"

    print("All tests passed!")


@app.command()
def query_model_group(query: str, limit: int = 10):
    store = InMemoryMessageGroupStore(ROOT_USER_ID)
    messages = store.search(query=query, limit=limit)
    print("Num rows:", len(messages))
    for i, (row, score) in enumerate(messages):
        color = ["red", "green"][i % 2]
        print("===", score, {k: v for k, v in asdict(row).items() if k != "content"})
        print(termcolor.colored(row.content_for_search, color))


if __name__ == "__main__":
    # python -m clam_ptb.message_search
    app()
