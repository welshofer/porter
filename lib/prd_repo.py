"""
Porter — PRD repository manager.

Extracted from porter.py.
"""

import logging
import re
import threading
from pathlib import Path
from typing import Tuple

from lib.git_ops import GitOperations


class PRDRepoManager:
    """Manages the central PRD repository."""

    def __init__(self, repo_url: str, local_path: Path, git: GitOperations):
        self.repo_url = repo_url
        self.local_path = local_path
        self.git = git
        self._lock = threading.Lock()
        self.logger = logging.getLogger('porter')

    def ensure_updated(self) -> bool:
        """Ensure PRD repo is cloned and up to date."""
        return self.git.clone_or_update(self.repo_url, self.local_path)

    def save_prd(self, project_name: str, feature_slug: str, content: str) -> Tuple[Path, str]:
        """
        Save a PRD to the repo and push it. Thread-safe.
        Returns (local_path, github_url).
        """
        with self._lock:
            return self._save_prd_impl(project_name, feature_slug, content)

    def _save_prd_impl(self, project_name: str, feature_slug: str, content: str) -> Tuple[Path, str]:
        """Internal PRD save implementation (called under lock)."""
        # Pull latest to avoid push rejection from diverged histories
        self.git._run_git(["pull", "--rebase", "origin", "main"], self.local_path)

        # Create project directory
        project_dir = self.local_path / project_name.lower()
        project_dir.mkdir(parents=True, exist_ok=True)

        # Write PRD file
        prd_filename = f"{feature_slug}.md"
        prd_path = project_dir / prd_filename
        prd_path.write_text(content)

        # Commit and push
        self.git.commit_all(self.local_path, f"Add PRD: {project_name}/{feature_slug}")
        push_success, push_error = self.git.push(self.local_path, "main")
        if not push_success:
            self.logger.warning(f"PRD push failed ({push_error}), retrying after pull --rebase")
            self.git._run_git(["pull", "--rebase", "origin", "main"], self.local_path)
            push_success, push_error = self.git.push(self.local_path, "main")
            if not push_success:
                self.logger.error(f"PRD push failed after retry: {push_error}")

        # Construct GitHub URL
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", self.repo_url)
        if match:
            repo_path = match.group(1)
            github_url = f"https://github.com/{repo_path}/blob/main/{project_name.lower()}/{prd_filename}"
        else:
            github_url = ""

        return prd_path, github_url

    def get_prd_diff(self, project_name: str, feature_slug: str) -> str:
        """F7: get the git diff of the most recent PRD change.

        Returns the diff string, or empty string if no history.
        Useful for understanding what changed between PRD iterations.
        """
        prd_filename = f"{project_name.lower()}/{feature_slug}.md"
        result = self.git._run_git(
            ["log", "-1", "--format=", "-p", "--", prd_filename],
            self.local_path,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
