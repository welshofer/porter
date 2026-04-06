"""Tests for AgentRunner — prompt building, options building, result handling.

Does NOT test actual Agent SDK calls (those need the real CLI).
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock the claude_agent_sdk before importing agent_runner
mock_sdk = MagicMock()
sys.modules["claude_agent_sdk"] = mock_sdk
# Provide mock classes the implementation expects to import
mock_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
mock_sdk.ResultMessage = type("ResultMessage", (), {})
mock_sdk.SystemMessage = type("SystemMessage", (), {})
mock_sdk.query = MagicMock()

from lib.agent_runner import AgentConfig, AgentResult, AgentRunner, BudgetExceededError


# =============================================================================
# AgentConfig tests
# =============================================================================


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig()
        assert cfg.model == "opus"
        assert cfg.permission_mode == "bypassPermissions"
        assert cfg.max_turns == 150
        assert cfg.timeout_seconds == 3600
        assert cfg.max_budget_usd is None
        assert cfg.logs_path == Path.home() / ".claude/agents/porter/logs"

    def test_custom_values(self):
        cfg = AgentConfig(model="sonnet", max_turns=50, max_budget_usd=5.0)
        assert cfg.model == "sonnet"
        assert cfg.max_turns == 50
        assert cfg.max_budget_usd == 5.0


# =============================================================================
# AgentResult tests
# =============================================================================


class TestAgentResult:
    def test_duration_with_times(self):
        start = datetime(2026, 1, 1, 12, 0, 0)
        end = datetime(2026, 1, 1, 12, 5, 30)
        result = AgentResult(success=True, started_at=start, finished_at=end)
        assert result.duration_seconds == 330.0

    def test_duration_without_times(self):
        result = AgentResult(success=False)
        assert result.duration_seconds == 0

    def test_duration_with_only_start(self):
        result = AgentResult(success=True, started_at=datetime.now())
        assert result.duration_seconds == 0

    def test_default_fields(self):
        result = AgentResult(success=True)
        assert result.session_id is None
        assert result.result_text == ""
        assert result.stop_reason is None
        assert result.timed_out is False
        assert result.error is None
        assert result.question is None

    def test_question_field(self):
        result = AgentResult(success=False, question="What database should I use?")
        assert result.question == "What database should I use?"


# =============================================================================
# AgentRunner.build_prompt tests
# =============================================================================


class TestBuildPrompt:
    def setup_method(self):
        self.runner = AgentRunner(AgentConfig())

    def test_feature_prompt_includes_prd_and_branch(self):
        prompt = self.runner.build_prompt(
            task_type="feature",
            prd_content="## Requirements\n- Do the thing",
            branch_name="feature/do-thing",
            task_title="Do the thing",
            task_notes="Some notes here",
        )
        assert "## Requirements" in prompt
        assert "Do the thing" in prompt
        assert "feature/do-thing" in prompt
        assert "test" in prompt.lower()
        assert "commit" in prompt.lower()
        assert "do NOT push" in prompt.lower() or "do not push" in prompt.lower()

    def test_feature_prompt_includes_notes(self):
        prompt = self.runner.build_prompt(
            task_type="feature",
            prd_content="PRD content",
            branch_name="feature/x",
            task_title="Title",
            task_notes="Important context from the user",
        )
        assert "Important context from the user" in prompt

    def test_quickfix_prompt_is_minimal_no_prd(self):
        prompt = self.runner.build_prompt(
            task_type="quickfix",
            prd_content=None,
            branch_name="quickfix/typo",
            task_title="Fix typo in README",
            task_notes="Line 42",
        )
        assert "Fix typo in README" in prompt
        assert "Line 42" in prompt
        assert "quickfix/typo" in prompt
        # Quickfix should not reference PRD content
        assert "PRD" not in prompt.upper() or "prd" not in prompt.lower()
        assert "do NOT push" in prompt.lower() or "do not push" in prompt.lower()

    def test_bugfix_prompt_focuses_on_reproduction(self):
        prompt = self.runner.build_prompt(
            task_type="bugfix",
            prd_content="## Bug Report\nLogin fails",
            branch_name="bugfix/login",
            task_title="Fix login crash",
            task_notes="Users report 500 error",
        )
        assert "bugfix/login" in prompt
        assert "Fix login crash" in prompt
        # Should mention reproduction / root cause
        assert "reproduc" in prompt.lower() or "root cause" in prompt.lower()
        assert "do NOT push" in prompt.lower() or "do not push" in prompt.lower()

    def test_refactor_prompt(self):
        prompt = self.runner.build_prompt(
            task_type="refactor",
            prd_content="## Refactor Plan\nExtract helpers",
            branch_name="refactor/helpers",
            task_title="Extract helper functions",
            task_notes="",
        )
        assert "refactor/helpers" in prompt
        assert "Extract helper functions" in prompt
        # Should mention keeping tests green
        assert "test" in prompt.lower()
        assert "do NOT push" in prompt.lower() or "do not push" in prompt.lower()

    def test_retry_prompt_includes_previous_output(self):
        previous = "Error: test_foo failed\nAssertionError: expected 5 got 3\n" * 20
        prompt = self.runner.build_prompt(
            task_type="feature",
            prd_content="PRD here",
            branch_name="feature/retry",
            task_title="Retry task",
            task_notes="",
            previous_output=previous,
            attempt=2,
        )
        assert "attempt" in prompt.lower() or "retry" in prompt.lower()
        # Should include tail of previous output
        assert "AssertionError" in prompt

    def test_retry_attempt_1_no_previous_context(self):
        prompt = self.runner.build_prompt(
            task_type="feature",
            prd_content="PRD",
            branch_name="feature/first",
            task_title="First try",
            task_notes="",
            previous_output=None,
            attempt=1,
        )
        # Should NOT mention retry/previous attempt
        assert "previous attempt" not in prompt.lower()

    def test_agent_question_sentinel_in_all_prompts(self):
        for task_type in ["feature", "quickfix", "bugfix", "refactor"]:
            prompt = self.runner.build_prompt(
                task_type=task_type,
                prd_content="content",
                branch_name="branch/x",
                task_title="Title",
                task_notes="",
            )
            assert "AGENT_QUESTION:" in prompt


# =============================================================================
# AgentRunner.build_sdk_options tests
# =============================================================================


class TestBuildSdkOptions:
    def setup_method(self):
        self.runner = AgentRunner(AgentConfig())

    def test_options_has_correct_cwd(self):
        opts = self.runner.build_sdk_options(Path("/tmp/my-repo"))
        assert opts.cwd == "/tmp/my-repo"

    def test_options_has_required_tools(self):
        opts = self.runner.build_sdk_options(Path("/tmp/repo"))
        required_tools = {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"}
        assert required_tools.issubset(set(opts.allowed_tools))

    def test_options_uses_config_values(self):
        cfg = AgentConfig(model="sonnet", max_turns=75, permission_mode="default", max_budget_usd=10.0)
        runner = AgentRunner(cfg)
        opts = runner.build_sdk_options(Path("/tmp/repo"))
        assert opts.model == "sonnet"
        assert opts.max_turns == 75
        assert opts.permission_mode == "default"
        assert opts.max_budget_usd == 10.0

    def test_options_no_budget_when_none(self):
        cfg = AgentConfig(max_budget_usd=None)
        runner = AgentRunner(cfg)
        opts = runner.build_sdk_options(Path("/tmp/repo"))
        # Should not set max_budget_usd or set it to None
        assert opts.max_budget_usd is None


# =============================================================================
# AgentRunner._extract_activity tests
# =============================================================================


class TestExtractActivity:
    def _make_message(self, tool_name, tool_input):
        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_name
        block.input = tool_input
        msg = MagicMock()
        msg.content = [block]
        return msg

    def test_read_tool(self):
        msg = self._make_message("Read", {"file_path": "/src/main.py"})
        assert AgentRunner._extract_activity(msg) == "Read: /src/main.py"

    def test_bash_tool_truncates(self):
        long_cmd = "x" * 200
        msg = self._make_message("Bash", {"command": long_cmd})
        result = AgentRunner._extract_activity(msg)
        assert result.startswith("Bash: ")
        assert len(result) <= 86  # "Bash: " + 80 chars

    def test_edit_tool(self):
        msg = self._make_message("Edit", {"file_path": "/src/utils.py"})
        assert AgentRunner._extract_activity(msg) == "Edit: /src/utils.py"

    def test_no_tool_use_returns_none(self):
        block = MagicMock(type="text")
        msg = MagicMock(content=[block])
        assert AgentRunner._extract_activity(msg) is None

    def test_no_content_returns_none(self):
        msg = MagicMock(content=None)
        assert AgentRunner._extract_activity(msg) is None


# =============================================================================
# BudgetExceededError tests
# =============================================================================


class TestBudgetExceededError:
    def test_error_message_includes_costs(self):
        err = BudgetExceededError(estimated_cost=12.50, budget=10.00)
        assert "$12.50" in str(err)
        assert "$10.00" in str(err)
        assert err.estimated_cost == 12.50
        assert err.budget == 10.00

    def test_is_exception(self):
        err = BudgetExceededError(1.0, 0.5)
        assert isinstance(err, Exception)


# =============================================================================
# AgentResult.estimated_cost_usd tests
# =============================================================================


class TestAgentResultCost:
    def test_cost_calculation(self):
        result = AgentResult(
            success=True,
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=500_000,
            cache_write_tokens=200_000,
        )
        # $5/M input + $2.50 output + $0.25 cache read + $1.00 cache write = $8.75
        assert abs(result.estimated_cost_usd - 8.75) < 0.01

    def test_zero_tokens_zero_cost(self):
        result = AgentResult(success=True)
        assert result.estimated_cost_usd == 0.0

    def test_max_cost_per_task_config(self):
        cfg = AgentConfig(max_cost_per_task_usd=5.0)
        assert cfg.max_cost_per_task_usd == 5.0

    def test_timeout_preserves_session_id_and_tokens(self):
        """Timeout should capture session_id and accumulated token counts."""
        result = AgentResult(
            success=False,
            session_id="sess-123",
            result_text="partial work",
            stop_reason=None,
            started_at=datetime(2026, 1, 1, 12, 0, 0),
            finished_at=datetime(2026, 1, 1, 13, 0, 0),
            timed_out=True,
            error="Agent session timed out",
            input_tokens=1500,
            output_tokens=800,
            cache_read_tokens=200,
            cache_write_tokens=100,
        )
        assert result.session_id == "sess-123"
        assert result.timed_out is True
        assert result.input_tokens == 1500
        assert result.output_tokens == 800
        assert result.cache_read_tokens == 200
        assert result.cache_write_tokens == 100
        assert result.estimated_cost_usd > 0


# =============================================================================
# AgentRunner hard timeout enforcement tests
# =============================================================================


class TestAgentRunnerTimeout:
    def test_agent_runner_enforces_hard_timeout(self):
        """Test that agent runner times out if SDK stream stalls."""
        config = AgentConfig(timeout_seconds=2)
        runner = AgentRunner(config)

        # Create mock message types that match the isinstance checks
        SystemMsg = mock_sdk.SystemMessage
        AssistantMsg = mock_sdk.AssistantMessage
        ResultMsg = mock_sdk.ResultMessage

        # Build an init message that passes isinstance(message, SystemMessage)
        init_msg = SystemMsg()
        init_msg.subtype = "init"
        init_msg.session_id = "test-sess-timeout"

        async def stalling_query(**kwargs):
            """Yields an init message then hangs forever."""
            yield init_msg
            await asyncio.sleep(999)

        # Patch query to use our stalling generator
        original_query = mock_sdk.query
        mock_sdk.query = stalling_query

        # Re-import to pick up the patched query
        import lib.agent_runner as ar
        original_ar_query = ar.query
        ar.query = stalling_query

        try:
            result = runner.run(
                prompt="test prompt",
                repo_path=Path("/tmp"),
                task_id="t-timeout-test",
                task_type="quickfix",
            )

            assert result.timed_out, "Expected timed_out=True"
            assert not result.success
            assert result.session_id == "test-sess-timeout"
            assert result.stop_reason == "timeout"
            assert "timed out" in result.error.lower()
        finally:
            mock_sdk.query = original_query
            ar.query = original_ar_query
