"""
PRD Evaluator-Optimizer — Quality gate for PRDs before human review.

Implements the evaluator-optimizer pattern from Anthropic's agent guide.
Uses `claude --print` (subprocess) for stateless single-shot evaluation.
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Evaluation prompt
# ---------------------------------------------------------------------------

EVAL_PROMPT = """\
You are a PRD quality evaluator. Review the following PRD and evaluate it on these 5 criteria:

1. **Testable Acceptance Criteria** — Are the acceptance criteria specific and verifiable? Could a developer write automated tests against them?
2. **Reasonable Scope** — Is this achievable in a single pull request? Is it too broad or too narrow?
3. **Clear Technical Approach** — Does the PRD identify specific files, components, patterns, or APIs to use? Is the implementation path clear?
4. **Edge Cases Covered** — Does the PRD address error handling, boundary conditions, and failure modes?
5. **No Ambiguity** — Is every requirement unambiguous? Could two developers read this and build the same thing?

For each criterion, note any issues (blocking problems that must be fixed) or suggestions (nice-to-haves).

Output your evaluation as a JSON block with this exact structure:
```json
{{
  "verdict": "pass" or "fail",
  "score": <integer 1-10>,
  "issues": ["list of blocking problems"],
  "suggestions": ["list of non-blocking improvements"]
}}
```

Rules:
- A PRD passes ONLY if score >= 6 AND there are zero blocking issues.
- If you find any blocking issue, the verdict MUST be "fail".
- Be constructive but strict. Vague acceptance criteria or missing technical details are blocking issues.

Here is the PRD to evaluate:

---
{prd_content}
---
"""


# ---------------------------------------------------------------------------
# EvalResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    """Result of PRD evaluation."""
    passed: bool
    score: int = 0  # 1-10
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    raw_output: str = ""

    @property
    def feedback_for_regeneration(self) -> str:
        """Format issues as feedback for PRD regeneration."""
        parts: List[str] = []
        if self.issues:
            parts.append("Issues that MUST be fixed:")
            for issue in self.issues:
                parts.append(f"  - {issue}")
        if self.suggestions:
            parts.append("Suggestions to consider:")
            for sug in self.suggestions:
                parts.append(f"  - {sug}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# PRDEvaluator class
# ---------------------------------------------------------------------------

class PRDEvaluator:
    """Evaluates PRD quality using Claude as a judge."""

    def __init__(
        self,
        claude_bin: str = "/opt/homebrew/bin/claude",
        model: str = "sonnet",
        timeout: int = 120,
        permission_mode: str = "bypassPermissions",
    ):
        self.claude_bin = claude_bin
        self.model = model
        self.timeout = timeout
        self.permission_mode = permission_mode

    def evaluate(
        self,
        prd_content: str,
        repo_path: Path,
        calibration_notes: Optional[List[Dict]] = None,
    ) -> EvalResult:
        """Run Claude to evaluate the PRD and return an EvalResult.

        If *calibration_notes* are provided (from past eval/human disagreements),
        they are appended to the prompt so the evaluator can self-correct.
        """
        prompt = EVAL_PROMPT.format(prd_content=prd_content)

        if calibration_notes:
            prompt += self._build_calibration_section(calibration_notes)

        cmd = [
            self.claude_bin,
            "--print",
            "--model", self.model,
            "--permission-mode", self.permission_mode,
            "-p", prompt,
        ]

        # Strip CLAUDECODE env var to avoid nested-session detection
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(repo_path),
                env=env,
            )
        except subprocess.TimeoutExpired:
            log.warning("PRD evaluation timed out after %ds", self.timeout)
            return EvalResult(
                passed=False,
                score=0,
                issues=["Evaluation timeout — could not assess PRD quality"],
                raw_output="",
            )

        if proc.returncode != 0:
            log.warning(
                "PRD evaluation failed (exit %d): %s",
                proc.returncode,
                proc.stderr[:500] if proc.stderr else "(no stderr)",
            )
            return EvalResult(
                passed=False,
                score=0,
                issues=[f"Evaluation process failed (exit {proc.returncode}): {proc.stderr or '(no output)'}"],
                raw_output=proc.stdout or "",
            )

        return self.parse_eval_output(proc.stdout)

    @staticmethod
    def _build_calibration_section(calibration_notes: List[Dict]) -> str:
        """Format past disagreements as calibration context for the evaluator."""
        lines = [
            "\n\n---\n## Calibration Notes\n"
            "The following are past cases where the evaluator's verdict disagreed "
            "with the human reviewer. Use these to calibrate your strictness:\n"
        ]
        for note in calibration_notes:
            direction = (
                "Evaluator PASSED but human REJECTED"
                if note["eval_passed"]
                else "Evaluator FAILED but human APPROVED"
            )
            score_info = f" (score {note['eval_score']})" if note.get("eval_score") else ""
            issues_info = f" Issues: {note['eval_issues']}" if note.get("eval_issues") else ""
            lines.append(f"- {direction}{score_info}.{issues_info}")

        too_strict = sum(1 for n in calibration_notes if not n["eval_passed"])
        too_lenient = sum(1 for n in calibration_notes if n["eval_passed"])
        if too_strict > too_lenient:
            lines.append(
                f"\nPattern: You have been TOO STRICT in {too_strict} cases. "
                "Consider being more lenient on borderline PRDs."
            )
        elif too_lenient > too_strict:
            lines.append(
                f"\nPattern: You have been TOO LENIENT in {too_lenient} cases. "
                "Consider raising your quality bar."
            )
        return "\n".join(lines)

    @staticmethod
    def parse_eval_output(output: str) -> EvalResult:
        """Parse Claude's evaluation output into an EvalResult."""
        # Try to extract JSON from markdown fences first
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", output, re.DOTALL)
        json_str = json_match.group(1).strip() if json_match else None

        # Fall back to bare JSON object
        if not json_str:
            bare_match = re.search(r"\{.*\}", output, re.DOTALL)
            json_str = bare_match.group(0).strip() if bare_match else None

        if not json_str:
            return EvalResult(
                passed=False,
                score=0,
                issues=["Failed to parse evaluation output — no JSON found"],
                raw_output=output,
            )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            return EvalResult(
                passed=False,
                score=0,
                issues=[f"Failed to parse evaluation JSON: {exc}"],
                raw_output=output,
            )

        score = int(data.get("score", 0))
        issues = list(data.get("issues", []))
        suggestions = list(data.get("suggestions", []))

        # Pass requires score >= 6 AND zero blocking issues
        passed = score >= 6 and len(issues) == 0

        return EvalResult(
            passed=passed,
            score=score,
            issues=issues,
            suggestions=suggestions,
            raw_output=output,
        )
