"""Unit tests for the memory store module."""

import pytest

from src.dev_agent.memory.memory_store import format_memories_for_prompt


class FakeMemory:
    """Minimal stand-in for Memory model for testing format function."""

    def __init__(self, category: str, key: str, value: str):
        self.category = category
        self.key = key
        self.value = value


@pytest.mark.unit
class TestFormatMemories:
    def test_empty_list(self) -> None:
        result = format_memories_for_prompt([])
        assert result == ""

    def test_single_memory(self) -> None:
        mems = [FakeMemory("preference", "dark_mode", "User prefers dark UI")]
        result = format_memories_for_prompt(mems)  # type: ignore[arg-type]
        assert "[PREFERENCE]" in result
        assert "dark_mode: User prefers dark UI" in result

    def test_multiple_categories(self) -> None:
        mems = [
            FakeMemory("preference", "style", "minimal"),
            FakeMemory("pattern", "flask_blueprints", "use blueprints"),
            FakeMemory("preference", "theme", "dark"),
        ]
        result = format_memories_for_prompt(mems)  # type: ignore[arg-type]
        assert "[PREFERENCE]" in result
        assert "[PATTERN]" in result
        assert "style: minimal" in result
        assert "flask_blueprints: use blueprints" in result
        assert "theme: dark" in result

    def test_output_structure(self) -> None:
        mems = [FakeMemory("project_summary", "todo_app", "Built a React todo app")]
        result = format_memories_for_prompt(mems)  # type: ignore[arg-type]
        lines = result.strip().split("\n")
        assert lines[0] == "[PROJECT_SUMMARY]"
        assert "- todo_app: Built a React todo app" in lines[1]
