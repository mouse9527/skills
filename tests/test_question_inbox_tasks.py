"""skills/dev/question-inbox 只读任务视图：stage、父子、失败保留、题挂任务、herdr 对应、转义、Host 校验、无写请求。

假 gh（GH_BIN_PATH 注入，返回固定 GraphQL JSON）+ 假 herdr；只用虚构数据：ws `demo`、repo `owner/repo`。
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_question_inbox as base  # noqa: E402

QI = base.QI
FAKE_GH = Path(__file__).resolve().parent / 'question_inbox_fake_gh.py'
qstore = base.qstore
tasks = base._load('tasks')

NOW = datetime.now(qstore.TZ)


def ts(**delta):
    return (NOW - timedelta(**delta)).astimezone(qstore.TZ).isoformat(timespec='seconds')


def pr(n, state='OPEN', checks='SUCCESS', body='', draft=False, repo='owner/repo', closing=(), updated=None):
    return {'number': n, 'title': f'PR {n}', 'url': f'https://github.com/{repo}/pull/{n}', 'state': state,
            'isDraft': draft, 'updatedAt': updated or ts(days=10), 'mergedAt': None, 'body': body,
            'author': {'login': 'dev'}, 'repository': {'nameWithOwner': repo},
            'statusCheckRollup': {'state': checks} if checks else None,
            'closingIssuesReferences': {'nodes': [{'number': c, 'repository': {'nameWithOwner': 'owner/repo'}} for c in closing]}}


def issue(n, title=None, updated=None, labels=(), parent=None, sub=None, state='OPEN', reason=None, closed=None,
          refs=(), url=None):
    node = {'number': n, 'title': title or f'任务 {n}', 'url': url or f'https://github.com/owner/repo/issues/{n}',
            'state': state, 'stateReason': reason, 'updatedAt': updated or ts(days=10), 'closedAt': closed,
            'labels': {'nodes': [{'name': l} for l in labels]},
            'parent': {'number': parent, 'repository': {'nameWithOwner': 'owner/repo'}} if parent else None,
            'subIssuesSummary': {'total': sub[1], 'completed': sub[0]} if sub else {'total': 0, 'completed': 0},
            'closedByPullRequestsReferences': {'nodes': []}}
    if state == 'OPEN':
        node['timelineItems'] = {'nodes': [{'source': p} for p in refs]}
    return node


def response(open_issues, closed=(), open_prs=()):
    return {'data': {'repository': {'issues': {'totalCount': len(open_issues), 'nodes': list(open_issues)},
                                    'pullRequests': {'totalCount': len(open_prs), 'nodes': list(open_prs)}},
                     'closed': {'issueCount': len(closed), 'nodes': list(closed)}}}


def fixture():
    """每个 stage 至少一个；号即用途。"""
    return response([
        issue(1),                                                         # 等你回答（题挂上）
        issue(2, refs=[pr(102, body='Part of owner/repo#2')]),            # 等你合并：open PR checks 绿
        issue(3, refs=[pr(103, state='MERGED', body='Part of #3')]),      # 等你验收：合并了、Issue 未关
        issue(4),                                                         # 卡住：agent blocked（cwd）
        issue(5, refs=[pr(105, checks='FAILURE', closing=[5])]),          # 卡住：PR check 失败（GitHub 关联）
        issue(6),                                                         # 在做：agent（名字）
        issue(7, updated=ts(hours=2)),                                    # 在做：72h 内 Issue 更新
        issue(8),                                                         # 待开工
        issue(9, labels=['on-hold']),                                     # 停着
        issue(10, title='父任务', sub=(3, 3)),                             # 父：子 Issue 全关但自己没关
        issue(11, parent=10, updated=ts(hours=1)),                        # 子：在做，父不在同组
        issue(12, parent=10),                                             # 子：待开工
        issue(13, refs=[pr(113, body='Relates to owner/repo#13')]),       # 只是提到，不算关联 PR → 待开工
        issue(14, title='<script>alert(1)</script>', url='javascript:alert(1)'),
    ], closed=[issue(20, state='CLOSED', reason='COMPLETED', closed=ts(days=1)),
               issue(21, state='CLOSED', reason='NOT_PLANNED', closed=ts(days=2))])


class TasksBase(base.Base):
    def setUp(self):
        super().setUp()
        fake = self.tmp / 'bin' / 'gh'
        fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
        fake.chmod(0o755)
        self.cfg = self.tmp / 'tasks.json'
        self.cfg.write_text(json.dumps({'repos': [{'ws': 'demo', 'repo': 'owner/repo'}]}))
        extra = {'GH_BIN_PATH': str(fake), 'FAKE_GH_STATE': str(self.tmp / 'gh.json'),
                 'FAKE_GH_LOG': str(self.tmp / 'gh.log'), 'QI_TASKS_CONFIG': str(self.cfg), 'QI_GH_TIMEOUT': '5'}
        self.env.update(extra)
        os.environ.update(extra)
        (self.tmp / 'gh.log').write_text('')
        self.set_gh(fixture())
        ws = self.tmp / 'ws' / 'demo'
        self.set_herdr(panes={'demo': [
            base.pane('w1:p1'),  # lead 自己，cwd /tmp，对不上任何任务
            dict(base.pane('w1:p2', lead='x'), status='', agent_status='blocked', cwd=str(ws / 'issues' / '4' / 'repo')),
            dict(base.pane('w1:p3', lead='y'), name='w-6', cwd='/tmp'),
            dict(base.pane('w1:p4', lead='z'), cwd=str(ws / 'stories' / '6' / 'review')),
        ]})
        tasks.CACHE = tasks.Cache()

    def set_gh(self, resp):
        (self.tmp / 'gh.json').write_text(json.dumps({'repos': {'owner/repo': resp}}, ensure_ascii=False))

    def gh_calls(self):
        return [json.loads(l) for l in (self.tmp / 'gh.log').read_text().splitlines()]

    def flat(self, v):
        return {t['number']: t for g in v['groups'] for w in g['ws'] for t in w['tasks'] if t['kind'] == 'task'}

    def ask_issue(self, issue_ref, title='挂到任务上'):
        f = self.tmp / f'b-{title}.md'
        f.write_text(base.body())
        r = self.q('ask', '--body', str(f), '--title', title, '--issue', issue_ref, '--blocking', 'yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split('已发布 ')[1].split('：')[0]


class StageTest(TasksBase):
    def test_stages_and_reasons(self):
        qid = self.ask_issue('owner/repo#1')
        v = tasks.view(refresh=True)
        t = self.flat(v)
        want = {1: 'answer', 2: 'merge', 3: 'accept', 4: 'stuck', 5: 'stuck', 6: 'doing', 7: 'doing', 8: 'todo',
                9: 'hold', 13: 'todo', 20: 'done', 21: 'dropped'}
        self.assertEqual({n: t[n]['stage']['key'] for n in want}, want)
        for n in want:
            self.assertTrue(t[n]['why'][0], n)  # 每个 stage 都附依据
        self.assertIn(qid, t[1]['why'][0])
        self.assertIn('PR #102', t[2]['why'][0])
        self.assertIn('已合并', t[3]['why'][0])
        self.assertIn('blocked', t[4]['why'][0])
        self.assertIn('checks 失败', t[5]['why'][0])
        self.assertIn('动过', t[7]['why'][0])
        self.assertEqual([g['key'] for g in v['groups']], ['you', 'stuck', 'doing', 'todo', 'hold', 'closed'])
        self.assertEqual(t[13]['prs'], [])  # 「Relates to」不是关联

    def test_empty_groups_hidden(self):
        self.set_gh(response([issue(8)]))
        v = tasks.view(refresh=True)
        self.assertEqual([g['key'] for g in v['groups']], ['todo'])

    def test_parent_progress_not_done(self):
        v = tasks.view(refresh=True)
        t = self.flat(v)
        self.assertEqual(t[10]['sub'], {'total': 3, 'completed': 3})
        self.assertNotIn(t[10]['stage']['key'], ('done', 'dropped'))  # 子 Issue 全关 ≠ 父完成
        todo = next(g for g in v['groups'] if g['key'] == 'todo')['ws'][0]['tasks']
        i = next(i for i, r in enumerate(todo) if r['kind'] == 'task' and r['number'] == 10)
        self.assertEqual((todo[i + 1]['number'], todo[i + 1]['depth']), (12, 1))  # 同组：子缩进在父下面
        doing = next(g for g in v['groups'] if g['key'] == 'doing')['ws'][0]['tasks']
        j = next(i for i, r in enumerate(doing) if r['kind'] == 'task' and r['number'] == 11)
        self.assertEqual((doing[j - 1]['kind'], doing[j - 1]['number'], doing[j]['depth']), ('parent', 10, 1))
        self.assertIn('3/3', tasks.render_text(v))


class FailureAndCacheTest(TasksBase):
    def test_gh_failure_keeps_last_data_and_shows_error(self):
        first = tasks.view(refresh=True)
        at = first['sources'][0]['at']
        self.set_gh('fail:HTTP 502 Bad Gateway')
        v = tasks.view(refresh=True)
        src = v['sources'][0]
        self.assertEqual((src['ok'], src['has_data'], src['at']), (False, True, at))
        self.assertIn('502', src['error'])
        self.assertEqual(set(self.flat(v)), set(self.flat(first)))  # 没有被空列表替换
        self.assertIn('无法刷新 owner/repo', tasks.render_text(v))

    def test_never_succeeded_is_not_no_tasks(self):
        self.set_gh('fail:gh auth required')
        v = tasks.view(refresh=True)
        self.assertEqual((v['sources'][0]['has_data'], v['stale']), (False, True))
        self.assertIn('不代表没有任务', tasks.render_text(v))
        r = self.q('tasks')
        self.assertEqual(r.returncode, 1)

    def test_herdr_failure_keeps_last_agents(self):
        at = tasks.view(refresh=True)['sources'][-1]['at']
        self.set_herdr(panes={'demo': [base.pane()]}, prompt='ok')
        (self.tmp / 'herdr.json').write_text('{"panes": {"demo": [}')  # 假 herdr 崩溃：输出无法解析
        v = tasks.view(refresh=True)
        h = v['sources'][-1]
        self.assertEqual((h['ok'], h['has_data'], h['at']), (False, True, at))
        self.assertIn('herdr', h['error'])
        self.assertEqual({a['pane'] for a in self.flat(v)[6]['agents']}, {'w1:p3', 'w1:p4'})  # 仍是上次的
        self.assertEqual(self.flat(v)[4]['stage']['key'], 'stuck')

    def test_ttl_cache_and_refresh(self):
        tasks.view(refresh=False)
        tasks.view(refresh=False)
        self.assertEqual(len(self.gh_calls()), 1)
        tasks.view(refresh=True)
        self.assertEqual(len(self.gh_calls()), 2)
        later = datetime.now(qstore.TZ) + timedelta(seconds=tasks.TTL + 1)
        tasks.view(now=later)
        self.assertEqual(len(self.gh_calls()), 3)

    def test_gh_called_by_argv_read_only(self):
        tasks.view(refresh=True)
        argv = self.gh_calls()[0]
        self.assertEqual(argv[:2], ['api', 'graphql'])
        q = next(a for a in argv if a.startswith('query='))
        self.assertNotIn('mutation', q)
        self.assertIn('owner=owner', argv)

    def test_bad_config(self):
        self.cfg.write_text('{"repos":[{"ws":"demo","repo":"not a repo; rm -rf"}]}')
        v = tasks.view(refresh=True)
        self.assertIn('不合法', v['config_error'])
        self.assertEqual(self.gh_calls(), [])


class LinkTest(TasksBase):
    def test_questions_attach_to_tasks_or_unlinked(self):
        a = self.ask_issue('owner/repo#8', '挂 8')
        b = self.ask_issue('none', '没有 Issue')
        c = self.ask_issue('other/repo#1', '对不上')
        d = self.ask_issue('OWNER/Repo#20', '挂到已关闭的')
        v = tasks.view(refresh=True)
        t = self.flat(v)
        self.assertEqual([q['id'] for q in t[8]['questions']], [a])
        self.assertEqual(t[8]['stage']['key'], 'answer')
        self.assertEqual(t[20]['stage']['key'], 'done')  # 只有 closed 才显示完成；题作小标签
        self.assertEqual([x['key'] for x in t[20]['tags']], ['answer'])
        self.assertEqual({q['id'] for q in v['unlinked']}, {b, c})
        self.assertNotIn(d, {q['id'] for q in v['unlinked']})

    def test_herdr_by_name_and_cwd(self):
        t = self.flat(tasks.view(refresh=True))
        self.assertEqual({(a['pane'], a['by']) for a in t[6]['agents']}, {('w1:p3', 'name'), ('w1:p4', 'cwd')})
        self.assertEqual([(a['pane'], a['by'], a['status']) for a in t[4]['agents']], [('w1:p2', 'cwd', 'blocked')])
        self.assertEqual(t[8]['agents'], [])

    def test_missing_socket_is_skipped_without_herdr_calls(self):
        shutil.rmtree(self.tmp / 'sock' / 'demo')
        v = tasks.view(refresh=True)
        self.assertEqual(self.herdr_calls('pane'), [])
        self.assertTrue(v['sources'][-1]['ok'])
        self.assertFalse((self.tmp / 'sock' / 'demo').exists())  # 不创建 session

    def test_links_only_github(self):
        t = self.flat(tasks.view(refresh=True))
        self.assertEqual(t[14]['url'], '')
        self.assertTrue(t[2]['url'].startswith('https://github.com/'))


class HttpTest(TasksBase):
    def test_api_tasks_host_check_and_no_writes(self):
        qid = self.ask_issue('owner/repo#1')
        before = {p.name: p.read_bytes() for p in (self.tmp / 'inbox').iterdir()}
        self.start_server()
        code, res, _ = self.http('GET', '/api/tasks', headers={'Host': 'evil.example:80'})
        self.assertEqual(code, 403)
        code, res, _ = self.http('GET', '/api/tasks', headers={'Host': f'127.0.0.1:{self.port}', 'Origin': 'http://evil.example'})
        self.assertEqual(code, 403)
        code, res, _ = self.http('GET', '/api/tasks?refresh=1')
        self.assertEqual(code, 200)
        self.assertEqual(self.flat(res)[1]['questions'][0]['id'], qid)
        self.assertEqual(res['groups'][0]['ws'][0]['tasks'][0]['title'], '任务 1')
        t14 = self.flat(res)[14]
        self.assertEqual((t14['title'], t14['url']), ('<script>alert(1)</script>', ''))  # JSON 原样，页面按文字渲染
        code, _, _ = self.http('POST', '/api/tasks', {})
        self.assertEqual(code, 404)  # 没有写接口
        after = {p.name: p.read_bytes() for p in (self.tmp / 'inbox').iterdir()}
        self.assertEqual(before, after)

    def test_cli_tasks_json_and_text(self):
        r = self.q('tasks', '--json', '--ws', 'demo')
        self.assertEqual(r.returncode, 0, r.stderr)
        v = json.loads(r.stdout)
        self.assertEqual(self.flat(v)[2]['stage']['key'], 'merge')
        r = self.q('tasks')
        self.assertIn('## 等你', r.stdout)
        self.assertIn('owner/repo#2', r.stdout)
        self.assertIn('#tasks', r.stdout)

    def test_plist_has_gh_path(self):
        dt = base.DocAndSmallFixTest('test_skill_install_block_renders_plist')
        dt.tmp = self.tmp
        home = dt.fake_home()
        block = next(b for b in dt.skill_blocks() if 'plist.template' in b)
        script = '\n'.join(l for l in block.split('\n') if not l.startswith('launchctl'))
        env = {**os.environ, 'HOME': str(home), 'PATH': f'{self.tmp / "bin"}:{os.environ.get("PATH", "")}'}
        r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(home / 'Library' / 'LaunchAgents' / 'local.question-inbox.plist', 'rb') as f:
            pl = plistlib.load(f)
        self.assertEqual(pl['EnvironmentVariables']['GH_BIN_PATH'], str(self.tmp / 'bin' / 'gh'))


class ReviewFixTest(TasksBase):
    """审查 I1–I4 的验收。"""

    def two_sessions(self, demo_panes, other_panes, **extra):
        (self.tmp / 'sock' / 'other').mkdir(parents=True, exist_ok=True)
        (self.tmp / 'sock' / 'other' / 'herdr.sock').write_text('')
        self.set_herdr(panes={'demo': demo_panes, 'other': other_panes}, **extra)

    def ws_pane(self, pane_id, n, status='idle', agent='claude'):
        p = dict(base.pane(pane_id, lead=pane_id, agent=agent or 'claude', status=status),
                 cwd=str(self.tmp / 'ws' / 'demo' / 'issues' / str(n) / 'repo'))
        if not agent:
            p.pop('agent'), p.pop('agent_session')
            p['agent_status'] = None
        return p

    def test_i1_partial_herdr_failure_keeps_failed_session(self):
        self.two_sessions([self.ws_pane('w1:p7', 8, 'blocked')], [self.ws_pane('w2:p1', 12)])
        first = tasks.view(refresh=True)
        self.assertEqual(self.flat(first)[8]['stage']['key'], 'stuck')
        at = {s['name']: s['at'] for s in first['sources']}
        # 第二轮：demo 的 pane list 超时；other 正常且换了内容
        self.two_sessions([self.ws_pane('w1:p7', 8, 'blocked')], [self.ws_pane('w2:p1', 9)],
                          pane_list_fail={'demo': 'timeout'}, sleep=5)
        v = tasks.view(refresh=True)
        src = {s['name']: s for s in v['sources']}
        self.assertEqual((src['herdr demo']['ok'], src['herdr demo']['has_data'], src['herdr demo']['at']),
                         (False, True, at['herdr demo']))
        self.assertIn('超时', src['herdr demo']['error'])
        self.assertTrue(src['herdr other']['ok'])
        self.assertTrue(v['stale'])
        t = self.flat(v)
        self.assertEqual(t[8]['stage']['key'], 'stuck')                      # 旧的 blocked agent 还在
        self.assertEqual([a['pane'] for a in t[8]['agents']], ['w1:p7'])
        self.assertEqual([a['pane'] for a in t[9]['agents']], ['w2:p1'])     # 成功的 session 用新数据
        self.assertEqual(t[12]['agents'], [])
        self.assertIn('无法刷新 herdr demo', tasks.render_text(v))

    def test_i1_never_succeeded_session_is_unknown_not_no_agent(self):
        self.two_sessions([], [], pane_list_fail={'demo': 'error:internal'})
        v = tasks.view(refresh=True)
        t = self.flat(v)
        self.assertEqual(t[8]['stage']['key'], 'todo')
        self.assertIn('agent 情况待核', t[8]['why'][0])
        self.assertNotIn('没有 agent', t[8]['why'][0])
        self.assertTrue(v['stale'])

    def test_i2_only_explicit_success_is_green(self):
        self.set_gh(response([
            issue(1, refs=[pr(101, checks=None, body='Part of #1')]),
            issue(2, refs=[pr(102, checks='PENDING', body='Part of #2')]),
            issue(3, refs=[pr(103, checks='FAILURE', body='Part of #3')]),
            issue(4, refs=[pr(104, checks='SUCCESS', body='Part of #4')]),
            issue(5, refs=[pr(105, checks='SUCCESS', draft=True, body='Part of #5')]),
            issue(6, refs=[pr(106, checks='ERROR', body='Part of #6')]),
        ]))
        t = self.flat(tasks.view(refresh=True))
        self.assertEqual({n: t[n]['stage']['key'] for n in t},
                         {1: 'doing', 2: 'doing', 3: 'stuck', 4: 'merge', 5: 'doing', 6: 'stuck'})
        self.assertIn('尚无检查结果', t[1]['why'][0])
        self.assertIn('checks 运行中', t[2]['why'][0])
        self.assertIn('草稿', t[5]['why'][0])
        self.assertNotIn('merge', [x['key'] for n in (1, 2, 5) for x in t[n]['tags']])

    def test_i3_issue_side_native_link_beyond_pr_side_page(self):
        p = pr(101, checks='SUCCESS', closing=range(1, 11))  # PR 侧只返回前 10 个，没有 #11；正文没有 Part of
        p['closingIssuesReferences']['totalCount'] = 11
        i11 = issue(11)
        i11['closedByPullRequestsReferences'] = {'totalCount': 1, 'nodes': [p]}
        i12 = issue(12)  # Issue 侧原生关联也被截断
        i12['closedByPullRequestsReferences'] = {'totalCount': 12, 'nodes': [pr(120 + k, checks='FAILURE') for k in range(10)]}
        i13 = issue(13, refs=[pr(113, body='Relates to owner/repo#13；也提到 #13')])
        self.set_gh(response([i11, i12, i13]))
        v = tasks.view(refresh=True)
        t = self.flat(v)
        self.assertEqual([x['number'] for x in t[11]['prs']], [101])
        self.assertEqual(t[11]['stage']['key'], 'merge')
        self.assertEqual(len(t[12]['prs']), 10)
        self.assertEqual((t[13]['prs'], t[13]['stage']['key']), ([], 'todo'))  # 提及不算关联
        more = ' '.join(v['sources'][0]['more'])
        self.assertIn('Issue #12 的原生关联 PR 超过', more)
        self.assertIn('PR #101 关闭的 Issue 超过', more)

    def test_i3_timeline_truncation_is_reported(self):
        i = issue(1, refs=[pr(101, body='Part of #1')])
        i['timelineItems']['pageInfo'] = {'hasPreviousPage': True}
        self.set_gh(response([i]))
        self.assertIn('Issue #1 的 PR 交叉引用超过', ' '.join(tasks.view(refresh=True)['sources'][0]['more']))

    def test_i4_shell_pane_is_not_an_agent(self):
        self.set_herdr(panes={'demo': [self.ws_pane('w1:p8', 8, agent=None), self.ws_pane('w1:p9', 9, 'blocked')]})
        t = self.flat(tasks.view(refresh=True))
        self.assertEqual((t[8]['agents'], t[8]['stage']['key']), ([], 'todo'))
        self.assertEqual(([a['pane'] for a in t[9]['agents']], t[9]['stage']['key']), (['w1:p9'], 'stuck'))


# ---------- node：任务页渲染，切换后答题页的保护仍然有效 ----------

TASKS_VIEW = {
    'generated_at': '2026-11-03T10:00:00+08:00', 'stale': True, 'count': 3,
    'sources': [{'name': 'owner/repo', 'ws': 'demo', 'ok': False, 'error': 'gh 超时', 'at': '2026-11-03T09:59:00+08:00',
                 'more': ['open Issue 共 150 个，只显示最近更新的 100 个'], 'has_data': True},
                {'name': 'herdr', 'ws': '', 'ok': True, 'error': '', 'at': '2026-11-03T10:00:00+08:00', 'more': [], 'has_data': True}],
    'groups': [{'key': 'you', 'title': '等你', 'count': 1, 'ws': [{'ws': 'demo', 'tasks': [
        {'kind': 'task', 'depth': 0, 'ws': 'demo', 'repo': 'owner/repo', 'number': 1, 'title': '<script>evil()</script>',
         'url': 'javascript:alert(1)', 'stage': {'key': 'answer', 'label': '等你回答', 'group': 'you'},
         'why': ['题 demo-0001'], 'tags': [{'key': 'doing', 'label': '在做', 'why': 'w-1'}], 'prs': [], 'agents': [],
         'questions': [{'id': 'demo-0001', 'title': 'q'}], 'sub': None, 'updated': '2026-11-03T09:00:00+08:00', 'closed': ''}]}]},
        {'key': 'todo', 'title': '待开工', 'count': 2, 'ws': [{'ws': 'demo', 'tasks': [
            {'kind': 'parent', 'depth': 0, 'repo': 'owner/repo', 'number': 10, 'title': '父任务', 'url': 'https://github.com/owner/repo/issues/10',
             'stage': '在做', 'sub': {'total': 3, 'completed': 1}},
            {'kind': 'task', 'depth': 1, 'ws': 'demo', 'repo': 'owner/repo', 'number': 12, 'title': '子任务',
             'url': 'https://github.com/owner/repo/issues/12', 'stage': {'key': 'todo', 'label': '待开工', 'group': 'todo'},
             'why': ['没有 agent、没有 PR'], 'tags': [], 'agents': [], 'questions': [], 'sub': None,
             'prs': [{'number': 5, 'url': 'https://github.com/owner/repo/pull/5', 'state': 'MERGED', 'draft': False, 'checks': '',
                      'repo': 'owner/repo', 'full': 'owner/repo'}], 'updated': '2026-11-01T09:00:00+08:00', 'closed': ''}]}]}],
    'unlinked': [{'id': 'demo-0009', 'title': '<b>未关联</b>', 'ws': 'demo', 'issue': 'none', 'blocking': True, 'created': ''}],
}

TASKS_SCENARIO = r"""
const modes = mk('nav', 'modes'); doc.append(modes);
const TV = JSON.parse(process.argv[4]);
(async () => {
  const out = {};
  ctx.location.hash = '#demo-0001';
  run(src);
  const open = async d => { answer('/api/q/demo-0001', 200, d); await flush(); };
  const taskGets = () => requests.filter(r => r.path.startsWith('/api/tasks')).map(r => r.path);
  await open(mkDetail('demo-0001', 1));
  pick('Q1', 'opt:0');
  ctx.confirm = () => false;                       // 有未保存输入：取消离开
  ctx.location.hash = '#tasks'; listeners.hashchange(); await flush();
  out.cancel = {cur: run('cur'), mode: run('mode'), gets: taskGets().length, kept: run('checkedValue("Q1")'), hash: ctx.location.hash};
  ctx.confirm = () => true;
  ctx.location.hash = '#tasks'; listeners.hashchange(); await flush();
  out.view = app.attrs['data-view']; out.mode = run('mode');
  const html0 = htmlSets.length;
  answer('/api/tasks', 200, TV); await flush();
  out.text = text(main);
  out.htmlSets = htmlSets.length - html0;
  out.scripts = findAll(e => e.tagName === 'SCRIPT').length;
  out.controls = findAll(e => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName), main).length;
  out.buttons = findAll(e => e.tagName === 'BUTTON', main).map(text);
  out.hrefs = findAll(e => e.tagName === 'A', main).map(a => a.attrs.href);
  out.indent = findAll(e => /\btrow\b/.test(e.className), main).map(e => e.className);
  out.modes = text(modes);
  button('刷新', main).dispatch('click'); await flush();
  out.refreshing = {disabled: byId('tasks_refresh').disabled, paths: taskGets()};
  answer('/api/tasks?refresh=1', 500, {error: '任务视图出错'}); await flush();
  out.afterFail = text(main);
  out.postsOnTasks = posts().length;
  ctx.location.hash = '#demo-0001'; listeners.hashchange(); await flush();
  await open(mkDetail('demo-0001', 1));
  out.back = {view: app.attrs['data-view'], mode: run('mode'), form: formShown()};
  pick('Q1', 'opt:0');
  const go = byId('go'); go.dispatch('click'); go.dispatch('click'); key('Enter', {metaKey: true}); await flush();
  out.submit = {posts: posts().map(p => p.path), disabled: byId('go').disabled, label: text(byId('go'))};
  for (const r of requests) if (r.path === '/api/list' && !r.done) { r.done = true; r.resolve({status: 200, json: async () => EMPTY}); }
  await flush();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.stack); process.exit(2); });
"""


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class TasksPageTest(unittest.TestCase):
    def setUp(self):
        base.PageScriptTest.setUp(self)
        prefix = base.DOM_HARNESS[:base.DOM_HARNESS.index('(async () => {')]
        (self.tmp / 'harness.js').write_text(prefix + TASKS_SCENARIO, encoding='utf-8')

    def test_tasks_view_read_only_and_answer_guards_survive(self):
        out = base.PageScriptTest.run_page(self, 'tasks', json.dumps(TASKS_VIEW, ensure_ascii=False))
        self.assertEqual(out['cancel'], {'cur': 'demo-0001', 'mode': 'inbox', 'gets': 0, 'kept': 'opt:0', 'hash': '#demo-0001'})
        self.assertEqual((out['view'], out['mode']), ('tasks', 'tasks'))
        self.assertIn('<script>evil()</script>', out['text'])   # 文字，不是元素
        self.assertIn('<b>未关联</b>', out['text'])
        self.assertEqual((out['htmlSets'], out['scripts'], out['controls']), (0, 0, 0))
        self.assertEqual(out['buttons'], ['刷新'])
        self.assertTrue(all(h.startswith('#') or h.startswith('https://github.com/') for h in out['hrefs']), out['hrefs'])
        self.assertNotIn('javascript:alert(1)', out['hrefs'])
        self.assertIn('#demo-0001', out['hrefs'])
        self.assertIn('#demo-0009', out['hrefs'])
        for s in ('等你', '待开工', '无法刷新 owner/repo', '只显示最近更新的 100 个', '子 Issue 1/3 已关', '父 ', '依据：题 demo-0001', '（已合并）'):
            self.assertIn(s, out['text'])
        self.assertTrue(any('d1' in c for c in out['indent']))
        self.assertIn('任务', out['modes'])
        self.assertEqual(out['refreshing']['disabled'], True)
        self.assertEqual(out['refreshing']['paths'], ['/api/tasks', '/api/tasks?refresh=1'])
        self.assertIn('无法刷新：任务视图出错', out['afterFail'])
        self.assertIn('<script>evil()</script>', out['afterFail'])  # 失败后保留上一次的数据
        self.assertEqual(out['postsOnTasks'], 0)
        self.assertEqual(out['back'], {'view': 'detail', 'mode': 'inbox', 'form': True})
        self.assertEqual(out['submit']['posts'], ['/api/q/demo-0001/submit'])  # 连点只发一次
        self.assertEqual((out['submit']['disabled'], out['submit']['label']), (True, '提交中…'))


if __name__ == '__main__':
    unittest.main()
