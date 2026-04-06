"""Tests for structured JSON logging with task correlation."""

import json
import logging
from io import StringIO

from lib.logging_config import JsonFormatter, task_context, set_task_context, clear_task_context


class TestJsonFormatter:
    def test_output_is_valid_json(self):
        handler = logging.StreamHandler(stream := StringIO())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.json")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("hello world")

        line = stream.getvalue().strip()
        data = json.loads(line)
        assert data["message"] == "hello world"
        assert data["level"] == "INFO"
        assert "timestamp" in data
        assert data["task_id"] is None

    def test_task_id_from_contextvar(self):
        handler = logging.StreamHandler(stream := StringIO())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.ctx")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        set_task_context("task-abc-123")
        logger.info("working on task")
        clear_task_context()

        data = json.loads(stream.getvalue().strip())
        assert data["task_id"] == "task-abc-123"

    def test_clear_resets_to_none(self):
        handler = logging.StreamHandler(stream := StringIO())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.clear")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        set_task_context("task-xyz")
        clear_task_context()
        logger.info("after clear")

        data = json.loads(stream.getvalue().strip())
        assert data["task_id"] is None

    def test_includes_logger_name(self):
        handler = logging.StreamHandler(stream := StringIO())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("lib.orchestrator")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("state transition")

        data = json.loads(stream.getvalue().strip())
        assert data["logger"] == "lib.orchestrator"

    def test_includes_exception_info(self):
        handler = logging.StreamHandler(stream := StringIO())
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("test.exc")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("caught error")

        data = json.loads(stream.getvalue().strip())
        assert "ValueError: boom" in data["exception"]
