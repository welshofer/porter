# Porter

**Turn an Apple Reminder into a pull request.**

Porter is a macOS daemon that watches your Apple Reminders for coding tasks and carries them out autonomously — from understanding the task, to writing a plan, to implementing the code, to opening a PR on GitHub.

You write `Task: Add dark mode support` in Reminders. You come back to a ready-to-merge pull request.

---

## What It Does

```
You create a Reminder           Agent discovers it, classifies type
       │                                    │
       ▼                                    ▼
"Task: Add dark mode"          Clones repo, generates a PRD (plan)
                                            │
                                            ▼
                               Updates reminder: [REVIEW PRD]
                                            │
You review PRD, complete                    │
the reminder to approve ───────────────────►│
                                            ▼
                               Runs Claude Agent SDK session:
                               reads codebase, implements, tests, commits
                                            │
                                            ▼
                               Pushes branch, creates PR on GitHub
                               Updates reminder: [MERGE PR #42]
                                            │
You review PR, complete                     │
the reminder to merge ────────────────────►│
                                            ▼
                               Rebases, runs tests, merges PR ✓
```

**Everything happens through Apple Reminders and iMessage notifications.** No terminal, no IDE required. Create a task from your phone while walking the dog.

---

## Features

- **Six task types** — quickfix, bugfix, feature, refactor, analysis, composite (auto-classified or manually tagged)
- **PRD quality gate** — AI-generated plans are evaluated for quality before you see them
- **Single-reminder UX** — status updates appear as title prefixes on the original reminder, no clutter
- **Priority system** — `[P0]` through `[P4]` for priority ordering
- **Parallel execution** — up to 5 tasks across different projects simultaneously
- **Per-task workspace isolation** — concurrent tasks never corrupt each other
- **Exponential backoff retry** — 5 → 20 → 80 min, with session resumption on timeout
- **Merge conflict resolution** — agent resolves simple conflicts automatically
- **Circuit breaker** — stops dispatching after consecutive failures to prevent budget burn
- **Cost tracking** — per-task and daily token/cost tracking with optional budget caps
- **iMessage notifications** — key lifecycle events with optional AI-generated images
- **Full audit trail** — every tool call the agent makes is logged as JSONL

---

## Requirements

- **macOS** (uses Apple Reminders via [reminders-cli](https://github.com/keith/reminders-cli))
- **Python 3.9+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** with the Agent SDK (`pip install claude-agent-sdk`)
- **[GitHub CLI](https://cli.github.com/)** (`gh`)
- **[reminders-cli](https://github.com/keith/reminders-cli)**
- **An Anthropic API key** (set as `ANTHROPIC_API_KEY` environment variable)
- Optional: [imsg](https://github.com/nicklama/imsg) for iMessage notifications

---

## Installation

### 1. Install dependencies

```bash
# Homebrew packages
brew install gh
brew install keith/formulae/reminders-cli

# Claude Code CLI (requires Node.js)
npm install -g @anthropic-ai/claude-code

# Python dependencies
pip install toml claude-agent-sdk anyio
```

### 2. Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/porter.git
cd porter
```

### 3. Authenticate

```bash
# GitHub CLI
gh auth login

# Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 4. Run the interactive setup

```bash
./setup.sh
```

This will:
- Detect your CLI tool paths
- Ask for your phone number (optional, for iMessage notifications)
- Create a PRD repository mapping
- Generate `config.toml` with your settings
- Generate a launchd plist for your user

### 5. Create the PRD repository

PRDs (Product Requirements Documents) are stored in a separate GitHub repo for easy review:

```bash
gh repo create prds --private
```

### 6. Add your first project

Edit `~/.claude/agents/porter/config.toml`:

```toml
[projects]
"MyProject" = "https://github.com/username/my-project"
```

Then create a matching Reminders list called "MyProject" in Apple Reminders.

### 7. Start the daemon

**Option A: Run directly** (foreground, good for testing)
```bash
python3 porter.py --daemon
```

**Option B: Install as a launchd service** (background, auto-restart on crash)
```bash
./install.sh install
```

**Option C: Deploy to production path**
```bash
bash deploy/deploy.sh
```

### 8. Verify it's working

```bash
# Check health
python3 porter.py --healthcheck

# Run a dry-run cycle
python3 porter.py --once --dry-run
```

---

## Usage

### Creating Tasks

Add a reminder to any mapped project list with the `Task:` prefix:

```
Task: Add user authentication
Task: [P0] Fix login crash on empty email
Task: [type:quickfix] Fix typo in README
Task: Refactor the database layer into a service
```

**Title syntax:** `Task: [P<0-4>] [type:<type>] <description>`

| Component | Required | Example | Notes |
|-----------|----------|---------|-------|
| `Task:` prefix | Yes | `Task: ...` | Case-sensitive |
| Priority tag | No | `[P0]`, `[P1]` | P0 = critical, P4 = backlog |
| Type tag | No | `[type:quickfix]` | Overrides auto-classification |
| Description | Yes | `Add dark mode` | Becomes the task title |
| Notes field | No | (Reminder notes) | Extra context, links, constraints |

### Task Types

| Type | PRD? | Approval? | Model | What Happens |
|------|------|-----------|-------|-------------|
| `quickfix` | No | No | Sonnet | Fast fix, no ceremony |
| `bugfix` | Yes | Yes | Opus | Failing test first, then fix |
| `feature` | Yes | Yes | Opus | Full PRD + quality gate + implementation |
| `refactor` | Yes | Yes | Opus | Incremental, tests green throughout |
| `analysis` | No | No | Sonnet | Read-only report, no code changes |
| `composite` | No | No | Sonnet | Decomposes into sub-task Reminders |

Auto-classification keywords:
- "fix typo", "rename", "update readme" → `quickfix`
- "fix", "bug", "crash", "broken" → `bugfix`
- "refactor", "extract", "migrate" → `refactor`
- Everything else → `feature`

### Interacting via Reminders

The daemon updates your reminder's title prefix to show progress:

| Prefix | Meaning | Your Action |
|--------|---------|-------------|
| `[QUEUED]` | Waiting to be processed | — |
| `[WORKING]` | Agent is implementing | — |
| `[REVIEW PRD]` | PRD ready (link in notes) | **Complete** to approve, **delete** to reject |
| `[QUESTION]` | Agent needs input (question in notes) | Change prefix to `[ANSWER]`, answer in notes |
| `[PR #N]` | PR created on GitHub | — |
| `[MERGE PR #N]` | Ready for merge | **Complete** to merge |
| `[RETRY N/M]` | Failed, retrying after backoff | — |
| `[FAILED]` | Failed after all retries | Check `--status` |

**To request PRD changes:** Change prefix to `[FIX]` and put feedback in the notes field.

**To cancel a running task:** Complete the reminder while the agent is working.

### CLI Commands

```bash
python3 porter.py --daemon             # Run as daemon
python3 porter.py --once               # One poll cycle
python3 porter.py --status             # Task dashboard
python3 porter.py --list-tasks         # All tasks (incl. completed)
python3 porter.py --healthcheck        # Daemon health
python3 porter.py --reset-task NAME    # Reset failed task for retry
python3 porter.py --cancel-task NAME   # Soft cancel (mark REJECTED)
python3 porter.py --kill-task NAME     # Hard kill (stop agent, clean up)
python3 porter.py --kill-all           # Kill ALL active tasks
python3 porter.py --cleanup            # Remove stale branches/workspaces
python3 porter.py --cleanup-reminders  # Complete orphan reminders
python3 porter.py --process-task ID    # Process single task (debug)
python3 porter.py --dry-run --once     # Preview mode
python3 porter.py --verbose            # Debug logging
```

---

## Configuration

Copy and customize the example config:

```bash
cp config.example.toml config.toml
```

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `poll_interval_seconds` | 300 | How often to check for new tasks |
| `max_concurrent_tasks` | 5 | Max parallel tasks across all projects |
| `max_concurrent_per_project` | 1 | Max parallel tasks per project |
| `retry_max` | 3 | Max retries before ERROR |
| `approval_timeout_days` | 7 | Auto-reject unapproved PRDs |
| `model` | opus | Claude model for implementation |
| `impl_timeout_seconds` | 3600 | Agent session timeout (60 min) |
| `run_tests_before_pr` | true | Run tests before PR creation |
| `merge_strategy` | squash | squash, merge, or rebase |
| `daily_budget_usd` | — | Optional daily spending cap |
| `max_cost_per_task_usd` | — | Optional per-task spending cap |

See [config.example.toml](config.example.toml) for all options with inline documentation.

**Environment variables:**
- `ANTHROPIC_API_KEY` — required for Claude API access
- `PORTER_PHONE` — phone number for iMessage notifications (optional)

---

## How It Works

### The State Machine

Every task moves through a 14-state lifecycle:

```
NEW → ROUTING → IN_FLIGHT → PRD_EVALUATING → AWAITING_APPROVAL
                                                      │
                                                  (you approve)
                                                      │
                                               AGENT_RUNNING
                                                      │
                                    PR_CREATED → PR_REVIEW_PENDING
                                                      │
                                               AWAITING_MERGE
                                                      │
                                                  (you approve)
                                                      │
                                                   MERGING → COMPLETE
```

Side states: `NEEDS_CLARIFICATION` (agent asked a question), `ERROR` (failed after retries), `REJECTED` (you declined).

### Error Recovery

- **Exponential backoff**: Failures schedule a retry after 5/20/80 minutes. The next poll cycle picks it up — no recursive retry burning.
- **Session resumption**: Timed-out agent sessions resume from where they left off.
- **Merge retry**: If merge fails (network, CI timing), the system retries without re-running the entire implementation.
- **PR creation retry**: If implementation succeeded but PR creation failed, retry skips straight to PR creation.
- **Circuit breaker**: 3 consecutive failures stop dispatching for a cooldown period.
- **Reminders CLI resilience**: CLI failures skip the check gracefully — never falsely reject a task.

### Workspace Isolation

Each task gets its own workspace directory (`repo-name-taskid[:8]`). Concurrent tasks targeting the same repo can't corrupt each other's uncommitted changes. Workspaces are cleaned up after task completion via `--cleanup`.

### Security

- The agent works on isolated feature branches — never pushes to main directly
- You approve the plan before implementation starts (for non-trivial tasks)
- You approve the merge before code enters your main branch
- Tests run automatically before PR creation
- Optional budget caps prevent runaway spending
- Every tool call is logged in an audit trail (`logs/audit.jsonl`)
- Agent tools are scoped per task type (analysis tasks are read-only)

---

## Architecture

```
porter/
├── porter.py              # Entry point, daemon loop, CLI
├── config.example.toml           # Configuration template
├── lib/
│   ├── orchestrator.py           # State machine, phase coordination
│   ├── agent_runner.py           # Claude Agent SDK wrapper
│   ├── models.py                 # Config, Task, enums, state transitions
│   ├── db.py                     # SQLite persistence (WAL mode)
│   ├── git_ops.py                # Git/GitHub operations (all with timeouts)
│   ├── prd_evaluator.py          # PRD quality gate (Claude-as-judge)
│   ├── reminders.py              # Apple Reminders client + per-cycle cache
│   ├── notifier.py               # iMessage notifications
│   ├── router.py                 # Task type classification
│   ├── test_runner.py            # Test framework auto-detection
│   ├── circuit_breaker.py        # Failure circuit breaker
│   ├── healthcheck.py            # Startup health checks
│   ├── logging_config.py         # JSON structured logging
│   └── audit_hook.py             # Agent SDK audit trail
├── tests/                        # 191 tests
├── setup.sh                      # Interactive setup wizard
├── install.sh                    # launchd service management
└── deploy/
    └── deploy.sh                 # Production deployment script
```

### Data Storage

| Location | Purpose |
|----------|---------|
| `~/.claude/agents/porter/state.db` | SQLite database (task state, history, usage) |
| `~/.claude/agents/porter/logs/` | Structured JSON logs, audit trail, heartbeat |
| `~/.claude/agents/porter/workspaces/` | Cloned repos (one per active task) |

### Supported Test Frameworks

The test runner auto-detects and runs:

| Detection | Command |
|-----------|---------|
| `package.json` with `test` script | `npm test` |
| `pytest.ini` or `test_*.py` | `pytest -v` |
| `Package.swift` | `swift test` |
| `*.xcodeproj` | `xcodebuild test` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |

Or set a custom test command per project in config.

---

## Troubleshooting

### Daemon isn't picking up tasks

```bash
# Check if it's running
python3 porter.py --healthcheck

# Check logs
tail -50 ~/.claude/agents/porter/logs/porter.log

# Run one cycle manually with verbose logging
python3 porter.py --once --verbose
```

### Task stuck in ERROR

```bash
# See the error details
python3 porter.py --status

# Reset for retry
python3 porter.py --reset-task "task name"
```

### Task stuck in AGENT_RUNNING

The zombie reaper checks every poll cycle and kills tasks that have been running past the timeout. You can also kill manually:

```bash
python3 porter.py --kill-task "task name"
```

### Circuit breaker opened

Logs will show `Circuit breaker OPEN for claude`. This means 3+ consecutive failures. Fix the underlying issue (API key expired? network down?) and the breaker auto-resets after `2 × poll_interval`.

### Orphan reminders

```bash
# Clean up reminders from finished tasks (only touches managed project lists)
python3 porter.py --cleanup-reminders
```

### Disk space

```bash
# Remove workspaces and branches from completed tasks
python3 porter.py --cleanup
```

---

## Cost Estimates

Using Claude Opus pricing ($15/M input, $75/M output):

| Task Type | Typical Cost |
|-----------|-------------|
| Quickfix (Sonnet) | $0.20 – $1.00 |
| Bugfix (Opus) | $1.00 – $5.00 |
| Feature (Opus) | $3.00 – $15.00 |
| PRD generation (Sonnet) | $0.10 – $0.50 |

Set `daily_budget_usd` and `max_cost_per_task_usd` in config to control costs.

---

## Further Reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — Deep dive into the system architecture and design decisions
- [HOW_IT_WORKS.md](HOW_IT_WORKS.md) — Step-by-step walkthrough with diagrams
- [COMMANDS.md](COMMANDS.md) — CLI reference and task lifecycle details
- [OPERATIONS.md](OPERATIONS.md) — Runbook for common scenarios and recovery procedures
- [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) — All configuration options

---

## License

MIT
