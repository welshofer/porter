# Porter v3.0 — User Manual

A macOS daemon that monitors Apple Reminders for coding tasks, uses Claude Code to autonomously implement them, and coordinates via Reminders for approvals, Q&A, and merging.

## Quick Start

```bash
# One poll cycle
python3 porter.py

# Continuous daemon (for testing)
python3 porter.py --daemon

# Check health
python3 porter.py --health

# View active tasks
python3 porter.py --status
```

## How It Works

### The Full Lifecycle

```
1. Create reminder    "Task: Add dark mode support"
2. Agent generates    PRD pushed to GitHub, approval reminder created
3. You review         Read PRD, complete reminder to approve (or delete to reject)
4. Agent implements   Single Claude session: implement + test + fix + verify
5. PR created         "PR Ready:" reminder with link, iMessage notification
6. You trigger merge  Complete "Merge:" reminder when ready
7. Agent merges       Rebase, resolve conflicts, test, squash-merge, done
```

### Task Discovery

The agent scans each Reminders list mapped in `config.toml` under `[projects]`. Any uncompleted reminder starting with `Task:` is picked up:

```
Task: Add user authentication
Task: [P1] Fix login crash        ← priority tag (P0-P4, lower = higher)
```

Notes on the reminder become context for PRD generation.

### PRD Review & Feedback

After generating a PRD, the agent creates an **"Approve PRD:"** reminder:

| Action | Effect |
|--------|--------|
| **Complete** the reminder | Approve — implementation begins |
| **Delete** the reminder | Reject — task is cancelled |
| Create a **"FIX:"** reminder | Agent regenerates PRD with your feedback |

FIX reminders look like: `FIX: [id:abc123] Add more detail about error handling`

The `[id:xxx]` tag routes feedback to the correct task when multiple PRDs are pending. If only one PRD is pending in a list, the tag is optional.

### Implementation (v3.0 Combined Session)

v3.0 runs a **single Claude session** that handles the entire workflow:

1. **Analyze** — reads codebase, plans approach
2. **Implement** — makes changes, atomic commits
3. **Write tests** — follows existing test patterns
4. **Run & fix tests** — up to 5 internal iterations
5. **Verify** — checks acceptance criteria from PRD

This replaces the v2.0 approach of 4 separate cold-start sessions. The combined session uses up to **150 turns** and a **60-minute timeout** (configurable).

After the combined session, an **external test verification** runs as a safety net — catching environment-specific issues Claude might miss. If external tests fail, Claude gets up to 5 more fix attempts.

### Merge Flow (New in v3.0)

After PR creation, the agent creates a **"Merge:"** reminder:

| Action | Effect |
|--------|--------|
| **Complete** the reminder | Agent starts autonomous merge |
| **Delete** the reminder | Task completes, PR stays open for manual handling |

The merge process:

1. **Rebase** feature branch onto latest `main`
2. **Resolve conflicts** — if any, Claude reads conflicted files and resolves them
3. **Run tests** — verifies nothing broke during rebase
4. **Fix failures** — up to 3 attempts if post-merge tests fail
5. **Push** the rebased branch (force-with-lease)
6. **Merge PR** via `gh pr merge --squash --delete-branch`

### Concurrent Processing (New in v3.0)

The agent can process multiple projects in parallel:

- `max_concurrent_tasks = 3` — total tasks across all projects
- `max_concurrent_per_project = 1` — one task per project (prevents git conflicts)

Discovery (scanning Reminders) stays sequential; only task processing (implementation, merging) runs in parallel via a thread pool.

### PR Review Integration

When `auto_respond_to_reviews = true` in `[pr_review]`, the agent monitors PRs for review comments and has Claude address them. Once all comments are addressed, the task routes to the merge flow.

If `auto_merge_on_review_complete = true` in `[merge]`, the merge happens automatically without a merge reminder.

### Priority System

Tag tasks with `[P0]` through `[P4]` in the title:

| Priority | Meaning |
|----------|---------|
| P0 | Critical |
| P1 | High |
| P2 | Medium |
| P3 | Low |
| P4 | Lowest |

Higher-priority tasks are processed first. Omitting the tag defaults to P5 (lowest).

### Error Handling & Retries

When a task fails, the agent retries with exponential backoff:

- Retry 1: 5 minutes
- Retry 2: 20 minutes
- Retry 3: 80 minutes

After `retry_max` (default 3) failures, the task moves to ERROR state and you get an iMessage notification. Use `--reset <task name>` to manually retry.

### Notifications

The agent sends iMessage notifications (via `imsg` CLI) for key events:

- PRD ready for review
- Implementation starting
- Tests passed/failed
- PR created
- Merge ready / starting / conflicts / complete / failed
- Errors

Disable emoji with `use_emojis = false` in `[notification]`.

## Configuration Reference

### `config.toml`

```toml
[daemon]
poll_interval_seconds = 300        # How often to check for new tasks
max_concurrent_tasks = 3           # Max parallel tasks across all projects
max_concurrent_per_project = 1     # Max parallel tasks per project
retry_max = 3                      # Max retries before ERROR state
retry_backoff_base_minutes = 5     # Base for exponential backoff
approval_timeout_days = 7          # Auto-reject unapproved PRDs after N days

[claude]
model = "opus"                     # Claude model
timeout_seconds = 1800             # Per-invocation timeout (PRD gen, test fix, etc.)
max_turns = 50                     # Per-invocation turn limit
impl_max_turns = 150               # Combined implement+test session turn limit
impl_timeout_seconds = 3600        # Combined session timeout (60 min)
permission_mode = "bypassPermissions"
stream_output = true               # Stream to file for crash recovery

[reliability]
write_tests_for_implementation = true   # Write tests (always true in v3 combined session)
max_test_fix_iterations = 5             # External test fix attempts
verify_acceptance_criteria = true       # Verify PRD criteria before PR

[merge]
strategy = "squash"                     # "squash", "merge", or "rebase"
max_fix_iterations = 3                  # Post-merge test fix attempts
auto_merge_on_review_complete = false   # Skip merge reminder after review

[projects]
"MyApp" = "https://github.com/user/repo"
```

### Adding a New Project

1. Create a Reminders list with the exact project name
2. Add the mapping in `[projects]`: `"ListName" = "https://github.com/user/repo"`
3. Create tasks in that list prefixed with `Task:`

## CLI Reference

| Command | Description |
|---------|-------------|
| `python3 porter.py` | Run one poll cycle |
| `python3 porter.py --daemon` | Run continuously |
| `python3 porter.py --status` | Show all active tasks |
| `python3 porter.py --health` | Run health checks |
| `python3 porter.py --dry-run` | Simulate without changes |
| `python3 porter.py --usage` | Show Claude API usage stats |
| `python3 porter.py --logs` | Show recent log entries |
| `python3 porter.py --reset <name>` | Reset a task to NEW state |
| `python3 porter.py --web` | Start web dashboard |
| `python3 porter.py --version` | Show version |

## Task States

| State | Description |
|-------|-------------|
| `NEW` | Discovered, waiting to be processed |
| `IN_FLIGHT` | PRD generation in progress |
| `AWAITING_APPROVAL` | PRD created, waiting for user to approve/reject |
| `NEEDS_CLARIFICATION` | Agent has a question, waiting for answer |
| `IMPLEMENTING` | Claude is implementing the feature |
| `WRITING_TESTS` | Claude is writing tests (legacy path) |
| `TESTING` | Running external test verification |
| `FIXING_TESTS` | Claude fixing code to pass tests |
| `VERIFYING` | Checking acceptance criteria (legacy path) |
| `PR_CREATED` | PR created, transitioning to next state |
| `PR_REVIEW_PENDING` | Monitoring for PR review comments |
| `AWAITING_MERGE` | Merge reminder created, waiting for user |
| `MERGING` | Rebase and conflict resolution in progress |
| `MERGE_TESTING` | Running tests after rebase |
| `MERGE_FIXING` | Fixing post-rebase test failures |
| `COMPLETE` | Task finished |
| `ERROR` | Failed after max retries |
| `REJECTED` | PRD was rejected or approval timed out |

## Architecture Notes

### Thread Safety

- **StateDB** — uses `threading.local()` for per-thread SQLite connections
- **GitOperations** — each task has its own repo directory; `max_concurrent_per_project = 1` prevents conflicts
- **ClaudeCodeRunner** — each call spawns a unique subprocess with a unique output file
- **PRDRepoManager** — `save_prd()` is protected by a `threading.Lock` since multiple threads push to the shared PRD repo
- **Notifier** — each notification spawns an independent subprocess
- **RemindersClient** — discovery runs sequentially in the main thread; only processing goes parallel

### Database

SQLite at `~/.claude/agents/porter/state.db`. Schema auto-migrates via `_ensure_columns()` — new columns are added on startup without losing existing data.

### Logs

Stored at `~/.claude/agents/porter/logs/`. Claude session outputs are saved to `logs/claude_outputs/` with task ID and phase in the filename. Old logs are compressed after `compress_after_days`.
