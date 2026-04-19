"""Workspace/logseq diff route — daily git diffs."""

import datetime
import logging
import os
import subprocess

import pytz
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

bp = Blueprint("workspace", __name__)

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

REPOS = {
    "workspace": {
        "dir": os.path.join(PROJECT_ROOT, "workspace"),
        "exclude": [":!claude-ai-data-*"],
        "author": None,
    },
    "logseq": {
        "dir": os.path.join(PROJECT_ROOT, "logseq"),
        "exclude": [],
        "author": "Yarvis",
    },
}


def _git(repo_dir: str, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _get_diff_for_date(repo_key: str, date_str: str) -> dict:
    """Compute git diff for a repo on a given date (1am-to-1am boundary)."""
    repo = REPOS[repo_key]
    repo_dir = repo["dir"]
    exclude = repo["exclude"]
    author = repo["author"]

    date_obj = datetime.date.fromisoformat(date_str)
    next_date = (date_obj + datetime.timedelta(days=1)).isoformat()

    from yarvis_ptb.timezones import get_complex_chat_timezone_str

    tz = pytz.timezone(get_complex_chat_timezone_str())
    today = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    is_today = date_str == today

    # Build author filter
    author_args = ["--author", author] if author else []

    # Find first commit after 1am on date
    out = _git(
        repo_dir,
        [
            "log",
            "--oneline",
            "--reverse",
            "--format=%H",
            f"--after={date_str}T01:00:00",
            *author_args,
        ],
    )
    lines = out.strip().split("\n") if out.strip() else []
    from_commit = lines[0] if lines else None

    # Find last commit
    if is_today:
        to_commit = "HEAD" if not author else None
        if author:
            # For author-filtered repos, find last commit by that author
            out = _git(
                repo_dir,
                [
                    "log",
                    "--format=%H",
                    "-1",
                    *author_args,
                ],
            )
            to_commit = out.strip() or None
    else:
        out = _git(
            repo_dir,
            [
                "log",
                "--format=%H",
                "-1",
                f"--before={next_date}T01:00:00",
                *author_args,
            ],
        )
        to_commit = out.strip() or None

    if not from_commit:
        return {
            "date": date_str,
            "repo": repo_key,
            "diff": "",
            "stat": "",
            "commits": 0,
            "error": "No commits found for this date",
        }

    # Count commits in range
    count_args = [
        "log",
        "--oneline",
        f"--after={date_str}T01:00:00",
        *author_args,
    ]
    if not is_today:
        count_args.append(f"--before={next_date}T01:00:00")
    commit_log = _git(repo_dir, count_args)
    num_commits = len(commit_log.strip().split("\n")) if commit_log.strip() else 0

    if author:
        # For author-filtered repos, use git log -p to get only that author's patches.
        # A plain git diff between first..last would include other authors' changes.
        log_args = [
            "log",
            "-p",
            "--stat",
            "--reverse",
            f"--after={date_str}T01:00:00",
            *author_args,
        ]
        if not is_today:
            log_args.append(f"--before={next_date}T01:00:00")
        full_log = _git(repo_dir, log_args)

        # Split stat and diff from the log output
        return {
            "date": date_str,
            "repo": repo_key,
            "diff": full_log,
            "stat": "",
            "commits": num_commits,
            "from_commit": from_commit[:7],
            "to_commit": (to_commit or from_commit)[:7],
        }

    if not to_commit:
        to_commit = from_commit

    # Get parent to include from_commit's changes
    parent = _git(repo_dir, ["rev-parse", f"{from_commit}^"])
    if not parent:
        parent = from_commit

    # Diff
    diff = _git(
        repo_dir,
        [
            "diff",
            parent,
            to_commit,
            "--",
            ".",
            *exclude,
        ],
    )

    # Stat
    stat = _git(
        repo_dir,
        [
            "diff",
            "--stat",
            parent,
            to_commit,
            "--",
            ".",
            *exclude,
        ],
    )

    return {
        "date": date_str,
        "repo": repo_key,
        "diff": diff,
        "stat": stat,
        "commits": num_commits,
        "from_commit": parent[:7],
        "to_commit": to_commit[:7] if to_commit != "HEAD" else "HEAD",
    }


@bp.route("/api/workspace-pull", methods=["POST"])
def api_workspace_pull():
    """Run git pull in the workspace repo."""
    repo_dir = REPOS["workspace"]["dir"]
    result = subprocess.run(
        ["git", "-C", repo_dir, "pull", "--ff-only"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    ok = result.returncode == 0
    output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
    if not ok:
        logger.warning(
            "workspace-pull failed (rc=%s) in %s:\n%s",
            result.returncode,
            repo_dir,
            output,
        )
    return jsonify({"ok": ok, "output": output}), 200 if ok else 500


@bp.route("/api/workspace-diff")
def api_workspace_diff():
    """Return git diffs for workspace and logseq on a given date.

    Query params:
      date: YYYY-MM-DD (default: today)
    """
    date_str = request.args.get("date")
    if not date_str:
        from yarvis_ptb.timezones import get_complex_chat_timezone_str

        tz = pytz.timezone(get_complex_chat_timezone_str())
        date_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    workspace_diff = _get_diff_for_date("workspace", date_str)
    logseq_diff = _get_diff_for_date("logseq", date_str)

    return jsonify(
        {
            "date": date_str,
            "workspace": workspace_diff,
            "logseq": logseq_diff,
        }
    )
