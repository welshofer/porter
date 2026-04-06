from unittest.mock import patch, MagicMock
from pathlib import Path
from lib.git_ops import GitOperations


def test_push_does_not_recurse_infinitely():
    """Push should give up after a bounded number of retries."""
    git = GitOperations("/usr/bin/gh")

    # Simulate persistent "already exists" error
    mock_result = MagicMock(
        returncode=1,
        stdout="",
        stderr="error: src refspec already exists"
    )

    call_count = 0

    def counting_run_git(args, path, **kwargs):
        nonlocal call_count
        if args[0] == "push":
            call_count += 1
        return mock_result

    with patch.object(git, "_run_git", side_effect=counting_run_git), \
         patch.object(git, "get_current_branch", return_value="feature"), \
         patch.object(git, "has_commits_ahead", return_value=(True, 1)):
        success, error = git.push(Path("/tmp"), "feature")

    assert not success
    assert call_count <= 3, f"Push called {call_count} times — unbounded recursion!"


def test_push_force_retry_works_on_non_fast_forward():
    """Push should retry with force once on non-fast-forward, then stop."""
    git = GitOperations("/usr/bin/gh")

    calls = []
    def mock_run_git(args, path, **kwargs):
        calls.append(args)
        if args[0] == "push" and "--force-with-lease" not in args:
            return MagicMock(returncode=1, stdout="", stderr="non-fast-forward")
        elif args[0] == "push" and "--force-with-lease" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(git, "_run_git", side_effect=mock_run_git), \
         patch.object(git, "get_current_branch", return_value="feature"), \
         patch.object(git, "has_commits_ahead", return_value=(True, 1)):
        success, error = git.push(Path("/tmp"), "feature")

    assert success
