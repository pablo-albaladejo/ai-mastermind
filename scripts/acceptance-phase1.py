#!/usr/bin/env python3
"""
Phase 1 acceptance. A span that merely "appears in the UI" would pass while being the
exact empty-render failure documented in PLAN.md Finding B, so this probe asserts on
content and answers the two open questions in one pass:

  1. does a generation render WITH tokens and cost?      (attribute contract)
  2. does posting the same span twice yield ONE?          (dedupe gate — the backfill
                                                           of ~78k spans depends on it)
  3. when two spans in one trace disagree on user.id and
     tags, which value survives?                          (decides whether a 43-day
                                                           trace with two accounts is
                                                           representable at all)
"""
import json, os, sys, time, urllib.request, urllib.error
from base64 import b64encode

BASE = os.environ.get("LANGFUSE_BASE", "http://localhost:3001")
PK, SK = os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]
AUTH = b64encode(f"{PK}:{SK}".encode()).decode()
RUN = str(int(time.time()))


def attr(k, v):
    if isinstance(v, (list, tuple)):
        return {"key": k, "value": {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}}
    return {"key": k, "value": {"stringValue": str(v)}}


def post_spans(spans):
    body = {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "acceptance")]},
                               "scopeSpans": [{"scope": {"name": "probe"}, "spans": spans}]}]}
    req = urllib.request.Request(f"{BASE}/api/public/otel/v1/traces",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def api(path):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Basic {AUTH}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read()[:300].decode(errors="replace")}


now = int(time.time() * 1e9)
tid = f"{RUN:0>32}"[-32:].replace(" ", "0")
tid = ("a" * (32 - len(RUN)) + RUN)[:32]


def gen_span(sid_hex, user, tags, model="claude-opus-5"):
    return {
        "traceId": tid, "spanId": sid_hex, "name": f"llm {model}", "kind": 3,
        "startTimeUnixNano": str(now - 2_000_000_000), "endTimeUnixNano": str(now),
        "attributes": [
            attr("langfuse.observation.type", "generation"),
            attr("langfuse.observation.model.name", model),
            attr("gen_ai.request.model", model),
            attr("langfuse.observation.usage_details",
                 json.dumps({"input": 1000, "output": 500,
                             "cache_read_input_tokens": 20000,
                             "cache_creation_input_tokens": 3000})),
            attr("langfuse.observation.cost_details",
                 json.dumps({"input": 0.005, "output": 0.0125,
                             "cache_read_input_tokens": 0.01,
                             "cache_creation_input_tokens": 0.03})),
            attr("langfuse.user.id", user),
            attr("langfuse.session.id", f"acceptance-{RUN}"),
            attr("langfuse.trace.tags", tags),
        ],
        "status": {"code": 1},
    }


SPAN_A = ("a" * 15 + "1")[:16]
SPAN_B = ("b" * 15 + "2")[:16]

print(f"trace {tid}\n")

print("1/4  posting span A (tokens + cost + tags) ...")
print("     HTTP", post_spans([gen_span(SPAN_A, "claude-work", ["claude-code", "mac", "probe-" + RUN])]))

print("2/4  posting the IDENTICAL span again (dedupe gate) ...")
print("     HTTP", post_spans([gen_span(SPAN_A, "claude-work", ["claude-code", "mac", "probe-" + RUN])]))

print("3/4  posting span B in the SAME trace with a CONFLICTING user.id and tags ...")
print("     HTTP", post_spans([gen_span(SPAN_B, "claude-personal", ["claude-code", "laptop", "conflict-" + RUN])]))

print("\nwaiting for the worker to flush ...")
results = {}
for wait in range(24):
    time.sleep(5)
    obs = api(f"/api/public/observations?traceId={tid}&limit=50")
    n = len(obs.get("data", []) or []) if "_error" not in obs else 0
    if n:
        results["obs"] = obs
        break
print(f"observations found: {n}")

if not results:
    sys.exit("FAIL: nothing ingested — check langfuse-worker logs")

data = results["obs"]["data"]
by_span = {}
for o in data:
    by_span.setdefault(o.get("id"), o)

print("\n--- RESULTS ---")
ok = True

# (1) attribute contract
gen = [o for o in data if o.get("type") == "GENERATION"]
if not gen:
    print("FAIL 1: no observation typed GENERATION"); ok = False
else:
    g = gen[0]
    usage = g.get("usageDetails") or {}
    cost = g.get("costDetails") or {}
    print(f"  1 attribute contract : model={g.get('model')} usage={usage} cost={cost}")
    if not usage or not cost:
        print("      FAIL: tokens/cost did not render (the Finding B empty-render shape)"); ok = False
    else:
        print("      PASS: tokens and cost rendered")

# (2) dedupe
print(f"  2 dedupe             : {len(data)} observations for 3 posts (2 identical + 1 distinct)")
if len(data) == 2:
    print("      PASS: identical span posted twice -> ONE observation. Backfill is re-runnable.")
elif len(data) == 3:
    print("      FAIL: duplicated. spanId is NOT a dedupe key — the replayer needs its own")
    print("            sent-ledger, and the backfill becomes a one-shot."); ok = False
else:
    print(f"      UNCLEAR: expected 2 or 3, got {len(data)}"); ok = False

# (3) trace-scoped conflict resolution
tr = api(f"/api/public/traces/{tid}")
if "_error" in tr:
    print(f"  3 trace conflict     : could not read trace ({tr['_error']})")
else:
    print(f"  3 trace conflict     : trace.userId={tr.get('userId')!r} tags={tr.get('tags')!r}")
    print("      (span A said claude-work/probe-*, span B said claude-personal/conflict-*)")
    print("      -> this is the rule the replayer must design around; recorded, not asserted.")

print("\nOVERALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
