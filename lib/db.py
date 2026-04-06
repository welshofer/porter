"""
Porter — SQLite persistence for task state.

Extracted from porter.py.
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from lib.models import (
    VALID_TRANSITIONS,
    ClaudeUsage,
    Task,
    TaskError,
    TaskPriority,
    TaskSource,
    TaskState,
)

logger = logging.getLogger(__name__)


class StateDB:
    """SQLite persistence for task state with connection pooling and transaction support."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._in_transaction = threading.local()  # R12: per-thread nesting guard
        self._init_db()
        # S4: restrict DB file permissions — it contains task history and session IDs
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local connection (connection pooling)."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn.execute("PRAGMA busy_timeout = 5000")
        return self._local.conn

    def close(self) -> None:
        """Close the thread-local connection (call during shutdown)."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    @contextmanager
    def transaction(self):
        """Context manager for atomic transactions.

        R12: prevents nesting — SQLite doesn't support nested transactions,
        so reuse the connection without a new BEGIN if already in a transaction.
        """
        already = getattr(self._in_transaction, 'active', False)
        conn = self._get_connection()
        if already:
            # Nested call — just yield the existing connection, no commit/rollback
            yield conn
            return
        self._in_transaction.active = True
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._in_transaction.active = False

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    reminder_id TEXT UNIQUE NOT NULL,
                    reminder_list TEXT NOT NULL,
                    task_title TEXT NOT NULL,
                    task_notes TEXT,
                    repo_url TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'NEW',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    local_repo_path TEXT,
                    branch_name TEXT,
                    prd_path TEXT,
                    prd_url TEXT,
                    pr_url TEXT,
                    pr_number INTEGER,
                    pr_reminder_title TEXT,
                    pr_reminder_id TEXT,
                    error_message TEXT,
                    error_type TEXT,
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at TEXT,
                    approval_reminder_title TEXT,
                    approval_reminder_id TEXT,
                    question_reminder_title TEXT,
                    question_reminder_id TEXT,
                    question_text TEXT,
                    priority INTEGER DEFAULT 5,
                    source TEXT DEFAULT 'REMINDERS',
                    github_issue_number INTEGER,
                    last_fix_comment_id INTEGER,
                    last_review_comment_id INTEGER,
                    implementation_started_at TEXT,
                    implementation_finished_at TEXT,
                    test_fix_iterations INTEGER DEFAULT 0,
                    last_test_output TEXT,
                    tests_written INTEGER DEFAULT 0,
                    verification_result TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_reminder_list ON tasks(reminder_list)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_retry ON tasks(next_retry_at)")
            # P2: compound index for get_all_active_tasks() — the most common query path
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state_priority_created ON tasks(state, priority, created_at)")
            # P7: index for title-prefix dedup lookups
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_list_title ON tasks(reminder_list, task_title)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS claude_usage (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    model TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    turns_used INTEGER DEFAULT 0,
                    output_file TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_task ON claude_usage(task_id)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS eval_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    eval_passed INTEGER NOT NULL,
                    human_approved INTEGER NOT NULL,
                    eval_score INTEGER,
                    eval_issues TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            """)

            self._ensure_columns(conn)
            conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Add missing columns for forward-compatible schema updates."""
        conn.row_factory = sqlite3.Row
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        columns = {
            "approval_reminder_id": "TEXT",
            "question_reminder_id": "TEXT",
            "pr_number": "INTEGER",
            "pr_reminder_title": "TEXT",
            "pr_reminder_id": "TEXT",
            "error_type": "TEXT",
            "next_retry_at": "TEXT",
            "priority": "INTEGER DEFAULT 5",
            "source": "TEXT DEFAULT 'REMINDERS'",
            "github_issue_number": "INTEGER",
            "last_fix_comment_id": "INTEGER",
            "last_review_comment_id": "INTEGER",
            "implementation_started_at": "TEXT",
            "implementation_finished_at": "TEXT",
            # Test-fix loop and verification columns
            "test_fix_iterations": "INTEGER DEFAULT 0",
            "last_test_output": "TEXT",
            "tests_written": "INTEGER DEFAULT 0",
            "verification_result": "TEXT",
            # Merge flow columns
            "merge_reminder_title": "TEXT",
            "merge_reminder_id": "TEXT",
            "merge_fix_iterations": "INTEGER DEFAULT 0",
            "merge_strategy": "TEXT DEFAULT 'squash'",
            # Enhanced implementation columns
            "last_implementation_output": "TEXT",
            "implementation_session_id": "TEXT",
            # v4 new columns
            "task_type": "TEXT DEFAULT 'feature'",
            "prd_eval_result": "TEXT",
            "prd_eval_passed": "INTEGER DEFAULT 0",
            "agent_session_id": "TEXT",
            # Token usage tracking
            "input_tokens": "INTEGER DEFAULT 0",
            "output_tokens": "INTEGER DEFAULT 0",
            # Parallelism fix: status message for transition context
            "status_message": "TEXT",
            # F8: explicit dispatch queue flag
            "queued_for_dispatch": "INTEGER DEFAULT 0",
        }
        for name, col_type in columns.items():
            if name not in existing:
                # SAFETY: name and col_type are from the hardcoded dict above —
                # never from external input.  Do not populate this dict dynamically.
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {col_type}")

    def save_task(self, task: Task) -> None:
        """Save or update a task (wrapped in a transaction for atomicity)."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO tasks (
                    id, reminder_id, reminder_list, task_title, task_notes,
                    repo_url, state, created_at, updated_at, local_repo_path,
                    branch_name, prd_path, prd_url, pr_url, pr_number, pr_reminder_title, pr_reminder_id, error_message,
                    error_type, retry_count, next_retry_at, approval_reminder_title,
                    question_reminder_title, approval_reminder_id, question_reminder_id,
                    question_text, priority, source, github_issue_number, last_fix_comment_id, last_review_comment_id,
                    implementation_started_at, implementation_finished_at,
                    test_fix_iterations, last_test_output, tests_written, verification_result,
                    merge_reminder_title, merge_reminder_id, merge_fix_iterations, merge_strategy,
                    last_implementation_output, implementation_session_id,
                    task_type, prd_eval_result, prd_eval_passed, agent_session_id,
                    input_tokens, output_tokens, status_message, queued_for_dispatch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.id, task.reminder_id, task.reminder_list, task.task_title,
                task.task_notes, task.repo_url, task.state.value,
                task.created_at.isoformat(), task.updated_at.isoformat(),
                task.local_repo_path, task.branch_name, task.prd_path,
                task.prd_url, task.pr_url, task.pr_number, task.pr_reminder_title, task.pr_reminder_id, task.error_message,
                task.error_type.value if task.error_type else None,
                task.retry_count,
                task.next_retry_at.isoformat() if task.next_retry_at else None,
                task.approval_reminder_title, task.question_reminder_title,
                task.approval_reminder_id, task.question_reminder_id,
                task.question_text, task.priority.value, task.source.value,
                task.github_issue_number, task.last_fix_comment_id, task.last_review_comment_id,
                task.implementation_started_at.isoformat() if task.implementation_started_at else None,
                task.implementation_finished_at.isoformat() if task.implementation_finished_at else None,
                task.test_fix_iterations, task.last_test_output, 1 if task.tests_written else 0, task.verification_result,
                task.merge_reminder_title, task.merge_reminder_id, task.merge_fix_iterations, task.merge_strategy,
                task.last_implementation_output, task.implementation_session_id,
                task.task_type, task.prd_eval_result, 1 if task.prd_eval_passed else 0, task.agent_session_id,
                task.input_tokens, task.output_tokens, task.status_message,
                1 if task.queued_for_dispatch else 0,
            ))

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by its primary ID."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def get_task_by_reminder_id(self, reminder_id: str) -> Optional[Task]:
        """Get a non-terminal task by its reminder ID.

        Excludes COMPLETE/REJECTED/ERROR so killed or finished tasks don't
        block re-discovery of the same reminder.
        """
        terminal_states = [TaskState.COMPLETE.value, TaskState.REJECTED.value, TaskState.ERROR.value]
        conn = self._get_connection()
        placeholders = ",".join("?" * len(terminal_states))
        row = conn.execute(
            f"SELECT * FROM tasks WHERE reminder_id = ? AND state NOT IN ({placeholders})",
            [reminder_id] + terminal_states,
        ).fetchone()
        return self._row_to_task(row) if row else None

    def get_task_by_github_issue(self, repo_url: str, issue_number: int) -> Optional[Task]:
        """Get a task by its GitHub issue number."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM tasks WHERE repo_url = ? AND github_issue_number = ?",
            (repo_url, issue_number)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def get_tasks_by_state(self, state: TaskState) -> List[Task]:
        """Get all tasks in a given state, ordered by priority then creation time."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY priority ASC, created_at ASC",
            (state.value,)
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_ready_for_retry(self) -> List[Task]:
        """Get tasks ready for retry (next_retry_at has passed)."""
        conn = self._get_connection()
        now = datetime.now().isoformat()
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE state = 'NEW' AND next_retry_at IS NOT NULL AND next_retry_at <= ?
               ORDER BY priority ASC, next_retry_at ASC""",
            (now,)
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_all_active_tasks(self) -> List[Task]:
        """Get all tasks not in terminal states."""
        terminal_states = [TaskState.COMPLETE.value, TaskState.REJECTED.value, TaskState.ERROR.value]
        conn = self._get_connection()
        placeholders = ",".join("?" * len(terminal_states))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE state NOT IN ({placeholders}) ORDER BY priority ASC, created_at ASC",
            terminal_states
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_active_task_by_title_prefix(self, reminder_list: str, title_prefix: str) -> Optional[Task]:
        """Find an active task whose title starts with the given prefix.

        Used as a dedup fallback when reminder_id changes (e.g., user edits title).
        Only matches tasks NOT in terminal states (COMPLETE, REJECTED, ERROR).
        """
        terminal_states = [TaskState.COMPLETE.value, TaskState.REJECTED.value, TaskState.ERROR.value]
        conn = self._get_connection()
        placeholders = ",".join("?" * len(terminal_states))
        # Escape LIKE wildcards so literal % and _ in titles are not treated as patterns
        escaped_prefix = title_prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        row = conn.execute(
            f"""SELECT * FROM tasks
                WHERE reminder_list = ? AND task_title LIKE ? ESCAPE '\\' AND state NOT IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            [reminder_list, f"{escaped_prefix}%"] + terminal_states
        ).fetchone()
        return self._row_to_task(row) if row else None

    def get_tasks_by_project(self, reminder_list: str) -> List[Task]:
        """Get all active tasks for a specific project."""
        terminal_states = [TaskState.COMPLETE.value, TaskState.REJECTED.value, TaskState.ERROR.value]
        conn = self._get_connection()
        placeholders = ",".join("?" * len(terminal_states))
        rows = conn.execute(
            f"""SELECT * FROM tasks
                WHERE reminder_list = ? AND state NOT IN ({placeholders})
                ORDER BY priority ASC, created_at ASC""",
            [reminder_list] + terminal_states
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def transition_if(
        self, task_id: str, expected_state: TaskState,
        new_state: TaskState, notes: str = "",
    ) -> bool:
        """Atomically transition task state using compare-and-swap.

        Returns True if the transition succeeded (state was expected_state).
        Returns False if the task was already in a different state (stale).
        Raises ValueError if the transition violates VALID_TRANSITIONS.

        R13: the UPDATE and history INSERT are wrapped in a single transaction
        so both succeed or neither does.
        """
        # D4: validate the state machine — warn on unexpected transitions but
        # NEVER crash.  A running system that logs warnings is infinitely more
        # useful than a dead system that enforced purity.
        allowed = VALID_TRANSITIONS.get(expected_state, set())
        if new_state not in allowed:
            logger.warning(
                "Unexpected state transition: %s -> %s (expected one of: %s). "
                "Proceeding anyway — please update VALID_TRANSITIONS.",
                expected_state.value, new_state.value,
                sorted(s.value for s in allowed),
            )

        now = datetime.now().isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state=?, updated_at=?, status_message=? "
                "WHERE id=? AND state=?",
                (new_state.value, now, notes or None,
                 task_id, expected_state.value),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute("""
                INSERT INTO task_history (task_id, from_state, to_state, timestamp, details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id,
                expected_state.value,
                new_state.value,
                now,
                notes,
            ))
        return True

    def log_transition(self, task_id: str, from_state: Optional[TaskState],
                       to_state: TaskState, details: str = None) -> None:
        """Log a state transition (standalone — used outside transition_if)."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO task_history (task_id, from_state, to_state, timestamp, details)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task_id,
                from_state.value if from_state else None,
                to_state.value,
                datetime.now().isoformat(),
                details
            ))

    def save_usage(self, usage: ClaudeUsage) -> None:
        """Save Claude usage record."""
        conn = self._get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO claude_usage (
                id, task_id, phase, model, started_at, finished_at, turns_used, output_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            usage.id, usage.task_id, usage.phase, usage.model,
            usage.started_at.isoformat(),
            usage.finished_at.isoformat() if usage.finished_at else None,
            usage.turns_used, usage.output_file
        ))
        conn.commit()

    def get_usage_by_task(self, task_id: str) -> List[ClaudeUsage]:
        """Get all usage records for a task."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM claude_usage WHERE task_id = ? ORDER BY started_at",
            (task_id,)
        ).fetchall()
        return [self._row_to_usage(row) for row in rows]

    def get_total_usage(self, since: Optional[datetime] = None) -> Dict[str, int]:
        """Get total usage statistics."""
        conn = self._get_connection()
        if since:
            rows = conn.execute(
                "SELECT phase, SUM(turns_used) as total FROM claude_usage WHERE started_at >= ? GROUP BY phase",
                (since.isoformat(),)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT phase, SUM(turns_used) as total FROM claude_usage GROUP BY phase"
            ).fetchall()
        return {row["phase"]: row["total"] or 0 for row in rows}

    def get_daily_cost_estimate(self) -> float:
        """Estimate total cost for today across all tasks (for daily budget cap).

        Uses Opus pricing: $5/M input, $25/M output.
        """
        conn = self._get_connection()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = conn.execute(
            """SELECT COALESCE(SUM(input_tokens), 0) AS total_in,
                      COALESCE(SUM(output_tokens), 0) AS total_out
               FROM tasks WHERE updated_at >= ?""",
            (today,),
        ).fetchone()
        total_in = row["total_in"]
        total_out = row["total_out"]
        return total_in * 5.0 / 1_000_000 + total_out * 25.0 / 1_000_000

    # -------------------------------------------------------------------------
    # Evaluator learning (eval_feedback table)
    # -------------------------------------------------------------------------

    def record_eval_feedback(
        self,
        task_id: str,
        eval_passed: bool,
        human_approved: bool,
        eval_score: Optional[int] = None,
        eval_issues: Optional[str] = None,
    ) -> None:
        """Record a disagreement (or agreement) between evaluator and human."""
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO eval_feedback
               (task_id, eval_passed, human_approved, eval_score, eval_issues, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                1 if eval_passed else 0,
                1 if human_approved else 0,
                eval_score,
                eval_issues,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()

    def get_eval_calibration(self, limit: int = 20) -> List[Dict]:
        """Get recent eval/human disagreements for calibration context.

        Returns rows where evaluator and human disagreed, most recent first.
        """
        conn = self._get_connection()
        rows = conn.execute(
            """SELECT task_id, eval_passed, human_approved, eval_score, eval_issues, timestamp
               FROM eval_feedback
               WHERE eval_passed != human_approved
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "eval_passed": bool(row["eval_passed"]),
                "human_approved": bool(row["human_approved"]),
                "eval_score": row["eval_score"],
                "eval_issues": row["eval_issues"],
            }
            for row in rows
        ]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert a database row to a Task object.

        All columns are guaranteed to exist via _ensure_columns() at startup.
        """
        return Task(
            id=row["id"],
            reminder_id=row["reminder_id"],
            reminder_list=row["reminder_list"],
            task_title=row["task_title"],
            task_notes=row["task_notes"] or "",
            repo_url=row["repo_url"],
            state=TaskState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            local_repo_path=row["local_repo_path"],
            branch_name=row["branch_name"],
            prd_path=row["prd_path"],
            prd_url=row["prd_url"],
            pr_url=row["pr_url"],
            pr_number=row["pr_number"],
            pr_reminder_title=row["pr_reminder_title"],
            pr_reminder_id=row["pr_reminder_id"],
            error_message=row["error_message"],
            error_type=TaskError(row["error_type"]) if row["error_type"] else None,
            retry_count=row["retry_count"],
            next_retry_at=datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None,
            approval_reminder_title=row["approval_reminder_title"],
            approval_reminder_id=row["approval_reminder_id"],
            question_reminder_title=row["question_reminder_title"],
            question_reminder_id=row["question_reminder_id"],
            question_text=row["question_text"],
            priority=TaskPriority(row["priority"]) if row["priority"] is not None else TaskPriority.DEFAULT,
            source=TaskSource(row["source"]) if row["source"] else TaskSource.REMINDERS,
            github_issue_number=row["github_issue_number"],
            last_fix_comment_id=row["last_fix_comment_id"],
            last_review_comment_id=row["last_review_comment_id"],
            implementation_started_at=datetime.fromisoformat(row["implementation_started_at"]) if row["implementation_started_at"] else None,
            implementation_finished_at=datetime.fromisoformat(row["implementation_finished_at"]) if row["implementation_finished_at"] else None,
            test_fix_iterations=row["test_fix_iterations"] or 0,
            last_test_output=row["last_test_output"],
            tests_written=bool(row["tests_written"]),
            verification_result=row["verification_result"],
            merge_reminder_title=row["merge_reminder_title"],
            merge_reminder_id=row["merge_reminder_id"],
            merge_fix_iterations=row["merge_fix_iterations"] or 0,
            merge_strategy=row["merge_strategy"] or "squash",
            last_implementation_output=row["last_implementation_output"],
            implementation_session_id=row["implementation_session_id"],
            task_type=row["task_type"] or "feature",
            prd_eval_result=row["prd_eval_result"],
            prd_eval_passed=bool(row["prd_eval_passed"]),
            agent_session_id=row["agent_session_id"],
            input_tokens=row["input_tokens"] or 0,
            output_tokens=row["output_tokens"] or 0,
            status_message=row["status_message"],
            queued_for_dispatch=bool(row["queued_for_dispatch"]),
        )

    def _row_to_usage(self, row: sqlite3.Row) -> ClaudeUsage:
        """Convert a database row to a ClaudeUsage object."""
        return ClaudeUsage(
            id=row["id"],
            task_id=row["task_id"],
            phase=row["phase"],
            model=row["model"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            turns_used=row["turns_used"],
            output_file=row["output_file"]
        )
