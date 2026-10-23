#!/usr/bin/env python3
"""Project skills of selected groups into the flat layout clients discover (<id>/SKILL.md). Stdlib only; it is a
set projector, not a package manager.

  release   --root DIR (--repo GITDIR | --github OWNER/REPO) --ref REF --groups g1,g2 [--loop SECONDS] [--link DEST]…
            Immutable release of one full commit SHA: DIR/releases/<sha12>-<groups>/view/<id>/…, then DIR/current is
            switched atomically. REF = full SHA (pinned) or a branch (resolved to its SHA every run).
            DIR itself is never replaced (it may be a shared volume); only releases/, current, .project.json inside.
            --link DEST (repeatable): after every run, DEST/<id> -> DIR/current/view/<id> for each skill (as `link`)
  rollback  --root DIR        current <-> previous release
  link      --dest DIR (--view VIEWDIR | --repo WORKTREE --groups g1,g2)
            One leaf symlink per skill in DIR (a client's skills directory): DIR/<id> -> VIEWDIR/<id>, or in maintainer
            mode -> WORKTREE/skills/<group>/<id> (live: edits show immediately; no SHA). Only links recorded in
            DIR/.skills-project.json are ever replaced or removed; any other entry with the same name -> failure.
  unlink    --dest DIR        remove the links this tool created

Contract: a release is activated only after it is fully built and its skill set equals what the manifest lists for
the selected groups; unknown groups, a frontmatter name != directory, duplicate ids, symlinks / special files in the
archive -> failure and the current release stays. The previous release is kept for rollback; older ones are pruned
(only releases this tool built). One JSON line per run.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

SHA_RE = re.compile(r'^[0-9a-f]{40}$')
ID_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
FRONT_RE = re.compile(r'\A---\n(.*?)\n---\n', re.S)
NAME_RE = re.compile(r'^name:[ \t]*["\']?([^"\'\n]*?)["\']?[ \t]*$', re.M)
MANIFEST = '.claude-plugin/marketplace.json'
MAX_BYTES, MAX_FILES = 32 * 1024 * 1024, 5000
TIMEOUT = 60
STATE = '.project.json'
LINK_STATE = '.skills-project.json'


class ProjectError(Exception):
    pass


def log(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


# ---------- sources: {path: (bytes, executable)} at one commit ----------

def _read_tar(data: bytes, strip_top: bool) -> dict[str, tuple[bytes, bool]]:
    files: dict[str, tuple[bytes, bool]] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            name = m.name.split('/', 1)[1] if strip_top and '/' in m.name else ('' if strip_top else m.name)
            if m.isdir() or not name or m.name == 'pax_global_header':
                continue
            if not m.isfile():
                raise ProjectError(f'archive holds a symlink / special file: {name!r}')
            if name.startswith('/') or '..' in name.split('/'):
                raise ProjectError(f'unsafe path in archive: {name!r}')
            total += m.size
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise ProjectError('archive too large')
            files[name] = (t.extractfile(m).read(), bool(m.mode & 0o111))
    return files


def resolve_repo(repo: str, ref: str) -> str:
    try:
        return subprocess.run(['git', '-C', repo, 'rev-parse', '--verify', '--quiet', ref + '^{commit}'],
                              check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError:
        raise ProjectError(f'ref {ref!r} not found in {repo}') from None


def fetch_repo(repo: str, sha: str) -> dict:
    data = subprocess.run(['git', '-C', repo, 'archive', '--format=tar', sha], check=True, capture_output=True).stdout
    return _read_tar(data, strip_top=False)


def _get(url: str, limit: int = MAX_BYTES) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # HTTPS_PROXY is honoured; TLS fully verified
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ProjectError(f'response larger than {limit} bytes')
    return data


def resolve_github(slug: str, ref: str, base: str = 'https://github.com') -> str:
    if SHA_RE.match(ref):
        return ref
    # smart-HTTP ref advertisement: no API token / rate limit
    raw = _get(f'{base}/{slug}.git/info/refs?service=git-upload-pack', 4 * 1024 * 1024).decode('utf-8', 'replace')
    m = re.search(r'([0-9a-f]{40}) refs/heads/' + re.escape(ref) + r'(?:\x00|\n)', raw)
    if not m:
        raise ProjectError(f'branch {ref!r} not found on {slug}')
    return m.group(1)


def fetch_github(slug: str, sha: str, base: str = 'https://codeload.github.com') -> dict:
    return _read_tar(_get(f'{base}/{slug}/tar.gz/{sha}'), strip_top=True)


# ---------- selection ----------

def select(files: dict, groups: list[str]) -> dict[str, str]:
    """id -> group dir path (skills/<g>/<id>) for the selected groups, as the manifest lists them."""
    if MANIFEST not in files:
        raise ProjectError(f'{MANIFEST} missing at this commit')
    plugins = {p.get('name'): p for p in json.loads(files[MANIFEST][0]).get('plugins') or []}
    out: dict[str, str] = {}
    for g in groups:
        if g not in plugins:
            raise ProjectError(f'unknown group {g!r} (manifest has {sorted(plugins)})')
        for sp in plugins[g].get('skills') or []:
            m = re.fullmatch(r'\./(skills/' + re.escape(g) + r'/([^/]+))', str(sp))
            if not m or not ID_RE.match(m.group(2)):
                raise ProjectError(f'group {g}: bad skill path {sp!r}')
            sid, path = m.group(2), m.group(1)
            if sid in out:
                raise ProjectError(f'skill id {sid} selected twice')
            md = files.get(path + '/SKILL.md')
            if md is None:
                raise ProjectError(f'{path}/SKILL.md missing')
            fm = FRONT_RE.match(md[0].decode('utf-8', 'replace'))
            nm = NAME_RE.search(fm.group(1)) if fm else None
            if not nm or nm.group(1).strip() != sid:
                raise ProjectError(f'{path}/SKILL.md: frontmatter name must be {sid!r}')
            out[sid] = path
    if not out:
        raise ProjectError('selected groups hold no skills')
    return out


# ---------- release ----------

def _state(root: Path) -> dict:
    try:
        return json.loads((root / STATE).read_text())
    except FileNotFoundError:
        return {}


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
    os.replace(tmp, path)


def _switch(root: Path, name: str) -> None:
    tmp = root / 'current.tmp'
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f'releases/{name}', tmp)
    os.replace(tmp, root / 'current')


def build(root: Path, sha: str, groups: list[str], files: dict) -> tuple[str, list[str]]:
    sel = select(files, groups)
    name = f'{sha[:12]}-{"+".join(groups)}'
    rel = root / 'releases'
    rel.mkdir(exist_ok=True)
    final = rel / name
    if (final / 'release.json').is_file():
        return name, sorted(sel)
    tmp = rel / f'.tmp-{name}'
    shutil.rmtree(tmp, ignore_errors=True)
    view = tmp / 'view'
    for sid, path in sel.items():
        for f, (data, exe) in files.items():
            if f.startswith(path + '/'):
                target = view / sid / f[len(path) + 1:]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if exe else 0o644)
    for d, dirs, _ in os.walk(tmp):
        for x in dirs:
            os.chmod(os.path.join(d, x), 0o755)
    got = sorted(p.name for p in view.iterdir() if (p / 'SKILL.md').is_file())
    if got != sorted(sel) or sorted(p.name for p in view.iterdir()) != got:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ProjectError(f'built view does not match the selection: {got}')
    _write_json(tmp / 'release.json', {'sha': sha, 'groups': groups, 'skills': got, 'built': int(time.time())})
    os.chmod(tmp, 0o755)
    shutil.rmtree(final, ignore_errors=True)
    os.rename(tmp, final)
    return name, got


def release(root: Path, resolve, fetch, ref: str, groups: list[str]) -> dict:
    if not root.is_dir():
        raise ProjectError(f'{root} does not exist (the root is never created or replaced here)')
    st = _state(root)
    if st.pop('pinned_after_rollback', None):  # a manual release resumes the loop, even if nothing changes
        _write_json(root / STATE, st)
    sha = resolve(ref)
    if not SHA_RE.match(sha):
        raise ProjectError(f'not a full commit SHA: {sha!r}')
    want = f'{sha[:12]}-{"+".join(groups)}'
    if st.get('current') == want and (root / 'current').is_symlink():
        return {'unchanged': want}
    if st.get('failed', {}).get('release') == want:
        return {'still_failing': want, 'why': st['failed']['why']}
    files = fetch(sha)  # network / git errors: nothing recorded, retried next run
    try:
        name, skills = build(root, sha, groups, files)
    except ProjectError as e:  # this commit is unusable: remembered, not rebuilt every run
        st['failed'] = {'release': want, 'why': str(e)[:300]}
        _write_json(root / STATE, st)
        raise
    prev = st.get('current')
    _switch(root, name)
    st.update({'current': name, 'previous': prev if prev != name else st.get('previous'), 'sha': sha,
               'groups': groups, 'skills': skills})
    st.pop('failed', None)
    st.pop('pinned_after_rollback', None)
    keep = {st['current'], st.get('previous')}
    owned = set(st.get('owned', [])) | {name}
    for old in sorted(owned - keep):
        shutil.rmtree(root / 'releases' / old, ignore_errors=True)
    st['owned'] = sorted(owned & keep)
    _write_json(root / STATE, st)
    return {'released': name, 'sha': sha, 'skills': skills, 'previous': st.get('previous')}


def rollback(root: Path) -> dict:
    st = _state(root)
    prev = st.get('previous')
    if not prev or not (root / 'releases' / prev / 'release.json').is_file():
        raise ProjectError('no previous release to roll back to')
    info = json.loads((root / 'releases' / prev / 'release.json').read_text())
    _switch(root, prev)
    st.update({'current': prev, 'previous': st['current'], 'sha': info['sha'], 'groups': info['groups'],
               'skills': info['skills'], 'pinned_after_rollback': True})
    _write_json(root / STATE, st)
    return {'rolled_back_to': prev}


# ---------- link ----------

def link(dest: Path, targets: dict[str, str]) -> dict:
    """targets: id -> absolute link target."""
    if not dest.is_dir():
        raise ProjectError(f'{dest} does not exist')
    sp = dest / LINK_STATE
    owned = set(json.loads(sp.read_text()).get('owned', [])) if sp.exists() else set()
    conflicts = [s for s in targets if s not in owned and (os.path.lexists(dest / s))]
    if conflicts:
        raise ProjectError(f'{dest}: entries not created by this tool already use these names: {sorted(conflicts)}')
    foreign = [s for s in owned if os.path.lexists(dest / s) and not (dest / s).is_symlink()]
    if foreign:
        raise ProjectError(f'{dest}: recorded entries are no longer links (changed by hand?): {sorted(foreign)}')
    changed = []
    for sid, target in sorted(targets.items()):
        p = dest / sid
        if p.is_symlink() and os.readlink(p) == target:
            continue
        tmp = dest / f'.{sid}.link-tmp'
        if os.path.lexists(tmp):
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, p)
        changed.append(sid)
    removed = []
    for sid in sorted(owned - set(targets)):
        if (dest / sid).is_symlink():
            (dest / sid).unlink()
        removed.append(sid)
    _write_json(sp, {'owned': sorted(targets)})
    return {'dest': str(dest), 'linked': sorted(targets), 'changed': changed, 'removed': removed}


def view_targets(view: str) -> dict[str, str]:
    v = Path(view)
    if not v.is_dir():
        raise ProjectError(f'{view} is not a directory')
    return {p.name: os.path.join(view, p.name) for p in sorted(v.iterdir())
            if ID_RE.match(p.name) and (p / 'SKILL.md').is_file()}


def worktree_targets(repo: str, groups: list[str]) -> dict[str, str]:
    root = Path(repo).resolve()
    files = {}
    for p in [root / MANIFEST, *root.glob('skills/*/*/SKILL.md')]:
        files[p.relative_to(root).as_posix()] = (p.read_bytes(), False)
    return {sid: str(root / path) for sid, path in select(files, groups).items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('release')
    r.add_argument('--root', required=True)
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument('--repo')
    src.add_argument('--github')
    r.add_argument('--ref', required=True)
    r.add_argument('--groups', required=True)
    r.add_argument('--loop', type=float)
    r.add_argument('--link', action='append', default=[], help='client skills directory to keep linked to current')
    rb = sub.add_parser('rollback')
    rb.add_argument('--root', required=True)
    lk = sub.add_parser('link')
    lk.add_argument('--dest', required=True)
    lsrc = lk.add_mutually_exclusive_group(required=True)
    lsrc.add_argument('--view')
    lsrc.add_argument('--repo')
    lk.add_argument('--groups')
    ul = sub.add_parser('unlink')
    ul.add_argument('--dest', required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'release':
            groups = [g for g in a.groups.split(',') if g]
            if a.repo:
                res, fet = (lambda ref: resolve_repo(a.repo, ref)), (lambda sha: fetch_repo(a.repo, sha))
            else:
                res, fet = (lambda ref: resolve_github(a.github, ref)), (lambda sha: fetch_github(a.github, sha))
            while True:
                try:
                    if a.loop and _state(Path(a.root)).get('pinned_after_rollback'):
                        log(skipped='pinned after rollback (run release without --loop to resume)')
                    else:
                        log(**release(Path(a.root), res, fet, a.ref, groups), t=time.strftime('%Y-%m-%dT%H:%M:%S'))
                except Exception as e:  # noqa: BLE001  the current release stays
                    log(error=f'{type(e).__name__}: {e}'[:300], t=time.strftime('%Y-%m-%dT%H:%M:%S'))
                    if not a.loop:
                        return 1
                for dest in a.link:  # also after a failed run: links follow whatever release is current
                    try:
                        view = os.path.join(a.root, 'current', 'view')
                        if os.path.isdir(view):
                            out = link(Path(os.path.expanduser(dest)), view_targets(view))
                            if out['changed'] or out['removed']:
                                log(**out)
                    except (ProjectError, OSError) as e:
                        log(error=f'link {dest}: {type(e).__name__}: {e}'[:300])
                        if not a.loop:
                            return 1
                if not a.loop:
                    return 0
                time.sleep(a.loop)
        if a.cmd == 'rollback':
            log(**rollback(Path(a.root)))
        elif a.cmd == 'link':
            if a.view:
                targets = view_targets(a.view)
            else:
                if not a.groups:
                    raise ProjectError('--repo needs --groups')
                targets = worktree_targets(a.repo, [g for g in a.groups.split(',') if g])
            log(**link(Path(os.path.expanduser(a.dest)), targets))
        elif a.cmd == 'unlink':
            log(**link(Path(os.path.expanduser(a.dest)), {}))
    except (ProjectError, OSError, ValueError) as e:
        log(error=f'{type(e).__name__}: {e}'[:300])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
