"""7788 工作台 v2 · 第 2 轮审查后的修复（skills#25 / Issue #23，被审 head abfa347）：按 R2 报告的失败场景写，守住行为。

Claude R2：I1 接手请求发出后吞掉 lead 消息 / I3 扫描中途退出后评论永远不排投递 / Nit；
GPT R2：I1 排队时间被当成宽限期 / I2 可信账号为空 / I3 分页续扫边界 / I4 正文失败仍可 ack / I5 引用片段被当回声 /
I6 合批命令不可执行 / I7 回填长持全局锁。虚构数据：ws `demo`、repo `owner/repo`。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_question_inbox as base  # noqa: E402
import test_question_inbox_v2 as v2  # noqa: E402
import test_question_inbox_v2fix as fix1  # noqa: E402

qstore, server, board, poll, ghapi, send = v2.qstore, v2.server, v2.board, v2.poll, v2.ghapi, v2.send
ts, ISSUE = v2.ts, v2.ISSUE


class Fix2Base(fix1.FixBase):
    def seed_poster(self):
        """记下发布账号（等于以前成功发过一条评论）。"""
        with ghapi._write_lock():
            ghapi._note_poster('demo-owner')

    def forget_poster(self):
        st = ghapi._read_write_state()
        st.pop('poster', None)
        ghapi._save_write_state(st)


# ---------- GPT I1：宽限期从实际 POST 算；同一意图只 POST 一次 ----------

class SendOnceTest(Fix2Base):
    def race(self, nid, call):
        """A 已记下发送意图、卡在写门外排队（还没 POST）；期间把意图的时间回拨 10 分钟（排队很久），
        B 用同一个 request_id 重试并先发；最后放行 A。返回 (A 的结果或异常, B 的结果)。"""
        real = ghapi._write_lock
        arrived, go = threading.Event(), threading.Event()

        @contextmanager
        def gated():
            if threading.current_thread().name == 'A':
                arrived.set()
                go.wait(20)
            with real():
                yield
        out = {}

        def run_a():
            try:
                out['a'] = call()
            except qstore.QError as e:
                out['a'] = e
        with mock.patch.object(ghapi, '_write_lock', gated):
            t = threading.Thread(target=run_a, name='A')
            t.start()
            self.assertTrue(arrived.wait(20))
            with qstore.locked():
                cur = qstore.read_need(nid)
                for it in cur['send_intents'].values():
                    if it.get('state') == 'sending':
                        it['tried_at'] = it['at'] = ts(timedelta(seconds=600))
                qstore._save_need(cur)
            try:
                out['b'] = call()
            except qstore.QError as e:
                out['b'] = e
            go.set()
            t.join(30)
        return out.get('a'), out.get('b')

    def test_queued_original_and_retry_post_exactly_once_owner(self):
        nid = self.filed()
        a, b = self.race(nid, lambda: send.owner_message(nid, '排队很久的结论', 'req-queue-0001', True))
        self.assertEqual(sum('排队很久的结论' in x for x in self.bodies()), 1)
        self.assertNotIsInstance(b, Exception, b)
        if not isinstance(a, Exception):
            self.assertEqual(a['comment_id'], b['comment_id'])
        self.assertEqual(qstore.read_need(nid)['send_intents']['req-queue-0001']['state'], 'sent')

    def test_queued_original_and_retry_post_exactly_once_lead(self):
        nid = self.filed()
        ident = self.as_ident('w1:p1')
        self.race(nid, lambda: send.lead_comment(nid, '排队很久的 lead 结论', ident))
        self.assertEqual(sum('排队很久的 lead 结论' in x for x in self.bodies()), 1)

    def test_grace_counts_from_actual_post_not_from_queueing(self):
        """真的 POST 了、回包丢了：宽限期从 POST 那一刻算，排队时间不算。"""
        nid = self.filed()
        self.set_gh(post='fail:HTTP 502: Bad Gateway')
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, 'POST 过但不知道结果', 'req-grace-0001', True)
        with qstore.locked():  # 只把「记下意图」的时间回拨（像排队了很久），POST 时间不动
            cur = qstore.read_need(nid)
            cur['send_intents']['req-grace-0001']['tried_at'] = cur['send_intents']['req-grace-0001']['at'] = ts(timedelta(seconds=600))
            qstore._save_need(cur)
        self.set_gh(post='ok')
        with self.assertRaises(qstore.QError) as e:
            send.owner_message(nid, 'POST 过但不知道结果', 'req-grace-0001', True)
        self.assertEqual(e.exception.code, 409)
        self.assertFalse(any('POST 过但不知道结果' in x for x in self.bodies()))

    def test_refused_before_post_is_still_failed(self):
        nid = self.filed()
        qstore.set_paused(True)
        with self.assertRaises(ghapi.GHError):
            send.owner_message(nid, '发出之前就失败', 'req-prefail-01', True)
        it = qstore.read_need(nid)['send_intents']['req-prefail-01']
        self.assertEqual(it['state'], 'failed')
        self.assertFalse(it.get('posting_at'))


# ---------- GPT I2 / Claude Nit 1：可信账号为空时不放行 ----------

class TrustedAuthorTest(Fix2Base):
    def test_no_poster_record_means_unknown_not_any_author(self):
        """首次 link 就丢回包、没配置 Owner、没有发布账号记录：团队复制的旧评论不能成为 own；对账判为未知，不重发。"""
        nid = self.assigned('首次就丢回包')
        self.as_lead('need', 'claim', nid)
        self.issue_bodies(**{'30': f'需求：{nid}'})
        self.forget_poster()
        env = {k: v for k, v in os.environ.items() if k != 'QI_OWNER_LOGIN'}
        self.add_comment(30, 900, '团队早先的一条', author='teammate', created=ts(timedelta(seconds=30)))
        self.set_gh(post='lost')
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '30')
            self.assertNotEqual(r.returncode, 0)
            real = next(c for c in self.gh_state()['comments']['owner/repo'] if '按原话立项' in c['body'])
            self.edit_comment(900, real['body'])  # 团队把公开的整段（含尾标）复制到自己更早的评论
            self.forget_poster()
            self.set_gh(post='ok')
            r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '30')
            self.assertNotEqual(r.returncode, 0)  # 判不了是不是我们发的：不认、也不重发
            self.assertIn('发布账号', r.stderr)
            poll.scan()
            rec = qstore.read_need(nid)
            self.assertNotIn(900, {c['id'] for c in rec['own_comments']})
            self.assertEqual(next(c for c in rec['comments'] if c['id'] == 900)['self'], '')
            self.assertEqual(sum('按原话立项' in b for b in self.bodies()), 2)  # 真的那条 + 复制品；没有第三条
            # 之后有一次成功发评论记下了发布账号：再对账就认真的那条，不认复制品
            self.seed_poster()
            r = self.as_lead('need', 'link', nid, '--issue', ISSUE + '30')
            self.assertEqual(r.returncode, 0, r.stderr)
        rec = qstore.read_need(nid)
        self.assertEqual(rec['send_intents'][f'link-{nid}']['comment_id'], real['id'])
        self.assertNotIn(900, {c['id'] for c in rec['own_comments']})

    def test_comment_older_than_intent_never_matches(self):
        nid = self.filed()
        with qstore.locked():
            rec = qstore.read_need(nid)
            it, _ = qstore.send_intent(rec, 'req-old-00001', '正文', qstore.root_issue(rec)[2])
            it['posted_hash'] = qstore._hash(ghapi.norm_body(ghapi.posted_text('正文', ghapi.tag(nid, it['tag_id']))))
            qstore._save_need(rec)
        body = ghapi.posted_text('正文', ghapi.tag(nid, it['tag_id']))
        old = {'id': 1, 'body': body, 'user': {'login': 'demo-owner'}, 'created_at': ts(timedelta(hours=1))}
        new = dict(old, id=2, created_at=ts())
        self.assertFalse(ghapi.matches_intent(old, nid, it))
        self.assertTrue(ghapi.matches_intent(new, nid, it))


# ---------- GPT I3 / Claude I3 / Claude Nit 2：扫描可恢复、分页续扫不丢不卡 ----------

class ScanRecoveryTest(Fix2Base):
    def setUp(self):
        super().setUp()
        self.set_gh(strict_since=True)  # GitHub 文档：since 是「之后」（严格大于）

    def test_crash_after_comments_saved_before_queue_recovers_next_round(self):
        """Claude I3：评论已落盘、还没排投递时进程退出 → 下一轮补排投递。"""
        nid = self.filed()
        self.clear_delivery(nid)
        self.add_comment(30, 8101, '中途退出前读到的', author='teammate')
        with mock.patch.object(poll, '_settle', side_effect=RuntimeError('进程在这里退出')):
            with self.assertRaises(RuntimeError):
                poll.scan()
        self.assertEqual(qstore.read_need(nid)['delivery'], 'received')
        poll.scan()
        rec = qstore.read_need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to']), ('pending', 'lead'))
        self.deliver_now(nid)
        self.assertEqual(next(c for c in qstore.read_need(nid)['comments'] if c['id'] == 8101)['state'], 'notified')

    def test_page_boundary_same_second_is_not_lost_with_strict_since(self):
        """GPT I3 证据一：页尾和下一条同一秒、since 严格大于：不能漏。"""
        nid = self.filed()
        self.addCleanup(setattr, ghapi, 'LIST_MAX_PAGES', ghapi.LIST_MAX_PAGES)
        ghapi.LIST_MAX_PAGES = 1
        for i in range(99):
            self.add_comment(30, 21000 + i, f'较早 {i}', author='teammate', updated=ts(timedelta(seconds=400 - i)))
        same = ts(timedelta(seconds=200))
        self.add_comment(30, 21100, '同秒 A', author='teammate', updated=same)
        self.add_comment(30, 21101, '同秒 B', author='teammate', updated=same)
        for _ in range(5):
            if poll.scan()['owner/repo'].get('complete'):
                break
        got = {c['id'] for c in qstore.read_need(nid)['comments']}
        self.assertTrue({21100, 21101} <= got)
        self.assertEqual(len([i for i in got if 21000 <= i < 21200]), 101)

    def test_more_same_second_comments_than_a_round_does_not_stall(self):
        """GPT I3 证据二：一秒内的评论多于一轮能读的：照样读完、最终结束。"""
        nid = self.filed()
        self.addCleanup(setattr, ghapi, 'LIST_MAX_PAGES', ghapi.LIST_MAX_PAGES)
        ghapi.LIST_MAX_PAGES = 1
        same = ts(timedelta(seconds=100))
        for i in range(150):
            self.add_comment(30, 22000 + i, f'同秒 {i}', author='teammate', updated=same)
        done = False
        for _ in range(5):
            if poll.scan()['owner/repo'].get('complete'):
                done = True
                break
        self.assertTrue(done)
        self.assertEqual(len([c for c in qstore.read_need(nid)['comments'] if c['id'] >= 22000]), 150)

    def test_comment_edited_during_a_multi_page_scan_does_not_hide_others(self):
        """Claude Nit 2：多页扫描期间有评论被编辑，不会让其余评论跨页漏掉（按创建顺序翻页）。"""
        nid = self.filed()
        for i in range(150):
            self.add_comment(30, 23000 + i, f'第 {i} 条', author='teammate', updated=ts(timedelta(seconds=300 - i)))
        real = ghapi._run
        state = {'edited': False}

        def run(argv, stdin=None):
            res = real(argv, stdin)
            if not state['edited'] and any('page=1' in a for a in argv) and any('issues/comments?' in a for a in argv):
                state['edited'] = True
                self.edit_comment(23000, '第 0 条（扫描期间被编辑）', updated=ts())
            return res
        with mock.patch.object(ghapi, '_run', side_effect=run):
            poll.scan()
        got = {c['id'] for c in qstore.read_need(nid)['comments'] if c['id'] >= 23000}
        self.assertEqual(len(got), 150)


# ---------- GPT I4：正文读不到的那条不给确认口令 ----------

class InboxBodyFailureTest(Fix2Base):
    def test_failed_body_gets_no_ack_and_stays_unread(self):
        nid = self.filed()
        self.add_comment(30, 30001, '读不到正文的', author='teammate')
        self.add_comment(30, 30002, '正常的', author='teammate')
        poll.scan()
        real = ghapi.get_comment

        def flaky(repo, cid):
            if int(cid) == 30001:
                raise ghapi.GHError(409, 'temporary failure', 'http', 503)
            return real(repo, cid)
        import q  # noqa: E402
        with mock.patch.object(ghapi, 'get_comment', side_effect=flaky):
            r = qstore.lead_inbox(nid, [], self.as_ident('w1:p1'), poll.body_fetcher)
            bad = next(c for c in r['comments'] if c['id'] == 30001)
            self.assertFalse(bad['ack'])
            self.assertIn('读不到', bad['body'])
            raw = io.BytesIO()
            buf = io.TextIOWrapper(raw, encoding='utf-8')
            with redirect_stdout(buf):
                q.main(['need', 'inbox', nid])
            buf.flush()
        out = raw.getvalue().decode('utf-8')
        ack_line = next(l for l in out.splitlines() if l.startswith('读完执行'))
        self.assertNotIn('30001@', ack_line)
        self.assertIn('30002@', ack_line)
        self.assertIn('正文读不到，这条先不要确认', out)
        tokens = re.findall(r'--ack (\S+)', ack_line)
        qstore.lead_inbox(nid, tokens, self.as_ident('w1:p1'), poll.body_fetcher)
        left = qstore.lead_inbox(nid, [], self.as_ident('w1:p1'), poll.body_fetcher)
        self.assertEqual([c['id'] for c in left['comments']], [30001])
        self.assertTrue(left['comments'][0]['ack'])  # 恢复后重新拿到口令


# ---------- GPT I5 / Claude I2：回声只认完整尾标 + 正文哈希；引用不算 ----------

class EchoTest(Fix2Base):
    def test_team_quoting_our_tag_is_not_echo(self):
        nid = self.filed()
        send.owner_message(nid, '一条经 7788 发的', 'req-echo-src-01', True)
        ours = self.gh_state()['comments']['owner/repo'][-1]['body']
        for i in range(4):
            quote = ours if i % 2 else f'排查格式时引用片段 qi:{nid} i-deadbeef ；这是团队问题 {i}'
            self.add_comment(30, 44000 + i, quote, author='teammate')
            poll.scan()
            self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertFalse(rec['loop_suspect'])
        self.assertEqual({c['state'] for c in rec['comments'] if c['id'] >= 44000}, {'notified'})

    def test_our_own_comment_reposted_is_not_delivered_back_and_brakes_after_3_rounds(self):
        nid = self.filed()
        send.owner_message(nid, '我们发的那条', 'req-echo-own-01', True)
        ours = self.gh_state()['comments']['owner/repo'][-1]['body']
        self.deliver_now(nid)  # Owner 这条本身正常投给 lead
        self.clear_delivery(nid)
        for i in range(3):
            self.add_comment(30, 45000 + i, ours, author='demo-owner')  # 同一条内容被我们的账号又发了一遍
            poll.scan()
        rec = qstore.read_need(nid)
        self.assertTrue(rec['loop_suspect'])
        self.assertEqual({c['state'] for c in rec['comments'] if c['id'] >= 45000}, {'own'})
        self.assertNotEqual(rec.get('delivery'), 'pending')  # 自己发的没有投回给自己

    def test_skill_states_the_loop_brake_boundary(self):
        text = (base.QI / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('循环刹车只拦', text)
        self.assertIn('不经 `need comment` 直接用 gh 发的评论', text)
        ws = (base.QI.parent / 'workspace' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('--request-id', ws)  # Claude Nit 3：同一正文的第二条里程碑要换幂等键


# ---------- GPT I6：合批提示里的每条命令都能照原样执行 ----------

class PromptCommandTest(Fix2Base):
    def test_multi_need_multi_kind_prompt_commands_run(self):
        a = self.filed('需求 A', number=30)
        b = self.filed('需求 B', number=31)
        self.clear_delivery(a)
        self.clear_delivery(b)
        self.add_comment(30, 46001, 'A 的团队评论', author='teammate')
        poll.scan()
        qstore.owner_reply(b, 'B 的 7788 回复', 'req-batch-b-01')
        server.scan_lead_needs([qstore.read_need(a), qstore.read_need(b)])
        p = self.lead_prompts()[-1]
        cmds = re.findall(r'python3 \S+q\.py ((?:need inbox|need show) \S+)', p)
        self.assertIn(f'need inbox {a}', cmds)
        self.assertIn(f'need inbox {b}', cmds)
        for c in cmds:
            r = self.as_lead(*c.split())
            self.assertEqual(r.returncode, 0, (c, r.stderr))
        self.assertIn('A 的团队评论', self.as_lead('need', 'inbox', a).stdout)
        self.assertIn('B 的 7788 回复', self.as_lead('need', 'inbox', b).stdout)

    def test_router_takeover_prompt_has_one_command_per_need(self):
        recs = [{'id': 'need-0001'}, {'id': 'need-0002'}]
        p = server.router_prompt('takeover', recs)
        self.assertEqual(re.findall(r'router takeover (\S+)', p), ['need-0001', 'need-0002'])
        self.assertNotIn('router takeover need-0001 need-0002', p)


# ---------- GPT I7：回填不长持全局锁 ----------

class BackfillLockTest(Fix2Base):
    def test_large_backfill_holds_lock_briefly_and_owner_reply_is_not_blocked(self):
        nid = self.filed(number=30)
        self.put = fix1.TreeScopeTest.put.__get__(self)
        node = fix1.TreeScopeTest.node.__get__(self)
        self.put('owner/repo', node(30, subs=[(31, 'owner/repo')]))
        self.put('owner/repo', node(31))
        board.TREE.clear()
        data = self.gh_state()
        old = ts(timedelta(days=3))
        data['comments']['owner/repo'] += [
            {'id': 60000 + i, 'body': f'历史 {i}', 'user': {'login': 'teammate'}, 'created_at': old, 'updated_at': old,
             'issue_url': 'https://api.github.com/repos/owner/repo/issues/31',
             'html_url': f'https://github.com/owner/repo/issues/31#issuecomment-{60000 + i}'} for i in range(3000)]
        (self.tmp / 'gh.json').write_text(json.dumps(data, ensure_ascii=False))
        real_locked, holds, saves = qstore.locked, [], []

        @contextmanager
        def timed():
            with real_locked():
                t0 = time.monotonic()
                try:
                    yield
                finally:
                    holds.append(time.monotonic() - t0)
        real_save = qstore._save_need

        def counted(rec, event=None):
            if rec.get('id') == nid:
                saves.append(1)
            return real_save(rec, event)
        waits = []

        def reply():
            time.sleep(0.3)
            t0 = time.monotonic()
            qstore.owner_reply(nid, '回填期间的一句', 'req-during-bf1')
            waits.append(time.monotonic() - t0)
        with mock.patch.object(qstore, 'locked', timed), mock.patch.object(qstore, '_save_need', counted):
            t = threading.Thread(target=reply)
            t.start()
            poll.scan()
            t.join(60)
        rec = qstore.read_need(nid)
        self.assertEqual(len([c for c in rec['comments'] if c['id'] >= 60000]), 3000)
        self.assertTrue(any(x.get('text') == '回填期间的一句' for x in rec['thread']))  # 没被回填的旧快照覆盖
        self.assertLess(len(saves), 60)        # 按批落盘，不是每条一次
        self.assertLess(max(holds), 2.0)       # 单次持锁有上界
        self.assertLess(waits[0], 3.0)         # 前台操作不用等整段回填


# ---------- Claude I1：接手请求发出后，lead 的消息不能被吞 ----------

class TakeoverNoSwallowTest(Fix2Base):
    def test_original_lead_returns_after_router_was_asked(self):
        nid = self.filed()
        self.clear_delivery(nid)
        self.lead_panes(gone=('w1:p1',))
        self.add_comment(30, 8001, '不在时来的评论', author='teammate')
        poll.scan()
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual(rec['delivery'], 'held')
        self.assertIn('已请 Router 安排接手', rec['last_error'])
        self.assertEqual(len([c for c in self.router_prompts() if 'router takeover' in c[-1]]), 1)
        self.lead_panes()  # 原 lead 回来了
        r = self.as_router('router', 'takeover', nid)
        self.assertNotEqual(r.returncode, 0)  # Router 拒绝接手（原 lead 还在）
        self.add_comment(30, 8002, '回来之后的评论', author='teammate')
        poll.scan()
        qstore.owner_reply(nid, '回来之后的 7788 回复', 'req-back-00001')
        self.assertTrue([x for x in qstore.needs_due(qstore.datetime.now(qstore.TZ) + timedelta(seconds=400)) if x['id'] == nid])
        self.deliver_now(nid)
        rec = qstore.read_need(nid)
        self.assertEqual(rec['delivery'], 'sent')
        self.assertEqual({c['state'] for c in rec['comments'] if c['id'] in (8001, 8002)}, {'notified'})
        self.assertTrue(any(c[2:5] == ['agent', 'prompt', 'w1:p1'] for c in self.herdr_calls() if c[1] == 'demo'))
        self.assertNotIn('takeover_ask', rec)  # 现场核对 lead 在：请求清掉，下次不在时重新叫

    def test_new_lead_after_filed_absence_gets_messages(self):
        nid = self.filed()
        self.clear_delivery(nid)
        self.lead_panes(gone=('w1:p1',))
        qstore.owner_reply(nid, '立项后没人时的一句', 'req-fg-000001')
        self.deliver_now(nid)
        self.assertEqual(qstore.read_need(nid)['delivery'], 'held')
        qstore.claim_need(nid, self.as_ident('w1:p2'), takeover=True)
        self.deliver_now(nid)
        self.assertTrue(any(c[2:5] == ['agent', 'prompt', 'w1:p2'] for c in self.herdr_calls() if c[1] == 'demo'))
        self.assertIn('立项后没人时的一句',
                      [x['text'] for x in qstore.lead_inbox(nid, [], self.as_ident('w1:p2'), poll.body_fetcher)['thread']])

    def test_legacy_takeover_slot_sent_does_not_swallow(self):
        """旧记录停在 sent / takeover：lead 类消息照常排队。"""
        nid = self.filed()
        with qstore.locked():
            cur = qstore.read_need(nid)
            cur.update({'delivery': 'sent', 'delivery_kind': 'takeover', 'delivery_to': 'router'})
            qstore._save_need(cur)
        qstore.owner_reply(nid, '旧记录之后的一句', 'req-legacy-0001')
        rec = qstore.read_need(nid)
        self.assertEqual((rec['delivery'], rec['delivery_to']), ('pending', 'lead'))


if __name__ == '__main__':
    unittest.main()
