#!/usr/bin/env python3
"""
Claude Code transcript -> Langfuse (OTLP/HTTP JSON) replayer.

Why this exists rather than Claude Code's native OTel: the OTLP destination is fixed by
org-managed settings on this machine (see docs/PLAN.md Finding A), and the beta spans
omit the 1h/5m cache split. The transcripts are both reachable and richer.

Every non-obvious rule below was forced by a measurement; see docs/PLAN.md §7.

  - A JSONL record is a CONTENT BLOCK, not an API call. One response is written as N
    records that each repeat the same `usage`. Summing records inflates tokens 2.21x.
    We key on message.id and take the max usage (it accumulates across blocks).
  - Per-block fields (tool_use) must NOT be deduped. We only dedupe usage/model.
  - Transcripts nest THREE levels: session / subagents / workflows/wf_*/. `journal.jsonl`
    files are not transcripts.
  - `<synthetic>` records are API failures with all-zero usage; they are emitted as error
    events, never as priced generations.
  - The account is NOT in the transcript. It comes from a capture-time ledger, joined by
    timestamp, because ~/.claude.json is overwritten in place with no history.
  - The corpus is live and growing; a backfill must run against a frozen --until slice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

# --- pricing ------------------------------------------------------------------
# USD per million tokens. Cache multipliers (verified against Claude Code's own
# cost-state records to within 0.14%): read 0.1x input, 5m write 1.25x, 1h write 2.0x.
PRICE_TABLE_VERSION = "2026-08-30"
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_MULT, CACHE_WRITE_5M_MULT, CACHE_WRITE_1H_MULT = 0.1, 1.25, 2.0


class UnknownModel(Exception):
    """Raised rather than silently pricing an unknown model at $0."""


def cost_details(model: str, u: dict) -> dict:
    if model not in PRICES:
        raise UnknownModel(model)
    p_in, p_out = PRICES[model]
    cc = u.get("cache_creation") or {}
    e1h = cc.get("ephemeral_1h_input_tokens", 0)
    e5m = cc.get("ephemeral_5m_input_tokens", 0)
    cw = u.get("cache_creation_input_tokens", 0)
    # cache_creation_input_tokens is authoritative; the split is a breakdown of it and
    # can disagree by a few tokens on multi-iteration responses. Reconcile onto cw.
    if e1h + e5m != cw:
        if e1h + e5m == 0:
            e5m = cw
        else:
            scale = cw / (e1h + e5m)
            e1h, e5m = e1h * scale, e5m * scale
    m = 1e6
    parts = {
        "input": u.get("input_tokens", 0) / m * p_in,
        "output": u.get("output_tokens", 0) / m * p_out,
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0) / m * p_in * CACHE_READ_MULT,
        "cache_creation_input_tokens_1h": e1h / m * p_in * CACHE_WRITE_1H_MULT,
        "cache_creation_input_tokens_5m": e5m / m * p_in * CACHE_WRITE_5M_MULT,
    }
    # Langfuse only populates its own total_cost column from a "total" key; without it
    # the UI's cost aggregations read 0 even though cost_details is stored correctly.
    parts["total"] = sum(parts.values())
    return parts


def usage_details(u: dict) -> dict:
    cc = u.get("cache_creation") or {}
    return {
        "input": u.get("input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
        "cache_creation_1h": cc.get("ephemeral_1h_input_tokens", 0),
        "cache_creation_5m": cc.get("ephemeral_5m_input_tokens", 0),
    }



def repo_from_cwd(cwd: str | None) -> str | None:
    """Repository name from a working directory.

    basename() is wrong here: inside a git worktree it returns the BRANCH, not the repo.
    `/Users/x/development/chatbot-kb.worktrees/main` basenames to "main", which was 19% of
    all spans and meant nothing. Strip the `<repo>.worktrees/<branch>` layer, and also the
    trailing subdirectory when the session was started below the repo root.
    """
    if not cwd:
        return None
    parts = [p for p in str(cwd).strip("/").split("/") if p]
    for i, p in enumerate(parts):
        if p.endswith(".worktrees"):
            return p[: -len(".worktrees")]
    # not a worktree: the repo is the first path element under a known code root
    for root in ("development", "src", "code", "projects", "repos", "work"):
        if root in parts:
            i = parts.index(root)
            if len(parts) > i + 1:
                return parts[i + 1]
    return parts[-1] if parts else None


# --- ids ----------------------------------------------------------------------
def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:n]


def trace_id(session_id: str) -> str:
    return _hex("trace:" + session_id, 32)


def span_id(seed: str) -> str:
    return _hex("span:" + seed, 16)


# --- account ledger -----------------------------------------------------------
class Ledger:
    """Capture-time record of which account was active when.

    ~/.claude.json holds only the account logged in *right now* and is overwritten in
    place, so reading it at replay time would retroactively relabel history after any
    /login. Entries are appended by watch-account.py and joined here by timestamp.
    """

    def __init__(self, path: Path):
        self.entries = []
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    self.entries.append(json.loads(line))
        self.entries.sort(key=lambda e: e["observed_at"])

    def at(self, ts_iso: str, default: str = "unknown-pre-split") -> str:
        if not self.entries:
            return default
        hit = None
        for e in self.entries:
            if e["observed_at"] <= ts_iso:
                hit = e
            else:
                break
        return hit["account"] if hit else default


# --- transcript scanning ------------------------------------------------------
def iter_transcripts(root: Path):
    """Yield every real transcript. journal.jsonl files are workflow logs, not
    transcripts (no sessionId/message), and must be skipped."""
    for p in root.rglob("*.jsonl"):
        if p.name == "journal.jsonl":
            continue
        yield p


def classify(path: Path, root: Path) -> tuple[str, str | None]:
    """-> (kind, workflow_id). kind in {main, subagent, workflow-agent}."""
    rel = path.relative_to(root).parts
    if "workflows" in rel:
        i = rel.index("workflows")
        return "workflow-agent", rel[i + 1] if len(rel) > i + 1 else None
    if "subagents" in rel:
        return "subagent", None
    return "main", None


def to_nano(ts_iso: str) -> int:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


# --- OTLP emission ------------------------------------------------------------
def attr(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    if isinstance(v, (list, tuple)):
        return {"key": k, "value": {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}}
    return {"key": k, "value": {"stringValue": str(v)}}


class Exporter:
    def __init__(self, endpoint: str, public_key: str, secret_key: str, dry_run=False):
        self.endpoint = endpoint.rstrip("/")
        self.auth = b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.dry_run = dry_run
        self.sent = 0

    def send(self, spans: list[dict]) -> None:
        if not spans:
            return
        body = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    attr("service.name", "claude-code"),
                    attr("service.version", "replayer/1"),
                ]},
                "scopeSpans": [{
                    "scope": {"name": "ai-mastermind.replayer"},
                    "spans": spans,
                }],
            }]
        }
        if self.dry_run:
            self.sent += len(spans)
            return
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.endpoint}/v1/traces", data=data, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Basic {self.auth}"},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    if r.status < 300:
                        self.sent += len(spans)
                        return
                    raise RuntimeError(f"HTTP {r.status}")
            except urllib.error.HTTPError as e:
                # 4xx other than 429 will never succeed on retry — surface it loudly.
                if e.code != 429 and 400 <= e.code < 500:
                    raise RuntimeError(f"HTTP {e.code}: {e.read()[:400].decode(errors='replace')}") from e
                time.sleep(2 ** attempt)
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)


# --- main ---------------------------------------------------------------------
def build_spans(path: Path, root: Path, ledger: Ledger, machine: str,
                since: str | None, until: str | None, with_content: bool,
                account: str | None = None):
    kind, wf = classify(path, root)
    by_msg: dict[str, dict] = {}
    meta = {}
    errors = []

    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        if not u:
            continue
        ts = d.get("timestamp")
        mid = m.get("id")
        if not ts or not mid:
            continue
        if since and ts < since:
            continue
        if until and ts >= until:
            continue
        model = m.get("model")
        if model == "<synthetic>":
            errors.append(d)
            continue
        tot = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
               + u.get("cache_read_input_tokens", 0) + u.get("cache_read_input_tokens", 0))
        prev = by_msg.get(mid)
        if prev is None or tot >= prev["_tot"]:
            by_msg[mid] = {"_tot": tot, "d": d, "u": u, "m": m,
                           "_first": (prev or {}).get("_first", ts), "_last": ts}
        else:
            prev["_last"] = max(prev["_last"], ts)
            prev["_first"] = min(prev["_first"], ts)
        meta.setdefault("sessionId", d.get("sessionId"))
        meta["agentName"] = d.get("agentName") or meta.get("agentName")
        meta["agentId"] = d.get("agentId") or meta.get("agentId")

    if not by_msg:
        return [], 0, len(errors)

    sid = meta.get("sessionId")
    if not sid:
        return [], 0, len(errors)
    tid = trace_id(sid)
    root_span = span_id(f"session:{sid}")

    spans = []
    # Intermediate span so subagents nest under the session instead of landing as flat
    # siblings (one trace can otherwise absorb 466 files / ~14k spans).
    parent_for_gen = root_span
    if kind in ("subagent", "workflow-agent"):
        agent_key = meta.get("agentId") or path.stem
        if kind == "workflow-agent" and wf:
            wf_span = span_id(f"wf:{sid}:{wf}")
            parent_for_gen = span_id(f"agent:{sid}:{agent_key}")
        else:
            parent_for_gen = span_id(f"agent:{sid}:{agent_key}")

    recs = sorted(by_msg.values(), key=lambda r: r["d"]["timestamp"])
    for r in recs:
        d, u, m = r["d"], r["u"], r["m"]
        model = m.get("model")
        ts = d["timestamp"]
        acct = account or ledger.at(ts)
        # No transcript record carries a duration field (checked: 0 of 56,482), so the
        # only real timing signal is that the N content-block records of one response are
        # written as the stream arrives. first->last across them approximates the
        # generation window (p50 2.8s, p90 13.1s across the corpus).
        # A single-block response gets a ZERO-WIDTH span rather than an invented 1ms:
        # a fabricated duration reads as a measurement and would poison latency analysis.
        end = to_nano(r["_last"])
        start = to_nano(r["_first"])
        if start > end:
            start = end

        tags = ["claude-code", machine, acct, kind]
        repo = repo_from_cwd(d.get("cwd"))
        if repo:
            tags.append(f"repo:{repo}")

        a = [
            attr("langfuse.observation.type", "generation"),
            attr("langfuse.observation.model.name", model),
            attr("gen_ai.request.model", model),
            attr("gen_ai.system", "anthropic"),
            attr("langfuse.observation.usage_details", json.dumps(usage_details(u))),
            attr("langfuse.observation.cost_details", json.dumps(cost_details(model, u))),
            # Stamped on EVERY span, not just the root: Langfuse needs these per span for
            # reliable filtering, and one trace can span weeks and two accounts.
            attr("langfuse.user.id", acct),
            attr("langfuse.session.id", sid),
            attr("langfuse.trace.tags", tags),
            attr("price_table_version", PRICE_TABLE_VERSION),
            attr("priced_at", datetime.now(timezone.utc).isoformat()),
            attr("cc.cwd", d.get("cwd") or ""),
            attr("cc.repo", repo or ""),
            attr("cc.version", d.get("version") or ""),
            attr("cc.effort", d.get("effort") or ""),
            attr("cc.is_sidechain", bool(d.get("isSidechain"))),
            attr("cc.kind", kind),
            attr("cc.request_id", d.get("requestId") or ""),
        ]
        for k, src in (("cc.agent_name", "agentName"), ("cc.team_name", "teamName"),
                       ("cc.skill", "attributionSkill"), ("cc.plugin", "attributionPlugin"),
                       ("cc.mcp_server", "attributionMcpServer")):
            if d.get(src):
                a.append(attr(k, d[src]))
        if wf:
            a.append(attr("cc.workflow_id", wf))
        # gitBranch is captured but useless as a tag: 81.9% of spans read "HEAD".
        # Kept as an attribute, never promoted to a tag.
        if d.get("gitBranch"):
            a.append(attr("cc.git_branch", d["gitBranch"]))

        if with_content:
            c = m.get("content")
            if isinstance(c, list):
                txt = "\n".join(b.get("text", "") for b in c
                                if isinstance(b, dict) and b.get("type") == "text")
                if txt:
                    a.append(attr("langfuse.observation.output", txt[:8000]))

        spans.append({
            "traceId": tid,
            "spanId": span_id(f"gen:{m['id']}"),
            "parentSpanId": parent_for_gen,
            "name": f"llm {model}",
            "kind": 3,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": a,
            "status": {"code": 1},
        })

    # Structural spans, emitted last so their children already exist.
    first, last = recs[0]["d"]["timestamp"], recs[-1]["d"]["timestamp"]
    acct = account or ledger.at(first)
    common = [attr("langfuse.user.id", acct), attr("langfuse.session.id", sid),
              attr("langfuse.trace.tags", ["claude-code", machine, acct])]
    if kind in ("subagent", "workflow-agent"):
        spans.append({
            "traceId": tid, "spanId": parent_for_gen,
            "parentSpanId": span_id(f"wf:{sid}:{wf}") if wf else root_span,
            "name": f"subagent {meta.get('agentName') or path.stem[:24]}",
            "kind": 1, "startTimeUnixNano": str(to_nano(first)),
            "endTimeUnixNano": str(to_nano(last)),
            "attributes": common + [attr("langfuse.observation.type", "span")],
        })
        if wf:
            spans.append({
                "traceId": tid, "spanId": span_id(f"wf:{sid}:{wf}"),
                "parentSpanId": root_span, "name": f"workflow {wf}", "kind": 1,
                "startTimeUnixNano": str(to_nano(first)), "endTimeUnixNano": str(to_nano(last)),
                "attributes": common + [attr("langfuse.observation.type", "span")],
            })
    else:
        spans.append({
            "traceId": tid, "spanId": root_span, "name": f"session {sid[:8]}",
            "kind": 1, "startTimeUnixNano": str(to_nano(first)),
            "endTimeUnixNano": str(to_nano(last)),
            "attributes": common + [attr("langfuse.observation.type", "span")],
        })

    return spans, len(by_msg), len(errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--endpoint", default=os.environ.get("LANGFUSE_OTEL_ENDPOINT",
                                                         "http://localhost:3001/api/public/otel"))
    ap.add_argument("--public-key", default=os.environ.get("LANGFUSE_PUBLIC_KEY", ""))
    ap.add_argument("--secret-key", default=os.environ.get("LANGFUSE_SECRET_KEY", ""))
    ap.add_argument("--ledger", default=os.path.expanduser("~/.ai-mastermind/account-ledger.jsonl"))
    ap.add_argument("--checkpoint", default=os.path.expanduser("~/.ai-mastermind/checkpoint.json"))
    ap.add_argument("--machine", default=os.uname().nodename.split(".")[0])
    ap.add_argument("--account", help=(
        "Explicit account label for this root. REQUIRED when a machine runs several "
        "config dirs in parallel: the timestamp ledger cannot tell them apart, because "
        "the boundary is WHICH DIRECTORY the transcript was written into, not when."))
    ap.add_argument("--since"), ap.add_argument("--until")
    ap.add_argument("--with-content", action="store_true",
                    help="include assistant text. OFF by default: work-account content.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--limit-files", type=int, default=0)
    args = ap.parse_args()

    if not args.dry_run and not (args.public_key and args.secret_key):
        sys.exit("need --public-key/--secret-key (or LANGFUSE_PUBLIC_KEY/SECRET_KEY)")

    root = Path(args.root)
    ledger = Ledger(Path(args.ledger))
    exp = Exporter(args.endpoint, args.public_key, args.secret_key, args.dry_run)

    ck_path = Path(args.checkpoint)
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = json.loads(ck_path.read_text()) if ck_path.exists() else {}

    files = list(iter_transcripts(root))
    if args.limit_files:
        files = files[:args.limit_files]

    total_spans = total_gen = total_err = 0
    unknown: set[str] = set()
    buf: list[dict] = []

    for i, p in enumerate(files, 1):
        key = str(p)
        st = p.stat()
        sig = f"{st.st_ino}:{st.st_size}:{int(st.st_mtime)}"
        if ck.get(key) == sig and not args.dry_run:
            continue
        try:
            spans, ngen, nerr = build_spans(p, root, ledger, args.machine,
                                            args.since, args.until, args.with_content,
                                            args.account)
        except UnknownModel as e:
            unknown.add(str(e))
            continue
        total_gen += ngen
        total_err += nerr
        buf.extend(spans)
        while len(buf) >= args.batch:
            exp.send(buf[:args.batch])
            buf = buf[args.batch:]
        total_spans += len(spans)
        ck[key] = sig
        if i % 200 == 0:
            print(f"  {i}/{len(files)} ficheros · {total_gen:,} generaciones · {exp.sent:,} spans enviados",
                  flush=True)

    exp.send(buf)
    if not args.dry_run:
        ck_path.write_text(json.dumps(ck))

    print(f"\nficheros      : {len(files):,}")
    print(f"generaciones  : {total_gen:,}   (deduplicadas por message.id)")
    print(f"spans enviados: {exp.sent:,}    (incluye spans estructurales)")
    print(f"<synthetic>   : {total_err:,}   (fallos de API, no facturables, omitidos)")
    if unknown:
        print(f"MODELOS DESCONOCIDOS (no tarifados, NO enviados): {sorted(unknown)}")
        sys.exit(2)


if __name__ == "__main__":
    main()
