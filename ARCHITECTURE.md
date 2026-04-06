# Porter — Architecture

> A macOS daemon that turns Apple Reminders into pull requests, powered by the Claude Agent SDK.

---

## What It Does

Porter monitors Apple Reminders lists for coding tasks. When it finds one, it:

1. **Classifies** the task (quickfix? bugfix? feature? refactor?)
2. **Generates a PRD** (Product Requirements Document) and sends it for human approval
3. **Runs a Claude Agent SDK session** that reads the codebase, implements the feature, writes tests, and commits
4. **Creates a pull request** and notifies you via iMessage
5. **Responds to PR review comments** automatically
6. **Merges** when you say go

The entire pipeline is autonomous. You write a Reminder like `Task: Add dark mode support` and come back to a ready-to-merge PR.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Apple Reminders                          │
│   "Task: Add dark mode support"                                 │
│   "Task: [P0] Fix login crash"                                  │
│   "Task: [type:quickfix] Fix typo in README"                    │
└────────────────────────┬────────────────────────────────────────┘
                         │ poll every 5 min
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      porter.py                            │
│   Daemon loop · Task discovery · Concurrency control            │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   OrchestratorV4 (orchestrator.py)               │
│   State machine · Phase coordination · Error recovery           │
│                                                                  │
│   ┌──────────┐  ┌────────────┐  ┌──────────────┐               │
│   │  Router  │  │ PRD Eval   │  │ Agent Runner  │               │
│   │ classify │→ │ quality    │→ │ Claude SDK    │               │
│   │ task type│  │ gate       │  │ session       │               │
│   └──────────┘  └────────────┘  └──────────────┘               │
│                                                                  │
│   ┌──────────┐  ┌────────────┐  ┌──────────────┐               │
│   │ Git Ops  │  │ Test Runner│  │  Notifier     │               │
│   │ clone/PR │  │ auto-detect│  │  iMessage     │               │
│   └──────────┘  └────────────┘  └──────────────┘               │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
                   Pull Request on GitHub
```

---

## Reminders Command Reference

Every interaction with the system happens through Apple Reminders. The daemon watches specific lists (mapped to GitHub repos in config) for items prefixed with `Task:`.

### Creating Tasks

| Reminder Title | What Happens |
|---|---|
| `Task: Add dark mode support` | Feature — generates PRD, requires approval, full implementation |
| `Task: [P0] Fix login crash` | Priority P0 feature (processed first) |
| `Task: [type:quickfix] Fix typo in README` | Quickfix — no PRD, no approval, fast Sonnet implementation |
| `Task: [type:bugfix] Users can't log in on Safari` | Bugfix — generates PRD, reproduces with test, fixes |
| `Task: [type:refactor] Extract auth middleware` | Refactor — generates PRD, incremental with tests green throughout |

**Title syntax:** `Task: [P<0-4>] [type:<type>] <description>`

- **Prefix** `Task:` — required, case-sensitive
- **Priority** `[P0]` through `[P4]` — optional, P0 is highest (default: P5/unset)
- **Type tag** `[type:quickfix|bugfix|feature|refactor]` — optional, overrides auto-classification
- **Description** — everything after the prefix/tags becomes the task title

**Notes field:** Add details in the Reminder's notes. The agent receives these as context alongside the task title. Useful for:
- Links to relevant files or docs
- Specific requirements or constraints
- Error messages or reproduction steps (for bugs)
- Design preferences

### Task Type Auto-Classification

If you don't specify `[type:...]`, the system classifies based on keywords:

| Keywords in Title | Classified As |
|---|---|
| "fix typo", "fix import", "fix whitespace", "fix lint", "update readme", "rename", "format" | `quickfix` |
| "refactor", "restructure", "extract", "migrate" | `refactor` |
| "fix", "bug", "crash", "broken", "doesn't work" | `bugfix` |
| Everything else | `feature` |

### What Each Type Gets

| Capability | Quickfix | Bugfix | Feature | Refactor |
|---|---|---|---|---|
| PRD generated | No | Yes | Yes | Yes |
| PRD quality evaluation | No | No | Yes | Yes |
| Human approval required | No | Yes | Yes | Yes |
| Model | Sonnet | Opus | Opus | Opus |
| Max conversation turns | 30 | 100 | 150 | 150 |
| Timeout | 60 min | 60 min | 60 min | 60 min |

### Approval Flow (Single-Reminder UX)

Instead of creating separate reminders, the system updates the **original task reminder's title prefix** to show the current status. This keeps your Reminders list clean.

When a PRD needs approval, the reminder title changes to:

```
[REVIEW PRD] Task: Add dark mode support
```

The PRD link appears in the reminder's notes field.

**Your options:**
- **Complete the reminder** → approved, implementation begins
- **Delete the reminder** → rejected, task cancelled
- **Change prefix to `[FIX]`** and add feedback in notes → triggers PRD regeneration

### Merge Flow

After the PR is created, the reminder title changes to:

```
[MERGE PR #42] Task: Add dark mode support
```

**Your options:**
- **Complete the reminder** → squash-merge (or configured strategy), post-merge tests, cleanup
- **Delete the reminder** → merge skipped, PR stays open

### Answering Agent Questions

If the agent needs human input, the reminder title changes to:

```
[QUESTION] Task: Add dark mode support
```

The question appears in the notes field.

**To answer:** Change the prefix to `[ANSWER]` and put your answer in the notes field. The agent resumes with your answer.

### Status Prefixes at a Glance

| Prefix | Meaning | Action needed? |
|--------|---------|---------------|
| `[QUEUED]` | Waiting to be processed | No |
| `[WORKING]` | Agent is implementing | No |
| `[REVIEW PRD]` | PRD ready for review | **Yes** — complete to approve |
| `[QUESTION]` | Agent needs your input | **Yes** — change to `[ANSWER]` |
| `[PR #N]` | PR created | No |
| `[MERGE PR #N]` | Ready for merge | **Yes** — complete to merge |
| `[RETRY N/M]` | Failed, retrying after backoff | No |
| `[FAILED]` | Failed after all retries | Check `--status` |

---

## What It Can Do

### Implementation Capabilities

The agent has access to: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, `WebFetch`. In practice, this means it can:

- Read and understand any codebase (explores files, greps for patterns, reads docs)
- Write new files and edit existing ones
- Run any shell command (build, test, lint, install dependencies, curl APIs)
- Search the web for documentation, API references, or solutions
- Create atomic commits with meaningful messages
- Run the project's test suite and fix failures iteratively

### Supported Project Types

The test runner auto-detects frameworks:

| Detection | Test Command |
|---|---|
| `package.json` with `test` script | `npm test` |
| `vitest.config.ts` | `npx vitest run` |
| `jest.config.js` | `npx jest` |
| `pytest.ini` or `test_*.py` files | `pytest -v` |
| `Package.swift` | `swift test` |
| `*.xcodeproj` | `xcodebuild test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

### Multi-Project Support

The daemon manages 10 projects simultaneously (configured in `[projects]`). Each Reminders list maps to a GitHub repo:

```toml
[projects]
"Arcus" = "https://github.com/welshofer/Splat"
"MangaMaker" = "https://github.com/welshofer/MangaMaker"
"Juno" = "https://github.com/welshofer/Juno"
```

Create a Reminder in the "Arcus" list → the agent works on the Splat repo. Create one in "Juno" → it works on Juno. Tasks across different projects run in parallel.

### Error Recovery

- **Exponential backoff retry:** 5 min → 20 min → 80 min, up to 3 attempts. Retries are scheduled via `next_retry_at` and picked up by the next poll cycle — not retried recursively (which would burn all attempts instantly)
- **Merge retry:** Merge failures (network blips, CI timing) are retried through the same backoff system. On retry, the system skips straight to merge — it doesn't re-run the entire route/clone/PRD/agent pipeline
- **Session resumption:** If an agent session times out, it resumes where it left off (not from scratch)
- **Reminders CLI resilience:** If the Reminders CLI times out or crashes, the system skips that check and retries next cycle — it never falsely rejects a task due to a CLI hiccup
- **Git operation timeouts:** All git/gh subprocess calls have a 120-second timeout to prevent hung credential prompts or network stalls from blocking the daemon forever
- **Per-task workspace isolation:** Each task gets its own directory (`repo_name-task_id[:8]`), preventing concurrent tasks from corrupting each other's uncommitted changes
- **Circuit breaker:** After 3 consecutive failures across any tasks, stops dispatching new work for a cooldown period to avoid burning budget on a systemic issue (e.g., API outage)
- **Crash recovery:** The daemon auto-restarts via launchd `KeepAlive` with a 30-second throttle

### Observability

- **Structured JSON logs** with automatic `task_id` correlation via contextvars — every log line traces back to its task
- **Agent session audit trail** — every tool call Claude makes is recorded as a JSONL line (tool name, file path, command, timestamp)
- **iMessage notifications** at key lifecycle events (task received, PRD ready, PR created, errors, merge complete)
- **State history** — every state transition is recorded in SQLite with timestamps
- **Token/cost tracking** — input, output, and cache tokens tracked per session with cost estimation
- **Healthcheck** — `--health` reports daemon status based on heartbeat file freshness

---

## What It Can't Do

### Hard Limitations

- **No interactive debugging.** The agent can't attach a debugger, use breakpoints, or inspect running processes. It debugs by reading code, adding print statements, and running tests.
- **No GUI interaction.** It can't click buttons, fill forms, or visually verify UI changes. It works purely through code and terminal commands.
- **No iOS Simulator / Android Emulator.** It can build iOS apps with `xcodebuild`, but can't launch a simulator, take screenshots, or test on-device behavior.
- **No deployment.** It creates PRs — it doesn't deploy to production, staging, or any environment. Deployment is your responsibility after merge.
- **No cross-repo changes.** Each task targets one repo. If a change requires coordinated modifications across multiple repos, you need separate tasks.
- **No secrets management.** It can't access API keys, tokens, or credentials that aren't already in the repo's environment. It won't add secrets to CI/CD.
- **No code review of others' work.** It creates PRs for its own work. It doesn't review, approve, or comment on other PRs.

### Practical Limitations

- **Long-running tasks may time out.** The agent session has a 60-minute timeout. Very large features that require extensive exploration and implementation may hit this limit. The system will retry with session resumption, but complex features may need to be broken into smaller tasks.
- **Shallow history.** Default `--depth 1` clone means the agent can't `git log`, `git blame`, or explore commit history. This is fine for most implementation work but limits debugging that requires understanding *why* code was written a certain way.
- **One task per project at a time.** To prevent git conflicts, only one task per repo runs at once. If you queue 5 tasks for the same project, they run sequentially.
- **No real-time feedback.** You can't watch the agent work or intervene mid-session. You get iMessage updates at milestones, but can't steer the implementation. If the agent goes down a wrong path, it discovers this itself (via test failures) or you catch it at PR review.
- **PRD quality varies.** The auto-generated PRD is a starting point. For complex features, you'll likely want to edit the PRD before approving, or reject it with feedback for regeneration.
- **No monorepo support.** Each project maps to one repo root. Monorepo setups with multiple apps/packages in subdirectories aren't explicitly supported.

---

## Capacity and Performance

### How Long Can It Run?

The daemon is designed to run indefinitely as a launchd service:

| Aspect | Limit |
|---|---|
| Daemon uptime | Indefinite — `KeepAlive` restarts on crash, `ThrottleInterval` prevents rapid restart loops |
| Per-task Agent SDK session | 60 minutes (configurable via `impl_timeout_seconds`) |
| Per-task PRD generation | 30 minutes (configurable via `timeout_seconds`) |
| Per-task test execution | 10 minutes (configurable via `test_timeout_seconds`) |
| Global executor timeout | 90 minutes per poll cycle (all concurrent tasks combined) |
| Retry window | Up to ~2 hours per task (5 + 20 + 80 min backoff across 3 retries) |
| Approval timeout | 7 days before auto-reject (configurable via `approval_timeout_days`) |

**Typical task durations:**

| Task Type | Typical Duration | Range |
|---|---|---|
| Quickfix | 2-5 minutes | 1-15 min |
| Bugfix | 10-30 minutes | 5-60 min |
| Feature (small) | 15-45 minutes | 10-60 min |
| Feature (large) | 30-60 minutes | 20-60+ min (may need retry) |
| Refactor | 15-45 minutes | 10-60 min |

### How Hard Can It Work?

| Resource | Configuration | Default |
|---|---|---|
| Max concurrent tasks (global) | `max_concurrent_tasks` | 5 |
| Max concurrent per project | `max_concurrent_per_project` | 1 |
| Parallel project processing | `enable_parallel_projects` | true |
| Poll interval | `poll_interval_seconds` | 300 seconds (5 min) |
| Agent turns per task | 30-150 depending on type | — |
| Budget cap per task | `max_cost_per_task_usd` | None (unlimited) |

**Throughput at max capacity (5 concurrent, all features):**

- ~5 tasks per hour at steady state (assuming 60-min sessions with overlap)
- ~40 tasks per 8-hour workday
- ~120 tasks per 24 hours

In practice, throughput depends on task complexity and the 1-per-project constraint. With 10 projects mapped, you're more likely bottlenecked by having enough tasks queued than by the system's capacity.

**Cost at scale (Opus pricing: $5/M input, $25/M output):**

| Scenario | Estimated Cost |
|---|---|
| Quickfix (Sonnet, 30 turns) | $0.20 - $1.00 |
| Bugfix (Opus, 100 turns) | $1.00 - $5.00 |
| Feature (Opus, 150 turns) | $3.00 - $15.00 |
| PRD generation (Sonnet) | $0.10 - $0.50 |
| PRD evaluation (Sonnet) | $0.05 - $0.20 |
| Full feature pipeline (PRD + eval + impl) | $3.00 - $16.00 |

---

## The State Machine

Every task moves through a well-defined state machine. The path depends on the task type — a quickfix skips PRD generation and approval, while a feature goes through every phase.

```
NEW ──→ ROUTING ──→ IN_FLIGHT ──→ PRD_EVALUATING ──→ AWAITING_APPROVAL
                                        │                    │
                                   (quickfix skips)    ┌─────┴──────┐
                                        │              │            │
                                        ▼          approved    rejected
                                  AGENT_RUNNING        │            │
                                        │              ▼            ▼
                                        │         AGENT_RUNNING  REJECTED
                                        │              │
                                        ▼              ▼
                                   PR_CREATED ──→ PR_REVIEW_PENDING
                                        │              │
                                        ▼              ▼
                                  AWAITING_MERGE ──→ MERGING ──→ COMPLETE
```

**Escape hatches at any point:**
- `NEEDS_CLARIFICATION` — the agent encountered something ambiguous and asked a question
- `ERROR` — something failed after all retries exhausted

### State Details

| State | What's happening | Human action needed? |
|---|---|---|
| `NEW` | Just discovered from Reminders | No |
| `ROUTING` | Classifying as quickfix/bugfix/feature/refactor | No |
| `IN_FLIGHT` | Cloning repo + generating PRD (in parallel) | No |
| `PRD_EVALUATING` | Claude-as-judge checking PRD quality | No |
| `AWAITING_APPROVAL` | PRD posted, waiting for human review | **Yes** — complete the approval Reminder |
| `AGENT_RUNNING` | Claude Agent SDK implementing the feature | No |
| `PR_CREATED` | Branch pushed, PR created on GitHub | No |
| `PR_REVIEW_PENDING` | Addressing PR review comments | No |
| `AWAITING_MERGE` | Waiting for merge signal | **Yes** — complete the merge Reminder |
| `MERGING` | Rebasing, running tests, merging PR | No |
| `COMPLETE` | Done | No |
| `NEEDS_CLARIFICATION` | Agent asked a question | **Yes** — answer in the question Reminder |
| `ERROR` | Failed after retries | Check `--status` for details |

---

## PRD Evaluation — The Quality Gate

For features and refactors, the system evaluates PRD quality before asking for human approval. This catches vague or untestable PRDs before they waste an expensive implementation session.

**How it works:**

1. Claude generates the PRD
2. A separate Claude call evaluates it against 5 criteria:
   - Testable acceptance criteria
   - Reasonable scope
   - Clear technical approach
   - Edge cases covered
   - No ambiguity
3. Score < 6 or blocking issues → regenerate (up to 2 attempts)
4. Score >= 6, no blockers → pass through to approval

**Self-calibrating evaluator:** The system records when the evaluator and human disagree (eval passed but human rejected, or vice versa). On future evaluations, it includes these disagreements as calibration notes:

```
Pattern: You have been TOO STRICT in 3 of 5 cases.
The human approved PRDs that you rejected. Lower your bar
for the issues you flagged in those cases.
```

This feedback loop means the evaluator gets better over time without any manual tuning.

---

## Agent Runner — The Implementation Engine

The Agent Runner wraps the Claude Agent SDK to run a single continuous implementation session. This replaced v3's 6-state pipeline (PRD -> implement -> write tests -> fix tests -> verify -> review) with one session that maintains full context.

### What the agent receives

A task-type-specific prompt with:

- The task title and notes from the Reminder
- The full PRD (for features/bugfix/refactor)
- The branch name to work on
- Instructions tailored to the task type:
  - **Feature:** "Read the PRD, explore the codebase, implement incrementally, write tests, verify acceptance criteria"
  - **Bugfix:** "Reproduce the bug with a failing test, find root cause, fix it, verify the test passes"
  - **Quickfix:** "Make the change, keep it minimal, write a test if appropriate"
  - **Refactor:** "Run tests first, make changes incrementally, tests must pass after every commit"
- On retry: the last 4,000 characters of the previous attempt's output

### What the agent can do

Tools: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, `WebFetch`. It works in the cloned repo directory with `bypassPermissions` mode — it can read, write, and run anything in the workspace.

### What the system tracks during execution

- **Streams progress** by inspecting tool use blocks (e.g., "Read: /src/main.py", "Bash: pytest -v")
- **Tracks token usage** per message for cost estimation
- **Enforces budget limits** — raises `BudgetExceededError` if estimated cost exceeds `max_cost_per_task_usd`
- **Audit trail** — every tool call is logged as a JSON line with task_id, tool name, file path, and timestamp
- **Updates heartbeat** during workspace setup, PRD generation, push/PR creation, merge operations, and agent progress — so the healthcheck doesn't report STALE during any long-running operation

### Session resumption

If an agent session crashes or times out, the system stores the `session_id`. On retry, it resumes the existing session instead of starting fresh — the agent picks up with full context of what it already did.

---

## Error Handling & Retry Strategy

Errors are categorized into 18 types. The retry strategy adapts based on category:

| Error Type | Retryable? | Strategy |
|---|---|---|
| `IMPLEMENTATION_FAILED` | Yes | Resume previous session if possible, else fresh start with retry context |
| `TIMEOUT` | Yes | Resume previous session (it likely just ran out of time) |
| `CLONE_FAILED` | Yes | Network might be back, retry normally |
| `PR_CREATION_FAILED` | Yes | Skip re-implementation, just retry PR creation |
| `BUDGET_EXCEEDED` | **No** | Terminal — task already blew the budget, retrying would too |
| `PUSH_FAILED` | Yes | Retry with force-with-lease |
| `MERGE_FAILED` | Yes | Skip to merge (PR already exists), re-rebase and retry |
| `TEST_FAILED` | Yes | Re-run agent with test output for context |
| `MERGE_CONFLICT_FAILED` | Yes | Skip to merge, re-rebase against latest main |
| `MERGE_TEST_FAILED` | Yes | Skip to merge, post-rebase tests may pass on retry |

**Backoff schedule:** 5 min → 20 min → 80 min (exponential: `base * 4^(attempt-1)`, base = 5 minutes). Maximum 3 retries by default. Retries are scheduled via `next_retry_at` and the task transitions back to `NEW` — the next poll cycle picks it up after the backoff elapses. This prevents recursive retry loops from burning all attempts instantly.

**Merge retry fast path:** When a task with an existing PR retries, it skips the entire route/clone/PRD/agent pipeline and goes straight to `process_merge`. The implementation work is already done — there's no reason to redo it.

**Circuit breaker:** After 3 consecutive failures across any tasks, the system stops dispatching new work for `2 * poll_interval` seconds. This prevents burning budget on a systemic issue (API outage, network failure). A single success resets the breaker.

---

## Notification System

The notifier sends iMessage updates via the `imsg` CLI at key lifecycle events. It's rate-limited to prevent spam — intermediate events are throttled to once per 5 minutes per task, while critical events (PR created, errors, questions) always send.

**Notification flow for a typical feature:**

```
📋 Task received: 'Add dark mode support'
⚙️  Generating PRD for 'Add dark mode support'...
📝 PRD complete for 'Add dark mode support': https://github.com/.../dark-mode-prd.md
🚀 Beginning work on 'Add dark mode support'
✅ Work completed for 'Add dark mode support': https://github.com/.../pull/42
🔀 PR ready for merge: 'Add dark mode support'. Complete the Merge reminder to proceed.
✅ Merged and complete: 'Add dark mode support'
```

---

## Data Storage

### SQLite Database (`state.db`)

| Table | Purpose |
|---|---|
| `tasks` | Full task state — 50+ columns tracking the entire lifecycle |
| `task_history` | Audit log of every state transition with timestamps |
| `claude_usage` | Token usage records per phase (PRD generation, implementation, review) |
| `eval_feedback` | Evaluator/human disagreement records for calibration |

**Auto-migration:** On startup, the DB checks for missing columns and adds them without data loss. No manual migration scripts needed.

**Thread safety:** Per-thread connections via `threading.local()` — no lock contention.

### PRD Repository

PRDs are committed to a dedicated Git repo (separate from the project repos). Each PRD gets a GitHub URL that's included in the approval Reminder and the PR description. This creates a persistent record of what was planned vs. what was built.

### Audit Log

Every tool call Claude makes during agent sessions is recorded in `logs/audit.jsonl`:

```json
{"timestamp": "2026-03-14T10:23:45Z", "task_id": "abc-123", "tool": "Edit", "file_path": "/src/auth.py"}
{"timestamp": "2026-03-14T10:23:47Z", "task_id": "abc-123", "tool": "Bash", "detail": "pytest -v"}
```

---

## Git Operations

The system handles the full git lifecycle. All subprocess calls have a 120-second timeout to prevent hung operations from blocking the daemon.

1. **Shallow clone** (`--depth 1` by default) into per-task workspace (`repo-taskid[:8]`)
2. **Branch creation** — `porter/<feature-slug>` from `origin/main`
3. **Push** with automatic force-with-lease fallback on rejection
4. **PR creation** via `gh` CLI (GraphQL with REST API fallback)
5. **Rebase** before merge, with automatic unshallow if needed
6. **Conflict resolution** — dispatches agent to resolve merge conflicts (up to 3 rounds)
7. **Merge** with configurable strategy (squash/merge/rebase, default: squash)
8. **Post-merge tests** — runs tests after rebase, before merge

---

## Deployment

### launchd Service

The daemon runs as a launchd user agent with `KeepAlive`:

```xml
<key>KeepAlive</key>  <true/>
<key>ThrottleInterval</key>  <integer>30</integer>
```

This means:
- **Always running** — launchd restarts it if it crashes
- **30-second minimum** between restarts — prevents rapid restart loops
- **Starts at login** — loaded from `~/Library/LaunchAgents/`

### Production Paths

```
~/.claude/agents/porter/
├── porter.py              # Main daemon
├── lib/                          # All modules
├── config.toml                   # Configuration
├── state.db                      # SQLite database
├── logs/
│   ├── porter.log         # Structured JSON logs (10MB rotation, 5 backups)
│   ├── audit.jsonl               # Agent tool call audit trail
│   ├── daemon-stdout.log         # launchd stdout capture
│   ├── daemon-stderr.log         # launchd stderr capture
│   └── heartbeat                 # Last-active timestamp
└── workspaces/
    ├── prds/                     # PRD repository clone
    ├── Splat-a1b2c3d4/           # Per-task workspace (repo-taskid[:8])
    ├── MangaMaker-e5f6g7h8/      # Each active task gets its own clone
    └── ...                       # Cleaned up after completion via --cleanup
```

### Deploy Script

```bash
deploy/deploy.sh
```

7-step process: pre-flight checks -> run tests -> stop old daemon -> backup -> copy files -> install plist -> verify health.

---

## CLI Commands

```bash
# Run as daemon (normal operation)
python3 porter.py --daemon

# Run one poll cycle
python3 porter.py --once

# Check daemon health
python3 porter.py --health

# View active tasks
python3 porter.py --status

# List all tasks (including completed)
python3 porter.py --list-tasks

# Process a specific task (debugging)
python3 porter.py --process-task <ID>

# Reset a failed task for retry
python3 porter.py --reset-task "task name"

# Soft cancel (mark REJECTED, doesn't stop running agent)
python3 porter.py --cancel-task "task name"

# Hard kill (stop agent, clean DB, complete reminders)
python3 porter.py --kill-task "task name"

# Hard kill ALL active tasks
python3 porter.py --kill-all

# Clean up stale branches and workspaces
python3 porter.py --cleanup

# Complete orphan reminders from finished tasks
python3 porter.py --cleanup-reminders

# Dry run (discover tasks but don't execute)
python3 porter.py --once --dry-run
```

---

## Configuration Reference

Everything is configured via `~/.claude/agents/porter/config.toml`:

```toml
[daemon]
poll_interval_seconds = 300        # How often to check for new tasks (minimum: 60)
max_concurrent_tasks = 5           # Max tasks running in parallel across all projects
max_concurrent_per_project = 1     # Max tasks per project (prevents git conflicts)
enable_parallel_projects = true    # Process multiple projects concurrently
retry_max = 3                      # Max retries before ERROR state
retry_backoff_base_minutes = 5     # Exponential backoff base (5, 20, 80 min)
approval_timeout_days = 7          # Auto-reject if not approved within this time

[paths]
workspaces = "~/.claude/agents/porter/workspaces"
logs = "~/.claude/agents/porter/logs"
db = "~/.claude/agents/porter/state.db"

[logging]
level = "INFO"                     # DEBUG, INFO, WARNING, ERROR
compress_after_days = 7            # Compress logs older than this

[prd_repo]
url = "https://github.com/user/prds"
local_path = "~/.claude/agents/porter/workspaces/prds"

[tools]
reminders_bin = "/opt/homebrew/bin/reminders"
claude_bin = "/opt/homebrew/bin/claude"
gh_bin = "/opt/homebrew/bin/gh"
imsg_bin = ""  # Optional: path to imsg binary for iMessage notifications

[notification]
phone_number = ""                  # Fallback; prefer PORTER_PHONE env var
notify_on_prd_ready = true
notify_on_pr_created = true
notify_on_error = true
use_emojis = true

[claude]
model = "opus"                     # "opus" for quality, "sonnet" for speed
timeout_seconds = 1800             # 30 min for PRD generation
permission_mode = "bypassPermissions"
max_turns = 50                     # Default turns (overridden per task type)
impl_max_turns = 150               # Agent session turns
impl_timeout_seconds = 3600        # 60 min agent timeout
# max_cost_per_task_usd = 15.0    # Optional budget cap

[testing]
run_tests_before_pr = true
test_timeout_seconds = 600         # 10 min test timeout

[reliability]
write_tests_for_implementation = true
max_test_fix_iterations = 5
verify_acceptance_criteria = true

[merge]
strategy = "squash"                # "squash", "merge", or "rebase"
max_fix_iterations = 3             # Post-merge test fix attempts

[git]
clone_depth = 1                    # Shallow clone (null = full)

[projects]
"MyApp" = "https://github.com/user/my-app"
"MyAPI" = "https://github.com/user/my-api"
```

**Environment variables:**
- `PORTER_PHONE` — phone number for iMessage notifications (overrides config)

---

## File Map

```
porter/
├── porter.py              # Entry point, daemon loop, CLI
├── config.toml                   # Configuration
├── lib/
│   ├── orchestrator.py           # State machine, phase coordination
│   ├── agent_runner.py           # Claude Agent SDK wrapper
│   ├── models.py                 # Config, Task, enums
│   ├── db.py                     # SQLite persistence
│   ├── git_ops.py                # Git/GitHub operations
│   ├── prd_evaluator.py          # PRD quality gate
│   ├── github_issues.py          # GitHub Issues integration
│   ├── reminders.py              # Apple Reminders client
│   ├── notifier.py               # iMessage notifications
│   ├── test_runner.py            # Test framework detection
│   ├── router.py                 # Task classification
│   ├── prd_repo.py               # PRD repository management
│   ├── circuit_breaker.py        # Failure circuit breaker
│   ├── logging_config.py         # JSON formatter + contextvars
│   └── audit_hook.py             # Agent SDK audit trail
├── tests/                        # 191 tests
├── deploy/
│   ├── deploy.sh                 # Deployment script
│   └── com.welshofer.porter-v4.plist  # launchd config
└── docs/
    └── plans/                    # Implementation plans
```

---

## Design Decisions

### Why a single Agent SDK session instead of a multi-step pipeline?

v3 used a 6-step pipeline where each step was a separate Claude call with a narrow prompt. The problem: context was lost between steps. The test-writing step didn't know what tricky implementation choices were made. The verification step didn't know why certain tests were written a certain way.

v4 uses one continuous session with all context preserved. The agent reads the PRD, explores the code, implements, writes tests, fixes failures, and verifies — all in one conversation. It can backtrack, reconsider, and course-correct without losing context.

### Why Apple Reminders as the task queue?

It's always available on macOS/iOS, supports notes (for task details), completion status (for approvals), and is easily scriptable. No external service, no server, no accounts. The `reminders-cli` tool provides JSON-formatted access. You can create a task from your phone while walking the dog.

### Why categorize errors?

Not all failures are equal. A budget-exceeded error will never succeed on retry — there's no point trying again. A timeout might succeed if the session is resumed (it just ran out of time). A clone failure is probably transient (network). Categorizing errors lets the retry system make smarter decisions about whether and how to retry.

### Why shallow clone by default?

Many repos are large. A shallow clone (`--depth 1`) is much faster and uses less disk. The agent rarely needs deep history — it's reading current code and making changes. If a rebase is needed (at merge time), the system automatically unshallows first.

### Why rate-limit notifications?

Without rate limiting, a task that hits 3 retries with intermediate progress events would send 30+ messages. The 5-minute cooldown per (task_id, event_key) keeps notifications informative without being overwhelming.
