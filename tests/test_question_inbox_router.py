"""skills/dev/question-inbox · Router MVP-0：需求记录、投递给 Router、router ensure、审批题、暂停开关、需求页安全、任务页归组。

假 herdr / 假 gh 通过 env 注入（HERDR_BIN_PATH / GH_BIN_PATH），从不碰真实 herdr 或 GitHub。
虚构数据：ws `demo`、repo `owner/repo`、lead 所在 session `demo`、Router 所在 session `rt`（QI_ROUTER_SESSION）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_question_inbox as base  # noqa: E402
import test_question_inbox_tasks as tbase  # noqa: E402

QI = base.QI
qstore = base.qstore
server = base.server
tasks = tbase.tasks
RT = 'rt'
ISSUE = 'https://github.com/owner/repo/issues/'


def router_pane(pane_id='r1:p1', sess='sess-router-1', name='router', status='idle'):
    return dict(base.pane(pane_id, lead=sess, status=status), name=name, workspace_id=pane_id.split(':')[0])


def approval_body(drop=(), opts=('A. 批准', 'B. 不批准', 'C. 需要先改'), extra=''):
    parts = {
        '问题': 'Q1. 批准把 owner/repo PR #12（HEAD abc1234）合并到 main 吗？',
        '背景': '演示用的合并审批。',
        '证据': '`make check` 全绿；PR https://github.com/owner/repo/pull/12',
        '动作': '在 GitHub 上合并 PR #12（merge commit）。',
        '对象': 'owner/repo · PR #12 · HEAD abc1234 → main',
        '后果': 'main 带上新导出功能；可以 revert。',
        '选项': '### Q1\n' + '\n'.join(f'- {o}' for o in opts),
        '答完之后': '按回答合并或修改，结论记在 PR 上。',
    }
    return '\n\n'.join(f'## {k}\n\n{v}' for k, v in parts.items() if k not in drop) + extra + '\n'


class RouterBase(base.Base):
    def setUp(self):
        super().setUp()
        (self.tmp / 'sock' / RT).mkdir(parents=True)
        (self.tmp / 'sock' / RT / 'herdr.sock').write_text('')
        gh = self.tmp / 'bin' / 'gh'
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tbase.FAKE_GH}" "$@"\n')
        gh.chmod(0o755)
        extra = {'QI_ROUTER_SESSION': RT, 'GH_BIN_PATH': str(gh), 'FAKE_GH_STATE': str(self.tmp / 'gh.json'),
                 'FAKE_GH_LOG': str(self.tmp / 'gh.log'), 'QI_TASKS_CONFIG': str(self.tmp / 'tasks.json')}
        self.env.update(extra)
        os.environ.update(extra)
        (self.tmp / 'gh.log').write_text('')
        (self.tmp / 'gh.json').write_text(json.dumps({'repos': {}, 'issues': {}}))
        (self.tmp / 'tasks.json').write_text(json.dumps({'repos': [{'ws': 'demo', 'repo': 'owner/repo'}]}))
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]})
        (self.tmp / 'ws' / 'other').mkdir(parents=True)
        self.lead_panes()

    # ----- helpers -----
    def herdr_state(self):
        return json.loads((self.tmp / 'herdr.json').read_text())

    def patch_herdr(self, **kw):
        st = self.herdr_state()
        st.update(kw)
        (self.tmp / 'herdr.json').write_text(json.dumps(st))

    def all_calls(self):
        return [json.loads(l) for l in (self.tmp / 'herdr.log').read_text().splitlines()]

    def as_router(self, *args, pane='r1:p1'):
        return self.q(*args, env={'HERDR_SESSION': RT, 'HERDR_PANE_ID': pane})

    def register(self, pane='r1:p1'):
        r = self.as_router('router', 'register', pane=pane)
        self.assertEqual(r.returncode, 0, r.stderr)

    def new_need(self, text='给 demo 加 CSV 导出', rid=None):
        rec, _ = qstore.create_need(text, rid or '')
        return rec['id']

    def need(self, nid):
        return qstore.read_need(nid)

    def lead_panes(self, gone=()):
        ws = self.tmp / 'ws'
        # w1:p1 demo 的 lead；w1:p2 也叫 lead-demo、cwd 正确（接手用）；w1:p3 别的 ws 的 lead；w1:p4 demo 的 worker（子目录）
        ps = [dict(base.pane('w1:p1', lead='lead-a'), cwd=str(ws / 'demo'), name='lead-demo'),
              dict(base.pane('w1:p2', lead='lead-b'), cwd=str(ws / 'demo'), name='lead-demo'),
              dict(base.pane('w1:p3', lead='lead-c'), cwd=str(ws / 'other'), name='lead-other'),
              dict(base.pane('w1:p4', lead='lead-w'), cwd=str(ws / 'demo' / 'issues' / '3'), name='w-3')]
        st = self.herdr_state()
        st['panes']['demo'] = [x for x in ps if x['pane_id'] not in gone]
        (self.tmp / 'herdr.json').write_text(json.dumps(st))

    def issue_bodies(self, **bodies):
        """假 gh 的 issue view：{号: 正文}；state 默认 OPEN。"""
        data = json.loads((self.tmp / 'gh.json').read_text())
        data.setdefault('issues', {}).update({ISSUE + n: {'body': b, 'state': 'OPEN', 'stateReason': ''} for n, b in bodies.items()})
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def as_lead(self, *args, pane='w1:p1'):
        return self.q(*args, env={'HERDR_SESSION': 'demo', 'HERDR_PANE_ID': pane})

    def assigned(self, text='给 demo 加 CSV 导出'):
        """建一条需求并让 Router take + route 到 demo。"""
        nid = self.new_need(text)
        if not qstore.router_info():
            self.register()
        for args in (('take', nid), ('route', nid, '--ws', 'demo', '--why', '导出属于 demo')):
            r = self.as_router('need', *args)
            self.assertEqual(r.returncode, 0, r.stderr)
        return nid

    def card_file(self, tag='v1', drop=''):
        secs = {'理解': f'要给导出加 CSV（{tag}）', '范围': '只做导出页', '不做什么': '不改导入', '验收草案': '- 下载的文件能用表格软件打开',
                '开放问题': '- 分隔符用逗号还是分号？推荐逗号'}
        f = self.tmp / f'card-{tag}-{drop}.md'
        f.write_text('\n\n'.join(f'## {k}\n\n{v}' for k, v in secs.items() if k != drop))
        return str(f)

    def router_prompts(self):
        return [c for c in self.herdr_calls() if RT in c and 'prompt' in c]

    def post(self, path, payload, **headers):
        return self.http('POST', path, payload, headers=headers)


# ---------- 1. 需求记录：写入、幂等、状态转换、身份 ----------

class NeedStoreTest(RouterBase):
    def test_create_is_idempotent_by_request_id_and_stores_only_allowed_fields(self):
        self.start_server()
        st, j, _ = self.post('/api/need', {'text': '  原话 <保持原样>\n第二行  ', 'request_id': 'req-aaaaaaaa'})
        self.assertEqual((st, j['id'], j['created']), (200, 'need-0001', True))
        st, j, _ = self.post('/api/need', {'text': '  原话 <保持原样>\n第二行  ', 'request_id': 'req-aaaaaaaa'})
        self.assertEqual((st, j['id'], j['created']), (200, 'need-0001', False))  # 连点两次：只有 1 个
        st, j, _ = self.post('/api/need', {'text': '另一条', 'request_id': 'req-bbbbbbbb'})
        self.assertEqual(j['id'], 'need-0002')
        self.assertEqual(sorted(p.name for p in (self.tmp / 'inbox' / 'needs').glob('*.json')), ['need-0001.json', 'need-0002.json'])
        rec = json.loads((self.tmp / 'inbox' / 'needs' / 'need-0001.json').read_text())
        self.assertEqual(rec['text'], '  原话 <保持原样>\n第二行  ')  # 原话不改动
        self.assertEqual((rec['status'], rec['issues'], rec['delivery'], rec['delivery_kind']), ('new', [], 'pending', 'new'))
        for k in ('stage', 'progress', 'prs', 'pr', 'agent'):  # 不存进度 / stage / PR / 谁在做（lead 只是 claim 后的身份）
            self.assertNotIn(k, rec)
        for bad in ({'text': '   ', 'request_id': 'req-cccccccc'}, {'text': 'x', 'request_id': 'bad id!'}):
            self.assertEqual(self.post('/api/need', bad)[0], 400)
        # 问题编号不受影响（共用 _seq，不同的键）
        self.assertEqual(self.ask(), 'demo-0001')

    def test_concurrent_creates_get_unique_ids(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            ids = list(ex.map(lambda i: qstore.create_need(f'需求 {i}', f'req-{i:08d}')[0]['id'], range(12)))
        self.assertEqual(len(set(ids)), 12)

    def test_router_take_route_and_identity_exit_4(self):
        nid = self.new_need()
        self.assertEqual(self.as_router('need', 'take', nid).returncode, 4)  # 没登记 Router：谁都不能 take
        self.register()
        self.assertEqual(self.q('need', 'take', nid).returncode, 4)  # 普通 lead（session demo）
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(), router_pane('r2:p1', 'sess-lead-demo', 'lead-demo')]})
        for args in (('take', nid), ('route', nid, '--ws', 'demo', '--why', 'x'), ('hold', nid, '--why', 'x'),
                     ('note', nid, '--text', 'x')):
            r = self.as_router('need', *args, pane='r2:p1')  # 同一个 session 里的 lead 也不行
            self.assertIn(r.returncode, (3, 4), (args, r.stderr))
            self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.need(nid)['status'], 'new')
        r = self.as_router('need', 'take', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('给 demo 加 CSV 导出', r.stdout)
        self.assertEqual((self.need(nid)['status'], self.need(nid)['delivery']), ('routing', 'received'))
        self.assertEqual(self.as_router('need', 'take', nid).returncode, 0)  # 幂等
        self.assertEqual(self.as_router('need', 'route', nid, '--ws', 'nope', '--why', 'x').returncode, 1)  # ws 目录不存在
        self.assertEqual(self.as_router('need', 'route', nid, '--ws', 'demo', '--why', 'x', pane='r2:p1').returncode, 4)
        r = self.as_router('need', 'route', nid, '--ws', 'other', '--why', '先分错')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.as_router('need', 'route', nid, '--ws', 'demo', '--why', '导出属于 demo')  # 还没 claim：可以改派
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.need(nid)
        self.assertEqual((rec['status'], rec['ws'], rec['why'], rec['issues']), ('assigned', 'demo', '导出属于 demo', []))
        self.assertEqual(self.as_router('need', 'take', nid).returncode, 3)  # 已交给 ws，Router 不再 take

    def test_claim_card_link_identity_and_takeover(self):
        nid = self.assigned()
        self.assertEqual(self.as_lead('need', 'card', nid, '--body', self.card_file()).returncode, 4)  # 还没 claim
        self.assertEqual(self.as_lead('need', 'claim', nid, pane='w1:p3').returncode, 4)  # pane 在别的 ws
        self.assertEqual(self.as_lead('need', 'claim', nid, pane='w1:p4').returncode, 4)  # demo 的 worker（子目录）不行
        self.assertEqual(self.need(nid)['lead'], {})
        r = self.as_lead('need', 'claim', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.need(nid)['lead']['lead_session'], 'lead-a')
        self.assertEqual(self.as_lead('need', 'claim', nid).returncode, 0)  # 幂等
        for args in (('card', nid, '--body', self.card_file()), ('link', nid, '--issue', ISSUE + '1'),
                     ('drop', nid, '--issue', ISSUE + '1', '--why', 'x'), ('hold', nid, '--why', 'x')):
            for pane in ('w1:p2', 'w1:p4'):  # 同名的另一个 lead、worker 都不是负责 lead
                r = self.as_lead('need', *args, pane=pane)
                self.assertEqual(r.returncode, 4, (args, pane, r.stderr))
            r = self.as_lead('need', *args, pane='w1:p2')
            self.assertEqual(r.returncode, 4, (args, r.stderr))
        self.assertEqual(self.as_router('need', 'link', nid, '--issue', ISSUE + '1').returncode, 4)  # Router 也不行
        self.assertEqual(self.as_lead('need', 'claim', nid, pane='w1:p2').returncode, 4)  # 没 --takeover
        self.assertEqual(self.as_lead('need', 'claim', nid, '--takeover', pane='w1:p2').returncode, 4)  # 原 lead 还在
        self.assertEqual(self.need(nid)['lead']['lead_session'], 'lead-a')
        self.lead_panes(gone=('w1:p1',))
        r = self.as_lead('need', 'claim', nid, '--takeover', pane='w1:p2')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.need(nid)['lead']['lead_session'], 'lead-b')

    def test_discussion_flow_confirm_latest_then_link_and_seal(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        bad = self.card_file(drop='验收草案')
        self.assertEqual(self.as_lead('need', 'card', nid, '--body', bad).returncode, 1)
        r = self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.need(nid)
        self.assertEqual((rec['status'], rec['discuss'], rec['card_rev']), ('discussing', 'waiting_owner', 1))
        self.assertEqual(self.as_lead('need', 'link', nid, '--issue', ISSUE + '1').returncode, 3)  # 讨论中不能建 Issue
        rec, new = qstore.owner_reply(nid, '范围再小一点', 'req-reply-001')
        self.assertTrue(new)
        self.assertFalse(qstore.owner_reply(nid, '范围再小一点', 'req-reply-001')[1])  # 幂等
        rec = self.need(nid)
        self.assertEqual((rec['discuss'], rec['delivery'], rec['delivery_to'], rec['delivery_kind']),
                         ('waiting_lead', 'pending', 'lead', 'reply'))
        self.assertIn('范围再小一点', self.as_lead('need', 'show', nid).stdout)
        self.as_lead('need', 'card', nid, '--body', self.card_file('v2'))
        rec = self.need(nid)
        self.assertEqual((rec['discuss'], rec['card_rev'], rec['delivery']), ('waiting_owner', 2, 'received'))
        with self.assertRaises(qstore.QError) as e:
            qstore.owner_confirm(nid, 1, 'req-conf-0001')  # 确认旧版 → 409
        self.assertEqual(e.exception.code, 409)
        rec, _ = qstore.owner_confirm(nid, 2, 'req-conf-0002')
        self.assertEqual((rec['status'], rec['confirmed_rev'], rec['delivery_to'], rec['delivery_kind']), ('confirmed', 2, 'lead', 'confirm'))
        self.assertFalse(qstore.owner_confirm(nid, 2, 'req-conf-0002')[1])  # 同一个 request_id 重放：返回原记录
        self.issue_bodies(**{'30': f'\n需求：{nid}\n\n## 验收标准\n…', '29': '# 标题\n需求：' + nid})
        r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '29')  # 第一行不是「需求：」：拒绝
        self.assertEqual(r.returncode, 1, r.stderr)
        r = self.as_lead('need', 'link', nid, '--issue', 'https://github.com/someone/else/issues/30')  # 根不在主 repo
        self.assertEqual(r.returncode, 1, r.stderr)
        r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '30')
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.need(nid)
        self.assertEqual((rec['status'], rec['issues'], rec['delivery']), ('filed', [ISSUE + '30'], 'received'))
        rec, new = qstore.owner_reply(nid, '再改改', 'req-reply-002')  # 修订 1：立项后线程继续用
        self.assertTrue(new)
        self.assertEqual((rec['status'], rec['delivery'], rec['delivery_kind']), ('filed', 'pending', 'reply'))
        self.assertEqual(self.as_lead('need', 'card', nid, '--body', self.card_file('v3')).returncode, 3)
        self.assertEqual(self.as_lead('need', 'link', nid, '--issue', ISSUE + '31').returncode, 0)  # 可以追加子 Issue
        self.assertEqual([x['kind'] for x in self.need(nid)['thread'] if x['kind'] in ('card', 'reply', 'confirm')],
                         ['card', 'reply', 'card', 'confirm', 'reply'])
        self.assertNotIn('已封存', self.as_lead('need', 'show', nid).stdout)
        self.assertIn('再改改', self.as_lead('need', 'show', nid).stdout)

    def test_clear_need_links_without_discussion(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.issue_bodies(**{'7': f'需求：{nid}'})
        r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '7', '--issue', ISSUE + '8')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.need(nid)['status'], self.need(nid)['issues']), ('filed', [ISSUE + '7', ISSUE + '8']))

    def test_notes_before_claim(self):
        nid = self.new_need()
        qstore.owner_reply(nid, '还在 new 时的补充')
        self.assertEqual(self.need(nid)['delivery_kind'], 'new')  # new 的不另投，take 时一起看到
        self.register()
        self.as_router('need', 'take', nid)
        seq = self.need(nid)['delivery_seq']
        qstore.owner_reply(nid, 'routing 时的纠正', 'req-note-0001')
        rec = self.need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to'], rec['delivery_kind'], rec['delivery_seq']), ('pending', 'router', 'note', seq + 1))
        self.assertIn('← Router 待处理', self.as_router('need', 'show', nid).stdout)
        self.assertEqual(self.as_router('need', 'note', nid, '--text', '已按纠正改分').returncode, 0)
        rec = self.need(nid)
        self.assertEqual((qstore.pending_notes(rec), rec['delivery']), ([], 'received'))
        self.as_router('need', 'route', nid, '--ws', 'demo', '--why', 'x')
        self.assertEqual(self.need(nid)['delivery'], 'received')
        qstore.owner_reply(nid, 'assigned、还没 lead 时的补充')  # 还没人 claim：Router 仍是负责人，投给它（审查 DS 2）
        rec = self.need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to'], rec['delivery_kind'], rec['thread']), ('pending', 'router', 'note', []))

    def test_drop_requires_not_planned_issue_and_hold(self):
        a, b = self.assigned('a'), self.new_need('b')
        self.as_lead('need', 'claim', a)
        url = ISSUE + '9'
        (self.tmp / 'gh.json').write_text(json.dumps({'issues': {url: {'state': 'OPEN', 'stateReason': ''}}}))
        self.assertEqual(self.as_lead('need', 'drop', a, '--issue', url, '--why', '重复').returncode, 3)
        (self.tmp / 'gh.json').write_text(json.dumps({'issues': {url: {'state': 'CLOSED', 'stateReason': 'COMPLETED'}}}))
        self.assertEqual(self.as_lead('need', 'drop', a, '--issue', url, '--why', '重复').returncode, 3)
        (self.tmp / 'gh.json').write_text(json.dumps({'issues': {url: {'state': 'CLOSED', 'stateReason': 'NOT_PLANNED'}}}))
        r = self.as_lead('need', 'drop', a, '--issue', url, '--why', '重复')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.need(a)['status'], self.need(a)['issues']), ('dropped', [url]))
        gh = [json.loads(l) for l in (self.tmp / 'gh.log').read_text().splitlines()]
        self.assertTrue(all(c[:2] == ['issue', 'view'] for c in gh), gh)  # 只读
        self.as_router('need', 'take', b)
        r = self.as_router('need', 'hold', b, '--why', '找不到归属的 ws')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.need(b)['status'], 'held')
        self.assertEqual(self.as_router('need', 'take', b).returncode, 0)  # held 可以重新 take

    def test_router_asks_owner_which_ws_and_answer_comes_back(self):
        self.register()
        f = self.tmp / 'ws-q.md'
        f.write_text(base.body(1).replace('第 1 个问题选哪个？', 'need-0001 归哪个 ws？').replace('A. 方案甲（后果：快）', 'A. demo（导出在这里）')
                     .replace('B. 方案乙（后果：稳）', 'B. other（也有导出）').replace('Q1: B —— 理由 1', 'Q1: A —— README 写着导出归 demo'))
        r = self.as_router('ask', '--ws', 'router', '--body', str(f), '--title', 'need-0001 归哪个 ws', '--issue', 'none', '--blocking', 'no')
        self.assertEqual(r.returncode, 0, r.stderr)
        qid = r.stdout.split('已发布 ')[1].split('：')[0]
        self.assertEqual((self.meta(qid)['herdr_session'], self.meta(qid)['lead_session']), (RT, 'sess-router-1'))  # 回答投回 Router
        self.start_server()
        self.post(f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0, 'slots': {'Q1': {'type': 'suggested'}}})
        self.wait_for(lambda: self.meta(qid)['delivery'] == 'sent', msg='answer to router')
        self.assertEqual(self.router_prompts()[-1][4], 'r1:p1')

    def test_register_rules(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(), router_pane('r2:p1', 'sess-x', 'lead-demo'),
                                                            router_pane('r3:p1', 'sess-y', 'router')]})
        self.assertEqual(self.q('router', 'register').returncode, 4)  # 不在 router session
        self.assertEqual(self.as_router('router', 'register', pane='r2:p1').returncode, 4)  # 名字不是 router
        self.register()
        r = self.as_router('router', 'register', pane='r3:p1')  # 已登记的还活着：不能顶替
        self.assertEqual(r.returncode, 4)
        self.assertEqual(qstore.router_info()['pane_id'], 'r1:p1')
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane('r3:p1', 'sess-y', 'router')]})
        self.register('r3:p1')  # 原来的没了：新起的可以登记
        self.assertEqual(qstore.router_info()['lead_session'], 'sess-y')


# ---------- 2 / 5. 投递给 Router：合并、不重复、held / uncertain、note、暂停 ----------

class NeedDeliveryTest(RouterBase):
    def setUp(self):
        super().setUp()
        self.register()

    def test_batches_of_five_not_repeated(self):
        ids = [self.new_need(f'需求 {i}') for i in range(7)]  # 先建好再起服务：批次确定
        self.start_server()
        self.wait_for(lambda: all(self.need(i)['delivery'] == 'sent' for i in ids), msg='all sent')
        time.sleep(1)
        ps = self.router_prompts()
        self.assertEqual(len(ps), 2, ps)
        self.assertTrue(all(c[c.index('prompt') + 1] == 'r1:p1' for c in ps))
        first = ps[0][-1]
        self.assertTrue(first.startswith('[question-inbox] 新需求：need-0001、need-0002、need-0003、need-0004、need-0005。'), first)
        self.assertIn('这条消息本身不是新的授权', first)
        self.assertIn('need take need-0001 need-0002 need-0003 need-0004 need-0005', first)
        self.assertIn('按 skill router 处理', first)
        self.assertIn('need-0006', ps[1][-1])
        self.assertNotIn('需求 0', first)  # 原话不进 prompt：只当数据，由 take 输出
        self.assertNotEqual(qstore.router_info()['last_delivery_at'], '')

    def test_blocked_holds_then_resumes_and_garbage_is_uncertain(self):
        self.patch_herdr(prompt='blocked')
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: self.need(nid)['delivery'] == 'held', msg='held')
        self.assertIn('agent_blocked', self.need(nid)['last_error'])
        self.patch_herdr(prompt='ok')
        self.wait_for(lambda: self.need(nid)['delivery'] == 'sent', msg='resumed')
        self.patch_herdr(prompt='garbage')
        n2 = self.new_need('第二条')
        self.wait_for(lambda: self.need(n2)['delivery'] == 'uncertain', msg='uncertain')
        before = len(self.router_prompts())
        time.sleep(1.2)
        self.assertEqual(len(self.router_prompts()), before)  # 不自动重发
        self.patch_herdr(prompt='ok')
        st, j, _ = self.post(f'/api/need/{n2}/renotify', {})
        self.assertEqual(st, 200, j)
        self.wait_for(lambda: self.need(n2)['delivery'] == 'sent', msg='renotified')

    def test_owner_reply_and_confirm_go_to_responsible_lead(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
        self.start_server()
        st, j, _ = self.post(f'/api/need/{nid}/reply', {'text': '范围再小一点', 'request_id': 'req-reply-0001'})
        self.assertEqual((st, j['created'], j['to']), (200, True, 'lead'))
        lead_prompts = lambda: [c for c in self.herdr_calls() if c[1] == 'demo']
        self.wait_for(lambda: lead_prompts(), msg='reply prompt to lead')
        c = lead_prompts()[-1]
        self.assertEqual(c[2:5], ['agent', 'prompt', 'w1:p1'])  # 负责 lead 的 pane，不是 Router
        self.assertIn('need show ' + nid, c[-1])
        self.assertIn('讨论线程里回复', c[-1])
        self.as_lead('need', 'card', nid, '--body', self.card_file('v2'))
        st, j, _ = self.post(f'/api/need/{nid}/confirm', {'card_rev': 1, 'request_id': 'req-conf-0001'})
        self.assertEqual(st, 409, j)
        self.assertIn('v2', j['error'])
        self.assertEqual(self.post(f'/api/need/{nid}/confirm', {'card_rev': '2', 'request_id': 'req-conf-0002'})[0], 400)
        st, j, _ = self.post(f'/api/need/{nid}/confirm', {'card_rev': 2, 'request_id': 'req-conf-0003'})
        self.assertEqual((st, j['card_rev']), (200, 2))
        self.wait_for(lambda: any('确认了理解卡' in c[-1] for c in lead_prompts()), msg='confirm prompt')
        self.assertEqual(self.router_prompts(), [])  # 讨论不经过 Router
        st, h, _ = self.http('GET', '/api/list')
        self.assertEqual(h['needs_talk'], [])

    def test_waiting_owner_shows_in_inbox_and_lead_gone_is_held(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
        self.start_server()
        st, h, _ = self.http('GET', '/api/list')
        self.assertEqual([t['id'] for t in h['needs_talk']], [nid])
        self.assertEqual(h['open_count'], 1)
        self.lead_panes(gone=('w1:p1',))  # 负责 lead 不在了：只暂缓，不改投别的 lead、也不拉起谁
        self.post(f'/api/need/{nid}/reply', {'text': '在吗', 'request_id': 'req-reply-0009'})
        self.wait_for(lambda: self.need(nid)['delivery'] == 'held', msg='held')
        self.assertIn('负责 lead', self.need(nid)['last_error'])
        time.sleep(0.8)
        self.assertEqual([c for c in self.herdr_calls() if 'prompt' in c and RT not in c], [])  # 没有 prompt 给任何 lead
        self.assertEqual([c for c in self.all_calls() if 'start' in c], [])
        # lead 决定（R1b）：立项前负责 lead 确认不在 → 自动请 Router 安排接手，**恰好一次**（held 和原因不变）
        self.wait_for(lambda: self.router_prompts(), msg='takeover ask to router')
        time.sleep(0.8)  # 再过几轮投递循环（held 退避后重试）也不重复叫
        self.assertEqual(len(self.router_prompts()), 1)
        self.assertIn('router takeover ' + nid, self.router_prompts()[0][-1])
        self.assertEqual(self.need(nid)['delivery'], 'held')
        self.assertIn('已请 Router 安排接手', self.need(nid)['last_error'])
        # 页面不能显示「已通知」；看板进「卡住 · 没人管」；按钮保留作手动重试
        st, d, _ = self.http('GET', f'/api/need/{nid}')
        self.assertEqual(st, 200, d)
        self.assertNotIn('已通知', d['receipt']['note'])
        self.assertIn('暂缓', d['receipt']['note'])
        self.assertIn('已请 Router 安排接手', json.dumps(d['timeline'], ensure_ascii=False))
        self.assertTrue(d['can_takeover'])
        self.assertIn('没人管', json.dumps(d['flags'], ensure_ascii=False))
        st, j, _ = self.post(f'/api/need/{nid}/takeover', {})
        self.assertEqual(st, 200, j)
        self.wait_for(lambda: len(self.router_prompts()) == 2, msg='manual retry prompts router again')
        self.assertEqual([c for c in self.all_calls() if 'start' in c], [])  # Router 在：不拉起

    def test_pause_stops_delivery_and_resume(self):
        self.start_server()
        st, j, _ = self.post('/api/router/pause', {'paused': True})
        self.assertEqual((st, j['paused']), (200, True))
        self.assertIn('paused: yes', self.q('router', 'paused').stdout)
        nid = self.new_need()
        time.sleep(1.2)
        self.assertEqual(self.router_prompts(), [])
        self.assertEqual(self.need(nid)['delivery'], 'pending')
        self.assertEqual(self.as_router('need', 'take', nid).returncode, 3)  # Router 暂停时不领新需求
        self.assertEqual(self.post('/api/router/pause', {'paused': 'no'})[0], 400)
        self.post('/api/router/pause', {'paused': False})
        self.assertIn('paused: no', self.q('router', 'paused').stdout)
        self.wait_for(lambda: self.need(nid)['delivery'] == 'sent', msg='resumed after unpause')


# ---------- 3. router ensure ----------

class EnsureTest(RouterBase):
    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [base.pane(), router_pane('w9:p1', 'sess-human', 'router')], RT: []})

    def started(self):
        return [c for c in self.all_calls() if 'start' in c and 'agent' in c]

    def test_absent_router_is_started_only_in_router_session(self):
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: any('prompt' in c for c in self.all_calls()), msg='ensure prompt')
        calls = self.all_calls()
        writes = [c for c in calls if c[2:4] not in (['pane', 'list'], ['agent', 'list'], ['agent', 'get'], ['agent', 'read'])]
        self.assertTrue(writes)
        self.assertTrue(all(c[1] == RT for c in calls), calls)  # 不碰其他 session（demo 里那个叫 router 的也不算）
        create = next(c for c in writes if c[2:4] == ['workspace', 'create'])
        self.assertEqual(create[create.index('--label') + 1], 'Router')
        self.assertEqual(create[create.index('--cwd') + 1], str(self.tmp / 'ws'))
        self.assertIn(f'QUESTION_INBOX_DIR={self.tmp / "inbox"}', create)  # 起出来的 Router 用同一个收件箱
        start = self.started()[0]
        self.assertEqual(start[4:9], ['router', '--kind', 'claude', '--pane', 'r1:p1'])
        self.assertEqual(start[start.index('--') + 1:], ['--dangerously-skip-permissions', '--model', 'opus'])
        prompt = next(c for c in writes if c[2:4] == ['agent', 'prompt'])
        self.assertIn('router register', prompt[-1])
        self.assertIn('need list', prompt[-1])
        self.assertEqual(self.need(nid)['delivery'], 'held')
        # 新起的 Router 登记后，暂缓的需求马上投给它
        self.register('r1:p1')
        self.wait_for(lambda: self.need(nid)['delivery'] == 'sent', msg='delivered after register')
        self.assertEqual(len(self.started()), 1)
        st, h, _ = self.http('GET', '/api/needs')
        self.assertEqual((h['router']['ensure_total'], h['router']['registered']), (1, True))

    def test_trust_dialog_selects_trust(self):
        self.patch_herdr(start='trust')
        self.start_server()
        self.new_need()
        self.wait_for(lambda: any('prompt' in c for c in self.all_calls()), msg='prompt after trust')
        keys = [c[-1] for c in self.all_calls() if 'send-keys' in c]
        self.assertEqual(keys, ['down', 'enter'])
        p = self.herdr_state()['panes'][RT][0]
        self.assertEqual((p['name'], p['agent_status']), ('router', 'idle'))

    def test_at_most_three_per_hour_then_red_and_stop(self):
        self.patch_herdr(start='error:boom')
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: qstore.ensure_state().get('gave_up'), timeout=15, msg='gave up')
        time.sleep(1.5)
        self.assertEqual(len(self.started()), 3)
        self.assertEqual(self.herdr_state()['panes'][RT], [])  # 失败时关掉自己建的 workspace
        st, h, _ = self.http('GET', '/api/needs')
        self.assertTrue(h['router']['gave_up'])
        self.assertIn('起不来', self.need(nid)['last_error'])
        self.patch_herdr(start='ok')
        self.assertEqual(self.post('/api/router/retry', {})[0], 200)
        self.wait_for(lambda: len(self.started()) == 4, msg='retry starts again')

    def test_waits_while_ensure_in_flight(self):
        self.start_server()
        for i in range(3):
            self.new_need(f'n{i}')
        self.wait_for(lambda: self.started(), msg='started')
        time.sleep(1.5)  # 名为 router 的 agent 已在、还没登记：只等，不再起
        self.assertEqual(len(self.started()), 1)

    def test_missing_socket_creates_nothing(self):
        (self.tmp / 'sock' / RT / 'herdr.sock').unlink()
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: self.need(nid)['delivery'] == 'held', msg='held')
        self.assertIn('不在', self.need(nid)['last_error'])
        time.sleep(0.8)
        self.assertEqual([c for c in self.all_calls() if RT in c], [])
        self.assertFalse(qstore.ensure_state())

    def test_paused_does_not_start(self):
        qstore.set_paused(True)
        self.start_server()
        self.new_need()
        time.sleep(1.2)
        self.assertEqual(self.all_calls(), [])

    def test_session_switch_asks_router_to_reregister_once_then_pane_gone_restarts(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]})
        self.register()
        # 同一个 pane 里的 router 换了会话（重开过）：不重启它，只发一句「重新登记」（审查 DS 3）
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(sess='sess-reopened')]})
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: [c for c in self.all_calls() if 'prompt' in c], msg='reregister prompt')
        time.sleep(1.2)
        prompts = [c for c in self.all_calls() if 'prompt' in c]
        self.assertEqual(len(prompts), 1, prompts)  # 同一会话 10 分钟内只提醒一次
        self.assertEqual(prompts[0][4], 'r1:p1')
        self.assertIn('router register', prompts[0][-1])
        self.assertEqual(self.started(), [])
        self.assertEqual(self.need(nid)['delivery'], 'held')
        st, h, _ = self.http('GET', '/api/needs')
        self.assertTrue(h['router']['reregister'])
        st, j, _ = self.post('/api/router/reregister', {})  # 页面按钮：清掉提醒记录，再请一次
        self.assertEqual(st, 200, j)
        self.wait_for(lambda: len([c for c in self.all_calls() if 'prompt' in c]) == 2, msg='second reregister')
        self.register('r1:p1')  # 它照做后，暂缓的需求投给新会话
        self.wait_for(lambda: self.need(nid)['delivery'] == 'sent', msg='delivered after reregister')
        # 登记的 pane 不见了：拉起新的
        n2 = self.new_need('第二条')
        self.set_herdr(panes={'demo': [base.pane()], RT: []})
        self.wait_for(lambda: self.started(), timeout=15, msg='restarted')

    def test_unknown_presence_is_not_absent(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]})
        self.register()
        self.patch_herdr(pane_list_fail={RT: 'error:internal'})  # 查不清：只暂缓，不拉起、不提醒
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: self.need(nid)['delivery'] == 'held', msg='held')
        self.assertIn('核对不了', self.need(nid)['last_error'])
        time.sleep(1)
        self.assertEqual([c for c in self.all_calls() if c[2:4] not in (['pane', 'list'], ['agent', 'list'])], [])


class DispatchTest(RouterBase):
    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        os.environ['QI_DISPATCH_WAIT'] = '3'
        self.addCleanup(os.environ.pop, 'QI_DISPATCH_WAIT', None)
        self.register()

    def test_dispatch_writes_task_book_starts_lead_once_in_router_session(self):
        a, b = self.assigned('a'), self.assigned('b')
        self.assertEqual(self.q('router', 'dispatch', a).returncode, 4)  # 不是 Router
        r = self.as_router('router', 'dispatch', a)
        self.assertEqual(r.returncode, 0, r.stderr)
        book = self.tmp / 'ws' / 'demo' / 'dispatch' / f'{a}.md'
        text = book.read_text()
        for s_ in (f'need claim {a}', 'need card', f'需求：{a}', 'owner/repo', 'not planned', '可审的 PR'):
            self.assertIn(s_, text)
        self.assertNotIn('gh issue create', text)
        calls = self.all_calls()
        create = next(c for c in calls if c[2:4] == ['workspace', 'create'])
        self.assertEqual((create[1], create[create.index('--label') + 1], create[create.index('--cwd') + 1]),
                         (RT, 'L·demo', str(self.tmp / 'ws' / 'demo')))
        start = next(c for c in calls if c[2:4] == ['agent', 'start'])
        self.assertEqual(start[4], 'lead-demo')
        prompts = [c[-1] for c in calls if c[2:4] == ['agent', 'prompt']]
        self.assertIn('加载 skill workspace / herdr-workers / question-inbox', prompts[0])
        self.assertEqual(prompts[1], f'读 {book} 并照做。')
        r = self.as_router('router', 'dispatch', b)  # 同一个 ws：续派，不另起
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('续派', r.stdout)
        self.assertEqual(len([c for c in self.all_calls() if c[2:4] == ['agent', 'start']]), 1)
        self.assertTrue(all(c[1] == RT for c in self.all_calls() if c[2:4] != ['pane', 'list'] and c[2:4] != ['agent', 'list']))
        c_ = self.assigned('c')
        qstore.set_paused(True)
        self.assertEqual(self.as_router('router', 'dispatch', c_).returncode, 3)


# ---------- 4. 审批题 ----------

class ApprovalTest(RouterBase):
    def ask_approval(self, text=None):
        return self.ask(text if text is not None else approval_body(), title='合并 PR #12', extra=('--kind', 'approval'))

    def test_hard_validation(self):
        bad = {
            'two questions': approval_body().replace('Q1. 批准', 'Q1. 批准').replace('## 背景', 'Q2. 还有吗？\n\n## 背景'),
            'option text': approval_body(opts=('A. 批准', 'B. 不批准', 'C. 改一下')),
            'four options': approval_body(opts=('A. 批准', 'B. 不批准', 'C. 需要先改', 'D. 其余按建议')),
            'has suggestion': approval_body(extra='\n\n## 建议\n\nQ1: A —— 都绿了'),
            'no action': approval_body(drop=('动作',)),
            'no target': approval_body(drop=('对象',)),
            'no consequence': approval_body(drop=('后果',)),
            'no evidence': approval_body(drop=('证据',)),
        }
        for why, text in bad.items():
            r = self.ask_approval(text)
            self.assertIsInstance(r, subprocess.CompletedProcess, why)
            self.assertEqual(r.returncode, 1, why)
        qid = self.ask_approval()
        self.assertEqual(self.meta(qid)['kind'], 'approval')
        self.assertEqual(self.meta(self.ask())['kind'], 'question')
        r = self.ask(approval_body(), extra=('--kind', 'approval', '--supersedes', 'demo-0002'))
        self.assertEqual(r.returncode, 1)  # 不能跨题型替换

    def test_answer_rules_and_delivery_to_lead(self):
        qid = self.ask_approval()
        meta, body = qstore.read(qid)
        for slots in ({'Q1': {'type': 'suggested'}}, {'Q1': {'type': 'free', 'text': '批'}}, {'Q1': {'type': 'defer'}},
                      {'Q1': {'type': 'option', 'choice': 'C. 需要先改'}}):
            with self.assertRaises(qstore.QError, msg=slots):
                qstore.submit(qid, 1, 0, slots)
        self.start_server()
        st, j, _ = self.post(f'/api/q/{qid}/submit', {'content_rev': 1, 'answer_rev': 0,
                                                      'slots': {'Q1': {'type': 'option', 'choice': 'A. 批准'}}})
        self.assertEqual(st, 200, j)
        self.wait_for(lambda: self.meta(qid)['delivery'] == 'sent', msg='delivered to lead')
        p = self.herdr_calls()[-1]
        self.assertEqual(p[1:5], ['demo', 'agent', 'prompt', 'w1:p1'])  # 投回发起的 lead
        r = self.q('receive', qid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('【审批】', r.stdout)
        self.assertIn('重新核对 HEAD 和环境', r.stdout)
        st, j, _ = self.http('GET', '/api/list')
        st, d, _ = self.http('GET', f'/api/q/{qid}')
        self.assertEqual(d['kind'], 'approval')
        self.assertEqual([q['options'] for q in d['questions']], [['A. 批准', 'B. 不批准', 'C. 需要先改']])
        self.assertEqual(d['questions'][0]['suggestion'], '')
        self.assertEqual([k for k, _ in d['approval_html']], ['动作', '对象', '后果'])  # 单独醒目显示，不挤进补充材料
        self.assertNotIn('合并 PR #12', d['extra_html'])


# ---------- 6. 需求页的跨站请求伪造 / 缺 token ----------

class NeedSecurityTest(RouterBase):
    def test_writes_need_all_four_checks(self):
        self.start_server()
        nid = self.new_need()
        good = {'text': '<script>alert(1)</script>', 'request_id': 'req-xss-0001', 'card_rev': 1}
        for path in ('/api/need', f'/api/need/{nid}/reply', f'/api/need/{nid}/confirm', '/api/router/pause', '/api/router/retry'):
            for why, headers in {'no token': {'X-QI-Token': None}, 'bad token': {'X-QI-Token': 'x' * 48},
                                 'cross origin': {'Origin': 'http://evil.example'}, 'no origin': {'Origin': None},
                                 'form post': {'Content-Type': 'application/x-www-form-urlencoded'},
                                 'bad host': {'Host': 'evil.example'}}.items():
                st, j, _ = self.http('POST', path, good, headers=headers)
                self.assertEqual(st, 403, (path, why, j))
        self.assertEqual(sorted(p.name for p in (self.tmp / 'inbox' / 'needs').glob('*.json')), [f'{nid}.json'])
        self.assertEqual((self.need(nid)['notes'], self.need(nid)['thread']), ([], []))
        self.assertFalse(qstore.paused())
        st, j, r = self.http('GET', '/api/needs', headers={'Host': 'evil.example'})
        self.assertEqual(st, 403)
        st, j, r = self.post('/api/need', good)
        self.assertEqual(st, 200)
        st, j, r = self.http('GET', '/api/needs')
        self.assertTrue(r.getheader('Content-Type').startswith('application/json'))
        self.assertEqual(j['needs'][0]['text'], '<script>alert(1)</script>')  # 原样存、交给前端当文字渲染


# ---------- 7. 任务页：「待合并检查」与按需求归组 ----------

class TasksNeedTest(RouterBase):
    def test_merge_label_and_group_by_need(self):
        root = tbase.issue(30, title='根')
        root['body'] = '需求：need-0007\n\n## 验收标准\n…'
        child = tbase.issue(31, parent=30)
        child['body'] = '子任务正文'
        other = tbase.issue(32)
        other['body'] = '说明\n需求：need-0009'  # 不在第一行：不算
        green = tbase.issue(2, refs=[tbase.pr(102, body='Part of owner/repo#2')])
        (self.tmp / 'gh.json').write_text(json.dumps({'repos': {'owner/repo': tbase.response([root, child, other, green])}}))
        tasks.CACHE = tasks.Cache()
        v = tasks.view(refresh=True)
        flat = {t['number']: t for g in v['groups'] for w in g['ws'] for t in w['tasks'] if t['kind'] == 'task'}
        self.assertEqual(flat[2]['stage']['label'], '待合并检查')
        self.assertIn('checks 全绿，审查和授权需在 PR 上核对', flat[2]['why'][0])
        self.assertEqual((flat[30]['need'], flat[31]['need'], flat[32]['need']), ('need-0007', 'need-0007', ''))
        self.assertEqual([(t['number'], t['depth']) for t in v['needs']['need-0007']], [(30, 0), (31, 1)])
        self.assertNotIn('need-0009', v['needs'])
        self.assertNotIn('body', json.dumps(flat[30]))  # 正文不进页面数据


# ---------- 第 1 轮审查后的修复（A–G），含并发屏障 ----------

def snapshot(tmp):
    """收件箱和状态目录里所有文件的字节（判断「什么都没改」）。"""
    out = {}
    for d in ('inbox', 'state'):
        for f in sorted((tmp / d).rglob('*')):
            if f.is_file() and f.name not in ('server.log', '.lock', 'control.lock'):
                out[str(f.relative_to(tmp))] = f.read_bytes()
    return out


class R1IdentityTest(RouterBase):
    """A1：特权命令只认调用者自己的环境变量；A2：claim 只认 lead 目录 + lead-<ws>；B：未知不当不在。"""

    def writes(self):
        return [c for c in self.all_calls() if c[2:4] not in (['pane', 'list'], ['agent', 'list'], ['agent', 'get'])]

    def test_a1_flags_cannot_borrow_router_or_lead_identity(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        n2 = self.new_need('另一条')
        before, calls = snapshot(self.tmp), len(self.all_calls())
        router = ('--herdr-session', RT, '--pane', 'r1:p1')
        lead = ('--herdr-session', 'demo', '--pane', 'w1:p1')
        cmds = [('need', 'take', n2, *router), ('need', 'route', nid, '--ws', 'other', '--why', 'x', *router),
                ('need', 'note', nid, '--text', 'x', *router), ('need', 'hold', n2, '--why', 'x', *router),
                ('need', 'claim', nid, '--takeover', *lead), ('need', 'card', nid, '--body', self.card_file(), *lead),
                ('need', 'link', nid, '--issue', ISSUE + '1', *lead), ('need', 'drop', nid, '--issue', ISSUE + '1', '--why', 'x', *lead),
                ('router', 'register', *router), ('router', 'lead', 'demo', *router), ('router', 'dispatch', nid, *router),
                ('router', 'close-lead', 'demo', *router)]
        for args in cmds:
            for env in ({'HERDR_SESSION': '', 'HERDR_PANE_ID': ''},                 # 普通 shell
                        {'HERDR_SESSION': 'demo', 'HERDR_PANE_ID': 'w1:p3'}):       # 别的 lead 的 pane
                r = self.q(*args, env=env)
                self.assertEqual(r.returncode, 4, (args, env, r.stderr))
        self.assertEqual(snapshot(self.tmp), before)                    # 持久文件逐字节不变
        self.assertEqual([c for c in self.all_calls()[calls:] if c[2:4] not in (['pane', 'list'], ['agent', 'list'])], [])
        r = self.as_router('need', 'take', n2, '--herdr-session', RT, '--pane', 'r1:p1')  # 参数和自己的环境一致：照常
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a2_claim_requires_exact_lead_dir_and_lead_name(self):
        nid = self.assigned()
        ws = self.tmp / 'ws' / 'demo'
        st = self.herdr_state()
        st['panes']['demo'] += [dict(base.pane('w1:p5', lead='lead-x'), cwd=str(ws), name='w-9'),        # 目录对、名字不对
                                dict(base.pane('w1:p6', lead='lead-y'), cwd=str(ws / 'repo'), name='lead-demo')]  # 名字对、在子目录
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        for pane in ('w1:p4', 'w1:p5', 'w1:p6'):
            r = self.as_lead('need', 'claim', nid, pane=pane)
            self.assertEqual(r.returncode, 4, (pane, r.stderr))
        self.assertEqual(self.need(nid)['lead'], {})
        self.assertEqual(self.as_lead('need', 'claim', nid).returncode, 0)

    def test_b_takeover_refused_when_original_unknown(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        # 接手的 lead 在另一个 session（rt2），原 lead 所在的 demo 查询失败：未知，拒绝
        (self.tmp / 'sock' / 'rt2').mkdir()
        (self.tmp / 'sock' / 'rt2' / 'herdr.sock').write_text('')
        st = self.herdr_state()
        st['panes']['rt2'] = [dict(base.pane('z1:p1', lead='lead-z'), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo')]
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        before = snapshot(self.tmp)
        for bad in ('error:internal', 'timeout'):
            self.patch_herdr(pane_list_fail={'demo': bad}, sleep=5)
            r = self.q('need', 'claim', nid, '--takeover', env={'HERDR_SESSION': 'rt2', 'HERDR_PANE_ID': 'z1:p1'})
            self.assertEqual(r.returncode, 4, (bad, r.stderr))
            self.assertIn('核对不了', r.stderr)
        self.assertEqual(snapshot(self.tmp), before)
        # 同一个会话出现在两个 pane：也算查不清
        self.patch_herdr(pane_list_fail={})
        st = self.herdr_state()
        st['panes']['demo'].append(dict(base.pane('w1:p9', lead='lead-a'), cwd='/tmp'))
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        r = self.q('need', 'claim', nid, '--takeover', env={'HERDR_SESSION': 'rt2', 'HERDR_PANE_ID': 'z1:p1'})
        self.assertEqual(r.returncode, 4, r.stderr)
        # 原 lead 的 session 确认没在跑：可以接手
        st['panes'].pop('demo')
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        r = self.q('need', 'claim', nid, '--takeover', env={'HERDR_SESSION': 'rt2', 'HERDR_PANE_ID': 'z1:p1'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.need(nid)['lead']['lead_session'], 'lead-z')

    def test_b_register_refused_when_old_router_unknown(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(), router_pane('r3:p1', 'sess-y', 'router')]})
        self.register()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane('r3:p1', 'sess-y', 'router')]})
        info = qstore.router_info()
        real = qstore.presence
        with mock.patch.object(qstore, 'presence', lambda s, l: ('unknown', None, 'pane list 超时')):
            with self.assertRaises(qstore.QError) as e:
                qstore.register_router(qstore.identify(RT, 'r3:p1'))
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(qstore.router_info(), info)
        self.assertEqual(real(RT, 'sess-router-1')[0], 'absent')


class R1OwnershipTest(RouterBase):
    """A3：Router 只复用 / 关闭自己建、三项对得上的 lead；人工同名 pane、重复同名都不碰。"""

    def setUp(self):
        super().setUp()
        os.environ['QI_DISPATCH_WAIT'] = '2'
        self.addCleanup(os.environ.pop, 'QI_DISPATCH_WAIT', None)
        self.register()

    def touched(self, pane):
        return [c for c in self.all_calls() if pane in c and c[2:4] not in (['agent', 'get'],)]

    def human_lead(self, pane='human:p1', sess='sess-human', ws_id='manual'):
        return dict(base.pane(pane, lead=sess), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo', workspace_id=ws_id)

    def test_manual_same_name_pane_is_conflict_not_prompted(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(), self.human_lead()]}, prompt_working=True)
        nid = self.assigned()
        r = self.as_router('router', 'dispatch', nid)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('不是 Router 建的', r.stderr)
        r = self.as_router('need', 'hold', nid, '--why', r.stderr.strip()[:200])  # skill：冲突时 hold，页面显示原因
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.need(nid)['status'], 'held')
        self.assertEqual(self.touched('human:p1'), [])
        self.assertEqual([c for c in self.all_calls() if c[2:4] in (['agent', 'start'], ['workspace', 'create'], ['workspace', 'close'])], [])
        self.assertEqual(self.as_router('router', 'close-lead', 'demo').returncode, 3)
        self.assertEqual(self.touched('human:p1'), [])

    def test_duplicate_same_name_is_conflict(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        a = self.assigned('a')
        self.assertEqual(self.as_router('router', 'dispatch', a).returncode, 0)
        rec = qstore.router_leads()['demo']
        st = self.herdr_state()
        st['panes'][RT].append(self.human_lead('human:p2'))
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        b = self.assigned('b')
        r = self.as_router('router', 'dispatch', b)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('2 个名为 lead-demo', r.stderr)
        self.assertEqual(self.touched('human:p2'), [])
        prompts = [c for c in self.all_calls() if c[2:4] == ['agent', 'prompt']]
        self.assertFalse(any(c[4] == rec['pane_id'] and b in c[-1] for c in prompts))  # 冲突时连托管的也不派

    def test_recorded_lead_reused_only_while_identity_matches_and_close(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        a, b, c = self.assigned('a'), self.assigned('b'), self.assigned('c')
        self.assertEqual(self.as_router('router', 'dispatch', a).returncode, 0)
        rec = qstore.router_leads()['demo']
        self.assertEqual(set(rec), {'workspace_id', 'pane_id', 'agent_session', 'created_at', 'pending'})
        self.assertFalse(rec['pending'])
        self.assertTrue(rec['agent_session'])
        r = self.as_router('router', 'dispatch', b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('续派', r.stdout)
        # 同一个 pane、同名、同 cwd，只是会话重开过：归属没变，刷新记录后继续派（审查 DS N1 / 任务书 I）
        st = self.herdr_state()
        p = next(x for x in st['panes'][RT] if x['pane_id'] == rec['pane_id'])
        p['agent_session']['value'] = 'sess-reopened'
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        r = self.as_router('router', 'dispatch', c)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('续派', r.stdout)
        self.assertEqual(qstore.router_leads()['demo']['agent_session'], 'sess-reopened')
        self.assertEqual(len([x for x in self.all_calls() if x[2:4] == ['agent', 'start']]), 1)
        # 换了 cwd（同名 agent 被挪到别的目录）就不是同一个归属：冲突
        p['cwd'] = str(self.tmp / 'ws' / 'other')
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        self.assertEqual(self.as_router('router', 'close-lead', 'demo').returncode, 3)
        p['cwd'] = str(self.tmp / 'ws' / 'demo')
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        r = self.as_router('router', 'close-lead', 'demo')
        self.assertEqual(r.returncode, 0, r.stderr)
        closes = [x for x in self.all_calls() if x[2:4] == ['workspace', 'close']]
        self.assertEqual([x[4] for x in closes], [rec['workspace_id']])
        self.assertNotIn('demo', qstore.router_leads())


class R1PauseTest(RouterBase):
    """C：暂停在动作链中途提交，之后不再有新的启动 / 投递 / 派工；停在可恢复的状态；恢复后只投一次。"""

    def pause_after(self, target, match):
        """包住 target（模块, 名字）：被调用且参数命中 match 时，在它返回之后（没有持任何锁）提交暂停。"""
        mod, name = target
        real = getattr(mod, name)

        def wrapper(*a, **k):
            out = real(*a, **k)
            if match(a):
                qstore.set_paused(True)
            return out
        return mock.patch.object(mod, name, wrapper)

    def test_pause_between_create_and_start(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: []})
        with self.pause_after((qstore, 'gated'), lambda a: list(a[1:3]) == ['workspace', 'create']):
            result, why = server.routerctl.ensure()
        self.assertEqual(result, 'paused', why)
        calls = self.all_calls()
        self.assertEqual([c[2:4] for c in calls if c[2:4] in (['agent', 'start'], ['agent', 'prompt'])], [])
        self.assertEqual(self.herdr_state()['panes'][RT], [])  # 刚建的 workspace 已关
        self.assertEqual(qstore.geti(qstore.ensure_state(), 'total'), 0)  # 不计拉起次数
        self.assertFalse(qstore.ensure_state().get('gave_up'))

    def test_pause_between_identity_lookup_and_prompt(self):
        self.register()
        nid = self.new_need()
        with self.pause_after((qstore, 'presence'), lambda a: True):
            server.scan_needs()
        self.assertEqual(self.router_prompts(), [])
        rec = self.need(nid)
        self.assertEqual(rec['delivery'], 'pending')  # 不卡在 sending
        server.scan_needs()  # 还在暂停：仍不发
        self.assertEqual(self.router_prompts(), [])
        qstore.set_paused(False)
        server.scan_needs()
        server.scan_needs()
        self.assertEqual(len(self.router_prompts()), 1)  # 恢复后只发一次
        self.assertEqual(self.need(nid)['delivery'], 'sent')

    def test_pause_between_ensure_lead_and_dispatch_prompt(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        self.register()
        nid = self.assigned()
        with self.pause_after((server.routerctl, 'ensure_lead'), lambda a: True):
            with self.assertRaises(qstore.QError) as e:
                server.routerctl.dispatch(nid)
        self.assertIn('暂停', e.exception.message)
        prompts = [c for c in self.all_calls() if c[2:4] == ['agent', 'prompt']]
        self.assertFalse(any('dispatch' in c[-1] and nid in c[-1] for c in prompts), prompts)
        self.assertNotIn('dispatch', self.need(nid))

    def test_pause_waits_for_inflight_write_and_blocks_later_ones(self):
        """门禁是真的锁：暂停要等正在进行的那一次写完才提交，提交之后的写一律不发。"""
        import threading
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt='timeout', sleep=1.5)
        t0 = time.monotonic()
        th = threading.Thread(target=lambda: qstore.gated(RT, 'agent', 'prompt', 'r1:p1', 'x', timeout=5))
        th.start()
        time.sleep(0.4)
        qstore.set_paused(True)
        waited = time.monotonic() - t0
        th.join()
        self.assertGreater(waited, 1.2)
        self.assertEqual(qstore.gated(RT, 'agent', 'prompt', 'r1:p1', 'y')['kind'], 'paused')
        self.assertEqual(len([c for c in self.all_calls() if 'prompt' in c]), 1)

    def test_lead_replies_continue_during_pause_and_route_is_blocked(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.as_lead('need', 'card', nid, '--body', self.card_file())
        n2 = self.new_need('另一条')
        self.as_router('need', 'take', n2)
        qstore.set_paused(True)
        self.assertEqual(self.as_router('need', 'route', n2, '--ws', 'demo', '--why', 'x').returncode, 3)  # DS Nit 1
        qstore.owner_reply(nid, '暂停期间的回复')
        server.scan_needs()
        p = [c for c in self.herdr_calls() if c[1] == 'demo']
        self.assertEqual(len(p), 1)  # 已在跑的 lead 不受影响（DS Nit 2）
        self.assertEqual(p[0][4], 'w1:p1')


class R1RaceTest(RouterBase):
    """D：claim 与 route 并发（屏障插在 claim 查 herdr 之后、写之前）。"""

    def test_route_between_claim_checks_and_write_is_409(self):
        nid = self.assigned()
        router = qstore.identify(RT, 'r1:p1')
        lead = qstore.identify('demo', 'w1:p1')
        real = qstore.agent_name

        def barrier(session, pane):
            qstore.route(nid, 'other', '改派到 other', router)  # 合法的并发改派
            return real(session, pane)
        with mock.patch.object(qstore, 'agent_name', barrier):
            with self.assertRaises(qstore.QError) as e:
                qstore.claim_need(nid, lead)
        self.assertEqual(e.exception.code, 409)
        rec = self.need(nid)
        self.assertEqual((rec['ws'], rec['lead']), ('other', {}))  # 不会出现 ws=other、lead=demo 的 lead

    def test_lead_change_between_checks_and_write_is_409(self):
        nid = self.assigned()
        real = qstore.agent_name
        first = qstore.identify('demo', 'w1:p1')
        second = qstore.identify('demo', 'w1:p2')

        def barrier(session, pane):
            if pane == 'w1:p2':
                qstore.claim_need(nid, first)
            return real(session, pane)
        with mock.patch.object(qstore, 'agent_name', barrier):
            with self.assertRaises(qstore.QError) as e:
                qstore.claim_need(nid, second)
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(self.need(nid)['lead']['lead_session'], 'lead-a')


class R1StuckTest(RouterBase):
    """E1：assigned 没人 claim 的补充投给 Router、超时自动补投一次、页面能重新通知。"""

    def test_unclaimed_nudged_once_and_renotify(self):
        os.environ['QI_UNCLAIMED_AFTER'] = '3'
        self.addCleanup(os.environ.pop, 'QI_UNCLAIMED_AFTER', None)
        nid = self.assigned()
        self.assertEqual(qstore.need_delivery_view(self.need(nid))[0], '已分到 ws，等 lead claim')
        self.assertTrue(qstore.need_delivery_view(self.need(nid))[2])  # 页面给「重新通知」
        server.scan_needs()
        self.assertEqual(self.router_prompts(), [])  # 还没到时间
        time.sleep(3.2)
        server.scan_needs()
        server.scan_needs()
        ps = self.router_prompts()
        self.assertEqual(len(ps), 1)
        self.assertIn('一直没有 lead claim', ps[0][-1])
        time.sleep(3.2)
        server.scan_needs()
        self.assertEqual(len(self.router_prompts()), 1)  # 每次分派只自动补投一次
        qstore.need_renotify(nid)  # Owner 点「重新通知」
        qstore.need_renotify(nid)  # 已在排队时再点：不报错、不重复排（DS R2 Nit 3）
        server.scan_needs()
        self.assertEqual(len(self.router_prompts()), 2)
        self.as_lead('need', 'claim', nid)
        self.assertFalse(qstore.need_delivery_view(self.need(nid))[2])


class R1MiscTest(RouterBase):
    """F Nit 5、G1、G2。"""

    def test_dead_router_workspace_closed_before_new_one(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [{'pane_id': 'old:p1', 'workspace_id': 'old', 'cwd': '/tmp', 'agent_status': 'unknown'},
                                                          {'pane_id': 'keep:p1', 'workspace_id': 'keep', 'cwd': '/tmp', 'agent_status': 'unknown'}]})
        qstore.ensure_end('started', '', workspace_id='old', root_pane='old:p1')
        result, why = server.routerctl.ensure()
        self.assertEqual(result, 'started', why)
        closes = [c[4] for c in self.all_calls() if c[2:4] == ['workspace', 'close']]
        self.assertEqual(closes, ['old'])  # 只关自己记着的那个空壳，别的空 workspace 不碰

    def test_g1_task_view_uses_agent_list_names(self):
        self.set_herdr(panes={'demo': [dict(base.pane('w1:p7', lead='x'), name='w-42', cwd='/tmp')]})
        agents = tbase.tasks.fetch_session('demo')
        self.assertEqual([a['pane']['name'] for a in agents], ['w-42'])
        self.assertEqual([c[2:4] for c in self.all_calls()], [['agent', 'list']])
        refs = tbase.tasks.agent_refs(agents, [])
        self.assertIn(('demo', 42), refs)

    def test_g2_lead_cwd_from_tasks_json(self):
        lc = self.tmp / 'leads' / 'demo-lead'
        lc.mkdir(parents=True)
        (self.tmp / 'tasks.json').write_text(json.dumps({'repos': [{'ws': 'demo', 'repo': 'owner/repo', 'lead_cwd': str(lc)}]}))
        self.assertEqual(qstore.lead_cwd('demo'), lc)
        self.assertEqual(qstore.lead_cwd('other'), self.tmp / 'ws' / 'other')
        self.set_herdr(panes={'demo': [base.pane(), dict(base.pane('w1:p1', lead='lead-a'), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo')],
                              RT: [router_pane()]}, prompt_working=True)
        self.set_herdr(panes={'demo': [dict(base.pane('w1:p1', lead='lead-a'), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo')],
                              RT: [router_pane()]}, prompt_working=True)
        os.environ['QI_DISPATCH_WAIT'] = '2'
        self.addCleanup(os.environ.pop, 'QI_DISPATCH_WAIT', None)
        self.register()
        nid = self.assigned()
        r = self.as_router('router', 'dispatch', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((lc / 'dispatch' / f'{nid}.md').is_file())
        self.assertFalse((self.tmp / 'ws' / 'demo' / 'dispatch').exists())
        create = next(c for c in self.all_calls() if c[2:4] == ['workspace', 'create'])
        self.assertEqual(create[create.index('--cwd') + 1], str(lc))
        self.assertEqual(self.as_lead('need', 'claim', nid).returncode, 4)  # ~/workspaces/demo 不再是 lead 目录
        rec = qstore.router_leads()['demo']
        r = self.as_router('need', 'show', nid)
        r = self.q('need', 'claim', nid, env={'HERDR_SESSION': RT, 'HERDR_PANE_ID': rec['pane_id']})
        self.assertEqual(r.returncode, 0, r.stderr)  # Router 起的 lead-demo（cwd = lead_cwd）可以 claim
        (self.tmp / 'tasks.json').write_text(json.dumps({'repos': [{'ws': 'demo', 'repo': 'owner/repo', 'lead_cwd': 'relative/x'}]}))
        with self.assertRaises(ValueError):
            tbase.tasks.load_config()


# ---------- 第 2 轮审查后的修复（H–L） ----------

BAD_SHAPES = {  # rc=0、合法 JSON，但结构不对
    'missing list': lambda t, k: {'id': f'cli:{t}:list', 'result': {'type': f'{t}_list'}},
    'null list': lambda t, k: {'id': f'cli:{t}:list', 'result': {'type': f'{t}_list', k: None}},
    'empty result': lambda t, k: {'id': f'cli:{t}:list', 'result': {}},
    'wrong type': lambda t, k: {'id': f'cli:{t}:list', 'result': {'type': 'something_else', k: []}},
    'bad item': lambda t, k: {'id': f'cli:{t}:list', 'result': {'type': f'{t}_list', k: [{'agent': 'claude'}]}},
    'list result': lambda t, k: {'id': f'cli:{t}:list', 'result': []},
}


class R2ShapeTest(RouterBase):
    """H：结构坏了的成功响应只能算「查不清」，不能当「确认不在」。"""

    def bad(self, session, which, shape):
        t, k = ('pane', 'panes') if which == 'pane' else ('agent', 'agents')
        st = self.herdr_state()
        st['list_raw'] = {session: {which: BAD_SHAPES[shape](t, k)}}
        (self.tmp / 'herdr.json').write_text(json.dumps(st))

    def test_wrappers_reject_every_bad_shape_but_accept_real_empty(self):
        for shape in BAD_SHAPES:
            for which, fn in (('pane', qstore.pane_list), ('agent', qstore.agent_list)):
                self.bad('demo', which, shape)
                r = fn('demo')
                self.assertFalse(r['ok'], (shape, which))
                self.assertEqual(r['code'], 'bad_shape', (shape, which))
        self.patch_herdr(list_raw={})
        self.set_herdr(panes={'demo': []})
        self.assertEqual((qstore.pane_list('demo')['panes'], qstore.agent_list('demo')['agents']), ([], []))
        self.assertEqual(qstore.presence('demo', 'lead-a')[0], 'absent')  # 真正的空列表：确认不在

    def test_bad_shape_blocks_takeover(self):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        (self.tmp / 'sock' / 'rt2').mkdir()
        (self.tmp / 'sock' / 'rt2' / 'herdr.sock').write_text('')
        st = self.herdr_state()
        st['panes']['rt2'] = [dict(base.pane('z1:p1', lead='lead-z'), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo')]
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        before = snapshot(self.tmp)
        for shape in BAD_SHAPES:
            self.bad('demo', 'pane', shape)
            self.assertEqual(qstore.presence('demo', 'lead-a')[0], 'unknown', shape)
            r = self.q('need', 'claim', nid, '--takeover', env={'HERDR_SESSION': 'rt2', 'HERDR_PANE_ID': 'z1:p1'})
            self.assertEqual(r.returncode, 4, (shape, r.stderr))
        self.assertEqual(snapshot(self.tmp), before)

    def test_bad_shape_blocks_ensure_register_lead_record_and_delivery(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]})
        self.register()
        info = qstore.router_info()
        for shape in BAD_SHAPES:
            for which in ('agent', 'pane'):
                self.bad(RT, which, shape)
                calls = len(self.all_calls())
                result, why = server.routerctl.ensure()
                self.assertIn(result, ('skipped', 'waiting'), (shape, which, why))
                self.assertEqual([c for c in self.all_calls()[calls:] if c[2:4] not in (['pane', 'list'], ['agent', 'list'])], [], (shape, which))
                self.assertNotEqual(qstore.lead_record_presence('demo')[0], 'absent', (shape, which))
                pane, why, start = server.router_presence(info)
                if which == 'pane':
                    self.assertEqual((pane, start), (None, False), (shape, why))  # 查不清：不拉起
        # register：旧 Router 的 session 查询结构坏了，不顶替
        self.patch_herdr(list_raw={})
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(), router_pane('r3:p1', 'sess-y', 'router')]})
        with mock.patch.object(qstore, 'agent_name', lambda s, p: 'router'):
            self.bad(RT, 'pane', 'missing list')
            with self.assertRaises(qstore.QError):
                qstore.register_router({'herdr_session': RT, 'pane_id': 'r3:p1', 'lead_session': 'sess-y', 'agent_kind': 'claude'})
        self.assertEqual(qstore.router_info(), info)
        # 任务页：这个 session 算查询失败，不是「没有 agent」
        self.bad('demo', 'agent', 'null list')
        with self.assertRaises(RuntimeError):
            tbase.tasks.fetch_session('demo')

    def test_bad_agent_list_does_not_start_second_router(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]})
        self.bad(RT, 'agent', 'missing list')
        self.start_server()
        nid = self.new_need()
        self.wait_for(lambda: self.need(nid)['delivery'] == 'held', msg='held')
        time.sleep(1)
        self.assertEqual([c for c in self.all_calls() if c[2:4] in (['workspace', 'create'], ['agent', 'start'])], [])


class R2LeadRecordTest(RouterBase):
    """I：自建 lead 的归属记录能自愈：起完读不到身份先记待核验，恢复后补全；同一个 pane 换会话照样复用。"""

    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        os.environ['QI_DISPATCH_WAIT'] = '2'
        self.addCleanup(os.environ.pop, 'QI_DISPATCH_WAIT', None)
        self.register()

    def flaky(self, fail_from, fail_count):
        """让第 fail_from 次起的 fail_count 次 pane_list 超时（其余走真的假 herdr）。"""
        real, n = qstore.pane_list, {'i': 0}

        def wrapped(session):
            n['i'] += 1
            if fail_from <= n['i'] < fail_from + fail_count:
                return {'ok': False, 'kind': 'timeout', 'code': 'timeout', 'message': 'herdr 调用超时'}
            return real(session)
        return mock.patch.object(qstore, 'pane_list', wrapped)

    def test_identity_read_timeout_after_start_then_recovers(self):
        starts = lambda: len([c for c in self.all_calls() if c[2:4] == ['agent', 'start']])
        # agent start 回包里没有会话 id（真 herdr 的回包形状未实测）、紧接着的身份查询又连续超时：只能记待核验
        with mock.patch.object(server.routerctl, '_start_session', lambda r, s, p: ''):
            pane, new = server.routerctl.ensure_lead('demo')
        self.assertTrue(new)
        rec = qstore.router_leads()['demo']
        self.assertEqual((rec['agent_session'], rec['pending']), ('', True))  # 没有把未知身份写成完整记录
        with self.flaky(1, 3):
            for _ in range(3):  # 恢复前连续几次：查不清，409，不起新的
                with self.assertRaises(qstore.QError):
                    server.routerctl.ensure_lead('demo')
        self.assertEqual(starts(), 1)
        p2, new2 = server.routerctl.ensure_lead('demo')  # 恢复：补全记录、复用原来的 lead
        self.assertEqual((p2, new2), (pane, False))
        rec = qstore.router_leads()['demo']
        self.assertEqual((rec['pending'], bool(rec['agent_session'])), (False, True))
        self.assertEqual(starts(), 1)
        self.assertEqual(self.as_router('router', 'close-lead', 'demo').returncode, 0)

    def test_start_response_session_is_used_when_lookup_fails(self):
        with self.flaky(1, 99):
            info, err = server.routerctl.start_in_new_workspace(RT, 'L·demo', self.tmp / 'ws' / 'demo', 'lead-demo', 'hi')
        self.assertIsNone(err)
        self.assertTrue(info[2].startswith('sess-lead-demo'))  # 来自 agent start 回包

    def test_real_conflicts_still_409(self):
        server.routerctl.ensure_lead('demo')
        rec = qstore.router_leads()['demo']
        st = self.herdr_state()
        root = next(x for x in st['panes'][RT] if x['pane_id'] == rec['pane_id'])
        for change in ({'workspace_id': 'elsewhere'}, {'cwd': str(self.tmp / 'ws' / 'other')}):
            saved = dict(root)
            root.update(change)
            (self.tmp / 'herdr.json').write_text(json.dumps(st))
            self.assertEqual(qstore.lead_record_presence('demo')[0], 'conflict', change)
            root.clear(); root.update(saved)
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        qstore.record_lead('demo', None)  # 没有记录的同名 lead
        self.assertEqual(qstore.lead_record_presence('demo')[0], 'conflict')


class R2TreeTest(RouterBase):
    """J：关 workspace 前核验整棵树，有人工 / 说不清的 pane 就不关、保留记录。"""

    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        self.register()
        server.routerctl.ensure_lead('demo')
        self.rec = qstore.router_leads()['demo']

    def add(self, **pane):
        st = self.herdr_state()
        st['panes'][RT].append(dict({'workspace_id': self.rec['workspace_id'], 'agent_status': 'idle'}, **pane))
        (self.tmp / 'herdr.json').write_text(json.dumps(st))

    def closes(self):
        return [c for c in self.all_calls() if c[2:4] == ['workspace', 'close']]

    def test_manual_claude_or_shell_in_lead_workspace_blocks_close(self):
        lc = self.tmp / 'ws' / 'demo'
        for extra in (dict(base.pane('man:p1', lead='human-session'), name='human-not-managed', cwd=str(lc)),  # 人工 claude
                      {'pane_id': 'man:p2', 'cwd': str(lc)},                                                  # 普通 shell
                      dict(base.pane('man:p3', lead='h3'), name='w-5', cwd='/tmp'),                           # worker 名但不在 lead 目录下
                      dict(base.pane('man:p4', lead='h4'), cwd=str(lc / 'issues' / '5'))):                   # 无名 agent
            st = self.herdr_state()
            st['panes'][RT] = [x for x in st['panes'][RT] if not x['pane_id'].startswith('man:')]
            (self.tmp / 'herdr.json').write_text(json.dumps(st))
            self.add(**{k: v for k, v in extra.items() if k != 'workspace_id'})
            r = self.as_router('router', 'close-lead', 'demo')
            self.assertEqual(r.returncode, 3, (extra, r.stderr))
            self.assertIn('不归 Router 管', r.stderr)
            self.assertEqual(self.closes(), [])
            self.assertIn('demo', qstore.router_leads())  # 记录保留
            self.assertTrue(any(x['pane_id'] == extra['pane_id'] for x in self.herdr_state()['panes'][RT]))

    def test_lead_with_its_workers_closes(self):
        lc = self.tmp / 'ws' / 'demo'
        self.add(**dict(base.pane('wk:p1', lead='w1'), name='w-12', cwd=str(lc / 'issues' / '12' / 'repo')))
        self.add(**dict(base.pane('wk:p2', lead='w2'), name='rv-12-codex', cwd=str(lc / 'issues' / '12' / 'review')))
        r = self.as_router('router', 'close-lead', 'demo')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[4] for c in self.closes()], [self.rec['workspace_id']])

    def test_dead_shell_cleanup_requires_only_root(self):
        st = self.herdr_state()
        root = next(x for x in st['panes'][RT] if x['pane_id'] == self.rec['pane_id'])
        for k in ('agent', 'name', 'agent_session'):
            root.pop(k, None)  # lead 退出：根 pane 成了空 shell
        st['panes'][RT].append({'pane_id': 'man:p9', 'workspace_id': self.rec['workspace_id'], 'cwd': '/tmp'})  # 有人在里面开了个终端
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        ok, why = server.routerctl.close_if_dead(RT, self.rec['workspace_id'], self.rec['pane_id'])
        self.assertFalse(ok)
        self.assertEqual(self.closes(), [])
        st['panes'][RT] = [x for x in st['panes'][RT] if x['pane_id'] != 'man:p9']
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        ok, why = server.routerctl.close_if_dead(RT, self.rec['workspace_id'], self.rec['pane_id'])
        self.assertTrue(ok, why)


class OldWorkspaceBase(RouterBase):
    """Router 建的 lead-demo 已退出（根 pane 留成 shell），旧 workspace 里还有 worker w-42。"""

    def setUp(self):
        super().setUp()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane()]}, prompt_working=True)
        os.environ['QI_DISPATCH_WAIT'] = '2'
        self.addCleanup(os.environ.pop, 'QI_DISPATCH_WAIT', None)
        self.register()
        server.routerctl.ensure_lead('demo')
        self.rec = qstore.router_leads()['demo']
        st = self.herdr_state()
        root = next(x for x in st['panes'][RT] if x['pane_id'] == self.rec['pane_id'])
        for k in ('agent', 'name', 'agent_session', 'interactive_ready'):
            root.pop(k, None)  # lead 的 agent 退出，pane 留成 shell
        st['panes'][RT].append(dict(base.pane('w:p9', lead='sess-w42'), name='w-42', workspace_id=self.rec['workspace_id'],
                                    cwd=str(self.tmp / 'ws' / 'demo' / 'issues' / '42')))
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        self.calls0 = len(self.all_calls())

    def new_calls(self, *sub):
        return [c for c in self.all_calls()[self.calls0:] if c[2:4] == list(sub)]



class R3BlockedOldWorkspaceTest(OldWorkspaceBase):
    """第 3 轮 DS I1：旧 lead 退出、旧 workspace 里还有 worker 时，不清记录、不另起新 lead，需求 hold。"""

    def test_worker_left_blocks_new_lead_keeps_record_and_holds(self):
        self.assertEqual(qstore.lead_record_presence('demo')[0], 'absent')
        with self.assertRaises(server.routerctl.LeadBlocked) as e:
            server.routerctl.ensure_lead('demo')
        self.assertEqual(e.exception.code, 409)
        self.assertIn('worker w-42', e.exception.message)
        self.assertEqual((self.new_calls('agent', 'start'), self.new_calls('workspace', 'close'), self.new_calls('workspace', 'create')), ([], [], []))
        self.assertEqual(qstore.router_leads()['demo']['workspace_id'], self.rec['workspace_id'])  # 记录保留
        # dispatch：需求自动 hold，原因上页面
        nid = self.assigned()
        r = self.as_router('router', 'dispatch', nid)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('关不掉', r.stderr)
        rec = self.need(nid)
        self.assertEqual(rec['status'], 'held')
        self.assertIn('worker w-42', rec['notes'][-1]['text'])
        self.assertEqual(self.new_calls('agent', 'start'), [])
        # close-lead 报出里面还有 worker，不关、记录保留
        r = self.as_router('router', 'close-lead', 'demo')
        self.assertIn('worker w-42', r.stdout + r.stderr)
        self.assertEqual(self.new_calls('workspace', 'close'), [])
        self.assertEqual(qstore.router_leads()['demo']['workspace_id'], self.rec['workspace_id'])

    def test_only_empty_root_shell_left_cleans_up_and_starts_new(self):
        st = self.herdr_state()
        st['panes'][RT] = [x for x in st['panes'][RT] if x['pane_id'] != 'w:p9']  # worker 已回收，只剩空根 shell
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        pane, new = server.routerctl.ensure_lead('demo')
        self.assertTrue(new)
        self.assertEqual([c[4] for c in self.new_calls('workspace', 'close')], [self.rec['workspace_id']])
        self.assertEqual(len(self.new_calls('agent', 'start')), 1)
        self.assertNotEqual(qstore.router_leads()['demo']['workspace_id'], self.rec['workspace_id'])


class R4ClaimedLeadGoneTest(OldWorkspaceBase):
    """补完 DS R3 I1：需求已有负责 lead 时，LeadBlocked 也要能 hold（负责 lead 确认不在），否则记 note、不吞原话。"""

    def claimed_need(self):
        nid = self.assigned()
        lead_pane = dict(base.pane('w1:p1', lead='lead-a'), cwd=str(self.tmp / 'ws' / 'demo'), name='lead-demo')
        st = self.herdr_state()
        st['panes']['demo'] = [lead_pane]
        (self.tmp / 'herdr.json').write_text(json.dumps(st))
        r = self.as_lead('need', 'claim', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        return nid

    def drop_lead(self):
        st = self.herdr_state()
        st['panes']['demo'] = []
        (self.tmp / 'herdr.json').write_text(json.dumps(st))

    def test_claimed_lead_gone_dispatch_holds(self):
        nid = self.claimed_need()
        self.drop_lead()
        r = self.as_router('router', 'dispatch', nid)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('关不掉', r.stderr)
        self.assertIn('已 hold', r.stderr)
        rec = self.need(nid)
        self.assertEqual(rec['status'], 'held')
        self.assertIn('worker w-42', rec['notes'][-1]['text'])
        self.assertEqual(rec['notes'][-1]['by'], 'router')

    def test_lead_unknown_keeps_status_records_note(self):
        nid = self.claimed_need()
        self.patch_herdr(pane_list_fail={'demo': 'error:internal'})
        r = self.as_router('router', 'dispatch', nid)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('worker w-42', r.stderr)            # 原话保留
        self.assertIn('已记 note', r.stderr)
        self.assertIn('需求状态未变', r.stderr)
        rec = self.need(nid)
        self.assertEqual(rec['status'], 'assigned')
        self.assertIn('worker w-42', rec['notes'][-1]['text'])
        self.assertEqual(rec['notes'][-1]['by'], 'router')

    def test_lead_present_hold_still_refused(self):
        nid = self.claimed_need()
        r = self.as_router('need', 'hold', nid, '--why', 'x')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(self.need(nid)['status'], 'assigned')
        r = self.as_router('router', 'dispatch', nid)  # dispatch 也一样：记 note，不 hold
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('需求状态未变', r.stderr)
        self.assertEqual(self.need(nid)['status'], 'assigned')


class R2CountTest(RouterBase):
    """K：启动计数只能退还本次自己预占、且还没执行 agent start 的那一次。"""

    def three_starts(self):
        for _ in range(3):
            ok, aid = qstore.ensure_begin()
            self.assertTrue(ok)
        return dict(qstore.ensure_state())

    def test_paused_reregister_does_not_touch_counts(self):
        before = self.three_starts()
        self.set_herdr(panes={'demo': [base.pane()], RT: [router_pane(sess='sess-reopened')]})
        qstore.router_info()  # 没登记 / 会话不同：走重新登记分支
        qstore.set_paused(True)
        result, why = server.routerctl.ensure()
        self.assertEqual(result, 'paused')
        after = qstore.ensure_state()
        self.assertEqual((after['attempts'], after['total']), (before['attempts'], before['total']))
        qstore.set_paused(False)
        self.set_herdr(panes={'demo': [base.pane()], RT: []})  # Router 不在了：第 4 次必须被限流
        result, why = server.routerctl.ensure()
        self.assertEqual(result, 'skipped', why)
        self.assertTrue(qstore.ensure_state()['gave_up'])
        self.assertEqual([c for c in self.all_calls() if c[2:4] == ['agent', 'start']], [])

    def pause_after(self, match):
        real = qstore.gated

        def wrapper(*a, **k):
            out = real(*a, **k)
            if match(a):
                qstore.set_paused(True)
            return out
        return mock.patch.object(qstore, 'gated', wrapper)

    def test_pause_after_agent_start_keeps_count(self):
        self.set_herdr(panes={'demo': [base.pane()], RT: []})
        with self.pause_after(lambda a: list(a[1:3]) == ['agent', 'start']):
            result, why = server.routerctl.ensure()
        self.assertEqual(result, 'paused')
        st = qstore.ensure_state()
        self.assertEqual((len(st['attempts']), st['total']), (1, 1))  # 已执行过 agent start：照常计数

    def test_pause_before_agent_start_refunds_only_own_attempt(self):
        before = self.three_starts()
        qstore.ensure_retry()  # 清掉放弃标记和本小时计数（Owner 的重试），total 保留
        old_total = qstore.ensure_state()['total']
        self.set_herdr(panes={'demo': [base.pane()], RT: []})
        with self.pause_after(lambda a: list(a[1:3]) == ['workspace', 'create']):
            result, why = server.routerctl.ensure()
        self.assertEqual(result, 'paused')
        st = qstore.ensure_state()
        self.assertEqual((st['attempts'], st['total']), ([], old_total))  # 退掉的是自己那一次，不是历史


# ---------- node：需求页、审批页、任务页筛选 ----------

PAGE_SCENARIOS = r"""
const modes = mk('nav', 'modes'); doc.append(modes);
const ARG = JSON.parse(process.argv[4] || '{}');
const H = {session: 'rt', socket: true, registered: true, present: true, why: '', pane_id: 'r1:p1', registered_at: 't',
  last_delivery_at: '2026-11-03T10:00:00+08:00', ensure_total: 2, ensure_last_at: 't', ensure_last_result: 'started',
  gave_up: false, last_error: '', paused: false};
const CARD = rev => [['理解', '理解 ' + rev + ' <i>x</i>'], ['范围', '范围'], ['不做什么', '不做'], ['验收草案', '验收草案 ' + rev], ['开放问题', '']];
const N = (id, over) => Object.assign({id, text: '<script>evil()</script> 第一行\n第二行', status: 'discussing', discuss: 'waiting_owner', ws: 'demo', why: '<b>理由</b>',
  lead: 'demo · pane w1:p1', card_rev: 2, confirmed_rev: 0, sealed: false,
  thread: [{at: 't1', by: 'lead', kind: 'card', rev: 1, card: CARD('v1')}, {at: 't2', by: 'owner', kind: 'reply', text: '<b>回复</b>'},
           {at: 't3', by: 'lead', kind: 'card', rev: 2, card: CARD('v2')}],
  created: '2026-11-03T09:00:00+08:00', updated: '', issues: ['https://github.com/owner/repo/issues/30', 'javascript:alert(1)'],
  notes: [{at: 't', by: 'owner', text: '<img src=x onerror=alert(1)>'}], pending_notes: 1, delivery: 'Router 已领取', delivery_detail: '',
  renotify: false, log: []}, over || {});
(async () => {
  const out = {};
  const answerAll = (path, status, body) => { for (const r of pending(path)) { r.done = true; r.resolve({status, json: async () => body}); } };
  if (scenario === 'board') {
    ctx.location.hash = '#board';
    run(src); await flush();
    const c = (id, title, col, ws, over) => Object.assign({id, title, number: 30, ws: ws || 'demo', column: col, flags: [], next: '下一步',
      who: 'lead-demo', updated: '2026-11-03T10:00:00+08:00', unread: 0, loop: false, subs: {done: 0, skipped: 0, left: 0, total: 0},
      prs: [], comments: [], error: '', status: 'filed', live: col === 'doing'}, over || {});
    const col = (key, label, cards) => ({key, label, count: cards.length, cards});
    answerAll('/api/board', 200, {generated_at: 't', errors: [], ws: ['demo'], degrade: false, columns: [
      col('wait', '待处理', [c('need-0007', '演示需求 <b>x</b>', 'wait', 'demo', {flags: [{kind: 'wait', text: '理解卡 v2 等你确认'}]})]),
      col('stuck', '卡住', []), col('todo', '待立项', []), col('review', '审查中', []),
      col('doing', '进行中', [c('need-0008', '第二个', 'doing')]), col('done', '完成', [])]});
    answerAll('/api/needs', 200, {needs: [], router: Object.assign({}, H, ARG.router || {})});
    await flush();
    if (pending('/api/needs').length) { answerAll('/api/needs', 200, {needs: [], router: Object.assign({}, H, ARG.router || {})}); await flush(); }
    out.rail = findAll(e => e.tagName === 'A', modes).map(a => [a.attrs.href, text(a)]);
    out.text = text(main);
    out.hrefs = findAll(e => e.tagName === 'A', main).map(a => a.attrs.href);
    out.cardCls = byId('router_card') ? byId('router_card').className : '';
    out.buttons = findAll(e => e.tagName === 'BUTTON', main).map(text);
    // 提需求对话框
    button('+ 提需求', main).dispatch('click');
    const ta = byId('new_need_text'); ta.value = '加一个 CSV 导出'; ta.dispatch('input');
    const go = byId('new_need_go'); go.dispatch('click'); go.dispatch('click'); await flush();
    out.postsPending = posts().length;
    out.goPending = {disabled: byId('new_need_go').disabled, text: text(byId('new_need_go'))};
    answerAll('/api/need', ARG.fail ? 500 : 200, ARG.fail ? {error: '服务端出错'} : {ok: true, id: 'need-0008', created: true});
    await flush();
    out.afterMsg = ARG.fail ? text(byId('sheet')) : (run('boardMsg && boardMsg.text') || text(main));
    out.afterValue = byId('new_need_text') ? byId('new_need_text').value : '';
    if (ARG.fail) { byId('new_need_go').dispatch('click'); await flush(); }
    // Router 暂停开关（放在看板底部的小卡片里）
    if (byId('pause_btn')) { byId('pause_btn').dispatch('click'); await flush(); out.pauseBusy = text(byId('pause_btn')); }
    out.posts = posts();
  } else if (scenario === 'old-needs') {
    ctx.location.hash = '#needs';
    run(src); await flush();
    out.mode = run('mode'); out.needId = run('needId');
    ctx.location.hash = '#needs:need-0007'; listeners.hashchange(); await flush();
    out.mode2 = run('mode'); out.needId2 = run('needId');
    answerAll('/api/need/need-0007', 200, {id: 'need-0007', title: 'T', text: 't', status: 'filed', ws: 'demo', lead: {},
      card_rev: 0, confirmed_rev: 0, issues: [], github: '', column: 'doing', column_label: '进行中', flags: [], sealed: false,
      timeline: [], unread: 0, errors: [], receipt: {}, loop: false, subs: [], subs_progress: {done: 0, skipped: 0, left: 0, total: 0},
      prs: [], created: 't', updated: 't', to_issue: true, lead_state: 'present'});
    await flush();
    out.detail = text(main);
  } else if (scenario === 'need-detail') {
    ctx.location.hash = '#need:need-0007';
    run(src); await flush();
    const ago = new Date(Date.now() - 3 * 60000).toISOString();
    answer('/api/need/need-0007', 200, {id: 'need-0007', title: '详情', text: 't', status: 'filed', ws: 'demo', lead: {}, card_rev: 0,
      confirmed_rev: 0, issues: [], github: '', column: 'doing', column_label: '进行中', flags: [], sealed: false, unread: 0, errors: [],
      receipt: {note: '已通知 lead-demo'}, loop: false, subs: [], subs_progress: {done: 0, skipped: 0, left: 0, total: 0}, prs: [],
      created: 't', updated: 't', to_issue: true, lead_state: 'present', timeline: [
        {kind: 'thread', at: ago, by: 'owner', source: '7788 讨论', text: 'OWNER_BODY_MARK：只在 7788 里聊', receipt: {notified: true, read: false, note: ''}},
        {kind: 'system', at: ago, text: '负责 lead 不在，等待接手', source: '系统'}]});
    await flush();
    out.text = text(main);
    out.noise = out.text.includes('开始投递') || out.text.includes('投递暂缓');
    out.relative = out.text.includes('分钟前');
  } else if (scenario === 'need-inline') {
    const qd = (n, status) => ({id: 'demo-0001', kind: 'question', title: '就地答题', status, content_rev: 3, answer_rev: status === 'answered' ? 1 : 2,
      editable: true, draft: {slots: {}}, option_notes: {}, revs: status === 'answered' ? [{rev: 1, at: 't', corrected_by: 0, slots: {Q1: {type: 'option', choice: 'B. 乙'}, Q2: {type: 'free', text: '自由回答'}}}] : [],
      questions: [{q: 'Q1', text: '第一问', options: ['A. 甲', 'B. 乙'], suggestion: 'A —— 理由', withdrawn: false},
                  {q: 'Q2', text: '第二问', options: ['A. 甲', 'B. 乙'], suggestion: '', withdrawn: false}]});
    const need = (status) => ({id: 'need-0007', title: '就地答题需求', text: 't', status: 'filed', ws: 'demo', lead: {}, card_rev: 0,
      confirmed_rev: 0, issues: [], github: '', column: 'doing', column_label: '进行中', flags: [], sealed: false, unread: 0, errors: [],
      receipt: {}, loop: false, subs: [], subs_progress: {done: 0, skipped: 0, left: 0, total: 0}, prs: [], created: 't', updated: 't',
      to_issue: true, lead_state: 'present', timeline: [{kind: 'question', at: 't', id: 'demo-0001', title: '就地答题', qkind: 'question',
        qstatus: status, blocking: false, invalid: '', source: '7788 问题卡', detail: qd(2, status)}]});
    ctx.location.hash = '#need:need-0007';
    run(src); await flush();
    answer('/api/need/need-0007', 200, need('open')); await flush();
    const pickIn = (name, v) => { const r = find(e => e.tagName === 'INPUT' && e.attrs.name === name && e.attrs.value === v);
      r.checked = true; r.dispatch('change'); };
    pickIn('i_demo-0001_Q1', 'opt:1');
    const t2 = byId('i_demo-0001_Q2'); t2.value = '自由回答'; t2.dispatch('input');
    out.inlineText = text(main);
    button('提交回答', main).dispatch('click'); await flush();
    out.sheetOpen = 'data-open' in byId('sheet').attrs;
    out.postsBefore = posts().length;
    const ok = button('确认提交', byId('sheet')); ok.dispatch('click'); ok.dispatch('click'); await flush();
    out.busy = text(byId('inline_demo-0001'));
    out.postsWhilePending = posts().length;
    if (ARG.fail) {
      answerAll('/api/q/demo-0001/submit', 409, {error: '问题或回答已被更新'});
      await flush();
      out.msg = run('inlineState["demo-0001"].msg && inlineState["demo-0001"].msg.text');
      out.kept = run('inlineDraft["demo-0001"].checked.Q1');
      out.formStill = !!byId('i_demo-0001_Q1');
    } else {
      answerAll('/api/q/demo-0001/submit', 200, {ok: true, answer_rev: 3});
      await flush();
      answer('/api/need/need-0007', 200, need('answered')); await flush();
      out.readOnly = text(main);
      out.noForm = !byId('i_demo-0001_Q1');
    }
    out.posts = posts();
  } else if (scenario === 'approval') {
    ctx.location.hash = '#demo-0001';
    run(src);
    const d = mkDetail('demo-0001', 1, {kind: 'approval', title: '合并 PR #12'});
    d.questions[0].options = ['A. 批准', 'B. 不批准', 'C. 需要先改']; d.questions[0].suggestion = '';
    answer('/api/q/demo-0001', 200, d); await flush();
    out.radios = findAll(e => e.tagName === 'INPUT' && e.attrs.type === 'radio', main).map(e => e.attrs.value);
    out.checked = findAll(e => e.tagName === 'INPUT' && e.checked).length;
    out.buttons = findAll(e => e.tagName === 'BUTTON', main).map(text);
    out.text = text(main);
    out.textarea = !!byId('x_Q1');
    byId('x_Q1').value = '随便写的'; byId('x_Q1').dispatch('input');
    byId('go').dispatch('click'); await flush();
    out.postsWithoutChoice = posts().length;   // 只写字不选：不算回答
    pick('Q1', 'opt:2'); byId('x_Q1').value = ''; byId('x_Q1').dispatch('input');
    byId('go').dispatch('click'); await flush();
    out.postsCEmpty = posts().length; out.msgCEmpty = run('msg && msg.text');
    byId('x_Q1').value = '先补测试'; byId('x_Q1').dispatch('input');
    byId('go').dispatch('click'); await flush();
    out.posts = posts();
    ctx.location.hash = ''; listeners.hashchange(); await flush();
    const it = (id, kind, blocking) => ({id, kind, ws: 'demo', blocking, created: 't' + id, updated: '', title: 'T ' + id, issue: 'none',
      status: 'open', content_rev: 1, n_questions: 1, has_draft: false, delivery: '', renotify: false, invalid: '', closed_by: ''});
    answerAll('/api/list', 200, {open_count: 3, groups: {todo: [it('demo-0001', 'question', 'yes'), it('demo-0002', 'approval', 'no'),
      it('demo-0003', 'question', 'no')], waiting: [], received: [], closed: []},
      needs_talk: [{id: 'need-0007', text: '<b>需求</b>', ws: 'demo', card_rev: 2, updated: 't'}]});
    await flush();
    out.groups = findAll(e => e.tagName === 'SECTION', side).map(s => [text(s.children[0]), findAll(e => e.tagName === 'A', s).map(a => a.attrs.href)]);
    out.mainLinks = findAll(e => e.tagName === 'A', main).map(a => a.attrs.href);
  } else if (scenario === 'tasks-filter') {
    ctx.location.hash = '#tasks:need-0007';
    run(src); await flush();
    out.mode = run('mode'); out.filter = run('taskFilter');
    const row = (n, need) => ({kind: 'task', depth: 0, ws: 'demo', repo: 'owner/repo', number: n, title: 'T' + n, url: 'https://github.com/owner/repo/issues/' + n,
      stage: {key: 'merge', label: '待合并检查', group: 'you'}, why: ['PR #1 checks 全绿，审查和授权需在 PR 上核对'], tags: [], prs: [], agents: [], questions: [], sub: null, need});
    answerAll('/api/tasks', 200, {generated_at: 't', sources: [], unlinked: [], needs: {'need-0007': [{repo: 'owner/repo', number: 30, title: 'T30', url: '', stage: {key: 'merge', label: '待合并检查'}, sub: null, depth: 0}]},
      groups: [{key: 'you', title: '等你', count: 2, ws: [{ws: 'demo', tasks: [row(30, 'need-0007'), row(40, '')]}]}]});
    await flush();
    out.text = text(main);
    out.hrefs = findAll(e => e.tagName === 'A', main).map(a => a.attrs.href);
    ctx.location.hash = '#tasks'; listeners.hashchange(); await flush();
    answerAll('/api/tasks', 200, {generated_at: 't', sources: [], unlinked: [], needs: {'need-0007': [{repo: 'owner/repo', number: 30, title: 'T30', url: '', stage: {key: 'merge', label: '待合并检查'}, sub: null, depth: 0}]},
      groups: [{key: 'you', title: '等你', count: 2, ws: [{ws: 'demo', tasks: [row(30, 'need-0007'), row(40, '')]}]}]});
    await flush();
    out.allText = text(main);
    out.posts = posts().length;
  }
  for (const r of requests) if (!r.done) { r.done = true; r.resolve({status: 200, json: async () => (r.path === '/api/list' ? EMPTY : {})}); }
  await flush();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error('REQDUMP', JSON.stringify(requests.map(r => [r.path, !!r.done]))); console.error(e.stack); process.exit(2); });
"""


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class RouterPageTest(unittest.TestCase):
    def setUp(self):
        base.PageScriptTest.setUp(self)
        prefix = base.DOM_HARNESS[:base.DOM_HARNESS.index('(async () => {')]
        (self.tmp / 'harness.js').write_text(prefix + PAGE_SCENARIOS, encoding='utf-8')

    def run_page(self, scenario, arg=None):
        return base.PageScriptTest.run_page(self, scenario, json.dumps(arg or {}))

    def test_board_rail_create_dialog_and_pause(self):
        out = self.run_page('board')
        self.assertEqual(out['rail'], [['#', '待处理1'], ['#board', '需求']])
        self.assertIn('演示需求 <b>x</b>', out['text'])
        self.assertIn('理解卡 v2 等你确认', out['text'])
        self.assertIn('完成', out['text'])
        self.assertIn('#need:need-0007', out['hrefs'])
        self.assertEqual(out['cardCls'], 'rcard')  # Router 在线：不是 bad
        self.assertIn('Router · 在线', out['text'])
        self.assertIn('全局暂停', out['buttons'])
        self.assertEqual(out['postsPending'], 1)
        self.assertEqual(out['goPending'], {'disabled': True, 'text': '提交中…'})
        self.assertIn('已提交 need-0008', out['afterMsg'])
        paths = [p['path'] for p in out['posts']]
        self.assertEqual(paths, ['/api/need', '/api/router/pause'])
        self.assertEqual(out['posts'][0]['body']['text'], '加一个 CSV 导出')
        self.assertTrue(out['posts'][0]['body']['request_id'])
        self.assertEqual(out['posts'][1]['body'], {'paused': True})

    def test_failed_submit_keeps_text_and_retries_with_same_request_id(self):
        out = self.run_page('board', {'fail': True})
        self.assertIn('服务端出错', out['afterMsg'])
        self.assertEqual(out['afterValue'], '加一个 CSV 导出')
        creates = [p['body'] for p in out['posts'] if p['path'] == '/api/need']
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0]['request_id'], creates[1]['request_id'])

    def test_router_cannot_start_is_red(self):
        out = self.run_page('board', {'router': {'gave_up': True, 'present': False, 'last_error': '1 小时内已拉起 3 次'}})
        self.assertIn('bad', out['cardCls'])
        self.assertIn('Router 起不来', out['text'])
        self.assertIn('重新尝试拉起', out['buttons'])

    def test_pause_button_says_waiting_while_posting(self):
        out = self.run_page('board')
        self.assertIn('暂停中…等当前动作做完', out['pauseBusy'])

    def test_reregister_button_when_router_absent(self):
        out = self.run_page('board', {'router': {'present': False, 'why': '登记的 Router 会话不在了'}})
        self.assertIn('让 Router 重新登记', out['buttons'])
        self.assertIn('登记的 Router 会话不在了', out['text'])
        out = self.run_page('board')
        self.assertNotIn('让 Router 重新登记', out['buttons'])

    def test_detail_shows_owner_body_and_hides_delivery_noise(self):
        out = self.run_page('need-detail')
        self.assertIn('OWNER_BODY_MARK：只在 7788 里聊', out['text'])
        self.assertIn('负责 lead 不在，等待接手', out['text'])
        self.assertFalse(out['noise'])
        self.assertTrue(out['relative'])

    def test_inline_question_uses_summary_and_versions(self):
        out = self.run_page('need-inline')
        self.assertIn('第一问', out['inlineText'])
        self.assertIn('第二问', out['inlineText'])
        self.assertTrue(out['sheetOpen'])
        self.assertEqual(out['postsBefore'], 0)
        self.assertEqual(out['postsWhilePending'], 1)
        self.assertIn('提交中', out['busy'])
        post = out['posts'][0]
        self.assertEqual(post['path'], '/api/q/demo-0001/submit')
        self.assertEqual(post['body']['content_rev'], 3)
        self.assertEqual(post['body']['answer_rev'], 2)
        self.assertEqual(post['body']['slots']['Q1']['choice'], 'B. 乙')
        self.assertEqual(post['body']['slots']['Q2'], {'type': 'free', 'text': '自由回答'})
        self.assertTrue(out['noForm'])
        self.assertIn('已提交 Rev 1', out['readOnly'])

    def test_inline_question_409_keeps_input(self):
        out = self.run_page('need-inline', {'fail': True})
        self.assertIn('版本冲突', out['msg'])
        self.assertEqual(out['kept'], 'opt:1')
        self.assertTrue(out['formStill'])
        self.assertEqual(len(out['posts']), 1)

    def test_old_needs_addresses_redirect(self):
        out = self.run_page('old-needs')
        self.assertEqual((out['mode'], out['needId']), ('board', ''))
        self.assertEqual((out['mode2'], out['needId2']), ('need', 'need-0007'))
        self.assertIn('T', out['detail'])

    def test_approval_page(self):
        out = self.run_page('approval')
        self.assertEqual(out['radios'], ['opt:0', 'opt:1', 'opt:2'])
        self.assertEqual(out['checked'], 0)
        self.assertNotIn('其余按建议', out['buttons'])
        self.assertNotIn('其余按建议', out['text'])
        self.assertIn('审批', out['text'])
        self.assertTrue(out['textarea'])
        self.assertEqual(out['postsWithoutChoice'], 0)
        self.assertEqual(out['postsCEmpty'], 0)
        self.assertIn('C. 需要先改', out['msgCEmpty'])
        self.assertEqual(len(out['posts']), 1)
        self.assertEqual(out['posts'][0]['body']['slots'], {'Q1': {'type': 'option', 'choice': 'C. 需要先改', 'text': '先补测试'}})
        self.assertIn('#need:need-0007', out['mainLinks'])
        for href in ('#demo-0001', '#demo-0002', '#demo-0003'):
            self.assertIn(href, out['mainLinks'])

    def test_tasks_filter_by_need(self):
        out = self.run_page('tasks-filter')
        self.assertEqual((out['mode'], out['filter']), ('tasks', 'need-0007'))
        self.assertIn('只看需求 need-0007', out['text'])
        self.assertIn('T30', out['text'])
        self.assertNotIn('T40', out['text'])
        self.assertIn('待合并检查', out['text'])
        self.assertIn('审查和授权需在 PR 上核对', out['text'])
        self.assertIn('按需求', out['allText'])
        self.assertIn('T40', out['allText'])
        self.assertEqual(out['posts'], 0)


class RouterDocTest(RouterBase):
    def test_router_plist_template_renders(self):
        import plistlib
        tpl = (ROOT_DIR / 'skills' / 'dev' / 'router' / 'local.herdr-router.plist.template').read_text()
        text = tpl.replace('__HERDR__', '/opt/demo/herdr').replace('__HOME__', '/Users/demo')
        pl = plistlib.loads(text.encode())
        self.assertEqual(pl['ProgramArguments'], ['/opt/demo/herdr', '--session', 'router', 'server'])
        self.assertTrue(pl['KeepAlive'])
        self.assertNotIn('__', text)

    def test_need_cli_help_and_doctor(self):
        nid = self.new_need()
        self.assertIn(nid, self.q('need', 'list', '--todo').stdout)
        self.assertEqual(self.q('need', 'show', nid).returncode, 0)
        self.assertIn('登记：无', self.q('router', 'status').stdout)
        self.assertEqual(self.q('doctor').returncode, 0)


ROOT_DIR = base.ROOT

if __name__ == '__main__':
    unittest.main()
