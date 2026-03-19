"""Generate human-readable slugs for agents."""

import datetime

import coolname


def generate_agent_slug() -> str:
    """Generate a human-readable slug like 'swift-pine'."""
    return coolname.generate_slug(2)


def archive_slug(date: datetime.date) -> str:
    """Build an archive slug like 'archive/2026-03-04'."""
    return f"archive/{date.isoformat()}"


def reflect_slug(date: datetime.date) -> str:
    """Build a reflect slug like 'auto-reflect/2026-03-04-swift-pine'."""
    return f"auto-reflect/{date.isoformat()}-{coolname.generate_slug(2)}"
