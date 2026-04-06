"""
Porter — Orchestrator.

Simplified state machine that wires together the router, evaluator, and agent
runner with existing infrastructure (Reminders, Git, Notifier, DB).

Replaces v3's 18-state TaskStateMachine with a streamlined 10-state flow:
  NEW -> ROUTING -> IN_FLIGHT -> (PRD_EVALUATING) -> (AWAITING_APPROVAL)
      -> AGENT_RUNNING -> PR_CREATED -> (PR_REVIEW_PENDING) -> (AWAITING_MERGE)
      -> MERGING -> COMPLETE
"""

import concurrent.futures
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from lib.agent_runner import AgentConfig, AgentRunner
from lib.circuit_breaker import CircuitBreakerRegistry
from lib.db import StateDB
from lib.git_ops import GitOperations
from lib.github_issues import GitHubIssuesClient
from lib.models import (
    APPROVAL_PREFIX,
    MERGE_PREFIX,
    PR_READY_PREFIX,
    PR_UPDATE_PREFIX,
    QUESTION_PREFIX,
    STATUS_FAILED,
    STATUS_MERGE,
    STATUS_PR,
    STATUS_QUEUED,
    STATUS_QUESTION,
    STATUS_RETRY,
    STATUS_REVIEW_PRD,
    STATUS_WORKING,
    TASK_PREFIX,
    USER_ANSWER,
    USER_FIX,
    Config,
    Task,
    TaskError,
    TaskSource,
    TaskState,
)
from lib.notifier import Notifier
from lib.prd_evaluator import PRDEvaluator
from lib.prd_repo import PRDRepoManager
from lib.reminders import RemindersClient
from lib.router import TaskRouter, TaskType
from lib.test_runner import TestRunner

# =============================================================================
# Workflow configuration — maps task type to ceremony level
# =============================================================================

WORKFLOW_CONFIG = {
    TaskType.QUICKFIX: {
        "needs_prd": False, "needs_approval": False, "needs_eval": False,
        "model": "sonnet", "max_turns": 30,
    },
    TaskType.BUGFIX: {
        "needs_prd": True, "needs_approval": True, "needs_eval": False,
        "model": "opus", "max_turns": 100,
    },
    TaskType.FEATURE: {
        "needs_prd": True, "needs_approval": True, "needs_eval": True,
        "model": "opus", "max_turns": 150,
    },
    TaskType.REFACTOR: {
        "needs_prd": True, "needs_approval": True, "needs_eval": True,
        "model": "opus", "max_turns": 150,
    },
    TaskType.ANALYSIS: {
        "needs_prd": False, "needs_approval": False, "needs_eval": False,
        "model": "sonnet", "max_turns": 50,
    },
    TaskType.COMPOSITE: {
        "needs_prd": False, "needs_approval": False, "needs_eval": False,
        "model": "sonnet", "max_turns": 50,
    },
}

# Maximum number of PRD regeneration attempts after eval failure
_MAX_PRD_REGEN_ATTEMPTS = 2


class OrchestratorV4:
    """v4 orchestrator — simplified state machine for task processing."""

    def __init__(
        self,
        config: Config,
        db: StateDB,
        reminders: RemindersClient,
        git: GitOperations,
        prd_repo: PRDRepoManager,
        notifier: Notifier,
        logger: logging.Logger,
        github_issues: Optional[GitHubIssuesClient] = None,
        dry_run: bool = False,
    ):
        self.config = config
        self.db = db
        self.reminders = reminders
        self.git = git
        self.prd_repo = prd_repo
        self.notifier = notifier
        self.logger = logger
        self.github_issues = github_issues
        self.dry_run = dry_run

        # v4 components
        self.router = TaskRouter(claude_bin=config.claude_bin)
        self.evaluator = PRDEvaluator(
            claude_bin=config.claude_bin,
            model="sonnet",
            timeout=120,
        )
        self.test_runner = TestRunner(config.test_timeout_seconds, flaky_retries=config.test_flaky_retries)
        self.circuit_breaker = CircuitBreakerRegistry(
            failure_threshold=3,
            recovery_timeout_seconds=config.poll_interval_seconds * 2,
        )

    def _make_cancellation_check(self, task: Task) -> callable:
        """Build a cancellation check.

        Returns a callable that returns True if:
        1. The task was killed via --kill-task (DB state is REJECTED), or
        2. The original task reminder was completed by the user.

        Checks DB state every 10 seconds (cheap), Reminders every 60 seconds.
        """
        _last_db_check = [0.0]
        _last_rem_check = [0.0]
        _DB_INTERVAL = 10.0
        _REM_INTERVAL = 60.0

        def _check() -> bool:
            import time
            now = time.monotonic()

            # Fast check: DB state (catches --kill-task)
            if now - _last_db_check[0] >= _DB_INTERVAL:
                _last_db_check[0] = now
                try:
                    reloaded = self.db.get_task(task.id)
                    if reloaded and reloaded.state in (TaskState.REJECTED, TaskState.ERROR):
                        self.logger.info("Task killed via DB — cancelling '%s'", task.task_title)
                        return True
                except Exception:
                    pass

            # Slow check: Reminders (catches user completing the reminder)
            if now - _last_rem_check[0] >= _REM_INTERVAL:
                _last_rem_check[0] = now
                try:
                    if task.source == TaskSource.REMINDERS and task.reminder_id:
                        completed = self.reminders.is_completed_by_external_id(
                            task.reminder_list, task.reminder_id
                        )
                        if completed is True:
                            self.logger.info("Task reminder completed — cancelling '%s'", task.task_title)
                            return True
                except Exception:
                    pass

            return False

        return _check

    def _agent_for_task_type(self, task_type: str) -> AgentRunner:
        """Build an AgentRunner configured for the given task type."""
        try:
            wf = WORKFLOW_CONFIG[TaskType(task_type)]
        except (ValueError, KeyError):
            wf = WORKFLOW_CONFIG[TaskType.FEATURE]

        return AgentRunner(AgentConfig(
            model=wf.get("model", self.config.claude_model),
            permission_mode=self.config.claude_permission_mode,
            max_turns=wf.get("max_turns", self.config.claude_impl_max_turns),
            timeout_seconds=self.config.claude_impl_timeout,
            max_cost_per_task_usd=self.config.max_cost_per_task_usd,
            logs_path=self.config.logs_path,
        ))

    # -------------------------------------------------------------------------
    # State transition helper
    # -------------------------------------------------------------------------

    def transition(self, task: Task, new_state: TaskState, notes: str = "") -> Task:
        """Update task state in DB and log the transition.

        Uses atomic compare-and-swap. If the task has moved to a different
        state since we loaded it (stale), log a warning and reload.
        """
        old_state = task.state
        if not self.db.transition_if(task.id, old_state, new_state, notes):
            # State was changed by another thread/process
            self.logger.warning(
                "Stale transition for '%s': expected %s but task has moved. Reloading.",
                task.task_title, old_state.value,
            )
            reloaded = self.db.get_task(task.id)
            if reloaded:
                return reloaded
            return task  # Fallback if task not found (shouldn't happen)

        task.state = new_state
        task.status_message = notes or None
        task.updated_at = datetime.now()
        self.logger.info(
            "Task '%s' transitioned: %s -> %s%s",
            task.task_title, old_state.value, new_state.value,
            f" ({notes})" if notes else "",
        )

        # Auto-update reminder on terminal states
        if new_state == TaskState.ERROR:
            # Show failure in Reminders — DON'T complete, user needs to see it
            try:
                error_notes = task.error_message[:200] if task.error_message else notes or "Unknown error"
                self._update_reminder_status(task, STATUS_FAILED, notes=error_notes)
            except Exception as e:
                self.logger.warning("Failed to update reminder to FAILED for '%s': %s", task.task_title, e)
        elif new_state in (TaskState.COMPLETE, TaskState.REJECTED):
            # Task is done — complete all associated reminders
            try:
                self._complete_task_reminders(task)
            except Exception as e:
                self.logger.warning("Failed to cleanup reminders for '%s': %s", task.task_title, e)

        return task

    # -------------------------------------------------------------------------
    # Reminder status prefix
    # -------------------------------------------------------------------------

    # Sentinel indicating a Reminders CLI error (as opposed to "not found")
    _REMINDER_LOOKUP_ERROR = object()

    def _find_task_reminder(self, task: Task) -> Optional[dict]:
        """Find the original reminder for a task by externalId.

        Returns:
        - The reminder dict if found.
        - None if the reminder was genuinely deleted (list fetched
          successfully but reminder not in it).
        - _REMINDER_LOOKUP_ERROR if the Reminders CLI failed (callers
          should skip this check and try again next cycle, NOT treat
          it as a deletion).
        """
        if not task.reminder_id:
            return None
        try:
            reminders = self.reminders.show_list(task.reminder_list, include_completed=True)
        except Exception as e:
            self.logger.warning(
                "Reminders CLI failed for '%s', will retry next cycle: %s",
                task.reminder_list, e,
            )
            return self._REMINDER_LOOKUP_ERROR
        for r in reminders:
            if r.get("externalId") == task.reminder_id:
                return r
        return None  # Genuinely deleted

    def _update_reminder_status(self, task: Task, prefix: str, notes: Optional[str] = None) -> None:
        """Update the original task reminder's title prefix and optionally notes.

        Strips any existing status prefix like [QUEUED], [WORKING], etc.
        and prepends the new one.  Does nothing for non-Reminders tasks.
        """
        if task.source != TaskSource.REMINDERS or not task.reminder_id:
            return

        # Build the clean title (strip any existing [...] prefix)
        base_title = f"{TASK_PREFIX}{task.task_title}"

        new_title = f"{prefix}{base_title}"

        try:
            success = self.reminders.edit_by_external_id(
                task.reminder_list, task.reminder_id,
                new_title=new_title,
                new_notes=notes,
            )
            if not success:
                self.logger.warning("Failed to update reminder status for '%s'", task.task_title)
        except Exception as e:
            self.logger.warning("Error updating reminder status: %s", e)

    # -------------------------------------------------------------------------
    # Utility helpers (ported from v3)
    # -------------------------------------------------------------------------

    @staticmethod
    def _slugify(text: str) -> str:
        """Convert text to a URL-safe slug."""
        slug = text.lower().strip()
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[-\s]+', '-', slug)
        return slug[:50]

    def _task_token(self, task: Task) -> str:
        """Short, human-friendly token for task identification."""
        return task.id.split("-")[0]

    def _record_eval_human_feedback(self, task: Task, human_approved: bool) -> None:
        """Record evaluator vs. human decision for calibration learning.

        Only records when the task went through PRD evaluation (has eval result).
        """
        if not task.prd_eval_result:
            return  # No eval was done — nothing to compare

        eval_passed = task.prd_eval_passed
        if eval_passed == human_approved:
            return  # Agreement — no calibration signal

        # Parse eval score from stored result
        import json as _json
        eval_score = None
        eval_issues = None
        try:
            match = re.search(r"\{.*\}", task.prd_eval_result, re.DOTALL)
            if match:
                data = _json.loads(match.group(0))
                eval_score = data.get("score")
                eval_issues = "; ".join(data.get("issues", []))
        except Exception:
            pass

        self.db.record_eval_feedback(
            task_id=task.id,
            eval_passed=eval_passed,
            human_approved=human_approved,
            eval_score=eval_score,
            eval_issues=eval_issues,
        )
        self.logger.info(
            "Recorded eval calibration: eval=%s human=%s (score=%s)",
            "pass" if eval_passed else "fail",
            "approved" if human_approved else "rejected",
            eval_score,
        )

    def _write_heartbeat(self) -> None:
        """Write heartbeat during long-running operations.

        Called by workspace setup, PRD generation, merge, and other
        operations that can run for minutes.  Without this, the daemon
        appears dead during those operations and the healthcheck
        reports STALE.
        """
        try:
            heartbeat_path = self.config.logs_path / "heartbeat"
            heartbeat_path.write_text(datetime.now().isoformat())
        except OSError:
            pass

    def _set_error(self, task: Task, error_type: TaskError, message: str) -> None:
        """Set structured error information on task."""
        task.error_type = error_type
        task.error_message = message
        self.logger.error("[%s] %s", error_type.value, message)

    def _calculate_backoff(self, retry_count: int) -> datetime:
        """Calculate next retry time using exponential backoff.

        Formula: base * (4 ^ (retry_count - 1)) minutes
          retry 1 -> 5 min, retry 2 -> 20 min, retry 3 -> 80 min
        """
        exponent = max(retry_count - 1, 0)
        backoff_minutes = self.config.retry_backoff_base * (4 ** exponent)
        return datetime.now() + timedelta(minutes=backoff_minutes)

    # -------------------------------------------------------------------------
    # Main entry point — process a NEW task
    # -------------------------------------------------------------------------

    def process_new_task(self, task: Task) -> Task:
        """Process a task in NEW state — the main orchestrator entry point.

        Flow:
        0. If this is a merge retry (PR already exists), skip straight to merge
        1. Check daily budget (F1)
        2. Route (classify task type)
        3. Set up workspace (clone/pull, create branch)
        4. If needs_prd: generate PRD
        5. If needs_eval: evaluate PRD, regenerate up to 2x on failure
        6. Save PRD, create approval reminder if needs_approval
        7. Otherwise go straight to _run_agent()
        """
        self.logger.info("Processing new task: %s", task.task_title)

        # Fast path: merge retry — PR already exists, just re-attempt the merge.
        # Skip the entire route/clone/PRD/agent pipeline.
        if task.pr_url and task.pr_number and task.retry_count > 0:
            _MERGE_ERRORS = {TaskError.MERGE_FAILED, TaskError.MERGE_CONFLICT_FAILED, TaskError.MERGE_TEST_FAILED}
            last_error = task.error_type
            if last_error in _MERGE_ERRORS or (not last_error and task.pr_url):
                self.logger.info(
                    "Merge retry for '%s' — PR #%s exists, skipping to merge",
                    task.task_title, task.pr_number,
                )
                task.error_type = None
                task.error_message = None
                self.db.save_task(task)
                try:
                    return self.process_merge(task)
                except Exception as e:
                    self.logger.error("Merge retry failed: %s", e)
                    return self._handle_failure(task, TaskError.MERGE_FAILED, str(e))

        # F1: daily budget cap — refuse new work if we've spent too much today
        if self.config.daily_budget_usd is not None:
            spent = self.db.get_daily_cost_estimate()
            if spent >= self.config.daily_budget_usd:
                self.logger.warning(
                    "Daily budget exhausted ($%.2f / $%.2f). Deferring task '%s'.",
                    spent, self.config.daily_budget_usd, task.task_title,
                )
                self._set_error(task, TaskError.BUDGET_EXCEEDED,
                                f"Daily budget exhausted (${spent:.2f} / ${self.config.daily_budget_usd:.2f})")
                return self._handle_failure(task, TaskError.BUDGET_EXCEEDED, task.error_message)

        if self.dry_run:
            self.logger.info("[DRY RUN] Would process task, skipping actual execution")
            return task

        # --- Step 1: Route ---
        task = self.transition(task, TaskState.ROUTING, "Classifying task type")
        task_type = self.router.classify(task.task_title, task.task_notes)
        task.task_type = task_type.value
        self.db.save_task(task)
        self.logger.info("Task classified as: %s", task_type.value)

        wf = WORKFLOW_CONFIG[task_type]

        # --- Analysis tasks: clone repo, run read-only agent, skip PRD/PR ---
        if task_type == TaskType.ANALYSIS:
            task = self.transition(task, TaskState.IN_FLIGHT, "Setting up workspace for analysis")
            try:
                self._setup_workspace_readonly(task)
                return self._run_analysis(task)
            except Exception as e:
                self.logger.error("Error processing analysis task: %s", e)
                return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

        # --- Composite tasks: clone repo, run decomposition agent, create sub-task Reminders ---
        if task_type == TaskType.COMPOSITE:
            task = self.transition(task, TaskState.IN_FLIGHT, "Setting up workspace for decomposition")
            try:
                self._setup_workspace_readonly(task)
                return self._run_composite(task)
            except Exception as e:
                self.logger.error("Error processing composite task: %s", e)
                return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

        # --- Steps 2-3: Workspace setup and PRD generation ---
        task = self.transition(task, TaskState.IN_FLIGHT, "Setting up workspace")
        self.notifier.notify_prd_starting(task)

        try:
            prd_content: Optional[str] = None

            if wf["needs_prd"]:
                # R9: pass task fields explicitly to _generate_prd so it never
                # reads the shared mutable Task object from the workspace thread.
                prd_kwargs = dict(
                    task_title=task.task_title,
                    task_notes=task.task_notes,
                    reminder_list=task.reminder_list,
                )
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    workspace_future = executor.submit(self._setup_workspace, task)
                    prd_future = executor.submit(self._generate_prd, **prd_kwargs)

                    # Wait for both — workspace writes task fields, PRD is independent
                    workspace_future.result()
                    prd_content = prd_future.result()

                if not prd_content:
                    self._set_error(task, TaskError.PRD_GENERATION_FAILED, "Failed to generate PRD")
                    raise Exception(task.error_message)

                # --- Step 4: PRD evaluation (if needed) ---
                if wf["needs_eval"]:
                    task = self.transition(task, TaskState.PRD_EVALUATING, "Evaluating PRD quality")
                    calibration = self.db.get_eval_calibration(limit=10)
                    regen_attempts = 0
                    while regen_attempts < _MAX_PRD_REGEN_ATTEMPTS:
                        eval_result = self.evaluator.evaluate(
                            prd_content,
                            Path(task.local_repo_path),
                            calibration_notes=calibration or None,
                        )
                        task.prd_eval_result = eval_result.raw_output
                        task.prd_eval_passed = eval_result.passed
                        self.db.save_task(task)

                        if eval_result.passed:
                            self.logger.info(
                                "PRD evaluation passed (score %d/10)", eval_result.score
                            )
                            break

                        regen_attempts += 1
                        self.logger.warning(
                            "PRD evaluation failed (score %d/10, attempt %d/%d): %s",
                            eval_result.score, regen_attempts, _MAX_PRD_REGEN_ATTEMPTS,
                            eval_result.issues,
                        )

                        feedback = eval_result.feedback_for_regeneration
                        new_prd = self._regenerate_prd(task, prd_content, feedback)
                        if not new_prd:
                            self.logger.warning("PRD regeneration returned empty, keeping previous version")
                            break
                        prd_content = new_prd
                    else:
                        # Exhausted regen attempts — proceed anyway with a warning
                        self.logger.warning(
                            "PRD evaluation still failing after %d regen attempts, proceeding",
                            _MAX_PRD_REGEN_ATTEMPTS,
                        )

                # --- Save PRD to repo ---
                feature_slug = self._slugify(task.task_title)
                self.prd_repo.ensure_updated()
                try:
                    prd_path, prd_url = self.prd_repo.save_prd(
                        task.reminder_list, feature_slug, prd_content
                    )
                except Exception as e:
                    self._set_error(task, TaskError.PRD_SAVE_FAILED, f"Failed to save PRD: {e}")
                    raise

                task.prd_path = str(prd_path)
                task.prd_url = prd_url
                self.db.save_task(task)

                # Notify PRD ready
                if self.config.notify_on_prd_ready:
                    self.notifier.notify_prd_ready(task)
            else:
                # Quickfixes: just set up workspace, no PRD needed
                self._setup_workspace(task)

            # --- Step 5: Approval (if needed) ---
            if wf["needs_approval"]:
                if task.source == TaskSource.REMINDERS:
                    # Update the original reminder to show PRD review status
                    self._update_reminder_status(
                        task, STATUS_REVIEW_PRD,
                        notes=f"PRD: {task.prd_url}\n\nComplete to approve. Change prefix to [FIX] and add feedback in notes to request changes. Delete to reject."
                    )

                elif task.source == TaskSource.GITHUB_ISSUES and self.github_issues and task.github_issue_number:
                    comment = (
                        f"## PRD Generated\n\n"
                        f"I've analyzed the codebase and created a PRD for this issue:\n"
                        f"**[View PRD]({task.prd_url})**\n\n"
                        f"To approve implementation, add the `approved` label.\n"
                        f"To request changes, comment with feedback starting with `FIX:`"
                    )
                    self.github_issues.comment_on_issue(task.repo_url, task.github_issue_number, comment)
                    self.github_issues.add_label(task.repo_url, task.github_issue_number, "prd-ready")

                task = self.transition(task, TaskState.AWAITING_APPROVAL, f"PRD created: {task.prd_url}")
                return task

            # --- Step 6: No approval needed — go straight to agent ---
            return self._run_agent(task)

        except Exception as e:
            self.logger.error("Error processing task: %s", e)
            return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

    # -------------------------------------------------------------------------
    # Workspace setup (clone + branch)
    # -------------------------------------------------------------------------

    @staticmethod
    def _workspace_dir_name(repo_url: str, task_id: str) -> str:
        """Deterministic per-task workspace directory name.

        Format: repo_name-task_id_prefix.  The short ID suffix ensures
        concurrent tasks targeting the same repo get isolated directories
        instead of stomping on each other's uncommitted changes.
        """
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        return f"{repo_name}-{task_id[:8]}"

    def _setup_workspace(self, task: Task) -> None:
        """Clone/update repo and create feature branch.

        Mutates task in-place (sets local_repo_path and branch_name) and saves
        to DB.  Raises on failure so that concurrent.futures propagates the
        error.

        Each task gets its own workspace directory (repo_name-task_id[:8])
        to prevent concurrent tasks from corrupting each other's state.
        """
        local_repo = self.config.workspaces_path / self._workspace_dir_name(task.repo_url, task.id)

        # R4: collect branches belonging to other active tasks so cleanup
        # doesn't delete them.
        active_tasks = self.db.get_tasks_by_project(task.reminder_list)
        protected_branches = [
            t.branch_name for t in active_tasks
            if t.branch_name and t.id != task.id
        ]

        self._write_heartbeat()
        if not self.git.clone_or_update(task.repo_url, local_repo, protected_branches=protected_branches):
            self._set_error(task, TaskError.CLONE_FAILED, f"Failed to clone/update repo: {task.repo_url}")
            raise Exception(task.error_message)

        task.local_repo_path = str(local_repo)

        feature_slug = self._slugify(task.task_title)
        branch_name = f"porter/{feature_slug}"

        if not self.git.create_branch(local_repo, branch_name):
            self._set_error(task, TaskError.BRANCH_FAILED, f"Failed to create branch: {branch_name}")
            raise Exception(task.error_message)

        task.branch_name = branch_name
        self.db.save_task(task)

    def _setup_workspace_readonly(self, task: Task) -> None:
        """Clone/update repo for read-only analysis (no branch creation).

        Like _setup_workspace but skips branch creation since analysis
        tasks run on the default branch.
        """
        local_repo = self.config.workspaces_path / self._workspace_dir_name(task.repo_url, task.id)

        if not self.git.clone_or_update(task.repo_url, local_repo):
            self._set_error(task, TaskError.CLONE_FAILED, f"Failed to clone/update repo: {task.repo_url}")
            raise Exception(task.error_message)

        task.local_repo_path = str(local_repo)
        task.branch_name = "main"  # Analysis runs on default branch
        self.db.save_task(task)

    # -------------------------------------------------------------------------
    # PRD generation via subprocess (claude --print)
    # -------------------------------------------------------------------------

    def _generate_prd(
        self,
        task_title: str,
        task_notes: str,
        reminder_list: str,
        task: Optional[Task] = None,
    ) -> Optional[str]:
        """Generate a PRD using claude --print (stateless subprocess call).

        R9: accepts explicit params so it can run in a thread without
        accessing the shared mutable Task object.  Falls back to
        workspaces_path as cwd since the PRD prompt is self-contained.
        When called outside parallel context, pass task= for backward compat.
        """
        repo_path = self.config.workspaces_path
        if task and getattr(task, "local_repo_path", None):
            repo_path = Path(task.local_repo_path)
        custom_template = self.config.get_project_prd_template(reminder_list)

        # Build a lightweight Task-like for _build_prd_prompt
        class _PrdCtx:
            pass
        ctx = _PrdCtx()
        ctx.task_title = task_title
        ctx.task_notes = task_notes
        prompt = self._build_prd_prompt(ctx, custom_template)

        cmd = [
            self.config.claude_bin,
            "--print",
            "--model", "sonnet",
            "--permission-mode", self.config.claude_permission_mode,
            "-p", prompt,
        ]

        self.logger.info("Generating PRD for: %s", task_title)
        self._write_heartbeat()

        # Strip CLAUDECODE env var to avoid nested-session detection
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.claude_timeout,
                cwd=str(repo_path),
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.logger.error("PRD generation timed out after %ds", self.config.claude_timeout)
            return None

        if proc.returncode != 0:
            self.logger.error("PRD generation failed (exit %d): %s", proc.returncode, proc.stderr[:500] if proc.stderr else "")
            return None

        content = proc.stdout.strip()
        if not content:
            self.logger.error("PRD generation returned empty output")
            return None

        self.logger.info("PRD generated successfully (%d chars)", len(content))
        return content

    def _build_prd_prompt(self, task: Task, custom_template: Optional[str] = None) -> str:
        """Build the prompt for PRD generation."""
        notes_section = f"\n\nAdditional context:\n{task.task_notes}" if task.task_notes else ""

        if custom_template:
            try:
                template = Path(custom_template).read_text()
                return (
                    f"Generate a PRD for the following task using the template below.\n\n"
                    f"Task: {task.task_title}{notes_section}\n\n"
                    f"Template:\n{template}"
                )
            except Exception:
                self.logger.warning("Custom PRD template not found: %s, using default", custom_template)

        return (
            f"You are a senior product engineer. Analyze the codebase and generate a detailed PRD "
            f"(Product Requirements Document) for the following task.\n\n"
            f"Task: {task.task_title}{notes_section}\n\n"
            f"The PRD should include:\n"
            f"1. Overview — what the feature/change does and why\n"
            f"2. Technical approach — specific files, components, and patterns to use\n"
            f"3. Acceptance criteria — specific, testable requirements\n"
            f"4. Edge cases and error handling\n"
            f"5. Testing strategy\n\n"
            f"Be specific about file paths, function signatures, and implementation details. "
            f"A developer reading this PRD should know exactly what to build."
        )

    # -------------------------------------------------------------------------
    # PRD regeneration with evaluator feedback
    # -------------------------------------------------------------------------

    def _regenerate_prd(self, task: Task, prd_content: str, feedback: str) -> Optional[str]:
        """Regenerate the PRD incorporating evaluator feedback."""
        repo_path = Path(task.local_repo_path)

        prompt = (
            f"You previously generated a PRD for the task: {task.task_title}\n\n"
            f"Here is the current PRD:\n\n---\n{prd_content}\n---\n\n"
            f"The PRD was evaluated and needs improvements:\n\n{feedback}\n\n"
            f"Please regenerate the complete PRD addressing ALL the issues above. "
            f"Output the full updated PRD."
        )

        cmd = [
            self.config.claude_bin,
            "--print",
            "--model", "sonnet",
            "--permission-mode", self.config.claude_permission_mode,
            "-p", prompt,
        ]

        self.logger.info("Regenerating PRD with evaluator feedback for: %s", task.task_title)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.claude_timeout,
                cwd=str(repo_path),
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.logger.error("PRD regeneration timed out")
            return None

        if proc.returncode != 0:
            self.logger.error("PRD regeneration failed (exit %d)", proc.returncode)
            return None

        content = proc.stdout.strip()
        return content if content else None

    # -------------------------------------------------------------------------
    # Analysis — read-only codebase exploration
    # -------------------------------------------------------------------------

    def _run_analysis(self, task: Task) -> Task:
        """Run a read-only analysis task.

        No branch, no push, no PR. Runs agent on main branch,
        sends results via iMessage, transitions to COMPLETE.
        """
        repo_path = Path(task.local_repo_path)

        task.implementation_started_at = datetime.now()
        task = self.transition(task, TaskState.AGENT_RUNNING, "Running analysis")
        self.notifier.notify_work_starting(task)

        try:
            task_type = task.task_type or "analysis"
            agent = self._agent_for_task_type(task_type)

            prompt = agent.build_prompt(
                task_type=task_type,
                prd_content=None,
                branch_name="main",  # Analysis runs on main
                task_title=task.task_title,
                task_notes=task.task_notes,
            )

            result = agent.run(
                prompt=prompt,
                repo_path=repo_path,
                task_id=task.id,
                task_type=task_type,
            )

            task.agent_session_id = result.session_id
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            task.implementation_finished_at = datetime.now()

            if result.question:
                return self._create_question_reminder(task, result.question)

            if not result.success:
                error_msg = result.error or "Analysis failed"
                self._set_error(task, TaskError.IMPLEMENTATION_FAILED, error_msg)
                return self._handle_failure(task, TaskError.IMPLEMENTATION_FAILED, error_msg)

            # F2: write full report to file, send summary via iMessage
            report = result.result_text or "Analysis completed (no output)"
            report_path = self.config.logs_path / f"analysis-{task.id[:8]}.md"
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    f"# Analysis: {task.task_title}\n\n{report}"
                )
                self.logger.info("Analysis report saved: %s", report_path)
            except OSError as e:
                self.logger.warning("Failed to save analysis report: %s", e)

            summary = report[:500] + "..." if len(report) > 500 else report
            self.notifier.send(
                f"{self.notifier._project_prefix('verification_passed', task)}"
                f"Analysis complete for '{task.task_title}':\n\n{summary}"
                f"\n\nFull report: {report_path}"
            )

            task = self.transition(task, TaskState.COMPLETE, "Analysis delivered")
            return task

        except Exception as e:
            self.logger.error("Error in analysis: %s", e)
            self._set_error(task, TaskError.UNKNOWN, str(e))
            return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

    # -------------------------------------------------------------------------
    # Composite task — decompose into sub-tasks
    # -------------------------------------------------------------------------

    def _parse_subtasks(self, text: str) -> list[tuple[str, str]]:
        """Parse SUBTASK:/DESCRIPTION: pairs from agent output.

        Returns list of (title, description) tuples.
        """
        subtasks = []
        lines = text.splitlines()
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

    def _run_composite(self, task: Task) -> Task:
        """Run a composite task: explore, decompose, create sub-task Reminders.

        The agent identifies sub-tasks and outputs them in SUBTASK:/DESCRIPTION: format.
        We parse the output and create new Reminders for each sub-task.
        """
        repo_path = Path(task.local_repo_path)

        task.implementation_started_at = datetime.now()
        task = self.transition(task, TaskState.AGENT_RUNNING, "Decomposing task")
        self.notifier.notify_work_starting(task)

        try:
            task_type = task.task_type or "composite"
            agent = self._agent_for_task_type(task_type)

            prompt = agent.build_prompt(
                task_type=task_type,
                prd_content=None,
                branch_name="main",
                task_title=task.task_title,
                task_notes=task.task_notes,
            )

            result = agent.run(
                prompt=prompt,
                repo_path=repo_path,
                task_id=task.id,
                task_type=task_type,
            )

            task.agent_session_id = result.session_id
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            task.implementation_finished_at = datetime.now()

            if result.question:
                return self._create_question_reminder(task, result.question)

            if not result.success:
                error_msg = result.error or "Decomposition failed"
                self._set_error(task, TaskError.IMPLEMENTATION_FAILED, error_msg)
                return self._handle_failure(task, TaskError.IMPLEMENTATION_FAILED, error_msg)

            # Parse sub-tasks from agent output
            subtasks = self._parse_subtasks(result.result_text or "")

            if not subtasks:
                self.logger.warning("No sub-tasks found in composite output")
                self.notifier.send(
                    f"{self.notifier._project_prefix('error', task)}"
                    f"Could not decompose '{task.task_title}' -- no sub-tasks identified"
                )
                task = self.transition(task, TaskState.COMPLETE, "No sub-tasks found")
                return task

            # Create Reminders for each sub-task
            created_count = 0
            for title, description in subtasks:
                reminder_title = f"Task: {title}"
                success = self.reminders.add_reminder(
                    task.reminder_list, reminder_title, notes=description
                )
                if success:
                    created_count += 1
                    self.logger.info("Created sub-task reminder: %s", title)
                else:
                    self.logger.warning("Failed to create sub-task reminder: %s", title)

            # Notify results
            self.notifier.send(
                f"{self.notifier._project_prefix('pr_created', task)}"
                f"Decomposed '{task.task_title}' into {created_count} sub-tasks"
            )

            task = self.transition(task, TaskState.COMPLETE,
                                  f"Decomposed into {created_count} sub-tasks")
            return task

        except Exception as e:
            self.logger.error("Error in composite task: %s", e)
            self._set_error(task, TaskError.UNKNOWN, str(e))
            return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

    # -------------------------------------------------------------------------
    # Agent runner — the v4 implementation path
    # -------------------------------------------------------------------------

    def _run_agent(self, task: Task) -> Task:
        """Launch Agent SDK session for implementation.

        1. Check if previous agent work exists (skip agent on PR-only retry)
        2. Build prompt via agent.build_prompt()
        3. Run via agent.run()
        4. Handle questions, failures
        5. Verify commits exist
        6. Run external test verification if configured
        7. Push + create PR
        """
        repo_path = Path(task.local_repo_path)

        # If a previous agent session already produced commits (e.g. PR creation
        # failed but implementation succeeded), skip straight to push+PR.
        if task.error_type == TaskError.PR_CREATION_FAILED:
            self.git.ensure_on_branch(repo_path, task.branch_name)
            has_commits, commit_count = self.git.has_commits_ahead(repo_path, task.branch_name)
            if has_commits:
                self.logger.info(
                    "Skipping agent session — previous run has %d commits, retrying PR creation only",
                    commit_count,
                )
                task.error_type = None
                task.error_message = None
                task = self.transition(task, TaskState.AGENT_RUNNING, "Retrying PR creation (agent work preserved)")
                try:
                    return self._push_and_create_pr(task, commit_count)
                except Exception as pr_err:
                    self.logger.error("PR creation retry failed: %s", pr_err)
                    self._set_error(task, TaskError.PR_CREATION_FAILED, str(pr_err))
                    return self._handle_failure(task, TaskError.PR_CREATION_FAILED, str(pr_err))

        task.implementation_started_at = datetime.now()
        # F8: clear dispatch queue flag — agent is now actively running
        task.queued_for_dispatch = False
        task = self.transition(task, TaskState.AGENT_RUNNING, "Starting Agent SDK session")
        self.db.save_task(task)
        self._update_reminder_status(task, STATUS_WORKING)
        self.notifier.notify_work_starting(task)

        def _on_progress(activity: str, turn: int) -> None:
            self.logger.info("[turn %d] %s", turn, activity)
            # Update heartbeat so healthcheck knows we're alive during long runs
            heartbeat_path = self.config.logs_path / "heartbeat"
            try:
                heartbeat_path.write_text(datetime.now().isoformat())
            except OSError:
                pass

        try:
            # Ensure on correct branch
            if not self.git.ensure_on_branch(repo_path, task.branch_name):
                self._set_error(task, TaskError.BRANCH_FAILED, f"Failed to checkout branch: {task.branch_name}")
                raise Exception(task.error_message)

            # Read PRD content (if exists)
            prd_content: Optional[str] = None
            if task.prd_path:
                try:
                    prd_content = Path(task.prd_path).read_text()
                except FileNotFoundError:
                    self.logger.warning("PRD file not found: %s", task.prd_path)

            # Determine attempt number for retry context
            attempt = 1
            if task.last_implementation_output:
                attempt = task.retry_count + 1

            # Build and run prompt
            task_type = task.task_type or "feature"
            agent = self._agent_for_task_type(task_type)

            # Determine if we can resume a previous session that crashed or
            # timed out (as opposed to a logic failure that needs a fresh
            # start).  Resumable errors are timeout and generic crashes —
            # not logic-level failures like PR_CREATION_FAILED which are
            # handled separately above.
            _RESUMABLE_ERRORS = {TaskError.IMPLEMENTATION_FAILED, TaskError.TIMEOUT}
            can_resume = (
                task.agent_session_id
                and task.error_type in _RESUMABLE_ERRORS
            )

            # F4: build cancellation check for in-flight task cancellation
            cancel_check = self._make_cancellation_check(task)

            if can_resume:
                self.logger.info(
                    "Resuming previous agent session %s (error_type=%s)",
                    task.agent_session_id, task.error_type,
                )
                # Prompt is ignored when resuming — the SDK uses the
                # existing conversation from the session.
                result = agent.run(
                    prompt="",
                    repo_path=repo_path,
                    task_id=task.id,
                    task_type=task_type,
                    attempt=attempt,
                    resume_session_id=task.agent_session_id,
                    progress_callback=_on_progress,
                    cancellation_check=cancel_check,
                )
            else:
                prompt = agent.build_prompt(
                    task_type=task_type,
                    prd_content=prd_content,
                    branch_name=task.branch_name,
                    task_title=task.task_title,
                    task_notes=task.task_notes,
                    previous_output=task.last_implementation_output,
                    attempt=attempt,
                )

                result = agent.run(
                    prompt=prompt,
                    repo_path=repo_path,
                    task_id=task.id,
                    task_type=task_type,
                    attempt=attempt,
                    progress_callback=_on_progress,
                    cancellation_check=cancel_check,
                )

            # Store result for potential retry
            if result.result_text:
                tail = result.result_text[-8000:] if len(result.result_text) > 8000 else result.result_text
                task.last_implementation_output = tail
            task.agent_session_id = result.session_id
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            self.db.save_task(task)

            # Check for questions
            if result.question:
                return self._create_question_reminder(task, result.question)

            # F4: handle cancellation
            if result.stop_reason == "cancelled":
                self.logger.info("Task '%s' was cancelled by user", task.task_title)
                task = self.transition(task, TaskState.COMPLETE, "Cancelled by user")
                return task

            # Check for failure
            if not result.success:
                error_msg = result.error or f"Agent session failed (stop_reason={result.stop_reason})"
                if result.timed_out:
                    error_msg = f"Agent session timed out after {result.duration_seconds:.0f}s"
                    self._set_error(task, TaskError.TIMEOUT, error_msg)
                elif result.stop_reason == "budget_exceeded":
                    error_msg = f"Budget exceeded: {result.error}"
                    self._set_error(task, TaskError.BUDGET_EXCEEDED, error_msg)
                else:
                    self._set_error(task, TaskError.IMPLEMENTATION_FAILED, error_msg)
                raise Exception(task.error_message)

            # Verify we have commits
            git_status = self.git.get_status(repo_path)
            has_commits, commit_count = self.git.has_commits_ahead(repo_path, task.branch_name)

            if not has_commits:
                # Check for uncommitted changes
                if git_status.get("has_uncommitted"):
                    self.logger.warning("Found uncommitted changes, committing them")
                    self.git.commit_all(repo_path, f"[Porter] Complete implementation for {task.task_title}")
                    has_commits, commit_count = self.git.has_commits_ahead(repo_path, task.branch_name)

                if not has_commits:
                    self._set_error(task, TaskError.IMPLEMENTATION_FAILED, "No commits were made during implementation")
                    raise Exception(task.error_message)

            self.logger.info("Agent session complete: %d commits in %.0fs", commit_count, result.duration_seconds)

            # --- External test verification (safety net) ---
            test_warning: Optional[str] = None
            if self.config.run_tests_before_pr:
                custom_test_cmd = self.config.get_project_test_command(task.reminder_list)
                test_passed, test_output = self.test_runner.run_tests(repo_path, custom_test_cmd)
                task.last_test_output = test_output[:2000] if test_output else None

                if test_passed:
                    self.logger.info("External test verification passed")
                    self.notifier.notify_tests_passed(task)
                else:
                    self.logger.warning("External tests failed — will create PR with test-warning label")
                    self.notifier.notify_tests_failed(task)
                    test_warning = test_output[:1000] if test_output else "Tests failed (no output)"

            # --- Push + create PR ---
            # Separated from the agent exception handler so that PR creation
            # failures don't trigger an agent retry (the implementation is done).
            try:
                return self._push_and_create_pr(task, commit_count, test_warning=test_warning)
            except Exception as pr_err:
                self.logger.error("PR creation failed (agent work preserved): %s", pr_err)
                if not task.error_type:
                    self._set_error(task, TaskError.PR_CREATION_FAILED, str(pr_err))
                return self._handle_failure(task, TaskError.PR_CREATION_FAILED, str(pr_err))

        except Exception as e:
            self.logger.error("Error in agent run: %s", e)
            if not task.error_type:
                self._set_error(task, TaskError.UNKNOWN, str(e))
            return self._handle_failure(task, task.error_type or TaskError.UNKNOWN, str(e))

    # -------------------------------------------------------------------------
    # Push + PR creation
    # -------------------------------------------------------------------------

    def _push_and_create_pr(
        self,
        task: Task,
        commit_count: int,
        test_warning: Optional[str] = None,
    ) -> Task:
        """Push branch and create a pull request."""
        repo_path = Path(task.local_repo_path)
        self._write_heartbeat()

        # Push
        push_success, push_error = self.git.push(repo_path, task.branch_name)
        if not push_success:
            self._set_error(task, TaskError.PUSH_FAILED, f"Failed to push branch: {push_error}")
            raise Exception(task.error_message)

        self.logger.info("Pushed to origin/%s", task.branch_name)

        task.implementation_finished_at = datetime.now()
        duration = task.implementation_finished_at - task.implementation_started_at

        # Build PR body
        pr_title = f"[Porter] {task.task_title}"

        prd_line = f"\n**PRD:** {task.prd_url}\n" if task.prd_url else ""
        test_warning_section = ""
        if test_warning:
            test_warning_section = (
                f"\n## Test Warning\n\n"
                f"External test verification **failed**. Please review:\n\n"
                f"```\n{test_warning}\n```\n"
            )

        pr_body = (
            f"## Summary\n\n"
            f"Automated implementation by Porter.\n\n"
            f"**Task:** {task.task_title}\n"
            f"{prd_line}"
            f"**Task type:** {task.task_type}\n"
            f"**Commits:** {commit_count}\n"
            f"**Duration:** {duration.total_seconds() / 60:.1f} minutes\n"
            f"{'**Tokens:** ' + f'{task.input_tokens:,} in / {task.output_tokens:,} out' + chr(10) if task.input_tokens or task.output_tokens else ''}"
            f"{'**Est. cost:** $' + f'{task.input_tokens * 5.0 / 1_000_000 + task.output_tokens * 25.0 / 1_000_000:.2f}' + chr(10) if task.input_tokens or task.output_tokens else ''}"
            f"{test_warning_section}"
            f"\n## Notes\n\n"
            f"{task.task_notes if task.task_notes else 'No additional notes.'}\n\n"
            f"---\n*This PR was automatically generated by Porter*"
        )

        pr_url, pr_error = self.git.create_pr(repo_path, pr_title, pr_body)
        if not pr_url:
            self._set_error(task, TaskError.PR_CREATION_FAILED, f"Failed to create PR: {pr_error}")
            raise Exception(task.error_message)

        task.pr_url = pr_url

        # Extract PR number
        pr_match = re.search(r'/pull/(\d+)', pr_url)
        if pr_match:
            task.pr_number = int(pr_match.group(1))

        # Add test-warning label if tests failed
        if test_warning and task.pr_number and self.github_issues:
            try:
                self.github_issues.add_label(task.repo_url, task.pr_number, "test-warning")
            except Exception:
                self.logger.warning("Failed to add test-warning label to PR")

        task = self.transition(task, TaskState.PR_CREATED, f"PR created: {pr_url}")

        # Update reminder to show PR status
        pr_prefix = STATUS_PR.format(pr_number=task.pr_number or "?")
        self._update_reminder_status(task, pr_prefix, notes=f"PR: {task.pr_url}")

        # Close GitHub issue if applicable
        if task.source == TaskSource.GITHUB_ISSUES and self.github_issues and task.github_issue_number:
            comment = f"Implementation complete! PR: {pr_url}"
            self.github_issues.comment_on_issue(task.repo_url, task.github_issue_number, comment)

        # Route to next state
        if self.config.auto_respond_to_reviews:
            task = self.transition(task, TaskState.PR_REVIEW_PENDING, "Monitoring for PR reviews")
        else:
            task = self._create_merge_reminder(task)

        # Notify
        self._notify_pr_url(task, updated=False, notify=self.config.notify_on_pr_created)

        return task

    # -------------------------------------------------------------------------
    # Failure handling with retry
    # -------------------------------------------------------------------------

    # Error types that should NOT be retried
    _NO_RETRY_ERRORS = {TaskError.BUDGET_EXCEEDED}

    def _handle_failure(self, task: Task, error_type: TaskError, message: str) -> Task:
        """Handle failure with exponential backoff retry or transition to ERROR.

        Instead of retrying recursively (which burns all retries instantly),
        we schedule the retry via next_retry_at and transition back to NEW.
        The next poll cycle picks it up after the backoff has elapsed.
        """
        if not task.error_type:
            self._set_error(task, error_type, message)

        # Some errors are terminal — no point retrying
        if error_type in self._NO_RETRY_ERRORS:
            task = self.transition(task, TaskState.ERROR, message)
            if self.config.notify_on_error:
                self.notifier.notify_error(task, self.config.retry_max)
            return task

        task.retry_count += 1

        if task.retry_count >= self.config.retry_max:
            task = self.transition(task, TaskState.ERROR, message)
            if self.config.notify_on_error:
                self.notifier.notify_error(task, self.config.retry_max)
        else:
            # Schedule retry with exponential backoff — let the poll cycle
            # pick it up after the backoff elapses.  Do NOT call
            # process_new_task() recursively — that burns all retries
            # instantly with zero breathing room.
            self._prepare_for_retry(task)
            task.next_retry_at = self._calculate_backoff(task.retry_count)
            retry_prefix = STATUS_RETRY.format(n=task.retry_count, max=self.config.retry_max)
            self._update_reminder_status(task, retry_prefix)
            self.notifier.notify_retry(task)

            self.logger.info(
                "Scheduling retry for '%s' at %s (attempt %d/%d, backoff %s)",
                task.task_title,
                task.next_retry_at.strftime("%H:%M:%S"),
                task.retry_count + 1,
                self.config.retry_max,
                task.next_retry_at - datetime.now(),
            )
            # Transition back to NEW — poll cycle will pick it up when
            # next_retry_at has elapsed.
            task.error_type = None
            task.error_message = None
            self.db.save_task(task)
            task = self.transition(task, TaskState.NEW, f"Retry scheduled at {task.next_retry_at.strftime('%H:%M')}")

        return task

    def _prepare_for_retry(self, task: Task) -> None:
        """Prepare workspace for a retry attempt."""
        try:
            if not task.local_repo_path:
                return
            repo_path = Path(task.local_repo_path)

            has_commits, _ = self.git.has_commits_ahead(repo_path, task.branch_name or "main")
            if has_commits:
                self.logger.info("Retry: found unpushed commits, keeping workspace")
                return

            self.logger.info("Retry: resetting workspace for fresh attempt")
            self.git.clone_or_update(task.repo_url, repo_path)
        except Exception as e:
            self.logger.warning("Failed to prepare for retry: %s", e)

    # -------------------------------------------------------------------------
    # Approval status check
    # -------------------------------------------------------------------------

    def check_approval_status(self, task: Task) -> Task:
        """Check if a task awaiting approval has been approved, needs fixes, or was rejected.

        Detection via the original reminder:
        - Completed → approved
        - Deleted → rejected
        - Title changed to [FIX] → feedback in notes
        """
        # GitHub Issues path
        if task.source == TaskSource.GITHUB_ISSUES:
            return self._check_github_approval(task)

        if not task.reminder_id:
            return task

        # Find the original reminder and check its state
        reminder_data = self._find_task_reminder(task)

        if reminder_data is self._REMINDER_LOOKUP_ERROR:
            # Reminders CLI failed — do NOT treat as deletion.  Skip and retry next cycle.
            self.logger.info("Skipping approval check for '%s' — Reminders CLI unavailable", task.task_title)
            return task

        if reminder_data is None:
            # Reminder was genuinely deleted — rejected
            self.logger.info("PRD rejected (reminder deleted): %s", task.task_title)
            self._record_eval_human_feedback(task, human_approved=False)
            return self.transition(task, TaskState.REJECTED, "Reminder deleted")

        title = reminder_data.get("title", "")
        is_completed = reminder_data.get("isCompleted", False)

        if is_completed:
            # Approved — user completed the reminder
            self.logger.info("PRD approved: %s", task.task_title)
            self._record_eval_human_feedback(task, human_approved=True)
            task.queued_for_dispatch = True
            self.db.save_task(task)
            return self.transition(task, TaskState.AGENT_RUNNING, "PRD approved, queued for implementation")

        # Check if user changed prefix to [FIX]
        if title.startswith(USER_FIX):
            feedback = reminder_data.get("notes", "").strip() or "No feedback provided."
            self.logger.info("PRD feedback received for: %s", task.task_title)
            self.notifier.notify_fix_received(task, feedback)
            return self._regenerate_prd_with_user_feedback(task, feedback)

        # Check for FIX: feedback
        fix_reminders = self.reminders.find_reminder_by_prefix_icase(task.reminder_list, "fix:")
        if fix_reminders:
            for idx, fix_reminder in fix_reminders:
                fix_title = fix_reminder.get("title", "")
                token = self._extract_id_token(fix_title)

                notes = None
                if not token and self.config.notes_fetch_mode != "json_only":
                    notes = self.reminders.get_notes_via_applescript(task.reminder_list, fix_title)
                    token = self._extract_id_token(notes or "")

                if token and not self._token_matches_task(task, token):
                    continue

                feedback = self._extract_fix_feedback(fix_title, notes)
                if feedback == "No feedback provided." and self.config.notes_fetch_mode != "json_only":
                    notes = notes or self.reminders.get_notes_via_applescript(task.reminder_list, fix_title)
                    feedback = self._extract_fix_feedback(fix_title, notes)

                self.logger.info("PRD feedback received for: %s", task.task_title)
                self.logger.info("Feedback: %s", feedback)
                self.notifier.notify_fix_received(task, feedback)

                # Complete the FIX reminder
                fix_external_id = fix_reminder.get("externalId")
                if fix_external_id:
                    self.reminders.complete_by_external_id(task.reminder_list, fix_external_id)
                else:
                    self.reminders.complete_by_index(task.reminder_list, idx)

                return self._regenerate_prd_with_user_feedback(task, feedback)

        # Check for timeout
        age = datetime.now() - task.updated_at
        if age > timedelta(days=self.config.approval_timeout_days):
            self.logger.info("PRD approval timed out: %s", task.task_title)
            return self.transition(task, TaskState.REJECTED, "Approval timeout")

        # Check for stale approval
        if task.updated_at:
            age_days = (datetime.now() - task.updated_at).days
            if age_days >= self.config.approval_timeout_days:
                self.notifier.notify_approval_stale(task, age_days)

        return task

    def _check_github_approval(self, task: Task) -> Task:
        """Handle approval flow for GitHub Issues sourced tasks."""
        if not self.github_issues or not task.github_issue_number:
            return task

        labels = self.github_issues.get_issue_labels(task.repo_url, task.github_issue_number)
        label_names = [l.lower() for l in labels]

        if "approved" in label_names:
            self.logger.info("GitHub issue approved: %s", task.task_title)
            self._record_eval_human_feedback(task, human_approved=True)
            task.queued_for_dispatch = True
            self.db.save_task(task)
            return self.transition(task, TaskState.AGENT_RUNNING, "PRD approved, queued for implementation")

        # Check for FIX feedback comments
        comments = self.github_issues.get_issue_comments(task.repo_url, task.github_issue_number)
        fix_comments = []
        for comment in comments:
            body = comment.get("body", "")
            match = re.search(r'(?is)^\s*fix:\s*(.+)$', body.strip())
            if match:
                fix_comments.append((comment, match.group(1).strip()))

        if fix_comments:
            fix_comments.sort(key=lambda item: item[0].get("id", 0))
            comment, feedback = fix_comments[-1]
            comment_id = comment.get("id")

            if comment_id and task.last_fix_comment_id and comment_id <= task.last_fix_comment_id:
                return task

            self.logger.info("PRD feedback received from GitHub for: %s", task.task_title)
            self.notifier.notify_fix_received(task, feedback)

            task.last_fix_comment_id = comment_id
            self.db.save_task(task)

            return self._regenerate_prd_with_user_feedback(task, feedback)

        return task

    def _regenerate_prd_with_user_feedback(self, task: Task, feedback: str) -> Task:
        """Regenerate PRD incorporating user feedback (from approval flow)."""
        self.logger.info("Regenerating PRD with user feedback for: %s", task.task_title)

        if self.dry_run:
            self.logger.info("[DRY RUN] Would regenerate PRD, skipping")
            return task

        try:
            current_prd = Path(task.prd_path).read_text() if task.prd_path else ""
            new_prd = self._regenerate_prd(task, current_prd, feedback)

            if not new_prd:
                raise Exception("Failed to regenerate PRD")

            self.prd_repo.ensure_updated()
            feature_slug = self._slugify(task.task_title)
            prd_path, prd_url = self.prd_repo.save_prd(
                task.reminder_list, feature_slug, new_prd
            )

            task.prd_path = str(prd_path)
            task.prd_url = prd_url
            task.updated_at = datetime.now()
            self.db.save_task(task)

            self.logger.info("PRD regenerated: %s", prd_url)

            if self.config.notify_on_prd_ready:
                self.notifier.notify_prd_updated(task)

            if task.source == TaskSource.GITHUB_ISSUES and self.github_issues and task.github_issue_number:
                comment = (
                    f"## PRD Updated\n\n"
                    f"I've updated the PRD based on your feedback:\n"
                    f"**[View PRD]({prd_url})**\n\n"
                    f"To approve implementation, add the `approved` label."
                )
                self.github_issues.comment_on_issue(task.repo_url, task.github_issue_number, comment)

            return task

        except Exception as e:
            self.logger.error("Error regenerating PRD: %s", e)
            task.error_message = str(e)
            self.db.save_task(task)
            return task

    # -------------------------------------------------------------------------
    # Question handling
    # -------------------------------------------------------------------------

    def _create_question_reminder(self, task: Task, question: str) -> Task:
        """Update the task reminder to show a question and transition to NEEDS_CLARIFICATION."""
        self._update_reminder_status(
            task, STATUS_QUESTION,
            notes=f"{question}\n\nTo answer: change the title prefix to [ANSWER] and put your answer in the notes."
        )

        task.question_text = question
        task = self.transition(task, TaskState.NEEDS_CLARIFICATION, f"Question: {question[:100]}")

        self.notifier.notify_question(task)
        return task

    def check_question_answered(self, task: Task) -> Task:
        """Check if the user changed the reminder prefix to [ANSWER]."""
        if not task.reminder_id:
            return task

        reminder_data = self._find_task_reminder(task)
        if reminder_data is self._REMINDER_LOOKUP_ERROR or reminder_data is None:
            return task

        title = reminder_data.get("title", "")
        notes = reminder_data.get("notes", "")

        # Check if user changed prefix to [ANSWER]
        if title.startswith(USER_ANSWER):
            answer = notes.strip() if notes else "No answer provided."
            self.logger.info("Question answered for: %s", task.task_title)

            # Add answer context and resume
            task.task_notes = f"{task.task_notes}\n\nQ: {task.question_text}\nA: {answer}"
            task.question_text = None
            task.queued_for_dispatch = True

            self.db.save_task(task)
            self._update_reminder_status(task, STATUS_QUEUED)
            return self.transition(task, TaskState.AGENT_RUNNING, "Question answered, queued for implementation")

        # Check for stale question
        if task.updated_at:
            age_days = (datetime.now() - task.updated_at).days
            question_timeout = getattr(self.config, 'question_timeout_days', self.config.approval_timeout_days)
            if age_days >= question_timeout:
                self.notifier.notify_question_stale(task, age_days)

        return task

    @staticmethod
    def _extract_answer_from_notes(notes: str) -> Optional[str]:
        """Extract answer text from question reminder notes."""
        if not notes:
            return None
        match = re.search(r"ANSWER:\s*(.+)", notes, re.DOTALL)
        if match:
            answer = match.group(1).strip()
            if answer:
                return answer
        return None

    # -------------------------------------------------------------------------
    # PR review handling
    # -------------------------------------------------------------------------

    def check_pr_reviews(self, task: Task) -> Task:
        """Check for PR review comments and dispatch agent to respond.

        For GitHub-sourced tasks: fetches PR review comments via API.
        For Reminders-sourced tasks: checks for Fix: reminders targeting this task.
        """
        if task.source == TaskSource.REMINDERS:
            return self._check_reminders_pr_feedback(task)

        if not task.pr_number or not self.github_issues:
            return task

        self.logger.info("Checking PR reviews for: %s", task.task_title)

        try:
            comments = self.github_issues.get_pr_review_comments(task.repo_url, task.pr_number)
            if task.last_review_comment_id:
                comments = [c for c in comments if c.get("id", 0) > task.last_review_comment_id]

            if not comments:
                if task.state == TaskState.PR_REVIEW_PENDING:
                    self.logger.info("No pending review comments, proceeding to merge")
                    if self.config.auto_merge_on_review_complete:
                        return self.process_merge(task)
                    else:
                        return self._create_merge_reminder(task)
                return task

            self.logger.info("Found %d review comments to address", len(comments))
            self.notifier.notify_pr_review_pending(task)

            if self.dry_run:
                self.logger.info("[DRY RUN] Would address PR reviews, skipping")
                return task

            repo_path = Path(task.local_repo_path)

            if not self.git.ensure_on_branch(repo_path, task.branch_name):
                raise Exception(f"Failed to checkout branch for PR review: {task.branch_name}")

            # Build a review-response prompt and run agent
            review_text = "\n\n".join(
                f"File: {c.get('path', '?')} Line: {c.get('line', '?')}\n{c.get('body', '')}"
                for c in comments
            )
            prompt = (
                f"You are responding to PR review comments on branch `{task.branch_name}`.\n\n"
                f"## Review Comments\n\n{review_text}\n\n"
                f"## Instructions\n"
                f"1. Read each review comment and understand what the reviewer is asking.\n"
                f"2. Make the requested changes.\n"
                f"3. Commit your changes with descriptive messages.\n"
                f"Do NOT push — commit locally only.\n"
            )

            def _on_review_progress(activity: str, turn: int) -> None:
                self.logger.info("[review turn %d] %s", turn, activity)

            agent = self._agent_for_task_type(task.task_type or "bugfix")
            result = agent.run(
                prompt=prompt,
                repo_path=repo_path,
                task_id=task.id,
                task_type=task.task_type or "bugfix",
                attempt=1,
                progress_callback=_on_review_progress,
            )

            if result.success:
                has_commits, commit_count = self.git.has_commits_ahead(repo_path, task.branch_name)
                if has_commits:
                    push_success, push_error = self.git.push(repo_path, task.branch_name)
                    if push_success:
                        self.logger.info("Pushed %d review fix commits", commit_count)
                        self._notify_pr_url(task, updated=True, notify=self.config.notify_on_pr_created)
                    else:
                        self.logger.warning("Failed to push review fixes: %s", push_error)

            # Track processed comment IDs
            max_comment_id = max((c.get("id", 0) for c in comments), default=0)
            if max_comment_id:
                task.last_review_comment_id = max_comment_id
                self.db.save_task(task)

            task = self.transition(task, TaskState.PR_REVIEW_PENDING, f"Addressed {len(comments)} review comments")
            return task

        except Exception as e:
            self.logger.error("Error addressing PR reviews: %s", e)
            if not task.error_type:
                self._set_error(task, TaskError.PR_REVIEW_FAILED, str(e))
            return task

    def _check_reminders_pr_feedback(self, task: Task) -> Task:
        """Check for Fix: reminders targeting this task's PR."""
        fix_reminders = self.reminders.find_reminder_by_prefix_icase(task.reminder_list, "fix:")
        if not fix_reminders:
            return task

        for idx, fix_reminder in fix_reminders:
            fix_title = fix_reminder.get("title", "")
            token = self._extract_id_token(fix_title)

            notes = None
            if not token and self.config.notes_fetch_mode != "json_only":
                notes = self.reminders.get_notes_via_applescript(task.reminder_list, fix_title)
                token = self._extract_id_token(notes or "")

            if token and not self._token_matches_task(task, token):
                continue

            feedback = self._extract_fix_feedback(fix_title, notes)
            if feedback == "No feedback provided." and self.config.notes_fetch_mode != "json_only":
                notes = notes or self.reminders.get_notes_via_applescript(task.reminder_list, fix_title)
                feedback = self._extract_fix_feedback(fix_title, notes)

            self.logger.info("PR feedback received for: %s", task.task_title)
            self.notifier.notify_fix_received(task, feedback)

            # Complete the FIX reminder
            fix_external_id = fix_reminder.get("externalId")
            if fix_external_id:
                self.reminders.complete_by_external_id(task.reminder_list, fix_external_id)
            else:
                self.reminders.complete_by_index(task.reminder_list, idx)

            # Run agent to address feedback
            try:
                repo_path = Path(task.local_repo_path)
                if not self.git.ensure_on_branch(repo_path, task.branch_name):
                    raise Exception(f"Failed to checkout branch: {task.branch_name}")

                prompt = (
                    f"You are addressing PR feedback on branch `{task.branch_name}`.\n\n"
                    f"## Feedback\n\n{feedback}\n\n"
                    f"## Instructions\n"
                    f"1. Understand the feedback.\n"
                    f"2. Make the requested changes.\n"
                    f"3. Commit with descriptive messages.\n"
                    f"Do NOT push — commit locally only.\n"
                )

                def _on_fix_progress(activity: str, turn: int) -> None:
                    self.logger.info("[fix turn %d] %s", turn, activity)

                agent = self._agent_for_task_type(task.task_type or "bugfix")
                result = agent.run(
                    prompt=prompt,
                    repo_path=repo_path,
                    task_id=task.id,
                    task_type=task.task_type or "bugfix",
                    attempt=1,
                    progress_callback=_on_fix_progress,
                )

                if result.success:
                    has_commits, commit_count = self.git.has_commits_ahead(repo_path, task.branch_name)
                    if has_commits:
                        push_success, push_error = self.git.push(repo_path, task.branch_name)
                        if push_success:
                            self.logger.info("Pushed %d PR fix commits", commit_count)
                            self._notify_pr_url(task, updated=True, notify=self.config.notify_on_pr_created)
                        else:
                            self.logger.warning("Failed to push PR fixes: %s", push_error)

                task = self.transition(task, TaskState.PR_REVIEW_PENDING, f"Addressed PR feedback")
            except Exception as e:
                self.logger.error("Error addressing PR feedback: %s", e)
                self._set_error(task, TaskError.PR_REVIEW_FAILED, str(e))

            return task

        return task

    # -------------------------------------------------------------------------
    # Merge flow
    # -------------------------------------------------------------------------

    def process_merge(self, task: Task) -> Task:
        """Execute the merge flow: rebase, test, merge PR."""
        if self.dry_run:
            self.logger.info("[DRY RUN] Would process merge, skipping")
            return task

        task = self.transition(task, TaskState.MERGING, "Starting merge process")
        self.notifier.notify_merge_starting(task)
        self._write_heartbeat()

        try:
            repo_path = Path(task.local_repo_path)
            base_branch = self.git.get_default_branch(repo_path)

            # Clean up stale rebase state
            if (repo_path / ".git" / "rebase-merge").exists() or \
               (repo_path / ".git" / "rebase-apply").exists():
                self.logger.warning("Cleaning up stale rebase state")
                self.git.abort_rebase(repo_path)

            # Ensure on feature branch
            if not self.git.ensure_on_branch(repo_path, task.branch_name, from_main=False):
                self._set_error(task, TaskError.MERGE_FAILED, f"Failed to checkout branch: {task.branch_name}")
                raise Exception(task.error_message)

            # Rebase onto latest base
            rebase_success, had_conflicts = self.git.rebase_onto(repo_path, task.branch_name, base_branch)

            if had_conflicts:
                self.notifier.notify_merge_conflicts(task)

                # F6: dispatch agent to resolve merge conflicts before giving up
                conflict_files = self.git.get_conflict_files(repo_path)
                self.logger.info(
                    "Rebase conflicts in %d files, dispatching agent to resolve",
                    len(conflict_files),
                )
                conflict_resolved = self._resolve_conflicts_with_agent(
                    task, repo_path, conflict_files
                )

                if not conflict_resolved:
                    self.git.abort_rebase(repo_path)
                    self._set_error(
                        task, TaskError.MERGE_CONFLICT_FAILED,
                        f"Agent could not resolve conflicts in: {', '.join(conflict_files)}"
                    )
                    raise Exception(task.error_message)

            if not rebase_success:
                self._set_error(task, TaskError.MERGE_FAILED, "Rebase failed with no conflicts")
                raise Exception(task.error_message)

            # Run tests post-rebase
            custom_test_cmd = self.config.get_project_test_command(task.reminder_list)
            test_passed, test_output = self.test_runner.run_tests(repo_path, custom_test_cmd)

            if not test_passed:
                self._set_error(
                    task, TaskError.MERGE_TEST_FAILED,
                    f"Post-rebase tests failed: {test_output[:500] if test_output else 'No output'}"
                )
                raise Exception(task.error_message)

            self.logger.info("Post-rebase tests passed")

            # Force push rebased branch
            push_success, push_error = self.git.push(repo_path, task.branch_name, force=True)
            if not push_success:
                self._set_error(task, TaskError.MERGE_FAILED, f"Failed to push rebased branch: {push_error}")
                raise Exception(task.error_message)

            # Merge the PR
            if task.pr_number:
                merge_success, merge_error = self.git.merge_pr(
                    repo_path, task.pr_number, task.merge_strategy
                )
                if not merge_success:
                    self._set_error(task, TaskError.MERGE_FAILED, f"PR merge failed: {merge_error}")
                    raise Exception(task.error_message)
            else:
                self.logger.warning("No PR number found, skipping gh merge")

            # Complete
            task = self.transition(task, TaskState.COMPLETE, "Merged successfully")
            self.notifier.notify_merge_complete(task)

            return task

        except Exception as e:
            self.logger.error("Error during merge: %s", e)
            if not task.error_type:
                self._set_error(task, TaskError.MERGE_FAILED, str(e))
            # Route through _handle_failure so merge failures get retried
            # instead of being immediately terminal.  The implementation
            # work is already done — losing it to a transient network
            # issue is unacceptable.
            if self.config.notify_on_error:
                self.notifier.notify_merge_failed(task, str(e))
            return self._handle_failure(task, task.error_type or TaskError.MERGE_FAILED, str(e))

    def _resolve_conflicts_with_agent(
        self, task: Task, repo_path: Path, conflict_files: list[str],
    ) -> bool:
        """F6: dispatch an agent to resolve merge conflicts during rebase.

        The agent sees the conflicted files, resolves markers, stages, and
        continues the rebase.  Returns True if all conflicts were resolved
        and the rebase completed, False otherwise.
        """
        max_rounds = 3  # limit conflict-resolution rounds for multi-step rebases
        agent = self._agent_for_task_type(task.task_type or "bugfix")

        for round_num in range(1, max_rounds + 1):
            if not self.git.has_conflicts(repo_path):
                # Rebase may still be in progress (multi-commit) — continue it
                success, still_conflicted = self.git.continue_rebase(repo_path)
                if success and not still_conflicted:
                    self.logger.info("Rebase completed after conflict resolution")
                    return True
                if not still_conflicted:
                    return success
                conflict_files = self.git.get_conflict_files(repo_path)

            conflict_list = "\n".join(f"- {f}" for f in conflict_files)
            prompt = (
                f"You are resolving merge conflicts on branch `{task.branch_name}` "
                f"during a rebase onto the latest main branch.\n\n"
                f"## Conflicted Files\n{conflict_list}\n\n"
                f"## Instructions\n"
                f"1. Read each conflicted file and understand both sides.\n"
                f"2. Resolve the conflict markers (<<<<<<< / ======= / >>>>>>>).\n"
                f"3. Keep the intent of the feature branch while incorporating main's changes.\n"
                f"4. After resolving all files, run: git add <resolved_files>\n"
                f"5. Do NOT run git rebase --continue — the system will handle that.\n"
            )

            result = agent.run(
                prompt=prompt,
                repo_path=repo_path,
                task_id=task.id,
                task_type="bugfix",
                attempt=round_num,
            )

            if not result.success:
                self.logger.warning(
                    "Conflict resolution agent failed (round %d): %s",
                    round_num, result.error,
                )
                return False

            # Continue the rebase after agent resolved conflicts
            success, still_conflicted = self.git.continue_rebase(repo_path)
            if success and not still_conflicted:
                self.logger.info("Rebase completed after %d resolution round(s)", round_num)
                return True

            if not still_conflicted:
                return success

            # More conflicts from next commit in rebase — loop again
            conflict_files = self.git.get_conflict_files(repo_path)
            self.logger.info(
                "Rebase produced new conflicts in round %d: %s", round_num, conflict_files
            )

        self.logger.warning("Conflict resolution exhausted %d rounds", max_rounds)
        return False

    def check_merge_status(self, task: Task) -> Task:
        """Check if the original reminder has been completed (merge) or deleted (skip)."""
        if not task.reminder_id:
            return task

        reminder_data = self._find_task_reminder(task)

        if reminder_data is self._REMINDER_LOOKUP_ERROR:
            self.logger.info("Skipping merge check for '%s' — Reminders CLI unavailable", task.task_title)
            return task

        if reminder_data is None:
            self.logger.info("Merge cancelled (reminder deleted): %s", task.task_title)
            return self.transition(task, TaskState.COMPLETE, "Merge cancelled by user")

        if reminder_data.get("isCompleted", False):
            self.logger.info("Merge approved: %s", task.task_title)
            return self.process_merge(task)

        return task

    # -------------------------------------------------------------------------
    # Reminder and notification helpers
    # -------------------------------------------------------------------------

    def _create_merge_reminder(self, task: Task) -> Task:
        """Create a Merge reminder and transition to AWAITING_MERGE."""
        # Guard: don't create merge reminder if there's no PR to merge
        if not task.pr_url:
            self.logger.warning("No PR URL for '%s' — skipping merge reminder, completing task", task.task_title)
            task = self.transition(task, TaskState.COMPLETE, "No PR to merge")
            return task

        if self.dry_run:
            self.logger.info("[DRY RUN] Would update reminder to merge status")
            return task

        merge_prefix = STATUS_MERGE.format(pr_number=task.pr_number or "?")
        self._update_reminder_status(
            task, merge_prefix,
            notes=f"PR: {task.pr_url}\n\nComplete to merge. Delete to skip (PR stays open)."
        )
        task.merge_strategy = self.config.merge_strategy
        task = self.transition(task, TaskState.AWAITING_MERGE, f"Awaiting merge: {task.pr_url}")
        self.notifier.notify_merge_ready(task)

        return task

    def _complete_task_reminders(self, task: Task) -> None:
        """Complete ALL reminders associated with a task — Task:, Approve/Complete to approve:,
        PR Ready:, Merge/Complete to merge:, and Question: items.

        Uses the raw reminders client (not cache) since indices shift during completion.
        """
        self.logger.info("Completing reminders for task: %s", task.task_title)

        # Bypass RemindersCache — we need fresh indices for each completion
        from lib.reminders import RemindersCache
        client = self.reminders
        if isinstance(client, RemindersCache):
            # Access the underlying non-cached client
            client = RemindersClient(client.bin, client.applescript_timeout_seconds, client.cli_timeout)

        # Complete original task reminder
        original_task_title = f"{TASK_PREFIX}{task.task_title}"
        if task.reminder_id:
            success = client.complete_by_external_id(task.reminder_list, task.reminder_id)
            if not success:
                client.complete_by_title(task.reminder_list, original_task_title, case_sensitive=False)
        else:
            client.complete_by_title(task.reminder_list, original_task_title, case_sensitive=False)

        # Complete approval reminder (handles both old "Approve PRD:" and new "Complete to approve:")
        if task.approval_reminder_title:
            if task.approval_reminder_id:
                client.complete_by_external_id(task.reminder_list, task.approval_reminder_id)
            else:
                client.complete_by_title(
                    task.reminder_list, task.approval_reminder_title, case_sensitive=False
                )

        # Complete PR Ready reminder (legacy — no longer created, but clean up old ones)
        if task.pr_reminder_id:
            client.complete_by_external_id(task.reminder_list, task.pr_reminder_id)
        elif task.pr_reminder_title:
            client.complete_by_title(task.reminder_list, task.pr_reminder_title, case_sensitive=False)

        # Complete merge reminder
        if task.merge_reminder_id:
            client.complete_by_external_id(task.reminder_list, task.merge_reminder_id)
        elif task.merge_reminder_title:
            client.complete_by_title(task.reminder_list, task.merge_reminder_title, case_sensitive=False)

        # Complete question reminder
        if task.question_reminder_id:
            client.complete_by_external_id(task.reminder_list, task.question_reminder_id)
        elif task.question_reminder_title:
            client.complete_by_title(task.reminder_list, task.question_reminder_title, case_sensitive=False)

    def _notify_pr_url(self, task: Task, updated: bool = False, notify: bool = True) -> None:
        """Send PR URL via Reminders and notifications."""
        if not task.pr_url:
            return

        if self.dry_run:
            self.logger.info("[DRY RUN] Would create PR reminder and notification")
            return

        # PR Ready/Updated are informational — send iMessage only, don't
        # clutter the Reminders list with non-actionable items.
        if notify:
            if updated:
                self.notifier.notify_pr_updated(task)
            else:
                self.notifier.notify_pr_created(task)

    # -------------------------------------------------------------------------
    # Text extraction helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_id_token(text: str) -> Optional[str]:
        """Extract a task id token from text."""
        if not text:
            return None
        match = re.search(r"\[id:([^\]]+)\]", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"\bid[:=]\s*([A-Za-z0-9-]+)\b", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _token_matches_task(self, task: Task, token: str) -> bool:
        """Return True if token matches task id or prefix."""
        if not token:
            return False
        token = token.strip().lower()
        if len(token) < 6:
            return False
        return task.id.lower().startswith(token)

    @staticmethod
    def _extract_fix_feedback(title: str, notes: Optional[str]) -> str:
        """Extract feedback text from a Fix reminder."""
        feedback = title
        colon_pos = feedback.lower().find("fix:")
        if colon_pos >= 0:
            feedback = feedback[colon_pos + 4:]
        feedback = re.sub(r"\[id:[^\]]+\]", "", feedback, flags=re.IGNORECASE).strip()
        if feedback:
            return feedback
        if notes:
            return notes.strip()
        return "No feedback provided."
