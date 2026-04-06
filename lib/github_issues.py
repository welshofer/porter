"""
Porter — GitHub Issues client.

Extracted from porter.py.
"""

import json
import logging
import re
import subprocess
from typing import Dict, List


class GitHubIssuesClient:
    """Fetches and manages tasks from GitHub Issues."""

    def __init__(self, gh_bin: str, label: str = "porter"):
        self.gh = gh_bin
        self.label = label
        self.logger = logging.getLogger('porter')

    def get_issues_for_repo(self, repo_url: str) -> List[Dict]:
        """Get open issues with the porter label from a repo."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            self.logger.warning(f"Could not extract repo from URL: {repo_url}")
            return []

        repo = match.group(1)

        try:
            result = subprocess.run(
                [
                    self.gh, "issue", "list",
                    "--repo", repo,
                    "--label", self.label,
                    "--state", "open",
                    "--json", "number,title,body,labels,createdAt"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                self.logger.warning(f"Failed to fetch issues from {repo}: {result.stderr}")
                return []

            return json.loads(result.stdout)

        except subprocess.TimeoutExpired:
            self.logger.error(f"Timeout fetching issues from {repo}")
            return []
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse issues JSON: {e}")
            return []

    def get_issue_labels(self, repo_url: str, issue_number: int) -> List[str]:
        """Get labels for a specific issue."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return []

        repo = match.group(1)

        try:
            result = subprocess.run(
                [
                    self.gh, "issue", "view", str(issue_number),
                    "--repo", repo,
                    "--json", "labels"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode != 0:
                return []
            data = json.loads(result.stdout)
            return [label.get("name", "") for label in data.get("labels", [])]
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def get_issue_comments(self, repo_url: str, issue_number: int) -> List[Dict]:
        """Get issue comments as JSON objects."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return []

        repo = match.group(1)

        try:
            result = subprocess.run(
                [
                    self.gh, "api",
                    f"repos/{repo}/issues/{issue_number}/comments"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode != 0:
                return []
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def add_label(self, repo_url: str, issue_number: int, label: str) -> bool:
        """Add a label to an issue."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return False

        repo = match.group(1)

        result = subprocess.run(
            [self.gh, "issue", "edit", str(issue_number), "--repo", repo, "--add-label", label],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    def comment_on_issue(self, repo_url: str, issue_number: int, body: str) -> bool:
        """Add a comment to an issue."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return False

        repo = match.group(1)

        result = subprocess.run(
            [self.gh, "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    def close_issue(self, repo_url: str, issue_number: int) -> bool:
        """Close an issue."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return False

        repo = match.group(1)

        result = subprocess.run(
            [self.gh, "issue", "close", str(issue_number), "--repo", repo],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    def get_pr_reviews(self, repo_url: str, pr_number: int) -> List[Dict]:
        """Get review comments on a PR."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return []

        repo = match.group(1)

        try:
            result = subprocess.run(
                [
                    self.gh, "pr", "view", str(pr_number),
                    "--repo", repo,
                    "--json", "reviewDecision,reviews,comments"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return []

            data = json.loads(result.stdout)
            reviews = data.get("reviews", [])

            pending_comments = []
            for review in reviews:
                if review.get("state") == "CHANGES_REQUESTED":
                    pending_comments.append({
                        "type": "review",
                        "body": review.get("body", ""),
                        "author": review.get("author", {}).get("login", "")
                    })

            return pending_comments

        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []

    def get_pr_review_comments(self, repo_url: str, pr_number: int) -> List[Dict]:
        """Get inline review comments on a PR."""
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
        if not match:
            return []

        repo = match.group(1)

        try:
            result = subprocess.run(
                [
                    self.gh, "api",
                    f"repos/{repo}/pulls/{pr_number}/comments",
                    "--jq", ".[].{path: .path, line: .line, body: .body, id: .id}"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return []

            comments = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        comments.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return comments

        except subprocess.TimeoutExpired:
            return []
