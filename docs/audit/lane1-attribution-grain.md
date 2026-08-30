# Lane 1 — Attribution grain audit of docs/PLAN.md

Corpus measured 2026-08-30 on `~/.claude/projects`, recursive glob, 1,644 files /
831,785 records / 172,989 generation-bearing records. Every number below is measured.

## Findings

### F1 — A "generation span per assistant message" is 2.21x the number of API calls
`§3.2: "one **generation span per assistant message** that has `message.usage`"` ·
`§4: "generation spans: ~172,450"`

- Assumed grain: one record with `message.usage` = one model call.
- Actual grain: one record = one **content block**. One API response is written as N
  records that share a single `message.id` and `requestId`.
- **Measured: 172,989 records with `message.usage`, but only 78,222 distinct
  `(message.id, requestId)` -> factor 2.21x.** 52,234 keys hold >1 record;
  52,176/52,178 multi-record `requestId`s share exactly one `message.id`.
  - 44,223 keys: the usage tuple is byte-identical across all records.
  - 7,953 keys: usage accumulates — **7,953/7,953 monotonically non-decreasing in file
    order, zero exceptions** — so the last record is complete and earlier ones are
    partial snapshots.
  - Example `req_011Cdr9GjMQbSaoVAaKK` / `msg_011Cdr9GkQA7gVPF`: 6 records
    (`thinking`, `text`, `tool_use` x4), each carrying usage `(2, 1529, 48584, 1745)`.
- Failure: span count inflated 2.21x, and token sums inflated per class —
  **input 3.75x, output 3.13x, cache-write 2.52x, cache-read 2.04x**.
  Applying the plan's own price rule (cache read 0.1x, write-5m 1.25x, write-1h 2x) to
  deduped tokens gives cost factor **0.447**:
  **the §1.2 headline `≈ 63,400` becomes `≈ 28,350`** (range $28.1k–$28.5k across
  first/last/max dedup and output:input price ratio 3–5).
  **"30.6 cache reads per write" becomes 37.9.**
  The per-model "Calls" column is a record count: opus-5 102,969 records = **49,800 calls**.
- Fix grain: key on `message.id` (null in 0 of 172,989) and take the last/max usage.

### F2 — `traceId = hash(sessionId)` + `spanId = hash(message.uuid)` emits no parent link
`§3.2` gives exactly two IDs and no `parentSpanId`, while `§1.2` promises
"the replayer can reconstruct **parent session -> subagent** as nested spans in Langfuse
rather than a flat list."

- **Measured: all 1,335 subagent transcripts carry the parent's `sessionId` verbatim
  (0 mismatches against the parent directory uuid).** One trace therefore absorbs up to
  **466 files / 465 distinct `agentId`s / 13,957 generation records**.
- With no parent linkage in the ID scheme those spans are flat siblings — the exact flat
  list the plan says it avoids. The data to fix it is present: `parentUuid` on
  172,865 of 172,871 assistant records, plus `agentId` (55,865 records).

### F3 — Trace fan-in is unbudgeted
Median **95** spans/trace, p90 **776**, max **13,957**.
**24 traces (8%) hold 124,793 spans = 72.2% of the corpus.**
`§4` sizes the backfill only by total spans (~172.5k), never by spans-per-trace, while
simultaneously memory-capping ClickHouse (`§Phase 1`).

### F4 — "One trace per `sessionId`" spans weeks, not a sitting
**35/300 sessions (11.7%) span >24h and hold 73.7% of all generation records;
11 span >7 days (34.9%); longest 43.3 days** (2026-07-06 -> 2026-08-19, 9,371 records).
Finding E's fix — "the trace keeps its `sessionId`, and its spans carry two different
accounts" — asserts per-span account inside a trace-scoped identity field. A 43-day trace
makes a mid-trace `/login` near-certain rather than exceptional.

### F5 — `cwd` is per-message; the plan holds it per-trace
`§3.1 metadata: cwd` · `§1.2 dimension table: "Repo / worktree | cwd | 916"`

**Measured on main-thread files only** (subagents excluded, so this is not a subagent
artifact): **152/300 sessions (50.7%) contain >1 distinct `cwd`, covering 123,813 of
141,921 main-thread generation records (87.2%); max 114 distinct `cwd` in one session.**
A trace-level `cwd` picks 1 of 114, and the "repo/worktree" filter mislabels 87% of spans.

### F6 — `gitBranch` is per-message but lives inside `langfuse.trace.tags`
`§3.1: langfuse.trace.tags = ["claude-code","mac-mini","personal","<git-branch>"]`

**Measured: 7/300 sessions (2.3%) carry 2 distinct branches (2,045 main-thread records)** —
e.g. `main` <-> `HEAD`, `docs/revise-first-delivery` <-> `HEAD` (detached HEAD during
rebase/worktree ops). Small, but it puts a per-message value in a trace-scoped array while
`§Phase 1` simultaneously requires tags "on **every** span" — two grains for one value.

### F7 — `version` drifts inside the largest sessions
`§3.1 metadata: version` · `§1.2: "CC version | version | for before/after regressions"`

**Measured main-thread: 13/300 sessions (4.3%) carry >1 version, but those hold 45,545
records = 32.1% of main-thread generations; up to 4 distinct versions in one session.**
A before/after version regression keyed per session mixes up to 4 versions across a third
of the data.

### F8 — Two disagreeing session identifiers; the plan names only one
Records carry both `sessionId` (825,764) and `session_id` (574,546).
**They disagree on 17,951 generation records (10.4%)**, across 8 `sessionId`s / 8 files;
4 `sessionId`s map to 2 distinct `session_id`s.
Measured segment shapes:
- `d28ef2b8.jsonl`: records 7–231 carry `session_id=13ae6714`, then 242–247 native.
- `2e34e200.jsonl`: 18,188 records of `session_id=48a924e2`, then 1,637 native.
- `171d20a1.jsonl`: 100% `session_id=ab592db9` — one contiguous segment, zero native.

`§3.2` keys `traceId` and `langfuse.session.id` on `sessionId` without noting the other
field exists. Which is canonical is not resolved here; either way 10.4% of generations
are attributed to a session that is not the one recorded as producing them.

### F9 — `spanId = hash(message.uuid)` is not unique
`§3.2: "Deterministic IDs ... The intent is that a re-run cannot duplicate."`

1,381 uuids appear twice corpus-wide (1,356 across different files). Restricted to
generation records: **348 uuids appear in exactly 2 records, all 348 straddling two
different `sessionId`s**, byte-identical — e.g. uuid `b1496825…` in both `13ae6714` and
`d28ef2b8` at `2026-08-12T15:13:02.339Z`, model `claude-opus-5`, `out=349`.
Under the plan's scheme the same `spanId` is emitted into two different `traceId`s.
0.44% of the 78,222 real calls. **Already absorbed by the F1 `(message.id, requestId)`
dedup — do not stack the two corrections.**
Mechanism visible in-data: 6 `fork-context-ref` records carrying `parentSessionId`,
`parentLastUuid`, `contextLength: 69`.

### F10 — The machine tag has no source in the data, and one Collector stamps two machines
**No host-like field exists in any of 832,423 records** (probed `hostname`, `host`,
`machine`, `deviceId`, `machineId`, `platform`, `os`: zero hits). The `§3` diagram routes
the laptop's replayer over Tailscale into the mini's **single** Collector whose stated job
includes "stamp user.id + tags" — a per-deployment stamp applied to data from two hosts.
`mac-mini` in the `§3.1` tag array is therefore correct only if the stamping happens in
each replayer, which the diagram contradicts.

### F11 — `langfuse.user.id` is specified at three different grains in one document
- `§3.1` table: source = "config dir -> `.claude.json`" (per-replayer)
- `§3` diagram: the Collector stamps it (per-deployment)
- `Finding E`: per-message ledger time-range join — "not per session, not per directory"

The Finding E fix landed in prose; the identity table and the architecture diagram still
carry the pre-fix grain. Same defect shape as the three already found.

### F12 — The backfill acceptance number is fixed; the corpus is append-only and live
`§4: "confirm the backfilled span count in Langfuse equals the number above"` and
`§4: "a ~0.1% instrument difference from unparseable lines"`.

**Measured: 0 unparseable lines in 831,785 records.** The gap is not parse failure —
it is corpus growth. During this single audit the file count moved **1,640 -> 1,642 ->
1,644** and generation records **172,737 -> 172,989**. An equality check against a frozen
number cannot pass while the incremental replayer is also running.

**Nit:** the `**/*.jsonl` glob also sweeps 9 `journal.jsonl` workflow files that are not
transcripts (keys `type`/`key`/`agentId`/`result`; no `sessionId`, `timestamp`, `message`).

## Evidence against my own lane

- **`isSidechain` — plan verified.** 142,017 main / 30,816 sidechain, **zero crossover**
  with the `subagents/` path, exactly as `§1.2` claims (plan: 141,791/30,816; the delta is
  corpus growth).
- **Model-per-message — plan verified.** Main-thread only: **41/300 sessions** carry >1
  model. The plan says 41/292. Correct, and correctly scoped to the main thread.
- **Compaction does not break the byte-offset checkpoint.** 70 `compact_boundary` records
  in 25 files; the first boundary sits at line **3,271 (min) / 5,550 (median) / 9,250
  (max)** — always mid-file, history retained, `sessionId` unchanged. `§3.2`'s per-file
  byte-offset checkpoint is safe against compaction.
- **`gitBranch` is never missing:** present on 683,516 records, 0 absent on
  assistant/user records.
- **The recursion warning is right and if anything understated.** 299 top-level vs 1,644
  total. But `§1.2` says subagents are "nested **one level down**" — measured:
  217 files at `<project>/<sid>/subagents/`, **1,127 at
  `<project>/<sid>/subagents/workflows/wf_*/`**. `**` handles it; the prose does not.
- **`effort` / `service_tier` / `speed` are NOT grain defects.** Per-message fields, but
  cardinality 1 across the whole corpus (`effort=xhigh` 149,975 records;
  `service_tier=standard`; `speed=standard`), zero within-session variation.
  *Weak separate note:* the `§1.2` dimension table prints record counts (`Effort | 149,569
  records`) where distinct-value counts belong, so `effort` reads as a filter dimension
  while having zero discriminating power. That is a property of current settings, not of
  the format.

## Critical unknown

How Langfuse resolves trace-scoped `langfuse.user.id` and `langfuse.trace.tags` when spans
under one `traceId` disagree — root span, first write, last write, or merge. This single
fact decides whether F4, F6 and F11 are representable at all, or whether a 43-day trace
silently reports one account and one branch. Not answerable from local data.

## Discriminating probe

Extend the `§Phase 1` acceptance span — which already has to be posted — to **two spans
sharing one `traceId` but carrying conflicting `langfuse.user.id` and different
`langfuse.trace.tags` arrays**, then read the trace back and record which value survives.
Same POST the plan already requires; one extra span answers the whole question.
