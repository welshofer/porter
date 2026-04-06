"""Tests for PID-validated LockFile."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "test.lock"


@pytest.fixture
def make_lock(lock_path):
    """Factory that creates a LockFile instance."""
    from porter import LockFile

    created = []

    def _make(path=None):
        lf = LockFile(path or lock_path)
        created.append(lf)
        return lf

    yield _make

    # Cleanup
    for lf in created:
        lf.release()


class TestLockFile:
    def test_acquire_creates_file_with_pid(self, lock_path, make_lock):
        lock = make_lock()
        assert lock.acquire() is True
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())

    def test_acquire_fails_if_another_process_holds_lock(self, lock_path, make_lock):
        # Write our own PID (which is alive) to simulate another holder
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()))

        lock = make_lock()
        assert lock.acquire() is False

    def test_acquire_succeeds_if_stale_pid(self, lock_path, make_lock):
        # PID 99999999 almost certainly doesn't exist
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999999")

        lock = make_lock()
        assert lock.acquire() is True
        assert lock_path.read_text().strip() == str(os.getpid())

    def test_release_removes_file(self, lock_path, make_lock):
        lock = make_lock()
        lock.acquire()
        lock.release()
        assert not lock_path.exists()

    def test_double_release_is_safe(self, lock_path, make_lock):
        lock = make_lock()
        lock.acquire()
        lock.release()
        lock.release()  # Should not raise
        assert not lock_path.exists()

    def test_acquire_succeeds_if_lock_file_corrupted(self, lock_path, make_lock):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not-a-pid")
        lock = make_lock()
        assert lock.acquire() is True
        assert lock_path.read_text().strip() == str(os.getpid())
