#!/usr/bin/env python3
"""Emit one line whenever a watched herdr worker changes status or disappears.

Exits when every watched worker is neither working nor blocked.
"""
import json
import subprocess
import sys
import time

names = sys.argv[1:]
last = {}

def snapshot():
    try:
        out = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=20).stdout
        agents = json.loads(out)["result"]["agents"]
    except Exception as error:  # transient socket/JSON failure: report, keep watching
        print(f"watch: herdr agent list failed: {error}", flush=True)
        return None
    by_name = {a.get("name"): a.get("agent_status") for a in agents}
    return {n: by_name.get(n, "MISSING") for n in names}

while True:
    current = snapshot()
    if current is not None:
        for name, status in current.items():
            if last.get(name) != status:
                print(f"{name}: {last.get(name, '-')} -> {status}", flush=True)
                last[name] = status
        if all(s not in ("working", "blocked") for s in current.values()):
            print("all watched workers stopped: " + ", ".join(f"{k}={v}" for k, v in current.items()), flush=True)
            break
    time.sleep(15)
