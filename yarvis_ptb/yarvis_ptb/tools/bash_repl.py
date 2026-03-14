import json
import subprocess
import tempfile
from inspect import cleandoc
from typing import Tuple

from yarvis_ptb.settings import PROJECT_ROOT
from yarvis_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec
from yarvis_ptb.util import truncate_middle_and_maybe_save

TOOL_DEFAULT_TIMEOUT_SEC = 15
TOOL_MAX_TIMEOUT_SEC = 600

CWD = PROJECT_ROOT


class BashSingleCommandRunner:
    def execute(self, cmd: str, timeout: int | None = None) -> Tuple[str, str, bool]:
        """
        Execute a bash command and return (stdout, stderr, had_error).

        Args:
            cmd: The bash command to execute
            timeout: Timeout in seconds for the command (None = no timeout)

        Returns:
            Tuple containing:
            - stdout: captured standard output
            - stderr: captured standard error
            - had_error: True if an exception was raised
        """
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=CWD,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return "", f"Command timed out after {timeout} seconds", True
        except Exception as e:
            return "", str(e), True
        stdout = stdout.decode()
        stderr = stderr.decode()
        had_error = process.returncode != 0
        return stdout, stderr, had_error


class BashRunTool(LocalTool):
    def __init__(self):
        self._repl: BashSingleCommandRunner
        self._max_output_length = 10000

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash_run",
            description=cleandoc(f"""
                Executes single sh command. Not persistent between calls.
                Output is truncated after {self._max_output_length} characters (middle-truncated, keeping start and end).

                CWD is {CWD}
                """),
            args=[
                ArgSpec(
                    name="code",
                    type=str,
                    description="Bash code to execute",
                    is_required=True,
                ),
                ArgSpec(
                    name="timeout_sec",
                    type=int,
                    description=f"Timeout in seconds (default {TOOL_DEFAULT_TIMEOUT_SEC}, max {TOOL_MAX_TIMEOUT_SEC}). Use higher values for long-running commands.",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self, *, code: str, timeout_sec: int | None = None, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        timeout = min(timeout_sec or TOOL_DEFAULT_TIMEOUT_SEC, TOOL_MAX_TIMEOUT_SEC)
        try:
            stdout, stderr, is_error = self._repl.execute(code, timeout=timeout)

            def clip(s: str, stream: str) -> str:
                save_path = None
                if len(s) > self._max_output_length:
                    f = tempfile.NamedTemporaryFile(
                        prefix=f"bash_{stream}_",
                        suffix=".txt",
                        dir="/tmp",
                        delete=False,
                    )
                    save_path = f.name
                    f.close()
                return truncate_middle_and_maybe_save(
                    s, self._max_output_length, save_path=save_path
                )

            result = dict(stdout=clip(stdout, "stdout"), stderr=clip(stderr, "stderr"))
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

        # Test 5: Timeout - command should timeout
        print("\nTest 5: Timeout (2s limit on sleep 10)")
        import time as _time

        t0 = _time.monotonic()
        result = await runner(code="sleep 10", timeout_sec=2)
        elapsed = _time.monotonic() - t0
        print(f"Result: {result}")
        assert result.is_error, f"Expected error, got: {result}"
        assert (
            "timed out" in result.text.lower()
        ), f"Expected timeout message, got: {result.text}"
        assert elapsed < 5, f"Timeout took too long: {elapsed:.1f}s"
        print(f"  Elapsed: {elapsed:.1f}s (expected ~2s)")

        # Test 6: Default timeout should be applied
        print("\nTest 6: Default timeout is applied (fast command succeeds)")
        result = await runner(code="echo 'quick'")
        assert not result.is_error, f"Expected success, got: {result}"
        print(f"Result: {result}")

        # Test 7: Multiple commands
        print("\nTest 7: Multiple commands")
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
    # python -m yarvis_ptb.tools.bash_repl
    # Run the tests
    import asyncio

    asyncio.run(run_tests())
