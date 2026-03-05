import json
import subprocess
from inspect import cleandoc
from typing import Tuple

from clam_ptb.settings import PROJECT_ROOT
from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

CWD = PROJECT_ROOT


class BashSingleCommandRunner:
    def execute(self, cmd: str) -> Tuple[str, str, bool]:
        """
        Execute a bash command and return (stdout, stderr, had_error).

        Args:
            cmd: The Python code to execute

        Returns:
            Tuple containing:
            - stdout: captured standard output
            - stderr: captured standard error
            - had_error: True if an exception was raised
        """
        # using subprocess
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=CWD,
        )
        try:
            stdout, stderr = process.communicate()
        except Exception as e:
            return "", str(e), True
        stdout = stdout.decode()
        stderr = stderr.decode()
        had_error = process.returncode != 0
        return stdout, stderr, had_error


class BashRunTool(LocalTool):
    def __init__(self):
        self._rept: BashSingleCommandRunner
        self._max_output_length = 4096

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash_run",
            description=cleandoc(f"""
                Executes single sh command. Not persistent between calls.

                CWD is {CWD}
                """),
            args=[
                ArgSpec(
                    name="code",
                    type=str,
                    description="Bash code to execute",
                    is_required=True,
                )
            ],
        )

    async def _execute(self, *, code: str, **kwargs) -> ToolResult:
        def _clip(s: str) -> str:
            if len(s) <= self._max_output_length:
                return s
            num_extra_chars = len(s) - self._max_output_length
            return s[: self._max_output_length] + f"... ({num_extra_chars} more chars)"

        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        try:
            stdout, stderr, is_error = self._repl.execute(code)
            result = dict(stdout=_clip(stdout), stderr=_clip(stderr))
            return ToolResult(json.dumps(result), is_error=is_error)

        except Exception as e:
            return ToolResult.error(f"Error executing Bash code: {str(e)}")

    async def init(self):
        self._repl = BashSingleCommandRunner()

    async def close(self):
        """Cleanup when the tool is destroyed."""
        del self._repl


async def run_tests():
    print("Starting Bash Runner Tool tests...")

    # Create tool instance
    runner = BashRunTool()

    async with runner.context():
        # Test 1: Basic execution
        print("\nTest 1: Basic execution")
        result = await runner(code="echo 'Hello, World!'")
        print(f"Result: {result}")

        # Test 2: Command with args
        print("\nTest 2: Command with args")
        result = await runner(code="ls -l")
        print(f"Result: {result}")

        # Test 3: Pipeline
        print("\nTest 3: Pipeline")
        result = await runner(code="echo 'test' | grep 'test'")
        print(f"Result: {result}")

        # Test 4: Error handling
        print("\nTest 4: Error handling")
        result = await runner(code="nonexistent_command")
        print(f"Result: {result}")

        # Test 5: Multiple commands
        print("\nTest 5: Multiple commands")
        result = await runner(
            code="""
            mkdir -p /tmp/test_dir
            cd /tmp/test_dir
            touch test.txt
            echo "test content" > test.txt
            cat test.txt
            cd ..
            rm -r /tmp/test_dir
            """
        )
        print(f"Result: {result}")


if __name__ == "__main__":
    # python -m clam_ptb.tools.bash_repl
    # Run the tests
    import asyncio

    asyncio.run(run_tests())
