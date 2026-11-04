"""需求的发言路径：默认只进 7788 线程；勾选后发到根 Issue；lead 的结论、立项评论也用这里发。

三个写入口（Owner 发到 Issue、lead 的 need comment、立项时的「已确认的理解」）走同一套持久发送协议：
1. 持锁落发送意图（request_id → 正文哈希、目标根 Issue、随机意图 id、发布正文哈希）；
2. 过写门（ghapi.writing：跨进程串行、间隔、限流、暂停复核包住实际 POST），gh argv 发评论；
3. 拿到 comment id 后补记账（own_comments + 评论元数据 + 各入口的副作用）。
失败分两类：确定没发出去 → failed，同一个 request_id 可以直接重发；可能已经发出去 → uncertain，
之后同一个 request_id 再来（或下一轮轮询）先到根 Issue 对账：只认完整尾标 + 发布正文哈希 + 我们记下的发布账号；
读完了也找不到、且距**实际发出 POST** 已超过宽限期，才判定没发出去再发。同一个意图只会真正 POST 一次（写锁内核对）。
正文不落盘（GitHub 是事实源），本地只记 comment id / 作者 / 时间 / 哈希。
"""
import hashlib
from datetime import datetime

import ghapi
import qstore


def reconcile_grace():
    """结果不明之后，至少等这么久（> gh 超时）还对不到账，才认为那次确实没发出去。"""
    return max(60.0, ghapi.timeout() + 30)


def _ref(rec):
    ref = qstore.root_issue(rec)
    if not ref:
        raise qstore.QError(409, f'{rec["id"]} 还没有根 Issue：先立项（need link）再发到 GitHub')
    return ref


def _row(rec, data, by):
    user = (data.get('user') or {}).get('login') or ''
    repo, number, _ = _ref(rec)
    return {'id': data.get('id'), 'repo': repo, 'number': number, 'author': user,
            'created': data.get('created_at') or qstore.now(), 'updated': data.get('updated_at') or data.get('created_at') or qstore.now(),
            'hash': qstore._hash((data.get('updated_at') or data.get('created_at') or '') + (data.get('body') or '')), 'self': by}


def _record(rec, data, by):
    """把一次成功的写评论记进需求：own_comments（身份依据）+ 评论元数据（不存正文）。"""
    qstore.add_own_comment(rec, int(data['id']), by, issue=_ref(rec)[2])
    qstore.upsert_comment(rec, _row(rec, data, by))


def complete_intent(rec, request_id, data):
    """（调用方持锁）意图确认发出：补记账，并做该入口的副作用。主动发送、重试对账、后台轮询对账都走这里，副作用一致。
    返回 False 表示这个意图早已完成（不重复做副作用）。"""
    it = (rec.get('send_intents') or {}).get(request_id) or {}
    if it.get('state') == 'sent' and it.get('comment_id'):
        return False
    by = it.get('by') or 'owner'
    qstore.intent_done(rec, request_id, data['id'])
    _record(rec, data, by)
    if by == 'owner':
        qstore._queue_need(rec, 'comments')  # Owner 发到 Issue 的消息同样要通知负责 lead
    else:
        qstore.add_thread(rec, {'at': qstore.now(), 'by': 'lead', 'kind': 'issue', 'comment_id': int(data['id']),
                                **({'note': it['note']} if it.get('note') else {})})
    return True


def _result(rec, it, created):
    return {'id': rec['id'], 'to_issue': True, 'created': created, 'comment_id': it.get('comment_id'),
            'issue': _ref(rec)[2], 'receipt': qstore.receipt(rec)}


def _posted_at(it):
    """这个意图上一次**实际发出 POST** 的时间；没 POST 过返回 None。旧记录没有 posting_at：uncertain 的按 tried_at 算。"""
    return qstore.parse_time(it.get('posting_at') or (it.get('tried_at') if it.get('state') == 'uncertain' else ''))


def _post(nid, request_id, body, by, check=None, note=''):
    """一次持久发送。by：'owner' 或 'lead-<ws>'；check(rec)：持锁时的额外校验（lead 身份）。
    返回 (记录, 意图, 是否这次新发出)。
    同一个意图恰好 POST 一次：真正 POST 之前，在跨进程写锁（ghapi.writing）里重新读意图——已经发出就直接返回原结果，
    别的请求已经 POST 过（posting_at 已记）就不再发；POST 和结果记账都在这把锁里完成。
    宽限期从实际 POST 的时刻（posting_at）算，排队等写锁的时间不算；从没 POST 过的意图（还在排队、或在 POST 前退出）
    重试时直接进写队列，由上面的锁内核对保证不重复。"""
    if len(body) > ghapi.MAX_COMMENT:
        raise qstore.QError(400, f'发到 Issue 的正文不超过 {ghapi.MAX_COMMENT} 字（现在 {len(body)} 字）')
    with qstore.locked():
        rec = qstore.read_need(nid)
        if check:
            check(rec)
        if rec.get('status') in qstore.SEALED:
            raise qstore.QError(409, f'{nid} 已不做，不能再往 Issue 写')
        repo, number, url = _ref(rec)
        it, new = qstore.send_intent(rec, request_id, body, url, by)
        if new:
            it['posted_hash'] = qstore._hash(ghapi.norm_body(ghapi.posted_text(body, ghapi.tag(nid, it['tag_id']))))
            if note:
                it['note'] = note
        if it.get('state') == 'sent' and it.get('comment_id'):
            return rec, it, False
        check_first = not new and it.get('state') in ('sending', 'uncertain') and _posted_at(it) is not None
        if not check_first:
            it.update({'state': 'sending', 'tried_at': qstore.now(), 'error': ''})
            qstore._save_need(rec, f'记下发送意图 {request_id}，准备发到 Issue')
    if check_first:  # 上一次真的 POST 过、结果不明：先对账
        got, how = ghapi.find_intent(repo, number, nid, it)
        with qstore.locked():
            rec = qstore.read_need(nid)
            it = rec['send_intents'][request_id]
            if got:
                complete_intent(rec, request_id, got)
                qstore._save_need(rec, f'对账找到已经发出去的评论 {got["id"]}，补记账')
                return rec, rec['send_intents'][request_id], False
            if it.get('state') == 'sent':
                return rec, it, False
            if how == 'no_poster':
                qstore._save_need(rec, f'重复发送 {request_id}：还不知道我们的发布账号，对不了账，不重发')
                raise qstore.QError(409, '上一次发送结果不明，而且还不知道我们的发布账号（发评论用的 GitHub 账号，还没有一次成功发评论的回包），'
                                         '对不了账：先不重发。之后有一次成功发评论，再点会重新对账')
            posted = _posted_at(it)
            age = (datetime.now(qstore.TZ) - posted).total_seconds() if posted else 1e9
            if how != 'complete' or age < reconcile_grace():
                qstore._save_need(rec, f'重复发送 {request_id}：结果不明，暂时对不上账，不重发')
                raise qstore.QError(409, '上一次发送结果不明，根 Issue 上暂时找不到那条评论：先不重发。'
                                         f'约 {int(reconcile_grace())} 秒后再点一次会重新对账，确认没发出去才会发')
            it.update({'state': 'sending', 'tried_at': qstore.now(), 'posting_at': '',
                       'error': f'对账：读完根 Issue 的评论也没有这条（距实际发出 {int(age)} 秒），确认没发出去，重发'})
            qstore._save_need(rec, f'{request_id} 对账确认没发出去，重发')
    started = False
    try:
        with ghapi.writing(nid):
            with qstore.locked():  # 锁内核对：只有一个请求能真正 POST 这个意图
                rec = qstore.read_need(nid)
                it = rec['send_intents'][request_id]
                if it.get('state') == 'sent' and it.get('comment_id'):
                    return rec, it, False
                if it.get('posting_at'):
                    raise qstore.QError(409, '同一条消息已经由另一次请求发出（结果还在确认）：不再重发，稍后刷新会对账')
                it['posting_at'] = qstore.now()
                qstore._save_need(rec)
            started = True
            data = ghapi.create_comment(repo, number, body, ghapi.tag(nid, it['tag_id']))
            with qstore.locked():
                rec = qstore.read_need(nid)
                complete_intent(rec, request_id, data)
                qstore._save_need(rec, f'{"Owner" if by == "owner" else by} 发到 Issue（评论 {data["id"]}）')
                return rec, rec['send_intents'][request_id], True
    except ghapi.GHError as e:
        sure = not started or ghapi.definitely_not_sent(e)  # POST 之前就失败（写门拒绝）一律算确定没发
        with qstore.locked():
            rec = qstore.read_need(nid)
            qstore.intent_fail(rec, request_id, 'failed' if sure else 'uncertain', e.message)
            if sure:
                rec['send_intents'][request_id]['posting_at'] = ''
            qstore._save_need(rec, f'发 Issue 评论{"失败（确定没发出）" if sure else "结果不明（先对账，不重发）"}：{e.kind} {e.message}')
        raise


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
        raise qstore.QError(400, '发到 Issue 必须带 request_id（页面会自动生成；重试要用同一个）')
    rec, it, created = _post(nid, request_id, text, 'owner')
    return _result(rec, it, created)


def lead_request_id(nid, body):
    """lead 没给 --request-id 时：同一需求、同一正文得到同一个幂等键，重跑命令不会重复发。"""
    return 'lead-' + hashlib.sha1(f'{nid}\n{body}'.encode('utf-8')).hexdigest()[:32]


def lead_comment(nid, body, ident, request_id=''):
    """负责 lead 把结论写到根 Issue（need comment）。同一 request_id（默认由正文派生）重跑：已发过就返回原评论，
    结果不明就先对账。"""
    body = qstore._clean_text(body, '结论')
    request_id = qstore._check_request_id(request_id) or lead_request_id(nid, body)
    with qstore.locked():
        rec = qstore.read_need(nid)
        qstore._check_lead(rec, ident)
        by = f'lead-{rec.get("ws") or "?"}'
    rec, it, created = _post(nid, request_id, body, by, check=lambda r: qstore._check_lead(r, ident))
    return {'comment_id': it.get('comment_id'), 'url': _ref(rec)[2], 'body': body, 'created': created,
            'request_id': request_id}


def link_request_id(nid):
    return f'link-{nid}'


def link_pending(rec):
    """立项评论还要不要贴：第一次 link，或者上次贴失败 / 结果不明（重跑 need link 会接着贴，不会贴第二条）。"""
    it = (rec.get('send_intents') or {}).get(link_request_id(rec['id']))
    return (it or {}).get('state') != 'sent'


def link_comment(rec, ident):
    """立项时把**已确认**的理解卡贴成根 Issue 的一条评论（不改正文、不贴讨论原文）。幂等键固定为 link-<需求 id>。"""
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
    # 不贴 127.0.0.1:<端口> 链接：团队打不开，也不暴露本机端口
    parts.append(f'7788 讨论：需求 {rec["id"]}（Owner 本机工作台，地址 #need:{rec["id"]}）')
    body = '\n'.join(parts)
    if len(body) > ghapi.MAX_COMMENT:
        body = body[:ghapi.MAX_COMMENT - 40].rstrip() + f'\n\n…（太长已截断，全文见 7788 需求 {rec["id"]}）'
    note = '已确认的理解' if card else '按原话立项'
    cur, it, created = _post(rec['id'], link_request_id(rec['id']), body, f'lead-{rec.get("ws") or "?"}',
                             check=lambda r: qstore._check_lead(r, ident), note=note)
    return {'comment_id': it.get('comment_id'), 'url': _ref(cur)[2], 'created': created}
