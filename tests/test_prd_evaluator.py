"""Tests for PRD evaluator-optimizer pattern."""

import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from lib.db import StateDB
from lib.prd_evaluator import PRDEvaluator, EvalResult


class TestEvalResult:
    def test_feedback_for_regeneration(self):
        """Should format issues and suggestions into feedback string."""
        result = EvalResult(passed=False, score=3, issues=["Bad criteria"], suggestions=["Add examples"])
        feedback = result.feedback_for_regeneration
        assert "Bad criteria" in feedback
        assert "Add examples" in feedback
        assert "Issues that MUST be fixed:" in feedback
        assert "Suggestions to consider:" in feedback

    def test_feedback_for_regeneration_issues_only(self):
        """Should handle feedback with only issues."""
        result = EvalResult(passed=False, score=3, issues=["Bad criteria"], suggestions=[])
        feedback = result.feedback_for_regeneration
        assert "Bad criteria" in feedback
        assert "Suggestions" not in feedback

    def test_feedback_for_regeneration_suggestions_only(self):
        """Should handle feedback with only suggestions."""
        result = EvalResult(passed=True, score=7, issues=[], suggestions=["Add examples"])
        feedback = result.feedback_for_regeneration
        assert "Add examples" in feedback
        assert "Issues" not in feedback

    def test_feedback_for_regeneration_empty(self):
        """Should return empty string when no issues or suggestions."""
        result = EvalResult(passed=True, score=9, issues=[], suggestions=[])
        assert result.feedback_for_regeneration == ""


class TestPRDEvaluator:
    def test_parse_eval_result_pass(self):
        """Should parse a passing evaluation from Claude output."""
        output = '''I've reviewed the PRD.\n\n```json\n{"verdict": "pass", "issues": [], "suggestions": ["Consider adding error handling"], "score": 8}\n```'''
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is True
        assert result.score == 8
        assert len(result.issues) == 0
        assert len(result.suggestions) == 1

    def test_parse_eval_result_fail(self):
        """Should parse a failing evaluation."""
        output = '''The PRD has issues.\n\n```json\n{"verdict": "fail", "issues": ["Acceptance criteria are not testable", "Scope is too broad"], "suggestions": [], "score": 3}\n```'''
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is False
        assert result.score == 3
        assert len(result.issues) == 2

    def test_parse_eval_result_garbage(self):
        """Should handle unparseable output gracefully."""
        result = PRDEvaluator.parse_eval_output("no json here")
        assert result.passed is False
        assert len(result.issues) > 0

    def test_parse_eval_result_pass_threshold(self):
        """Score >= 6 with no issues should pass."""
        output = '```json\n{"verdict": "pass", "issues": [], "suggestions": [], "score": 6}\n```'
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is True

    def test_parse_eval_result_fail_low_score(self):
        """Score < 6 should fail even with pass verdict."""
        output = '```json\n{"verdict": "pass", "issues": [], "suggestions": [], "score": 4}\n```'
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is False

    def test_parse_eval_result_fail_with_issues(self):
        """Having issues should fail even with pass verdict and high score."""
        output = '```json\n{"verdict": "pass", "issues": ["Something blocking"], "suggestions": [], "score": 8}\n```'
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is False

    def test_parse_eval_bare_json(self):
        """Should parse JSON without markdown fences."""
        output = '{"verdict": "pass", "issues": [], "suggestions": ["Minor nit"], "score": 7}'
        result = PRDEvaluator.parse_eval_output(output)
        assert result.passed is True
        assert result.score == 7

    def test_parse_eval_stores_raw_output(self):
        """Should store the raw output in the result."""
        output = '```json\n{"verdict": "pass", "issues": [], "suggestions": [], "score": 9}\n```'
        result = PRDEvaluator.parse_eval_output(output)
        assert result.raw_output == output

    @patch("lib.prd_evaluator.subprocess.run")
    def test_evaluate_calls_claude(self, mock_run):
        """Should invoke claude --print with the right arguments."""
        mock_run.return_value = MagicMock(
            stdout='```json\n{"verdict": "pass", "issues": [], "suggestions": [], "score": 8}\n```',
            returncode=0,
        )
        evaluator = PRDEvaluator(claude_bin="/usr/local/bin/claude", model="sonnet")
        result = evaluator.evaluate("# My PRD\nDo the thing", Path("/tmp/repo"))

        assert result.passed is True
        assert result.score == 8
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "/usr/local/bin/claude" in cmd
        assert "--print" in cmd
        assert "--model" in cmd

    @patch("lib.prd_evaluator.subprocess.run")
    def test_evaluate_handles_timeout(self, mock_run):
        """Should handle subprocess timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        evaluator = PRDEvaluator(claude_bin="/usr/local/bin/claude")
        result = evaluator.evaluate("# PRD", Path("/tmp/repo"))

        assert result.passed is False
        assert any("timeout" in issue.lower() for issue in result.issues)

    @patch("lib.prd_evaluator.subprocess.run")
    def test_evaluate_handles_nonzero_exit(self, mock_run):
        """Should handle non-zero exit code gracefully."""
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="Error: something went wrong",
            returncode=1,
        )
        evaluator = PRDEvaluator(claude_bin="/usr/local/bin/claude")
        result = evaluator.evaluate("# PRD", Path("/tmp/repo"))

        assert result.passed is False
        assert len(result.issues) > 0


class TestCalibrationSection:
    def test_too_strict_pattern(self):
        """Should detect evaluator being too strict."""
        notes = [
            {"eval_passed": False, "human_approved": True, "eval_score": 5, "eval_issues": "Vague criteria"},
            {"eval_passed": False, "human_approved": True, "eval_score": 4, "eval_issues": "Missing tests"},
        ]
        section = PRDEvaluator._build_calibration_section(notes)
        assert "TOO STRICT" in section
        assert "Vague criteria" in section

    def test_too_lenient_pattern(self):
        """Should detect evaluator being too lenient."""
        notes = [
            {"eval_passed": True, "human_approved": False, "eval_score": 7, "eval_issues": ""},
            {"eval_passed": True, "human_approved": False, "eval_score": 8, "eval_issues": ""},
        ]
        section = PRDEvaluator._build_calibration_section(notes)
        assert "TOO LENIENT" in section

    def test_mixed_no_pattern(self):
        """Should not show pattern when disagreements are balanced."""
        notes = [
            {"eval_passed": True, "human_approved": False, "eval_score": 7, "eval_issues": ""},
            {"eval_passed": False, "human_approved": True, "eval_score": 5, "eval_issues": ""},
        ]
        section = PRDEvaluator._build_calibration_section(notes)
        assert "TOO STRICT" not in section
        assert "TOO LENIENT" not in section

    @patch("lib.prd_evaluator.subprocess.run")
    def test_evaluate_passes_calibration_to_prompt(self, mock_run):
        """Should include calibration notes in prompt sent to Claude."""
        mock_run.return_value = MagicMock(
            stdout='{"verdict": "pass", "issues": [], "suggestions": [], "score": 8}',
            returncode=0,
        )
        evaluator = PRDEvaluator()
        calibration = [
            {"eval_passed": False, "human_approved": True, "eval_score": 5, "eval_issues": "Too vague"},
        ]
        evaluator.evaluate("# PRD", Path("/tmp/repo"), calibration_notes=calibration)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "Calibration Notes" in prompt_arg
        assert "TOO STRICT" in prompt_arg


class TestEvalFeedbackDB:
    @pytest.fixture
    def db(self, tmp_path):
        return StateDB(tmp_path / "test.db")

    def test_record_and_query_disagreement(self, db):
        """Should record disagreements and query them back."""
        db.record_eval_feedback("task-1", eval_passed=True, human_approved=False, eval_score=7, eval_issues="Too lenient")
        db.record_eval_feedback("task-2", eval_passed=False, human_approved=True, eval_score=5, eval_issues="Too strict")

        calibration = db.get_eval_calibration(limit=10)
        assert len(calibration) == 2
        assert calibration[0]["task_id"] == "task-2"  # most recent first
        assert calibration[0]["human_approved"] is True
        assert calibration[0]["eval_passed"] is False

    def test_agreements_excluded(self, db):
        """Should only return disagreements, not agreements."""
        db.record_eval_feedback("task-1", eval_passed=True, human_approved=True, eval_score=8)
        db.record_eval_feedback("task-2", eval_passed=False, human_approved=False, eval_score=3)

        calibration = db.get_eval_calibration()
        assert len(calibration) == 0

    def test_limit_respected(self, db):
        """Should respect the limit parameter."""
        for i in range(5):
            db.record_eval_feedback(f"task-{i}", eval_passed=True, human_approved=False, eval_score=7)

        calibration = db.get_eval_calibration(limit=3)
        assert len(calibration) == 3
