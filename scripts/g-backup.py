#!/usr/bin/env python3
"""Nightly offsite backup: the instance's directories and sqlite DBs -> an rclone remote.

What to carry is the instance's: `backup.json` in the instance (gp-core) that
g-env's bin/g-instance names. It lists the directories and the sqlite DBs (an
`optional` one may be absent, and the run names what it skipped), the local tarball
root, the rclone config and the remote, `repos`: git checkouts carried as bundles of
every ref, since a host's agents commit without pushing and those commits may exist
nowhere else (tracked changes not yet committed ride beside each as a patch),
and `latest`: files an app already dumps locally (a glob each), of which only the
newest is shipped, under <remote>/<name>/, keeping the newest `keep` there. With no
instance or no list the run fails, so systemd shows it, and so does a `latest` glob
that matches nothing.

Runs as root, to read root-owned DBs and docker volumes. Flow: stage a dated
snapshot -> tar.gz -> rclone copy to the remote -> prune remote (>30d) and local
(keep the last `local_keep`, default 3: the remote already holds 30 days, so a host
short of disk can keep just the newest). Exits non-zero on any failure so the systemd
unit shows as failed.

Restore: rclone copy <remote>/g-backup-<date>.tar.gz <dest> --config <rclone_config>
--s3-no-check-bucket, then tar xzf. On R2 every rclone call needs
--s3-no-check-bucket, or a bucket-scoped token gets a 403 on the bucket-exists
preflight. Directories restore as plain trees under their names, and the DBs are
ready-to-use sqlite files in db/. Check them with PRAGMA integrity_check (through
Python's sqlite3 module on a host with no sqlite3 CLI). A repo restores with
`git clone repos/<name>.bundle <dir>`, then `git apply repos/<name>.uncommitted.patch`
if there is one. A repo that will not bundle fails the run, but only after the rest
has shipped.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import date
from pathlib import Path

LOCAL_KEEP = 3
REMOTE_MIN_AGE = "30d"

# g-env sits beside this checkout; G_ENV overrides that.
WORKSPACE = Path(__file__).resolve().parents[2]
G_ENV = os.environ.get("G_ENV") or os.environ.get("GENV_DIR") or next(
    (str(p) for p in (WORKSPACE / "g-env", WORKSPACE / "genv") if p.exists()), str(WORKSPACE / "g-env"))


def log(msg: str) -> None:
    print(f"[g-backup] {msg}", flush=True)


def backup_sqlite(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"missing DB source: {src}")
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def bundle_repo(src: Path, dest: Path, name: str) -> None:
    """Every ref of a checkout as one bundle, and its uncommitted tracked changes as a patch."""
    if not (src / ".git").exists():
        # Without this, git would walk up and bundle whatever repo holds the directory.
        raise FileNotFoundError(f"not a git checkout: {src}")
    # Runs as root over checkouts other users own, which git refuses without safe.directory.
    git = ["git", "-c", "safe.directory=*", "-C", str(src)]
    subprocess.run([*git, "bundle", "create", str(dest / f"{name}.bundle"), "--all"],
                   check=True, capture_output=True)
    diff = subprocess.run([*git, "diff", "HEAD", "--binary"], check=True, capture_output=True).stdout
    if diff:
        (dest / f"{name}.uncommitted.patch").write_bytes(diff)


def load_config() -> dict:
    instance = subprocess.run([str(Path(G_ENV) / "bin/g-instance")], capture_output=True, text=True,
                              check=True).stdout.strip()
    if not instance:
        raise FileNotFoundError("no instance, so no backup.json to say what to back up")
    return json.loads((Path(instance) / "backup.json").read_text())


def run(cmd: list[str]) -> None:
    log("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def ship_latest(entry: dict, remote: str, rclone_config: str) -> None:
    files = sorted(glob.glob(entry["glob"]))
    if not files:
        raise FileNotFoundError(f"nothing matches {entry['glob']}")
    dest = f"{remote}/{entry['name']}"
    flags = ["--config", rclone_config, "--s3-no-check-bucket"]
    run(["rclone", "copy", files[-1], f"{dest}/", *flags])
    # Names sort by date, as the app writes them; keep the newest `keep` offsite.
    listed = subprocess.run(["rclone", "lsf", "--files-only", dest, *flags],
                            capture_output=True, text=True, check=True).stdout.split()
    for old in sorted(listed)[:-entry["keep"]]:
        run(["rclone", "deletefile", f"{dest}/{old}", *flags])


def main() -> None:
    config = load_config()
    backup_root = Path(config["root"])
    remote, rclone_config = config["remote"], config["rclone_config"]
    today = date.today().isoformat()
    stage_dir = backup_root / "staging" / today
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    log(f"staging into {stage_dir}")

    # directories, each under its name (an optional one, such as dated wages
    # backups, is carried as-is when present)
    skipped = []
    for entry in config["dirs"]:
        src = Path(entry["path"])
        if not src.exists():
            if entry.get("optional"):
                skipped.append(entry["name"])
                continue
            raise FileNotFoundError(f"missing dir: {src}")
        run(["rsync", "-a", f"{src}/", str(stage_dir / entry["name"]) + "/"])

    # sqlite DBs, snapshotted via the sqlite backup API (safe against a live writer)
    db_dir = stage_dir / "db"
    db_dir.mkdir(exist_ok=True)
    for entry in config["sqlite"]:
        src, dst = Path(entry["path"]), db_dir / entry["name"]
        # A paused or retired stack's DB may go; marking it optional keeps that from
        # failing the whole night's backup.
        if not src.exists() and entry.get("optional"):
            skipped.append(entry["name"])
            continue
        log(f"snapshotting {src} -> {dst}")
        backup_sqlite(src, dst)

    # git checkouts. One that will not bundle is named and fails the run at the end,
    # so it never costs the night's copy of the data.
    unbundled = []
    repo_dir = stage_dir / "repos"
    for entry in config.get("repos", []):
        src = Path(entry["path"])
        if not src.exists():
            if entry.get("optional"):
                skipped.append(entry["name"])
                continue
            raise FileNotFoundError(f"missing repo: {src}")
        log(f"bundling {src}")
        repo_dir.mkdir(exist_ok=True)
        try:
            bundle_repo(src, repo_dir, entry["name"])
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or b""
            log(f"could not bundle {entry['name']}: {detail.decode(errors='replace').strip() or exc}")
            unbundled.append(entry["name"])

    # tar the whole staged snapshot
    backup_root.mkdir(parents=True, exist_ok=True)
    tarball = backup_root / f"g-backup-{today}.tar.gz"
    log(f"creating {tarball}")
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(stage_dir, arcname=today)

    shutil.rmtree(stage_dir)

    # ship it. --s3-no-check-bucket: a bucket-scoped R2 token has no
    # ListBuckets/CreateBucket, so rclone's pre-upload bucket check must be skipped.
    run([
        "rclone", "copy", str(tarball), f"{remote}/",
        "--config", rclone_config, "--s3-no-check-bucket",
    ])

    for entry in config.get("latest", []):
        ship_latest(entry, remote, rclone_config)

    # prune remote beyond retention. A `latest` dir keeps its newest `keep` by count
    # instead: rclone keeps the source mtime, so if the app stopped dumping for 30
    # days the age prune would delete every offsite copy it has.
    keep_dirs = [arg for entry in config.get("latest", [])
                 for arg in ("--exclude", f"/{entry['name']}/**")]
    run([
        "rclone", "delete", remote, "--min-age", REMOTE_MIN_AGE,
        "--config", rclone_config, "--s3-no-check-bucket", *keep_dirs,
    ])

    # keep the last local_keep local tarballs; the one just shipped is always kept
    keep = max(1, int(config.get("local_keep", LOCAL_KEEP)))
    tarballs = sorted(glob.glob(str(backup_root / "g-backup-*.tar.gz")))
    for old in tarballs[:-keep]:
        log(f"pruning local {old}")
        os.remove(old)

    note = f"; skipped optional, not there: {', '.join(skipped)}" if skipped else ""
    log(f"done: {tarball.name} ({tarball.stat().st_size} bytes){note}")
    if unbundled:
        raise RuntimeError(f"shipped, but could not bundle: {', '.join(unbundled)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - systemd needs a clean non-zero exit
        log(f"FAILED: {exc}")
        sys.exit(1)
