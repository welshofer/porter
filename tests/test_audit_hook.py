"""Tests for Agent SDK PostToolUse audit hook."""

import json
from pathlib import Path

from lib.audit_hook import make_audit_hook


class TestAuditHook:
    def test_writes_json_line_to_file(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        hook = make_audit_hook(audit_file, task_id="task-123")

        import asyncio
        asyncio.run(hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/repo/src/main.py", "old_string": "x", "new_string": "y"},
            },
            "tool-use-001",
            None,
        ))

        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["task_id"] == "task-123"
        assert data["tool"] == "Edit"
        assert data["file_path"] == "/repo/src/main.py"
        assert "timestamp" in data

    def test_bash_records_command(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        hook = make_audit_hook(audit_file, task_id="task-456")

        import asyncio
        asyncio.run(hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            },
            "tool-use-002",
            None,
        ))

        data = json.loads(audit_file.read_text().strip())
        assert data["tool"] == "Bash"
        assert data["detail"] == "git status"

    def test_multiple_calls_append(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        hook = make_audit_hook(audit_file, task_id="task-789")

        import asyncio
        for i in range(3):
            asyncio.run(hook(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": f"/file{i}.py"},
                },
                f"tool-use-{i}",
                None,
            ))

        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_returns_empty_dict(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"
        hook = make_audit_hook(audit_file, task_id="task-000")

        import asyncio
        result = asyncio.run(hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Glob",
                "tool_input": {"pattern": "**/*.py"},
            },
            "tool-use-003",
            None,
        ))

        assert result == {}
