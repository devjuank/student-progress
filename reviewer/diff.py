"""Reads a PR from git: touched files, added lines and file contents at base and head."""
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class FileChange:
    path: str
    status: str  # git name-status letter: A (added), M (modified), D (deleted)
    added: dict[int, str] = field(default_factory=dict)  # head line number -> text


@dataclass
class PRContext:
    files: dict[str, FileChange]
    # read(ref, path) -> file content, or None if the file does not exist at that ref.
    read: Callable[[str, str], str | None]
    base_ref: str
    head_ref: str

    def base(self, path: str) -> str | None:
        return self.read(self.base_ref, path)

    def head(self, path: str) -> str | None:
        return self.read(self.head_ref, path)


def parse_unified_diff(text: str) -> dict[str, dict[int, str]]:
    """Maps each file in a unified diff to its added lines, keyed by head line number."""
    added: dict[str, dict[int, str]] = {}
    current: dict[int, str] | None = None
    line_no = 0
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            current = None if target == "/dev/null" else added.setdefault(target.removeprefix("b/"), {})
        elif raw.startswith("--- "):
            continue
        elif match := HUNK_HEADER.match(raw):
            line_no = int(match.group(1))
        elif current is not None and raw.startswith("+"):
            current[line_no] = raw[1:]
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
    return added


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def load_pr(base: str, head: str, repo: str = ".") -> PRContext:
    """Builds the PR context from git, diffing head against its merge base with base."""
    merge_base = _git(repo, "merge-base", base, head).strip()
    statuses = {}
    for row in _git(repo, "diff", "--name-status", "--no-renames", merge_base, head).splitlines():
        status, path = row.split("\t", 1)
        statuses[path] = status[0]
    added = parse_unified_diff(_git(repo, "diff", "--unified=0", "--no-renames", "--no-color", merge_base, head))
    files = {path: FileChange(path, status, added.get(path, {})) for path, status in statuses.items()}

    def read(ref: str, path: str) -> str | None:
        result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None

    return PRContext(files=files, read=read, base_ref=merge_base, head_ref=head)
