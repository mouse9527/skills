#!/usr/bin/env python3
"""Emit one line whenever a watched herdr worker changes state or disappears.

A change is detected by herdr's state_change_seq, not by comparing status
strings, so a worker that goes blocked -> working -> blocked between two polls
is still reported. Exits only after every watched worker has stayed out of
working/blocked for SETTLE seconds (a single done/idle blip is not an exit).
"""
import json
import subprocess
import sys
import time

POLL = 3
SETTLE = 20

names = sys.argv[1:]
if not names or any(n.startswith("-") for n in names):
    print("usage: watch_workers.py <name> [<name>...]", file=sys.stderr)
    sys.exit(2)
last = {}
stopped_since = None

def snapshot():
    try:
        out = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=20).stdout
        agents = json.loads(out)["result"]["agents"]
    except Exception as error:  # transient socket/JSON failure: report, keep watching
        print(f"watch: herdr agent list failed: {error}", flush=True)
        return None
    by_name = {a.get("name"): (a.get("agent_status"), a.get("state_change_seq")) for a in agents}
    return {n: by_name.get(n, ("MISSING", None)) for n in names}

while True:
    current = snapshot()
    if current is not None:
        for name, (status, seq) in current.items():
            prev = last.get(name)
            if prev != (status, seq):
                print(f"{name}: {prev[0] if prev else '-'} -> {status}", flush=True)
                last[name] = (status, seq)
        if all(s not in ("working", "blocked") for s, _ in current.values()):
            stopped_since = stopped_since or time.time()
            if time.time() - stopped_since >= SETTLE:
                print("all watched workers stopped: " + ", ".join(f"{k}={v[0]}" for k, v in current.items()), flush=True)
                break
        else:
            stopped_since = None
    time.sleep(POLL)
