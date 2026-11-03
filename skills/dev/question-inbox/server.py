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
import qstore  # noqa: E402
import routerctl  # noqa: E402
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


def router_prompt(kind, recs):
    ids = '、'.join(r['id'] for r in recs)
    args = ' '.join(r['id'] for r in recs)
    if kind == 'reply':
        return (f'[question-inbox] Owner 在需求讨论线程里回复了：{ids}。这条消息本身不是新的授权。\n'
                f'先执行：python3 {q_cmd()} need show {args}；按 skill workspace §3 继续讨论（需要时 need card 发新一版理解卡）。')
    if kind == 'confirm':
        return (f'[question-inbox] Owner 确认了理解卡：{ids}。这条消息本身不是新的授权。\n'
                f'先执行：python3 {q_cmd()} need show {args}；按已确认的那一版理解卡在本 ws 主 repo 建根 Issue'
                f'（正文第一行「需求：<id>」），然后 need link。')
    if kind == 'note':
        return (f'[question-inbox] 需求有 Owner 的新补充：{ids}。这条消息本身不是新的授权。\n'
                f'先执行：python3 {q_cmd()} need show {args}；按 skill router 处理，处理完用 need note 记一句。')
    return (f'[question-inbox] 新需求：{ids}。这条消息本身不是新的授权。\n'
            f'先执行：python3 {q_cmd()} need take {args}；按 skill router 处理。')


def router_presence(info):
    """(pane, 原因, 该不该拉起)。只有「没登记」「登记的 pane 没了 / agent 已退出」才拉起；
    pane 还在但换了会话（有人接手、手动重启）就只暂缓，不碰它。"""
    s = qstore.router_session()
    if not info.get('lead_session') or info.get('herdr_session') != s:
        return None, 'Router 还没登记', True
    pane, why = qstore.find_lead(s, info['lead_session'])
    if pane:
        return pane, '', False
    r = qstore.pane_list(s)
    if not r['ok']:
        return None, why, False
    p = next((p for p in r['panes'] if p.get('pane_id') == info.get('pane_id')), None)
    if p is None:
        return None, f'登记的 Router pane {info.get("pane_id")} 已不存在', True
    if not p.get('agent'):
        return None, f'登记的 Router pane {info.get("pane_id")} 里 agent 已退出', True
    return None, why, False


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
    if qstore.paused():  # 暂停：不投递、不拉起
        return
    due = qstore.needs_due()
    if not due:
        return
    to_lead = [r for r in due if r.get('delivery_to') == 'lead']
    due = [r for r in due if r.get('delivery_to') != 'lead']
    if to_lead:
        scan_lead_needs(to_lead)
    if not due:
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
    """Owner 的回复 / 确认投给该需求的负责 lead：按 claim 时记下的 agent_session 找，找不到只暂缓，不起、不改投。"""
    groups = {}
    for r in sorted(due, key=lambda r: r['id']):
        lead = r.get('lead') or {}
        groups.setdefault((lead.get('herdr_session'), lead.get('lead_session'), r.get('delivery_kind')), []).append(r)
    for (session, lead, kind), recs in groups.items():
        refs = [(r['id'], qstore.geti(r, 'delivery_seq')) for r in recs]
        pane, why = qstore.find_lead(session, lead) if session and lead else (None, '还没有负责 lead')
        if not pane:
            qstore.need_mark_held(refs, f'负责 lead {why}')
            continue
        for i in range(0, len(recs), qstore.NEED_BATCH):
            deliver_router(session, pane, '', kind, recs[i:i + qstore.NEED_BATCH])


def deliver_router(session, pane, lead, kind, recs):
    """投给 Router（lead 非空）或负责 lead（lead 为空），同一套写前记账。"""
    claimed = qstore.need_claim([(r['id'], qstore.geti(r, 'delivery_seq')) for r in recs])
    if not claimed:
        return
    ids = {c[0] for c in claimed}
    r = qstore.herdr(session, 'agent', 'prompt', pane['pane_id'], router_prompt(kind, [x for x in recs if x['id'] in ids]))
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
            'gave_up': bool(st.get('gave_up')), 'last_error': st.get('last_error') or '', 'paused': qstore.paused()}
        HEALTH['at'] = time.monotonic()
        return HEALTH['data']


def need_summary(rec):
    label, why, renot = qstore.need_delivery_view(rec)
    lead = rec.get('lead') or {}
    thread = []
    for x in rec.get('thread') or []:
        item = {'at': x.get('at'), 'by': x.get('by'), 'kind': x.get('kind'), 'rev': x.get('rev') or 0}
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
            'pending_notes': len(qstore.pending_notes(rec)), 'delivery': label, 'delivery_detail': why,
            'renotify': renot, 'log': (rec.get('log') or [])[-8:]}


def needs_view():
    recs = sorted(qstore.all_needs().values(), key=lambda r: r['id'], reverse=True)
    return {'needs': [need_summary(r) for r in recs], 'router': router_health()}


def delivery_loop(stop):
    interval = float(os.environ.get('QI_SCAN_INTERVAL') or 5)
    while not stop.is_set():
        try:
            scan_once()
        except Exception:  # 循环不能死；错误进日志
            log.exception('scan failed')
        try:
            scan_needs()
        except Exception:
            log.exception('need scan failed')
        stop.wait(interval)


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
                return self.send(200, list_view())
            if path == '/api/needs':
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
        other = re.fullmatch(r'/api/(need)|/api/need/(need-\d{4,})/(reply|confirm|renotify)|/api/router/(pause|retry)', self.path)
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
                res = {'ok': True, 'id': nid, 'created': new, 'to': 'lead' if rec.get('lead') else 'router'}
            elif action == 'confirm':
                if not isinstance(req.get('card_rev'), int) or isinstance(req.get('card_rev'), bool):
                    return self.send(400, {'error': 'card_rev 必须是整数'})
                rec, new = qstore.owner_confirm(nid, req['card_rev'], rid)
                res = {'ok': True, 'id': nid, 'created': new, 'card_rev': rec.get('confirmed_rev')}
            elif action == 'renotify':
                qstore.need_renotify(nid)
                res = {'ok': True, 'note': '已重新排队；这条可能已经在 Router 的队列里'}
            elif ctl == 'pause':
                if not isinstance(req.get('paused'), bool):
                    return self.send(400, {'error': 'paused 必须是 true / false'})
                qstore.set_paused(req['paused'])
                res = {'ok': True, 'paused': req['paused']}
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
</style></head><body>
<nav id="modes" aria-label="切换视图"></nav>
<div id="app" data-view="list"><nav id="side"></nav><main id="main"><div class="wrap"><p class="mute">选择左边的问题。</p></div></main></div>
<div id="sheet" role="dialog" aria-modal="true" aria-label="提交前确认"></div>
<div id="help" role="dialog" aria-label="快捷键"></div>
<script nonce="__NONCE__">
'use strict';
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

async function refreshList() {
  const seq = ++listSeq;
  const {status, j} = await api('/api/list');
  if (seq !== listSeq || status !== 200) return;  // 迟到的旧列表不覆盖新的
  openCount = j.open_count;
  renderModes();
  document.title = (j.open_count ? `(${j.open_count}) ` : '') + '问题收件箱';
  const side = document.getElementById('side'), top = side.scrollTop, todo = j.groups.todo;
  const qs = todo.filter(s => s.kind !== 'approval');  // 审批一栏排在问题前面
  const talk = j.needs_talk || [];  // 需求讨论：等你回复或确认理解卡，和审批同级、排最前
  const parts = [
    talk.length ? el('section', {class: 'grp hot'}, el('h2', {}, '需求讨论', el('span', {class: 'n'}, `${talk.length} 条`)),
      talk.map(t => el('a', {class: 'item', href: '#' + NEEDS + ':' + t.id},
        el('div', {class: 'l1'}, [t.ws, t.id].filter(Boolean).join(' · '), el('span', {class: 'tag'}, `理解卡 v${t.card_rev}`)),
        el('div', {class: 'l2'}, el('span', {class: 'tt', title: t.text}, t.text), when(t.updated))))) : null,
    listGroup('审批', todo.filter(s => s.kind === 'approval'), 'hot', 'todo'),
    listGroup('lead 正在等', qs.filter(s => s.blocking === 'yes'), 'hot', 'todo'),
    listGroup('可稍后', qs.filter(s => s.blocking !== 'yes'), '', 'todo'),
    listGroup('已答，lead 未接收', j.groups.waiting, 'dim', 'waiting'),
    listGroup('lead 已接收，未闭环', j.groups.received, 'dim', 'received'),
    listGroup('最近已闭环 / 撤回', j.groups.closed, 'dim', 'closed')].filter(Boolean);
  side.replaceChildren(el('div', {class: 'brand'}, '问题收件箱', el('span', {class: 'count'}, `待答 ${j.open_count}`)),
    ...(parts.length ? parts : [el('div', {class: 'empty'}, '收件箱是空的。')]));
  side.scrollTop = top;
  const back = document.getElementById('back');
  if (back) back.replaceChildren(`← 收件箱（待答 ${openCount}）`);
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
const STAGE_CLS = {answer: 'hot', merge: 'hot', accept: 'hot', stuck: 'bad', doing: 'info', todo: 'mute', hold: 'mute', done: 'ok', dropped: 'mute'};

function renderModes() {
  const box = document.getElementById('modes'); if (!box) return;
  const link = (href, label, on) => el('a', {href, 'aria-current': on ? 'page' : null}, label);
  box.replaceChildren(link('#' + NEEDS, '需求', mode === 'needs'), link('#', `待我决定（${openCount}）`, mode === 'inbox'),
    link('#' + TASKS, '任务', mode === 'tasks'));
}

// 只放行 https://github.com/ 的链接；别的一律当文字
function ghLink(url, text) {
  return typeof url === 'string' && url.startsWith('https://github.com/')
    ? el('a', {href: url, target: '_blank', rel: 'noopener noreferrer'}, text) : el('span', {}, text);
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
  needBusy[key] = true; renderNeeds();
  let status = 0, j = {};
  try { ({status, j} = await api(path, body)); } catch (e) { j = {error: String(e)}; }
  needBusy[key] = false;
  if (status === 200) done(j); else needMsg = {cls: 'bad', text: (j.error || ('错误 ' + status)) + '（输入还在，可以重试）'};
  loadNeeds(); renderNeedsSoon();
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
        j => { needMsg = {cls: 'ok', text: j.paused ? '已全局暂停' : '已恢复'}; })}, h.paused ? '恢复' : '全局暂停'),
      h.gave_up ? el('button', {id: 'retry_btn', disabled: !!needBusy._retry, onclick: () => needPost('_retry', '/api/router/retry', {},
        j => { needMsg = {cls: 'ok', text: j.note}; })}, '重新尝试拉起') : null),
    h.gave_up ? el('div', {class: 'msgbar bad'}, 'Router 起不来：1 小时内自动拉起 3 次仍不在，已停止自动拉起。' + (h.last_error ? '原因：' + h.last_error : '')) : null,
    h.paused ? el('div', {class: 'msgbar warn'}, '全局暂停：不向 Router 投递新需求、不自动拉起 Router，Router 不新起 lead；已经在跑的 lead 不受影响。') : null,
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
      n.lead ? el('span', {}, '负责 lead：' + n.lead) : null,
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
  const next = decodeURIComponent(location.hash.slice(1)) || null;
  if (detail && next === cur) return;  // 取消离开时恢复 hash 触发的那次 hashchange
  if (dirty && detail && detail.editable && !confirm(LEAVE_MSG)) { location.hash = '#' + cur; return; }
  // 题号形如 <ws>-NNNN（不含冒号），不会和 #tasks、#tasks:need-0007、#needs 撞
  const toTasks = next === TASKS || (next || '').startsWith(TASKS + ':'), toNeeds = next === NEEDS || (next || '').startsWith(NEEDS + ':');
  gen++; cur = toTasks || toNeeds ? null : next; detail = null; dirty = false; msg = null;
  curQ = null; editing = false; sheet = null; openState = {}; shownId = null; formShown = false;
  mode = toTasks ? 'tasks' : toNeeds ? 'needs' : 'inbox';
  taskFilter = toTasks ? next.slice(TASKS.length + 1) : '';
  needFocus = toNeeds ? next.slice(NEEDS.length + 1) : ''; needFocused = '';
  renderSheet(); renderModes();
  document.getElementById('app').setAttribute('data-view', toTasks ? 'tasks' : toNeeds ? 'needs' : cur ? 'detail' : 'list');
  if (toTasks) { renderTasks(); loadTasks(false); refreshList(); return; }
  if (toNeeds) { needMsg = null; renderNeeds(); loadNeeds(); if (!tasksData && !tasksLoading) loadTasks(false); refreshList(); return; }
  document.getElementById('main').replaceChildren(el('div', {class: 'wrap'}, el('p', {class: 'mute'}, cur ? '加载中…' : '选择左边的问题。')));
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
  if (mode === 'needs') { loadNeeds(); if (needsStale) renderNeedsSoon(); }
}, 3000);
</script></body></html>
'''

if __name__ == '__main__':
    sys.exit(main())
