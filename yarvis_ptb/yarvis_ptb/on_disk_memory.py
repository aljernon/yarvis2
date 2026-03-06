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


def _render_skill(path: str, content: str) -> list[str]:
    """Render a single skill file for inclusion in a prompt."""
    skill_name = path.strip("/").split("/")[-2]
    return [f"Content of skill {skill_name} - read from {path}", content, ""]


def load_skills_by_name(names: list[str]) -> tuple[str, list[str]]:
    """Load specific skills by folder name. Returns (combined content, missing names)."""
    lines = []
    missing = []
    for name in names:
        skill_path = MEMORY_PATH / name / "SKILL.md"
        if skill_path.exists():
            lines.extend(_render_skill(str(skill_path), skill_path.read_text()))
        else:
            missing.append(name)
    return "\n".join(lines), missing


def resolve_memory_preload(spec: "list[str] | str") -> str:
    """Resolve memory spec into rendered content for the system prompt.

    - ``"auto"`` — all files with ``autoload: true`` in frontmatter
    - ``["logseq", "whoop"]`` — specific skills by folder name
    - ``[]`` — empty string
    """
    if spec == "auto":
        lines: list[str] = []
        autoload_memories = read_autoload_memory()
        if autoload_memories:
            lines.append("=== Preloaded Skills ===\n")
            for path, content in sorted(
                autoload_memories.items(),
                key=lambda x: (SYSTEM_CORE not in x[0], x[0]),
            ):
                lines.extend(_render_skill(path, content))
        return "\n".join(lines)

    if not spec:
        return ""
    assert isinstance(spec, list)
    skill_content, _missing = load_skills_by_name(spec)
    if not skill_content:
        return ""
    return (
        "=== Reference Knowledge ===\n"
        "The following skill files were provided to help you with this task.\n\n"
        + skill_content
    )


def render_memory_catalogue() -> str:
    """Render the full CKR skill catalogue with paths and descriptions."""
    all_memories = list_memory_with_descriptions()
    if not all_memories:
        return ""
    lines = [
        "=== Core Knowledge Repository ===",
        "All skill files are on disk and can be read or modified using tools.",
        f"Each skill is located at {MEMORY_PATH}/<name>/SKILL.md\n",
    ]
    for memory in all_memories:
        autoload_marker = " [preloaded]" if memory["autoload"] else ""
        lines.append(
            f"- **{memory['name']}**: {memory['description']}{autoload_marker}"
        )
    return "\n".join(lines)


def render_memory_content() -> str:
    """Render full CKR content for system prompt (catalogue + preloaded)."""
    parts = []
    catalogue = render_memory_catalogue()
    if catalogue:
        parts.append(catalogue)
    preloaded = resolve_memory_preload("auto")
    if preloaded:
        parts.append(preloaded)
    return "\n\n".join(parts)


def commit_memory() -> None:
    if not is_local():
        subprocess.check_call(
            "git add .; if [[ -n $(git status --porcelain) ]]; then git commit -a -m 'update memory'; fi; git push --force",
            cwd=MEMORY_PATH,
            executable="/bin/bash",
            shell=True,
        )
