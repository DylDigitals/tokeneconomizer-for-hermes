"""Chisle + TokenEconomizer on one tool result: does the guard's redirect survive when Chisle
compresses the same call? Hermes keeps the first string a `transform_tool_result` hook returns.

    HX_WITH_CHISLE=1 HX_PATH_PREFIX=$W/sandbox/bin-node /usr/bin/python3 harness/chisle_conflict.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance as A  # noqa: E402

# A failing check whose output Chisle compresses (well over its 100-line head+tail window).
# It names the failure first: the guard reads a result's first 20,000 characters.
NOISY = """#!/bin/sh
id=$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')
echo "error: transient upstream timeout; retrying shards" >&2
i=0
while [ $i -lt 300 ]; do echo "remote-check: request $id step $i fetching build-cache shard $i of 300 ... ok" >&2; i=$((i+1)); done
echo "error: transient upstream timeout (request $id); please retry" >&2
exit 3
"""

r = A.root("chisle-loop")
with open(f"{r}/repo/scripts/remote-check.sh", "w") as fh:
    fh.write(NOISY)
A.sh(f"git -C {r}/repo commit -qam noisy")
for i in range(1, 6):                       # ended-green turns: the guard's envelope
    A.turn(r, f"g{i}", A.TESTS)
u, out = A.turn(r, "loop", A.LOOP)
sid = u["session_id"]
ev = [e for e in A.events(r) if e.get("session_id") == sid]
decided = [e["tool_calls"] for e in ev if e["kind"] in ("nudge", "abort")]
ds = A.dumps(r, sid)
results = [m for m in (ds[-1]["messages"] if ds else []) if m.get("role") == "tool"]
carrying = [i + 1 for i, m in enumerate(results) if A.carries(m)]
chisled = [i + 1 for i, m in enumerate(results) if "chisle" in json.dumps(m).lower()]
report = {"calls": len(results), "guard_decisions_at": decided, "te_block_on": carrying,
          "chisle_compressed": chisled, "lost": sorted(set(decided) - set(carrying)),
          "reply_notice": [l for l in out.splitlines() if "TokenEconomizer" in l][:2]}
print(json.dumps(report, indent=1))
