"""Fake herdr for tests/test_question_inbox.py (never touches a real herdr).

State: JSON file at $FAKE_HERDR_STATE, re-read on every call so a test can change it between calls:
  {"panes": {"<session>": [<pane>, ...]},           # sessions not listed answer server_not_running
   "prompt": "ok" | "blocked" | "gone" | "timeout" | "garbage" | "error:<code>",
   "sleep": <seconds for "timeout">}
Every call is appended to $FAKE_HERDR_LOG as one JSON line (the argv).
Supports: --session S pane list ; --session S agent prompt <pane> <text>.
"""
import json
import os
import sys
import time


def out(obj, rc=0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(rc)


def main(argv):
    with open(os.environ['FAKE_HERDR_LOG'], 'a', encoding='utf-8') as f:
        f.write(json.dumps(argv, ensure_ascii=False) + '\n')
    with open(os.environ['FAKE_HERDR_STATE'], encoding='utf-8') as f:
        state = json.load(f)
    session = argv[argv.index('--session') + 1] if '--session' in argv else 'default'
    rest = [a for i, a in enumerate(argv) if a != '--session' and (i == 0 or argv[i - 1] != '--session')]
    panes = state.get('panes', {})
    if session not in panes:
        out({'id': 'cli', 'error': {'code': 'server_not_running', 'message': f'no herdr server for {session}'}}, 1)
    if rest[:2] == ['pane', 'list']:
        out({'id': 'cli:pane:list', 'result': {'panes': panes[session], 'type': 'pane_list'}})
    if rest[:2] == ['agent', 'prompt']:
        target = rest[2]
        mode = state.get('prompt', 'ok')
        if mode == 'timeout':
            time.sleep(float(state.get('sleep', 30)))
            out({'id': 'cli:agent:prompt', 'result': {'type': 'ok'}})
        if mode == 'garbage':
            print('not json at all')
            sys.exit(0)
        if mode == 'gone' or not any(p.get('pane_id') == target for p in panes[session]):
            out({'error': {'code': 'pane_not_found', 'message': f'pane {target} not found'}, 'id': 'cli:agent:prompt'}, 1)
        if mode == 'blocked':
            out({'error': {'code': 'agent_blocked', 'message': 'agent is blocked'}, 'id': 'cli:agent:prompt'}, 1)
        if mode.startswith('error:'):
            out({'error': {'code': mode[6:], 'message': 'fake error'}, 'id': 'cli:agent:prompt'}, 1)
        out({'id': 'cli:agent:prompt', 'result': {'type': 'agent_prompt', 'pane_id': target}})
    out({'error': {'code': 'unsupported', 'message': 'fake herdr: unsupported command'}}, 2)


if __name__ == '__main__':
    main(sys.argv[1:])
