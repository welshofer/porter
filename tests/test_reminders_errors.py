"""Tests for RemindersClient error handling — failures must be loud, not silent."""

from unittest.mock import patch, MagicMock
from lib.reminders import RemindersClient, RemindersError


def test_show_list_raises_on_cli_failure():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        try:
            client.show_list("TestList")
            assert False, "Expected RemindersError"
        except RemindersError:
            pass


def test_show_list_raises_on_bad_json():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="bad{{{", stderr="")
        try:
            client.show_list("TestList")
            assert False, "Expected RemindersError"
        except RemindersError:
            pass


def test_show_list_returns_empty_on_valid_empty():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        assert client.show_list("TestList") == []


def test_show_list_safe_returns_empty_on_error():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = client.show_list_safe("TestList")
        assert result == []


def test_show_list_safe_returns_data_on_success():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='[{"title": "test", "isCompleted": false}]',
            stderr=""
        )
        result = client.show_list_safe("TestList")
        assert len(result) == 1
        assert result[0]["title"] == "test"


def test_show_list_error_message_includes_list_name():
    client = RemindersClient("/fake/bin")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="some error")
        try:
            client.show_list("MyList")
            assert False, "Expected RemindersError"
        except RemindersError as e:
            assert "MyList" in str(e)
