import asyncio
import time
from typing import Optional, Type, TypeVar

T = TypeVar("T")


def ensure(x: Optional[T]) -> T:
    """
    Ensures that a value is not None and returns it with proper typing.
    Includes caller context in error message when available.

    Args:
        x: Value to check, can be of any type T or None

    Returns:
        The input value if it's not None

    Raises:
        AssertionError: If the input value is None
    """

    assert x is not None, "Expected non-None value"
    return x


def ensure_type(x, expected_type: Type[T]) -> T:
    assert isinstance(
        x, expected_type
    ), f"Expected value of type {expected_type}, got {type(x)}"
    return x


def get_human_readable_delta(dt1, dt2) -> str:
    delta = abs(dt2 - dt1)
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    return " and ".join(parts) if parts else "less than a minute"


def to_truncated_str(object, limit: int = 64, truncate_front: bool = True) -> str:
    """
    Converts an object to a string and truncates it to the given limit if necessary.

    Args:
        object: The object to convert to string
        limit: The maximum length of the string

    Returns:
        The string representation of the object, truncated if necessary
    """
    str_repr = str(object)
    if len(str_repr) > limit:
        if truncate_front:
            str_repr = (
                str_repr[: limit - 3] + f"[... ({len(str_repr) - limit} more chars)]"
            )
        else:
            str_repr = (
                f"[... ({len(str_repr) - limit} more chars)]" + str_repr[-(limit - 3) :]
            )
    return str_repr


class RateController:
    def __init__(self, wait_between_events_secs: float):
        self.wait_between_events_secs = wait_between_events_secs
        self.last_event = None

    async def wait_until_can_run(self):
        if self.last_event is None:
            self.last_event = time.time()
            return
        time_to_sleep = self.wait_between_events_secs - (time.time() - self.last_event)
        await asyncio.sleep(max(0, time_to_sleep))
        self.last_event = time.time()

    def can_run(self):
        if self.last_event is None:
            self.last_event = time.time()
            return True
        if time.time() - self.last_event > self.wait_between_events_secs:
            self.last_event = time.time()
            return True
        return False
