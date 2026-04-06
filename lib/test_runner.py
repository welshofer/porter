"""
Porter — Test runner for multiple project types.

Extracted from porter.py.
"""

import json
import logging
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


class TestRunner:
    """Runs tests for different project types."""

    def __init__(self, timeout_seconds: int = 600, flaky_retries: int = 1):
        self.timeout = timeout_seconds
        self.flaky_retries = flaky_retries  # Extra attempts on failure (0 = no retry)

    def detect_test_framework(self, repo_path: Path) -> Optional[Tuple[str, List[str]]]:
        """Detect test framework and return (name, command)."""
        # Node.js / JavaScript / TypeScript
        if (repo_path / "package.json").exists():
            try:
                pkg = json.loads((repo_path / "package.json").read_text())
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    return ("npm", ["npm", "test"])
                if "vitest" in scripts or (repo_path / "vitest.config.ts").exists():
                    return ("vitest", ["npx", "vitest", "run"])
                if "jest" in scripts or (repo_path / "jest.config.js").exists():
                    return ("jest", ["npx", "jest"])
            except json.JSONDecodeError:
                pass

        # Python
        if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
            if (repo_path / "pytest.ini").exists() or list(repo_path.glob("**/test_*.py")):
                return ("pytest", ["pytest", "-v"])

        # Swift / iOS
        if (repo_path / "Package.swift").exists():
            return ("swift", ["swift", "test"])

        # Check for Xcode project with test targets
        xcodeprojs = list(repo_path.glob("*.xcodeproj"))
        if xcodeprojs:
            return ("xcodebuild", ["xcodebuild", "test", "-scheme", xcodeprojs[0].stem])

        # Go
        if (repo_path / "go.mod").exists():
            return ("go", ["go", "test", "./..."])

        # Rust
        if (repo_path / "Cargo.toml").exists():
            return ("cargo", ["cargo", "test"])

        return None

    def _run_once(self, cmd: List[str], repo_path: Path, framework: str) -> Tuple[bool, str]:
        """Run tests once. Returns (passed, output)."""
        logger = logging.getLogger('porter')
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            output = result.stdout + "\n" + result.stderr
            passed = result.returncode == 0

            if passed:
                logger.info(f"Tests passed ({framework})")
            else:
                logger.warning(f"Tests failed ({framework}): exit code {result.returncode}")

            return passed, output

        except subprocess.TimeoutExpired:
            logger.error(f"Tests timed out after {self.timeout}s")
            return False, f"Tests timed out after {self.timeout} seconds"
        except FileNotFoundError as e:
            logger.error(f"Test command not found: {e}")
            return False, f"Test command not found: {e}"
        except Exception as e:
            logger.error(f"Error running tests: {e}")
            return False, str(e)

    def run_tests(self, repo_path: Path, custom_command: Optional[str] = None) -> Tuple[bool, str]:
        """
        Run tests in the repository with automatic flaky retry.
        Returns (passed, output).
        """
        logger = logging.getLogger('porter')

        if custom_command:
            # S6: shlex.split handles quoted paths and shell metacharacters correctly
            cmd = shlex.split(custom_command)
            framework = "custom"
        else:
            detection = self.detect_test_framework(repo_path)
            if not detection:
                logger.info("No test framework detected, skipping tests")
                return True, "No test framework detected"
            framework, cmd = detection

        logger.info(f"Running tests with {framework}: {' '.join(cmd)}")

        passed, output = self._run_once(cmd, repo_path, framework)

        # Retry on failure to handle flaky tests
        if not passed and self.flaky_retries > 0:
            for retry in range(1, self.flaky_retries + 1):
                logger.info(f"Retrying tests (attempt {retry + 1}/{self.flaky_retries + 1})...")
                passed, output = self._run_once(cmd, repo_path, framework)
                if passed:
                    logger.info(f"Tests passed on retry {retry} — likely flaky test")
                    break

        return passed, output
