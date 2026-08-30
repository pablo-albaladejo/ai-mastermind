#!/usr/bin/env bash
# Configure one host (mini or laptop) to feed the ai-mastermind Langfuse instance.
#
# Idempotent: safe to re-run. Touches only ~/.ai-mastermind, ~/.claude-personal,
# ~/Library/LaunchAgents, and appends a guarded block to the shell rc.
# It never modifies the existing ~/.claude profile beyond adding cleanupPeriodDays.
#
#   ./setup-host.sh --endpoint http://<mini>:3001/api/public/otel \
#                   --public-key pk-lf-... --secret-key sk-lf-...
#
# On the server host the endpoint is http://localhost:3001/api/public/otel.

set -euo pipefail

ENDPOINT=""; PUBLIC_KEY=""; SECRET_KEY=""; REPO="$(cd "$(dirname "$0")/.." && pwd)"
WITH_CONTENT="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint)   ENDPOINT="$2"; shift 2 ;;
    --public-key) PUBLIC_KEY="$2"; shift 2 ;;
    --secret-key) SECRET_KEY="$2"; shift 2 ;;
    --with-content) WITH_CONTENT="true"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$ENDPOINT" && -n "$PUBLIC_KEY" && -n "$SECRET_KEY" ]] || {
  echo "need --endpoint --public-key --secret-key" >&2; exit 2; }

STATE="$HOME/.ai-mastermind"
mkdir -p "$STATE" "$HOME/Library/LaunchAgents"

echo "==> credentials"
umask 077
cat > "$STATE/env" <<EOF
LANGFUSE_OTEL_ENDPOINT=$ENDPOINT
LANGFUSE_PUBLIC_KEY=$PUBLIC_KEY
LANGFUSE_SECRET_KEY=$SECRET_KEY
EOF
chmod 600 "$STATE/env"

echo "==> retention on every profile (transcripts are a 30-day cache by default)"
for dir in "$HOME/.claude" "$HOME/.claude-personal"; do
  mkdir -p "$dir"
  f="$dir/settings.json"
  [[ -f "$f" ]] || echo '{}' > "$f"
  python3 - "$f" <<'PY'
import json,sys
p=sys.argv[1]
d=json.load(open(p))
if d.get("cleanupPeriodDays") != 3650:
    d["cleanupPeriodDays"]=3650
    json.dump(d,open(p,"w"),indent=2)
    print(f"   set cleanupPeriodDays=3650 in {p}")
else:
    print(f"   ok {p}")
PY
done

echo "==> shell helpers"
RC="$HOME/.zshrc"; [[ -f "$RC" ]] || RC="$HOME/.bashrc"
MARK="# >>> ai-mastermind >>>"
if ! grep -qF "$MARK" "$RC" 2>/dev/null; then
  cat >> "$RC" <<'EOF'

# >>> ai-mastermind >>>
# Two Claude profiles, each its own CLAUDE_CONFIG_DIR. The config dir is the account
# boundary: transcripts carry no account field, so attribution comes from which dir
# they were written into.
ccw() { CLAUDE_CONFIG_DIR="$HOME/.claude"          claude "$@"; }   # work
ccp() { CLAUDE_CONFIG_DIR="$HOME/.claude-personal" claude "$@"; }   # personal
# <<< ai-mastermind <<<
EOF
  echo "   appended helpers to $RC (ccw = work, ccp = personal)"
else
  echo "   helpers already present in $RC"
fi

echo "==> seeding the account ledger for each profile"
python3 "$REPO/replayer/watch-account.py" --once --config-dir "$HOME/.claude" --label claude-work || true
if [[ -f "$HOME/.claude-personal/.claude.json" ]]; then
  python3 "$REPO/replayer/watch-account.py" --once --config-dir "$HOME/.claude-personal" --label claude-personal || true
else
  echo "   personal profile not logged in yet — run: ccp   then /login"
fi

echo "==> launchd agents"
mk_agent() { # name, program args...
  local name="$1"; shift
  local plist="$HOME/Library/LaunchAgents/$name.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$name</string>"
    echo '  <key>ProgramArguments</key><array>'
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo "  <key>StandardOutPath</key><string>$STATE/$name.log</string>"
    echo "  <key>StandardErrorPath</key><string>$STATE/$name.err</string>"
    echo '</dict></plist>'
  } > "$plist"
  launchctl bootout "gui/$UID/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null || launchctl load "$plist" 2>/dev/null || true
  echo "   $name"
}

PY3="$(command -v python3)"
mk_agent com.ai-mastermind.account-work "$PY3" "$REPO/replayer/watch-account.py" \
  --config-dir "$HOME/.claude" --label claude-work
mk_agent com.ai-mastermind.account-personal "$PY3" "$REPO/replayer/watch-account.py" \
  --config-dir "$HOME/.claude-personal" --label claude-personal

# The replayer runs on a loop rather than KeepAlive-restart-on-exit, so a crash backs off.
cat > "$STATE/replay-loop.sh" <<EOF
#!/usr/bin/env bash
set -a; . "$STATE/env"; set +a
while true; do
  "$PY3" "$REPO/replayer/replay.py" $( [[ "$WITH_CONTENT" == true ]] && echo --with-content ) || echo "replay failed, backing off"
  sleep 120
done
EOF
chmod +x "$STATE/replay-loop.sh"
mk_agent com.ai-mastermind.replayer /bin/bash "$STATE/replay-loop.sh"

echo
echo "done. verify with:"
echo "  launchctl list | grep ai-mastermind"
echo "  tail -f $STATE/com.ai-mastermind.replayer.log"
