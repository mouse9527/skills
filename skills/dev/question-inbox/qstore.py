"""问题收件箱的存储层：一题一个 markdown 文件（头部 key: value，正文 markdown）。

所有写操作只走本模块，且都在目录锁 .lock 内完成；q.py（lead 用）和 server.py（网页 + 投递）共用。
只用标准库。路径、端口、herdr 位置都能用环境变量覆盖（测试用），见 root() 等函数。
"""
import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


# ---------- 配置（每次调用时读环境变量，测试可以随时改） ----------

def root():
    return Path(os.environ.get('QUESTION_INBOX_DIR') or Path.home() / 'workspaces' / '_questions')


def state_dir():
    return Path(os.environ.get('QI_STATE_DIR') or Path.home() / '.local' / 'state' / 'question-inbox')


def workspaces_root():
    return Path(os.environ.get('QI_WORKSPACES_ROOT') or Path.home() / 'workspaces')


def herdr_bin():
    return os.environ.get('HERDR_BIN_PATH') or str(Path.home() / '.local' / 'bin' / 'herdr')


def socket_root():
    return Path(os.environ.get('QI_HERDR_SOCKET_ROOT') or Path.home() / '.config' / 'herdr' / 'sessions')


def port():
    return int(os.environ.get('QI_PORT') or 7788)


def herdr_timeout():
    return float(os.environ.get('QI_HERDR_TIMEOUT') or 15)


def backoff_schedule():
    raw = os.environ.get('QI_BACKOFF') or '10,30,120,300'
    return [float(x) for x in raw.split(',') if x.strip()]


STALE_IDLE, STALE_WORKING = 120, 900  # sent 之后多久没 receive 算「未领取」（秒）


# ---------- 常量 ----------

FIELDS = (
    'id', 'ws', 'title', 'issue', 'group', 'blocking', 'status', 'content_rev', 'answer_rev', 'withdrawn_qs',
    'herdr_session', 'pane_id', 'agent_kind', 'lead_session',
    'delivery', 'delivery_rev', 'delivery_kind', 'received_rev', 'attempt_id', 'attempts', 'hold_count',
    'next_try_at', 'last_error', 'lead_status', 'herdr_accepted_at', 'received_at', 'withdraw_notice',
    'supersedes', 'superseded_by', 'withdraw_reason', 'withdrawn_by', 'resolved_at', 'resolution', 'resolved_rev',
    'resolved_by',
    'created', 'updated', 'warnings',
)
STATUSES = ('open', 'answered', 'resolved', 'withdrawn')
DELIVERIES = ('none', 'pending', 'sending', 'sent', 'held', 'uncertain', 'received', 'cancelled')
NOTICES = ('', 'pending', 'sending', 'sent', 'held', 'uncertain')
SLOT_TYPES = ('option', 'free', 'suggested', 'need_context', 'defer', 'withdrawn')
LEAD_SECTIONS = ('问题', '背景', '证据', '选项', '建议')
SYSTEM_SECTIONS = ('勘误', '草稿回答', '回答', '时间线')  # 固定顺序放在正文末尾，只由本模块写
# 依赖聊天记录才看得懂的措辞（实际踩过的坑）
FORBIDDEN_PHRASES = ('上一条消息', '见上文', '如上所述', '同上', '前面说的', '上面提到')
ID_RE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9_.-]*)-(\d{4,})$')
WS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
ISSUE_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+$')
Q_LINE_RE = re.compile(r'^Q(\d+)[.．、]\s*(\S.*)$')
SUGGEST_RE = re.compile(r'^\s*(?:[-*]\s*)?Q(\d+)\s*[:：]\s*(\S.*)$')
OPTION_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(\S.*)$')
SLOT_LINE_RE = re.compile(r'^- (Q\d+) \[(\w+)\] (\{.*\})$')
REV_HEAD_RE = re.compile(r'^### Rev (\d+) · (\S+)(?: · 已被 Rev (\d+) 更正)?$')
MAX_TEXT = 20000


class QError(Exception):
    """code 对应网页的 HTTP 状态：400 不合法、404 不存在、409 版本冲突/状态不允许、410 已撤回或被替换、403 身份不符。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def parse_time(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def geti(meta, key):
    try:
        return int(meta.get(key) or 0)
    except ValueError:
        return 0


def path_of(qid):
    if not ID_RE.match(qid or ''):
        raise QError(400, f'非法问题 id: {qid!r}')
    return root() / f'{qid}.md'


# ---------- 文件格式 ----------

def parse(text):
    meta, body = {}, text
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end != -1:
            for line in text[4:end].split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 5:]
    return meta, body


def one_line(v):
    return ' '.join(str(v).split('\n')).replace('\r', ' ')


def render(meta, body):
    keys = [k for k in FIELDS if k in meta] + sorted(k for k in meta if k not in FIELDS)
    head = '\n'.join(f'{k}: {one_line(meta[k])}' for k in keys)
    return f'---\n{head}\n---\n{body}'


def fenced(lines):
    """每行是否在代码围栏里（围栏行本身也算）。只按 `\\n` 分行的结果传进来。"""
    out, fence = [], False
    for line in lines:
        is_fence = line.lstrip().startswith('```')
        out.append(fence or is_fence)
        if is_fence:
            fence = not fence
    return out


def split_body(body):
    """(lead 写的正文, {系统段名: 内容})。系统段从第一个围栏外的 `## <系统段名>` 行开始。"""
    lines = body.split('\n')
    infence = fenced(lines)
    start = next((i for i, l in enumerate(lines) if not infence[i] and _sys_heading(l)), len(lines))
    lead = '\n'.join(lines[:start]).strip('\n')
    sys_parts, cur = {}, None
    for line in lines[start:]:
        name = _sys_heading(line)
        if name:
            cur = name
            sys_parts.setdefault(cur, [])
        elif cur:
            sys_parts[cur].append(line)
    return lead, {k: '\n'.join(v).strip('\n') for k, v in sys_parts.items()}


def _sys_heading(line):
    m = re.match(r'^##\s+(\S+)\s*$', line)
    return m.group(1) if m and m.group(1) in SYSTEM_SECTIONS else None


def join_body(lead, sys_parts):
    out = lead.rstrip('\n') + '\n'
    for name in SYSTEM_SECTIONS:
        if sys_parts.get(name, '').strip():
            out += f'\n## {name}\n\n{sys_parts[name].strip(chr(10))}\n'
    return out


def sections(lead):
    """lead 正文里的二级段 {标题: 内容}；代码块里的 ## 不算标题。"""
    out, cur = {}, None
    lines = lead.split('\n')
    for line, infence in zip(lines, fenced(lines)):
        m = None if infence else re.match(r'^##\s+(.+?)\s*$', line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        elif cur:
            out[cur].append(line)
    return {k: '\n'.join(v).strip() for k, v in out.items()}


def questions(lead):
    """[(qid, 文本)]，来自 `## 问题`。"""
    out = []
    for line in sections(lead).get('问题', '').split('\n'):
        m = Q_LINE_RE.match(line.strip())
        if m:
            out.append((f'Q{int(m.group(1))}', m.group(2).strip()))
    return out


def options(lead):
    """{qid: [选项行]}，来自 `## 选项` 下的 `### Qn` 子段。"""
    out, cur = {}, None
    for line in sections(lead).get('选项', '').split('\n'):
        m = re.match(r'^###\s+Q(\d+)\b', line.strip())
        if m:
            cur = f'Q{int(m.group(1))}'
            out.setdefault(cur, [])
            continue
        om = OPTION_RE.match(line)
        if cur and om and not line.startswith('  '):
            out[cur].append(om.group(1).strip())
    return out


def suggestions(lead):
    out = {}
    for line in sections(lead).get('建议', '').split('\n'):
        m = SUGGEST_RE.match(line)
        if m:
            out.setdefault(f'Q{int(m.group(1))}', m.group(2).strip())
    return out


def slot_lines(slots):
    return '\n'.join(f'- {q} [{s["type"]}] {json.dumps(s, ensure_ascii=False, sort_keys=True)}'
                     for q, s in sorted(slots.items(), key=lambda kv: int(kv[0][1:])))


def parse_slots(text):
    slots = {}
    for line in text.split('\n'):
        m = SLOT_LINE_RE.match(line.strip())
        if m:
            try:
                slots[m.group(1)] = json.loads(m.group(3))
            except json.JSONDecodeError:
                continue
    return slots


def answer_revs(sys_parts):
    """[{rev, at, corrected_by, slots}]，来自 `## 回答`。"""
    revs, cur = [], None
    for line in sys_parts.get('回答', '').split('\n'):
        m = REV_HEAD_RE.match(line.strip())
        if m:
            cur = {'rev': int(m.group(1)), 'at': m.group(2), 'corrected_by': int(m.group(3) or 0), 'lines': []}
            revs.append(cur)
        elif cur is not None:
            cur['lines'].append(line)
    for r in revs:
        r['slots'] = parse_slots('\n'.join(r.pop('lines')))
    return revs


def draft(sys_parts):
    text = sys_parts.get('草稿回答', '')
    return {'slots': parse_slots(text), 'note': text.split('\n')[0] if text else ''}


# ---------- 读写 ----------

def read(qid):
    p = path_of(qid)
    if not p.is_file():
        raise QError(404, f'{qid} 不存在')
    meta, body = parse(p.read_text(encoding='utf-8'))
    meta.setdefault('id', qid)
    return meta, body


def all_questions():
    out = {}
    d = root()
    if not d.is_dir():
        return out
    for p in sorted(d.glob('*.md')):
        if p.name.startswith('.') or not ID_RE.match(p.stem):
            continue
        try:
            meta, body = parse(p.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError):
            continue
        meta.setdefault('id', p.stem)
        out[p.stem] = (meta, body)
    return out


def _write_atomic(path, text):
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _save(meta, body, event=None):
    t = now()
    meta['updated'] = t
    if event:
        lead, sp = split_body(body)
        sp['时间线'] = (sp.get('时间线', '') + f'\n- {t} {one_line(event)}').strip('\n')
        body = join_body(lead, sp)
    _write_atomic(path_of(meta['id']), render(meta, body))


@contextmanager
def locked():
    """所有读改写都在这把锁里做。flock 不可重入：持锁期间不要再调用会加锁的公开函数。"""
    d = root()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ---------- 有效性 ----------

def superseder(qid, allq):
    """非 withdrawn 且 supersedes == qid 的题 id（有多条时取最小）。"""
    hits = sorted(i for i, (m, _) in allq.items() if m.get('supersedes') == qid and m.get('status') != 'withdrawn')
    return hits[0] if hits else None


def invalid_reason(meta, allq):
    b = superseder(meta['id'], allq)
    if b:
        return f'已被 {b} 替换'
    if meta.get('status') == 'withdrawn':
        if meta.get('superseded_by'):
            return f'已被 {meta["superseded_by"]} 替换'
        if meta.get('withdrawn_by') == 'owner':
            return f'已由 {meta.get("withdraw_reason")}'
        return f'已撤回：{meta.get("withdraw_reason") or "未写原因"}'
    return None


def closed_by_owner(meta):
    return (meta.get('status') == 'withdrawn' and meta.get('withdrawn_by') == 'owner') or \
        (meta.get('status') == 'resolved' and meta.get('resolved_by') == 'owner')


def owner_closed_message(meta):
    """Owner 关闭的题给 lead 的统一提示；不是 Owner 关闭的返回 None。"""
    if not closed_by_owner(meta):
        return None
    why = meta.get('resolution') if meta.get('status') == 'resolved' else meta.get('withdraw_reason')
    return (f'{meta["id"]} 已由 Owner 在页面上关闭（{why}）：不要再 receive / resolve / withdraw 它，'
            f'也不要按其中的回答继续行动；需要的话另开一题')


def owner_closed(qid):
    """只读预检：q.py 在核对身份之前先调它，lead 会话丢了也能拿到明确提示。"""
    try:
        meta, _ = read(qid)
    except QError:
        return None
    return owner_closed_message(meta)


def _load_checked(qid, allq):
    if qid not in allq:
        path_of(qid)  # 校验 id 格式
        raise QError(404, f'{qid} 不存在')
    meta, body = allq[qid]
    closed = owner_closed_message(meta)
    if closed:
        raise QError(409, closed)
    why = invalid_reason(meta, allq)
    if why:
        raise QError(410, f'{qid} {why}，不能再保存、提交、投递或领取')
    return meta, body


# ---------- 发布 ----------

def validate(fields, lead):
    """硬校验返回 errors，软提示返回 warnings。fields: ws/title/issue/blocking/group。"""
    errors, warnings = [], []
    if not WS_RE.match(fields.get('ws') or ''):
        errors.append(f'ws 不合法：{fields.get("ws")!r}')
    if not (fields.get('title') or '').strip() or '\n' in fields.get('title', ''):
        errors.append('title 不能为空且只能一行')
    if not (fields.get('issue') == 'none' or ISSUE_RE.match(fields.get('issue') or '')):
        errors.append('issue 必须形如 owner/repo#123，或写 none')
    if fields.get('blocking') not in ('yes', 'no'):
        errors.append('blocking 只能是 yes / no')
    lines = lead.split('\n')
    infence = fenced(lines)
    if lines and sum(1 for l in lines if l.lstrip().startswith('```')) % 2:
        errors.append('代码围栏 ``` 没有闭合')
    for i, line in enumerate(lines):
        name = None if infence[i] else _sys_heading(line)
        if name:
            errors.append(f'第 {i + 1} 行 `## {name}` 由系统写入，正文里不能有（引用请放进 ``` 代码块）')
    sec = sections(lead)
    for name in LEAD_SECTIONS:
        if not sec.get(name):
            errors.append(f'缺少 `## {name}` 段或内容为空')
    qs = questions(lead)
    raw_q = [l for l in sec.get('问题', '').split('\n') if l.strip()]
    if len(raw_q) != len(qs):
        errors.append('`## 问题` 每行必须是一条 `Qn. ……？`')
    nums = [int(q[1:]) for q, _ in qs]
    if nums and nums != list(range(1, len(nums) + 1)):
        errors.append(f'Q 编号必须从 Q1 起连续且不重复，实际是 {", ".join("Q%d" % n for n in nums)}')
    opts, sugg = options(lead), suggestions(lead)
    for q, _ in qs:
        if q not in opts:
            errors.append(f'`## 选项` 里缺少 `### {q}`')
        if q not in sugg:
            errors.append(f'`## 建议` 里缺少 `{q}: 建议 —— 理由`')
    for p in FORBIDDEN_PHRASES:
        if p in lead:
            errors.append(f'正文含「{p}」：问题必须不看聊天记录也能看懂')
    if fields.get('blocking') == 'yes' and not sec.get('证据'):
        errors.append('blocking: yes 时 `## 证据` 不能为空')
    ev = sec.get('证据', '')
    if ev and not re.search(r'\S+:\d+|`[^`]+`|https?://|\d', ev):
        warnings.append('证据里看不到 文件:行号、命令、URL 或数字')
    if len(sec.get('背景', '').split('\n')) > 12:
        warnings.append('背景超过 12 行')
    if len(lead.split('\n')) > 200:
        warnings.append('全文超过 200 行')
    if not sec.get('答完之后'):
        warnings.append('缺少 `## 答完之后`')
    return errors, warnings


def _alloc_id(ws, allq):
    seq_path = root() / '_seq'
    try:
        seq = json.loads(seq_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seq = {}
    used = [int(ID_RE.match(i).group(2)) for i in allq if ID_RE.match(i).group(1) == ws]
    n = max([int(seq.get(ws, 0))] + used) + 1
    while path_of(f'{ws}-{n:04d}').exists():
        n += 1
    seq[ws] = n
    _write_atomic(seq_path, json.dumps(seq, ensure_ascii=False, sort_keys=True) + '\n')  # 先持久化预留，再建文件
    return f'{ws}-{n:04d}'


_after_new_hook = None  # 测试用：模拟「写完 B、还没改 A」时崩溃


def publish(fields, lead, identity, supersedes=None):
    """校验并发布一题，返回 (id, warnings)。identity: herdr_session/pane_id/agent_kind/lead_session。"""
    lead = lead.strip('\n')
    errors, warnings = validate(fields, lead)
    for k in ('herdr_session', 'pane_id', 'agent_kind', 'lead_session'):
        if not identity.get(k):
            errors.append(f'身份信息缺少 {k}')
    if errors:
        raise QError(400, '\n'.join(errors))
    with locked():
        allq = all_questions()
        if supersedes:
            if supersedes not in allq:
                raise QError(400, f'--supersedes 引用的 {supersedes} 不存在')
            a, _ = allq[supersedes]
            why = invalid_reason(a, allq)
            if why:
                raise QError(400, f'{supersedes} {why}，不能再被替换')
            if a.get('status') not in ('open', 'answered'):
                raise QError(400, f'{supersedes} 状态是 {a.get("status")}，只能替换 open / answered 的题')
            if a.get('ws') != fields['ws']:
                raise QError(400, f'{supersedes} 属于 ws {a.get("ws")}，不能跨 ws 替换')
            check_owner(a, identity)  # 没有显式交接，替换不能改归属
        if any(m.get('issue') == fields['issue'] != 'none' and m.get('status') == 'open' and i != supersedes
               for i, (m, _) in allq.items()):
            warnings.append(f'issue {fields["issue"]} 下已经有 open 的题')
        qid = _alloc_id(fields['ws'], allq)
        t = now()
        meta = {
            'id': qid, 'ws': fields['ws'], 'title': fields['title'].strip(), 'issue': fields['issue'],
            'group': fields.get('group') or '', 'blocking': fields['blocking'], 'status': 'open',
            'content_rev': '1', 'answer_rev': '0', 'withdrawn_qs': '',
            'herdr_session': identity['herdr_session'], 'pane_id': identity['pane_id'],
            'agent_kind': identity['agent_kind'], 'lead_session': identity['lead_session'],
            'delivery': 'none', 'delivery_rev': '0', 'delivery_kind': '', 'received_rev': '0', 'attempt_id': '',
            'attempts': '0', 'hold_count': '0', 'next_try_at': '', 'last_error': '', 'lead_status': '',
            'herdr_accepted_at': '', 'received_at': '', 'withdraw_notice': '',
            'supersedes': supersedes or '', 'superseded_by': '', 'withdraw_reason': '',
            'resolved_at': '', 'resolution': '', 'resolved_rev': '0',
            'created': t, 'updated': t, 'warnings': ' | '.join(warnings),
        }
        _save(meta, join_body(lead, {}), f'发布（content_rev 1）' + (f'，替换 {supersedes}' if supersedes else ''))
        if _after_new_hook:
            _after_new_hook()
        if supersedes:
            a, abody = allq[supersedes]
            _withdraw(a, abody, f'被 {qid} 替换', superseded_by=qid)
    return qid, warnings


DELIVERED = ('sending', 'sent', 'uncertain', 'received')  # lead 可能已经拿到过回答


def _queue_notice(meta):
    """撤回 / Owner 关闭时，回答可能已经在 lead 手里：单独排一条通知（送不到不影响撤回或关闭本身）。"""
    if meta.get('delivery') in DELIVERED:
        meta['withdraw_notice'] = 'pending'
        meta['next_try_at'] = now()
        meta['hold_count'] = '0'


def _withdraw(meta, body, reason, superseded_by='', by='lead'):
    _queue_notice(meta)
    meta['status'] = 'withdrawn'
    meta['withdraw_reason'] = reason
    meta['withdrawn_by'] = by
    if superseded_by:
        meta['superseded_by'] = superseded_by
    if meta.get('delivery') != 'none':
        meta['delivery'] = 'cancelled'
    _save(meta, body, f'撤回：{reason}')


def check_owner(meta, ident):
    """调用者 (herdr_session, lead_session) 必须等于记录里的值，否则 QError(403)。"""
    want = (meta.get('herdr_session'), meta.get('lead_session'))
    got = ((ident or {}).get('herdr_session'), (ident or {}).get('lead_session'))
    if not all(got) or got != want:
        raise QError(403, f'{meta.get("id")} 不属于你这个 lead 会话（身份不符）：只有发题的原 lead 能领取、闭环、'
                          f'撤回、勘误或替换它。如果你是接手的新 lead，请让 Owner 知道')


def withdraw(qid, reason, ident):
    if not (reason or '').strip():
        raise QError(400, '必须写 --reason')
    with locked():
        allq = all_questions()
        meta, body = _load_checked(qid, allq)
        check_owner(meta, ident)
        if meta.get('status') not in ('open', 'answered'):
            raise QError(409, f'{qid} 状态是 {meta.get("status")}，不能撤回')
        _withdraw(meta, body, reason.strip())


def erratum(qid, note, ident, drop_qs=()):
    """open 状态下补勘误（content_rev + 1），可顺带撤回部分子问题。"""
    if not (note or '').strip():
        raise QError(400, '勘误内容不能为空')
    with locked():
        allq = all_questions()
        meta, body = _load_checked(qid, allq)
        check_owner(meta, ident)
        if meta.get('status') != 'open':
            raise QError(409, f'{qid} 状态是 {meta.get("status")}：只有 open 的题能补勘误，实质修改请 supersede')
        lead, sp = split_body(body)
        qids = [q for q, _ in questions(lead)]
        dropped = [q for q in (meta.get('withdrawn_qs') or '').split(',') if q]
        for q in drop_qs:
            if q not in qids:
                raise QError(400, f'{q} 不在问题列表里')
            if q not in dropped:
                dropped.append(q)
        if len(dropped) >= len(qids):
            raise QError(400, '不能撤回全部子问题，请用 withdraw')
        rev = geti(meta, 'content_rev') + 1
        meta['content_rev'] = str(rev)
        meta['withdrawn_qs'] = ','.join(sorted(dropped, key=lambda q: int(q[1:])))
        text = '\n  '.join(note.strip().split('\n'))
        extra = f'（撤回子问题 {", ".join(drop_qs)}）' if drop_qs else ''
        sp['勘误'] = (sp.get('勘误', '') + f'\n- {now()}（content_rev {rev}）{extra}{text}').strip('\n')
        _save(meta, join_body(lead, sp), f'勘误 content_rev {rev}{extra}')
        return rev


# ---------- 回答（网页） ----------

def _check_slots(lead, meta, slots, complete):
    if not isinstance(slots, dict):
        raise QError(400, 'slots 必须是对象')
    qids = [q for q, _ in questions(lead)]
    dropped = set(q for q in (meta.get('withdrawn_qs') or '').split(',') if q)
    opts, sugg = options(lead), suggestions(lead)
    out = {}
    for q, s in slots.items():
        if q not in qids:
            raise QError(400, f'{q} 不在问题列表里')
        if q in dropped:
            continue
        if not isinstance(s, dict) or s.get('type') in (None, ''):
            continue
        t = s.get('type')
        if t not in SLOT_TYPES or t == 'withdrawn':
            raise QError(400, f'{q} 的答案类型不合法：{t!r}')
        clean = {'type': t}
        for k in ('choice', 'text'):
            v = s.get(k)
            if v not in (None, ''):
                if not isinstance(v, str) or len(v) > MAX_TEXT:
                    raise QError(400, f'{q}.{k} 必须是不超过 {MAX_TEXT} 字的文本')
                clean[k] = v
        if t == 'option' and complete and clean.get('choice') not in opts.get(q, []):
            raise QError(400, f'{q} 选的选项不在 `### {q}` 里')
        if t == 'free' and complete and not clean.get('text', '').strip():
            raise QError(400, f'{q} 自由回答不能为空')
        if t == 'suggested':
            if q not in sugg:
                raise QError(400, f'{q} 没有建议可按')
            clean['choice'] = sugg[q]  # 存下当时看到的建议原文
        out[q] = clean
    for q in dropped:
        out[q] = {'type': 'withdrawn'}
    if complete:
        missing = [q for q in qids if q not in out]
        if missing:
            raise QError(400, f'还有子问题没答：{", ".join(missing)}')
    return out


def _store_draft(meta, body, slots, note, event=None):
    lead, sp = split_body(body)
    sp['草稿回答'] = f'{note}\n{slot_lines(slots)}'.strip('\n')
    _save(meta, join_body(lead, sp), event)


def _writable_for_owner(qid, allq):
    meta, body = _load_checked(qid, allq)
    if meta.get('status') not in ('open', 'answered'):
        raise QError(409, f'{qid} 已闭环（{meta.get("status")}），不能再改；需要的话请 lead 另开一题')
    return meta, body


def save_draft(qid, content_rev, slots):
    with locked():
        allq = all_questions()
        meta, body = _writable_for_owner(qid, allq)
        lead, _ = split_body(body)
        clean = _check_slots(lead, meta, slots, complete=False)
        if int(content_rev) != geti(meta, 'content_rev'):
            _store_draft(meta, body, clean, f'保存于 {now()}（版本冲突时自动保留，基于 content_rev {content_rev}）')
            raise QError(409, f'问题已更新到 content_rev {meta["content_rev"]}，请刷新；你填的内容已保留为草稿')
        _store_draft(meta, body, clean, f'保存于 {now()}（content_rev {content_rev}）')


def submit(qid, content_rev, answer_rev, slots):
    """返回新的 answer_rev。"""
    with locked():
        allq = all_questions()
        meta, body = _writable_for_owner(qid, allq)
        lead, sp = split_body(body)
        if int(content_rev) != geti(meta, 'content_rev') or int(answer_rev) != geti(meta, 'answer_rev'):
            clean = _check_slots(lead, meta, slots, complete=False)
            _store_draft(meta, body, clean, f'保存于 {now()}（提交时版本冲突，自动保留，基于 content_rev '
                                            f'{content_rev} / answer_rev {answer_rev}）')
            raise QError(409, f'问题或回答已被更新（content_rev {meta["content_rev"]}，answer_rev '
                              f'{meta["answer_rev"]}），请刷新；你填的内容已保留为草稿')
        clean = _check_slots(lead, meta, slots, complete=True)
        n = geti(meta, 'answer_rev') + 1
        t = now()
        old = sp.get('回答', '')
        old = re.sub(r'^(### Rev \d+ · \S+)$', rf'\1 · 已被 Rev {n} 更正', old, flags=re.M)
        sp['回答'] = (old + f'\n\n### Rev {n} · {t}\n{slot_lines(clean)}').strip('\n')
        sp.pop('草稿回答', None)
        was_delivered = meta.get('delivery') in ('sending', 'sent', 'uncertain', 'received')
        meta.update({
            'answer_rev': str(n), 'status': 'answered', 'delivery': 'pending', 'delivery_rev': str(n),
            'delivery_kind': 'correction' if n > 1 and was_delivered else 'answer',
            'attempt_id': '', 'attempts': '0', 'hold_count': '0', 'next_try_at': t, 'last_error': '',
        })
        _save(meta, join_body(lead, sp), f'Owner 提交 Rev {n}' + ('（更正）' if n > 1 else ''))
        return n


def has_notice_channel(meta):
    """撤回的题、Owner 关闭的题：只剩撤回 / 关闭通知这一条投递。"""
    return meta.get('status') == 'withdrawn' or closed_by_owner(meta)


def _append_error(old, new):
    """last_error 只保留一行：旧原因在前、新记录在后，超长时截掉最旧的部分。"""
    text = f'{old}；{new}' if old else new
    return one_line(text)[-300:]


def renotify(qid):
    """网页「重新通知」：回答 / 更正投递（uncertain、已投未领取、held）或撤回通知（uncertain、held）重新排队。
    投递时会重新核对身份；原来的失败原因保留在 last_error 和时间线里。"""
    with locked():
        allq = all_questions()
        if qid in allq and has_notice_channel(allq[qid][0]):
            meta, body = allq[qid]
            if meta.get('withdraw_notice') not in ('uncertain', 'held'):
                raise QError(409, f'{qid} 的撤回 / 关闭通知当前不需要重新通知（{meta.get("withdraw_notice") or "无"}）')
            old = meta.get('last_error') or ''
            meta.update({'withdraw_notice': 'pending', 'next_try_at': now(), 'hold_count': '0',
                         'last_error': _append_error(old, '重新通知撤回：这条可能已经在 lead 的队列里')})
            _save(meta, body, f'Owner 点了重新通知撤回（之前：{old or "无记录"}）')
            return
        meta, body = _load_checked(qid, allq)
        if meta.get('status') != 'answered' or meta.get('delivery') not in ('uncertain', 'sent', 'held'):
            raise QError(409, f'{qid} 当前不需要重新通知（status {meta.get("status")}，delivery {meta.get("delivery")}）')
        old = meta.get('last_error') or ''
        meta.update({'delivery': 'pending', 'next_try_at': now(), 'hold_count': '0',
                     'last_error': _append_error(old, '重新通知：这条可能已经在 lead 的队列里')})
        _save(meta, body, f'Owner 点了重新通知（Rev {meta.get("answer_rev")}，之前：{old or "无记录"}）')


def owner_close(qid, content_rev, answer_rev, reason):
    """Owner 在页面上「由我关闭」：lead 会话丢了也能闭环，不放宽 lead 侧身份校验。
    open → withdrawn（withdrawn_by: owner）；answered → resolved（resolved_by: owner）。返回关闭后的 status。"""
    reason = one_line(reason or '').strip()
    if not reason:
        raise QError(400, '关闭必须写原因')
    if len(reason) > 500:
        raise QError(400, '原因不超过 500 字')
    with locked():
        allq = all_questions()
        meta, body = _writable_for_owner(qid, allq)
        if int(content_rev) != geti(meta, 'content_rev') or int(answer_rev) != geti(meta, 'answer_rev'):
            raise QError(409, f'问题或回答已被更新（content_rev {meta["content_rev"]}，answer_rev '
                              f'{meta["answer_rev"]}），请刷新后再决定是否关闭')
        text = f'Owner 关闭：{reason}'
        if meta.get('status') == 'open':
            _withdraw(meta, body, text, by='owner')
            return 'withdrawn'
        _queue_notice(meta)
        if meta.get('delivery') != 'received':
            meta['delivery'] = 'cancelled'  # 待投递的取消；正在发的回包也不会再覆盖（finish 只认 sending）
        meta.update({'status': 'resolved', 'resolved_at': now(), 'resolution': text, 'resolved_by': 'owner',
                     'resolved_rev': meta.get('answer_rev') or '0'})
        _save(meta, body, f'{text}（Rev {meta.get("answer_rev")}，不是 lead 闭环）')
        return 'resolved'


# ---------- lead 侧：receive / resolve ----------

def receive(qid, ident):
    """核对后标记已接收，返回给 lead 看的全文。不通过时抛 QError，不返回答案。"""
    with locked():
        allq = all_questions()
        meta, body = _load_checked(qid, allq)
        check_owner(meta, ident)
        n = geti(meta, 'answer_rev')
        if n <= 0:
            raise QError(409, f'{qid} 还没有回答')
        if meta.get('status') not in ('answered', 'resolved'):
            raise QError(409, f'{qid} 状态是 {meta.get("status")}')
        current_slots(meta, body)  # 回答解析不完整就报错，不标 received
        if meta.get('status') == 'answered' and not (meta.get('delivery') == 'received' and geti(meta, 'received_rev') == n):
            t = now()
            meta.update({'delivery': 'received', 'delivery_rev': str(n), 'received_rev': str(n), 'received_at': t,
                         'pane_id': ident.get('pane_id') or meta.get('pane_id')})
            _save(meta, body, f'lead 领取 Rev {n}')
        return format_for_lead(meta, body)


def current_slots(meta, body):
    """当前 Rev 的回答槽；槽和问题对不上（记录损坏、解析丢槽）时抛 QError(500)，不能当成完整回答。"""
    lead, sp = split_body(body)
    n = geti(meta, 'answer_rev')
    cur = next((r for r in answer_revs(sp) if r['rev'] == n), None)
    want = {q for q, _ in questions(lead)}
    got = set((cur or {}).get('slots', {}))
    if cur is None or got != want:
        missing = ', '.join(sorted(want - got, key=lambda q: int(q[1:]))) or '无'
        raise QError(500, f'{meta.get("id")} 的 Rev {n} 回答记录不完整（缺 {missing}），不能领取：'
                          f'请让 Owner 知道，并运行 doctor')
    return cur['slots']


def format_for_lead(meta, body):
    lead, sp = split_body(body)
    n = geti(meta, 'answer_rev')
    revs = answer_revs(sp)
    cur = next((r for r in revs if r['rev'] == n), None)
    qs = dict(questions(lead))
    out = [f'# {meta["id"]} · {meta.get("title")}',
           f'issue: {meta.get("issue")}  status: {meta.get("status")}  answer_rev: {n}  content_rev: {meta.get("content_rev")}']
    if n > 1:
        out.append(f'⚠️ 以 Rev {n} 为准，之前的 Rev 已被更正，不要按旧答案执行。')
    out += ['', lead.strip()]
    if sp.get('勘误'):
        out += ['', '## 勘误', sp['勘误']]
    out += ['', f'## Owner 的回答（Rev {n}）']
    labels = {'option': '选项', 'free': '自由回答', 'suggested': '按建议', 'need_context': '需要更多上下文',
              'defer': '暂不决定', 'withdrawn': '已撤回的子问题'}
    for q, s in sorted((cur or {}).get('slots', {}).items(), key=lambda kv: int(kv[0][1:])):
        line = f'- {q}（{qs.get(q, "")}）→ [{labels.get(s.get("type"), s.get("type"))}]'
        if s.get('choice'):
            line += f' {s["choice"]}'
        if s.get('text'):
            line += f'\n  Owner 原话：{s["text"]}'
        out.append(line)
    out += ['', '提醒：`need_context` / `defer` 不是授权；只有明确给出的选择才算。']
    if meta.get('status') == 'answered':
        out.append(f'处理完后执行：resolve {meta["id"]} --answer-rev {n} --note "决定了什么，记在哪里"')
    return '\n'.join(out)


def resolve(qid, answer_rev, note, ident):
    if not (note or '').strip():
        raise QError(400, '必须写 --note：决定了什么，记在哪里')
    with locked():
        allq = all_questions()
        meta, body = _load_checked(qid, allq)
        check_owner(meta, ident)
        n = geti(meta, 'answer_rev')
        if meta.get('status') != 'answered':
            raise QError(409, f'{qid} 状态是 {meta.get("status")}，不能 resolve')
        if int(answer_rev) != n:
            raise QError(409, f'{qid} 当前是 Rev {n}，你传的是 Rev {answer_rev}：先 receive 看最新回答')
        if geti(meta, 'received_rev') != n:
            raise QError(409, f'{qid} 的 Rev {n} 还没 receive，先执行 receive {qid}')
        meta.update({'status': 'resolved', 'resolved_at': now(), 'resolution': note.strip(), 'resolved_rev': str(n),
                     'resolved_by': 'lead'})
        _save(meta, body, f'lead 闭环 Rev {n}：{note.strip()}')


# ---------- 启动修复 ----------

def repair():
    """服务启动时、开放写入和投递之前调用：补齐被替换题的头部，停在 sending 的改 uncertain。返回改动说明。"""
    changes = []
    with locked():
        allq = all_questions()
        for qid, (meta, body) in allq.items():
            b = superseder(qid, allq)
            if b and meta.get('status') != 'withdrawn':
                _withdraw(meta, body, f'被 {b} 替换', superseded_by=b)
                changes.append(f'{qid}: 补齐被 {b} 替换')
                meta, body = read(qid)
            dirty = False
            if meta.get('delivery') == 'sending':
                meta.update({'delivery': 'uncertain', 'last_error': '服务重启时停在 sending，不确定是否送达'})
                dirty = True
                changes.append(f'{qid}: sending → uncertain')
            if meta.get('withdraw_notice') == 'sending':
                meta['withdraw_notice'] = 'uncertain'
                dirty = True
                changes.append(f'{qid}: 撤回通知 sending → uncertain')
            if dirty:
                _save(meta, body, '服务重启：停在 sending 的投递改为 uncertain，不自动重发')
    return changes


# ---------- 投递记账（server.py 的投递循环用） ----------

def due_for_delivery(at=None):
    """[(meta, kind)]：kind 是 answer / correction / withdraw。只读，不加锁也行。"""
    at = at or datetime.now(TZ)
    allq = all_questions()
    out = []
    for qid, (meta, _) in allq.items():
        due = (parse_time(meta.get('next_try_at')) or at) <= at
        if not due:
            continue
        if (meta.get('status') == 'answered' and meta.get('delivery') in ('pending', 'held')
                and not invalid_reason(meta, allq)):
            out.append((meta, meta.get('delivery_kind') or 'answer'))
        elif has_notice_channel(meta) and meta.get('withdraw_notice') in ('pending', 'held'):
            out.append((meta, 'withdraw'))
    return out


def _eligible(meta, kind, allq, delivery_rev=None):
    if kind == 'withdraw':
        return meta.get('withdraw_notice') in ('pending', 'held')
    return (meta.get('status') == 'answered' and meta.get('delivery') in ('pending', 'held')
            and not invalid_reason(meta, allq)
            and (delivery_rev is None or geti(meta, 'delivery_rev') == delivery_rev))


def _backoff(meta):
    sched = backoff_schedule()
    k = geti(meta, 'hold_count') + 1
    meta['hold_count'] = str(k)
    nxt = datetime.now(TZ) + timedelta(seconds=sched[min(k, len(sched)) - 1])
    meta['next_try_at'] = nxt.isoformat(timespec='seconds')


def mark_held(items, reason):
    """items: [(qid, kind, delivery_rev)]。还没调用 agent prompt 就失败（找不到 lead、socket 不在）时用。"""
    with locked():
        allq = all_questions()
        for qid, kind, rev in items:
            if qid not in allq:
                continue
            meta, body = allq[qid]
            if not _eligible(meta, kind, allq, None if kind == 'withdraw' else rev):
                continue
            if kind == 'withdraw':
                meta['withdraw_notice'] = 'held'
            else:
                meta['delivery'] = 'held'
            meta['last_error'] = one_line(reason)[:300]
            _backoff(meta)
            _save(meta, body, f'投递暂缓：{meta["last_error"]}')


def claim(items):
    """写前记账：在锁内把每题写成 sending + 新 attempt_id。返回 [(qid, kind, delivery_rev, attempt_id)]。"""
    out = []
    with locked():
        allq = all_questions()
        for qid, kind, rev in items:
            if qid not in allq:
                continue
            meta, body = allq[qid]
            if not _eligible(meta, kind, allq, None if kind == 'withdraw' else rev):
                continue
            aid = uuid.uuid4().hex[:12]
            if kind == 'withdraw':
                meta['withdraw_notice'] = 'sending'
            else:
                meta['delivery'] = 'sending'
            meta['attempt_id'] = aid
            meta['attempts'] = str(geti(meta, 'attempts') + 1)
            _save(meta, body, f'开始投递（attempt {aid}）')
            out.append((qid, kind, rev, aid))
    return out


def finish(claimed, outcome, error='', lead_status=''):
    """outcome: sent / held / uncertain。按 attempt_id 条件更新，状态已变（received、cancelled、新 Rev）就不覆盖。"""
    with locked():
        allq = all_questions()
        for qid, kind, rev, aid in claimed:
            if qid not in allq:
                continue
            meta, body = allq[qid]
            if meta.get('attempt_id') != aid:
                continue
            if kind == 'withdraw':
                if meta.get('withdraw_notice') != 'sending':
                    continue
                meta['withdraw_notice'] = outcome
            else:
                if meta.get('delivery') != 'sending' or geti(meta, 'delivery_rev') != rev:
                    continue
                meta['delivery'] = outcome
            meta['last_error'] = one_line(error)[:300]
            if outcome == 'sent':
                meta['herdr_accepted_at'] = now()
                meta['lead_status'] = lead_status
                meta['hold_count'] = '0'
            elif outcome == 'held':
                _backoff(meta)
            label = {'sent': 'herdr 已接受', 'held': '暂缓', 'uncertain': '不确定是否送达'}[outcome]
            _save(meta, body, f'投递{label}（attempt {aid}）' + (f'：{meta["last_error"]}' if error else ''))


# ---------- herdr ----------

NOT_SENT_CODES = {'agent_blocked', 'pane_not_found', 'agent_not_found', 'target_not_found', 'not_found',
                  'server_not_running', 'session_not_found'}


def socket_exists(session):
    return bool(session) and (socket_root() / session / 'herdr.sock').exists()


def _parse_json(text):
    """整段是 JSON 就用整段，否则取最后一个非空行（前面可能有日志行）；都不是返回 None。"""
    text = (text or '').strip()
    for cand in (text, text.split('\n')[-1].strip()):
        if cand:
            try:
                return json.loads(cand)
            except ValueError:
                continue
    return None


def herdr(session, *args, timeout=None):
    """{'ok', 'kind', 'code', 'message', 'data'}；kind: ok / no_socket / timeout / bad_output / error / exec_error。"""
    if not socket_exists(session):
        return {'ok': False, 'kind': 'no_socket', 'code': 'no_socket', 'message': f'herdr session {session} 的 socket 不存在'}
    try:
        res = subprocess.run([herdr_bin(), '--session', session, *args], capture_output=True, text=True,
                             timeout=timeout or herdr_timeout())
    except subprocess.TimeoutExpired:
        return {'ok': False, 'kind': 'timeout', 'code': 'timeout', 'message': 'herdr 调用超时'}
    except OSError as e:
        return {'ok': False, 'kind': 'exec_error', 'code': 'exec_error', 'message': f'无法执行 herdr：{e}'}
    data = _parse_json(res.stdout)
    if not (isinstance(data, dict) and (data.get('error') or res.returncode == 0)):
        # 真实 herdr 出错时 stdout 为空、错误 JSON 写在 stderr、rc=1
        alt = _parse_json(res.stderr)
        if isinstance(alt, dict) and alt.get('error'):
            data = alt
    if data is None:
        return {'ok': False, 'kind': 'bad_output', 'code': 'bad_output',
                'message': f'herdr 输出无法解析（rc={res.returncode}）：{(res.stderr or res.stdout).strip()[:200]}'}
    if isinstance(data, dict) and data.get('error'):
        err = data['error'] if isinstance(data['error'], dict) else {'message': str(data['error'])}
        return {'ok': False, 'kind': 'error', 'code': err.get('code') or 'error', 'message': err.get('message') or ''}
    if res.returncode != 0 or not isinstance(data, dict):
        return {'ok': False, 'kind': 'bad_output', 'code': 'bad_output', 'message': f'herdr rc={res.returncode}'}
    return {'ok': True, 'kind': 'ok', 'code': '', 'message': '', 'data': data}


def pane_list(session):
    r = herdr(session, 'pane', 'list')
    if r['ok']:
        r['panes'] = ((r['data'].get('result') or {}).get('panes')) or []
    return r


def session_value(pane):
    s = pane.get('agent_session')
    return s.get('value') if isinstance(s, dict) else None


def find_lead(session, lead_session):
    """(pane, None) 或 (None, 原因)。只认 agent == claude 且 agent_session 相同，恰好一个。"""
    r = pane_list(session)
    if not r['ok']:
        return None, f'pane list 失败：{r["code"]} {r["message"]}'.strip()
    hits = [p for p in r['panes'] if p.get('agent') == 'claude' and session_value(p) == lead_session]
    if len(hits) != 1:
        return None, ('找不到原 lead 会话（pane 可能已关闭或换成了别的会话）' if not hits
                      else f'有 {len(hits)} 个 pane 是同一个 lead 会话，不知道投哪个')
    return hits[0], None


def identify(session, pane_id):
    """调用者身份；失败抛 QError(403)。"""
    if not session or not pane_id:
        raise QError(403, '拿不到 $HERDR_SESSION / $HERDR_PANE_ID：请在 lead 的 herdr pane 里执行，'
                          '或用 --herdr-session / --pane 显式指定（会现场核对）')
    r = pane_list(session)
    if not r['ok']:
        raise QError(403, f'无法核对身份：{r["code"]} {r["message"]}')
    pane = next((p for p in r['panes'] if p.get('pane_id') == pane_id), None)
    if not pane:
        raise QError(403, f'herdr session {session} 里找不到 pane {pane_id}')
    if pane.get('agent') != 'claude':
        raise QError(403, f'pane {pane_id} 的 agent 是 {pane.get("agent")!r}，第一版只支持 claude lead')
    if not session_value(pane):
        raise QError(403, f'pane {pane_id} 没有 agent_session，无法确认身份')
    return {'herdr_session': session, 'pane_id': pane_id, 'agent_kind': 'claude',
            'lead_session': session_value(pane), 'agent_status': pane.get('agent_status') or ''}


def ws_from_cwd(cwd):
    base = workspaces_root().resolve()
    try:
        rel = Path(cwd).resolve().relative_to(base)
    except ValueError:
        return None
    name = rel.parts[0] if rel.parts else None
    return name if name and not name.startswith(('_', '.')) and WS_RE.match(name) else None


# ---------- 展示（网页、status 共用） ----------

def delivery_view(meta, at=None):
    """(短标签, 说明, 是否显示「重新通知」)。"""
    at = at or datetime.now(TZ)
    d, n = meta.get('delivery'), geti(meta, 'answer_rev')
    err = meta.get('last_error') or ''
    if has_notice_channel(meta):
        note = meta.get('withdraw_notice') or ''
        owner = closed_by_owner(meta)
        head, kind = ('Owner 已关闭', '关闭通知') if owner else ('已撤回', '撤回通知')
        why = (meta.get('resolution') if meta.get('status') == 'resolved' else meta.get('withdraw_reason')) or ''
        if note in ('uncertain', 'held'):
            state = '不确定是否送达' if note == 'uncertain' else f'暂缓，下次尝试 {meta.get("next_try_at")}'
            return f'{head}，{kind}未送达', f'{why}；{kind}{state}：{err}', True
        if note in ('pending', 'sending'):
            return f'{head}，{kind}投递中', why, False
        if note == 'sent':
            return f'{head}，{kind}已投进 pane', why, False
        return head, why, False
    if d == 'received' and geti(meta, 'received_rev') == n:
        return 'lead 已接收', f'Rev {n}，{meta.get("received_at")}', False
    if d == 'sent':
        acc = parse_time(meta.get('herdr_accepted_at'))
        limit = STALE_IDLE if meta.get('lead_status') == 'idle' else STALE_WORKING
        if acc and (at - acc).total_seconds() > limit:
            return '已投进 pane，未领取', f'herdr 于 {meta.get("herdr_accepted_at")} 接受，lead 还没 receive', True
        return '已投进 pane', f'herdr 于 {meta.get("herdr_accepted_at")} 接受，等 lead receive', False
    if d == 'held':
        return '暂缓投递', f'{err}；下次尝试 {meta.get("next_try_at")}', True
    if d == 'uncertain':
        return '不确定是否送达', err, True
    if d == 'sending':
        return '投递中', '', False
    if d == 'pending':
        return '排队投递', err, False
    if d == 'cancelled':
        return '已取消', '', False
    return '', '', False
