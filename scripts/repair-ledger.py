#!/usr/bin/env python3
"""Re-label an account ledger written by an older, broken rule.

Two defects produced wrong labels in ledgers written before 2026-09-02:

  * sanitising the repo for publication replaced a literal org name with an unset env
    var, and `"" in org` is True for every string, so every profile became "claude-work";
  * the watcher was started with a fixed --label per directory, which cannot survive the
    directory later holding a different account.

The event data in each entry (email, organizationName) was always recorded correctly, so
the labels can be recomputed from it. Idempotent; writes a .bak first.
"""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = Path(os.path.expanduser(
    sys.argv[1] if len(sys.argv) > 1 else "~/.ai-mastermind/account-ledger.jsonl"))

if not LEDGER.exists():
    sys.exit(f"no ledger at {LEDGER} — nothing to repair")

spec = importlib.util.spec_from_file_location("w", REPO / "replayer" / "watch-account.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shutil.copy(LEDGER, str(LEDGER) + ".bak")
rows, changed = [], 0
for line in LEDGER.read_text().splitlines():
    if not line.strip():
        continue
    d = json.loads(line)
    before = d.get("account")
    d["account"] = mod.label(d)
    if before != d["account"]:
        changed += 1
        print(f"  {d['observed_at'][:19]}  {d.get('emailAddress','?'):36s} "
              f"{before} -> {d['account']}")
    rows.append(d)

# Collapse redundant entries. The ledger records account CHANGES, so for one directory
# two consecutive entries naming the same account carry no information — they are the
# artefact of a watcher restart under the pre-2026-09-03 bug, which compared against the
# last line of the whole file instead of the last line for its own directory. Keeping the
# earliest of each run preserves the real transition timestamps, which is what the
# replayer joins on.
rows.sort(key=lambda d: d["observed_at"])
kept, last_by_dir = [], {}
for d in rows:
    key = d.get("config_dir")
    if last_by_dir.get(key) == d.get("accountUuid"):
        continue
    last_by_dir[key] = d.get("accountUuid")
    kept.append(d)
dropped = len(rows) - len(kept)

LEDGER.write_text("".join(json.dumps(d) + "\n" for d in kept))
print(f"  {changed} relabelled, {dropped} redundant dropped, {len(kept)} kept "
      f"· backup at {LEDGER}.bak")
if changed:
    print("  -> re-run replay-all.sh so the corrected labels reach Langfuse")
