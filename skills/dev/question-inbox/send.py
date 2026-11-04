"""需求的发言路径：默认只进 7788 线程；勾选后发到根 Issue；lead 的结论也用这里发。

只有本模块调 ghapi 写评论；每个写动作先记发送意图 / 写门，再调 gh（argv），拿到 comment id 后补记账。
正文不落盘（GitHub 是事实源），本地只记 comment id / 作者 / 时间 / 哈希。
"""
import uuid

import ghapi
import qstore


def _ref(rec):
    ref = qstore.root_issue(rec)
    if not ref:
        raise qstore.QError(409, f'{rec["id"]} 还没有根 Issue：先立项（need link）再发到 GitHub')
    return ref


def _tag(nid, rid):
    return f'<!-- qi:need-{nid} msg-{rid} -->'


def _row(rec, data, by):
    user = (data.get('user') or {}).get('login') or ''
    return {'id': data.get('id'), 'repo': _ref(rec)[0], 'number': _ref(rec)[1], 'author': user,
            'created': data.get('created_at') or qstore.now(), 'updated': data.get('updated_at') or data.get('created_at') or qstore.now(),
            'hash': qstore._hash((data.get('body') or '') + (data.get('updated_at') or '')), 'self': by}


def _record(rec, data, by):
    """把一次成功的写评论记进需求：own_comments（身份依据）+ 评论元数据（不存正文）。"""
    cid = int(data['id'])
    qstore.add_own_comment(rec, cid, by, issue=_ref(rec)[2])
    qstore.upsert_comment(rec, _row(rec, data, by))


def _reconcile(rec, rid):
    """结果不明时先去根 Issue 按尾标对账；找到就补记账，找不到返回 None（不盲目重发）。"""
    repo, number, _ = _ref(rec)
    tag = _tag(rec['id'], rid)
    try:
        return ghapi.find_tagged(repo, number, tag)
    except ghapi.GHError:
        return None


def owner_message(nid, text, request_id='', to_issue=False):
    """Owner 在详情页发消息。默认只进 7788 线程；to_issue=True 才发根 Issue 评论。
    返回 {id, comment_id, to_issue, receipt, created}。同一 request_id 重发返回原结果。"""
    text = qstore._clean_text(text, '消息')
    request_id = qstore._check_request_id(request_id)
    if not to_issue:
        rec, new = qstore.owner_reply(nid, text, request_id)
        return {'id': nid, 'to_issue': False, 'created': new, 'receipt': qstore.receipt(rec),
                'thread': rec.get('thread') or []}
    if not request_id:
        request_id = uuid.uuid4().hex
    # 1) 先看有没有同一个 request_id 的发送意图
    with qstore.locked():
        rec = qstore.read_need(nid)
        if rec.get('status') in qstore.SEALED:
            raise qstore.QError(409, f'{nid} 已不做，不能再发')
        repo, number, url = _ref(rec)
        it, new = qstore.send_intent(rec, request_id, text, url)
        state = it.get('state')
        if state == 'sent' and it.get('comment_id'):
            return {'id': nid, 'to_issue': True, 'created': False, 'comment_id': it['comment_id'],
                    'issue': url, 'receipt': qstore.receipt(rec)}
        if state in ('sending', 'uncertain') and not new:
            qstore._save_need(rec, '重复发送同一个 request_id：先对账')
            ambiguous = True
        else:
            qstore._save_need(rec, '记下发送意图，准备发到 Issue')
            ambiguous = False
    if ambiguous:
        got = _reconcile(qstore.read_need(nid), request_id)
        if got:
            with qstore.locked():
                rec = qstore.read_need(nid)
                qstore.intent_done(rec, request_id, got['id'])
                _record(rec, got, 'owner')
                qstore._queue_need(rec, 'comments')
                qstore._save_need(rec, '对账找到已经发出去的评论，补记账')
                return {'id': nid, 'to_issue': True, 'created': False, 'comment_id': got['id'],
                        'issue': url, 'receipt': qstore.receipt(rec)}
        raise qstore.QError(409, '上一次发送结果不明，暂时找不到那条评论：先到根 Issue 上核对，确认没有再用新的 request_id 发')
    # 2) 过写门（暂停 / 串行 / 限流），再调 gh
    try:
        ghapi.write_gate(nid)
        data = ghapi.create_comment(repo, number, text, _tag(nid, request_id))
    except ghapi.GHError as e:
        with qstore.locked():
            rec = qstore.read_need(nid)
            qstore.intent_fail(rec, request_id,
                               'uncertain' if e.kind in ('timeout', 'uncertain') or not e.status else 'failed',
                               e.message)
            qstore._save_need(rec, f'发 Issue 评论失败：{e.kind} {e.message}')
        raise
    with qstore.locked():
        rec = qstore.read_need(nid)
        qstore.intent_done(rec, request_id, data['id'])
        _record(rec, data, 'owner')
        qstore._queue_need(rec, 'comments')
        qstore._save_need(rec, f'发到 Issue（评论 {data["id"]}），投给负责 lead')
        return {'id': nid, 'to_issue': True, 'created': True, 'comment_id': data['id'], 'issue': _ref(rec)[2],
                'receipt': qstore.receipt(rec)}


def lead_comment(nid, body, ident):
    """负责 lead 把结论写到根 Issue（need comment）。正文尾部附隐藏关联标记（只作对账提示）。"""
    body = qstore._clean_text(body, '结论')
    with qstore.locked():
        rec = qstore.read_need(nid)
        qstore._check_lead(rec, ident)
        if rec.get('status') in qstore.SEALED:
            raise qstore.QError(409, f'{nid} 已不做，不能再往 Issue 写结论')
        repo, number, url = _ref(rec)
        by = f'lead-{rec.get("ws") or "?"}'
    rid = uuid.uuid4().hex
    try:
        ghapi.write_gate(nid)
        data = ghapi.create_comment(repo, number, body, _tag(nid, rid))
    except ghapi.GHError as e:
        with qstore.locked():
            rec = qstore.read_need(nid)
            qstore._save_need(rec, f'lead 发结论失败：{e.kind} {e.message}')
        raise
    with qstore.locked():
        rec = qstore.read_need(nid)
        _record(rec, data, by)
        qstore.add_thread(rec, {'at': qstore.now(), 'by': 'lead', 'kind': 'issue', 'comment_id': data['id']})
        qstore._save_need(rec, f'lead 把结论写到 Issue（评论 {data["id"]}）')
        return {'comment_id': data['id'], 'url': url, 'body': body}


def link_comment(rec, ident, port):
    """立项时把**已确认**的理解卡贴成根 Issue 的一条评论（不改正文、不贴讨论原文）。"""
    rev = qstore.geti(rec, 'confirmed_rev')
    card = next((x.get('card') for x in reversed(rec.get('thread') or [])
                 if x.get('kind') == 'card' and qstore.geti(x, 'rev') == rev), None)
    if card:
        parts = [f'**已确认的理解 · v{rev}**（来自 7788 的讨论线程，Owner 已确认）', '']
        for k in ('理解', '范围', '不做什么', '验收草案'):
            if card.get(k):
                parts += [f'- **{k}**：{card[k]}', '']
    else:  # 直接立项：没有单独讨论过，把原话留痕，不伪造“已确认”
        parts = ['**按原话立项（未单独讨论）**', '', rec.get('text') or '', '']
    parts.append(f'7788 讨论：http://127.0.0.1:{port}/#need:{rec["id"]}')
    body = '\n'.join(parts)
    with qstore.locked():
        cur = qstore.read_need(rec['id'])
        repo, number, url = _ref(cur)
        by = f'lead-{cur.get("ws") or "?"}'
    ghapi.write_gate(rec['id'])
    data = ghapi.create_comment(repo, number, body, _tag(rec['id'], f'link-{uuid.uuid4().hex[:10]}'))
    with qstore.locked():
        cur = qstore.read_need(rec['id'])
        _record(cur, data, by)
        qstore.add_thread(cur, {'at': qstore.now(), 'by': 'lead', 'kind': 'issue', 'comment_id': data['id'],
                                'note': '已确认的理解'})
        qstore._save_need(cur, '把已确认的理解贴到根 Issue')
    return {'comment_id': data['id'], 'url': url}
