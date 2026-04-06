"""Test duplicate task detection with title prefix fallback."""
from datetime import datetime
from uuid import uuid4

from lib.db import StateDB
from lib.models import Task, TaskState


def _make_task(title="Fix login bug", state=TaskState.NEW, reminder_list="TestProject") -> Task:
    return Task(
        id=str(uuid4()),
        reminder_id=f"r-{uuid4()}",
        reminder_list=reminder_list,
        task_title=title,
        task_notes="",
        repo_url="https://github.com/test/repo",
        state=state,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def test_title_prefix_finds_active_task(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Fix login bug in auth module")
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is not None
    assert result.id == task.id


def test_title_prefix_ignores_completed_tasks(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Fix login bug", state=TaskState.COMPLETE)
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is None


def test_title_prefix_ignores_rejected_tasks(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Fix login bug", state=TaskState.REJECTED)
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is None


def test_title_prefix_ignores_error_tasks(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Fix login bug", state=TaskState.ERROR)
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is None


def test_title_prefix_scoped_to_project(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Fix login bug", reminder_list="OtherProject")
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is None


def test_title_prefix_no_match(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task("Add new feature")
    db.save_task(task)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is None


def test_title_prefix_returns_most_recent(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task1 = _make_task("Fix login bug v1")
    task1.created_at = datetime(2024, 1, 1)
    db.save_task(task1)

    task2 = _make_task("Fix login bug v2")
    task2.created_at = datetime(2024, 6, 1)
    db.save_task(task2)

    result = db.get_active_task_by_title_prefix("TestProject", "Fix login bug")
    assert result is not None
    assert result.id == task2.id


def test_title_prefix_escapes_like_wildcards(tmp_path):
    """LIKE special chars in the prefix must be treated as literals."""
    db = StateDB(tmp_path / "test.db")
    task = _make_task("fix_bug_123")
    db.save_task(task)

    # "fix%" contains a literal %, should NOT act as a wildcard matching "fix_bug_123"
    result = db.get_active_task_by_title_prefix("TestProject", "fix%")
    assert result is None, "% in prefix must be escaped, not treated as LIKE wildcard"

    # "fix_" as a literal prefix should NOT match "fixXbug" (where _ would be
    # a single-char wildcard in unescaped LIKE)
    task2 = _make_task("fixXbug")
    db.save_task(task2)
    result = db.get_active_task_by_title_prefix("TestProject", "fix_")
    # With proper escaping, "fix_" only matches titles starting with literal "fix_",
    # not "fixX". It DOES still match "fix_bug_123" since that starts with "fix_".
    assert result is not None
    assert result.id == task.id, "fix_ should match fix_bug_123 (literal prefix) not fixXbug"

    # Verify that without escaping, "fix_" would have matched "fixXbug" too.
    # With escaping, searching for "fixX" should find task2.
    result = db.get_active_task_by_title_prefix("TestProject", "fixXbug")
    assert result is not None
    assert result.id == task2.id

    # Exact prefix with underscores should still match
    result = db.get_active_task_by_title_prefix("TestProject", "fix_bug")
    assert result is not None
    assert result.id == task.id
