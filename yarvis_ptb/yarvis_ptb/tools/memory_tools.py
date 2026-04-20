"""Tools for reading workspace skills and validating workspace structure."""

import logging

from yarvis_ptb.on_disk_memory import (
    MEMORY_DATA_PATH,
    SKILLS_PATH,
    WORKSPACE_PATH,
    resolve_file_path,
)
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ReadMemoryTool(LocalTool):
    """Tool for reading workspace files by name."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_skill",
            description=(
                "Load a skill's procedural knowledge before performing a task that matches it. "
                "Skills contain domain-specific instructions, code patterns, and critical rules "
                "that you MUST follow. When a task matches an available skill's description, "
                "load the skill BEFORE taking any action on the task. "
                "Available skills are listed in your system prompt under 'Available Skills'. "
                "For data files (workspace/memory/), use bash to read them directly."
            ),
            args=[
                ArgSpec(
                    name="name",
                    type=str,
                    description=(
                        "Skill directory name exactly as listed (e.g., 'calendar-scheduling', "
                        "'morning-bookkeeping')."
                    ),
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, name: str, **kwargs) -> ToolResult:  # pyre-ignore[14]
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        path, err = resolve_file_path(name)
        if path is None:
            return ToolResult.error(err)

        try:
            content = path.read_text()
            return ToolResult(f"Content of '{name}' (path: {path}):\n\n{content}")
        except Exception as e:
            logger.exception(f"Error reading workspace file {name}: {e}")
            return ToolResult.error(f"Error reading workspace file: {str(e)}")


class CheckMemoryValidTool(LocalTool):
    """Tool for validating the workspace structure."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="check_memory_valid",
            description=(
                "Validate the workspace structure. Checks root files, "
                "skills (skills/*/SKILL.md), and data files (memory/*.md). "
                "Use after editing workspace files to ensure structural correctness."
            ),
            args=[],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        from yarvis_ptb.on_disk_memory import ROOT_FILES, parse_skill_frontmatter

        errors = []
        warnings = []
        valid_items = []

        if not WORKSPACE_PATH.exists():
            return ToolResult.error(f"Workspace path does not exist: {WORKSPACE_PATH}")

        for name in ROOT_FILES:
            path = WORKSPACE_PATH / name
            if path.exists():
                valid_items.append(f"✅ root/{name}")
            else:
                errors.append(f"❌ Missing root file: {name}")

        boot = WORKSPACE_PATH / "BOOT.md"
        if boot.exists():
            valid_items.append("✅ root/BOOT.md")
        else:
            warnings.append("⚠️  BOOT.md missing (optional)")

        # Check skills: every skills/<name>/ must have SKILL.md with a
        # frontmatter block containing at least `name` and `description`.
        if SKILLS_PATH.exists():
            for item in sorted(SKILLS_PATH.iterdir()):
                if item.name.startswith(".") or not item.is_dir():
                    continue
                skill_md = item / "SKILL.md"
                if not skill_md.exists():
                    errors.append(f"❌ skills/{item.name}/ missing SKILL.md")
                    continue
                try:
                    fm = parse_skill_frontmatter(skill_md.read_text())
                except OSError as e:
                    errors.append(f"❌ skills/{item.name}/SKILL.md unreadable: {e}")
                    continue
                missing = [k for k in ("name", "description") if k not in fm]
                if missing:
                    errors.append(
                        f"❌ skills/{item.name}/SKILL.md missing frontmatter keys: "
                        f"{', '.join(missing)}"
                    )
                else:
                    valid_items.append(f"✅ skills/{item.name}")
        else:
            warnings.append("⚠️  skills/ directory missing")

        if MEMORY_DATA_PATH.exists():
            for item in sorted(MEMORY_DATA_PATH.glob("*.md")):
                valid_items.append(f"✅ memory/{item.name}")
        else:
            warnings.append("⚠️  memory/ directory missing")

        # Flag unexpected items in workspace root. `claude-ai-data-*`
        # exports are tolerated (see dashboard/routes/workspace.py).
        expected_root = set(ROOT_FILES) | {"BOOT.md", "settings.json"}
        expected_dirs = {"skills", "memory", "todos"}
        for item in sorted(WORKSPACE_PATH.iterdir()):
            if item.name.startswith(".") or item.name.startswith("claude-ai-data-"):
                continue
            if item.is_dir() and item.name not in expected_dirs:
                warnings.append(
                    f"⚠️  Unexpected directory in workspace root: {item.name}/"
                )
            elif item.is_file() and item.name not in expected_root:
                warnings.append(f"⚠️  Unexpected file in workspace root: {item.name}")

        expectations = (
            "\nExpected workspace layout:\n"
            f"  Root files (required): {', '.join(ROOT_FILES)}\n"
            "  Root files (optional): BOOT.md, settings.json\n"
            "  skills/<name>/SKILL.md — frontmatter needs: name, description (autoload optional)\n"
            "  memory/*.md — supplementary memory, linked from MEMORY.md\n"
            "  todos/ — per-agent todo lists\n"
            "  claude-ai-data-* and dotfiles are ignored."
        )

        # Build result
        result_lines = []
        if valid_items:
            result_lines.append(f"Valid files ({len(valid_items)}):")
            result_lines.extend(valid_items)
        if warnings:
            result_lines.append(f"\nWarnings ({len(warnings)}):")
            result_lines.extend(warnings)
        if errors:
            result_lines.append(f"\nErrors ({len(errors)}):")
            result_lines.extend(errors)
            result_lines.append(expectations)
            result_lines.append("\n❌ Validation FAILED — fix errors above")
            return ToolResult.error("\n".join(result_lines))
        elif warnings:
            result_lines.append(expectations)
            result_lines.append("\n⚠️  Validation passed with warnings")
            return ToolResult("\n".join(result_lines))
        else:
            result_lines.append("\n✅ All workspace files valid!")
            return ToolResult("\n".join(result_lines))


def build_memory_tools() -> list[LocalTool]:
    """Build and return all workspace-related tools."""
    return [
        ReadMemoryTool(),
        CheckMemoryValidTool(),
    ]
