"""Fake gh for tests/test_question_inbox_tasks.py (never touches GitHub).

State: JSON file at $FAKE_GH_STATE, re-read on every call:
  {"repos": {"owner/repo": <full GraphQL response object> | "fail:<message>" | "timeout"}}
Every call is appended to $FAKE_GH_LOG as one JSON line (the argv).
Supports only: gh api graphql -f query=... -f owner=O -f name=N [-f k=v ...].
A failure mimics real gh: message on stderr, rc=1.
"""
import json
import os
import sys
import time


def main(argv):
    with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as f:
        f.write(json.dumps(argv, ensure_ascii=False) + '\n')
    if argv[:2] != ['api', 'graphql']:
        print('fake gh: unsupported command', file=sys.stderr)
        sys.exit(1)
    fields = dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a == '-f')
    with open(os.environ['FAKE_GH_STATE'], encoding='utf-8') as f:
        state = json.load(f)
    resp = state.get('repos', {}).get(f'{fields.get("owner")}/{fields.get("name")}')
    if resp is None:
        print(json.dumps({'errors': [{'message': 'Could not resolve to a Repository'}], 'data': {'repository': None}}))
        sys.exit(1)
    if resp == 'timeout':
        time.sleep(30)
    if isinstance(resp, str):
        print(resp[5:] if resp.startswith('fail:') else resp, file=sys.stderr)
        sys.exit(1)
    print(json.dumps(resp, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv[1:])
