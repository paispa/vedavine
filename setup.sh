#!/usr/bin/env bash
# VedaVine — one-command Pi setup.
# Idempotent: safe to re-run.

set -euo pipefail

APP_USER="${SUDO_USER:-$USER}"
APP_HOME="$(eval echo ~"$APP_USER")"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

bold "==> VedaVine setup"
echo "    User:   $APP_USER"
echo "    Dir:    $APP_DIR"

# 1. APT packages
bold "==> Installing system packages"
DEBIAN_FRONTEND=noninteractive sudo apt-get update
DEBIAN_FRONTEND=noninteractive sudo apt-get install -y \
  python3-pip python3-venv avahi-daemon curl

sudo systemctl enable --now avahi-daemon

# 2. Virtualenv + Python deps
bold "==> Creating virtualenv"
if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 3. Config
bold "==> Checking config.yaml"
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cp "$APP_DIR/config.yaml.template" "$APP_DIR/config.yaml"
  warn "------------------------------------------------------------"
  warn "  config.yaml was missing. A copy of config.yaml.template was"
  warn "  installed. EDIT IT NOW with your vineyard details and a"
  warn "  random flask.secret_key, then re-run setup.sh."
  warn "------------------------------------------------------------"
  exit 1
fi
if grep -q 'change-this-to-a-random-string' "$APP_DIR/config.yaml"; then
  fail "config.yaml still has the placeholder secret_key. Replace it before continuing."
fi

mkdir -p "$APP_DIR/uploads"
if [ ! -f "$APP_DIR/static/logo.png" ]; then
  warn "static/logo.png is missing — the page will render without a logo."
fi

# 4. Read model from config
MODEL=$(awk '/^ollama:/,0' "$APP_DIR/config.yaml" | awk -F'"' '/model:/ {print $2; exit}')
if [ -z "$MODEL" ]; then
  fail "Could not read ollama.model from config.yaml"
fi

# 5. Ollama
bold "==> Ensuring Ollama is installed"
if ! command -v ollama >/dev/null 2>&1; then
  echo "    Ollama not found, installing..."
  if ! curl -fsSL https://ollama.ai/install.sh | sh; then
    fail "Ollama installation failed. Check your network and try again."
  fi
fi
sudo systemctl enable --now ollama

bold "==> Configuring Ollama keep-alive override"
OLLAMA_OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
OLLAMA_OVERRIDE_PATH="$OLLAMA_OVERRIDE_DIR/override.conf"
sudo mkdir -p "$OLLAMA_OVERRIDE_DIR"
TMP_OVERRIDE=$(mktemp)
cat >"$TMP_OVERRIDE" <<'EOF'
[Service]
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF
if ! sudo cmp -s "$TMP_OVERRIDE" "$OLLAMA_OVERRIDE_PATH" 2>/dev/null; then
  sudo install -m 0644 "$TMP_OVERRIDE" "$OLLAMA_OVERRIDE_PATH"
  sudo systemctl daemon-reload
  sudo systemctl restart ollama
fi
rm -f "$TMP_OVERRIDE"

bold "==> Pulling model: $MODEL"
ollama pull "$MODEL"

bold "==> Warming up the model"
ollama run "$MODEL" "ready" >/dev/null || warn "Warmup run failed — first request may be slow."

# 6. Hostname → vedavine.local
bold "==> Setting hostname to 'vedavine'"
CURRENT_HOST=$(hostnamectl --static 2>/dev/null || hostname)
if [ "$CURRENT_HOST" != "vedavine" ]; then
  sudo hostnamectl set-hostname vedavine
fi
if ! grep -qE '^\s*127\.0\.1\.1\s+vedavine' /etc/hosts; then
  echo "127.0.1.1 vedavine" | sudo tee -a /etc/hosts >/dev/null
fi

# 7. systemd unit
bold "==> Installing systemd service"
SERVICE_PATH="/etc/systemd/system/vedavine.service"
TMP_UNIT=$(mktemp)
cat >"$TMP_UNIT" <<EOF
[Unit]
Description=VedaVine Vineyard Advisor
After=network.target ollama.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn -b 0.0.0.0:5000 -w 1 -t 900 app:app
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

if ! sudo cmp -s "$TMP_UNIT" "$SERVICE_PATH" 2>/dev/null; then
  sudo install -m 0644 "$TMP_UNIT" "$SERVICE_PATH"
  sudo systemctl daemon-reload
fi
rm -f "$TMP_UNIT"

sudo systemctl enable --now vedavine
sudo systemctl restart vedavine

# 8. Cloudflare Tunnel (optional remote access on vedavine.app)
# Exposes localhost:5000 over the tunnel. Inference stays on the Pi; only the
# HTTP request/response traverse Cloudflare's edge (TLS). Put a Cloudflare
# Access policy in front in the Zero Trust dashboard so /analyze isn't public.
#
# Token comes from $CLOUDFLARE_TUNNEL_TOKEN or $APP_DIR/cloudflared.token
# (gitignored). Create the tunnel in the dashboard:
#   Zero Trust -> Networks -> Tunnels -> Create -> copy the connector token,
#   then add a Public Hostname: vedavine.app -> HTTP -> localhost:5000.
bold "==> Cloudflare Tunnel (remote access)"
CF_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-}"
if [ -z "$CF_TOKEN" ] && [ -f "$APP_DIR/cloudflared.token" ]; then
  CF_TOKEN="$(tr -d '[:space:]' < "$APP_DIR/cloudflared.token")"
fi

if [ -z "$CF_TOKEN" ]; then
  warn "  No tunnel token found — skipping remote access."
  warn "  To enable: create a tunnel in the Cloudflare Zero Trust dashboard,"
  warn "  save the connector token to $APP_DIR/cloudflared.token, re-run setup.sh."
else
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "    Installing cloudflared..."
    sudo mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
      | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    DEBIAN_FRONTEND=noninteractive sudo apt-get update
    DEBIAN_FRONTEND=noninteractive sudo apt-get install -y cloudflared
  fi
  # Token-based connector runs as the 'cloudflared' systemd service.
  # `service install` is not idempotent, so reinstall cleanly to pick up
  # token changes.
  if systemctl list-unit-files 2>/dev/null | grep -q '^cloudflared\.service'; then
    sudo cloudflared service uninstall >/dev/null 2>&1 || true
  fi
  sudo cloudflared service install "$CF_TOKEN"
  sudo systemctl enable --now cloudflared
  echo "    cloudflared connector installed and running."
fi

bold "==> Done"
echo
echo "  VedaVine is live at: http://vedavine.local:5000"
echo "  (keep your iPhone on the same Wi-Fi)"
if [ -n "${CF_TOKEN:-}" ]; then
  echo "  Remote:              https://vedavine.app  (behind Cloudflare Access)"
  echo "  Tunnel status:       sudo systemctl status cloudflared"
fi
echo
echo "  Service status: sudo systemctl status vedavine"
echo "  Service logs:   journalctl -u vedavine -f"
