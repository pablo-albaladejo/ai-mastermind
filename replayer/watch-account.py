#!/usr/bin/env python3
"""
Append-only ledger of which Claude account was active, and when.

Why: the transcripts carry NO account field, and ~/.claude.json holds only the account
logged in *right now* — it is overwritten in place, with no history and no backup deep
enough to help (~/.claude/backups is a 5-slot ring about 4.6 minutes deep). A replayer
that reads that file at replay time would retroactively relabel every unreplayed
transcript after any /login. See docs/PLAN.md Finding E.

So the account has to be pinned at CAPTURE time. This watcher appends an entry whenever
accountUuid changes; replay.py joins it by message timestamp.

Run one instance per config dir (see launchd/). Watching beats polling: an account that
is switched and switched back between two polls is invisible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def read_account(config_dir: Path) -> dict | None:
    """.claude.json lives beside the config dir, or inside it when CLAUDE_CONFIG_DIR is set."""
    for cand in (config_dir / ".claude.json",
                 config_dir.parent / f"{config_dir.name}.json",
                 Path.home() / ".claude.json"):
        if cand.exists():
            try:
                d = json.loads(cand.read_text())
            except json.JSONDecodeError:
                return None
            oa = d.get("oauthAccount") or {}
            if not oa.get("accountUuid"):
                return None
            return {
                "accountUuid": oa["accountUuid"],
                "emailAddress": oa.get("emailAddress"),
                "organizationUuid": oa.get("organizationUuid"),
                "organizationName": oa.get("organizationName"),
                "source": str(cand),
            }
    return None


def label(acct: dict, override: str | None) -> str:
    if override:
        return override
    org = (acct.get("organizationName") or "").strip().lower().replace(" ", "-")
    return f"claude-{org}" if org else "claude-personal"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR",
                                                           os.path.expanduser("~/.claude")))
    ap.add_argument("--ledger", default=os.path.expanduser("~/.ai-mastermind/account-ledger.jsonl"))
    ap.add_argument("--label", help="force the account label (else derived from org name)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="record current state and exit")
    args = ap.parse_args()

    cfg = Path(args.config_dir)
    led = Path(args.ledger)
    led.parent.mkdir(parents=True, exist_ok=True)

    last_uuid = None
    if led.exists():
        for line in led.read_text().splitlines():
            if line.strip():
                last_uuid = json.loads(line).get("accountUuid")

    def record(acct):
        entry = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "account": label(acct, args.label),
            "config_dir": str(cfg),
            "machine": os.uname().nodename.split(".")[0],
            **acct,
        }
        with led.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        print(f"[ledger] {entry['observed_at']} {entry['account']} "
              f"({acct.get('emailAddress')})", flush=True)

    acct = read_account(cfg)
    if acct and acct["accountUuid"] != last_uuid:
        record(acct)
        last_uuid = acct["accountUuid"]
    elif not acct:
        print(f"[ledger] no account found under {cfg}", file=sys.stderr)

    if args.once:
        return

    while True:
        time.sleep(args.interval)
        acct = read_account(cfg)
        if acct and acct["accountUuid"] != last_uuid:
            record(acct)
            last_uuid = acct["accountUuid"]


if __name__ == "__main__":
    main()
