"""Test that SQLite connections use WAL mode and busy timeout."""
import threading
from datetime import datetime
from uuid import uuid4
from lib.db import StateDB
from lib.models import Task, TaskState


def test_wal_mode_enabled(tmp_path):
    db = StateDB(tmp_path / "test.db")
    conn = db._get_connection()
    result = conn.execute("PRAGMA journal_mode").fetchone()
    assert result[0] == "wal", f"Expected WAL mode, got {result[0]}"


def test_busy_timeout_set(tmp_path):
    db = StateDB(tmp_path / "test.db")
    conn = db._get_connection()
    result = conn.execute("PRAGMA busy_timeout").fetchone()
    assert result[0] >= 5000, f"Expected busy_timeout >= 5000ms, got {result[0]}"


def test_concurrent_writes_dont_error(tmp_path):
    db = StateDB(tmp_path / "test.db")
    errors = []

    def write_task(i):
        try:
            task = Task(
                id=str(uuid4()), reminder_id=f"r-{i}", reminder_list="Test",
                task_title=f"Task {i}", task_notes="",
                repo_url="https://github.com/test/repo",
                state=TaskState.NEW,
                created_at=datetime.now(), updated_at=datetime.now(),
            )
            db.save_task(task)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write_task, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"Concurrent writes failed: {errors}"
