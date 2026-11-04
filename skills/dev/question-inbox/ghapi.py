"""GitHub 读写（只读任务/看板 + 根 Issue 评论），gh 一律 argv 调用，不经过 shell。

评论写入集中在这里，配合同一把「写门」：全局串行、间隔 ≥1 秒、每个需求 20 次/分钟，全局暂停时禁止写。
返回和错误都用 qstore.QError（409 表示拒绝 / 暂缓，页面保留输入）。只用标准库。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path

import qstore

TAG_RE = re.compile(r'<!--\s*qi:need-(need-\d{4,})\s+msg-([A-Za-z0-9_-]{8,64})\s*-->')
WRITE_INTERVAL = float(os.environ.get('QI_GH_WRITE_INTERVAL') or 1.0)
PER_NEED_PER_MIN = 20
WRITE_LOCK = threading.Lock()


def gh_bin():
    return os.environ.get('GH_BIN_PATH') or shutil.which('gh') or 'gh'


def timeout():
    return float(os.environ.get('QI_GH_TIMEOUT') or 30)


class GHError(qstore.QError):
    """code: 409 拒绝/暂缓；kind 给页面看的机器可读原因。"""

    def __init__(self, code, message, kind='', status=0, retry_after=0):
        super().__init__(code, message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


def _run(argv, stdin=None):
    try:
        res = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=timeout(),
                             input=stdin)
    except subprocess.TimeoutExpired:
        raise GHError(409, 'gh 超时，结果不明；先到 Issue 上对账，不盲目重发', 'timeout')
    except OSError as e:
        raise GHError(409, f'无法执行 gh（{gh_bin()}）：{e}', 'exec')
    return res


def _headers_remaining(text):
    if not text.startswith('HTTP/'):
        return None, text
    norm = text.replace('\r\n', '\n')
    head, _, body = norm.partition('\n\n')
    rem = None
    for line in head.split('\n')[1:]:
        k, _, v = line.partition(':')
        if k.strip().lower() == 'x-ratelimit-remaining':
            try:
                rem = int(v.strip())
            except ValueError:
                pass
        if k.strip().lower() == 'retry-after':
            try:
                rem_retry = int(v.strip())
                globals()['_LAST_RETRY_AFTER'] = rem_retry
            except ValueError:
                pass
    return rem, body


_LAST_RETRY_AFTER = 0


def _json(res, what):
    body = res.stdout or ''
    rem, body = _headers_remaining(body)
    globals()['_LAST_REMAINING'] = rem
    try:
        data = json.loads(body or 'null')
    except ValueError:
        data = None
    if res.returncode != 0 or (isinstance(data, dict) and data.get('message') and not data.get('id')):
        msg = (res.stderr or body).strip()
        if isinstance(data, dict):
            msg = data.get('message') or msg
        status = 0
        m = re.search(r'HTTP[/ ]\S*\s*(\d{3})', (res.stdout or '') + '\n' + (res.stderr or ''))
        if m:
            status = int(m.group(1))
        low = (msg or '').lower()
        kind = 'rate' if status in (403, 429) or 'rate limit' in low or 'secondary rate' in low else 'http'
        raise GHError(409, f'{what}失败（{status or res.returncode}）：{msg[:240]}', kind, status, _LAST_RETRY_AFTER)
    if data is None:
        raise GHError(409, f'{what}返回的不是 JSON：{(res.stdout or res.stderr).strip()[:200]}', 'json')
    return data


_LAST_REMAINING = None


def remaining():
    return _LAST_REMAINING


# ---------- 评论 ----------

def list_comments(repo, since=None, per_page=100, max_pages=50, include_headers=True):
    """按 updated_at 升序返回某个 repo 的 Issue 评论（含 PR 评论，页面按 Issue 号过滤）。翻完所有页。"""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise GHError(400, f'repo 不合法：{repo!r}', 'bad_repo')
    page, out = 1, []
    while page <= max_pages:
        q = f'repos/{repo}/issues/comments?per_page={per_page}&page={page}'
        if since:
            q += f'&since={quote(since)}'
        argv = [gh_bin(), 'api', *( ['--include'] if include_headers else []), q]
        data = _json(_run(argv), f'读取 {repo} 评论')
        if not isinstance(data, list):
            raise GHError(409, f'{repo} 评论返回的不是列表', 'json')
        out += [c for c in data if isinstance(c, dict) and isinstance(c.get('id'), int)]
        if len(data) < per_page:
            break
        page += 1
    return out


def issue_comments(repo, number, per_page=100, max_pages=20):
    """某个 Issue（或 PR）的全部评论，正文现场读，不落盘。"""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise GHError(400, f'repo 不合法：{repo!r}', 'bad_repo')
    page, out = 1, []
    while page <= max_pages:
        argv = [gh_bin(), 'api', f'repos/{repo}/issues/{int(number)}/comments?per_page={per_page}&page={page}']
        data = _json(_run(argv), f'读取 {repo}#{number} 评论')
        if not isinstance(data, list):
            raise GHError(409, '评论返回的不是列表', 'json')
        out += [c for c in data if isinstance(c, dict) and isinstance(c.get('id'), int)]
        if len(data) < per_page:
            break
        page += 1
    return out


def create_comment(repo, number, body, tag=''):
    """在 Issue（或 PR）上发一条评论；正文走临时文件 + -F body=@，argv 调用。
    tag 是尾部隐藏关联标记，只用于服务端对账，不作为身份依据。"""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise GHError(400, f'repo 不合法：{repo!r}', 'bad_repo')
    text = body.rstrip('\n')
    if tag:
        text += f'\n\n{tag}'
    fd, path = tempfile.mkstemp(prefix='qi-comment-', suffix='.md')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        argv = [gh_bin(), 'api', '-X', 'POST', f'repos/{repo}/issues/{int(number)}/comments', '-F', f'body=@{path}']
        data = _json(_run(argv), '发评论')
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not isinstance(data, dict) or not isinstance(data.get('id'), int):
        raise GHError(409, '发评论成功但没拿到评论 id，结果不明', 'uncertain')
    return data


def find_tagged(repo, number, tag):
    """对账：在 Issue 评论里找尾标，找到返回评论对象，找不到 None。"""
    for c in issue_comments(repo, number):
        if tag and tag in (c.get('body') or ''):
            return c
    return None


# ---------- 写门（qstore 里的需求记录 + state 目录里的全局时间） ----------

def _write_state():
    return qstore.state_dir() / 'gh-write.json'


def _read_write_state():
    try:
        data = json.loads(_write_state().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_write_state(data):
    d = qstore.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f'.gh-write-{os.getpid()}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, _write_state())


def write_gate(nid):
    """发评论前的门：暂停、串行、全局间隔 ≥1 秒、每个需求 20 次/分钟。不通过抛 GHError(409)。
    通过后先记账（占掉这一格），发送失败也不会退格，避免重试风暴。"""
    if qstore.paused():
        raise GHError(409, '全局暂停中：只读，不发评论', 'paused')
    with WRITE_LOCK:
        st = _read_write_state()
        now = datetime.now(qstore.TZ)
        last = qstore.parse_time(st.get('last_at'))
        if last:
            wait = WRITE_INTERVAL - (now - last).total_seconds()
            if wait > 0:
                time.sleep(min(wait, WRITE_INTERVAL))
                now = datetime.now(qstore.TZ)
        per = st.get('needs') if isinstance(st.get('needs'), dict) else {}
        recent = [t for t in per.get(nid) or []
                  if (qstore.parse_time(t) or now) > now - timedelta(minutes=1)]
        if len(recent) >= PER_NEED_PER_MIN:
            raise GHError(429, f'{nid} 一分钟内已经发了 {len(recent)} 条评论，暂停一下再发', 'rate_need')
        per[nid] = (recent + [now.isoformat(timespec='seconds')])[-PER_NEED_PER_MIN:]
        st.update({'last_at': now.isoformat(timespec='seconds'), 'needs': per})
        _save_write_state(st)


# ---------- 看板：定向读一个需求的根 Issue 树 ----------

TREE_QUERY = '''query($owner:String!,$name:String!,$number:Int!){
 repository(owner:$owner,name:$name){
  issue(number:$number){
   number title url state stateReason closedAt createdAt updatedAt body
   labels(first:30){nodes{name}}
   subIssues(first:50){nodes{number title url state stateReason closedAt updatedAt body
     labels(first:30){nodes{name}}}}
   timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],last:50){nodes{
     ...on CrossReferencedEvent{source{...on PullRequest{number title url state isDraft mergedAt updatedAt
       reviewDecision statusCheckRollup{state} additions deletions repository{nameWithOwner}}}}}}
   closedByPullRequestsReferences(first:20,includeClosedPrs:true){nodes{number title url state isDraft mergedAt
     updatedAt reviewDecision statusCheckRollup{state} repository{nameWithOwner}}}
  }
 }
}'''


def issue_tree(url):
    """定向读根 Issue（不依赖「最近 100 个 open」）：根 + 子 Issue + 交叉引用 PR + 原生关联 PR。
    返回 {'root': {...}, 'subs': [...], 'prs': [...]}；查不到抛 GHError(409)。"""
    m = qstore.ISSUE_URL_RE.match(url or '')
    if not m:
        raise GHError(400, f'Issue 必须是完整 URL：{url!r}', 'bad_url')
    owner, name, number = m.group(1), m.group(2), int(m.group(3))
    argv = [gh_bin(), 'api', 'graphql', '-f', f'query={TREE_QUERY}', '-f', f'owner={owner}', '-f', f'name={name}',
            '-F', f'number={number}']
    data = _json(_run(argv), f'读取 {owner}/{name}#{number}')
    repo = (data.get('data') or {}).get('repository') if isinstance(data, dict) else None
    node = (repo or {}).get('issue')
    if not node:
        raise GHError(409, f'gh 找不到 {url}', 'not_found')
    prs, seen = [], set()

    def add_pr(pr):
        if not isinstance(pr, dict) or not isinstance(pr.get('number'), int):
            return
        key = (((pr.get('repository') or {}).get('nameWithOwner') or f'{owner}/{name}').lower(), pr['number'])
        if key in seen:
            return
        seen.add(key)
        prs.append({'number': pr['number'], 'title': pr.get('title') or '',
                    'url': pr.get('url') if str(pr.get('url') or '').startswith('https://github.com/') else '',
                    'state': pr.get('state') or '', 'draft': bool(pr.get('isDraft')),
                    'merged': pr.get('state') == 'MERGED', 'merged_at': pr.get('mergedAt') or '',
                    'updated': pr.get('updatedAt') or '',
                    'review': pr.get('reviewDecision') or '',
                    'checks': ((pr.get('statusCheckRollup') or {}).get('state') or ''),
                    'additions': pr.get('additions') or 0, 'deletions': pr.get('deletions') or 0,
                    'repo': key[0], 'full': (pr.get('repository') or {}).get('nameWithOwner') or f'{owner}/{name}'})

    for ev in ((node.get('timelineItems') or {}).get('nodes') or []):
        add_pr((ev or {}).get('source'))
    for pr in ((node.get('closedByPullRequestsReferences') or {}).get('nodes') or []):
        add_pr(pr)
    subs = [n for n in ((node.get('subIssues') or {}).get('nodes') or []) if n and n.get('number')]
    return {'root': _issue(node, f'{owner}/{name}'), 'subs': [_issue(n, f'{owner}/{name}') for n in subs], 'prs': prs}


def _issue(n, repo):
    return {'repo': repo.lower(), 'number': n.get('number'), 'title': n.get('title') or '',
            'url': n.get('url') if str(n.get('url') or '').startswith('https://github.com/') else '',
            'state': n.get('state') or 'OPEN', 'state_reason': n.get('stateReason') or '',
            'closed': n.get('closedAt') or '', 'created': n.get('createdAt') or '', 'updated': n.get('updatedAt') or '',
            'body': n.get('body') or '',
            'labels': [l['name'] for l in ((n.get('labels') or {}).get('nodes') or []) if l and l.get('name')]}
