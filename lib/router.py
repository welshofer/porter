"""Task classification router for determining workflow ceremony level."""

import re
from enum import Enum


class TaskType(str, Enum):
    QUICKFIX = "quickfix"   # Skip PRD, minimal implementation
    BUGFIX = "bugfix"       # Lighter PRD, focused implementation
    FEATURE = "feature"     # Full PRD + approval ceremony
    REFACTOR = "refactor"   # Full PRD but different implementation prompt
    ANALYSIS = "analysis"   # Read-only codebase exploration, no PR
    COMPOSITE = "composite"  # Decomposes into sub-tasks via Reminders


# Tag pattern: [type:quickfix], [type:feature], etc.
_TAG_PATTERN = re.compile(r"\[type:(\w+)\]", re.IGNORECASE)

# Keyword rules: list of (TaskType, compiled_patterns) in priority order.
# Each pattern is checked against the lowercased title.
#
# Philosophy: most tasks should just be done. Only true new features and
# major refactors need the full PRD + approval ceremony. Everything else
# is a quickfix or bugfix — assign it to the agent and knock it out.
_KEYWORD_RULES: list[tuple[TaskType, list[re.Pattern]]] = [
    (TaskType.ANALYSIS, [
        re.compile(r"\bsuggest\b.*\bimprov", re.IGNORECASE),
        re.compile(r"\bfind\b.*\bbug", re.IGNORECASE),
        re.compile(r"\baudit\b", re.IGNORECASE),
        re.compile(r"\breview\b.*\b(for|code|security)\b", re.IGNORECASE),
        re.compile(r"\banalyze\b", re.IGNORECASE),
        re.compile(r"\blist\b.*\b(issues?|problems?)\b", re.IGNORECASE),
        re.compile(r"\bidentify\b", re.IGNORECASE),
        re.compile(r"\btop\s+\d+\b", re.IGNORECASE),
    ]),
    (TaskType.QUICKFIX, [
        # Explicit quickfix patterns
        re.compile(r"\bfix\s+typo\b", re.IGNORECASE),
        re.compile(r"\bfix\s+import\b", re.IGNORECASE),
        re.compile(r"\bfix\s+whitespace\b", re.IGNORECASE),
        re.compile(r"\bfix\s+lint\b", re.IGNORECASE),
        re.compile(r"\brename\b", re.IGNORECASE),
        re.compile(r"\bformat\b", re.IGNORECASE),
        # Documentation tasks — just do them, no PRD needed
        re.compile(r"\bupdate\b.*\b(doc|readme|docs|documentation|md)\b", re.IGNORECASE),
        re.compile(r"\bdocument\b", re.IGNORECASE),
        re.compile(r"\badd\b.*\b(doc|readme|section|entry|row|note)\b", re.IGNORECASE),
        re.compile(r"\bremove\b.*\b(from|claim|stale|outdated)\b", re.IGNORECASE),
        re.compile(r"\breplace\b.*\b(block|section|content)\b", re.IGNORECASE),
        re.compile(r"\bmark\b.*\b(as|implemented|complete)\b", re.IGNORECASE),
        re.compile(r"\bverify\b", re.IGNORECASE),
        # Simple modifications
        re.compile(r"\bupdate\b.*\b(count|number|version|status|list|table)\b", re.IGNORECASE),
        re.compile(r"\bfix\b.*\b(incorrect|wrong|outdated|stale|missing)\b", re.IGNORECASE),
        re.compile(r"\bupdate\b.*\bto\s+(reflect|match|include)\b", re.IGNORECASE),
    ]),
    (TaskType.REFACTOR, [
        re.compile(r"\brefactor\b", re.IGNORECASE),
        re.compile(r"\brestructure\b", re.IGNORECASE),
        re.compile(r"\bextract\b", re.IGNORECASE),
        re.compile(r"\bmigrate\b", re.IGNORECASE),
    ]),
    (TaskType.BUGFIX, [
        re.compile(r"\bfix\b", re.IGNORECASE),
        re.compile(r"\bbug\b", re.IGNORECASE),
        re.compile(r"\bcrash\b", re.IGNORECASE),
        re.compile(r"\bbroken\b", re.IGNORECASE),
        re.compile(r"\bdoesn'?t\s+work\b", re.IGNORECASE),
    ]),
]


class TaskRouter:
    """Classifies incoming tasks to determine which workflow they follow."""

    def __init__(self, claude_bin: str = ""):
        self.claude_bin = claude_bin

    def classify(self, title: str, notes: str = "") -> TaskType:
        """Classify a task based on its title.

        Classification priority:
        1. Explicit [type:X] tag in title
        2. Keyword heuristics (quickfix > refactor > bugfix)
        3. Default to FEATURE
        """
        # 1. Check for explicit tag override
        tag_match = _TAG_PATTERN.search(title)
        if tag_match:
            tag_value = tag_match.group(1).lower()
            try:
                return TaskType(tag_value)
            except ValueError:
                pass  # Unknown tag, fall through to heuristics

        # 2. Keyword heuristics (first match wins, order matters)
        for task_type, patterns in _KEYWORD_RULES:
            for pattern in patterns:
                if pattern.search(title):
                    return task_type

        # 3. LLM fallback for ambiguous tasks
        return self._classify_with_llm(title, notes)

    def _classify_with_llm(self, title: str, notes: str = "") -> TaskType:
        """Use Claude Haiku to classify ambiguous tasks.

        Called when no keyword rule matches. Falls back to FEATURE on any error.
        """
        if not self.claude_bin:
            return TaskType.QUICKFIX

        import logging
        import subprocess

        logger = logging.getLogger("porter")

        task_types = ", ".join(t.value for t in TaskType)
        prompt = (
            f"Classify this task into exactly one type: {task_types}\n\n"
            f"Title: {title}\n"
        )
        if notes:
            prompt += f"Notes: {notes}\n"
        prompt += (
            "\nRules:\n"
            "- quickfix: updates, doc changes, simple fixes, adding/removing content — MOST tasks are this\n"
            "- bugfix: fixing a specific bug or crash\n"
            "- feature: ONLY for building genuinely new user-facing functionality\n"
            "- refactor: restructuring code without changing behavior\n"
            "- analysis: read-only exploration (suggest improvements, audit, review)\n"
            "- composite: almost never used — only for explicitly multi-project work\n"
            "\nWhen in doubt, classify as quickfix. Only use feature for truly new capabilities.\n"
            "\nRespond with ONLY the type name, nothing else."
        )

        try:
            result = subprocess.run(
                [self.claude_bin, "--print", "-p", prompt, "--model", "haiku"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                classification = result.stdout.strip().lower()
                try:
                    task_type = TaskType(classification)
                    logger.info(
                        "LLM classified '%s' as %s", title, task_type.value
                    )
                    return task_type
                except ValueError:
                    logger.warning(
                        "LLM returned unknown type '%s' for '%s'",
                        classification,
                        title,
                    )
        except subprocess.TimeoutExpired:
            logger.warning("LLM classification timed out for '%s'", title)
        except FileNotFoundError:
            logger.warning(
                "Claude binary not found at '%s'", self.claude_bin
            )
        except Exception as e:
            logger.warning(
                "LLM classification failed for '%s': %s", title, e
            )

        return TaskType.QUICKFIX

    def clean_title(self, title: str) -> str:
        """Remove routing tags like [type:X] from the title."""
        return _TAG_PATTERN.sub("", title).strip()
