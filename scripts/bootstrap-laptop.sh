#!/usr/bin/env bash
# One-shot bootstrap for the LAPTOP (or any second machine).
#
# Run this ON the laptop. It clones/updates the repo, then configures the same two
# Claude profiles and the replayer, pointing at the Langfuse instance on the mini
# over Tailscale.
#
#   curl -fsSL <raw-url>/scripts/bootstrap-laptop.sh | bash -s -- \
#       --mini <tailscale-name-or-ip> --public-key pk-lf-... --secret-key sk-lf-...
#
# or, with the repo already cloned:
#   ./scripts/bootstrap-laptop.sh --mini mac --public-key pk-lf-... --secret-key sk-lf-...

set -euo pipefail

MINI=""; PUBLIC_KEY=""; SECRET_KEY=""; REPO_DIR="${REPO_DIR:-$HOME/development/ai-mastermind}"
REPO_URL="${REPO_URL:-git@github.com:<your-user>/ai-mastermind.git}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mini)       MINI="$2"; shift 2 ;;
    --public-key) PUBLIC_KEY="$2"; shift 2 ;;
    --secret-key) SECRET_KEY="$2"; shift 2 ;;
    --repo-dir)   REPO_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$MINI" && -n "$PUBLIC_KEY" && -n "$SECRET_KEY" ]] || {
  echo "need --mini --public-key --secret-key" >&2; exit 2; }

echo "==> reachability: the mini must answer over Tailscale"
if ! curl -fsS --max-time 10 -o /dev/null "http://$MINI:3001/api/public/health"; then
  echo "   FAILED: http://$MINI:3001 is not answering." >&2
  echo "   Check: tailscale status | grep $MINI   and that the stack is up on the mini." >&2
  exit 1
fi
echo "   ok — Langfuse reachable at http://$MINI:3001"

echo "==> repo"
if [[ -d "$REPO_DIR/.git" ]]; then
  git -C "$REPO_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$REPO_DIR")"
  git clone "$REPO_URL" "$REPO_DIR"
fi

echo "==> host setup"
"$REPO_DIR/scripts/setup-host.sh" \
  --endpoint "http://$MINI:3001/api/public/otel" \
  --public-key "$PUBLIC_KEY" --secret-key "$SECRET_KEY"

cat <<EOF

==> laptop-specific remaining steps

1. Log the personal profile in (interactive, cannot be scripted):
       ccp
       /login          # choose the PERSONAL account
   Then re-seed the ledger:
       python3 $REPO_DIR/replayer/watch-account.py --once \\
           --config-dir ~/.claude-personal --label claude-personal

2. Cursor (laptop only). Point it at the LiteLLM proxy so its calls are traced:
       Cursor Settings -> Models -> Override OpenAI Base URL
           http://$MINI:4000/v1
       API key: the LiteLLM virtual key for cursor
   NOTE: this only takes effect in Cursor's "own API key" mode, which bypasses the
   included subscription usage. Verify that trade before relying on it — see
   docs/PLAN.md §3.3. Claude Code needs no such change: it is traced from transcripts.

3. Confirm data is flowing:
       tail -f ~/.ai-mastermind/com.ai-mastermind.replayer.log
   then open http://$MINI:3001 and filter by tag \`$(uname -n | cut -d. -f1)\`.
EOF
