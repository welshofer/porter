#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Porter Deploy Script
# Deploys from dev repo to production location and manages launchd plist.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PROD_DIR="$HOME/.claude/agents/porter"
PLIST_LABEL="com.$(whoami).porter-v4"
PLIST_DIR="$HOME/Library/LaunchAgents"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo -e "${GREEN}=== Porter Deploy ===${NC}"
echo "Source: $REPO_DIR"
echo "Target: $PROD_DIR"
echo ""

# Pre-flight checks
echo -e "${YELLOW}[1/7] Running pre-flight checks...${NC}"

if [ ! -f "$REPO_DIR/porter.py" ]; then
    echo -e "${RED}ERROR: porter.py not found in $REPO_DIR${NC}"
    exit 1
fi

if [ ! -d "$REPO_DIR/lib" ]; then
    echo -e "${RED}ERROR: lib/ directory not found in $REPO_DIR${NC}"
    exit 1
fi

# Run tests
echo -e "${YELLOW}[2/7] Running tests...${NC}"
cd "$REPO_DIR"
if python3 -m pytest tests/ -v --tb=short; then
    echo -e "${GREEN}All tests passed${NC}"
else
    echo -e "${RED}Tests failed! Aborting deploy.${NC}"
    exit 1
fi

# Stop old daemon
echo -e "${YELLOW}[3/7] Stopping old daemon...${NC}"
if launchctl list 2>/dev/null | grep -q "$PLIST_LABEL"; then
    launchctl unload "$PLIST_DIR/$PLIST_LABEL.plist" 2>/dev/null || true
    echo "  Stopped: $PLIST_LABEL"
fi
sleep 2

# Backup production
echo -e "${YELLOW}[4/7] Backing up production...${NC}"
BACKUP_DIR="$PROD_DIR/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
if [ -f "$PROD_DIR/porter.py" ]; then
    cp "$PROD_DIR/porter.py" "$BACKUP_DIR/"
    echo "  Backed up porter.py"
fi
if [ -d "$PROD_DIR/lib" ]; then
    cp -r "$PROD_DIR/lib" "$BACKUP_DIR/"
    echo "  Backed up lib/"
fi

# Deploy v4
echo -e "${YELLOW}[5/7] Deploying v4...${NC}"
mkdir -p "$PROD_DIR/lib"

cp "$REPO_DIR/porter.py" "$PROD_DIR/porter.py"
echo "  Copied porter.py"

cp "$REPO_DIR/lib/"*.py "$PROD_DIR/lib/"
echo "  Copied lib/ modules"

if [ ! -f "$PROD_DIR/config.toml" ]; then
    cp "$REPO_DIR/config.example.toml" "$PROD_DIR/config.toml"
    echo "  Copied config.example.toml -> config.toml (new install)"
else
    echo "  Config exists — not overwriting"
fi

# Generate and install plist
echo -e "${YELLOW}[6/7] Installing launchd plist...${NC}"
PYTHON_BIN=$(which python3)

cat > "$PLIST_DIR/$PLIST_LABEL.plist" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$PROD_DIR/porter.py</string>
        <string>--daemon</string>
    </array>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>$PROD_DIR/logs/daemon-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$PROD_DIR/logs/daemon-stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
PLISTEOF
echo "  Generated and installed plist"

# Start daemon
launchctl load "$PLIST_DIR/$PLIST_LABEL.plist"
echo "  Started $PLIST_LABEL"
sleep 3

# Verify health
echo -e "${YELLOW}[7/7] Verifying health...${NC}"
if pgrep -f "porter.py.*--daemon" > /dev/null; then
    PID=$(pgrep -f "porter.py.*--daemon")
    echo -e "${GREEN}Daemon running (PID $PID)${NC}"
else
    echo -e "${RED}WARNING: Daemon process not found — check logs${NC}"
    echo "  Logs: $PROD_DIR/logs/"
fi

echo ""
echo -e "${GREEN}=== Deploy Complete ===${NC}"
echo "Monitor: tail -f $PROD_DIR/logs/daemon-stderr.log"
echo "Status:  python3 $PROD_DIR/porter.py --status"
echo "Health:  python3 $PROD_DIR/porter.py --healthcheck"
