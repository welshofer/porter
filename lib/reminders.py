"""
Porter — Reminders CLI client.

Extracted from porter.py.
"""

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple


class RemindersError(Exception):
    """Raised when the Reminders CLI fails or returns unparseable output."""
    pass


_DEFAULT_CLI_TIMEOUT = 30  # seconds — prevent hung reminders-cli from blocking daemon


class RemindersClient:
    """Wrapper around reminders-cli."""

    def __init__(self, bin_path: str, applescript_timeout_seconds: int = 5,
                 cli_timeout_seconds: int = _DEFAULT_CLI_TIMEOUT):
        self.bin = bin_path
        self.applescript_timeout_seconds = applescript_timeout_seconds
        self.cli_timeout = cli_timeout_seconds

    @staticmethod
    def _escape_applescript(s: str) -> str:
        """Escape string for safe use in AppleScript (prevents injection)."""
        return s.replace('\\', '\\\\').replace('"', '\\"')

    def show_list(self, list_name: str, include_completed: bool = False) -> List[Dict]:
        """Get all reminders in a list as JSON.

        Raises RemindersError on CLI failure or unparseable output.
        """
        if include_completed:
            cmd = [self.bin, "show", list_name, "--include-completed", "--format", "json"]
        else:
            cmd = [self.bin, "show", list_name, "--format", "json"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=self.cli_timeout,
            )
        except subprocess.TimeoutExpired:
            raise RemindersError(
                f"reminders-cli timed out after {self.cli_timeout}s for list '{list_name}'"
            )
        if result.returncode != 0:
            raise RemindersError(
                f"reminders-cli exited {result.returncode} for list '{list_name}': "
                f"stderr={result.stderr.strip()}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RemindersError(
                f"Unparseable JSON from reminders-cli for list '{list_name}': {e}"
            ) from e

    def show_list_safe(self, list_name: str, include_completed: bool = False) -> List[Dict]:
        """show_list() but logs and returns [] on error instead of raising."""
        try:
            return self.show_list(list_name, include_completed)
        except RemindersError as e:
            logging.getLogger('porter').error("Reminders error (non-fatal): %s", e)
            return []

    def add_reminder(self, list_name: str, title: str, notes: str = None) -> bool:
        """Add a new reminder with optional notes."""
        cmd = [self.bin, "add", list_name, title]
        if notes:
            cmd.extend(["--notes", notes])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cli_timeout)
        except subprocess.TimeoutExpired:
            logging.getLogger('porter').error("reminders-cli add timed out for '%s'", title)
            return False
        return result.returncode == 0

    def edit_reminder(self, list_name: str, index: int,
                      new_title: Optional[str] = None,
                      new_notes: Optional[str] = None) -> bool:
        """Edit a reminder's title and/or notes by index."""
        if new_title is None and new_notes is None:
            return True
        cmd = [self.bin, "edit", list_name, str(index)]
        if new_title is not None:
            cmd.append(new_title)
        if new_notes is not None:
            cmd.extend(["--notes", new_notes])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cli_timeout)
        except subprocess.TimeoutExpired:
            logging.getLogger('porter').error("reminders-cli edit timed out for index %d in '%s'", index, list_name)
            return False
        if result.returncode != 0:
            logging.getLogger('porter').error(
                "Failed to edit reminder at index %d in '%s': stdout=%s, stderr=%s",
                index, list_name, result.stdout.strip(), result.stderr.strip()
            )
        return result.returncode == 0

    def edit_by_external_id(self, list_name: str, external_id: str,
                            new_title: Optional[str] = None,
                            new_notes: Optional[str] = None) -> bool:
        """Edit a reminder by its externalId."""
        reminders = self.show_list_safe(list_name)
        for i, r in enumerate(reminders):
            if r.get("externalId") == external_id:
                return self.edit_reminder(list_name, i, new_title, new_notes)
        logging.getLogger('porter').warning("Could not find reminder with externalId: %s", external_id)
        return False

    def complete_by_index(self, list_name: str, index: int) -> bool:
        """Complete a reminder by its index. Returns True if successful."""
        try:
            result = subprocess.run(
                [self.bin, "complete", list_name, str(index)],
                capture_output=True, text=True, timeout=self.cli_timeout,
            )
        except subprocess.TimeoutExpired:
            logging.getLogger('porter').error("reminders-cli complete timed out for index %d in '%s'", index, list_name)
            return False
        if result.returncode != 0:
            logging.getLogger('porter').error(
                f"Failed to complete reminder at index {index} in '{list_name}': "
                f"stdout={result.stdout}, stderr={result.stderr}"
            )
        return result.returncode == 0

    def complete_by_external_id(self, list_name: str, external_id: str) -> bool:
        """
        Complete a reminder by its externalId. More reliable than index-based completion.
        Returns True if successful.
        """
        logger = logging.getLogger('porter')

        reminders = self.show_list_safe(list_name)
        for i, r in enumerate(reminders):
            if r.get("externalId") == external_id:
                logger.debug(f"Found reminder with externalId {external_id} at index {i}: {r.get('title')}")
                return self.complete_by_index(list_name, i)

        logger.warning(f"Could not find reminder with externalId: {external_id}")
        return False

    def complete_by_title(self, list_name: str, title: str, case_sensitive: bool = True) -> bool:
        """
        Complete a reminder by its title. Handles index shifting by re-fetching.
        Returns True if successful.
        """
        logger = logging.getLogger('porter')

        reminders = self.show_list_safe(list_name)

        for i, r in enumerate(reminders):
            reminder_title = r.get("title", "")
            if case_sensitive:
                match = reminder_title == title
            else:
                match = reminder_title.lower() == title.lower()

            if match:
                logger.info(f"Found reminder '{title}' at index {i}")
                success = self.complete_by_index(list_name, i)
                if success:
                    logger.info(f"Successfully completed '{title}'")
                return success

        logger.warning(f"Could not find reminder with title: '{title}'")
        logger.debug(f"Available reminders: {[r.get('title') for r in reminders]}")
        return False

    def find_reminder(self, list_name: str, title: str) -> Optional[Tuple[int, Dict]]:
        """Find a reminder by exact title match. Returns (index, reminder_dict)."""
        reminders = self.show_list_safe(list_name)
        for i, r in enumerate(reminders):
            if r.get("title") == title:
                return (i, r)
        return None

    def find_external_id_by_title(self, list_name: str, title: str) -> Optional[str]:
        """Find a reminder's externalId by exact title match."""
        result = self.find_reminder(list_name, title)
        if result:
            _, reminder = result
            return reminder.get("externalId")
        return None

    def find_reminder_by_prefix(self, list_name: str, prefix: str) -> List[Tuple[int, Dict]]:
        """Find reminders by title prefix. Returns list of (index, reminder_dict)."""
        reminders = self.show_list_safe(list_name)
        results = []
        for i, r in enumerate(reminders):
            if r.get("title", "").startswith(prefix):
                results.append((i, r))
        return results

    def find_reminder_by_prefix_icase(self, list_name: str, prefix: str) -> List[Tuple[int, Dict]]:
        """Find reminders by title prefix (case-insensitive). Returns list of (index, reminder_dict)."""
        reminders = self.show_list_safe(list_name)
        results = []
        prefix_lower = prefix.lower()
        for i, r in enumerate(reminders):
            if r.get("title", "").lower().startswith(prefix_lower):
                results.append((i, r))
        return results

    def is_completed(self, list_name: str, title: str) -> Optional[bool]:
        """
        Check if a reminder is completed. Returns:
        - True if completed
        - False if exists but not completed
        - None if not found (deleted)
        """
        reminders = self.show_list_safe(list_name, include_completed=True)
        if not reminders:
            return None
        for r in reminders:
            if r.get("title") == title:
                return r.get("isCompleted", False)
        return None  # Not found = deleted

    def is_completed_by_external_id(self, list_name: str, external_id: str) -> Optional[bool]:
        """
        Check if a reminder with externalId is completed.
        Returns True/False if found, None if not found.
        """
        reminders = self.show_list_safe(list_name, include_completed=True)
        if not reminders:
            return None
        for r in reminders:
            if r.get("externalId") == external_id:
                return r.get("isCompleted", False)
        return None

    def get_notes_via_applescript(self, list_name: str, title: str) -> Optional[str]:
        """Get notes field via AppleScript (reminders-cli doesn't expose it in JSON)."""
        escaped_title = self._escape_applescript(title)
        escaped_list = self._escape_applescript(list_name)
        timeout_seconds = max(1, int(self.applescript_timeout_seconds))
        script = f'''
        with timeout of {timeout_seconds} seconds
            tell application "Reminders"
                try
                    set r to reminder "{escaped_title}" in list "{escaped_list}"
                    return body of r
                on error
                    return ""
                end try
            end tell
        end timeout
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True,
                timeout=timeout_seconds + 1
            )
        except subprocess.TimeoutExpired:
            logging.getLogger('porter').warning(
                f"AppleScript notes fetch timed out for reminder '{title}' in '{list_name}'"
            )
            return None
        if result.returncode == 0:
            notes = result.stdout.strip()
            logging.getLogger('porter').debug(
                f"AppleScript notes fetch succeeded for reminder '{title}' in '{list_name}'"
            )
            return notes
        logging.getLogger('porter').warning(
            f"AppleScript notes fetch failed for reminder '{title}' in '{list_name}': "
            f"stderr={result.stderr.strip()}"
        )
        return None


class RemindersCache(RemindersClient):
    """P6: per-poll-cycle cache that wraps a RemindersClient.

    Caches show_list results so repeated lookups within a single poll cycle
    (approval checks, question checks, merge checks) don't spawn redundant
    subprocess calls.  All higher-level methods (is_completed, find_reminder,
    etc.) automatically benefit because they call show_list_safe() internally.

    Usage:
        cache = RemindersCache(raw_client)
        # ... poll cycle uses cache as if it were the client ...
        cache.clear()  # discard at end of cycle
    """

    def __init__(self, client: RemindersClient):
        # Copy the underlying client's config — don't call super().__init__
        self.bin = client.bin
        self.applescript_timeout_seconds = client.applescript_timeout_seconds
        self.cli_timeout = client.cli_timeout
        self._cache: Dict[Tuple[str, bool], List[Dict]] = {}

    def show_list(self, list_name: str, include_completed: bool = False) -> List[Dict]:
        key = (list_name, include_completed)
        if key not in self._cache:
            self._cache[key] = super().show_list(list_name, include_completed)
        return list(self._cache[key])  # return copy to prevent mutation

    def show_list_safe(self, list_name: str, include_completed: bool = False) -> List[Dict]:
        try:
            return self.show_list(list_name, include_completed)
        except RemindersError:
            return []

    def clear(self) -> None:
        """Discard all cached data (call at end of poll cycle)."""
        self._cache.clear()
