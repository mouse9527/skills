"""问题收件箱 · 只读任务视图：GitHub Issue / PR + herdr + 收件箱题，读的时候现算 stage。

不写任何本地状态：repo 清单是配置（~/.config/question-inbox/tasks.json，可用 QI_TASKS_CONFIG 覆盖）；
gh / herdr 的结果只缓存在内存（TTL 60 秒）。查询失败时保留上一次成功的数据并标出时间，绝不拿空列表冒充「没有任务」。
gh 和 herdr 都用 argv 调用，不经过 shell。
"""
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import qstore

TTL = 60
ISSUE_LIMIT = 100       # 每个 repo 最多拿多少个 open Issue
CLOSED_DAYS = 7         # 最近关闭：几天内
ACTIVE_HOURS = 72       # 在做：多久内有 PR / Issue 更新
REF_LIMIT = 15          # 每个 open Issue 看最近几条 PR 交叉引用（「Part of」会在 Issue 上留下交叉引用）
LINK_PREFIX = 'https://github.com/'
HOLD_LABEL_RE = re.compile(r'^(?:status[:/]\s*)?(?:blocked|on[- ]?hold|paused)$', re.I)
PART_OF_RE = re.compile(r'Part of\s+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)', re.I)
NAME_RE = re.compile(r'^(?:w|rv)-(\d+)(?:-|$)')

# 分组顺序 = 「要不要你动手」；stage 判定顺序见 stage_of
GROUPS = (('you', '等你'), ('stuck', '卡住'), ('doing', '在做'), ('todo', '待开工'), ('hold', '停着'), ('closed', '最近关闭'))
STAGES = {'answer': ('you', '等你回答'), 'merge': ('you', '等你合并'), 'accept': ('you', '等你验收'),
          'stuck': ('stuck', '卡住'), 'doing': ('doing', '在做'), 'todo': ('todo', '待开工'), 'hold': ('hold', '停着'),
          'done': ('closed', '已完成'), 'dropped': ('closed', '不做')}

QUERY = '''query($owner:String!,$name:String!,$closedQ:String!){
 repository(owner:$owner,name:$name){
  issues(states:OPEN,first:%(limit)d,orderBy:{field:UPDATED_AT,direction:DESC}){totalCount nodes{...I
   timelineItems(itemTypes:[CROSS_REFERENCED_EVENT,CONNECTED_EVENT],last:%(refs)d){nodes{
    ...on CrossReferencedEvent{source{...on PullRequest{...P}}} ...on ConnectedEvent{subject{...on PullRequest{...P}}}}}}}
  pullRequests(states:OPEN,first:50,orderBy:{field:UPDATED_AT,direction:DESC}){totalCount nodes{...P}}
 }
 closed:search(type:ISSUE,first:100,query:$closedQ){issueCount nodes{...on Issue{...I}}}
}
fragment I on Issue{number title url state stateReason updatedAt closedAt labels(first:20){nodes{name}}
 parent{number repository{nameWithOwner}} subIssuesSummary{total completed}
 closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{...P}}}
fragment P on PullRequest{number title url state isDraft updatedAt mergedAt body author{login}
 repository{nameWithOwner} statusCheckRollup{state}
 closingIssuesReferences(first:10){nodes{number repository{nameWithOwner}}}}''' % {'limit': ISSUE_LIMIT, 'refs': REF_LIMIT}


# ---------- 配置 ----------

def config_path():
    return Path(os.environ.get('QI_TASKS_CONFIG') or Path.home() / '.config' / 'question-inbox' / 'tasks.json')


def load_config():
    """[(ws, repo)]；配置缺失或不合法抛 ValueError（页面显示原因）。"""
    p = config_path()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError(f'没有配置文件 {p}（格式：{{"repos":[{{"ws":"demo","repo":"owner/repo"}}]}}）')
    except (OSError, ValueError) as e:
        raise ValueError(f'配置文件 {p} 读不了：{e}')
    out = []
    for r in (data.get('repos') if isinstance(data, dict) else None) or []:
        ws, repo = (r or {}).get('ws'), (r or {}).get('repo')
        if not (isinstance(ws, str) and qstore.WS_RE.match(ws) and isinstance(repo, str)
                and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)):
            raise ValueError(f'配置文件 {p} 里有不合法的条目：{r!r}')
        out.append((ws, repo))
    return out


def gh_bin():
    return os.environ.get('GH_BIN_PATH') or shutil.which('gh') or 'gh'


def gh_timeout():
    return float(os.environ.get('QI_GH_TIMEOUT') or 30)


# ---------- 查询（失败抛 RuntimeError，原因写进页面） ----------

def _iso(dt):
    return dt.astimezone(qstore.TZ).isoformat(timespec='seconds')


def fetch_repo(repo, at=None):
    at = at or datetime.now(qstore.TZ)
    owner, name = repo.split('/')
    closed_since = (at - timedelta(days=CLOSED_DAYS)).date().isoformat()
    argv = [gh_bin(), 'api', 'graphql', '-f', f'query={QUERY}', '-f', f'owner={owner}', '-f', f'name={name}',
            '-f', f'closedQ=repo:{repo} is:issue is:closed closed:>={closed_since}']
    try:
        res = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=gh_timeout())
    except subprocess.TimeoutExpired:
        raise RuntimeError('gh 超时')
    except OSError as e:
        raise RuntimeError(f'无法执行 gh（{gh_bin()}）：{e}')
    try:
        data = json.loads(res.stdout or 'null')
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get('errors') or res.returncode != 0 or not data.get('data'):
        errs = data.get('errors') if isinstance(data, dict) else None
        why = '; '.join(str(e.get('message', e)) for e in errs) if isinstance(errs, list) else \
            (res.stderr or res.stdout or '').strip()
        raise RuntimeError(f'gh 返回错误（rc={res.returncode}）：{why[:300]}')
    d = data['data']
    rp = d.get('repository')
    if not rp:
        raise RuntimeError(f'gh 找不到 repo {repo}')
    more = []
    if rp['issues']['totalCount'] > len(rp['issues']['nodes']):
        more.append(f'open Issue 共 {rp["issues"]["totalCount"]} 个，只显示最近更新的 {len(rp["issues"]["nodes"])} 个')
    if rp['pullRequests']['totalCount'] > len(rp['pullRequests']['nodes']):
        more.append(f'open PR 共 {rp["pullRequests"]["totalCount"]} 个，只看了最近更新的 {len(rp["pullRequests"]["nodes"])} 个')
    if d['closed']['issueCount'] > len(d['closed']['nodes']):
        more.append(f'{CLOSED_DAYS} 天内关闭的 Issue 还有更多（共 {d["closed"]["issueCount"]} 个）')
    prs = list(rp['pullRequests']['nodes'])
    for n in rp['issues']['nodes']:
        for ev in ((n.pop('timelineItems', None) or {}).get('nodes') or []):
            pr = (ev or {}).get('source') or (ev or {}).get('subject')
            if pr and pr.get('number'):
                prs.append(pr)
        prs += [pr for pr in ((n.get('closedByPullRequestsReferences') or {}).get('nodes') or []) if pr]
    return {'open': rp['issues']['nodes'], 'closed': [n for n in d['closed']['nodes'] if n], 'prs': prs, 'more': more}


def fetch_herdr():
    """[{session, pane}]；socket 不存在的 session 跳过（不创建）。每个 session 的错误单独记。"""
    agents, errors = [], []
    base = qstore.socket_root()
    sessions = sorted(p.name for p in base.iterdir() if (p / 'herdr.sock').exists()) if base.is_dir() else []
    for s in sessions:
        r = qstore.pane_list(s)
        if r['ok']:
            agents += [{'session': s, 'pane': p} for p in r['panes'] if isinstance(p, dict)]
        elif r['code'] not in ('server_not_running', 'no_socket'):  # 残留 socket：没有在跑的 session，不算错
            errors.append(f'{s}：{r["code"]} {r["message"]}'.strip())
    if errors and not agents:
        raise RuntimeError('herdr pane list 失败：' + '；'.join(errors))
    return {'agents': agents, 'errors': errors}


# ---------- 内存缓存：每个来源单独保留上一次成功的数据 ----------

class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.src = {}   # key -> {'data', 'at', 'error', 'tried'}

    def get(self, keys_fetchers, refresh=False, now=None):
        now = now or datetime.now(qstore.TZ)
        with self.lock:  # 同一时间只有一次刷新；别的请求等它完成后直接用缓存
            due = [(k, f) for k, f in keys_fetchers
                   if refresh or k not in self.src or (now - self.src[k]['tried']).total_seconds() >= TTL]
            if due:
                with ThreadPoolExecutor(max_workers=min(8, len(due))) as ex:
                    futs = {k: ex.submit(f) for k, f in due}
                    for k, fut in futs.items():
                        old = self.src.get(k, {'data': None, 'at': None})
                        try:
                            self.src[k] = {'data': fut.result(), 'at': now, 'error': '', 'tried': now}
                        except Exception as e:  # noqa: BLE001 任何失败都只记原因，保留旧数据
                            self.src[k] = {'data': old['data'], 'at': old['at'], 'error': str(e) or type(e).__name__,
                                           'tried': now}
            return {k: dict(self.src[k]) for k, _ in keys_fetchers}


CACHE = Cache()


# ---------- 拼装 ----------

def safe_url(u):
    return u if isinstance(u, str) and u.startswith(LINK_PREFIX) else ''


def _repo_of(node, default):
    r = (node or {}).get('repository') or {}
    return (r.get('nameWithOwner') or default).lower()


def _ago(t, now):
    ts = qstore.parse_time(t.replace('Z', '+00:00')) if isinstance(t, str) else None
    if not ts:
        return ''
    s = max(0, (now - ts).total_seconds())
    if s < 3600:
        return f'{int(s // 60)} 分钟前' if s >= 60 else '刚刚'
    if s < 86400:
        return f'{int(s // 3600)} 小时前'
    return f'{int(s // 86400)} 天前'


def _recent(t, now, hours):
    ts = qstore.parse_time(t.replace('Z', '+00:00')) if isinstance(t, str) else None
    return bool(ts) and (now - ts).total_seconds() <= hours * 3600


def agent_refs(agents, repos):
    """{(ws, 号): [agent]}，依据是名字（w-<号>… / rv-<号>-…）或 cwd（<ws>/(issues|stories)/<号>/）。"""
    root = qstore.workspaces_root()
    out = {}
    for a in agents:
        p, s = a['pane'], a['session']
        name = next((p.get(k) for k in ('name', 'agent_name', 'label') if isinstance(p.get(k), str) and p.get(k)), '')
        hits = []
        m = NAME_RE.match(name)
        if m:
            hits.append((s, int(m.group(1)), 'name'))
        try:
            rel = Path(p.get('cwd') or '').relative_to(root).parts
        except ValueError:
            rel = ()
        if len(rel) >= 3 and rel[1] in ('issues', 'stories') and rel[2].isdigit():
            hits.append((rel[0], int(rel[2]), 'cwd'))
        for ws, n, by in hits:
            info = {'name': name or p.get('terminal_title_stripped') or p.get('pane_id') or '', 'by': by,
                    'status': p.get('agent_status') or 'unknown', 'agent': p.get('agent') or '',
                    'session': s, 'pane': p.get('pane_id') or ''}
            lst = out.setdefault((ws, n), [])
            if not any(x['session'] == s and x['pane'] == info['pane'] for x in lst):
                lst.append(info)
    return out


def pr_refs(prs, repo):
    """{(repo, 号): [pr]}：GitHub 关联（closingIssuesReferences）优先，再按正文 `Part of <repo>#<号>`。"""
    out, seen = {}, set()
    for pr in prs:
        key = (_repo_of(pr, repo), pr.get('number'))
        if key in seen:
            continue
        seen.add(key)
        targets = [(_repo_of(n, repo), n.get('number')) for n in
                   ((pr.get('closingIssuesReferences') or {}).get('nodes') or [])]
        for m in PART_OF_RE.finditer(pr.get('body') or ''):
            targets.append(((m.group(1) or key[0]).lower(), int(m.group(2))))  # 不写 repo = PR 自己的 repo
        info = {'number': pr.get('number'), 'url': safe_url(pr.get('url')), 'state': pr.get('state'),
                'draft': bool(pr.get('isDraft')), 'checks': ((pr.get('statusCheckRollup') or {}).get('state') or ''),
                'updated': pr.get('updatedAt') or '', 'author': ((pr.get('author') or {}).get('login') or ''),
                'repo': key[0], 'full': (pr.get('repository') or {}).get('nameWithOwner') or repo}
        for t in dict.fromkeys(targets):
            out.setdefault(t, []).append(info)
    return out


def question_refs(allq):
    """({(repo, 号): [题]}, [未关联的待答题])；只挂有效、未答（status=open）的题。"""
    linked, loose = {}, []
    for qid, (m, _) in sorted(allq.items()):
        if m.get('status') != 'open' or qstore.invalid_reason(m, allq):
            continue
        q = {'id': qid, 'title': m.get('title') or qid, 'ws': m.get('ws') or '', 'issue': m.get('issue') or 'none',
             'blocking': m.get('blocking') == 'yes', 'created': m.get('created') or ''}
        mm = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)', q['issue'])
        if mm:
            linked.setdefault((mm.group(1).lower(), int(mm.group(2))), []).append(q)
        else:
            loose.append(q)
    return linked, loose


def pr_name(p, t):
    """同 repo 写 PR #N，别的 repo 写 owner/repo#N。"""
    return f'PR #{p["number"]}' if p['repo'] == t['repo'].lower() else f'PR {p["full"]}#{p["number"]}'


def stage_of(t, now):
    """[(stage, 依据)]，按「最需要处理」排好；第一个是主 stage，其余作小标签。"""
    hits = []
    if t['state'] == 'CLOSED':
        r = t.get('state_reason') or ''
        k = 'done' if r == 'COMPLETED' else 'dropped'
        hits.append((k, ('完成' if k == 'done' else '不做' + ('（重复）' if r == 'DUPLICATE' else '')) +
                     ' · ' + (_ago(t.get('closed'), now) or '时间未知')))
    if t['questions']:
        hits.append(('answer', '题 ' + '、'.join(q['id'] for q in t['questions'])))
    if t['state'] == 'CLOSED':
        return hits
    open_prs = [p for p in t['prs'] if p['state'] == 'OPEN']
    merged = [p for p in t['prs'] if p['state'] == 'MERGED']
    green = [p for p in open_prs if not p['draft'] and p['checks'] in ('SUCCESS', '')]
    if green:
        hits.append(('merge', '、'.join(pr_name(p, t) for p in green) +
                     (' checks 全绿' if all(p['checks'] for p in green) else ' 没有 checks')))
    elif merged and not open_prs:
        hits.append(('accept', '、'.join(pr_name(p, t) for p in merged) + ' 已合并，Issue 未关'))
    blocked = [a for a in t['agents'] if a['status'] == 'blocked']
    failed = [p for p in open_prs if p['checks'] in ('FAILURE', 'ERROR')]
    if blocked or failed:
        hits.append(('stuck', '；'.join([f'{a["name"]} blocked' for a in blocked] +
                                        [f'{pr_name(p, t)} checks 失败' for p in failed])))
    hold = [l for l in t['labels'] if HOLD_LABEL_RE.match(l)]
    recent = [p for p in t['prs'] if _recent(p['updated'], now, ACTIVE_HOURS)]
    if t['agents']:
        a = t['agents'][0]
        more = f' 等 {len(t["agents"])} 个' if len(t['agents']) > 1 else ''
        last = max([p['updated'] for p in t['prs']] + [t['updated'] or ''])
        hits.append(('doing', f'{a["name"]}（{a["status"]}，依据 {"名字" if a["by"] == "name" else "cwd"}）{more}'
                     + (f' · {_ago(last, now)}动过' if last else '')))
    elif hold:
        hits.append(('hold', 'label ' + '、'.join(hold)))
    elif recent or _recent(t['updated'], now, ACTIVE_HOURS) or [p for p in open_prs if p not in green + failed]:
        p = max(recent, key=lambda x: x['updated']) if recent else None
        who = (pr_name(p, t) + (f' {p["author"]}' if p['author'] else '')) if p else 'Issue'
        when = _ago(p['updated'] if p else t['updated'], now)
        hits.append(('doing', f'{who} · {when}动过' if when else who))
    if hold and t['agents']:
        hits.append(('hold', 'label ' + '、'.join(hold)))
    if not t['agents'] and not open_prs and not merged and not hold and not any(h[0] == 'doing' for h in hits):
        hits.append(('todo', '没有 agent、没有 PR'))
    return hits


def _task(ws, repo, n):
    return {'ws': ws, 'repo': repo, 'number': n['number'], 'title': n.get('title') or '', 'url': safe_url(n.get('url')),
            'state': n.get('state') or 'OPEN', 'state_reason': n.get('stateReason') or '',
            'updated': n.get('updatedAt') or '', 'closed': n.get('closedAt') or '',
            'labels': [l['name'] for l in ((n.get('labels') or {}).get('nodes') or []) if l and l.get('name')],
            'parent': ({'repo': _repo_of(n['parent'], repo), 'number': n['parent']['number']} if n.get('parent') else None),
            'sub': (n.get('subIssuesSummary') if (n.get('subIssuesSummary') or {}).get('total') else None)}


def build(repos, results, herdr_res, allq, now=None):
    """纯函数：配置 + 各来源结果 + 收件箱 → 视图数据。results: {repo: cache entry}。"""
    now = now or datetime.now(qstore.TZ)
    agents = agent_refs((herdr_res.get('data') or {}).get('agents') or [], repos)
    linked_q, loose_q = question_refs(allq)
    tasks, sources = {}, []
    for ws, repo in repos:
        ent = results.get(repo) or {}
        sources.append({'name': repo, 'ws': ws, 'ok': not ent.get('error'), 'error': ent.get('error') or '',
                        'at': _iso(ent['at']) if ent.get('at') else '', 'more': (ent.get('data') or {}).get('more') or [],
                        'has_data': ent.get('data') is not None})
        data = ent.get('data')
        if data is None:
            continue
        prs = pr_refs(data['prs'], repo.lower())
        for n in data['open'] + data['closed']:
            if not n or not n.get('number'):
                continue
            t = _task(ws, repo, n)
            key = (repo.lower(), t['number'])
            if key in tasks:
                continue
            t['prs'] = sorted(prs.get(key, []), key=lambda p: p['number'])
            t['agents'] = agents.get((ws, t['number']), [])
            t['questions'] = linked_q.pop(key, [])
            hits = stage_of(t, now)
            main = hits[0][0]
            t['stage'] = {'key': main, 'label': STAGES[main][1], 'group': STAGES[main][0]}
            t['why'] = [h[1] for h in hits]
            t['tags'] = [{'key': h[0], 'label': STAGES[h[0]][1], 'why': h[1]} for h in hits[1:]]
            tasks[key] = t
    for qs in linked_q.values():  # issue 字段对不上任何任务（repo 不在配置、或 Issue 不在列表里）
        loose_q += qs
    sources.append({'name': 'herdr', 'ws': '', 'ok': not herdr_res.get('error'), 'error': herdr_res.get('error') or '',
                    'at': _iso(herdr_res['at']) if herdr_res.get('at') else '',
                    'more': [f'部分 session 查询失败：{e}' for e in (herdr_res.get('data') or {}).get('errors', [])],
                    'has_data': herdr_res.get('data') is not None})
    return {'generated_at': _iso(now), 'sources': sources, 'groups': _group(tasks), 'unlinked': loose_q,
            'stale': any(s['error'] for s in sources), 'count': len(tasks)}


ORDER = ('answer', 'merge', 'accept', 'stuck', 'doing', 'todo', 'hold', 'done', 'dropped')


def _key(t):
    return (t['repo'].lower(), t['number'])


def _sorted(items):
    """stage 优先级 → 最近动过的在前 → 号小的在前。"""
    items = sorted(items, key=lambda t: t['number'])
    items.sort(key=lambda t: t['closed'] or t['updated'], reverse=True)
    items.sort(key=lambda t: ORDER.index(t['stage']['key']))
    return items


def _group(tasks):
    """分组（空组不出现）→ 组内按 ws → 父子：子 Issue 缩进放在父 Issue 下面；
    父 Issue 不在这一组时，同一个父的子 Issue 聚在一行「父 #N 标题（在别的组）」下面。"""
    out = []
    for gk, gtitle in GROUPS:
        items = [t for t in tasks.values() if t['stage']['group'] == gk]
        if not items:
            continue
        wss = []
        for ws in sorted({t['ws'] for t in items}):
            mine = _sorted([t for t in items if t['ws'] == ws])
            here = {_key(t) for t in mine}
            kids = {}
            for t in mine:
                if t['parent']:
                    kids.setdefault((t['parent']['repo'], t['parent']['number']), []).append(t)
            rows, placed = [], set()

            def put(t, depth):
                if _key(t) in placed:
                    return
                placed.add(_key(t))
                rows.append(dict(t, depth=depth, kind='task'))
                for c in kids.get(_key(t), []):
                    put(c, min(depth + 1, 2))
            for t in mine:
                pk = (t['parent']['repo'], t['parent']['number']) if t['parent'] else None
                if pk in here or _key(t) in placed:
                    continue
                if pk:  # 父 Issue 在别的组 / 没拿到：先放一行父的引用
                    p = tasks.get(pk)
                    rows.append({'kind': 'parent', 'repo': p['repo'] if p else pk[0], 'number': pk[1], 'depth': 0,
                                 'title': p['title'] if p else '', 'url': p['url'] if p else '',
                                 'stage': p['stage']['label'] if p else '', 'sub': p['sub'] if p else None})
                    for c in kids[pk]:
                        put(c, 1)
                else:
                    put(t, 0)
            for t in mine:  # 父子成环等异常情况兜底：不丢
                put(t, 0)
            wss.append({'ws': ws, 'tasks': rows})
        out.append({'key': gk, 'title': gtitle, 'count': len(items), 'ws': wss})
    return out


def view(refresh=False, now=None):
    """页面和 CLI 共用的入口。配置错误也返回结构化数据（error 字段），不抛。"""
    try:
        repos = load_config()
    except ValueError as e:
        return {'generated_at': _iso(now or datetime.now(qstore.TZ)), 'config_error': str(e), 'sources': [],
                'groups': [], 'unlinked': question_refs(qstore.all_questions())[1], 'stale': True, 'count': 0}
    fetchers = [(repo, (lambda r=repo: fetch_repo(r))) for repo in dict.fromkeys(r for _, r in repos)]
    fetchers.append(('__herdr__', fetch_herdr))
    got = CACHE.get(fetchers, refresh=refresh, now=now)
    herdr_res = got.pop('__herdr__')
    return build(repos, got, herdr_res, qstore.all_questions(), now=now)


# ---------- CLI 文本 ----------

def render_text(v, ws=None):
    lines = []
    if v.get('config_error'):
        lines.append(f'✗ {v["config_error"]}')
    for s in v.get('sources', []):
        if s['error']:
            keep = f'；下面是 {s["at"]} 的数据' if s['has_data'] else '；没有可用数据（不代表没有任务）'
            lines.append(f'✗ 无法刷新 {s["name"]}：{s["error"]}{keep}')
        for m in s['more']:
            lines.append(f'… {s["name"]}：{m}')
    lines.append(f'数据时间：{v["generated_at"]}')
    for g in v['groups']:
        blocks = [w for w in g['ws'] if not ws or w['ws'] == ws]
        if not blocks:
            continue
        lines.append(f'\n## {g["title"]}（{sum(len(w["tasks"]) for w in blocks)}）')
        for w in blocks:
            lines.append(f'### {w["ws"]}')
            for t in w['tasks']:
                ind = '  ' * t['depth'] + ('↳ ' if t['depth'] else '- ')
                if t['kind'] == 'parent':
                    sub = f'，子 Issue {t["sub"]["completed"]}/{t["sub"]["total"]} 已关' if t['sub'] else ''
                    lines.append(f'- 父 {t["repo"]}#{t["number"]} {t["title"]}（{t["stage"] or "不在列表里"}{sub}）')
                    continue
                sub = f' [子 Issue {t["sub"]["completed"]}/{t["sub"]["total"]} 已关]' if t['sub'] else ''
                tags = ''.join(f' +{x["label"]}' for x in t['tags'])
                lines.append(f'{ind}{t["repo"]}#{t["number"]} {t["title"]}{sub} · {t["stage"]["label"]}{tags}'
                             f' · {t["why"][0]}  {t["url"]}')
    loose = [q for q in v.get('unlinked', []) if not ws or q['ws'] == ws]
    if loose:
        lines.append(f'\n## 未关联的待答题（{len(loose)}）')
        for q in loose:
            lines.append(f'- {q["id"]} · {q["issue"]} · {q["title"]}')
    return '\n'.join(lines)
