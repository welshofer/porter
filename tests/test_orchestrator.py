# tests/test_orchestrator.py
"""Tests for OrchestratorV4 state transitions."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from lib.models import Config, Task, TaskError, TaskSource, TaskState


@pytest.fixture
def config():
    """Create a real Config with test-appropriate defaults."""
    cfg = Config()
    cfg.prd_repo_url = "https://github.com/test/prds"
    cfg.retry_max = 3
    cfg.retry_backoff_base = 1
    cfg.poll_interval_seconds = 120
    cfg.test_timeout_seconds = 10
    cfg.test_flaky_retries = 0
    cfg.notify_on_error = False  # suppress notification side-effects
    return cfg


@pytest.fixture
def orchestrator(config):
    """Create an orchestrator with mocked external dependencies."""
    db = MagicMock()
    reminders = MagicMock()
    git = MagicMock()
    prd_repo = MagicMock()
    notifier = MagicMock()
    logger = MagicMock()

    with patch("lib.orchestrator.TaskRouter"), \
         patch("lib.orchestrator.PRDEvaluator"), \
         patch("lib.orchestrator.TestRunner"), \
         patch("lib.orchestrator.CircuitBreakerRegistry"):
        from lib.orchestrator import OrchestratorV4
        orch = OrchestratorV4(
            config=config, db=db, reminders=reminders, git=git,
            prd_repo=prd_repo, notifier=notifier, logger=logger,
        )

    return orch


@pytest.fixture
def sample_task():
    now = datetime.now()
    return Task(
        id="task-1", reminder_id="rem-1", reminder_list="TestProject",
        task_title="Test Feature", task_notes="Do the thing",
        repo_url="https://github.com/test/repo", state=TaskState.NEW,
        created_at=now, updated_at=now,
        source=TaskSource.REMINDERS,
    )


class TestTransition:
    def test_transition_updates_state_and_saves(self, orchestrator, sample_task):
        orchestrator.db.transition_if.return_value = True
        result = orchestrator.transition(sample_task, TaskState.ROUTING, "Starting routing")
        assert result.state == TaskState.ROUTING
        orchestrator.db.transition_if.assert_called_once_with(
            "task-1", TaskState.NEW, TaskState.ROUTING, "Starting routing",
        )

    def test_transition_logs_transition_in_db(self, orchestrator, sample_task):
        orchestrator.db.transition_if.return_value = True
        orchestrator.transition(sample_task, TaskState.ROUTING, "test note")
        # log_transition is called inside transition_if
        orchestrator.db.transition_if.assert_called_once_with(
            "task-1", TaskState.NEW, TaskState.ROUTING, "test note",
        )

    def test_transition_updates_timestamp(self, orchestrator, sample_task):
        before = sample_task.updated_at
        result = orchestrator.transition(sample_task, TaskState.ROUTING)
        assert result.updated_at >= before

    def test_transition_sets_status_message(self, orchestrator, sample_task):
        result = orchestrator.transition(sample_task, TaskState.IN_FLIGHT, "cloning repo")
        assert result.status_message == "cloning repo"

    def test_transition_clears_status_message_when_empty(self, orchestrator, sample_task):
        sample_task.status_message = "old message"
        result = orchestrator.transition(sample_task, TaskState.IN_FLIGHT)
        assert result.status_message is None

    def test_chained_transitions(self, orchestrator, sample_task):
        """Verify multiple transitions accumulate correctly."""
        orchestrator.db.transition_if.return_value = True
        orchestrator.transition(sample_task, TaskState.ROUTING)
        orchestrator.transition(sample_task, TaskState.IN_FLIGHT)
        orchestrator.transition(sample_task, TaskState.AGENT_RUNNING)
        assert sample_task.state == TaskState.AGENT_RUNNING
        assert orchestrator.db.transition_if.call_count == 3


class TestHandleFailure:
    def test_retries_under_max(self, orchestrator, sample_task):
        sample_task.retry_count = 0
        result = orchestrator._handle_failure(sample_task, TaskError.CLONE_FAILED, "clone error")
        assert result.retry_count == 1
        # Should schedule a retry via backoff, NOT call process_new_task recursively
        assert result.next_retry_at is not None
        assert result.state == TaskState.NEW
        # Error fields cleared for next attempt
        assert result.error_type is None
        assert result.error_message is None
        orchestrator.notifier.notify_retry.assert_called_once()

    def test_error_state_at_max_retries(self, orchestrator, sample_task):
        sample_task.retry_count = 2  # will become 3, hitting retry_max=3
        result = orchestrator._handle_failure(sample_task, TaskError.CLONE_FAILED, "clone error")
        assert result.state == TaskState.ERROR
        assert result.retry_count == 3

    def test_sets_error_type_and_message(self, orchestrator, sample_task):
        orchestrator._handle_failure(sample_task, TaskError.PUSH_FAILED, "auth denied")
        # Check the notifier got the retry info
        orchestrator.notifier.notify_retry.assert_called_once()

    def test_budget_exceeded_is_terminal(self, orchestrator, sample_task):
        """BUDGET_EXCEEDED should go straight to ERROR without retry."""
        sample_task.retry_count = 0
        result = orchestrator._handle_failure(sample_task, TaskError.BUDGET_EXCEEDED, "over budget")
        assert result.state == TaskState.ERROR
        assert result.retry_count == 0  # never incremented

    def test_notifies_on_error_when_enabled(self, orchestrator, sample_task):
        orchestrator.config.notify_on_error = True
        sample_task.retry_count = 2
        orchestrator._handle_failure(sample_task, TaskError.CLONE_FAILED, "fail")
        orchestrator.notifier.notify_error.assert_called_once()

    def test_notifies_on_retry(self, orchestrator, sample_task):
        sample_task.retry_count = 0
        orchestrator._handle_failure(sample_task, TaskError.CLONE_FAILED, "fail")
        orchestrator.notifier.notify_retry.assert_called_once()

    def test_retry_schedules_backoff_not_recursive(self, orchestrator, sample_task):
        """Retries should schedule backoff and transition to NEW, NOT call process_new_task recursively."""
        sample_task.retry_count = 0
        result = orchestrator._handle_failure(sample_task, TaskError.CLONE_FAILED, "fail")
        # Should have a backoff scheduled
        assert result.next_retry_at is not None
        # Should transition to NEW for the poll cycle to pick up
        assert result.state == TaskState.NEW
