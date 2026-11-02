#!/usr/bin/env python3
"""问题收件箱 · 网页 + 投递循环（单进程）。绑定 127.0.0.1:$QI_PORT（默认 7788），端口被占就退出。

启动顺序：进程锁 → 修复（补齐被替换题、sending → uncertain）→ 生成 token → 绑定端口 → 投递循环。
日志只记 id、状态和错误摘要，不记答案正文。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qstore  # noqa: E402

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


def delivery_loop(stop):
    interval = float(os.environ.get('QI_SCAN_INTERVAL') or 5)
    while not stop.is_set():
        try:
            scan_once()
        except Exception:  # 循环不能死；错误进日志
            log.exception('scan failed')
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


def md(text):
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
            out.append('<pre><code>' + html.escape('\n'.join(lines[i + 1:j])) + '</code></pre>')
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

def summary(meta, allq):
    label, why, renot = qstore.delivery_view(meta)
    return {'id': meta['id'], 'title': meta.get('title'), 'ws': meta.get('ws'), 'blocking': meta.get('blocking'),
            'status': meta.get('status'), 'created': meta.get('created'), 'updated': meta.get('updated'),
            'answer_rev': qstore.geti(meta, 'answer_rev'), 'delivery': label, 'delivery_detail': why,
            'renotify': renot, 'invalid': qstore.invalid_reason(meta, allq)}


def list_view():
    allq = qstore.all_questions()
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
    return {'open_count': len(groups['todo']), 'groups': groups}


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
        'not_executed': (f'这条回答不会被执行，原因：{invalid}' if invalid and n > 0 else ''),
        'resolution': meta.get('resolution') if meta.get('status') == 'resolved' else '',
        'body_html': md(lead), 'errata_html': md(sp.get('勘误', '')),
        'timeline': [l[2:] for l in sp.get('时间线', '').splitlines() if l.startswith('- ')],
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
        path = self.path.split('?', 1)[0]
        try:
            if path == '/':
                nonce = secrets.token_urlsafe(12)
                page = PAGE.replace('__TOKEN__', self.token).replace('__NONCE__', nonce)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
                       f"connect-src 'self'; img-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
                return self.send(200, page.encode(), 'text/html; charset=utf-8', {'Content-Security-Policy': csp})
            if path == '/api/list':
                return self.send(200, list_view())
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
        m = re.fullmatch(r'/api/q/([A-Za-z0-9_.-]+)/(save|submit|renotify)', self.path)
        if not m:
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
        qid, action = m.groups()
        try:
            if action == 'save':
                qstore.save_draft(qid, int(req.get('content_rev', -1)), req.get('slots') or {})
                res = {'ok': True}
            elif action == 'submit':
                n = qstore.submit(qid, int(req.get('content_rev', -1)), int(req.get('answer_rev', -1)),
                                  req.get('slots') or {})
                res = {'ok': True, 'answer_rev': n}
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
    for c in qstore.repair():  # 开放写入和投递之前
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="qi-token" content="__TOKEN__">
<title>问题收件箱</title>
<style>
:root{--bg:#fafaf8;--fg:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--card:#fff;--acc:#2f5fd0;--warn:#fff4c2;--bad:#fde3e1;--ok:#e3f4e6}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#e8e8e3;--mute:#9a9a94;--line:#333336;--card:#212124;--acc:#7da2ff;--warn:#4a3f12;--bad:#4a2220;--ok:#1f3d25}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"PingFang SC",system-ui,sans-serif}
#app{display:grid;grid-template-columns:320px 1fr;height:100vh}
#side{border-right:1px solid var(--line);overflow:auto;padding:12px}
#main{overflow:auto;padding:16px 24px 80px}
h2.g{font-size:12px;color:var(--mute);margin:16px 0 6px;font-weight:600}
.item{display:block;padding:8px 10px;border-radius:6px;cursor:pointer;margin-bottom:2px;color:inherit;text-decoration:none}
.item:hover{background:var(--card)}.item.cur{background:var(--card);outline:1px solid var(--line)}
.item small{display:block;color:var(--mute)}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--line);margin-right:4px}
.tag.b{border-color:#d0442f;color:#d0442f}
.bar{padding:8px 12px;border-radius:6px;margin:8px 0}.bar.warn{background:var(--warn)}.bar.bad{background:var(--bad)}.bar.ok{background:var(--ok)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}
.meta{color:var(--mute);font-size:12px}
pre{background:var(--bg);border:1px solid var(--line);padding:8px;overflow:auto;border-radius:4px}
code{background:var(--bg);padding:0 3px;border-radius:3px}
.slot{border-top:1px solid var(--line);padding:10px 0}.slot:first-child{border-top:0}
.slot label{display:block;margin:2px 0}
textarea{width:100%;min-height:48px;font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:6px}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;margin-right:8px}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
.sugg{color:var(--mute);font-size:12px}
@media (max-width:760px){#app{grid-template-columns:1fr;height:auto}#side{border-right:0;border-bottom:1px solid var(--line)}#main{padding:16px}}
</style></head><body>
<div id="app"><nav id="side"></nav><main id="main"><p class="meta">选择左边的问题。</p></main></div>
<script nonce="__NONCE__">
'use strict';
const TOKEN = document.querySelector('meta[name=qi-token]').content;
const TYPES = {free:'自由回答', suggested:'按建议', need_context:'需要更多上下文', defer:'暂不决定'};
let cur = null, detail = null, dirty = false, msg = null;

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
function html(s) { const d = document.createElement('div'); d.innerHTML = s; return d; } // 只用于服务端已 escape 的白名单 HTML

async function api(path, body) {
  const opt = body === undefined ? {} : {method: 'POST', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-QI-Token': TOKEN}};
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  return {status: r.status, j};
}

async function refreshList() {
  const {status, j} = await api('/api/list');
  if (status !== 200) return;
  document.title = (j.open_count ? `(${j.open_count}) ` : '') + '问题收件箱';
  const side = document.getElementById('side'); side.replaceChildren();
  const names = {todo: '待答', waiting: '已答，lead 未接收', received: 'lead 已接收，未闭环', closed: '最近已闭环 / 撤回'};
  for (const [g, title] of Object.entries(names)) {
    side.append(el('h2', {class: 'g'}, `${title}（${j.groups[g].length}）`));
    for (const s of j.groups[g]) {
      const sub = g === 'todo' ? s.created : (s.invalid || s.delivery || s.status);
      side.append(el('a', {class: 'item' + (s.id === cur ? ' cur' : ''), href: '#' + s.id},
        s.blocking === 'yes' && g === 'todo' ? el('span', {class: 'tag b'}, 'blocking') : null,
        s.id, ' · ', s.title, el('small', {}, sub || '')));
    }
  }
}

function slotsFromForm() {
  const slots = {};
  for (const q of detail.questions) {
    if (q.withdrawn) continue;
    const r = document.querySelector(`input[name="t_${q.q}"]:checked`);
    const text = document.getElementById('x_' + q.q).value;
    if (!r) { if (text.trim()) slots[q.q] = {type: 'free', text}; continue; }
    const v = r.value;
    if (v.startsWith('opt:')) slots[q.q] = {type: 'option', choice: q.options[+v.slice(4)], text};
    else slots[q.q] = {type: v, text};
  }
  return slots;
}

function fillForm(slots) {
  for (const q of detail.questions) {
    const s = slots[q.q]; if (!s || q.withdrawn) continue;
    let v = s.type;
    if (v === 'option') { const i = q.options.indexOf(s.choice); if (i < 0) continue; v = 'opt:' + i; }
    const r = document.querySelector(`input[name="t_${q.q}"][value="${v}"]`); if (r) r.checked = true;
    document.getElementById('x_' + q.q).value = s.text || '';
  }
}

async function post(action, body) {
  const {status, j} = await api(`/api/q/${encodeURIComponent(cur)}/${action}`, body);
  if (status === 200) { msg = {cls: 'ok', text: j.note || ({save: '已保存草稿（不投递）', submit: `已提交 Rev ${j.answer_rev}，将发回 lead`}[action] || '完成')}; dirty = false; await load(true); }
  else { msg = {cls: 'bad', text: (status === 409 ? '版本冲突：' : '') + (j.error || ('错误 ' + status))}; renderMsg(); }
  refreshList();
}

function renderMsg() {
  const box = document.getElementById('msg'); if (!box) return;
  box.replaceChildren(msg ? el('div', {class: 'bar ' + msg.cls}, msg.text) : '');
}

function render() {
  const d = detail, main = document.getElementById('main'); main.replaceChildren();
  main.append(el('h1', {}, `${d.id} · ${d.title}`),
    el('div', {class: 'meta'}, `issue ${d.issue} · ${d.blocking === 'yes' ? 'blocking · ' : ''}${d.status} · content_rev ${d.content_rev} · answer_rev ${d.answer_rev} · 创建于 ${d.created}`),
    el('div', {id: 'msg'}));
  if (d.invalid) main.append(el('div', {class: 'bar bad'}, `已失效：${d.invalid}`));
  if (d.not_executed) main.append(el('div', {class: 'bar bad'}, d.not_executed));
  for (const w of d.warnings) main.append(el('div', {class: 'bar warn'}, '提示：' + w));
  if (d.status === 'answered' || d.status === 'resolved') {
    const st = d.received ? 'lead 已接收' : (d.delivery || '');
    main.append(el('div', {class: 'bar ' + (d.received ? 'ok' : 'warn'), id: 'dstate'}, `投递：${st}${d.delivery_detail && !d.received ? '（' + d.delivery_detail + '）' : ''}`,
      d.renotify ? el('button', {style: 'margin-left:12px', onclick: () => post('renotify', {})}, '重新通知') : null,
      d.renotify ? el('div', {class: 'sugg'}, '重新通知会重新核对 lead 身份后再发；这条可能已经在 lead 的队列里。') : null));
  }
  if (d.resolution) main.append(el('div', {class: 'bar ok'}, '已闭环：' + d.resolution));
  main.append(el('div', {class: 'card'}, html(d.body_html)));
  if (d.errata_html) main.append(el('div', {class: 'card'}, el('h3', {}, '勘误'), html(d.errata_html)));
  if (d.editable) {
    const form = el('div', {class: 'card'}, el('h3', {}, d.answer_rev ? `更正回答（当前 Rev ${d.answer_rev}，提交后生成 Rev ${d.answer_rev + 1}）` : '回答'));
    for (const q of d.questions) {
      const box = el('div', {class: 'slot'}, el('strong', {}, `${q.q}. ${q.text}`));
      if (q.withdrawn) { box.append(el('div', {class: 'sugg'}, 'lead 已撤回这个子问题（算已填）')); form.append(box); continue; }
      q.options.forEach((o, i) => box.append(el('label', {}, el('input', {type: 'radio', name: 't_' + q.q, value: 'opt:' + i, onchange: () => dirty = true}), ' ', o)));
      for (const [t, name] of Object.entries(TYPES)) {
        if (t === 'suggested' && !q.suggestion) continue;
        box.append(el('label', {}, el('input', {type: 'radio', name: 't_' + q.q, value: t, onchange: () => dirty = true}), ' ', name, t === 'suggested' ? `：${q.suggestion}` : ''));
      }
      box.append(el('textarea', {id: 'x_' + q.q, placeholder: '补充 / 原话（自由回答必填）', oninput: () => dirty = true}));
      form.append(box);
    }
    form.append(el('div', {},
      el('button', {onclick: () => post('save', {content_rev: d.content_rev, slots: slotsFromForm()})}, '保存草稿'),
      el('button', {onclick: () => { for (const q of d.questions) { if (q.withdrawn || !q.suggestion) continue; if (!document.querySelector(`input[name="t_${q.q}"]:checked`)) { document.querySelector(`input[name="t_${q.q}"][value="suggested"]`).checked = true; dirty = true; } } }}, '其余按建议'),
      el('button', {class: 'pri', onclick: () => post('submit', {content_rev: d.content_rev, answer_rev: d.answer_rev, slots: slotsFromForm()})}, d.answer_rev ? '提交更正并发回 lead' : '提交并发回 lead'),
      el('div', {class: 'sugg'}, '「其余按建议」只填进表单，你可以再改；只有点提交才算回答。')));
    main.append(form);
  }
  for (const r of [...d.revs].reverse()) {
    const c = el('div', {class: 'card'}, el('h3', {}, `Rev ${r.rev}${r.corrected_by ? `（已被 Rev ${r.corrected_by} 更正）` : ''}`), el('div', {class: 'meta'}, r.at));
    for (const [q, s] of Object.entries(r.slots)) c.append(el('div', {}, `${q}：[${s.type === 'option' ? '选项' : (TYPES[s.type] || s.type)}] ${s.choice || ''}`, s.text ? el('div', {class: 'sugg'}, '原话：' + s.text) : null));
    main.append(c);
  }
  main.append(el('details', {class: 'card'}, el('summary', {}, '时间线'), ...d.timeline.map(t => el('div', {class: 'meta'}, t))));
  if (d.editable) {
    const latest = d.revs.find(r => r.rev === d.answer_rev);
    fillForm(Object.keys(d.draft.slots).length ? d.draft.slots : (latest ? latest.slots : {}));
    if (d.draft.note) main.querySelector('.card h3').after(el('div', {class: 'sugg'}, '草稿：' + d.draft.note));
  }
  renderMsg();
}

async function load(force) {
  if (!cur) return;
  const {status, j} = await api('/api/q/' + encodeURIComponent(cur));
  if (status !== 200) { document.getElementById('main').replaceChildren(el('div', {class: 'bar bad'}, j.error || '加载失败')); detail = null; return; }
  const changed = !detail || detail.id !== j.id || detail.content_rev !== j.content_rev || detail.answer_rev !== j.answer_rev || detail.status !== j.status;
  if (!force && !dirty && JSON.stringify(detail) === JSON.stringify(j)) return;
  if (force || !dirty) { const keep = force ? msg : null; detail = j; render(); msg = keep; renderMsg(); return; }
  if (changed) { msg = {cls: 'warn', text: '问题或回答已在别处更新：你填的内容还在表单里，提交会提示冲突并自动保留为草稿，然后刷新页面。'}; renderMsg(); }
}

function route() { cur = decodeURIComponent(location.hash.slice(1)) || null; detail = null; dirty = false; msg = null; load(true); refreshList(); }
window.addEventListener('hashchange', route);
route();
setInterval(() => { refreshList(); load(false); }, 3000);
</script></body></html>
'''

if __name__ == '__main__':
    sys.exit(main())
