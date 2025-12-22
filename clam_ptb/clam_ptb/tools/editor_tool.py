import difflib
import os
from inspect import cleandoc

from clam_ptb.tools.tool_spec import LocalTool, ToolResult, ToolSpec


class EditorTool(LocalTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="str_replace_editor",
            description=cleandoc("""Custom editing tool for viewing, creating and editing files
            * State is persistent across command calls and discussions with the user
            * If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep
            * The `create` command cannot be used if the specified `path` already exists as a file
            * If a `command` generates a long output, it will be truncated and marked with `<response clipped>`
            * The `undo_edit` command will revert the last edit made to the file at `path`

            Notes for using the `str_replace` command:
            * The `old_str` parameter should match EXACTLY one or more consecutive lines from the original file. Be mindful of whitespaces!
            * If the `old_str` parameter is not unique in the file, the replacement will not be performed. Make sure to include enough context in `old_str` to make it unique
            * The `new_str` parameter should contain the edited lines that should replace the `old_str`"""),
            args={
                "type": "object",
                "properties": {
                    "command": {
                        "description": "The commands to run. Allowed options are: `view`, `create`, `str_replace`, `insert`, `undo_edit`.",
                        "enum": [
                            "view",
                            "create",
                            "str_replace",
                            "insert",
                            "undo_edit",
                        ],
                        "type": "string",
                    },
                    "file_text": {
                        "description": "Required parameter of `create` command, with the content of the file to be created.",
                        "type": "string",
                    },
                    "insert_line": {
                        "description": "Required parameter of `insert` command. The `new_str` will be inserted AFTER the line `insert_line` of `path`.",
                        "type": "integer",
                    },
                    "new_str": {
                        "description": "Optional parameter of `str_replace` command containing the new string (if not given, no string will be added). Required parameter of `insert` command containing the string to insert.",
                        "type": "string",
                    },
                    "old_str": {
                        "description": "Required parameter of `str_replace` command containing the string in `path` to replace.",
                        "type": "string",
                    },
                    "path": {
                        "description": "Absolute path to file or directory, e.g. `/repo/file.py` or `/repo`.",
                        "type": "string",
                    },
                    "view_range": {
                        "description": "Optional parameter of `view` command when `path` points to a file. If none is given, the full file is shown. If provided, the file will be shown in the indicated line number range, e.g. [11, 12] will show lines 11 and 12. Indexing at 1 to start. Setting `[start_line, -1]` shows all lines from `start_line` to the end of the file.",
                        "items": {"type": "integer"},
                        "type": "array",
                    },
                },
                "required": ["command", "path"],
            },
        )

    async def _execute(self, **kwargs) -> ToolResult:
        command = kwargs.pop("command")
        path = kwargs.pop("path")

        if not os.path.exists(os.path.dirname(path)):
            return ToolResult.error(f"Directory not found: {os.path.dirname(path)}")

        try:
            if command == "view":
                view_range = kwargs.pop("view_range", None)
                return view_command(path, view_range)

            elif command == "create":
                file_text = kwargs.pop("file_text", None)
                return create_command(path, file_text)

            elif command == "str_replace":
                old_str = kwargs.pop("old_str", None)
                new_str = kwargs.pop("new_str", "")
                return str_replace_command(path, old_str, new_str)

            elif command == "insert":
                insert_line = kwargs.pop("insert_line", None)
                new_str = kwargs.pop("new_str", None)
                return insert_command(path, insert_line, new_str)

            elif command == "undo_edit":
                return ToolResult.error("undo_edit not implemented")

            else:
                return ToolResult.error(f"Unknown command: {command}")

        except Exception as e:
            return ToolResult.error(f"Error executing {command} command: {str(e)}")

        if kwargs:
            return ToolResult.error(f"Unexpected arguments: {kwargs}")

        return ToolResult("Command executed successfully")


def view_command(path: str, view_range: list[int] | None) -> ToolResult:
    if os.path.isfile(path):
        with open(path, "r") as f:
            lines = f.readlines()
            if view_range:
                start, end = view_range[0] - 1, view_range[1]
                return ToolResult(text="".join(lines[start:end]))
            return ToolResult(text="".join(lines))
    elif os.path.isdir(path):
        # List directory contents up to 2 levels deep
        try:
            result_lines = []
            for root, dirs, files in os.walk(path):
                # Calculate depth relative to the starting path
                depth = root[len(path) :].count(os.sep)
                if depth > 2:
                    continue

                # Skip hidden directories at any level
                dirs[:] = [d for d in dirs if not d.startswith(".")]

                indent = "  " * depth
                if depth == 0:
                    result_lines.append(f"{path}:")
                else:
                    relative_path = os.path.relpath(root, path)
                    result_lines.append(f"{indent}{relative_path}/")

                # Add files in current directory (skip hidden files)
                for file in sorted(files):
                    if not file.startswith("."):
                        result_lines.append(f"{indent}  {file}")

                # Add subdirectories
                for dir_name in sorted(dirs):
                    if depth < 2:  # Only show subdirs if we're not at max depth
                        result_lines.append(f"{indent}  {dir_name}/")

            return ToolResult(text="\n".join(result_lines))
        except PermissionError:
            return ToolResult.error(f"Permission denied accessing directory: {path}")
    else:
        return ToolResult.error(f"Path not found: {path}")


def create_command(path: str, file_text: str | None) -> ToolResult:
    if not file_text:
        return ToolResult.error("file_text parameter required for create command")
    with open(path, "w") as f:
        f.write(file_text)
    return ToolResult(f"Created file: {path}")


def str_replace_command(
    path: str, old_str: str | None, new_str: str = ""
) -> ToolResult:
    if not old_str:
        return ToolResult.error("old_str parameter required for str_replace command")
    if not os.path.isfile(path):
        return ToolResult.error(f"File not found: {path}")
    with open(path, "r") as f:
        content = f.read()
    num_replacements = content.count(old_str)
    if num_replacements == 0:
        return ToolResult.error(f"String '{old_str}' not found in file")
    new_content = content.replace(old_str, new_str)
    rendered_diff = difflib.unified_diff(
        content.splitlines(),
        new_content.splitlines(),
        fromfile=f"old/{path}",
        tofile=f"new/{path}",
        lineterm="",
    )
    with open(path, "w") as f:
        f.write(new_content)
    return ToolResult(
        f"Successfully made {num_replacements} replacement{'s' if num_replacements != 1 else ''} in {path}",
        meta_info={"diff": "\n".join(rendered_diff)},
    )


def insert_command(
    path: str, insert_line: int | None, new_str: str | None
) -> ToolResult:
    if not (insert_line is not None and new_str):
        return ToolResult.error("insert_line and new_str required for insert command")
    if not os.path.isfile(path):
        return ToolResult.error(f"File not found: {path}")
    with open(path, "r") as f:
        lines = f.readlines()
    if insert_line > len(lines):
        return ToolResult.error(
            f"Insert line {insert_line} beyond file length {len(lines)}"
        )
    lines.insert(insert_line, new_str + "\n")
    with open(path, "w") as f:
        f.writelines(lines)
    return ToolResult(f"Successful insertion in {path}")


async def test_editor_tool():
    print("Starting Editor Tool tests...")

    # Create tool instance
    editor = EditorTool()
    print(editor.spec().description)

    async with editor.context():
        # Test create command
        test_file = "/tmp/test_editor.txt"
        test_content = "Line 1\nLine 2\nLine 3\n"

        print("\nTest 1: Create file")
        result = await editor(command="create", path=test_file, file_text=test_content)
        print(f"Result: {result}")

        # Test view command
        print("\nTest 2: View file")
        result = await editor(command="view", path=test_file)
        print(f"Result: {result}")

        # Test view with range
        print("\nTest 3: View file with range")
        result = await editor(command="view", path=test_file, view_range=[1, 2])
        print(f"Result: {result}")

        # Test str_replace
        print("\nTest 4: String replace")
        # Test successful replace
        result = await editor(
            command="str_replace",
            path=test_file,
            old_str="Line 2",
            new_str="Modified Line 2",
        )
        print(f"Result: {result}")
        with open(test_file) as f:
            content = f.read()
        assert (
            content == "Line 1\nModified Line 2\nLine 3\n"
        ), f"Unexpected content after replace: {content!r}"

        # Test replace with non-existent string
        result = await editor(
            command="str_replace",
            path=test_file,
            old_str="Non-existent Line",
            new_str="Modified Line",
        )
        print(f"Result: {result}")
        assert result.is_error, "Expected error for non-existent string"

        # Test insert
        print("\nTest 5: Insert")
        result = await editor(
            command="insert", path=test_file, insert_line=2, new_str="Inserted Line"
        )
        print(f"Result: {result}")
        with open(test_file) as f:
            content = f.read()
        assert (
            content == "Line 1\nModified Line 2\nInserted Line\nLine 3\n"
        ), f"Unexpected content after insert: {content!r}"

        # Cleanup
        os.remove(test_file)


if __name__ == "__main__":
    # python -m clam_ptb.tools.editor_tool
    import asyncio

    asyncio.run(test_editor_tool())
