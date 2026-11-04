"""7788 工作台 v2（skills#23）：修订 1 的两条发言路径、评论轮询/回执/接手、看板分列、安全与循环刹车。

假 gh（tests/question_inbox_fake_gh.py，支持 comments GET 分页 / since / POST / 丢失响应）
+ 假 herdr；虚构数据：ws `demo`、repo `owner/repo`、Owner 登录名 `demo-owner`。
"""
import json
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_question_inbox as base  # noqa: E402
import test_question_inbox_router as rbase  # noqa: E402
import test_question_inbox_tasks as tbase  # noqa: E402

qstore = base.qstore
server = base.server
board = server.board
poll = server.poll
ghapi = server.ghapi
send = server.send
routerctl = server.routerctl
tasks = tbase.tasks

ISSUE = rbase.ISSUE


def ts(delta=None):
    from datetime import datetime
    return (datetime.now(qstore.TZ) - (delta or timedelta())).isoformat(timespec='seconds')


class V2Base(rbase.RouterBase):
    def setUp(self):
        super().setUp()
        self.env['QI_OWNER_LOGIN'] = 'demo-owner'
        os.environ['QI_OWNER_LOGIN'] = 'demo-owner'
        ghapi.WRITE_INTERVAL = 0
        ghapi.PER_NEED_PER_MIN = 20
        board.TREE.clear()
        poll.clear_cache()
        self.set_gh(login='demo-owner', comments={}, board={}, post='ok', next_comment_id=1000)

    def set_gh(self, **kw):
        data = self.gh_state()
        data.update(kw)
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def gh_state(self):
        return json.loads((self.tmp / 'gh.json').read_text())

    def add_comment(self, number, cid, body, author='dev', updated=None, created=None):
        data = self.gh_state()
        now = updated or created or ts()
        data.setdefault('comments', {}).setdefault('owner/repo', []).append(
            {'id': cid, 'body': body, 'user': {'login': author}, 'created_at': created or now,
             'updated_at': now, 'issue_url': f'https://api.github.com/repos/owner/repo/issues/{number}',
             'html_url': f'https://github.com/owner/repo/issues/{number}#issuecomment-{cid}'})
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def edit_comment(self, cid, body, updated=None):
        data = self.gh_state()
        for c in data.get('comments', {}).get('owner/repo', []):
            if c['id'] == cid:
                c['body'] = body
                c['updated_at'] = updated or ts()
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def set_tree(self, number, root_state='OPEN', subs=(), prs=(), title='根需求', updated=None, body=None, created=None):
        def node(n, state='OPEN', reason=None, closed=None, title_=None):
            return {'number': n, 'title': title_ or f'#{n}', 'url': f'https://github.com/owner/repo/issues/{n}',
                    'state': state, 'stateReason': reason, 'closedAt': closed, 'createdAt': created or ts(), 'updatedAt': updated or ts(),
                    'body': f'需求：need-0001\n\n{body or "正文"}', 'labels': {'nodes': []}}
        def pr(n, state='OPEN', draft=False, review='', checks='SUCCESS', merged=None, updated_=None):
            return {'number': n, 'title': f'PR {n}', 'url': f'https://github.com/owner/repo/pull/{n}',
                    'state': state, 'isDraft': draft, 'mergedAt': merged, 'updatedAt': updated_ or ts(),
                    'reviewDecision': review, 'statusCheckRollup': {'state': checks} if checks else None,
                    'repository': {'nameWithOwner': 'owner/repo'}}
        root = node(number, root_state)
        root['subIssues'] = {'nodes': [node(n, st, reason) for n, st, reason in
                                      [(s[0], s[1], s[2] if len(s) > 2 else None) for s in subs]]}
        root['timelineItems'] = {'nodes': [{'source': pr(*p)} for p in prs]}
        root['closedByPullRequestsReferences'] = {'nodes': []}
        resp = {'data': {'repository': {'issue': root}}}
        data = self.gh_state()
        data.setdefault('board', {})[f'owner/repo#{number}'] = resp
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def filed(self, text='给 demo 加 CSV 导出', number=30, with_card=False):
        nid = self.assigned(text)
        self.as_lead('need', 'claim', nid)
        if with_card:
            self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
            qstore.owner_confirm(nid, 1)
        self.issue_bodies(**{str(number): f'需求：{nid}\n\n## 验收标准\n…'})
        r = self.as_lead('need', 'link', nid, '--issue', ISSUE + str(number))
        self.assertEqual(r.returncode, 0, r.stderr)
        return nid

    def as_ident(self, pane):
        return qstore.identify('demo', pane)

    def deliver_now(self, nid):
        rec = qstore.read_need(nid)
        server.scan_lead_needs([rec])


# ---------- 修订 1：默认 7788，勾选才进 Issue ----------

class V2DiscussionPathsTest(V2Base):
    def test_default_stays_in_7788_and_toggle_writes_github(self):
        nid = self.filed(with_card=True)
        before = len(self.gh_state()['comments']['owner/repo'])
        r = send.owner_message(nid, '我们先随便聊聊，用 CSV 还是 TSV？', 'req-chat-0001', False)
        self.assertFalse(r['to_issue'])
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), before)  # 没写 GitHub
        rec = qstore.read_need(nid)
        self.assertEqual(rec['thread'][-1]['text'], '我们先随便聊聊，用 CSV 还是 TSV？')
        self.assertEqual(rec['thread'][-1]['kind'], 'reply')
        self.assertEqual((rec['delivery'], rec['delivery_kind']), ('pending', 'reply'))
        r = send.owner_message(nid, '结论：先做 CSV，分号以后再说。', 'req-issue-0001', True)
        self.assertTrue(r['to_issue'])
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), before + 1)
        rec = qstore.read_need(nid)
        self.assertTrue(rec['own_comments'])
        self.assertEqual(rec['comments'][-1]['state'], 'pending')
        d = server.need_detail(nid)
        srcs = [x.get('source') for x in d['timeline'] if x['kind'] == 'comment']
        self.assertIn('Issue 评论 · 经 7788', srcs)
        self.assertIn('结论：先做 CSV', json.dumps(d, ensure_ascii=False))

    def test_same_request_id_same_body_once_different_body_409(self):
        nid = self.filed()
        before = len(self.gh_state()['comments']['owner/repo'])
        a = send.owner_message(nid, '第一版结论', 'req-dup-0001', True)
        b = send.owner_message(nid, '第一版结论', 'req-dup-0001', True)
        self.assertEqual(a['comment_id'], b['comment_id'])
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), before + 1)
        with self.assertRaises(qstore.QError) as e:
            send.owner_message(nid, '换了内容', 'req-dup-0001', True)
        self.assertEqual(e.exception.code, 409)

    def test_lost_response_reconciles_without_second_comment(self):
        nid = self.filed()
        before = len(self.gh_state()['comments']['owner/repo'])
        self.set_gh(post='lost')
        with self.assertRaises(qstore.QError):
            send.owner_message(nid, '会丢回包的结论', 'req-lost-0001', True)
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), before + 1)  # 远端其实成功了
        rec = qstore.read_need(nid)
        self.assertEqual(rec['send_intents']['req-lost-0001']['state'], 'uncertain')
        self.set_gh(post='ok')
        got = send.owner_message(nid, '会丢回包的结论', 'req-lost-0001', True)  # 对账，不重发
        self.assertEqual(got['comment_id'], self.gh_state()['comments']['owner/repo'][-1]['id'])
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), before + 1)

    def test_unknown_result_without_tag_is_not_resent(self):
        nid = self.filed()
        self.set_gh(post='lost')
        with self.assertRaises(qstore.QError):
            send.owner_message(nid, '结果不明', 'req-lost-0002', True)
        # 模拟本地记录丢了、远端也找不到：同一 request_id 再发只会报「先核对」，不会创建第二条
        data = self.gh_state()
        data['comments']['owner/repo'] = []
        (self.tmp / 'gh.json').write_text(json.dumps(data))
        with self.assertRaises(qstore.QError) as e:
            send.owner_message(nid, '结果不明', 'req-lost-0002', True)
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(len(self.gh_state()['comments']['owner/repo']), 0)


# ---------- 轮询：分页 / 同秒 / 编辑 / 漏走入口 ----------

class V2PollTest(V2Base):
    def test_pagination_same_second_and_edit_redelivers(self):
        nid = self.filed(number=30)
        same = ts()
        for i in range(150):
            self.add_comment(30, 2000 + i, f'评论 {i}', author='teammate', updated=same)
        out = poll.scan()
        self.assertTrue(out['owner/repo']['ok'])
        rec = qstore.read_need(nid)
        self.assertEqual(len([c for c in rec['comments'] if c['id'] >= 2000]), 150)  # 翻完了所有页（排除 link 评论）
        self.assertEqual(rec['delivery_kind'], 'comments')
        self.assertTrue(rec['delivery'])
        self.edit_comment(2007, '评论 7 编辑过（结论变了）', updated=ts(timedelta(seconds=-2)))  # 明确晚于创建时间
        poll.clear_cache()
        poll.scan()
        rec = qstore.read_need(nid)
        row = next(c for c in rec['comments'] if c['id'] == 2007)
        self.assertEqual(row['state'], 'pending')  # 编辑当「更新」再投一次
        self.assertTrue(row['edited'])
        self.assertEqual(rec['delivery'], 'pending')
        inbox = qstore.lead_inbox(nid, [], self.as_ident('w1:p1'), poll.body_fetcher)
        self.assertIn('编辑过', json.dumps(inbox, ensure_ascii=False))

    def test_own_comment_is_not_delivered_back(self):
        nid = self.filed()
        send.lead_comment(nid, 'lead 的结论', self.as_ident('w1:p1'))
        with qstore.locked():
            cur = qstore.read_need(nid)
            cur['delivery'] = 'received'
            cur['delivery_kind'] = ''
            qstore._save_need(cur, '测试前清掉投递')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(rec['comments'][0]['state'], 'own')
        self.assertNotEqual(rec.get('delivery_kind'), 'comments')

    def test_unverified_owner_comment_is_delivered(self):
        nid = self.filed()
        self.add_comment(30, 3001, '我在 GitHub 上直接写的', author='demo-owner')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(next(c for c in rec['comments'] if c['id'] == 3001)['state'], 'pending')
        self.assertEqual((rec['delivery'], rec['delivery_kind']), ('pending', 'comments'))

    def test_three_rounds_of_own_echo_trigger_loop_brake(self):
        # 带本需求尾标、却对不上任何发送意图的评论 = 我们自己发的被当成新消息投回；连续 3 轮才刹车
        nid = self.filed()
        for i in range(3):
            self.add_comment(30, 4000 + i, f'自己发的被投回 {i}\n\n<!-- qi:{nid} i-{"ab" * 16} -->', author='demo-owner')
            poll.scan()
            rec = qstore.read_need(nid)
            if rec.get('delivery') in ('pending', 'held', 'sending'):
                with qstore.locked():
                    cur = qstore.read_need(nid)
                    cur['delivery'] = 'received'
                    qstore._save_need(cur, '模拟 lead 已读')
        rec = qstore.read_need(nid)
        self.assertTrue(rec['loop_suspect'])
        self.add_comment(30, 4009, '第四条（人写的）', author='demo-owner')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(rec['comments'][-1]['state'], 'held')
        self.assertNotEqual(rec.get('delivery'), 'pending')
        col, flags = board.column(rec, {'root': {'state': 'OPEN'}, 'subs': [], 'prs': []},
                                  {'lead': 'present', 'questions': []})
        self.assertEqual(col, 'stuck')
        self.assertIn('疑似循环', json.dumps(flags, ensure_ascii=False))


# ---------- 送审前修正：列顺序 / 原话评论 / 回填水位 / 噪音 ----------

class V2Fix1Test(V2Base):
    def test_board_display_order_is_wait_first_done_last(self):
        self.assertEqual([k for k, _ in board.COLUMNS], ['wait', 'stuck', 'todo', 'review', 'doing', 'done'])
        v = board.view()
        self.assertEqual([c['key'] for c in v['columns']], ['wait', 'stuck', 'todo', 'review', 'doing', 'done'])

    def test_link_without_card_posts_original_words_comment(self):
        nid = self.filed('直接立项的原话：给 demo 加 CSV 导出')
        bodies = [c['body'] for c in self.gh_state()['comments']['owner/repo']]
        hit = next(b for b in bodies if '按原话立项' in b)
        self.assertIn('直接立项的原话：给 demo 加 CSV 导出', hit)
        self.assertIn(f'#need:{nid}', hit)

    def test_first_link_seeds_comment_watermark_from_root_created(self):
        nid = self.assigned('回填水位')
        self.as_lead('need', 'claim', nid)
        created = ts(timedelta(days=3))
        self.set_tree(30, created=created)
        self.issue_bodies(**{'30': f'需求：{nid}'})
        r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '30')
        self.assertEqual(r.returncode, 0, r.stderr)
        since = poll.scan_state()['owner/repo']['since']
        self.assertEqual(qstore.parse_time(since), qstore.parse_time(created))

    def test_detail_omits_delivery_bookkeeping_and_keeps_problem_events(self):
        rec, _ = qstore.create_need('噪音测试')
        with qstore.locked():
            cur = qstore.read_need(rec['id'])
            cur['log'] = [{'at': ts(), 'text': '开始投递（attempt abc）'}, {'at': ts(), 'text': '投递暂缓：lead 忙'}]
            cur['delivery'] = 'pending'
            cur['delivery_kind'] = 'takeover'
            qstore._save_need(cur, '测试日志')
        d = server.need_detail(rec['id'])
        raw = json.dumps(d, ensure_ascii=False)
        self.assertNotIn('开始投递', raw)
        self.assertNotIn('投递暂缓', raw)
        self.assertIn('负责 lead 不在，等待接手', raw)


# ---------- 立项后 lead 掉线：接手 / 只读一次 / 旧 ack ----------

class V2TakeoverTest(V2Base):
    def test_owner_message_after_lead_gone_wakes_router_and_new_lead_reads_once(self):
        nid = self.filed()
        self.add_comment(30, 5001, '团队的问题', author='teammate')
        poll.scan()
        self.lead_panes(gone=('w1:p1',))  # 原 lead-a 退出，lead-b 还在
        qstore.owner_reply(nid, '新补充', 'req-after-gone')
        self.deliver_now(nid)  # 投给负责 lead 失败，确认不在 → 叫 Router 安排接手
        rec = qstore.read_need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_kind']), ('pending', 'takeover'))
        ident_b = self.as_ident('w1:p2')
        qstore.claim_need(nid, ident_b, takeover=True)  # 新 lead 接手
        self.assertEqual(qstore.read_need(nid)['lead']['pane_id'], 'w1:p2')
        inbox = qstore.lead_inbox(nid, [], ident_b, poll.body_fetcher)
        self.assertEqual([c['id'] for c in inbox['comments']], [5001])
        self.assertEqual([x['text'] for x in inbox['thread']], ['新补充'])
        qstore.lead_inbox(nid, [c['ack'] for c in inbox['comments']] + [x['ack'] for x in inbox['thread']], ident_b, poll.body_fetcher)
        again = qstore.lead_inbox(nid, [], ident_b, poll.body_fetcher)
        self.assertEqual((again['comments'], again['thread']), ([], []))  # 恰好读一次
        # 旧 lead 迟到的 ack：身份不符，不能吞掉新 lead 的未读
        self.add_comment(30, 5002, '又一条', author='teammate')
        poll.scan()
        tok = qstore.comment_ack(next(c for c in qstore.read_need(nid)['comments'] if c['id'] == 5002))
        with self.assertRaises(qstore.QError) as e:
            qstore.lead_inbox(nid, [tok], self.as_ident('w1:p1'), poll.body_fetcher)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual([c['id'] for c in qstore.lead_inbox(nid, [], ident_b, poll.body_fetcher)['comments']], [5002])

    def test_unknown_or_paused_does_not_start_takeover(self):
        nid = self.filed()
        self.lead_panes(gone=('w1:p1',))
        self.patch_herdr(panes={'demo': [{'agent': 'claude', 'pane_id': 'x1', 'cwd': str(self.tmp / 'ws' / 'demo'),
                                          'name': 'lead-demo'}]})  # 坏结构：没有 agent_session
        with self.assertRaises(qstore.QError) as e:
            qstore.claim_need(nid, self.as_ident('w1:p2'), takeover=True)
        self.assertEqual(e.exception.code, 403)
        self.lead_panes(gone=('w1:p1',))
        qstore.set_paused(True)
        with self.assertRaises(qstore.QError) as e:
            routerctl.takeover(nid)
        self.assertEqual(e.exception.code, 409)


# ---------- 看板：恰好一列 ----------

class V2BoardColumnTest(V2Base):
    def need(self, **kw):
        rec = {'id': 'need-0001', 'status': 'filed', 'ws': 'demo', 'lead': {'pane_id': 'p'}, 'discuss': '',
               'card_rev': 0, 'comments': [], 'loop_suspect': False, 'last_error': '', 'text': 't', 'created': ts()}
        rec.update(kw)
        return rec

    def tree(self, state='OPEN', subs=(), prs=()):
        return {'root': {'state': state, 'state_reason': 'COMPLETED' if state == 'CLOSED' else '', 'number': 1,
                         'title': 'T', 'url': '', 'updated': ts(),
                         'closed': ts(timedelta(days=8)) if state == 'CLOSED' else ''},
                'subs': [{'state': s, 'state_reason': r, 'number': n} for n, s, r in subs], 'prs': list(prs)}

    def pr(self, state='OPEN', draft=False, review='', checks='SUCCESS', updated=None):
        return {'number': 9, 'state': state, 'draft': draft, 'review': review, 'checks': checks,
                'updated': updated or ts(), 'title': 'PR', 'url': ''}

    def test_every_open_need_lands_in_exactly_one_column(self):
        cases = [
            ('todo-new', self.need(status='new', lead={}), None, {}, 'todo'),
            ('doing', self.need(), self.tree(), {'lead': 'present'}, 'doing'),
            ('wait-question', self.need(), self.tree(), {'lead': 'present', 'questions': [{'open': True, 'kind': 'approval'}]}, 'wait'),
            ('wait-card', self.need(discuss='waiting_owner', card_rev=2), self.tree(), {'lead': 'present'}, 'wait'),
            ('wait-approved', self.need(), self.tree(prs=[self.pr(review='APPROVED')]), {'lead': 'present'}, 'wait'),
            ('stuck-held', self.need(status='held', last_error='找不到路'), self.tree(), {'lead': 'present'}, 'stuck'),
            ('stuck-gone', self.need(), self.tree(), {'lead': 'absent'}, 'stuck'),
            ('stuck-check', self.need(), self.tree(prs=[self.pr(checks='FAILURE')]), {'lead': 'present'}, 'stuck'),
            ('stuck-stale', self.need(), self.tree(prs=[self.pr(review='CHANGES_REQUESTED', updated=ts(timedelta(days=8)))]),
             {'lead': 'present'}, 'stuck'),
            ('review', self.need(), self.tree(prs=[self.pr()]), {'lead': 'present'}, 'review'),
            ('review-draft-only', self.need(), self.tree(prs=[self.pr(draft=True)]), {'lead': 'present'}, 'doing'),
            ('done-leftover', self.need(), self.tree(state='CLOSED', subs=[(2, 'OPEN', None)]), {'lead': 'absent'}, 'done'),
            ('done-closed-8d', self.need(), self.tree(state='CLOSED'), {'lead': 'absent'}, 'done'),
            ('done-dropped', self.need(status='dropped'), None, {}, 'done'),
        ]
        for name, rec, tree, ctx, want in cases:
            with self.subTest(name):
                col, flags = board.column(rec, tree, ctx)
                self.assertEqual(col, want, (col, flags))
                self.assertIn(col, [k for k, _ in board.COLUMNS])

    def test_root_closed_with_open_sub_is_not_all_done(self):
        rec = self.need()
        col, flags = board.column(rec, self.tree(state='CLOSED', subs=[(2, 'OPEN', None), (3, 'CLOSED', 'NOT_PLANNED')]),
                                  {'lead': 'absent'})
        self.assertEqual(col, 'done')
        self.assertIn('有遗留', json.dumps(flags, ensure_ascii=False))
        self.assertEqual(board._sub_progress(self.tree(state='CLOSED', subs=[(2, 'OPEN', None), (3, 'CLOSED', 'NOT_PLANNED')])['subs']),
                         {'done': 0, 'skipped': 1, 'left': 1, 'total': 2})

    def test_approval_on_sub_issue_is_wait_on_root_and_closed_8d_still_opened(self):
        nid = self.filed('看板测试', number=30)
        self.set_tree(30, root_state='OPEN', subs=[(31, 'OPEN', None), (32, 'CLOSED', 'NOT_PLANNED')])
        f = self.tmp / 'q.md'
        f.write_text(rbase.approval_body())
        r = self.q('ask', '--body', str(f), '--title', '子审批', '--issue', 'owner/repo#31', '--blocking', 'yes',
                   '--kind', 'approval')
        self.assertEqual(r.returncode, 0, r.stderr)
        qid = r.stdout.split('已发布 ')[1].split('：')[0]
        board.TREE.clear()
        v = board.view(refresh=True)
        calls = [json.loads(l) for l in (self.tmp / 'gh.log').read_text().splitlines()]
        self.assertTrue(any('-F' in c and any('number=30' == a for a in c) for c in calls))  # GraphQL Int 变量必须用 -F
        card = next(c for col in v['columns'] for c in col['cards'] if c['id'] == nid)
        self.assertEqual(card['column'], 'wait')
        self.assertIn('审批', json.dumps(card['flags'], ensure_ascii=False))
        # 根已关闭 8 天：仍然定向读到（不是从「最近 7 天关闭」列表里找）
        self.set_tree(30, root_state='CLOSED', subs=[(31, 'OPEN', None), (32, 'CLOSED', 'NOT_PLANNED')])
        board.TREE.clear()
        v = board.view(refresh=True)
        closed = next(c for col in v['columns'] if col['key'] == 'done' for c in col['cards'] if c['id'] == nid)
        self.assertIn('有遗留', json.dumps(closed['flags'], ensure_ascii=False))
        self.assertEqual(closed['subs'], {'done': 0, 'skipped': 1, 'left': 1, 'total': 2})
        q = next(x for x in server.need_detail(nid)['timeline'] if x['kind'] == 'question' and x['id'] == qid)
        self.assertEqual(q['detail']['content_rev'], 1)
        self.assertEqual(q['detail']['questions'][0]['options'], list(rbase.APPROVAL_OPTIONS) if hasattr(rbase, 'APPROVAL_OPTIONS') else ['A. 批准', 'B. 不批准', 'C. 需要先改'])

# ---------- 安全 ----------

class V2SecurityTest(V2Base):
    def test_web_write_guards_repo_is_server_side_and_oversize_kept(self):
        nid = self.filed()
        self.start_server()
        st, j, _ = self.http('POST', f'/api/need/{nid}/comment', {'body': 'x', 'to_issue': True},
                             headers={'X-QI-Token': None})
        self.assertEqual(st, 403)
        st, j, _ = self.http('POST', f'/api/need/{nid}/comment', {'body': 'x', 'to_issue': True},
                             headers={'Origin': 'http://evil.example'})
        self.assertEqual(st, 403)
        st, j, _ = self.http('POST', f'/api/need/{nid}/comment', {'body': 'x' * (qstore.MAX_TEXT + 1), 'to_issue': True})
        self.assertEqual(st, 400)
        st, j, _ = self.http('POST', f'/api/need/{nid}/comment',
                             {'body': '正文', 'request_id': 'req-sec-0001', 'to_issue': True, 'repo': 'evil/evil'})
        self.assertEqual(st, 200, j)
        path = [json.loads(l) for l in (self.tmp / 'gh.log').read_text().splitlines() if 'POST' in l][-1]
        self.assertTrue(any('repos/owner/repo/issues/30/comments' in a for a in path))
        self.assertFalse(any('evil/evil' in a for a in path))

    def test_malicious_markdown_escaped_and_no_js_href(self):
        nid = self.filed()
        self.add_comment(30, 7001, '<script>alert(1)</script> [点我](javascript:alert(1))', author='teammate')
        poll.scan()
        d = server.need_detail(nid)
        raw = json.dumps(d, ensure_ascii=False)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', raw)
        self.assertNotIn('<script>alert(1)</script>', raw)
        self.assertNotIn('javascript:alert(1)</a>', raw)

    def test_rate_limits_and_upstream_errors_defer_without_blind_resend(self):
        nid = self.filed()
        for i, (mode, kind) in enumerate((('fail:HTTP 403: rate limit exceeded', 'rate'),
                                          ('fail:HTTP 429: Too Many Requests', 'rate'),
                                          ('timeout', 'timeout'))):
            with self.subTest(mode):
                self.set_gh(post=mode)
                with self.assertRaises(ghapi.GHError) as e:
                    send.owner_message(nid, '内容 ' + mode, f'req-{kind}-{i:04d}', True)
                self.assertEqual(e.exception.code, 409)
        # 403 / 429 是明确的「没发出去」；timeout 是结果不明，不会自动重发
        rec = qstore.read_need(nid)
        states = sorted(v['state'] for v in rec['send_intents'].values())
        self.assertIn('uncertain', states)
        self.assertIn('failed', states)

    def test_per_need_rate_cap(self):
        nid = self.filed()
        try:
            (self.tmp / 'state' / 'gh-write.json').unlink()
        except OSError:
            pass
        old = ghapi.PER_NEED_PER_MIN
        ghapi.PER_NEED_PER_MIN = 1
        self.addCleanup(setattr, ghapi, 'PER_NEED_PER_MIN', old)
        ghapi.write_gate(nid)
        with self.assertRaises(ghapi.GHError) as e:
            ghapi.write_gate(nid)
        self.assertEqual(e.exception.code, 429)


# ---------- 命名 ----------

class V2NamingTest(unittest.TestCase):
    def test_waiting_name_is_daichuli(self):
        self.assertIn('待处理', server.PAGE)
        self.assertNotIn('待我决定', server.PAGE)
        self.assertIn('待处理', [label for _, label in board.COLUMNS])
        self.assertIn('待处理', (base.QI / 'SKILL.md').read_text(encoding='utf-8'))
        self.assertNotIn('待我决定', (base.QI / 'SKILL.md').read_text(encoding='utf-8'))
