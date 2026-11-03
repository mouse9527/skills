"""Fake herdr for tests/test_question_inbox.py (never touches a real herdr).

State: JSON file at $FAKE_HERDR_STATE, re-read on every call so a test can change it between calls:
  {"panes": {"<session>": [<pane>, ...]},           # sessions not listed answer server_not_running
   "prompt": "ok" | "blocked" | "gone" | "timeout" | "garbage" | "error:<code>",
   "sleep": <seconds for "timeout">,
   "pane_list_fail": {"<session>": "timeout" | "error:<code>"},   # 只让这个 session 的 pane list / agent list 失败
   "start": "ok" | "trust" | "error:<code>",       # agent start：就绪 / 先弹「是否信任」（默认项 No）/ 失败
   "workspace_create": "ok" | "error:<code>",
   "prompt_working": true}                          # 收到 prompt 后把目标 pane 改成 working
Every call is appended to $FAKE_HERDR_LOG as one JSON line (the argv).
Router 相关的命令会改写状态文件（像真 herdr 一样，pane 是活的）：workspace create 加一个空 shell pane，agent start 让它变成
claude（名字写在 pane 的 name 上；agent list 只列有 agent 的 pane），trust 模式下先停在信任弹窗，send-keys down / enter 选择，
选 No 就退出 agent；workspace close 删掉这个 workspace 的 pane。agent read 输出屏幕原文（不是 JSON），和真 herdr 一样。
Like the real herdr: success JSON goes to stdout with rc=0; an error leaves stdout empty, writes the error JSON to
stderr and exits 1 (observed on a real agent_blocked).
Supports: pane list; agent prompt/list/get/read/send-keys/start; workspace create/close.
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


TRUST = ['Accessing workspace:', '', ' Quick safety check: Is this a project you created or one you trust?', '']


def trust_screen(cursor):
    opts = ['No, exit', 'Yes, I trust this folder']
    return '\n'.join(TRUST + [(' ❯ ' if i == cursor else '   ') + o for i, o in enumerate(opts)] + ['', ' Enter to confirm'])


def save(state):
    with open(os.environ['FAKE_HERDR_STATE'], 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)


def target_of(panes, target):
    return next((p for p in panes if p.get('pane_id') == target or (p.get('name') and p.get('name') == target)), None)


def ready(state, p, name):
    n = state['seq'] = state.get('seq', 0) + 1
    p.update({'agent': 'claude', 'agent_status': 'idle', 'name': name, 'interactive_ready': True,
              'agent_session': {'agent': 'claude', 'kind': 'id', 'value': f'sess-{name}-{n}'}})
    p.pop('screen', None)


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
    if rest[:2] == ['workspace', 'create']:
        mode = state.get('workspace_create', 'ok')
        if mode.startswith('error:'):
            fail(mode[6:], 'fake workspace create error', 'cli:workspace:create')
        n = state['wseq'] = state.get('wseq', 0) + 1
        while any(p.get('pane_id') == f'r{n}:p1' for ps in panes.values() for p in ps):
            n = state['wseq'] = n + 1
        opt = {rest[i]: rest[i + 1] for i in range(2, len(rest) - 1) if rest[i] in ('--label', '--cwd')}
        wid = f'r{n}'
        p = {'pane_id': f'{wid}:p1', 'workspace_id': wid, 'cwd': opt.get('--cwd', ''), 'agent_status': 'unknown',
             'workspace_label': opt.get('--label', ''), 'env': [rest[i + 1] for i, a in enumerate(rest) if a == '--env']}
        panes[session].append(p)
        save(state)
        out({'id': 'cli:workspace:create', 'result': {'type': 'workspace_created', 'root_pane': dict(p),
                                                        'workspace': {'workspace_id': wid, 'label': p['workspace_label']}}})
    if rest[:2] == ['workspace', 'close']:
        panes[session] = [p for p in panes[session] if p.get('workspace_id') != rest[2]]
        save(state)
        out({'id': 'cli:workspace:close', 'result': {'type': 'ok'}})
    if rest[:2] == ['agent', 'list']:
        bad = state.get('pane_list_fail', {}).get(session)  # 同一个 session 的 list 一起失败
        if bad == 'timeout':
            time.sleep(float(state.get('sleep', 30)))
        elif bad:
            fail(bad[6:] if bad.startswith('error:') else bad, 'fake agent list error', 'cli:agent:list')
        out({'id': 'cli:agent:list', 'result': {'type': 'agent_list', 'agents': [p for p in panes[session] if p.get('agent')]}})
    if rest[:2] in (['agent', 'get'], ['agent', 'read'], ['agent', 'send-keys']):
        p = target_of(panes[session], rest[2])
        if not p:
            fail('agent_not_found', f'agent {rest[2]} not found', 'cli:agent')
        if rest[1] == 'get':
            out({'id': 'cli:agent:get', 'result': {'type': 'agent_info', 'agent': p}})
        if rest[1] == 'read':
            print(p.get('screen', 'claude ready'))
            sys.exit(0)
        for key in rest[3:]:
            if 'screen' in p and key == 'down':
                p['cursor'] = min(1, p.get('cursor', 0) + 1)
            elif 'screen' in p and key == 'enter':
                if p.get('cursor', 0) == 1:
                    ready(state, p, p['name'])
                else:  # 选了 No, exit：agent 退出，pane 回到 shell
                    for k in ('agent', 'name', 'screen', 'cursor', 'agent_session'):
                        p.pop(k, None)
                    p['agent_status'] = 'unknown'
            if 'screen' in p:
                p['screen'] = trust_screen(p.get('cursor', 0))
        save(state)
        out({'id': 'cli:agent:send-keys', 'result': {'type': 'ok'}})
    if rest[:2] == ['agent', 'start']:
        name, pid = rest[2], rest[rest.index('--pane') + 1]
        p = target_of(panes[session], pid)
        if not p or p.get('agent'):
            fail('pane_not_found' if not p else 'pane_busy', 'fake start: pane not at a shell prompt', 'cli:agent:start')
        mode = state.get('start', 'ok')
        if mode.startswith('error:'):
            fail(mode[6:], 'fake agent start error', 'cli:agent:start')
        p['argv'] = rest[rest.index('--') + 1:] if '--' in rest else []
        if mode == 'trust':
            p.update({'agent': 'claude', 'agent_status': 'blocked', 'name': name, 'cursor': 0, 'screen': trust_screen(0)})
            save(state)
            fail('agent_not_ready', f'agent {name} is blocked during startup and is not ready for prompts', 'cli:agent:start')
        ready(state, p, name)
        save(state)
        out({'id': 'cli:agent:start', 'result': {'type': 'agent_started', 'agent': p}})
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
        if state.get('prompt_working'):  # 可选：收到 prompt 后转 working（测 router dispatch 的确认）
            target_of(panes[session], target)['agent_status'] = 'working'
            save(state)
        out({'id': 'cli:agent:prompt', 'result': {'type': 'agent_prompt', 'pane_id': target}})
    fail('unsupported', 'fake herdr: unsupported command')


if __name__ == '__main__':
    main(sys.argv[1:])
