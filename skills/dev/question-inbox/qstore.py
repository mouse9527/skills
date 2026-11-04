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

def tasks_config_path():
    return Path(os.environ.get('QI_TASKS_CONFIG') or Path.home() / '.config' / 'question-inbox' / 'tasks.json')


def lead_cwd(ws):
    """ws 的 lead 工作目录：tasks.json 条目里的可选 `lead_cwd`（可写 ~），默认 ~/workspaces/<ws>。
    Router 起 lead、写派工书（<lead_cwd>/dispatch/）、claim 校验都按它。"""
    try:
        data = json.loads(tasks_config_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    for r in (data.get('repos') if isinstance(data, dict) else None) or []:
        if isinstance(r, dict) and r.get('ws') == ws and isinstance(r.get('lead_cwd'), str) and r['lead_cwd'].strip():
            return Path(r['lead_cwd']).expanduser()
    return workspaces_root() / ws


def router_session():
    """Router 所在的 herdr session；真机自查时指向临时 session，绝不在别的 session 起 agent。"""
    return os.environ.get('QI_ROUTER_SESSION') or 'router'


FIELDS = (
    'id', 'kind', 'ws', 'title', 'issue', 'group', 'blocking', 'status', 'content_rev', 'answer_rev', 'withdrawn_qs',
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
KINDS = ('question', 'approval')
APPROVAL_OPTIONS = ('A. 批准', 'B. 不批准', 'C. 需要先改')
APPROVAL_SECTIONS = ('动作', '对象', '后果')  # 加上通用的 `## 证据`：批什么、对哪个 repo / PR / HEAD / 环境 / 制品、批了会怎样


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
    kind = fields.get('kind') or 'question'
    if kind not in KINDS:
        errors.append(f'kind 只能是 {" / ".join(KINDS)}')
    approval = kind == 'approval'
    for name in LEAD_SECTIONS + (APPROVAL_SECTIONS if approval else ()):
        if approval and name == '建议':
            continue
        if not sec.get(name):
            errors.append(f'缺少 `## {name}` 段或内容为空')
    qs = questions(lead)
    if approval:
        if [q for q, _ in qs] != ['Q1']:
            errors.append('审批题只能有一个子问题 Q1')
        if tuple(options(lead).get('Q1') or ()) != APPROVAL_OPTIONS:
            errors.append('审批题 `### Q1` 的选项必须正好是：' + ' / '.join(APPROVAL_OPTIONS))
        if '建议' in sec:
            errors.append('审批题不写 `## 建议`：页面不预选、没有「按建议」')
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
        if q not in sugg and not approval:
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
            if kind_of(a) != (fields.get('kind') or 'question'):
                raise QError(400, f'{supersedes} 是 {kind_of(a)} 题，不能用别的题型替换')
            check_owner(a, identity)  # 没有显式交接，替换不能改归属
        if any(m.get('issue') == fields['issue'] != 'none' and m.get('status') == 'open' and i != supersedes
               for i, (m, _) in allq.items()):
            warnings.append(f'issue {fields["issue"]} 下已经有 open 的题')
        qid = _alloc_id(fields['ws'], allq)
        t = now()
        meta = {
            'id': qid, 'kind': fields.get('kind') or 'question', 'ws': fields['ws'], 'title': fields['title'].strip(), 'issue': fields['issue'],
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


def kind_of(meta):
    return 'approval' if meta.get('kind') == 'approval' else 'question'


DELIVERED = ('sending', 'sent', 'uncertain', 'received')  # lead 可能已经拿到过回答


def ever_delivered(meta):
    """任一版本曾经交给 herdr 或被领取过（当前 delivery 可能因为更正、重新通知回到了 pending / held）。"""
    return bool(meta.get('herdr_accepted_at')) or geti(meta, 'received_rev') > 0 or meta.get('delivery') in DELIVERED


def _queue_notice(meta):
    """撤回 / Owner 关闭时，回答可能已经在 lead 手里：单独排一条通知（送不到不影响撤回或关闭本身）。"""
    if ever_delivered(meta):
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
        if kind_of(meta) == 'approval':  # 审批只有三个固定选项；「需要先改」必须写要改什么
            if t != 'option':
                raise QError(400, f'审批题只能选 {" / ".join(APPROVAL_OPTIONS)}')
            if complete and clean.get('choice') == APPROVAL_OPTIONS[2] and not clean.get('text', '').strip():
                raise QError(400, f'选「{APPROVAL_OPTIONS[2]}」要写清楚改什么')
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
    """返回新的 answer_rev。更正内容和当前 Rev 逐字相同时不生成新 Rev、不投递，返回当前 answer_rev。"""
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
        last = next((r for r in answer_revs(sp) if r['rev'] == geti(meta, 'answer_rev')), None)
        if last is not None and last['slots'] == clean:  # 重复点击提交：不算更正
            if '草稿回答' in sp:
                sp.pop('草稿回答')
                _save(meta, join_body(lead, sp), '提交内容与当前回答相同，丢弃草稿，未生成新 Rev')
            return geti(meta, 'answer_rev')
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
    out = [f'# {meta["id"]} · {"【审批】" if kind_of(meta) == "approval" else ""}{meta.get("title")}',
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
    if kind_of(meta) == 'approval':
        out.append(f'审批：只有「{APPROVAL_OPTIONS[0]}」是授权，且只授权正文写的那个动作和对象；'
                   f'把结论落到 Issue / PR，动作执行前重新核对 HEAD 和环境，和正文不一致就不执行、另开一题。')
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


def _checked_list(session, cmd, rtype, key):
    """`herdr <cmd> list` 并校验结构：result.type == rtype、result[key] 是 list、每个条目是带 str pane_id 的对象，
    条目里有 name / agent_session 时类型也要对。不合格的（缺字段、null、空对象、条目坏、type 不对）一律当查询失败
    （kind bad_output，code bad_shape）——**只有成功解析出的空列表才代表「确实没有」**，坏输出只能算「查不清」。
    原始响应保留在 r['data']。"""
    r = herdr(session, cmd, 'list')
    if not r['ok']:
        return r
    res = r['data'].get('result')
    items = res.get(key) if isinstance(res, dict) else None
    why = ''
    if not isinstance(res, dict):
        why = 'result 不是对象'
    elif res.get('type') != rtype:
        why = f'result.type 是 {res.get("type")!r}，应为 {rtype!r}'
    elif not isinstance(items, list):
        why = f'result.{key} 缺失或不是列表'
    else:
        for it in items:
            if not (isinstance(it, dict) and isinstance(it.get('pane_id'), str) and it['pane_id']):
                why = f'{key} 里有缺 pane_id 的条目'
            elif it.get('name') is not None and not isinstance(it.get('name'), str):
                why = f'{key} 里有 name 类型不对的条目'
            elif it.get('agent_session') is not None and not isinstance(it.get('agent_session'), dict):
                why = f'{key} 里有 agent_session 类型不对的条目'
            if why:
                break
    if why:
        return {'ok': False, 'kind': 'bad_output', 'code': 'bad_shape', 'message': f'herdr {cmd} list 输出结构不对：{why}',
                'data': r['data']}
    r[key] = items
    return r


def pane_list(session):
    return _checked_list(session, 'pane', 'pane_list', 'panes')


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


ABSENT_CODES = ('no_socket', 'server_not_running', 'session_not_found')  # herdr 明确答复「这个 session 没在跑」


def presence(session, lead_session):
    """(结论, pane, 说明)。结论三选一：present（恰好一个 claude pane 是这个会话）/ absent（确认不在：查询成功且零匹配，
    或 session 根本没在跑）/ unknown（查询失败、超时、输出坏、多个匹配）。**unknown 不能当成 absent 用。**"""
    if not session or not lead_session:
        return 'absent', None, '没有身份记录'
    r = pane_list(session)
    if not r['ok']:
        if r['code'] in ABSENT_CODES:
            return 'absent', None, f'herdr session {session} 没在跑（{r["code"]}）'
        return 'unknown', None, f'pane list 失败：{r["code"]} {r["message"]}'.strip()
    if not isinstance(r['panes'], list):
        return 'unknown', None, 'pane list 输出不是列表'
    hits = [p for p in r['panes'] if isinstance(p, dict) and p.get('agent') == 'claude' and session_value(p) == lead_session]
    if len(hits) == 1:
        return 'present', hits[0], ''
    if hits:
        return 'unknown', None, f'有 {len(hits)} 个 pane 是同一个会话'
    return 'absent', None, '找不到这个会话（pane 已关闭或换成了别的会话）'


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
            'lead_session': session_value(pane), 'agent_status': pane.get('agent_status') or '',
            'cwd': pane.get('foreground_cwd') or pane.get('cwd') or ''}


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


# ---------- 需求记录（workspace §9 第二个例外：needs/need-NNNN.json） ----------
# 只存 Owner 原话、Router 的分配结论（ws / why / Issue URL）、notes 和投递记账；
# 不存进度、stage、PR、谁在做 —— 这些和任务视图一样从 GitHub + herdr 现算。

NEED_ID_RE = re.compile(r'^need-(\d{4,})$')
# 修订 1：Router 只路由；ws lead claim 后判断清不清楚，不清楚就在需求线程里发理解卡讨论，确认后在本 ws 主 repo 建 Issue
NEED_STATUSES = ('new', 'routing', 'assigned', 'discussing', 'confirmed', 'filed', 'dropped', 'held')
DISCUSS = ('', 'waiting_owner', 'waiting_lead')
SEALED = ('filed', 'dropped')  # 线程只读封存
LEAD_STATES = ('assigned', 'discussing', 'confirmed', 'held')  # 负责 lead 能发卡 / link / drop / hold 的状态
NEED_KINDS = ('new', 'note', 'unclaimed', 'reply', 'confirm')  # reply / confirm 投给负责 lead，其余投给 Router
CARD_SECTIONS = ('理解', '范围', '不做什么', '验收草案', '开放问题')
ISSUE_URL_RE = re.compile(r'^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)$')
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
NEED_FIELDS = ('id', 'text', 'created', 'updated', 'status', 'discuss', 'ws', 'why', 'lead', 'issues', 'card_rev',
               'confirmed_rev', 'thread', 'notes', 'notes_acked', 'request_id', 'delivery', 'delivery_to',
               'delivery_kind', 'delivery_seq', 'attempt_id', 'attempts', 'hold_count', 'next_try_at', 'last_error',
               'router_session', 'herdr_accepted_at', 'log')
NEED_BATCH = 5
ENSURE_PER_HOUR = 3


def needs_dir():
    return root() / 'needs'


def need_path(nid):
    if not NEED_ID_RE.match(nid or ''):
        raise QError(400, f'非法需求 id: {nid!r}')
    return needs_dir() / f'{nid}.json'


def _need_load(p):
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    rec.setdefault('id', p.stem)
    for k, v in (('issues', []), ('notes', []), ('log', []), ('thread', [])):
        if not isinstance(rec.get(k), list):
            rec[k] = v
    if not isinstance(rec.get('lead'), dict):
        rec['lead'] = {}
    return rec


def all_needs():
    out = {}
    d = needs_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob('need-*.json')):
        if NEED_ID_RE.match(p.stem):
            rec = _need_load(p)
            if rec is not None:
                out[p.stem] = rec
    return out


def read_need(nid):
    p = need_path(nid)
    rec = _need_load(p) if p.is_file() else None
    if rec is None:
        raise QError(404, f'{nid} 不存在')
    return rec


def _save_need(rec, event=None):
    t = now()
    rec['updated'] = t
    if event:
        rec['log'] = (rec.get('log') or []) + [{'at': t, 'text': one_line(event)[:300]}]
    out = {k: rec[k] for k in NEED_FIELDS if k in rec}
    out.update({k: v for k, v in rec.items() if k not in NEED_FIELDS})
    _write_atomic(need_path(rec['id']), json.dumps(out, ensure_ascii=False, indent=1) + '\n')


def _alloc_need_id(existing):
    """和问题共用 _seq 文件与同一把锁；键 `_need` 不会和 ws 名撞（ws 不能以 _ 开头）。"""
    seq_path = root() / '_seq'
    try:
        seq = json.loads(seq_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seq = {}
    used = [int(NEED_ID_RE.match(i).group(1)) for i in existing]
    n = max([int(seq.get('_need', 0))] + used) + 1
    while (needs_dir() / f'need-{n:04d}.json').exists():
        n += 1
    seq['_need'] = n
    _write_atomic(seq_path, json.dumps(seq, ensure_ascii=False, sort_keys=True) + '\n')
    return f'need-{n:04d}'


def _clean_text(text, what):
    if not isinstance(text, str) or not text.strip():
        raise QError(400, f'{what}不能为空')
    if len(text) > MAX_TEXT:
        raise QError(400, f'{what}不超过 {MAX_TEXT} 字')
    return text.replace('\r\n', '\n')


def _check_request_id(request_id):
    if request_id in (None, ''):
        return ''
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        raise QError(400, 'request_id 不合法')
    return request_id


def _queue_need(rec, kind):
    """排一次投递；delivery_seq + 1，让正在发的那次回包（finish）不覆盖这次。reply / confirm 投给负责 lead，其余投给 Router。"""
    rec.update({'delivery': 'pending', 'delivery_kind': kind, 'delivery_to': 'lead' if kind in ('reply', 'confirm') else 'router',
                'delivery_seq': geti(rec, 'delivery_seq') + 1, 'attempt_id': '', 'attempts': 0, 'hold_count': 0,
                'next_try_at': now(), 'last_error': ''})


def create_need(text, request_id=''):
    """Owner 在页面提交一句话。返回 (记录, 是否新建)；同一个 request_id 重复提交直接返回原记录。"""
    text = _clean_text(text, '需求')
    request_id = _check_request_id(request_id)
    with locked():
        alln = all_needs()
        if request_id:
            dup = next((r for r in alln.values() if r.get('request_id') == request_id), None)
            if dup:
                return dup, False
        nid = _alloc_need_id(alln)
        t = now()
        rec = {'id': nid, 'text': text, 'created': t, 'updated': t, 'status': 'new', 'discuss': '', 'ws': '',
               'why': '', 'lead': {}, 'issues': [], 'card_rev': 0, 'confirmed_rev': 0, 'thread': [], 'notes': [],
               'notes_acked': 0, 'request_id': request_id, 'router_session': '', 'log': []}
        _queue_need(rec, 'new')
        _save_need(rec, 'Owner 提交')
        return rec, True


def pending_notes(rec):
    """Router 还没处理的 Owner 补充（take / route 或 Router 写 note 时记为已处理）。"""
    return [n for n in (rec.get('notes') or [])[geti(rec, 'notes_acked'):] if n.get('by') == 'owner']


def _seen(rec, request_id):
    return bool(request_id) and any(x.get('request_id') == request_id for x in rec['notes'] + rec['thread'])


def owner_reply(nid, text, request_id=''):
    """Owner 在需求页写一段话。lead 已 claim：进讨论线程（→ discussing / waiting_lead），投给负责 lead；
    还没 lead：记成 note，Router 正在处理（routing / held）时投给 Router。filed / dropped 后封存。返回 (记录, 是否新建)。"""
    text = _clean_text(text, '回复')
    request_id = _check_request_id(request_id)
    with locked():
        rec = read_need(nid)
        if _seen(rec, request_id):
            return rec, False
        if rec.get('status') in SEALED:
            raise QError(409, f'{nid} 已{"立项" if rec["status"] == "filed" else "不做"}，讨论已封存；要改范围请找 lead 或另提需求')
        extra = {'request_id': request_id} if request_id else {}
        if rec['lead']:
            rec['thread'].append({'at': now(), 'by': 'owner', 'kind': 'reply', 'text': text, **extra})
            rec.update({'status': 'discussing', 'discuss': 'waiting_lead'})
            _queue_need(rec, 'reply')
            _save_need(rec, 'Owner 回复，投给负责 lead')
        else:
            rec['notes'].append({'at': now(), 'by': 'owner', 'text': text, **extra})
            to_router = rec.get('status') in ('routing', 'held', 'assigned')  # 还没 lead claim：Router 仍是负责人
            if to_router:
                _queue_need(rec, 'note')
            _save_need(rec, 'Owner 补充' + ('，投给 Router' if to_router else '（Router take 时会一起看到）'))
        return rec, True


def owner_confirm(nid, card_rev, request_id=''):
    """Owner 确认**最新**一张理解卡；绑定 card_rev，过期 409。→ confirmed，投给负责 lead。"""
    request_id = _check_request_id(request_id)
    with locked():
        rec = read_need(nid)
        if _seen(rec, request_id):
            return rec, False
        if rec.get('status') != 'discussing' or rec.get('discuss') != 'waiting_owner':
            raise QError(409, f'{nid} 现在不在等你确认（{rec.get("status")}{"/" + rec["discuss"] if rec.get("discuss") else ""}）')
        if int(card_rev) != geti(rec, 'card_rev'):
            raise QError(409, f'理解卡已更新到 v{geti(rec, "card_rev")}，你确认的是 v{card_rev}：请刷新后看最新一张')
        n = geti(rec, 'card_rev')
        rec['thread'].append({'at': now(), 'by': 'owner', 'kind': 'confirm', 'rev': n,
                              **({'request_id': request_id} if request_id else {})})
        rec.update({'status': 'confirmed', 'discuss': '', 'confirmed_rev': n})
        _queue_need(rec, 'confirm')
        _save_need(rec, f'Owner 确认理解卡 v{n}，投给负责 lead')
        return rec, True


# ----- Router 身份：$QI_STATE_DIR/router.json -----

def _state_file(name):
    return state_dir() / name


def _read_state(name):
    try:
        data = json.loads(_state_file(name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(name, data):
    d = state_dir()
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    _write_atomic(_state_file(name), json.dumps(data, ensure_ascii=False, indent=1) + '\n')


def router_info():
    return _read_state('router.json')


def agent_list(session):
    return _checked_list(session, 'agent', 'agent_list', 'agents')


def agent_name(session, pane_id):
    """pane 上 agent 的名字（`agent start <NAME>`）；pane list 里没有名字，要看 agent list。"""
    r = agent_list(session)
    if not r['ok']:
        raise QError(403, f'无法核对 agent 名字：{r["code"]} {r["message"]}')
    a = next((a for a in r['agents'] if a.get('pane_id') == pane_id), None)
    return (a or {}).get('name') or ''


def register_router(ident):
    """在 router 的 pane 里执行。只认：session = QI_ROUTER_SESSION、claude、agent 名字是 router；
    已登记的 Router 还活着时不让别的会话顶替。"""
    if ident.get('herdr_session') != router_session():
        raise QError(403, f'Router 只能在 herdr session {router_session()} 里登记（你在 {ident.get("herdr_session")}）')
    name = agent_name(ident['herdr_session'], ident['pane_id'])
    if name != 'router':
        raise QError(403, f'pane {ident["pane_id"]} 的 agent 名字是 {name or "（无）"}，只有名为 router 的 agent 能登记')
    old = router_info()
    if old.get('lead_session') and old.get('lead_session') != ident['lead_session']:
        st, pane, why = presence(old.get('herdr_session'), old['lead_session'])  # 查 herdr 不持锁
        if st == 'present':
            raise QError(403, f'已登记的 Router（pane {pane.get("pane_id")}）还在，不能顶替')
        if st == 'unknown':
            raise QError(409, f'核对不了已登记的 Router 是否还在（{why}），先不顶替，稍后重试')
    with locked():
        if router_info() != old:
            raise QError(409, 'Router 登记刚刚变了，请重试')
        info = {'herdr_session': ident['herdr_session'], 'lead_session': ident['lead_session'],
                'pane_id': ident['pane_id'], 'registered_at': now(),
                'last_delivery_at': old.get('last_delivery_at', '')}
        _write_state('router.json', info)
        st = _read_state('router-ensure.json')
        if st.get('gave_up'):  # Router 起来了：解除「起不来」
            st.update({'gave_up': False, 'last_error': ''})
            _write_state('router-ensure.json', st)
        for rec in all_needs().values():  # 暂缓中、投给 Router 的需求不等退避，下一轮就投
            if rec.get('delivery') == 'held' and rec.get('delivery_to', 'router') == 'router':
                rec.update({'next_try_at': now(), 'hold_count': 0})
                _save_need(rec, 'Router 已登记，暂缓的投递重新排队')
    return info


def check_router(ident):
    info = router_info()
    want = (info.get('herdr_session'), info.get('lead_session'))
    got = ((ident or {}).get('herdr_session'), (ident or {}).get('lead_session'))
    if not all(want):
        raise QError(403, '还没有登记的 Router：只有执行过 `q.py router register` 的 Router 会话能做这一步')
    if got != want:
        raise QError(403, '身份不符：只有登记的 Router 会话能 take / route 需求')


# ----- Router 建的 lead：$QI_STATE_DIR/router-leads.json -----
# {ws: {workspace_id, pane_id, agent_session, created_at}}。Router 只复用 / 关闭这里记着、且三项都对得上的 lead。

def router_leads():
    return _read_state('router-leads.json')


def record_lead(ws, info):
    """记 / 清 Router 建的 lead。info 里 agent_session 为空时记成「待核验」（pending），下次能安全对上时再补全。"""
    with locked():
        data = router_leads()
        if info:
            data[ws] = dict(info, created_at=now(), pending=not info.get('agent_session'))
        else:
            data.pop(ws, None)
        _write_state('router-leads.json', data)


def _refresh_lead(ws, old, session_id):
    """归属不变（同一个 workspace、pane、名字、cwd）、只是会话 id 变了 / 第一次读到：把记录刷新成当前会话。
    在锁内比对，记录这期间被别人改过就不动。"""
    with locked():
        data = router_leads()
        if data.get(ws) != old:
            return False
        data[ws] = dict(old, agent_session=session_id, pending=False, refreshed_at=now())
        _write_state('router-leads.json', data)
        return True


WORKER_NAME_RE = re.compile(r'^(?:w-\d+(?:-.+)?|rv-\d+-.+)$')


def _cwd_of(item):
    out = []
    for k in ('cwd', 'foreground_cwd'):
        v = item.get(k)
        if isinstance(v, str) and v:
            try:
                out.append(Path(v).resolve())
            except OSError:
                pass
    return out


def _list_or_status(r):
    """列表查询失败时：herdr 明确说 session 没在跑 → absent；其余（含结构不对）→ unknown。"""
    return 'absent' if r['code'] in ABSENT_CODES else 'unknown'


def lead_record_presence(ws):
    """(结论, 记录, 根 pane, 说明)。根据 router-leads.json 判断 Router 建的 lead-<ws>：
    - present：记录里的 workspace、pane 对得上，那个 pane 上的 agent 名是 lead-<ws>、cwd 是 lead 目录。
      会话 id 和记录不同（同一个 pane 里重开过）或记录还是「待核验」时，刷新记录成当前会话——归属没变，不算冲突；
    - absent：确认没有（包括记录里的 pane 还在、但 lead 已退出）；
    - unknown：查不清（查询失败、结构不对、会话 id 还读不到）；
    - conflict：同名 lead-<ws> 不止一个，或有同名的但不是记录里那个（没有记录 / workspace、pane、cwd 对不上）。"""
    rec = router_leads().get(ws) or {}
    s = router_session()
    ra = agent_list(s)
    if not ra['ok']:
        return _list_or_status(ra), rec, None, f'agent list 失败：{ra["code"]} {ra["message"]}'
    rp = pane_list(s)
    if not rp['ok']:
        return _list_or_status(rp), rec, None, f'pane list 失败：{rp["code"]} {rp["message"]}'
    named = {a['pane_id']: a for a in ra['agents'] if a.get('name') == f'lead-{ws}'}
    panes = {p['pane_id']: p for p in rp['panes']}
    if len(named) > 1:
        return 'conflict', rec, None, f'session {s} 里有 {len(named)} 个名为 lead-{ws} 的 agent'
    want = lead_cwd(ws).resolve()
    root = panes.get(rec.get('pane_id')) if rec else None
    agent = named.get(rec.get('pane_id')) if rec else None
    root_ok = bool(root and agent and root.get('workspace_id') == rec.get('workspace_id')
                   and want in _cwd_of(agent) + _cwd_of(root))
    if named and not root_ok:
        pid = next(iter(named))
        return 'conflict', rec, None, (f'session {s} 里的 lead-{ws}（pane {pid}）不是 Router 建的'
                                      f'（没有归属记录，或 workspace / pane / cwd 对不上）')
    if not root_ok:
        return 'absent', rec, root, '没有在跑的、Router 建的 lead-' + ws
    cur = session_value(root) or session_value(agent)
    if not cur:
        return 'unknown', rec, None, f'lead-{ws}（pane {root["pane_id"]}）的会话 id 还读不到'
    if cur != rec.get('agent_session'):
        _refresh_lead(ws, rec, cur)
        rec = router_leads().get(ws) or rec
    return 'present', rec, root, ''


def is_worker_of(item, ws):
    """lead 派出去的 worker：名字 w-<号>… / rv-<号>-…，且 cwd 在这个 ws 的 lead 目录之下。"""
    if not (isinstance(item.get('name'), str) and WORKER_NAME_RE.match(item['name'])):
        return False
    base = lead_cwd(ws).resolve()
    for c in _cwd_of(item):
        try:
            c.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def workspace_tree(session, wsid, root_pane, root='agent', ws=None):
    """关一个 workspace 之前核验整棵树：(能不能关, 说明)。每个 pane 都必须是托管的：
    - 记录里的根 pane：root='agent' 时上面必须有 agent（在跑的 lead）；'shell' 时必须已没有 agent（退出后的空壳）；'any' 都行
      （刚建、启动失败的那个）；
    - 给了 ws 时，还允许这个 lead 派出去的 worker（w-<号>… / rv-<号>-…，cwd 在 lead 目录下）。
    人工的、归属说不清的（包括别的普通 shell）、查询失败或结构不对，一律不能关。"""
    if not wsid or not root_pane:
        return False, '没有 workspace / 根 pane 记录'
    rp, ra = pane_list(session), agent_list(session)
    for r in (rp, ra):
        if not r['ok']:
            return False, f'查不清这个 workspace 里有什么（{r["code"]} {r["message"]}）'
    agents = {a['pane_id']: a for a in ra['agents']}
    mine = [p for p in rp['panes'] if p.get('workspace_id') == wsid]
    if not any(p['pane_id'] == root_pane for p in mine):
        return False, f'workspace {wsid} 里找不到记录的根 pane {root_pane}'
    bad = []
    for p in mine:
        pid, a = p['pane_id'], agents.get(p['pane_id'])
        has_agent = bool(a or p.get('agent'))
        if pid == root_pane:
            if (root == 'agent' and not has_agent) or (root == 'shell' and has_agent):
                bad.append(f'{pid}（根 pane {"已没有 agent" if root == "agent" else "里还有 agent"}）')
            continue
        if a and ws and is_worker_of(dict(p, **a), ws):
            continue
        name = (a or {}).get('name') or ''
        label = (f'worker {name}' if WORKER_NAME_RE.match(name) else name) or (f'无名 {p.get("agent")}' if has_agent else '普通 shell')
        bad.append(f'{pid}（{label}）')
    if bad:
        return False, f'workspace {wsid} 里有不归 Router 管的 pane：{"、".join(bad)}'
    return True, ''


# ----- 全局暂停：$QI_STATE_DIR/control.json -----

def paused():
    return bool(_read_state('control.json').get('paused'))


@contextmanager
def _control_lock(mode):
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / 'control.lock', 'a') as f:
        fcntl.flock(f, mode)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def set_paused(on):
    """暂停要等正在进行的外部写动作（gated 里的那一次 herdr 调用）做完才提交；提交之后不会再有新的。"""
    with _control_lock(fcntl.LOCK_EX):
        with locked():
            _write_state('control.json', {'paused': bool(on), 'at': now()})


PAUSED = {'ok': False, 'kind': 'paused', 'code': 'paused', 'message': '全局暂停中'}


def gated(session, *args, timeout=None):
    """Router 侧的每一次外部写动作（建 workspace、起 agent、send-keys、prompt、关 workspace）都走这里：
    持共享锁现查暂停，暂停了就不调 herdr、返回 PAUSED。set_paused 持独占锁，所以两者不会交错。"""
    with _control_lock(fcntl.LOCK_SH):
        if paused():
            return dict(PAUSED)
        return herdr(session, *args, timeout=timeout)


# ----- 状态转换：Router 侧（take / route / hold / note） -----

def _ack_delivery(rec, to):
    if rec.get('delivery_to', 'router') == to and rec.get('delivery') not in (None, 'none', 'received'):
        rec['delivery'] = 'received'


def take(nid, ident):
    """new / held（还没有负责 lead）→ routing；routing 重复 take 幂等。返回记录。"""
    with locked():
        check_router(ident)
        rec = read_need(nid)
        if paused():
            raise QError(409, '全局暂停中：不领新需求（q.py router paused 查询）')
        st = rec.get('status')
        if st == 'routing':
            pass
        elif st == 'new' or (st == 'held' and not rec['lead']):
            rec['status'] = 'routing'
        else:
            raise QError(409, f'{nid} 状态是 {st}，Router 不能 take（已交给 lead 的需求由 lead 处理）')
        rec['notes_acked'] = len(rec['notes'])
        rec['router_session'] = ident['lead_session']
        _ack_delivery(rec, 'router')
        _save_need(rec, f'Router take（{st} → routing）')
        return rec


def route(nid, ws, why, ident):
    """routing → assigned：分到 ws（必须是 ~/workspaces 下已有的目录）。还没有 lead claim 的 assigned 可以改派。"""
    why = one_line(_clean_text(why, '分配理由'))[:500]
    if not WS_RE.match(ws or '') or ws.startswith(('_', '.')) or not lead_cwd(ws).is_dir():
        raise QError(400, f'ws 不合法或它的 lead 目录不存在：{ws!r}')
    with locked():
        check_router(ident)
        if paused():  # route 是派工的前一半
            raise QError(409, '全局暂停中：不分派')
        rec = read_need(nid)
        if not (rec.get('status') == 'routing' or (rec.get('status') == 'assigned' and not rec['lead'])):
            raise QError(409, f'{nid} 状态是 {rec.get("status")}：先 take；已有 lead claim 的不能改派')
        old = rec.get('ws')
        rec.update({'status': 'assigned', 'ws': ws, 'why': why, 'assigned_at': now(), 'nudged_at': ''})
        rec['notes_acked'] = len(rec['notes'])
        _ack_delivery(rec, 'router')
        _save_need(rec, f'Router 分到 ws {ws}' + (f'（原 {old}）' if old and old != ws else '') + f'：{why}')
        return rec


def router_note(nid, text, ident):
    """Router 记一句说明；同时把之前的 Owner 补充记为已处理。"""
    text = _clean_text(text, '说明')
    with locked():
        check_router(ident)
        rec = read_need(nid)
        rec['notes'].append({'at': now(), 'by': 'router', 'text': text})
        rec['notes_acked'] = len(rec['notes'])
        _ack_delivery(rec, 'router')
        _save_need(rec, 'Router 记录说明')
        return rec


def hold(nid, why, ident):
    """Router（routing，或 assigned 但还没 lead claim 时）或负责 lead（assigned / discussing / confirmed 时）找不到路、卡住 → held，页面显示原因。"""
    why = one_line(_clean_text(why, '原因'))[:500]
    with locked():
        rec = read_need(nid)
        st = rec.get('status')
        if st == 'routing' or (st == 'assigned' and not rec['lead']):  # 还没 lead claim：Router 仍是负责人
            check_router(ident)
            by = 'router'
        elif st in LEAD_STATES and st != 'held' and rec['lead']:
            _check_lead(rec, ident)
            by = 'lead'
        else:
            raise QError(409, f'{nid} 状态是 {st}，不能 hold')
        rec['status'], rec['discuss'] = 'held', ''
        rec['notes'].append({'at': now(), 'by': by, 'text': f'卡住：{why}'})
        rec['notes_acked'] = len(rec['notes'])
        _ack_delivery(rec, by if by == 'router' else 'lead')
        _save_need(rec, f'{"Router" if by == "router" else "lead"} hold：{why}')
        return rec


# ----- 状态转换：负责 lead 侧（claim / card / link / drop） -----

def _check_lead(rec, ident):
    lead = rec.get('lead') or {}
    want = (lead.get('herdr_session'), lead.get('lead_session'))
    got = ((ident or {}).get('herdr_session'), (ident or {}).get('lead_session'))
    if not all(want):
        raise QError(403, f'{rec["id"]} 还没有负责 lead：先由 ws {rec.get("ws") or "?"} 的 lead 执行 need claim')
    if got != want:
        raise QError(403, f'身份不符：{rec["id"]} 的负责 lead 是 {lead.get("herdr_session")} pane {lead.get("pane_id")}；'
                          f'只有它能发理解卡 / link / drop。lead 换人时新 lead 用 need claim --takeover（原 lead 不在时才行）')


def claim_need(nid, ident, takeover=False, name_of=None):
    """该 ws 的 lead 在自己的 pane 里登记为负责 lead：pane 的 cwd **恰好**是这个 ws 的 lead 目录（lead_cwd，worker 在子目录里，
    不算），且 agent 名是 `lead-<ws>`。已有负责 lead 时：同一个会话幂等；别的会话要 --takeover，且原 lead **确认**不在
    （查询失败 / 多匹配算未知，拒绝）。查 herdr 不持锁；写之前在锁内重新核对 status / ws / lead，有变化就 409。"""
    rec = read_need(nid)
    if rec.get('status') not in LEAD_STATES:
        raise QError(409, f'{nid} 状态是 {rec.get("status")}：只有 Router 分到 ws 之后（assigned）才能 claim')
    ws = rec.get('ws') or ''
    want = lead_cwd(ws).resolve()
    try:
        here = Path(ident.get('cwd') or '').resolve() if ident.get('cwd') else None
    except OSError:
        here = None
    if here != want:
        raise QError(403, f'{nid} 分给了 ws {ws}：只有 cwd 恰好是 {want} 的 lead 能 claim（你的 pane 在 {ident.get("cwd") or "?"}；'
                          f'worker 在子目录里，不能 claim）')
    name = (name_of or agent_name)(ident['herdr_session'], ident['pane_id'])
    if name != f'lead-{ws}':
        raise QError(403, f'只有名为 lead-{ws} 的 agent 能 claim {nid}（你的 pane 是 {name or "无名"}）')
    old = rec.get('lead') or {}
    same = (old.get('herdr_session'), old.get('lead_session')) == (ident['herdr_session'], ident['lead_session'])
    if old and not same:
        if not takeover:
            raise QError(403, f'{nid} 已由 {old.get("herdr_session")} pane {old.get("pane_id")} 负责；接手要 --takeover')
        st, pane, why = presence(old.get('herdr_session'), old.get('lead_session'))
        if st == 'present':
            raise QError(403, f'原负责 lead 还在（pane {pane.get("pane_id")}），不能 takeover')
        if st == 'unknown':
            raise QError(403, f'核对不了原负责 lead 是否还在（{why}），不能 takeover；稍后重试')
    with locked():
        cur = read_need(nid)
        if cur.get('status') not in LEAD_STATES or cur.get('ws') != ws or (cur.get('lead') or {}) != old:
            raise QError(409, f'{nid} 刚刚变了（现在 ws {cur.get("ws")}、状态 {cur.get("status")}），请重新 need show 后再 claim')
        cur['lead'] = {'herdr_session': ident['herdr_session'], 'lead_session': ident['lead_session'],
                       'pane_id': ident['pane_id'], 'claimed_at': now()}
        _ack_delivery(cur, 'lead')
        if not same:
            _save_need(cur, ('lead 接手（takeover）' if old else 'lead claim') + f'：{ident["herdr_session"]} pane {ident["pane_id"]}')
        return cur


def parse_card(text):
    """理解卡：固定五段，前四段不能空；`## 开放问题` 可以空（写「无」也行），有内容时每条带推荐由 lead 负责。"""
    text = _clean_text(text, '理解卡').strip('\n')
    secs = sections(text)
    errors = [f'缺少 `## {k}` 段' for k in CARD_SECTIONS if k not in secs]
    errors += [f'`## {k}` 不能为空' for k in CARD_SECTIONS[:4] if k in secs and not secs[k]]
    extra = [k for k in secs if k not in CARD_SECTIONS]
    if extra:
        errors.append('理解卡只有这五段：' + ' / '.join(CARD_SECTIONS) + f'（多了 {"、".join(extra)}）')
    for p in FORBIDDEN_PHRASES:
        if p in text:
            errors.append(f'含「{p}」：理解卡要不看聊天记录也能看懂')
    if errors:
        raise QError(400, '\n'.join(errors))
    return {k: secs[k] for k in CARD_SECTIONS}


def card(nid, text, ident):
    """负责 lead 发一张理解卡：card_rev + 1，→ discussing / waiting_owner（已确认过的也会回到等 Owner）。"""
    sec = parse_card(text)
    with locked():
        rec = read_need(nid)
        _check_lead(rec, ident)
        if rec.get('status') not in LEAD_STATES:
            raise QError(409, f'{nid} 状态是 {rec.get("status")}，讨论已封存')
        n = geti(rec, 'card_rev') + 1
        rec['thread'].append({'at': now(), 'by': 'lead', 'kind': 'card', 'rev': n, 'card': sec})
        rec.update({'card_rev': n, 'status': 'discussing', 'discuss': 'waiting_owner'})
        _ack_delivery(rec, 'lead')
        _save_need(rec, f'lead 发理解卡 v{n}，等 Owner 回复或确认')
        return rec


def link(nid, urls, ident, issue_info=None, main_repo=''):
    """负责 lead 建好 Issue 后登记（完整 URL，第一个是根）→ filed，线程封存。讨论过的必须先 confirmed；
    filed 之后还可以追加子 Issue。"""
    urls = [u.strip() for u in urls or [] if u and u.strip()]
    if not urls:
        raise QError(400, '至少给一个 --issue')
    for u in urls:
        if not ISSUE_URL_RE.match(u):
            raise QError(400, f'Issue 必须是完整 URL（https://github.com/<owner>/<repo>/issues/<号>）：{u!r}')
    with locked():
        rec = read_need(nid)
        _check_lead(rec, ident)
    if issue_info and not rec['issues']:  # 第一次 link：核对根 Issue（查 GitHub 不持锁，只读）
        m = ISSUE_URL_RE.match(urls[0])
        if main_repo and f'{m.group(1)}/{m.group(2)}'.lower() != main_repo.lower():
            raise QError(400, f'根 Issue 要建在本 ws 的主 repo {main_repo}（你给的是 {m.group(1)}/{m.group(2)}）；跨 repo 的部分作子 Issue')
        body = (issue_info(urls[0]).get('body') or '')
        first = next((l.strip() for l in body.split('\n') if l.strip()), '')
        if first != f'需求：{nid}':
            raise QError(400, f'根 Issue 正文第一行必须是「需求：{nid}」（现在是 {first[:60]!r}），任务页靠它归组')
    with locked():
        rec = read_need(nid)
        _check_lead(rec, ident)
        st = rec.get('status')
        if st == 'discussing':
            raise QError(409, f'{nid} 还在讨论（{rec.get("discuss")}）：Owner 确认最新理解卡后才能建 Issue')
        if geti(rec, 'card_rev') and st not in ('confirmed', 'filed'):
            raise QError(409, f'{nid} 讨论过但还没确认（{st}）')
        if st not in ('assigned', 'confirmed', 'filed'):
            raise QError(409, f'{nid} 状态是 {st}，不能 link')
        rec['issues'] = list(dict.fromkeys(rec['issues'] + urls))
        rec.update({'status': 'filed', 'discuss': ''})
        _ack_delivery(rec, 'lead')
        _save_need(rec, f'lead link {" ".join(urls)}（filed，线程封存）')
        return rec


def drop(nid, url, why, ident, issue_state):
    """负责 lead 决定不做：那个 Issue 必须已写理由并以 not planned 关闭。issue_state(url) → (state, reason)。"""
    why = one_line(_clean_text(why, '理由'))[:500]
    if not ISSUE_URL_RE.match(url or ''):
        raise QError(400, '--issue 必须是那个 not planned Issue 的完整 URL')
    with locked():
        _check_lead(read_need(nid), ident)
    state, reason = issue_state(url)  # 查 GitHub 不持锁
    if (state, reason) != ('CLOSED', 'NOT_PLANNED'):
        raise QError(409, f'{url} 现在是 {state or "?"} / {reason or "?"}：先写清理由并以 not planned 关闭它')
    with locked():
        rec = read_need(nid)
        _check_lead(rec, ident)
        if rec.get('status') not in LEAD_STATES:
            raise QError(409, f'{nid} 状态是 {rec.get("status")}，不能 drop')
        rec['issues'] = list(dict.fromkeys(rec['issues'] + [url]))
        rec.update({'status': 'dropped', 'discuss': ''})
        rec['notes'].append({'at': now(), 'by': 'lead', 'text': f'不做：{why}'})
        _ack_delivery(rec, 'lead')
        _save_need(rec, f'lead 不做：{why}（{url}）')
        return rec


# ----- 投递记账（server.py 用；机制同回答投递：写前记账、held / uncertain、退避、不重复发） -----

def needs_due(at=None):
    at = at or datetime.now(TZ)
    return [r for r in all_needs().values()
            if r.get('delivery') in ('pending', 'held') and (parse_time(r.get('next_try_at')) or at) <= at]


def _need_eligible(rec, seq):
    return rec.get('delivery') in ('pending', 'held') and geti(rec, 'delivery_seq') == seq


def need_mark_held(refs, reason):
    """refs: [(nid, seq)]。"""
    with locked():
        for nid, seq in refs:
            try:
                rec = read_need(nid)
            except QError:
                continue
            if not _need_eligible(rec, seq):
                continue
            rec['delivery'] = 'held'
            rec['last_error'] = one_line(reason)[:300]
            _backoff(rec)
            _save_need(rec, f'投递暂缓：{rec["last_error"]}')


def need_claim(refs):
    out = []
    with locked():
        for nid, seq in refs:
            try:
                rec = read_need(nid)
            except QError:
                continue
            if not _need_eligible(rec, seq):
                continue
            aid = uuid.uuid4().hex[:12]
            rec.update({'delivery': 'sending', 'attempt_id': aid, 'attempts': geti(rec, 'attempts') + 1})
            _save_need(rec, f'开始投递给 {"负责 lead" if rec.get("delivery_to") == "lead" else "Router"}（attempt {aid}）')
            out.append((nid, seq, aid))
    return out


def need_finish(claimed, outcome, error='', router_lead=''):
    """router_lead 非空表示这批投给了 Router（记 Router 的最近投递时间）。"""
    with locked():
        sent = False
        for nid, seq, aid in claimed:
            try:
                rec = read_need(nid)
            except QError:
                continue
            if rec.get('attempt_id') != aid or rec.get('delivery') != 'sending' or geti(rec, 'delivery_seq') != seq:
                continue  # 期间被 take / 新补充 / 重启修复改过：不覆盖
            rec['delivery'] = outcome
            rec['last_error'] = one_line(error)[:300]
            if outcome == 'sent':
                rec.update({'herdr_accepted_at': now(), 'hold_count': 0})
                if router_lead:
                    rec['router_session'] = router_lead
                    sent = True
            elif outcome == 'held':
                _backoff(rec)
            label = {'sent': 'herdr 已接受', 'held': '暂缓', 'uncertain': '不确定是否送达'}[outcome]
            _save_need(rec, f'投给{"lead" if rec.get("delivery_to") == "lead" else "Router"}：{label}（attempt {aid}）' + (f'：{rec["last_error"]}' if error else ''))
        if sent:
            info = router_info()
            if info:
                info['last_delivery_at'] = now()
                _write_state('router.json', info)


def need_renotify(nid):
    with locked():
        rec = read_need(nid)
        stale = rec.get('delivery') == 'sent' and need_stale(rec)
        if unclaimed(rec) and rec.get('delivery') in ('pending', 'sending'):
            return  # 已经在排队 / 正在发：不报错，也不重复排（页面按钮显示后状态刚好变了的那一下）
        if unclaimed(rec) and rec.get('delivery') in ('received', 'sent', 'uncertain', 'none', None):
            _queue_need(rec, 'unclaimed')  # 分到 ws 但没人 claim：叫醒 Router 去看 lead
            _save_need(rec, 'Owner 点了重新通知：还没人 claim，投给 Router')
            return
        if rec.get('delivery') not in ('uncertain', 'held') and not stale:
            raise QError(409, f'{nid} 当前不需要重新通知（delivery {rec.get("delivery")}）')
        old = rec.get('last_error') or ''
        rec.update({'delivery': 'pending', 'next_try_at': now(), 'hold_count': 0,
                    'last_error': _append_error(old, '重新通知：这条可能已经在对方的队列里')})
        _save_need(rec, f'Owner 点了重新通知（之前：{old or "无记录"}）')


def unclaimed_after():
    return float(os.environ.get('QI_UNCLAIMED_AFTER') or 1800)


def unclaimed(rec):
    return rec.get('status') == 'assigned' and not rec.get('lead')


def nudge_unclaimed(at=None):
    """分到 ws 超过 QI_UNCLAIMED_AFTER 秒（默认 30 分钟）还没人 claim：每次分派只自动补投一次给 Router。返回补投的 id。"""
    at = at or datetime.now(TZ)
    out = []
    with locked():
        for nid, rec in all_needs().items():
            t = parse_time(rec.get('assigned_at'))
            if (unclaimed(rec) and not rec.get('nudged_at') and t and (at - t).total_seconds() >= unclaimed_after()
                    and rec.get('delivery') not in ('pending', 'sending', 'held')):
                rec['nudged_at'] = now()
                _queue_need(rec, 'unclaimed')
                _save_need(rec, f'分到 ws {rec.get("ws")} 后 {int(unclaimed_after() // 60)} 分钟没人 claim，补投给 Router')
                out.append(nid)
    return out


def need_unclaim(claimed, why):
    """记了 sending 但发之前发现暂停了：退回 pending（恢复后再发一次），不留在 sending。"""
    with locked():
        for nid, seq, aid in claimed:
            try:
                rec = read_need(nid)
            except QError:
                continue
            if rec.get('attempt_id') == aid and rec.get('delivery') == 'sending' and geti(rec, 'delivery_seq') == seq:
                rec.update({'delivery': 'pending', 'attempt_id': '', 'last_error': one_line(why)[:300]})
                _save_need(rec, f'没有发出（{why}），退回排队')


def need_stale(rec, at=None):
    acc = parse_time(rec.get('herdr_accepted_at'))
    return bool(acc) and ((at or datetime.now(TZ)) - acc).total_seconds() > STALE_WORKING


def repair_needs():
    changes = []
    with locked():
        for nid, rec in all_needs().items():
            if rec.get('delivery') == 'sending':
                rec.update({'delivery': 'uncertain', 'last_error': '服务重启时停在 sending，不确定是否送达'})
                _save_need(rec, '服务重启：停在 sending 的投递改为 uncertain，不自动重发')
                changes.append(f'{nid}: sending → uncertain')
    return changes


# ----- Router 拉起记账：$QI_STATE_DIR/router-ensure.json -----

def ensure_state():
    return _read_state('router-ensure.json')


def _attempt_at(x):
    """attempts 里的一条：{'at', 'id'}（旧文件里可能是纯时间串）。"""
    return parse_time(x.get('at') if isinstance(x, dict) else x)


def ensure_begin(at=None):
    """在锁内决定这次能不能拉起：暂停、已放弃、1 小时内已 3 次都不行。能的话先**预占**一次尝试，返回 (True, attempt_id)；
    不能返回 (False, 原因)。退还只能用 ensure_end(refund=这个 id)，没预占的分支（如请 Router 重新登记）碰不到计数。"""
    at = at or datetime.now(TZ)
    with locked():
        st = ensure_state()
        if paused() or st.get('gave_up'):
            return False, ('全局暂停中' if paused() else 'Router 起不来，已停止自动拉起')
        recent = [x for x in st.get('attempts') or [] if (_attempt_at(x) or at) > at - timedelta(hours=1)]
        if len(recent) >= ENSURE_PER_HOUR:
            st.update({'gave_up': True, 'last_error': _append_error(st.get('last_error') or '',
                                                                   f'1 小时内已拉起 {len(recent)} 次，停止自动拉起')})
            _write_state('router-ensure.json', st)
            return False, 'Router 起不来，已停止自动拉起'
        aid = uuid.uuid4().hex[:12]
        st['attempts'] = recent + [{'at': at.isoformat(timespec='seconds'), 'id': aid}]
        st['total'] = geti(st, 'total') + 1
        st['last_at'] = at.isoformat(timespec='seconds')
        _write_state('router-ensure.json', st)
        return True, aid


def ensure_end(result, error='', refund='', workspace_id=None, root_pane=None):
    """refund：退还**这一次**预占的 attempt（只在还没执行过 agent start 就停下时用）；按 id 找，找不到什么都不改。
    workspace_id / root_pane：这次建的 Router workspace 和它的根 pane（下次若只剩这个空壳就关掉）。"""
    with locked():
        st = ensure_state()
        st['last_result'] = result
        st['last_error'] = one_line(error)[:300] if error else ''
        if refund:
            keep = [x for x in st.get('attempts') or [] if not (isinstance(x, dict) and x.get('id') == refund)]
            if len(keep) != len(st.get('attempts') or []):
                st['attempts'] = keep
                st['total'] = max(0, geti(st, 'total') - 1)
        if workspace_id:
            st['workspace_id'] = workspace_id
            st['root_pane'] = root_pane or ''
        _write_state('router-ensure.json', st)


def ensure_note(key, value):
    with locked():
        st = ensure_state()
        if value is None:
            st.pop(key, None)
        else:
            st[key] = value
        _write_state('router-ensure.json', st)


def note_dispatch(nid, lead, pane, new, status):
    """记下这条需求派给了哪个 lead（页面显示「已派给 lead-<ws>」）；这是分派记录，不是进度。"""
    with locked():
        rec = read_need(nid)
        rec['dispatch'] = {'lead': lead, 'pane_id': pane, 'at': now(), 'new': bool(new), 'status': status or ''}
        _save_need(rec, f'Router 派工给 {lead}（pane {pane}，{"新起" if new else "续派"}，lead 状态 {status or "未知"}）')


def ensure_retry():
    """页面「重新尝试拉起」：清掉放弃标记和本小时的计数。"""
    with locked():
        st = ensure_state()
        st.update({'gave_up': False, 'attempts': [], 'last_error': ''})
        _write_state('router-ensure.json', st)


def need_delivery_view(rec, at=None):
    d = rec.get('delivery')
    err = rec.get('last_error') or ''
    who = 'lead' if rec.get('delivery_to') == 'lead' else 'Router'
    if unclaimed(rec) and d in ('received', 'sent', 'uncertain'):
        return '已分到 ws，等 lead claim', f'分派于 {rec.get("assigned_at")}', True
    if d == 'received':
        return f'{who} 已领取', '', False
    if d == 'sent':
        if need_stale(rec, at):
            return f'已投给 {who}，未领取', f'herdr 于 {rec.get("herdr_accepted_at")} 接受', True
        return f'已投给 {who}', f'herdr 于 {rec.get("herdr_accepted_at")} 接受', False
    if d == 'held':
        return '暂缓投递', f'{err}；下次尝试 {rec.get("next_try_at")}', True
    if d == 'uncertain':
        return '不确定是否送达', err, True
    if d == 'sending':
        return '投递中', '', False
    if d == 'pending':
        return '排队投递', err, False
    return '', '', False
