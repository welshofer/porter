# Porter — Commands & Flow Reference

## CLI Commands

```
porter.py [OPTIONS]
```

| Flag               | Description                                      |
|--------------------|--------------------------------------------------|
| `--status`         | Show status dashboard of all active tasks        |
| `--list-tasks`     | List all tasks, including completed              |
| `--reset-task NAME`| Reset a task to NEW state (triggers re-processing)|
| `--cancel-task NAME`| Soft cancel — marks REJECTED, doesn't stop running agent |
| `--kill-task NAME` | Hard kill — stops agent, cleans DB, completes reminders |
| `--kill-all`       | Hard kill ALL active tasks                       |
| `--cleanup`        | Remove stale branches and unused workspaces      |
| `--cleanup-reminders` | Complete orphan reminders from finished tasks (only touches managed lists) |
| `--process-task ID`| Process a single task by ID (debug mode)         |
| `--healthcheck`    | Check daemon health and exit                     |
| `--once`           | Run one poll cycle and exit                      |
| `--daemon`         | Run continuously as a daemon                     |
| `--dry-run`        | Preview mode — no changes will be made           |
| `--verbose`        | Enable debug-level logging                       |
| `--config PATH`    | Path to config.toml (default: `~/.claude/agents/porter/config.toml`) |
| `--version`        | Print version and exit                           |

## Deployment

```bash
bash deploy/deploy.sh   # Tests → stop → backup → deploy → start → verify
```

The deploy script:
1. Runs pre-flight checks
2. Runs all tests (aborts on failure)
3. Stops old daemon (both v3 and v4 plists)
4. Backs up production code
5. Copies v4 entry point + lib/ modules
6. Installs launchd plist
7. Starts daemon and verifies health

## Task Lifecycle (State Machine)

```
                    ┌─────────────────────────────────────────────┐
                    │                                             │
  Apple Reminders   │   ┌──────┐    ┌─────────┐    ┌───────────┐ │
  ─────────────────►│   │ NEW  │───►│ ROUTING │───►│ IN_FLIGHT │ │
                    │   └──────┘    └─────────┘    └─────┬─────┘ │
                    │                                     │       │
                    │              ┌──────────────────────┤       │
                    │              │                      │       │
                    │              ▼                      ▼       │
                    │   ┌──────────────────┐   ┌────────────────┐│
                    │   │ AGENT_RUNNING    │   │ PRD_EVALUATING ││
                    │   │ (quickfix/bugfix)│   │ (feature/      ││
                    │   └────────┬─────────┘   │  refactor)     ││
                    │            │              └───────┬────────┘│
                    │            │                      │         │
                    │            │              ┌───────▼────────┐│
                    │            │              │AWAITING_APPROVAL││
                    │            │              └───────┬────────┘│
                    │            │                      │         │
                    │            │              ┌───────▼────────┐│
                    │            │              │ AGENT_RUNNING  ││
                    │            │              │ (approved)     ││
                    │            │              └───────┬────────┘│
                    │            │                      │         │
                    │            ├──────────────────────┘         │
                    │            ▼                                │
                    │   ┌────────────────┐   ┌─────────────────┐ │
                    │   │  PR_CREATED    │──►│ AWAITING_MERGE  │ │
                    │   └────────────────┘   └───────┬─────────┘ │
                    │                                 │           │
                    │                        ┌───────▼─────────┐ │
                    │                        │    MERGING       │ │
                    │                        └───────┬─────────┘ │
                    │                                 │           │
                    │                        ┌───────▼─────────┐ │
                    │                        │   COMPLETE       │ │
                    │                        └─────────────────┘ │
                    └─────────────────────────────────────────────┘

  Side states (reachable from most states):
    NEEDS_CLARIFICATION ──► (agent asks question → user answers → AGENT_RUNNING)
    ERROR               ──► (auto-retry up to retry_max → back to NEW)
    REJECTED            ──► (terminal, user rejected via approval)
```

## Task Types

Tasks are classified by the router based on title keywords or explicit tags.

| Type        | Tag        | PRD? | Approval? | PR? | Description                           |
|-------------|------------|------|-----------|-----|---------------------------------------|
| `quickfix`  | `[quick]`  | No   | No        | Yes | Skip PRD, minimal implementation      |
| `bugfix`    | `[bug]`    | Lite | No        | Yes | Lighter PRD, focused fix              |
| `feature`   | `[feature]`| Full | Yes       | Yes | Full PRD + approval ceremony          |
| `refactor`  | `[refactor]`| Full| Yes       | Yes | Full PRD, different impl prompt       |
| `analysis`  | `[analysis]`| No  | No        | No  | Read-only codebase exploration        |
| `composite` | (auto)     | No   | No        | —   | Decomposes into sub-tasks via Reminders|

## Priority System

Add `[P0]`–`[P4]` to task titles in Apple Reminders for priority ordering:

| Tag  | Level    | Description |
|------|----------|-------------|
| `[P0]` | Critical | Processed first |
| `[P1]` | High     | High priority   |
| `[P2]` | Medium   | Normal priority |
| `[P3]` | Low      | Low priority    |
| `[P4]` | Backlog  | Lowest priority |

## Creating Tasks

Tasks are created as Apple Reminders in project-specific lists:

```
Reminder List Name = GitHub repo name (e.g., "porter")
Reminder Title     = Task description with optional tags
Reminder Notes     = Additional context, repo URL, constraints
```

**Example reminder titles:**
- `[P1] Add user authentication to API`  → feature (full PRD)
- `[quick] Fix typo in README header`    → quickfix (no PRD)
- `[bug] Login fails on Safari`          → bugfix (lite PRD)
- `Refactor database layer`              → refactor (keyword match)

## Reminder Status Prefixes

The daemon updates the original task reminder's title prefix to show progress:

| Prefix              | Meaning                              | Action needed? |
|---------------------|--------------------------------------|---------------|
| `[QUEUED]`          | Waiting to be processed              | No |
| `[WORKING]`         | Agent is implementing                | No |
| `[REVIEW PRD]`      | PRD ready for review (link in notes) | **Yes** — complete to approve |
| `[QUESTION]`        | Agent needs input (question in notes)| **Yes** — change to `[ANSWER]`, answer in notes |
| `[PR #N]`           | PR created on GitHub                 | No |
| `[MERGE PR #N]`     | Ready for merge                      | **Yes** — complete to merge |
| `[RETRY N/M]`       | Failed, retrying after backoff       | No |
| `[FAILED]`          | Failed after all retries             | Check `--status` |

### User-set prefixes

| Prefix              | Purpose                              |
|---------------------|--------------------------------------|
| `[FIX]`             | Request PRD changes (feedback in notes) |
| `[ANSWER]`          | Answer agent question (answer in notes) |

## Monitoring

```bash
# Live daemon logs
tail -f ~/.claude/agents/porter/logs/daemon-stderr.log

# Status dashboard
python3 ~/.claude/agents/porter/porter.py --status

# Health check
python3 ~/.claude/agents/porter/porter.py --healthcheck
```

## Configuration

Config lives at `~/.claude/agents/porter/config.toml`. Key settings:

| Section        | Key                    | Default | Description                    |
|----------------|------------------------|---------|--------------------------------|
| `daemon`       | `poll_interval_seconds`| 300     | Poll interval (5 min)          |
| `daemon`       | `max_concurrent_tasks` | 5       | Max tasks across all projects  |
| `daemon`       | `max_concurrent_per_project` | 1 | One task per project at a time |
| `daemon`       | `retry_max`            | 3       | Max retries on failure         |
| `daemon`       | `approval_timeout_days`| 7       | Auto-reject unapproved PRDs   |
| `notification` | `notify_on_prd_ready`  | true    | iMessage on PRD ready          |
| `notification` | `notify_on_pr_created` | true    | iMessage on PR created         |
| `notification` | `notify_on_error`      | true    | iMessage on error              |

## Circuit Breakers

The daemon has circuit breakers per subsystem (git, Claude API, reminders). When a subsystem fails repeatedly:
- **Closed** → normal operation
- **Open** → skip tasks needing that subsystem (prevents cascading failures)
- **Half-open** → allow one attempt after timeout to test recovery

## Atomic Transitions

State transitions use compare-and-swap (`transition_if`) to prevent race conditions when multiple tasks are processed concurrently. If a task has already moved to a different state, the transition is rejected and the task is reloaded from the database.
