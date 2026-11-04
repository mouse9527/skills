"""轮询根 / 子 Issue 的评论（不含 PR 讨论），映射到需求，记投递账，交给负责 lead。

- 按 repo `issues/comments?since=…&per_page=100` 翻完所有页，再按需求关联的 Issue 号过滤；
- 以本轮开始时间为保守水位，完整扫完并落盘后才推进，边界重叠 2 分钟，按 (id, updated_at, 正文哈希) 去重；
- 评论正文不落盘：`need inbox` / 详情页现场从 GitHub 取；
- 编辑过的评论当「更新」再投一次（不是新的授权）；
- 同一需求连续 3 次投递都来自「未核实 / 同一负责 lead」→ 停止自动投递，标「疑似循环」。
"""
import json
import re
import threading
from datetime import datetime, timedelta

import ghapi
import qstore

STATE = 'comments.json'
SCAN_LOCK = threading.Lock()
BODY_CACHE = {}
BODY_TTL = 60
ISSUE_API_RE = re.compile(r'/repos/([^/]+/[^/]+)/issues/(\d+)(?:$|[?#])')


def _state_path():
    return qstore.state_dir() / STATE


def _read_state():
    try:
        data = json.loads(_state_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data):
    d = qstore.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f'.comments-{threading.get_ident()}.tmp'
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    tmp.replace(_state_path())


def scan_state():
    return _read_state()


def _need_index(needs):
    """{(repo, number): [need 记录]}；根和子 Issue 都算。"""
    idx = {}
    for rec in needs:
        for repo, number, _ in qstore.need_issue_refs(rec):
            idx.setdefault((repo, number), []).append(rec)
    return idx


def _issue_of(comment):
    for key in ('issue_url', 'html_url'):
        m = ISSUE_API_RE.search(comment.get(key) or '') if key == 'issue_url' else \
            re.search(r'https://github\.com/([^/]+/[^/]+)/issues/(\d+)', comment.get(key) or '')
        if m:
            return m.group(1).lower(), int(m.group(2))
    return None


def _trigger(rec, comment):
    author = ((comment.get('user') or {}).get('login') or '').lower()
    owner = qstore.owner_login()
    body = comment.get('body') or ''
    if owner and author == owner:
        return 'unverified'
    if f'@lead-{rec.get("ws")}' in body:
        return f'lead-{rec.get("ws")}'
    return f'team-{author or "?"}'


def _deliver(rec, nid, repo_entry):
    """按负责 lead 在不在决定投给谁；返回 'lead' / 'router' / ''。"""
    lead = rec.get('lead') or {}
    if not lead:
        qstore._queue_need(rec, 'takeover')
        return 'router'
    st, _, _ = qstore.presence(lead.get('herdr_session'), lead.get('lead_session'))
    if st == 'absent':
        qstore._queue_need(rec, 'takeover')
        return 'router'
    qstore._queue_need(rec, 'comments')
    return 'lead'


def ingest(rec, comment, repo, number):
    """把一条评论并进需求记录（调用方持锁）。返回需要投递的原因或 ''。"""
    if rec.get('loop_suspect'):
        qstore.upsert_comment(rec, {'id': int(comment['id']), 'repo': repo, 'number': number,
                                    'author': ((comment.get('user') or {}).get('login') or ''),
                                    'created': comment.get('created_at') or qstore.now(),
                                    'updated': comment.get('updated_at') or comment.get('created_at') or qstore.now(),
                                    'hash': qstore._hash((comment.get('updated_at') or '') + (comment.get('body') or '')),
                                    'self': ''})
        for c in rec['comments']:
            if c.get('state') == 'pending':
                c['state'] = 'held'
        qstore._save_need(rec, '疑似循环未解除：新评论只记下，不自动投递')
        return ''
    cid = int(comment['id'])
    author = ((comment.get('user') or {}).get('login') or '')
    created = comment.get('created_at') or qstore.now()
    updated = comment.get('updated_at') or created
    body = comment.get('body') or ''
    self_by = ''
    own = qstore.own_comment(rec, cid)
    if own:
        self_by = own.get('by') or 'owner'
    is_new = qstore.upsert_comment(rec, {'id': cid, 'repo': repo, 'number': number, 'author': author,
                                         'created': created, 'updated': updated,
                                         'hash': qstore._hash(updated + body), 'self': self_by})
    if self_by or not is_new:
        return ''
    src = _trigger(rec, comment)
    if qstore.note_trigger(rec, src):
        for c in rec['comments']:
            if c.get('state') == 'pending':
                c['state'] = 'held'
        rec['last_error'] = '疑似循环：连续 3 次投递都来自未核实来源 / 同一 lead，已停止自动投递'
        qstore._save_need(rec, '疑似循环，停止自动投递')
        return ''
    return _deliver(rec, rec['id'], None)


def _reconcile_intents(rec, comment, repo, number):
    body = comment.get('body') or ''
    for rid, it in (rec.get('send_intents') or {}).items():
        if it.get('state') in ('sending', 'uncertain') and (f'msg-{rid}' in body):
            qstore.intent_done(rec, rid, comment['id'])
            qstore.add_own_comment(rec, comment['id'], 'owner', issue=(qstore.root_issue(rec) or ('', 0, ''))[2])
            qstore.upsert_comment(rec, {'id': int(comment['id']), 'repo': repo, 'number': number,
                                        'author': ((comment.get('user') or {}).get('login') or ''),
                                        'created': comment.get('created_at') or qstore.now(),
                                        'updated': comment.get('updated_at') or qstore.now(),
                                        'hash': qstore._hash(comment.get('body') or ''), 'self': 'owner'})
            qstore._save_need(rec, f'对账找到评论 {comment["id"]}（request_id 已补记）')
            return True
    return False


def scan(force=False):
    """扫一轮。返回 {repo: {'ok','new','error','at','remaining'}}；失败保留上次水位不推进。"""
    if not SCAN_LOCK.acquire(blocking=False):
        return {'busy': True}
    try:
        return _scan(force)
    finally:
        SCAN_LOCK.release()


def _scan(force):
    st = _read_state()
    needs = list(qstore.all_needs().values())
    idx = _need_index(needs)
    repos = sorted({repo for repo, _ in idx})
    out = {}
    for repo in repos:
        ent = st.get(repo) if isinstance(st.get(repo), dict) else {}
        now = datetime.now(qstore.TZ)
        since = ent.get('since') or (now - timedelta(days=1)).isoformat(timespec='seconds')
        overlap = (qstore.parse_time(since) or now) - timedelta(minutes=2)
        try:
            comments = ghapi.list_comments(repo, overlap.isoformat(timespec='seconds'))
        except ghapi.GHError as e:
            ent.update({'error': e.message, 'kind': e.kind, 'at': ent.get('at') or '',
                        'tried': now.isoformat(timespec='seconds')})
            st[repo] = ent
            out[repo] = {'ok': False, 'error': e.message, 'kind': e.kind, 'at': ent.get('at') or ''}
            continue
        touched, new = set(), 0
        for c in comments:
            key = _issue_of(c)
            if not key or key not in idx:
                continue
            for rec in idx[key]:
                with qstore.locked():
                    cur = qstore.read_need(rec['id'])
                    if _reconcile_intents(cur, c, key[0], key[1]):
                        touched.add(rec['id'])
                        qstore._save_need(cur, '对账完成')
                        continue
                    why = ingest(cur, c, key[0], key[1])
                    if why:
                        new += 1
                        qstore._save_need(cur, f'轮询到 Issue 评论 {c["id"]}，投给 {why}')
                touched.add(rec['id'])
        ent.update({'since': now.isoformat(timespec='seconds'), 'at': now.isoformat(timespec='seconds'),
                    'error': '', 'kind': '', 'tried': now.isoformat(timespec='seconds'),
                    'remaining': ghapi.remaining()})
        st[repo] = ent
        out[repo] = {'ok': True, 'new': new, 'at': ent['at'], 'remaining': ghapi.remaining(), 'needs': sorted(touched)}
    _write_state(st)
    return out


def issue_bodies(repo, number, force=False):
    """某个 Issue 的评论列表（含正文，内存缓存 60 秒）；详情页时间线用，不落盘。"""
    key = (repo, int(number))
    now = datetime.now(qstore.TZ)
    hit = BODY_CACHE.get(key)
    if hit and not force and (now - hit[0]).total_seconds() < BODY_TTL:
        return hit[1]
    items = ghapi.issue_comments(repo, number)
    BODY_CACHE[key] = (now, items)
    return items


def seed_watermark(url):
    """第一次 link：把 repo 的评论水位设成根 Issue 的创建时间，回填根 Issue 的全部评论。
    读不到创建时间（gh 失败 / 字段缺失）就维持原水位，不改。"""
    m = qstore.ISSUE_URL_RE.match(url or '')
    if not m:
        return None
    repo = f'{m.group(1)}/{m.group(2)}'.lower()
    tree = ghapi.issue_tree(url)
    created = ((tree or {}).get('root') or {}).get('created') or ''
    if not created:
        return None
    st = _read_state()
    ent = st.get(repo) if isinstance(st.get(repo), dict) else {}
    since = qstore.parse_time(ent.get('since'))
    made = qstore.parse_time(created.replace('Z', '+00:00'))
    if made and (not since or since > made):
        ent['since'] = made.astimezone(qstore.TZ).isoformat(timespec='seconds')
        st[repo] = ent
        _write_state(st)
    return st.get(repo, {}).get('since')


def body_fetcher(repo, number, comment_id):
    """现场取一条评论的正文（内存缓存 60 秒）；不落盘。"""
    items = issue_bodies(repo, number)
    for c in items:
        if int(c.get('id') or 0) == int(comment_id):
            return c.get('body') or ''
    raise qstore.QError(404, f'{repo}#{number} 上找不到评论 {comment_id}')


def degrade():
    """读最近一轮的 remaining，<1000 就降档（返回 True）。"""
    st = _read_state()
    rems = [v.get('remaining') for v in st.values() if isinstance(v, dict) and isinstance(v.get('remaining'), int)]
    return bool(rems) and min(rems) < 1000


def clear_cache():
    BODY_CACHE.clear()
