"""
Porter — Git and GitHub operations.

Extracted from porter.py.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_GIT_TIMEOUT = 120  # seconds — prevent hung git commands from blocking the daemon


class GitOperations:
    """Git and GitHub operations with robust error handling."""

    def __init__(self, gh_bin: str, logger: logging.Logger = None, clone_depth: Optional[int] = 1):
        self.gh = gh_bin
        self.logger = logger or logging.getLogger('porter')
        self.clone_depth = clone_depth  # None = full clone

    def _run_git(self, args: List[str], cwd: Path, check: bool = False,
                 timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
        """Run a git command with logging and timeout.

        Every git subprocess gets a timeout to prevent hung credential
        prompts, network stalls, or lock contention from blocking the
        daemon forever.
        """
        cmd = ["git"] + args
        self.logger.debug(f"Running: {' '.join(cmd)} in {cwd}")
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            self.logger.error("Git command timed out after %ds: %s", timeout, ' '.join(cmd))
            # Return a synthetic failed result so callers handle it uniformly
            return subprocess.CompletedProcess(
                cmd, returncode=124,  # 124 = timeout convention
                stdout="", stderr=f"Command timed out after {timeout}s",
            )
        if result.returncode != 0 and check:
            self.logger.error(f"Git command failed: {' '.join(cmd)}")
            self.logger.error(f"stderr: {result.stderr}")
        return result

    def clone_or_update(self, repo_url: str, local_path: Path, protected_branches: Optional[List[str]] = None) -> bool:
        """Clone repo if not exists, otherwise fetch and reset to origin/main.

        R14: each intermediate step is checked — a fetch failure (network blip)
        now fails early instead of proceeding with stale data.
        """
        if local_path.exists():
            fetch = self._run_git(["fetch", "origin"], local_path)
            if fetch.returncode != 0:
                self.logger.error("fetch origin failed: %s", fetch.stderr.strip())
                return False
            self._run_git(["reset", "--hard"], local_path)
            self._run_git(["clean", "-fd"], local_path)
            checkout = self._run_git(["checkout", "main"], local_path)
            if checkout.returncode != 0:
                self.logger.error("checkout main failed: %s", checkout.stderr.strip())
                return False
            result = self._run_git(["reset", "--hard", "origin/main"], local_path)
            self._cleanup_local_branches(local_path, protected_branches=protected_branches)
            return result.returncode == 0
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone"]
            if self.clone_depth:
                cmd += ["--depth", str(self.clone_depth)]
            cmd += [repo_url, str(local_path)]
            self.logger.info("Cloning %s (depth=%s)", repo_url, self.clone_depth or "full")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=_GIT_TIMEOUT * 2)  # clones can be slower
            except subprocess.TimeoutExpired:
                self.logger.error("Clone timed out after %ds: %s", _GIT_TIMEOUT * 2, repo_url)
                return False
            return result.returncode == 0

    def unshallow(self, repo_path: Path) -> bool:
        """Fetch full history if repo is shallow (needed before rebase)."""
        is_shallow = self._run_git(["rev-parse", "--is-shallow-repository"], repo_path)
        if is_shallow.stdout.strip() != "true":
            return True  # Already full
        self.logger.info("Unshallowing repository for rebase")
        result = self._run_git(["fetch", "--unshallow", "origin"], repo_path)
        return result.returncode == 0

    def _cleanup_local_branches(self, repo_path: Path, protected_branches: Optional[List[str]] = None) -> None:
        """Remove local porter branches to ensure clean state.

        R4: *protected_branches* (if given) are skipped — these belong to
        active tasks and must not be deleted.
        """
        protected = set(protected_branches or [])
        result = self._run_git(["branch", "--list", "porter/*"], repo_path)
        branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
        for branch in branches:
            if branch in protected:
                self.logger.debug("Skipping protected branch: %s", branch)
                continue
            self._run_git(["branch", "-D", branch], repo_path)
            self.logger.debug(f"Cleaned up local branch: {branch}")

    def ensure_on_branch(self, repo_path: Path, branch_name: str, from_main: bool = True) -> bool:
        """
        Ensure we're on the specified branch. Creates from main if needed.
        Handles: branch exists locally, branch exists on remote, or needs creation.
        """
        current = self.get_current_branch(repo_path)

        if current == branch_name:
            self.logger.debug(f"Already on branch: {branch_name}")
            return True

        self._run_git(["fetch", "origin"], repo_path)

        local_check = self._run_git(["rev-parse", "--verify", branch_name], repo_path)
        if local_check.returncode == 0:
            result = self._run_git(["checkout", branch_name], repo_path)
            if result.returncode == 0:
                self.logger.info(f"Checked out existing local branch: {branch_name}")
                return True

        remote_check = self._run_git(["rev-parse", "--verify", f"origin/{branch_name}"], repo_path)
        if remote_check.returncode == 0:
            result = self._run_git(["checkout", "-b", branch_name, f"origin/{branch_name}"], repo_path)
            if result.returncode == 0:
                self.logger.info(f"Checked out remote branch: {branch_name}")
                return True
            self._run_git(["checkout", branch_name], repo_path)
            self._run_git(["reset", "--hard", f"origin/{branch_name}"], repo_path)
            return True

        if from_main:
            self._run_git(["checkout", "main"], repo_path)
            self._run_git(["reset", "--hard", "origin/main"], repo_path)

        result = self._run_git(["checkout", "-b", branch_name], repo_path)
        if result.returncode == 0:
            self.logger.info(f"Created new branch: {branch_name}")
            return True

        self.logger.error(f"Failed to ensure branch: {branch_name}")
        return False

    def create_branch(self, repo_path: Path, branch_name: str) -> bool:
        """Create and checkout a new branch (wrapper for backward compat)."""
        return self.ensure_on_branch(repo_path, branch_name, from_main=True)

    def commit_all(self, repo_path: Path, message: str) -> bool:
        """Add all changes and commit."""
        self._run_git(["add", "-A"], repo_path)
        result = self._run_git(["commit", "-m", message], repo_path)
        return result.returncode == 0

    def has_commits_ahead(self, repo_path: Path, branch_name: str) -> Tuple[bool, int]:
        """Check if local branch has commits ahead of origin."""
        remote_check = self._run_git(["rev-parse", "--verify", f"origin/{branch_name}"], repo_path)

        if remote_check.returncode != 0:
            result = self._run_git(["rev-list", "--count", "origin/main..HEAD"], repo_path)
            if result.returncode == 0:
                count = int(result.stdout.strip() or "0")
                return count > 0, count
            return False, 0

        result = self._run_git(["rev-list", "--count", f"origin/{branch_name}..HEAD"], repo_path)
        if result.returncode == 0:
            count = int(result.stdout.strip() or "0")
            return count > 0, count
        return False, 0

    def get_commit_log(self, repo_path: Path, count: int = 5) -> List[str]:
        """Get recent commit messages."""
        result = self._run_git(["log", f"-{count}", "--oneline"], repo_path)
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        return []

    def push(self, repo_path: Path, branch_name: str, force: bool = False, _depth: int = 0) -> Tuple[bool, str]:
        """
        Push branch to origin. Returns (success, error_message).
        If force=True, will use --force-with-lease for safety.
        """
        if _depth > 1:
            self.logger.error("Push failed after %d retry attempts", _depth)
            return False, "Push failed after retry attempts"

        current = self.get_current_branch(repo_path)
        if current != branch_name:
            return False, f"Not on expected branch. Current: {current}, Expected: {branch_name}"

        if not force:
            has_commits, commit_count = self.has_commits_ahead(repo_path, branch_name)
            if not has_commits:
                return False, "No commits to push"
            self.logger.info(f"Pushing {commit_count} commits to origin/{branch_name}")
        else:
            self.logger.info(f"Force-pushing {branch_name} to origin (post-rebase)")

        push_args = ["push", "-u", "origin", branch_name]
        if force:
            push_args.insert(1, "--force-with-lease")

        result = self._run_git(push_args, repo_path)

        if result.returncode == 0:
            return True, ""

        stderr = result.stderr.lower()
        if "non-fast-forward" in stderr or "rejected" in stderr:
            if not force:
                # R6: audit-log the force-push escalation for traceability
                self.logger.warning(
                    "Push rejected for %s, escalating to --force-with-lease (audit)", branch_name
                )
                return self.push(repo_path, branch_name, force=True, _depth=_depth + 1)
            return False, "Push rejected even with force"

        if "already exists" in stderr:
            self._run_git(["branch", f"--set-upstream-to=origin/{branch_name}", branch_name], repo_path)
            return self.push(repo_path, branch_name, force=force, _depth=_depth + 1)

        return False, result.stderr

    def get_owner_repo(self, repo_path: Path) -> Optional[Tuple[str, str]]:
        """Extract (owner, repo) from the git remote origin URL."""
        result = self._run_git(["remote", "get-url", "origin"], repo_path)
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        # Handle SSH: git@github.com:owner/repo.git
        # Handle HTTPS: https://github.com/owner/repo.git
        match = re.search(r'[:/]([^/]+)/([^/]+?)(?:\.git)?$', url)
        if match:
            return match.group(1), match.group(2)
        return None

    def create_pr(self, repo_path: Path, title: str, body: str, base: str = "main") -> Tuple[Optional[str], str]:
        """Create a PR using gh CLI with REST API fallback. Returns (pr_url, error_message)."""
        branch = self.get_current_branch(repo_path)
        check_result = subprocess.run(
            [self.gh, "pr", "view", branch, "--json", "url,headRefOid"],
            cwd=repo_path, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
        if check_result.returncode == 0:
            try:
                data = json.loads(check_result.stdout)
                existing_url = data.get("url")
                if existing_url:
                    # Check if PR has our latest commits
                    head_ref = data.get("headRefOid", "")
                    local_head = self._run_git(["rev-parse", "HEAD"], repo_path).stdout.strip()
                    if head_ref and local_head and head_ref != local_head:
                        self.logger.warning(
                            "Existing PR %s is stale (PR: %s, local: %s)",
                            existing_url, head_ref[:8], local_head[:8],
                        )
                        return existing_url, "stale: PR HEAD differs from local HEAD"
                    else:
                        self.logger.info(f"PR already exists and is up to date: {existing_url}")
                    return existing_url, ""
            except json.JSONDecodeError:
                pass

        # Try gh pr create (uses GraphQL)
        result = subprocess.run(
            [self.gh, "pr", "create", "--title", title, "--body", body, "--base", base],
            cwd=repo_path, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip(), ""

        graphql_error = result.stderr
        self.logger.warning("gh pr create failed (GraphQL), trying REST API fallback: %s", graphql_error)

        # Fallback: REST API via gh api
        owner_repo = self.get_owner_repo(repo_path)
        if not owner_repo:
            return None, f"GraphQL failed: {graphql_error}; could not determine owner/repo for REST fallback"

        owner, repo = owner_repo
        pr_data = json.dumps({
            "title": title,
            "body": body,
            "head": branch,
            "base": base,
        })
        rest_result = subprocess.run(
            [self.gh, "api", f"repos/{owner}/{repo}/pulls",
             "--method", "POST", "--input", "-"],
            input=pr_data, cwd=repo_path, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT,
        )
        if rest_result.returncode == 0:
            try:
                pr_response = json.loads(rest_result.stdout)
                pr_url = pr_response.get("html_url")
                if pr_url:
                    self.logger.info("PR created via REST API: %s", pr_url)
                    return pr_url, ""
            except json.JSONDecodeError:
                pass

        return None, f"GraphQL: {graphql_error}; REST: {rest_result.stderr}"

    def get_current_branch(self, repo_path: Path) -> str:
        """Get current branch name."""
        result = self._run_git(["branch", "--show-current"], repo_path)
        return result.stdout.strip()

    def get_status(self, repo_path: Path) -> Dict[str, any]:
        """Get comprehensive git status for debugging."""
        status = {
            "current_branch": self.get_current_branch(repo_path),
            "has_uncommitted": False,
            "commits_ahead": 0,
            "recent_commits": []
        }

        result = self._run_git(["status", "--porcelain"], repo_path)
        status["has_uncommitted"] = bool(result.stdout.strip())

        branch = status["current_branch"]
        _, count = self.has_commits_ahead(repo_path, branch)
        status["commits_ahead"] = count

        status["recent_commits"] = self.get_commit_log(repo_path, 5)

        return status

    def get_default_branch(self, repo_path: Path) -> str:
        """Get the default branch name (usually 'main' or 'master')."""
        result = self._run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_path)
        if result.returncode == 0:
            ref = result.stdout.strip()
            return ref.split("/")[-1]
        for branch in ["main", "master"]:
            check = self._run_git(["rev-parse", "--verify", f"origin/{branch}"], repo_path)
            if check.returncode == 0:
                return branch
        return "main"

    def rebase_onto(self, repo_path: Path, branch_name: str, base: str = "main") -> Tuple[bool, bool]:
        """
        Fetch latest base and rebase feature branch onto it.
        Returns (success, had_conflicts).
        """
        self.logger.info(f"Rebasing {branch_name} onto {base}")

        # Unshallow if needed — rebase requires full history
        if not self.unshallow(repo_path):
            self.logger.error("Failed to unshallow, cannot rebase")
            return False, False
        self._run_git(["fetch", "origin", base], repo_path)

        current = self.get_current_branch(repo_path)
        if current != branch_name:
            checkout = self._run_git(["checkout", branch_name], repo_path)
            if checkout.returncode != 0:
                self.logger.error(f"Failed to checkout {branch_name}: {checkout.stderr}")
                return False, False

        result = self._run_git(["rebase", f"origin/{base}"], repo_path)

        if result.returncode == 0:
            self.logger.info("Rebase completed cleanly")
            return True, False

        if self.has_conflicts(repo_path):
            self.logger.warning("Rebase stopped with conflicts")
            return False, True

        self.logger.error(f"Rebase failed: {result.stderr}")
        return False, False

    def has_conflicts(self, repo_path: Path) -> bool:
        """Check for unresolved merge conflicts (unmerged files)."""
        result = self._run_git(["diff", "--name-only", "--diff-filter=U"], repo_path)
        return bool(result.stdout.strip())

    def get_conflict_files(self, repo_path: Path) -> List[str]:
        """Get list of files with conflicts."""
        result = self._run_git(["diff", "--name-only", "--diff-filter=U"], repo_path)
        if result.returncode == 0 and result.stdout.strip():
            return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return []

    def abort_rebase(self, repo_path: Path) -> bool:
        """Abort an in-progress rebase."""
        result = self._run_git(["rebase", "--abort"], repo_path)
        return result.returncode == 0

    def continue_rebase(self, repo_path: Path) -> Tuple[bool, bool]:
        """
        Stage all resolved files and continue the rebase.
        Returns (success, still_conflicted).
        Uses GIT_EDITOR=true to prevent editor prompts in non-interactive mode.
        """
        self._run_git(["add", "-A"], repo_path)

        cmd = ["git", "rebase", "--continue"]
        env = {**subprocess.os.environ, "GIT_EDITOR": "true"}
        try:
            result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True,
                                    env=env, timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error("Rebase --continue timed out after %ds", _GIT_TIMEOUT)
            return False, False

        if result.returncode == 0:
            if self.has_conflicts(repo_path):
                return False, True
            return True, False

        if self.has_conflicts(repo_path):
            return False, True

        self.logger.error(f"Rebase --continue failed: {result.stderr}")
        return False, False

    def get_pr_state(self, repo_path: Path, pr_number: int) -> str:
        """Check if a PR is open, merged, or closed. Returns 'OPEN', 'MERGED', 'CLOSED', or 'UNKNOWN'."""
        try:
            result = subprocess.run(
                [self.gh, "pr", "view", str(pr_number), "--json", "state"],
                cwd=repo_path, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return "UNKNOWN"
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                return data.get("state", "UNKNOWN").upper()
            except (json.JSONDecodeError, KeyError):
                return "UNKNOWN"
        return "UNKNOWN"

    def merge_pr(self, repo_path: Path, pr_number: int, strategy: str = "squash") -> Tuple[bool, str]:
        """
        Merge a PR using gh CLI. Returns (success, error_message).
        Strategy: 'squash', 'merge', or 'rebase'.
        """
        args = [self.gh, "pr", "merge", str(pr_number), f"--{strategy}", "--delete-branch"]

        self.logger.info(f"Merging PR #{pr_number} with strategy: {strategy}")
        try:
            result = subprocess.run(args, cwd=repo_path, capture_output=True, text=True,
                                    timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error("PR merge timed out after %ds", _GIT_TIMEOUT)
            return False, f"Merge command timed out after {_GIT_TIMEOUT}s"

        if result.returncode == 0:
            self.logger.info(f"PR #{pr_number} merged successfully")
            return True, ""

        error = result.stderr.strip()
        self.logger.error(f"Failed to merge PR #{pr_number}: {error}")
        return False, error
