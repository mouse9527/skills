#!/usr/bin/env python3
"""问题收件箱 · lead 用的 CLI。用法见同目录 SKILL.md；`python3 q.py <命令> --help` 看参数。

退出码：0 成功；1 参数或内容不合法；3 状态不允许（已撤回 / 被替换 / 版本不符）；4 身份不符。
"""
import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qstore  # noqa: E402
import tasks  # noqa: E402

EXIT = {400: 1, 404: 1, 409: 3, 410: 3, 403: 4}


def page_url(qid=''):
    return f'http://127.0.0.1:{qstore.port()}/' + (f'#{qid}' if qid else '')


def caller(args):
    return qstore.identify(args.herdr_session or os.environ.get('HERDR_SESSION'),
                           args.pane or os.environ.get('HERDR_PANE_ID'))


def cmd_ask(args):
    ws = args.ws or qstore.ws_from_cwd(os.getcwd())
    if not ws:
        raise qstore.QError(400, f'当前目录不在 {qstore.workspaces_root()}/<ws>/ 下，请用 --ws 指定')
    body = Path(args.body).read_text(encoding='utf-8')
    ident = caller(args)
    fields = {'ws': ws, 'title': args.title, 'issue': args.issue, 'blocking': args.blocking, 'group': args.group}
    qid, warnings = qstore.publish(fields, body, ident, supersedes=args.supersedes)
    print(f'已发布 {qid}：{page_url(qid)}')
    if args.supersedes:
        print(f'{args.supersedes} 已撤回（被 {qid} 替换）')
    for w in warnings:
        print(f'提示：{w}')
    print(f'聊天里最多带一行：待你回答：{qid}')


def refuse_if_owner_closed(qid):
    """Owner 在页面上关闭的题：先于身份核对给出明确提示（lead 会话丢了也一样），退出码 3。"""
    msg = qstore.owner_closed(qid)
    if msg:
        raise qstore.QError(409, msg)


def cmd_receive(args):
    ident = None
    rc = 0
    for i, qid in enumerate(args.ids):
        try:
            refuse_if_owner_closed(qid)
            ident = ident or caller(args)
            text = qstore.receive(qid, ident)
        except qstore.QError as e:
            print(f'✗ {qid}：{e.message}', file=sys.stderr)
            rc = max(rc, EXIT.get(e.code, 1))
            continue
        if i:
            print('\n' + '=' * 60 + '\n')
        print(text)
    return rc


def cmd_resolve(args):
    refuse_if_owner_closed(args.id)
    qstore.resolve(args.id, args.answer_rev, args.note, caller(args))
    print(f'{args.id} 已闭环（Rev {args.answer_rev}）')


def cmd_withdraw(args):
    refuse_if_owner_closed(args.id)
    qstore.withdraw(args.id, args.reason, caller(args))
    print(f'{args.id} 已撤回')


def cmd_erratum(args):
    refuse_if_owner_closed(args.id)
    rev = qstore.erratum(args.id, args.note, caller(args), [q.strip().upper() for q in args.drop or []])
    print(f'{args.id} 已补勘误，content_rev = {rev}（Owner 若正在填写，提交时会收到冲突提示，输入不会丢）')


def cmd_status(args):
    allq = qstore.all_questions()
    me = None
    if not args.all:
        try:
            me = caller(args)['lead_session']
        except qstore.QError as e:
            print(f'（无法确定当前 lead：{e.message}；列出全部）', file=sys.stderr)
    groups = {'等 Owner 回答': [], '已答，未领取': [], '已领取，未闭环': [], 'Owner 已关闭（近 7 天）': []}
    week_ago = datetime.now(qstore.TZ) - timedelta(days=7)
    for qid, (m, _) in sorted(allq.items()):
        if (args.ws and m.get('ws') != args.ws) or (me and m.get('lead_session') != me):
            continue
        if qstore.closed_by_owner(m):
            if (qstore.parse_time(m.get('updated')) or week_ago) >= week_ago:
                groups['Owner 已关闭（近 7 天）'].append(m)
            continue
        if qstore.invalid_reason(m, allq):
            continue
        n = qstore.geti(m, 'answer_rev')
        if m.get('status') == 'open':
            groups['等 Owner 回答'].append(m)
        elif m.get('status') == 'answered':
            got = m.get('delivery') == 'received' and qstore.geti(m, 'received_rev') == n
            groups['已领取，未闭环' if got else '已答，未领取'].append(m)
    for name, items in groups.items():
        print(f'## {name}（{len(items)}）')
        for m in items:
            label, why, _ = qstore.delivery_view(m)
            extra = f'  [{label}{"：" + why if why else ""}]' if m.get('status') != 'open' else ''
            blk = ' blocking' if m.get('blocking') == 'yes' else ''
            print(f'- {m["id"]}{blk} Rev {m.get("answer_rev")} · {m.get("title")}{extra}')
    print(f'页面：{page_url()}')


def cmd_show(args):
    meta, body = qstore.read(args.id)
    why = qstore.owner_closed_message(meta) or qstore.invalid_reason(meta, qstore.all_questions())
    if why:
        print(f'⚠️ {args.id} {why}：不要执行其中的回答\n')
    print(qstore.render(meta, body))


def cmd_tasks(args):
    """只读：和网页「任务」同一份数据（每次现查 gh + herdr + 收件箱）。"""
    v = tasks.view(refresh=True)
    if args.json:
        if args.ws:
            v = dict(v, groups=[dict(g, ws=[w for w in g['ws'] if w['ws'] == args.ws]) for g in v['groups']],
                     unlinked=[q for q in v['unlinked'] if q['ws'] == args.ws])
            v['groups'] = [g for g in v['groups'] if g['ws']]
        print(json.dumps(v, ensure_ascii=False, indent=1))
    else:
        print(tasks.render_text(v, ws=args.ws))
        print(f'页面：{page_url("tasks")}')
    return 1 if v.get('config_error') or any(s['error'] and not s['has_data'] for s in v['sources']) else 0


def cmd_doctor(_args):
    problems = doctor()
    for p in problems:
        print(f'✗ {p}')
    print('doctor: 没发现问题' if not problems else f'doctor: {len(problems)} 个问题')
    return 1 if problems else 0


def server_running():
    """服务进程锁被占着就是服务在跑：这时 sending 是正常的进行中状态。"""
    path = qstore.state_dir() / 'server.lock'
    if not path.exists():
        return False
    with open(path, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def doctor():
    out = []
    allq = qstore.all_questions()
    for qid, (m, body) in allq.items():
        if m.get('id') != qid:
            out.append(f'{qid}: 文件名与 id {m.get("id")!r} 不一致')
        if m.get('status') not in qstore.STATUSES:
            out.append(f'{qid}: status 不合法 {m.get("status")!r}')
        if m.get('delivery') not in qstore.DELIVERIES:
            out.append(f'{qid}: delivery 不合法 {m.get("delivery")!r}')
        if m.get('blocking') not in ('yes', 'no'):
            out.append(f'{qid}: blocking 不合法')
        for k in ('content_rev', 'answer_rev', 'delivery_rev', 'received_rev', 'resolved_rev'):
            if not re.fullmatch(r'\d+', m.get(k) or ''):
                out.append(f'{qid}: {k} 不是数字')
        lead, sp = qstore.split_body(body)
        n = qstore.geti(m, 'answer_rev')
        if n > 0:
            try:
                qstore.current_slots(m, body)
            except qstore.QError as e:
                out.append(f'{qid}: {e.message}')
        revs = [r['rev'] for r in qstore.answer_revs(sp)]
        if revs != list(range(1, n + 1)):
            out.append(f'{qid}: 回答里的 Rev {revs} 与 answer_rev {n} 对不上（rev 回退或缺失）')
        errata = len(re.findall(r'^- \S+（content_rev \d+）', sp.get('勘误', ''), re.M))
        if qstore.geti(m, 'content_rev') != 1 + errata:
            out.append(f'{qid}: content_rev {m.get("content_rev")} 与勘误条数 {errata} 对不上')
        for k in ('delivery_rev', 'received_rev', 'resolved_rev'):
            if qstore.geti(m, k) > n:
                out.append(f'{qid}: {k} 大于 answer_rev')
        if m.get('delivery') == 'received' and qstore.geti(m, 'received_rev') != qstore.geti(m, 'delivery_rev'):
            out.append(f'{qid}: delivery=received 但 received_rev ≠ delivery_rev')
        if m.get('resolved_by', '') not in ('', 'lead', 'owner') or m.get('withdrawn_by', '') not in ('', 'lead', 'owner'):
            out.append(f'{qid}: resolved_by / withdrawn_by 不合法')
        if m.get('status') == 'resolved' and m.get('resolved_by') != 'owner' \
                and qstore.geti(m, 'resolved_rev') != qstore.geti(m, 'received_rev'):
            out.append(f'{qid}: lead 闭环但 resolved_rev ≠ received_rev')
        if m.get('status') == 'resolved' and m.get('resolved_by') == 'owner' \
                and qstore.geti(m, 'resolved_rev') != qstore.geti(m, 'answer_rev'):
            out.append(f'{qid}: Owner 关闭但 resolved_rev ≠ answer_rev')
        if (m.get('delivery') == 'sending' or m.get('withdraw_notice') == 'sending') and not server_running():
            out.append(f'{qid}: 停在 sending 且服务没在运行（服务启动时会改为 uncertain）')
        sup = m.get('supersedes')
        if sup:
            if sup not in allq:
                out.append(f'{qid}: supersedes 悬空（{sup} 不存在）')
            elif sup == qid:
                out.append(f'{qid}: 替换自己')
            elif allq[sup][0].get('supersedes') == qid and 'withdrawn' not in (
                    m.get('status'), allq[sup][0].get('status')):
                out.append(f'{qid}: 与 {sup} 互相替换且都未撤回')
            seen, cur = {qid}, sup
            while cur in allq and allq[cur][0].get('supersedes'):
                cur = allq[cur][0]['supersedes']
                if cur in seen:
                    out.append(f'{qid}: 替换关系成环')
                    break
                seen.add(cur)
        b = qstore.superseder(qid, allq)
        if b and m.get('status') != 'withdrawn':
            out.append(f'{qid}: 已被 {b} 替换但头部还没标 withdrawn（服务启动时会补齐）')
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog='q.py', description='问题收件箱（lead 用）')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        sp.add_argument('--herdr-session', help='默认取 $HERDR_SESSION（会现场核对）')
        sp.add_argument('--pane', help='默认取 $HERDR_PANE_ID（会现场核对）')
        return sp

    a = add('ask', cmd_ask, '校验后发布一题')
    a.add_argument('--body', required=True, help='正文 markdown 文件')
    a.add_argument('--title', required=True)
    a.add_argument('--issue', required=True, help='owner/repo#123 或 none')
    a.add_argument('--blocking', required=True, choices=('yes', 'no'))
    a.add_argument('--group', default='')
    a.add_argument('--supersedes')
    a.add_argument('--ws')
    r = add('receive', cmd_receive, '核对身份后领取回答')
    r.add_argument('ids', nargs='+')
    r = add('resolve', cmd_resolve, '闭环')
    r.add_argument('id')
    r.add_argument('--answer-rev', type=int, required=True)
    r.add_argument('--note', required=True)
    r = add('withdraw', cmd_withdraw, '撤回')
    r.add_argument('id')
    r.add_argument('--reason', required=True)
    r = add('erratum', cmd_erratum, 'open 状态下补勘误（content_rev + 1）')
    r.add_argument('id')
    r.add_argument('--note', required=True)
    r.add_argument('--drop', action='append', metavar='Qn', help='同时撤回这个子问题，可重复')
    r = add('status', cmd_status, '列出本 lead 的题')
    r.add_argument('--ws')
    r.add_argument('--all', action='store_true')
    r = add('show', cmd_show, '只读查看')
    r.add_argument('id')
    add('doctor', cmd_doctor, '一致性检查')
    r = sub.add_parser('tasks', help='只读任务视图：GitHub Issue / PR + herdr + 收件箱，现查现算')
    r.set_defaults(fn=cmd_tasks)
    r.add_argument('--ws')
    r.add_argument('--json', action='store_true')
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Owner 原话要逐字输出，不受 locale 影响
        stream.reconfigure(encoding='utf-8')
    try:
        return args.fn(args) or 0
    except qstore.QError as e:
        print(f'✗ {e.message}', file=sys.stderr)
        return EXIT.get(e.code, 1)
    except OSError as e:
        print(f'✗ {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
