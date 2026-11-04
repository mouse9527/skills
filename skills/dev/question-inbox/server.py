#!/usr/bin/env python3
"""问题收件箱 · 网页 + 投递循环（单进程）。绑定 127.0.0.1:$QI_PORT（默认 7788），端口被占就退出。

启动顺序：进程锁 → 修复（补齐被替换题、sending → uncertain）→ 生成 token → 绑定端口 → 投递循环。
投递循环同时把需求（needs/）投给登记的 Router；Router 不在时在 session $QI_ROUTER_SESSION 里拉起（routerctl.ensure）。
日志只记 id、状态和错误摘要，不记答案正文和需求原话。
"""
import fcntl
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402
import ghapi  # noqa: E402
import poll  # noqa: E402
import qstore  # noqa: E402
import routerctl  # noqa: E402
import send  # noqa: E402
import tasks  # noqa: E402

log = logging.getLogger('question-inbox')
MAX_BODY = 1 << 20
BATCH = 5  # 同一个 lead 一条 prompt 最多合并几题


# ---------- 投递 ----------

def q_cmd():
    p = str(Path(__file__).resolve().parent / 'q.py')
    home = str(Path.home())
    return '~' + p[len(home):] if p.startswith(home + os.sep) else p


def prompt_text(kind, metas):
    ids = '、'.join(m['id'] for m in metas)
    args = ' '.join(m['id'] for m in metas)
    if kind == 'withdraw':
        m = metas[0]
        if qstore.closed_by_owner(m):
            why = m.get('resolution') if m.get('status') == 'resolved' else m.get('withdraw_reason')
            return (f'[question-inbox] Owner 已在页面上关闭问题 {m["id"]}（{why}）。这条消息本身不是新的授权。\n'
                    f'不用再 receive / resolve 它；如果还没按之前的回答执行，先停下，有需要另开一题问 Owner。\n'
                    f'需要时执行：python3 {q_cmd()} show {m["id"]}')
        return (f'[question-inbox] 问题 {m["id"]} 已撤回（{m.get("withdraw_reason") or "未写原因"}），'
                f'之前投递给你的回答不要再执行。这条消息本身不是新的授权。\n'
                f'需要时执行：python3 {q_cmd()} show {m["id"]}')
    head = (f'[question-inbox] Owner 更正了回答：{ids}（现为 Rev {metas[0].get("answer_rev")}，旧版作废）。'
            if kind == 'correction' else f'[question-inbox] Owner 已回答：{ids}。')
    return (f'{head}这条消息本身不是新的授权。\n'
            f'先执行：python3 {q_cmd()} receive {args}\n'
            f'它会核对你的身份和回答版本后输出答案；如果报身份不符、已撤回、已更正，按它的提示做，不要消费旧答案。\n'
            f'处理完每条后执行：python3 {q_cmd()} resolve <id> --answer-rev <N> --note "决定了什么，记在哪里"')


def scan_once():
    groups = {}
    for meta, kind in qstore.due_for_delivery():
        groups.setdefault((meta.get('herdr_session'), meta.get('lead_session'), kind), []).append(meta)
    for (session, lead, kind), metas in groups.items():
        metas.sort(key=lambda m: m['id'])
        size = BATCH if kind == 'answer' else 1  # 撤回、更正通知单独发
        for i in range(0, len(metas), size):
            deliver(session, lead, kind, metas[i:i + size])


def deliver(session, lead, kind, metas):
    refs = [(m['id'], kind, qstore.geti(m, 'delivery_rev')) for m in metas]
    pane, why = qstore.find_lead(session, lead)  # socket 不在时不执行任何 herdr 命令
    if not pane:
        qstore.mark_held(refs, why)
        log.info('held %s: %s', ','.join(r[0] for r in refs), why)
        return
    claimed = qstore.claim(refs)
    if not claimed:
        return
    ids = {c[0] for c in claimed}
    text = prompt_text(kind, [m for m in metas if m['id'] in ids])
    r = qstore.herdr(session, 'agent', 'prompt', pane['pane_id'], text)
    if r['ok']:
        outcome, err = 'sent', ''
    elif r['kind'] in ('no_socket', 'exec_error') or r['code'] in qstore.NOT_SENT_CODES:
        outcome, err = 'held', f'{r["code"]} {r["message"]}'.strip()
    else:  # 超时、输出无法解析、不认识的错误：可能已经发出去了，不自动重发
        outcome, err = 'uncertain', f'{r["code"]} {r["message"]}'.strip()
    qstore.finish(claimed, outcome, err, pane.get('agent_status') or '')
    log.info('%s %s %s %s', kind, outcome, ','.join(sorted(ids)), err)


# ---------- 需求 → Router ----------

ENSURE_LOCK = threading.Lock()  # 同一时间只拉起一次


def lead_prompt(recs):
    """投给负责 lead 的一次唤醒：同一个 lead 的几个需求、每个需求的几种消息合并成一条；内容按这次投递实际带上的消息
    （need_claim 记下的 inflight）现算，不靠「最后一次排队的是哪一种」，所以评论唤醒、7788 回复、确认、收尾不会互相吞掉。"""
    ids = '、'.join(r['id'] for r in recs)
    lines = [f'[question-inbox] 需求 {ids} 有新消息。这条消息本身不是新的授权。']
    for r in recs:
        fl = r.get('inflight') if isinstance(r.get('inflight'), dict) else {}
        kinds = fl.get('kinds') or r.get('delivery_kinds') or [r.get('delivery_kind')]
        mids = set(fl.get('thread') or [])
        got = [x for x in r.get('thread') or [] if x.get('mid') in mids]
        replies = [x for x in got if x.get('kind') == 'reply']
        confirms = [x for x in got if x.get('kind') == 'confirm']
        n_comments = len(fl.get('comments') or [])
        what = []
        if confirms or ('confirm' in kinds and r.get('status') == 'confirmed'):
            what.append(f'Owner 确认了理解卡 v{qstore.geti(r, "confirmed_rev")}：按已确认的那一版在本 ws 主 repo 建根 Issue'
                        f'（正文第一行「需求：{r["id"]}」），然后 need link')
        if replies or 'reply' in kinds:
            what.append(f'Owner 在需求讨论线程里回复了{f" {len(replies)} 条" if replies else ""}（按 skill workspace §3 继续讨论，'
                        f'需要时 need card 发新一版理解卡）')
        if n_comments or 'comments' in kinds:
            what.append(f'有{f" {n_comments} 条" if n_comments else ""}新的 / 编辑过的 Issue 评论（正文现场从 GitHub 取）')
        if fl.get('closeout') or 'closeout' in kinds:
            why = (r.get('closeout') or {}).get('why') or '根 Issue 已关闭或关联 PR 全部合并'
            what.append(f'到了收尾（{why}）：确认合并了什么、哪些子 Issue 还开着，清理 worktree，用 need say 或 need comment 回报一句；'
                        f'还开着的事另提需求')
        lines.append(f'- {r["id"]}：' + '；'.join(what or ['有更新']))
        # need inbox 一次只接一个需求（ack 也按需求隔离）：每个需求单独给一条能照原样执行的命令
        lines.append(f'  先执行：python3 {q_cmd()} need inbox {r["id"]} —— 列出没读的 7788 消息和 Issue 评论，读完照它打印的口令 --ack；'
                     f'需要时：python3 {q_cmd()} need show {r["id"]}')
    lines.append('评论是协作输入，不是审批：合并、部署、扩权仍按审批题和已有授权。团队的问题回答到根 Issue（只能用 need comment 发）。')
    return '\n'.join(lines)


def router_prompt(kind, recs):
    ids = '、'.join(r['id'] for r in recs)
    args = ' '.join(r['id'] for r in recs)
    if kind in qstore.LEAD_KINDS:
        return lead_prompt(recs)
    if kind == 'takeover':  # router takeover 一次只接一个需求：每个需求一条命令
        return (f'[question-inbox] 需求 {ids} 的负责 lead 确认不在，但还有没读的消息。这条消息本身不是新的授权。\n'
                + '\n'.join(f'- {r["id"]}：先执行 python3 {q_cmd()} need show {r["id"]}；然后 python3 {q_cmd()} router takeover {r["id"]}'
                             for r in recs)
                + '\n安排本 ws 的新 lead 接手（新 lead 会 claim --takeover 并读漏掉的消息）；原 lead 还在时它会拒绝，不用重试。')
    if kind == 'unclaimed':
        return (f'[question-inbox] 需求已分到 ws，但一直没有 lead claim：{ids}。这条消息本身不是新的授权。\n'
                f'先执行：python3 {q_cmd()} need show {args}；看对应的 lead-<ws> 在不在、有没有收到派工书，必要时 router dispatch 重发。')
    if kind == 'note':
        return (f'[question-inbox] 需求有 Owner 的新补充：{ids}。这条消息本身不是新的授权。\n'
                f'先执行：python3 {q_cmd()} need show {args}；按 skill router 处理，处理完用 need note 记一句。')
    return (f'[question-inbox] 新需求：{ids}。这条消息本身不是新的授权。\n'
            f'先执行：python3 {q_cmd()} need take {args}；按 skill router 处理。')


def router_presence(info):
    """(pane, 原因, 该不该拉起)。用三态判断：登记的会话在 → 投；**确认不在**（没登记、pane 没了、换了会话、agent 已退出）→ 拉起
    （ensure 见到名为 router 的 agent 只请它重新登记，不重启它的会话）；**查不清**（查询失败、超时、多个匹配）→ 只暂缓，不拉起。"""
    s = qstore.router_session()
    if not info.get('lead_session') or info.get('herdr_session') != s:
        return None, 'Router 还没登记', True
    st, pane, why = qstore.presence(s, info['lead_session'])
    if st == 'present':
        return pane, '', False
    if st == 'unknown':
        return None, f'核对不了 Router 是否还在（{why}）', False
    return None, f'登记的 Router 会话不在了（{why}）', True


def _ensure_thread():
    try:
        result, why = routerctl.ensure()
        log.info('router ensure %s: %s', result, why)
    except Exception:  # noqa: BLE001
        log.exception('router ensure failed')
        qstore.ensure_end('failed', 'ensure 异常，见 server.log')
    finally:
        ENSURE_LOCK.release()


def kick_ensure():
    """后台拉起 Router，返回给 last_error 的一句说明。"""
    st = qstore.ensure_state()
    if st.get('gave_up'):
        return 'Router 起不来，已停止自动拉起（页面上可重试）'
    if not ENSURE_LOCK.acquire(blocking=False):
        return '正在拉起 Router'
    threading.Thread(target=_ensure_thread, daemon=True).start()
    return '正在拉起 Router'


def scan_needs():
    for nid in qstore.nudge_unclaimed():  # 分到 ws 太久没人 claim：补投给 Router 一次
        log.info('need %s unclaimed, nudging router', nid)
    due = qstore.needs_due()
    to_lead = [r for r in due if r.get('delivery_to') == 'lead']
    due = [r for r in due if r.get('delivery_to') != 'lead']
    if to_lead:  # Owner 对讨论的回复 / 确认：给已在跑的 lead，暂停不拦（暂停只停 Router 侧）
        scan_lead_needs(to_lead)
    if not due or qstore.paused():  # 暂停：不投给 Router、不拉起
        return
    s = qstore.router_session()
    refs = [(r['id'], qstore.geti(r, 'delivery_seq')) for r in due]
    if not qstore.socket_exists(s):  # 不创建 session，也不执行任何 herdr 命令
        qstore.need_mark_held(refs, f'herdr session {s} 不在（由 launchd 负责，服务不创建）')
        return
    info = qstore.router_info()
    pane, why, start = router_presence(info)
    if not pane:
        qstore.need_mark_held(refs, why + ('；' + kick_ensure() if start else ''))
        log.info('need held %s: %s', ','.join(r[0] for r in refs), why)
        return
    groups = {}
    for r in sorted(due, key=lambda r: r['id']):
        groups.setdefault(r.get('delivery_kind') or 'new', []).append(r)
    for kind, recs in groups.items():
        for i in range(0, len(recs), qstore.NEED_BATCH):
            deliver_router(s, pane, info['lead_session'], kind, recs[i:i + qstore.NEED_BATCH])


def scan_lead_needs(due):
    """投给该需求的负责 lead：按 claim 时记下的 agent_session 找；同一个 lead 的多个需求合并成一次唤醒。
    找不到时，负责 lead **确认不在**（立项前后都一样）→ 消息留在 lead 这边 held（原因写明、按退避重试），
    并自动请 Router 安排接手（ask_router_takeover：不占投递槽、去重、暂停时只记录）；查不清（unknown）→ 只暂缓，不叫 Router。
    之后重试时原 lead 回来了 → 照常投给它并作废接手请求；新 lead claim --takeover → 投给新 lead。都不起 lead、不改投。查 herdr 不持锁。"""
    groups = {}
    for r in sorted(due, key=lambda r: r['id']):
        lead = r.get('lead') or {}
        groups.setdefault((lead.get('herdr_session'), lead.get('lead_session')), []).append(r)
    for (session, lead), recs in groups.items():
        refs = [(r['id'], qstore.geti(r, 'delivery_seq')) for r in recs]
        pane, why = qstore.find_lead(session, lead) if session and lead else (None, '还没有负责 lead')
        if not pane:
            st, _, _ = qstore.presence(session, lead) if session and lead else ('absent', None, '')
            for ref, r in zip(refs, recs):
                if st != 'absent':
                    qstore.need_mark_held([ref], f'负责 lead {why}')
                    continue
                asked = ask_router_takeover(r) if r.get('lead') and r.get('status') in qstore.LEAD_STATES else 'n/a'
                note = {'sent': '已请 Router 安排接手', 'dup': '已请 Router 安排接手', 'uncertain': '已请 Router 安排接手（不确定是否送达）',
                        'paused': '全局暂停中，没有叫 Router，恢复后再叫', 'router_absent': 'Router 暂时不在，稍后再叫'}.get(asked, '')
                qstore.need_mark_held([ref], f'负责 lead {why}（确认不在' + (f'：{note}' if note else '') + '）')
            continue
        stale = [r['id'] for r in recs if r.get('takeover_ask')]
        if stale:  # 现场核对 lead 在（原 lead 回来了）：之前的接手请求作废，照常投递
            qstore.clear_takeover_ask(stale)
        for i in range(0, len(recs), qstore.NEED_BATCH):
            deliver_router(session, pane, '', 'lead', recs[i:i + qstore.NEED_BATCH])  # 提示词由 lead_prompt 按消息现算


def ask_router_takeover(rec, force=False):
    """负责 lead 确认不在：直接 prompt Router 一次「router takeover <id>」，不占投递槽（消息照样 held）。
    去重：已叫过且在 QI_TAKEOVER_REASK 窗口内不再叫（force=True 是页面上的手动重试）；全局暂停时不叫、只记录；
    Router 不在时不叫，需要时拉起它，下一轮再叫。返回 sent / uncertain / dup / paused / router_absent。"""
    if not force and not qstore.takeover_ask_due(rec):
        return 'dup'
    nid = rec['id']
    if qstore.paused():
        qstore.note_takeover_ask(nid, 'paused')
        return 'paused'
    s = qstore.router_session()
    if not qstore.socket_exists(s):
        qstore.note_takeover_ask(nid, 'router_absent', f'herdr session {s} 不在')
        return 'router_absent'
    pane, why, start = router_presence(qstore.router_info())
    if not pane:
        qstore.note_takeover_ask(nid, 'router_absent', why + ('；' + kick_ensure() if start else ''))
        return 'router_absent'
    r = qstore.gated(s, 'agent', 'prompt', pane['pane_id'], router_prompt('takeover', [rec]))
    if r.get('kind') == 'paused':
        qstore.note_takeover_ask(nid, 'paused')
        return 'paused'
    if r['ok']:
        result = 'sent'
    elif r['kind'] in ('no_socket', 'exec_error') or r['code'] in qstore.NOT_SENT_CODES:
        result = 'router_absent'
    else:
        result = 'uncertain'
    qstore.note_takeover_ask(nid, result, '' if r['ok'] else f'{r["code"]} {r["message"]}'.strip())
    log.info('need %s takeover ask %s', nid, result)
    return result


def deliver_router(session, pane, lead, kind, recs):
    """投给 Router（lead 非空）或负责 lead（lead 为空），同一套写前记账。投给 Router 的那一下过暂停门禁：
    记了 sending 之后才发现暂停，就退回 pending（恢复后发一次），不留在 sending。"""
    claimed = qstore.need_claim([(r['id'], qstore.geti(r, 'delivery_seq')) for r in recs])
    if not claimed:
        return
    ids = {c[0] for c in claimed}
    if lead:
        text = router_prompt(kind, [x for x in recs if x['id'] in ids])
    else:  # 投给负责 lead：按 claim 时记下的这次实际带上的消息写提示词
        text = lead_prompt([qstore.read_need(c[0]) for c in sorted(claimed)])
    r = (qstore.gated if lead else qstore.herdr)(session, 'agent', 'prompt', pane['pane_id'], text)
    if r.get('kind') == 'paused':
        qstore.need_unclaim(claimed, '全局暂停，没有发出')
        log.info('need %s paused before prompt %s', kind, ','.join(sorted(ids)))
        return
    if r['ok']:
        outcome, err = 'sent', ''
    elif r['kind'] in ('no_socket', 'exec_error') or r['code'] in qstore.NOT_SENT_CODES:
        outcome, err = 'held', f'{r["code"]} {r["message"]}'.strip()
    else:
        outcome, err = 'uncertain', f'{r["code"]} {r["message"]}'.strip()
    qstore.need_finish(claimed, outcome, err, lead)
    log.info('need %s %s %s %s', kind, outcome, ','.join(sorted(ids)), err)


HEALTH = {'at': 0.0, 'data': None}
HEALTH_LOCK = threading.Lock()


def router_health():
    """页面的 Router 健康状态；现场查 herdr，内存里缓存 10 秒。"""
    with HEALTH_LOCK:
        if HEALTH['data'] is not None and time.monotonic() - HEALTH['at'] < 10:
            return HEALTH['data']
        s = qstore.router_session()
        info, st = qstore.router_info(), qstore.ensure_state()
        sock = qstore.socket_exists(s)
        present, why = False, ''
        if not sock:
            why = f'herdr session {s} 不在'
        else:
            pane, why, _ = router_presence(info)
            present = bool(pane)
        HEALTH['data'] = {
            'session': s, 'socket': sock, 'registered': bool(info.get('lead_session')), 'present': present,
            'why': why, 'pane_id': info.get('pane_id') or '', 'registered_at': info.get('registered_at') or '',
            'last_delivery_at': info.get('last_delivery_at') or '', 'ensure_total': qstore.geti(st, 'total'),
            'ensure_last_at': st.get('last_at') or '', 'ensure_last_result': st.get('last_result') or '',
            'gave_up': bool(st.get('gave_up')), 'last_error': st.get('last_error') or '', 'paused': qstore.paused(),
            'reregister': (st.get('reregister') or {}).get('at') or ''}
        HEALTH['at'] = time.monotonic()
        return HEALTH['data']


def need_summary(rec):
    label, why, renot = qstore.need_delivery_view(rec)
    lead = rec.get('lead') or {}
    thread = []
    for x in rec.get('thread') or []:
        item = {'at': x.get('at'), 'by': x.get('by'), 'kind': x.get('kind'), 'rev': x.get('rev') or 0,
                'notified': bool(x.get('notified_at')), 'read': bool(x.get('read_at'))}
        if x.get('kind') == 'card':
            item['card'] = [[k, (x.get('card') or {}).get(k) or ''] for k in qstore.CARD_SECTIONS]
        elif x.get('kind') == 'reply':
            item['text'] = x.get('text') or ''
        thread.append(item)
    return {'id': rec['id'], 'text': rec.get('text') or '', 'status': rec.get('status'), 'discuss': rec.get('discuss') or '',
            'lead': (f'{lead.get("herdr_session")} · pane {lead.get("pane_id")}' if lead else ''),
            'card_rev': qstore.geti(rec, 'card_rev'), 'confirmed_rev': qstore.geti(rec, 'confirmed_rev'),
            'thread': thread, 'sealed': rec.get('status') in qstore.SEALED, 'ws': rec.get('ws') or '',
            'why': rec.get('why') or '', 'created': rec.get('created'), 'updated': rec.get('updated'),
            'issues': [u for u in rec.get('issues') or [] if isinstance(u, str) and qstore.ISSUE_URL_RE.match(u)],
            'notes': [{'at': n.get('at'), 'by': n.get('by'), 'text': n.get('text')} for n in rec.get('notes') or []],
            'pending_notes': len(qstore.pending_notes(rec)), 'delivery': label,
            'dispatch': (f'{(rec.get("dispatch") or {}).get("lead")} · pane {(rec.get("dispatch") or {}).get("pane_id")}'
                         if rec.get('dispatch') else ''), 'delivery_detail': why,
            'renotify': renot, 'log': (rec.get('log') or [])[-8:],
            'unread': len(qstore.unread_comments(rec)), 'loop': bool(rec.get('loop_suspect')),
            'receipt': qstore.receipt(rec)}


def needs_view():
    recs = sorted(qstore.all_needs().values(), key=lambda r: r['id'], reverse=True)
    return {'needs': [need_summary(r) for r in recs], 'router': router_health()}


def _comment_source(rec, c):
    own = qstore.own_comment(rec, c.get('id'))
    author = ((c.get('user') or {}).get('login') or '')
    if own:
        return ('Issue 评论 · 经 7788' if own.get('by') == 'owner' else 'Issue 评论 · 结论'), own.get('by') or 'owner'
    if qstore.owner_login() and author.lower() == qstore.owner_login():
        return 'Issue 评论 · 来源未核实', 'unverified'
    return f'Issue 评论 · {author or "?"}', 'team'


def need_detail(nid):
    rec = qstore.read_need(nid)
    refs = qstore.need_issue_refs(rec)
    tree, err = (None, '')
    if refs:
        tree, err = board._tree(refs[0][2])
    refs_all = list(refs)
    for sub in (tree or {}).get('subs') or []:
        if sub.get('url'):
            refs_all.append((sub.get('repo') or '', sub.get('number'), sub.get('url')))
    rows = {int(c.get('id') or 0): c for c in rec.get('comments') or []}
    timeline, errors = [], []
    if err:
        errors.append(f'根 Issue：{err}')
    for repo, number, url in refs_all:
        try:
            items = poll.issue_bodies(repo, number)
        except qstore.QError as e:
            errors.append(f'{repo}#{number}：{e.message}')
            continue
        for c in items:
            src, by = _comment_source(rec, c)
            row = rows.get(int(c.get('id') or 0)) or {}
            timeline.append({
                'kind': 'comment', 'at': c.get('updated_at') or c.get('created_at') or '',
                'id': c.get('id'), 'author': ((c.get('user') or {}).get('login') or ''), 'source': src,
                'self': by, 'state': row.get('state') or ('own' if by != 'team' else 'pending'),
                'edited': bool(c.get('created_at') and c.get('updated_at') and c.get('created_at') != c.get('updated_at')),
                'body_html': md(ghapi.strip_tag(c.get('body') or '')), 'url': c.get('html_url') or url,
                'receipt': {'notified': row.get('state') in ('notified', 'read'),
                            'read': row.get('state') == 'read', 'note': ''}})
    for x in rec.get('thread') or []:
        kind = x.get('kind')
        if kind == 'confirm':
            timeline.append({'kind': 'system', 'at': x.get('at') or '', 'text': f'Owner 确认理解卡 v{qstore.geti(x, "rev")}',
                             'source': '7788 讨论'})
            continue
        item = {'kind': 'thread', 'at': x.get('at') or '', 'by': x.get('by') or '', 'source': '7788 讨论', 'mid': x.get('mid') or '',
                'text': x.get('text') or '', 'rev': qstore.geti(x, 'rev'), 'kind2': kind,
                'receipt': {'notified': bool(x.get('notified_at')), 'read': bool(x.get('read_at')), 'note': ''}}
        if kind == 'card':
            item['card'] = [[k, (x.get('card') or {}).get(k) or ''] for k in qstore.CARD_SECTIONS]
            item['latest'] = qstore.geti(x, 'rev') == qstore.geti(rec, 'card_rev')
        if kind == 'issue':
            item['source'] = 'Issue 评论 · 结论'
        timeline.append(item)
    allq = qstore.all_questions()
    ref_set = {(r, n) for r, n, _ in refs_all}
    for qid, (m, _) in allq.items():
        mm = re.fullmatch(r'([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)', m.get('issue') or '')
        if not mm or (f'{mm.group(1).lower()}/{mm.group(2).lower()}', int(mm.group(3))) not in ref_set:
            continue
        q = {'kind': 'question', 'at': m.get('created') or '', 'id': qid, 'title': m.get('title') or qid,
             'qkind': qstore.kind_of(m), 'qstatus': m.get('status'), 'blocking': m.get('blocking') == 'yes',
             'invalid': qstore.invalid_reason(m, allq) or '',
             'source': '7788 问题卡', 'receipt': {'notified': False, 'read': False, 'note': ''}}
        if not q['invalid']:
            try:
                q['detail'] = detail_view(qid)  # 就地答题：带上问题、选项、版本和已提交回答
            except qstore.QError:
                q['invalid'] = '读取问题失败'
        timeline.append(q)
    # B3：投递记账不逐条显示；只在出问题时留一条可解释的系统事件
    notes = []
    if rec.get('loop_suspect'):
        notes.append('疑似循环：已停止自动投递')
    if rec.get('delivery_kind') == 'takeover' and rec.get('delivery') in ('pending', 'sending', 'held', 'uncertain', 'sent'):
        notes.append('负责 lead 不在，等待接手（已请 Router 安排）')
    elif rec.get('delivery_to') == 'lead' and rec.get('delivery') == 'held' and '负责 lead' in (rec.get('last_error') or ''):
        notes.append('消息暂缓：' + rec['last_error'])
    if rec.get('closed_notified'):
        notes.append('根 Issue 已关闭或 PR 全部合并，已发收尾通知')
    elif isinstance(rec.get('closeout'), dict):
        notes.append(f'收尾通知排队中（{rec["closeout"].get("why")}）')
    for t in (tree or {}).get('truncated') or []:
        errors.append('需求树没读全：' + t)
    if rec.get('last_error') and any(k in rec['last_error'] for k in ('起不来', 'GitHub 查询失败', '查询失败')):
        notes.append(rec['last_error'])
    for note in dict.fromkeys(notes):
        timeline.append({'kind': 'system', 'at': rec.get('updated') or '', 'text': note, 'source': '系统'})
    def _at(x):
        t = qstore.parse_time((x.get('at') or '').replace('Z', '+00:00'))
        return t.astimezone(qstore.TZ) if t else qstore.parse_time('1970-01-01T00:00:00+08:00')
    timeline.sort(key=_at)
    questions = []
    for q in timeline:
        if q['kind'] == 'question':
            questions.append({'open': q['qstatus'] == 'open' and not q['invalid'], 'kind': q['qkind']})
    st, blocked = board._lead_state(rec)
    col, flags = board.column(rec, tree, {'lead': st, 'questions': questions, 'blocked': blocked})
    label = dict(board.COLUMNS).get(col, col)
    root = (tree or {}).get('root') or {}
    return {'id': rec['id'], 'text': rec.get('text') or '', 'title': root.get('title') or (rec.get('text') or '').split('\n')[0],
            'body_html': md(root.get('body') or ''), 'lead_state': st,
            'number': root.get('number') or '',
            'status': rec.get('status'), 'discuss': rec.get('discuss') or '', 'ws': rec.get('ws') or '',
            'lead': rec.get('lead') or {},
            'can_takeover': bool(rec.get('lead') and st == 'absent' and rec.get('status') in qstore.LEAD_STATES),  # 手动重试；平时服务会自动叫
            'takeover_ask': rec.get('takeover_ask') if isinstance(rec.get('takeover_ask'), dict) else None,
            'card_rev': qstore.geti(rec, 'card_rev'), 'confirmed_rev': qstore.geti(rec, 'confirmed_rev'),
            'issues': [u for _, _, u in refs], 'github': refs[0][2] if refs else '',
            'column': col, 'column_label': label, 'flags': flags, 'sealed': rec.get('status') in qstore.SEALED,
            'timeline': timeline, 'unread': len(qstore.unread_comments(rec)), 'errors': errors,
            'receipt': qstore.receipt(rec), 'loop': bool(rec.get('loop_suspect')),
            'subs': (tree or {}).get('subs') or [], 'subs_progress': board._sub_progress((tree or {}).get('subs') or []),
            'prs': (tree or {}).get('prs') or [], 'created': rec.get('created'), 'updated': rec.get('updated'),
             'to_issue': bool(refs), 'sent_intents': {k: {'state': v.get('state'), 'comment_id': v.get('comment_id')}
                                                     for k, v in (rec.get('send_intents') or {}).items()}}


def board_view(query):
    ws = ''
    refresh = False
    for part in (query or '').split('&'):
        if part.startswith('ws='):
            ws = part[3:]
        if part == 'refresh=1':
            refresh = True
    return board.view(refresh=refresh, ws=ws)


def delivery_loop(stop):
    interval = float(os.environ.get('QI_SCAN_INTERVAL') or 5)
    polled = 0.0
    while not stop.is_set():
        try:
            scan_once()
        except Exception:  # 循环不能死；错误进日志
            log.exception('scan failed')
        try:
            scan_needs()
        except Exception:
            log.exception('need scan failed')
        interval_s = 60 if time.monotonic() - VIEW['at'] < 120 else 600
        if poll.degrade():
            interval_s = 600
        if time.monotonic() - polled >= interval_s:
            try:
                poll.scan()
            except Exception:
                log.exception('comment poll failed')
            polled = time.monotonic()
        stop.wait(interval)


VIEW = {'at': 0.0}


def maybe_poll():
    """打开看板 / 详情页时立即拉一次评论（有 45 秒的节流）。"""
    st = poll.scan_state()
    now = time.time()
    last = max((qstore.parse_time(v.get('at')) or qstore.parse_time('1970-01-01T00:00:00+08:00')
                for v in st.values() if isinstance(v, dict)), default=None)
    if last and (qstore.datetime.now(qstore.TZ) - last).total_seconds() < 45:
        return
    if not any(r.get('status') == 'filed' for r in qstore.all_needs().values()):
        return
    threading.Thread(target=poll.scan, daemon=True).start()


# ---------- 渲染：先 escape，再按白名单 ----------

def inline(s):
    parts = re.split(r'(`[^`]+`)', html.escape(s, quote=True))
    out = []
    for p in parts:
        if len(p) > 2 and p.startswith('`') and p.endswith('`'):
            out.append(f'<code>{p[1:-1]}</code>')
        else:
            out.append(re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', p))
    return ''.join(out)


FOLD_LINES = 12  # 证据里超过这么多行的代码块折叠显示


def md(text, fold=False):
    out, para, lst = [], [], None
    lines = text.split('\n')

    def flush():
        nonlocal lst
        if para:
            out.append('<p>' + '<br>'.join(inline(x) for x in para) + '</p>')
            para.clear()
        if lst:
            out.append(f'</{lst}>')
            lst = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith('```'):
            flush()
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith('```'):
                j += 1
            code = lines[i + 1:j]
            block = '<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>'
            if fold and len(code) > FOLD_LINES:
                block = f'<details><summary>代码（{len(code)} 行）</summary>{block}</details>'
            out.append(block)
            i = j + 1
            continue
        if line.lstrip().startswith('|'):
            flush()
            j = i
            while j < len(lines) and lines[j].lstrip().startswith('|'):
                j += 1
            out.append('<pre>' + html.escape('\n'.join(lines[i:j])) + '</pre>')
            i = j
            continue
        h = re.match(r'^(#{1,6})\s+(.*)$', line)
        li = re.match(r'^\s*([-*+]|\d+[.)])\s+(.*)$', line)
        if h:
            flush()
            n = min(len(h.group(1)) + 1, 6)
            out.append(f'<h{n}>{inline(h.group(2))}</h{n}>')
        elif li:
            if para:
                out.append('<p>' + '<br>'.join(inline(x) for x in para) + '</p>')
                para.clear()
            kind = 'ol' if li.group(1)[0].isdigit() else 'ul'
            if lst != kind:
                if lst:
                    out.append(f'</{lst}>')
                out.append(f'<{kind}>')
                lst = kind
            out.append(f'<li>{inline(li.group(2))}</li>')
        elif not line.strip():
            flush()
        else:
            if lst:
                out.append(f'</{lst}>')
                lst = None
            para.append(line)
        i += 1
    flush()
    return '\n'.join(out)


# ---------- 视图数据 ----------

CARD_SECTIONS = ('问题', '选项', '建议')  # 拆进 Qn 卡片
SHOWN_SECTIONS = CARD_SECTIONS + ('背景', '证据', '答完之后') + qstore.APPROVAL_SECTIONS  # 审批的动作 / 对象 / 后果单独醒目显示


def lead_view(lead):
    """展示用拆段：背景 / 证据 / 答完之后各自渲染；没拆进 Qn 卡片、也不属于这几段的行都进「补充材料」，不丢。"""
    secs = qstore.sections(lead)
    lines = lead.split('\n')
    infence = qstore.fenced(lines)
    first = next((i for i, l in enumerate(lines) if not infence[i] and re.match(r'^##\s+', l)), len(lines))
    extra = ['\n'.join(lines[:first]).strip()]
    rest = [l for l in secs.get('问题', '').split('\n') if l.strip() and not qstore.Q_LINE_RE.match(l.strip())]
    if rest:
        extra.append('#### 问题段的其他内容\n' + '\n'.join(rest))
    notes, cur, head = {}, None, []
    for line in secs.get('选项', '').split('\n'):
        m = re.match(r'^###\s+Q(\d+)\b', line.strip())
        if m:
            cur = f'Q{int(m.group(1))}'
            continue
        if cur and qstore.OPTION_RE.match(line) and not line.startswith('  '):
            continue  # 选项本身，已在卡片按钮上
        if cur:
            notes.setdefault(cur, []).append(line)
        elif line.strip():
            head.append(line)
    if head:
        extra.append('#### 选项段的其他内容\n' + '\n'.join(head))
    seen, rest = set(), []
    for line in secs.get('建议', '').split('\n'):
        m = qstore.SUGGEST_RE.match(line)
        if m and f'Q{int(m.group(1))}' not in seen:
            seen.add(f'Q{int(m.group(1))}')
        elif line.strip():
            rest.append(line)
    if rest:
        extra.append('#### 建议段的其他内容\n' + '\n'.join(rest))
    extra += [f'### {k}\n\n{v}' for k, v in secs.items() if k not in SHOWN_SECTIONS]
    return {'background_html': md(secs.get('背景', '')), 'evidence_html': md(secs.get('证据', ''), fold=True),
            'after_html': md(secs.get('答完之后', '')), 'extra_html': md('\n\n'.join(x for x in extra if x)),
            'approval_html': [[k, md(secs[k])] for k in qstore.APPROVAL_SECTIONS if secs.get(k)],
            'option_notes': {q: md('\n'.join(v).strip()) for q, v in notes.items() if '\n'.join(v).strip()}}


def summary(meta, allq):
    label, why, renot = qstore.delivery_view(meta)
    lead, sp = qstore.split_body(allq[meta['id']][1]) if meta.get('id') in allq else ('', {})
    dropped = set(q for q in (meta.get('withdrawn_qs') or '').split(',') if q)
    return {'id': meta['id'], 'kind': qstore.kind_of(meta), 'title': meta.get('title'), 'ws': meta.get('ws'),
            'blocking': meta.get('blocking'),
            'status': meta.get('status'), 'created': meta.get('created'), 'updated': meta.get('updated'),
            'answer_rev': qstore.geti(meta, 'answer_rev'), 'delivery': label, 'delivery_detail': why,
            'renotify': renot, 'invalid': qstore.invalid_reason(meta, allq),
            'closed_by': ('owner' if qstore.closed_by_owner(meta) else
                          'lead' if meta.get('status') in ('resolved', 'withdrawn') else ''),
            'issue': meta.get('issue'), 'content_rev': qstore.geti(meta, 'content_rev'),
            'n_questions': len([q for q, _ in qstore.questions(lead) if q not in dropped]),
            'has_draft': bool(qstore.draft(sp)['slots'])}


def list_view():
    allq = qstore.all_questions()
    talk = [{'id': r['id'], 'text': (r.get('text') or '').split('\n')[0][:120], 'ws': r.get('ws') or '',
             'card_rev': qstore.geti(r, 'card_rev'), 'updated': r.get('updated')}
            for r in sorted(qstore.all_needs().values(), key=lambda r: r['id'])
            if r.get('status') == 'discussing' and r.get('discuss') == 'waiting_owner']
    groups = {'todo': [], 'waiting': [], 'received': [], 'closed': []}
    for qid, (m, _) in allq.items():
        s = summary(m, allq)
        n = s['answer_rev']
        if s['invalid'] or m.get('status') in ('resolved', 'withdrawn'):
            groups['closed'].append(s)
        elif m.get('status') == 'open':
            groups['todo'].append(s)
        elif m.get('delivery') == 'received' and qstore.geti(m, 'received_rev') == n:
            groups['received'].append(s)
        else:
            groups['waiting'].append(s)
    groups['todo'].sort(key=lambda s: (s['blocking'] != 'yes', s['created'] or ''))
    groups['waiting'].sort(key=lambda s: s['updated'] or '')
    groups['received'].sort(key=lambda s: s['updated'] or '')
    groups['closed'] = sorted(groups['closed'], key=lambda s: s['updated'] or '', reverse=True)[:20]
    return {'open_count': len(groups['todo']) + len(talk), 'groups': groups, 'needs_talk': talk}


def detail_view(qid):
    allq = qstore.all_questions()
    if qid not in allq:
        qstore.path_of(qid)
        raise qstore.QError(404, f'{qid} 不存在')
    meta, body = allq[qid]
    lead, sp = qstore.split_body(body)
    invalid = qstore.invalid_reason(meta, allq)
    n = qstore.geti(meta, 'answer_rev')
    dropped = set(q for q in (meta.get('withdrawn_qs') or '').split(',') if q)
    opts, sugg = qstore.options(lead), qstore.suggestions(lead)
    s = summary(meta, allq)
    s.update({
        'issue': meta.get('issue'), 'group': meta.get('group'), 'content_rev': qstore.geti(meta, 'content_rev'),
        'warnings': [w for w in (meta.get('warnings') or '').split(' | ') if w],
        'received': meta.get('delivery') == 'received' and qstore.geti(meta, 'received_rev') == n and n > 0,
        'editable': not invalid and meta.get('status') in ('open', 'answered'),
        'closable': not invalid and meta.get('status') in ('open', 'answered'),
        'not_executed': (f'这条回答不会被执行，原因：{invalid}' if invalid and n > 0 else ''),
        'resolution': meta.get('resolution') if meta.get('status') == 'resolved' else '',
        'body_html': md(lead), 'errata_html': md(sp.get('勘误', '')), **lead_view(lead),
        'timeline': [l[2:] for l in sp.get('时间线', '').split('\n') if l.startswith('- ')],
        'questions': [{'q': q, 'text': t, 'options': opts.get(q, []), 'suggestion': sugg.get(q, ''),
                       'withdrawn': q in dropped} for q, t in qstore.questions(lead)],
        'draft': qstore.draft(sp), 'revs': qstore.answer_revs(sp),
    })
    return s


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = 'question-inbox'
    sys_version = ''
    token = ''

    def log_message(self, fmt, *args):  # 不记 URL 以外的内容；写请求只记路径
        log.debug('%s %s', self.command, self.path)

    def send(self, code, payload, ctype='application/json; charset=utf-8', extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def allowed_hosts(self):
        p = self.server.server_address[1]
        return {f'127.0.0.1:{p}', f'localhost:{p}'}

    def guard(self, write):
        """不通过时返回错误信息。"""
        host = self.headers.get('Host', '')
        if host not in self.allowed_hosts():
            return 'Host 不对'
        origin = self.headers.get('Origin')
        if origin is not None and origin != f'http://{host}':
            return 'Origin 不同源'
        if write:
            if origin is None:
                return '写请求必须带 Origin'
            ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if ctype != 'application/json':
                return '写请求只接受 application/json'
            if not hmac.compare_digest(self.headers.get('X-QI-Token', ''), self.token):
                return '缺少或错误的 X-QI-Token'
        return None

    def do_GET(self):
        bad = self.guard(write=False)
        if bad:
            return self.send(403, {'error': bad})
        path, _, query = self.path.partition('?')
        try:
            if path == '/':
                nonce = secrets.token_urlsafe(12)
                page = PAGE.replace('__TOKEN__', self.token).replace('__NONCE__', nonce)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                       f"connect-src 'self'; img-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
                return self.send(200, page.encode(), 'text/html; charset=utf-8', {'Content-Security-Policy': csp})
            if path == '/api/list':
                VIEW['at'] = time.monotonic()
                return self.send(200, list_view())
            if path == '/api/board':
                VIEW['at'] = time.monotonic()
                maybe_poll()
                return self.send(200, board_view(query))
            m = re.fullmatch(r'/api/need/(need-\d{4,})', path)
            if m:
                VIEW['at'] = time.monotonic()
                maybe_poll()
                return self.send(200, need_detail(m.group(1)))
            if path == '/api/needs':
                VIEW['at'] = time.monotonic()
                return self.send(200, needs_view())
            if path == '/api/tasks':  # 只读；refresh=1 只是跳过 60 秒缓存，不写任何东西
                try:
                    return self.send(200, tasks.view(refresh='refresh=1' in query.split('&')))
                except Exception as e:  # noqa: BLE001 页面显示「无法刷新：原因」，不当成没有任务
                    log.exception('tasks view failed')
                    return self.send(500, {'error': f'任务视图出错：{type(e).__name__} {e}'[:300]})
            m = re.fullmatch(r'/api/q/([A-Za-z0-9_.-]+)', path)
            if m:
                return self.send(200, detail_view(m.group(1)))
        except qstore.QError as e:
            return self.send(e.code, {'error': e.message})
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        bad = self.guard(write=True)
        if bad:
            log.warning('rejected write %s: %s', self.path, bad)
            return self.send(403, {'error': bad})
        m = re.fullmatch(r'/api/q/([A-Za-z0-9_.-]+)/(save|submit|renotify|close)', self.path)
        other = re.fullmatch(r'/api/(need)|/api/need/(need-\d{4,})/(reply|confirm|renotify|message|comment|unloop|takeover)'
                             r'|/api/router/(pause|retry|reregister)', self.path)
        if not m and not other:
            return self.send(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY:
                return self.send(413, {'error': '请求太大'})
            req = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {'error': '请求体不是合法 JSON 对象'})
        if other:
            return self.post_need(other, req)
        qid, action = m.groups()
        try:
            if action == 'save':
                qstore.save_draft(qid, int(req.get('content_rev', -1)), req.get('slots') or {})
                res = {'ok': True}
            elif action == 'submit':
                base = int(req.get('answer_rev', -1))
                n = qstore.submit(qid, int(req.get('content_rev', -1)), base, req.get('slots') or {})
                res = {'ok': True, 'answer_rev': n}
                if n == base:
                    res['unchanged'] = True
            elif action == 'close':
                st = qstore.owner_close(qid, int(req.get('content_rev', -1)), int(req.get('answer_rev', -1)),
                                        req.get('reason') if isinstance(req.get('reason'), str) else '')
                res = {'ok': True, 'status': st, 'note': '已由你关闭' + ('（等于撤回）' if st == 'withdrawn' else '')}
            else:
                qstore.renotify(qid)
                res = {'ok': True, 'note': '已重新排队；这条可能已经在 lead 的队列里'}
            log.info('%s %s ok', action, qid)
            return self.send(200, res)
        except qstore.QError as e:
            log.info('%s %s rejected %s', action, qid, e.code)
            return self.send(e.code, {'error': e.message})
        except (TypeError, ValueError):
            return self.send(400, {'error': '参数不合法'})

    def post_need(self, m, req):
        """需求 / Router 的写接口：和答题一样过 JSON、Host、Origin、token 四项校验（do_POST 里已做）。"""
        create, nid, action, ctl = m.groups()
        text = req.get('text') if isinstance(req.get('text'), str) else ''
        rid = req.get('request_id') if isinstance(req.get('request_id'), str) else ''
        try:
            if create:
                rec, new = qstore.create_need(text, rid)
                res = {'ok': True, 'id': rec['id'], 'created': new}
            elif action == 'reply':
                rec, new = qstore.owner_reply(nid, text, rid)
                res = {'ok': True, 'id': nid, 'created': new, 'to': 'lead' if rec.get('lead') else 'router',
                       'to_issue': False, 'receipt': qstore.receipt(rec)}
            elif action in ('message', 'comment'):
                body = req.get('text') if action == 'message' else req.get('body')
                res = send.owner_message(nid, body if isinstance(body, str) else '', rid, bool(req.get('to_issue')))
                res['ok'] = True
            elif action == 'confirm':
                if not isinstance(req.get('card_rev'), int) or isinstance(req.get('card_rev'), bool):
                    return self.send(400, {'error': 'card_rev 必须是整数'})
                rec, new = qstore.owner_confirm(nid, req['card_rev'], rid)
                res = {'ok': True, 'id': nid, 'created': new, 'card_rev': rec.get('confirmed_rev')}
            elif action == 'renotify':
                qstore.need_renotify(nid)
                res = {'ok': True, 'note': '已重新排队；这条可能已经在 Router 的队列里'}
            elif action == 'unloop':
                qstore.unloop(nid)
                res = {'ok': True, 'note': '已解除疑似循环，held 的评论恢复投递'}
            elif action == 'takeover':  # 负责 lead 确认不在：手动重试「请 Router 安排接手」（平时服务会自动叫；查 herdr 不持锁）
                cur = qstore.read_need(nid)
                lead = cur.get('lead') or {}
                st = qstore.presence(lead.get('herdr_session'), lead.get('lead_session'))[0] if lead else 'absent'
                got = ask_router_takeover(qstore.request_takeover(nid, st), force=True)
                if got in ('paused', 'router_absent'):
                    return self.send(409, {'error': '全局暂停中：没有叫 Router' if got == 'paused' else 'Router 暂时不在，已记录，稍后会再叫'})
                res = {'ok': True, 'note': '已请 Router 安排接手；接手的 lead 会读到没读的消息'}
            elif ctl == 'pause':
                if not isinstance(req.get('paused'), bool):
                    return self.send(400, {'error': 'paused 必须是 true / false'})
                qstore.set_paused(req['paused'])
                res = {'ok': True, 'paused': req['paused']}
            elif ctl == 'reregister':  # 不重启 Router 的会话，只请那个名为 router 的 agent 重新登记一次
                if qstore.paused():
                    return self.send(409, {'error': '全局暂停中'})
                qstore.ensure_note('reregister', None)
                res = {'ok': True, 'note': kick_ensure()}
            else:
                qstore.ensure_retry()
                res = {'ok': True, 'note': '已清除「起不来」标记，下次有需求要投递时会重新拉起'}
            with HEALTH_LOCK:
                HEALTH['data'] = None
            log.info('%s ok', self.path)
            return self.send(200, res)
        except qstore.QError as e:
            log.info('%s rejected %s', self.path, e.code)
            return self.send(e.code, {'error': e.message})

    def do_PUT(self):
        self.send(405, {'error': 'method not allowed'})

    do_DELETE = do_PATCH = do_PUT


# ---------- 启动 ----------

def main():
    sd = qstore.state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    logging.basicConfig(filename=str(sd / 'server.log'), level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    lock = open(sd / 'server.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('已有一个 question-inbox 服务在运行', file=sys.stderr)
        return 1
    for c in qstore.repair() + qstore.repair_needs():  # 开放写入和投递之前
        log.info('repair %s', c)
    token = secrets.token_hex(24)
    tp = sd / 'token'
    fd = os.open(tp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(token + '\n')
    os.chmod(tp, 0o600)
    Handler.token = token
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', qstore.port()), Handler)
    except OSError as e:
        print(f'无法绑定 127.0.0.1:{qstore.port()}：{e}', file=sys.stderr)
        log.error('bind failed: %s', e)
        return 1
    httpd.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=delivery_loop, args=(stop,), daemon=True).start()
    log.info('serving on 127.0.0.1:%s, inbox %s', qstore.port(), qstore.root())
    print(f'question-inbox: http://127.0.0.1:{qstore.port()}/', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="qi-token" content="__TOKEN__">
<title>问题收件箱</title>
<style>
:root{--bg:#F7F8FA;--surface:#FFFFFF;--text:#1F2937;--muted:#5B6472;--border:#D8DEE8;--accent:#2563EB;--on-accent:#FFFFFF;--accent-weak:#EAF1FE;
--warn-text:#9A4D00;--warn-bg:#FFF4E5;--bad-text:#B42318;--bad-bg:#FFF0EE;--ok-text:#18794E;--ok-bg:#EDF8F1;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#11151C;--surface:#1A2029;--text:#EDF2F7;--muted:#A8B3C2;--border:#3A4656;--accent:#8AB4FF;--on-accent:#101826;--accent-weak:#1C2A42;
--warn-text:#FFC078;--warn-bg:#382A1A;--bad-text:#FFB4AB;--bad-bg:#3D2024;--ok-text:#8DDFB5;--ok-bg:#183329}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--accent)}
#modes{display:flex;gap:4px;align-items:center;height:44px;padding:0 12px;border-bottom:1px solid var(--border);background:var(--surface)}
#modes a{padding:6px 12px;border-radius:8px;text-decoration:none;color:var(--muted);font-size:14px;font-weight:600}
#modes a[aria-current]{background:var(--accent-weak);color:var(--accent)}
#app{display:grid;grid-template-columns:280px minmax(0,1fr);height:calc(100vh - 44px)}
#app[data-view=tasks],#app[data-view=needs]{grid-template-columns:minmax(0,1fr)}
#app[data-view=tasks] #side,#app[data-view=needs] #side{display:none}
.needs{max-width:1040px}
.nform{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin:12px 0}
.nform h2{font-size:15px;margin:0}.nform .acts{margin-top:8px}
.rcard{border:1px solid var(--border);border-radius:10px;padding:10px 16px;margin:12px 0;background:var(--surface)}
.rcard.bad{border:2px solid var(--bad-text)}
.nrow{padding:10px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);margin:10px 0;font-size:14px;line-height:1.55}
.nrow .t1{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;color:var(--muted);font-size:13px}
.nrow .ntext{font-weight:600;margin:4px 0;white-space:pre-wrap;word-break:break-word}
.nrow .iss{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0}
.nrow .nt{margin:2px 0;font-size:13px;white-space:pre-wrap;word-break:break-word}
.nrow .nt.owner{color:var(--text)}.nrow .nt.router{color:var(--muted)}
.plain{white-space:pre-wrap;word-break:break-word}
.nrow.focus{outline:2px solid var(--accent)}
.thread{border-left:3px solid var(--border);padding-left:10px;margin:6px 0}
.ncard{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:6px 0;background:var(--bg)}
.ncard.cur{border:2px solid var(--accent)}.ncard .csec{margin:4px 0}.ncard .csec strong{font-size:13px;color:var(--muted)}
.nbox{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}.nbox textarea{flex:1 1 280px;min-height:44px}
.tasks{max-width:1040px}
.tgrp h2{font-size:15px;margin:24px 0 6px;display:flex;gap:8px;align-items:baseline}
.tgrp.you h2{color:var(--warn-text)}.tgrp.stuck h2{color:var(--bad-text)}.tgrp.closed{opacity:.8}
.tgrp h3{font-size:13px;color:var(--muted);margin:10px 0 2px;font-weight:600}
.trow{padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface);margin:6px 0;font-size:14px;line-height:1.5}
.trow.d1{margin-left:24px}.trow.d2{margin-left:48px}
.trow.pref{background:transparent;border-style:dashed;color:var(--muted)}
.trow .t1{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:var(--muted);font-size:13px}
.trow .t2{font-weight:600;margin:2px 0}
.trow .t3{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 12px}
.chip.info{background:var(--accent-weak);color:var(--accent)}.chip.mute{background:var(--bg);color:var(--muted);border:1px solid var(--border)}
.thead{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
#side{border-right:1px solid var(--border);overflow:auto;padding:14px 10px 24px;background:var(--surface)}
#main{overflow:auto;position:relative}
.brand{display:flex;justify-content:space-between;align-items:baseline;font-weight:600;padding:0 8px 6px}
.brand .count{font-size:13px;color:var(--muted);font-weight:400}
.grp h2{font-size:13px;margin:16px 8px 4px;font-weight:600;display:flex;justify-content:space-between}
.grp.hot h2{color:var(--warn-text)}
.grp h3{font-size:13px;font-weight:600;color:var(--muted);margin:8px 8px 2px;display:flex;justify-content:space-between}
.grp .n{font-weight:400;color:var(--muted)}
.grp.dim{opacity:.78}.grp.dim h2{color:var(--muted)}
.item{display:block;padding:6px 8px;border-radius:8px;color:inherit;text-decoration:none;border-left:3px solid transparent;font-size:13px;line-height:1.5}
.item:hover{background:var(--bg)}
.item.cur{background:var(--accent-weak);border-left-color:var(--accent)}
.item .l1{color:var(--muted);display:flex;align-items:center;gap:6px}
.item .l2{display:flex;gap:8px;font-size:14px}
.item .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item time,.item .l3{color:var(--muted);font-size:12px;white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);display:inline-block;flex:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.tag{font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--border);color:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:16px 8px}
.wrap{max-width:880px;margin:0 auto;padding:24px 32px 32px}
.prose{max-width:72ch}
.mute{color:var(--muted)}
.small{font-size:13px;line-height:1.5}
.back{display:none;font-size:15px;text-decoration:none;margin-bottom:8px}
.where{display:flex;justify-content:space-between;align-items:center;color:var(--muted);font-size:13px}
h1{font-size:24px;line-height:1.3;margin:4px 0 6px}
.state{font-size:14px;color:var(--muted)}
.chip{display:inline-block;font-size:13px;padding:0 8px;border-radius:10px;font-weight:600}
.chip.hot{background:var(--warn-bg);color:var(--warn-text)}
.chip.bad{background:var(--bad-bg);color:var(--bad-text)}
.chip.ok{background:var(--ok-bg);color:var(--ok-text)}
.more{position:relative}
.more>summary{list-style:none;cursor:pointer;padding:2px 10px;border-radius:6px;border:1px solid var(--border);font-size:16px;line-height:1.4}
.more>summary::-webkit-details-marker{display:none}
.more .pop{position:absolute;right:0;top:34px;z-index:5;width:min(420px,80vw);background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;box-shadow:0 6px 24px rgba(0,0,0,.15);color:var(--text);font-size:14px}
.msgbar{padding:8px 12px;border-radius:8px;margin:8px 0;font-size:14px}
.msgbar.warn{background:var(--warn-bg);color:var(--warn-text)}.msgbar.bad{background:var(--bad-bg);color:var(--bad-text)}
.msgbar.ok{background:var(--ok-bg);color:var(--ok-text)}.msgbar.info{background:var(--accent-weak);color:var(--text)}
.msgbar button{margin-left:12px}
.banner{padding:14px 16px;border-radius:10px;margin:12px 0;font-size:17px;border:2px solid currentColor}
.banner .small{font-size:13px;margin-top:4px}.banner button{margin-left:12px;font-size:14px}
.banner.ok{background:var(--ok-bg);color:var(--ok-text)}.banner.info{background:var(--accent-weak);color:var(--accent)}
.banner.bad{background:var(--bad-bg);color:var(--bad-text)}
.sec{margin:18px 0}
.sec>h2,.sec>summary{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 4px;cursor:default}
.sec>summary{cursor:pointer}
.sec p{margin:0 0 8px}.sec ul,.sec ol{margin:0 0 8px;padding-left:22px}
.sec.appr{border-left:3px solid var(--warn-text);padding-left:12px}.sec.appr>h2{color:var(--warn-text)}
.after{border-left:3px solid var(--border);padding:2px 12px;margin:18px 0;display:flex;gap:8px;align-items:baseline}
.after .mute{flex:none}.after p{margin:0}
.errata>summary{color:var(--warn-text)}
pre{background:var(--bg);border:1px solid var(--border);padding:8px 10px;overflow:auto;border-radius:6px;font-size:13px;line-height:1.5}
code{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);padding:0 3px;border-radius:3px;word-break:break-all}
pre code{padding:0;word-break:normal}
.nav{position:sticky;top:0;z-index:3;background:var(--bg);padding:8px 0;margin:12px 0 4px;border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px}
.nav .anc{min-width:44px;padding:2px 8px;border-radius:6px;border:1px solid var(--muted);background:var(--surface);color:var(--text);font:inherit;cursor:pointer}
.nav .anc.done{border-color:var(--accent);background:var(--accent-weak)}
.nav .anc.gone{text-decoration:line-through;color:var(--muted);border-color:var(--border)}
.nav .next{margin-left:auto}
.q{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin:16px 0;scroll-margin-top:64px}
.q.focus{outline:2px solid var(--accent);outline-offset:2px}
.q.gone{opacity:.7}
.q h3{font-size:17px;line-height:1.5;font-weight:600;margin:0 0 10px;max-width:72ch}
.qn{color:var(--accent);margin-right:4px}
.opts{display:flex;flex-direction:column;gap:8px}
.opt{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 12px;border:1px solid var(--muted);border-radius:8px;cursor:pointer;background:var(--surface)}
.opt.sug{background:var(--accent-weak)}
.opt:has(input:checked){border:2px solid var(--accent);padding:7px 11px}
.opt:has(input:checked)::after{content:'已选';margin-left:auto;font-size:13px;color:var(--accent);font-weight:600}
.opt input{accent-color:var(--accent);width:18px;height:18px;margin:0;flex:none}
.opt .ot{flex:1;min-width:0}
kbd{font:12px/1.4 ui-monospace,Menlo,monospace;border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;padding:0 5px;color:var(--muted);background:var(--bg)}
.badge{font-size:12px;font-weight:600;padding:0 8px;border-radius:9px;background:var(--accent);color:var(--on-accent);flex:none}
.why{color:var(--muted);font-size:14px;margin:6px 0 0 4px}
.sugrow{margin-top:8px}
.note{font-size:14px;margin-top:8px;color:var(--muted)}
.fold{margin-top:8px}
.fold>summary{display:inline-block;cursor:pointer;color:var(--accent);font-size:14px;padding:4px 2px;list-style:none}
.fold>summary::-webkit-details-marker{display:none}
.others{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.others .opt{flex:1 1 180px}
textarea{width:100%;min-height:56px;font-family:inherit;font-size:16px;line-height:1.5;background:var(--bg);color:var(--text);border:1px solid var(--muted);border-radius:8px;padding:8px 10px;margin-top:6px}
button{font:inherit;font-size:15px;min-height:40px;padding:6px 14px;border-radius:8px;border:1px solid var(--muted);background:var(--surface);color:var(--text);cursor:pointer}
button.pri{background:var(--accent);color:var(--on-accent);border-color:var(--accent);font-weight:600}
button.pri kbd{background:transparent;color:inherit;border-color:currentColor;margin-left:6px;opacity:.8}
button:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible,.opt:has(input:focus-visible),a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ans{margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--bg);border:1px solid var(--border)}
.ans .k{font-size:13px;color:var(--muted)}
.olist{margin:0;padding:0;list-style:none;color:var(--muted)}.olist li{margin:2px 0}.olist .badge{margin-left:8px}
.rev{margin:12px 0;font-size:14px}
.dbar{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.bar-b{position:sticky;bottom:0;z-index:4;background:var(--surface);border-top:1px solid var(--border);margin:24px -32px -32px;padding:10px 32px calc(10px + env(safe-area-inset-bottom))}
.bar-b .row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.bar-b .prog{display:flex;align-items:center;gap:10px;font-size:14px;flex:1;min-width:200px}
.pb{width:120px;height:6px;border-radius:3px;background:var(--border);overflow:hidden;flex:none}
.pb span{display:block;height:100%;background:var(--accent)}
.acts{display:flex;gap:8px;flex-wrap:wrap}
.foot{margin:28px 0 8px;font-size:13px;color:var(--muted)}
.foot details{margin:6px 0}.foot summary{cursor:pointer}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:6px 0}
#sheet,#help{display:none}
#sheet[data-open],#help[data-open]{display:flex;position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.45);align-items:center;justify-content:center;padding:16px}
.dlg{background:var(--surface);color:var(--text);border-radius:12px;max-width:760px;width:100%;max-height:calc(100vh - 32px);overflow:auto;padding:20px 24px;box-shadow:0 12px 40px rgba(0,0,0,.3)}
.dlg h2{font-size:18px;margin:0 0 4px}
.srow{display:grid;grid-template-columns:40px minmax(0,1fr);gap:4px 10px;padding:10px 12px;border-radius:8px;margin:6px 0;border:1px solid var(--border);cursor:pointer}
.srow .qq{font-size:13px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.srow .dec{font-weight:600}
.srow .tg{font-size:12px;font-weight:600;margin-left:8px;padding:0 6px;border-radius:8px;background:var(--ok-bg);color:var(--ok-text)}
.srow.warn{background:var(--warn-bg);border-color:transparent}.srow.warn .tg{background:var(--surface);color:var(--warn-text)}
.srow.miss{background:var(--bad-bg);border-color:transparent}.srow.miss .dec{color:var(--bad-text)}
.dlg .btns{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
.keys{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:10px 0}
@media (max-width:760px){
  #app{display:block;height:auto}
  #modes{position:sticky;top:0;z-index:6}
  .trow.d1{margin-left:12px}.trow.d2{margin-left:24px}
  #app[data-view=detail] #side{display:none}
  #app[data-view=list] #main{display:none}
  #side{border-right:0;min-height:100vh}
  #main{overflow:visible}
  .wrap{padding:12px 16px 16px}
  .back{display:inline-block}
  h1{font-size:20px;line-height:1.4}
  .q{padding:14px 14px}
  .nav{overflow-x:auto;flex-wrap:nowrap}
  .nav .next{margin-left:8px}
  .bar-b{margin:20px -16px -16px;padding:8px 16px calc(8px + env(safe-area-inset-bottom))}
  .bar-b .row{gap:8px}
  .acts{width:100%}.acts button{flex:1;padding:6px 8px}
  .acts kbd,.bar-b>p,.prog .hint{display:none}
  .item{padding:10px 8px}
  #sheet[data-open],#help[data-open]{padding:8px}
  .dlg{padding:16px 14px;max-height:calc(100vh - 16px)}
  .srow{padding:8px 10px}
}
:root[data-theme="light"]{--bg:#F7F8FA;--surface:#FFFFFF;--text:#1F2937;--muted:#5B6472;--border:#D8DEE8;--accent:#2563EB;--on-accent:#FFFFFF;--accent-weak:#EAF1FE;--warn-text:#9A4D00;--warn-bg:#FFF4E5;--bad-text:#B42318;--bad-bg:#FFF0EE;--ok-text:#18794E;--ok-bg:#EDF8F1;color-scheme:light}
:root[data-theme="dark"]{--bg:#11151C;--surface:#1A2029;--text:#EDF2F7;--muted:#A8B3C2;--border:#3A4656;--accent:#8AB4FF;--on-accent:#101826;--accent-weak:#1C2A42;--warn-text:#FFC078;--warn-bg:#382A1A;--bad-text:#FFB4AB;--bad-bg:#3D2024;--ok-text:#8DDFB5;--ok-bg:#183329;color-scheme:dark}
/* ---------- v2：线框 token + rail / side / board / detail ---------- */
:root{
  --canvas:var(--bg); --panel:var(--surface); --side-bg:var(--bg); --hover:var(--bg);
  --line:var(--border); --line-2:var(--border); --fg:var(--text); --fg-2:var(--muted);
  --mute:var(--muted); --faint:var(--muted); --accent:var(--accent); --accent-soft:var(--accent-weak);
  --s-wait:var(--warn-text); --s-wait-soft:var(--warn-bg); --s-bad:var(--bad-text); --s-bad-soft:var(--bad-bg);
  --s-prog:#d9a10b; --s-review:var(--ok-text); --s-done:var(--accent); --s-todo:var(--faint);
  --mono:ui-monospace,"SF Mono",Menlo,monospace;
}
body{background:var(--canvas);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Segoe UI",sans-serif;margin:0}
#app{display:grid;grid-template-columns:64px 196px minmax(0,1fr);height:100vh;max-width:none;border:0;border-radius:0;box-shadow:none;background:var(--panel);overflow:hidden}
#app[data-view=board],#app[data-view=need],#app[data-view=list],#app[data-view=tasks]{grid-template-columns:64px 196px minmax(0,1fr)}
#app[data-view=board] #side,#app[data-view=need] #side,#app[data-view=list] #side,#app[data-view=tasks] #side{display:flex}
#modes{grid-column:1;display:flex;flex-direction:column;align-items:center;gap:5px;height:auto;padding:14px 0;border:0;border-right:1px solid var(--line);background:var(--side-bg);z-index:4}
#modes a{width:48px;padding:7px 0 5px;border-radius:10px;display:flex;flex-direction:column;align-items:center;gap:2px;color:var(--mute);font-size:11px;position:relative;text-decoration:none}
#modes a:hover{background:var(--hover);color:var(--fg-2)}
#modes a.on{background:var(--hover);color:var(--fg)}
#modes .badge{position:absolute;top:3px;right:7px;min-width:16px;height:16px;padding:0 4px;border-radius:8px;background:var(--accent);color:#fff;font-size:10px;line-height:16px;text-align:center}
.ws-logo{width:32px;height:32px;border-radius:9px;background:var(--fg);color:var(--panel);display:grid;place-items:center;font-weight:600;font-size:14px;margin-bottom:10px}
#side{grid-column:2;display:flex;flex-direction:column;gap:2px;background:var(--side-bg);border-right:1px solid var(--line);padding:18px 10px;overflow:auto;min-height:0}
.side-head{padding:0 8px 12px}.side-head h2{font-size:15px;font-weight:600;margin:0}
.side-link{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;color:var(--fg-2);font-size:13.5px;text-decoration:none}
.side-link:hover{background:var(--hover)}
.side-link.on{background:var(--panel);color:var(--fg);box-shadow:0 1px 2px rgba(24,24,27,.04);border:1px solid var(--line)}
.side-link .n{margin-left:auto;font:12px var(--mono);color:var(--faint)}
.side-lbl{font-size:12px;color:var(--mute);margin:14px 8px 4px}
.sq{width:9px;height:9px;border-radius:3px;display:inline-block;flex:none}
.side-foot{margin-top:auto;font-size:12px;color:var(--mute);padding:8px}
.side-foot a{color:inherit;text-decoration:none}
#main{grid-column:3;min-width:0;overflow:auto;position:relative;background:var(--panel)}
.boardwrap,.detailwrap{max-width:1320px}
.boardhead{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:6px 0}
.boardhead h1{margin:0;margin-right:auto;font-size:18px}
.wsfilter{display:flex;gap:4px;flex-wrap:wrap;font-size:13px}
.wsfilter a{padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--mute);text-decoration:none}
.wsfilter a.on{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.board{display:grid;grid-template-columns:repeat(6,minmax(178px,1fr));gap:12px;padding:14px 0;overflow-x:auto;align-items:start}
.bcol{min-width:0}
.colh{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;padding:2px 4px 10px}
.colh svg{color:var(--s-todo)}.c-wait .colh svg{color:var(--s-wait)}.c-stuck .colh svg{color:var(--s-bad)}
.c-review .colh svg{color:var(--s-review)}.c-doing .colh svg{color:var(--s-prog)}.c-done .colh svg{color:var(--s-done)}
.colh .c{font:12px var(--mono);color:var(--faint);font-weight:400}
.cards{display:flex;flex-direction:column;gap:10px}
.bcard{display:flex;flex-direction:column;gap:7px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:11px 12px;position:relative;box-shadow:0 1px 2px rgba(24,24,27,.04)}
.bcard:hover{border-color:var(--line-2);box-shadow:0 4px 14px rgba(24,24,27,.07)}
.bcard.bad{border-color:var(--s-bad)}
.bcard .row1{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mute)}
.bcard .row1 .when{margin-left:auto}
.bcard .bt{font-size:14px;font-weight:500;color:var(--fg);text-decoration:none;line-height:1.45}
.bcard .bt:hover{text-decoration:underline}
.bcard .row3{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mute);flex-wrap:wrap}
.bcard .row3 .next{color:var(--fg-2)}
.bcard .arch{position:static;align-self:flex-end;font-size:11px;min-height:24px;padding:1px 7px;opacity:.65}
.live{display:inline-flex;align-items:center;gap:5px;color:var(--fg-2);font-size:12px}
.pulse{width:7px;height:7px;border-radius:50%;background:#30a46c;box-shadow:0 0 0 3px rgba(48,164,108,.18);display:inline-block}
.ws{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:var(--fg-2);background:var(--hover);border-radius:6px;padding:1px 6px}
.unread{width:7px;height:7px;border-radius:50%;background:var(--accent);display:inline-block}
.chip.info{background:var(--accent-soft);color:var(--accent)}
.crumb{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mute);flex-wrap:wrap}
.crumb .sep{color:var(--faint)}.crumb a{color:inherit}
.chipw{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mute);cursor:default;user-select:none}
.chipw.live{color:var(--ok-text)}
.detailwrap{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:26px;align-items:start;max-width:1180px}
.detailwrap>header,.detailwrap>.body,.detailwrap>.subs,.detailwrap>.subhd,.detailwrap>.sech,.detailwrap>.tl,.detailwrap>.composer,.detailwrap>.msgbar{grid-column:1}
.detailwrap>aside.props{grid-column:2;grid-row:1 / span 20;border-left:1px solid var(--line);padding-left:20px;position:sticky;top:12px}
.detailwrap h1{font-size:26px;font-weight:650;letter-spacing:-.01em;line-height:1.3;margin:8px 0}
.body{color:var(--fg-2);font-size:14.5px;line-height:1.75;max-width:72ch}
.subhd{font-size:13px;color:var(--mute);display:flex;gap:7px;align-items:center;margin-bottom:4px}
.subs{display:flex;flex-direction:column;gap:2px}
.subrow{display:flex;align-items:center;gap:9px;padding:5px 4px;font-size:14px}
.subrow svg{color:var(--s-todo)}.subrow .k{font:12px var(--mono);color:var(--mute);width:52px}
.subrow .r{margin-left:auto;font-size:12px;color:var(--mute)}
.sech{font-size:15px;font-weight:600;display:flex;gap:10px;align-items:baseline;margin-top:6px}
.sech span{font-size:12px;font-weight:400;color:var(--mute)}
.tl{display:flex;flex-direction:column;gap:12px}
.msg{display:grid;grid-template-columns:28px minmax(0,1fr);gap:10px}
.av{width:26px;height:26px;border-radius:50%;display:inline-grid;place-items:center;font-size:11px;font-weight:600;color:#fff;background:#3f3f46}
.av.lg{width:28px;height:28px;font-size:12px}
.a-me{background:#3f3f46}.a-axis{background:#5b5bd6}.a-team{background:#e5484d}
.bubble{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 13px;min-width:0;box-shadow:0 1px 2px rgba(24,24,27,.04)}
.bubble.soft{background:var(--side-bg)}
.bubble.ask{border-color:color-mix(in srgb,var(--s-wait) 45%,var(--line))}
.bubble .mh{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mute);margin-bottom:4px;flex-wrap:wrap}
.bubble .mh b{color:var(--fg);font-size:13px}
.bubble .mh svg{color:var(--s-wait)}
.mtext{font-size:14px;color:var(--fg-2);line-height:1.65;white-space:pre-wrap;word-break:break-word}
.src{font-size:10.5px;color:var(--mute);border:1px solid var(--line);border-radius:5px;padding:0 5px}
.src.src-7788{background:var(--side-bg)}
.receipt{font-size:12px;color:var(--mute);margin-top:6px}
.receipt .ok{color:var(--ok-text)}
.sys{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--mute);padding-left:38px;flex-wrap:wrap}
.sys::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--faint)}
.tlq{margin:2px 0 2px 0}
.tlq .opts{display:flex;flex-direction:row;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:flex-start}
.tlq .opt{display:inline-flex;align-items:center;gap:7px;min-height:32px;width:auto;flex:0 0 auto;padding:4px 12px;font-size:13px;border-radius:8px;border:1px solid var(--line-2);background:var(--panel);color:var(--fg-2);cursor:pointer}
.tlq .opt.go{background:var(--fg);color:var(--panel);border-color:var(--fg)}
.tlq .opt input{width:14px;height:14px;margin:0;accent-color:var(--accent)}
.tlq .iq{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
.tlq .qtext{font-size:14px;line-height:1.55;margin-bottom:6px}
.tlq .qn{color:var(--accent);margin-right:5px}
.tlq .why{font-size:12px;color:var(--mute);margin:4px 0}
.tlq a.ghost{display:inline-flex;align-items:center;min-height:32px;padding:4px 10px;border-radius:8px;border:1px solid var(--line-2);color:var(--fg-2);text-decoration:none;font-size:13px}
.csec{margin:3px 0}.csec strong{font-size:12.5px;color:var(--mute)}
.rowflags{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.flag{display:inline-flex;font-size:11.5px;border-radius:6px;padding:1px 7px;align-self:flex-start}
.f-wait{background:var(--s-wait-soft);color:var(--s-wait)}.f-bad{background:var(--s-bad-soft);color:var(--s-bad)}.f-mute{background:var(--hover);color:var(--mute);border:1px solid var(--line)}
.composer{border:1px solid var(--line-2);border-radius:14px;padding:11px 13px;background:var(--panel);margin:10px 0 24px;box-shadow:0 1px 2px rgba(24,24,27,.04)}
.composer textarea{margin-top:0;min-height:52px;border:0;background:transparent;padding:2px 0}
.composer .row{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--mute);flex-wrap:wrap;margin-top:8px}
.composer .send{margin-left:auto;width:30px;height:30px;min-height:30px;padding:0;border-radius:50%;background:var(--fg);color:var(--panel);border:0;display:grid;place-items:center}
.composer .hint{margin-top:6px}
.chk{display:inline-flex;align-items:center;gap:5px;color:var(--fg-2)}
.chk input{width:15px;height:15px;margin:0;accent-color:var(--accent)}
.props h4{font-size:13px;margin:0 0 8px}
.prop{display:grid;grid-template-columns:64px minmax(0,1fr);gap:8px;font-size:13px;align-items:center}
.prop .k{color:var(--mute)}.prop .v{display:flex;align-items:center;gap:6px;color:var(--fg);min-width:0;overflow-wrap:anywhere}
.waitbox{display:flex;flex-direction:column;gap:5px;margin:16px 0;padding:10px;border-radius:10px;background:var(--s-wait-soft)}
.pr{display:flex;flex-direction:column;gap:2px;padding:8px 0;border-top:1px solid var(--line);font-size:13px}
.pr:first-of-type{border-top:0}.pr .l1 a{color:var(--fg);text-decoration:none}
.pr .l2{font:12px var(--mono);color:var(--mute)}
.msgbar{padding:8px 12px;border-radius:8px;margin:8px 0;font-size:14px}
.msgbar.warn{background:var(--warn-bg);color:var(--warn-text)}.msgbar.bad{background:var(--bad-bg);color:var(--bad-text)}
.msgbar.ok{background:var(--ok-bg);color:var(--ok-text)}.msgbar.info{background:var(--accent-soft);color:var(--text)}
.inboxcards{max-width:920px}
.router-mini{margin:18px 0 8px;font-size:13px;color:var(--mute)}
.router-mini>summary{cursor:pointer;padding:4px 0}
.router-mini .rcard{margin:6px 0;box-shadow:none}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--canvas:var(--bg)}}
@media (max-width:760px){
  #app,#app[data-view=board],#app[data-view=need],#app[data-view=list],#app[data-view=tasks]{
    display:grid!important;grid-template-columns:1fr!important;grid-template-rows:auto minmax(0,1fr);height:auto!important;overflow:visible}
  #modes{grid-column:1!important;grid-row:1;flex-direction:row!important;justify-content:flex-start;padding:8px;border-right:0;border-bottom:1px solid var(--line);height:auto;position:static!important}
  .ws-logo{margin:0 8px 0 0}
  #app[data-view=board] #side,#app[data-view=need] #side,#app[data-view=list] #side,#app[data-view=tasks] #side,#app #side{display:none!important}
  #main{grid-column:1!important;grid-row:2;min-height:calc(100vh - 58px);width:auto}
  .boardwrap,.detailwrap{padding:0 4px}
  .detailwrap{display:block;max-width:100%}
  .detailwrap>aside.props{border-left:0;border-top:1px solid var(--line);padding:14px 0 0;position:static;margin-top:18px}
  .detailwrap>header,.detailwrap>.body,.detailwrap>.subs,.detailwrap>.subhd,.detailwrap>.sech,.detailwrap>.tl,.detailwrap>.composer,.detailwrap>.msgbar{grid-column:1}
  .board{grid-template-columns:repeat(6,minmax(160px,1fr))}
  .detailwrap h1{font-size:21px}
  .wrap{padding:12px 14px 18px}
  .boardhead .primary{width:auto}
}
</style></head><body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="st-todo" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.2 2"/></symbol>
  <symbol id="st-prog" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7 3.5a3.5 3.5 0 0 1 0 7z" fill="currentColor"/></symbol>
  <symbol id="st-review" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7 3.5a3.5 3.5 0 1 1-3.03 5.25L7 7z" fill="currentColor"/></symbol>
  <symbol id="st-done" viewBox="0 0 14 14"><circle cx="7" cy="7" r="6.25" fill="currentColor"/><path d="M4.4 7.2l1.8 1.8 3.5-3.6" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="st-wait" viewBox="0 0 14 14"><circle cx="7" cy="7" r="6.25" fill="currentColor"/><rect x="6.25" y="3.6" width="1.5" height="4.2" rx=".75" fill="#fff"/><circle cx="7" cy="10" r=".9" fill="#fff"/></symbol>
  <symbol id="st-bad" viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.2 9.8l5.6-5.6" stroke="currentColor" stroke-width="1.5"/></symbol>
  <symbol id="i-inbox" viewBox="0 0 20 20"><path d="M3 11l2-6.5h10L17 11v4.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z M3 11h4l1 2h4l1-2h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></symbol>
  <symbol id="i-board" viewBox="0 0 20 20"><rect x="3" y="3.5" width="4" height="13" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="8.5" y="3.5" width="4" height="9" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="14" y="3.5" width="3" height="6" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
  <symbol id="i-list" viewBox="0 0 20 20"><path d="M6.5 5.5h10M6.5 10h10M6.5 14.5h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="3.6" cy="5.5" r="1" fill="currentColor"/><circle cx="3.6" cy="10" r="1" fill="currentColor"/><circle cx="3.6" cy="14.5" r="1" fill="currentColor"/></symbol>
  <symbol id="i-plus" viewBox="0 0 16 16"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></symbol>
  <symbol id="i-send" viewBox="0 0 16 16"><path d="M8 12.5V3.5M4 7.5l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-pr" viewBox="0 0 16 16"><circle cx="4.5" cy="3.8" r="1.6" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="4.5" cy="12.2" r="1.6" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="11.5" cy="12.2" r="1.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M4.5 5.4v5.2M11.5 10.6V6.5a2 2 0 0 0-2-2H7.5" fill="none" stroke="currentColor" stroke-width="1.3"/></symbol>
</svg>
<div id="app" data-view="board"><nav id="modes" class="rail" aria-label="主导航"></nav><aside id="side"></aside><main id="main"><div class="wrap"><p class="mute">加载中…</p></div></main></div>
<div id="sheet" role="dialog" aria-modal="true" aria-label="提交前确认"></div>
<div id="help" role="dialog" aria-label="快捷键"></div>
<script nonce="__NONCE__">
'use strict';
try { const th = new URLSearchParams((location.search || '')).get('theme');
  if ((th === 'light' || th === 'dark') && document.documentElement) document.documentElement.setAttribute('data-theme', th); } catch (e) {}
const TOKEN = document.querySelector('meta[name=qi-token]').content;
const TYPES = {free:'自由回答', suggested:'按建议', need_context:'需要更多上下文', defer:'暂不决定'};
const OTHER = ['free', 'need_context', 'defer'];
let cur = null, detail = null, dirty = false, msg = null;
// 异步回包都要对得上发起时的视图：gen 每次切题 +1；formGen 每次表单输入 +1；seq 只认最新的一次读取
let gen = 0, formGen = 0, listSeq = 0, loadSeq = 0, loadApplied = 0;
// 只影响展示的本地状态，切题时清空：当前卡片、是否在更正、提交摘要、展开过的折叠块
let curQ = null, editing = false, sheet = null, help = false, openState = {}, shownId = null, formShown = false;
let openCount = 0, posting = false;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
function html(s) { const d = document.createElement('div'); d.innerHTML = s; return d; } // 只用于服务端已 escape 的白名单 HTML
function setCls(e, cls, on) {
  if (!e) return;
  const list = (e.className || '').split(' ').filter(c => c && c !== cls);
  if (on) list.push(cls);
  e.className = list.join(' ');
}
function store(k, v) {  // localStorage 只记「看过哪一版」，不存答案；不可用时当作没记
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); return true; } catch (e) { return v === undefined ? null : false; }
}

async function api(path, body) {
  const opt = body === undefined ? {} : {method: 'POST', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-QI-Token': TOKEN}};
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  return {status: r.status, j};
}

function ago(t) {
  const ms = Date.parse(t || '');
  if (isNaN(ms)) return t || '';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return t.slice(0, 10);
}
function when(t) { return el('time', {title: t || ''}, ago(t)); }
function issueTag(issue) { const m = /#(\d+)$/.exec(issue || ''); return m ? '#' + m[1] : ''; }
function where(s) { return [s.ws, issueTag(s.issue)].filter(Boolean).join(' · '); }

// ---------- 左栏 ----------

function listItem(s, g) {
  const unseen = g === 'todo' && store('qi.seen.' + s.id) !== String(s.content_rev);
  const closedAs = s.closed_by === 'owner' ? 'Owner 关闭' : (s.status === 'resolved' ? 'lead 闭环' : (s.invalid || '已撤回'));
  const sub = g === 'todo' ? '' : g === 'closed' ? closedAs + (s.renotify ? ' · ' + s.delivery : '') : (s.invalid || s.delivery || s.status);
  return el('a', {class: 'item' + (s.id === cur ? ' cur' : ''), href: '#' + s.id},
    el('div', {class: 'l1'}, unseen ? [el('span', {class: 'dot', title: '有没看过的内容'}), el('span', {class: 'sr'}, '未看')] : null,
      (where(s) || s.id) + (s.n_questions ? ` · ${s.n_questions} 问` : ''), s.kind === 'approval' ? el('span', {class: 'tag'}, '审批') : null,
      s.has_draft ? el('span', {class: 'tag'}, '草稿') : null),
    el('div', {class: 'l2'}, el('span', {class: 'tt', title: s.title || ''}, s.title || s.id), when(g === 'todo' ? s.created : s.updated)),
    sub ? el('div', {class: 'l3'}, sub) : null);
}

// 待答按 ws 聚合；ws 的先后看组里最早的一题，组内从旧到新。空分组不渲染
function listGroup(title, items, cls, g) {
  if (!items.length) return null;
  const box = el('section', {class: 'grp ' + cls}, el('h2', {}, title, el('span', {class: 'n'}, `${items.length} 题`)));
  if (g !== 'todo') { for (const s of items) box.append(listItem(s, g)); return box; }
  const byWs = new Map();
  for (const s of [...items].sort((a, b) => (a.created || '').localeCompare(b.created || ''))) {
    if (!byWs.has(s.ws)) byWs.set(s.ws, []);
    byWs.get(s.ws).push(s);
  }
  for (const [ws, list] of byWs) box.append(el('h3', {}, ws || '（无 ws）', el('span', {class: 'n'}, `${list.length} 题`)), ...list.map(s => listItem(s, 'todo')));
  return box;
}

function renderInbox() {
  const main = document.getElementById('main');
  const wait = (boardData && boardData.columns) ? (boardData.columns.find(c => c.key === 'wait') || {cards: []}).cards.filter(c => !archived(c)) : [];
  const waitIds = new Set(wait.map(c => c.id));
  // 只按完整的「repo#号」合并：另一个 repo 同号的题不能被当成已合并而消失
  const waitIssues = new Set(wait.flatMap(c => (c.issue_keys || []).map(k => String(k).toLowerCase())));
  const todo = ((listData && listData.groups ? listData.groups.todo : []) || []).filter(q => !waitIssues.has(String(q.issue || '').toLowerCase()));
  const talk = ((listData && listData.needs_talk) || []).filter(t => !waitIds.has(t.id));
  const wrap = el('div', {class: 'wrap boardwrap'});
  wrap.append(el('header', {class: 'boardhead'}, el('h1', {}, '待处理'),
    el('span', {class: 'mute small'}, '只放要你动手的事，同一个需求合并成一条')));
  let n = 0;
  if (wait.length) {
    n += wait.length;
    wrap.append(el('div', {class: 'colh'}, '等你决定', el('span', {class: 'c'}, String(wait.length))),
      el('div', {class: 'cards inboxcards'}, wait.map(boardCard)));
  }
  if (talk.length) {
    n += talk.length;
    wrap.append(el('div', {class: 'colh'}, '需求讨论', el('span', {class: 'c'}, String(talk.length))),
      el('div', {class: 'cards'}, talk.map(t => el('div', {class: 'bcard'},
        el('div', {class: 'row1'}, el('span', {class: 'mono'}, t.id), el('span', {class: 'when'}, when(t.updated))),
        el('a', {class: 'bt', href: '#need:' + t.id}, t.text || t.id),
        el('div', {class: 'row3'}, t.ws ? el('span', {class: 'ws'}, el('span', {class: 'sq', style: 'background:' + wsColor(t.ws)}), t.ws) : null, '理解卡 v' + t.card_rev)))));
  }
  if (todo.length) {
    n += todo.length;
    wrap.append(el('div', {class: 'colh'}, '问题 / 审批', el('span', {class: 'c'}, String(todo.length))),
      el('div', {class: 'cards'}, todo.map(q => el('div', {class: 'bcard'},
        el('div', {class: 'row1'}, el('span', {class: 'mono'}, (q.issue || q.id)), el('span', {class: 'when'}, when(q.created))),
        el('a', {class: 'bt', href: '#' + q.id}, q.title || q.id),
        el('div', {class: 'row3'}, q.kind === 'approval' ? el('span', {class: 'flag f-wait'}, '审批') : el('span', {class: 'flag f-wait'}, `${q.n_questions} 个问题`),
          q.ws ? el('span', {class: 'ws'}, el('span', {class: 'sq', style: 'background:' + wsColor(q.ws)}), q.ws) : null)))));
  }
  if (!n) wrap.append(el('p', {class: 'mute'}, '暂时没有要你动手的事。'));
  main.replaceChildren(wrap);
}

async function refreshList() {
  const seq = ++listSeq;
  const {status, j} = await api('/api/list');
  if (seq !== listSeq || status !== 200) return;
  openCount = j.open_count;
  listData = j;
  document.title = (j.open_count ? `(${j.open_count}) ` : '') + '7788 工作台';
  renderModes(); renderSide();
  const back = document.getElementById('back');
  if (back) back.replaceChildren(`← 待处理（${openCount}）`);
  if (mode === 'inbox') renderInbox();
}

// ---------- 表单读写（不依赖卡片的样子，只认 radio 和 textarea） ----------

function markDirty() { dirty = true; formGen++; updateProgress(); }
function checkedValue(q) { const r = document.querySelector(`input[name="t_${q}"]:checked`); return r ? r.value : null; }
function textOf(q) { const t = document.getElementById('x_' + q); return t ? t.value : ''; }
function liveQs(d) { return d.questions.filter(q => !q.withdrawn); }
function isApproval(d) { return !!d && d.kind === 'approval'; }
const APPROVE_FIX = 'C. 需要先改';

// 「已填」只在这里判定：选了类型，或者没选类型但写了非空文字（算自由回答）。按钮和提交共用
function slotFor(q) {
  if (q.withdrawn) return null;
  const v = checkedValue(q.q), text = textOf(q.q);
  if (!v) return text.trim() && !isApproval(detail) ? {type: 'free', text} : null;  // 审批只认三个选项
  if (v.startsWith('opt:')) return {type: 'option', choice: q.options[+v.slice(4)], text};
  return {type: v, text};
}

function slotsFromForm(d) {
  const slots = {};
  for (const q of d.questions) { const s = slotFor(q); if (s) slots[q.q] = s; }
  return slots;
}

// 「其余按建议」：只填真正的空槽，不提交。返回填了几个
function fillSuggested(d) {
  if (d !== detail) return 0;
  let n = 0;
  for (const q of d.questions) {
    if (q.withdrawn || !q.suggestion || slotFor(q)) continue;
    const r = document.querySelector(`input[name="t_${q.q}"][value="suggested"]`);
    if (r) { r.checked = true; n++; }
  }
  if (n) markDirty();
  return n;
}

// 回填草稿 / 上一版回答；选项已不在当前列表里（有勘误）的，返回提示，原话照样填回
function fillForm(d, slots) {
  const stale = [];
  for (const q of d.questions) {
    const s = slots[q.q]; if (!s || q.withdrawn) continue;
    const t = document.getElementById('x_' + q.q); if (t) t.value = s.text || '';
    let v = s.type;
    if (v === 'option') {
      const i = q.options.indexOf(s.choice);
      if (i < 0) { stale.push(`${q.q} 原来选的「${s.choice}」已不在当前选项里`); continue; }
      v = 'opt:' + i;
    }
    const r = document.querySelector(`input[name="t_${q.q}"][value="${v}"]`); if (r) r.checked = true;
  }
  return stale;
}

// 建议开头的字母能对上某个选项（「A —— 理由」对「A. …」）时返回那个选项的下标，否则 -1。只影响展示
function suggestIndex(q) {
  const m = /^\s*([A-Za-z])(?=\s|[.．、:：)）—-]|$)/.exec(q.suggestion || '');
  if (!m) return -1;
  const re = new RegExp('^\\s*' + m[1] + '(?=\\s|[.．、:：)）]|$)', 'i');
  return q.options.findIndex(o => re.test(o));
}
function suggestReason(q) { return (q.suggestion || '').replace(/^\s*[A-Za-z]\s*(?:[.．、:：)）]|[—-]+)?\s*/, ''); }

// 一个槽「具体决定了什么」：提交摘要和只读回答共用。follows = 跟建议一致
function decision(q, s) {
  if (!s) return {main: '未答', tag: '', follows: false, miss: true};
  const si = suggestIndex(q), extra = s.type !== 'free' && (s.text || '').trim() ? s.text : '';
  if (s.type === 'option') {
    const same = si >= 0 && q.options[si] === s.choice;
    return {main: s.choice || '', tag: same ? '与建议一致' : (q.suggestion ? '不是建议' : ''), follows: same, extra};
  }
  if (s.type === 'suggested') {
    const sugg = s.choice || q.suggestion || '';
    return {main: si >= 0 ? q.options[si] : sugg, sub: si >= 0 ? '建议原文：' + sugg : '', tag: '按建议', follows: true, extra};
  }
  if (s.type === 'free') return {main: s.text || '', tag: '自由回答', follows: false};
  if (s.type === 'withdrawn') return {main: '已撤回', tag: '', follows: true};
  return {main: TYPES[s.type] || s.type, tag: '没做决定', follows: false, extra};
}

// ---------- 进度、当前卡片、快捷键 ----------

function updateProgress() {
  const d = detail;
  if (!d || !formShown) return;
  const live = liveQs(d), done = live.filter(q => slotFor(q)).length;
  for (const q of d.questions) {
    const a = document.getElementById('nav_' + q.q); if (!a) continue;
    const st = q.withdrawn ? 'gone' : slotFor(q) ? 'done' : 'todo';
    a.className = 'anc ' + st;
    a.setAttribute('title', {gone: '已撤回', done: '已填', todo: '未填'}[st]);
    a.replaceChildren(q.q + (st === 'done' ? ' ✓' : ''));
  }
  for (const id of ['prog', 'prog2']) { const p = document.getElementById(id); if (p) p.replaceChildren(`已答 ${done}/${live.length}`); }
  const b = document.getElementById('pbar'); if (b) b.setAttribute('style', `width:${live.length ? Math.round(done * 100 / live.length) : 100}%`);
  const ds = document.getElementById('dstat');
  if (ds) ds.replaceChildren(dirty ? '有未保存的改动' : (d.draft.note ? '已保存草稿' : ''));
}

function setCurQ(q, scroll) {
  if (curQ && curQ !== q) setCls(document.getElementById('card_' + curQ), 'focus', false);
  curQ = q;
  const c = document.getElementById('card_' + q);
  setCls(c, 'focus', true);
  if (scroll && c && c.scrollIntoView) c.scrollIntoView({block: 'start', behavior: 'smooth'});
}

function nextUnanswered(d) {
  const live = liveQs(d), i = live.findIndex(q => q.q === curQ);
  const order = live.slice(i + 1).concat(live.slice(0, i + 1));
  const q = order.find(x => !slotFor(x));
  if (q) setCurQ(q.q, true);
  else { msg = {cls: 'info', text: '都填了，可以检查并提交。'}; renderMsg(); }
}

// 展开「补充」「其他回答」：已有内容就一直展开，不会被收起
function reveal(q) {
  const v = checkedValue(q.q), t = textOf(q.q).trim();
  const open = (k, b) => {
    if (!b) return;
    openState[`${k}_${q.q}`] = true;
    const e = document.getElementById(`f_${k}_${q.q}`); if (e) e.open = true;
  };
  open('add', t || v === 'free');
  open('oth', OTHER.includes(v));
}

function typing(t) {
  if (!t || !t.tagName) return false;
  const tag = String(t.tagName).toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable) return true;
  return tag === 'INPUT' && !['radio', 'checkbox', 'button', 'submit'].includes(String(t.type || '').toLowerCase());
}

function onKey(e) {
  if (e.isComposing || e.keyCode === 229) return;  // 中文输入法正在合成
  if (sheet) { if (e.key === 'Escape') { e.preventDefault(); closeSheet(); } return; }
  if (help && (e.key === 'Escape' || e.key === '?')) { e.preventDefault(); showHelp(false); return; }
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    if (formShown && detail) { e.preventDefault(); startSubmit(detail); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
  if (e.key === '?') { e.preventDefault(); showHelp(true); return; }
  if (!formShown || !detail) return;
  const live = liveQs(detail); if (!live.length) return;
  let i = live.findIndex(q => q.q === curQ);
  if (e.key === 'j' || e.key === 'k') {
    i = i < 0 ? 0 : Math.max(0, Math.min(live.length - 1, i + (e.key === 'j' ? 1 : -1)));
    e.preventDefault(); setCurQ(live[i].q, true); return;
  }
  const q = live[i < 0 ? 0 : i];
  let v = null;
  if (/^[1-9]$/.test(e.key)) { const k = +e.key - 1; if (k < q.options.length) v = 'opt:' + k; }
  else if (e.key === 's' && q.suggestion) v = 'suggested';
  if (!v) return;
  const r = document.querySelector(`input[name="t_${q.q}"][value="${v}"]`);
  if (r) { e.preventDefault(); r.checked = true; setCurQ(q.q); markDirty(); }
}

function showHelp(on) {
  help = on;
  const box = document.getElementById('help');
  if (!on) { box.removeAttribute ? box.removeAttribute('data-open') : null; box.replaceChildren(); return; }
  const keys = [['1 – 9', '选当前卡片的第 n 个选项'], ['s', '当前卡片按建议'], ['j / k', '下一张 / 上一张卡片'],
    ['⌘↵ / Ctrl+↵', '多问题：打开提交摘要；单问题：直接提交'], ['?', '显示 / 关闭这份帮助'], ['Esc', '关闭摘要或帮助']];
  box.setAttribute('data-open', '');
  box.replaceChildren(el('div', {class: 'dlg'}, el('h2', {}, '快捷键'),
    el('div', {class: 'keys'}, keys.map(([k, t]) => [el('kbd', {}, k), el('span', {}, t)])),
    el('p', {class: 'mute small'}, '「当前卡片」是最后点过、或用 j / k 定位到的那张。焦点在输入框里或输入法正在合成时，字母和数字键不响应。'),
    el('div', {class: 'btns'}, el('button', {onclick: () => showHelp(false)}, '关闭'))));
}

// ---------- 提交：多问题先出摘要，单问题直接提交 ----------

function revKey(d) { return `${d.id}/${d.content_rev}/${d.answer_rev}`; }

function startSubmit(d) {
  if (d !== detail || posting) return;
  const slots = slotsFromForm(d);
  if (isApproval(d) && !slots.Q1) { msg = {cls: 'bad', text: '审批要选一项：批准 / 不批准 / 需要先改'}; renderMsg(); return; }
  if (isApproval(d) && slots.Q1.choice === APPROVE_FIX && !(slots.Q1.text || '').trim()) {
    msg = {cls: 'bad', text: `选「${APPROVE_FIX}」要写清楚改什么（写在选项下面的框里）`}; renderMsg(); return;
  }
  if (liveQs(d).length < 2) return submitNow(d, slots);
  sheet = {key: revKey(d), fg: formGen, slots};
  renderSheet();
}

function goLabel(d) { return posting ? '提交中…' : liveQs(d).length > 1 ? '检查并提交' : (d.answer_rev ? '提交更正' : '提交并发回 lead'); }
function syncGo(d) {
  const go = document.getElementById('go'); if (!go) return;
  go.disabled = posting;
  go.replaceChildren(goLabel(d), posting ? '' : el('kbd', {}, '⌘↵'));
}

// 同一次提交只发一个请求：提交中按钮禁用并显示「提交中…」，失败后恢复，输入原样保留
async function submitNow(d, slots) {
  if (posting) return false;
  posting = true; syncGo(d);
  try { return await post(d, 'submit', {content_rev: d.content_rev, answer_rev: d.answer_rev, slots}); }
  finally { posting = false; if (detail) syncGo(detail); }
}

function closeSheet() { sheet = null; renderSheet(); }

function confirmSheet() {
  const d = detail, s = sheet;
  if (!s || !d || s.key !== revKey(d)) {
    closeSheet(); msg = {cls: 'warn', text: '问题或回答在别处更新了，摘要已关闭，请重新核对。'}; renderMsg(); return;
  }
  if (s.fg !== formGen) { sheet = {key: s.key, fg: formGen, slots: slotsFromForm(d)}; renderSheet(); return; }
  closeSheet();
  submitNow(d, s.slots);
}

function renderSheet() {
  const box = document.getElementById('sheet'), d = detail;
  if (!sheet || !d) { if (box.removeAttribute) box.removeAttribute('data-open'); box.replaceChildren(); return; }
  const live = liveQs(d), rows = [];
  let follows = 0, missing = [];
  for (const q of live) {
    const x = decision(q, sheet.slots[q.q]);
    if (x.miss) missing.push(q.q); else if (x.follows) follows++;
    rows.push(el('div', {class: 'srow' + (x.miss ? ' miss' : x.follows ? '' : ' warn'), title: '点这一行回到这题修改',
      onclick: () => { closeSheet(); setCurQ(q.q, true); }},
      el('strong', {}, q.q),
      el('div', {}, el('div', {class: 'qq'}, q.text),
        el('div', {}, el('span', {class: 'dec'}, x.main), x.tag ? el('span', {class: 'tg'}, x.tag) : null),
        x.sub ? el('div', {class: 'small mute'}, x.sub) : null,
        x.extra ? el('div', {class: 'small'}, '补充：' + x.extra) : null)));
  }
  const n = live.length - missing.length;
  const ok = el('button', {class: 'pri', id: 'sheet_ok', onclick: confirmSheet, disabled: missing.length > 0},
    d.answer_rev ? `确认提交（生成 Rev ${d.answer_rev + 1}）` : '确认提交');
  box.setAttribute('data-open', '');
  box.replaceChildren(el('div', {class: 'dlg'},
    el('h2', {}, `提交前确认 · ${live.length} 个问题`),
    el('p', {class: 'mute small'}, missing.length ? `还有 ${missing.join('、')} 没答，补完才能提交。` :
      `${follows} 个按建议，${n - follows} 个不是（黄色）。确认后才会发回 lead。`),
    rows,
    el('div', {class: 'btns'}, el('button', {onclick: closeSheet}, '返回修改'), ok)));
  if (ok.focus && !missing.length) ok.focus();
}

// ---------- 详情 ----------

// d 是发起操作时屏幕上的那题；切题后不再发，回包也不再改当前视图
async function post(d, action, body) {
  const id = d.id, g = gen, fg = formGen;
  if (d !== detail || id !== cur) {
    msg = {cls: 'bad', text: '页面已经切到别的问题，这次操作没有发出；请确认当前显示的问题后再操作。'}; renderMsg();
    return false;
  }
  const {status, j} = await api(`/api/q/${encodeURIComponent(id)}/${action}`, body);
  refreshList();
  if (g !== gen || id !== cur) return status === 200;
  if (status === 200) {
    const text = j.note || (action === 'submit' && j.unchanged ? '和当前回答相同，没有重复发送' :
      {save: '已保存草稿（不投递）', submit: `已提交 Rev ${j.answer_rev}，正在发回 lead`}[action] || '完成');
    if (fg === formGen) { dirty = false; if (action === 'submit') editing = false; msg = {cls: 'ok', text}; await load(true); }
    else { msg = {cls: 'warn', text: text + '。你在请求期间又改了表单，改动还在，先别刷新。'}; renderMsg(); }
    return true;
  }
  msg = {cls: 'bad', text: (status === 409 ? '版本冲突：' : '') + (j.error || ('错误 ' + status))}; renderMsg();
  return false;
}

function renderMsg() {
  const box = document.getElementById('msg'); if (!box) return;
  box.replaceChildren(msg ? el('div', {class: 'msgbar ' + msg.cls}, msg.text) : '');
}

// 折叠块：展开状态记在 openState，轮询重建后保持
function fold(key, cls, title, def, ...kids) {
  const open = key in openState ? openState[key] : def;
  return el('details', {class: cls, id: 'f_' + key, open, ontoggle: e => { openState[key] = !!e.target.open; }},
    el('summary', {}, title), ...kids);
}

function stateLine(d) {
  const live = liveQs(d).length, gone = d.questions.length - live;
  let head;
  if (d.invalid) head = el('span', {class: 'chip bad'}, '已失效');
  else if (d.status === 'open') head = d.blocking === 'yes' ? el('span', {class: 'chip hot'}, 'lead 正在等你') : '可稍后回答';
  else if (d.status === 'answered') head = d.received ? el('span', {class: 'chip ok'}, `lead 已接收 Rev ${d.answer_rev}`) : `已提交 Rev ${d.answer_rev}，等 lead 接收`;
  else if (d.status === 'resolved') head = el('span', {class: 'chip ok'}, d.closed_by === 'owner' ? '已由你关闭' : 'lead 已闭环');
  else head = '已撤回';
  if (isApproval(d)) return [el('span', {class: 'chip hot'}, '审批'), ' ', head, ' · ', when(d.created)];
  return [head, ` · ${live} 个问题${gone ? `（另有 ${gone} 个已撤回）` : ''} · `, when(d.created)];
}

function closeMenu(d) {
  if (!d.closable) return null;
  const reason = el('textarea', {id: 'close_reason', placeholder: '关闭原因（必填）', oninput: markDirty});
  return fold('more', 'more', '⋯', false, el('div', {class: 'pop'},
    el('strong', {}, '由我关闭'),
    el('p', {class: 'mute small'}, d.status === 'open'
      ? '还没回答的题：关闭等于撤回，之后不会投递。'
      : '已回答的题：记为「Owner 关闭」（不是 lead 闭环），待投递的取消；如果回答已投递或已领取，会另发一条关闭通知给原 lead。用于 lead 会话已经没了、没人能闭环的情况。'),
    reason,
    el('div', {class: 'btns'}, el('button', {onclick: () => {
      if (!reason.value.trim()) { msg = {cls: 'bad', text: '关闭必须写原因'}; renderMsg(); return; }
      post(d, 'close', {content_rev: d.content_rev, answer_rev: d.answer_rev, reason: reason.value});
    }}, '确认关闭'))));
}

function deliveryBar(d) {
  if (d.status === 'answered') {  // 已提交：醒目横幅，随投递状态更新
    const cls = d.received ? 'ok' : (d.renotify ? 'bad' : 'info');
    const st = d.received ? 'lead 已接收' : d.renotify ? `投递有问题：${d.delivery}` : '正在发回 lead';
    return el('div', {class: 'banner ' + cls, id: 'dstate'},
      el('div', {class: 'dbar'}, el('strong', {}, `已提交 Rev ${d.answer_rev}`), ` · ${st}`,
        d.renotify ? el('button', {onclick: () => post(d, 'renotify', {})}, '重新通知') : null),
      !d.received && d.delivery ? el('div', {class: 'small'}, `投递：${d.delivery}${d.delivery_detail ? '（' + d.delivery_detail + '）' : ''}`) : null,
      d.renotify ? el('div', {class: 'small'}, '重新通知会重新核对 lead 身份后再发；这条可能已经在 lead 的队列里。') : null);
  }
  const notice = (d.status === 'withdrawn' || d.closed_by === 'owner') && d.delivery && !['已撤回', 'Owner 已关闭'].includes(d.delivery);
  if (!(d.status === 'answered' || d.status === 'resolved' || notice)) return null;
  const st = d.received ? 'lead 已接收' : (d.delivery || '');
  return el('div', {class: 'msgbar ' + (d.received ? 'ok' : (d.renotify ? 'bad' : 'warn')), id: 'dstate'},
    el('div', {class: 'dbar'}, `投递：${st}${d.delivery_detail && !d.received ? '（' + d.delivery_detail + '）' : ''}`,
      d.renotify ? el('button', {onclick: () => post(d, 'renotify', {})}, '重新通知') : null),
    d.renotify ? el('div', {class: 'small'}, '重新通知会重新核对 lead 身份后再发；这条可能已经在 lead 的队列里。') : null);
}

function answerCard(q, s, label) {  // 只读：一题的已提交决定
  const x = decision(q, s);
  return el('div', {class: 'ans'}, el('div', {class: 'k'}, label),
    el('div', {}, el('strong', {}, x.main), x.tag ? el('span', {class: 'mute small'}, ' · ' + x.tag) : null),
    x.sub ? el('div', {class: 'small mute'}, x.sub) : null,
    x.extra ? el('div', {class: 'small'}, '补充：' + x.extra) : null);
}

function card(d, q, latest) {
  const c = el('section', {class: 'q' + (q.q === curQ ? ' focus' : '') + (q.withdrawn ? ' gone' : ''), id: 'card_' + q.q,
    onclick: () => { if (curQ !== q.q) setCurQ(q.q); }},
    el('h3', {}, el('span', {class: 'qn'}, q.q), q.text));
  if (q.withdrawn) { c.append(el('p', {class: 'mute small'}, 'lead 已撤回这个子问题，不用答（算已填）。')); return c; }
  const si = suggestIndex(q), note = (d.option_notes || {})[q.q];
  if (!formShown) {  // 只读：选项、建议、已提交的决定
    c.append(el('ul', {class: 'olist'}, q.options.map((o, k) => el('li', {}, o, k === si ? el('span', {class: 'badge'}, '建议') : null))));
    if (note) c.append(el('div', {class: 'note'}, html(note)));
    if (q.suggestion) c.append(el('p', {class: 'why'}, 'lead 建议：' + q.suggestion));
    if (latest && latest.slots[q.q]) c.append(answerCard(q, latest.slots[q.q], `你的回答（Rev ${latest.rev}）`));
    return c;
  }
  const name = 't_' + q.q;
  const radio = (v, cls, ...kids) => el('label', {class: 'opt' + cls},
    el('input', {type: 'radio', name, value: v, onchange: () => { markDirty(); reveal(q); }}), ...kids);
  const opts = el('div', {class: 'opts'});
  q.options.forEach((o, k) => opts.append(radio('opt:' + k, k === si ? ' sug' : '', el('kbd', {}, k < 9 ? String(k + 1) : '·'),
    el('span', {class: 'ot'}, o), k === si ? el('span', {class: 'badge'}, '建议') : null)));
  c.append(opts);
  if (q.suggestion && si >= 0) {
    c.append(el('p', {class: 'why'}, '理由：' + (suggestReason(q) || q.suggestion)),
      el('div', {class: 'sugrow'}, radio('suggested', '', el('kbd', {}, 's'),
        el('span', {class: 'ot'}, '按建议', el('span', {class: 'mute small'}, `（记为「按建议」，存下建议原文：${q.suggestion}）`)))));
  } else if (q.suggestion) {
    c.append(el('div', {class: 'sugrow'}, radio('suggested', ' sug', el('kbd', {}, 's'),
      el('span', {class: 'ot'}, 'lead 建议：' + q.suggestion), el('span', {class: 'badge'}, '按此建议'))));
  }
  if (note) c.append(el('div', {class: 'note'}, html(note)));
  if (isApproval(d)) {  // 审批：三个固定选项，不预选、没有「按建议」和「其他回答」；补充框常开
    c.append(el('textarea', {id: 'x_' + q.q, rows: 2, placeholder: `补充（选「${APPROVE_FIX}」时必填：要改什么）`, oninput: markDirty}));
    return c;
  }
  c.append(fold('add_' + q.q, 'fold', '＋补充', false,
      el('textarea', {id: 'x_' + q.q, rows: 2, placeholder: '补充 / 原话（不选上面的类型直接写，就算自由回答）', oninput: markDirty})),
    fold('oth_' + q.q, 'fold', '其他回答 ▾', false,
      el('div', {class: 'others'}, OTHER.map(t => radio(t, '', el('span', {class: 'ot'}, TYPES[t]))))));
  return c;
}

function render() {
  const d = detail, main = document.getElementById('main');
  const same = shownId === d.id, top = main.scrollTop, wy = window.scrollY || 0;
  shownId = d.id;
  if (sheet && sheet.key !== revKey(d)) { sheet = null; msg = {cls: 'warn', text: '问题或回答在别处更新了，摘要已关闭，请重新核对。'}; }
  formShown = !!(d.editable && (!d.answer_rev || editing || Object.keys(d.draft.slots).length));
  const latest = d.revs.find(r => r.rev === d.answer_rev), live = liveQs(d);
  const wrap = el('div', {class: 'wrap'});
  wrap.append(el('header', {},
    el('a', {class: 'back', id: 'back', href: '#'}, `← 收件箱（待答 ${openCount}）`),
    el('div', {class: 'where'}, el('span', {}, where(d) || d.id), closeMenu(d)),
    el('h1', {}, d.title || d.id),
    el('div', {class: 'state'}, stateLine(d))));
  if (!formShown) wrap.append(el('div', {id: 'msg'}));
  if (d.invalid) wrap.append(el('div', {class: 'msgbar bad'}, `已失效：${d.invalid}`));
  if (d.not_executed) wrap.append(el('div', {class: 'msgbar bad'}, d.not_executed));
  for (const w of d.warnings) wrap.append(el('div', {class: 'msgbar warn'}, '提示：' + w));
  wrap.append(deliveryBar(d) || '');
  if (d.resolution) wrap.append(el('div', {class: 'msgbar ok'}, (d.closed_by === 'owner' ? '由你在页面上关闭（不是 lead 闭环）：' : 'lead 已闭环：') + d.resolution));
  const prose = (title, h, cls) => h ? el('section', {class: 'sec prose ' + (cls || '')}, el('h2', {}, title), html(h)) : null;
  for (const [k, h] of d.approval_html || []) wrap.append(prose(k, h, 'appr'));  // 审批：批什么、对谁、后果
  wrap.append(prose('背景', d.background_html) || '', prose('证据', d.evidence_html) || '');
  if (d.errata_html) wrap.append(fold('errata', 'sec prose errata', '勘误（lead 改过问题，作答前请看）', false, html(d.errata_html)));
  if (d.after_html) wrap.append(el('div', {class: 'after prose'}, el('span', {class: 'mute small'}, '答完之后：'), html(d.after_html)));
  if (d.extra_html) wrap.append(fold('extra', 'sec prose', '补充材料', true, html(d.extra_html)));
  let staleBox = null;
  if (formShown) {
    staleBox = el('div', {});
    wrap.append(el('div', {class: 'nav'}, el('span', {id: 'prog'}),
      d.questions.map(q => el('button', {class: 'anc', id: 'nav_' + q.q, onclick: () => setCurQ(q.q, true)}, q.q)),
      el('button', {class: 'next', onclick: () => nextUnanswered(d)}, '下一个未答')), staleBox);
    if (d.answer_rev) wrap.append(el('div', {class: 'msgbar info'}, `正在更正：当前 Rev ${d.answer_rev}，提交后生成 Rev ${d.answer_rev + 1}，旧版作废。`));
  }
  for (const q of d.questions) wrap.append(card(d, q, latest));
  if (!formShown && d.editable && latest) {
    wrap.append(el('div', {class: 'acts'}, el('button', {onclick: () => { editing = true; msg = null; render(); }}, '更正回答')));
  }
  const older = d.revs.filter(r => r.rev !== d.answer_rev || formShown).reverse();
  wrap.append(el('div', {class: 'foot'},
    older.length ? fold('revs', '', `已提交的回答（${older.length} 版）`, false, older.map(r => el('div', {class: 'rev'},
      el('strong', {}, `Rev ${r.rev}${r.corrected_by ? `（已被 Rev ${r.corrected_by} 更正）` : ''}`), ' ', el('span', {title: r.at}, ago(r.at)),
      d.questions.filter(q => r.slots[q.q]).map(q => { const x = decision(q, r.slots[q.q]); return el('div', {}, `${q.q}：${x.main}${x.tag ? '（' + x.tag + '）' : ''}`, x.extra ? el('div', {class: 'small'}, '补充：' + x.extra) : null); })))) : null,
    fold('tl', '', '时间线', false, d.timeline.map(t => el('div', {}, t))),
    fold('info', '', '详情', false, el('div', {class: 'kv'},
      [['id', d.id], ['状态', d.status], ['issue', d.issue], ['group', d.group], ['content_rev', d.content_rev],
        ['answer_rev', d.answer_rev], ['创建', d.created], ['更新', d.updated]]
        .filter(([, v]) => v !== '' && v != null).map(([k, v]) => [el('span', {}, k), el('span', {}, String(v))])))));
  if (formShown) {
    const empty = () => liveQs(d).filter(q => q.suggestion && !slotFor(q)).map(q => q.q);
    wrap.append(el('div', {class: 'bar-b'}, el('div', {id: 'msg'}), el('div', {class: 'row'},
      el('div', {class: 'prog'}, el('strong', {id: 'prog2'}), el('span', {class: 'pb'}, el('span', {id: 'pbar'})),
        el('span', {id: 'dstat', class: 'mute small'}), el('span', {class: 'mute small hint'}, '? 快捷键')),
      el('div', {class: 'acts'},
        d.answer_rev && !Object.keys(d.draft.slots).length ? el('button', {onclick: () => {
          if (dirty && !confirm(LEAVE_MSG)) return;
          editing = false; dirty = false; msg = null; render();
        }}, '放弃更正') : null,
        el('button', {onclick: () => post(d, 'save', {content_rev: d.content_rev, slots: slotsFromForm(d)})}, '保存草稿'),
        !isApproval(d) && live.some(q => q.suggestion) ? el('button', {onclick: () => {
          const todo = empty(), n = fillSuggested(d);
          msg = {cls: 'info', text: n ? `已按建议填入 ${todo.join('、')}，还没提交，可以再改。` : '没有能按建议填的空题。'}; renderMsg();
        }}, '其余按建议') : null,
        el('button', {class: 'pri', id: 'go', disabled: posting, onclick: () => startSubmit(d)}, goLabel(d), posting ? null : el('kbd', {}, '⌘↵')))),
      el('p', {class: 'mute small'}, isApproval(d) ? '只有提交的「A. 批准」才算授权；lead 执行前会重新核对 HEAD 和环境。'
        : '「其余按建议」只填还空着的题，你可以再改；只有提交才算回答。')));
  }
  main.replaceChildren(wrap);
  if (formShown) {
    const stale = fillForm(d, Object.keys(d.draft.slots).length ? d.draft.slots : (latest ? latest.slots : {}));
    for (const s of stale) staleBox.append(el('div', {class: 'msgbar warn'}, s + '（问题有勘误），请重新选择。'));
    for (const q of live) reveal(q);
  }
  renderMsg(); updateProgress(); renderSheet();
  if (same) { main.scrollTop = top; if (window.scrollTo && window.scrollY !== wy) window.scrollTo(0, wy); }
  else { main.scrollTop = 0; if (window.scrollTo) window.scrollTo(0, 0); }
  if (store('qi.seen.' + d.id) !== String(d.content_rev) && store('qi.seen.' + d.id, String(d.content_rev))) refreshList();
}

// 会重建表单的只有这里（render 只被 load 和本地按钮调用）。force 也不能绕过输入保护：
// 请求发出后 Owner 又输入过（formGen 变了），就保留表单，只提示服务端状态变了
async function load(force) {
  const id = cur, g = gen, seq = ++loadSeq, fg = formGen;
  if (!id) return;
  const {status, j} = await api('/api/q/' + encodeURIComponent(id));
  if (g !== gen || id !== cur || seq < loadApplied) return;  // 发起后切了题，或已有更新的读取生效
  loadApplied = seq;
  if (status !== 200) { document.getElementById('main').replaceChildren(el('div', {class: 'wrap'}, el('div', {class: 'msgbar bad'}, j.error || '加载失败'))); detail = null; return; }
  if (j.id !== id) return;
  const changed = !detail || detail.id !== j.id || detail.content_rev !== j.content_rev || detail.answer_rev !== j.answer_rev || detail.status !== j.status;
  if (!force && !dirty && JSON.stringify(detail) === JSON.stringify(j)) return;
  if (fg !== formGen && detail && detail.editable) {
    if (changed) msg = {cls: 'warn', text: (force && msg ? msg.text + '。' : '') + '服务端状态已变化：你刚才的新输入还在表单里，没有被覆盖；需要时先保存草稿再刷新页面。'};
    renderMsg();
    return;
  }
  if (force || !dirty) { const keep = force ? msg : null; detail = j; render(); msg = keep; renderMsg(); return; }
  if (changed) { msg = {cls: 'warn', text: '问题或回答已在别处更新：你填的内容还在表单里，提交会提示冲突并自动保留为草稿，然后刷新页面。'}; renderMsg(); }
}

// ---------- 任务视图（只读：除了刷新、链接、跳到题，没有任何输入控件，也不发写请求） ----------

const TASKS = 'tasks', NEEDS = 'needs';
let mode = 'inbox', tasksData = null, tasksErr = '', tasksSeq = 0, tasksLoading = false, tasksAt = 0, taskFilter = '';
let listData = null;
let needId = '', boardNewOpen = false, boardNewText = '', boardNewBusy = false, boardNewReq = '', needReqText2 = '';
let boardNewErr = '';
const STAGE_CLS = {answer: 'hot', merge: 'hot', accept: 'hot', stuck: 'bad', doing: 'info', todo: 'mute', hold: 'mute', done: 'ok', dropped: 'mute'};

function svgEl(name, attrs, ...kids) {
  const e = document.createElementNS ? document.createElementNS('http://www.w3.org/2000/svg', name) : document.createElement(name);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  for (const k of kids.flat()) if (k != null) e.append(k);
  return e;
}
function icon(id, size) {
  const u = svgEl('use', {href: '#' + id});
  if (u.setAttributeNS) u.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', '#' + id);
  return svgEl('svg', {width: String(size || 18), height: String(size || 18), 'aria-hidden': 'true'}, u);
}
const WS_PALETTE = ['#5b5bd6', '#12a594', '#e5870a', '#8e4ec6', '#e5484d', '#30a46c'];
function wsColor(ws) {
  if (!ws) return '#a1a1aa';
  const list = (boardData && boardData.ws) || [];
  return WS_PALETTE[Math.max(0, list.indexOf(ws)) % WS_PALETTE.length];
}
function avatar(ws, big) {
  const initial = (ws || '?').slice(0, 1).toUpperCase();
  return el('span', {class: 'av' + (big ? ' lg' : ''), style: 'background:' + wsColor(ws)}, initial);
}
function meAvatar(big) { return el('span', {class: 'av a-me' + (big ? ' lg' : ''), title: 'Owner（你）'}, '我'); }
function allCards() {
  return (boardData && boardData.columns) ? boardData.columns.flatMap(c => c.cards) : [];
}
function waitCards() {
  return (boardData && boardData.columns) ? ((boardData.columns.find(c => c.key === 'wait') || {cards: []}).cards) : [];
}
function sideCounts() {
  const all = allCards(), out = {};
  for (const c of all) if (c.ws) out[c.ws] = (out[c.ws] || 0) + 1;
  return out;
}
function renderModes() {
  const box = document.getElementById('modes'); if (!box) return;
  const wait = waitCards().length + ((listData && listData.needs_talk) || []).length;
  box.replaceChildren(
    el('div', {class: 'ws-logo', title: 'Owner 的工作台'}, 'M'),
    el('a', {href: '#', class: mode === 'inbox' ? 'on' : '', 'aria-current': mode === 'inbox' ? 'page' : null},
      icon('i-inbox', 20), el('span', {}, '待处理'), wait ? el('span', {class: 'badge'}, String(wait)) : null),
    el('a', {href: '#' + BOARD, class: mode === 'board' || mode === 'need' ? 'on' : '', 'aria-current': mode === 'board' || mode === 'need' ? 'page' : null},
      icon('i-board', 20), el('span', {}, '需求')));
}

function renderSide() {
  const side = document.getElementById('side'); if (!side) return;
  const all = allCards(), counts = sideCounts();
  const wsList = (boardData && boardData.ws) || [];
  side.replaceChildren(
    el('div', {class: 'side-head'}, el('h2', {}, '需求')),
    el('a', {class: 'side-link' + (!boardWs && mode !== 'inbox' ? ' on' : ''), href: '#' + BOARD},
      icon('i-board', 16), el('span', {}, '全部需求'), el('span', {class: 'n'}, String(all.length))),
    el('a', {class: 'side-link' + (mode === 'inbox' ? ' on' : ''), href: '#'},
      icon('i-inbox', 16), el('span', {}, '待处理'), el('span', {class: 'n'}, String(waitCards().length))),
    el('div', {class: 'side-lbl'}, '工作区'),
    ...wsList.map(ws => el('a', {class: 'side-link' + (boardWs === ws ? ' on' : ''), href: '#' + BOARD + ':' + ws},
      el('span', {class: 'sq', style: 'background:' + wsColor(ws)}), el('span', {}, ws),
      el('span', {class: 'n'}, String(counts[ws] || 0)))),
    el('div', {class: 'side-foot'}, el('a', {href: '#' + TASKS}, '全部 Issue（只读视图）↗')));
}

// 只放行 https://github.com/ 的链接；别的一律当文字
function ghLink(url, text) {
  return typeof url === 'string' && url.startsWith('https://github.com/')
    ? el('a', {href: url, target: '_blank', rel: 'noopener noreferrer'}, text) : el('span', {}, text);
}

// ---------- 需求看板 / 详情（v2） ----------

const BOARD = 'board', NEEDP = 'need';
let boardData = null, boardErr = '', boardSeq = 0, boardAt = 0, boardWs = '', boardMsg = null, routerHealth = null;
let needData = null, needErr = '', needSeq = 0, needMsg2 = null, needAt = 0;

function archived(c) { return store('qi.arch.' + c.id) === String(c.updated || ''); }

function receiptLine(r, withGithub) {
  const parts = [];
  if (withGithub) parts.push('✓ 已发到 GitHub');
  if (r && r.notified) parts.push('✓ 已通知 lead');
  else if (r && r.note) parts.push(r.note);
  if (r && r.read) parts.push('lead 已读');
  if (!parts.length) parts.push('排队通知');
  return parts.join(' · ');
}

function flagEl(f) {
  return el('span', {class: 'flag ' + (f.kind === 'wait' ? 'f-wait' : f.kind === 'bad' ? 'f-bad' : 'f-mute')}, f.text);
}

function sourceTag(src) {
  return el('span', {class: 'src' + (src && src.indexOf('7788') >= 0 ? ' src-7788' : '')}, src || '');
}

function timelineView(n) {
  const box = el('div', {class: 'tl'});
  for (const x of n.timeline || []) {
    if (x.kind === 'system') {
      box.append(el('div', {class: 'sys'}, el('span', {title: x.at || ''}, when(x.at)), ' ' + (x.text || '')));
      continue;
    }
    if (x.kind === 'question') {
      box.append(inlineCard(x));
      continue;
    }
    if (x.kind === 'comment') {
      const who = x.author || '?', mine = x.self === 'owner';
      box.append(el('div', {class: 'msg'}, mine ? meAvatar(true) : el('span', {class: 'av a-team'}, who.slice(0, 1).toUpperCase()),
        el('div', {class: 'bubble ' + (x.self ? 'soft' : '')},
          el('div', {class: 'mh'}, el('b', {}, mine ? '你' : who), sourceTag(x.source),
            el('span', {title: x.at || ''}, when(x.at)), x.edited ? el('span', {class: 'src'}, '编辑过') : null),
          html(x.body_html || ''),
          x.self && x.source && x.source.indexOf('经 7788') >= 0 ? el('div', {class: 'receipt'}, receiptLine(x.receipt, true)) : null)));
      continue;
    }
    if (x.kind2 === 'card') {
      const body = el('div', {}, (x.card || []).map(([k, v]) => el('div', {class: 'csec'}, el('strong', {}, k), el('div', {class: 'plain'}, v || '（空）'))));
      const head = el('div', {class: 'mh'}, el('b', {}, '理解卡 v' + x.rev), sourceTag('7788 讨论'),
        el('span', {title: x.at || ''}, when(x.at)),
        x.latest && n.confirmed_rev >= x.rev ? el('span', {class: 'src'}, '你已确认') : null);
      box.append(el('div', {class: 'msg'}, avatar(n.ws, true),
        x.latest ? el('div', {class: 'bubble soft'}, head, body)
          : fold('ncard_' + (x.rev || ''), 'fold', `理解卡 v${x.rev}（旧版）`, false, head, body)));
      continue;
    }
    if (x.kind2 === 'issue') {
      box.append(el('div', {class: 'sys'}, el('span', {title: x.at || ''}, when(x.at)), ' 结论已发到 Issue'));
      continue;
    }
    const owner = x.by === 'owner';
    box.append(el('div', {class: 'msg'}, owner ? meAvatar(true) : avatar(n.ws, true),
      el('div', {class: 'bubble soft'},
        el('div', {class: 'mh'}, el('b', {}, owner ? '你' : 'lead-' + (n.ws || '')),
          sourceTag('7788 讨论'), el('span', {title: x.at || ''}, when(x.at))),
        el('div', {class: 'mtext'}, x.text || ''),
        owner ? el('div', {class: 'receipt'}, receiptLine(x.receipt, false)) : null)));
  }
  if (!box.children.length) box.append(el('p', {class: 'mute'}, '还没有对话。'));
  return box;
}

// ---------- C1：详情页就地答题（复用 /api/q/<id>/submit 的全部服务端保护） ----------

let inlineState = {}, inlineDraft = {}, inlineEdit = {}, inlineSheet = null;

function inlineGet(id) {
  if (!inlineState[id]) inlineState[id] = {busy: false, gen: 0, msg: null};
  return inlineState[id];
}
// 草稿绑定它开始时的版本（content_rev/answer_rev）；选项按文字记，不靠序号
function inlineRev(d) { return `${d.content_rev}/${d.answer_rev}`; }
function inlineDraftOf(d) {
  if (!inlineDraft[d.id]) inlineDraft[d.id] = {rev: inlineRev(d), checked: {}, choices: {}, texts: {}, gen: 0};
  return inlineDraft[d.id];
}
function inlineHasInput(st) {
  return !!st && (Object.values(st.checked).some(Boolean) || Object.values(st.texts).some(t => (t || '').trim()));
}
// 题目被勘误 / 替换 / 回答在别处更新：旧草稿不套到新版本上。丢弃，并把旧输入原样摆出来让 Owner 对照重填
function inlineCheckStale(d) {
  const st = inlineDraft[d.id], s = inlineGet(d.id);
  if (!st || st.rev === inlineRev(d) || s.busy) return;
  if (inlineHasInput(st)) {
    const old = Object.keys(Object.assign({}, st.checked, st.texts)).sort().map(q =>
      `${q}：${st.choices[q] || ({suggested: '按建议', need_context: '需要更多上下文', defer: '暂不决定'}[st.checked[q]] || '')}${(st.texts[q] || '').trim() ? ' · ' + st.texts[q].trim() : ''}`);
    s.msg = {cls: 'warn', text: `这题已更新（版本 ${st.rev} → ${inlineRev(d)}），你在旧版本上的输入没有套用到新版本，请看完更新后重新作答。旧输入：` + old.join('；')};
  }
  delete inlineDraft[d.id];
  if (inlineSheet && inlineSheet.id === d.id) inlineSheet = null;
}
function inlineSlot(d, q) {
  const st = inlineDraft[d.id] || {checked: {}, choices: {}, texts: {}};
  const v = st.checked[q.q] || '', text = st.texts[q.q] || '';
  if (!v) return text.trim() && d.kind !== 'approval' ? {type: 'free', text} : null;
  if (v.indexOf('opt:') === 0) {
    const c = st.choices[q.q];
    return c && (q.options || []).includes(c) ? {type: 'option', choice: c, text} : null;
  }
  return {type: v, text};
}
function inlineSlots(d) {
  const out = {};
  for (const q of d.questions || []) { const s = inlineSlot(d, q); if (s && !q.withdrawn) out[q.q] = s; }
  return out;
}
function inlineDecision(d, q, s) {
  if (!s) return '未答';
  if (s.type === 'option') return s.choice || '';
  if (s.type === 'suggested') return '按建议：' + (s.choice || q.suggestion || '');
  if (s.type === 'free') return s.text || '';
  if (s.type === 'withdrawn') return '已撤回';
  return {need_context: '需要更多上下文', defer: '暂不决定'}[s.type] || s.type;
}
// 作答前必须看到的上下文：审批的动作 / 对象 / 后果、勘误（默认展开）；背景、证据可折叠
function inlineContext(d) {
  const out = [];
  if (d.invalid) out.push(el('div', {class: 'msgbar bad'}, '已失效：' + d.invalid));
  for (const [k, h] of d.approval_html || []) out.push(el('div', {class: 'appr prose'}, el('strong', {}, k), html(h)));
  if (d.errata_html) out.push(el('div', {class: 'errata prose'}, el('strong', {}, '勘误（lead 改过问题，作答前请看）'), html(d.errata_html)));
  if (d.background_html) out.push(fold('ibg_' + d.id, 'sec prose', '背景', d.kind === 'approval', html(d.background_html)));
  if (d.evidence_html) out.push(fold('iev_' + d.id, 'sec prose', '证据', false, html(d.evidence_html)));
  return out;
}
function inlineCard(x) {
  const d = x.detail;
  if (!d) {
    return el('div', {class: 'bubble ask tlq'},
      el('div', {class: 'mh'}, icon('st-wait', 14), el('b', {}, x.title || x.id), el('span', {class: 'src'}, x.invalid || '读取失败')));
  }
  inlineCheckStale(d);
  const st = inlineGet(d.id);
  const editing = !!inlineEdit[d.id];
  const live = (d.questions || []).filter(q => !q.withdrawn);
  const answered = d.status === 'answered' || d.status === 'resolved';
  const msg = st.msg ? el('div', {class: 'msgbar ' + st.msg.cls}, st.msg.text) : null;
  if (answered && !editing) {
    const latest = (d.revs || []).find(r => r.rev === d.answer_rev) || {slots: {}};
    return el('div', {class: 'bubble ask tlq'},
      el('div', {class: 'mh'}, icon(d.kind === 'approval' ? 'st-wait' : 'st-prog', 14), el('b', {}, d.title || d.id),
        el('span', {class: 'src'}, d.kind === 'approval' ? '审批' : '问题'), el('span', {class: 'src'}, `已提交 Rev ${d.answer_rev}`),
        el('span', {title: d.updated || ''}, when(d.updated))),
      el('div', {class: 'mtext'}, live.map(q => `${q.q}：${inlineDecision(d, q, latest.slots[q.q])}`).join('\n')),
      msg,
      el('div', {class: 'opts'},
        d.editable ? el('button', {onclick: () => { inlineEdit[d.id] = true; renderNeed(); }}, '更正回答') : null,
        el('a', {class: 'ghost', href: '#' + d.id}, '在完整页打开 ↗')));
  }
  if (d.status !== 'open' && !editing) {
    return el('div', {class: 'bubble ask tlq'},
      el('div', {class: 'mh'}, el('b', {}, d.title || d.id), el('span', {class: 'src'}, d.invalid || d.status)), msg);
  }
  const box = el('div', {class: 'bubble ask tlq' + (st.busy ? ' busy' : '')},
    el('div', {class: 'mh'}, icon(d.kind === 'approval' ? 'st-wait' : 'st-prog', 14), el('b', {}, d.title || d.id),
      el('span', {class: 'src'}, d.kind === 'approval' ? '审批 · 等你' : '问题 · 等你'),
      el('span', {title: d.updated || ''}, when(d.updated))),
    inlineContext(d));
  if (answered) box.append(el('div', {class: 'msgbar info'}, `正在更正：当前 Rev ${d.answer_rev}，提交后生成 Rev ${d.answer_rev + 1}，旧版作废。`));
  const cur = inlineDraft[d.id] || {checked: {}, texts: {}};
  const input = (q, f) => { const dr = inlineDraftOf(d); f(dr); dr.gen++; };
  for (const q of live) {
    const sel = cur.checked[q.q] || '';
    const qbox = el('div', {class: 'iq'}, el('div', {class: 'qtext'}, el('span', {class: 'qn'}, q.q), q.text));
    if (d.kind === 'approval') {
      const row = el('div', {class: 'opts'});
      q.options.forEach((o, i) => row.append(el('button', {class: 'opt' + (sel === 'opt:' + i ? ' go' : ''), disabled: st.busy,
        onclick: () => { if (st.busy) return; input(q, dr => { dr.checked[q.q] = 'opt:' + i; dr.choices[q.q] = o; }); renderNeed(); }}, o)));
      qbox.append(row);
      const ta = el('textarea', {id: 'i_' + d.id + '_' + q.q, rows: 2, disabled: st.busy,
        placeholder: '补充（选「C. 需要先改」时必填：要改什么）',
        oninput: e => { input(q, dr => { dr.texts[q.q] = e.target.value; }); }});
      ta.value = cur.texts[q.q] || '';
      qbox.append(ta);
    } else {
      const row = el('div', {class: 'opts'});
      const mkRadio = (v, label) => {
        const inp = el('input', {type: 'radio', name: 'i_' + d.id + '_' + q.q, value: v, disabled: st.busy,
          onchange: () => { if (st.busy) return; input(q, dr => { dr.checked[q.q] = v; dr.choices[q.q] = v.indexOf('opt:') === 0 ? label : ''; }); renderNeed(); }});
        if (sel === v) inp.checked = true;
        return el('label', {class: 'opt' + (sel === v ? ' go' : '')}, inp, el('span', {class: 'ot'}, label));
      };
      q.options.forEach((o, i) => row.append(mkRadio('opt:' + i, o)));
      if (q.suggestion) {
        row.append(mkRadio('suggested', '按建议'));
        qbox.append(el('p', {class: 'why'}, 'lead 建议：' + q.suggestion));
      }
      qbox.append(row);
      const ta = el('textarea', {id: 'i_' + d.id + '_' + q.q, rows: 2, disabled: st.busy, placeholder: '补充 / 原话（不选选项直接写，就算自由回答）',
        oninput: e => { input(q, dr => { dr.texts[q.q] = e.target.value; }); }});
      ta.value = cur.texts[q.q] || '';
      qbox.append(ta);
    }
    box.append(qbox);
  }
  box.append(el('div', {class: 'opts'},
    el('button', {class: 'pri', id: 'inline_' + d.id, disabled: st.busy, onclick: () => startInline(d)},
      st.busy ? '提交中…' : (answered ? '提交更正' : '提交回答')),
    editing ? el('button', {disabled: st.busy, onclick: () => { delete inlineEdit[d.id]; delete inlineDraft[d.id]; st.msg = null; renderNeed(); }}, '取消更正') : null,
    el('a', {class: 'ghost', href: '#' + d.id}, '在完整页打开 ↗')));
  if (msg) box.append(msg);
  return box;
}

function startInline(d) {
  const st = inlineGet(d.id);
  if (st.busy) return;
  const slots = inlineSlots(d);
  const live = (d.questions || []).filter(q => !q.withdrawn);
  const missing = live.filter(q => !slots[q.q]).map(q => q.q);
  if (missing.length) { st.msg = {cls: 'bad', text: '还有没答的：' + missing.join('、')}; renderNeed(); return; }
  if (d.kind === 'approval' && slots.Q1 && slots.Q1.choice === 'C. 需要先改' && !(slots.Q1.text || '').trim()) {
    st.msg = {cls: 'bad', text: '选「C. 需要先改」要写清楚改什么'}; renderNeed(); return;
  }
  const dr = inlineDraftOf(d);
  if (live.length > 1) { inlineSheet = {id: d.id, slots, d, rev: dr.rev, gen: dr.gen}; renderInlineSheet(); return; }
  inlineSubmit(d, slots, dr.rev, dr.gen);
}

async function inlineSubmit(d, slots, rev, dgen) {
  const st = inlineGet(d.id);
  if (st.busy) return;
  const [crev, arev] = String(rev || inlineRev(d)).split('/').map(Number);  // 用草稿自己的版本提交：版本变了服务端 409
  st.busy = true; st.gen++; st.msg = null; inlineSheet = null;
  const g = st.gen;
  renderInlineSheet(); renderNeed();
  let status = 0, j = {};
  try {
    ({status, j} = await api(`/api/q/${encodeURIComponent(d.id)}/submit`, {content_rev: crev, answer_rev: arev, slots}));
  } catch (e) { j = {error: String(e)}; }
  if (g !== st.gen) return;  // 迟到回包：不覆盖新的输入
  st.busy = false;
  if (status === 200) {
    const dr = inlineDraft[d.id];
    if (!dr || dr.gen === dgen) delete inlineDraft[d.id];  // 只清掉实际提交的那一版输入
    delete inlineEdit[d.id];
    st.msg = {cls: 'ok', text: `已提交 Rev ${j.answer_rev}，正在发回 lead`};
    await loadNeed(true); refreshList();
  } else {
    st.msg = {cls: 'bad', text: (status === 409 ? '版本冲突：' : '') + (j.error || ('错误 ' + status)) + '（输入还在，可以重试）'};
    renderNeed();
  }
}

function renderInlineSheet() {
  const box = document.getElementById('sheet');
  if (!inlineSheet) { if (box && !sheet) { box.removeAttribute('data-open'); box.replaceChildren(); } return; }
  const {d, slots, rev, gen} = inlineSheet;
  const rows = (d.questions || []).filter(q => !q.withdrawn).map(q =>
    el('div', {class: 'srow'}, el('strong', {}, q.q), el('div', {},
      el('div', {class: 'qq'}, q.text),
      el('div', {class: 'dec'}, inlineDecision(d, q, slots[q.q])))));
  box.setAttribute('data-open', '');
  box.replaceChildren(el('div', {class: 'dlg'}, el('h2', {}, `提交前确认 · ${rows.length} 个问题`),
    el('p', {class: 'mute small'}, '确认后才会发回 lead；提交后这一版回答会锁定为只读，可再点「更正回答」。'),
    rows,
    el('div', {class: 'btns'}, el('button', {onclick: () => { inlineSheet = null; renderInlineSheet(); }}, '返回修改'),
      el('button', {class: 'pri', onclick: () => inlineSubmit(d, slots, rev, gen)}, '确认提交'))));
}

// 输入框按需求分开记：正文、「同时发到 Issue」的勾选、发送中状态；重新渲染不丢
let needDrafts = {}, needToIssue = {}, needSending = {}, needSendReq = {};
function needComposer(n) {
  if (n.sealed) return el('div', {class: 'composer'}, el('div', {class: 'ph'}, '讨论已封存（不做）'));
  const busy = !!needSending[n.id];
  const ta = el('textarea', {id: 'need_text', rows: 2, disabled: busy, placeholder: '写消息给 ' + (n.ws ? 'lead-' + n.ws : '负责 lead') + '…',
    oninput: e => { needDrafts[n.id] = e.target.value; needMsg2 = null; }});
  ta.value = needDrafts[n.id] || '';
  const chk = el('input', {type: 'checkbox', id: 'need_to_issue', disabled: busy || !n.to_issue,
    onchange: e => { needToIssue[n.id] = !!e.target.checked; renderNeed(); }});
  chk.checked = !!(n.to_issue && needToIssue[n.id]);
  return el('div', {class: 'composer' + (chk.checked ? ' to-issue' : '')},
    ta,
    el('div', {class: 'row'},
      el('label', {class: 'chk'}, chk, ' 同时发到 Issue（团队可见）'),
      el('span', {class: 'mute small'}, !n.to_issue ? '还没有根 Issue：只能进 7788 讨论。'
        : chk.checked ? '这条会写到根 Issue，团队可见。' : '默认只进 7788 讨论；勾选后写到根 Issue。'),
      el('button', {class: 'send', id: 'need_send', disabled: busy, onclick: () => sendNeedMessage(n)},
        busy ? '…' : icon('i-send', 14))),
    el('div', {class: 'mute small hint'}, 'PR 上的讨论不会汇总到这里，请到 GitHub 查看'),
    el('div', {id: 'need_msg'}, needMsg2 ? el('div', {class: 'msgbar ' + needMsg2.cls}, needMsg2.text) : ''));
}

async function sendNeedMessage(n) {
  const id = n.id;
  if (needSending[id]) return;
  const text = needDrafts[id] || '';
  if (!text.trim()) { needMsg2 = {cls: 'bad', text: '先写内容'}; renderNeed(); return; }
  const toIssue = !!(n.to_issue && needToIssue[id]);
  // 幂等键绑定「正文 + 发到哪」：同一段话重试用同一个；改了正文或改了去向就换新的
  const rq = needSendReq[id];
  if (!rq || rq.text !== text || rq.toIssue !== toIssue) needSendReq[id] = {rid: rid(), text, toIssue};
  needSending[id] = true; needMsg2 = null; renderNeed();
  let status = 0, j = {};
  try { ({status, j} = await api(`/api/need/${encodeURIComponent(id)}/comment`,
    {body: text, request_id: needSendReq[id].rid, to_issue: toIssue})); } catch (e) { j = {error: String(e)}; }
  needSending[id] = false;
  if (status === 200) {
    delete needSendReq[id];
    if ((needDrafts[id] || '') === text) delete needDrafts[id];  // 只清掉实际发出的那段
    needToIssue[id] = false;
    if (needId === id) needMsg2 = {cls: 'ok', text: toIssue ? '已发到 GitHub，团队可见；正在通知 lead' : '已发到 7788 讨论，正在通知 lead'};
    await loadNeed(true); loadBoard(false);
  } else {
    if (needId === id) needMsg2 = {cls: 'bad', text: (j.error || ('错误 ' + status)) + '（输入还在' + (toIssue ? '，仍勾着「发到 Issue」' : '') + '，可以重试）'};
    if (mode === 'need' && needId === id) renderNeed();
  }
}

async function needAction(n, path, body, okText) {
  let status = 0, j = {};
  try { ({status, j} = await api(`/api/need/${encodeURIComponent(n.id)}/${path}`, body || {})); }
  catch (e) { j = {error: String(e)}; }
  needMsg2 = status === 200 ? {cls: 'ok', text: okText || j.note || '已完成'} : {cls: 'bad', text: j.error || ('错误 ' + status)};
  await loadNeed(true);
}

async function confirmNeed(n) {
  const b = document.getElementById('confirm_' + n.id); if (b) b.disabled = true;
  await needAction(n, 'confirm', {card_rev: n.card_rev, request_id: rid()}, `已确认理解卡 v${n.card_rev}，会通知负责 lead 建 Issue`);
}

function renderNeed() {
  const main = document.getElementById('main'), n = needData;
  if (!n) { main.replaceChildren(el('div', {class: 'wrap'}, el('p', {class: 'mute'}, needErr || '加载中…'))); return; }
  const top = main.scrollTop;
  const wrap = el('div', {class: 'wrap detailwrap'});
  const crumb = el('div', {class: 'crumb'},
    el('span', {class: 'sq', style: 'background:' + wsColor(n.ws)}), el('a', {href: '#' + BOARD}, n.ws || '需求'),
    el('span', {class: 'sep'}, '/'), el('span', {class: 'mono'}, n.number ? `#${n.number}` : n.id),
    el('span', {class: 'chipw' + (n.lead_state === 'present' ? ' live' : '')},
      icon('st-prog', 12), n.lead_state === 'present' ? 'lead-' + n.ws + ' 在线' : n.lead_state === 'absent' ? 'lead 不在' : 'lead 状态待核'));
  wrap.append(el('header', {}, crumb, el('h1', {}, n.title || n.id),
    el('div', {class: 'rowflags'}, el('span', {class: 'chip ' + (n.column === 'done' ? 'ok' : n.column === 'wait' ? 'hot' : n.column === 'stuck' ? 'bad' : 'info')}, n.column_label),
      (n.flags || []).map(flagEl),
      n.status === 'discussing' && n.discuss === 'waiting_owner' && n.card_rev
        ? el('button', {class: 'pri', id: 'confirm_' + n.id, onclick: () => confirmNeed(n)}, `确认理解卡 v${n.card_rev}`) : null,
      n.loop ? el('button', {id: 'unloop_' + n.id, onclick: () => needAction(n, 'unloop')}, '解除疑似循环') : null,
      n.can_takeover ? el('button', {id: 'takeover_' + n.id, onclick: () => needAction(n, 'takeover')}, '请 Router 安排接手') : null)));
  if (n.body_html) wrap.append(el('div', {class: 'body prose'}, html(n.body_html)));
  else wrap.append(el('div', {class: 'body'}, n.text || ''));
  if ((n.subs || []).length) {
    wrap.append(el('div', {class: 'subhd'}, icon('i-list', 14),
      `子 Issue · 完成 ${n.subs_progress.done} · 不做 ${n.subs_progress.skipped} · 未完 ${n.subs_progress.left}`),
      el('div', {class: 'subs'}, n.subs.map(s => el('div', {class: 'subrow'},
        icon(s.state === 'CLOSED' ? (s.state_reason === 'NOT_PLANNED' ? 'st-todo' : 'st-done') : (s.state === 'OPEN' ? 'st-prog' : 'st-todo'), 14),
        el('span', {class: 'k'}, '#' + s.number), ghLink(s.url, s.title || ''),
        el('span', {class: 'r'}, s.state === 'CLOSED' ? (s.state_reason === 'NOT_PLANNED' ? '不做' : '完成') : '未完')))));
  }
  wrap.append(el('div', {class: 'sech'}, '对话', el('span', {}, '7788 讨论和根 Issue 评论在同一时间线上')));
  wrap.append(timelineView(n));
  if (needErr) wrap.append(el('div', {class: 'msgbar bad'}, '无法刷新：' + needErr));
  for (const e of n.errors || []) wrap.append(el('div', {class: 'msgbar warn'}, e));
  wrap.append(needComposer(n));
  const aside = el('aside', {class: 'props'},
    el('div', {}, el('h4', {}, '属性'), el('div', {class: 'prop'},
      el('span', {class: 'k'}, '状态'), el('span', {class: 'v'}, icon(n.column === 'done' ? 'st-done' : n.column === 'wait' ? 'st-wait' : n.column === 'stuck' ? 'st-bad' : n.column === 'review' ? 'st-review' : n.column === 'todo' ? 'st-todo' : 'st-prog', 14), n.column_label),
      el('span', {class: 'k'}, '工作区'), el('span', {class: 'v'}, el('span', {class: 'sq', style: 'background:' + wsColor(n.ws)}), n.ws || '—'),
      el('span', {class: 'k'}, '负责'), el('span', {class: 'v'}, n.lead && n.lead.lead_session ? [avatar(n.ws), 'lead-' + n.ws] : '—'),
      el('span', {class: 'k'}, '需求'), el('span', {class: 'v mono'}, n.id),
      el('span', {class: 'k'}, '创建'), el('span', {class: 'v'}, when(n.created)))),
    (n.flags || []).length ? el('div', {class: 'waitbox'}, (n.flags || []).map(flagEl)) : null,
    el('div', {}, el('h4', {}, 'Pull requests'), (n.prs || []).length
      ? n.prs.map(p => el('div', {class: 'pr'}, el('div', {class: 'l1'}, ghLink(p.url, '#' + p.number + ' ' + (p.title || ''))),
        el('div', {class: 'l2'}, p.draft ? '草稿' : (p.state === 'MERGED' ? '已合并' : ({APPROVED: '已批准', CHANGES_REQUESTED: '要求修改', REVIEW_REQUIRED: '待审查'}[p.review] || '无 review')),
          p.additions || p.deletions ? ` · +${p.additions || 0} −${p.deletions || 0}` : '',
          p.checks ? ' · checks ' + p.checks : ''))) : el('p', {class: 'mute small'}, '还没有登记 PR')),
    n.receipt && n.receipt.note ? el('div', {class: 'mute small'}, '回执：' + n.receipt.note) : null,
    n.github ? el('div', {class: 'small'}, ghLink(n.github, '在 GitHub 打开 ↗')) : null);
  wrap.append(aside);
  main.replaceChildren(wrap);
  main.scrollTop = top;
  const ta = document.getElementById('need_text'); if (ta) ta.value = needDrafts[n.id] || '';
  renderInlineSheet();
}

async function loadNeed(force) {
  const id = needId, seq = ++needSeq;
  let status = 0, j = {};
  try { ({status, j} = await api('/api/need/' + encodeURIComponent(id))); } catch (e) { j = {error: String(e)}; }
  if (seq !== needSeq || needId !== id) return;
  needAt = Date.now();
  if (status === 200) { needData = j; needErr = ''; if (mode === 'need') renderNeed(); }
  else { needErr = j.error || ('错误 ' + status); if (mode === 'need') renderNeed(); }
}

function boardCard(c) {
  const unread = c.unread > 0 ? el('span', {class: 'unread', title: '有没看过的评论'}) : null;
  return el('div', {class: 'bcard' + (c.loop ? ' bad' : ''), id: 'bc_' + c.id},
    el('div', {class: 'row1'}, el('span', {class: 'mono'}, c.number ? `#${c.number}` : c.id), unread,
      c.live ? el('span', {class: 'live'}, el('span', {class: 'pulse'}), '在干') : null,
      el('span', {class: 'when', title: c.updated || ''}, when(c.updated))),
    el('a', {class: 'bt', href: '#' + NEEDP + ':' + c.id}, c.title || c.id),
    (c.flags || []).slice(0, 2).map(flagEl),
    el('div', {class: 'row3'}, c.ws ? el('span', {class: 'ws'}, el('span', {class: 'sq', style: 'background:' + wsColor(c.ws)}), c.ws) : null,
      avatar(c.ws), el('span', {class: 'who'}, c.who || ''),
      (c.flags || []).some(f => f.text === c.next) ? null : el('span', {class: 'next'}, c.next || '')),
    (c.subs && c.subs.total ? el('div', {class: 'small mute'}, `子 Issue：完成 ${c.subs.done} / 不做 ${c.subs.skipped} / 未完 ${c.subs.left}`) : null),
    (c.prs || []).length ? el('div', {class: 'small mute'}, c.prs.map(p => `PR #${p.number}${p.draft ? ' 草稿' : p.state === 'MERGED' ? ' 已合并' : p.review === 'APPROVED' ? ' 已批准' : p.review === 'CHANGES_REQUESTED' ? ' 要求修改' : ''}${p.checks === 'FAILURE' ? ' · checks ✗' : p.checks === 'SUCCESS' ? ' · checks ✓' : ''}`).join('；')) : null,
    c.flags && c.flags.length && c.column === 'wait' && !archived(c)
      ? el('button', {class: 'arch', onclick: e => { e.preventDefault(); store('qi.arch.' + c.id, String(c.updated || '')); renderBoard(); }}, '归档')
      : null);
}

function showNewNeed() {
  const box = document.getElementById('sheet');
  if (!box) return;
  const dialog = el('div', {class: 'dlg'}, el('h2', {}, '提需求'),
    el('p', {class: 'mute small'}, '一句话说要做什么，不用选 ws；提交后回到看板，卡片会出现在「待立项」。'),
    el('textarea', {id: 'new_need_text', rows: 3, oninput: e => { boardNewText = e.target.value; }}),
    el('div', {class: 'msgbar bad', id: 'new_need_err'}, boardNewErr),
    el('div', {class: 'btns'}, el('button', {onclick: closeNewNeed}, '取消'),
      el('button', {class: 'pri', id: 'new_need_go', disabled: boardNewBusy, onclick: submitNewNeed}, boardNewBusy ? '提交中…' : '提交')));
  box.setAttribute('data-open', '');
  box.replaceChildren(dialog);
  const ta = document.getElementById('new_need_text'); if (ta) { ta.value = boardNewText; ta.focus && ta.focus(); }
}

function closeNewNeed() {
  boardNewOpen = false; boardNewText = ''; boardNewReq = ''; boardNewErr = '';
  const box = document.getElementById('sheet'); if (box) { box.removeAttribute('data-open'); box.replaceChildren(); }
}

async function submitNewNeed() {
  if (boardNewBusy) return;
  const text = boardNewText || '';
  if (!text.trim()) { needMsg = {cls: 'bad', text: '先写内容'}; return; }
  if (!boardNewReq || needReqText2 !== text) { boardNewReq = rid(); needReqText2 = text; }
  boardNewErr = '';
  boardNewBusy = true; showNewNeed();
  let status = 0, j = {};
  try { ({status, j} = await api('/api/need', {text, request_id: boardNewReq})); } catch (e) { j = {error: String(e)}; }
  boardNewBusy = false;
  if (status === 200) {
    closeNewNeed();
    boardMsg = {cls: 'ok', text: '已提交 ' + j.id + '，分配失败的会进待处理'};
    loadBoard(true);
  } else {
    boardNewErr = (j.error || ('错误 ' + status)) + '（输入还在，可以重试）';
    showNewNeed();
  }
}

function renderBoard() {
  const main = document.getElementById('main'), v = boardData, top = main.scrollTop;
  const wrap = el('div', {class: 'wrap boardwrap'});
  wrap.append(el('header', {class: 'boardhead'}, el('h1', {}, boardWs ? boardWs : '全部需求'),
    el('button', {class: 'primary', onclick: showNewNeed}, '+ 提需求')));
  if (boardMsg) wrap.append(el('div', {class: 'msgbar ' + boardMsg.cls}, boardMsg.text));
  if (boardErr) wrap.append(el('div', {class: 'msgbar bad'}, '无法刷新：' + boardErr + (v ? `；下面是 ${v.generated_at} 的数据` : '')));
  if (v && v.degrade) wrap.append(el('div', {class: 'msgbar warn'}, 'GitHub 剩余配额低，轮询已降档到 10 分钟；打开详情页仍会立即拉一次。'));
  if (!v || !Array.isArray(v.columns)) { wrap.append(el('p', {class: 'mute'}, boardErr || '加载中…')); main.replaceChildren(wrap); return; }
  const board = el('div', {class: 'board'});
  for (const col of v.columns) {
    const cards = col.cards.filter(c => (!boardWs || c.ws === boardWs) && !(col.key === 'wait' && archived(c)));
    board.append(el('section', {class: 'bcol c-' + col.key},
      el('div', {class: 'colh'}, icon('st-' + ({wait: 'wait', stuck: 'bad', todo: 'todo', review: 'review', doing: 'prog', done: 'done'}[col.key] || 'todo'), 14),
        el('span', {}, col.label), el('span', {class: 'c'}, String(cards.length))),
      el('div', {class: 'cards'}, cards.map(boardCard))));
  }
  wrap.append(board);
  if (routerHealth) wrap.append(el('details', {class: 'router-mini'},
    el('summary', {}, 'Router · ' + (routerHealth.paused ? '暂停中' : routerHealth.gave_up ? '起不来' : routerHealth.present ? '在线' : '不在')),
    routerCard(routerHealth)));
  main.replaceChildren(wrap);
  main.scrollTop = top;
}

async function loadRouterHealth() {
  let status = 0, j = {};
  try { ({status, j} = await api('/api/needs')); } catch (e) { j = {error: String(e)}; }
  if (status === 200 && j.router) { routerHealth = j.router; if (mode === 'board') renderBoard(); }
}

async function loadBoard(refresh) {
  const seq = ++boardSeq;
  let status = 0, j = {};
  try { ({status, j} = await api('/api/board' + (refresh ? '?refresh=1' : ''))); } catch (e) { j = {error: String(e)}; }
  if (seq !== boardSeq) return;
  boardAt = Date.now();
  if (status === 200) { boardData = j; boardErr = ''; } else boardErr = j.error || ('错误 ' + status);
  renderModes(); renderSide();
  if (mode === 'board') renderBoard();
  if (mode === 'inbox') renderInbox();
  loadRouterHealth().catch(() => {});
}

async function loadTasks(refresh) {
  const seq = ++tasksSeq;
  tasksLoading = true; if (mode === 'tasks') renderTasks();
  let status = 0, j = {};
  try { ({status, j} = await api('/api/tasks' + (refresh ? '?refresh=1' : ''))); } catch (e) { j = {error: String(e)}; }
  if (seq !== tasksSeq) return;
  tasksLoading = false; tasksAt = Date.now();
  if (status === 200) { tasksData = j; tasksErr = ''; } else tasksErr = j.error || ('错误 ' + status);  // 失败时保留上一次的数据
  if (mode === 'tasks') renderTasks();
  if (mode === 'needs') renderNeedsSoon();  // 需求页的 Issue 进度复用任务数据
}

function taskRow(t) {
  if (t.kind === 'parent') {
    return el('div', {class: 'trow pref d' + t.depth},
      el('div', {class: 't1'}, '父 ', ghLink(t.url, `${t.repo}#${t.number}`), t.stage ? ` · 在「${t.stage}」` : ' · 不在列表里',
        t.sub ? ` · 子 Issue ${t.sub.completed}/${t.sub.total} 已关` : ''),
      t.title ? el('div', {}, t.title) : null);
  }
  const why = t.why || [];
  return el('div', {class: 'trow d' + t.depth},
    el('div', {class: 't1'}, el('span', {}, t.ws), '·', ghLink(t.url, `${t.repo}#${t.number}`),
      el('span', {class: 'chip ' + (STAGE_CLS[t.stage.key] || 'mute'), title: why[0] || ''}, t.stage.label),
      (t.tags || []).map(x => el('span', {class: 'tag', title: x.why}, x.label)),
      t.need ? el('a', {class: 'tag', href: '#' + TASKS + ':' + t.need, title: '只看这个需求的任务'}, t.need) : null,
      t.sub ? el('span', {class: 'tag', title: '子 Issue 全关不等于父 Issue 完成'}, `子 Issue ${t.sub.completed}/${t.sub.total} 已关`) : null),
    el('div', {class: 't2'}, t.title),
    el('div', {class: 't3'}, el('span', {}, '依据：' + (why[0] || '—')),
      (t.questions || []).map(q => el('a', {href: '#' + q.id, title: q.title}, `去回答 ${q.id} →`)),
      (t.prs || []).filter(p => p.state === 'OPEN' || p.state === 'MERGED').map(p =>
        ghLink(p.url, `PR ${p.repo === t.repo.toLowerCase() ? '' : p.full}#${p.number}${p.state === 'MERGED' ? '（已合并）' : p.draft ? '（草稿）' : ''}`)),
      t.updated ? el('span', {}, 'Issue 更新 ', when(t.closed || t.updated)) : null));
}

function renderTasks() {
  const main = document.getElementById('main'), v = tasksData, top = main.scrollTop;
  const wrap = el('div', {class: 'wrap tasks'});
  wrap.append(el('header', {}, el('h1', {}, '任务'),
    el('div', {class: 'thead state'},
      el('span', {}, '数据时间：', v ? when(v.generated_at) : '—'),
      el('button', {id: 'tasks_refresh', disabled: tasksLoading, onclick: () => loadTasks(true)}, tasksLoading ? '刷新中…' : '刷新'),
      el('span', {class: 'small'}, '只读：状态以 GitHub 为准，这里每次现查，不存任何东西。'))));
  if (tasksErr) wrap.append(el('div', {class: 'msgbar bad'}, `无法刷新：${tasksErr}` + (v ? `；下面是 ${v.generated_at} 的数据` : '')));
  if (v && v.config_error) wrap.append(el('div', {class: 'msgbar bad'}, v.config_error));
  for (const s of (v && v.sources) || []) {
    if (s.error) wrap.append(el('div', {class: 'msgbar bad'}, `无法刷新 ${s.name}：${s.error}；` +
      (s.has_data ? `显示的是 ${s.at} 的数据` : '没有可用数据（不代表没有任务）')));
    for (const m of s.more || []) wrap.append(el('div', {class: 'msgbar warn'}, `${s.name}：${m}`));
  }
  if (!v) { wrap.append(el('p', {class: 'mute'}, tasksErr ? '还没有拿到过数据。' : '加载中…')); main.replaceChildren(wrap); return; }
  const needs = v.needs || {};
  if (taskFilter) {
    wrap.append(el('div', {class: 'msgbar info'}, `只看需求 ${taskFilter} 的任务（按根 Issue 正文第一行「需求：${taskFilter}」归组）· `,
      el('a', {href: '#' + TASKS}, '显示全部'), ' · ', el('a', {href: '#' + NEEDS}, '回需求')));
    if (!(needs[taskFilter] || []).length) wrap.append(el('p', {class: 'mute'}, '任务里还没有正文第一行写着这个需求号的 Issue（可能还没建、或 repo 不在 tasks.json 里）。'));
  } else if (Object.keys(needs).length) {
    const box = el('section', {class: 'tgrp'}, el('h2', {}, '按需求', el('span', {class: 'n mute small'}, `${Object.keys(needs).length} 个需求`)));
    for (const [nid, list] of Object.entries(needs).sort((a, b) => b[0].localeCompare(a[0]))) {
      box.append(el('h3', {}, el('a', {href: '#' + TASKS + ':' + nid}, nid)),
        ...list.map(t => el('div', {class: 'trow d' + t.depth}, el('div', {class: 't1'}, ghLink(t.url, `${t.repo}#${t.number}`),
          el('span', {class: 'chip ' + (STAGE_CLS[t.stage.key] || 'mute')}, t.stage.label),
          t.sub ? el('span', {class: 'tag'}, `子 Issue ${t.sub.completed}/${t.sub.total} 已关`) : null, t.title))));
    }
    wrap.append(box);
  }
  for (const g of v.groups) {
    const ws = g.ws.map(w => ({ws: w.ws, tasks: taskFilter ? w.tasks.filter(t => t.kind === 'task' && t.need === taskFilter) : w.tasks}))
      .filter(w => w.tasks.length);
    if (!ws.length) continue;
    const n = taskFilter ? ws.reduce((a, w) => a + w.tasks.length, 0) : g.count;
    const box = el('section', {class: 'tgrp ' + g.key}, el('h2', {}, g.title, el('span', {class: 'n mute small'}, `${n} 个`)));
    for (const w of ws) box.append(el('h3', {}, w.ws), ...w.tasks.map(taskRow));
    wrap.append(box);
  }
  if ((v.unlinked || []).length) {
    wrap.append(el('section', {class: 'tgrp'}, el('h2', {}, '未关联的待答题', el('span', {class: 'n mute small'}, `${v.unlinked.length} 题`)),
      v.unlinked.map(q => el('div', {class: 'trow'}, el('div', {class: 't1'}, q.ws, '·', q.issue === 'none' ? '无 Issue' : q.issue),
        el('a', {href: '#' + q.id}, `${q.id} · ${q.title} →`)))));
  }
  const ok = (v.sources || []).every(s => !s.error) && !v.config_error;
  if (!v.groups.length && !(v.unlinked || []).length) wrap.append(el('p', {class: 'mute'}, ok ? '没有任务。' : '没有可显示的任务（有来源查询失败，见上）。'));
  main.replaceChildren(wrap);
  main.scrollTop = top;
}

// ---------- 需求（提交一句话、补充 / 纠正、Router 健康与全局暂停）----------
// 输入只存在内存里（needDraft），轮询重建时填回；有输入框正在聚焦就推迟重建，不打断输入
let needsData = null, needsErr = '', needsSeq = 0, needsStale = false, needMsg = null;
const needDraft = {}, needReq = {}, needBusy = {};
const NEED_ST = {new: ['新', 'info'], routing: ['Router 分派中', 'info'], assigned: ['已分到 ws', 'info'], discussing: ['讨论中', 'info'],
  confirmed: ['理解已确认', 'ok'], filed: ['已立项', 'ok'], dropped: ['不做', 'mute'], held: ['卡住', 'bad']};
let needFocus = '', needFocused = '';

function rid() {  // 客户端生成的幂等键：同一段文字重试用同一个
  try { return crypto.randomUUID(); } catch (e) { return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 14); }
}

async function loadNeeds() {
  const seq = ++needsSeq;
  let status = 0, j = {};
  try { ({status, j} = await api('/api/needs')); } catch (e) { j = {error: String(e)}; }
  if (seq !== needsSeq) return;
  if (status === 200) {
    const same = JSON.stringify(j) === JSON.stringify(needsData);
    needsData = j; if (same && !needsErr) return; needsErr = '';
  } else needsErr = j.error || ('错误 ' + status);
  if (mode === 'needs') renderNeedsSoon();
}

function typingNow() { const a = document.activeElement; return !!a && String(a.tagName || '').toUpperCase() === 'TEXTAREA'; }
function renderNeedsSoon() { if (mode !== 'needs') return; if (typingNow()) { needsStale = true; return; } renderNeeds(); }

async function needPost(key, path, body, done) {
  if (needBusy[key]) return;
  needBusy[key] = true; if (mode === 'board') renderBoard(); else renderNeeds();
  let status = 0, j = {};
  try { ({status, j} = await api(path, body)); } catch (e) { j = {error: String(e)}; }
  needBusy[key] = false;
  if (status === 200) done(j); else needMsg = {cls: 'bad', text: (j.error || ('错误 ' + status)) + '（输入还在，可以重试）'};
  if (mode === 'board') renderBoard();
  if (mode === 'board' || mode === 'inbox') loadBoard(false);
}

function sendText(key, path, okText) {
  const text = needDraft[key] || '';
  if (!text.trim()) {  // 刚提交成功又点了一下：不盖掉「已提交」
    if (!(needMsg && needMsg.cls === 'ok' && Date.now() - needMsg.at < 5000)) needMsg = {cls: 'bad', text: '先写内容'};
    renderNeeds(); return;
  }
  if (!needReq[key] || needReq[key].text !== text) needReq[key] = {text, id: rid()};
  needPost(key, path, {text, request_id: needReq[key].id}, j => {
    if (needDraft[key] === text) needDraft[key] = '';
    delete needReq[key];
    needMsg = {cls: 'ok', text: okText(j), at: Date.now()};
  });
}

function draftBox(key, placeholder, rows) {
  const t = el('textarea', {id: 'nd_' + key, rows, placeholder, oninput: e => { needDraft[key] = e.target.value; }});
  t.value = needDraft[key] || '';
  return t;
}

function routerCard(h) {
  const st = h.paused ? ['暂停中', 'mute'] : h.gave_up ? ['Router 起不来', 'bad'] : h.present ? ['在线', 'ok'] : !h.socket ? ['session 不在', 'bad'] : ['不在', 'hot'];
  const rows = [['登记', h.registered ? `已登记（session ${h.session} · pane ${h.pane_id} · ${h.registered_at}）` : '未登记'],
    ['现在', h.present ? '在' : (h.why || '不在')],
    ['被拉起', `${h.ensure_total} 次` + (h.ensure_last_at ? `（最近 ${h.ensure_last_at} · ${h.ensure_last_result || '进行中'}）` : '')],
    ['最近一次投递', h.last_delivery_at || '—']];
  return el('section', {class: 'rcard' + (h.gave_up ? ' bad' : ''), id: 'router_card'},
    el('div', {class: 'thead'}, el('strong', {}, 'Router'), el('span', {class: 'chip ' + st[1]}, st[0]),
      el('button', {id: 'pause_btn', disabled: !!needBusy._pause, onclick: () => needPost('_pause', '/api/router/pause', {paused: !h.paused},
        j => { needMsg = {cls: 'ok', text: j.paused ? '已全局暂停' : '已恢复'}; })},
        needBusy._pause ? (h.paused ? '恢复中…' : '暂停中…等当前动作做完') : (h.paused ? '恢复' : '全局暂停')),
      !h.present && h.socket && !h.paused && !h.gave_up ? el('button', {id: 'rereg_btn', disabled: !!needBusy._rereg,
        onclick: () => needPost('_rereg', '/api/router/reregister', {}, j => { needMsg = {cls: 'ok', text: '已请 Router 重新登记：' + j.note, at: Date.now()}; })},
        '让 Router 重新登记') : null,
      h.gave_up ? el('button', {id: 'retry_btn', disabled: !!needBusy._retry, onclick: () => needPost('_retry', '/api/router/retry', {},
        j => { needMsg = {cls: 'ok', text: j.note}; })}, '重新尝试拉起') : null),
    h.gave_up ? el('div', {class: 'msgbar bad'}, 'Router 起不来：1 小时内自动拉起 3 次仍不在，已停止自动拉起。' + (h.last_error ? '原因：' + h.last_error : '')) : null,
    h.paused ? el('div', {class: 'msgbar warn'}, '全局暂停：不向 Router 投递、不自动拉起 Router，Router 不分派、不派工、不新起 lead。已经在跑的 lead 不受影响：你在讨论线程里的回复和确认照常投给负责 lead。') : null,
    el('div', {class: 'kv small'}, rows.map(([k, v]) => [el('span', {class: 'mute'}, k), el('span', {}, v)])));
}

function taskByUrl() {
  const m = {};
  for (const g of (tasksData && tasksData.groups) || []) for (const w of g.ws) for (const t of w.tasks) if (t.kind === 'task' && t.url) m[t.url] = t;
  return m;
}

function cardView(x, latest) {
  return el('div', {class: 'ncard' + (latest ? ' cur' : '')}, el('div', {class: 'small mute'}, `lead · 理解卡 v${x.rev} · ${x.at}`),
    x.card.map(([k, v]) => el('div', {class: 'csec'}, el('strong', {}, k), el('div', {class: 'plain'}, v || '（空）'))));
}

function threadView(n) {
  if (!n.thread.length) return null;
  const items = [];
  for (const x of n.thread) {
    if (x.kind === 'card') {
      const latest = x.rev === n.card_rev;
      items.push(latest ? cardView(x, true) : fold(`card_${n.id}_${x.rev}`, 'fold', `理解卡 v${x.rev}（旧版）`, false, cardView(x, false)));
    } else if (x.kind === 'confirm') items.push(el('div', {class: 'nt owner'}, `Owner · ${x.at}：确认理解卡 v${x.rev}`));
    else items.push(el('div', {class: 'nt ' + (x.by === 'owner' ? 'owner' : 'router')}, `${x.by === 'owner' ? 'Owner' : 'lead'} · ${x.at}：${x.text}`));
  }
  return el('div', {class: 'thread'}, el('div', {class: 'small mute'}, '讨论线程' + (n.sealed ? '（已封存，只读）' : '')), items);
}

function needRow(n, idx) {
  let st = NEED_ST[n.status] || [n.status, 'mute'];
  if (n.status === 'discussing') st = n.discuss === 'waiting_owner' ? ['讨论中 · 等你', 'hot'] : ['讨论中 · 等 lead', 'info'];
  const first = n.text.split('\n')[0];
  const issues = n.issues.map((u, i) => {
    const m = /github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)$/.exec(u), t = idx[u];
    return el('span', {class: 'iss'}, i === 0 ? el('span', {class: 'tag'}, '根') : null, ghLink(u, m ? `${m[1]}#${m[2]}` : u),
      t ? el('span', {class: 'chip ' + (STAGE_CLS[t.stage.key] || 'mute'), title: (t.why || [])[0] || ''}, t.stage.label) : null,
      t && t.sub ? el('span', {class: 'tag'}, `子 Issue ${t.sub.completed}/${t.sub.total} 已关`) : null);
  });
  const busy = !!needBusy[n.id], cf = '_cf' + n.id + '_' + n.card_rev;
  const confirm = n.status === 'discussing' && n.discuss === 'waiting_owner' && n.card_rev
    ? el('button', {class: 'pri', id: 'confirm_' + n.id, disabled: !!needBusy[cf], onclick: () => {
        needReq[cf] = needReq[cf] || rid();
        needPost(cf, `/api/need/${encodeURIComponent(n.id)}/confirm`, {card_rev: n.card_rev, request_id: needReq[cf]},
          () => { needMsg = {cls: 'ok', text: `已确认 ${n.id} 的理解卡 v${n.card_rev}，会通知负责 lead 建 Issue`, at: Date.now()}; });
      }}, `确认理解卡 v${n.card_rev}`) : null;
  const hint = n.lead ? '回复负责 lead（进讨论线程，投递给它）' : '补充 / 纠正（还没有负责 lead：记成 note，Router 处理时会看到）';
  return el('section', {class: 'nrow' + (n.id === needFocus ? ' focus' : ''), id: 'need_' + n.id},
    el('div', {class: 't1'}, el('strong', {}, n.id), el('span', {class: 'chip ' + st[1]}, st[0]), n.ws ? el('span', {}, 'ws ' + n.ws) : null,
      n.lead ? el('span', {}, '负责 lead：' + n.lead) : n.dispatch ? el('span', {}, '已派给 ' + n.dispatch + '，等它 claim') : null,
      when(n.created), n.delivery ? el('span', {title: n.delivery_detail || ''}, '投递：' + n.delivery) : null,
      n.renotify ? el('button', {onclick: () => needPost('_rn' + n.id, `/api/need/${encodeURIComponent(n.id)}/renotify`, {},
        j => { needMsg = {cls: 'ok', text: j.note, at: Date.now()}; })}, '重新通知') : null,
      n.pending_notes ? el('span', {class: 'chip hot'}, `${n.pending_notes} 条补充待 Router 处理`) : null,
      el('a', {href: '#' + TASKS + ':' + n.id}, '看任务 →')),
    el('div', {class: 'ntext', title: n.text}, first.length > 160 ? first.slice(0, 160) + '…' : first),
    n.text !== first ? fold('nfull_' + n.id, 'fold', '原话全文', false, el('div', {class: 'plain'}, n.text)) : null,
    n.why ? el('div', {class: 'small'}, '分配理由：' + n.why) : null,
    issues.length ? el('div', {class: 'iss'}, issues) : null,
    n.notes.map(x => el('div', {class: 'nt ' + (x.by === 'owner' ? 'owner' : 'router')}, `${{owner: 'Owner', router: 'Router', lead: 'lead'}[x.by] || x.by} · ${x.at}：${x.text}`)),
    threadView(n),
    n.sealed ? null : el('div', {class: 'nbox'}, draftBox(n.id, hint, 1),
      el('button', {disabled: busy, onclick: () => sendText(n.id, `/api/need/${encodeURIComponent(n.id)}/reply`,
        j => `已发到 ${n.id}` + (j.to === 'lead' ? '，会投递给负责 lead' : ''))}, busy ? '提交中…' : (n.lead ? '回复' : '补充 / 纠正')), confirm));
}

function renderNeeds() {
  needsStale = false;
  const main = document.getElementById('main'), v = needsData, top = main.scrollTop, busy = !!needBusy._new;
  const wrap = el('div', {class: 'wrap needs'});
  wrap.append(el('header', {}, el('h1', {}, '需求')),
    el('section', {class: 'nform'}, el('h2', {}, '提交需求'),
      el('p', {class: 'mute small'}, '一句话说要做什么，不用选 ws。Router 会查重、判断归属、交给对应 ws 的 lead；进度在下面和「任务」里看。'),
      draftBox('_new', '例如：给 demo 的导出加一个 CSV 格式', 3),
      el('div', {class: 'acts'}, el('button', {class: 'pri', id: 'need_go', disabled: busy,
        onclick: () => sendText('_new', '/api/need', j => `已提交 ${j.id}` + (j.created ? '' : '（重复提交，返回的是原记录）'))}, busy ? '提交中…' : '提交'))),
    el('div', {id: 'need_msg'}, needMsg ? el('div', {class: 'msgbar ' + needMsg.cls}, needMsg.text) : ''));
  if (needsErr) wrap.append(el('div', {class: 'msgbar bad'}, '无法刷新：' + needsErr));
  if (v && v.router && Array.isArray(v.needs)) {
    wrap.append(routerCard(v.router));
    const idx = taskByUrl();
    if (!v.needs.length) wrap.append(el('p', {class: 'mute'}, '还没有需求。'));
    for (const n of v.needs) wrap.append(needRow(n, idx));
  } else if (!needsErr) wrap.append(el('p', {class: 'mute'}, '加载中…'));
  main.replaceChildren(wrap);
  main.scrollTop = top;
  const f = needFocus && document.getElementById('need_' + needFocus);
  if (f && needFocused !== needFocus) { needFocused = needFocus; if (f.scrollIntoView) f.scrollIntoView({block: 'start'}); }
}

const LEAVE_MSG = '当前问题有未保存的输入，离开会丢失。确定离开？';

// 切题前有未保存的输入：先确认，取消就留在原题并恢复 hash。前端不在后台发任何写请求
function route() {
  let next = decodeURIComponent(location.hash.slice(1)) || null;
  // C3：旧地址还能打开，但不再有自己的页面
  if (next === NEEDS) next = BOARD;
  else if ((next || '').startsWith(NEEDS + ':')) next = NEEDP + ':' + next.slice(NEEDS.length + 1);
  if (detail && next === cur) return;  // 取消离开时恢复 hash 触发的那次 hashchange
  if (dirty && detail && detail.editable && !confirm(LEAVE_MSG)) { location.hash = '#' + cur; return; }
  // 题号形如 <ws>-NNNN（不含冒号），不会和 #tasks、#board、#need: 撞
  const toTasks = next === TASKS || (next || '').startsWith(TASKS + ':');
  const toBoard = next === BOARD || (next || '').startsWith(BOARD + ':');
  const toNeed = (next || '').startsWith(NEEDP + ':');
  gen++; cur = toTasks || toBoard || toNeed ? null : next; detail = null; dirty = false; msg = null;
  curQ = null; editing = false; sheet = null; openState = {}; shownId = null; formShown = false;
  mode = toTasks ? 'tasks' : toBoard ? 'board' : toNeed ? 'need' : 'inbox';
  taskFilter = toTasks ? next.slice(TASKS.length + 1) : '';
  needId = toNeed ? next.slice(NEEDP.length + 1) : '';
  boardWs = toBoard && next.length > BOARD.length + 1 ? next.slice(BOARD.length + 1) : '';
  needMsg2 = null; boardMsg = boardMsg && toBoard ? boardMsg : null;
  renderSheet(); renderModes(); renderSide();
  document.getElementById('app').setAttribute('data-view', toTasks ? 'tasks' : toBoard ? 'board' : toNeed ? 'need' : cur ? 'detail' : 'list');
  if (toTasks) { renderTasks(); loadTasks(false); refreshList(); return; }
  if (toBoard) { renderBoard(); loadBoard(false); refreshList(); return; }
  if (toNeed) { if (!boardData) loadBoard(false); renderNeed(); loadNeed(true); return; }
  document.getElementById('main').replaceChildren(el('div', {class: 'wrap'}, el('p', {class: 'mute'}, cur ? '加载中…' : '待处理')));
  if (!boardData) loadBoard(false);
  load(true); refreshList();
}
window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', e => {
  if (dirty || Object.values(needDraft).some(v => (v || '').trim())) { e.preventDefault(); e.returnValue = LEAVE_MSG; }
});
window.addEventListener('keydown', onKey);
route();
setInterval(() => {
  refreshList(); load(false);
  if ((mode === 'tasks' || mode === 'needs') && !tasksLoading && Date.now() - tasksAt > 60000) loadTasks(false);  // 服务端也有 60 秒缓存
  if (mode === 'board' && Date.now() - boardAt > 20000) loadBoard(false);
  if (mode === 'need' && !needSending[needId] && Date.now() - needAt > 15000) loadNeed(false);
}, 3000);
</script></body></html>
'''

if __name__ == '__main__':
    sys.exit(main())
