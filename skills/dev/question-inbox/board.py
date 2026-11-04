"""需求看板：每个需求恰好落在一列（完成 / 待处理 / 卡住 / 待立项 / 审查中 / 进行中）。

判据只看 GitHub + 收件箱 + herdr 三态；不用时间阈值决定列，时间只做卡片说明。
根 Issue 定向读取（按号，不依赖「最近 100 个 open」），所以关闭超过 7 天也能打开。
"""
import json
import re
import threading
from datetime import datetime, timedelta

import ghapi
import qstore

# 显示顺序按线框：待处理 → 卡住 → 待立项 → 审查中 → 进行中 → 完成；判断优先级仍见 column()
COLUMNS = (('wait', '待处理'), ('stuck', '卡住'), ('todo', '待立项'),
           ('review', '审查中'), ('doing', '进行中'), ('done', '完成'))
TREE_TTL = 60
TREE = {}
TREE_LOCK = threading.Lock()
PRESENCE = {}


def column(need, tree, ctx=None):
    """纯函数：返回 (列 key, 标记列表)。顺序按 §4：完成 → 待处理 → 卡住 → 待立项 → 审查中 → 进行中。
    tree 是 ghapi.issue_tree 的结果（没立项为 None）。
    ctx: {'lead': present/absent/unknown, 'blocked': bool, 'questions': [...]}。"""
    ctx = ctx or {}
    flags = []
    status = need.get('status')
    if status == 'dropped':
        return 'done', [{'kind': 'muted', 'text': '不做'}]
    tree = tree or {}
    root = tree.get('root') or {}
    subs = tree.get('subs') or []
    prs = tree.get('prs') or []
    questions = [q for q in ctx.get('questions') or [] if q.get('open')]
    if root.get('state') == 'CLOSED':
        flags.append({'kind': 'muted', 'text': '不做' if root.get('state_reason') == 'NOT_PLANNED' else '已完成'})
        left = [s for s in subs if s.get('state') != 'CLOSED'] + questions
        if left:
            flags.append({'kind': 'bad', 'text': f'有遗留：{len(left)} 项未完成'})
        if tree.get('truncated'):
            flags.append({'kind': 'bad', 'text': '需求树没读全，遗留情况待核'})
        return 'done', flags
    # 待处理：理解卡等确认、挂在需求上的未答题 / 审批、已批准待 Owner 操作的 PR
    if need.get('discuss') == 'waiting_owner':
        flags.append({'kind': 'wait', 'text': f'理解卡 v{qstore.geti(need, "card_rev")} 等你确认'})
    if questions:
        kinds = {q.get('kind') for q in questions}
        flags.append({'kind': 'wait', 'text': f'{"审批" if "approval" in kinds else "题"} {len(questions)} 个等你答'})
    approved = [p for p in prs if p.get('state') == 'OPEN' and p.get('review') == 'APPROVED']
    if approved:
        flags.append({'kind': 'wait', 'text': '已批准，待你操作：' + '、'.join(f'PR #{p["number"]}' for p in approved)})
    if flags:
        return 'wait', flags
    # 卡住
    if status == 'held':
        flags.append({'kind': 'bad', 'text': '卡住：' + (need.get('last_error') or 'need 被标为 held')})
    if need.get('loop_suspect'):
        flags.append({'kind': 'bad', 'text': '疑似循环：已停止自动投递'})
    lead = ctx.get('lead') or ('absent' if not need.get('lead') else 'unknown')
    claimed = bool(need.get('lead')) and status in qstore.LEAD_STATES  # 有负责 lead 的需求（立项前后都算）
    if claimed and lead == 'absent':
        flags.append({'kind': 'bad', 'text': '没人管：负责 lead 已不在'})
    elif claimed and lead == 'unknown':
        flags.append({'kind': 'mute', 'text': 'lead 状态待核'})
    if ctx.get('blocked'):
        flags.append({'kind': 'bad', 'text': 'lead 被 blocked'})
    failed = [p for p in prs if p.get('state') == 'OPEN' and p.get('checks') in ('FAILURE', 'ERROR')]
    if failed:
        flags.append({'kind': 'bad', 'text': 'checks 失败：' + '、'.join(f'PR #{p["number"]}' for p in failed)})
    stale = []
    cutoff = (datetime.now(qstore.TZ) - timedelta(days=7)).isoformat(timespec='seconds')
    for p in prs:
        t = qstore.parse_time((p.get('updated') or '').replace('Z', '+00:00'))
        if p.get('state') == 'OPEN' and p.get('review') == 'CHANGES_REQUESTED' and t and \
                t.astimezone(qstore.TZ).isoformat(timespec='seconds') < cutoff:
            stale.append(p)
    if stale:
        flags.append({'kind': 'bad', 'text': '要求修改且 7 天无动静：' + '、'.join(f'PR #{p["number"]}' for p in stale)})
    if any(f['kind'] == 'bad' for f in flags):
        return 'stuck', flags
    if status != 'filed':
        return 'todo', flags
    # 审查中：有 open 且非草稿的 PR
    review = [p for p in prs if p.get('state') == 'OPEN' and not p.get('draft')]
    if review:
        return 'review', flags
    return 'doing', flags


def _questions_by_issue(needs):
    allq = qstore.all_questions()
    out = {}
    for qid, (m, _) in allq.items():
        if qstore.invalid_reason(m, allq):
            continue
        issue = (m.get('issue') or '').lower()
        mm = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)', issue)
        if not mm:
            continue
        out.setdefault((mm.group(1), int(mm.group(2))), []).append(
            {'id': qid, 'kind': qstore.kind_of(m), 'title': m.get('title') or qid,
             'open': m.get('status') == 'open', 'blocking': m.get('blocking') == 'yes'})
    return out


def _tree(url, refresh=False):
    now = datetime.now(qstore.TZ)
    with TREE_LOCK:
        hit = TREE.get(url)
        if hit and not refresh and (now - hit[0]).total_seconds() < TREE_TTL:
            return hit[1], ''
    try:
        tree = ghapi.issue_tree(url)
    except ghapi.GHError as e:
        with TREE_LOCK:
            old = TREE.get(url)
        return (old[1] if old else None), e.message
    with TREE_LOCK:
        TREE[url] = (now, tree)
    return tree, ''


def _lead_state(rec):
    lead = rec.get('lead') or {}
    if not lead:
        return 'absent', False
    key = (lead.get('herdr_session'), lead.get('lead_session'))
    now = datetime.now(qstore.TZ)
    with TREE_LOCK:
        hit = PRESENCE.get(key)
        if hit and (now - hit[0]).total_seconds() < 10:
            return hit[1], hit[2]
    st, pane, _ = qstore.presence(*key)
    blocked = bool(pane and pane.get('agent_status') == 'blocked')
    with TREE_LOCK:
        PRESENCE[key] = (now, st, blocked)
    return st, blocked


def _lead_presence(rec):
    return _lead_state(rec)[0]


def _sub_progress(subs):
    done = [s for s in subs if s.get('state') == 'CLOSED' and s.get('state_reason') != 'NOT_PLANNED']
    skip = [s for s in subs if s.get('state') == 'CLOSED' and s.get('state_reason') == 'NOT_PLANNED']
    left = [s for s in subs if s.get('state') != 'CLOSED']
    return {'done': len(done), 'skipped': len(skip), 'left': len(left), 'total': len(subs)}


def _next(need, tree, flags, col):
    if col == 'wait':
        return flags[0]['text'] if flags else '等你'
    if col == 'stuck':
        return next((f['text'] for f in flags if f['kind'] == 'bad'), '需要有人接手')
    if col == 'todo':
        return {'new': 'Router 分派中', 'routing': 'Router 分派中', 'assigned': '等 lead claim',
                'discussing': '讨论中'}.get(need.get('status'), '等立项')
    if col == 'review':
        p = next((p for p in tree.get('prs') or [] if p.get('state') == 'OPEN' and not p.get('draft')), {})
        rv = {'APPROVED': '已批准', 'CHANGES_REQUESTED': '要求修改', 'REVIEW_REQUIRED': '待审查'}.get(p.get('review'), '无 review')
        ck = {'SUCCESS': 'checks ✓', 'FAILURE': 'checks ✗', 'PENDING': 'checks 运行中', '': ''}.get(p.get('checks'), p.get('checks') or '')
        return f'PR #{p.get("number")} · {rv}' + (f' · {ck}' if ck else '')
    if col == 'done':
        closed_np = ((tree or {}).get('root') or {}).get('state_reason') == 'NOT_PLANNED'
        return '不做' if need.get('status') == 'dropped' or closed_np else '已完成'
    p = next((p for p in tree.get('prs') or [] if p.get('state') == 'OPEN'), {})
    if p:
        return f'PR #{p["number"]} · 处理中'
    return {'assigned': '待开工'}.get(need.get('status'), '进行中')


def _card(need, tree, ctx, col, flags, err):
    root = (tree or {}).get('root') or {}
    subs = (tree or {}).get('subs') or []
    prs = (tree or {}).get('prs') or []
    comments = [c for c in need.get('comments') or [] if c.get('state') in ('pending', 'notified')]
    lead = need.get('lead') or {}
    return {'id': need['id'], 'number': root.get('number') or '',
            'issue': (qstore.root_issue(need) or ('', 0, ''))[2], 'title': root.get('title') or (need.get('text') or '').split('\n')[0][:120],
            'ws': need.get('ws') or '', 'column': col, 'flags': flags, 'next': _next(need, tree or {}, flags, col),
            'issue_numbers': [n for _, n, _ in qstore.need_issue_refs(need)] + [s.get('number') for s in subs if s.get('number')],
            # 待处理页按「repo#号」合并（不能只按号：另一个 repo 同号的题不能被吞掉）
            'issue_keys': list(dict.fromkeys([f'{r}#{n}' for r, n, _ in qstore.need_issue_refs(need)]
                                             + [f'{s["repo"]}#{s["number"]}' for s in subs if s.get('repo') and s.get('number')])),
            'who': f'lead-{need.get("ws")}' if lead else ('Router' if need.get('status') != 'filed' else '没人'),
            'live': bool(lead and ctx.get('lead') == 'present' and col in ('doing', 'review')),
            'updated': root.get('updated') or need.get('updated') or need.get('created') or '',
            'unread': len(comments), 'loop': bool(need.get('loop_suspect')),
            'subs': _sub_progress(subs), 'prs': [{'number': p.get('number'), 'title': p.get('title'),
                                                 'state': p.get('state'), 'draft': p.get('draft'),
                                                 'review': p.get('review'), 'checks': p.get('checks'),
                                                 'url': p.get('url')} for p in prs],
            'comments': [{'id': c.get('id'), 'state': c.get('state'), 'author': c.get('author')} for c in comments],
            'error': err, 'status': need.get('status')}


def view(refresh=False, ws=None):
    needs = sorted(qstore.all_needs().values(), key=lambda r: r.get('created') or '', reverse=True)
    qidx = _questions_by_issue(needs)
    cols = {k: [] for k, _ in COLUMNS}
    errors = []
    for rec in needs:
        tree, err = (None, '')
        if rec.get('status') in ('filed',):
            ref = qstore.root_issue(rec)
            if ref:
                tree, err = _tree(ref[2], refresh)
                if err:
                    errors.append(f'{rec["id"]}：{err}')
                elif tree:
                    try:
                        with qstore.locked():
                            qstore.closeout_due(qstore.read_need(rec['id']), tree)
                    except qstore.QError:
                        pass
        questions = []
        for repo, number, url in qstore.need_issue_refs(rec):
            questions += qidx.get((repo, number), [])
        for sub in (tree or {}).get('subs') or []:
            questions += qidx.get((sub.get('repo'), sub.get('number')), [])
        st, blocked = _lead_state(rec)
        ctx = {'lead': st, 'questions': questions, 'blocked': blocked}
        col, flags = column(rec, tree, ctx)
        if err and col == 'doing':
            flags.append({'kind': 'mute', 'text': 'GitHub 查询失败，状态待核'})
        card = _card(rec, tree, ctx, col, flags, err)
        if ws and card['ws'] != ws:
            continue
        cols[col].append(card)
    out = []
    for key, label in COLUMNS:
        cards = sorted(cols[key], key=lambda c: c.get('updated') or '', reverse=True)
        out.append({'key': key, 'label': label, 'count': len(cards), 'cards': cards})
    return {'generated_at': qstore.now(), 'columns': out, 'errors': errors,
            'ws': sorted({r.get('ws') for r in needs if r.get('ws')}), 'degrade': poll_degrade()}


def poll_degrade():
    import poll
    return poll.degrade()
