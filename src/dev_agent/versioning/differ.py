"""Diff computation — on-demand unified diffs between file snapshots."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import Enum


class FileStatus(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    UNCHANGED = "unchanged"


@dataclass
class FileDiff:
    """Diff result for a single file between two versions."""

    file: str
    status: FileStatus
    diff: str = ""
    additions: int = 0
    deletions: int = 0


@dataclass
class DiffResult:
    """Full diff result between two version snapshots."""

    v1: int
    v2: int
    changes: list[FileDiff] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def compute_file_diff(filename: str, content_v1: str | None, content_v2: str | None) -> FileDiff:
    """Compute unified diff for a single file between two versions."""
    if content_v1 is None and content_v2 is not None:
        # File added in v2
        lines = content_v2.splitlines(keepends=True)
        diff_text = "".join(
            difflib.unified_diff(
                [], lines,
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm="",
            )
        )
        return FileDiff(
            file=filename,
            status=FileStatus.ADDED,
            diff=diff_text,
            additions=len(lines),
            deletions=0,
        )

    if content_v1 is not None and content_v2 is None:
        # File deleted in v2
        lines = content_v1.splitlines(keepends=True)
        diff_text = "".join(
            difflib.unified_diff(
                lines, [],
                fromfile=f"a/{filename}",
                tofile=f"b/{filename}",
                lineterm="",
            )
        )
        return FileDiff(
            file=filename,
            status=FileStatus.DELETED,
            diff=diff_text,
            additions=0,
            deletions=len(lines),
        )

    if content_v1 == content_v2:
        return FileDiff(file=filename, status=FileStatus.UNCHANGED)

    # File modified
    lines_v1 = (content_v1 or "").splitlines(keepends=True)
    lines_v2 = (content_v2 or "").splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            lines_v1, lines_v2,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
    )
    diff_text = "".join(diff_lines)
    additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

    return FileDiff(
        file=filename,
        status=FileStatus.MODIFIED,
        diff=diff_text,
        additions=additions,
        deletions=deletions,
    )


def compute_diff(
    files_v1: dict[str, str],
    files_v2: dict[str, str],
    v1_number: int = 0,
    v2_number: int = 0,
) -> DiffResult:
    """Compute full diff between two file snapshots.

    Categorizes files as added, modified, deleted, or unchanged.
    Only returns changed files (excludes unchanged).
    """
    all_files = sorted(set(list(files_v1.keys()) + list(files_v2.keys())))
    changes: list[FileDiff] = []

    for filename in all_files:
        content_v1 = files_v1.get(filename)
        content_v2 = files_v2.get(filename)
        file_diff = compute_file_diff(filename, content_v1, content_v2)

        if file_diff.status != FileStatus.UNCHANGED:
            changes.append(file_diff)

    summary = {
        "added": sum(1 for c in changes if c.status == FileStatus.ADDED),
        "modified": sum(1 for c in changes if c.status == FileStatus.MODIFIED),
        "deleted": sum(1 for c in changes if c.status == FileStatus.DELETED),
        "total_additions": sum(c.additions for c in changes),
        "total_deletions": sum(c.deletions for c in changes),
    }

    return DiffResult(v1=v1_number, v2=v2_number, changes=changes, summary=summary)
