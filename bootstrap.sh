#!/usr/bin/env bash
# Bootstrap Linux server: calls the g-env base layer then adds Linux-specific wiring.
# Idempotent, so safe to re-run. Tested on Ubuntu 22.04/24.04.
# Run as the user who will run Claude Code on this host.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$REPO")"
# g-env: G_ENV, then the legacy GENV_DIR, then a sibling g-env, then a legacy sibling genv.
G_ENV="${G_ENV:-${GENV_DIR:-}}"
if [[ -z "$G_ENV" ]]; then
  G_ENV="$PARENT/g-env"
  if [[ ! -e "$G_ENV" && -d "$PARENT/genv" ]]; then G_ENV="$PARENT/genv"; fi
fi

# Pull g-env if not present
if [[ ! -e "$G_ENV/.git" ]]; then
  echo "g-env not found at $G_ENV; cloning..."
  git clone https://github.com/Volcanex/g-env "$G_ENV"
fi

# Run the platform-agnostic base layer first
bash "$G_ENV/bootstrap-base.sh"

link() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [[ -L "$dst" ]]; then rm "$dst"
  elif [[ -e "$dst" ]]; then mv "$dst" "$dst.backup.$(date +%s)"; fi
  ln -s "$src" "$dst"
  echo "linked $dst -> $src"
}

# Shell, only if zsh is installed
if command -v zsh &>/dev/null; then
  link "$REPO/zsh/zshrc" "$HOME/.zshrc"
else
  echo "zsh not found, skipping .zshrc"
fi

# SSH config: the ssh/config row of g-env's overlay table. The instance's
# env/ssh/config replaces the framework default whole. Keys live at their own
# paths and are never linked from here. A g-env without g-instance has no instance.
INSTANCE=""
if [[ -x "$G_ENV/bin/g-instance" ]]; then INSTANCE="$("$G_ENV/bin/g-instance" 2>/dev/null)"; fi
SSH_CONFIG="$REPO/ssh/config"
if [[ -n "$INSTANCE" && -f "$INSTANCE/env/ssh/config" ]]; then SSH_CONFIG="$INSTANCE/env/ssh/config"; fi
if [[ -f "$SSH_CONFIG" ]]; then
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  link "$SSH_CONFIG" "$HOME/.ssh/config"
fi

# settings.local.json: create from example if not present
LOCAL="$HOME/.claude/settings.local.json"
if [[ ! -f "$LOCAL" ]]; then
  cp "$REPO/claude/settings.local.linux.json" "$LOCAL"
  echo "created $LOCAL from linux defaults"
fi

# Skills are tracked directly in g-env (skills/) and symlinked by
# bootstrap-base.sh, with no sync step. To refresh pinned upstream skills, run
# scripts/vendor-skills.sh manually.

# Playwright MCP: install Chromium for headless browser testing
if command -v npx &>/dev/null; then
  echo "installing Playwright Chromium (headless)..."
  npx playwright install chromium 2>&1 | tail -2
else
  echo "npx not found, skipping Playwright Chromium install"
fi

# Install self-maintaining systemd timer (requires sudo / running as root)
if command -v systemctl &>/dev/null; then
  UNIT_DIR="/etc/systemd/system"
  GENV_LINUX_DIR="$REPO"
  GENV_BASE_DIR="$G_ENV"

  # Stamp real paths into the service unit
  sed \
    -e "s|__GENV_DIR__|$GENV_BASE_DIR|g" \
    -e "s|__GENV_LINUX_DIR__|$GENV_LINUX_DIR|g" \
    -e "s|__USER__|$(whoami)|g" \
    "$REPO/systemd/genv-sync.service.template" > "$UNIT_DIR/genv-sync.service"

  cp "$REPO/systemd/genv-sync.timer" "$UNIT_DIR/genv-sync.timer"

  systemctl daemon-reload
  systemctl enable --now genv-sync.timer
  echo "genv-sync.timer installed and enabled"
else
  echo "systemd not found, skipping timer install"
fi

# Keep guidance mirroring server-wide and persistent through root's crontab.
# Install the replacement first, then retire the older systemd timer so the two
# scanners cannot overlap. The marker limits edits to this managed cron entry,
# and g-cron names the job after it. An entry under the old genv- marker is
# replaced, not duplicated.
if [[ "${EUID:-$(id -u)}" -eq 0 ]] && command -v crontab >/dev/null && command -v flock >/dev/null; then
  CRON_MARKER='# g-env-mirror-claude'
  LEGACY_MARKER='# genv-mirror-claude'
  CRON_TMP="$(mktemp)"
  CRONTAB="$(command -v crontab)"
  FLOCK="$(command -v flock)"
  PYTHON="$(command -v python3)"
  printf -v MIRROR_ENTRY '*/5 * * * * %q -n %q %q %q --server-wide --user-skills >> %q 2>&1 %s' \
    "$FLOCK" /run/lock/g-env-mirror-claude.lock "$PYTHON" \
    "$G_ENV/scripts/mirror-claude.py" /var/log/g-env-mirror-claude.log "$CRON_MARKER"
  { "$CRONTAB" -l 2>/dev/null | grep -vF -e "$CRON_MARKER" -e "$LEGACY_MARKER" || true
    printf '%s\n' "$MIRROR_ENTRY"; } > "$CRON_TMP"
  "$CRONTAB" "$CRON_TMP"
  rm -f "$CRON_TMP"
  echo "installed root cron: server-wide guidance mirror every five minutes"

  if command -v systemctl >/dev/null; then
    systemctl disable --now genv-mirror-claude.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/genv-mirror-claude.service /etc/systemd/system/genv-mirror-claude.timer
    systemctl daemon-reload
  fi
elif [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "guidance mirror cron skipped: run Linux bootstrap as root to manage root's crontab"
else
  echo "guidance mirror cron unavailable: install crontab and flock before bootstrapping"
  exit 1
fi

echo
echo "g-env-linux: done${INSTANCE:+ (instance $INSTANCE)}."
