"""g-offload contract: only old, checked-identical files leave the disk, and they come back."""
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_loader('offload', SourceFileLoader('offload', str(HERE / 'g-offload.py')))
o = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(o)

# A stand-in rclone: a remote "r2:bucket/a/b" is the directory $FAKE_REMOTES/bucket/a/b.
FAKE_RCLONE = '''#!/usr/bin/env python3
import filecmp, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if os.environ.get("FAKE_FAIL") == args[0]:
    sys.exit(1)
def where(p):
    return os.path.join(os.environ["FAKE_REMOTES"], p.split(":", 1)[1]) if ":" in p else p
def opt(name):
    return args[args.index(name) + 1] if name in args else None
src, dst = where(args[1]), where(args[2])
listed = opt("--files-from-raw")
names = open(listed).read().split("\\n") if listed else None
if args[0] == "copy":
    include = opt("--include")
    for dirpath, _, files in os.walk(src):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), src)
            if names is not None and rel not in names:
                continue
            if include and not __import__("fnmatch").fnmatch(rel, include):
                continue
            os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), os.path.join(dst, rel))
            if rel == os.environ.get("FAKE_CORRUPT"):
                open(os.path.join(dst, rel), "a").write("bitrot")
elif args[0] == "check":
    bad = False
    with open(opt("--combined"), "w") as out:
        for rel in [n for n in names if n]:
            there = os.path.join(dst, rel)
            if not os.path.exists(there):
                out.write("- " + rel + "\\n"); bad = True
            elif filecmp.cmp(os.path.join(src, rel), there, shallow=False):
                out.write("= " + rel + "\\n")
            else:
                out.write("* " + rel + "\\n"); bad = True
    sys.exit(1 if bad else 0)
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class OffloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.instance = self.tmp / 'gp-core'
        self.instance.mkdir()
        (self.tmp / 'g-env/bin').mkdir(parents=True)
        executable(self.tmp / 'g-env/bin/g-instance', f'#!/bin/sh\necho "{self.instance}"\n')
        (self.tmp / 'bin').mkdir()
        executable(self.tmp / 'bin/rclone', FAKE_RCLONE)
        self.root = self.tmp / 'sessions'
        old = time.time() - 40 * 86400
        for rel in ('2026/09/01/a.jsonl', '2026/09/02/b.jsonl', '2026/09/02/note.txt', '.hidden/c.jsonl', '2026/09/03/.d.jsonl'):
            self.file(rel, 'x' * 10, old)
        self.file('2026/10/24/new.jsonl', 'fresh', time.time())
        (self.root / '2026/09/link.jsonl').symlink_to(self.root / '2026/09/01/a.jsonl')
        self.rule = {'name': 'sessions', 'root': str(self.root), 'glob': '*.jsonl', 'older_than_days': 30,
                     'remote': 'r2:bucket/offload/h/sessions', 'enabled': True}
        self.config = {'rclone_config': str(self.tmp / 'rclone.conf'), 'state': str(self.tmp / 'state'), 'rules': [self.rule]}
        self.write_config()
        env = {'PATH': f"{self.tmp / 'bin'}:{os.environ['PATH']}", 'FAKE_LOG': str(self.tmp / 'rclone.log'),
               'FAKE_REMOTES': str(self.tmp / 'remotes'), 'FAKE_FAIL': '', 'FAKE_CORRUPT': ''}
        for patcher in (patch.object(o, 'G_ENV', str(self.tmp / 'g-env')), patch.dict(os.environ, env)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def file(self, rel, text, when):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (when, when))

    def write_config(self):
        (self.instance / 'offload.json').write_text(json.dumps(self.config))

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = o.main(list(args))
        return code, out.getvalue()

    def remote(self, rel=''):
        return self.tmp / 'remotes/bucket/offload/h/sessions' / rel

    def calls(self):
        log = self.tmp / 'rclone.log'
        return log.read_text().splitlines() if log.exists() else []

    def test_dry_run_reports_and_touches_nothing(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('sessions: 2 file(s)', out)
        self.assertIn('dry run: nothing moved', out)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.root / '2026/09/01/a.jsonl').exists())

    def test_apply_moves_only_old_matching_regular_files_and_indexes_them(self):
        code, out = self.run_main('--apply')
        self.assertEqual(code, 0)
        self.assertFalse((self.root / '2026/09/01/a.jsonl').exists())
        self.assertFalse((self.root / '2026/09/02/b.jsonl').exists())
        self.assertEqual(self.remote('2026/09/01/a.jsonl').read_text(), 'x' * 10)
        for kept in ('2026/10/24/new.jsonl', '2026/09/02/note.txt', '.hidden/c.jsonl', '2026/09/03/.d.jsonl'):
            self.assertTrue((self.root / kept).exists(), kept)
        self.assertTrue((self.root / '2026/09/link.jsonl').is_symlink())
        index = [json.loads(line) for line in (self.tmp / 'state/sessions.jsonl').read_text().splitlines()]
        self.assertEqual([row['path'] for row in index], ['2026/09/01/a.jsonl', '2026/09/02/b.jsonl'])
        self.assertEqual(index[0]['remote'], 'r2:bucket/offload/h/sessions/2026/09/01/a.jsonl')
        self.assertTrue(all('--s3-no-check-bucket' in call for call in self.calls()))
        self.assertIn('freed 0.0 MB', out)

    def test_disabled_rule_only_reports(self):
        self.rule['enabled'] = False
        self.write_config()
        code, out = self.run_main('--apply')
        self.assertIn('(rule disabled)', out)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.root / '2026/09/01/a.jsonl').exists())

    def test_a_copy_that_does_not_check_identical_stays_on_disk(self):
        os.environ['FAKE_CORRUPT'] = '2026/09/02/b.jsonl'
        code, out = self.run_main('--apply')
        self.assertEqual(code, 0)
        self.assertFalse((self.root / '2026/09/01/a.jsonl').exists())
        self.assertTrue((self.root / '2026/09/02/b.jsonl').exists())
        self.assertIn('1 file(s) did not check identical', out)

    def test_failed_copy_deletes_nothing(self):
        os.environ['FAKE_FAIL'] = 'copy'
        with self.assertRaisesRegex(RuntimeError, 'copy failed, nothing deleted'):
            self.run_main('--apply')
        self.assertTrue((self.root / '2026/09/01/a.jsonl').exists())
        self.assertFalse((self.tmp / 'state/sessions.jsonl').exists())

    def test_failed_check_deletes_nothing(self):
        os.environ['FAKE_FAIL'] = 'check'
        with self.assertRaisesRegex(RuntimeError, 'check failed, nothing deleted'):
            self.run_main('--apply')
        self.assertTrue((self.root / '2026/09/01/a.jsonl').exists())

    def test_restore_brings_files_back(self):
        self.run_main('--apply')
        code, out = self.run_main('--restore', 'sessions', '--match', '2026/09/01/*')
        self.assertEqual(code, 0)
        self.assertEqual((self.root / '2026/09/01/a.jsonl').read_text(), 'x' * 10)
        self.assertFalse((self.root / '2026/09/02/b.jsonl').exists())

    def test_unknown_rule_is_an_error(self):
        with self.assertRaises(KeyError):
            self.run_main('--rule', 'nope')


if __name__ == '__main__':
    unittest.main()
