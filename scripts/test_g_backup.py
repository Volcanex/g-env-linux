"""g-backup contract: the instance's backup.json says what to carry, restic receives it, failures are loud."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_loader('backup', SourceFileLoader('backup', str(HERE / 'g-backup.py')))
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)

FAKE_RESTIC = r'''#!/usr/bin/env python3
"""Stands in for restic: logs every call, keeps what a backup was handed, restores it."""
import json, os, shutil, subprocess, sys
state = os.environ["FAKE_RESTIC_DIR"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"args": args, "repo": os.environ.get("RESTIC_REPOSITORY"),
                         "password": os.environ.get("RESTIC_PASSWORD")}) + "\n")
n = len(os.listdir(os.path.join(state, "snaps"))) if os.path.isdir(os.path.join(state, "snaps")) else 0
os.makedirs(os.path.join(state, "snaps"), exist_ok=True)
cmd = args[0]
if cmd == "version":
    print(os.environ.get("FAKE_RESTIC_VERSION", "restic 0.19.1 compiled with go1.26 on linux/amd64"))
    sys.exit(0)
if cmd == "cat":
    sys.exit(1 if os.environ.get("FAKE_REPO_MISSING") else 0)
if cmd == "backup":
    snap = os.path.join(state, "snaps", "s%03d" % n)
    os.makedirs(snap)
    if "--stdin-from-command" in args:
        command = args[args.index("--") + 1:]
        name = args[args.index("--stdin-filename") + 1]
        out = subprocess.run(command, capture_output=True)
        if out.returncode:
            sys.stderr.write("command failed\n")
            sys.exit(1)
        open(os.path.join(snap, "stdin"), "wb").write(out.stdout)
        open(os.path.join(snap, "name"), "w").write(name)
    elif "--stdin" in args:
        if os.environ.get("FAKE_STDIN_FAIL"):
            sys.stderr.write("repo write failed\n")
            sys.exit(1)
        open(os.path.join(snap, "stdin"), "wb").write(sys.stdin.buffer.read())
        open(os.path.join(snap, "name"), "w").write(args[args.index("--stdin-filename") + 1])
    else:
        src = args[-1]
        if os.environ.get("FAKE_SQLITE_FAIL") and "/sqlite/" in src:
            sys.stderr.write("repo write failed\\n")
            sys.exit(1)
        open(os.path.join(snap, "name"), "w").write(src)
        shutil.copytree(src, os.path.join(snap, "tree"), symlinks=True, ignore_dangling_symlinks=True) if os.path.isdir(src) else None
    print(json.dumps({"message_type": "summary", "snapshot_id": "s%03d" % n, "data_added": 100, "total_files_processed": 1}))
    sys.exit(0)
if cmd == "restore":
    snap = os.path.join(state, "snaps", args[1])
    target = args[args.index("--target") + 1]
    name = open(os.path.join(snap, "name")).read()
    dest = os.path.join(target, name.lstrip("/"))
    if os.path.isdir(os.path.join(snap, "tree")):
        shutil.copytree(os.path.join(snap, "tree"), dest)
        if os.environ.get("FAKE_RESTORE_CORRUPT"):
            for f in os.listdir(dest):
                open(os.path.join(dest, f), "a").write("x")
    sys.exit(0)
if cmd == "stats":
    print(json.dumps({"total_size": 12345}))
if cmd == "snapshots":
    print("[]")
sys.exit(0)
'''


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.instance = self.tmp / 'gp-core'
        self.instance.mkdir()
        (self.instance / '.env').write_text("# passport\nRESTIC_PASSWORD='hunter2'\nexport ENDPOINT=https://r2.example\n")
        (self.tmp / 'g-env/bin').mkdir(parents=True)
        executable(self.tmp / 'g-env/bin/g-instance',
                   f'#!/bin/sh\n[ -n "${{FAKE_INSTANCE+x}}" ] || FAKE_INSTANCE={self.instance}\n'
                   f'[ -z "$FAKE_INSTANCE" ] && exit 0\n[ "$1" = env ] && echo "$FAKE_INSTANCE/.env" || echo "$FAKE_INSTANCE"\n')
        (self.tmp / 'bin').mkdir()
        (self.tmp / 'fake').mkdir()
        executable(self.tmp / 'bin/restic', FAKE_RESTIC)
        executable(self.tmp / 'bin/docker', '#!/bin/sh\nexit 0\n')  # a host's real containers are not the test's business
        self.data = self.tmp / 'data'
        (self.data / '.pulse').mkdir(parents=True)
        (self.data / '.pulse/page.md').write_text('page')
        (self.data / 'node_modules').mkdir()
        (self.data / 'node_modules/live.db').write_bytes(b'SQLite format 3\x00' + b'0' * 600)
        (self.data / 'not-a-db.db').write_bytes(b'plain text ' * 100)
        self.db = self.data / 'app.db'
        with sqlite3.connect(self.db) as conn:
            conn.execute('create table t (x)')
            conn.execute("insert into t values ('kept')")
        conn.close()
        self.config = {
            'root': str(self.tmp / 'out'),
            'restic': {'host': 'h', 'repository': 's3:${ENDPOINT}/bucket/repo',
                       'env': {'RESTIC_PASSWORD': '${RESTIC_PASSWORD}'},
                       'keep': {'daily': 30, 'weekly': 8, 'monthly': 12}},
            'paths': [str(self.data)],
            'exclude': {'names': ['node_modules'], 'paths': [str(self.tmp / 'skipped')]},
            'sqlite': {'roots': [str(self.data)]},
            'services': {'app': [str(self.data)]},
        }
        self.write_config()
        envs = {'PATH': f"{self.tmp / 'bin'}:{os.environ['PATH']}", 'FAKE_RESTIC_DIR': str(self.tmp / 'fake')}
        facts = [('os.txt', ['echo', 'os']), ('crontabs.txt', ['echo', 'none'])]
        for patcher in (patch.object(b, 'G_ENV', str(self.tmp / 'g-env')), patch.object(b, 'FACTS', facts),
                        patch.dict(os.environ, envs)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self):
        (self.instance / 'backup.json').write_text(json.dumps(self.config))

    def calls(self):
        path = self.tmp / 'fake/calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def backups(self):
        return [c['args'] for c in self.calls() if c['args'][0] == 'backup']

    def stdin_snapshots(self):
        out = {}
        for snap in (self.tmp / 'fake/snaps').iterdir():
            if (snap / 'stdin').exists():
                out[(snap / 'name').read_text()] = (snap / 'stdin').read_bytes()
        return out

    def test_files_snapshot_carries_the_instance_list_and_the_passport_reaches_restic_only_as_env(self):
        b.main([])
        files = [a for a in self.backups() if 'files' in a][0]
        self.assertEqual(files[-1], str(self.data))
        for flag in ('--one-file-system', '--exclude-caches', '--exclude-file', '--host', 'h'):
            self.assertIn(flag, files)
        self.assertEqual(files[files.index('--host') + 1], 'h')
        patterns = Path(files[files.index('--exclude-file') + 1]).read_text().splitlines()
        self.assertIn('node_modules', patterns)
        self.assertIn(str(self.tmp / 'skipped'), patterns)
        self.assertIn(str(self.tmp / 'out'), patterns)
        first = self.calls()[0]
        self.assertEqual((first['repo'], first['password']), ('s3:https://r2.example/bucket/repo', 'hunter2'))
        self.assertFalse(any('hunter2' in ' '.join(c['args']) for c in self.calls()))

    def staged(self, service):
        for snap in (self.tmp / 'fake/snaps').iterdir():
            if (snap / 'name').read_text().endswith(f'/sqlite/{service}'):
                return snap / 'tree'

    def test_sqlite_is_found_by_header_copied_consistent_per_service_and_left_out_of_files(self):
        b.main([])
        tree = self.staged('app')
        self.assertEqual(sorted(p.name for p in tree.rglob('*') if p.is_file()), ['app.db'])
        with sqlite3.connect(tree / str(self.db).lstrip('/')) as conn:
            self.assertEqual(conn.execute('select x from t').fetchall(), [('kept',)])
        conn.close()
        self.assertFalse((self.tmp / 'out/sqlite/app').exists())
        files = [a for a in self.backups() if 'files' in a][0]
        patterns = Path(files[files.index('--exclude-file') + 1]).read_text().splitlines()
        self.assertIn(str(self.db), patterns)
        self.assertIn(str(self.db) + '-wal', patterns)
        self.assertNotIn(str(self.data / 'not-a-db.db'), patterns)
        self.assertTrue(any('svc:app' in a and 'sqlite' in a for a in self.backups()))

    def test_a_db_that_cannot_be_backed_up_stays_in_files_and_fails_the_run(self):
        with patch.dict(os.environ, {'FAKE_SQLITE_FAIL': '1'}), self.assertRaisesRegex(RuntimeError, 'failed'):
            b.main([])
        files = [a for a in self.backups() if 'files' in a][0]
        self.assertNotIn(str(self.db), Path(files[files.index('--exclude-file') + 1]).read_text().splitlines())
        status = json.loads((self.tmp / 'out/status.json').read_text())
        self.assertFalse(status['ok'])
        self.assertIsNone(status['last_success'])

    def test_clean_run_writes_status_retention_and_a_canary(self):
        b.main([])
        status = json.loads((self.tmp / 'out/status.json').read_text())
        self.assertTrue(status['ok'])
        self.assertEqual(status['repo']['repo_bytes'], 12345)
        self.assertEqual(status['last_success'], status['finished'])
        self.assertEqual({s['kind'] for s in status['snapshots']}, {'facts', 'sqlite', 'files'})
        forget = [c['args'] for c in self.calls() if c['args'][0] == 'forget'][0]
        self.assertEqual(forget[forget.index('--keep-daily') + 1:forget.index('--keep-daily') + 2], ['30'])
        self.assertIn('--prune', forget)
        self.assertTrue(any(c['args'][0] == 'restore' for c in self.calls()))
        self.assertTrue(any(c['args'][0] == 'check' for c in self.calls()))

    def test_status_exits_non_zero_when_the_last_run_failed_and_zero_after_a_clean_one(self):
        with patch.dict(os.environ, {'FAKE_SQLITE_FAIL': '1'}), self.assertRaises(RuntimeError):
            b.main([])
        with self.assertRaises(SystemExit) as failed:
            b.main(['status'])
        self.assertEqual(failed.exception.code, 1)
        b.main([])
        b.main(['status'])

    def test_canary_catches_a_restore_that_differs(self):
        with patch.dict(os.environ, {'FAKE_RESTORE_CORRUPT': '1'}), self.assertRaisesRegex(RuntimeError, 'canary'):
            b.main([])

    def test_postgres_dumps_every_database_but_the_excluded_and_a_failed_one_does_not_stop_the_rest(self):
        executable(self.tmp / 'bin/runuser', '#!/bin/sh\nshift 3\nexec "$@"\n')
        executable(self.tmp / 'bin/psql', '#!/bin/sh\nprintf "app\\napp_test_x\\nbroken\\n"\n')
        executable(self.tmp / 'bin/pg_dumpall', '#!/bin/sh\necho "-- roles"\n')
        executable(self.tmp / 'bin/pg_dump', '#!/bin/sh\n[ "$2" = broken ] || [ "$3" = broken ] && exit 3\necho "-- dump of $2$3"\n')
        self.config['postgres'] = [{'service': 'app', 'exclude': '_test_'}]
        self.write_config()
        with self.assertRaisesRegex(RuntimeError, 'broken'):
            b.main([])
        dumps = self.stdin_snapshots()
        self.assertIn('/dumps/postgres/host/app.sql', dumps)
        self.assertIn('/dumps/postgres/host/_globals.sql', dumps)
        self.assertNotIn('/dumps/postgres/host/app_test_x.sql', dumps)
        self.assertNotIn('/dumps/postgres/host/broken.sql', dumps)
        self.assertTrue(any('db:app' in a for a in self.backups()))
        self.assertTrue(any(a for a in self.backups() if 'files' in a))

    def test_a_database_container_nothing_dumps_fails_the_run(self):
        executable(self.tmp / 'bin/docker', '#!/bin/sh\n[ "$1" = ps ] && echo "cache redis:7"\nexit 0\n')
        with self.assertRaisesRegex(RuntimeError, 'cache'):
            b.main([])

    def test_a_configured_mariadb_container_is_dumped_without_its_password_on_the_command_line(self):
        executable(self.tmp / 'bin/docker', '#!/bin/sh\n[ "$1" = ps ] && echo "wp-db mariadb:11"\n[ "$1" = exec ] && echo "-- sql"\nexit 0\n')
        self.config['docker'] = [{'container': 'wp-db', 'kind': 'mariadb', 'service': 'wp'}]
        self.write_config()
        b.main([])
        self.assertIn('/dumps/mysql/wp-db/all-databases.sql', self.stdin_snapshots())
        self.assertFalse(any('MARIADB_ROOT_PASSWORD=' in ' '.join(c['args']) for c in self.calls()))

    def test_a_restic_older_than_0_17_is_refused_before_anything_is_read(self):
        with patch.dict(os.environ, {'FAKE_RESTIC_VERSION': 'restic 0.16.4 compiled with go1.22'}), \
                self.assertRaisesRegex(RuntimeError, '0.17'):
            b.main([])
        self.assertEqual(self.backups(), [])

    def test_unreachable_repo_fails_before_anything_is_read(self):
        with patch.dict(os.environ, {'FAKE_REPO_MISSING': '1'}), self.assertRaisesRegex(RuntimeError, 'cannot open'):
            b.main([])
        self.assertEqual(self.backups(), [])

    def test_a_passport_variable_the_config_names_but_is_missing_fails_clearly(self):
        self.config['restic']['env']['AWS_SECRET_ACCESS_KEY'] = '${NOT_IN_PASSPORT}'
        self.write_config()
        with self.assertRaisesRegex(KeyError, 'NOT_IN_PASSPORT'):
            b.main([])

    def test_old_tarball_config_is_refused(self):
        (self.instance / 'backup.json').write_text(json.dumps({'root': 'x', 'dirs': [], 'remote': 'r2:x'}))
        with self.assertRaisesRegex(KeyError, 'restic'):
            b.main([])

    def test_no_instance_fails(self):
        with patch.dict(os.environ, {'FAKE_INSTANCE': ''}), self.assertRaisesRegex(FileNotFoundError, 'no instance'):
            b.main([])

    def test_find_sqlite_skips_excluded_names_and_paths(self):
        found = b.find_sqlite([str(self.data)], ['node_modules'], [])
        self.assertEqual(found, [str(self.db)])
        self.assertEqual(b.find_sqlite([str(self.data)], ['node_modules'], [str(self.data)]), [])

    def test_backup_sqlite_copies_an_idle_wal_database_without_leaving_side_files(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('pragma journal_mode=wal')
        conn.close()
        for suffix in ('-wal', '-shm'):
            (self.data / f'app.db{suffix}').unlink(missing_ok=True)
        b.backup_sqlite(self.db, self.tmp / 'copy.db')
        self.assertEqual(sorted(p.name for p in self.data.glob('app.db*')), ['app.db'])
        with sqlite3.connect(self.tmp / 'copy.db') as conn:
            self.assertEqual(conn.execute('select x from t').fetchall(), [('kept',)])
        conn.close()

    def test_backup_sqlite_includes_what_a_live_writer_has_only_in_the_wal(self):
        writer = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute('pragma journal_mode=wal')
        writer.execute('pragma wal_autocheckpoint=0')
        writer.execute("insert into t values ('in the wal only')")
        self.assertTrue((self.data / 'app.db-wal').stat().st_size > 0)
        b.backup_sqlite(self.db, self.tmp / 'copy.db')
        with sqlite3.connect(self.tmp / 'copy.db') as conn:
            self.assertEqual(conn.execute('select x from t order by rowid').fetchall(), [('kept',), ('in the wal only',)])
        conn.close()

    def test_backup_sqlite_gives_up_on_a_db_another_process_holds_locked(self):
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute('begin exclusive')
        self.addCleanup(holder.close)
        started = time.monotonic()
        with self.assertRaises(b.SqliteBusy):
            b.backup_sqlite(self.db, self.tmp / 'locked-copy.db', busy_seconds=0.5)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_locked_db_is_a_warning_and_stays_in_the_file_snapshot(self):
        with patch.object(b, 'backup_sqlite', side_effect=b.SqliteBusy('locked by another process for 6s')):
            b.main([])
        status = json.loads((self.tmp / 'out/status.json').read_text())
        self.assertTrue(status['ok'])
        self.assertIn('raw file', status['warnings'][0])
        files = [a for a in self.backups() if 'files' in a][0]
        self.assertNotIn(str(self.db), Path(files[files.index('--exclude-file') + 1]).read_text().splitlines())

    def test_service_of_picks_the_longest_matching_path(self):
        run = b.Run({'root': str(self.tmp), 'restic': {}, 'services': {'a': ['/x'], 'b': ['/x/y']}}, {})
        self.assertEqual((run.service_of('/x/y/z.db'), run.service_of('/x/z.db'), run.service_of('/q')), ('b', 'a', None))


if __name__ == '__main__':
    unittest.main()
