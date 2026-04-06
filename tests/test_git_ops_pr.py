from unittest.mock import patch, MagicMock
from pathlib import Path
import json
from lib.git_ops import GitOperations


def test_create_pr_detects_stale_pr():
    """Should log warning when existing PR head doesn't match local HEAD."""
    git = GitOperations("/usr/bin/gh")

    pr_view_result = MagicMock(
        returncode=0,
        stdout=json.dumps({"url": "https://github.com/o/r/pull/1", "headRefOid": "aaa111"}),
        stderr=""
    )
    local_head_result = MagicMock(returncode=0, stdout="bbb222\n", stderr="")

    with patch("subprocess.run", return_value=pr_view_result), \
         patch.object(git, "_run_git", return_value=local_head_result), \
         patch.object(git, "get_current_branch", return_value="feature"), \
         patch.object(git.logger, "warning") as mock_warn:
        url, err = git.create_pr(Path("/tmp"), "title", "body")

    assert url == "https://github.com/o/r/pull/1"
    assert "stale" in err
    assert mock_warn.called
    assert "stale" in mock_warn.call_args[0][0].lower()


def test_create_pr_no_warning_when_up_to_date():
    """Should not warn when PR head matches local HEAD."""
    git = GitOperations("/usr/bin/gh")

    pr_view_result = MagicMock(
        returncode=0,
        stdout=json.dumps({"url": "https://github.com/o/r/pull/1", "headRefOid": "aaa111"}),
        stderr=""
    )
    local_head_result = MagicMock(returncode=0, stdout="aaa111\n", stderr="")

    with patch("subprocess.run", return_value=pr_view_result), \
         patch.object(git, "_run_git", return_value=local_head_result), \
         patch.object(git, "get_current_branch", return_value="feature"), \
         patch.object(git.logger, "warning") as mock_warn:
        url, err = git.create_pr(Path("/tmp"), "title", "body")

    assert url == "https://github.com/o/r/pull/1"
    assert err == ""
    assert not mock_warn.called
