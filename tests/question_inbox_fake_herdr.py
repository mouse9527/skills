"""Fake herdr for tests/test_question_inbox.py (never touches a real herdr).

State: JSON file at $FAKE_HERDR_STATE, re-read on every call so a test can change it between calls:
  {"panes": {"<session>": [<pane>, ...]},           # sessions not listed answer server_not_running
   "prompt": "ok" | "blocked" | "gone" | "timeout" | "garbage" | "error:<code>",
   "sleep": <seconds for "timeout">,
   "pane_list_fail": {"<session>": "timeout" | "error:<code>"}}   # 只让这个 session 的 pane list 失败
Every call is appended to $FAKE_HERDR_LOG as one JSON line (the argv).
Like the real herdr: success JSON goes to stdout with rc=0; an error leaves stdout empty, writes the error JSON to
stderr and exits 1 (observed on a real agent_blocked).
Supports: --session S pane list ; --session S agent prompt <pane> <text>.
"""
import json
import os
import sys
import time


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def fail(code, message, cmd='cli'):
    print(json.dumps({'error': {'code': code, 'message': message}, 'id': cmd}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def main(argv):
    with open(os.environ['FAKE_HERDR_LOG'], 'a', encoding='utf-8') as f:
        f.write(json.dumps(argv, ensure_ascii=False) + '\n')
    with open(os.environ['FAKE_HERDR_STATE'], encoding='utf-8') as f:
        state = json.load(f)
    session = argv[argv.index('--session') + 1] if '--session' in argv else 'default'
    rest = [a for i, a in enumerate(argv) if a != '--session' and (i == 0 or argv[i - 1] != '--session')]
    panes = state.get('panes', {})
    if session not in panes:
        fail('server_not_running', f'no herdr server for {session}')
    if rest[:2] == ['pane', 'list']:
        bad = state.get('pane_list_fail', {}).get(session)
        if bad == 'timeout':
            time.sleep(float(state.get('sleep', 30)))
        elif bad:
            fail(bad[6:] if bad.startswith('error:') else bad, 'fake pane list error', 'cli:pane:list')
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
            fail('agent_not_found', f'agent {target} not found', 'cli:agent:prompt')
        if mode == 'blocked':
            fail('agent_blocked', f'agent {target} is blocked and requires interactive input', 'cli:agent:prompt')
        if mode.startswith('error:'):
            fail(mode[6:], 'fake error', 'cli:agent:prompt')
        out({'id': 'cli:agent:prompt', 'result': {'type': 'agent_prompt', 'pane_id': target}})
    fail('unsupported', 'fake herdr: unsupported command')


if __name__ == '__main__':
    main(sys.argv[1:])
