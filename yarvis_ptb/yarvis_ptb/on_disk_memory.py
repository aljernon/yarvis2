import re
import socket
import subprocess

from yarvis_ptb.settings import PROJECT_ROOT


def is_local() -> bool:
    return socket.gethostname() == "AL.local"


WORKSPACE_PATH = (PROJECT_ROOT / "workspace").resolve()

# Back-compat alias used by timezones.py, todo_tools.py, daily_agent_update.py, etc.
MEMORY_PATH = WORKSPACE_PATH

SKILLS_PATH = WORKSPACE_PATH / "skills"
MEMORY_DATA_PATH = WORKSPACE_PATH / "memory"

# Root files that are always loaded into the system prompt (in this order).
ROOT_FILES = [
    "MEMORY.md",
    "HUMAN.md",
    "BEHAVIOR.md",
    "TOOLS.md",
]


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


def _render_root_file(name: str, content: str) -> list[str]:
    """Render a root workspace file for inclusion in a prompt."""
    return [f"=== {name} ===", content, ""]


def _render_skill(path: str, content: str) -> list[str]:
    """Render a single skill/data file for inclusion in a prompt."""
    # Extract a readable name from the path
    name = path.strip("/").split("/")[-1]
    if name == "SKILL.md":
        name = path.strip("/").split("/")[-2]
    return [f"Content of {name} - read from {path}", content, ""]


def read_root_files() -> dict[str, str]:
    """Read all root workspace files."""
    result = {}
    for name in ROOT_FILES:
        path = WORKSPACE_PATH / name
        if path.exists():
            result[name] = path.read_text()
    return result


def resolve_file_path(name: str):
    """Resolve a skill name to its path on disk.

    Only looks in skills/<name>/SKILL.md.
    Data files should be read via bash (cat workspace/memory/<name>.md).

    Returns (path, None) on success, (None, error_msg) on failure.
    """
    skill_path = SKILLS_PATH / name / "SKILL.md"
    if skill_path.exists():
        return skill_path, None

    return (
        None,
        f"Skill '{name}' not found. Available skills: {', '.join(s.name for s in sorted(SKILLS_PATH.iterdir()) if (s / 'SKILL.md').exists())}",
    )


def resolve_memory_preload(load: bool) -> str:
    """Resolve memory preload into rendered content for the system prompt.

    True = load all root workspace files.
    False = nothing.
    """
    if not load:
        return ""
    root_files = read_root_files()
    if not root_files:
        return ""
    lines: list[str] = [
        "=== Workspace Memory ===",
        "The following files are automatically included in every invocation.\n",
    ]
    for name, content in root_files.items():
        lines.extend(_render_root_file(name, content))
    return "\n".join(lines)


def render_workspace_content() -> str:
    """Render full workspace content for system prompt (root files only).

    Used by daily self-reflect to provide full workspace context.
    """
    return resolve_memory_preload(load=True)


# Keep old name for back-compat
render_memory_content = render_workspace_content


def list_all_workspace_files() -> list[str]:
    """List all readable files in the workspace (for validation / discovery)."""
    files = []
    # Root .md files
    for p in sorted(WORKSPACE_PATH.glob("*.md")):
        files.append(p.name)
    # Data files in memory/
    if MEMORY_DATA_PATH.exists():
        for p in sorted(MEMORY_DATA_PATH.glob("*.md")):
            files.append(f"memory/{p.name}")
    # Skills
    if SKILLS_PATH.exists():
        for p in sorted(SKILLS_PATH.glob("*/SKILL.md")):
            files.append(f"skills/{p.parent.name}")
    return files


def render_skill_listing() -> str:
    """Render a dynamic listing of available skills.

    Used when list_skills=True to show the agent what's available via read_skill.
    """
    lines = [
        "=== Available Skills ===",
        "Skills are procedural knowledge — domain-specific instructions, code patterns,",
        "and critical rules for specific tasks. When a task matches a skill's description,",
        "load it with `read_skill(name)` BEFORE taking action. Multiple skills may apply.\n",
    ]

    if SKILLS_PATH.exists():
        for skill_dir in sorted(SKILLS_PATH.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            metadata = parse_skill_frontmatter(skill_md.read_text())
            desc = metadata.get("description", "")
            lines.append(f"- **{skill_dir.name}**: {desc}")

    return "\n".join(lines)


def commit_memory() -> None:
    if not is_local():
        subprocess.check_call(
            "git add .; if [[ -n $(git status --porcelain) ]]; then git commit -a -m 'update memory'; fi; git push --force",
            cwd=WORKSPACE_PATH,
            executable="/bin/bash",
            shell=True,
        )
