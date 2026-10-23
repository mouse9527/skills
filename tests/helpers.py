"""Fixtures for the tests: a throw-away git repository shaped like this one. Fictional data only; personal-data
samples are assembled at runtime so that no file in this repository holds one."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDENTITY = {'GIT_AUTHOR_NAME': 'Test', 'GIT_COMMITTER_NAME': 'Test',
            'GIT_AUTHOR_EMAIL': '34568564+mouse9527' + '@users.noreply.github.com',
            'GIT_COMMITTER_EMAIL': '34568564+mouse9527' + '@users.noreply.github.com'}

# fictional samples (split so the rules do not match this file)
PHONE = '138' + '0013' + '8000'
TERM = '虚构' + '区'


def skill_md(sid, body='正文\n', desc=None, extra=''):
    return f'---\nname: {sid}\ndescription: {desc or "测试 " + sid + "：何时用"}\n{extra}---\n# {sid}\n\n{body}'


def git(repo, *args, env=None):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENTITY, **(env or {})}).stdout.strip()


class FakeRepo:
    """skills/<group>/<id>/SKILL.md + marketplace; commit() returns the full SHA."""

    def __init__(self, groups=None):
        self.dir = Path(tempfile.mkdtemp(prefix='skills-test-'))
        git(self.dir, 'init', '-q', '-b', 'main')
        self.groups = groups or {'assistant': ['alpha', 'beta'], 'dev': ['gamma']}
        for g, ids in self.groups.items():
            for s in ids:
                self.write(f'skills/{g}/{s}/SKILL.md', skill_md(s))
        self.write_manifest()
        shutil.copytree(ROOT / 'policy', self.dir / 'policy', ignore=shutil.ignore_patterns('__pycache__'))
        self.write('.gitignore', '__pycache__/\n')
        self.write('README.md', '# t\n\n<!-- skills-index:start -->\n<!-- skills-index:end -->\n')

    def write(self, rel, text, mode=None):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        if mode:
            p.chmod(mode)

    def remove(self, rel):
        p = self.dir / rel
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    def write_manifest(self, groups=None):
        g = groups or self.groups
        mp = {'name': 't', 'owner': {'name': 't'}, 'plugins': [
            {'name': k, 'source': './', 'strict': False, 'skills': [f'./skills/{k}/{s}' for s in v]} for k, v in g.items()]}
        self.write('.claude-plugin/marketplace.json', json.dumps(mp))

    def commit(self, msg='c'):
        git(self.dir, 'add', '-A')
        git(self.dir, 'commit', '-q', '--allow-empty', '-m', msg)
        return git(self.dir, 'rev-parse', 'HEAD')
