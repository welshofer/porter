"""Tests for StartupHealthCheck."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from lib.healthcheck import StartupHealthCheck, HealthCheckError
from lib.models import Config


@pytest.fixture
def valid_config(tmp_path):
    """Create a config with valid tool binaries and paths."""
    # Create fake binaries
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["reminders", "claude", "gh", "imsg"]:
        f = bin_dir / name
        f.touch()
        f.chmod(0o755)

    config = Config()
    config.reminders_bin = str(bin_dir / "reminders")
    config.claude_bin = str(bin_dir / "claude")
    config.gh_bin = str(bin_dir / "gh")
    config.imsg_bin = str(bin_dir / "imsg")
    config.db_path = tmp_path / "state.db"
    config.workspaces_path = tmp_path / "workspaces"
    config.logs_path = tmp_path / "logs"
    return config


class TestCheckToolBinaries:
    def test_all_binaries_exist(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        assert hc.check_tool_binaries() == []

    def test_missing_reminders_bin(self, valid_config):
        valid_config.reminders_bin = "/nonexistent/reminders"
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_tool_binaries()
        assert len(failures) == 1
        assert "reminders_bin" in failures[0]

    def test_missing_multiple_binaries(self, valid_config):
        valid_config.reminders_bin = "/nonexistent/reminders"
        valid_config.claude_bin = "/nonexistent/claude"
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_tool_binaries()
        assert len(failures) == 2

    def test_empty_imsg_bin_is_ok(self, valid_config):
        """Empty imsg_bin means notifications disabled, should not fail."""
        valid_config.imsg_bin = ""
        hc = StartupHealthCheck(valid_config)
        assert hc.check_tool_binaries() == []


class TestCheckGhAuth:
    def test_gh_auth_logged_in(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            failures = hc.check_gh_auth()
            assert failures == []

    def test_gh_auth_not_logged_in(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr="You are not logged into any GitHub hosts.",
            )
            failures = hc.check_gh_auth()
            assert len(failures) == 1
            assert "gh auth" in failures[0].lower() or "not logged in" in failures[0].lower()

    def test_gh_auth_binary_not_found(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            failures = hc.check_gh_auth()
            assert len(failures) == 1

    def test_gh_auth_timeout(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            failures = hc.check_gh_auth()
            assert len(failures) == 1
            assert "failed" in failures[0].lower() or "timed out" in failures[0].lower()


class TestCheckPaths:
    def test_paths_exist(self, valid_config, tmp_path):
        # Parent of db_path exists (tmp_path), workspaces and logs will be created
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_paths()
        assert failures == []

    def test_db_parent_not_writable(self, valid_config):
        valid_config.db_path = Path("/nonexistent/deeply/nested/state.db")
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_paths()
        assert len(failures) >= 1
        assert any("db_path" in f for f in failures)

    def test_workspaces_path_not_creatable(self, valid_config):
        valid_config.workspaces_path = Path("/nonexistent/deeply/nested/workspaces")
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_paths()
        assert len(failures) >= 1
        assert any("workspaces" in f for f in failures)

    def test_logs_path_not_creatable(self, valid_config):
        valid_config.logs_path = Path("/nonexistent/deeply/nested/logs")
        hc = StartupHealthCheck(valid_config)
        failures = hc.check_paths()
        assert len(failures) >= 1
        assert any("logs" in f for f in failures)


class TestRunAll:
    def test_all_pass(self, valid_config):
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            # Should not raise
            hc.run_all()

    def test_failures_raise_health_check_error(self, valid_config):
        valid_config.reminders_bin = "/nonexistent/reminders"
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            with pytest.raises(HealthCheckError) as exc_info:
                hc.run_all()
            assert len(exc_info.value.failures) >= 1
            assert "reminders_bin" in exc_info.value.failures[0]

    def test_health_check_error_has_failures_attribute(self, valid_config):
        valid_config.claude_bin = "/nonexistent/claude"
        valid_config.gh_bin = "/nonexistent/gh"
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not logged in")
            with pytest.raises(HealthCheckError) as exc_info:
                hc.run_all()
            # Should have failures from missing binaries AND gh auth
            assert len(exc_info.value.failures) >= 2

    def test_health_check_error_str(self, valid_config):
        valid_config.reminders_bin = "/nonexistent/reminders"
        hc = StartupHealthCheck(valid_config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            with pytest.raises(HealthCheckError) as exc_info:
                hc.run_all()
            error_str = str(exc_info.value)
            assert "reminders_bin" in error_str
