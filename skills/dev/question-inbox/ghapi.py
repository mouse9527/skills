"""GitHub 读写（只读任务/看板 + 根 Issue 评论），gh 一律 argv 调用，不经过 shell。

评论写入集中在这里，只能在 writing() 里写：跨进程串行、间隔 ≥1 秒、每个需求 20 次/分钟，暂停复核包住实际写动作。
错误分两类（definitely_not_sent）：确定没发出去 / 可能已经发出去（结果不明，先对账）。
返回和错误都用 qstore.QError（409 表示拒绝 / 暂缓，页面保留输入）。只用标准库。
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote
from datetime import datetime, timedelta

import qstore

# 尾标：<!-- qi:<需求 id> i-<意图 id> -->。意图 id 是服务端生成的随机串，只出现在我们自己发出的那条评论里
TAG_RE = re.compile(r'<!--\s*qi:(need-\d{4,})\s+i-([0-9a-f]{16,64})\s*-->')
ANY_TAG_RE = re.compile(r'\n*<!--\s*qi:[^>]{0,120}-->\s*$')  # 渲染时去掉（含旧格式 qi:need-need-0001 msg-…）
WRITE_INTERVAL = float(os.environ.get('QI_GH_WRITE_INTERVAL') or 1.0)
PER_NEED_PER_MIN = 20
MAX_COMMENT = 10000  # 发到 Issue 的正文上限（设计 §2）；7788 线程仍是 qstore.MAX_TEXT
LIST_MAX_PAGES = 50  # 一轮按 repo 最多读这么多页；没读完就只把水位推进到读到的最后一条
ISSUE_MAX_PAGES = 200
TREE_MAX_NODES = 300


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

def _check_repo(repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
        raise GHError(400, f'repo 不合法：{repo!r}', 'bad_repo')


def list_comments(repo, since=None, per_page=100, max_pages=None, include_headers=True):
    """某个 repo 的 Issue 评论（含 PR 评论，调用方按 Issue 号过滤），按 updated_at 升序。
    返回 (评论列表, 是否读完)。没读完（到了页数上限）时调用方只能把水位推进到读到的最后一条，不能当成扫完。"""
    _check_repo(repo)
    max_pages = max_pages or LIST_MAX_PAGES
    page, out = 1, []
    while page <= max_pages:
        q = f'repos/{repo}/issues/comments?sort=updated&direction=asc&per_page={per_page}&page={page}'
        if since:
            q += f'&since={quote(since)}'
        argv = [gh_bin(), 'api', *( ['--include'] if include_headers else []), q]
        data = _json(_run(argv), f'读取 {repo} 评论')
        if not isinstance(data, list):
            raise GHError(409, f'{repo} 评论返回的不是列表', 'json')
        out += [c for c in data if isinstance(c, dict) and isinstance(c.get('id'), int)]
        if len(data) < per_page:
            return out, True
        page += 1
    return out, False


def issue_comments(repo, number, per_page=100, max_pages=None):
    """某个 Issue（或 PR）的全部评论，正文现场读，不落盘。读不完（超过页数上限）抛 GHError(truncated)，不返回半截。"""
    _check_repo(repo)
    max_pages = max_pages or ISSUE_MAX_PAGES
    page, out = 1, []
    while page <= max_pages:
        argv = [gh_bin(), 'api', f'repos/{repo}/issues/{int(number)}/comments?per_page={per_page}&page={page}']
        data = _json(_run(argv), f'读取 {repo}#{number} 评论')
        if not isinstance(data, list):
            raise GHError(409, '评论返回的不是列表', 'json')
        out += [c for c in data if isinstance(c, dict) and isinstance(c.get('id'), int)]
        if len(data) < per_page:
            return out
        page += 1
    raise GHError(409, f'{repo}#{number} 的评论超过 {max_pages * per_page} 条，没有读完', 'truncated')


def get_comment(repo, comment_id):
    """按 id 读一条评论（不受分页影响）；不存在抛 GHError(404)。"""
    _check_repo(repo)
    res = _run([gh_bin(), 'api', f'repos/{repo}/issues/comments/{int(comment_id)}'])
    data = _json(res, f'读取评论 {comment_id}')
    if not isinstance(data, dict) or not isinstance(data.get('id'), int):
        raise GHError(404, f'{repo} 上找不到评论 {comment_id}', 'not_found', 404)
    return data


def norm_body(text):
    return (text or '').replace('\r\n', '\n').rstrip()


def tag(nid, tag_id):
    return f'<!-- qi:{nid} i-{tag_id} -->'


def posted_text(body, tag_text):
    return body.rstrip('\n') + (f'\n\n{tag_text}' if tag_text else '')


def strip_tag(text):
    return ANY_TAG_RE.sub('', text or '')


def create_comment(repo, number, body, tag_text=''):
    """在 Issue（或 PR）上发一条评论；正文走临时文件 + -F body=@，argv 调用。只在 writing() 里调用。
    tag_text 是尾部隐藏关联标记，只用于对账，单独不作为身份依据。"""
    _check_repo(repo)
    text = posted_text(body, tag_text)
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
    login = ((data.get('user') or {}).get('login') or '').lower()
    if login:
        _note_poster(login)
    return data


def definitely_not_sent(e):
    """True：这次写**确定没发出去**（写门拒绝、gh 起不来、参数不合法、GitHub 明确的 4xx 拒绝）。
    False：可能已经发出去（超时、5xx、回包丢失 / 解析不了、没有状态码的失败）→ 结果不明，先对账。"""
    if e.kind in ('paused', 'rate_need', 'exec', 'bad_repo', 'too_long'):
        return True
    return 400 <= (e.status or 0) < 500 and e.status != 408


def trusted_authors():
    """对账时认可的发布账号：配置的 Owner 登录名 + 我们发评论时 GitHub 回包里的账号。"""
    out = {a for a in (qstore.owner_login(), _read_write_state().get('poster') or '') if a}
    return {a.lower() for a in out}


def matches_intent(comment, nid, intent):
    """一条评论是不是某个发送意图发出去的那条：完整尾标（含随机意图 id）+ 发布正文哈希 + 可信作者，三项都对上才算。"""
    body = comment.get('body') or ''
    tid = intent.get('tag_id') or ''
    if not tid or tag(nid, tid) not in body:
        return False
    if intent.get('posted_hash') and qstore._hash(norm_body(body)) != intent['posted_hash']:
        return False
    author = ((comment.get('user') or {}).get('login') or '').lower()
    trusted = trusted_authors()
    return not trusted or author in trusted


def find_intent(repo, number, nid, intent):
    """对账：到目标 Issue 上找这个意图发出去的评论。返回 (评论或 None, 是否完整读过)。"""
    try:
        items = issue_comments(repo, number)
    except GHError:
        return None, False
    return next((c for c in items if matches_intent(c, nid, intent)), None), True


# ---------- 写门：跨进程串行（state/gh-write.lock）、间隔 ≥1 秒、每个需求 20 次/分钟、暂停复核包住实际写动作 ----------

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
    tmp = d / f'.gh-write-{os.getpid()}-{threading.get_ident()}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, _write_state())


def _note_poster(login):
    """调用方已持写锁。"""
    st = _read_write_state()
    if st.get('poster') != login:
        st['poster'] = login
        _save_write_state(st)


@contextmanager
def _write_lock():
    d = qstore.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / 'gh-write.lock', 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def writing(nid):
    """发评论的门，用法：`with ghapi.writing(nid): ghapi.create_comment(...)`。
    1) 跨进程独占锁（服务和 CLI 共用 state/gh-write.lock），整个写动作串行；
    2) 距上一次写不到间隔就等；3) 每个需求 1 分钟最多 PER_NEED_PER_MIN 次（先占格，失败不退格，避免重试风暴）；
    4) 持 control.lock 共享锁**现查暂停**再写：set_paused 持独占锁，所以暂停提交之后不会再有新的写，正在写的那一次做完它才生效。
    不通过抛 GHError(409)，这时**确定没发**。"""
    if qstore.paused():
        raise GHError(409, '全局暂停中：只读，不发评论', 'paused')
    with _write_lock():
        st = _read_write_state()
        last = qstore.parse_time(st.get('last_at'))
        if last:
            wait = WRITE_INTERVAL - (datetime.now(qstore.TZ) - last).total_seconds()
            if wait > 0:
                time.sleep(min(wait, WRITE_INTERVAL))
        now = datetime.now(qstore.TZ)
        per = st.get('needs') if isinstance(st.get('needs'), dict) else {}
        recent = [t for t in per.get(nid) or [] if (qstore.parse_time(t) or now) > now - timedelta(minutes=1)]
        if len(recent) >= PER_NEED_PER_MIN:
            raise GHError(429, f'{nid} 一分钟内已经发了 {len(recent)} 条评论，暂停一下再发', 'rate_need')
        with qstore._control_lock(fcntl.LOCK_SH):
            if qstore.paused():
                raise GHError(409, '全局暂停中：只读，不发评论', 'paused')
            per[nid] = (recent + [now.isoformat(timespec='microseconds')])[-PER_NEED_PER_MIN:]
            st.update({'last_at': now.isoformat(timespec='microseconds'), 'needs': per})
            _save_write_state(st)
            yield


def write_gate(nid):
    """只过门、不写（测试和旧调用用）；真正写评论要用 writing()。"""
    with writing(nid):
        pass


# ---------- 看板：从根 Issue 出发定向读整棵树 ----------

NODE_FIELDS = ('number title url state stateReason closedAt createdAt updatedAt body repository{nameWithOwner} '
               'labels(first:30){nodes{name}}')
PR_FIELDS = ('number title url state isDraft mergedAt updatedAt reviewDecision statusCheckRollup{state} '
             'additions deletions repository{nameWithOwner}')
TREE_QUERY = ('query($owner:String!,$name:String!,$number:Int!,$after:String){\n'
              ' repository(owner:$owner,name:$name){\n'
              '  issue(number:$number){\n'
              f'   {NODE_FIELDS}\n'
              '   subIssues(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{number repository{nameWithOwner}}}\n'
              '   timelineItems(itemTypes:[CROSS_REFERENCED_EVENT],first:100){pageInfo{hasNextPage}\n'
              f'     nodes{{...on CrossReferencedEvent{{source{{...on PullRequest{{{PR_FIELDS}}}}}}}}}}}\n'
              f'   closedByPullRequestsReferences(first:50,includeClosedPrs:true){{pageInfo{{hasNextPage}} nodes{{{PR_FIELDS}}}}}\n'
              '  }\n }\n}')


def _issue_node(repo, number, after=''):
    owner, name = repo.split('/', 1)
    argv = [gh_bin(), 'api', 'graphql', '-f', f'query={TREE_QUERY}', '-f', f'owner={owner}', '-f', f'name={name}',
            '-F', f'number={int(number)}']
    if after:
        argv += ['-f', f'after={after}']
    data = _json(_run(argv), f'读取 {repo}#{number}')
    node = (((data.get('data') or {}).get('repository') or {}).get('issue')) if isinstance(data, dict) else None
    if not node:
        raise GHError(409, f'gh 找不到 {repo}#{number}', 'not_found')
    return node


def issue_tree(url):
    """定向读根 Issue（不依赖「最近 100 个 open」）：根 + **所有层级**的子 Issue（翻页）+ 每个 Issue 的交叉引用 PR 和原生关联 PR。
    子 Issue 保留自己的仓库。返回 {'root', 'subs': [...带 depth / parent], 'prs': [...带 issue], 'truncated': [说明]}；
    根查不到抛 GHError(409)；子 Issue 查不到不中断，记进 truncated。"""
    m = qstore.ISSUE_URL_RE.match(url or '')
    if not m:
        raise GHError(400, f'Issue 必须是完整 URL：{url!r}', 'bad_url')
    root_repo, number = f'{m.group(1)}/{m.group(2)}', int(m.group(3))
    prs, seen_pr, subs, truncated = [], set(), [], []

    def add_pr(pr, at):
        if not isinstance(pr, dict) or not isinstance(pr.get('number'), int):
            return
        full = (pr.get('repository') or {}).get('nameWithOwner') or at[0]
        key = (full.lower(), pr['number'])
        if key in seen_pr:
            return
        seen_pr.add(key)
        prs.append({'number': pr['number'], 'title': pr.get('title') or '',
                    'url': pr.get('url') if str(pr.get('url') or '').startswith('https://github.com/') else '',
                    'state': pr.get('state') or '', 'draft': bool(pr.get('isDraft')),
                    'merged': pr.get('state') == 'MERGED', 'merged_at': pr.get('mergedAt') or '',
                    'updated': pr.get('updatedAt') or '',
                    'review': pr.get('reviewDecision') or '',
                    'checks': ((pr.get('statusCheckRollup') or {}).get('state') or ''),
                    'additions': pr.get('additions') or 0, 'deletions': pr.get('deletions') or 0,
                    'repo': key[0], 'full': full, 'issue': f'{at[0]}#{at[1]}'})

    def read(repo, num):
        node = _issue_node(repo, num)
        kids = list(((node.get('subIssues') or {}).get('nodes') or []))
        page = (node.get('subIssues') or {}).get('pageInfo') or {}
        while page.get('hasNextPage') and page.get('endCursor'):
            more = _issue_node(repo, num, page['endCursor']).get('subIssues') or {}
            kids += more.get('nodes') or []
            page = more.get('pageInfo') or {}
        tl = node.get('timelineItems') or {}
        for ev in tl.get('nodes') or []:
            add_pr((ev or {}).get('source'), (repo, num))
        cb = node.get('closedByPullRequestsReferences') or {}
        for pr in cb.get('nodes') or []:
            add_pr(pr, (repo, num))
        if (tl.get('pageInfo') or {}).get('hasNextPage') or (cb.get('pageInfo') or {}).get('hasNextPage'):
            truncated.append(f'{repo}#{num} 关联的 PR 超过 100 个，只读了前 100 个')
        return node, kids

    node, kids = read(root_repo, number)
    root = _issue(node, root_repo)
    queue = [(k, root_repo, number, 1) for k in kids]
    seen = {(root['repo'], root['number'])}
    while queue:
        k, prepo, pnum, depth = queue.pop(0)
        repo = ((k or {}).get('repository') or {}).get('nameWithOwner') or prepo
        num = (k or {}).get('number')
        if not isinstance(num, int) or (repo.lower(), num) in seen:
            continue
        if len(seen) >= TREE_MAX_NODES:
            truncated.append(f'子 Issue 超过 {TREE_MAX_NODES - 1} 个，后面的没有读')
            break
        seen.add((repo.lower(), num))
        try:
            sub, grand = read(repo, num)
        except GHError as e:
            truncated.append(f'{repo}#{num} 读不到：{e.message}')
            subs.append({'repo': repo.lower(), 'number': num, 'title': '', 'url': '', 'state': 'UNKNOWN',
                         'state_reason': '', 'closed': '', 'created': '', 'updated': '', 'body': '', 'labels': [],
                         'depth': depth, 'parent': f'{prepo.lower()}#{pnum}'})
            continue
        item = _issue(sub, repo)
        item.update({'depth': depth, 'parent': f'{prepo.lower()}#{pnum}'})
        subs.append(item)
        queue += [(g, repo, num, depth + 1) for g in grand]
    return {'root': root, 'subs': subs, 'prs': prs, 'truncated': truncated}


def _issue(n, repo):
    repo = ((n.get('repository') or {}).get('nameWithOwner') or repo)
    return {'repo': repo.lower(), 'number': n.get('number'), 'title': n.get('title') or '',
            'url': n.get('url') if str(n.get('url') or '').startswith('https://github.com/') else '',
            'state': n.get('state') or 'OPEN', 'state_reason': n.get('stateReason') or '',
            'closed': n.get('closedAt') or '', 'created': n.get('createdAt') or '', 'updated': n.get('updatedAt') or '',
            'body': n.get('body') or '',
            'labels': [l['name'] for l in ((n.get('labels') or {}).get('nodes') or []) if l and l.get('name')]}
