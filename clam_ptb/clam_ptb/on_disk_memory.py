import socket
import subprocess

from clam_ptb.settings import PROJECT_ROOT


def is_local() -> bool:
    return socket.gethostname() == "AL.local"


MEMORY_PATH = (
    PROJECT_ROOT / ("../core_knowledge" if is_local() else "core_knowledge")
).resolve()

MEMORY_AUTOLOAD_PATH = MEMORY_PATH / "autoload"


def read_autoload_memory() -> dict[str, str]:
    """Reads all *.md files form MEMORY_PATH"""
    memories = {}
    assert MEMORY_AUTOLOAD_PATH.exists(), f"{MEMORY_AUTOLOAD_PATH} does not exist"
    for path in MEMORY_AUTOLOAD_PATH.glob("*.md"):
        memories[str(path)] = path.read_text()
    return memories


def list_memory() -> list[str]:
    """Reads all *.md files form MEMORY_PATH"""
    memories = []
    assert MEMORY_PATH.exists(), f"{MEMORY_PATH} does not exist"
    for path in MEMORY_PATH.glob("**/*.md"):
        memories.append(str(path))
    return sorted(memories)


def render_memory_content() -> str:
    lines = []
    for name, content in sorted(read_autoload_memory().items()):
        lines.append(f"$ cat {name}")
        lines.append(content)
    lines.append(f"$ find {MEMORY_PATH}")
    lines.extend(list_memory())
    return "\n".join(lines)


def commit_memory() -> None:
    if not is_local():
        subprocess.check_call(
            "git add .; if [[ -n $(git status --porcelain) ]]; then git commit -a -m 'update memory'; fi; git push --force",
            cwd=MEMORY_PATH,
            executable="/bin/bash",
            shell=True,
        )
