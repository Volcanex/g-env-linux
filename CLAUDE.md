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
- The root crontab runs the server-wide guidance mirror every five minutes (below).
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
- `scripts/g-backup.py` is the nightly offsite backup, run as root. What it carries
  (directories, sqlite DBs, the local tarball root, the rclone config and remote) is
  the instance's `backup.json`, found through `g-instance`, and with no instance or
  no list the run fails. Snapshots go through `sqlite3.backup()`, never a plain `cp`
  of a live db. It keeps the last 3 local tarballs, prunes remote objects older than
  30 days, and exits non-zero on any failure so systemd marks the unit failed. The
  script's docstring has the restore command, and `scripts/test_g_backup.py` covers
  the contract.

## Claude guidance mirroring

Bootstrap, run as root, installs one root crontab line (marked `# g-env-mirror-claude`,
which g-cron and the crons page show as its name) that runs g-env's
`scripts/mirror-claude.py --server-wide --user-skills` every five minutes under `flock`.
It replaces a line under the old `# genv-mirror-claude` marker and retires the older
`genv-mirror-claude.timer`. Until bootstrap runs again, a host keeps its old line. The mirror
picks up newly created nonignored instructions and registered worktrees. Existing
relative links reflect content edits immediately. Discovery is bounded to depth
three under g-env's siblings and the invoking user's home, and dependency and hidden
trees are excluded except registered worktrees. Independent AGENTS.md files are
preserved and reported. This mirrors files, not Claude hooks or MCP configuration.
Inspect failures in `/var/log/g-env-mirror-claude.log` (`/var/log/genv-mirror-claude.log`
on a host still running the old line).
