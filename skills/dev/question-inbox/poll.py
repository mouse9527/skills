"""轮询根 / 子 Issue 的评论（不含 PR 讨论），映射到需求，记投递账，交给负责 lead。

- 按 repo `issues/comments?since=…&per_page=100` 翻完所有页，再按需求关联的 Issue 号过滤；
- 以本轮开始时间为保守水位，完整扫完并落盘后才推进，边界重叠 2 分钟，按 (id, updated_at, 正文哈希) 去重；
- 评论正文不落盘：`need inbox` / 详情页现场从 GitHub 取；
- 编辑过的评论当「更新」再投一次（不是新的授权）；
- 子 Issue 从根的真实子树里发现（含跨 repo、多层），新发现的整段回填一次；
- 疑似循环只认「我们自己发的评论被投回」（带本需求尾标、对不上发送记录），连续 3 轮 → 停止自动投递；
- 查 herdr 不在这里做（不持锁调 herdr）：只记账、排投递，投给谁由服务的投递循环判断。
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


def _watch_refs(rec, tree=None):
    """这个需求要轮询的 Issue：记录里的根 / 子 Issue，加上从根 Issue 的真实子树里发现的子 Issue（含跨 repo、多层）。"""
    out = [(repo, number) for repo, number, _ in qstore.need_issue_refs(rec)]
    for sub in (tree or {}).get('subs') or []:
        if sub.get('repo') and isinstance(sub.get('number'), int):
            out.append((sub['repo'].lower(), sub['number']))
    return list(dict.fromkeys(out))


def _tree_of(rec):
    if rec.get('status') != 'filed' or not qstore.root_issue(rec):
        return None
    import board
    tree, _ = board._tree(qstore.root_issue(rec)[2])
    return tree


def _need_index(needs, trees=None):
    """{(repo, number): [need 记录]}；根和（发现的）子 Issue 都算。"""
    idx = {}
    for rec in needs:
        for key in _watch_refs(rec, (trees or {}).get(rec['id'])):
            idx.setdefault(key, []).append(rec)
    return idx


def _issue_of(comment):
    for key in ('issue_url', 'html_url'):
        m = ISSUE_API_RE.search(comment.get(key) or '') if key == 'issue_url' else \
            re.search(r'https://github\.com/([^/]+/[^/]+)/issues/(\d+)', comment.get(key) or '')
        if m:
            return m.group(1).lower(), int(m.group(2))
    return None


def _source(rec, comment):
    """这条（不是 own、也对不上发送意图的）评论从哪来：
    echo：正文带我们给这个需求打的尾标——我们自己发的评论被当成新消息投回（循环迹象）；
    unverified：Owner 的账号直接在 GitHub 写的；team-<账号>：别人（含 @lead-<ws> 点名，点名不改变来源）。"""
    body = comment.get('body') or ''
    nid = rec['id']
    if f'qi:{nid} ' in body or f'qi:need-{nid} ' in body:
        return 'echo'
    author = ((comment.get('user') or {}).get('login') or '').lower()
    owner = qstore.owner_login()
    if owner and author == owner:
        return 'unverified'
    return f'team-{author or "?"}'


def _row(comment, repo, number, self_by=''):
    created = comment.get('created_at') or qstore.now()
    updated = comment.get('updated_at') or created
    return {'id': int(comment['id']), 'repo': repo, 'number': number,
            'author': ((comment.get('user') or {}).get('login') or ''), 'created': created, 'updated': updated,
            'hash': qstore._hash(updated + (comment.get('body') or '')), 'self': self_by}


def ingest(rec, comment, repo, number):
    """把一条评论并进需求记录（调用方持锁，不查 herdr）。返回来源（见 _source；新的或编辑过、需要投给 lead 时）或 ''。
    不在这里排投递：一轮扫完后按需求统一记循环轮次、排一次唤醒。"""
    own = qstore.own_comment(rec, int(comment['id']))
    self_by = (own.get('by') or 'owner') if own else ''
    is_new = qstore.upsert_comment(rec, _row(comment, repo, number, self_by))
    if self_by and self_by != 'owner':
        return ''  # lead 自己发的：不投回给它
    if not is_new:
        return ''
    if self_by == 'owner':
        return 'owner'  # 经 7788 发到 Issue 的 Owner 消息（被编辑过才会到这里）
    return _source(rec, comment)


def _reconcile_intents(rec, comment, repo, number):
    """后台对账：结果不明 / 发送中的意图，只认**目标 Issue** 上完整尾标 + 发布正文哈希 + 可信账号都对上的评论。
    对上了就和主动发送一样补记账并排投递（send.complete_intent）。"""
    import send
    for rid, it in (rec.get('send_intents') or {}).items():
        if it.get('state') not in ('sending', 'uncertain'):
            continue
        m = qstore.ISSUE_URL_RE.match(it.get('issue') or '')
        if not m or (f'{m.group(1)}/{m.group(2)}'.lower(), int(m.group(3))) != (repo, number):
            continue
        if ghapi.matches_intent(comment, rec['id'], it):
            send.complete_intent(rec, rid, comment)
            qstore._save_need(rec, f'对账找到评论 {comment["id"]}（{rid} 已补记，和主动发送一样排投递）')
            return True
    return False


def _take(cur, c, key, sources):
    if _reconcile_intents(cur, c, key[0], key[1]):
        return
    src = ingest(cur, c, key[0], key[1])
    if src:
        sources.setdefault(cur['id'], []).append(src)
    qstore._save_need(cur)


def _settle(nid, srcs):
    """一轮扫完：记循环轮次；没刹车就给负责 lead 排**一次**唤醒（多条合并，提示词按未通知的消息现算）。"""
    with qstore.locked():
        rec = qstore.read_need(nid)
        if rec.get('loop_suspect') or qstore.note_round(rec, srcs):
            n = 0
            for c in rec['comments']:
                if c.get('state') == 'pending':
                    c['state'] = 'held'
                    n += 1
            rec['last_error'] = '疑似循环：连续 3 轮新评论都是我们自己发的评论被投回（带本需求尾标、对不上发送记录），已停止自动投递'
            qstore._save_need(rec, f'疑似循环：{n} 条评论只记下，不自动投递')
            return False
        qstore._queue_need(rec, 'comments')
        qstore._save_need(rec, f'轮询到 {len(srcs)} 条新的 / 编辑过的 Issue 评论，排投递')
        return True


def scan(force=False):
    """扫一轮。返回 {repo: {'ok','new','error','at','remaining'}}；失败保留上次水位不推进。"""
    if not SCAN_LOCK.acquire(blocking=False):
        return {'busy': True}
    try:
        return _scan(force)
    finally:
        SCAN_LOCK.release()


def _backfill(needs, trees, sources):
    """新发现的子 Issue（GitHub 上后挂进来的、没逐一 need link 的）：整段读一次它的评论，不靠 repo 水位
    （水位早已越过它的旧评论）。读成功才记进 watch，下一轮不再整段读。"""
    out = {}
    for rec in needs:
        if rec.get('status') != 'filed':
            continue
        refs = _watch_refs(rec, trees.get(rec['id']))
        root = qstore.root_issue(rec)
        seen = set(rec.get('watch') or []) | ({f'{root[0]}#{root[1]}'} if root else set())
        for repo, number in refs:
            key = f'{repo}#{number}'
            if key in seen:
                continue
            try:
                items = ghapi.issue_comments(repo, number)
            except ghapi.GHError as e:
                out[key] = e.message
                continue
            with qstore.locked():
                cur = qstore.read_need(rec['id'])
                for c in items:
                    _take(cur, c, (repo, number), sources)
                cur['watch'] = list(dict.fromkeys((cur.get('watch') or []) + [key]))
                qstore._save_need(cur, f'开始轮询子 Issue {key}（回填 {len(items)} 条评论）')
    return out


def _scan(force):
    st = _read_state()
    needs = list(qstore.all_needs().values())
    trees = {}
    for rec in needs:
        try:
            trees[rec['id']] = _tree_of(rec)
        except qstore.QError:
            trees[rec['id']] = None
    idx = _need_index(needs, trees)
    repos = sorted({repo for repo, _ in idx})
    out, sources = {}, {}
    for repo in repos:
        ent = st.get(repo) if isinstance(st.get(repo), dict) else {}
        now = datetime.now(qstore.TZ)
        since = ent.get('since') or (now - timedelta(days=1)).isoformat(timespec='seconds')
        # 上一轮没读完时水位是 GitHub 上最后读到那条的时间（没有时钟偏差），不再回退 2 分钟，否则页数上限内永远读不到后面
        overlap = (qstore.parse_time(since) or now) - (timedelta() if ent.get('kind') == 'partial' else timedelta(minutes=2))
        try:
            comments, complete = ghapi.list_comments(repo, overlap.isoformat(timespec='seconds'))
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
                    before = len(sources.get(rec['id']) or [])
                    _take(cur, c, key, sources)
                    new += len(sources.get(rec['id']) or []) - before
                touched.add(rec['id'])
        if complete:
            mark = now.isoformat(timespec='seconds')
        else:  # 没读完（到了页数上限）：只推进到读到的最后一条，下一轮从那里接着读，不跳过
            last = max((qstore.parse_time((c.get('updated_at') or '').replace('Z', '+00:00')) for c in comments
                        if c.get('updated_at')), default=None)
            mark = last.astimezone(qstore.TZ).isoformat(timespec='seconds') if last else ent.get('since') or since
        ent.update({'since': mark, 'at': now.isoformat(timespec='seconds'),
                    'error': '' if complete else f'评论太多，这一轮只读到 {mark}，下一轮接着读', 'kind': '' if complete else 'partial',
                    'tried': now.isoformat(timespec='seconds'), 'remaining': ghapi.remaining()})
        st[repo] = ent
        out[repo] = {'ok': True, 'complete': complete, 'new': new, 'at': ent['at'], 'remaining': ghapi.remaining(),
                     'needs': sorted(touched)}
    errs = _backfill(needs, trees, sources)
    if errs:
        out['_backfill'] = errs
    for nid, srcs in sources.items():
        _settle(nid, srcs)
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
    """现场按 id 取一条评论的正文（不受 Issue 评论条数 / 分页影响）；不落盘。去掉我们的隐藏尾标。"""
    return ghapi.strip_tag(ghapi.get_comment(repo, comment_id).get('body') or '')


def degrade():
    """读最近一轮的 remaining，<1000 就降档（返回 True）。"""
    st = _read_state()
    rems = [v.get('remaining') for v in st.values() if isinstance(v, dict) and isinstance(v.get('remaining'), int)]
    return bool(rems) and min(rems) < 1000


def clear_cache():
    BODY_CACHE.clear()
