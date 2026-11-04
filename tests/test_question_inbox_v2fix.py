"""7788 工作台 v2 · 第 1 轮审查后的修复（skills#25 / Issue #23）：每条都按审查报告里的失败场景写，守住行为。

类别对应 TASK-fix-R1：1 发送状态分类与幂等 / 2 写入门禁 / 3 投递记账 / 4 循环刹车 / 5 接手 / 6 就地答题与表单 /
7 需求树与通知范围 / 8 Nit。虚构数据：ws `demo`、repo `owner/repo`、Owner 登录名 `demo-owner`。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_question_inbox as base  # noqa: E402
import test_question_inbox_v2 as v2  # noqa: E402

qstore, server, board, poll, ghapi, send = v2.qstore, v2.server, v2.board, v2.poll, v2.ghapi, v2.send
ts, ISSUE = v2.ts, v2.ISSUE


class FixBase(v2.V2Base):
    def bodies(self):
        return [c['body'] for c in self.gh_state()['comments'].get('owner/repo', [])]

    def posts(self):
        return [json.loads(l) for l in (self.tmp / 'gh.log').read_text().splitlines() if '"POST"' in l]

    def clear_delivery(self, nid):
        with qstore.locked():
            cur = qstore.read_need(nid)
            cur.update({'delivery': 'received', 'delivery_kind': '', 'delivery_kinds': []})
            qstore._save_need(cur, '测试：清掉投递')

    def lead_prompts(self):
        return [c[-1] for c in self.herdr_calls() if c[1] == 'demo' and 'prompt' in c]

    def age_intent(self, nid, rid, seconds=600):
        with qstore.locked():
            cur = qstore.read_need(nid)
            it = cur['send_intents'][rid]
            it['tried_at'] = ts(timedelta(seconds=seconds))
            if it.get('posting_at'):  # 宽限期从实际 POST 算：「时间过去了」要连 POST 时刻一起回拨
                it['posting_at'] = ts(timedelta(seconds=seconds))
            qstore._save_need(cur)


# ---------- 1. 发 Issue 评论的状态分类与幂等 ----------

class SendClassifyTest(FixBase):
    def test_refused_before_send_is_failed_and_same_request_id_resends_once(self):
        """Claude I1：暂停 / 每需求限流 / gh 起不来 / GitHub 4xx 都是「确定没发出去」→ failed；同一个 request_id 之后能发，且只发一条。"""
        nid = self.filed()
        qstore.set_paused(True)
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, '暂停时发的', 'req-paused-0001', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-paused-0001']['state'], 'failed')
        qstore.set_paused(False)
        self.assertTrue(send.owner_message(nid, '暂停时发的', 'req-paused-0001', True)['created'])
        self.assertEqual(sum('暂停时发的' in b for b in self.bodies()), 1)
        self.addCleanup(setattr, ghapi, 'PER_NEED_PER_MIN', ghapi.PER_NEED_PER_MIN)
        ghapi.PER_NEED_PER_MIN = 0
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, '限流时发的', 'req-ratecap-0001', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-ratecap-0001']['state'], 'failed')
        ghapi.PER_NEED_PER_MIN = 20
        self.assertTrue(send.owner_message(nid, '限流时发的', 'req-ratecap-0001', True)['created'])
        with mock.patch.dict(os.environ, {'GH_BIN_PATH': str(self.tmp / 'no-such-gh')}):
            with self.assertRaises(ghapi.GHError):
                send.owner_message(nid, 'gh 起不来时发的', 'req-exec-0001', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-exec-0001']['state'], 'failed')
        self.assertTrue(send.owner_message(nid, 'gh 起不来时发的', 'req-exec-0001', True)['created'])
        self.set_gh(post='fail:HTTP 422: Validation Failed')
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, '被 422 拒的', 'req-422-0001', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-422-0001']['state'], 'failed')
        self.assertFalse(any('被 422 拒的' in b for b in self.bodies()))

    def test_5xx_after_github_wrote_is_uncertain_and_retry_reconciles(self):
        """Claude I2：POST 已成功但回包 502 → uncertain；同一个 request_id 重试先对账，不出第二条。"""
        nid = self.filed()
        real = ghapi.create_comment

        def flaky(*a, **kw):
            real(*a, **kw)
            raise ghapi.GHError(409, '发评论失败（502）：Bad Gateway', 'http', 502)
        with mock.patch.object(ghapi, 'create_comment', side_effect=flaky):
            with self.assertRaises(ghapi.GHError):
                send.owner_message(nid, '502 但其实发出去了', 'req-502-0001', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-502-0001']['state'], 'uncertain')
        got = send.owner_message(nid, '502 但其实发出去了', 'req-502-0001', True)
        self.assertFalse(got['created'])
        self.assertEqual(sum('502 但其实发出去了' in b for b in self.bodies()), 1)
        self.assertEqual(got['comment_id'], self.gh_state()['comments']['owner/repo'][-1]['id'])

    def test_uncertain_not_found_waits_grace_then_resends_once(self):
        """结果不明、对账也找不到：宽限期内 409 不重发；超过宽限期（> gh 超时）且读完了评论，才判定没发出去，发且只发一条。"""
        nid = self.filed()
        self.set_gh(post='fail:HTTP 502: Bad Gateway')  # 假 gh：502 且远端没写
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, '502 且没写进去', 'req-502-0002', True)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-502-0002']['state'], 'uncertain')
        self.set_gh(post='ok')
        with self.assertRaises(qstore.QError) as e:
            send.owner_message(nid, '502 且没写进去', 'req-502-0002', True)
        self.assertEqual(e.exception.code, 409)
        self.assertFalse(any('502 且没写进去' in b for b in self.bodies()))
        self.age_intent(nid, 'req-502-0002')
        self.assertTrue(send.owner_message(nid, '502 且没写进去', 'req-502-0002', True)['created'])
        self.assertEqual(sum('502 且没写进去' in b for b in self.bodies()), 1)

    def test_reconcile_needs_full_tag_body_hash_and_trusted_author(self):
        """GPT I1：复制尾标 / 只含意图 id 片段 / 尾标对但正文不对，都不能完成意图、拿不到 own 身份；真的那条才行。"""
        nid = self.filed()
        self.set_gh(post='lost')
        with self.assertRaises(qstore.QError):
            send.owner_message(nid, '真正的 Owner 结论', 'req-forge-0001', True)
        self.set_gh(post='ok')
        real = self.gh_state()['comments']['owner/repo'][-1]
        tag_id = qstore.read_need(nid)['send_intents']['req-forge-0001']['tag_id']
        self.add_comment(30, 900, real['body'], author='teammate')
        self.add_comment(30, 901, f'无关内容 i-{tag_id} msg-req-forge-0001', author='demo-owner')
        self.add_comment(30, 902, '改过的正文\n\n' + ghapi.tag(nid, tag_id), author='demo-owner')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(rec['send_intents']['req-forge-0001']['comment_id'], real['id'])
        own = {c['id'] for c in rec['own_comments']}
        self.assertIn(real['id'], own)
        self.assertFalse(own & {900, 901, 902})

    def test_intent_before_post_is_not_completed_by_someone_elses_comment(self):
        """GPT I1：意图落盘后进程退出（没 POST）；团队评论里带 request_id / 意图片段也不能把它判成已发送。"""
        nid = self.filed()
        with qstore.locked():
            rec = qstore.read_need(nid)
            it, _ = qstore.send_intent(rec, 'req-crash-0001', '没发出去的私密决定', qstore.root_issue(rec)[2])
            qstore._save_need(rec)
        self.add_comment(30, 9201, f'unrelated comment msg-req-crash-0001 i-{it["tag_id"]}', author='teammate')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertNotEqual(rec['send_intents']['req-crash-0001']['state'], 'sent')
        self.assertNotIn(9201, {c['id'] for c in rec['own_comments']})
        self.age_intent(nid, 'req-crash-0001')
        self.assertTrue(send.owner_message(nid, '没发出去的私密决定', 'req-crash-0001', True)['created'])
        self.assertEqual(sum('没发出去的私密决定' in b for b in self.bodies()), 1)

    def test_lead_comment_lost_retry_once_and_not_delivered_back(self):
        """Claude I3 / GPT I11：need comment 丢回包后重跑同一正文：只有一条，own 记账完整，不会作为「来源未核实」投回 lead。"""
        nid = self.filed()
        self.clear_delivery(nid)
        self.set_gh(post='lost')
        r = self.as_lead('need', 'comment', nid, '--text', '结论：先做 CSV')
        self.assertNotEqual(r.returncode, 0)
        self.set_gh(post='ok')
        r = self.as_lead('need', 'comment', nid, '--text', '结论：先做 CSV')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('没有重发', r.stdout)
        self.assertEqual(sum('结论：先做 CSV' in b for b in self.bodies()), 1)
        cid = next(c['id'] for c in self.gh_state()['comments']['owner/repo'] if '结论：先做 CSV' in c['body'])
        self.assertEqual(qstore.own_comment(qstore.read_need(nid), cid)['by'], 'lead-demo')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(next(c for c in rec['comments'] if c['id'] == cid)['state'], 'own')
        self.assertNotEqual(rec.get('delivery'), 'pending')

    def test_link_comment_failure_then_relink_posts_exactly_one(self):
        """GPT I11：首次立项评论失败（确定没发 / 结果不明）后重跑同一条 need link：接着贴，恰好一条。"""
        for mode, n in (('fail:HTTP 422: Validation Failed', 30), ('lost', 40)):
            with self.subTest(mode):
                nid = self.assigned(f'立项评论 {mode}')
                self.as_lead('need', 'claim', nid)
                self.issue_bodies(**{str(n): f'需求：{nid}'})
                self.set_gh(post=mode)
                r = self.as_lead('need', 'link', nid, '--issue', ISSUE + str(n))
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(qstore.read_need(nid)['status'], 'filed')
                self.set_gh(post='ok')
                r = self.as_lead('need', 'link', nid, '--issue', ISSUE + str(n))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sum(f'立项评论 {mode}' in b for b in self.bodies()), 1)
                self.as_lead('need', 'link', nid, '--issue', ISSUE + str(n))
                self.assertEqual(sum(f'立项评论 {mode}' in b for b in self.bodies()), 1)

    def test_issue_body_limit_is_10000(self):
        nid = self.filed()
        with self.assertRaises(qstore.QError) as e:
            send.owner_message(nid, 'x' * 10001, 'req-long-0001', True)
        self.assertEqual(e.exception.code, 400)
        send.owner_message(nid, 'x' * 10001, 'req-long-0002', False)  # 7788 线程不受这个限制
        self.assertFalse(any(b.startswith('xxxx') for b in self.bodies()))


# ---------- 2. 写入门禁 ----------

class WriteGateTest(FixBase):
    def test_pause_during_interval_wait_blocks_the_post(self):
        """GPT I3：通过最初的暂停检查后、等间隔时被暂停 → 不 POST，意图记 failed。"""
        nid = self.filed()
        self.addCleanup(setattr, ghapi, 'WRITE_INTERVAL', 0)
        ghapi.WRITE_INTERVAL = 0.5
        ghapi.write_gate('need-9999')  # 刚写过一条：下一次要等间隔
        before = len(self.posts())
        with mock.patch.object(ghapi.time, 'sleep', side_effect=lambda s: qstore.set_paused(True)):
            with self.assertRaises(ghapi.GHError) as e:
                send.owner_message(nid, '等间隔时被暂停', 'req-pausewait-01', True)
        self.assertEqual(e.exception.kind, 'paused')
        self.assertEqual(len(self.posts()), before)
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-pausewait-01']['state'], 'failed')

    def test_two_processes_are_serialized_spaced_and_counted(self):
        """GPT I3：服务和 CLI 两个进程同时写：写动作不重叠、间隔 ≥ WRITE_INTERVAL、每需求计数两格都在。"""
        code = ('import sys, time, json; sys.path.insert(0, sys.argv[1]); import ghapi\n'
                'ghapi.WRITE_INTERVAL = 0.3\n'
                'with ghapi.writing("need-0042"):\n'
                '    t0 = time.time(); time.sleep(0.4); t1 = time.time()\n'
                'print(json.dumps([t0, t1]))\n')
        ps = [subprocess.Popen([sys.executable, '-c', code, str(base.QI)], stdout=subprocess.PIPE, text=True,
                               env=dict(os.environ)) for _ in range(2)]
        spans = sorted(json.loads(p.communicate(timeout=30)[0]) for p in ps)
        self.assertGreaterEqual(spans[1][0], spans[0][1])
        self.assertGreaterEqual(spans[1][0] - spans[0][0], 0.3 - 0.02)
        st = json.loads((self.tmp / 'state' / 'gh-write.json').read_text())
        self.assertEqual(len(st['needs']['need-0042']), 2)


# ---------- 3. 投递记账 ----------

class DeliveryLedgerTest(FixBase):
    def test_background_reconcile_queues_delivery_to_lead(self):
        """Claude I4 / GPT I2：回包丢失后由轮询对账找到 → 和主动发送一样排投递，lead 收到唤醒，评论变 notified。"""
        nid = self.filed()
        self.clear_delivery(nid)
        self.set_gh(post='lost')
        with self.assertRaises(qstore.QError):
            send.owner_message(nid, '丢回包的 Owner 消息', 'req-bg-0001', True)
        self.set_gh(post='ok')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(rec['send_intents']['req-bg-0001']['state'], 'sent')
        self.assertEqual((rec['delivery'], rec['delivery_to']), ('pending', 'lead'))
        self.deliver_now(nid)
        self.assertTrue(any('need inbox ' + nid in p for p in self.lead_prompts()))
        cid = rec['send_intents']['req-bg-0001']['comment_id']
        self.assertEqual(next(c for c in qstore.read_need(nid)['comments'] if c['id'] == cid)['state'], 'notified')
        again = send.owner_message(nid, '丢回包的 Owner 消息', 'req-bg-0001', True)
        self.assertEqual((again['created'], again['comment_id']), (False, cid))

    def test_closeout_survives_later_comment_and_is_marked_only_after_sent(self):
        """Claude I5-A：收尾排队后又来评论，收尾不被覆盖；closed_notified 在真正投出去后才置位。"""
        nid = self.filed()
        self.clear_delivery(nid)
        self.set_tree(30, root_state='CLOSED')
        board.TREE.clear()
        board.view(refresh=True)
        rec = qstore.read_need(nid)
        self.assertTrue(rec['closeout'])
        self.assertFalse(rec.get('closed_notified'))
        self.add_comment(30, 6101, '关了之后的一句', author='teammate')
        poll.scan()
        self.deliver_now(nid)
        p = self.lead_prompts()[-1]
        self.assertIn('收尾', p)
        self.assertIn('1 条新的', p)
        rec = qstore.read_need(nid)
        self.assertTrue(rec['closed_notified'])
        self.assertEqual(next(c for c in rec['comments'] if c['id'] == 6101)['state'], 'notified')

    def test_reply_after_comment_does_not_swallow_comment_wakeup(self):
        """Claude I5-B：先有团队评论待投，再来 7788 回复：一次唤醒里两者都在，评论记成 notified，回复记已通知。"""
        nid = self.filed()
        self.clear_delivery(nid)
        self.add_comment(30, 7001, '团队的问题', author='teammate')
        poll.scan()
        qstore.owner_reply(nid, '我补一句', 'req-merge-0001')
        self.deliver_now(nid)
        p = self.lead_prompts()[-1]
        self.assertIn('讨论线程里回复', p)
        self.assertIn('Issue 评论', p)
        rec = qstore.read_need(nid)
        self.assertEqual(next(c for c in rec['comments'] if c['id'] == 7001)['state'], 'notified')
        self.assertTrue(next(x for x in rec['thread'] if x.get('text') == '我补一句').get('notified_at'))

    def test_message_arriving_while_sending_is_redelivered_and_sent_ones_marked(self):
        nid = self.filed()
        self.clear_delivery(nid)
        qstore.owner_reply(nid, '第一句', 'req-fly-0001')
        claimed = qstore.need_claim([(nid, qstore.geti(qstore.read_need(nid), 'delivery_seq'))])
        qstore.owner_reply(nid, '投递途中来的第二句', 'req-fly-0002')
        qstore.need_finish(claimed, 'sent')
        rec = qstore.read_need(nid)
        self.assertTrue(next(x for x in rec['thread'] if x.get('text') == '第一句').get('notified_at'))
        self.assertFalse(next(x for x in rec['thread'] if x.get('text') == '投递途中来的第二句').get('notified_at'))
        self.assertEqual(rec['delivery'], 'pending')

    def test_ack_only_confirms_what_was_read(self):
        """Claude I6 / GPT I6：ack 绑定读取快照：后来的 7788 消息、读后被编辑的评论都不会被吞；只有 7788 消息也能 ack。"""
        nid = self.filed()
        ident = self.as_ident('w1:p1')
        qstore.owner_reply(nid, '第一句', 'req-ack-0001')
        seen = qstore.lead_inbox(nid, [], ident, poll.body_fetcher)
        self.assertEqual([x['text'] for x in seen['thread']], ['第一句'])
        qstore.owner_reply(nid, '第二句', 'req-ack-0002')
        qstore.lead_inbox(nid, [x['ack'] for x in seen['thread']], ident, poll.body_fetcher)
        self.assertEqual([x['text'] for x in qstore.lead_inbox(nid, [], ident, poll.body_fetcher)['thread']], ['第二句'])
        self.add_comment(30, 9601, 'OLD BODY', author='teammate')
        poll.scan()
        old = next(c for c in qstore.lead_inbox(nid, [], ident, poll.body_fetcher)['comments'] if c['id'] == 9601)
        self.assertEqual(old['body'], 'OLD BODY')
        self.edit_comment(9601, 'NEW BODY', updated=ts(timedelta(seconds=-5)))
        poll.scan()
        r = qstore.lead_inbox(nid, [old['ack']], ident, poll.body_fetcher)
        self.assertEqual(r['stale'], ['9601'])
        cur = next(c for c in qstore.lead_inbox(nid, [], ident, poll.body_fetcher)['comments'] if c['id'] == 9601)
        self.assertEqual(cur['body'], 'NEW BODY')
        out = self.as_lead('need', 'inbox', nid)
        self.assertIn('--ack m:', out.stdout)
        tok = next(x['ack'] for x in qstore.lead_inbox(nid, [], ident, poll.body_fetcher)['thread'])
        r = self.as_lead('need', 'inbox', nid, '--ack', tok)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.as_lead('need', 'inbox', nid, '--ack', 'x')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('Traceback', r.stderr)
        self.assertIn('对不上的确认口令', r.stderr)
        self.assertEqual(qstore.lead_inbox(nid, [], ident, poll.body_fetcher)['thread'], [])

    def test_receipt_reads_per_message(self):
        nid = self.filed()
        self.clear_delivery(nid)
        qstore.owner_reply(nid, '要回执的一句', 'req-rcpt-0001')
        self.deliver_now(nid)
        item = next(x for x in server.need_detail(nid)['timeline'] if x.get('text') == '要回执的一句')
        self.assertEqual((item['receipt']['notified'], item['receipt']['read']), (True, False))
        inbox = qstore.lead_inbox(nid, [], self.as_ident('w1:p1'), poll.body_fetcher)
        qstore.lead_inbox(nid, [x['ack'] for x in inbox['thread']], self.as_ident('w1:p1'), poll.body_fetcher)
        item = next(x for x in server.need_detail(nid)['timeline'] if x.get('text') == '要回执的一句')
        self.assertTrue(item['receipt']['read'])

    def test_capacity_never_drops_unread(self):
        """GPT I9：超过容量时只裁已读 / 自己发的，未读一条不丢。"""
        nid = self.filed()
        self.addCleanup(setattr, qstore, 'COMMENTS_KEEP', qstore.COMMENTS_KEEP)
        qstore.COMMENTS_KEEP = 5
        for i in range(8):
            self.add_comment(30, 10000 + i, f'未读 {i}', author='teammate')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual(sorted(c['id'] for c in rec['comments'] if c['id'] >= 10000), list(range(10000, 10008)))

    def test_partial_pages_do_not_advance_past_unread(self):
        """GPT I9：一轮到了页数上限没读完：水位只推进到读到的最后一条，下一轮接着读，不漏。"""
        nid = self.filed()
        self.addCleanup(setattr, ghapi, 'LIST_MAX_PAGES', ghapi.LIST_MAX_PAGES)
        ghapi.LIST_MAX_PAGES = 1
        for i in range(150):
            self.add_comment(30, 20000 + i, f'第 {i} 条', author='teammate', updated=ts(timedelta(seconds=300 - i)))
        out = poll.scan()
        self.assertFalse(out['owner/repo']['complete'])
        self.assertLess(len({c['id'] for c in qstore.read_need(nid)['comments'] if c['id'] >= 20000}), 150)
        for _ in range(3):
            if poll.scan()['owner/repo']['complete']:
                break
        self.assertEqual(len({c['id'] for c in qstore.read_need(nid)['comments'] if c['id'] >= 20000}), 150)

    def test_inbox_body_read_by_id_beyond_issue_page_limit(self):
        nid = self.filed()
        self.add_comment(30, 30001, '只读这一条', author='teammate')
        poll.scan()
        self.addCleanup(setattr, ghapi, 'ISSUE_MAX_PAGES', ghapi.ISSUE_MAX_PAGES)
        ghapi.ISSUE_MAX_PAGES = 0  # 整个 Issue 的评论读不了，也不影响按 id 读正文
        inbox = qstore.lead_inbox(nid, [], self.as_ident('w1:p1'), poll.body_fetcher)
        self.assertEqual(next(c for c in inbox['comments'] if c['id'] == 30001)['body'], '只读这一条')


# ---------- 4. 循环刹车 ----------

class LoopBrakeTest(FixBase):
    def rounds(self, nid, make):
        for i in range(4):
            make(i)
            poll.scan()
            self.deliver_now(nid)

    def test_owner_writing_directly_several_times_is_not_frozen(self):
        """Claude I7-A：Owner 在 GitHub 上直接连写多条（未核实来源）→ 照常投递，不冻结。"""
        nid = self.filed()
        self.rounds(nid, lambda i: self.add_comment(30, 41000 + i, f'Owner 手机上写的 {i}', author='demo-owner'))
        rec = qstore.read_need(nid)
        self.assertFalse(rec['loop_suspect'])
        self.assertEqual({c['state'] for c in rec['comments'] if c['id'] >= 41000}, {'notified'})

    def test_team_mentions_are_not_frozen(self):
        """Claude I7-B / GPT I10：三位团队成员各自 @lead-demo（同一轮三条），以及之后每轮点名，都正常投递。"""
        nid = self.filed()
        for i in range(3):
            self.add_comment(30, 42000 + i, f'@lead-demo 正常问题 {i}', author=f'mate{i}')
        poll.scan()
        self.rounds(nid, lambda i: self.add_comment(30, 42100 + i, f'@lead-demo 再问 {i}', author='mate0'))
        rec = qstore.read_need(nid)
        self.assertFalse(rec['loop_suspect'])
        self.assertNotIn('held', {c['state'] for c in rec['comments']})

    def test_echo_brake_can_be_cleared_from_page(self):
        nid = self.filed()
        send.lead_comment(nid, '会被重复发出的结论', self.as_ident('w1:p1'))
        ours = self.gh_state()['comments']['owner/repo'][-1]['body']
        for i in range(3):
            self.add_comment(30, 43000 + i, ours, author='demo-owner')  # 我们自己的内容又出现（回声）
            poll.scan()
        self.add_comment(30, 43009, '刹车之后来的团队评论', author='teammate')
        poll.scan()
        self.assertTrue(qstore.read_need(nid)['loop_suspect'])
        self.start_server()
        st, d, _ = self.http('GET', f'/api/need/{nid}')
        self.assertTrue(d['loop'])
        st, j, _ = self.post(f'/api/need/{nid}/unloop', {})
        self.assertEqual(st, 200, j)
        rec = qstore.read_need(nid)
        self.assertFalse(rec['loop_suspect'])
        self.assertNotIn('held', {c['state'] for c in rec['comments']})


# ---------- 5. 接手 ----------

class TakeoverFixTest(FixBase):
    def test_router_takeover_cli_succeeds_and_prompts_once(self):
        """Claude I8：q.py router takeover 不再崩溃（args["id"]），退出码 0，只 prompt 一次。"""
        nid = self.filed()
        self.lead_panes(gone=('w1:p1',))
        r = self.as_router('router', 'takeover', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('已安排接手', r.stdout)
        prompts = [c[-1] for c in self.all_calls() if c[2:4] == ['agent', 'prompt'] and 'takeover' in c[-1]]
        self.assertEqual(len(prompts), 1)
        book = self.tmp / 'ws' / 'demo' / 'dispatch' / f'{nid}-takeover.md'
        self.assertIn(f'need claim {nid} --takeover', book.read_text())

    def test_new_lead_gets_unacked_messages_redelivered(self):
        nid = self.filed()
        self.clear_delivery(nid)
        self.add_comment(30, 50001, '团队的问题', author='teammate')
        poll.scan()
        self.deliver_now(nid)  # 投给旧 lead：notified 但没 ack
        self.lead_panes(gone=('w1:p1',))
        qstore.claim_need(nid, self.as_ident('w1:p2'), takeover=True)
        rec = qstore.read_need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to']), ('pending', 'lead'))
        self.assertEqual(next(c for c in rec['comments'] if c['id'] == 50001)['state'], 'pending')
        self.deliver_now(nid)
        self.assertTrue(any(c[2:5] == ['agent', 'prompt', 'w1:p2'] for c in self.herdr_calls() if c[1] == 'demo'))

    def pre_filed_gone(self, text='在吗'):
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
        self.lead_panes(gone=('w1:p1',))
        qstore.owner_reply(nid, text, 'req-pre-' + text.encode().hex()[:12])
        return nid

    def asks(self):
        return [c for c in self.router_prompts() if 'router takeover' in c[-1]]

    def test_pre_filed_absent_lead_auto_asks_router_once_and_router_can_take_over(self):
        """Claude I9 + lead 决定（R1b）：立项前负责 lead 确认不在 → held（原因写明）、自动请 Router 接手一次；Router 能接（不再 409）。"""
        nid = self.pre_filed_gone()
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual(rec['delivery'], 'held')
        self.assertIn('已请 Router 安排接手', rec['last_error'])
        self.assertNotIn('已通知', qstore.receipt(rec)['note'])
        self.assertEqual(board.column(rec, None, {'lead': 'absent'})[0], 'stuck')
        self.assertEqual(len(self.asks()), 1)
        self.assertIn('router takeover ' + nid, self.asks()[0][-1])
        r = self.as_router('router', 'takeover', nid)
        self.assertEqual(r.returncode, 0, r.stderr)
        qstore.claim_need(nid, self.as_ident('w1:p2'), takeover=True)
        inbox = qstore.lead_inbox(nid, [], self.as_ident('w1:p2'), poll.body_fetcher)
        self.assertEqual([x['text'] for x in inbox['thread']], ['在吗'])


    def test_auto_ask_is_deduplicated_and_reasks_after_window(self):
        nid = self.pre_filed_gone()
        for _ in range(3):  # 投递循环反复重试 held：同一段时间只叫一次
            self.deliver_now(nid)
        qstore.owner_reply(nid, '又一句', 'req-pre-again-01')  # 新消息也不重复叫
        self.deliver_now(nid)
        self.assertEqual(len(self.asks()), 1)
        with mock.patch.dict(os.environ, {'QI_TAKEOVER_REASK': '0'}):  # 去重窗口过了还没人接：再叫一次
            self.deliver_now(nid)
        self.assertEqual(len(self.asks()), 2)
        qstore.claim_need(nid, self.as_ident('w1:p2'), takeover=True)  # 接手完成后清零
        self.assertNotIn('takeover_ask', qstore.read_need(nid))

    def test_paused_does_not_ask_only_records_then_asks_after_resume(self):
        nid = self.pre_filed_gone()
        qstore.set_paused(True)
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual(self.asks(), [])
        self.assertEqual(rec['delivery'], 'held')
        self.assertEqual(rec['takeover_ask']['result'], 'paused')
        self.assertIn('全局暂停中，没有叫 Router', rec['last_error'])
        qstore.set_paused(False)
        self.deliver_now(nid)
        self.assertEqual(len(self.asks()), 1)

    def test_unknown_presence_does_not_ask(self):
        nid = self.pre_filed_gone()
        self.patch_herdr(pane_list_fail={'demo': 'error:internal'})  # 查询失败：原 lead 是否还在查不清
        self.assertEqual(qstore.presence(*[qstore.read_need(nid)['lead'][k] for k in ('herdr_session', 'lead_session')])[0], 'unknown')
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual(self.asks(), [])
        self.assertEqual(rec['delivery'], 'held')
        self.assertNotIn('takeover_ask', rec)

    def test_filed_absence_also_holds_and_asks_router(self):
        """已立项的同样（R2，Claude R2 I1）：确认不在 → 消息 held 在 lead 这边，自动请 Router 接手，不占投递槽。"""
        nid = self.filed()
        self.lead_panes(gone=('w1:p1',))
        qstore.owner_reply(nid, '立项后在吗', 'req-filed-gone-1')
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to']), ('held', 'lead'))
        self.assertEqual(len(self.asks()), 1)


# ---------- 7. 需求树与通知范围 ----------

class TreeScopeTest(FixBase):
    def node(self, n, state='OPEN', reason=None, repo='owner/repo', subs=(), prs=()):
        return {'number': n, 'title': f'#{n}', 'url': f'https://github.com/{repo}/issues/{n}', 'state': state,
                'stateReason': reason, 'closedAt': None, 'createdAt': ts(), 'updatedAt': ts(), 'body': '需求：need-0001',
                'labels': {'nodes': []}, 'repository': {'nameWithOwner': repo},
                'subIssues': {'nodes': [{'number': s, 'repository': {'nameWithOwner': r}} for s, r in subs]},
                'timelineItems': {'nodes': [{'source': p} for p in prs]}, 'closedByPullRequestsReferences': {'nodes': []}}

    def pr(self, n, checks='SUCCESS', repo='owner/repo'):
        return {'number': n, 'title': f'PR {n}', 'url': f'https://github.com/{repo}/pull/{n}', 'state': 'OPEN', 'isDraft': False,
                'mergedAt': None, 'updatedAt': ts(), 'reviewDecision': '', 'statusCheckRollup': {'state': checks},
                'repository': {'nameWithOwner': repo}}

    def put(self, repo, node):
        data = self.gh_state()
        data.setdefault('board', {})[f'{repo}#{node["number"]}'] = {'data': {'repository': {'issue': node}}}
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))

    def test_discovered_child_issue_comments_are_delivered_and_backfilled(self):
        """GPT I7：只 link 根，团队在 GitHub 上挂子 Issue #31 并评论：通知 lead；水位之前的旧评论也回填。"""
        nid = self.filed(number=30)
        self.clear_delivery(nid)
        self.put('owner/repo', self.node(30, subs=[(31, 'owner/repo')]))
        self.put('owner/repo', self.node(31))
        board.TREE.clear()
        self.add_comment(31, 9401, '子 Issue 上早先的评论', author='teammate', updated=ts(timedelta(days=3)))
        self.add_comment(31, 9402, '子 Issue 上新的评论', author='teammate')
        poll.scan()
        ids = {c['id']: c['state'] for c in qstore.read_need(nid)['comments']}
        self.assertEqual((ids.get(9401), ids.get(9402)), ('pending', 'pending'))
        self.deliver_now(nid)
        self.assertIn('2 条新的', self.lead_prompts()[-1])
        poll.scan()
        self.assertEqual(len([c for c in qstore.read_need(nid)['comments'] if c['id'] in (9401, 9402)]), 2)

    def test_tree_reads_nested_children_their_prs_and_pages(self):
        """GPT I8：子 Issue 的 PR / 失败 checks、多层子 Issue、超过 100 个子 Issue（翻页）、跨 repo 子 Issue 都读到。"""
        nid = self.filed(number=30)
        kids = [(31, 'owner/repo'), (60, 'owner/other')] + [(100 + i, 'owner/repo') for i in range(120)]
        self.put('owner/repo', self.node(30, subs=kids))
        self.put('owner/repo', self.node(31, subs=[(32, 'owner/repo')], prs=[self.pr(40, checks='FAILURE')]))
        self.put('owner/repo', self.node(32))
        self.put('owner/other', self.node(60, repo='owner/other'))
        for i in range(120):
            self.put('owner/repo', self.node(100 + i, state='CLOSED', reason='COMPLETED'))
        tree = ghapi.issue_tree(ISSUE + '30')
        subs = {(s['repo'], s['number']): s for s in tree['subs']}
        self.assertEqual(len(subs), 123)
        self.assertEqual(subs[('owner/repo', 32)]['depth'], 2)
        self.assertIn(('owner/other', 60), subs)
        self.assertEqual([(p['number'], p['checks'], p['issue']) for p in tree['prs']], [(40, 'FAILURE', 'owner/repo#31')])
        col, flags = board.column(qstore.read_need(nid), tree, {'lead': 'present'})
        self.assertEqual(col, 'stuck')
        self.assertIn('checks 失败', json.dumps(flags, ensure_ascii=False))
        self.put('owner/repo', self.node(30, state='CLOSED', reason='COMPLETED', subs=kids))
        tree = ghapi.issue_tree(ISSUE + '30')
        col, flags = board.column(qstore.read_need(nid), tree, {'lead': 'present'})
        self.assertEqual(col, 'done')
        self.assertIn('有遗留：3 项', json.dumps(flags, ensure_ascii=False))  # #31、嵌套的 #32、跨 repo 的 #60

    def test_tree_truncation_is_reported(self):
        nid = self.filed(number=30)
        self.addCleanup(setattr, ghapi, 'TREE_MAX_NODES', ghapi.TREE_MAX_NODES)
        ghapi.TREE_MAX_NODES = 3
        self.put('owner/repo', self.node(30, subs=[(31 + i, 'owner/repo') for i in range(5)]))
        tree = ghapi.issue_tree(ISSUE + '30')
        self.assertTrue(tree['truncated'])
        board.TREE.clear()
        board.TREE[ISSUE + '30'] = (qstore.datetime.now(qstore.TZ), tree)
        self.assertIn('需求树没读全', json.dumps(server.need_detail(nid)['errors'], ensure_ascii=False))

    def test_root_closed_not_planned_shows_not_doing(self):
        rec = {'id': 'need-0001', 'status': 'filed', 'lead': {'pane_id': 'p'}, 'ws': 'demo'}
        tree = {'root': {'state': 'CLOSED', 'state_reason': 'NOT_PLANNED'}, 'subs': [], 'prs': []}
        col, flags = board.column(rec, tree, {'lead': 'present'})
        self.assertEqual(col, 'done')
        self.assertEqual(board._next(rec, tree, flags, col), '不做')


# ---------- 8. Nit ----------

class NitTest(FixBase):
    def test_poll_does_not_call_herdr_while_holding_the_store_lock(self):
        """Claude N4：轮询只记账、排投递；查 herdr 由投递循环在锁外做。"""
        nid = self.filed()
        self.add_comment(30, 60001, '一条评论', author='teammate')
        with mock.patch.object(qstore, 'presence', side_effect=AssertionError('poll 里不该查 herdr')):
            poll.scan()
        self.assertEqual(qstore.read_need(nid)['delivery'], 'pending')

    def test_own_comments_are_identity_and_never_truncated(self):
        rec = {'own_comments': [], 'comments': []}
        for i in range(250):
            qstore.add_own_comment(rec, 70000 + i, 'owner')
        self.assertEqual(len(rec['own_comments']), 250)
        self.assertIsNotNone(qstore.own_comment(rec, 70000))

    def test_detail_strips_hidden_tag_and_link_comment_has_no_local_port(self):
        nid = self.filed()
        send.owner_message(nid, '带尾标发出去的', 'req-tag-0001', True)
        poll.clear_cache()
        raw = json.dumps(server.need_detail(nid), ensure_ascii=False)
        self.assertIn('带尾标发出去的', raw)
        self.assertNotIn('&lt;!-- qi:', raw)
        self.assertNotIn('<!-- qi:', raw)
        self.assertNotIn('127.0.0.1', '\n'.join(self.bodies()))

    def test_skills_forbid_direct_gh_comments(self):
        for path in (base.QI / 'SKILL.md', base.QI.parent / 'workspace' / 'SKILL.md'):
            text = path.read_text(encoding='utf-8')
            with self.subTest(path.parent.name):
                self.assertIn('need comment', text)
                self.assertIn('gh issue comment', text)


# ---------- 6. 页面：就地答题与表单、Inbox 合并、头像、XSS ----------

PAGE_FIX = r"""
const modes = mk('nav', 'modes'); doc.append(modes);
const ARG = JSON.parse(process.argv[4] || '{}');
const answerAll = (path, status, body) => { for (const r of pending(path)) { r.done = true; r.resolve({status, json: async () => body}); } };
const QD = (over) => Object.assign({id: 'demo-0001', kind: 'question', title: '就地答题', status: 'open', content_rev: 1, answer_rev: 0,
  editable: true, draft: {slots: {}}, option_notes: {}, revs: [], errata_html: '', approval_html: [], background_html: '', evidence_html: '',
  questions: [{q: 'Q1', text: '第一问', options: ['A. 甲', 'B. 乙'], suggestion: '', withdrawn: false}]}, over || {});
const NEED = (qd, over) => Object.assign({id: 'need-0007', title: '需求', text: 't', status: 'filed', discuss: '', ws: 'demo', lead: {lead_session: 'x'},
  card_rev: 0, confirmed_rev: 0, issues: [], github: '', column: 'doing', column_label: '进行中', flags: [], sealed: false, unread: 0, errors: [],
  receipt: {}, loop: false, can_takeover: false, subs: [], subs_progress: {done: 0, skipped: 0, left: 0, total: 0}, prs: [], created: 't', updated: 't',
  to_issue: true, lead_state: 'present',
  timeline: qd ? [{kind: 'question', at: 't', id: qd.id, title: qd.title, qkind: qd.kind, qstatus: qd.status, blocking: false, invalid: '',
    source: '7788 问题卡', detail: qd}] : []}, over || {});
const pickIn = (v) => { const r = find(e => e.tagName === 'INPUT' && e.attrs.name === 'i_demo-0001_Q1' && e.attrs.value === v);
  r.checked = true; r.dispatch('change'); };
(async () => {
  const out = {};
  if (scenario === 'inline-stale') {
    ctx.location.hash = '#need:need-0007'; run(src); await flush();
    answer('/api/need/need-0007', 200, NEED(QD())); await flush();
    pickIn('opt:1');  // 选 B. 乙（v1）
    run('loadNeed(false)'); await flush();  // 15 秒自动刷新：lead 发了勘误，选项顺序也变了
    answer('/api/need/need-0007', 200, NEED(QD({content_rev: 2, errata_html: '<p>ERRATA_MARK 作答前补充</p>',
      questions: [{q: 'Q1', text: '第一问（改）', options: ['B. 乙（改）', 'A. 甲'], suggestion: '', withdrawn: false}]}))); await flush();
    out.text = text(main);
    out.htmls = htmlSets.slice();
    out.draft = run('inlineDraft["demo-0001"] || null');
    out.checked = findAll(e => e.tagName === 'INPUT' && e.checked).length;
    button('提交回答', main).dispatch('click'); await flush();
    out.postsAfterStale = posts().length;
    out.msg = run('inlineState["demo-0001"].msg && inlineState["demo-0001"].msg.text');
    pickIn('opt:1');
    button('提交回答', main).dispatch('click'); await flush();
    out.posts = posts();
  } else if (scenario === 'inline-busy') {
    ctx.location.hash = '#need:need-0007'; run(src); await flush();
    answer('/api/need/need-0007', 200, NEED(QD())); await flush();
    pickIn('opt:0');
    const ta = byId('i_demo-0001_Q1'); ta.value = '补充'; ta.dispatch('input');
    button('提交回答', main).dispatch('click'); await flush();
    out.taDisabled = byId('i_demo-0001_Q1').disabled;
    out.radioDisabled = findAll(e => e.tagName === 'INPUT' && e.attrs.type === 'radio').every(r => r.disabled);
    out.post = posts()[0];
    answerAll('/api/q/demo-0001/submit', 200, {ok: true, answer_rev: 1}); await flush();
    out.draftAfter = run('inlineDraft["demo-0001"] || null');
  } else if (scenario === 'inline-approval') {
    ctx.location.hash = '#need:need-0007'; run(src); await flush();
    answer('/api/need/need-0007', 200, NEED(QD({kind: 'approval', approval_html: [['动作', '<p>ACT_MARK 合并 PR</p>'], ['后果', '<p>CONSEQ_MARK</p>']],
      errata_html: '<p>ERRATA_MARK</p>', questions: [{q: 'Q1', text: '批不批', options: ['A. 批准', 'B. 不批准', 'C. 需要先改'], suggestion: '', withdrawn: false}]}))); await flush();
    out.htmls = htmlSets.slice();
    out.text = text(main);
  } else if (scenario === 'composer') {
    ctx.location.hash = '#need:need-0007'; run(src); await flush();
    answer('/api/need/need-0007', 200, NEED(null)); await flush();
    const ta = () => byId('need_text'), chk = () => byId('need_to_issue');
    ta().value = '团队可见的结论'; ta().dispatch('input');
    chk().checked = true; chk().dispatch('change');
    run('renderNeed()'); await flush();           // 自动刷新 / 重新渲染
    out.checkedAfterRender = chk().checked;
    byId('need_send').dispatch('click'); await flush();
    out.disabledWhileSending = ta().disabled;
    answerAll('/api/need/need-0007/comment', 409, {error: '全局暂停中：只读，不发评论'}); await flush();
    out.checkedAfterFail = chk().checked;
    out.textAfterFail = ta().value;
    byId('need_send').dispatch('click'); await flush();   // 原样重试：同一个 request_id
    answerAll('/api/need/need-0007/comment', 409, {error: '还是暂停'}); await flush();
    chk().checked = false; chk().dispatch('change');       // 改去向：换新的 request_id
    byId('need_send').dispatch('click'); await flush();
    // 发送途中切到另一个需求写字；旧回包成功不能清掉它
    ctx.location.hash = '#need:need-0008'; listeners.hashchange(); await flush();
    answerAll('/api/need/need-0008', 200, NEED(null, {id: 'need-0008'})); await flush();
    ta().value = '另一个需求正在写'; ta().dispatch('input');
    answerAll('/api/need/need-0007/comment', 200, {ok: true, to_issue: false}); await flush();
    answerAll('/api/need/need-0007', 200, NEED(null)); await flush();
    out.otherDraft = run('needDrafts["need-0008"]');
    out.firstDraft = run('needDrafts["need-0007"] || ""');
    out.posts = posts();
  } else if (scenario === 'confirm-card') {
    ctx.location.hash = '#need:' + ARG.detail.id; run(src); await flush();
    answer('/api/need/' + ARG.detail.id, 200, ARG.detail); await flush();
    const b = byId('confirm_' + ARG.detail.id);
    out.button = b ? text(b) : null;
    if (b) { b.dispatch('click'); await flush(); }
    out.posts = posts();
  } else if (scenario === 'inbox-repo') {
    ctx.location.hash = ''; run(src); await flush();
    const card = {id: 'need-0001', title: 'A', number: 30, ws: 'demo', column: 'wait', flags: [{kind: 'wait', text: '审批 1 个等你答'}],
      next: '', who: 'lead-demo', updated: 't', unread: 0, loop: false, subs: {total: 0}, prs: [], comments: [], error: '', status: 'filed',
      issue_numbers: [30], issue_keys: ['owner/repo#30']};
    answerAll('/api/board', 200, {generated_at: 't', errors: [], ws: ['demo'], degrade: false,
      columns: [{key: 'wait', label: '待处理', count: 1, cards: [card]}]});
    const q = (id, issue, title) => ({id, kind: 'approval', title, ws: 'demo', issue, created: 't', n_questions: 1, status: 'open'});
    answerAll('/api/list', 200, {open_count: 2, needs_talk: [], groups: {todo: [q('demo-0001', 'owner/repo#30', 'MERGED_INTO_NEED'),
      q('other-0001', 'owner/other#30', 'UNRELATED APPROVAL MUST BE VISIBLE')], waiting: [], received: [], closed: []}});
    await flush();
    out.text = text(main);
  } else if (scenario === 'detail-safe') {
    ctx.location.hash = '#need:need-0007'; run(src); await flush();
    answer('/api/need/need-0007', 200, NEED(null, {body_html: ARG.body_html, timeline: [
      {kind: 'thread', at: 't', by: 'owner', source: '7788 讨论', text: '<img src=x onerror=alert(1)> OWNER_TEXT', receipt: {}},
      {kind: 'comment', at: 't', id: 1, author: 'demo-owner', source: 'Issue 评论 · 经 7788', self: 'owner', state: 'notified',
       edited: false, body_html: ARG.comment_html, url: 'javascript:alert(1)', receipt: {notified: true}}]})); await flush();
    out.text = text(main);
    out.htmls = htmlSets.slice();
    out.tags = findAll(e => ['SCRIPT', 'IMG', 'IFRAME'].includes(e.tagName)).length;
    out.hrefs = findAll(e => e.tagName === 'A').map(a => a.attrs.href);
    out.avatars = findAll(e => /\bav\b/.test(e.className), main).map(text);
    const chip = find(e => /\bchipw\b/.test(e.className), main);
    out.chip = {tag: chip.tagName, href: chip.attrs.href || null, onclick: (chip.listeners.click || []).length};
  }
  for (const r of requests) if (!r.done) { r.done = true; r.resolve({status: 200, json: async () => (r.path === '/api/list' ? EMPTY : {})}); }
  await flush();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e.stack); process.exit(2); });
"""


@unittest.skipUnless(shutil.which('node'), 'node not installed')
class PageFixTest(unittest.TestCase):
    def setUp(self):
        base.PageScriptTest.setUp(self)
        prefix = base.DOM_HARNESS[:base.DOM_HARNESS.index('(async () => {')]
        (self.tmp / 'harness.js').write_text(prefix + PAGE_FIX, encoding='utf-8')

    def run_page(self, scenario, arg=None):
        return base.PageScriptTest.run_page(self, scenario, json.dumps(arg or {}))

    def test_inline_draft_not_applied_to_new_version_and_errata_shown(self):
        """Claude I10 / GPT I4：刷新换版本后旧草稿不套到新选项上；勘误显示；提交用新版本、新选项文字。"""
        out = self.run_page('inline-stale')
        self.assertIsNone(out['draft'])
        self.assertEqual(out['checked'], 0)
        self.assertIn('没有套用到新版本', out['text'])
        self.assertIn('B. 乙', out['text'])  # 旧输入原样摆出来，方便对照重填
        self.assertTrue(any('ERRATA_MARK' in h for h in out['htmls']))
        self.assertEqual(out['postsAfterStale'], 0)
        self.assertIn('还有没答的', out['msg'])
        self.assertEqual(len(out['posts']), 1)
        body = out['posts'][0]['body']
        self.assertEqual((body['content_rev'], body['slots']['Q1']['choice']), (2, 'A. 甲'))

    def test_inline_inputs_disabled_while_submitting(self):
        """GPT I12：提交中禁用这版表单；成功回包只清掉实际提交的那版草稿。"""
        out = self.run_page('inline-busy')
        self.assertTrue(out['taDisabled'])
        self.assertTrue(out['radioDisabled'])
        self.assertEqual(out['post']['body']['slots']['Q1'], {'type': 'option', 'choice': 'A. 甲', 'text': '补充'})
        self.assertIsNone(out['draftAfter'])

    def test_inline_approval_shows_context_and_errata(self):
        """GPT I4：就地审批也显示动作 / 后果和勘误。"""
        out = self.run_page('inline-approval')
        joined = '\n'.join(out['htmls'])
        for mark in ('ACT_MARK', 'CONSEQ_MARK', 'ERRATA_MARK'):
            self.assertIn(mark, joined)
        self.assertIn('勘误', out['text'])

    def test_composer_keeps_issue_toggle_and_later_input(self):
        """Claude I11 / GPT I12：勾选在重新渲染 / 失败后保持；发送中不可编辑；改去向换 request_id；旧回包不清别的需求的输入。"""
        out = self.run_page('composer')
        self.assertTrue(out['checkedAfterRender'])
        self.assertTrue(out['disabledWhileSending'])
        self.assertTrue(out['checkedAfterFail'])
        self.assertEqual(out['textAfterFail'], '团队可见的结论')
        sends = [p['body'] for p in out['posts'] if p['path'] == '/api/need/need-0007/comment']
        self.assertEqual([s['to_issue'] for s in sends], [True, True, False])
        self.assertEqual(sends[0]['request_id'], sends[1]['request_id'])
        self.assertNotEqual(sends[1]['request_id'], sends[2]['request_id'])
        self.assertEqual(out['otherDraft'], '另一个需求正在写')
        self.assertEqual(out['firstDraft'], '')

    def test_inbox_merges_by_repo_and_number(self):
        """GPT I13：另一个 repo 同号的审批不能被合并掉。"""
        out = self.run_page('inbox-repo')
        self.assertIn('UNRELATED APPROVAL MUST BE VISIBLE', out['text'])
        self.assertNotIn('MERGED_INTO_NEED', out['text'])

    def test_detail_is_text_only_owner_avatar_and_chip_not_a_link(self):
        """Claude N5：详情页的页面层 XSS 断言；lead 反馈：Owner 头像显示「我」，在线 chip 不是链接、不像按钮。"""
        evil = '<script>alert(1)</script> <img src=x onerror=alert(1)> [点我](javascript:alert(1))'
        out = self.run_page('detail-safe', {'body_html': server.md(evil), 'comment_html': server.md(evil)})
        self.assertEqual(out['tags'], 0)
        allowed = {'p', 'ul', 'ol', 'li', 'code', 'pre', 'strong', 'em', 'b', 'i', 'br', 'blockquote', 'details', 'summary',
                   'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr'}
        for h in out['htmls']:  # 交给 innerHTML 的只有服务端白名单标签，且标签里没有事件 / javascript 属性
            for name, attrs in re.findall(r'<\s*/?\s*([A-Za-z0-9]+)([^>]*)>', h):
                self.assertIn(name.lower(), allowed, h)
                self.assertNotRegex(attrs.lower(), r'\bon\w+\s*=|javascript:', h)
        self.assertIn('<img src=x onerror=alert(1)> OWNER_TEXT', out['text'])
        self.assertNotIn('javascript:alert(1)', out['hrefs'])
        self.assertEqual(out['avatars'][:2], ['我', '我'])
        self.assertEqual(out['chip'], {'tag': 'SPAN', 'href': None, 'onclick': 0})
        rule = server.PAGE[server.PAGE.index('.chipw{'):]
        self.assertNotIn('border', rule[:rule.index('}')])


class ConfirmCardPageTest(v2.V2Base):
    @unittest.skipUnless(shutil.which('node'), 'node not installed')
    def test_real_detail_api_has_discuss_and_confirm_button_posts_latest_rev(self):
        """GPT I5：从真实的 card 接口产物打开详情，确认按钮可见，POST 绑定最新 card_rev；过期 v1 仍被拒。"""
        nid = self.assigned()
        self.as_lead('need', 'claim', nid)
        self.as_lead('need', 'card', nid, '--body', self.card_file('v1'))
        self.as_lead('need', 'card', nid, '--body', self.card_file('v2'))
        detail = json.loads(json.dumps(server.need_detail(nid), ensure_ascii=False))
        self.assertEqual(detail['discuss'], 'waiting_owner')
        page = PageFixTest('run_page')
        page.setUp()
        try:
            out = page.run_page('confirm-card', {'detail': detail})
        finally:
            shutil.rmtree(page.tmp, True)
        self.assertEqual(out['button'], '确认理解卡 v2')
        conf = [p for p in out['posts'] if p['path'] == f'/api/need/{nid}/confirm']
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]['body']['card_rev'], 2)
        with self.assertRaises(qstore.QError):
            qstore.owner_confirm(nid, 1)
        rec, _ = qstore.owner_confirm(nid, 2, conf[0]['body']['request_id'])
        self.assertEqual(rec['status'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
