"""Tests for TaskRouter classification logic."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from lib.router import TaskRouter, TaskType


class TestTaskRouter:
    def setup_method(self):
        self.router = TaskRouter()

    def test_quickfix_from_title(self):
        assert self.router.classify("Fix typo in README") == TaskType.QUICKFIX
        assert self.router.classify("Fix import statement in utils.py") == TaskType.QUICKFIX
        assert self.router.classify("Fix whitespace in config") == TaskType.QUICKFIX
        assert self.router.classify("Update readme") == TaskType.QUICKFIX
        assert self.router.classify("Rename variable foo to bar") == TaskType.QUICKFIX
        assert self.router.classify("Fix lint errors") == TaskType.QUICKFIX
        assert self.router.classify("Format code in module") == TaskType.QUICKFIX


    def test_analysis_from_title(self):
        assert self.router.classify("Suggest the top 3 improvements") == TaskType.ANALYSIS
        assert self.router.classify("Find bugs in the auth module") == TaskType.ANALYSIS
        assert self.router.classify("Audit the codebase for security issues") == TaskType.ANALYSIS
        assert self.router.classify("Review for code quality issues") == TaskType.ANALYSIS
        assert self.router.classify("Analyze the performance bottlenecks") == TaskType.ANALYSIS
        assert self.router.classify("List all issues in the module") == TaskType.ANALYSIS
        assert self.router.classify("Identify potential problems") == TaskType.ANALYSIS
        assert self.router.classify("Top 5 performance bottlenecks") == TaskType.ANALYSIS

    def test_analysis_does_not_steal_quickfix(self):
        # "Fix typo" should still be QUICKFIX, not ANALYSIS
        assert self.router.classify("Fix typo in README") == TaskType.QUICKFIX

    def test_analysis_does_not_steal_bugfix(self):
        # "Fix the login button crash" should still be BUGFIX
        assert self.router.classify("Fix the login button crash") == TaskType.BUGFIX

    def test_explicit_tag_overrides_analysis_keywords(self):
        assert self.router.classify("[type:feature] Find bugs") == TaskType.FEATURE
        assert self.router.classify("[type:bugfix] Audit security") == TaskType.BUGFIX

    def test_bugfix_from_title(self):
        assert self.router.classify("Fix authentication bug") == TaskType.BUGFIX
        assert self.router.classify("Fix crash when user clicks submit") == TaskType.BUGFIX
        assert self.router.classify("Something is broken in auth") == TaskType.BUGFIX
        assert self.router.classify("Login doesn't work") == TaskType.BUGFIX
        assert self.router.classify("Bug in payment flow") == TaskType.BUGFIX

    def test_feature_from_title(self):
        # "Add dark mode" hits quickfix pattern "add...section", so use explicit tag
        assert self.router.classify("[type:feature] Add dark mode support") == TaskType.FEATURE
        assert self.router.classify("[type:feature] Implement user profile page") == TaskType.FEATURE

    def test_refactor_from_title(self):
        assert self.router.classify("Refactor authentication module") == TaskType.REFACTOR
        assert self.router.classify("Restructure the database layer") == TaskType.REFACTOR
        assert self.router.classify("Extract helper functions") == TaskType.REFACTOR
        assert self.router.classify("Migrate to new ORM") == TaskType.REFACTOR

    def test_explicit_tag_override(self):
        assert self.router.classify("[type:quickfix] Add dark mode") == TaskType.QUICKFIX
        assert self.router.classify("[type:feature] Fix typo") == TaskType.FEATURE
        assert self.router.classify("[type:bugfix] Refactor auth") == TaskType.BUGFIX
        assert self.router.classify("[type:refactor] Fix crash") == TaskType.REFACTOR

    def test_unknown_defaults_to_quickfix(self):
        # Default is quickfix — only explicit [type:feature] or truly new functionality gets feature
        assert self.router.classify("Do something cool") == TaskType.QUICKFIX

    def test_clean_title_removes_tag(self):
        assert self.router.clean_title("[type:quickfix] Add dark mode") == "Add dark mode"
        assert self.router.clean_title("[type:feature] Fix typo") == "Fix typo"
        assert self.router.clean_title("No tag here") == "No tag here"

    def test_classify_case_insensitive(self):
        assert self.router.classify("FIX TYPO in readme") == TaskType.QUICKFIX
        assert self.router.classify("REFACTOR the module") == TaskType.REFACTOR
        assert self.router.classify("[TYPE:BUGFIX] Something") == TaskType.BUGFIX

    def test_notes_do_not_affect_classification(self):
        # Notes param exists but classification is title-based
        assert self.router.classify("[type:feature] Add new feature", notes="fix typo") == TaskType.FEATURE


class TestLLMFallback:
    def test_llm_called_when_no_keyword_matches(self):
        router = TaskRouter(claude_bin="/fake/claude")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="analysis\n", stderr=""
            )
            result = router.classify(
                "What are the biggest issues in this codebase"
            )
        assert result == TaskType.ANALYSIS
        assert mock_run.called

    def test_llm_fallback_to_feature_on_error(self):
        router = TaskRouter(claude_bin="/fake/claude")
        with patch("subprocess.run", side_effect=Exception("fail")):
            result = router.classify("Some ambiguous task description")
        assert result == TaskType.QUICKFIX

    def test_llm_fallback_to_feature_on_timeout(self):
        router = TaskRouter(claude_bin="/fake/claude")
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 30),
        ):
            result = router.classify("Some ambiguous task description")
        assert result == TaskType.QUICKFIX

    def test_llm_not_called_when_keyword_matches(self):
        router = TaskRouter(claude_bin="/fake/claude")
        with patch("subprocess.run") as mock_run:
            result = router.classify("Fix typo in README")
        assert result == TaskType.QUICKFIX
        assert not mock_run.called

    def test_llm_not_called_without_claude_bin(self):
        router = TaskRouter()  # No claude_bin
        with patch("subprocess.run") as mock_run:
            result = router.classify("Some ambiguous task")
        assert result == TaskType.QUICKFIX
        assert not mock_run.called

    def test_llm_handles_bad_response(self):
        router = TaskRouter(claude_bin="/fake/claude")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="unknown_type\n", stderr=""
            )
            result = router.classify("Some ambiguous task")
        assert result == TaskType.QUICKFIX
