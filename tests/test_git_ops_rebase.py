from unittest.mock import patch, MagicMock
from pathlib import Path
from lib.git_ops import GitOperations


def test_rebase_aborts_if_unshallow_fails():
    """Rebase should not proceed if unshallow fails."""
    git = GitOperations("/usr/bin/gh")
    with patch.object(git, "unshallow", return_value=False), \
         patch.object(git, "_run_git") as mock_run, \
         patch.object(git, "get_current_branch", return_value="feature"):
        success, had_conflicts = git.rebase_onto(Path("/tmp"), "feature", "main")

    # Should NOT have called fetch or rebase
    rebase_calls = [c for c in mock_run.call_args_list if "rebase" in str(c)]
    assert not rebase_calls, "rebase should not have been called"
    assert not success, "Expected failure"
    assert not had_conflicts


def test_rebase_proceeds_if_unshallow_succeeds():
    """Rebase should proceed normally if unshallow succeeds."""
    git = GitOperations("/usr/bin/gh")
    mock_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(git, "unshallow", return_value=True), \
         patch.object(git, "_run_git", return_value=mock_result), \
         patch.object(git, "get_current_branch", return_value="feature"):
        success, had_conflicts = git.rebase_onto(Path("/tmp"), "feature", "main")

    assert success
    assert not had_conflicts
