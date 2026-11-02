"""skills/dev/question-inbox: storage, CLI, web server and delivery loop, against a fake herdr in temp dirs.

Covers the acceptance scenarios 1, 2, 4, 5, 6, 7, 8 of the spec (3 and 9 need a real herdr / launchd).
Fictional data only: ws `demo`, herdr session `demo`, lead sessions `lead-a` / `lead-b`.
"""
import http.client
import importlib.util
import json
import os
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
        return subprocess.run([sys.executable, str(QI / 'q.py'), *args], capture_output=True, text=True,
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


if __name__ == '__main__':
    unittest.main()
