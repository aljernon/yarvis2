"""Tests for Turn dataclasses: render(), to_db_message(), db_message_to_turn() roundtrip."""

import datetime
import unittest

import pytz

from yarvis_ptb.settings import BOT_USER_ID, SYSTEM_USER_ID
from yarvis_ptb.storage import IMAGE_B64_META_FIELD, DbMessage
from yarvis_ptb.turns import (
    BotTurn,
    InputMessageTurn,
    SystemTurn,
    db_message_to_turn,
)

NOW = datetime.datetime(2026, 3, 15, 10, 0, 0, tzinfo=pytz.UTC)
CHAT_ID = 100


class TestSystemTurn(unittest.TestCase):
    def test_render_notification(self):
        turn = SystemTurn(
            created_at=NOW, marked_for_archive=False, message="bot restarted"
        )
        msgs = turn.render()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        text = msgs[0]["content"]
        assert "bot restarted" in text
        assert "<system>" in text
        assert 'type="notification"' in text

    def test_render_schedule(self):
        turn = SystemTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="Context: check health",
            turn_type="schedule",
        )
        msgs = turn.render()
        text = msgs[0]["content"]
        assert 'type="schedule"' in text
        assert "<system>" not in text
        assert "Context: check health" in text

    def test_to_db_message(self):
        turn = SystemTurn(
            created_at=NOW, marked_for_archive=False, message="bot restarted"
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.user_id == SYSTEM_USER_ID
        assert db.chat_id == CHAT_ID
        assert db.message == "bot restarted"
        assert db.created_at == NOW
        assert db.agent_id is None
        assert db.meta["turn_type"] == "notification"

    def test_to_db_message_schedule(self):
        turn = SystemTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="check health",
            turn_type="schedule",
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.meta["turn_type"] == "schedule"

    def test_to_db_message_with_agent(self):
        turn = SystemTurn(created_at=NOW, marked_for_archive=False, message="freeze")
        db = turn.to_db_message(CHAT_ID, agent_id=5)
        assert db.agent_id == 5

    def test_roundtrip_notification(self):
        turn = SystemTurn(created_at=NOW, marked_for_archive=False, message="hello")
        db = turn.to_db_message(CHAT_ID)
        turn2 = db_message_to_turn(db)
        assert isinstance(turn2, SystemTurn)
        assert turn2.created_at == turn.created_at
        assert turn2.message == turn.message
        assert turn2.turn_type == "notification"

    def test_roundtrip_schedule(self):
        turn = SystemTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="do stuff",
            turn_type="schedule",
        )
        db = turn.to_db_message(CHAT_ID)
        turn2 = db_message_to_turn(db)
        assert isinstance(turn2, SystemTurn)
        assert turn2.turn_type == "schedule"

    def test_legacy_no_meta_defaults_to_notification(self):
        """Old system messages without meta default to notification."""
        db = DbMessage(
            created_at=NOW, chat_id=CHAT_ID, user_id=SYSTEM_USER_ID, message="restart"
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, SystemTurn)
        assert turn.turn_type == "notification"


class TestBotTurn(unittest.TestCase):
    def test_render_message_params(self):
        params = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        msgs = turn.render()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"

    def test_render_does_not_mutate_original(self):
        params = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        msgs = turn.render()
        msgs[0]["content"][0]["text"] = "mutated"
        assert params[0]["content"][0]["text"] == "hi"

    def test_render_drops_empty_trailing_assistant(self):
        params = [
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "ok", "tool_use_id": "x"}
                ],
            },
            {"role": "assistant", "content": ""},
        ]
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        msgs = turn.render()
        assert len(msgs) == 2

    def test_render_marked_for_archive(self):
        params = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        turn = BotTurn(created_at=NOW, marked_for_archive=True, message_params=params)
        msgs = turn.render()
        assert msgs[0]["content"][0]["text"].startswith("[MARKED_FOR_DELETION]")

    def test_requires_message_params(self):
        with self.assertRaises(TypeError):
            BotTurn(created_at=NOW, marked_for_archive=False)

    def test_to_db_message(self):
        params = [{"role": "assistant", "content": "hi"}]
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        db = turn.to_db_message(CHAT_ID)
        assert db.user_id == BOT_USER_ID
        assert db.message == "USE_CONTENT_FROM_META"
        assert db.meta["message_params"] == params

    def test_to_db_message_with_usage(self):
        params = [{"role": "assistant", "content": "hi"}]
        usage = {"calls": [], "estimated_cost_usd": 0.01}
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        db = turn.to_db_message(CHAT_ID, usage=usage)
        assert db.meta["usage"] == usage

    def test_roundtrip(self):
        params = [{"role": "assistant", "content": "hi"}]
        turn = BotTurn(created_at=NOW, marked_for_archive=False, message_params=params)
        db = turn.to_db_message(CHAT_ID)
        turn2 = db_message_to_turn(db)
        assert isinstance(turn2, BotTurn)
        assert turn2.message_params == params

    def test_legacy_plain_text_db_message(self):
        """db_message_to_turn wraps legacy plain-text bot messages into message_params."""
        db = DbMessage(
            created_at=NOW, chat_id=CHAT_ID, user_id=BOT_USER_ID, message="hello"
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, BotTurn)
        assert turn.message_params == [
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
        ]


class TestInputMessageTurn(unittest.TestCase):
    def test_render_basic(self):
        turn = InputMessageTurn(
            created_at=NOW, marked_for_archive=False, message="hi", user_id=123
        )
        msgs = turn.render()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        text = msgs[0]["content"][-1]["text"]
        assert "hi" in text
        assert 'type="message"' in text
        assert 'sender_type="human"' in text

    def test_render_voice(self):
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="hi",
            user_id=123,
            is_voice=True,
        )
        msgs = turn.render()
        text = msgs[0]["content"][-1]["text"]
        assert 'is_voice="true"' in text

    def test_render_no_voice_attr_when_false(self):
        turn = InputMessageTurn(
            created_at=NOW, marked_for_archive=False, message="hi", user_id=123
        )
        msgs = turn.render()
        text = msgs[0]["content"][-1]["text"]
        assert "is_voice" not in text

    def test_render_image(self):
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="look",
            user_id=123,
            image_b64="abc123",
        )
        msgs = turn.render()
        content = msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "image"
        assert content[0]["source"]["data"] == "abc123"
        assert content[1]["type"] == "text"

    def test_render_reply_to(self):
        reply = {"text": "original msg", "from": "Bob", "date": "2026-03-15T10:00:00"}
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="reply",
            user_id=123,
            reply_to=reply,
        )
        msgs = turn.render()
        text = msgs[0]["content"][-1]["text"]
        assert "[Replying to Bob" in text
        assert "original msg" in text

    def test_render_reply_to_long_text_truncated(self):
        reply = {"text": "x" * 300, "from": "Bob"}
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="reply",
            user_id=123,
            reply_to=reply,
        )
        msgs = turn.render()
        text = msgs[0]["content"][-1]["text"]
        assert "..." in text

    def test_render_marked_for_archive(self):
        turn = InputMessageTurn(
            created_at=NOW, marked_for_archive=True, message="hi", user_id=123
        )
        msgs = turn.render()
        text = msgs[0]["content"][-1]["text"]
        assert text.startswith("[MARKED_FOR_DELETION]")

    def test_to_db_message_basic(self):
        turn = InputMessageTurn(
            created_at=NOW, marked_for_archive=False, message="hi", user_id=123
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.user_id == 123
        assert db.chat_id == CHAT_ID
        assert db.message == "hi"
        assert db.meta == {"is_voice": False}

    def test_to_db_message_voice(self):
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="hi",
            user_id=123,
            is_voice=True,
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.meta["is_voice"] is True

    def test_to_db_message_image(self):
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="look",
            user_id=123,
            image_b64="abc",
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.meta[IMAGE_B64_META_FIELD] == "abc"

    def test_to_db_message_reply_to(self):
        reply = {"text": "orig", "from": "Bob", "date": "2026-03-15"}
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="re",
            user_id=123,
            reply_to=reply,
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.meta["reply_to"] == reply

    def test_to_db_message_uploaded_file(self):
        uf = {
            "file_path": "/tmp/f",
            "file_name": "f.pdf",
            "file_size": 100,
            "mime_type": "application/pdf",
            "file_type": "document",
        }
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="doc",
            user_id=123,
            uploaded_file=uf,
        )
        db = turn.to_db_message(CHAT_ID)
        assert db.meta["uploaded_file"] == uf

    def test_roundtrip(self):
        reply = {"text": "orig", "from": "Bob", "date": "2026-03-15"}
        uf = {
            "file_path": "/tmp/f",
            "file_name": "f.pdf",
            "file_size": 100,
            "mime_type": None,
            "file_type": "document",
        }
        turn = InputMessageTurn(
            created_at=NOW,
            marked_for_archive=False,
            message="hi",
            user_id=123,
            is_voice=True,
            image_b64="abc",
            reply_to=reply,
            uploaded_file=uf,
        )
        db = turn.to_db_message(CHAT_ID)
        turn2 = db_message_to_turn(db)
        assert isinstance(turn2, InputMessageTurn)
        assert turn2.message == "hi"
        assert turn2.user_id == 123
        assert turn2.is_voice is True
        assert turn2.image_b64 == "abc"
        assert turn2.reply_to == reply
        assert turn2.uploaded_file == uf


class TestDbMessageToTurn(unittest.TestCase):
    def test_system_message(self):
        db = DbMessage(
            created_at=NOW, chat_id=CHAT_ID, user_id=SYSTEM_USER_ID, message="restart"
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, SystemTurn)

    def test_system_message_schedule(self):
        db = DbMessage(
            created_at=NOW,
            chat_id=CHAT_ID,
            user_id=SYSTEM_USER_ID,
            message="heartbeat",
            meta={"turn_type": "schedule"},
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, SystemTurn)
        assert turn.turn_type == "schedule"

    def test_bot_message_with_params(self):
        params = [{"role": "assistant", "content": "hi"}]
        db = DbMessage(
            created_at=NOW,
            chat_id=CHAT_ID,
            user_id=BOT_USER_ID,
            message="USE_CONTENT_FROM_META",
            meta={"message_params": params},
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, BotTurn)
        assert turn.message_params == params

    def test_bot_message_legacy_plain(self):
        """Legacy plain-text bot messages are wrapped into message_params."""
        db = DbMessage(
            created_at=NOW, chat_id=CHAT_ID, user_id=BOT_USER_ID, message="hello"
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, BotTurn)
        assert turn.message_params == [
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
        ]

    def test_user_message(self):
        db = DbMessage(
            created_at=NOW,
            chat_id=CHAT_ID,
            user_id=123,
            message="hi",
            meta={"is_voice": True},
        )
        turn = db_message_to_turn(db)
        assert isinstance(turn, InputMessageTurn)
        assert turn.user_id == 123
        assert turn.is_voice is True

    def test_user_message_no_meta(self):
        db = DbMessage(created_at=NOW, chat_id=CHAT_ID, user_id=123, message="hi")
        turn = db_message_to_turn(db)
        assert isinstance(turn, InputMessageTurn)
        assert turn.is_voice is False
        assert turn.image_b64 is None


if __name__ == "__main__":
    unittest.main()
