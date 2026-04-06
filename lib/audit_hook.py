"""PostToolUse audit hook for Agent SDK sessions.

Writes one JSON line per tool call to an audit file,
correlated by task_id for traceability.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _extract_detail(tool_name: str, tool_input: dict) -> str | None:
    """Pull the most useful detail from a tool call for the audit log."""
    if tool_name in ("Read", "Write", "Edit"):
        return tool_input.get("file_path")
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:120]  # Truncate long commands
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern") or tool_input.get("query")
    if tool_name in ("WebSearch", "WebFetch"):
        return tool_input.get("query") or tool_input.get("url")
    return None


def make_audit_hook(audit_path: Path, task_id: str):
    """Create a PostToolUse callback that appends JSON lines to audit_path."""

    audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def _hook(input_data: dict, tool_use_id: str | None, context) -> dict:
        tool_name = input_data.get("tool_name", "unknown")
        tool_input = input_data.get("tool_input", {})

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "tool": tool_name,
            "tool_use_id": tool_use_id,
        }

        # Add the most useful detail based on tool type
        file_path = tool_input.get("file_path")
        if file_path:
            entry["file_path"] = file_path

        detail = _extract_detail(tool_name, tool_input)
        if detail and detail != file_path:
            entry["detail"] = detail

        try:
            with open(audit_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # Audit is best-effort; never crash the agent session

        return {}

    return _hook
