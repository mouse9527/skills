#!/usr/bin/env python3
"""Personal-data scan of what a push would make public (run by scripts/preflight.sh; stdlib only).

Scans, from git objects (not the working tree):
  - the final tree of HEAD: every file
  - every commit in RANGE (default origin/main..HEAD): author / committer identity (must be in
    policy.rules.COMMIT_IDENTITIES), the message, and every file version it adds — history stays public even when a
    later commit removes something
with the generic rules + the private term list. The term list lives outside every repository; it must exist, be
0600 (no group / other bits) and non-empty, else the scan fails (fail closed). Symlinks, submodules and binary files
are refused. Output names commit / file / line / kind only, never the matched text or a term.

    python3 scripts/pii_scan.py --terms FILE [--range BASE..HEAD]
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from policy import rules  # noqa: E402


class ScanError(Exception):
    pass


def load_terms(path: str) -> list[str]:
    p = Path(os.path.expanduser(path))
    try:
        st = p.stat()
    except FileNotFoundError:
        raise ScanError(f'private term list {p} does not exist (required before anything is made public)') from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ScanError(f'private term list {p}: permissions too open, chmod 600')
    terms = [l.strip() for l in p.read_text(encoding='utf-8').splitlines() if l.strip() and not l.lstrip().startswith('#')]
    if not terms:
        raise ScanError(f'private term list {p} is empty')
    return terms


def git(*args: str) -> str:
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout


def blob_text(sha: str) -> str | None:
    data = subprocess.run(['git', 'cat-file', 'blob', sha], check=True, capture_output=True).stdout
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def check_entry(where: str, mode: str, sha: str, terms: list[str], seen: dict) -> list[str]:
    if mode == '120000':
        return [f'{where}: symlink (not allowed)']
    if mode == '160000':
        return [f'{where}: submodule (not allowed)']
    if sha not in seen:
        text = blob_text(sha)
        seen[sha] = ['binary file (cannot be checked)'] if text is None else \
            [f.split(':', 1)[1] for f in rules.scan_text('', text, terms)]
    return [f'{where}:{f}' if f[:1].isdigit() else f'{where}: {f}' for f in seen[sha]]


def scan(rng: str, terms: list[str]) -> dict:
    seen: dict[str, list[str]] = {}
    findings: list[str] = []
    tree = [l.split('\t', 1) for l in git('ls-tree', '-r', '--full-tree', 'HEAD').splitlines()]
    for meta, path in tree:
        mode, _, sha = meta.split()
        findings += check_entry(f'HEAD:{path}', mode, sha, terms, seen)
    commits = git('rev-list', '--reverse', rng).split()
    for c in commits:
        short = c[:12]
        ae, ce = git('show', '-s', '--format=%ae%n%ce', c).split('\n')[:2]
        for who, email in (('author', ae), ('committer', ce)):
            if email not in rules.COMMIT_IDENTITIES:
                findings.append(f'{short}: {who} e-mail is not an allowed public identity')
        for who, name in zip(('author', 'committer'), git('show', '-s', '--format=%an%n%cn', c).split('\n')[:2]):
            findings += [f'{short}: {who} name: {f.split(": ", 1)[1]}' for f in rules.scan_text('x', name, terms)]
        msg = git('show', '-s', '--format=%B', c)
        findings += [f'{short}: message:{f.split(":", 1)[1]}' for f in rules.scan_text('', msg, terms)]
        for line in git('diff-tree', '-r', '--root', '--no-commit-id', '--no-renames', c).splitlines():
            meta, path = line.split('\t', 1)
            _, mode, _, sha, status = meta.split()
            if status != 'D':
                findings += check_entry(f'{short}:{path}', mode, sha, terms, seen)
    return {'range': rng, 'commits': len(commits), 'tree_files': len(tree), 'blobs_scanned': len(seen),
            'private_terms': len(terms), 'findings': findings}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--terms', required=True)
    ap.add_argument('--range', default='origin/main..HEAD')
    a = ap.parse_args()
    try:
        r = scan(a.range, load_terms(a.terms))
    except (ScanError, subprocess.CalledProcessError) as e:
        print(json.dumps({'error': str(e).splitlines()[0][:300]}))
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 1 if r['findings'] else 0


if __name__ == '__main__':
    sys.exit(main())
