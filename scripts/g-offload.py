#!/usr/bin/env python3
"""Move cold files off a host's disk into an rclone remote, verified before anything local goes.

What moves is the instance's: `offload.json` in the instance (gp-core) that g-env's
bin/g-instance names, beside backup.json. It holds the rclone config, a state directory
and a list of rules. A rule names a local `root`, a `glob` of files under it, an age in
days (`older_than_days`, by modification time) and the `remote` prefix. A file the rule
matches is copied to <remote>/<its path under root>, checked against the remote copy by
size and hash, and deleted locally only when the check says the two are identical.

Dry run by default: every rule reports what it would move and how much. `--apply` moves
the files of rules marked `"enabled": true`, so a rule can be read on the real disk before
anything is moved. Each moved file is appended to <state>/<rule>.jsonl (path, bytes,
mtime, remote, moved_at). Symlinks are never followed or moved, and neither is a file
whose name starts with a dot.

    g-offload.py                     report every rule
    g-offload.py --apply             move what enabled rules match
    g-offload.py --rule NAME         limit either to one rule
    g-offload.py --restore NAME [--match GLOB]   copy a rule's files back from the remote

Restore copies back and leaves the remote copy where it is. On R2 every rclone call needs
--s3-no-check-bucket, or a bucket-scoped token gets a 403 on the bucket-exists preflight.
Exits non-zero when a rclone step fails, before deleting anything that step covered.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# g-env sits beside this checkout; G_ENV overrides that.
WORKSPACE = Path(__file__).resolve().parents[2]
G_ENV = os.environ.get("G_ENV") or os.environ.get("GENV_DIR") or next(
    (str(p) for p in (WORKSPACE / "g-env", WORKSPACE / "genv") if p.exists()), str(WORKSPACE / "g-env"))


def log(msg: str) -> None:
    print(f"[g-offload] {msg}", flush=True)


def size(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def load_config() -> dict:
    instance = subprocess.run([str(Path(G_ENV) / "bin/g-instance")], capture_output=True, text=True,
                              check=True).stdout.strip()
    if not instance:
        raise FileNotFoundError("no instance, so no offload.json to say what may move")
    return json.loads((Path(instance) / "offload.json").read_text())


def candidates(rule: dict, now: float) -> list[tuple[str, int, float]]:
    """(path under root, bytes, mtime) for each regular file the rule moves, oldest first."""
    root = Path(rule["root"])
    if not root.is_dir() or root.is_symlink():
        return []
    cutoff = now - float(rule["older_than_days"]) * 86400
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if name.startswith(".") or not fnmatch.fnmatch(rel, rule.get("glob", "*")):
                continue
            st = path.lstat()
            if path.is_symlink() or not path.is_file() or st.st_mtime >= cutoff:
                continue
            found.append((rel, st.st_size, st.st_mtime))
    return sorted(found, key=lambda f: (f[2], f[0]))


def rclone(args: list[str], config: dict) -> subprocess.CompletedProcess:
    cmd = ["rclone", *args, "--config", config["rclone_config"], "--s3-no-check-bucket"]
    log("+ " + " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def identical(out_file: Path) -> set[str]:
    """Paths `rclone check --combined` marked `=`: present on both sides and matching."""
    same = set()
    for line in out_file.read_text().splitlines():
        if line.startswith("= "):
            same.add(line[2:])
    return same


def move(rule: dict, files: list[tuple[str, int, float]], config: dict) -> tuple[int, int]:
    """Copy, check, then delete what matched. Returns (files moved, bytes freed)."""
    root, remote = rule["root"], rule["remote"]
    with tempfile.TemporaryDirectory(prefix="g-offload-") as tmp:
        listing, combined = Path(tmp) / "files", Path(tmp) / "combined"
        listing.write_text("".join(f"{rel}\n" for rel, _, _ in files))
        copied = rclone(["copy", root, remote, "--files-from-raw", str(listing)], config)
        if copied.returncode != 0:
            raise RuntimeError(f"{rule['name']}: copy failed, nothing deleted: {copied.stderr.strip()[-300:]}")
        checked = rclone(["check", root, remote, "--files-from-raw", str(listing), "--one-way",
                          "--combined", str(combined)], config)
        same = identical(combined) if combined.exists() else set()
    state = Path(config["state"])
    state.mkdir(parents=True, exist_ok=True)
    moved = freed = 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(state / f"{rule['name']}.jsonl", "a") as index:
        for rel, nbytes, mtime in files:
            if rel not in same:
                continue
            path = Path(root) / rel
            # A file written since it was listed is not the file that was checked.
            if path.lstat().st_mtime != mtime or path.lstat().st_size != nbytes:
                continue
            path.unlink()
            index.write(json.dumps({"path": rel, "bytes": nbytes, "mtime": mtime,
                                    "remote": f"{remote}/{rel}", "moved_at": stamp}) + "\n")
            moved, freed = moved + 1, freed + nbytes
    left = len(files) - moved
    if left:
        detail = " ".join((checked.stderr or "").split())[-300:]
        log(f"{rule['name']}: {left} file(s) did not check identical and stay on disk ({detail})")
    if checked.returncode != 0 and not same:
        raise RuntimeError(f"{rule['name']}: check failed, nothing deleted")
    return moved, freed


def restore(rule: dict, pattern: str | None, config: dict) -> None:
    args = ["copy", rule["remote"], rule["root"]]
    if pattern:
        args += ["--include", pattern]
    out = rclone(args, config)
    if out.returncode != 0:
        raise RuntimeError(f"{rule['name']}: restore failed: {out.stderr.strip()[-300:]}")
    log(f"{rule['name']}: restored {pattern or 'everything'} into {rule['root']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="move what enabled rules match")
    ap.add_argument("--rule", help="only this rule")
    ap.add_argument("--restore", metavar="NAME", help="copy a rule's files back from the remote")
    ap.add_argument("--match", help="with --restore: an rclone include glob")
    args = ap.parse_args(argv)

    config = load_config()
    rules = {r["name"]: r for r in config["rules"]}
    if args.restore:
        if args.restore not in rules:
            raise KeyError(f"no rule named {args.restore}")
        restore(rules[args.restore], args.match, config)
        return 0
    if args.rule and args.rule not in rules:
        raise KeyError(f"no rule named {args.rule}")

    now = time.time()
    total = 0
    for name, rule in rules.items():
        if args.rule and name != args.rule:
            continue
        files = candidates(rule, now)
        nbytes = sum(f[1] for f in files)
        on = rule.get("enabled", False)
        log(f"{name}: {len(files)} file(s), {size(nbytes)} older than {rule['older_than_days']} days "
            f"in {rule['root']} -> {rule['remote']}{'' if on else ' (rule disabled)'}")
        if not (args.apply and on and files):
            continue
        moved, freed = move(rule, files, config)
        total += freed
        log(f"{name}: moved {moved} file(s), freed {size(freed)}")
    if args.apply:
        log(f"done: freed {size(total)}")
    else:
        log("dry run: nothing moved (pass --apply to move what enabled rules match)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - systemd needs a clean non-zero exit
        log(f"FAILED: {exc}")
        sys.exit(1)
