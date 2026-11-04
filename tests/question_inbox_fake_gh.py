"""Fake gh for the question-inbox tests (never touches GitHub).

State: JSON file at $FAKE_GH_STATE, re-read on every call:
  {"repos": {"owner/repo": <full GraphQL response object> | "fail:<message>" | "timeout"},
   "board": {"owner/repo#12": <GraphQL response for the board query>},
   "issues": {"<url>": {"state": ..., "stateReason": ..., "body": ...}},
   "comments": {"owner/repo": [<issue comment object>]},
   "next_comment_id": 1000,
   "post": "ok" | "fail:<message>" | "lost"}
Every call is appended to $FAKE_GH_LOG as one JSON line (the argv).
Supports:
  gh api [--include] graphql -f query=... -f owner=O -f name=N [-f number=N]
  gh api [--include] repos/O/R/issues/comments[?per_page=..&page=..&since=..]
  gh api -X POST repos/O/R/issues/N/comments -F body=@file
  gh issue view <url> --json ...
A failure mimics real gh: message on stderr, rc=1.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def load():
    with open(os.environ['FAKE_GH_STATE'], encoding='utf-8') as f:
        return json.load(f)


def save(state):
    with open(os.environ['FAKE_GH_STATE'], 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False)


def ts(v):
    try:
        return datetime.fromisoformat((v or '').replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def emit(state, data, include=False):
    body = json.dumps(data, ensure_ascii=False)
    if include:
        sys.stdout.write('HTTP/2.0 200 OK\nx-ratelimit-remaining: %s\ncontent-type: application/json\n\n%s'
                         % (state.get('remaining', 4999), body))
    else:
        sys.stdout.write(body)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def field(argv, name):
    for i, a in enumerate(argv):
        if a in ('-f', '-F') and i + 1 < len(argv) and argv[i + 1].startswith(name + '='):
            return argv[i + 1].split('=', 1)[1]
    return None


def comments(argv, state):
    path = next(a for a in argv if a.startswith('repos/') and re.search(r'/issues/(?:\d+/)?comments', a))
    parsed = urlparse('https://x/' + path.split('?', 1)[0])
    parts = parsed.path.split('/')
    repo = '/'.join(parts[2:4])
    number = int(parts[5]) if len(parts) > 6 and parts[4] == 'issues' and parts[5].isdigit() else None
    q = parse_qs(urlparse('https://x/' + path).query)
    per = int((q.get('per_page') or ['100'])[0])
    page = int((q.get('page') or ['1'])[0])
    since = (q.get('since') or [None])[0]
    items = [c for c in state.get('comments', {}).get(repo, [])]
    if number is not None:
        items = [c for c in items if (c.get('issue_url') or '').endswith(f'/issues/{number}')]
    if since:
        items = [c for c in items if ts(c.get('updated_at') or c.get('created_at')) >= ts(since)]
    items.sort(key=lambda c: c.get('id') or 0)
    chunk = items[(page - 1) * per: page * per]
    emit(state, chunk, include='--include' in argv)
    sys.exit(0)


def post_comment(argv, state):
    path = next(a for a in argv if a.startswith('repos/') and '/issues/' in a and a.endswith('/comments'))
    parts = path.split('/')
    repo, number = '/'.join(parts[1:3]), int(parts[4])
    body = ''
    for i, a in enumerate(argv):
        if a == '-F' and i + 1 < len(argv) and argv[i + 1].startswith('body=@'):
            with open(argv[i + 1][len('body=@'):], encoding='utf-8') as f:
                body = f.read()
    mode = state.get('post', 'ok')
    if mode == 'timeout':
        time.sleep(30)
    if isinstance(mode, str) and mode.startswith('fail:'):
        fail(mode[5:])
    n = int(state.get('next_comment_id') or 1000)
    state['next_comment_id'] = n + 1
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    c = {'id': n, 'body': body, 'user': {'login': state.get('login', 'demo-owner')},
         'created_at': now, 'updated_at': now, 'issue_url': f'https://api.github.com/repos/{repo}/issues/{number}',
         'html_url': f'https://github.com/{repo}/issues/{number}#issuecomment-{n}'}
    state.setdefault('comments', {}).setdefault(repo, []).append(c)
    save(state)
    if mode == 'lost':
        fail('simulated lost response')
    emit(state, c)
    sys.exit(0)


def main(argv):
    with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as f:
        f.write(json.dumps(argv, ensure_ascii=False) + '\n')
    state = load()
    if argv[:2] == ['issue', 'view']:  # gh issue view <url> --json state,stateReason
        info = state.get('issues', {}).get(argv[2])
        if info is None:
            fail(f'GraphQL: Could not resolve to an issue ({argv[2]})')
        print(json.dumps(info, ensure_ascii=False))
        sys.exit(0)
    if re.search(r'/issues/(?:\d+/)?comments', ' '.join(argv)):
        if any(a == '-X' for a in argv) and 'POST' in argv:
            return post_comment(argv, state)
        return comments(argv, state)
    if '-X' in argv and 'POST' in argv:
        return post_comment(argv, state)
    if argv[:2] != ['api', 'graphql'] and argv[:3] != ['api', 'graphql', '--include'] and \
            not (argv[:2] == ['api', '--include'] and argv[2:3] == ['graphql']):
        fail('fake gh: unsupported command')
    query = field(argv, 'query') or ''
    owner, name = field(argv, 'owner'), field(argv, 'name')
    if 'subIssues(first:' in query:
        number = int(field(argv, 'number') or 0)
        resp = state.get('board', {}).get(f'{owner}/{name}#{number}')
        if resp is None:
            fail(f'GraphQL: Could not resolve to an issue #{number}')
    else:
        resp = state.get('repos', {}).get(f'{owner}/{name}')
        if resp is None:
            emit(state, {'errors': [{'message': 'Could not resolve to a Repository'}], 'data': {'repository': None}})
            sys.exit(1)
    if resp == 'timeout':
        time.sleep(30)
    if isinstance(resp, str):
        fail(resp[5:] if resp.startswith('fail:') else resp)
    emit(state, resp, include='--include' in argv and 'graphql' not in argv)


if __name__ == '__main__':
    main(sys.argv[1:])
