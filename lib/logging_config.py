"""Structured JSON logging with task correlation via contextvars."""

import json
import logging
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone

# ── Task correlation ────────────────────────────────────────────
# Set this at task-processing entry points; every log line emitted
# while the var is set will include the task_id automatically.
task_context: ContextVar[str | None] = ContextVar("task_context", default=None)


def set_task_context(task_id: str) -> None:
    """Set the current task ID for log correlation."""
    task_context.set(task_id)


def clear_task_context() -> None:
    """Clear the task ID (e.g. after task processing completes)."""
    task_context.set(None)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line with automatic task_id injection."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": task_context.get(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str)
