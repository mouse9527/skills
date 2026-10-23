"""scripts/project.py: release / update / delete / rollback / failure keeps current / link ownership."""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helpers import ROOT, FakeRepo, skill_md

sys.path.insert(0, str(ROOT / 'scripts'))
import project as pj  # noqa: E402


def view(root):
    return sorted(os.listdir(Path(root) / 'current' / 'view'))


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.r = FakeRepo()
        self.root = Path(tempfile.mkdtemp(prefix='skills-root-'))

    def rel(self, ref='main', groups=('assistant',)):
        return pj.release(self.root, lambda x: pj.resolve_repo(str(self.r.dir), x),
                          lambda s: pj.fetch_repo(str(self.r.dir), s), ref, list(groups))

    def test_exact_set_update_delete_and_prune(self):
        sha1 = self.r.commit()
        out = self.rel()
        self.assertEqual(out['sha'], sha1)
        self.assertEqual(view(self.root), ['alpha', 'beta'])  # dev not selected
        self.assertTrue((self.root / 'current').is_symlink())
        self.assertEqual(self.rel(), {'unchanged': f'{sha1[:12]}-assistant'})
        # change alpha, add an attachment, delete beta
        self.r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', 'v2\n'))
        self.r.write('skills/assistant/alpha/scripts/run.sh', 'echo hi\n', 0o755)
        self.r.remove('skills/assistant/beta')
        self.r.groups['assistant'] = ['alpha']
        self.r.write_manifest()
        sha2 = self.r.commit()
        self.rel()
        self.assertEqual(view(self.root), ['alpha'])
        v = self.root / 'current' / 'view' / 'alpha'
        self.assertIn('v2', (v / 'SKILL.md').read_text())
        self.assertTrue(os.access(v / 'scripts' / 'run.sh', os.X_OK))
        st = json.loads((self.root / '.project.json').read_text())
        self.assertEqual((st['sha'], st['previous']), (sha2, f'{sha1[:12]}-assistant'))
        # third release prunes the first, keeps the previous
        self.r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', 'v3\n'))
        self.r.commit()
        self.rel()
        self.assertEqual(sorted(os.listdir(self.root / 'releases')),
                         sorted([f'{sha2[:12]}-assistant', json.loads((self.root / '.project.json').read_text())['current']]))

    def test_union_of_groups_and_unknown_group(self):
        self.r.commit()
        self.rel(groups=('assistant', 'dev'))
        self.assertEqual(view(self.root), ['alpha', 'beta', 'gamma'])
        with self.assertRaisesRegex(pj.ProjectError, 'unknown group'):
            self.rel(groups=('ops',))
        self.assertEqual(view(self.root), ['alpha', 'beta', 'gamma'])

    def test_bad_commit_keeps_current_and_is_remembered(self):
        self.r.commit()
        self.rel()
        self.r.write('skills/assistant/alpha/SKILL.md', skill_md('wrong-name'))
        bad = self.r.commit()
        with self.assertRaisesRegex(pj.ProjectError, 'frontmatter name'):
            self.rel()
        self.assertEqual(view(self.root), ['alpha', 'beta'])
        self.assertEqual(self.rel()['still_failing'], f'{bad[:12]}-assistant')
        # listed but missing
        self.r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha'))
        self.r.groups['assistant'] = ['alpha', 'beta', 'nothere']
        self.r.write_manifest()
        self.r.commit()
        with self.assertRaisesRegex(pj.ProjectError, 'missing'):
            self.rel()
        self.assertEqual(view(self.root), ['alpha', 'beta'])

    def test_symlink_in_archive_is_refused(self):
        self.r.commit()
        self.rel()
        os.symlink('/etc/passwd', self.r.dir / 'skills' / 'assistant' / 'alpha' / 'evil')
        self.r.commit()
        with self.assertRaisesRegex(pj.ProjectError, 'symlink'):
            self.rel()
        self.assertFalse((self.root / 'current' / 'view' / 'alpha' / 'evil').exists())

    def test_rollback_pins_until_next_manual_release(self):
        self.r.commit()
        self.rel()
        self.r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', 'v2\n'))
        self.r.commit()
        self.rel()
        pj.rollback(self.root)
        self.assertNotIn('v2', (self.root / 'current' / 'view' / 'alpha' / 'SKILL.md').read_text())
        self.assertTrue(json.loads((self.root / '.project.json').read_text())['pinned_after_rollback'])
        self.rel()  # manual release resumes
        self.assertIn('v2', (self.root / 'current' / 'view' / 'alpha' / 'SKILL.md').read_text())
        self.assertNotIn('pinned_after_rollback', json.loads((self.root / '.project.json').read_text()))

    def test_root_must_exist_and_is_never_replaced(self):
        self.r.commit()
        marker = self.root / 'keep-me'
        marker.write_text('x')
        self.rel()
        self.assertTrue(marker.exists())
        with self.assertRaisesRegex(pj.ProjectError, 'does not exist'):
            pj.release(self.root / 'nope', lambda x: x, lambda s: {}, 'a' * 40, ['assistant'])


class GithubSourceTest(unittest.TestCase):
    """resolve_github / fetch_github against a local fake (smart-HTTP refs + codeload tarball)."""

    def test_resolve_and_fetch(self):
        r = FakeRepo()
        sha = r.commit()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            for p in sorted(x for x in r.dir.rglob('*') if x.is_file() and '.git' not in x.parts):
                t.add(p, arcname=f'skills-{sha}/{p.relative_to(r.dir).as_posix()}')
        tgz = buf.getvalue()
        refs = f'001e# service=git-upload-pack\n0000{"0" * 4}{sha} HEAD\x00caps\n{"0" * 4}{sha} refs/heads/main\n0000'.encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = refs if 'info/refs' in self.path else tgz if self.path.endswith(sha) else None
                self.send_response(200 if body else 404)
                self.end_headers()
                if body:
                    self.wfile.write(body)

        srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{srv.server_address[1]}'
        try:
            self.assertEqual(pj.resolve_github('o/skills', 'main', base), sha)
            self.assertEqual(pj.resolve_github('o/skills', sha, base), sha)
            with self.assertRaisesRegex(pj.ProjectError, 'not found'):
                pj.resolve_github('o/skills', 'nope', base)
            files = pj.fetch_github('o/skills', sha, base)
            self.assertEqual(sorted(pj.select(files, ['assistant', 'dev'])), ['alpha', 'beta', 'gamma'])
        finally:
            srv.shutdown()
            srv.server_close()


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.r = FakeRepo()
        self.r.commit()
        self.root = Path(tempfile.mkdtemp(prefix='skills-root-'))
        pj.release(self.root, lambda x: pj.resolve_repo(str(self.r.dir), x), lambda s: pj.fetch_repo(str(self.r.dir), s),
                   'main', ['assistant', 'dev'])
        self.dest = Path(tempfile.mkdtemp(prefix='skills-dest-'))

    def test_links_only_own_entries_and_refuses_conflicts(self):
        (self.dest / '.system').mkdir()
        (self.dest / 'mine').mkdir()  # someone else's skill
        v = str(self.root / 'current' / 'view')
        out = pj.link(self.dest, pj.view_targets(v))
        self.assertEqual(out['linked'], ['alpha', 'beta', 'gamma'])
        self.assertEqual(os.readlink(self.dest / 'alpha'), os.path.join(v, 'alpha'))
        self.assertTrue((self.dest / 'alpha' / 'SKILL.md').is_file())
        # a name that exists and is not ours -> fail, nothing changed
        os.symlink('/elsewhere', self.dest / 'delta')
        with self.assertRaisesRegex(pj.ProjectError, 'not created by this tool'):
            pj.link(self.dest, {**pj.view_targets(v), 'delta': os.path.join(v, 'alpha')})
        # unlink removes ours only
        pj.link(self.dest, {})
        self.assertEqual(sorted(os.listdir(self.dest)), ['.skills-project.json', '.system', 'delta', 'mine'])

    def test_maintainer_mode_links_into_worktree(self):
        out = pj.link(self.dest, pj.worktree_targets(str(self.r.dir), ['dev']))
        self.assertEqual(out['linked'], ['gamma'])
        self.assertEqual(os.readlink(self.dest / 'gamma'), str((self.r.dir / 'skills' / 'dev' / 'gamma').resolve()))
        with self.assertRaisesRegex(pj.ProjectError, 'unknown group'):
            pj.worktree_targets(str(self.r.dir), ['ops'])


if __name__ == '__main__':
    unittest.main()
