#!/bin/bash
# Set up THIS macOS account as a canopy desktop runner (Claude app Code tab).
# Run it once, in a Terminal of the account that will host the runner, after the
# Claude app is signed in. Idempotent: re-running keeps the existing pairing.
#
#   ./setup-account.sh [--workspace dimagi] [--name <runner name>] [--model <model>]
#
# What it does:
#   1. installs the Claude Code CLI if missing and makes sure it is signed in
#      (the runner seeds each session with `claude -p` before the app adopts it);
#   2. copies this runner into ~/.canopy/desktop/src and registers its
#      `canopy-desktop` mod marketplace with Claude Code;
#   3. creates a scratch project repo, ~/canopy-scratch/ccr-scratch;
#   4. pairs the runner with canopy-web — it asks for a canopy-web token of the
#      PERSON who will own this runner (canopy-web → Settings → Tokens). An
#      agent's login cannot own a runner;
#   5. installs a LaunchAgent that keeps the runner running (it starts Claude.app
#      itself), so the account only has to stay LOGGED IN. Fast-user-switch away;
#      never log out — a logged-out account stops the app and the runner.
set -euo pipefail

WORKSPACE=dimagi
NAME="$(id -un)-desktop"
MODEL=""
BASE_URL="https://labs.connect.dimagi.com/canopy"
while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="$HOME/.canopy/desktop"
SRC="$HOME_DIR/src"
PY=/usr/bin/python3
mkdir -p "$HOME_DIR"

echo "== 1/5 Claude Code CLI"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
if ! claude auth status >/dev/null 2>&1; then
  echo "Sign the CLI in (a browser opens; use the same Claude account as the app):"
  claude auth login
fi
claude auth status | head -3

echo "== 2/5 runner + mod"
rm -rf "$SRC" && mkdir -p "$SRC" && cp -R "$HERE/." "$SRC/"
"$PY" "$SRC/ccd_runner.py" install-mod

echo "== 3/5 scratch project"
SCRATCH="$HOME/canopy-scratch/ccr-scratch"
if [ ! -d "$SCRATCH/.git" ]; then
  mkdir -p "$SCRATCH"
  git -C "$SCRATCH" init -q -b main
  echo "# ccr-scratch — a throwaway repo for the canopy desktop runner" > "$SCRATCH/README.md"
  git -C "$SCRATCH" add -A
  git -C "$SCRATCH" -c user.name="$(id -un)" -c user.email="$(id -un)@localhost" commit -qm init
fi
git config --global user.name >/dev/null || git config --global user.name "$(id -un)"
git config --global user.email >/dev/null || git config --global user.email "$(id -un)@localhost"

echo "== 4/5 pair with canopy-web"
CONFIG="$HOME_DIR/runner.json"
if [ ! -f "$CONFIG" ]; then
  TOKEN_FILE="$HOME_DIR/token"
  if [ ! -s "$TOKEN_FILE" ]; then
    read -rsp "Paste the runner OWNER's canopy-web token (input hidden): " TOKEN; echo
    umask 077; printf '%s' "$TOKEN" > "$TOKEN_FILE"; unset TOKEN
  fi
  "$PY" "$SRC/ccd_runner.py" pair --config "$CONFIG" --token "@$TOKEN_FILE" \
    --base-url "$BASE_URL" --workspace "$WORKSPACE" --name "$NAME" \
    --project "ccr-scratch=$SCRATCH" ${MODEL:+--model "$MODEL"}
else
  echo "already paired: $CONFIG"
fi

echo "== 5/5 LaunchAgent"
LABEL=com.canopy.desktop-runner
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-u</string><string>$SRC/ccd_runner.py</string>
    <string>run</string><string>--config</string><string>$CONFIG</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/canopy-desktop-runner.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/canopy-desktop-runner.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
tail -3 "$HOME/Library/Logs/canopy-desktop-runner.log" || true
echo
echo "Done. Keep this account LOGGED IN (fast-user-switch away; don't log out)."
