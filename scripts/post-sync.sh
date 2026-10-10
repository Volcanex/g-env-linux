#!/usr/bin/env bash
# Runs after every g-env pull. Also pulls the instance (gp-core), found through
# g-env's resolver. No render step: gp-core's pre-commit hook commits the rendered
# topology, so rendering here could only dirty the checkout and block later pulls.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$REPO")"
G_ENV="${G_ENV:-${GENV_DIR:-}}"
if [[ -z "$G_ENV" ]]; then
  G_ENV="$PARENT/g-env"
  if [[ ! -e "$G_ENV" && -d "$PARENT/genv" ]]; then G_ENV="$PARENT/genv"; fi
fi

INSTANCE=""
if [[ -x "$G_ENV/bin/g-instance" ]]; then INSTANCE="$("$G_ENV/bin/g-instance" 2>/dev/null)"; fi
# A g-env without the resolver (main before a host adopts) keeps the old location.
if [[ -z "$INSTANCE" && -d "$HOME/gcore" ]]; then INSTANCE="$HOME/gcore"; fi

if [[ -n "$INSTANCE" && -e "$INSTANCE/.git" ]]; then
  git -C "$INSTANCE" pull --ff-only --quiet && echo "instance: pulled $INSTANCE"
fi
