"""g-backup contract: the list comes from the instance's backup.json, and the tarball holds it."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_loader('backup', SourceFileLoader('backup', str(HERE / 'g-backup.py')))
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.instance = self.tmp / 'gp-core'
        self.instance.mkdir()
        (self.tmp / 'g-env/bin').mkdir(parents=True)
        executable(self.tmp / 'g-env/bin/g-instance', f'#!/bin/sh\necho "${{FAKE_INSTANCE-{self.instance}}}"\n')
        (self.tmp / 'bin').mkdir()
        executable(self.tmp / 'bin/rclone', f'#!/bin/sh\necho "$@" >> {self.tmp}/rclone.args\n')
        brain = self.tmp / 'brain'
        (brain / '.pulse').mkdir(parents=True)
        (brain / '.pulse/page.md').write_text('page')
        db = self.tmp / 'live.db'
        with sqlite3.connect(db) as conn:
            conn.execute('create table t (x)')
            conn.execute("insert into t values ('kept')")
        conn.close()
        self.config = {
            'root': str(self.tmp / 'out'), 'rclone_config': str(self.tmp / 'rclone.conf'), 'remote': 'r2:bucket/prefix/host',
            'dirs': [{'name': 'brain', 'path': str(brain)}, {'name': 'wages', 'path': str(self.tmp / 'absent'), 'optional': True}],
            'sqlite': [{'name': 'app-live.db', 'path': str(db)}],
        }
        self.write_config()
        for patcher in (patch.object(b, 'G_ENV', str(self.tmp / 'g-env')),
                        patch.dict(os.environ, {'PATH': f"{self.tmp / 'bin'}:{os.environ['PATH']}"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self):
        (self.instance / 'backup.json').write_text(json.dumps(self.config))

    def test_tarball_holds_the_instance_list(self):
        b.main()
        tarballs = list((self.tmp / 'out').glob('g-backup-*.tar.gz'))
        self.assertEqual(len(tarballs), 1)
        with tarfile.open(tarballs[0]) as tf:
            names = tf.getnames()
            day = names[0].split('/')[0]
            self.assertIn(f'{day}/brain/.pulse/page.md', names)
            self.assertIn(f'{day}/db/app-live.db', names)
            self.assertFalse(any('/wages' in n for n in names))
            tf.extract(f'{day}/db/app-live.db', self.tmp / 'restore', filter='data')
        with sqlite3.connect(self.tmp / 'restore' / day / 'db/app-live.db') as conn:
            self.assertEqual(conn.execute('select x from t').fetchall(), [('kept',)])
        conn.close()
        args = (self.tmp / 'rclone.args').read_text().splitlines()
        self.assertEqual(args[0], f"copy {tarballs[0]} r2:bucket/prefix/host/ --config {self.tmp / 'rclone.conf'} --s3-no-check-bucket")
        self.assertTrue(args[1].startswith('delete r2:bucket/prefix/host --min-age 30d'))
        self.assertFalse((self.tmp / 'out/staging' / day).exists())

    def test_latest_ships_only_the_newest_and_keeps_the_newest_offsite(self):
        dumps = self.tmp / 'dumps'
        dumps.mkdir()
        for day in ('20261015', '20261016', '20261017'):
            (dumps / f'app-{day}.sql.gz').write_text(day)
        listing = '\\n'.join(f'app-2026101{d}.sql.gz' for d in (4, 7, 5, 6))
        executable(self.tmp / 'bin/rclone',
                   f'#!/bin/sh\necho "$@" >> {self.tmp}/rclone.args\n[ "$1" = lsf ] && printf "{listing}\\n"\nexit 0\n')
        self.config['latest'] = [{'name': 'app', 'glob': str(dumps / 'app-*.sql.gz'), 'keep': 2}]
        self.write_config()
        b.main()
        args = (self.tmp / 'rclone.args').read_text().splitlines()
        flags = f"--config {self.tmp / 'rclone.conf'} --s3-no-check-bucket"
        shipped = [a for a in args if a.startswith('copy') and '/app/' in a]
        self.assertEqual(shipped, [f"copy {dumps / 'app-20261017.sql.gz'} r2:bucket/prefix/host/app/ {flags}"])
        deleted = [a.split()[1] for a in args if a.startswith('deletefile')]
        self.assertEqual(deleted, ['r2:bucket/prefix/host/app/app-20261014.sql.gz', 'r2:bucket/prefix/host/app/app-20261015.sql.gz'])
        pruned = [a for a in args if a.startswith('delete ')]
        self.assertEqual(pruned, [f"delete r2:bucket/prefix/host --min-age 30d {flags} --exclude /app/**"])

    def test_latest_with_no_match_fails(self):
        self.config['latest'] = [{'name': 'app', 'glob': str(self.tmp / 'none-*.gz'), 'keep': 7}]
        self.write_config()
        with self.assertRaisesRegex(FileNotFoundError, 'nothing matches'):
            b.main()

    def test_missing_required_dir_fails_before_shipping(self):
        self.config['dirs'][0]['path'] = str(self.tmp / 'gone')
        self.write_config()
        with self.assertRaisesRegex(FileNotFoundError, 'missing dir'):
            b.main()
        self.assertFalse((self.tmp / 'rclone.args').exists())

    def test_no_instance_fails(self):
        with patch.dict(os.environ, {'FAKE_INSTANCE': ''}), self.assertRaisesRegex(FileNotFoundError, 'no instance'):
            b.main()

    def test_local_keep_prunes_older_tarballs_but_never_the_newest(self):
        out = self.tmp / 'out'
        out.mkdir()
        for day in ('2026-10-01', '2026-10-02', '2026-10-03'):
            (out / f'g-backup-{day}.tar.gz').write_text('old')
        for keep, left in ((2, 2), (0, 1)):
            self.config['local_keep'] = keep
            self.write_config()
            with patch.object(b, 'run', lambda cmd: None):
                b.main()
            self.assertEqual(len(list(out.glob('g-backup-*.tar.gz'))), left)
        self.assertEqual(len(list(out.glob(f'g-backup-{b.date.today().isoformat()}.tar.gz'))), 1)

    def git_repo(self, name, dirty):
        repo = self.tmp / name
        repo.mkdir()
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
        for args in (['init', '-q', '-b', 'main'], ['commit', '-q', '--allow-empty', '-m', 'unpushed work']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=env)
        (repo / 'f.txt').write_text('one\n')
        for args in (['add', 'f.txt'], ['commit', '-q', '-m', 'add f']):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=env)
        if dirty:
            (repo / 'f.txt').write_text('one\ntwo\n')
        return repo

    def test_repos_carry_every_commit_and_uncommitted_changes(self):
        self.config['repos'] = [{'name': 'app', 'path': str(self.git_repo('app', dirty=True))},
                                {'name': 'clean', 'path': str(self.git_repo('clean', dirty=False))},
                                {'name': 'moved', 'path': str(self.tmp / 'moved'), 'optional': True}]
        self.write_config()
        out = io.StringIO()
        with patch.object(b, 'run', lambda cmd: None), redirect_stdout(out):
            b.main()
        self.assertIn('skipped optional, not there: wages, moved', out.getvalue())
        with tarfile.open(next((self.tmp / 'out').glob('g-backup-*.tar.gz'))) as tf:
            day = tf.getnames()[0].split('/')[0]
            tf.extractall(self.tmp / 'restore', filter='data')
        repos = self.tmp / 'restore' / day / 'repos'
        clone = self.tmp / 'clone'
        subprocess.run(['git', 'clone', '-q', str(repos / 'app.bundle'), str(clone)], check=True, capture_output=True)
        log = subprocess.run(['git', '-C', str(clone), 'log', '--format=%s'],
                             check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(log, ['add', 'f', 'unpushed', 'work'])
        subprocess.run(['git', '-C', str(clone), 'apply', str(repos / 'app.uncommitted.patch')], check=True)
        self.assertEqual((clone / 'f.txt').read_text(), 'one\ntwo\n')
        self.assertTrue((repos / 'clean.bundle').exists())
        self.assertFalse((repos / 'clean.uncommitted.patch').exists())

    def test_a_repo_that_will_not_bundle_fails_the_run_after_the_rest_ships(self):
        # A plain directory inside another checkout: git alone would bundle the parent.
        plain = self.git_repo('parent', dirty=False) / 'plain'
        plain.mkdir()
        self.config['repos'] = [{'name': 'plain', 'path': str(plain)}]
        self.write_config()
        calls = []
        with patch.object(b, 'run', calls.append), redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'could not bundle: plain'):
            b.main()
        self.assertTrue(any(cmd[:2] == ['rclone', 'copy'] for cmd in calls))
        self.assertEqual(len(list((self.tmp / 'out').glob('g-backup-*.tar.gz'))), 1)

    def test_optional_db_absent_is_named_and_a_required_one_still_fails(self):
        self.config['sqlite'].append({'name': 'paused.db', 'path': str(self.tmp / 'gone.db'), 'optional': True})
        self.write_config()
        out = io.StringIO()
        with patch.object(b, 'run', lambda cmd: None), redirect_stdout(out):
            b.main()
        self.assertIn('skipped optional, not there: wages, paused.db', out.getvalue())
        self.config['sqlite'][-1].pop('optional')
        self.write_config()
        with patch.object(b, 'run', lambda cmd: None), self.assertRaisesRegex(FileNotFoundError, 'missing DB source'):
            b.main()


if __name__ == '__main__':
    unittest.main()
