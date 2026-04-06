"""
Porter — iMessage notification sender.

Extracted from porter.py.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from lib.image_gen import ImageEvent, ImageGenerator
from lib.models import Task

# Default cooldown: 5 minutes between duplicate notifications per task
_DEFAULT_COOLDOWN_SECONDS = 300


class Notifier:
    """Send iMessage notifications via imsg CLI with configurable emoji and rate limiting."""

    # Emoji mappings for notifications
    EMOJI = {
        "task_received": "\U0001f4cb",
        "prd_starting": "\u2699\ufe0f",
        "prd_ready": "\U0001f4dd",
        "fix_received": "\U0001f527",
        "prd_updated": "\U0001f4dd",
        "work_starting": "\U0001f680",
        "writing_tests": "\U0001f9ea",
        "tests_passed": "\u2705",
        "tests_failed": "\u274c",
        "fixing_tests": "\U0001f527",
        "verifying": "\U0001f50d",
        "verification_passed": "\u2705",
        "verification_failed": "\u26a0\ufe0f",
        "pr_created": "\u2705",
        "pr_review": "\U0001f440",
        "error": "\u274c",
        "retry": "\U0001f504",
        "question": "\u2753",
        "merge_ready": "\U0001f500",
        "merge_starting": "\U0001f500",
        "merge_conflicts": "\u26a0\ufe0f",
        "merge_complete": "\u2705",
        "merge_failed": "\u274c",
    }

    def __init__(self, imsg_bin: str, phone: str, use_emoji: bool = True,
                 cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
                 image_generator: Optional[ImageGenerator] = None):
        self.imsg = imsg_bin
        self.phone = phone
        self.use_emoji = use_emoji
        self.cooldown_seconds = cooldown_seconds
        self.image_gen = image_generator
        # Rate limiter: maps (task_id, event_key) -> last send timestamp
        self._last_sent: Dict[Tuple[str, str], float] = {}

    def _emoji(self, key: str) -> str:
        """Get emoji for key, or empty string if emoji disabled."""
        if self.use_emoji:
            return self.EMOJI.get(key, "") + " "
        return ""

    def _project_prefix(self, key: str, task: Task) -> str:
        """Build prefix with emoji and project name: '📋 Juno : '."""
        project = getattr(task, "reminder_list", None) or ""
        emoji = self._emoji(key)
        if project:
            return f"{emoji}{project} : "
        return emoji

    def _send_with_image(self, message: str, event: ImageEvent, context: str = "") -> bool:
        """Generate an image for the event and send it with the message.

        Falls back to text-only if image generation fails or is unavailable.
        Cleans up the temp image file after sending.
        """
        image_path = None
        try:
            if self.image_gen:
                image_path = self.image_gen.generate(event, context)
            return self.send(message, image_path=image_path)
        finally:
            if image_path:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass

    def send(self, message: str, image_path: Optional[Path] = None) -> bool:
        """Send an iMessage notification via imsg CLI, optionally with an image attachment."""
        if not self.imsg or not self.phone:
            return False
        cmd = [
            self.imsg, "send",
            "--to", self.phone,
            "--text", message,
            "--service", "imessage"
        ]
        if image_path and image_path.exists():
            cmd += ["--file", str(image_path)]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0

    def _send_throttled(self, task: Task, event_key: str, message: str) -> bool:
        """Send a notification, rate-limited per (task_id, event_key).

        Returns False if throttled or send failed. Always-send events
        (pr_created, error, question) should call send() directly.
        """
        now = time.monotonic()
        key = (task.id, event_key)
        last = self._last_sent.get(key, 0)
        if now - last < self.cooldown_seconds:
            return False
        sent = self.send(message)
        if sent:
            self._last_sent[key] = now
        return sent

    def notify_task_received(self, task: Task) -> None:
        """Notify that a new task was received."""
        self._send_with_image(
            f"{self._project_prefix('task_received', task)}Task received: '{task.task_title}'",
            ImageEvent.TASK_RECEIVED, context=task.task_title
        )

    def notify_prd_starting(self, task: Task) -> None:
        """Notify that PRD generation is starting."""
        self._send_throttled(task, "prd_starting", f"{self._project_prefix('prd_starting', task)}Generating PRD for '{task.task_title}'...")

    def notify_prd_ready(self, task: Task) -> None:
        """Notify that PRD is ready for review."""
        self.send(f"{self._project_prefix('prd_ready', task)}PRD complete for '{task.task_title}': {task.prd_url}")

    def notify_fix_received(self, task: Task, feedback: str) -> None:
        """Notify that fix instructions were received."""
        short_feedback = feedback[:50] + "..." if len(feedback) > 50 else feedback
        self.send(f"{self._project_prefix('fix_received', task)}Fix received for '{task.task_title}': {short_feedback}")

    def notify_prd_updated(self, task: Task) -> None:
        """Notify that PRD was updated based on feedback."""
        self.send(f"{self._project_prefix('prd_updated', task)}PRD updated for '{task.task_title}': {task.prd_url}")

    def notify_work_starting(self, task: Task) -> None:
        """Notify that implementation is beginning."""
        self._send_with_image(
            f"{self._project_prefix('work_starting', task)}Beginning work on '{task.task_title}'",
            ImageEvent.WORK_STARTING, context=task.task_title
        )

    def notify_tests_passed(self, task: Task) -> None:
        """Notify that tests passed."""
        self._send_with_image(
            f"{self._project_prefix('tests_passed', task)}Tests passed for '{task.task_title}'",
            ImageEvent.TESTS_PASSED, context=task.task_title
        )

    def notify_tests_failed(self, task: Task) -> None:
        """Notify that tests failed."""
        self._send_with_image(
            f"{self._project_prefix('tests_failed', task)}Tests FAILED for '{task.task_title}'",
            ImageEvent.TESTS_FAILED, context=task.task_title
        )

    def notify_pr_created(self, task: Task) -> None:
        """Notify that PR was created (work completed)."""
        self._send_with_image(
            f"{self._project_prefix('pr_created', task)}Work completed for '{task.task_title}': {task.pr_url}",
            ImageEvent.PR_CREATED, context=task.task_title
        )

    def notify_pr_updated(self, task: Task) -> None:
        """Notify that PR was updated with new changes."""
        self.send(f"{self._project_prefix('pr_created', task)}PR updated for '{task.task_title}': {task.pr_url}")

    def notify_pr_review_pending(self, task: Task) -> None:
        """Notify that PR has pending review comments (throttled)."""
        self._send_throttled(task, "pr_review", f"{self._project_prefix('pr_review', task)}PR for '{task.task_title}' has review comments. Addressing...")

    def notify_error(self, task: Task, retry_max: int = 3) -> None:
        """Notify about an error with retry info."""
        error_msg = task.error_message or "Unknown error"
        if len(error_msg) > 100:
            error_msg = error_msg[:100] + "..."

        msg = f"{self._project_prefix('error', task)}Error on '{task.task_title}' (retry {task.retry_count}/{retry_max}): {error_msg}"
        self._send_with_image(msg, ImageEvent.ERROR, context=f"{task.task_title}: {error_msg}")

    def notify_retry(self, task: Task) -> None:
        """Notify about a retry attempt."""
        self._send_with_image(
            f"{self._project_prefix('retry', task)}Retrying '{task.task_title}' (attempt {task.retry_count + 1})",
            ImageEvent.RETRY, context=task.task_title
        )

    def notify_question(self, task: Task) -> None:
        """Notify that a question needs answering."""
        self.send(f"{self._project_prefix('question', task)}Needs input for '{task.task_title}'. Check Reminders.")

    def notify_writing_tests(self, task: Task) -> None:
        """Notify that Claude is writing tests (throttled)."""
        self._send_throttled(task, "writing_tests", f"{self._project_prefix('writing_tests', task)}Writing tests for '{task.task_title}'...")

    def notify_fixing_tests(self, task: Task, iteration: int) -> None:
        """Notify that Claude is fixing code to pass tests (throttled)."""
        self._send_throttled(task, "fixing_tests", f"{self._project_prefix('fixing_tests', task)}Fixing test failures for '{task.task_title}' (attempt {iteration})")

    def notify_verifying(self, task: Task) -> None:
        """Notify that acceptance criteria are being verified (throttled)."""
        self._send_throttled(task, "verifying", f"{self._project_prefix('verifying', task)}Verifying acceptance criteria for '{task.task_title}'...")

    def notify_verification_passed(self, task: Task) -> None:
        """Notify that all acceptance criteria passed."""
        self.send(f"{self._project_prefix('verification_passed', task)}Acceptance criteria verified for '{task.task_title}'")

    def notify_verification_failed(self, task: Task, issues: str) -> None:
        """Notify that verification found issues."""
        short_issues = issues[:100] + "..." if len(issues) > 100 else issues
        self.send(f"{self._project_prefix('verification_failed', task)}Verification issues for '{task.task_title}': {short_issues}")

    def notify_merge_ready(self, task: Task) -> None:
        """Notify that a merge reminder was created."""
        self.send(f"{self._project_prefix('merge_ready', task)}PR ready for merge: '{task.task_title}'. Complete the Merge reminder to proceed.")

    def notify_merge_starting(self, task: Task) -> None:
        """Notify that merge process is beginning."""
        self.send(f"{self._project_prefix('merge_starting', task)}Starting merge for '{task.task_title}'...")

    def notify_merge_conflicts(self, task: Task) -> None:
        """Notify that merge conflicts were found (throttled)."""
        self._send_throttled(task, "merge_conflicts", f"{self._project_prefix('merge_conflicts', task)}Merge conflicts detected for '{task.task_title}'. Resolving...")

    def notify_merge_complete(self, task: Task) -> None:
        """Notify that merge completed successfully."""
        self._send_with_image(
            f"{self._project_prefix('merge_complete', task)}Merged and complete: '{task.task_title}'",
            ImageEvent.MERGE_COMPLETE, context=task.task_title
        )

    def notify_merge_failed(self, task: Task, reason: str) -> None:
        """Notify that merge failed."""
        short_reason = reason[:100] + "..." if len(reason) > 100 else reason
        self.send(f"{self._project_prefix('merge_failed', task)}Merge failed for '{task.task_title}': {short_reason}")

    def notify_approval_stale(self, task: Task, days: int) -> None:
        """Notify that approval has been waiting too long (throttled)."""
        self._send_throttled(
            task, "approval_stale",
            f"{self._project_prefix('question', task)}PRD for '{task.task_title}' awaiting approval for {days} days"
        )

    def notify_question_stale(self, task: Task, days: int) -> None:
        """Notify that a question has been waiting too long (throttled)."""
        self._send_throttled(
            task, "question_stale",
            f"{self._project_prefix('question', task)}Question for '{task.task_title}' unanswered for {days} days"
        )
