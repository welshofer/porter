"""Startup health checks for Porter.

Validates environment before any work happens, catching misconfiguration
early instead of failing mid-task.
"""

import subprocess
from pathlib import Path
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from lib.models import Config


class HealthCheckError(Exception):
    """Raised when one or more startup health checks fail."""

    def __init__(self, failures: List[str]):
        self.failures = failures
        super().__init__(
            f"{len(failures)} health check failure(s): " + "; ".join(failures)
        )


class StartupHealthCheck:
    """Validates the runtime environment before the daemon starts processing."""

    def __init__(self, config: "Config"):
        self.config = config

    def check_tool_binaries(self) -> List[str]:
        """Verify that configured tool binary paths exist on disk."""
        failures = []
        checks = [
            ("reminders_bin", self.config.reminders_bin),
            ("claude_bin", self.config.claude_bin),
            ("gh_bin", self.config.gh_bin),
        ]
        # imsg_bin is optional — empty string means notifications disabled
        if self.config.imsg_bin:
            checks.append(("imsg_bin", self.config.imsg_bin))

        for name, path in checks:
            if not Path(path).exists():
                failures.append(f"{name} not found: {path}")
        return failures

    def check_gh_auth(self) -> List[str]:
        """Verify that gh CLI is authenticated."""
        failures = []
        try:
            result = subprocess.run(
                [self.config.gh_bin, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                msg = result.stderr.strip() if result.stderr else "gh auth status returned non-zero"
                failures.append(f"gh auth: not logged in ({msg})")
        except FileNotFoundError:
            failures.append(f"gh auth: binary not found at {self.config.gh_bin}")
        except subprocess.TimeoutExpired:
            failures.append("gh auth: status check timed out")
        return failures

    def check_paths(self) -> List[str]:
        """Verify that required paths exist or can be created."""
        failures = []

        # db_path parent must exist or be creatable
        db_parent = self.config.db_path.parent
        if not db_parent.exists():
            try:
                db_parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                failures.append(f"db_path parent cannot be created: {db_parent} ({e})")

        # workspaces_path must exist or be creatable
        if not self.config.workspaces_path.exists():
            try:
                self.config.workspaces_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                failures.append(f"workspaces_path cannot be created: {self.config.workspaces_path} ({e})")

        # logs_path must exist or be creatable
        if not self.config.logs_path.exists():
            try:
                self.config.logs_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                failures.append(f"logs_path cannot be created: {self.config.logs_path} ({e})")

        return failures

    def run_all(self) -> None:
        """Run all health checks. Raises HealthCheckError if any fail."""
        failures = []
        failures.extend(self.check_tool_binaries())
        failures.extend(self.check_gh_auth())
        failures.extend(self.check_paths())

        if failures:
            raise HealthCheckError(failures)
