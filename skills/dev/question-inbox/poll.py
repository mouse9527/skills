"""轮询根 / 子 Issue 的评论（不含 PR 讨论），映射到需求，记投递账，交给负责 lead。

- 按 repo `issues/comments?since=…&per_page=100` 翻完所有页，再按需求关联的 Issue 号过滤；
- 以本轮开始时间为保守水位，完整扫完并落盘后才推进，边界重叠 2 分钟，按 (id, updated_at, 正文哈希) 去重；
  一轮读不完就记下 (since, 页) 游标下一轮接着读（不用时间戳续扫，同秒的不漏不卡）；
- 并入按批短暂持锁、每批落盘一次；排投递按落盘状态做，进程中途退出后下一轮会补上；
- 评论正文不落盘：`need inbox` / 详情页现场从 GitHub 取；
- 编辑过的评论当「更新」再投一次（不是新的授权）；
- 子 Issue 从根的真实子树里发现（含跨 repo、多层），新发现的整段回填一次；
- 回声只认「我们自己发的内容又出现」（和对账同一个判定：完整尾标 + 正文哈希 + 我们的发布账号），记成自己的、不投回；
  连续 3 轮都是回声 → 标疑似循环、停止自动投递。团队引用尾标不算；
  不经 need comment 直接用 gh 发的评论（没有尾标、和 Owner 直写分不出）照常投递，这一条只靠 skill 的硬规定约束；
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
    """这条（不是 own、也不是我们自己发的）评论从哪来：unverified = Owner 的账号直接在 GitHub 写的；
    team-<账号> = 别人（含 @lead-<ws> 点名、引用我们的尾标——引用不改变来源）。"""
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


def _echo_of(rec, comment):
    """回声：这条评论按对账的同一判定（完整尾标 + 发布正文哈希 + 我们的发布账号）就是某个发送意图发出的内容，
    但不是那次记下的评论——我们自己发的内容又出现了一次。返回那个意图，否则 None。团队引用、复制都对不上账号，不算。"""
    cid = int(comment['id'])
    for it in (rec.get('send_intents') or {}).values():
        if int(it.get('comment_id') or 0) != cid and ghapi.matches_intent(comment, rec['id'], it):
            return it
    return None


def ingest(rec, comment, repo, number):
    """把一条评论并进需求记录（调用方持锁，不查 herdr、不做网络 IO）。返回来源（新的或编辑过时）或 ''：
    owner = 经 7788 发到 Issue 的 Owner 消息被编辑过；echo = 我们自己发的内容又出现（记成自己的，不投回）；
    其余见 _source。不在这里排投递：一批落盘后由 _settle 按落盘状态排。"""
    cid = int(comment['id'])
    own = qstore.own_comment(rec, cid)
    self_by = (own.get('by') or 'owner') if own else ''
    echo = None if own else _echo_of(rec, comment)
    if echo:
        qstore.add_own_comment(rec, cid, echo.get('by') or 'owner', issue=echo.get('issue') or '')
        self_by = 'echo'
    is_new = qstore.upsert_comment(rec, _row(comment, repo, number, self_by))
    if echo:
        return 'echo' if is_new else ''
    if self_by and self_by != 'owner':
        return ''  # lead 自己发的：不投回给它
    if not is_new:
        return ''
    if self_by == 'owner':
        return 'owner'
    return _source(rec, comment)


def _reconcile_intents(rec, comment, repo, number):
    """后台对账：结果不明 / 发送中的意图，只认**目标 Issue** 上 ghapi.matches_intent 对上的评论。
    对上了就和主动发送一样补记账并排投递（send.complete_intent）。调用方持锁、负责落盘。"""
    import send
    for rid, it in (rec.get('send_intents') or {}).items():
        if it.get('state') not in ('sending', 'uncertain'):
            continue
        m = qstore.ISSUE_URL_RE.match(it.get('issue') or '')
        if not m or (f'{m.group(1)}/{m.group(2)}'.lower(), int(m.group(3))) != (repo, number):
            continue
        if ghapi.matches_intent(comment, rec['id'], it):
            send.complete_intent(rec, rid, comment)
            rec['log'] = (rec.get('log') or []) + [{'at': qstore.now(), 'text': f'对账找到评论 {comment["id"]}（{rid} 已补记，和主动发送一样排投递）'}]
            return True
    return False


INGEST_BATCH = 200  # 一次持锁最多并入这么多条评论（落盘一次），然后放锁


def _apply(nid, items, sources):
    """把 [(评论, (repo, number))] 并进需求：网络 IO 都在调用方（锁外）做完；这里按批短暂持锁、每批只落盘一次，
    批与批之间放锁，前台的回复 / ack / 投递不用等整段回填。每批都重新读记录，不会用旧快照覆盖期间别人写的。"""
    for i in range(0, len(items), INGEST_BATCH):
        with qstore.locked():
            cur = qstore.read_need(nid)
            for c, key in items[i:i + INGEST_BATCH]:
                if _reconcile_intents(cur, c, key[0], key[1]):
                    continue
                src = ingest(cur, c, key[0], key[1])
                if src:
                    sources.setdefault(nid, []).append(src)
            qstore._save_need(cur)


def _settle(nid, srcs):
    """一轮扫完（也在每轮开头之后对所有需求跑一遍）：记循环轮次；没刹车时，**按落盘状态**决定要不要排唤醒——
    有待投递的评论、又没有正在进行 / 暂缓中的 lead 投递，就排一次。所以进程在「评论已落盘、还没排投递」时退出，
    下一轮也会补上。返回是否排了投递。"""
    with qstore.locked():
        rec = qstore.read_need(nid)
        if srcs and qstore.note_round(rec, srcs) or rec.get('loop_suspect'):
            n = 0
            for c in rec['comments']:
                if c.get('state') == 'pending':
                    c['state'] = 'held'
                    n += 1
            if n or srcs:
                rec['last_error'] = '疑似循环：连续 3 轮新评论都是我们自己发的内容又出现（对账判定为我们的发送），已停止自动投递'
                qstore._save_need(rec, f'疑似循环：{n} 条评论只记下，不自动投递')
            return False
        busy = rec.get('delivery_to') == 'lead' and rec.get('delivery') in ('pending', 'held', 'sending', 'uncertain')
        if not rec.get('lead') or busy or not any(c.get('state') == 'pending' for c in rec['comments']):
            if srcs:
                qstore._save_need(rec)
            return False
        qstore._queue_need(rec, 'comments')
        qstore._save_need(rec, f'有 {sum(c.get("state") == "pending" for c in rec["comments"])} 条待投递的 Issue 评论，排投递')
        return True


def scan(force=False):
    """扫一轮。返回 {repo: {'ok','complete','new','error','at','remaining'}}；失败保留上次水位 / 游标不推进。"""
    if not SCAN_LOCK.acquire(blocking=False):
        return {'busy': True}
    try:
        return _scan(force)
    finally:
        SCAN_LOCK.release()


def _backfill(needs, trees, sources):
    """新发现的子 Issue（GitHub 上后挂进来的、没逐一 need link 的）：整段读一次它的评论，不靠 repo 水位
    （水位早已越过它的旧评论）。读评论在锁外，并入按批短暂持锁；全部并入成功才记进 watch，下一轮不再整段读。"""
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
            _apply(rec['id'], [(c, (repo, number)) for c in items], sources)
            with qstore.locked():
                cur = qstore.read_need(rec['id'])
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
        cursor = ent.get('cursor') if isinstance(ent.get('cursor'), dict) else None
        if cursor:  # 上一轮没读完：同一个 since 从记下的页接着读，多读前一页吸收翻页期间的增删位移
            q_since, page, started = cursor.get('since'), int(cursor.get('page') or 1), cursor.get('started')
            start, pages = max(1, page - 1), ghapi.LIST_MAX_PAGES + (1 if page > 1 else 0)
        else:
            since = ent.get('since') or (now - timedelta(days=1)).isoformat(timespec='seconds')
            q_since = ((qstore.parse_time(since) or now) - timedelta(minutes=2)).isoformat(timespec='seconds')
            start, pages, started = 1, ghapi.LIST_MAX_PAGES, now.isoformat(timespec='seconds')
        try:
            comments, complete, nxt = ghapi.list_comments(repo, q_since, start_page=start, max_pages=pages)
        except ghapi.GHError as e:
            ent.update({'error': e.message, 'kind': e.kind, 'at': ent.get('at') or '',
                        'tried': now.isoformat(timespec='seconds')})
            st[repo] = ent
            out[repo] = {'ok': False, 'error': e.message, 'kind': e.kind, 'at': ent.get('at') or ''}
            continue
        groups = {}
        for c in comments:
            key = _issue_of(c)
            if key and key in idx:
                for rec in idx[key]:
                    groups.setdefault(rec['id'], []).append((c, key))
        new = 0
        for nid, items in groups.items():
            before = len(sources.get(nid) or [])
            _apply(nid, items, sources)
            new += len(sources.get(nid) or []) - before
        if complete:  # 整段读完：水位推进到这一串续扫的第一轮开始时间（期间的新改动下一轮带 2 分钟重叠再读）
            ent['since'] = started
            ent.pop('cursor', None)
        else:
            ent['cursor'] = {'since': q_since, 'page': nxt, 'started': started}
        ent.update({'at': now.isoformat(timespec='seconds'),
                    'error': '' if complete else f'评论太多，这一轮读到第 {nxt - 1} 页，下一轮接着读', 'kind': '' if complete else 'partial',
                    'tried': now.isoformat(timespec='seconds'), 'remaining': ghapi.remaining()})
        st[repo] = ent
        out[repo] = {'ok': True, 'complete': complete, 'new': new, 'at': ent['at'], 'remaining': ghapi.remaining(),
                     'needs': sorted(groups)}
    errs = _backfill(needs, trees, sources)
    if errs:
        out['_backfill'] = errs
    for rec in needs:  # 按落盘状态补排：上一轮中途退出时已落盘、没排投递的评论在这里补上
        if sources.get(rec['id']) or (rec.get('lead') and rec.get('status') in qstore.LEAD_STATES):
            _settle(rec['id'], sources.get(rec['id']) or [])
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
