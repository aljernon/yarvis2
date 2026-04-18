"""Generate human-readable slugs for agents."""

import datetime

import coolname


def generate_agent_slug() -> str:
    """Generate a human-readable slug like 'swift-pine'."""
    return coolname.generate_slug(2)


def archive_slug(date: datetime.date) -> str:
    """Build an archive slug like 'archive/2026-03-04'."""
    return f"archive/{date.isoformat()}"


def sched_slug() -> str:
    """Build a schedule-subagent slug like 'sched/swift-pine'."""
    return f"sched/{coolname.generate_slug(2)}"


def reflect_slug(date: datetime.date) -> str:
    """Build a reflect slug like 'auto-reflect/2026-03-04-swift-pine'."""
    return f"auto-reflect/{date.isoformat()}-{coolname.generate_slug(2)}"


def schedule_reflect_slug(base_agent_slug: str) -> str:
    """Build a schedule-reflect slug like 'auto-reflect/sched/swift-pine'."""
    return f"auto-reflect/{base_agent_slug}"
