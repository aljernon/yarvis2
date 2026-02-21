import re
import socket
import subprocess

from yarvis_ptb.settings import PROJECT_ROOT

SYSTEM_CORE = "system-core"


def is_local() -> bool:
    return socket.gethostname() == "AL.local"


MEMORY_PATH = (
    PROJECT_ROOT / ("../core_knowledge" if is_local() else "core_knowledge")
).resolve()


def parse_skill_frontmatter(content: str) -> dict[str, str | bool]:
    """Parse YAML frontmatter from SKILL.md file."""
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        return {}

    frontmatter = match.group(1)
    metadata = {}

    # Extract name
    name_match = re.search(r"name:\s*(.+)", frontmatter)
    if name_match:
        metadata["name"] = name_match.group(1).strip()

    # Extract description (can be multi-line)
    desc_match = re.search(
        r"description:\s*(.+?)(?=\nautoload:|$)", frontmatter, re.DOTALL
    )
    if desc_match:
        metadata["description"] = desc_match.group(1).strip()

    # Extract autoload flag
    autoload_match = re.search(r"autoload:\s*(true|false)", frontmatter)
    if autoload_match:
        metadata["autoload"] = autoload_match.group(1) == "true"

    return metadata


def read_autoload_memory() -> dict[str, str]:
    """Reads all SKILL.md files marked with autoload: true"""
    memories = {}
    assert MEMORY_PATH.exists(), f"{MEMORY_PATH} does not exist"

    for skill_path in MEMORY_PATH.glob("*/SKILL.md"):
        content = skill_path.read_text()
        metadata = parse_skill_frontmatter(content)

        if metadata.get("autoload", False):
            memories[str(skill_path)] = content

    return memories


def list_memory_with_descriptions() -> list[dict[str, str]]:
    """List all SKILL.md files with their metadata"""
    memories = []
    assert MEMORY_PATH.exists(), f"{MEMORY_PATH} does not exist"

    for skill_path in sorted(MEMORY_PATH.glob("*/SKILL.md")):
        content = skill_path.read_text()
        metadata = parse_skill_frontmatter(content)

        memories.append(
            {
                "path": str(skill_path),
                "name": metadata.get("name", skill_path.parent.name),
                "description": metadata.get("description", "No description"),
                "autoload": metadata.get("autoload", False),
            }
        )

    return memories


def render_memory_content() -> str:
    """Render memory content for system prompt.

    Autoload files: Show full content
    Non-autoload files: Show name and description only
    """
    lines = []

    # Show full content of autoload files
    autoload_memories = read_autoload_memory()
    if autoload_memories:
        lines.append("=== Core Knowledge Repository (Autoloaded) ===\n")
        for path, content in sorted(
            autoload_memories.items(), key=lambda x: (SYSTEM_CORE not in x[0], x[0])
        ):
            skill_name = path.strip("/").split("/")[-2]
            lines.append(f"Content of skill {skill_name} - read from {path}")
            lines.append(content)
            lines.append("")

    # Show descriptions of non-autoload files
    all_memories = list_memory_with_descriptions()
    non_autoload = [m for m in all_memories if not m["autoload"]]

    if non_autoload:
        lines.append("\n=== Available Knowledge Files (On-Demand) ===")
        lines.append("Use the read_memory tool to load these when needed:\n")
        for memory in non_autoload:
            lines.append(f"- **{memory['name']}**: {memory['description']}")

    return "\n".join(lines)


def commit_memory() -> None:
    if not is_local():
        subprocess.check_call(
            "git add .; if [[ -n $(git status --porcelain) ]]; then git commit -a -m 'update memory'; fi; git push --force",
            cwd=MEMORY_PATH,
            executable="/bin/bash",
            shell=True,
        )
