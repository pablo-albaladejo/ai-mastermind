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


def label(acct: dict, override: str | None = None) -> str:
    """work vs personal, derived from the account itself.

    A fixed --label cannot be right: one config dir can hold different accounts over
    time (observed: ~/.claude was a work account until 2026-09-01, personal after), and
    a flag pinned at watcher-start would mislabel everything on the other side of the
    switch. Personal Claude accounts name their org after their own email; a real
    company org does not.
    """
    if override:
        return override
    org = (acct.get("organizationName") or "").strip().lower()
    email = (acct.get("emailAddress") or "").strip().lower()
    local = email.split("@")[0] if email else ""
    if local and local in org:
        return "claude-personal"
    return "claude-work" if org else "claude-personal"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR",
                                                           os.path.expanduser("~/.claude")))
    ap.add_argument("--ledger", default=os.path.expanduser("~/.ai-mastermind/account-ledger.jsonl"))
    ap.add_argument("--label", help="override the derived label; normally leave unset")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="record current state and exit")
    args = ap.parse_args()

    cfg = Path(args.config_dir)
    led = Path(args.ledger)
    led.parent.mkdir(parents=True, exist_ok=True)

    # Per config_dir, NOT the last line of the file. Several watchers share one ledger,
    # so comparing against whatever directory happened to write last makes every restart
    # look like an account change and append an entry that never happened. That matters:
    # where a directory has held more than one account the replayer attributes by joining
    # this ledger on time, so a spurious entry silently relabels real spans.
    last_uuid = None
    if led.exists():
        for line in led.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("config_dir") == str(cfg):
                last_uuid = e.get("accountUuid")

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
