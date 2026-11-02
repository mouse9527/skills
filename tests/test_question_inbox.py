"""skills/dev/question-inbox: storage, CLI, web server and delivery loop, against a fake herdr in temp dirs.

Covers the acceptance scenarios 1, 2, 4, 5, 6, 7, 8 of the spec (3 and 9 need a real herdr / launchd).
Fictional data only: ws `demo`, herdr session `demo`, lead sessions `lead-a` / `lead-b`.
"""
import fcntl
import http.client
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
QI = ROOT / 'skills' / 'dev' / 'question-inbox'
FAKE = Path(__file__).resolve().parent / 'question_inbox_fake_herdr.py'


def _load(name):
    spec = importlib.util.spec_from_file_location(name, QI / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qstore = _load('qstore')
server = _load('server')


def body(n=2, extra_bg='', evidence='`demo/app.py:12` 返回 500，复现命令 `make check`'):
    qs = '\n'.join(f'Q{i}. 第 {i} 个问题选哪个？' for i in range(1, n + 1))
    opts = '\n\n'.join(f'### Q{i}\n- A. 方案甲（后果：快）\n- B. 方案乙（后果：稳）' for i in range(1, n + 1))
    sugg = '\n'.join(f'Q{i}: B —— 理由 {i}' for i in range(1, n + 1))
    return (f'## 问题\n\n{qs}\n\n## 背景\n\n在做演示功能，卡在接口选择。{extra_bg}\n\n## 证据\n\n{evidence}\n\n'
            f'## 选项\n\n{opts}\n\n## 建议\n\n{sugg}\n\n## 答完之后\n\n按回答改代码。\n')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def pane(pane_id='w1:p1', lead='lead-a', agent='claude', status='idle'):
    p = {'agent': agent, 'agent_status': status, 'pane_id': pane_id, 'cwd': '/tmp'}
    if lead:
        p['agent_session'] = {'agent': agent, 'kind': 'id', 'value': lead}
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='qi-test-'))
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        fake = bindir / 'herdr'
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        fake.chmod(0o755)
        (self.tmp / 'sock' / 'demo').mkdir(parents=True)
        (self.tmp / 'sock' / 'demo' / 'herdr.sock').write_text('')
        (self.tmp / 'ws' / 'demo' / 'sub').mkdir(parents=True)
        self.port = free_port()
        self.env = {
            'QUESTION_INBOX_DIR': str(self.tmp / 'inbox'), 'QI_STATE_DIR': str(self.tmp / 'state'),
            'QI_HERDR_SOCKET_ROOT': str(self.tmp / 'sock'), 'QI_WORKSPACES_ROOT': str(self.tmp / 'ws'),
            'HERDR_BIN_PATH': str(fake), 'FAKE_HERDR_STATE': str(self.tmp / 'herdr.json'),
            'FAKE_HERDR_LOG': str(self.tmp / 'herdr.log'), 'HERDR_SESSION': 'demo', 'HERDR_PANE_ID': 'w1:p1',
            'QI_PORT': str(self.port), 'QI_SCAN_INTERVAL': '0.2', 'QI_HERDR_TIMEOUT': '3', 'QI_BACKOFF': '0.3,0.3',
        }
        (self.tmp / 'herdr.log').write_text('')
        self.set_herdr(panes={'demo': [pane()]})
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for s in (p.stdout, p.stderr):
                if s:
                    s.close()
        qstore._after_new_hook = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- fake herdr -----
    def set_herdr(self, **state):
        (self.tmp / 'herdr.json').write_text(json.dumps({'prompt': 'ok', **state}))

    def herdr_calls(self, sub='prompt'):
        lines = (self.tmp / 'herdr.log').read_text().splitlines()
        return [json.loads(l) for l in lines if sub in json.loads(l)]

    # ----- CLI -----
    def q(self, *args, env=None, cwd=None):
        return subprocess.run([sys.executable, str(QI / 'q.py'), *args], capture_output=True, text=True, encoding='utf-8',
                              env={**os.environ, **(env or {})}, cwd=cwd or str(self.tmp / 'ws' / 'demo' / 'sub'),
                              timeout=60)

    def ask(self, text=None, title='演示问题', blocking='yes', extra=(), env=None):
        f = self.tmp / f'body-{time.monotonic_ns()}.md'
        f.write_text(text if text is not None else body())
        r = self.q('ask', '--body', str(f), '--title', title, '--issue', 'none', '--blocking', blocking, *extra,
                   env=env)
        if r.returncode == 0:
            return re.search(r'已发布 (\S+?)：', r.stdout).group(1)
        return r

    def meta(self, qid):
        return qstore.read(qid)[0]

    # ----- server -----
    def start_server(self):
        p = subprocess.Popen([sys.executable, str(QI / 'server.py')], env=dict(os.environ),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(p)
        for _ in range(200):
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.2):
                    break
            except OSError:
                if p.poll() is not None:
                    self.fail(f'server exited: {p.stderr.read().decode()}')
                time.sleep(0.05)
        self.token = (self.tmp / 'state' / 'token').read_text().strip()
        return p

    def http(self, method, path, payload=None, headers=None, raw=None):
        h = {'Host': f'127.0.0.1:{self.port}'}
        if method == 'POST':
            h.update({'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json',
                      'X-QI-Token': self.token})
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        try:
            return r.status, json.loads(text), r
        except ValueError:
            return r.status, text, r

    def wait_for(self, cond, timeout=10, msg='condition'):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail(f'timed out waiting for {msg}')

    def answer_all(self, qid, n=2, choice='B. 方案乙（后果：稳）'):
        return {f'Q{i}': {'type': 'option', 'choice': choice} for i in range(1, n + 1)}


class AskTest(Base):
    def test_publish_and_validation(self):
        qid = self.ask()
        self.assertEqual(qid, 'demo-0001')
        m = self.meta(qid)
        self.assertEqual((m['status'], m['lead_session'], m['herdr_session'], m['content_rev'], m['answer_rev']),
                         ('open', 'lead-a', 'demo', '1', '0'))
        bad = {
            'missing section': body().replace('## 证据', '## 别的'),
            'gap in Q numbers': body(3).replace('Q2. 第 2 个问题选哪个？\n', ''),
            'forbidden phrase': body(extra_bg='细节见上文。'),
            'missing option': body(2).replace('### Q2', '### Q9'),
            'missing suggestion': body(2).replace('Q2: B', 'Qx: B'),
            'system section': body() + '\n## 回答\n\n伪造\n',
        }
        for why, text in bad.items():
            r = self.ask(text)
            self.assertIsInstance(r, subprocess.CompletedProcess, why)
            self.assertEqual(r.returncode, 1, why)
        self.assertEqual(sorted(p.name for p in (self.tmp / 'inbox').glob('*.md')), ['demo-0001.md'])

    def test_soft_warnings(self):
        qid = self.ask(body(evidence='只是一些描述').replace('## 答完之后\n\n按回答改代码。\n', ''), blocking='no')
        self.assertIn('缺少', self.meta(qid)['warnings'])
        self.assertIn('证据', self.meta(qid)['warnings'])

    def test_identity_required(self):
        self.set_herdr(panes={'demo': [pane(agent='codex')]})
        r = self.ask()
        self.assertEqual(r.returncode, 4)
        r = self.ask(env={'HERDR_PANE_ID': ''})
        self.assertEqual(r.returncode, 4)
        self.assertFalse(list((self.tmp / 'inbox').glob('*.md')) if (self.tmp / 'inbox').exists() else [])

    def test_ws_required_outside_workspaces(self):
        r = self.q('ask', '--body', '/dev/null', '--title', 't', '--issue', 'none', '--blocking', 'no',
                   cwd=str(self.tmp))
        self.assertEqual(r.returncode, 1)

    def test_scenario1_concurrent_ask_ids_unique(self):
        with ThreadPoolExecutor(8) as ex:
            ids = list(ex.map(lambda i: self.ask(title=f't{i}'), range(8)))
        self.assertTrue(all(isinstance(i, str) for i in ids), ids)
        self.assertEqual(sorted(ids), [f'demo-{n:04d}' for n in range(1, 9)])
        self.assertEqual(json.loads((self.tmp / 'inbox' / '_seq').read_text()), {'demo': 8})


class WebTest(Base):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_scenario1_submit_vs_erratum_returns_409_and_keeps_input(self):
        qid = self.ask()
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertEqual(self.q('erratum', qid, '--note', '证据里的行号更正为 13').returncode, 0)
        slots = {'Q1': {'type': 'free', 'text': 'Owner 的原话：先别动'}, 'Q2': {'type': 'defer'}}
        code, res, _ = self.http('POST', f'/api/q/{qid}/submit',
                                 {'content_rev': d['content_rev'], 'answer_rev': d['answer_rev'], 'slots': slots})
        self.assertEqual(code, 409, res)
        m = self.meta(qid)
        self.assertEqual((m['status'], m['answer_rev'], m['content_rev']), ('open', '0', '2'))
        _, d2, _ = self.http('GET', f'/api/q/{qid}')
        self.assertEqual(d2['draft']['slots']['Q1']['text'], 'Owner 的原话：先别动')
        code, _, _ = self.http('POST', f'/api/q/{qid}/save', {'content_rev': 1, 'slots': slots})
        self.assertEqual(code, 409)
        code, res, _ = self.http('POST', f'/api/q/{qid}/submit', {'content_rev': 2, 'answer_rev': 0, 'slots': slots})
        self.assertEqual((code, res.get('answer_rev')), (200, 1))

    def test_scenario2_seven_questions_partial_save_suggest_modify_submit(self):
        qid = self.ask(body(7))
        raw = '先这样<b>不要</b>\n第二行 `x` & "引号"'
        partial = {'Q1': {'type': 'option', 'choice': 'A. 方案甲（后果：快）'}, 'Q2': {'type': 'free', 'text': raw}}
        code, _, _ = self.http('POST', f'/api/q/{qid}/save', {'content_rev': 1, 'slots': partial})
        self.assertEqual(code, 200)
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertEqual(d['draft']['slots'], partial)
        self.assertEqual(self.meta(qid)['answer_rev'], '0')  # draft never counts as an answer
        self.assertEqual(self.meta(qid)['delivery'], 'none')
        # 「其余按建议」 fills the empty slots in the form only; Owner then changes Q5 and submits
        slots = dict(d['draft']['slots'])
        for q in d['questions']:
            slots.setdefault(q['q'], {'type': 'suggested'})
        slots['Q5'] = {'type': 'option', 'choice': 'A. 方案甲（后果：快）', 'text': '这个我改主意了'}
        incomplete = {k: v for k, v in slots.items() if k != 'Q7'}
        code, _, _ = self.http('POST', f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0, 'slots': incomplete})
        self.assertEqual(code, 400)
        code, res, _ = self.http('POST', f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0, 'slots': slots})
        self.assertEqual((code, res.get('answer_rev')), (200, 1))
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        rev1 = d['revs'][0]
        self.assertEqual(rev1['rev'], 1)
        self.assertEqual(rev1['slots']['Q2'], {'type': 'free', 'text': raw})
        self.assertEqual(rev1['slots']['Q1'], partial['Q1'])
        self.assertEqual(rev1['slots']['Q5']['text'], '这个我改主意了')
        self.assertEqual(rev1['slots']['Q3'], {'type': 'suggested', 'choice': 'B —— 理由 3'})
        self.assertEqual(len(rev1['slots']), 7)
        self.assertEqual(d['draft']['slots'], {})
        m = self.meta(qid)
        self.assertEqual((m['status'], m['delivery'], m['delivery_rev']), ('answered', 'pending', '1'))

    def test_scenario8_csrf_and_escaping(self):
        qid = self.ask(body(extra_bg='<script>alert(1)</script>'), title='<script>x</script>')
        good = {'content_rev': 1, 'answer_rev': 0, 'slots': self.answer_all(qid)}
        url = f'/api/q/{qid}/submit'
        evil = [
            {'Origin': 'http://evil.example'},
            {'Origin': None},
            {'Content-Type': 'text/plain'},
            {'Content-Type': 'application/x-www-form-urlencoded'},
            {'X-QI-Token': None},
            {'X-QI-Token': 'wrong'},
            {'Host': f'evil.example:{self.port}', 'Origin': f'http://evil.example:{self.port}'},
            {'Host': 'evil.example'},
        ]
        for h in evil:
            code, _, _ = self.http('POST', url, good, headers=h)
            self.assertEqual(code, 403, h)
        code, _, _ = self.http('POST', url, raw=b'content_rev=1', headers={'Content-Type': 'text/plain'})
        self.assertEqual(code, 403)
        code, _, _ = self.http('GET', '/api/list', headers={'Host': f'attacker.example:{self.port}'})
        self.assertEqual(code, 403)
        m = self.meta(qid)
        self.assertEqual((m['answer_rev'], m['status']), ('0', 'open'))
        self.assertNotIn('草稿回答', qstore.split_body(qstore.read(qid)[1])[1])
        # same origin via localhost works; the page carries the token, never the question text
        code, _, r = self.http('POST', url, good, headers={'Host': f'localhost:{self.port}',
                                                          'Origin': f'http://localhost:{self.port}'})
        self.assertEqual(code, 200)
        self.assertEqual(r.getheader('Cache-Control'), 'no-store')
        self.assertIsNone(r.getheader('Access-Control-Allow-Origin'))
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', d['body_html'])
        self.assertNotIn('<script', d['body_html'])
        code, page, r = self.http('GET', '/')
        self.assertEqual(code, 200)
        self.assertNotIn('alert(1)', page)
        self.assertEqual(page.count('<script'), 1)  # only the page's own (nonce) script
        self.assertIn("script-src 'nonce-", r.getheader('Content-Security-Policy'))
        self.assertEqual(self.http('GET', '/etc/passwd')[0], 404)
        self.assertEqual(self.http('GET', '/../inbox/demo-0001.md')[0], 404)

    def test_server_refuses_second_instance(self):
        p = subprocess.run([sys.executable, str(QI / 'server.py')], env=dict(os.environ), capture_output=True,
                           text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)


class DeliveryTest(Base):
    def answered(self, n=2, **kw):
        qid = self.ask(body(n), **kw)
        qstore.submit(qid, 1, 0, self.answer_all(qid, n))
        return qid

    def test_delivery_ok_and_batching(self):
        ids = [self.answered() for _ in range(6)]
        server.scan_once()
        prompts = self.herdr_calls()
        self.assertEqual(len(prompts), 2)  # 5 + 1 for the same lead
        self.assertIn('Owner 已回答：demo-0001、demo-0002、demo-0003、demo-0004、demo-0005', prompts[0][-1])
        self.assertIn('不是新的授权', prompts[0][-1])
        self.assertEqual(prompts[0][:5], ['--session', 'demo', 'agent', 'prompt', 'w1:p1'])
        for qid in ids:
            m = self.meta(qid)
            self.assertEqual(m['delivery'], 'sent')
            self.assertTrue(m['herdr_accepted_at'])
        server.scan_once()
        self.assertEqual(len(self.herdr_calls()), 2)  # sent is never re-sent automatically

    def test_blocked_holds_then_resumes(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='blocked')
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual(m['delivery'], 'held')
        self.assertIn('agent_blocked', m['last_error'])
        self.set_herdr(panes={'demo': [pane()]}, prompt='ok')
        time.sleep(0.4)
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'sent')

    def test_timeout_and_garbage_are_uncertain_and_not_resent(self):
        for mode in ('timeout', 'garbage'):
            qid = self.answered()
            self.set_herdr(panes={'demo': [pane()]}, prompt=mode, sleep=5)
            before = len(self.herdr_calls())
            server.scan_once()
            self.assertEqual(self.meta(qid)['delivery'], 'uncertain', mode)
            server.scan_once()
            self.assertEqual(len(self.herdr_calls()), before + 1, mode)
            self.assertTrue(qstore.delivery_view(self.meta(qid))[2])  # 「重新通知」 offered

    def test_missing_socket_runs_no_herdr(self):
        qid = self.answered()
        (self.tmp / 'sock' / 'demo' / 'herdr.sock').unlink()
        (self.tmp / 'herdr.log').write_text('')
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'held')
        self.assertEqual((self.tmp / 'herdr.log').read_text(), '')

    def test_target_gone_or_duplicated_holds(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane('w1:p1', 'lead-a'), pane('w1:p2', 'lead-a')]})
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'held')
        self.set_herdr(panes={'demo': []})
        time.sleep(0.4)
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'held')
        self.assertEqual(self.herdr_calls(), [])

    def test_scenario4_pane_switched_to_other_session(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane('w1:p1', 'lead-b')]})
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual(m['delivery'], 'held')
        self.assertEqual(self.herdr_calls(), [])
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 4)
        self.assertNotIn('Owner 的回答', r.stdout)
        self.assertNotIn('方案乙', r.stdout)
        self.assertNotEqual(self.meta(qid)['delivery'], 'received')

    def test_scenario5_crash_between_new_and_old(self):
        a = self.answered()
        ident = qstore.identify('demo', 'w1:p1')

        def crash():
            raise RuntimeError('simulated crash')
        qstore._after_new_hook = crash
        with self.assertRaises(RuntimeError):
            qstore.publish({'ws': 'demo', 'title': '新版', 'issue': 'none', 'blocking': 'yes'}, body(), ident,
                           supersedes=a)
        qstore._after_new_hook = None
        self.assertEqual(self.meta(a)['status'], 'answered')  # A's header was never updated
        self.assertEqual(self.meta('demo-0002')['supersedes'], a)
        with self.assertRaises(qstore.QError) as e:
            qstore.submit(a, 1, 1, self.answer_all(a))
        self.assertEqual(e.exception.code, 410)
        server.scan_once()
        self.assertEqual(self.herdr_calls(), [])
        r = self.q('receive', a)
        self.assertEqual(r.returncode, 3)
        self.assertNotIn('方案乙', r.stdout)
        self.assertEqual(self.q('resolve', a, '--answer-rev', '1', '--note', 'x').returncode, 3)
        # restart: the server repairs A before serving, and the old page still cannot submit
        self.start_server()
        m = self.meta(a)
        self.assertEqual((m['status'], m['superseded_by'], m['delivery']), ('withdrawn', 'demo-0002', 'cancelled'))
        code, _, _ = self.http('POST', f'/api/q/{a}/submit',
                               {'content_rev': 1, 'answer_rev': 1, 'slots': self.answer_all(a)})
        self.assertEqual(code, 410)
        _, d, _ = self.http('GET', f'/api/q/{a}')
        self.assertIn('demo-0002', d['invalid'])
        self.assertIn('不会被执行', d['not_executed'])
        self.assertFalse(d['editable'])

    def test_supersede_rules(self):
        a = self.ask()
        self.assertIsInstance(self.ask(extra=('--supersedes', 'demo-0099')), subprocess.CompletedProcess)
        b = self.ask(extra=('--supersedes', a))
        self.assertEqual(self.meta(a)['status'], 'withdrawn')
        r = self.ask(extra=('--supersedes', a))  # A is already replaced
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.q('doctor').returncode, 0, self.q('doctor').stdout)
        self.assertEqual(self.meta(b)['supersedes'], a)

    def test_scenario6_correction(self):
        qid = self.answered()
        server.scan_once()
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Rev 1', r.stdout)
        self.assertTrue(server.detail_view(qid)['received'])
        self.assertEqual(self.q('receive', qid).stdout, r.stdout)  # repeat → same version
        rev2 = {'Q1': {'type': 'free', 'text': '改成先做甲'}, 'Q2': {'type': 'defer'}}
        self.assertEqual(qstore.submit(qid, 1, 1, rev2), 2)
        d = server.detail_view(qid)
        self.assertFalse(d['received'])
        self.assertEqual(d['revs'][0]['corrected_by'], 2)
        server.scan_once()
        self.assertIn('更正', self.herdr_calls()[-1][-1])
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '1', '--note', 'x').returncode, 3)
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '2', '--note', 'x').returncode, 3)  # not received
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 0)
        self.assertIn('改成先做甲', r.stdout)
        self.assertIn('以 Rev 2 为准', r.stdout)
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '2', '--note', '记到 Issue 评论').returncode, 0)
        m = self.meta(qid)
        self.assertEqual((m['status'], m['resolved_rev']), ('resolved', '2'))
        with self.assertRaises(qstore.QError):
            qstore.submit(qid, 1, 2, rev2)  # no correction after resolved
        self.assertEqual(self.q('doctor').returncode, 0)

    def test_withdraw_after_delivery_sends_notice(self):
        qid = self.answered()
        server.scan_once()
        self.assertEqual(self.q('withdraw', qid, '--reason', '需求取消').returncode, 0)
        server.scan_once()
        self.assertIn('已撤回', self.herdr_calls()[-1][-1])
        m = self.meta(qid)
        self.assertEqual((m['status'], m['delivery'], m['withdraw_notice']), ('withdrawn', 'cancelled', 'sent'))
        self.assertIn('不会被执行', server.detail_view(qid)['not_executed'])
        self.assertEqual(self.q('receive', qid).returncode, 3)

    def test_status_lists_own_questions(self):
        a = self.answered()
        self.set_herdr(panes={'demo': [pane(), pane('w1:p2', 'lead-b')]})
        b = self.ask(env={'HERDR_PANE_ID': 'w1:p2'})
        out = self.q('status').stdout
        self.assertIn(a, out)
        self.assertNotIn(b, out)
        self.assertIn(b, self.q('status', '--all').stdout)

    def test_scenario7_kill_while_sending(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='timeout', sleep=6)
        os.environ['QI_HERDR_TIMEOUT'] = '30'
        p = self.start_server()
        self.wait_for(lambda: self.meta(qid)['delivery'] == 'sending', msg='sending')
        self.wait_for(lambda: len(self.herdr_calls()) == 1, msg='prompt call in flight')
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        self.set_herdr(panes={'demo': [pane()]}, prompt='ok')
        calls = len(self.herdr_calls())
        self.start_server()
        self.assertEqual(self.meta(qid)['delivery'], 'uncertain')
        time.sleep(1)
        self.assertEqual(len(self.herdr_calls()), calls)  # no automatic resend
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertTrue(d['renotify'])
        code, _, _ = self.http('POST', f'/api/q/{qid}/renotify', {})
        self.assertEqual(code, 200)
        self.wait_for(lambda: self.meta(qid)['delivery'] == 'sent', msg='sent after renotify')
        self.assertEqual(len(self.herdr_calls()), calls + 1)


class OwnerCloseTest(Base):
    """I：lead 会话丢了也有出口——Owner 在页面上「由我关闭」，不放宽 lead 侧身份校验。"""

    def setUp(self):
        super().setUp()
        os.environ['QI_SCAN_INTERVAL'] = '3600'  # 投递由测试手动 scan_once()，结果确定
        self.start_server()

    def close(self, qid, reason='lead 会话已经没了', headers=None, **rev):
        m = self.meta(qid)
        payload = {'content_rev': int(m['content_rev']), 'answer_rev': int(m['answer_rev']), 'reason': reason}
        payload.update(rev)
        return self.http('POST', f'/api/q/{qid}/close', payload, headers=headers)

    def received_then_lead_gone(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        server.scan_once()
        self.assertEqual(self.q('receive', qid).returncode, 0)
        self.set_herdr(panes={'demo': []})  # 原 lead 的 pane 关了
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '1', '--note', 'x').returncode, 4)  # D 不放宽
        return qid

    def test_close_answered_after_lead_gone_and_notice_path(self):
        qid = self.received_then_lead_gone()
        code, res, _ = self.close(qid)
        self.assertEqual((code, res.get('status')), (200, 'resolved'))
        m = self.meta(qid)
        self.assertEqual((m['status'], m['resolved_by'], m['resolution']), ('resolved', 'owner', 'Owner 关闭：lead 会话已经没了'))
        self.assertEqual((m['delivery'], m['withdraw_notice']), ('received', 'pending'))
        self.assertIn('Owner 关闭', '\n'.join(server.detail_view(qid)['timeline']))
        # 通知送不到（pane 没了）也不影响关闭本身，页面可见、可重新通知
        calls = len(self.herdr_calls())
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual((m['status'], m['withdraw_notice']), ('resolved', 'held'))
        self.assertEqual(len(self.herdr_calls()), calls)
        _, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertEqual(d['closed_by'], 'owner')
        self.assertTrue(d['renotify'])
        self.assertIn('关闭通知', d['delivery'])
        self.assertFalse(d['editable'] or d['closable'])
        self.set_herdr(panes={'demo': [pane()]})  # lead 会话又找到了：通知走撤回通知同一路径送达
        self.assertEqual(self.http('POST', f'/api/q/{qid}/renotify', {})[0], 200)
        server.scan_once()
        self.assertEqual(self.meta(qid)['withdraw_notice'], 'sent')
        self.assertIn('Owner 已在页面上关闭', self.herdr_calls()[-1][-1])
        _, lst, _ = self.http('GET', '/api/list')
        self.assertEqual([s['closed_by'] for s in lst['groups']['closed'] if s['id'] == qid], ['owner'])
        self.assertEqual(self.q('doctor').returncode, 0, self.q('doctor').stdout)

    def test_close_open_is_withdraw_without_delivery(self):
        qid = self.ask()
        self.set_herdr(panes={'demo': []})
        code, res, _ = self.close(qid, reason='不需要了')
        self.assertEqual((code, res.get('status')), (200, 'withdrawn'))
        m = self.meta(qid)
        self.assertEqual((m['status'], m['withdrawn_by'], m['withdraw_reason']), ('withdrawn', 'owner', 'Owner 关闭：不需要了'))
        self.assertEqual(m['withdraw_notice'], '')
        server.scan_once()
        self.assertEqual(self.herdr_calls(), [])
        code, _, _ = self.http('POST', f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0, 'slots': self.answer_all(qid)})
        self.assertEqual(code, 409)  # 之后不能再回答、不会投递

    def test_close_answered_not_yet_delivered_cancels_delivery(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        code, _, _ = self.close(qid)
        self.assertEqual(code, 200)
        m = self.meta(qid)
        self.assertEqual((m['status'], m['delivery'], m['withdraw_notice']), ('resolved', 'cancelled', ''))
        server.scan_once()
        self.assertEqual(self.herdr_calls(), [])

    def test_in_flight_delivery_does_not_overwrite_close(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        claimed = qstore.claim([(qid, 'answer', 1)])
        self.assertEqual(self.close(qid)[0], 200)
        qstore.finish(claimed, 'sent')
        m = self.meta(qid)
        self.assertEqual((m['status'], m['delivery'], m['withdraw_notice']), ('resolved', 'cancelled', 'pending'))

    def test_lead_commands_after_close(self):
        a = self.received_then_lead_gone()
        self.set_herdr(panes={'demo': [pane()]})
        b = self.ask()
        self.assertEqual(self.close(a)[0], 200)
        self.assertEqual(self.close(b)[0], 200)
        gone = {'HERDR_SESSION': '', 'HERDR_PANE_ID': ''}
        for env in (None, gone):  # 原 lead 还在，或会话已丢：都得到明确提示、退出码 3
            for qid in (a, b):
                for args in (('receive', qid), ('resolve', qid, '--answer-rev', '1', '--note', 'x'),
                             ('withdraw', qid, '--reason', 'x'), ('erratum', qid, '--note', 'x')):
                    before = (self.tmp / 'inbox' / f'{qid}.md').read_bytes()
                    r = self.q(*args, env=env)
                    self.assertEqual(r.returncode, 3, (args, env, r.stderr))
                    self.assertIn('Owner 在页面上关闭', r.stderr)
                    self.assertNotIn('方案乙', r.stdout)
                    self.assertEqual((self.tmp / 'inbox' / f'{qid}.md').read_bytes(), before)
        out = self.q('status', '--all').stdout
        self.assertIn('Owner 已关闭', out)
        self.assertIn(a, out)
        self.assertIn(b, out)
        self.assertIn('Owner 在页面上关闭', self.q('show', a).stdout)
        self.assertEqual(self.q('doctor').returncode, 0)

    def test_close_requires_same_security_and_versions(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        for h in ({'Origin': 'http://evil.example'}, {'Origin': None}, {'Content-Type': 'text/plain'},
                  {'X-QI-Token': None}, {'X-QI-Token': 'wrong'}, {'Host': 'evil.example'}):
            self.assertEqual(self.close(qid, headers=h)[0], 403, h)
        self.assertEqual(self.close(qid, reason='  ')[0], 400)
        self.assertEqual(self.close(qid, answer_rev=0)[0], 409)  # 页面读到的是旧版本
        self.assertEqual(self.close(qid, content_rev=9)[0], 409)
        m = self.meta(qid)
        self.assertEqual((m['status'], m.get('resolved_by', '')), ('answered', ''))
        self.assertEqual(self.close(qid)[0], 200)
        self.assertEqual(self.close(qid)[0], 409)  # 已关闭不能再关
        lead_done = self.ask()
        qstore.submit(lead_done, 1, 0, self.answer_all(lead_done))
        self.assertEqual(self.q('receive', lead_done).returncode, 0)
        self.assertEqual(self.q('resolve', lead_done, '--answer-rev', '1', '--note', '记到 Issue').returncode, 0)
        self.assertEqual(self.close(lead_done)[0], 409)
        _, lst, _ = self.http('GET', '/api/list')
        by = {s['id']: s['closed_by'] for s in lst['groups']['closed']}
        self.assertEqual((by[qid], by[lead_done]), ('owner', 'lead'))


SEPARATORS = {'U+2028': ' ', 'U+2029': ' ', 'U+0085': '\u0085'}


class HerdrResultTest(Base):
    """A：真实 herdr 出错时 stdout 为空、错误 JSON 在 stderr、rc=1；假 herdr 按同样方式输出。"""

    def answered(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        return qid

    def test_error_json_on_stderr_is_structured(self):
        self.set_herdr(panes={'demo': [pane()]}, prompt='blocked')
        r = qstore.herdr('demo', 'agent', 'prompt', 'w1:p1', 'x')
        self.assertEqual((r['kind'], r['code']), ('error', 'agent_blocked'))
        r = qstore.herdr('demo', 'agent', 'prompt', 'w9:p9', 'x')
        self.assertEqual((r['kind'], r['code']), ('error', 'agent_not_found'))
        self.set_herdr(panes={})
        r = qstore.pane_list('demo')
        self.assertEqual((r['kind'], r['code']), ('error', 'server_not_running'))

    def test_blocked_on_stderr_is_held_not_uncertain(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='blocked')
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual(m['delivery'], 'held')
        self.assertIn('agent_blocked', m['last_error'])

    def test_agent_not_found_is_held(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='gone')
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'held')
        self.assertIn('agent_not_found', self.meta(qid)['last_error'])

    def test_unknown_error_code_stays_uncertain(self):
        qid = self.answered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='error:something_new')
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'uncertain')


class UncertainExitTest(Base):
    """B：每种 uncertain（回答、更正、撤回通知，含 repair 改出来的）都能在页面上看到并「重新通知」。"""

    def delivered(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'sent')
        return qid

    def test_withdraw_notice_uncertain_can_be_renotified(self):
        qid = self.delivered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='garbage')
        self.assertEqual(self.q('withdraw', qid, '--reason', '需求取消').returncode, 0)
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual(m['withdraw_notice'], 'uncertain')
        d = server.detail_view(qid)
        self.assertTrue(d['renotify'])
        self.assertIn('撤回通知', d['delivery'])
        calls = len(self.herdr_calls())
        server.scan_once()
        self.assertEqual(len(self.herdr_calls()), calls)  # 不自动重发
        old_error = m['last_error']
        qstore.renotify(qid)
        m = self.meta(qid)
        self.assertEqual(m['withdraw_notice'], 'pending')
        self.assertIn(old_error, m['last_error'])  # N3：原失败原因还在
        self.set_herdr(panes={'demo': [pane()]}, prompt='ok')
        server.scan_once()
        self.assertEqual(self.meta(qid)['withdraw_notice'], 'sent')
        self.assertIn('已撤回', self.herdr_calls()[-1][-1])
        self.assertFalse(server.detail_view(qid)['renotify'])

    def test_repaired_withdraw_notice_can_be_renotified(self):
        qid = self.delivered()
        self.assertEqual(self.q('withdraw', qid, '--reason', '需求取消').returncode, 0)
        claimed = qstore.claim([(qid, 'withdraw', 1)])  # 停在 sending 时服务被杀
        self.assertEqual(len(claimed), 1)
        qstore.repair()
        self.assertEqual(self.meta(qid)['withdraw_notice'], 'uncertain')
        self.assertTrue(qstore.delivery_view(self.meta(qid))[2])
        qstore.renotify(qid)
        server.scan_once()
        self.assertEqual(self.meta(qid)['withdraw_notice'], 'sent')

    def test_correction_uncertain_can_be_renotified(self):
        qid = self.delivered()
        qstore.submit(qid, 1, 1, {'Q1': {'type': 'defer'}, 'Q2': {'type': 'defer'}})
        self.set_herdr(panes={'demo': [pane()]}, prompt='garbage')
        server.scan_once()
        m = self.meta(qid)
        self.assertEqual((m['delivery'], m['delivery_kind']), ('uncertain', 'correction'))
        self.assertTrue(server.detail_view(qid)['renotify'])
        qstore.renotify(qid)
        self.set_herdr(panes={'demo': [pane()]}, prompt='ok')
        server.scan_once()
        self.assertEqual(self.meta(qid)['delivery'], 'sent')
        self.assertIn('更正', self.herdr_calls()[-1][-1])

    def test_renotify_keeps_previous_error(self):
        qid = self.delivered()
        self.set_herdr(panes={'demo': [pane()]}, prompt='garbage')
        qstore.renotify(qid)  # sent → pending
        server.scan_once()
        before = self.meta(qid)['last_error']
        self.assertIn('bad_output', before)
        qstore.renotify(qid)
        after = self.meta(qid)['last_error']
        self.assertTrue(after.startswith(before[:20]), after)
        self.assertIn('重新通知', after)
        self.assertIn('bad_output', '\n'.join(server.detail_view(qid)['timeline']))


class OwnershipTest(Base):
    """D：resolve / withdraw / erratum / ask --supersedes 只有原 lead 能做；不符时退出码 4、什么都不改。"""

    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [pane('w1:p1', 'lead-a'), pane('w1:p2', 'lead-b')]})
        self.b_env = {'HERDR_PANE_ID': 'w1:p2'}
        self.no_herdr = {'HERDR_SESSION': '', 'HERDR_PANE_ID': ''}

    def snapshot(self):
        return {p.name: p.read_bytes() for p in (self.tmp / 'inbox').glob('*.md')}

    def denied(self, *args, env):
        before = self.snapshot()
        r = self.q(*args, env=env)
        self.assertEqual(r.returncode, 4, (args, r.stdout, r.stderr))
        self.assertEqual(self.snapshot(), before, args)

    def test_other_lead_and_non_herdr_are_refused(self):
        qid = self.ask()
        f = self.tmp / 'b.md'
        f.write_text(body())
        for env in (self.b_env, self.no_herdr):
            self.denied('erratum', qid, '--note', 'x', '--drop', 'Q1', env=env)
            self.denied('withdraw', qid, '--reason', 'x', env=env)
            self.denied('ask', '--body', str(f), '--title', 't', '--issue', 'none', '--blocking', 'no',
                        '--supersedes', qid, env=env)
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        self.assertEqual(self.q('receive', qid).returncode, 0)
        for env in (self.b_env, self.no_herdr):
            self.denied('receive', qid, env=env)
            self.denied('resolve', qid, '--answer-rev', '1', '--note', 'x', env=env)
        # 存储层同样拒绝
        ident_b = qstore.identify('demo', 'w1:p2')
        for call in (lambda: qstore.resolve(qid, 1, 'x', ident_b), lambda: qstore.withdraw(qid, 'x', ident_b),
                     lambda: qstore.resolve(qid, 1, 'x', {}), lambda: qstore.withdraw(qid, 'x', None)):
            with self.assertRaises(qstore.QError) as e:
                call()
            self.assertEqual(e.exception.code, 403)
        m = self.meta(qid)
        self.assertEqual((m['status'], m['lead_session']), ('answered', 'lead-a'))
        # 原 lead 照常可以
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '1', '--note', '记到 Issue').returncode, 0)

    def test_original_lead_can_erratum_withdraw_and_supersede(self):
        a, b = self.ask(), self.ask()
        self.assertEqual(self.q('erratum', a, '--note', '行号更正').returncode, 0)
        self.assertEqual(self.q('withdraw', b, '--reason', '不需要了').returncode, 0)
        c = self.ask(extra=('--supersedes', a))
        self.assertEqual(self.meta(c)['lead_session'], 'lead-a')
        self.assertEqual(self.meta(a)['superseded_by'], c)


class SeparatorRoundTripTest(Base):
    """E：U+2028 / U+2029 / U+0085 在草稿、提交、网页读取、receive 四条路径上逐字往返。"""

    def setUp(self):
        super().setUp()
        self.start_server()

    def test_round_trip(self):
        for name, ch in SEPARATORS.items():
            with self.subTest(name):
                qid = self.ask()
                text = f'保持现状{ch}不要删除{ch}'
                slots = {'Q1': {'type': 'free', 'text': text}, 'Q2': {'type': 'option', 'choice': 'A. 方案甲（后果：快）',
                                                                    'text': f'补充{ch}说明'}}
                code, _, _ = self.http('POST', f'/api/q/{qid}/save', {'content_rev': 1, 'slots': slots})
                self.assertEqual(code, 200)
                _, d, _ = self.http('GET', f'/api/q/{qid}')
                self.assertEqual(d['draft']['slots'], slots)
                code, _, _ = self.http('POST', f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0, 'slots': slots})
                self.assertEqual(code, 200)
                _, d, _ = self.http('GET', f'/api/q/{qid}')
                self.assertEqual(d['revs'][0]['slots'], slots)
                r = self.q('receive', qid)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(text, r.stdout)
                self.assertIn(f'补充{ch}说明', r.stdout)
                self.assertEqual(self.meta(qid)['delivery'], 'received')
        self.assertEqual(self.q('doctor').returncode, 0)

    def test_incomplete_answer_record_is_not_received(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        path = self.tmp / 'inbox' / f'{qid}.md'
        broken = re.sub(r'^- Q2 \[option\] .*$', '- Q2 [option] {"type": "opt', path.read_text(encoding='utf-8'),
                        flags=re.M)
        path.write_text(broken, encoding='utf-8')
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 1)
        self.assertIn('不完整', r.stderr)
        self.assertNotIn('方案乙', r.stdout)
        self.assertNotEqual(self.meta(qid)['delivery'], 'received')
        self.assertEqual(self.q('resolve', qid, '--answer-rev', '1', '--note', 'x').returncode, 3)
        self.assertIn('不完整', self.q('doctor').stdout)


class DocAndSmallFixTest(Base):
    """G：SKILL.md 命令能照抄执行；链接跟随 QI_PORT；doctor 对进行中的 sending；系统段标题的围栏识别。"""

    def skill_blocks(self):
        return re.findall(r'```bash\n(.*?)```', (QI / 'SKILL.md').read_text(encoding='utf-8'), re.S)

    def fake_home(self):
        home = self.tmp / 'home'
        (home / '.claude' / 'skills').mkdir(parents=True)
        (home / '.claude' / 'skills' / 'question-inbox').symlink_to(QI)
        (home / 'Library' / 'LaunchAgents').mkdir(parents=True)
        return home

    def test_skill_q_alias_runs_verbatim(self):
        home = self.fake_home()
        line = next(l for b in self.skill_blocks() for l in b.split('\n') if l.startswith('Q='))
        r = subprocess.run(['bash', '-c', f'{line}\n$Q status --all'], capture_output=True, text=True,
                           encoding='utf-8', env={**os.environ, 'HOME': str(home)}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('等 Owner 回答', r.stdout)

    def test_skill_install_block_renders_plist(self):
        home = self.fake_home()
        block = next(b for b in self.skill_blocks() if 'plist.template' in b)
        script = '\n'.join(l for l in block.split('\n') if not l.startswith('launchctl'))
        r = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                           env={**os.environ, 'HOME': str(home)}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(home / 'Library' / 'LaunchAgents' / 'local.question-inbox.plist', 'rb') as f:
            pl = plistlib.load(f)
        self.assertEqual(pl['Label'], 'local.question-inbox')
        self.assertTrue(pl['ProgramArguments'][1].endswith('question-inbox/server.py'))
        self.assertTrue(Path(pl['ProgramArguments'][1]).is_file())
        self.assertTrue(pl['StandardErrorPath'].startswith(str(home)))

    def test_ask_link_follows_qi_port(self):
        f = self.tmp / 'q.md'
        f.write_text(body())
        r = self.q('ask', '--body', str(f), '--title', 't', '--issue', 'none', '--blocking', 'no')
        self.assertIn(f'http://127.0.0.1:{self.port}/#demo-0001', r.stdout)
        self.assertNotEqual(self.port, 7788)

    def test_doctor_sending_only_flagged_when_server_not_running(self):
        qid = self.ask()
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        self.assertEqual(len(qstore.claim([(qid, 'answer', 1)])), 1)
        r = self.q('doctor')
        self.assertEqual(r.returncode, 1)
        self.assertIn('sending', r.stdout)
        sd = self.tmp / 'state'
        sd.mkdir(parents=True, exist_ok=True)
        with open(sd / 'server.lock', 'a') as lock:  # 服务在跑：sending 是进行中的正常状态
            fcntl.flock(lock, fcntl.LOCK_EX)
            r = self.q('doctor')
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_system_heading_inside_fence_is_allowed(self):
        quoted = body(evidence='上次的输出：\n\n```\n## 回答\n\n### Rev 1 · x\n## 时间线\n```\n\n`demo/app.py:3`')
        qid = self.ask(quoted)
        self.assertIsInstance(qid, str, getattr(qid, 'stderr', ''))
        qstore.submit(qid, 1, 0, self.answer_all(qid))
        lead, sp = qstore.split_body(qstore.read(qid)[1])
        self.assertIn('## 回答', lead)  # 引用留在正文里
        self.assertEqual(len(qstore.answer_revs(sp)), 1)
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.ask(body(evidence='```\n## 回答\n'))  # 围栏没闭合
        self.assertEqual(r.returncode, 1)
        self.assertIn('没有闭合', r.stderr)
        r = self.ask(body() + '\n## 回答\n\n伪造\n')
        self.assertEqual(r.returncode, 1)


# ---------- C / F：用 node 执行 server.PAGE 里的真实前端函数（DOM 与网络换成最小替身） ----------

PAGE_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

class Elem {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  append(...k) { this.children.push(...k); }
  replaceChildren(...k) { this.children = k; }
  after() {}
  set innerHTML(v) { this._html = v; }
}
const form = {values: {}, checked: {}, texts: {}};
function radio(q, v) {
  if (!(form.values[q] || []).includes(v)) return null;
  return {value: v,
    get checked() { return form.checked[q] === v; },
    set checked(b) { if (b) form.checked[q] = v; else if (form.checked[q] === v) delete form.checked[q]; }};
}
const document = {
  title: '',
  createElement: t => new Elem(t),
  createTextNode: t => ({text: t}),
  querySelector(sel) {
    if (sel === 'meta[name=qi-token]') return {content: 'tok'};
    let m = sel.match(/^input\[name="t_(Q\d+)"\]:checked$/);
    if (m) { const v = form.checked[m[1]]; return v ? radio(m[1], v) : null; }
    m = sel.match(/^input\[name="t_(Q\d+)"\]\[value="([^"]+)"\]$/);
    if (m) return radio(m[1], m[2]);
    throw new Error('unexpected selector ' + sel);
  },
  getElementById(id) {
    const m = id.match(/^x_(Q\d+)$/);
    if (m) return (m[1] in form.texts) ? {get value() { return form.texts[m[1]]; }, set value(v) { form.texts[m[1]] = v; }} : null;
    return id === 'msg' ? null : new Elem('div');
  },
};
const requests = [];
function fetch(path, opt) { return new Promise(resolve => requests.push({path, opt: opt || {}, resolve})); }
function reply(req, status, body) { req.resolve({status, json: async () => body}); }
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const rendered = [];
const ctx = {document, window: {addEventListener() {}}, location: {hash: ''}, fetch, setInterval() {}, Node: Elem,
  console, __rendered: rendered,
  __buildForm(d) {
    form.values = {}; form.checked = {}; form.texts = {};
    for (const q of d.questions) {
      form.values[q.q] = [...q.options.map((o, i) => 'opt:' + i), 'free', 'need_context', 'defer'].concat(q.suggestion ? ['suggested'] : []);
      form.texts[q.q] = '';
    }
  }};
vm.createContext(ctx);
const run = code => vm.runInContext(code, ctx);
const LIST = {open_count: 0, groups: {todo: [], waiting: [], received: [], closed: []}};
function mkDetail(id, n) {
  const questions = [];
  for (let i = 1; i <= (n || 2); i++) questions.push({q: 'Q' + i, text: '问题 ' + i, options: ['A. 甲', 'B. 乙'], suggestion: 'B —— 理由', withdrawn: false});
  return {id, title: id, status: 'open', content_rev: 1, answer_rev: 0, editable: true, questions, draft: {slots: {}, note: ''},
    revs: [], timeline: [], warnings: [], body_html: '', errata_html: '', delivery: '', renotify: false};
}
const pending = path => requests.filter(r => r.path === path && !r.done);
function answer(path, status, body) { const r = pending(path)[0]; r.done = true; reply(r, status, body); }
const posts = () => requests.filter(r => r.opt.method === 'POST').map(r => ({path: r.path, body: JSON.parse(r.opt.body)}));

(async () => {
  const out = {};
  ctx.location.hash = '#demo-0001';
  run(src);
  run('render = function () { __rendered.push(detail.id); __buildForm(detail); }');
  if (scenario === 'late-get') {
    ctx.location.hash = '#demo-0002'; run('route()');
    answer('/api/q/demo-0002', 200, mkDetail('demo-0002')); await flush();
    answer('/api/q/demo-0001', 200, mkDetail('demo-0001')); await flush();
    out.cur = run('cur'); out.displayed = run('detail && detail.id'); out.rendered = rendered.slice();
    form.texts.Q1 = '只回答 B'; form.checked.Q2 = 'defer';
    run('post(detail, "submit", {content_rev: detail.content_rev, answer_rev: detail.answer_rev, slots: slotsFromForm(detail)})');
    await flush();
    out.posts = posts();
  } else if (scenario === 'stale-closure') {
    answer('/api/q/demo-0001', 200, mkDetail('demo-0001')); await flush();
    run('__oldA = detail');
    ctx.location.hash = '#demo-0002'; run('route()');
    answer('/api/q/demo-0002', 200, mkDetail('demo-0002')); await flush();
    out.result = await run('post(__oldA, "submit", {content_rev: 1, answer_rev: 0, slots: {Q1: {type: "free", text: "A 的答案"}}})');
    out.posts = posts(); out.displayed = run('detail.id'); out.msg = run('msg && msg.text');
  } else if (scenario === 'post-after-switch') {
    answer('/api/q/demo-0001', 200, mkDetail('demo-0001')); await flush();
    form.texts.Q1 = 'A 的草稿'; run('markDirty()');
    run('post(detail, "save", {content_rev: 1, slots: slotsFromForm(detail)})');
    await flush();
    ctx.location.hash = '#demo-0002'; run('route()');
    answer('/api/q/demo-0002', 200, mkDetail('demo-0002')); await flush();
    form.texts.Q1 = 'B 正在写'; run('markDirty()');
    const before = requests.length;
    answer('/api/q/demo-0001/save', 200, {ok: true}); await flush();
    out.dirty = run('dirty'); out.displayed = run('detail.id'); out.text = form.texts.Q1;
    out.newGets = requests.slice(before).filter(r => r.path.startsWith('/api/q/')).map(r => r.path);
    out.rendered = rendered.slice();
  } else if (scenario === 'suggest') {
    answer('/api/q/demo-0001', 200, mkDetail('demo-0001', 4)); await flush();
    form.texts.Q1 = '不要删除这个配置';
    form.checked.Q2 = 'opt:0';
    form.checked.Q4 = 'defer'; form.texts.Q4 = '下周再说';
    out.before = run('slotsFromForm(detail)');
    out.filled = run('fillSuggested(detail)');
    out.after = run('slotsFromForm(detail)');
    out.posts = posts();
    out.stale = run('fillForm(detail, {Q1: {type: "option", choice: "Z. 已被勘误删掉", text: "保留原话"}, Q2: {type: "option", choice: "B. 乙"}})');
    out.q1 = {checked: form.checked.Q1 || null, text: form.texts.Q1}; out.q2 = form.checked.Q2;
  } else if (scenario === 'post-then-type') {
    // POST 已成功、强制 GET 还没回来时继续输入，GET 回来后输入不能被覆盖
    const action = process.argv[4];
    const d = mkDetail('demo-0001');
    if (action !== 'save') { d.status = 'answered'; d.answer_rev = 1; d.revs = [{rev: 1, at: 't', corrected_by: 0, slots: {Q1: {type: 'defer'}, Q2: {type: 'defer'}}}]; d.renotify = action === 'renotify'; }
    answer('/api/q/demo-0001', 200, d); await flush();
    const initialRenders = rendered.length;
    form.texts.Q1 = '点按钮之前的原稿'; run('markDirty()');
    const bodies = {save: '{content_rev: 1, slots: slotsFromForm(detail)}',
      submit: '{content_rev: 1, answer_rev: 1, slots: {Q1: {type: "free", text: "更正"}, Q2: {type: "defer"}}}',
      renotify: '{}', close: '{content_rev: 1, answer_rev: 1, reason: "lead 会话没了"}'};
    run(`post(detail, "${action}", ${bodies[action]})`); await flush();
    answer(`/api/q/demo-0001/${action}`, 200, {ok: true, answer_rev: 2, note: '已处理', status: 'resolved'}); await flush();
    out.getPending = pending('/api/q/demo-0001').length;
    form.texts.Q1 = 'POST 成功后继续写的新回答'; form.checked.Q2 = 'opt:1'; run('markDirty()');
    const fresh = mkDetail('demo-0001');
    if (action === 'save') fresh.draft = {slots: {Q1: {type: 'free', text: '点按钮之前的原稿'}}, note: '保存于 t'};
    else { fresh.status = action === 'close' ? 'resolved' : 'answered'; fresh.answer_rev = action === 'submit' ? 2 : 1; fresh.editable = action !== 'close'; fresh.delivery = 'x'; }
    answer('/api/q/demo-0001', 200, fresh); await flush();
    out.text = form.texts.Q1; out.q2 = form.checked.Q2 || null; out.dirty = run('dirty');
    out.rebuilt = rendered.length - initialRenders; out.msg = run('msg && msg.text');
    // 再来一次轮询（不带 force），同样不能覆盖
    run('load(false)'); await flush();
    answer('/api/q/demo-0001', 200, fresh); await flush();
    out.textAfterPoll = form.texts.Q1; out.rebuiltAfterPoll = rendered.length - initialRenders;
  } else if (scenario === 'route-autosave') {
    answer('/api/q/demo-0001', 200, mkDetail('demo-0001')); await flush();
    form.texts.Q1 = '没保存就切题'; run('markDirty()');
    ctx.location.hash = '#demo-0002'; run('route()');
    out.posts = posts();
  }
  for (const r of requests) if (r.path === '/api/list' && !r.done) { r.done = true; reply(r, 200, LIST); }
  await flush();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.stack); process.exit(2); });
"""


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class PageScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='qi-page-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        page = server.PAGE
        start = page.index('>', page.index('<script nonce')) + 1
        (self.tmp / 'page.js').write_text(page[start:page.index('</script>')], encoding='utf-8')
        (self.tmp / 'harness.js').write_text(PAGE_HARNESS, encoding='utf-8')

    def run_page(self, scenario, *extra):
        r = subprocess.run([shutil.which('node'), str(self.tmp / 'harness.js'), str(self.tmp / 'page.js'), scenario, *extra],
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_late_response_for_previous_question_is_ignored(self):
        out = self.run_page('late-get')
        self.assertEqual((out['cur'], out['displayed']), ('demo-0002', 'demo-0002'))
        self.assertNotIn('demo-0001', out['rendered'])
        self.assertEqual(len(out['posts']), 1)
        self.assertEqual(out['posts'][0]['path'], '/api/q/demo-0002/submit')
        self.assertEqual(out['posts'][0]['body']['slots']['Q1'], {'type': 'free', 'text': '只回答 B'})

    def test_old_view_cannot_submit_to_current_route(self):
        out = self.run_page('stale-closure')
        self.assertFalse(out['result'])
        self.assertEqual(out['posts'], [])
        self.assertEqual(out['displayed'], 'demo-0002')
        self.assertIn('没有发出', out['msg'])

    def test_post_completing_after_switch_keeps_new_input(self):
        out = self.run_page('post-after-switch')
        self.assertTrue(out['dirty'])
        self.assertEqual(out['displayed'], 'demo-0002')
        self.assertEqual(out['text'], 'B 正在写')
        self.assertEqual(out['newGets'], [])
        self.assertEqual(out['rendered'][-1], 'demo-0002')

    def test_fill_suggested_only_fills_empty_slots(self):
        out = self.run_page('suggest')
        self.assertEqual(out['before']['Q1'], {'type': 'free', 'text': '不要删除这个配置'})
        self.assertEqual(out['filled'], 1)
        self.assertEqual(out['after']['Q1'], out['before']['Q1'])
        self.assertEqual(out['after']['Q2'], {'type': 'option', 'choice': 'A. 甲', 'text': ''})
        self.assertEqual(out['after']['Q3'], {'type': 'suggested', 'text': ''})
        self.assertEqual(out['after']['Q4'], {'type': 'defer', 'text': '下周再说'})
        self.assertEqual(out['posts'], [])  # 按钮本身不提交
        self.assertEqual(len(out['stale']), 1)  # N2：失效的选项有提示，原话保留
        self.assertIn('Q1', out['stale'][0])
        self.assertEqual(out['q1'], {'checked': None, 'text': '保留原话'})
        self.assertEqual(out['q2'], 'opt:1')

    def test_input_after_successful_post_survives_forced_reload(self):
        for action in ('save', 'submit', 'renotify', 'close'):
            with self.subTest(action):
                out = self.run_page('post-then-type', action)
                self.assertEqual(out['getPending'], 1)  # 成功后确实发起了强制 GET
                self.assertEqual(out['text'], 'POST 成功后继续写的新回答')
                self.assertEqual(out['q2'], 'opt:1')
                self.assertTrue(out['dirty'])
                self.assertEqual(out['rebuilt'], 0)
                self.assertIn('已处理', out['msg'])
                self.assertEqual(out['textAfterPoll'], 'POST 成功后继续写的新回答')
                self.assertEqual(out['rebuiltAfterPoll'], 0)

    def test_route_away_autosaves_unsaved_input(self):
        out = self.run_page('route-autosave')
        self.assertEqual(len(out['posts']), 1)
        self.assertEqual(out['posts'][0]['path'], '/api/q/demo-0001/save')
        self.assertEqual(out['posts'][0]['body']['slots']['Q1'], {'type': 'free', 'text': '没保存就切题'})


if __name__ == '__main__':
    unittest.main()
