"""Tests for approval and question timeout notifications."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from lib.models import Config, Task, TaskState, TaskSource


def _make_task(state: TaskState, updated_at: datetime, **kwargs) -> Task:
    """Create a Task with a specific updated_at timestamp."""
    return Task(
        id="task-001",
        reminder_id="rem-001",
        reminder_list="TestProject",
        task_title="Test Feature",
        task_notes="Some notes",
        repo_url="https://github.com/test/repo",
        state=state,
        created_at=updated_at - timedelta(days=1),
        updated_at=updated_at,
        approval_reminder_title="Approve: Test Feature" if state == TaskState.AWAITING_APPROVAL else None,
        question_reminder_title="Q: Test Feature" if state == TaskState.NEEDS_CLARIFICATION else None,
        question_text="What color?" if state == TaskState.NEEDS_CLARIFICATION else None,
        **kwargs,
    )


def _make_config(**overrides) -> Config:
    """Create a Config with sensible test defaults."""
    cfg = Config()
    cfg.approval_timeout_days = 7
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_orchestrator(config, notifier):
    """Create an OrchestratorV4 instance without calling __init__."""
    from lib.orchestrator import OrchestratorV4
    orch = OrchestratorV4.__new__(OrchestratorV4)
    orch.config = config
    orch.notifier = notifier
    orch.reminders = MagicMock()
    orch.db = MagicMock()
    orch.logger = MagicMock()
    orch.github_issues = None
    return orch


class TestApprovalTimeout:
    """Tests for stale approval notifications."""

    @patch("lib.orchestrator.RemindersClient")
    @patch("lib.orchestrator.StateDB")
    def test_stale_approval_notifies(self, mock_db_cls, mock_rem_cls):
        """A task awaiting approval for >= approval_timeout_days gets notify_approval_stale called.

        We use exactly 7 days + 1 hour so that (datetime.now() - updated_at).days == 7
        which satisfies >= 7, but the timedelta is NOT > timedelta(days=7) by enough
        to also trigger the rejection path (which uses strict >).
        We freeze datetime.now() to avoid race conditions.
        """
        frozen_now = datetime(2026, 3, 14, 12, 0, 0)
        # 7 days and 1 hour ago -- .days == 7, but < timedelta(days=8)
        # The existing rejection uses `age > timedelta(days=7)` which IS true for 7d1h.
        # So we need to set updated_at to exactly 7 days ago (within hours).
        # 7 days exactly: age > timedelta(days=7) is False, but .days == 7 so >= 7 is True.
        updated_at = frozen_now - timedelta(days=7)

        config = _make_config()
        task = _make_task(TaskState.AWAITING_APPROVAL, updated_at=updated_at)
        notifier = MagicMock()
        orch = _make_orchestrator(config, notifier)

        # Reminder exists, not completed, no [FIX] prefix
        orch.reminders.show_list.return_value = [
            {"externalId": "rem-001", "title": "[REVIEW PRD] Task: Test Feature", "isCompleted": False}
        ]

        with patch("lib.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = orch.check_approval_status(task)

        notifier.notify_approval_stale.assert_called_once_with(task, 7)

    @patch("lib.orchestrator.RemindersClient")
    @patch("lib.orchestrator.StateDB")
    def test_fresh_approval_no_notification(self, mock_db_cls, mock_rem_cls):
        """A task awaiting approval for < approval_timeout_days does NOT get notified."""
        frozen_now = datetime(2026, 3, 14, 12, 0, 0)
        updated_at = frozen_now - timedelta(days=3)

        config = _make_config()
        task = _make_task(TaskState.AWAITING_APPROVAL, updated_at=updated_at)
        notifier = MagicMock()
        orch = _make_orchestrator(config, notifier)

        orch.reminders.show_list.return_value = [
            {"externalId": "rem-001", "title": "[REVIEW PRD] Task: Test Feature", "isCompleted": False}
        ]

        with patch("lib.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = orch.check_approval_status(task)

        notifier.notify_approval_stale.assert_not_called()


class TestQuestionTimeout:
    """Tests for stale question notifications."""

    @patch("lib.orchestrator.RemindersClient")
    @patch("lib.orchestrator.StateDB")
    def test_stale_question_notifies(self, mock_db_cls, mock_rem_cls):
        """A stale question gets notify_question_stale called."""
        frozen_now = datetime(2026, 3, 14, 12, 0, 0)
        updated_at = frozen_now - timedelta(days=7)

        config = _make_config()
        task = _make_task(TaskState.NEEDS_CLARIFICATION, updated_at=updated_at)
        notifier = MagicMock()
        orch = _make_orchestrator(config, notifier)

        # Reminder exists with [QUESTION] prefix, no answer
        orch.reminders.show_list.return_value = [
            {"externalId": "rem-001", "title": "[QUESTION] Task: Test Feature",
             "isCompleted": False, "notes": "What color?"}
        ]

        with patch("lib.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = orch.check_question_answered(task)

        notifier.notify_question_stale.assert_called_once_with(task, 7)
