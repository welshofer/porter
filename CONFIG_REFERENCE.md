# Configuration Reference

All settings are read from `config.toml`. Sensitive values can be overridden via environment variables.

## `[daemon]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `poll_interval_seconds` | int | 300 | Seconds between poll cycles. Min: 60. |
| `max_concurrent_tasks` | int | 1 | Max tasks running in parallel globally. |
| `max_concurrent_per_project` | int | 1 | Max tasks per project (prevents git conflicts). |
| `retry_max` | int | 3 | Max retry attempts before moving to ERROR. |
| `retry_backoff_base_minutes` | int | 5 | Base for exponential backoff: `base * 4^(attempt-1)` minutes. |
| `approval_timeout_days` | int | 7 | Days before unapproved PRDs are auto-rejected. |

## `[paths]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workspaces` | string | `~/.claude/agents/porter/workspaces` | Directory for cloned repos. |
| `logs` | string | `~/.claude/agents/porter/logs` | Directory for log files and heartbeat. |
| `db` | string | `~/.claude/agents/porter/state.db` | SQLite database path. |

## `[logging]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `level` | string | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL. |
| `compress_after_days` | int | 7 | Compress log files older than this. 0 = disable. |

## `[prd_repo]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `url` | string | *required* | GitHub URL for the central PRD repository. |
| `local_path` | string | `~/.claude/agents/porter/workspaces/prds` | Local clone path. |

## `[tools]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `reminders_bin` | string | `/opt/homebrew/bin/reminders` | Path to `reminders-cli` binary. |
| `claude_bin` | string | `/opt/homebrew/bin/claude` | Path to `claude` CLI binary. |
| `gh_bin` | string | `/opt/homebrew/bin/gh` | Path to GitHub CLI binary. |
| `imsg_bin` | string | `""` | Path to `imsg` binary. Empty = notifications disabled. |

## `[notification]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `phone_number` | string | `""` | iMessage recipient. Overridden by `PORTER_PHONE` env var. |
| `notify_on_prd_ready` | bool | true | Send notification when PRD is ready for review. |
| `notify_on_pr_created` | bool | true | Send notification when PR is created. |
| `notify_on_error` | bool | true | Send notification on task errors. |
| `use_emoji` | bool | true | Include emoji in notification messages. |
| `image_notifications` | bool | true | Generate contextual images via Gemini API. |
| `image_model` | string | `gemini-2.5-flash-image` | Gemini model for image generation. |

## `[claude]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `opus` | Model for PRD generation and evaluation. Short names: opus, sonnet, haiku. |
| `timeout_seconds` | int | 1800 | Timeout for PRD generation subprocess. |
| `permission_mode` | string | `bypassPermissions` | Agent SDK permission mode. |
| `max_turns` | int | 50 | Max turns for PRD generation. |
| `impl_max_turns` | int | 150 | Max turns for implementation agent sessions. |
| `impl_timeout_seconds` | int | 3600 | Timeout for implementation agent sessions. |
| `max_cost_per_task_usd` | float | null | Per-task cost cap. Null = unlimited. |
| `daily_budget_usd` | float | null | Aggregate daily spending cap. Null = unlimited. |

## `[projects]`

Map of Reminders list name → GitHub repo URL (or config dict).

```toml
[projects]
MyApp = "https://github.com/user/myapp"

# Advanced: with custom options
[projects.BigProject]
url = "https://github.com/user/bigproject"
prd_template = "/path/to/template.md"
test_command = "make test"
```

## `[git]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `clone_depth` | int | 1 | Shallow clone depth. 0 or null = full clone. |

## `[testing]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `run_tests_before_pr` | bool | true | Run external test verification before creating PR. |
| `timeout_seconds` | int | 600 | Test execution timeout (seconds). |
| `flaky_retries` | int | 1 | Extra attempts on test failure. 0 = no retry. |

## `[merge]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `strategy` | string | `squash` | PR merge strategy: squash, merge, or rebase. |
| `max_fix_iterations` | int | 3 | Max agent attempts to fix merge issues. |
| `auto_merge_on_review_complete` | bool | false | Auto-merge when all review comments are addressed. |

## `[github_issues]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | false | Enable GitHub Issues as a task source. |
| `label` | string | `porter` | Label to filter issues by. |
| `trusted_authors` | list of strings | `[]` | GitHub logins whose issues, issue comments and PR review comments are passed to the agent. Text from anyone else is ignored and logged. Empty means the repo's owner only. |

## `[pr_review]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auto_respond` | bool | false | Automatically address PR review comments from `[github_issues] trusted_authors`. |
| `check_interval_seconds` | int | 3600 | How often to check for new review comments. |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PORTER_PHONE` | Overrides `[notification] phone_number`. |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Required for image notifications. |
| `ANTHROPIC_API_KEY` | Required for Claude Agent SDK sessions. |
