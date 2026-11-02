# g-env-linux

Linux and Ubuntu overlay for g-env. Adds the server zshrc, SSH config, and the
self-maintaining systemd sync timer that keeps g-env up to date on every server.

**Depends on:** g-env, found at `$G_ENV`, else the legacy `$GENV_DIR`, else a sibling
`g-env`, else a legacy sibling `genv` (cloned as a sibling `g-env` when none exists).
The instance (gp-core) is found through g-env's `bin/g-instance`. Hosts, users and
domains are the instance's: nothing here should name them.

## Bootstrap (one-time, per server)

```bash
git clone <this repo> /path/to/g-env-linux
sudo bash /path/to/g-env-linux/bootstrap.sh
```

This clones g-env if needed, runs `g-env/bootstrap-base.sh`, wires Linux config, creates
`~/.claude/settings.local.json` from the linux defaults, and installs the root cron entries.

## Self-maintenance

After bootstrap, `genv-sync.timer` fires daily (unit names keep the old `genv-`
prefix on existing hosts):
1. `git pull --ff-only` on g-env
2. Re-runs `g-env/bootstrap-base.sh` (idempotent)
3. Runs `scripts/post-sync.sh`: pulls the instance that `g-instance` names (legacy
   `$HOME/gcore` when g-env has no resolver). It does not render: gp-core's pre-commit
   hook commits the rendered topology.

Push to g-env and the servers update overnight, with no manual step.

Check sync status: `systemctl status genv-sync.timer`
Force a sync now: `systemctl start genv-sync.service`

## What lives here

- `bootstrap.sh`: full Linux setup.
- `zsh/zshrc`: minimal Linux zshrc (no Homebrew, no Aerospace). It sources the
  passport through g-env's resolver: the instance's `.env`, else the `.env` that the
  instance's push-secrets writes into the g-env checkout on servers. Every variable
  is exported.
- `ssh/config`: generic framework default (keepalives only). It is the `ssh/config`
  row of g-env's overlay table: bootstrap links the instance's `env/ssh/config`
  instead when one exists. Private keys are never tracked here.
- `systemd/genv-sync.service.template`: sync service (paths stamped by bootstrap).
- `systemd/genv-sync.timer`: daily timer.
- Guidance mirroring runs daily through genv-sync; there is no mirror cron (below).
- `scripts/post-sync.sh`: hook for post-sync tasks (extend as needed).
- `scripts/pulse-cron-dispatch.py`: pulse cron dispatcher. It scans
  `brain/.pulse/crons/*.md`, matches schedules from frontmatter, and runs due crons
  through g-env's `bin/g-agent-run`, never a vendor CLI. The frontmatter `role:` picks
  the vendor via g-env's `context/providers.json`. Each prompt gets a job contract
  (final line `RESULT: ok|noop|fail - <sentence>`). Every run appends a row to
  `state/cron-runs.jsonl`, and fail or missing pages the alert spine once per failure
  streak (`state/cron-streaks.json`). `--list` shows the cron files, and `--run NAME`
  runs one now. The brain, brain-tools and g-env are found beside this checkout, and
  `BRAIN_ROOT`, `BRAIN_TOOLS`, `G_ENV` and `G_AGENT_RUN` override them. Last-run times
  are in `.pulse/state/cron-runs.json`.
- `claude/settings.local.linux.json`: default server Claude Code settings (copied to
  `~/.claude/settings.local.json` on first bootstrap, then owned locally).
- `LICENSE`: GNU AGPL v3.0 only.

## Units the instance installs

The dispatcher and the backup script are framework, but the units that run them
name a host's paths and user, so they live in the instance. They are installed by
hand as copies in `/etc/systemd/system/`.

- The dispatcher fires every five minutes as **root**, so Claude's auth stays fresh.
  Every file a cron writes into the brain then lands root-owned and the brain-mcp
  user can no longer write it, so the unit hands the tree back with an
  `ExecStartPost` chown after each run. If pulse or reminder writes start failing,
  check ownership first.
- `scripts/g-backup.py` is the nightly offsite backup, run as root: the whole host into an
  encrypted restic repository (restic 0.17 or newer, which is not Ubuntu's apt version). The
  instance's `backup.json`, found through `g-instance`, says what: `paths` (normally `/`) minus
  `exclude`, one filesystem at a time plus every local disk mount, `sqlite.roots` (every SQLite
  file found by header, copied through `sqlite3.backup()`, never a plain `cp` of a live db),
  `postgres` (the host cluster) and `docker` (each database container), all dumped as streams,
  `services` (restore and tags only), and `restic` (repository and credentials as `${NAME}`s
  filled from the instance's `.env`, retention). With no instance or the old tarball format the run
  fails. A failed item never stops the others, but the run exits non-zero at the end so systemd
  marks the unit failed, and a running database container that `docker` does not dump is itself
  a failure. It also writes `<root>/status.json`, prunes to the retention, checks the repository,
  and restores its own facts snapshot every night to prove restore works. `status`, `snapshots`,
  `restore` and `restic ...` subcommands are in its docstring along with the restore commands.
  `scripts/test_g_backup.py` covers the contract with a fake restic.

## Claude guidance mirroring

g-env's `bootstrap-base.sh` mirrors guidance (`scripts/mirror-claude.py --discover`), and
genv-sync runs it daily. The five-minute root cron that used to run
`mirror-claude.py --server-wide --user-skills` was retired on 2026-10-13. Bootstrap, run
as root, now removes its line under either marker (`# g-env-mirror-claude`,
`# genv-mirror-claude`) and the older `genv-mirror-claude.timer`, and installs nothing.
For a one-off server-wide pass run `python3 <g-env>/scripts/mirror-claude.py --server-wide
--user-skills`. The old cron's log is `/var/log/g-env-mirror-claude.log`. Independent AGENTS.md
files are preserved and reported. This mirrors files, not Claude hooks or MCP configuration.
