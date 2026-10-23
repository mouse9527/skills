"""scripts/validate.py negatives (each must fail) and scripts/pii_scan.py fail-closed behaviour."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PHONE, ROOT, TERM, FakeRepo, git, skill_md

sys.path.insert(0, str(ROOT / 'scripts'))
import validate as va  # noqa: E402


def errors(r):
    va.validate(r.dir, write_readme=True)  # index regenerated first: each case tests one rule
    return va.validate(r.dir)[0]


class ValidateTest(unittest.TestCase):
    def test_this_repository_is_valid(self):
        errs, summary = va.validate(ROOT)
        self.assertEqual(errs, [])
        self.assertGreater(summary['skills'], 0)

    def test_fixture_is_valid(self):
        self.assertEqual(errors(FakeRepo()), [])

    def assertFails(self, mutate, pattern):
        r = FakeRepo()
        mutate(r)
        errs = errors(r)
        self.assertTrue(any(pattern in e for e in errs), errs)

    def test_name_must_equal_directory(self):
        self.assertFails(lambda r: r.write('skills/assistant/alpha/SKILL.md', skill_md('Alpha')), 'must equal the directory')

    def test_duplicate_id_across_groups(self):
        self.assertFails(lambda r: r.write('skills/dev/alpha/SKILL.md', skill_md('alpha')), 'already used')

    def test_unlisted_skill_needs_draft(self):
        self.assertFails(lambda r: r.write('skills/dev/delta/SKILL.md', skill_md('delta')), 'not in .claude-plugin')
        r = FakeRepo()
        r.write('skills/dev/delta/SKILL.md', skill_md('delta', extra='metadata:\n  status: draft\n'))
        self.assertEqual(errors(r), [])

    def test_listed_draft_and_missing_and_version(self):
        def listed_draft(r):
            r.write('skills/dev/gamma/SKILL.md', skill_md('gamma', extra='metadata:\n  status: draft\n'))
        self.assertFails(listed_draft, 'is a draft')

        def missing(r):
            r.groups['dev'].append('ghost')
            r.write_manifest()
        self.assertFails(missing, 'does not exist')

        def version(r):
            p = r.dir / '.claude-plugin/marketplace.json'
            mp = json.loads(p.read_text())
            mp['plugins'][0]['version'] = '1.0.0'
            p.write_text(json.dumps(mp))
        self.assertFails(version, 'no "version"')

    def test_empty_group_and_no_skills(self):
        self.assertFails(lambda r: (r.dir / 'skills' / 'ops').mkdir(), 'empty group')

        def none(r):
            for g in r.groups:
                r.remove(f'skills/{g}')
            r.write_manifest({})
        self.assertFails(none, 'no skills found')

    def test_symlink_refused(self):
        self.assertFails(lambda r: os.symlink('../beta', r.dir / 'skills' / 'assistant' / 'alpha' / 'link'), 'symlink')

    def test_personal_data_in_attachment(self):
        self.assertFails(lambda r: r.write('skills/dev/gamma/notes.md', f'call {PHONE}\n'), 'phone number')

    def test_device_security_and_whereabouts(self):
        # lead review 2026-10-24: facts about the user's device are personal data too
        for text, kind in (('手机没有' + '锁屏密码', 'device security statement'),
                           ('免密' + '登录已开启', 'device security statement'),
                           ('phone has no ' + 'passcode', 'device security statement'),
                           ('手机放' + '在家里', 'device whereabouts')):
            self.assertFails(lambda r: r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', text + '\n')), kind)
        # rules about what to do stay allowed
        r = FakeRepo()
        r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', '如果停在锁屏：需要密码就停下告诉用户。不索要、不输入密码。\n'))
        self.assertEqual(errors(r), [])

    def test_binary_refused(self):
        def binary(r):
            (r.dir / 'skills' / 'dev' / 'gamma' / 'x.bin').write_bytes(b'\xff\xfe\x00')
        self.assertFails(binary, 'binary file')

    def test_platform_words_only_in_neutral_groups(self):
        self.assertFails(lambda r: r.write('skills/assistant/alpha/SKILL.md', skill_md('alpha', 'Claude 会…\n')),
                         'client-neutral')
        r = FakeRepo()
        r.write('skills/dev/gamma/SKILL.md', skill_md('gamma', 'Claude / Codex / Pi\n'))
        self.assertEqual(errors(r), [])

    def test_prefixed_tool_names(self):
        for name in ('mcp__litellm__phone-mobile_take_screenshot', 'mcp_phone-mobile_take_screenshot'):
            self.assertFails(lambda r: r.write('skills/dev/gamma/SKILL.md', skill_md('gamma', f'`{name}`\n')),
                             'client-prefixed')

    def test_readme_out_of_date(self):
        r = FakeRepo()
        va.validate(r.dir, write_readme=True)
        r.write('skills/dev/gamma/SKILL.md', skill_md('gamma', desc='改过的说明'))
        self.assertTrue(any('out of date' in e for e in va.validate(r.dir)[0]))


class PiiScanTest(unittest.TestCase):
    def scan(self, r, terms, rng='HEAD'):
        p = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'pii_scan.py'), '--terms', terms, '--range', rng],
                           cwd=r.dir, capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def terms(self, text='# fictional\n' + TERM + '\n', mode=0o600):
        p = Path(tempfile.mkdtemp()) / 'terms.txt'
        p.write_text(text, encoding='utf-8')
        p.chmod(mode)
        return str(p)

    def test_fail_closed_on_term_list(self):
        r = FakeRepo()
        r.commit()
        for terms, why in ((str(Path(tempfile.mkdtemp()) / 'missing'), 'does not exist'),
                           (self.terms(mode=0o644), 'too open'), (self.terms('# only\n'), 'empty')):
            code, out = self.scan(r, terms)
            self.assertEqual(code, 2)
            self.assertIn(why, out['error'])

    def test_clean_and_findings_never_echo_text(self):
        r = FakeRepo()
        r.commit()
        code, out = self.scan(r, self.terms())
        self.assertEqual((code, out['findings']), (0, []))
        r.write('skills/dev/gamma/SKILL.md', skill_md('gamma', f'在{TERM}附近\n'))
        r.commit(f'msg {PHONE}')
        code, out = self.scan(r, self.terms(), 'HEAD~1..HEAD')
        self.assertEqual(code, 1)
        joined = json.dumps(out, ensure_ascii=False)
        self.assertIn('private term', joined)
        self.assertIn('message:1: phone number', joined)
        self.assertNotIn(TERM, joined)
        self.assertNotIn(PHONE, joined)

    def test_history_is_scanned_even_if_removed_later(self):
        r = FakeRepo()
        base = r.commit()
        r.write('skills/dev/gamma/old.md', f'{PHONE}\n')
        r.commit()
        r.remove('skills/dev/gamma/old.md')
        r.commit()
        code, out = self.scan(r, self.terms(), f'{base}..HEAD')
        self.assertEqual(code, 1)
        self.assertTrue(any('old.md:1: phone number' in f for f in out['findings']))
        self.assertFalse(any(f.startswith('HEAD:') for f in out['findings']))

    def test_identity_must_be_public_noreply(self):
        r = FakeRepo()
        base = r.commit()
        r.write('x.md', 'x\n')
        git(r.dir, 'add', '-A')
        git(r.dir, 'commit', '-q', '-m', 'x', env={'GIT_AUTHOR_EMAIL': 'someone' + '@host.example'})
        code, out = self.scan(r, self.terms(), f'{base}..HEAD')
        self.assertEqual(code, 1)
        self.assertTrue(any('author e-mail' in f for f in out['findings']))


if __name__ == '__main__':
    unittest.main()
