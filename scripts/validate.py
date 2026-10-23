#!/usr/bin/env python3
"""Repository rules for mouse9527/skills (CI and scripts/preflight.sh). Structure per the Agent Skills spec is checked
separately by `agentskills validate` (skills-ref); this script checks what is specific to this repository:

  - layout: skills/<group>/<id>/SKILL.md; frontmatter `name` = <id> (ASCII), description present and <= 1024
  - ids unique across groups; at least one skill
  - .claude-plugin/marketplace.json <-> directories, both ways: one plugin per non-empty group, `source: "./"`,
    `strict: false`, no `version`; every skill listed, except drafts (`metadata.status: draft`), which must not be
  - no symlinks anywhere (installers create links; the repository holds none)
  - generic personal-data rules (policy/rules.py) on every file; binary files are refused (cannot be checked)
  - skills in client-neutral groups name no client / gateway; no skill writes a client-prefixed tool name
  - README index matches the skills (`--write-readme` regenerates it)

    python3 scripts/validate.py [--write-readme] [--root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from policy import rules  # noqa: E402

FRONT_RE = re.compile(r'\A---\n(.*?)\n---\n', re.S)
SPEC_KEYS = {'name', 'description', 'license', 'compatibility', 'metadata', 'allowed-tools'}
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', 'node_modules'}
SKIP_FILES = {'.DS_Store'}
README_START, README_END = '<!-- skills-index:start -->', '<!-- skills-index:end -->'
MARKETPLACE = '.claude-plugin/marketplace.json'


def frontmatter(text: str) -> dict | None:
    import yaml
    m = FRONT_RE.match(text)
    if not m:
        return None
    fm = yaml.safe_load(m.group(1))
    return fm if isinstance(fm, dict) else None


def repo_files(root: Path) -> list[Path]:
    out = []
    for d, dirs, files in os.walk(root):
        out += [Path(d) / x for x in dirs if (Path(d) / x).is_symlink()]
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not (Path(d) / x).is_symlink())
        out += [Path(d) / f for f in sorted(files) if f not in SKIP_FILES]
    return out


def load_skills(root: Path, errors: list[str]) -> dict[str, dict]:
    """id -> {group, path, fm}"""
    skills: dict[str, dict] = {}
    base = root / 'skills'
    if not base.is_dir():
        errors.append('skills/: missing')
        return skills
    for g in sorted(base.iterdir()):
        if g.name in SKIP_FILES:
            continue
        if not g.is_dir() or g.is_symlink():
            errors.append(f'skills/{g.name}: only group directories belong here')
            continue
        if not rules.GROUP_RE.match(g.name):
            errors.append(f'skills/{g.name}: group name must match {rules.GROUP_RE.pattern}')
        entries = sorted(x for x in g.iterdir() if x.name not in SKIP_FILES)
        if not entries:
            errors.append(f'skills/{g.name}: empty group (create groups only with real content)')
        for d in entries:
            rel = f'skills/{g.name}/{d.name}'
            if not d.is_dir() or d.is_symlink() or not (d / 'SKILL.md').is_file():
                errors.append(f'{rel}: every entry of a group is a skill directory with SKILL.md')
                continue
            if not rules.SKILL_ID_RE.match(d.name):
                errors.append(f'{rel}: skill id (directory) must match {rules.SKILL_ID_RE.pattern}')
            try:
                fm = frontmatter((d / 'SKILL.md').read_text(encoding='utf-8'))
            except Exception as e:  # noqa: BLE001
                errors.append(f'{rel}/SKILL.md: frontmatter does not parse ({type(e).__name__})')
                continue
            if fm is None:
                errors.append(f'{rel}/SKILL.md: missing frontmatter')
                continue
            if fm.get('name') != d.name:
                errors.append(f'{rel}/SKILL.md: frontmatter name must equal the directory name {d.name!r}')
            desc = fm.get('description')
            if not isinstance(desc, str) or not desc.strip():
                errors.append(f'{rel}/SKILL.md: description required')
            elif len(desc) > rules.DESCRIPTION_MAX:
                errors.append(f'{rel}/SKILL.md: description longer than {rules.DESCRIPTION_MAX}')
            extra = sorted(set(fm) - SPEC_KEYS)
            if extra:
                errors.append(f'{rel}/SKILL.md: frontmatter keys outside the Agent Skills spec: {extra} (use metadata)')
            if d.name in skills:
                errors.append(f'{rel}: id already used in group {skills[d.name]["group"]}')
                continue
            skills[d.name] = {'group': g.name, 'path': rel, 'fm': fm}
    if not skills:
        errors.append('no skills found (expected at least one)')
    return skills


def is_draft(s: dict) -> bool:
    return ((s['fm'].get('metadata') or {}).get('status')) == 'draft'


def check_marketplace(root: Path, skills: dict, errors: list[str]) -> None:
    p = root / MARKETPLACE
    try:
        mp = json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        errors.append(f'{MARKETPLACE}: missing')
        return
    except ValueError as e:
        errors.append(f'{MARKETPLACE}: not JSON ({e})')
        return
    if not isinstance(mp.get('name'), str) or not isinstance(mp.get('owner'), dict):
        errors.append(f'{MARKETPLACE}: name and owner are required')
    if 'version' in mp or 'version' in (mp.get('metadata') or {}):
        errors.append(f'{MARKETPLACE}: no "version" (consumers pin the commit SHA)')
    listed: dict[str, str] = {}
    groups_seen = set()
    for pl in mp.get('plugins') or []:
        g = pl.get('name')
        where = f'{MARKETPLACE}: plugin {g!r}'
        if g in groups_seen:
            errors.append(f'{where}: listed twice')
        groups_seen.add(g)
        if pl.get('source') != './' or pl.get('strict') is not False:
            errors.append(f'{where}: needs "source": "./" and "strict": false')
        if 'version' in pl:
            errors.append(f'{where}: no "version" (consumers pin the commit SHA)')
        paths = pl.get('skills')
        if not isinstance(paths, list) or not paths:
            errors.append(f'{where}: "skills" must list the group\'s skills (no empty plugins)')
            continue
        for sp in paths:
            m = re.fullmatch(r'\./skills/([^/]+)/([^/]+)', str(sp))
            if not m or m.group(1) != g:
                errors.append(f'{where}: {sp!r} is not ./skills/{g}/<id>')
                continue
            sid = m.group(2)
            if sid not in skills or skills[sid]['group'] != g:
                errors.append(f'{where}: {sp} does not exist')
            elif sid in listed:
                errors.append(f'{where}: {sp} listed twice')
            elif is_draft(skills[sid]):
                errors.append(f'{where}: {sp} is a draft (metadata.status: draft) and must not be listed')
            listed[sid] = g
    for sid, s in sorted(skills.items()):
        if sid not in listed and not is_draft(s):
            errors.append(f'{s["path"]}: not in {MARKETPLACE} (list it, or mark metadata.status: draft)')


def check_files(root: Path, skills: dict, errors: list[str]) -> int:
    neutral = {s['path'] for s in skills.values() if s['group'] in rules.CLIENT_NEUTRAL_GROUPS}
    skill_dirs = {s['path'] for s in skills.values()}
    n = 0
    for p in repo_files(root):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            errors.append(f'{rel}: symlink (the repository holds none; installers create links)')
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            errors.append(f'{rel}: binary file (cannot be checked for personal data)')
            continue
        n += 1
        errors += [f'personal data: {f}' for f in rules.scan_text(rel, text)]
        owner = next((d for d in skill_dirs if rel.startswith(d + '/')), None)
        if owner is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if owner in neutral and rules.PLATFORM_RE.search(line):
                errors.append(f'{rel}:{i}: names a client / gateway (skills in this group stay client-neutral)')
            if rules.PREFIXED_TOOL_RE.search(line):
                errors.append(f'{rel}:{i}: client-prefixed tool name (write the upstream tool name only)')
    return n


def readme_index(skills: dict) -> str:
    rows = [README_START, '', '| group | skill | |', '|---|---|---|']
    for sid, s in sorted(skills.items(), key=lambda kv: (kv[1]['group'], kv[0])):
        desc = str(s['fm'].get('description') or '')
        head = desc.split('：', 1)[0] if '：' in desc[:40] else desc[:40] + ('…' if len(desc) > 40 else '')
        draft = '（draft）' if is_draft(s) else ''
        rows.append(f'| {s["group"]} | [{sid}]({s["path"]}/SKILL.md) | {head.replace("|", "/")}{draft} |')
    return '\n'.join(rows + ['', README_END])


def check_readme(root: Path, skills: dict, errors: list[str], write: bool) -> None:
    p = root / 'README.md'
    text = p.read_text(encoding='utf-8') if p.exists() else ''
    m = re.search(re.escape(README_START) + r'.*?' + re.escape(README_END), text, re.S)
    if not m:
        errors.append(f'README.md: needs the index markers {README_START} … {README_END}')
        return
    want = readme_index(skills)
    if m.group(0) != want:
        if write:
            p.write_text(text[:m.start()] + want + text[m.end():], encoding='utf-8')
        else:
            errors.append('README.md: skill index is out of date (python3 scripts/validate.py --write-readme)')


def validate(root: Path, write_readme: bool = False) -> tuple[list[str], dict]:
    errors: list[str] = []
    skills = load_skills(root, errors)
    check_marketplace(root, skills, errors)
    n = check_files(root, skills, errors)
    if skills:
        check_readme(root, skills, errors, write_readme)
    groups = sorted({s['group'] for s in skills.values()})
    return errors, {'skills': len(skills), 'groups': {g: sum(1 for s in skills.values() if s['group'] == g) for g in groups},
                    'files_checked': n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument('--write-readme', action='store_true')
    a = ap.parse_args()
    errors, summary = validate(Path(a.root), a.write_readme)
    for e in errors:
        print('ERROR', e)
    print(json.dumps({**summary, 'errors': len(errors)}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
