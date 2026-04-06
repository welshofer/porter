"""Test atomic state transitions with optimistic locking."""
import pytest
from datetime import datetime
from uuid import uuid4

from lib.db import StateDB
from lib.models import Task, TaskState


def _make_task(state=TaskState.NEW) -> Task:
    return Task(
        id=str(uuid4()),
        reminder_id=f"r-{uuid4()}",
        reminder_list="Test",
        task_title="Test Task",
        task_notes="",
        repo_url="https://github.com/test/repo",
        state=state,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


def test_transition_if_succeeds_when_state_matches(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    result = db.transition_if(task.id, TaskState.NEW, TaskState.ROUTING)
    assert result is True

    reloaded = db.get_task(task.id)
    assert reloaded.state == TaskState.ROUTING


def test_transition_if_fails_when_state_stale(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    # Simulate another thread transitioning first
    db.transition_if(task.id, TaskState.NEW, TaskState.ROUTING)

    # Our attempt should fail because state is no longer NEW
    # (use a valid NEW-> transition to isolate the CAS test from D4 enforcement)
    result = db.transition_if(task.id, TaskState.NEW, TaskState.ROUTING)
    assert result is False

    # State should still be ROUTING (the first transition won)
    reloaded = db.get_task(task.id)
    assert reloaded.state == TaskState.ROUTING


def test_transition_if_logs_transition(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    db.transition_if(task.id, TaskState.NEW, TaskState.ROUTING, "test transition")

    # Check task_history table
    conn = db._get_connection()
    rows = conn.execute(
        "SELECT * FROM task_history WHERE task_id = ?", (task.id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["from_state"] == "NEW"
    assert rows[0]["to_state"] == "ROUTING"
    assert rows[0]["details"] == "test transition"


def test_transition_if_does_not_log_on_failure(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    # Try to transition from wrong state
    result = db.transition_if(task.id, TaskState.ROUTING, TaskState.IN_FLIGHT)
    assert result is False

    # No history should be logged
    conn = db._get_connection()
    rows = conn.execute(
        "SELECT * FROM task_history WHERE task_id = ?", (task.id,)
    ).fetchall()
    assert len(rows) == 0


def test_transition_if_updates_status_message(tmp_path):
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    db.transition_if(task.id, TaskState.NEW, TaskState.ROUTING, "queued for routing")

    reloaded = db.get_task(task.id)
    assert reloaded.status_message == "queued for routing"


def test_transition_if_warns_on_unexpected_transition(tmp_path, caplog):
    """D4: VALID_TRANSITIONS — unexpected transitions warn but proceed."""
    import logging
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.NEW)
    db.save_task(task)

    # NEW -> COMPLETE is not in VALID_TRANSITIONS — should warn but succeed
    with caplog.at_level(logging.WARNING):
        result = db.transition_if(task.id, TaskState.NEW, TaskState.COMPLETE)

    assert result is True
    assert "Unexpected state transition" in caplog.text

    reloaded = db.get_task(task.id)
    assert reloaded.state == TaskState.COMPLETE


def test_transition_if_allows_valid_transition(tmp_path):
    """D4: valid transitions pass through to CAS normally."""
    db = StateDB(tmp_path / "test.db")
    task = _make_task(TaskState.ROUTING)
    db.save_task(task)

    result = db.transition_if(task.id, TaskState.ROUTING, TaskState.IN_FLIGHT, "setting up")
    assert result is True

    reloaded = db.get_task(task.id)
    assert reloaded.state == TaskState.IN_FLIGHT
