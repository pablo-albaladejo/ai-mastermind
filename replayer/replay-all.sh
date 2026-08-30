#!/usr/bin/env bash
# Replay EVERY Claude profile on this host, one pass per config dir.
#
# Why per-dir and not one pass over ~/.claude: a machine can run several profiles in
# PARALLEL (the laptop has ~/.claude-work with 3.5k transcripts and ~/.claude-personal
# with 100, both live). The account is decided by WHICH DIRECTORY a transcript was
# written into — a timestamp ledger cannot tell two concurrent dirs apart. So each root
# is replayed with an explicit --account.
#
# Profiles are DISCOVERED, not assumed: layouts differ between machines.
set -uo pipefail
STATE="$HOME/.ai-mastermind"
set -a; . "$STATE/env"; set +a
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXTRA="${1:-}"

label_for() {  # derive the account label from the dir's own .claude.json
  local dir="$1"
  python3 - "$dir" <<'PY'
import json, os, sys
d = sys.argv[1]
for cand in (os.path.join(d, ".claude.json"), d + ".json", os.path.expanduser("~/.claude.json")):
    if os.path.exists(cand):
        try: oa = json.load(open(cand)).get("oauthAccount") or {}
        except Exception: continue
        if oa.get("accountUuid"):
            org = (oa.get("organizationName") or "").strip().lower()
            print("claude-work" if os.environ.get("AI_MASTERMIND_WORK_ORG","") in org else "claude-personal"); sys.exit()
print("claude-unknown")
PY
}

for dir in "$HOME"/.claude "$HOME"/.claude-work "$HOME"/.claude-personal "$HOME"/.claude-shared; do
  [ -d "$dir/projects" ] || continue
  n=$(find "$dir/projects" -name '*.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] || continue
  acct=$(label_for "$dir")
  echo "--- $(basename "$dir") · $n ficheros · cuenta=$acct"
  python3 "$REPO/replayer/replay.py" \
      --root "$dir/projects" \
      --account "$acct" \
      --checkpoint "$STATE/checkpoint-$(basename "$dir").json" \
      $EXTRA || echo "    (fallo, se reintenta en la siguiente vuelta)"
done
