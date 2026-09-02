# ai-mastermind — Phase 1: Observability

Self-hosted Langfuse on the Mac mini as the single pane of glass for all AI coding
activity, across **two machines** and **two accounts**. Later phases add knowledge
graphs, evals, and optimisation on top of the same trace store.

Status: running.

> **Note on figures.** Absolute cost figures were removed before publishing. `$X` is the
> API-equivalent cost of this corpus; ratios and multipliers are kept because they are the
> transferable part. The employer is referred to generically: the finding is about how
> org-managed settings behave, not about any particular company.


---

## 0. DO THIS FIRST — the corpus is a 30-day cache and it is deleting itself

Discovered by an audit lane and independently confirmed. This outranks everything
below, including Phase 0.

```
sessions ever run here (.session-stats.json) : 729
with a transcript still on disk              : 261
WITHOUT a transcript                         : 468  (64%)

top-level transcripts with mtime > 30 days   : 0     (clean age cliff, no exceptions)
cleanupPeriodDays in all three settings files: absent -> 30-day default applies
.last-cleanup                                : 2026-08-30T08:13:35Z (ran today)
```

`claudeCodeFirstTokenDate` is 2026-02-11 and `numStartups` 726 — roughly **28 weeks** of
real usage, of which only the last **30 days** survive. Everything from 2026-05-08 to
2026-07-30 is **already permanently gone**, and one more day dies every day.

**Consequences for this plan:**

1. The "8 weeks / 2026-07-06 → 08-30" window is not a history. It is **survivorship** —
   what the sweeper had not yet eaten on the morning of 2026-08-30.
2. **Finding E's polarity was inverted.** It argued to set up attribution "before more
   history accumulates". The real race is **deletion**. Retention must be fixed before
   anything else is built, because every other phase gets cheaper with more history and
   this is the only clock that cannot be rewound.
3. The backfill cannot be sequenced behind Phase 0 and Phase 1.

**Action, in order:**

```bash
# 1. stop the bleeding — one key in ~/.claude/settings.json
"cleanupPeriodDays": 3650

# 2. freeze what exists, before anything else is built
rsync -a ~/.claude/projects/ ~/ai-mastermind-corpus-snapshot/
```

Neither step depends on Langfuse existing. Do them before Phase 0.

---

## 1. Ground truth (verified on 2026-08-30, not assumed)

Every claim here was probed on this machine. The commands are recorded so the next
session re-verifies instead of trusting.

### 1.1 The host

| Fact | Value |
|------|-------|
| Machine | Mac mini M4, macOS 26.2, arm64, 10 cores |
| RAM | 24 GB |
| Free disk | 307 GB |
| Docker | v29.4.3 + Compose v5.1.4 installed, **daemon not running** |
| Docker autostart | **absent** — no LaunchAgent for Docker (only Ollama, llama-server, whisper, xtts, tmux) |
| Tailscale | active. both machines on the same tailnet (`mac`, `pablos-macbook-pro`); addresses kept out of this repo |

`local-ai-stack` (private repo) already runs LiteLLM + Postgres + Redis + Open WebUI +
Piper on this host, but **is currently stopped and not in use**. Ports 3000/4000/5432/6379
are all free right now. Its conventions are worth copying (digest-pinned images,
named volumes, `expose:` instead of `ports:` for internal services, launchd for
native daemons).

### 1.2 Claude Code telemetry — what actually happens

Claude Code exports three OTel signals: metrics, logs/events, and **traces (beta,
opt-in via `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`)**.

**Finding A — the OTLP destination is locked by org-managed settings.**

`~/.claude/remote-settings.json` (pushed by the user's employer's org) sets
`CLAUDE_CODE_ENABLE_TELEMETRY=1`, an `OTEL_EXPORTER_OTLP_ENDPOINT` pointing at the
employer's developer-analytics platform, and an auth header. Per the docs, managed settings that carry
credentials **strip developer-set endpoints at startup** to prevent exfiltration.

Probed, and it reproduces:

```bash
# console exporter -> spans DO print (telemetry pipeline is alive)
CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
  OTEL_TRACES_EXPORTER=console claude -p "hi"

# otlp to a local listener -> ZERO hits, all 3 signals, 1s flush, 8s wait
CLAUDE_CODE_ENABLE_TELEMETRY=1 CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
  OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
  OTEL_TRACES_EXPORT_INTERVAL=1000 OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 claude -p "hi"
```

A positive control (`curl -X POST http://localhost:4318/v1/traces`) **did** log a hit.

**RESOLVED 2026-08-30 by two further probes.** The first write-up of this finding rested
on absence of hits, which only proved the *listener* worked. Both surviving alternatives
are now excluded, and the mechanism is more precise than "managed settings strip
endpoints":

**Probe 1 — `lsof` on the running process.** Claude Code opened 528+ outbound connections
during the run (so the instrument was demonstrably seeing its traffic) and **zero** to
`127.0.0.1:4318`. The only 4318 lines belong to the probe's own listener socket. It never
*attempted* the connection, which refutes the "no graceful flush on headless exit"
hypothesis — a flush failure still opens a socket.

**Probe 2 — the exporter override, observed directly.** `remote-settings.json` sets
exactly these: `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_LOGS_EXPORTER`,
`OTEL_METRICS_EXPORTER`, `OTEL_METRIC_EXPORT_INTERVAL`. Note what is **absent**:
`OTEL_TRACES_EXPORTER`.

That single gap explains every observation:

| My setting | Org sets it? | Result |
|---|---|---|
| `OTEL_TRACES_EXPORTER=console` | no | **worked** — spans printed |
| `OTEL_METRICS_EXPORTER=console` | yes | **overridden** — no metrics printed |
| `OTEL_EXPORTER_OTLP_ENDPOINT=localhost` | yes | **overridden** — no connection attempted |

> **The rule is not "managed settings strip telemetry config". It is: managed settings
> win on exactly the variables they define.** The traces *exporter* was mine to control;
> the *destination* never was. That is why a console exporter proves nothing about
> whether a custom endpoint is reachable — and it is why §2's conclusion holds.

Independent of the mechanism: today *all* Claude Code activity on this Mac — personal
side projects included — emits telemetry to the employer's endpoint. That part is not in
question.

**Finding B — the beta spans don't use the attribute names Langfuse reads.**

Real span captured from `claude_code.llm_request`:

```
model:                 "claude-haiku-4-5-20251001"
gen_ai.request.model:  "claude-haiku-4-5-20251001"   <- Langfuse reads this
gen_ai.system:         "anthropic"
user.id / session.id:  present                        <- Langfuse reads these
input_tokens:          10        <- NOT gen_ai.usage.input_tokens
output_tokens:         221       <- NOT gen_ai.usage.output_tokens
cache_read_tokens:     14629
cache_creation_tokens: 14801
ttft_ms, duration_ms, stop_reason, success, attempt
```

Langfuse maps usage from `gen_ai.usage.*` or `langfuse.observation.usage_details`.
Claude Code emits bare `input_tokens` / `output_tokens`. **Sent as-is, Langfuse would
render a timeline with no tokens and no cost** — the classic "present but unwired"
shape: a dashboard that looks fine because it has no data. A Collector `transform`
step renaming these is therefore mandatory, not cosmetic.

Also note the spans carry `user.email`, `organization.id` and `user.account_uuid`.

**Finding C — the local transcripts are richer than the telemetry, and unblocked.**

`~/.claude/projects/**/*.jsonl` (1.5 GB of existing history) carries per assistant
message:

```json
{"timestamp":"2026-08-08T17:08:06.336Z","model":"claude-opus-5",
 "sessionId":"fb0bbba6-…","cwd":"/Users/<user>/development/…","gitBranch":"…",
 "version":"…","userType":"…","agentName":"…","teamName":"…",
 "message":{"usage":{"input_tokens":2,"output_tokens":349,
   "cache_creation_input_tokens":18781,"cache_read_input_tokens":25113,
   "cache_creation":{"ephemeral_1h_input_tokens":18781,"ephemeral_5m_input_tokens":0},
   "service_tier":"standard","speed":"standard"}}}
```

This is **more** than the OTel spans give (it separates the 1h vs 5m cache tiers, which
are priced differently) and it is not subject to the managed-settings lock.

**Subagents are separate transcripts, nested up to TWO levels down:**

```
<project>/<sessionId>.jsonl                              300  main
<project>/<sessionId>/subagents/agent-*.jsonl            217  direct subagents
<project>/<sessionId>/subagents/workflows/wf_*/agent-*  1118  workflow agents
<project>/<sessionId>/subagents/.../journal.jsonl          9  NOT transcripts — skip
```

A one-level glob sees 299 files; the real corpus is **1,644**. Saying "one level down"
(as an earlier draft of this plan did) is itself an instance of the defect it flags: it
describes 217 of 1,344 subagent files — **16%**. The real hierarchy is
parent → **workflow** → agent, three levels, so a two-level model attaches 83% of
subagent spans at the wrong depth. `**` recursion handles the enumeration; the *tree
shape* still has to be modelled explicitly.

**All subagent transcripts carry the PARENT's `sessionId` verbatim** (0 mismatches).
That is convenient for `traceId = hash(sessionId)` — but §3.2 emits no `parentSpanId`,
so every subagent lands as a **flat sibling**. One trace can absorb 466 files / 13,957
spans. `parentUuid` is present on ~172,865 assistant records and goes unused: it is the
linkage the nesting needs.

**Dimensions the replay makes filterable** (measured across the corpus):

| Dimension | Field | Observed |
|-----------|-------|----------|
| Model | `message.model` — **per message, not per session** | 5 real models + `<synthetic>` |
| Main thread vs subagent | `isSidechain` (perfectly correlated with the `subagents/` path: 141,791 main / 30,816 sidechain, zero crossover) | separates *your* `/model` switches from what subagents picked |
| Session | `sessionId` | 292 |
| Repo / worktree | `cwd` | 916 |
| Git branch | `gitBranch` | 64 |
| Subagent | `agentName`, `agentId`, `teamName` | 30,816 records |
| Skill / plugin / MCP | `attributionSkill`, `attributionPlugin`, `attributionMcpServer` | 14,659 / 11,054 / 763 |
| Effort | `effort` | 149,569 records |
| Tool calls | `message.content[].tool_use.name` | Bash 56,110 · Read 5,867 · Edit 5,811 · … |
| CC version | `version` | for before/after regressions |
| Cache tier | `usage.cache_creation.ephemeral_1h/5m` | priced 2× vs 1.25× |

**⚠ A JSONL record is a CONTENT BLOCK, not an API call — do not sum usage across records.**
One API response is written as N records (thinking, text, tool_use ×N), **each repeating
the same `usage` object**. Measured: **173,094 usage-bearing records but only 78,265
distinct `message.id` → 2.21× inflation.** Deduplicate on `message.id` (present on
100% of records; `requestId` gives 78,120, marginally coarser) taking the max usage,
since usage accumulates across the blocks of one response.

The tell was visible in the very first probe of this plan — three byte-identical `usage`
objects in consecutive records — and was read straight past. Any future count over these
transcripts must state its unit before it states its number.

**API-equivalent cost, deduplicated**, with current list prices (cache read 0.1× input,
write 5m 1.25×, write 1h 2×):

| Model | API calls | Output tok | Cache read | API-equiv $ |
|-------|----------:|-----------:|-----------:|------------:|
| claude-opus-5 | 49,906 | 38,864,235 | 19,782,863,119 | — |
| claude-fable-5 | 13,830 | 14,386,190 | 4,709,677,002 | — |
| claude-opus-4-8 | 11,270 | 10,362,207 | 4,103,756,478 | 3,870 |
| claude-sonnet-5 | 2,782 | 2,392,517 | 580,483,207 | — |
| claude-haiku-4-5 | 54 | 16,749 | 2,504,712 | — |
| `<synthetic>` | 427 | — | — | not priced |
| **Total (8 weeks)** | **78,269** | | | **≈ X** |

Superseded figure: ≈2.2·$X over 172k "spans". Both the cost and the call count were
inflated ~2.2×. Real API calls: **78,269**, not 172,450.

Cache behaviour, recomputed: **37.9 reads per write** (not 30.6) — caching is working
even better than first reported, and cache reads still dominate at 0.1×. This table is
the dashboard Phase 2 reproduces in Langfuse, sliceable by every dimension above.

> Again: with a Max subscription this is **not a bill**. It is what the same work would
> have cost on the API — the number that makes the subscription's value legible, and the
> baseline against which model/effort routing changes are measured.

**Finding D — the transcripts do NOT identify the account, and the history is
permanently unattributable.** No email/org field anywhere in the JSONL; `userType` is
uniformly `external`. Account identity lives only in `~/.claude.json` →
`oauthAccount.emailAddress` / `organizationUuid` / `userID`, and that file holds **only
the account logged in right now** (currently the work account) — there is no history and no
per-session record. There is also just **one** config dir on this machine (`~/.claude`),
so every session ever run here wrote into the same `projects/` tree.

> **Consequence: the ≈$63.4k figure above is all accounts combined and cannot be split
> retroactively.** Backfilled traces must be tagged `account: unknown-pre-split`. Making
> up an attribution for them would be worse than admitting the gap, because every later
> per-account comparison would silently inherit the guess.

*A tempting signal, deliberately not used:* the org's remote settings list
`availableModels: [haiku, sonnet, opus]`, which excludes `claude-fable-5` — and fable-5
usage stops on 2026-08-26 while opus-5 continues to 08-30. It is tempting to read that as
"fable-5 sessions were personal". **It is not evidence.** `remote-settings.json`'s mtime
(08-28) is a last-write, not a first-appearance, and "stopped using a model" has many
ordinary explanations. If we ever want this answered, the test is whether the org policy
actually blocks the model, not whether usage happens to correlate.

**Finding E — reading the account at replay time is not safe.** A `/login` switch
mid-session breaks it, and worse than it first appears:

- The transcripts record **no auth event at all** (`type` values are `attachment`,
  `assistant`, `user`, `last-prompt`, `mode`, `permission-mode`, `pr-link`, `ai-title`,
  `queue-operation`, `system`, `atis-latch`, `agent-setting`, …; `system.subtype` covers
  `stop_hook_summary`, `turn_duration`, `compact_boundary`, `local_command`,
  `model_refusal_fallback` — nothing about authentication).
- `~/.claude.json` is **overwritten in place**, with no history and no backup. It holds
  `profileFetchedAt` but no record of when the account changed.

So a replayer that reads `.claude.json` when it runs would attribute by whatever account
happens to be logged in **at that moment** — misattributing not just the pre-switch half
of one session, but retroactively every transcript in that directory not yet replayed.
A single `/login` before a backfill would silently relabel weeks of history.

**Fix: pin the account at capture time, in an append-only ledger.**

1. `CLAUDE_CONFIG_DIR` per account gives the default and makes a switch anomalous rather
   than routine.
2. A watcher on each `<config-dir>/.claude.json` appends `{observed_at, accountUuid,
   emailAddress, organizationUuid}` to a local ledger whenever `accountUuid` changes.
3. The replayer attributes **per message timestamp** via a time-range join against the
   ledger — not per session, not per directory.

This makes a mid-session switch representable instead of merely survivable: the trace
keeps its `sessionId`, and its spans carry two different accounts, which is what actually
happened. Cost per account then sums **spans**, never traces — the same per-message rule
that already handles mid-session model switches.

The ledger cannot reconstruct the past: it is correct only from the moment it starts, and
everything before stays `account: unknown-pre-split`. That is the argument for setting it
up before more history accumulates.

> Residual gap to accept explicitly: if an account is switched **and** switched back
> between two ledger polls, the window is invisible. Bound it by watching the file with
> fsevents/`WatchPaths` rather than polling on an interval.

### 1.3 Langfuse

- **v4 self-hosted = 6 containers**: `langfuse-web`, `langfuse-worker`, `clickhouse`,
  `minio`, `redis`, `postgres`. Official guidance: ≥4 cores / 16 GiB for a production
  instance.
- **OTLP ingestion accepts traces only** — no metrics, no logs. Endpoint
  `/api/public/otel/v1/traces`, Basic auth `base64(pk-lf-…:sk-lf-…)`,
  `http/json` or `http/protobuf`. **gRPC is not supported.**
- **The legacy Ingestion API is deprecated**, sunset on Cloud 2026-11-16 and already
  unavailable on self-hosted v4 in default mode. Langfuse's own guidance is to use the
  OTel endpoint. *Anything we build must emit OTLP spans, not Ingestion events.*
- Attribute mapping: `langfuse.observation.model.name`, `langfuse.observation.usage_details`
  (JSON string), `langfuse.observation.cost_details` (JSON string), `langfuse.user.id`,
  `langfuse.session.id`, `langfuse.trace.tags` (string array).

> Because `claude_code.cost.usage` is a **metric** and Langfuse takes traces only, the
> cost number Claude Code already computes can never reach Langfuse via OTel. Cost has
> to be derived from token counts on our side.

---

## 2. The scenario to cover

| Source | Machine | Account | Viable path |
|--------|---------|---------|-------------|
| Claude Code | mac mini | personal | transcript replay |
| Claude Code | mac mini | work | transcript replay |
| Claude Code | laptop | personal | transcript replay |
| Claude Code | laptop | work | transcript replay |
| Cursor | laptop | own API key | LiteLLM proxy |

Claude Code's native OTel is unusable for the managed work account (Finding A) and
lossy for all accounts (Finding B). **Transcript replay is the only mechanism that
covers all four Claude Code rows with one implementation**, works retroactively over
existing history, and needs no beta flag.

---

## 3. Architecture

One OTel Collector is the spine. Every producer speaks OTLP to it; it normalises
attributes, stamps identity, buffers across Langfuse restarts, and is the only
component holding the Langfuse credentials.

```
mac mini (MINI_TS_IP)                            laptop (LAPTOP_TS_IP)
┌──────────────────────────────────────┐       ┌────────────────────────────┐
│  cc-replay (launchd)                 │       │  cc-replay (launchd)       │
│    ~/.claude-personal/projects/**    │       │   ~/.claude-personal/…     │
│    ~/.claude-work/projects/**        │       │   ~/.claude-work/…         │
│              │ OTLP                  │       │        │ OTLP (Tailscale)  │
│              ▼                       │       │        │                   │
│  ┌────────────────────────────────┐  │◄──────┼────────┘                   │
│  │  OTel Collector      :4318     │  │       │  Cursor ──► LiteLLM :4000  │
│  │   • rename token attrs         │  │◄──────┼────────────────┘           │
│  │   • stamp user.id + tags       │  │       └────────────────────────────┘
│  │   • batch + retry queue        │  │
│  └───────────┬────────────────────┘  │
│              │ OTLP http/protobuf    │
│              │ Basic base64(pk:sk)   │
│              ▼                       │
│  ┌────────────────────────────────┐  │
│  │  Langfuse :3001                │  │
│  │  web · worker · clickhouse     │  │
│  │  minio · redis · postgres      │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
```

**Why a Collector rather than pointing clients straight at Langfuse:** it is the only
place that can fix Finding B; it removes the no-gRPC constraint from every client; it
keeps `sk-lf-…` off the laptop; and it implements the single-project + `user_id` + tags
model at the transport layer instead of in every tool's config — which is the original
idea, just moved to where it can be enforced.

### 3.1 Identity model (the original tags/user_id proposal, made concrete)

One Langfuse project. Attribution by:

| Field | Value | Source |
|-------|-------|--------|
| `langfuse.user.id` | `claude-personal` / `claude-work` / `cursor` | config dir → `.claude.json` |
| `langfuse.session.id` | Claude Code `sessionId` | transcript |
| `langfuse.trace.tags` | `["claude-code","mac-mini","personal","<git-branch>"]` | transcript + host |
| metadata | `cwd`, `gitBranch`, `version`, `agentName`, `teamName`, `service_tier` | transcript |

This gives the consolidated monthly view **and** the per-tool / per-account filters.

### 3.2 Replayer design

- Watches `$CONFIG_DIR/projects/**/*.jsonl`; keeps a per-file byte-offset checkpoint,
  so it is incremental and cheap.
- One Langfuse **trace per `sessionId`**, one **generation span per assistant message**
  that has `message.usage`.
- **Deterministic IDs** — `traceId = hash(sessionId)`, `spanId = hash(message.uuid)`.
  The intent is that a re-run cannot duplicate.
  > ⚠️ **UNVERIFIED — gate this in Phase 1.** Nothing in the Langfuse OTLP docs states
  > that ingestion deduplicates on span ID; that behaviour was a property of the now-dead
  > Ingestion API. Test it before the backfill: POST the identical span twice and confirm
  > **one** observation, not two. If it does not dedupe, the replayer needs its own
  > "already-sent" ledger, or the 172k-span backfill is a one-shot that cannot be retried
  > after a partial failure.
- Emits `langfuse.observation.usage_details` with the four token classes **plus** the
  1h/5m cache split, and `langfuse.observation.cost_details` computed from a local
  price table.
- Content (prompt/response text) is opt-in **per account**, default off.

> **On the cost number:** with a Max subscription the marginal cost of a Claude Code
> request is zero. What the dashboard shows is *API-equivalent* cost — the right proxy
> for value and efficiency, not a bill. Label it as such in the dashboard so it is not
> misread later.

### 3.3 Cursor

Cursor only reaches a custom base URL in "own API key" mode, which bypasses the
included subscription usage and disables some Cursor-side features. **This trade is
not yet verified** (the public pricing/docs pages did not answer it) and is the first
thing to confirm in Phase 3 — before any work is done on that leg. If the trade is bad,
Cursor observability is dropped or reduced; it does not block Phases 1–2.

---

## 4. Phases

Each phase ends in a check whose failure is visible.

### Phase 0 — Reboot resilience (do first; it is the stated requirement)

Persistence has three layers and only one of them is `restart: unless-stopped`.
Checked on this host — **two of the three are already satisfied**:

| Layer | State |
|-------|-------|
| Compose `restart: unless-stopped` + named volumes | to build (Phase 1) |
| macOS power / boot | ✅ `autorestart 1`, `sleep 0`, `womp 1`; **FileVault off**; `autoLoginUser = pablo` |
| **Docker engine autostart** | ❌ **missing — the only real gap** |

FileVault being **off** matters: with it on, a reboot halts at pre-boot unlock, auto-login
never runs, and the acceptance test below could not pass at all. It is off, and auto-login
is already configured, so the remaining work is only to make the Docker engine come up by
itself. Docker Desktop's "start at login" fires on *user login, not boot* — with auto-login
set that is sufficient here, but colima under a LaunchAgent is the more explicit option.

**Acceptance: a real `sudo reboot`, then Langfuse answers on `:3001` with no manual
step and previous traces are still there.** Nothing short of an actual reboot counts.

### Phase 1 — Langfuse up, with persistence

- Compose adapted from upstream, following `local-ai-stack` conventions: images pinned
  by `@sha256:`, named volumes, internal services on `expose:` not `ports:` (upstream
  publishes postgres/redis/clickhouse to the host — strip that).
- `langfuse-web` on **3001**, not 3000, so a future `local-ai-stack` restart doesn't
  collide with Open WebUI.
- **Do not reuse `local-ai-stack`'s Postgres or Redis.** Langfuse uses Redis as a BullMQ
  queue and needs `maxmemory-policy noeviction`; LiteLLM's redis-stack is a cache that
  wants eviction. Version pins also differ (17 vs 16-alpine). Separate containers.
- ClickHouse is the memory floor. Cap it (`max_server_memory_usage_to_ram_ratio`,
  `mark_cache_size`) and **verify the setting actually landed** rather than assuming.
- Secrets (`SALT`, `ENCRYPTION_KEY`, `NEXTAUTH_SECRET`, DB/S3/Redis passwords) in a
  gitignored `.env`, generated locally.

**Acceptance — one span validates the whole attribute contract.** A bare span that
"appears in the UI" is not enough: it would pass while being exactly the empty-render
failure of Finding B. The probe span must carry `langfuse.observation.usage_details`,
`langfuse.observation.cost_details`, the observation-type attribute marking it a
*generation*, and `user.id` / `session.id` / `langfuse.trace.tags`. Require that:

1. it renders **with tokens and cost**, not just a timeline entry;
2. filtering by its tag **returns it**;
3. POSTing it **twice yields one observation** (the dedupe gate from §3.2).

A wrong Basic auth header is a silent 401 — "the collector started" proves nothing.
Stamp tags and user id on **every** span, not only the root: Langfuse's docs require
these attributes present on each span for reliable filtering and aggregation.

### Phase 2 — Claude Code replay (the core)

- Split config dirs: `CLAUDE_CONFIG_DIR=~/.claude-work` / `~/.claude-personal`,
  one per account, on both machines. Verify `.claude.json` relocates into the config
  dir as expected.
- Build the replayer per §3.2. Ship it as a launchd agent on the mini and the laptop.
- Backfill existing history, then run incrementally.

**Backfill size, measured on this host (mini only):**

```
transcript files (recursive):  1,644   <- NOT 296; subagents nest TWO levels
generation spans (dedup):    ~78,270   <- API calls, not records
usage-bearing records:      ~173,100   <- 2.21x the calls; do NOT use as span count
distinct sessions:               ~300
repos / worktrees (cwd):         ~923
date range:      2026-07-06 → 2026-08-30
```

**The corpus is live and growing — these are not fixed numbers.** The earlier
172,303-vs-172,454 gap was attributed in this plan to "unparseable lines". That
explanation is **false**: there are **zero** unparseable lines in 1.5 GB. The real cause
is that counts were taken minutes apart while sessions were still being written; during
the audit alone the file count moved 1,640 → 1,644 and records 172,737 → 173,094.

Consequence: **any acceptance test of the form "count in Langfuse == count in JSONL" is
unsatisfiable.** The backfill must run against a **frozen, date-bounded slice**
(`timestamp < T`), and the equality is asserted against that slice, not against the live
tree.

That is not a trivial import onto a ClickHouse we are simultaneously memory-capping, and
the laptop adds more. **Backfill oldest-first, in dated chunks, with the span count as
the completion check** — a partial import that dies halfway would not be caught by the
single-session cross-check below. Re-runnability depends on the dedupe gate (§3.2).

**Acceptance:** a session run on the laptop under the personal account shows up in
Langfuse within a minute, with correct model, token counts across all four classes,
and the right `user.id`/tags. Cross-check total tokens for one session against the raw
JSONL — the aggregate must **match**, not merely "look plausible" — and confirm the
backfilled span count in Langfuse equals the number above.

### Phase 3 — Cursor (conditional)

Verify the own-API-key trade first (§3.3). If acceptable: LiteLLM with the Langfuse
callback, Cursor pointed at it over Tailscale.

### Phase 4 — What observability was for

Only once traces are flowing: cost/efficiency dashboards, then the knowledge-graph and
eval work that motivated the repo. Trace data is the substrate for all of it.

---

## 5. Repo split

**The Langfuse compose lives in `ai-mastermind`.** Since `local-ai-stack` is stopped and
this project is now the primary one, splitting the stack across two repos would buy
nothing and cost a coordination problem. `local-ai-stack` stays untouched; we copy its
conventions (digest pins, named volumes, `expose:` over `ports:`, launchd) rather than
its files.

`ai-mastermind` is **public**, so:

- Compose, collector configs, the replayer, docs, dashboards and later phases live here.
- `.env`, real keys, Tailscale IPs and machine wiring do **not**.
- `.gitignore` and a secret scan land in the *first* commit, before any push. The
  company OTLP endpoint and its bearer token are referred to abstractly and never
  reproduced in this repo.

---

## 6. Open questions

1. Does a personal-account `CLAUDE_CONFIG_DIR` escape the managed-settings lock, or
   does `remote-settings.json` follow the machine? Decides whether live OTel is ever
   available as a second signal. (Probe blocked in-session: it required copying
   credentials.)
2. Cursor's own-API-key trade-off (§3.3).
3. Sending work-account *content* to a personal box is a deliberate choice, not a
   default. Content capture stays off per account until explicitly enabled.

---

## 7. Audit findings (4-lane deep dive, 2026-08-30)

Four independent lanes audited this document against the real corpus. The headline
defect was found by three of them separately and confirmed by a fourth instrument
(this session's own recomputation): **one class of error, ten more instances.**

### 7.1 The unit-of-observation defect (fixed above)

Records ≠ API calls. Corrected in §1.2. Two guards that must survive the fix:

**The dedup rule is ASYMMETRIC — do not over-correct.**

| Field kind | Examples | Rule |
|---|---|---|
| Per-**response** | `usage`, `model`, `stop_reason` | **dedupe** on `message.id` |
| Per-**block** | `tool_use`, `thinking`, `text` | **do NOT dedupe** — each record is a different block |

The tool counts in §1.2 (Bash 56,110 …) are **correct as-is**; deduping them would
undercount Bash 3.8×. The same corpus is right for one aggregation and 2.2× wrong for the
other — which is exactly why this survived several passes.

**Validated against Claude Code's own accounting.** The corpus contains 6 `cost-state`
records carrying `totalCostUSD` and per-model `modelUsage` — an oracle this plan never
used. Re-summing those sessions from the transcripts:

```
exact matches vs oracle:   per-record  0/21        deduped by message.id  11/21
```

Three sessions match on all three fields exactly (e.g. fable-5 cache-read
4,357,314 = 4,357,314). Residual mismatches are sessions whose transcript spans more than
one `cost-state` window, and they over-count — so **≈$X is an upper bound**. The
price formula itself reproduces `costUSD` to within 0.14%, which independently confirms
the 2× / 1.25× / 0.1× cache multipliers.

### 7.2 Remaining grain defects

| # | Defect | Measured |
|---|---|---|
| G1 | `cwd` held per-trace, varies per-message | **152/300 sessions (50.7%)** have >1 cwd, covering **87.2%** of main-thread records; max 114 in one session |
| G2 | `version` sold as "before/after regressions", drifts mid-session | 13/300 sessions, but **32.1%** of main-thread records; up to 4 versions |
| G3 | `gitBranch` in `trace.tags` but is per-message | 7/300 sessions, 2,045 records (small, but two grains for one value) |
| G4 | Two session identifiers; the plan names one | `sessionId` vs `session_id` **disagree on 10.4%** of generation records |
| G5 | `spanId = hash(message.uuid)` collides | 348 uuids in 2 sessions each (session forks). Absorbed by the `message.id` dedup — **do not stack the corrections** |
| G6 | The `machine` tag has **no source in the data** | **zero** host/hostname/platform fields in 832,423 records; a single Collector cannot stamp per-host |
| G7 | `langfuse.user.id` specified at **three** grains in one document | §3.1 per-replayer, §3 diagram per-deployment, Finding E per-message. The fix landed in prose only |
| G8 | Trace fan-in unbudgeted | median 95 spans/trace, p90 776, max 13,957; **24 traces hold 72.2% of all spans** |
| G9 | "One trace per session" spans weeks | 35/300 sessions >24h (73.7% of records); 11 >7 days; longest **43.3 days** — a mid-trace `/login` is near-certain |

**Not defects (checked and cleared):** `effort`, `service_tier`, `speed` have cardinality
1 corpus-wide with zero within-session variation. `isSidechain` ⟂ `subagents/` path with
zero crossover. Compaction never rewrites history, so the byte-offset checkpoint is safe.

### 7.3 Acceptance criteria that pass while broken

- **Phase 2's token cross-check is self-consistent, not correct.** If the replayer sums
  per record and the check sums per record, it matches exactly *at 2.2× wrong*. **Replace
  it with the `cost-state` oracle** (§7.1) — an independent source the replayer does not
  produce. Also add the 1h/5m cache split to the check: it is this plan's stated advantage
  over OTel and is currently untested.
- **Phase 0 can pass three ways while broken.** (1) `autoLoginUser=pablo` confounds
  boot-vs-login — test over ssh with no console login. (2) "previous traces still there"
  is vacuous with zero pre-existing traces — seed a known trace id first and query it by
  id. (3) ":3001 answers" passes with ClickHouse dead, because `langfuse-web` still serves
  a login page. Query a trace, don't ping a port.
- **Count-equality gates are unsatisfiable** against a live corpus (§1.2). Freeze a slice.

### 7.4 Cost model

- Weekly extrapolation was non-stationary: corrected weekly spend runs **2.8x growth**
  (2.8× growth, W28→W35). Corrected mean **$W/2.3/wk**, current run rate **~$W_now/wk**.
  The earlier "$W/week" was wrong twice — inflated *and* extrapolated from a mean.
- Cache reads/write corrected **30.6 → 37.9** (writes shrink 2.52× under dedup, reads only
  2.04×). Per model: opus-5 47.2 · fable-5 29.8 · opus-4-8 25.3 · sonnet-5 18.7.
- Composition: cache read **60.8%**, cache write 32.0%, output 7.1%, input 0.1%.
- Checked and immaterial: the `e5=cw` fallback fires on **zero** records; `cw` vs
  `e1+e5` differs on 231 records (worth **$7**, and `cw` is authoritative); `<synthetic>`
  (427) has all-zero usage; **0** batch and **0** fast-mode records; `iterations[]` does
  not double-count.
- **Make the price lookup raise on an unknown model id.** The live string is
  `claude-haiku-4-5-20251001`; a table keyed `claude-haiku-4-5` returns **$0 silently**.

### 7.5 Still open

- Langfuse's resolution of trace-scoped `langfuse.user.id` / `langfuse.trace.tags` when
  spans under one traceId disagree (root? first? last? merge?). Decides whether G7/G9 are
  representable at all. **Probe:** extend the Phase 1 acceptance span — which must be
  posted anyway — to *two* spans sharing a traceId with conflicting user id and tags, then
  read back which value survives. One extra span answers it.
- ~~Finding A's two unexcluded alternatives~~ — **CLOSED 2026-08-30**, both excluded
  (§1.2). Mechanism restated precisely.
- **Historical price changes remain unverifiable locally.** The audit flagged that
  `claude-opus-4-8` and `claude-sonnet-5` have no capture-time cost control (they appear
  in no `cost-state` record), and that a $15/M alternative for opus-4-8 would swing the
  total 27%. Status after probing:
  - The intended probe — read Claude Code's own `claude_code.cost.usage` metric — is
    **blocked by the same org override** (`OTEL_METRICS_EXPORTER` is set by the org, so a
    console exporter never runs). `claude -p` also writes no `cost-state` record.
  - Documented rates instead: Opus 4.8 **$5/$25**, stated explicitly where Opus 5 is
    described as "a drop-in upgrade **at Opus 4.8's pricing ($5/$25 per MTok)**";
    Sonnet 5 **$2/$10**. The $15/M scenario is supported by no source.
  - What stays open is narrower than it looked: not *what the price is*, but *whether it
    changed during 2026-07-06 → 08-30*. Nothing on this machine can answer that. Given
    §0 (retention), the practical fix is forward-looking — record `price_table_version`
    and `priced_at` on every span so the question never arises again.
- Cross-agent relations in Claude Code's beta spans are **span links**
  (`link.type: "parent_of"`) to a *different* traceId. Langfuse builds trees from
  parent-span-id, not links — so the Collector `transform` fixes tokens but would not
  fix the cross-agent tree.

---

## 8. Built and verified — 2026-08-30

Status moved from **plan** to **running on the mini**. Measured, not asserted.

### Stack

Seven containers, all healthy: `postgres` · `clickhouse` · `redis` · `minio` ·
`langfuse-web` (3001) · `langfuse-worker` · `litellm` (4000).
Reachable over the tailnet — `http://<mini>:3001/api/public/health` → 200.

### Phase 1 acceptance — PASSED on all three counts

Run via `scripts/acceptance-phase1.py`, verified in ClickHouse rather than through the UI:

1. **Attribute contract** — a generation renders with `usage_details`, `cost_details`,
   model, `user_id`, `session_id` and `tags`. Not the Finding B empty-render shape.
2. **Dedupe gate — ANSWERED: Langfuse deduplicates on `(trace_id, span_id)`.**
   Three posts (two identical + one distinct) produced two unique spans, so the backfill
   is safely re-runnable. **Caveat found in the doing:** `events_full` is a
   **ReplacingMergeTree**, so dedup is *eventual*, on merge. Raw `count()` overstates
   until then — 121,986 rows vs 79,751 unique. Any count must use `uniqExact` or `FINAL`,
   or it reads as data loss/duplication that isn't there.
3. **Trace-scoped conflict — ANSWERED, and better than feared.** Two spans in one trace
   with different `user.id` and different tags both persisted, **each keeping its own
   values**: in v4's events model `user_id` and `tags` are per-event columns. There is no
   trace-scoped attribute to conflict, so a 43-day trace spanning two accounts *is*
   representable, and cost-per-account sums spans. G7/G9 dissolve.

### Backfill (frozen slice, `--until 2026-08-30T11:09:55Z`)

```
files processed        1,642
generations           78,311   (deduped on message.id)
spans sent            80,678   (incl. structural session/subagent/workflow spans)
<synthetic> skipped      427   (API failures: not billable, not generations)
unique GENERATIONs in ClickHouse  78,313   <- matches the replayer
```

Cost, recomputed inside Langfuse over the deduplicated rows:

| Model | Calls | USD |
|---|---:|---:|
| claude-opus-5 | 50,260 | — |
| claude-fable-5 | 13,945 | — |
| claude-opus-4-8 | 11,272 | — |
| claude-sonnet-5 | 2,784 | — |
| claude-haiku-4-5 | 55 | — |
| **Total** | **78,316** | **—** |

Independent agreement: this session's own pre-build estimate was $X and the audit
lanes bracketed $X±1%. Three instruments, one number.

### Two defects found only by building it

- **`credsStore: "desktop"` hangs every `docker pull` under tmux.** The credential
  helper reads a TCC-protected group container and `openat` never returns. Nothing times
  out; the pull just sits there. Worked around with a `DOCKER_CONFIG` at
  `~/.ai-mastermind/dockerconf` that has the helper removed. Same root cause as the
  known tmux/TCC issue on this host.
- **Langfuse binds to the container IP, not loopback**, so a healthcheck against
  `localhost:3000` inside the container reports unhealthy while the service is fine.
  Fixed to `$HOSTNAME:3000`. This is the "acceptance fails for the wrong reason" twin of
  the failure mode §7.3 warns about.
- Langfuse v4 runs in **`events_only` mode**: the legacy `/api/public/traces` read API
  returns a 404-style refusal. Verification goes through ClickHouse or the v4 API.

### Persistence — three layers, all in place

| Layer | State |
|---|---|
| compose `restart: unless-stopped` | 7 services |
| Docker engine autostart | `com.ai-mastermind.docker-autostart` LaunchAgent |
| macOS boot | `autorestart 1`, auto-login `pablo`, FileVault off |

**Not yet proven: the actual reboot.** Every layer is present, but §Phase 0 says only a
real `sudo reboot` counts, checked over ssh with no console login. Until that runs, this
row is "configured", not "verified".

### Running services

```
com.ai-mastermind.account-work        ledger watcher, ~/.claude
com.ai-mastermind.account-personal    ledger watcher, ~/.claude-personal
com.ai-mastermind.replayer            replay loop, every 120 s
com.ai-mastermind.docker-autostart    starts Docker at login
```

### What is NOT done

- **The laptop.** Remote Login is off (`ssh: connect ... Connection refused`), so it
  could not be configured from here. `scripts/bootstrap-laptop.sh` does the whole job in
  one command once ssh is enabled or it is run there directly.
- **The personal Claude profile is not logged in** on either machine. `~/.claude-personal`
  exists with retention set; it needs one interactive `ccp` → `/login`. Until then every
  span is tagged `unknown-pre-split`, which is correct — not a bug.
- **Cursor is configured but unverified.** LiteLLM is up with the Langfuse callback and
  a config for it, but the own-API-key trade (§3.3) is still unconfirmed, and no
  provider key is set.

### 8.1 Cursor pipeline — verified, and one more defect

LiteLLM's built-in `langfuse` callback **does not work against Langfuse v4**. It
initializes cleanly (`Initialized Success Callbacks - ['langfuse']`) and then every
export fails with `Bad request`, because it speaks the **legacy Ingestion API** that v4
removed in `events_only` mode. Nothing in the proxy's response or health surfaces this:
the chat call returns 200 and the trace silently never arrives.

This is the same class as Finding B — a component that looks wired, reports healthy, and
moves no data. It was only caught by asserting on the *destination* rather than on the
call succeeding.

Fixed by routing LiteLLM through OTLP like everything else:

```yaml
litellm_settings:
  callbacks: ["otel"]        # NOT ["langfuse"]
```
```yaml
OTEL_EXPORTER_OTLP_ENDPOINT: http://langfuse-web:3000/api/public/otel
OTEL_EXPORTER_OTLP_PROTOCOL: http/json
OTEL_EXPORTER_OTLP_HEADERS: Authorization=Basic ${LANGFUSE_BASIC_B64}
```

Verified: three calls through the proxy produce `litellm_request` generations plus
`Received Proxy Server Request` spans in ClickHouse. A `mock-verify` model
(`mock_response`) exists so this path can be re-tested with no provider key and no spend.

Known gap: LiteLLM's `user` field does not map to `langfuse.user.id` on the OTLP path,
so Cursor traffic currently lands without an account tag. Fix with a Collector
`transform`, or accept `service.name` as the discriminator.

### 8.2 Cursor is not installed on the mini

`/Applications/Cursor.app` is absent and there is no Cursor config here — consistent
with Cursor being laptop-only. "Cursor on both machines" is therefore satisfied by the
proxy existing and being verified on the mini, plus the client-side setting applied on
the laptop. There is nothing further to configure on the mini.

### 8.3 The laptop — and a design flaw it exposed

Configured over SSH once Remote Login was enabled. Two corrections were forced by what
was actually there:

**Tailscale SSH is not available on the App Store build.** `sudo tailscale set --ssh`
returns *"The Tailscale SSH server does not run in sandboxed Tailscale GUI builds."* The
mini runs the standalone build (`io.tailscale.ipn.macsys`) and the laptop the sandboxed
one — checking the bundle id on one machine and generalising to the other was wrong.
Remote Login works on both regardless.

**The laptop's profile layout is different from the mini's, and the first setup pass got
it wrong.** Discovered layout:

```
~/.claude-work/projects       3,566 files   <- the real work profile (and the default,
                                               via `export CLAUDE_CONFIG_DIR` in .zshrc)
~/.claude-personal/projects     100 files   <- <personal-account>
~/.claude.bak/projects          752 files   <- old backup
~/.claude/projects               67 files   <- leftovers, 0 generations
```

`setup-host.sh` had pointed `ccw` at `~/.claude` and the replayer at `~/.claude/projects`
— which would have replayed **67 files and missed 3,666**. Retention was also left at the
30-day default on `~/.claude-work`, the directory that matters most.

> **The design flaw:** attribution by timestamp ledger assumes one active profile at a
> time. It is true on the mini and **false on the laptop**, where two config dirs are
> live in parallel. The account is decided by **which directory the transcript was
> written into**, not by when. Fixed with an explicit `--account` per root and
> `replay-all.sh`, which **discovers** the profile dirs rather than assuming a layout.

Laptop backfill: `.claude-work` → 40,479 generations · `.claude-personal` → 20,648.

### 8.4 Both machines, attributed

| Account | Generations | USD |
|---|---:|---:|
| unknown-pre-split (mini history, pre-ledger) | 78,221 | — |
| claude-work | 40,950 | — |
| claude-personal | 20,648 | — |

By machine: `mac` 78,493 · `Pablos-MacBook-Pro` 61,328.

**Still open: Cursor.** LiteLLM is up and reachable from the laptop (`:4000` → 200), but
Cursor's base-URL override lives in `state.vscdb`, not `settings.json`, so it is a UI
action — and it only applies in own-API-key mode, which bypasses the included
subscription usage. That trade is still unconfirmed, so the setting was deliberately not
forced.

### 8.5 Cursor cannot reach a tailnet endpoint — the §3.3 caveat was understated

Cursor, configured with `Override OpenAI Base URL = http://<tailnet-ip>:4000/v1`,
returns:

```
Provider returned error: Access to private networks is forbidden
```

**Cursor does not call the custom base URL from the client. It calls it from Cursor's own
servers**, which refuse private ranges — and Tailscale's `100.64.0.0/10` is CGNAT, private
from their side. The evidence lines up: the laptop reaches LiteLLM fine (`:4000` → 200),
yet **nothing** ever arrived in LiteLLM's logs. A call originating on the laptop would
have landed.

This is a harder blocker than the one §3.3 anticipated. That section warned only about the
own-API-key trade (losing included subscription usage). The real constraint is
architectural: **the proxy has to be reachable from the public internet.**

Options, with their real cost:

| Option | What it takes | What it costs |
|---|---|---|
| Tailscale Funnel | Enable HTTPS in the Tailscale admin console, then `tailscale funnel 4000` | **Publishes LiteLLM to the public internet.** Protected only by the master key |
| Cloudflare Tunnel / ngrok | Third-party account + agent | Same exposure, plus another dependency |
| Drop Cursor tracing | nothing | No Cursor visibility; Claude Code (the large majority of the spend) is unaffected |

Funnel was checked and is **not currently possible**: `tailscale cert` reports
*"HTTPS cert support is not enabled/configured for your tailnet"*.

Stacking the costs honestly: to trace Cursor you would give up its included subscription
usage **and** expose an LLM proxy publicly. Claude Code — 139k generations across both
machines — needs none of this, because it is traced from disk.

### 8.6 Two corrections found by looking at a real trace

**The dedupe finding in §8 was too broad.** `events_full`'s sort key is:

```
project_id, toStartOfMinute(start_time), xxHash32(trace_id), span_id, start_time
```

`start_time` is **part of the key**. So ReplacingMergeTree collapses a re-send only when
the timestamps are byte-identical. Re-sending a *corrected* span with different timing
produces a **second row**, not an update — 143,436 stale rows accumulated exactly that
way here and had to be deleted by keeping only `max(event_ts)` per span.

Restated: **identical re-send → deduped. Corrected re-send → duplicate.** The Phase 1
acceptance test only exercised the first case, so it confirmed a narrower property than
the one written down. Any future correction pass must delete the old versions.

**Every latency was fabricated.** No transcript record carries a duration field —
checked, **0 of 56,482** generation records. The replayer's fallback invented a 1 ms
span, which renders as a real measurement and would have quietly poisoned any latency
analysis.

Real signal found instead: the N content-block records of one response are written **as
the stream arrives**, so `max(ts) - min(ts)` across records sharing a `message.id`
approximates the generation window. Applied, the corpus now reports:

```
p50 1,276 ms   p90 8,399 ms   max 516 s   (was: every span exactly 1 ms)
```

Single-block responses get a **zero-width** span rather than an invented duration —
"not measured" should look different from "measured as fast".

### 8.7 Final state

| Account | Generations | USD |
|---|---:|---:|
| unknown-pre-split (mini, pre-ledger) | 78,221 | — |
| claude-work | 41,005 | — |
| claude-personal | 20,648 | — |
| **Total** | **139,882** | **—** |

Cursor: **deliberately not traced.** Reaching it would cost both the included
subscription usage and a publicly exposed proxy (§8.5). The mock wildcard was removed
from `litellm-config.yaml` so it cannot mask real traffic later.

### 8.8 The repo tag was the branch

`cc.repo` was derived with `basename(cwd)`. Inside a git worktree that returns the
**branch**, not the repository:

```
/…/development/chatbot-kb.worktrees/main            ->  "main"       (wrong)
/…/development/chatbot-kb.worktrees/conv-ai-settings->  "conv-ai-…"  (wrong)
```

`repo:main` was the single largest tag at 27,284 spans — **19% of the corpus, meaning
nothing** — and `repo:conv-ai-settings`, `repo:next-actions`, `repo:onboarding` were
branches too. Fixed by stripping the `<repo>.worktrees/<branch>` layer and resolving the
first element under a known code root. One repo's traffic went from being split across
five meaningless tags to a single honest one:

```
before: repo:main 27,284 · repo:conv-ai-settings 25,998 · repo:chatbot-kb 14,110
        · repo:chatbot-kb.worktrees 7,962 · repo:next-actions 3,216
after:  repo:chatbot-kb 63,230
```

Two operational notes from doing the correction:

- **The cleanup DELETE blew ClickHouse's memory cap** (2.33 GiB, set deliberately in
  `clickhouse-memory.xml`): the `NOT IN (SELECT … GROUP BY …)` aggregates the whole table.
  It half-completed. Redone **per partition**, which keeps each aggregation small — that
  is the pattern to reuse for any future correction pass.
- The full path survives in `attributes.cc.cwd`, so this was fixable without re-reading a
  single transcript. **Keep the raw value next to every derived one**; the derivation is
  what turned out to be wrong, not the data.

### 8.9 What the employer's telemetry does and does not carry

Asked directly, so verified across every config layer rather than assumed.

**Never sent, at any setting:** working directory, repository name, git branch. The docs
list these as not tracked; there is no variable that enables them.

**Always sent, not optional:** `organization.id`, `user.email`, `user.account_uuid`,
`user.account_id`, `user.id`, `session.id`, `terminal.type` — identity, timing, volume and
model, but not subject matter.

**Redacted by default**, each behind its own opt-in: prompt text
(`OTEL_LOG_USER_PROMPTS`), response text (`OTEL_LOG_ASSISTANT_RESPONSES`), bash commands
and **file paths** (`OTEL_LOG_TOOL_DETAILS`), tool input/output (`OTEL_LOG_TOOL_CONTENT`),
and the entire conversation as raw JSON (`OTEL_LOG_RAW_API_BODIES`).

Measured on this host: **none of the five is set** — not in the org's remote settings, not
in user settings, not in managed settings (absent), not in the live environment. Content is
therefore not leaving.

Two caveats worth keeping:

1. **Those switches are controlled by whoever pushes the remote settings, not by the
   user.** The posture can change without any local action. Re-check with:
   `grep -o 'OTEL_LOG_[A-Z_]*' ~/.claude/remote-settings.json`
2. `OTEL_LOG_TOOL_DETAILS` would send **file paths**. The repo name is never sent, but a
   path implies it — so the "repo is never sent" guarantee is weaker than it sounds if
   that one switch is ever flipped.

### 8.10 The ledger's change detection ignored which directory it was watching

`last_uuid` was read from the **last line of the whole ledger**, not the last line for the
directory being watched. Several watchers share one ledger file, so on startup each one
compared its own account against whichever directory happened to write last. If they
differed — which is the normal case, that being the entire point of separate profiles —
it appended an entry recording an account change that never happened.

Observed live: restarting the two watchers on the laptop made one of them write a
spurious entry immediately.

This is worse than noise. Where a directory has held more than one account the replayer
attributes spans by **joining this ledger on time**, so a fabricated entry silently
relabels every span after its timestamp. The mechanism built to make attribution
trustworthy was itself able to corrupt it.

Fixed by scoping the comparison to `config_dir`. Verified: three alternating `--once`
invocations across two directories now append nothing; before the fix the same sequence
wrote three entries.

A pattern worth naming, since it has now appeared four times in this work: **the record
was always right and the conclusion drawn from it was wrong.** The ledger held the correct
email and organization in every entry; the label on top was wrong. The transcripts held
the correct `usage`; the sum over them was wrong. `cc.cwd` held the correct path; the repo
derived from it was wrong. Keep the raw observation next to every derived value — every
one of these was repairable precisely because the raw value had been kept.
