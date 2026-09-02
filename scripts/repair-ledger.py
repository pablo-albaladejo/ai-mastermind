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

LEDGER.write_text("".join(json.dumps(d) + "\n" for d in rows))
print(f"  {changed} relabelled of {len(rows)} · backup at {LEDGER}.bak")
if changed:
    print("  -> re-run replay-all.sh so the corrected labels reach Langfuse")
