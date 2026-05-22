#!/usr/bin/env bash
# VedaVine — one-command deploy from Mac to the Pi.
#
# Usage:
#   ./deploy.sh            # rsync + restart
#   ./deploy.sh --setup    # rsync + run setup.sh on Pi (re-applies systemd unit, etc.)
#
# Requires SSH key auth to the Pi (no password prompt).

set -euo pipefail

PI_USER="${VEDAVINE_PI_USER:-frc}"
PI_HOST="${VEDAVINE_PI_HOST:-192.168.5.70}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "==> Syncing $APP_DIR/ -> ${PI_USER}@${PI_HOST}:~/vedavine/"
rsync -av --delete-after \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='uploads' \
  --exclude='rag.db' \
  --exclude='corpus' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='.DS_Store' \
  --exclude='CLAUDE.local.md' \
  "$APP_DIR/" "${PI_USER}@${PI_HOST}:~/vedavine/"

if [[ "${1:-}" == "--setup" ]]; then
  bold "==> Re-running setup.sh on Pi (apt + venv + systemd unit refresh)"
  # -t forces TTY so sudo can prompt for password during setup.sh
  ssh -t "${PI_USER}@${PI_HOST}" "cd ~/vedavine && bash setup.sh"
else
  bold "==> Restarting vedavine.service on Pi"
  # Requires NOPASSWD sudoers entry for these systemctl commands; see README.
  ssh "${PI_USER}@${PI_HOST}" "sudo systemctl restart vedavine && sudo systemctl is-active vedavine"
fi

bold "==> Done"
echo "    http://${PI_HOST}:5000/"
