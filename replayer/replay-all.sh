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

account_arg_for() {  # -> "--account X", o vacio para dejar que mande el ledger
  # A fixed label is only safe when a config dir has held exactly ONE account. If it has
  # held more (observed: ~/.claude was work until 2026-09-01 and personal after), the
  # account depends on WHEN, not on what the config file says today — reading it at
  # replay time is the very defect the ledger exists to avoid. In that case pass nothing
  # and let replay.py time-join against the ledger.
  local dir="$1"
  python3 - "$dir" "$HOME/.ai-mastermind/account-ledger.jsonl" <<'PY_INNER'
import json, os, sys
d, ledger = sys.argv[1], sys.argv[2]
seen = []
if os.path.exists(ledger):
    for line in open(ledger):
        if line.strip():
            e = json.loads(line)
            if e.get("config_dir") == d:
                seen.append(e["account"])
uniq = sorted(set(seen))
if len(uniq) == 1:
    print("--account " + uniq[0])          # un solo dueño: etiqueta directa
elif len(uniq) > 1:
    print("")                              # cambió de cuenta: manda el ledger, por fecha
else:
    print("")                              # sin registro: unknown-pre-split
PY_INNER
}


for dir in "$HOME"/.claude "$HOME"/.claude-work "$HOME"/.claude-personal "$HOME"/.claude-shared; do
  [ -d "$dir/projects" ] || continue
  n=$(find "$dir/projects" -name '*.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] || continue
  acct_arg=$(account_arg_for "$dir")
  echo "--- $(basename "$dir") · $n ficheros · ${acct_arg:-cuenta por ledger (cambio de cuenta detectado)}"
  python3 "$REPO/replayer/replay.py" \
      --root "$dir/projects" \
      ${acct_arg} \
      --checkpoint "$STATE/checkpoint-$(basename "$dir").json" \
      $EXTRA || echo "    (fallo, se reintenta en la siguiente vuelta)"
done
