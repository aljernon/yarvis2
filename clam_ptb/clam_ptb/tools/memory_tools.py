"""Tools for reading Core Knowledge Repository files."""

import logging

from clam_ptb.on_disk_memory import MEMORY_PATH, parse_skill_frontmatter
from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class ReadMemoryTool(LocalTool):
    """Tool for reading knowledge files from the Core Knowledge Repository by name."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_memory",
            description="Load a knowledge file from the Core Knowledge Repository by its name. Use this to access detailed knowledge when needed. Returns the full content of the knowledge file.",
            args=[
                ArgSpec(
                    name="name",
                    type=str,
                    description="Name of the knowledge file to read (e.g., 'code-self-improvement', 'calendar-management'). See the available knowledge files listed in the system prompt.",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, name: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        # Construct path to SKILL.md
        skill_path = MEMORY_PATH / name / "SKILL.md"

        if not skill_path.exists():
            # Try to find similar names to help the user
            available = sorted(
                [
                    d.name
                    for d in MEMORY_PATH.iterdir()
                    if d.is_dir() and (d / "SKILL.md").exists()
                ]
            )
            return ToolResult.error(
                f"Knowledge file '{name}' not found. Available knowledge files:\n"
                + "\n".join(f"  - {n}" for n in available)
            )

        try:
            content = skill_path.read_text()
            return ToolResult(f"Content of knowledge file '{name}':\n\n{content}")
        except Exception as e:
            logger.exception(f"Error reading knowledge file {name}: {e}")
            return ToolResult.error(f"Error reading knowledge file: {str(e)}")


class CheckMemoryValidTool(LocalTool):
    """Tool for validating the structure of the Core Knowledge Repository."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="check_memory_valid",
            description="Validate the structure of the Core Knowledge Repository. Checks that all entries are folders with SKILL.md files containing valid metadata. Use this after editing memory files to ensure structural correctness.",
            args=[],
        )

    async def _execute(self, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        errors = []
        warnings = []
        valid_folders = []

        # Check that MEMORY_PATH exists
        if not MEMORY_PATH.exists():
            return ToolResult.error(
                f"Core Knowledge Repository path does not exist: {MEMORY_PATH}"
            )

        # Check all items in MEMORY_PATH
        for item in sorted(MEMORY_PATH.iterdir()):
            item_name = item.name

            # Skip hidden files and git directory
            if item_name.startswith("."):
                continue

            # Check 1: Everything should be a folder
            if not item.is_dir():
                errors.append(
                    f"❌ '{item_name}' is not a folder (files not allowed in CKR root)"
                )
                continue

            # Check 2: Each folder should have SKILL.md
            skill_path = item / "SKILL.md"
            if not skill_path.exists():
                errors.append(f"❌ Folder '{item_name}' missing SKILL.md file")
                continue

            # Check 3: SKILL.md should have valid parseable metadata
            try:
                content = skill_path.read_text()
                metadata = parse_skill_frontmatter(content)

                # Check that required fields exist
                if not metadata.get("name"):
                    errors.append(
                        f"❌ '{item_name}/SKILL.md' missing 'name' in frontmatter"
                    )
                elif metadata["name"] != item_name:
                    warnings.append(
                        f"⚠️  '{item_name}/SKILL.md' has name='{metadata['name']}' (doesn't match folder name)"
                    )

                if not metadata.get("description"):
                    errors.append(
                        f"❌ '{item_name}/SKILL.md' missing 'description' in frontmatter"
                    )

                if "autoload" not in metadata:
                    errors.append(
                        f"❌ '{item_name}/SKILL.md' missing 'autoload' in frontmatter"
                    )

                # If all checks passed, it's valid
                if (
                    metadata.get("name")
                    and metadata.get("description")
                    and "autoload" in metadata
                ):
                    valid_folders.append(
                        f"✅ '{item_name}' - autoload={metadata['autoload']}"
                    )

            except Exception as e:
                errors.append(f"❌ '{item_name}/SKILL.md' failed to parse: {str(e)}")

        # Build result message
        result_lines = []

        if valid_folders:
            result_lines.append(f"Valid knowledge files ({len(valid_folders)}):")
            result_lines.extend(valid_folders)

        if warnings:
            result_lines.append(f"\nWarnings ({len(warnings)}):")
            result_lines.extend(warnings)

        if errors:
            result_lines.append(f"\nErrors ({len(errors)}):")
            result_lines.extend(errors)
            result_lines.append("\n❌ Validation FAILED - please fix the errors above")
            return ToolResult.error("\n".join(result_lines))
        elif warnings:
            result_lines.append("\n⚠️  Validation passed with warnings")
            return ToolResult("\n".join(result_lines))
        else:
            result_lines.append("\n✅ All knowledge files are valid!")
            return ToolResult("\n".join(result_lines))


def build_memory_tools() -> list[LocalTool]:
    """Build and return all memory-related tools."""
    return [
        ReadMemoryTool(),
        CheckMemoryValidTool(),
    ]
