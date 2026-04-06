"""Test composite task decomposition."""
from lib.router import TaskRouter, TaskType


class TestCompositeRouting:
    """Composite is still a valid type via explicit [type:composite] tag,
    but keyword routing no longer auto-assigns it — tasks should just be done."""

    def test_find_and_fix_is_analysis(self):
        router = TaskRouter()
        # "find...bug" pattern → analysis (read-only exploration)
        assert router.classify("Find three bugs and fix") == TaskType.ANALYSIS

    def test_fix_all_is_bugfix(self):
        router = TaskRouter()
        # "fix" keyword without specific quickfix qualifier → bugfix
        assert router.classify("Fix all lint warnings") == TaskType.BUGFIX

    def test_update_all_is_quickfix(self):
        router = TaskRouter()
        assert router.classify("Update all deprecated imports") == TaskType.QUICKFIX

    def test_explicit_tag_still_works(self):
        router = TaskRouter()
        assert router.classify("[type:composite] Break this into sub-tasks") == TaskType.COMPOSITE

    def test_explicit_tag_overrides(self):
        router = TaskRouter()
        assert router.classify("[type:bugfix] Fix all lint warnings") == TaskType.BUGFIX


class TestSubtaskParser:
    def test_parses_subtask_description_pairs(self):
        text = """Here are the sub-tasks:

SUBTASK: Fix null check in auth module
DESCRIPTION: Add null check for user parameter in auth/login.py:45

SUBTASK: Update deprecated datetime call
DESCRIPTION: Replace datetime.utcnow() with datetime.now(UTC) in utils/time.py
"""
        subtasks = _parse_subtasks_standalone(text)
        assert len(subtasks) == 2
        assert subtasks[0] == (
            "Fix null check in auth module",
            "Add null check for user parameter in auth/login.py:45",
        )
        assert subtasks[1] == (
            "Update deprecated datetime call",
            "Replace datetime.utcnow() with datetime.now(UTC) in utils/time.py",
        )

    def test_handles_empty_output(self):
        subtasks = _parse_subtasks_standalone("")
        assert subtasks == []

    def test_handles_no_subtasks_in_output(self):
        subtasks = _parse_subtasks_standalone("No issues found in the codebase.")
        assert subtasks == []

    def test_ignores_subtask_without_description(self):
        text = """SUBTASK: Fix something
Some random text without DESCRIPTION prefix
SUBTASK: Another fix
DESCRIPTION: This one has a description
"""
        subtasks = _parse_subtasks_standalone(text)
        assert len(subtasks) == 1
        assert subtasks[0][0] == "Another fix"


def _parse_subtasks_standalone(text: str) -> list:
    """Standalone copy of orchestrator's _parse_subtasks for isolated testing.

    NOTE: Keep in sync with lib/orchestrator.py _parse_subtasks().
    """
    subtasks = []
    lines = text.split("\n")
    current_title = None
    for line in lines:
        line = line.strip()
        if line.startswith("SUBTASK:"):
            current_title = line[len("SUBTASK:"):].strip()
        elif line.startswith("DESCRIPTION:") and current_title:
            description = line[len("DESCRIPTION:"):].strip()
            subtasks.append((current_title, description))
            current_title = None
    return subtasks
