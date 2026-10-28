"""
Porter — Models, enums, constants, and configuration.

Extracted from the monolithic porter.py with the new v4 TaskState enum.
"""

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import toml
except ImportError:
    print(
        "ERROR: 'toml' package is not installed. "
        "Install it with: pip install toml",
        file=sys.stderr,
    )
    sys.exit(1)


# =============================================================================
# CONSTANTS AND ENUMS
# =============================================================================

class TaskState(str, Enum):
    """Task state machine states (v4 — simplified 14-state version)."""
    NEW = "NEW"
    ROUTING = "ROUTING"
    IN_FLIGHT = "IN_FLIGHT"
    PRD_EVALUATING = "PRD_EVALUATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AGENT_RUNNING = "AGENT_RUNNING"
    PR_CREATED = "PR_CREATED"
    PR_REVIEW_PENDING = "PR_REVIEW_PENDING"
    AWAITING_MERGE = "AWAITING_MERGE"
    MERGING = "MERGING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class TaskError(str, Enum):
    """Structured error types for better diagnostics."""
    CLONE_FAILED = "CLONE_FAILED"
    BRANCH_FAILED = "BRANCH_FAILED"
    PRD_GENERATION_FAILED = "PRD_GENERATION_FAILED"
    PRD_SAVE_FAILED = "PRD_SAVE_FAILED"
    APPROVAL_REMINDER_FAILED = "APPROVAL_REMINDER_FAILED"
    IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
    TEST_WRITING_FAILED = "TEST_WRITING_FAILED"
    TEST_FAILED = "TEST_FAILED"
    TEST_FIX_FAILED = "TEST_FIX_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PUSH_FAILED = "PUSH_FAILED"
    PR_CREATION_FAILED = "PR_CREATION_FAILED"
    PR_REVIEW_FAILED = "PR_REVIEW_FAILED"
    MERGE_CONFLICT_FAILED = "MERGE_CONFLICT_FAILED"
    MERGE_TEST_FAILED = "MERGE_TEST_FAILED"
    MERGE_FAILED = "MERGE_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class TaskPriority(int, Enum):
    """Task priority levels (lower = higher priority)."""
    P0 = 0  # Critical
    P1 = 1  # High
    P2 = 2  # Medium
    P3 = 3  # Low
    P4 = 4  # Lowest
    DEFAULT = 5  # Default


class TaskSource(str, Enum):
    """Where the task originated from."""
    REMINDERS = "REMINDERS"
    GITHUB_ISSUES = "GITHUB_ISSUES"


# Valid state transitions — checked by StateDB.transition_if().
# Unknown transitions are WARNED (not crashed) so the system stays up.
# Admin CLI commands (--reset-task, --cancel-task) bypass this via save_task().
#
# This map was audited against every self.transition() call in orchestrator.py
# and every dispatch path in porter.py.
VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.NEW: {
        TaskState.ROUTING,          # normal: classify task type
        TaskState.ERROR,            # daily budget exhausted
    },
    TaskState.ROUTING: {
        TaskState.IN_FLIGHT,        # normal: begin workspace setup
        TaskState.NEW,              # retry scheduled via _handle_failure
        TaskState.ERROR,
    },
    TaskState.IN_FLIGHT: {
        TaskState.PRD_EVALUATING,       # PRD generated, needs quality gate
        TaskState.AGENT_RUNNING,        # quickfix/analysis/composite: skip PRD
        TaskState.AWAITING_APPROVAL,    # PRD ready, needs human sign-off
        TaskState.COMPLETE,             # analysis/composite tasks finish here
        TaskState.NEEDS_CLARIFICATION,  # agent asked a question
        TaskState.NEW,                  # retry after failure during setup/PRD gen
        TaskState.ERROR,
    },
    TaskState.PRD_EVALUATING: {
        TaskState.AWAITING_APPROVAL,    # eval passed, await human review
        TaskState.AGENT_RUNNING,        # eval passed + no approval needed
        TaskState.ERROR,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.AGENT_RUNNING,        # human approved (reminder completed)
        TaskState.REJECTED,             # human rejected (reminder deleted / timeout)
        TaskState.ERROR,
    },
    TaskState.AGENT_RUNNING: {
        TaskState.AGENT_RUNNING,        # re-enter: approved task starting SDK session
        TaskState.ROUTING,              # retry: process_new_task re-classifies
        TaskState.IN_FLIGHT,            # retry: skip routing on re-process
        TaskState.PR_CREATED,           # implementation done, PR opened
        TaskState.NEEDS_CLARIFICATION,  # agent asked a question
        TaskState.COMPLETE,             # analysis/composite done, or cancelled
        TaskState.NEW,                  # retry scheduled via _handle_failure
        TaskState.ERROR,
    },
    TaskState.PR_CREATED: {
        TaskState.PR_REVIEW_PENDING,    # auto_respond_to_reviews enabled
        TaskState.AWAITING_MERGE,       # merge reminder created
        TaskState.COMPLETE,             # merge reminder creation failed, complete anyway
        TaskState.ERROR,
    },
    TaskState.PR_REVIEW_PENDING: {
        TaskState.PR_REVIEW_PENDING,    # re-enter after addressing review comments
        TaskState.AWAITING_MERGE,       # no more comments → merge reminder
        TaskState.MERGING,              # auto_merge_on_review_complete
        TaskState.ERROR,
    },
    TaskState.AWAITING_MERGE: {
        TaskState.MERGING,              # user completed merge reminder
        TaskState.COMPLETE,             # user deleted merge reminder
        TaskState.ERROR,
    },
    TaskState.MERGING: {
        TaskState.COMPLETE,             # merge succeeded
        TaskState.NEW,                  # retry scheduled via _handle_failure
        TaskState.ERROR,                # rebase/test/merge failure (exhausted retries)
    },
    TaskState.NEEDS_CLARIFICATION: {
        TaskState.AGENT_RUNNING,        # question answered
        TaskState.ERROR,
    },
    # Terminal states — recovery requires admin CLI (--reset-task).
    TaskState.COMPLETE: set(),
    TaskState.REJECTED: set(),
    TaskState.ERROR: set(),
}


# Reminder title prefix — tasks start with "Task: "
TASK_PREFIX = "Task: "

# Status prefixes — prepended to the reminder title by the daemon to show
# progress.  The user sees these in the Reminders list at a glance.
STATUS_QUEUED = "[QUEUED] "
STATUS_WORKING = "[WORKING] "
STATUS_REVIEW_PRD = "[REVIEW PRD] "
STATUS_QUESTION = "[QUESTION] "
STATUS_PR = "[PR #{pr_number}] "          # formatted with .format()
STATUS_MERGE = "[MERGE PR #{pr_number}] "  # formatted with .format()
STATUS_RETRY = "[RETRY {n}/{max}] "        # formatted with .format()
STATUS_FAILED = "[FAILED] "

# User-set prefixes — user changes the title prefix to signal a response
USER_ANSWER = "[ANSWER] "
USER_FIX = "[FIX] "

# Legacy prefixes — kept for backward compat detection in cleanup
APPROVAL_PREFIX = "Complete to approve: "
QUESTION_PREFIX = "Question: "
FIX_PREFIX = "FIX: "
PR_READY_PREFIX = "PR Ready: "
PR_UPDATE_PREFIX = "PR Updated: "
MERGE_PREFIX = "Complete to merge: "
PRIORITY_PATTERN = re.compile(r'\[P(\d)\]', re.IGNORECASE)

# Claude model short names — passed directly to the Agent SDK / CLI which
# resolves them to the latest version automatically.  Do NOT hardcode dated
# model IDs here; they become stale as new versions are released.
CLAUDE_MODEL_SHORT_NAMES = ("opus", "sonnet", "haiku")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Config:
    """Application configuration."""
    # Daemon settings
    poll_interval_seconds: int = 300
    max_concurrent_tasks: int = 1
    max_concurrent_per_project: int = 1
    retry_max: int = 3
    retry_backoff_base: int = 5  # Base minutes for exponential backoff
    approval_timeout_days: int = 7

    # Paths
    workspaces_path: Path = field(default_factory=lambda: Path.home() / ".claude/agents/porter/workspaces")
    logs_path: Path = field(default_factory=lambda: Path.home() / ".claude/agents/porter/logs")
    db_path: Path = field(default_factory=lambda: Path.home() / ".claude/agents/porter/state.db")

    # PRD repo
    prd_repo_url: str = ""
    prd_repo_local: Path = field(default_factory=lambda: Path.home() / ".claude/agents/porter/workspaces/prds")

    # Tool paths
    reminders_bin: str = "/opt/homebrew/bin/reminders"
    claude_bin: str = "/opt/homebrew/bin/claude"
    gh_bin: str = "/opt/homebrew/bin/gh"
    imsg_bin: str = ""

    # Notifications
    phone_number: str = ""
    notify_on_prd_ready: bool = True
    notify_on_pr_created: bool = True
    notify_on_error: bool = True
    use_emoji: bool = True
    image_notifications: bool = True
    image_model: str = "gemini-2.5-flash-image"

    # Claude settings
    claude_model: str = "opus"
    claude_timeout: int = 1800
    claude_permission_mode: str = "bypassPermissions"
    claude_max_turns: int = 50
    claude_stream_output: bool = True

    # Projects - can be string (simple) or dict (with options)
    projects: Dict[str, Union[str, Dict[str, Any]]] = field(default_factory=dict)

    # Reminders settings
    notes_fetch_mode: str = "applescript_fallback"
    applescript_timeout_seconds: int = 5

    # GitHub Issues integration
    github_issues_enabled: bool = False
    github_issues_label: str = "porter"
    # Logins whose issues, issue comments and PR reviews may reach the agent.
    # Empty means the repo's owner only.
    github_trusted_authors: List[str] = field(default_factory=list)

    # Testing settings
    run_tests_before_pr: bool = True
    test_timeout_seconds: int = 600  # 10 min
    test_flaky_retries: int = 1  # Extra attempts on test failure (0 = no retry)

    # Reliability settings
    write_tests_for_implementation: bool = True
    max_test_fix_iterations: int = 5
    verify_acceptance_criteria: bool = True

    # Merge settings
    merge_strategy: str = "squash"
    merge_max_fix_iterations: int = 3
    auto_merge_on_review_complete: bool = False

    # Git settings
    clone_depth: Optional[int] = 1  # Shallow clone depth (None = full clone)

    # Enhanced implementation settings
    claude_impl_max_turns: int = 150
    claude_impl_timeout: int = 3600
    max_cost_per_task_usd: Optional[float] = None  # None = unlimited
    daily_budget_usd: Optional[float] = None  # Aggregate daily spending cap (None = unlimited)

    # PR review settings
    auto_respond_to_reviews: bool = False
    pr_review_check_interval: int = 3600  # 1 hour

    # Logging settings
    log_level: str = "INFO"
    compress_old_logs: bool = True
    compress_after_days: int = 7

    # Web dashboard settings
    web_port: int = 8742
    web_host: str = "127.0.0.1"

    # Usage tracking
    track_usage: bool = True

    def get_project_url(self, project_name: str) -> Optional[str]:
        """Get the repo URL for a project (handles both simple and complex configs)."""
        project = self.projects.get(project_name)
        if isinstance(project, str):
            return project
        elif isinstance(project, dict):
            return project.get("url")
        return None

    def get_project_prd_template(self, project_name: str) -> Optional[str]:
        """Get custom PRD template path for a project."""
        project = self.projects.get(project_name)
        if isinstance(project, dict):
            return project.get("prd_template")
        return None

    def get_project_test_command(self, project_name: str) -> Optional[str]:
        """Get custom test command for a project."""
        project = self.projects.get(project_name)
        if isinstance(project, dict):
            return project.get("test_command")
        return None

    def validate(self) -> List[str]:
        """Validate configuration and return list of errors (empty if valid)."""
        errors = []

        # Required fields
        if not self.prd_repo_url:
            errors.append("[prd_repo] url is required")

        if not self.projects:
            errors.append("[projects] At least one project mapping is required")

        # Validate project URLs
        for name, value in self.projects.items():
            url = value if isinstance(value, str) else value.get("url", "")
            if not url:
                errors.append(f"[projects] '{name}' has no URL")
            elif not url.startswith(("https://github.com/", "git@github.com:")):
                errors.append(f"[projects] '{name}' has invalid repo URL: {url}")

        # Validate tool paths
        if not Path(self.reminders_bin).exists():
            errors.append(f"[tools] reminders_bin not found: {self.reminders_bin}")
        if not Path(self.claude_bin).exists():
            errors.append(f"[tools] claude_bin not found: {self.claude_bin}")
        if not Path(self.gh_bin).exists():
            errors.append(f"[tools] gh_bin not found: {self.gh_bin}")
        if self.imsg_bin and not Path(self.imsg_bin).exists():
            errors.append(f"[tools] imsg_bin not found: {self.imsg_bin}")

        # Validate numeric ranges
        if self.poll_interval_seconds < 60:
            errors.append("[daemon] poll_interval_seconds should be >= 60")
        if self.retry_max < 1:
            errors.append("[daemon] retry_max should be >= 1")
        if self.claude_timeout < 60:
            errors.append("[claude] timeout_seconds should be >= 60")

        # Validate log level
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if self.log_level.upper() not in valid_levels:
            errors.append(f"[logging] level must be one of: {valid_levels}")
        if self.compress_after_days < 0:
            errors.append("[logging] compress_after_days must be >= 0")

        return errors

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        """Load configuration from TOML file."""
        data = toml.load(path)

        config = cls()

        # Daemon settings
        daemon = data.get("daemon", {})
        config.poll_interval_seconds = daemon.get("poll_interval_seconds", 300)
        config.max_concurrent_tasks = daemon.get("max_concurrent_tasks", 1)
        config.max_concurrent_per_project = daemon.get("max_concurrent_per_project", 1)
        config.retry_max = daemon.get("retry_max", 3)
        config.retry_backoff_base = daemon.get("retry_backoff_base", 5)
        if "retry_backoff_base_minutes" in daemon:
            config.retry_backoff_base = daemon.get("retry_backoff_base_minutes", config.retry_backoff_base)
        config.approval_timeout_days = daemon.get("approval_timeout_days", 7)
        if "enable_parallel_projects" in daemon:
            enable_parallel = daemon.get("enable_parallel_projects", False)
            if enable_parallel:
                config.max_concurrent_per_project = daemon.get("max_concurrent_per_project", config.max_concurrent_tasks)
            else:
                config.max_concurrent_per_project = 1

        # Logging (support both legacy daemon fields and new [logging] section)
        config.log_level = daemon.get("log_level", "INFO")
        config.compress_old_logs = daemon.get("compress_old_logs", True)

        # Paths
        paths = data.get("paths", {})
        if "workspaces" in paths:
            config.workspaces_path = Path(paths["workspaces"]).expanduser()
        if "logs" in paths:
            config.logs_path = Path(paths["logs"]).expanduser()
        if "db" in paths:
            config.db_path = Path(paths["db"]).expanduser()

        # Logging (preferred section)
        logging_cfg = data.get("logging", {})
        if "level" in logging_cfg:
            config.log_level = logging_cfg.get("level", config.log_level)
        if "compress_after_days" in logging_cfg:
            config.compress_after_days = logging_cfg.get("compress_after_days", config.compress_after_days)
            if config.compress_after_days <= 0:
                config.compress_old_logs = False

        # PRD repo
        prd = data.get("prd_repo", {})
        config.prd_repo_url = prd.get("url", "")
        if "local_path" in prd:
            config.prd_repo_local = Path(prd["local_path"]).expanduser()

        # Tools
        tools = data.get("tools", {})
        config.reminders_bin = tools.get("reminders_bin", "/opt/homebrew/bin/reminders")
        config.claude_bin = tools.get("claude_bin", "/opt/homebrew/bin/claude")
        config.gh_bin = tools.get("gh_bin", "/opt/homebrew/bin/gh")
        config.imsg_bin = tools.get("imsg_bin", "")

        # Notifications
        notif = data.get("notification", {})
        config.phone_number = notif.get("phone_number", "")
        config.notify_on_prd_ready = notif.get("notify_on_prd_ready", True)
        config.notify_on_pr_created = notif.get("notify_on_pr_created", True)
        config.notify_on_error = notif.get("notify_on_error", True)
        config.use_emoji = notif.get("use_emoji", notif.get("use_emojis", True))
        config.image_notifications = notif.get("image_notifications", True)
        config.image_model = notif.get("image_model", "gemini-2.5-flash-image")

        # Claude settings
        claude = data.get("claude", {})
        config.claude_model = claude.get("model", "opus")
        config.claude_timeout = claude.get("timeout_seconds", 1800)
        config.claude_permission_mode = claude.get("permission_mode", "bypassPermissions")
        config.claude_max_turns = claude.get("max_turns", config.claude_max_turns)
        config.claude_stream_output = claude.get("stream_output", config.claude_stream_output)
        config.claude_impl_max_turns = claude.get("impl_max_turns", config.claude_impl_max_turns)
        config.claude_impl_timeout = claude.get("impl_timeout_seconds", config.claude_impl_timeout)
        config.max_cost_per_task_usd = claude.get("max_cost_per_task_usd", config.max_cost_per_task_usd)
        config.daily_budget_usd = claude.get("daily_budget_usd", config.daily_budget_usd)

        # Projects (handles both simple strings and dicts)
        config.projects = data.get("projects", {})

        # Git settings
        git_cfg = data.get("git", {})
        clone_depth = git_cfg.get("clone_depth", 1)
        config.clone_depth = clone_depth if clone_depth and clone_depth > 0 else None

        # Reminders / notes settings
        reminders_cfg = data.get("reminders", {})
        config.notes_fetch_mode = reminders_cfg.get("notes_fetch_mode", "applescript_fallback")
        config.applescript_timeout_seconds = reminders_cfg.get("applescript_timeout_seconds", 5)

        # GitHub Issues integration
        github_cfg = data.get("github_issues", {})
        config.github_issues_enabled = github_cfg.get("enabled", False)
        config.github_issues_label = github_cfg.get("label", "porter")
        config.github_trusted_authors = list(github_cfg.get("trusted_authors", []))

        # Testing settings
        testing_cfg = data.get("testing", {})
        config.run_tests_before_pr = testing_cfg.get("run_tests_before_pr", True)
        config.test_timeout_seconds = testing_cfg.get("timeout_seconds", testing_cfg.get("test_timeout_seconds", 600))
        config.test_flaky_retries = testing_cfg.get("flaky_retries", 1)

        # Reliability settings
        reliability_cfg = data.get("reliability", {})
        config.write_tests_for_implementation = reliability_cfg.get("write_tests_for_implementation", True)
        config.max_test_fix_iterations = reliability_cfg.get("max_test_fix_iterations", 5)
        config.verify_acceptance_criteria = reliability_cfg.get("verify_acceptance_criteria", True)

        # Merge settings
        merge_cfg = data.get("merge", {})
        config.merge_strategy = merge_cfg.get("strategy", config.merge_strategy)
        config.merge_max_fix_iterations = merge_cfg.get("max_fix_iterations", config.merge_max_fix_iterations)
        config.auto_merge_on_review_complete = merge_cfg.get("auto_merge_on_review_complete", config.auto_merge_on_review_complete)

        # PR review settings
        pr_review_cfg = data.get("pr_review", {})
        config.auto_respond_to_reviews = pr_review_cfg.get("auto_respond", False)
        config.pr_review_check_interval = pr_review_cfg.get("check_interval_seconds", 3600)

        # Web dashboard
        web_cfg = data.get("web", {})
        config.web_port = web_cfg.get("port", 8742)
        config.web_host = web_cfg.get("host", "127.0.0.1")

        # Usage tracking
        config.track_usage = data.get("usage", {}).get("track", True)

        # Allow env var override for sensitive values
        config.phone_number = os.environ.get("PORTER_PHONE", config.phone_number)

        return config


@dataclass
class Task:
    """Represents a task being processed."""
    id: str
    reminder_id: str
    reminder_list: str
    task_title: str
    task_notes: str
    repo_url: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    local_repo_path: Optional[str] = None
    branch_name: Optional[str] = None
    prd_path: Optional[str] = None
    prd_url: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    pr_reminder_title: Optional[str] = None
    pr_reminder_id: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[TaskError] = None
    retry_count: int = 0
    next_retry_at: Optional[datetime] = None
    approval_reminder_title: Optional[str] = None
    approval_reminder_id: Optional[str] = None
    question_reminder_title: Optional[str] = None
    question_reminder_id: Optional[str] = None
    question_text: Optional[str] = None
    priority: TaskPriority = TaskPriority.DEFAULT
    source: TaskSource = TaskSource.REMINDERS
    github_issue_number: Optional[int] = None
    last_fix_comment_id: Optional[int] = None
    last_review_comment_id: Optional[int] = None
    implementation_started_at: Optional[datetime] = None
    implementation_finished_at: Optional[datetime] = None
    # Test-fix loop tracking
    test_fix_iterations: int = 0
    last_test_output: Optional[str] = None
    tests_written: bool = False
    # Verification tracking
    verification_result: Optional[str] = None
    # Merge flow tracking
    merge_reminder_title: Optional[str] = None
    merge_reminder_id: Optional[str] = None
    merge_fix_iterations: int = 0
    merge_strategy: str = "squash"
    # Enhanced implementation tracking
    last_implementation_output: Optional[str] = None
    implementation_session_id: Optional[str] = None
    # v4 new fields
    task_type: str = "feature"  # "feature", "bugfix", "quickfix", "refactor"
    prd_eval_result: Optional[str] = None
    prd_eval_passed: bool = False
    agent_session_id: Optional[str] = None  # Agent SDK session ID
    # Token usage tracking
    input_tokens: int = 0
    output_tokens: int = 0
    # Status message for tracking transition context (e.g. "queued for implementation")
    status_message: Optional[str] = None
    # F8: explicit flag for tasks queued by approval/question checks — replaces
    # fragile string matching on status_message.
    queued_for_dispatch: bool = False


@dataclass
class ClaudeUsage:
    """Tracks Claude API usage for cost visibility."""
    id: str
    task_id: str
    phase: str  # "prd_generation", "implementation", "regeneration", "pr_review"
    model: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    turns_used: int = 0
    output_file: Optional[str] = None  # Path to captured output
