"""Tests for parallelism fix: approval/question checks should NOT call _run_agent inline."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from lib.models import Task, TaskState, TaskSource, TaskPriority
from lib.orchestrator import OrchestratorV4 as Orchestrator


def _make_task(**overrides) -> Task:
    """Create a minimal Task with sensible defaults."""
    defaults = dict(
        id="test-id",
        reminder_id="reminder-1",
        reminder_list="TestProject",
        task_title="Test Task",
        task_notes="Some notes",
        repo_url="https://github.com/test/repo",
        state=TaskState.AWAITING_APPROVAL,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_orchestrator() -> Orchestrator:
    """Create an Orchestrator with mocked dependencies."""
    mock_config = MagicMock()
    mock_config.notes_fetch_mode = "json_only"
    mock_config.approval_timeout_days = 7
    mock_config.question_timeout_days = 7
    mock_db = MagicMock()
    mock_db.transition_if.return_value = True
    mock_reminders = MagicMock()
    mock_git = MagicMock()
    mock_notifier = MagicMock()

    orch = Orchestrator.__new__(Orchestrator)
    orch.config = mock_config
    orch.db = mock_db
    orch.reminders = mock_reminders
    orch.git = mock_git
    orch.notifier = mock_notifier
    orch.logger = MagicMock()
    orch._run_agent = MagicMock()

    # Bind real methods
    orch.transition = Orchestrator.transition.__get__(orch, Orchestrator)
    orch.check_approval_status = Orchestrator.check_approval_status.__get__(orch, Orchestrator)
    orch.check_question_answered = Orchestrator.check_question_answered.__get__(orch, Orchestrator)
    orch._find_task_reminder = Orchestrator._find_task_reminder.__get__(orch, Orchestrator)
    orch._update_reminder_status = MagicMock()
    orch._record_eval_human_feedback = MagicMock()

    return orch


class TestApprovalDoesNotBlock:
    """check_approval_status should transition to AGENT_RUNNING, not call _run_agent."""

    def test_approval_transitions_to_agent_running(self):
        """When reminder is completed, task should transition to AGENT_RUNNING."""
        orch = _make_orchestrator()
        task = _make_task(state=TaskState.AWAITING_APPROVAL)

        # Simulate the reminder being completed
        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[REVIEW PRD] Task: Test Task", "isCompleted": True}
        ]

        result = orch.check_approval_status(task)

        orch._run_agent.assert_not_called()
        assert result.state == TaskState.AGENT_RUNNING
        orch.db.transition_if.assert_called()
        transition_call_args = orch.db.transition_if.call_args
        assert "queued for implementation" in transition_call_args[0][3].lower()

    def test_approval_sets_status_message(self):
        """The status_message should contain 'queued for implementation'."""
        orch = _make_orchestrator()
        task = _make_task(state=TaskState.AWAITING_APPROVAL)
        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[REVIEW PRD] Task: Test Task", "isCompleted": True}
        ]

        result = orch.check_approval_status(task)

        assert result.status_message is not None
        assert "queued for implementation" in result.status_message

    def test_rejection_still_works(self):
        """Deleted reminder should transition to REJECTED."""
        orch = _make_orchestrator()
        task = _make_task(state=TaskState.AWAITING_APPROVAL)
        # Reminder not found = deleted
        orch.reminders.show_list.return_value = []

        result = orch.check_approval_status(task)

        orch._run_agent.assert_not_called()
        assert result.state == TaskState.REJECTED

    def test_pending_approval_no_change(self):
        """If approval is still pending (not completed), no transition should happen."""
        orch = _make_orchestrator()
        task = _make_task(
            state=TaskState.AWAITING_APPROVAL,
            approval_reminder_id="approval-ext-1",
            approval_reminder_title="Approve PRD: Test Task",
        )
        # Reminder exists but not completed, no [FIX] prefix
        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[REVIEW PRD] Task: Test Task", "isCompleted": False}
        ]

        result = orch.check_approval_status(task)

        orch._run_agent.assert_not_called()
        assert result.state == TaskState.AWAITING_APPROVAL


class TestQuestionAnsweredDoesNotBlock:
    """check_question_answered should transition to AGENT_RUNNING, not call _run_agent."""

    def test_question_answered_transitions_to_agent_running(self):
        """When user changes title to [ANSWER], task should transition to AGENT_RUNNING."""
        orch = _make_orchestrator()
        task = _make_task(
            state=TaskState.NEEDS_CLARIFICATION,
            question_text="What framework should I use?",
            task_notes="Original notes",
        )

        # Simulate user changing prefix to [ANSWER] with answer in notes
        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[ANSWER] Task: Test Task",
             "isCompleted": False, "notes": "Use React"}
        ]

        result = orch.check_question_answered(task)

        orch._run_agent.assert_not_called()
        assert result.state == TaskState.AGENT_RUNNING
        orch.db.transition_if.assert_called()
        assert result.status_message is not None
        assert "queued for implementation" in result.status_message

    def test_question_unanswered_no_change(self):
        """If title still has [QUESTION] prefix, task stays in NEEDS_CLARIFICATION."""
        orch = _make_orchestrator()
        task = _make_task(
            state=TaskState.NEEDS_CLARIFICATION,
            question_text="What framework should I use?",
        )

        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[QUESTION] Task: Test Task",
             "isCompleted": False, "notes": "What framework should I use?"}
        ]

        result = orch.check_question_answered(task)

        orch._run_agent.assert_not_called()
        assert result.state == TaskState.NEEDS_CLARIFICATION

    def test_answer_context_added_to_notes(self):
        """The answer should be appended to task_notes."""
        orch = _make_orchestrator()
        task = _make_task(
            state=TaskState.NEEDS_CLARIFICATION,
            question_text="What framework should I use?",
            task_notes="Original notes",
        )

        orch.reminders.show_list.return_value = [
            {"externalId": "reminder-1", "title": "[ANSWER] Task: Test Task",
             "isCompleted": False, "notes": "Use React"}
        ]

        result = orch.check_question_answered(task)

        assert "Use React" in result.task_notes
        assert "Original notes" in result.task_notes


class TestIsActiveExcludesQueuedTasks:
    """AGENT_RUNNING tasks queued for implementation should not count as active."""

    def test_queued_task_not_active(self):
        """A task in AGENT_RUNNING with 'queued for implementation' status_message
        and no next_retry_at should not be considered active."""
        task = _make_task(
            state=TaskState.AGENT_RUNNING,
            status_message="PRD approved, queued for implementation",
        )
        task.next_retry_at = None

        # Replicate the is_active logic from poll_cycle
        active_states = [TaskState.IN_FLIGHT, TaskState.AGENT_RUNNING, TaskState.MERGING]

        def is_active(t: Task) -> bool:
            if t.state not in active_states:
                return False
            if t.state == TaskState.AGENT_RUNNING and t.next_retry_at:
                return False
            if (t.state == TaskState.AGENT_RUNNING
                    and t.status_message
                    and "queued for implementation" in t.status_message):
                return False
            return True

        assert not is_active(task)

    def test_running_task_still_active(self):
        """A normally running AGENT_RUNNING task (no queued message) should still be active."""
        task = _make_task(
            state=TaskState.AGENT_RUNNING,
            status_message=None,
        )
        task.next_retry_at = None

        active_states = [TaskState.IN_FLIGHT, TaskState.AGENT_RUNNING, TaskState.MERGING]

        def is_active(t: Task) -> bool:
            if t.state not in active_states:
                return False
            if t.state == TaskState.AGENT_RUNNING and t.next_retry_at:
                return False
            if (t.state == TaskState.AGENT_RUNNING
                    and t.status_message
                    and "queued for implementation" in t.status_message):
                return False
            return True

        assert is_active(task)
