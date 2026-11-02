#!/usr/bin/env python3
"""Nightly encrypted offsite backup of the whole host into a restic repository.

The whole host is the unit: everything on the root filesystem except what the
instance names as regenerable, so a new directory is backed up by default rather
than lost by default. What to carry is the instance's: `backup.json` in the
instance (gp-core) that g-env's bin/g-instance names. Runs as root.

One run makes these restic snapshots, all tagged `g-backup`:
  files   `paths` (normally `/`), minus `exclude`, one filesystem (-x) at a time
  mount   every local disk filesystem mounted below (ext4 loop images and the like),
          since -x steps over them; a loop image that is mounted is left out of
          `files`, its mount point stands for it
  sqlite  every SQLite file found under `sqlite.roots` (by its header, not its name),
          one snapshot per service: each DB copied with the online backup API (safe
          against a live writer) to its own path under <root>/sqlite/<service>; the
          raw file is left out of `files`, so each DB has one consistent copy
  postgres, mysql   logical dumps streamed in (the host cluster by runuser, a
          container by docker exec), one snapshot per database; roles in `_globals`
  facts   what rebuilding needs and no file holds: package lists, docker inspect,
          unit states, crontabs, mounts (`<root>/facts`)
Then `forget --prune` to the retention in `restic.keep`, a structural `check`
(plus a read of `restic.check_subset` of the data on `restic.check_day`), a canary
restore of the facts snapshot compared byte for byte, and `<root>/status.json`.

A failed item never stops the others; the run exits non-zero at the end if any
failed, so systemd shows it and the OnFailure pager fires. A DB container that
nothing in `docker` dumps is an error too: its volume would otherwise be copied
raw, mid-write. Needs restic 0.17 or newer (`--stdin-from-command` fails the
snapshot when the dump command does; Ubuntu's apt package is older).

Credentials come from the passport (`g-instance env`) through `${NAME}` in
`restic.repository` and `restic.env`; they only ever reach restic's environment.

Other commands: `status` (freshness of every group, from the repo itself; exits 1 when the
last run failed or none has succeeded in 36 hours), `init`,
`snapshots`, `restore`, and `restic ...` to run restic itself with the right
environment. Restore, for the real thing:
  g-backup.py restore --target /scratch --service NAME      one service, latest
  g-backup.py restore --target /scratch --tag db:opentable   one database
  g-backup.py restore --target /                             everything, latest
Databases come back as .sql under <target>/dumps/ to be loaded by hand
(`createdb x; psql x < file`); they are plain dumps with no CREATE DATABASE.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from string import Template
from urllib.parse import quote

# g-env sits beside this checkout; G_ENV overrides that.
WORKSPACE = Path(__file__).resolve().parents[2]
G_ENV = os.environ.get("G_ENV") or os.environ.get("GENV_DIR") or next(
    (str(p) for p in (WORKSPACE / "g-env", WORKSPACE / "genv") if p.exists()), str(WORKSPACE / "g-env"))

SQLITE_MAGIC = b"SQLite format 3\x00"
SQLITE_BUSY_SECONDS = 6
DB_IMAGES = re.compile(r"(^|/)(postgres|mariadb|mysql|mongo|redis|valkey|timescaledb|clickhouse|influxdb)\b")
LOCAL_FS = {"ext4", "ext3", "xfs", "btrfs", "zfs"}
FACTS = [
    ("os.txt", ["sh", "-c", "cat /etc/os-release; uname -a; restic version; python3 --version; "
                "docker --version; psql --version; nginx -v 2>&1; date -u"]),
    ("dpkg-selections.txt", ["dpkg", "--get-selections"]),
    ("apt-manual.txt", ["apt-mark", "showmanual"]),
    ("snap-list.txt", ["snap", "list"]),
    ("pip-freeze.txt", ["python3", "-m", "pip", "freeze"]),
    ("docker-containers.txt", ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"]),
    ("docker-inspect.json", ["sh", "-c", "docker inspect $(docker ps -aq)"]),
    ("docker-volumes.txt", ["docker", "volume", "ls"]),
    ("docker-images.txt", ["docker", "images", "--digests"]),
    ("systemd-unit-files.txt", ["systemctl", "list-unit-files", "--no-pager"]),
    ("systemd-timers.txt", ["systemctl", "list-timers", "--all", "--no-pager"]),
    ("crontabs.txt", ["sh", "-c", "for u in $(cut -d: -f1 /etc/passwd); do "
                      "c=$(crontab -l -u $u 2>/dev/null) && printf '### %s\\n%s\\n' $u \"$c\"; done"]),
    ("mounts.txt", ["findmnt", "-r"]),
    ("loop-devices.txt", ["losetup", "-l"]),
    ("disk.txt", ["df", "-h"]),
    ("network.txt", ["ip", "-br", "addr"]),
    ("listening.txt", ["ss", "-ltnpH"]),
    ("firewall.txt", ["sh", "-c", "ufw status verbose; iptables-save"]),
    ("postgres-clusters.txt", ["pg_lsclusters"]),
]


def log(msg: str) -> None:
    print(f"[g-backup] {msg}", flush=True)


def instance_path(kind: str) -> Path:
    out = subprocess.run([str(Path(G_ENV) / "bin/g-instance"), kind], capture_output=True, text=True,
                         check=True).stdout.strip()
    if not out:
        raise FileNotFoundError("no instance, so no backup.json to say what to back up")
    return Path(out)


def load_config() -> tuple[dict, dict]:
    """The instance's backup.json and the passport (its .env) that fills in its ${NAME}s."""
    config = json.loads((instance_path("dir") / "backup.json").read_text())
    if "restic" not in config:
        raise KeyError("backup.json has no `restic` section (the old tarball format is gone)")
    passport = {}
    env_file = instance_path("env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                value = m.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                passport[m.group(1)] = value
    return config, passport


def restic_env(config: dict, passport: dict) -> dict:
    merged = {**os.environ, **passport}
    section = config["restic"]

    def fill(text: str) -> str:
        try:
            return Template(text).substitute(merged)
        except KeyError as exc:
            raise KeyError(f"backup.json names ${exc.args[0]} and the passport has no such variable") from None

    env = {**os.environ, "RESTIC_REPOSITORY": fill(section["repository"]), "RESTIC_PROGRESS_FPS": "0.01"}
    for key, value in section.get("env", {}).items():
        env[key] = fill(value)
    return env


def escape_pattern(path: str) -> str:
    return re.sub(r"([\\*?\[])", r"\\\1", path)


def under(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


class SqliteBusy(Exception):
    """The DB stayed locked by another process for the whole copy window."""


def copy_idle_wal_db(src: Path, dst: Path, side: list[Path]) -> bool:
    """A WAL-mode DB with no -wal/-shm is closed and whole, and opening it just to copy it would
    leave a -wal and -shm beside it in the app's directory. Copy the bytes instead, and keep the
    copy only if nothing woke up meanwhile (same size and mtime, still no side files)."""
    with open(src, "rb") as fh:
        head = fh.read(20)
    if head[:16] != SQLITE_MAGIC or head[18:20] != b"\x02\x02" or any(p.exists() for p in side):
        return False
    before = src.stat()
    shutil.copyfile(src, dst)
    after = src.stat()
    if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size) and not any(p.exists() for p in side):
        return True
    dst.unlink()
    return False


def backup_sqlite(src: Path, dst: Path, busy_seconds: float = SQLITE_BUSY_SECONDS) -> None:
    """Snapshot a live DB into dst. The online backup API retries a locked DB for ever, and a
    browser profile holds its DBs locked for good, so it is given busy_seconds and then SqliteBusy.
    Opening a DB that is in use as root can create a root-owned -wal/-shm that locks the app out,
    so any this call created are handed to the DB's owner."""
    if not src.exists():
        raise FileNotFoundError(f"missing DB source: {src}")
    side = [src.with_name(src.name + s) for s in ("-wal", "-shm")]
    if copy_idle_wal_db(src, dst, side):
        return
    had = [p.exists() for p in side]
    owner = src.stat()
    deadline = time.monotonic() + busy_seconds

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise SqliteBusy(f"locked by another process for {busy_seconds:.0f}s")

    dst_conn = sqlite3.connect(dst)
    try:
        # a read-only open of an idle WAL DB with no -shm can be refused; a plain open only reads here
        for query in ("?mode=ro", ""):
            src_conn = None
            try:
                src_conn = sqlite3.connect(f"file:{quote(str(src))}{query}", uri=True)
                src_conn.backup(dst_conn, progress=progress)
                return
            except sqlite3.OperationalError:
                if not query:
                    raise
            finally:
                if src_conn is not None:
                    src_conn.close()
    finally:
        dst_conn.close()
        for path, existed in zip(side, had):
            if not existed and path.exists():
                os.chown(path, owner.st_uid, owner.st_gid)
                os.chmod(path, owner.st_mode & 0o666)


def size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def same_device(path: str, dev: int) -> bool:
    try:
        return os.lstat(path).st_dev == dev
    except OSError:  # a dead FUSE mount answers "transport endpoint is not connected"
        return False


def find_sqlite(roots: list[str], names: list[str], prefixes: list[str]) -> list[str]:
    """SQLite files under roots, by header rather than extension, skipping what `files` skips."""
    found = []
    for root in roots:
        if not os.path.isdir(root) or under(root, prefixes):
            continue
        dev = os.lstat(root).st_dev
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if not any(fnmatch.fnmatch(d, n) for n in names)
                       and not under(os.path.join(dirpath, d), prefixes)
                       and same_device(os.path.join(dirpath, d), dev)]
            for name in files:
                path = os.path.join(dirpath, name)
                if any(fnmatch.fnmatch(name, n) for n in names) or name.endswith(("-wal", "-shm", "-journal")):
                    continue
                try:
                    if os.path.islink(path) or os.path.getsize(path) < 512:
                        continue
                except OSError:
                    continue
                if is_sqlite(path):
                    found.append(path)
    return sorted(set(found))


def local_mounts(prefixes: list[str]) -> list[tuple[str, str]]:
    """(mount point, device) of every separate local disk filesystem, bind mounts left out."""
    out = subprocess.run(["findmnt", "-rn", "-o", "TARGET,FSTYPE,SOURCE"], capture_output=True, text=True,
                         check=True).stdout
    mounts = []
    for line in out.splitlines():
        target, fstype, source = (re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), f)
                                  for f in line.split(" ", 2))
        if fstype in LOCAL_FS and target != "/" and source.startswith("/dev/") and "[" not in source \
                and not under(target, prefixes):
            mounts.append((target, source))
    return mounts


def loop_images() -> dict[str, str]:
    out = subprocess.run(["losetup", "-ln", "-O", "NAME,BACK-FILE"], capture_output=True, text=True).stdout
    return dict(line.split(None, 1) for line in out.splitlines() if len(line.split(None, 1)) == 2)


class Run:
    def __init__(self, config: dict, env: dict):
        self.config, self.env = config, env
        self.host = config["restic"].get("host") or os.uname().nodename
        self.root = Path(config["root"])
        self.snapshots: list[dict] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.services = config.get("services", {})
        self.mounts: list[tuple[str, str]] = []

    def restic(self, *args: str, stdin=None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["restic", *args], env=self.env, stdin=stdin, capture_output=True, text=True,
                              check=check)

    def fail(self, what: str, detail: str = "") -> None:
        msg = f"{what}: {detail.strip()[-600:]}" if detail.strip() else what
        log(f"ERROR {msg}")
        self.errors.append(msg)

    def service_of(self, path: str) -> str | None:
        best = None
        for name, paths in self.services.items():
            for p in paths:
                if under(path, [p]) and (best is None or len(p) > best[0]):
                    best = (len(p), name)
        return best[1] if best else None

    def backup(self, kind: str, name: str, args: list[str], tags: list[str], stdin=None) -> bool:
        """One restic snapshot. Exit 3 is a snapshot with unreadable files: kept, reported as a warning."""
        cmd = ["backup", "--json", "--host", self.host, "--tag", "g-backup", "--tag", kind]
        for tag in tags:
            cmd += ["--tag", tag]
        started = time.time()
        proc = self.restic(*cmd, *args, stdin=stdin, check=False)
        summary, problems = {}, []
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("message_type") == "summary":
                summary = msg
            elif msg.get("message_type") == "error":
                problems.append(f"{msg.get('item', '?')}: {msg.get('error', {}).get('message', '?')}")
        if proc.returncode not in (0, 3) or not summary:
            self.fail(f"{kind} {name}", proc.stderr or proc.stdout[-300:])
            return False
        if proc.returncode == 3:
            note = f"{kind} {name}: {len(problems)} unreadable (snapshot kept); first: {'; '.join(problems[:3])}"
            log(f"WARNING {note}")
            self.warnings.append(note)
        entry = {"kind": kind, "name": name, "id": summary.get("snapshot_id"),
                 "added_bytes": summary.get("data_added_packed") or summary.get("data_added"), "files": summary.get("total_files_processed"),
                 "seconds": round(time.time() - started, 1)}
        self.snapshots.append(entry)
        log(f"{kind} {name}: {(entry['id'] or '')[:8]} +{(entry['added_bytes'] or 0) / 1048576:.1f} MiB in {entry['seconds']}s")
        return True

    def dump(self, kind: str, name: str, filename: str, command: list[str], tags: list[str]) -> bool:
        return self.backup(kind, name, ["--stdin-from-command", "--stdin-filename", filename, "--", *command], tags)

    def preflight(self) -> None:
        found = re.search(r"restic (\d+)\.(\d+)", self.restic("version", check=False).stdout)
        if not found or (int(found[1]), int(found[2])) < (0, 17):
            raise RuntimeError("restic 0.17 or newer is required, and there is none on PATH (Ubuntu's apt "
                               "package is older: use the static binary from github.com/restic/restic/releases)")
        proc = self.restic("cat", "config", check=False)
        if proc.returncode:
            raise RuntimeError(f"cannot open the restic repository (not initialised, wrong password or "
                               f"unreachable): {proc.stderr.strip()[-300:]}")
        # We hold the local run lock, so a lock left by an earlier run that was killed is stale.
        # Without --remove-all this only drops locks restic judges stale.
        self.restic("unlock", check=False)

    def write_facts(self) -> Path:
        facts = self.root / "facts"
        facts.mkdir(parents=True, exist_ok=True)
        os.chmod(facts, 0o700)
        for name, cmd in FACTS:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                text = proc.stdout + (f"\n[exit {proc.returncode}] {proc.stderr}" if proc.returncode else "")
            except (OSError, subprocess.SubprocessError) as exc:
                text = f"[not collected: {exc}]"
            (facts / name).write_text(text)
        return facts

    def canary(self, facts: Path, snapshot_id: str) -> None:
        """Restore the facts snapshot and compare it with what was just written: proves the
        password, the repo and a restore all work, every night, for a few kilobytes."""
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            self.restic("restore", snapshot_id, "--target", tmp)
            restored = Path(tmp) / str(facts).lstrip("/")
            for src in sorted(facts.iterdir()):
                want = hashlib.sha256(src.read_bytes()).hexdigest()
                got = hashlib.sha256((restored / src.name).read_bytes()).hexdigest()
                if want != got:
                    raise RuntimeError(f"canary restore differs for {src.name}")

    def sqlite_pass(self, exclude_names: list[str], exclude_paths: list[str]) -> list[str]:
        """Back up every discovered SQLite DB, one snapshot per service; returns the ones now safely
        held, to leave out of `files`. Each service's consistent copies are staged at their own
        absolute path under <root>/sqlite/<service>, one service at a time to bound the disk."""
        section = self.config.get("sqlite")
        if not section:
            return []
        started = time.time()
        found = find_sqlite(section["roots"] + [m for m, _ in self.mounts], exclude_names, exclude_paths)
        log(f"sqlite: {len(found)} databases found in {time.time() - started:.0f}s")
        groups: dict[str, list[str]] = {}
        for path in found:
            groups.setdefault(self.service_of(path) or "other", []).append(path)
        held = []
        for group, paths in sorted(groups.items()):
            stage = self.root / "sqlite" / group
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True)
            need = sum(size_or_zero(p) for p in paths)
            if shutil.disk_usage(stage).free < need + (1 << 30):
                self.fail(f"sqlite {group}", f"{need >> 20} MiB of copies will not fit in the free disk space")
                continue
            copied = []
            for path in paths:
                dst = stage / path.lstrip("/")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    backup_sqlite(Path(path), dst)
                    with contextlib.closing(sqlite3.connect(dst)) as conn:
                        verdict = conn.execute("pragma quick_check").fetchone()[0]
                    if verdict != "ok":
                        self.warnings.append(f"sqlite {path}: quick_check says {verdict[:80]}")
                    copied.append(path)
                except FileNotFoundError:
                    # deleted between the search and the copy (scratch DBs come and go): not a failure
                    dst.unlink(missing_ok=True)
                    self.warnings.append(f"sqlite {path}: vanished before it could be copied")
                except SqliteBusy as exc:
                    # the file snapshot carries the raw file instead: usually a browser profile
                    dst.unlink(missing_ok=True)
                    note = f"sqlite {path}: {exc}, so the file snapshot carries the raw file"
                    log(f"WARNING {note}")
                    self.warnings.append(note)
                except Exception as exc:  # noqa: BLE001 - one bad DB must not stop the rest
                    dst.unlink(missing_ok=True)
                    self.fail(f"sqlite {path}", str(exc))
            if copied and self.backup("sqlite", group, [str(stage)], [f"svc:{group}"]):
                held += copied
            shutil.rmtree(stage, ignore_errors=True)
        return held

    def database_passes(self) -> None:
        for entry in self.config.get("postgres", []):
            try:
                self.postgres(entry.get("service"), ["runuser", "-u", entry.get("user", "postgres"), "--"],
                              "host", entry.get("exclude"))
            except Exception as exc:  # noqa: BLE001
                self.fail("postgres host", str(exc))
        configured = set()
        for entry in self.config.get("docker", []):
            configured.add(entry["container"])
            try:
                if entry["kind"] == "postgres":
                    self.postgres(entry.get("service"), ["docker", "exec", entry["container"]],
                                  entry["container"], entry.get("exclude"), user=entry.get("user", "postgres"))
                elif entry["kind"] in ("mariadb", "mysql"):
                    self.mysql(entry)
                else:
                    self.fail(f"docker {entry['container']}", f"unknown kind {entry['kind']}")
            except Exception as exc:  # noqa: BLE001
                self.fail(f"docker {entry['container']}", str(exc))
        self.audit_db_containers(configured)

    def postgres(self, service, wrap: list[str], label: str, exclude: str | None, user: str | None = None) -> None:
        extra = ["-U", user] if user else []
        listing = subprocess.run(
            [*wrap, "psql", *extra, "-d", "postgres", "-qAt", "-c",
             "select datname from pg_database where not datistemplate order by 1"],
            capture_output=True, text=True, check=True).stdout.split()
        base = f"/dumps/postgres/{label}"
        svc = [f"svc:{service}"] if service else []
        self.dump("postgres", f"{label}/_globals", f"{base}/_globals.sql",
                  [*wrap, "pg_dumpall", *extra, "--globals-only"], [*svc, f"db:{label}/_globals"])
        for db in listing:
            if exclude and re.search(exclude, db):
                continue
            self.dump("postgres", f"{label}/{db}", f"{base}/{db}.sql",
                      [*wrap, "pg_dump", *extra, "--no-password", db], [*svc, f"db:{db}"] if label == "host"
                      else [*svc, f"db:{label}/{db}"])

    def mysql(self, entry: dict) -> None:
        name = entry["container"]
        svc = [f"svc:{entry['service']}"] if entry.get("service") else []
        password_var = entry.get("password_env", "MARIADB_ROOT_PASSWORD")
        script = (f'MYSQL_PWD="${password_var}" exec mariadb-dump -uroot --all-databases '
                  f'--single-transaction --routines --events --triggers')
        self.dump("mysql", f"{name}/all-databases", f"/dumps/mysql/{name}/all-databases.sql",
                  ["docker", "exec", name, "sh", "-c", script], [*svc, f"db:{name}/all"])

    def audit_db_containers(self, configured: set[str]) -> None:
        proc = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Image}}"], capture_output=True, text=True)
        if proc.returncode:
            return
        for line in proc.stdout.splitlines():
            name, image = line.split(" ", 1)
            if DB_IMAGES.search(image) and name not in configured:
                self.fail(f"docker {name}", f"runs {image}, a database, and backup.json `docker` does not dump "
                                            f"it: its volume would be copied raw, mid-write")

    def file_passes(self, held_sqlite: list[str], exclude: dict) -> None:
        patterns = list(exclude.get("names", [])) + list(exclude.get("paths", [])) + [str(self.root)]
        images = loop_images() if self.mounts else {}
        dynamic = [images[dev] for _, dev in self.mounts if dev in images]
        for db in held_sqlite:
            dynamic += [db, db + "-wal", db + "-shm", db + "-journal"]
        exclude_file = self.root / "exclude.txt"
        exclude_file.write_text("\n".join(patterns + [escape_pattern(p) for p in dynamic]) + "\n")
        os.chmod(exclude_file, 0o600)
        flags = ["--exclude-file", str(exclude_file), "--exclude-caches", "--exclude-if-present", ".nobackup",
                 "--one-file-system"]
        for path in self.config["paths"]:
            self.backup("files", path, [*flags, path], [f"root:{path}"])
        for target, _ in self.mounts:
            self.backup("mount", target, [*flags, target], [f"root:{target}"])

    def housekeeping(self) -> dict:
        section = self.config["restic"]
        keep = section["keep"]
        proc = self.restic("forget", "--host", self.host, "--group-by", "host,paths", "--prune",
                           "--keep-daily", str(keep["daily"]), "--keep-weekly", str(keep["weekly"]),
                           "--keep-monthly", str(keep["monthly"]), check=False)
        if proc.returncode:
            self.fail("forget/prune", proc.stderr)
        check = ["check"]
        if date.today().strftime("%a").lower() == section.get("check_day", "sun")[:3].lower():
            check += ["--read-data-subset", section.get("check_subset", "2%")]
        proc = self.restic(*check, check=False)
        if proc.returncode:
            self.fail("repository check", proc.stderr or proc.stdout)
        stats = self.restic("stats", "--mode", "raw-data", "--json", check=False)
        snaps = self.restic("snapshots", "--json", "--no-lock", check=False)
        info = {}
        try:
            info["repo_bytes"] = json.loads(stats.stdout)["total_size"]
            info["snapshots"] = len(json.loads(snaps.stdout))
        except (ValueError, KeyError):
            pass
        return info

    def write_status(self, started: float, repo: dict) -> None:
        path = self.root / "status.json"
        try:
            previous = json.loads(path.read_text())
        except (OSError, ValueError):
            previous = {}
        finished = time.time()
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")  # noqa: E731
        ok = not self.errors
        status = {"ok": ok, "started": iso(started), "finished": iso(finished),
                  "seconds": round(finished - started), "last_success": iso(finished) if ok else previous.get("last_success"),
                  "errors": self.errors, "warnings": self.warnings, "repo": repo,
                  "added_bytes": sum(s["added_bytes"] or 0 for s in self.snapshots), "snapshots": self.snapshots}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=1))
        tmp.replace(path)


def cmd_run(config: dict, env: dict) -> None:
    run = Run(config, env)
    run.root.mkdir(parents=True, exist_ok=True)
    lock = open(run.root / "g-backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another g-backup is already running") from None
    with lock:
        run_locked(run, config)


def run_locked(run: Run, config: dict) -> None:
    started = time.time()
    run.preflight()
    exclude = config.get("exclude", {})
    exclude_names, exclude_paths = exclude.get("names", []), exclude.get("paths", []) + [str(run.root)]
    if config.get("mounts"):
        run.mounts = local_mounts(exclude_paths)
    log("facts")
    facts = run.write_facts()
    if run.backup("facts", str(facts), [str(facts)], []):
        try:
            run.canary(facts, run.snapshots[-1]["id"])
            log("canary restore of facts matches")
        except Exception as exc:  # noqa: BLE001
            run.fail("canary restore", str(exc))
    held = run.sqlite_pass(exclude_names, exclude_paths)
    run.database_passes()
    run.file_passes(held, exclude)
    repo = run.housekeeping()
    run.write_status(started, repo)
    if run.errors:
        raise RuntimeError(f"{len(run.errors)} item(s) failed: " + " | ".join(e[:120] for e in run.errors[:5]))
    log(f"done: {len(run.snapshots)} snapshots, +{sum(s['added_bytes'] or 0 for s in run.snapshots) / 1048576:.0f} MiB, "
        f"repo {repo.get('repo_bytes', 0) / 1073741824:.2f} GiB, {len(run.warnings)} warnings, "
        f"{time.time() - started:.0f}s")


def latest_by_group(run: Run, tags: list[str]) -> list[dict]:
    """Newest snapshot of every group (path set) carrying all of `tags` (restic ANDs a comma list)."""
    snaps = json.loads(run.restic("snapshots", "--json", "--host", run.host, "--tag",
                                  ",".join(["g-backup", *tags])).stdout)
    groups: dict[tuple, dict] = {}
    for snap in sorted(snaps, key=lambda s: s["time"]):
        groups[tuple(snap["paths"])] = snap
    return list(groups.values())


def cmd_status(config: dict, env: dict) -> None:
    """Print how fresh the backups are; exit 1 if the last run failed or no run has succeeded in 36 hours."""
    run = Run(config, env)
    now = datetime.now(timezone.utc)
    stale = False
    try:
        status = json.loads((run.root / "status.json").read_text())
        last = status["last_success"]
        age = (now - datetime.fromisoformat(last)).total_seconds() / 3600 if last else None
        stale = not status["ok"] or age is None or age > 36
        log(f"last run: {'ok' if status['ok'] else 'FAILED'} at {status['finished']} ({status['seconds']}s), "
            f"last success {last}, repo {status['repo'].get('repo_bytes', 0) / 1073741824:.2f} GiB, "
            f"{len(status['warnings'])} warnings, {len(status['errors'])} errors")
    except (OSError, ValueError, KeyError):
        stale = True
        log("no status.json yet: g-backup has not completed a run here")
    kinds: dict[str, list] = {}
    for snap in latest_by_group(run, []):
        kind = next((t for t in snap["tags"] if t in ("files", "mount", "sqlite", "postgres", "mysql", "facts")), "?")
        kinds.setdefault(kind, []).append(snap)
    for kind, snaps in sorted(kinds.items()):
        ages = [(now - datetime.fromisoformat(s["time"][:19] + "+00:00")).total_seconds() / 3600 for s in snaps]
        log(f"{kind}: {len(snaps)} groups, newest {min(ages):.1f}h old, oldest {max(ages):.1f}h old")
    if stale:
        sys.exit(1)


def cmd_restore(config: dict, env: dict, args) -> None:
    run = Run(config, env)
    paths = run.services.get(args.service, []) if args.service else []
    if args.service and not paths:
        log(f"note: `services` in backup.json has no paths for {args.service}; restoring its tagged snapshots only")
    wanted = ([f"svc:{args.service}"] if args.service else []) + args.tag
    selected = {s["id"]: (s, []) for s in latest_by_group(run, wanted)}
    if paths:
        for kind in ("files", "mount"):
            for snap in latest_by_group(run, [kind]):
                inside = [p for p in paths if any(under(p, [r]) or under(r, [p]) for r in snap["paths"])]
                if inside:
                    selected[snap["id"]] = (snap, inside)
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    done = 0
    for snap, includes in selected.values():
        flags = [x for p in includes for x in ("--include", p)]
        log(f"restoring {snap['short_id']} {','.join(snap['paths'])[:80]} -> {target}")
        staged = "sqlite" in snap["tags"]
        into = Path(tempfile.mkdtemp(dir=target)) if staged else target
        proc = run.restic("restore", snap["id"], "--target", str(into), *flags, check=False)
        if proc.returncode:
            raise RuntimeError(f"restore {snap['short_id']} failed: {proc.stderr.strip()[-300:]}")
        if staged:  # the copies were staged under <root>/sqlite/<service>; put them at their own paths
            shutil.copytree(into / snap["paths"][0].lstrip("/"), target, dirs_exist_ok=True)
            shutil.rmtree(into)
        done += 1
    log(f"restored {done} snapshot(s) into {target}")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="g-backup")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("init")
    sub.add_parser("snapshots")
    restore = sub.add_parser("restore")
    restore.add_argument("--target", required=True)
    restore.add_argument("--service")
    restore.add_argument("--tag", action="append", default=[])
    passthrough = sub.add_parser("restic")
    passthrough.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    config, passport = load_config()
    env = restic_env(config, passport)
    if args.cmd in (None, "run"):
        cmd_run(config, env)
    elif args.cmd == "status":
        cmd_status(config, env)
    elif args.cmd == "init":
        subprocess.run(["restic", "init"], env=env, check=True)
    elif args.cmd == "snapshots":
        subprocess.run(["restic", "snapshots", "--group-by", "host,paths", "--latest", "1"], env=env, check=True)
    elif args.cmd == "restore":
        cmd_restore(config, env, args)
    elif args.cmd == "restic":
        sys.exit(subprocess.run(["restic", *[a for a in args.rest if a != "--"]], env=env).returncode)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:  # noqa: BLE001 - systemd needs a clean non-zero exit
        log(f"FAILED: {exc}")
        sys.exit(1)
