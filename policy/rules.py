"""Public rules for everything in this repository (stdlib only; used by scripts/validate.py and scripts/pii_scan.py).

The repository is public: nothing in it may carry personal data. Two layers:
  - GENERIC below (public, run by CI): phone numbers, ID-card numbers, e-mail addresses, coordinates with >= 3
    decimals (except PUBLIC_COORDS), address shapes (house numbers, building / unit / room, named buildings / compounds),
    statements about the user's device security (that it lacks a screen lock / passcode, that password-free login is
    enabled) and where the device sits (the phone stays at home)
  - a private term list kept OUTSIDE every repository (real places found so far), used only by scripts/preflight.sh
    before anything is pushed; never committed, never printed
Findings name the file, line and kind only — never the matched text (it may be the personal data itself).
Tests build their fictional samples at runtime, so no file here holds a literal sample.
"""
from __future__ import annotations

import re

# public example coordinates skills may use (People's Square, Shanghai)
PUBLIC_COORDS = {('31.230', '121.473'), ('31.2304', '121.4737')}

GENERIC = (
    ('phone number', re.compile(r'(?<![\d.])(?:\+?86[- ]?)?1[3-9]\d{9}(?![\d.])')),
    ('ID-card number', re.compile(r'(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])')),
    ('e-mail address', re.compile(r'[\w.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+')),
    ('house number', re.compile(r'[一-龥]{1,8}(?:路|街|道|巷|弄|里)\s*\d+\s*(?:号|弄)')),
    ('building / unit / room', re.compile(r'\d+\s*(?:号楼|幢|栋|单元|室)(?![一-龥]{0,2}(?:内|外))')),
    ('named building / compound', re.compile(r'[一-龥]{2,10}(?:大厦|小区|公寓|新村|家园|花苑|写字楼)')),
    # device security configuration stated as a fact (lead review 2026-10-24): that the device lacks a screen lock /
    # passcode, that password-free login is enabled. Rules may say what to do at a lock screen, never what the device lacks
    ('device security statement', re.compile(
        r'(?:没有|没设置?|未设置?|不设|无|关闭了?|取消了?|去掉了?)\s*(?:锁屏|开机|解锁|屏幕)?\s*(?:密码|PIN|口令|锁屏)'
        r'|免密(?:登录|支付|解锁)?\s*(?:已|开|开启|打开)|(?:已|开启了?|打开了?)\s*免密'
        r'|\bno\s+(?:screen[- ]?lock|passcode|lock[- ]?screen\s+password)\b', re.I)),
    # where the user's device sits / habits ("the phone stays at home")
    ('device whereabouts', re.compile(
        r'(?:手机|设备|电脑|平板|iPad|iPhone)[^。；\n]{0,6}(?:放在|留在|一直在|常在)\s*(?:家|公司|办公室|单位)')),
)
COORD_RE = re.compile(r'(-?\d{1,3}\.(\d{3,}))\s*[,，]\s*(-?\d{1,3}\.(\d{3,}))')

# the only identity allowed on public commits (GitHub noreply; already public)
COMMIT_IDENTITIES = {'34568564+mouse9527' + '@users.noreply.github.com'}  # split so the e-mail rule passes this file

# groups whose skills every client loads (chat UI, terminal, CLIs): the text stays client-neutral
CLIENT_NEUTRAL_GROUPS = {'assistant'}
PLATFORM_RE = re.compile(r'open[\s_-]?web[\s_-]?ui|owui|\bOI\b|litellm|claude|codex|herdr', re.I)

# tool names as one client shows them (prefix added by the client / gateway) are never written in a skill:
# `mcp__<server>__<tool>` (Claude Code / Codex / Pi), `mcp_<server>-<tool>` (chat UI). Name the upstream tool only
PREFIXED_TOOL_RE = re.compile(r'\bmcp__[A-Za-z0-9-]+__[A-Za-z0-9_*-]+|\bmcp_[a-z0-9]+-[a-z0-9_*]+')

SKILL_ID_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
GROUP_RE = SKILL_ID_RE
DESCRIPTION_MAX = 1024


def scan_text(where: str, text: str, terms: list[str] | None = None) -> list[str]:
    """PII findings as '<where>:<line>: <kind>' — the matched text is never included."""
    out = []
    lowered = [t.lower() for t in terms or [] if t]
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in GENERIC:
            if rx.search(line):
                out.append(f'{where}:{n}: {kind}')
        for m in COORD_RE.finditer(line):
            if (m.group(1), m.group(3)) not in PUBLIC_COORDS:
                out.append(f'{where}:{n}: coordinate with >= 3 decimals (not a public example)')
        low = line.lower()
        hits = sum(1 for t in lowered if t in low)
        if hits:
            out.append(f'{where}:{n}: {hits} private term(s)')
    return out
