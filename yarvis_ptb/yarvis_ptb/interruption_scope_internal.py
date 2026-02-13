from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yarvis_ptb.ptb_util import InterruptionScope

INTERRUPTABLES: list["InterruptionScope"] = []
CHAT2LAST_MESSAGE_ID: dict[int, int] = {}
