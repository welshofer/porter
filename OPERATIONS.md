# Operations Runbook

Common scenarios and recovery procedures for Porter.

## Checking Daemon Health

```bash
# Quick health check (reads heartbeat file)
python3 porter.py --healthcheck

# View active tasks and status dashboard
python3 porter.py --status

# List all tasks (including completed)
python3 porter.py --list-tasks
```

## Common Scenarios

### Circuit Breaker Opened

**Symptom:** Log shows `Circuit breaker OPEN for claude — skipping task dispatch this cycle`

**Cause:** 3+ consecutive task failures triggered the breaker. It will auto-recover after `2 * poll_interval_seconds`.

**Recovery:**
1. Check logs for the root cause (API outage, auth expired, etc.)
2. Fix the underlying issue
3. The breaker will auto-close on the next successful request after the recovery timeout

**For Reminders circuit breaker:** Same pattern — `Circuit breaker OPEN for reminders`. Usually means the `reminders-cli` binary crashed or macOS Reminders is inaccessible.

### Task Stuck in AWAITING_APPROVAL

**Symptom:** Task shows `AWAITING_APPROVAL` for days.

**Cause:** User hasn't completed or deleted the approval reminder.

**What happens automatically:**
- After `approval_timeout_days` (default 7), the task auto-transitions to REJECTED
- Stale approval notifications are sent periodically

**Manual recovery:**
```bash
# Force-approve by resetting to NEW (will re-process without approval)
python3 porter.py --reset-task "task name"

# Or reject manually
python3 porter.py --cancel-task "task name"
```

### Task Stuck in ERROR

**Symptom:** `python3 porter.py --status` shows task in ERROR state.

**Diagnosis:**
1. Check the error type and message in `--status` output
2. Check logs: `~/.claude/agents/porter/logs/porter.log`
3. Check audit trail: `~/.claude/agents/porter/logs/audit.jsonl`

**Recovery:**
```bash
# Reset task for retry
python3 porter.py --reset-task "task name"
```

**Common error types:**
- `CLONE_FAILED` — network issue or repo access denied. Check `gh auth status`.
- `BUDGET_EXCEEDED` — task hit cost limit. Increase `max_cost_per_task_usd` or `daily_budget_usd`.
- `TIMEOUT` — agent ran too long. On retry, session will be resumed from where it left off.
- `IMPLEMENTATION_FAILED` — agent couldn't complete the task. Check the last output in logs.
- `PR_CREATION_FAILED` — implementation succeeded but PR failed. On retry, existing commits are preserved and only PR creation is re-attempted.
- `MERGE_FAILED` — rebase, push, or merge command failed. These are now retried with backoff (the implementation work is preserved). On retry, the system skips straight to merge.
- `MERGE_CONFLICT_FAILED` — rebase conflicts the agent couldn't resolve after 3 rounds. May need manual intervention.
- `MERGE_TEST_FAILED` — post-rebase tests failed. May pass on retry if the failure was flaky or base branch was fixed.

### Task Stuck in AGENT_RUNNING (Zombie)

**Symptom:** Task shows `AGENT_RUNNING` but no progress.

**What happens automatically:** The zombie reaper checks every poll cycle. Tasks in `AGENT_RUNNING` with no `agent_session_id` past the timeout, or with a session past 2x the timeout, are reaped to `ERROR`.

**Manual recovery:**
```bash
# Hard kill — stops agent, cleans DB, completes all reminders
python3 porter.py --kill-task "task name"

# Or kill everything
python3 porter.py --kill-all
```

### Orphan Reminders After Errors

**Symptom:** Old `[REVIEW PRD]`, `[MERGE PR #N]`, or `[QUESTION]` reminders left behind after tasks finish.

**Recovery:**
```bash
# Complete orphan reminders for finished tasks (only touches managed project lists)
python3 porter.py --cleanup-reminders
```

### Daemon Crashed Mid-Agent-Run

**Symptom:** Daemon is not running, task is in `AGENT_RUNNING` state.

**What happens:**
- The PID lock file (`~/.claude/agents/porter/logs/porter.lock`) becomes stale
- Next daemon start detects the stale lock and removes it
- Tasks in `AGENT_RUNNING` with `agent_session_id` will be resumed from their last checkpoint

**Recovery:**
```bash
# Restart the daemon — stale lock is auto-cleaned
python3 porter.py --daemon

# Or reset the task if you want a fresh start
python3 porter.py --reset-task "task name"
```

### PRD Repo Out of Sync

**Symptom:** PRD push fails with "diverged" or "non-fast-forward".

**What happens automatically:** The system does `pull --rebase` before push, and retries once on failure.

**Manual recovery:**
```bash
cd ~/.claude/agents/porter/workspaces/prds
git fetch origin
git reset --hard origin/main
```

### Disk Space Running Low

```bash
# Clean up branches and workspaces for completed/errored tasks
python3 porter.py --cleanup
```

### Database Issues

The SQLite database is at `~/.claude/agents/porter/state.db` (or as configured).

```bash
# Check integrity
sqlite3 ~/.claude/agents/porter/state.db "PRAGMA integrity_check;"

# View active tasks directly
sqlite3 ~/.claude/agents/porter/state.db \
  "SELECT task_title, state, error_type FROM tasks WHERE state NOT IN ('COMPLETE','REJECTED','ERROR');"

# View state transition history for a task
sqlite3 ~/.claude/agents/porter/state.db \
  "SELECT from_state, to_state, timestamp, details FROM task_history WHERE task_id='<id>' ORDER BY timestamp;"
```

### Viewing Cost / Usage

```bash
# Per-task costs in status view
python3 porter.py --status

# Aggregate usage from DB
sqlite3 ~/.claude/agents/porter/state.db \
  "SELECT SUM(input_tokens) as input, SUM(output_tokens) as output FROM tasks WHERE updated_at >= date('now');"
```

## Launchd Service Management

If running as a macOS launchd service:

```bash
# Check if running
launchctl list | grep porter

# Start
launchctl load ~/Library/LaunchAgents/com.porter.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.porter.plist

# View recent logs
tail -50 ~/.claude/agents/porter/logs/porter.log | python3 -m json.tool
```
