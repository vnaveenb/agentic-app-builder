"""Unit tests for the versioning differ module."""

import pytest

from src.dev_agent.versioning.differ import (
    DiffResult,
    FileDiff,
    FileStatus,
    compute_diff,
    compute_file_diff,
)


@pytest.mark.unit
class TestComputeFileDiff:
    def test_added_file(self) -> None:
        result = compute_file_diff("new.py", None, "print('hello')\n")
        assert result.status == FileStatus.ADDED
        assert result.additions == 1
        assert result.deletions == 0
        assert "b/new.py" in result.diff

    def test_deleted_file(self) -> None:
        result = compute_file_diff("old.py", "print('goodbye')\n", None)
        assert result.status == FileStatus.DELETED
        assert result.additions == 0
        assert result.deletions == 1
        assert "a/old.py" in result.diff

    def test_unchanged_file(self) -> None:
        content = "x = 1\n"
        result = compute_file_diff("same.py", content, content)
        assert result.status == FileStatus.UNCHANGED
        assert result.diff == ""
        assert result.additions == 0
        assert result.deletions == 0

    def test_modified_file(self) -> None:
        v1 = "x = 1\ny = 2\n"
        v2 = "x = 1\ny = 3\nz = 4\n"
        result = compute_file_diff("mod.py", v1, v2)
        assert result.status == FileStatus.MODIFIED
        assert result.additions == 2  # y = 3, z = 4
        assert result.deletions == 1  # y = 2
        assert "a/mod.py" in result.diff
        assert "b/mod.py" in result.diff


@pytest.mark.unit
class TestComputeDiff:
    def test_empty_snapshots(self) -> None:
        result = compute_diff({}, {}, 1, 2)
        assert result.v1 == 1
        assert result.v2 == 2
        assert result.changes == []
        assert result.summary["added"] == 0

    def test_all_new_files(self) -> None:
        files_v1: dict[str, str] = {}
        files_v2 = {"main.py": "print('hi')\n", "utils.py": "def helper(): pass\n"}
        result = compute_diff(files_v1, files_v2, 0, 1)
        assert len(result.changes) == 2
        assert result.summary["added"] == 2
        assert result.summary["modified"] == 0
        assert result.summary["deleted"] == 0

    def test_mixed_changes(self) -> None:
        files_v1 = {
            "keep.py": "x = 1\n",
            "modify.py": "a = 1\n",
            "remove.py": "old\n",
        }
        files_v2 = {
            "keep.py": "x = 1\n",  # unchanged
            "modify.py": "a = 2\n",  # modified
            "add.py": "new\n",  # added
            # remove.py is gone — deleted
        }
        result = compute_diff(files_v1, files_v2, 1, 2)
        statuses = {c.file: c.status for c in result.changes}
        assert "keep.py" not in statuses  # unchanged excluded
        assert statuses["modify.py"] == FileStatus.MODIFIED
        assert statuses["add.py"] == FileStatus.ADDED
        assert statuses["remove.py"] == FileStatus.DELETED
        assert result.summary["added"] == 1
        assert result.summary["modified"] == 1
        assert result.summary["deleted"] == 1

    def test_summary_totals(self) -> None:
        files_v1 = {"a.py": "line1\nline2\nline3\n"}
        files_v2 = {"a.py": "line1\nmodified\nline3\nnew_line\n"}
        result = compute_diff(files_v1, files_v2, 1, 2)
        assert result.summary["total_additions"] > 0
        assert result.summary["total_deletions"] > 0
