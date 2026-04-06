#!/bin/bash
#
# Porter - Installation and Management Script
#
# Usage:
#   ./install.sh install      - Install and start the agent
#   ./install.sh uninstall    - Stop and remove the agent
#   ./install.sh start        - Start the agent
#   ./install.sh stop         - Stop the agent
#   ./install.sh restart      - Restart the agent
#   ./install.sh status       - Show agent status (launchd)
#   ./install.sh tasks        - Show detailed task status
#   ./install.sh logs         - Tail the agent logs
#   ./install.sh run          - Run one poll cycle manually
#   ./install.sh dry-run      - Preview what would be processed
#   ./install.sh reset NAME   - Reset a task to NEW state for retry
#   ./install.sh doctor       - Run health check
#   ./install.sh usage        - Show Claude API usage statistics
#   ./install.sh web [PORT]   - Start web dashboard
#   ./install.sh version      - Show version
#

set -e

AGENT_DIR="$HOME/.claude/agents/porter"
PLIST_NAME="com.$(whoami).porter.plist"
PLIST_SRC="$AGENT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_dependencies() {
    log_info "Checking dependencies..."

    local missing=()

    if ! command -v python3 &> /dev/null; then
        missing+=("python3")
    fi

    if ! command -v reminders &> /dev/null; then
        missing+=("reminders-cli (brew install keith/formulae/reminders-cli)")
    fi

    if ! command -v gh &> /dev/null; then
        missing+=("gh (brew install gh)")
    fi

    if ! command -v claude &> /dev/null; then
        missing+=("claude (Claude Code CLI)")
    fi

    if [ ! -f "$HOME/.local/bin/imsg" ]; then
        log_warn "imsg not found at ~/.local/bin/imsg - notifications will be disabled"
    fi

    if [ ${#missing[@]} -ne 0 ]; then
        log_error "Missing dependencies:"
        for dep in "${missing[@]}"; do
            echo "  - $dep"
        done
        exit 1
    fi

    log_info "All dependencies found"
}

install_agent() {
    log_info "Installing Porter..."

    check_dependencies

    # Check if plist source exists
    if [ ! -f "$PLIST_SRC" ]; then
        log_error "LaunchAgent plist not found: $PLIST_SRC"
        exit 1
    fi

    # Create LaunchAgents directory if needed
    mkdir -p "$HOME/Library/LaunchAgents"

    # Stop if already running
    if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
        log_info "Stopping existing agent..."
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi

    # Copy plist to LaunchAgents
    cp "$PLIST_SRC" "$PLIST_DST"

    # Install Python dependencies
    log_info "Installing Python dependencies..."
    python3 -m pip install --quiet toml 2>/dev/null || true

    # Load the agent
    log_info "Loading agent..."
    launchctl load "$PLIST_DST"

    log_info "Porter installed and started!"
    echo ""
    echo "Configuration: $AGENT_DIR/config.toml"
    echo "Logs:          $AGENT_DIR/logs/porter.log"
    echo ""
    echo "Edit config.toml to add your project mappings."
    echo "Create reminders with prefix 'Task: ' in mapped lists."
}

uninstall_agent() {
    log_info "Uninstalling Porter..."

    if [ -f "$PLIST_DST" ]; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        rm "$PLIST_DST"
        log_info "LaunchAgent removed"
    else
        log_warn "LaunchAgent not found"
    fi

    log_info "Porter uninstalled"
    echo ""
    echo "Note: Configuration and data preserved at $AGENT_DIR"
    echo "Delete manually if no longer needed."
}

start_agent() {
    if [ ! -f "$PLIST_DST" ]; then
        log_error "Agent not installed. Run './install.sh install' first."
        exit 1
    fi

    log_info "Starting Porter..."
    launchctl load "$PLIST_DST" 2>/dev/null || launchctl start "$PLIST_NAME"
    log_info "Agent started"
}

stop_agent() {
    if [ ! -f "$PLIST_DST" ]; then
        log_warn "Agent not installed"
        return
    fi

    log_info "Stopping Porter..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    log_info "Agent stopped"
}

restart_agent() {
    stop_agent
    sleep 1
    start_agent
}

show_status() {
    echo "Porter Status"
    echo "================="
    echo ""

    if launchctl list | grep -q "com.$(whoami).porter"; then
        echo -e "LaunchAgent: ${GREEN}Loaded${NC}"

        # Get PID if running
        local pid=$(launchctl list | grep "com.$(whoami).porter" | awk '{print $1}')
        if [ "$pid" != "-" ] && [ -n "$pid" ]; then
            echo -e "Process:     ${GREEN}Running (PID: $pid)${NC}"
        else
            echo -e "Process:     ${YELLOW}Idle (waiting for next interval)${NC}"
        fi
    else
        echo -e "LaunchAgent: ${RED}Not loaded${NC}"
    fi

    echo ""
    echo "Configuration: $AGENT_DIR/config.toml"
    echo "Database:      $AGENT_DIR/state.db"
    echo "Logs:          $AGENT_DIR/logs/porter.log"
    echo ""

    # Show active tasks from database
    if [ -f "$AGENT_DIR/state.db" ]; then
        echo "Active Tasks:"
        sqlite3 "$AGENT_DIR/state.db" "SELECT state, task_title FROM tasks WHERE state NOT IN ('COMPLETE', 'REJECTED', 'ERROR') ORDER BY created_at;" 2>/dev/null | while IFS='|' read -r state title; do
            echo "  [$state] $title"
        done

        local count=$(sqlite3 "$AGENT_DIR/state.db" "SELECT COUNT(*) FROM tasks WHERE state NOT IN ('COMPLETE', 'REJECTED', 'ERROR');" 2>/dev/null)
        if [ "$count" = "0" ]; then
            echo "  (none)"
        fi
    fi
}

tail_logs() {
    local log_file="$AGENT_DIR/logs/porter.log"

    if [ ! -f "$log_file" ]; then
        log_warn "Log file not found: $log_file"
        exit 1
    fi

    log_info "Tailing logs (Ctrl+C to stop)..."
    tail -f "$log_file"
}

run_once() {
    log_info "Running one poll cycle..."
    python3 "$AGENT_DIR/porter.py"
}

run_dry() {
    log_info "Running dry-run (preview mode)..."
    python3 "$AGENT_DIR/porter.py" --dry-run
}

show_tasks() {
    python3 "$AGENT_DIR/porter.py" --status
}

reset_task() {
    if [ -z "${2:-}" ]; then
        log_error "Usage: $0 reset <task_name>"
        exit 1
    fi
    python3 "$AGENT_DIR/porter.py" --reset "$2"
}

run_doctor() {
    python3 "$AGENT_DIR/porter.py" --health
}

show_usage() {
    python3 "$AGENT_DIR/porter.py" --usage
}

start_web() {
    local port="${2:-8742}"
    python3 "$AGENT_DIR/porter.py" --web "$port"
}

show_version() {
    python3 "$AGENT_DIR/porter.py" --version
}

# Main command handler
case "${1:-}" in
    install)
        install_agent
        ;;
    uninstall)
        uninstall_agent
        ;;
    start)
        start_agent
        ;;
    stop)
        stop_agent
        ;;
    restart)
        restart_agent
        ;;
    status)
        show_status
        ;;
    tasks)
        show_tasks
        ;;
    logs)
        tail_logs
        ;;
    run)
        run_once
        ;;
    dry-run)
        run_dry
        ;;
    reset)
        reset_task "$@"
        ;;
    doctor|health)
        run_doctor
        ;;
    usage)
        show_usage
        ;;
    web)
        start_web "$@"
        ;;
    version|-v|--version)
        show_version
        ;;
    *)
        echo "Porter - Reminder-Driven Autonomous Coding Agent"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  install      Install and start the agent"
        echo "  uninstall    Stop and remove the agent"
        echo "  start        Start the agent"
        echo "  stop         Stop the agent"
        echo "  restart      Restart the agent"
        echo "  status       Show agent status (launchd)"
        echo "  tasks        Show detailed task status"
        echo "  logs         Tail the agent logs"
        echo "  run          Run one poll cycle manually"
        echo "  dry-run      Preview what would be processed"
        echo "  reset NAME   Reset a task to NEW state for retry"
        echo "  doctor       Run health check (verify dependencies)"
        echo "  usage        Show Claude API usage statistics"
        echo "  web [PORT]   Start web dashboard (default port: 8742)"
        echo "  version      Show version"
        exit 1
        ;;
esac
