# g-env-linux

The server layer of the G-System. It sits on top of g-env and adds what a Linux
box needs: a minimal zshrc, the SSH config row, a daily timer that keeps g-env
current, and the scripts a server runs on a schedule.

Like g-env, it names no operator. Hosts, users and secrets come from the
instance (gp-core), which it finds through g-env's `bin/g-instance`. See
[CLAUDE.md](CLAUDE.md) for the search order and what each file does.

## Setup

```sh
git clone <this repo> /path/to/g-env-linux
sudo bash /path/to/g-env-linux/bootstrap.sh
```

Bootstrap clones g-env beside this checkout when there isn't one, runs its
`bootstrap-base.sh`, links the Linux config, writes `~/.claude/settings.local.json`
from the server defaults, and installs the sync timer and the root cron line. It
is idempotent: run it again after pulling and nothing breaks.

Claude Code on a server starts in `bypassPermissions`, with `IS_SANDBOX=1` so
that it accepts that mode as root: the server is treated as the sandbox, and
agents there work without prompts. Bootstrap copies those defaults to
`~/.claude/settings.local.json` once, so edit that file to turn the prompts
back on.

## What keeps running

- `genv-sync.timer`, daily: pulls g-env, re-runs its base bootstrap, then
  `scripts/post-sync.sh` pulls the instance. Push to g-env and every server has
  it by morning.
- A root cron line runs g-env's `mirror-claude.py` every five minutes, so a new
  CLAUDE.md or AGENTS.md gets its twin as a link without anyone asking.
- `scripts/pulse-cron-dispatch.py` runs the brain's scheduled model crons through
  g-env's `g-agent-run`, and pages once when one starts failing.
- `scripts/g-backup.py` takes the nightly offsite backup with rclone.

The units that run the dispatcher and the backup name a host's paths and user,
so they live in the instance and are installed by hand as copies in
`/etc/systemd/system/`.

```sh
systemctl status genv-sync.timer     # last and next sync
systemctl start genv-sync.service    # sync now
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE).
