# Lane 3 — Measurement & verification audit of docs/PLAN.md

Method: independent re-derivation over `/Users/pablo/.claude/projects` (recursive
`os.walk`), 1,644 files / 1.5 GB, plus a live console-exporter re-probe.
Scripts: `/tmp/lane3/{scan,dup,full,oracle,pairs}.py`.

## Observation

The plan reports ~172.45k generation spans, ≈$63.4k API-equivalent cost, and
attributes a 172,303 vs 172,454 two-pass discrepancy to "unparseable lines".

---

## Findings

### F1 — CRITICAL. Token totals and the ≈$63.4k cost are ~2.2× overstated: `usage` is replicated per content block

**Plan's rule (§3.2):** "one generation span per assistant message that has `message.usage`".

**Measured:** a single API response (one `message.id`) is written as N JSONL lines —
one per content block (`thinking` / `text` / `tool_use`) — and **every line repeats a
byte-identical copy of the same `usage` object** (`distinct usage payloads among them: 1`
in every sampled case).

| | records | distinct `message.id` | fan-out |
|---|---:|---:|---:|
| whole corpus | 172,811 | 78,154 | **2.21×** |
| claude-opus-5 | 102,947 | 49,792 | 2.07× |
| claude-fable-5 | 36,025 | 13,830 | 2.60× |

**Cost re-derived:** $63,490 raw (reproduces the plan's ≈$63,400) vs **$27,311 deduped
by `message.id`**.

**Independent oracle confirms it.** The corpus contains 6 `cost-state` records carrying
Claude Code's *own* `totalCostUSD` and per-model `modelUsage`. Against those 11 model-rows:

- summing **per record** (the plan's rule): **0 / 11** exact matches
- summing **deduped by `message.id`**: **3 / 11** output-token and **4 / 11**
  cache-read exact matches (e.g. `c9c4929e`/fable-5 cache-read 4,357,314 = 4,357,314)

The plan's rule never agrees with Claude Code's own accounting; the deduped rule agrees
exactly wherever the session windows line up. Residual mismatches are explained by
`sessionId` spanning several `cost-state` windows and by haiku calls that appear in
`cost-state` but are absent from the transcripts.

**Also:** the plan's "Calls" column is a record count, not a call count (opus-5: 102,599
≈ raw 102,947, not deduped 49,792). ~$27.3k is an **upper bound** — retried/streamed
responses would push it lower.

**Verdict: the plan's conclusion does not survive.** The arithmetic is sound — the table
reproduces exactly from raw sums under a self-consistent price table — but the inputs
double-count. Conclusion inherited by §3.2 and by the Phase 2 acceptance.

### F1b — the dedupe rule is ASYMMETRIC (do not over-correct)

- **Per-response** quantities (`usage`, `model`, `stop_reason`) → dedupe by `message.id`. Raw is 2.21× inflated.
- **Per-block** quantities (`tool_use` names) → do **not** dedupe. Each record holds a
  *different* content block. The plan's tool counts (Bash 56,110 etc.) are **correct**;
  deduping them would undercount Bash by 3.8× (14,842).

Same corpus, correct for one aggregation and 2.2× wrong for the other. That asymmetry is
why the defect survived.

### F2 — The 172,303 vs 172,454 discrepancy: the plan's stated cause is false

**Plan:** "a ~0.1% instrument difference from unparseable lines."

**Measured across all 1.5 GB:** **zero** unparseable lines, **zero** blank lines, **zero**
non-dict lines, and **zero** assistant records lacking `message.usage`
(`assistant_records == assistant_with_usage == 172,757`). There is nothing for an
instrument to disagree about.

**Actual cause: the corpus is live and monotonically growing.** Successive counts:

```
172,303  (plan, pass 1)      141,791 + 30,816 = 172,607  (plan's own §1.2 sidechain row)
172,454  (plan, pass 2)      172,757 → 172,811 → 172,827  (mine, ~5 min apart)
```

8 files were modified in the 10 minutes spanning my passes. Note the plan is **internally
inconsistent**: its sidechain row sums to 172,607, matching neither of its own stated
totals — three mutually inconsistent numbers, each ~150 apart, exactly the per-pass drift
I measured. `1,640 → 1,643` files and `292 → 300` sessions move the same way.

**Verdict: does not survive.** No reconciliation is possible at import time as the plan
proposes; the target must be a **frozen, date-bounded slice**.

### F3 — `spanId = hash(message.uuid)` collides on 348 spans (session forks)

348 message uuids appear in **two** files each, across exactly 2 file-pairs, all
main-thread, same project. In all 348: **same `message.id`, different `sessionId`.**
This is the fork/resume shape (`type: fork-context-ref` with `parentSessionId` is present
in the corpus).

Consequence for §3.2: `traceId = hash(sessionId)` → two *different* traces;
`spanId = hash(message.uuid)` → the *same* span id. So the Phase 1 dedupe gate is
**two-sided**, which the plan does not recognise: if Langfuse dedupes globally on span id,
348 legitimate spans are silently dropped; if it does not, the same content is ingested
into two traces. The plan treats "dedupes" as unambiguously the desired outcome.

### F4 — Finding C's path shape is wrong; nesting is three levels, not one

**Plan:** "nested one level down: `<project>/<sessionId>/subagents/agent-*.jsonl`".

**Measured** (1,644 files):

| shape | count |
|---|---:|
| `<project>/<uuid>.jsonl` (main) | 300 |
| `<project>/<uuid>/subagents/agent-*.jsonl` | 217 |
| `<project>/<uuid>/subagents/workflows/wf_*/agent-*.jsonl` | **1,118** |
| `.../workflows/wf_*/journal.jsonl` (**not a transcript**) | 9 |

The literal glob the plan states catches 217 of 1,344 subagent files — **16%**. A second,
subtler instance of the very defect the plan flagged. §3.2's `**/*.jsonl` watcher is
recursive so the *design* survives, but (a) the real hierarchy is
parent → **workflow** → agent, so 83% of subagent spans would attach at the wrong level
under the plan's stated two-level reconstruction, and (b) `journal.jsonl` is matched by
`**/*.jsonl` and is not a transcript.

### F5 — `attributionMcpServer` does not reproduce, and it is not growth

Plan 763; measured **1,488** (+95%). All 1,488 predate today, so growth is excluded.

Control in the same table cell: `attributionSkill` measured **14,659** before today —
**exactly** the plan's number — and 14,696 today. My instrument agrees with the plan's on
the sibling metric, so the MCP disagreement is a real instrument difference in that one
cell, cause unknown.

### F6 — Finding A: one alternative excluded, two not

**Excluded by direct evidence:** signal-specific env precedence. `remote-settings.json`
sets only the generic `OTEL_EXPORTER_OTLP_ENDPOINT` (plus `_HEADERS`, `_PROTOCOL`,
`OTEL_LOGS_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`) — there is
**no** `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` to out-rank the probe's variable.

**Not excluded — no graceful flush on headless exit.** `claude -p` may tear down without a
shutdown flush. The console exporter prints synchronously, so it would still show spans.
This explains *every* observation without any credential-stripping.

**Not excluded — the probe's flush variable may be inert.** `OTEL_TRACES_EXPORT_INTERVAL`
is not a standard OTel batch-span-processor variable (`OTEL_BSP_SCHEDULE_DELAY` is); if
unrecognised the default delay applies and the 8 s wait may not cover teardown.

**The positive control proves the listener works — not that Claude Code attempted an
outbound connection.** Those are different propositions. Finding A's "dead on arrival"
verdict is load-bearing for §2 ("transcript replay is the only mechanism"), and the two
surviving explanations have a completely different fix.

### F7 — Phase 0 acceptance can pass while broken (three ways)

> "a real `sudo reboot`, then Langfuse answers on `:3001` with no manual step and previous traces are still there"

1. **Auto-login confounds the whole test.** `autoLoginUser = pablo` means the check cannot
   distinguish "Docker autostarted at boot" from "Docker started because a user session
   began". The plan notes Docker Desktop fires on *login, not boot*, then calls auto-login
   "sufficient" — which is exactly what makes the acceptance blind to the difference.
   Discriminator: verify over ssh with no console login, or compare daemon start time to boot time.
2. **"previous traces are still there" is vacuous with zero pre-existing traces.** Needs a
   trace seeded with a *known id* before the reboot and queried by that id after.
3. **":3001 answers" passes with ClickHouse dead.** `langfuse-web` serves a login page with
   the whole storage tier down — the Finding B empty-render shape one layer up. The check
   must traverse an authenticated API path that actually reads ClickHouse.

### F8 — Phase 2 acceptance is self-consistent rather than correct

> "Cross-check total tokens for one session against the raw JSONL — the aggregate must **match**"

If the replayer sums `usage` per record, a JSONL cross-check computed by the same rule
matches **exactly — at 2.2× wrong**. This is the strongest-looking gate in the plan and it
is precisely blind to F1. Two more holes:

- **"token counts across all four classes" does not cover the 1h/5m cache split** — the
  plan's own stated advantage over OTel (§1.2, "priced 2× vs 1.25×"). My re-probe confirms
  the OTel span carries only an aggregate `cache_creation_tokens: 16962`, so this is the
  differentiator, and the acceptance does not test it.
- **"confirm the backfilled span count equals the number above" is unsatisfiable as
  written** — moving corpus (F2) plus 348 uuid collisions (F3) mean a count-equality gate
  can never go green even when the import is correct.

Fix: cross-check against the **`cost-state` records** (independent oracle, Claude Code's
own `totalCostUSD` + `modelUsage`), over a **frozen date-bounded slice**, and assert the
1h/5m split explicitly.

---

## Claims that REPRODUCED correctly

| Claim | Plan | Measured | Note |
|---|---|---|---|
| One-level glob undercount | 296 vs 1,640 (~82%) | 299 vs 1,643 (**81.8%**) | headline defect is real |
| `isSidechain` ⟂ `subagents/` path, zero crossover | 141,791 / 30,816 | **141,941 / 30,816, zero crossover** | sidechain count exact |
| Corpus size | 1.5 GB | 1.5 GB | ✓ |
| Date range | 2026-07-06 → 2026-08-30 | identical | ✓ |
| `gitBranch` distinct | 64 | **64** | exact |
| `<synthetic>` as 6th model value | present | 427 records | ✓ (all-zero usage → 427 zero-token spans) |
| Tool-call counts | Bash 56,110 · Read 5,867 · Edit 5,811 | 56,230 · 5,872 · 5,818 | growth only; **these are correct — see F1b** |
| `attributionSkill` / `attributionPlugin` | 14,659 / 11,054 | **14,659** / 11,054 before today | exact |
| `effort` | 149,569 | 149,926 (growth) | ✓ |
| Cache reads per write | 30.6 | **30.65** | ✓ (37.88 deduped) |
| `sessionId` distinct, `cwd` | 292 / 916 | 300 / 923 | growth only (F2) |
| Cost-table arithmetic | ≈$63,400 | $63,490 from raw sums | internally consistent — **which is how F1 is proven** |
| Finding B attribute names | `model`, `gen_ai.request.model`, `gen_ai.system`, `user.id`, `session.id`, bare `input_tokens`/`output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `ttft_ms`, `duration_ms`, `stop_reason`, `success`, `attempt` | **all present, verbatim; no `gen_ai.usage.*` anywhere** | ✓ |
| Spans carry `user.email`, `organization.id`, `user.account_uuid` | yes | confirmed (+ `user.account_id`) | ✓ |
| Transcripts richer than OTel (1h/5m split) | yes | confirmed — span has only aggregate | ✓ strengthens Finding C |
| Subagent transcripts carry parent `sessionId` | implied | 53,785 True / **0 False** | ✓ supports `traceId = hash(sessionId)` |

**Finding B is accurate but incomplete.** Undocumented attributes present: `span.type`,
`llm_request.context`, `request_id`, `gen_ai.response.id`, `client_request_id`,
`terminal.type`, `user.account_id`, `speed`, `gen_ai.response.finish_reasons`.
Structurally: intra-process parenting is correct (`claude_code.interaction` →
`claude_code.llm_request` via `parentSpanContext`, same traceId), but the cross-agent
relationship is expressed as a **span link** (`link.type: "parent_of"`) pointing at a
**different traceId**. Langfuse builds its observation tree from parent-span-id, not links
— so the Collector `transform` the plan proposes fixes the token attributes but not the
cross-agent tree.

---

## Critical unknown

**Whether a real LLM call is one span or N spans — i.e. whether the replayer's unit is the
`message.id` (78,154) or the JSONL record (172,811).** Everything downstream depends on it:
the backfill size, the ClickHouse capacity plan, the cost dashboard, and the Phase 2
acceptance. The plan picks the record, and no gate in it can detect the choice.

Secondary: whether Claude Code *attempts* an outbound OTLP connection at all under the
Finding A probe (F6).

## Discriminating probe

**Replay one session both ways and diff against its `cost-state` record.**
Pick a `sessionId` that has a `cost-state` entry, sum `usage` (a) per record and
(b) deduped by `message.id`, and compare `totalCostUSD` / `modelUsage`. One command,
already written (`/tmp/lane3/oracle.py`), decides F1, fixes the Phase 2 acceptance to use
an independent oracle, and yields the correct backfill target in one pass.

Second, cheapest-next: `lsof -nP -p <pid> -i` (or `tcpdump -i lo0 port 4318`) during the
Finding A probe — observe whether an outbound connection to `localhost:4318` is attempted,
rather than whether the listener received one.
