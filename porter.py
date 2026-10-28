#!/usr/bin/env python3
"""Porter — PRD-to-PR system using agent patterns.

Daemon entry point that mirrors v3's porter.py but uses the v4 orchestrator
with router, evaluator, and agent runner components.
"""

__version__ = "4.0.0"

import argparse
import atexit
import hashlib
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from lib.models import (
    PRIORITY_PATTERN,
    STATUS_QUEUED,
    TASK_PREFIX,
    USER_ANSWER,
    USER_FIX,
    Config,
    Task,
    TaskError,
    TaskPriority,
    TaskSource,
    TaskState,
)
from lib.healthcheck import StartupHealthCheck, HealthCheckError
from lib.db import StateDB
from lib.reminders import RemindersCache, RemindersClient, RemindersError
from lib.git_ops import GitOperations
from lib.image_gen import ImageGenerator
from lib.notifier import Notifier
from lib.prd_repo import PRDRepoManager
from lib.github_issues import GitHubIssuesClient
from lib.orchestrator import OrchestratorV4
from lib.agent_runner import AgentResult
from lib.logging_config import JsonFormatter, set_task_context, clear_task_context


# =============================================================================
# Constants
# =============================================================================

MIN_DEDUP_PREFIX_LEN = 5  # Minimum prefix length for title-based dedup matching

# Global kill registry — maps task_id → threading.Event.
# When the event is set, the running agent's cancellation_check fires and stops.
# Shared between the daemon loop and the --kill-task CLI command (via DB flag).
import threading as _threading
_kill_signals: Dict[str, _threading.Event] = {}

# =============================================================================
# PID-validated lock file
# =============================================================================

class LockFile:
    """PID-validated file lock to ensure single instance execution.

    Unlike fcntl.flock(), this implementation detects and cleans up stale locks
    left behind by crashed processes by checking whether the PID in the lock
    file is still alive.
    """

    def __init__(self, path: Path, logger: logging.Logger = None):
        self.lock_path = path
        self.logger = logger or logging.getLogger("porter-v4")
        self._held = False

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check whether a process with the given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission to signal it.
            return True
        except (OSError, ValueError):
            return False

    def acquire(self) -> bool:
        """Try to acquire the lock.

        If a lock file already exists, read the PID and check if it is alive.
        If the owning process is alive, return False.
        If the owning process is dead (stale lock), remove the stale file and
        create a new lock with our PID.

        Returns True on success, False if the lock is held by a live process.
        """
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)

            if self.lock_path.exists():
                try:
                    existing_pid = int(self.lock_path.read_text().strip())
                except (ValueError, OSError):
                    existing_pid = None

                if existing_pid is not None and self._is_pid_alive(existing_pid):
                    self.logger.warning(
                        "Lock held by live process %d: %s", existing_pid, self.lock_path
                    )
                    return False

                # Stale lock — remove it
                self.logger.info(
                    "Removing stale lock (PID %s): %s",
                    existing_pid,
                    self.lock_path,
                )
                self.lock_path.unlink(missing_ok=True)

            self.lock_path.write_text(str(os.getpid()))
            self._held = True
            self.logger.debug("Lock acquired: %s", self.lock_path)
            return True
        except OSError as e:
            self.logger.warning("Could not acquire lock: %s", e)
            return False

    def release(self) -> None:
        """Release the lock by removing the file, but only if we hold it.

        Safe to call multiple times.
        """
        if not self._held:
            return
        try:
            # Only remove if the file still contains our PID
            if self.lock_path.exists():
                try:
                    file_pid = int(self.lock_path.read_text().strip())
                except (ValueError, OSError):
                    file_pid = None
                if file_pid == os.getpid():
                    self.lock_path.unlink(missing_ok=True)
                    self.logger.debug("Lock released: %s", self.lock_path)
        except OSError as e:
            self.logger.warning("Error releasing lock: %s", e)
        finally:
            self._held = False


# =============================================================================
# Logging setup
# =============================================================================

def setup_logging(log_dir: Path, level: str = "INFO", verbose: bool = False) -> logging.Logger:
    """Set up logging with file rotation and console output."""
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("porter-v4")
    log_level = logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    # Clear existing handlers
    logger.handlers = []

    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_dir / "porter.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    ))
    logger.addHandler(console_handler)

    return logger


# =============================================================================
# Helper functions
# =============================================================================

def _extract_priority(title: str) -> TaskPriority:
    """Extract priority from task title like [P1] or [P2]."""
    match = PRIORITY_PATTERN.search(title)
    if match:
        level = int(match.group(1))
        try:
            return TaskPriority(level)
        except ValueError:
            pass
    return TaskPriority.DEFAULT


def _clean_title(title: str) -> str:
    """Remove priority tags from title."""
    return PRIORITY_PATTERN.sub("", title).strip()


def _stable_reminder_id(list_name: str, reminder: dict) -> str:
    """Generate a deterministic ID when externalId is missing."""
    parts = [
        list_name,
        reminder.get("title", ""),
        reminder.get("creationDate", "") or "",
    ]
    base = "|".join(parts)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# =============================================================================
# Status display
# =============================================================================

def show_status(config: Config) -> None:
    """Show current status of all tasks with summary dashboard."""
    db = StateDB(config.db_path)
    all_tasks = db.get_all_active_tasks()

    print(f"\n\033[1m{'=' * 50}")
    print(f" Porter v{__version__} Status Dashboard")
    print(f"{'=' * 50}\033[0m\n")

    # Summary counts
    completed = db.get_tasks_by_state(TaskState.COMPLETE)
    state_counts: Dict[str, int] = {}
    total_cost = 0.0
    for t in all_tasks + completed:
        label = t.state.value
        state_counts[label] = state_counts.get(label, 0) + 1
        if t.input_tokens or t.output_tokens:
            total_cost += AgentResult.estimate_cost(t.input_tokens or 0, t.output_tokens or 0)

    summary_parts = []
    for label in ["NEW", "AGENT_RUNNING", "PR_CREATED", "AWAITING_MERGE", "ERROR", "COMPLETE"]:
        count = state_counts.get(label, 0)
        if count > 0:
            summary_parts.append(f"{label}: {count}")
    print(f"  \033[1mSummary:\033[0m {' | '.join(summary_parts)}")
    if total_cost > 0:
        print(f"  \033[1mTotal est. cost:\033[0m ${total_cost:.2f}")
    print()

    if not all_tasks:
        print("  No active tasks.\n")
        return

    state_colors = {
        TaskState.NEW: "\033[0;34m",                 # Blue
        TaskState.ROUTING: "\033[0;36m",             # Cyan
        TaskState.IN_FLIGHT: "\033[0;33m",           # Yellow
        TaskState.PRD_EVALUATING: "\033[0;36m",      # Cyan
        TaskState.AWAITING_APPROVAL: "\033[0;35m",   # Magenta
        TaskState.AGENT_RUNNING: "\033[0;33m",       # Yellow
        TaskState.NEEDS_CLARIFICATION: "\033[0;35m", # Magenta
        TaskState.PR_CREATED: "\033[0;32m",          # Green
        TaskState.PR_REVIEW_PENDING: "\033[0;33m",   # Yellow
        TaskState.AWAITING_MERGE: "\033[0;35m",      # Magenta
        TaskState.MERGING: "\033[0;33m",             # Yellow
        TaskState.COMPLETE: "\033[0;32m",            # Green
        TaskState.ERROR: "\033[0;31m",               # Red
        TaskState.REJECTED: "\033[0;90m",            # Gray
    }

    priority_colors = {
        TaskPriority.P0: "\033[0;31m",  # Red
        TaskPriority.P1: "\033[0;31m",  # Red
        TaskPriority.P2: "\033[0;33m",  # Yellow
    }

    for task in all_tasks:
        color = state_colors.get(task.state, "\033[0m")
        print(f"  \033[1m{task.task_title}\033[0m")
        print(f"    State: {color}{task.state.value}\033[0m", end="")
        if task.priority != TaskPriority.DEFAULT:
            pc = priority_colors.get(task.priority, "")
            print(f"  {pc}[{task.priority.name}]\033[0m", end="")
        print()
        print(f"    Project: {task.reminder_list} ({task.source.value})")
        print(f"    Type: {task.task_type}")
        if task.retry_count > 0:
            print(f"    Retries: {task.retry_count}/{config.retry_max}")
        if task.next_retry_at and task.state == TaskState.NEW:
            print(f"    Next retry: {task.next_retry_at.strftime('%Y-%m-%d %H:%M:%S')}")
        if task.branch_name:
            print(f"    Branch: {task.branch_name}")
        if task.prd_url:
            print(f"    PRD: {task.prd_url}")
        if task.pr_url:
            print(f"    PR: {task.pr_url}")
        if task.input_tokens or task.output_tokens:
            cost = AgentResult.estimate_cost(task.input_tokens or 0, task.output_tokens or 0)
            print(f"    Tokens: {(task.input_tokens or 0) + (task.output_tokens or 0):,} (est. ${cost:.2f})")
        if task.error_type:
            print(f"    Error: \033[0;31m{task.error_type.value}\033[0m", end="")
            if task.error_message:
                msg = task.error_message[:80] + ("..." if len(task.error_message) > 80 else "")
                print(f" — {msg}", end="")
            print()
        age = datetime.now() - task.created_at if task.created_at else None
        if age:
            if age.days > 0:
                print(f"    Age: {age.days}d {age.seconds // 3600}h")
            else:
                print(f"    Age: {age.seconds // 3600}h {(age.seconds % 3600) // 60}m")
        print()


def list_tasks(config: Config) -> None:
    """List all tasks (including completed)."""
    db = StateDB(config.db_path)
    # Get all active tasks (includes everything non-complete)
    active = db.get_all_active_tasks()
    # Also get completed/rejected
    completed = db.get_tasks_by_state(TaskState.COMPLETE)
    rejected = db.get_tasks_by_state(TaskState.REJECTED)

    all_tasks = active + completed + rejected

    print(f"\n\033[1m=== All Tasks ({len(all_tasks)}) ===\033[0m\n")
    if not all_tasks:
        print("No tasks found.\n")
        return

    for task in all_tasks:
        state_sym = {
            TaskState.COMPLETE: "\033[0;32m[done]\033[0m",
            TaskState.ERROR: "\033[0;31m[err]\033[0m",
            TaskState.REJECTED: "\033[0;90m[rej]\033[0m",
        }.get(task.state, f"\033[0;33m[{task.state.value}]\033[0m")
        print(f"  {state_sym} {task.task_title} ({task.reminder_list})")
    print()


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def reset_task(config: Config, task_name: str) -> None:
    """Reset a task to NEW state for retry."""
    db = StateDB(config.db_path)

    # S7: escape LIKE wildcards in user-supplied task name
    escaped = _escape_like(task_name)
    with sqlite3.connect(config.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_title LIKE ? ESCAPE '\\'",
            (f"%{escaped}%",),
        ).fetchall()

    if not rows:
        print(f"\033[0;31m[ERROR]\033[0m No task found matching: {task_name}")
        return

    for row in rows:
        task = db._row_to_task(row)
        print(f"Resetting task: {task.task_title}")
        # F10: admin CLI commands use save_task() directly to bypass
        # VALID_TRANSITIONS enforcement — this is intentional, as manual
        # resets need to force arbitrary state changes for recovery.
        task.state = TaskState.NEW
        task.error_message = None
        task.error_type = None
        task.retry_count = 0
        task.next_retry_at = None
        task.queued_for_dispatch = False
        task.updated_at = datetime.now()
        db.save_task(task)
        print(f"\033[0;32m[OK]\033[0m Task reset to NEW state")


def cancel_task(config: Config, task_name: str) -> None:
    """Soft cancel — marks task as REJECTED but doesn't stop a running agent.
    Use --kill-task for a hard kill that stops the agent and cleans everything up."""
    db = StateDB(config.db_path)

    escaped = _escape_like(task_name)
    with sqlite3.connect(config.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_title LIKE ? ESCAPE '\\'",
            (f"%{escaped}%",),
        ).fetchall()

    if not rows:
        print(f"\033[0;31m[ERROR]\033[0m No task found matching: {task_name}")
        return

    for row in rows:
        task = db._row_to_task(row)
        if task.state in (TaskState.COMPLETE, TaskState.REJECTED):
            print(f"  \033[0;90m[SKIP]\033[0m '{task.task_title}' already {task.state.value}")
            continue
        print(f"  Cancelling: {task.task_title} (was {task.state.value})")
        task.state = TaskState.REJECTED
        task.error_message = "Cancelled by user"
        task.updated_at = datetime.now()
        db.save_task(task)
        print(f"  \033[0;32m[OK]\033[0m Task cancelled")


def kill_task(config: Config, task_name: str) -> None:
    """Hard kill — stops running agent, cleans DB, completes all reminders.

    1. Sets task to REJECTED in DB (agent's cancellation_check will see this)
    2. Completes all associated reminders (Task:, Approve:, Merge:, PR Ready:, Question:)
    3. Logs the kill
    """
    db = StateDB(config.db_path)
    reminders_client = RemindersClient(config.reminders_bin, config.applescript_timeout_seconds)

    escaped = _escape_like(task_name)
    with sqlite3.connect(config.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_title LIKE ? ESCAPE '\\' AND state NOT IN ('COMPLETE', 'REJECTED')",
            (f"%{escaped}%",),
        ).fetchall()

    if not rows:
        print(f"\033[0;31m[ERROR]\033[0m No active task found matching: {task_name}")
        return

    for row in rows:
        task = db._row_to_task(row)
        print(f"  \033[0;31m[KILLING]\033[0m {task.task_title}")
        print(f"    State: {task.state.value} | Project: {task.reminder_list}")

        # 1. Set REJECTED in DB — running agent will see this within 10s
        task.state = TaskState.REJECTED
        task.error_message = "Killed by user via --kill-task"
        task.updated_at = datetime.now()
        db.save_task(task)
        print(f"    DB: REJECTED")

        # 2. Complete all associated reminders
        prefixes_to_try = []
        # Original task reminder
        prefixes_to_try.append(f"Task: {task.task_title}")
        prefixes_to_try.append(f"TASK: {task.task_title}")
        # Named reminders from DB
        for field in ('approval_reminder_title', 'pr_reminder_title',
                      'merge_reminder_title', 'question_reminder_title'):
            title = getattr(task, field, None)
            if title:
                prefixes_to_try.append(title)

        for title in prefixes_to_try:
            try:
                if reminders_client.complete_by_title(task.reminder_list, title, case_sensitive=False):
                    print(f"    Reminder completed: {title[:60]}")
            except Exception:
                pass  # Best effort

        print(f"  \033[0;32m[DEAD]\033[0m {task.task_title}")

    print(f"\nKilled {len(rows)} task(s). Running agents will stop within ~10 seconds.")


def check_health(config: Config) -> None:
    """Check daemon health by reading the heartbeat file."""
    heartbeat_path = config.logs_path / "heartbeat"
    if not heartbeat_path.exists():
        print("\033[0;31m[DEAD]\033[0m No heartbeat file found — daemon may never have run")
        sys.exit(2)

    try:
        last_beat = datetime.fromisoformat(heartbeat_path.read_text().strip())
    except (ValueError, OSError) as e:
        print(f"\033[0;31m[ERROR]\033[0m Cannot read heartbeat: {e}")
        sys.exit(2)

    age = datetime.now() - last_beat
    age_minutes = age.total_seconds() / 60
    threshold = config.poll_interval_seconds * 2.5 / 60  # 2.5x poll interval

    if age_minutes <= threshold:
        print(f"\033[0;32m[HEALTHY]\033[0m Last heartbeat {age_minutes:.1f} min ago ({last_beat.strftime('%H:%M:%S')})")
        sys.exit(0)
    else:
        print(f"\033[0;33m[STALE]\033[0m Last heartbeat {age_minutes:.1f} min ago ({last_beat.strftime('%H:%M:%S')})")
        sys.exit(1)


def cleanup_stale(config: Config) -> None:
    """Remove local branches and optionally workspaces for completed/rejected tasks."""
    import shutil
    db = StateDB(config.db_path)
    completed = db.get_tasks_by_state(TaskState.COMPLETE)
    rejected = db.get_tasks_by_state(TaskState.REJECTED)
    error_tasks = db.get_tasks_by_state(TaskState.ERROR)

    stale_tasks = completed + rejected + error_tasks
    if not stale_tasks:
        print("No completed/rejected/error tasks to clean up.")
        return

    git = GitOperations(config.gh_bin, clone_depth=config.clone_depth)
    cleaned_branches = 0
    cleaned_dirs = 0

    for task in stale_tasks:
        if not task.branch_name:
            continue
        if task.local_repo_path and Path(task.local_repo_path).exists():
            result = git._run_git(
                ["branch", "--list", task.branch_name],
                Path(task.local_repo_path),
            )
            if result.stdout.strip():
                git._run_git(["branch", "-D", task.branch_name], Path(task.local_repo_path))
                print(f"  Deleted branch: {task.branch_name}")
                cleaned_branches += 1

    # Check for workspace dirs not referenced by any active task
    active_tasks = db.get_all_active_tasks()
    active_paths = {t.local_repo_path for t in active_tasks if t.local_repo_path}

    if config.workspaces_path.exists():
        for workspace_dir in config.workspaces_path.iterdir():
            if not workspace_dir.is_dir():
                continue
            ws_str = str(workspace_dir)
            if ws_str not in active_paths:
                # Check if ANY active task uses this workspace
                in_use = any(ws_str in (p or "") for p in active_paths)
                if not in_use:
                    size_mb = sum(f.stat().st_size for f in workspace_dir.rglob("*") if f.is_file()) / (1024 * 1024)
                    print(f"  Removing workspace: {workspace_dir.name} ({size_mb:.1f} MB)")
                    shutil.rmtree(workspace_dir, ignore_errors=True)
                    cleaned_dirs += 1

    print(f"\nCleaned {cleaned_branches} branches, {cleaned_dirs} workspace dirs.")


def cleanup_reminders(config: Config) -> None:
    """Complete orphan reminders for finished tasks.

    ONLY touches Reminders lists defined in config.projects — never touches
    any other list. Completes reminders with agent prefixes that belong to
    tasks in terminal states (COMPLETE, REJECTED, ERROR).

    Iterates in reverse index order to avoid index-shifting bugs.
    """
    db = StateDB(config.db_path)
    reminders_client = RemindersClient(config.reminders_bin, config.applescript_timeout_seconds)

    managed_lists = set(config.projects.keys())
    print(f"Scanning {len(managed_lists)} managed lists: {', '.join(sorted(managed_lists))}")
    print("(Only these lists will be touched — your other Reminders are safe.)\n")

    # Prefixes for agent-created reminders (both old and new naming)
    agent_prefixes = (
        "Approve PRD: ",
        "Complete to approve: ",
        "PR Ready: ",
        "PR Updated: ",
        "Merge: ",
        "Complete to merge: ",
        "Question: ",
    )

    # Build sets for matching: titles known to the DB, and task titles of finished tasks
    finished_task_titles: set[str] = set()
    known_reminder_titles: set[str] = set()
    for state in (TaskState.COMPLETE, TaskState.REJECTED, TaskState.ERROR):
        for t in db.get_tasks_by_state(state):
            finished_task_titles.add(t.task_title.lower())
            for field in ('approval_reminder_title', 'pr_reminder_title',
                          'merge_reminder_title', 'question_reminder_title'):
                title = getattr(t, field, None)
                if title:
                    known_reminder_titles.add(title)

    # Also get active task titles so we DON'T touch those
    active_task_titles: set[str] = set()
    for t in db.get_all_active_tasks():
        active_task_titles.add(t.task_title.lower())

    cleaned = 0
    skipped = 0

    for list_name in sorted(managed_lists):
        try:
            items = reminders_client.show_list(list_name)
        except RemindersError as e:
            print(f"  [SKIP] {list_name}: {e}")
            continue

        # Collect indices to complete, then process in REVERSE order
        # so completing item N doesn't shift items 0..N-1
        to_complete: list[tuple[int, str]] = []

        for i, reminder in enumerate(items):
            title = reminder.get("title", "")
            if reminder.get("isCompleted", False):
                continue

            # Only touch reminders with agent prefixes
            if not any(title.startswith(p) for p in agent_prefixes):
                continue

            # Match by exact title in DB, OR by extracting the task name from
            # the prefix and checking if that task is finished
            is_orphan = False

            if title in known_reminder_titles:
                is_orphan = True
            else:
                # Extract the task title portion after the prefix
                for prefix in agent_prefixes:
                    if title.startswith(prefix):
                        remainder = title[len(prefix):]
                        # Strip [id:xxx] suffix if present
                        import re
                        task_name = re.sub(r'\s*\[id:\w+\]\s*$', '', remainder).strip().lower()
                        if task_name in finished_task_titles and task_name not in active_task_titles:
                            is_orphan = True
                        break

            if is_orphan:
                to_complete.append((i, title))
            else:
                skipped += 1

        # Complete in reverse index order
        for i, title in reversed(to_complete):
            if reminders_client.complete_by_index(list_name, i):
                print(f"  [CLEANED] {list_name}: {title[:90]}")
                cleaned += 1
            else:
                print(f"  [FAILED] {list_name}: {title[:90]}")

    print(f"\nCleaned {cleaned} orphan reminders, skipped {skipped} (belong to active tasks)")


def write_heartbeat(config: Config) -> None:
    """Write current timestamp to heartbeat file."""
    heartbeat_path = config.logs_path / "heartbeat"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(datetime.now().isoformat())


def process_single_task(config: Config, task_id: str, logger: logging.Logger) -> None:
    """Process a single task by ID (for debugging)."""
    db = StateDB(config.db_path)
    task = db.get_task(task_id)
    if not task:
        print(f"\033[0;31m[ERROR]\033[0m No task found with ID: {task_id}")
        sys.exit(1)

    reminders = RemindersClient(config.reminders_bin, config.applescript_timeout_seconds)
    git = GitOperations(config.gh_bin, logger, clone_depth=config.clone_depth)
    prd_repo = PRDRepoManager(config.prd_repo_url, config.prd_repo_local, git)
    image_gen = ImageGenerator(model=config.image_model, enabled=config.image_notifications)
    notifier = Notifier(config.imsg_bin, config.phone_number, config.use_emoji, image_generator=image_gen)

    github_issues = None
    if config.github_issues_enabled:
        github_issues = GitHubIssuesClient(
            config.gh_bin, config.github_issues_label, config.github_trusted_authors
        )

    orchestrator = OrchestratorV4(
        config=config,
        db=db,
        reminders=reminders,
        git=git,
        prd_repo=prd_repo,
        notifier=notifier,
        logger=logger,
        github_issues=github_issues,
    )

    print(f"\033[0;32m[INFO]\033[0m Processing task: {task.task_title} (state: {task.state.value})")
    orchestrator.process_new_task(task)
    print(f"\033[0;32m[OK]\033[0m Task processed. New state: {task.state.value}")


# =============================================================================
# Poll cycle — one iteration of the daemon loop
# =============================================================================

def _reap_zombies(
    db: StateDB, config: Config,
    orchestrator: "OrchestratorV4", logger: logging.Logger,
) -> None:
    """Kill tasks stuck in AGENT_RUNNING that are clearly dead.

    A task is a zombie if:
    1. It's AGENT_RUNNING with no agent_session_id and implementation_started_at
       is older than the configured timeout (SDK never connected), OR
    2. It's AGENT_RUNNING with a session but implementation_started_at is older
       than 2x the timeout (agent truly stuck), OR
    3. It's IN_FLIGHT for longer than the timeout (workspace setup hung).
    """
    from lib.models import STATUS_FAILED
    now = datetime.now()
    timeout = config.claude_impl_timeout  # seconds
    reaped = 0

    for task in db.get_tasks_by_state(TaskState.AGENT_RUNNING):
        if task.queued_for_dispatch:
            continue  # queued, not actually running
        if not task.implementation_started_at:
            continue  # hasn't started yet

        elapsed = (now - task.implementation_started_at).total_seconds()

        # No session ID and past timeout → SDK never connected
        if not task.agent_session_id and elapsed > timeout:
            logger.warning(
                "Reaping zombie: '%s' (%s) — no session after %ds",
                task.task_title, task.reminder_list, int(elapsed),
            )
            task.state = TaskState.ERROR
            task.error_type = TaskError.TIMEOUT
            task.error_message = f"Agent SDK never connected after {int(elapsed)}s"
            task.updated_at = now
            db.save_task(task)
            orchestrator._update_reminder_status(task, STATUS_FAILED, notes=task.error_message)
            reaped += 1

        # Has session but 2x timeout → agent stuck
        elif task.agent_session_id and elapsed > timeout * 2:
            logger.warning(
                "Reaping stuck agent: '%s' (%s) — running for %ds",
                task.task_title, task.reminder_list, int(elapsed),
            )
            task.state = TaskState.ERROR
            task.error_type = TaskError.TIMEOUT
            task.error_message = f"Agent stuck for {int(elapsed)}s (2x timeout)"
            task.updated_at = now
            db.save_task(task)
            orchestrator._update_reminder_status(task, STATUS_FAILED, notes=task.error_message)
            reaped += 1

    # Also reap IN_FLIGHT tasks stuck longer than timeout
    for task in db.get_tasks_by_state(TaskState.IN_FLIGHT):
        elapsed = (now - task.updated_at).total_seconds()
        if elapsed > timeout:
            logger.warning(
                "Reaping stuck IN_FLIGHT: '%s' (%s) — in setup for %ds",
                task.task_title, task.reminder_list, int(elapsed),
            )
            task.state = TaskState.ERROR
            task.error_type = TaskError.TIMEOUT
            task.error_message = f"Stuck in setup for {int(elapsed)}s"
            task.updated_at = now
            db.save_task(task)
            orchestrator._update_reminder_status(task, STATUS_FAILED, notes=task.error_message)
            reaped += 1

    if reaped:
        logger.info("Reaped %d zombie/stuck tasks", reaped)


def poll_cycle(
    orchestrator: OrchestratorV4,
    config: Config,
    db: StateDB,
    reminders: RemindersClient,
    logger: logging.Logger,
    dry_run: bool = False,
) -> None:
    """One iteration of the daemon loop."""
    logger.info("Starting poll cycle%s", " [DRY RUN]" if dry_run else "")

    # ── Zombie reaper ──────────────────────────────────────────────
    # Kill tasks stuck in AGENT_RUNNING with no session ID for longer
    # than the configured timeout. These are hung SDK calls that never
    # connected. Also kill tasks that HAVE a session but exceeded 2x
    # the timeout (agent is truly stuck).
    _reap_zombies(db, config, orchestrator, logger)

    # P6: wrap the reminders client in a per-cycle cache so approval/question/
    # merge checks reuse fetched list data instead of spawning new subprocesses.
    cached_reminders = RemindersCache(reminders)
    original_reminders = orchestrator.reminders
    orchestrator.reminders = cached_reminders

    github_issues = orchestrator.github_issues

    # -----------------------------------------------------------------
    # 1. Check tasks in AWAITING_APPROVAL — did user approve?
    # -----------------------------------------------------------------
    for task in db.get_tasks_by_state(TaskState.AWAITING_APPROVAL):
        try:
            orchestrator.check_approval_status(task)
        except Exception as e:
            logger.error("Error checking approval for '%s': %s", task.task_title, e, exc_info=True)

    # -----------------------------------------------------------------
    # 2. Check tasks in NEEDS_CLARIFICATION — did user answer?
    # -----------------------------------------------------------------
    for task in db.get_tasks_by_state(TaskState.NEEDS_CLARIFICATION):
        try:
            orchestrator.check_question_answered(task)
        except Exception as e:
            logger.error("Error checking question for '%s': %s", task.task_title, e, exc_info=True)

    # -----------------------------------------------------------------
    # 3. Check tasks in PR_CREATED / PR_REVIEW_PENDING — review comments?
    # -----------------------------------------------------------------
    if config.auto_respond_to_reviews:
        for state in (TaskState.PR_REVIEW_PENDING, TaskState.PR_CREATED):
            for task in db.get_tasks_by_state(state):
                try:
                    orchestrator.check_pr_reviews(task)
                except Exception as e:
                    logger.error("Error checking PR reviews for '%s': %s", task.task_title, e, exc_info=True)

    # -----------------------------------------------------------------
    # 4. Check tasks in AWAITING_MERGE — merge requested?
    # -----------------------------------------------------------------
    for task in db.get_tasks_by_state(TaskState.AWAITING_MERGE):
        try:
            orchestrator.check_merge_status(task)
        except Exception as e:
            logger.error("Error checking merge status for '%s': %s", task.task_title, e, exc_info=True)

    # -----------------------------------------------------------------
    # 5. Check tasks in MERGING — merge in progress?
    #    (handled inline by orchestrator.process_merge, but pick up
    #     any that were left mid-merge between poll cycles)
    # -----------------------------------------------------------------
    for task in db.get_tasks_by_state(TaskState.MERGING):
        try:
            orchestrator.process_merge(task)
        except Exception as e:
            logger.error("Error processing merge for '%s': %s", task.task_title, e, exc_info=True)

    # -----------------------------------------------------------------
    # 6. Discover new tasks from Reminders
    # -----------------------------------------------------------------
    # R11: skip Reminders scanning if circuit breaker is open (consecutive failures)
    _scan_reminders = orchestrator.circuit_breaker.allow_request("reminders")
    if not _scan_reminders:
        logger.warning("Circuit breaker OPEN for reminders — skipping discovery this cycle")

    for list_name, project_config in config.projects.items():
        if not _scan_reminders:
            break
        repo_url = project_config if isinstance(project_config, str) else project_config.get("url", "")
        if not repo_url:
            continue

        logger.debug("Scanning Reminders list: %s", list_name)
        try:
            reminders_in_list = cached_reminders.show_list(list_name)
            orchestrator.circuit_breaker.record_success("reminders")
        except RemindersError as e:
            logger.error("Reminders scan failed for '%s': %s", list_name, e)
            orchestrator.circuit_breaker.record_failure("reminders")
            continue

        for reminder in reminders_in_list:
            title = reminder.get("title", "")

            # Strip any existing status prefix like [QUEUED], [WORKING], etc.
            clean_title = re.sub(r'^\[[\w\s#/]+\]\s*', '', title)

            if clean_title.upper().startswith(TASK_PREFIX.upper()) and not reminder.get("isCompleted", False):
                reminder_id = reminder.get("externalId")
                notes = reminder.get("notes", "")

                # Try AppleScript fallback for notes if needed
                if config.notes_fetch_mode == "applescript_only":
                    notes = reminders.get_notes_via_applescript(list_name, title) or ""
                elif not notes and config.notes_fetch_mode == "applescript_fallback":
                    notes = reminders.get_notes_via_applescript(list_name, title) or ""

                if not reminder_id:
                    reminder_id = _stable_reminder_id(list_name, reminder)
                    logger.warning("Reminder missing externalId, using stable hash for '%s'", title)

                existing = db.get_task_by_reminder_id(reminder_id)
                if not existing:
                    # Fallback: check if an active task matches by title prefix
                    # This catches cases where the user edited the reminder title
                    raw_title = title[len(TASK_PREFIX):]
                    clean = _clean_title(raw_title)
                    prefix = clean[:20]
                    if len(prefix) >= MIN_DEDUP_PREFIX_LEN:
                        existing = db.get_active_task_by_title_prefix(list_name, prefix)
                        if existing:
                            logger.info(
                                "Matched existing task '%s' by title prefix for reminder '%s'",
                                existing.task_title, clean,
                            )
                if not existing:
                    raw_title = title[len(TASK_PREFIX):]
                    priority = _extract_priority(raw_title)
                    clean = _clean_title(raw_title)

                    task = Task(
                        id=str(uuid4()),
                        reminder_id=reminder_id,
                        reminder_list=list_name,
                        task_title=clean,
                        task_notes=notes,
                        repo_url=repo_url,
                        state=TaskState.NEW,
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                        priority=priority,
                        source=TaskSource.REMINDERS,
                    )
                    if not dry_run:
                        db.save_task(task)
                        orchestrator._update_reminder_status(task, STATUS_QUEUED)
                        orchestrator.notifier.notify_task_received(task)
                    logger.info(
                        "Discovered new task: %s (priority: %s) in %s",
                        clean, priority.name, list_name,
                    )

    # 6b. Discover tasks from GitHub Issues (if enabled)
    if github_issues:
        for list_name, project_config in config.projects.items():
            repo_url = project_config if isinstance(project_config, str) else project_config.get("url", "")
            if not repo_url:
                continue

            logger.debug("Scanning GitHub Issues: %s", repo_url)
            issues = github_issues.get_issues_for_repo(repo_url)

            for issue in issues:
                issue_number = issue.get("number")
                if not issue_number:
                    continue

                existing = db.get_task_by_github_issue(repo_url, issue_number)
                if existing:
                    continue

                labels = [lb.get("name", "") for lb in issue.get("labels", [])]
                priority = TaskPriority.DEFAULT
                for label in labels:
                    if label.lower().startswith("p") and len(label) == 2 and label[1].isdigit():
                        try:
                            priority = TaskPriority(int(label[1]))
                        except ValueError:
                            pass

                task = Task(
                    id=str(uuid4()),
                    reminder_id=f"github-issue-{issue_number}",
                    reminder_list=list_name,
                    task_title=issue.get("title", ""),
                    task_notes=issue.get("body", ""),
                    repo_url=repo_url,
                    state=TaskState.NEW,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                    priority=priority,
                    source=TaskSource.GITHUB_ISSUES,
                    github_issue_number=issue_number,
                )
                if not dry_run:
                    db.save_task(task)
                logger.info("Discovered GitHub Issue #%s: %s", issue_number, task.task_title)

    # -----------------------------------------------------------------
    # 7. Process new/retry tasks (within concurrency limits)
    # -----------------------------------------------------------------
    active_states = [
        TaskState.IN_FLIGHT,
        TaskState.AGENT_RUNNING,
        TaskState.MERGING,
    ]

    all_active = db.get_all_active_tasks()
    now = datetime.now()

    def is_active(t: Task) -> bool:
        if t.state not in active_states:
            return False
        # F8: tasks queued for dispatch are not yet consuming a slot
        if t.state == TaskState.AGENT_RUNNING and t.queued_for_dispatch:
            return False
        return True

    active_count = len([t for t in all_active if is_active(t)])

    # Count active per project
    active_per_project: Dict[str, int] = {}
    for t in all_active:
        if is_active(t):
            active_per_project[t.reminder_list] = active_per_project.get(t.reminder_list, 0) + 1

    tasks_to_process: List[Tuple[str, Task]] = []

    # F8: tasks queued for implementation by approval/question checks
    queued_tasks = [
        t for t in db.get_tasks_by_state(TaskState.AGENT_RUNNING)
        if t.queued_for_dispatch and not t.next_retry_at
    ]
    for task in queued_tasks:
        if active_count >= config.max_concurrent_tasks:
            break
        project_active = active_per_project.get(task.reminder_list, 0)
        if project_active >= config.max_concurrent_per_project:
            continue
        tasks_to_process.append(("approved", task))
        active_per_project[task.reminder_list] = project_active + 1
        active_count += 1

    # New tasks
    if active_count < config.max_concurrent_tasks:
        new_tasks = db.get_tasks_by_state(TaskState.NEW)
        for task in new_tasks:
            if task.next_retry_at and task.next_retry_at > now:
                logger.debug("Task '%s' waiting for retry at %s", task.task_title, task.next_retry_at)
                continue
            project_active = active_per_project.get(task.reminder_list, 0)
            if project_active >= config.max_concurrent_per_project:
                logger.debug("Project '%s' at concurrency limit (%d)", task.reminder_list, project_active)
                continue

            tasks_to_process.append(("new", task))
            active_per_project[task.reminder_list] = project_active + 1
            active_count += 1

            if active_count >= config.max_concurrent_tasks:
                break

    # Check circuit breaker before dispatching tasks
    if not orchestrator.circuit_breaker.allow_request("claude"):
        logger.warning("Circuit breaker OPEN for claude — skipping task dispatch this cycle")
        write_heartbeat(config)
        return

    # Execute tasks
    if tasks_to_process:
        def _process_task(action: str, task: Task) -> Tuple[str, str, Optional[str]]:
            # Register a kill signal so --kill-task can stop this agent
            kill_event = _threading.Event()
            _kill_signals[task.id] = kill_event

            set_task_context(task.id)
            try:
                # Check if task was killed between discovery and dispatch
                reloaded = db.get_task(task.id)
                if reloaded and reloaded.state in (TaskState.REJECTED, TaskState.ERROR, TaskState.COMPLETE):
                    logger.info("Task '%s' was killed before dispatch, skipping", task.task_title)
                    return task.task_title, action, None

                if action == "approved":
                    orchestrator._run_agent(task)
                else:
                    orchestrator.process_new_task(task)
                orchestrator.circuit_breaker.record_success("claude")
                return task.task_title, action, None
            except Exception as e:
                orchestrator.circuit_breaker.record_failure("claude")
                logger.error("Error processing task '%s': %s", task.task_title, e, exc_info=True)
                return task.task_title, action, str(e)
            finally:
                _kill_signals.pop(task.id, None)
                clear_task_context()

        if len(tasks_to_process) == 1:
            action, task = tasks_to_process[0]
            _process_task(action, task)
        else:
            logger.info("Processing %d tasks concurrently", len(tasks_to_process))
            global_timeout = 5400  # 90 minutes

            with ThreadPoolExecutor(max_workers=config.max_concurrent_tasks) as executor:
                futures = {
                    executor.submit(_process_task, action, task): (action, task)
                    for action, task in tasks_to_process
                }
                try:
                    for future in as_completed(futures, timeout=global_timeout):
                        action, task = futures[future]
                        try:
                            title, act, error = future.result()
                            if error:
                                logger.error("Task '%s' (%s) failed: %s", title, act, error)
                            else:
                                logger.info("Task '%s' (%s) completed", title, act)
                        except Exception as e:
                            logger.error("Task '%s' raised exception: %s", task.task_title, e)
                except FuturesTimeoutError:
                    logger.error("Global timeout (%ds) exceeded, cancelling remaining tasks", global_timeout)
                    for future in futures:
                        future.cancel()

    # P6: restore original (uncached) client and discard cache
    orchestrator.reminders = original_reminders
    cached_reminders.clear()

    write_heartbeat(config)
    logger.info("Poll cycle complete")


# =============================================================================
# Main entry point
# =============================================================================

def main() -> None:
    """Entry point — parses args, loads config, runs poll loop."""
    parser = argparse.ArgumentParser(
        description=f"Porter v{__version__} — PRD-to-PR system using agent patterns",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Priority System:
  Add [P0] through [P4] to task titles for priority ordering.
  P0 = Critical, P1 = High, P2 = Medium, P3 = Low, P4 = Backlog
""",
    )
    parser.add_argument("--version", action="version", version=f"Porter v{__version__}")
    parser.add_argument("--status", action="store_true", help="Show status of all tasks")
    parser.add_argument("--list-tasks", action="store_true", help="List all tasks (including completed)")
    parser.add_argument("--reset-task", metavar="NAME", help="Reset a task to NEW state")
    parser.add_argument("--cancel-task", metavar="NAME", help="Soft cancel (marks REJECTED, doesn't stop running agent)")
    parser.add_argument("--kill-task", metavar="NAME", help="Hard kill — stops agent, cleans DB and reminders")
    parser.add_argument("--kill-all", action="store_true", help="Hard kill ALL active tasks")
    parser.add_argument("--cleanup", action="store_true", help="Remove stale branches and unused workspaces")
    parser.add_argument("--cleanup-reminders", action="store_true", help="Complete orphan reminders from finished tasks (only touches managed project lists)")
    parser.add_argument("--process-task", metavar="ID", help="Process a single task by ID")
    parser.add_argument("--healthcheck", action="store_true", help="Check daemon health and exit")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as daemon")
    parser.add_argument("--dry-run", action="store_true", help="Preview mode — no changes will be made")
    parser.add_argument("--verbose", action="store_true", help="Enable debug-level logging")
    parser.add_argument("--config", metavar="PATH", help="Path to config.toml",
                        default=str(Path.home() / ".claude/agents/porter/config.toml"))

    args = parser.parse_args()

    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"\033[0;31m[ERROR]\033[0m Configuration file not found: {config_path}")
        print("Please create the configuration file first.")
        sys.exit(1)

    config = Config.from_file(config_path)

    # Run startup health checks for commands that do real work
    if not any([args.status, args.list_tasks, args.healthcheck, args.reset_task, args.cancel_task, args.cleanup, args.cleanup_reminders, args.kill_task, args.kill_all]):
        try:
            health = StartupHealthCheck(config)
            health.run_all()
        except HealthCheckError as e:
            print(f"\033[0;31m[HEALTH CHECK FAILED]\033[0m {e}")
            sys.exit(1)

    # Commands that don't need full infrastructure
    if args.healthcheck:
        check_health(config)
        sys.exit(0)

    if args.status:
        show_status(config)
        sys.exit(0)

    if args.list_tasks:
        list_tasks(config)
        sys.exit(0)

    if args.reset_task:
        reset_task(config, args.reset_task)
        sys.exit(0)

    if args.cancel_task:
        cancel_task(config, args.cancel_task)
        sys.exit(0)

    if args.kill_task:
        kill_task(config, args.kill_task)
        sys.exit(0)

    if args.kill_all:
        kill_task(config, "%")  # matches everything
        sys.exit(0)

    if args.cleanup:
        cleanup_stale(config)
        sys.exit(0)

    if args.cleanup_reminders:
        cleanup_reminders(config)
        sys.exit(0)

    # Set up logging
    logger = setup_logging(config.logs_path, level=config.log_level, verbose=args.verbose)

    # Handle --process-task (debug mode, no lock)
    if args.process_task:
        process_single_task(config, args.process_task, logger)
        sys.exit(0)

    # Initialize infrastructure
    db = StateDB(config.db_path)
    reminders_client = RemindersClient(config.reminders_bin, config.applescript_timeout_seconds)
    git = GitOperations(config.gh_bin, logger, clone_depth=config.clone_depth)
    prd_repo = PRDRepoManager(config.prd_repo_url, config.prd_repo_local, git)
    image_gen = ImageGenerator(model=config.image_model, enabled=config.image_notifications)
    notifier = Notifier(config.imsg_bin, config.phone_number, config.use_emoji, image_generator=image_gen)

    github_issues = None
    if config.github_issues_enabled:
        github_issues = GitHubIssuesClient(
            config.gh_bin, config.github_issues_label, config.github_trusted_authors
        )

    orchestrator = OrchestratorV4(
        config=config,
        db=db,
        reminders=reminders_client,
        git=git,
        prd_repo=prd_repo,
        notifier=notifier,
        logger=logger,
        github_issues=github_issues,
        dry_run=args.dry_run,
    )

    # F9: eagerly check image generation availability and log once at startup
    if config.image_notifications and not image_gen._ensure_client():
        logger.warning("Image notifications configured but unavailable — falling back to text-only")

    # Lock file to prevent concurrent execution
    lock_path = config.logs_path / "porter.lock"
    lock = LockFile(lock_path, logger)

    # Signal handling for clean shutdown
    def cleanup_handler(signum=None, frame=None):
        sig_name = signal.Signals(signum).name if signum else "unknown"
        logger.info("Received %s, shutting down...", sig_name)
        # R8: flush WAL and close DB connections before exiting
        db.close()
        lock.release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup_handler)
    signal.signal(signal.SIGINT, cleanup_handler)
    atexit.register(lock.release)

    # Daemon mode
    if args.daemon:
        logger.info("Starting Porter v%s in daemon mode", __version__)

        if not lock.acquire():
            logger.error("Another instance is already running")
            print("\033[0;31m[ERROR]\033[0m Another instance is already running")
            sys.exit(1)

        try:
            while True:
                try:
                    poll_cycle(orchestrator, config, db, reminders_client, logger, dry_run=args.dry_run)
                except Exception as e:
                    logger.error("Error in poll cycle: %s", e, exc_info=True)
                time.sleep(config.poll_interval_seconds)
        finally:
            lock.release()

    else:
        # Single poll cycle (for launchd or --once)
        if not lock.acquire():
            logger.warning("Another instance is already running, skipping this cycle")
            print("\033[0;33m[WARN]\033[0m Another instance running, skipping")
            sys.exit(0)

        try:
            if args.dry_run:
                print("\033[0;36m[DRY-RUN]\033[0m Preview mode — no changes will be made")
            else:
                print(f"\033[0;32m[INFO]\033[0m Running one poll cycle (v{__version__})...")
            poll_cycle(orchestrator, config, db, reminders_client, logger, dry_run=args.dry_run)
        except Exception as e:
            logger.error("Error in poll cycle: %s", e, exc_info=True)
            sys.exit(1)
        finally:
            lock.release()


if __name__ == "__main__":
    main()
