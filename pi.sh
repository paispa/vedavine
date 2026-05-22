#!/usr/bin/env bash
# VedaVine — Pi dev loop helper.  Faster than deploy.sh for iterating.
#
#   ./pi.sh push            fast rsync (no restart) — for standalone scripts
#   ./pi.sh deploy          push + restart vedavine + /healthz check
#   ./pi.sh restart         restart service + /healthz check
#   ./pi.sh health          just hit /healthz
#   ./pi.sh smoke [file]    full /analyze against a Pi-side image (newest upload
#                           if no file given); prints severity + elapsed + summary
#   ./pi.sh logs            tail -f the service journal
#   ./pi.sh run "<cmd>"     run a command in the venv on the Pi
#   ./pi.sh watch           auto: on file save -> push -> restart -> /healthz
#
# Honors VEDAVINE_PI_USER / VEDAVINE_PI_HOST (same as deploy.sh).
# Requires SSH key auth to the Pi.

set -euo pipefail

PI_USER="${VEDAVINE_PI_USER:-frc}"
PI_HOST="${VEDAVINE_PI_HOST:-192.168.5.70}"
PORT="${VEDAVINE_PORT:-5000}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DIR="vedavine"
PI="${PI_USER}@${PI_HOST}"

# Pi-built artifacts (rag.db, corpus) and local-only files stay protected.
EXCLUDES=(
  --exclude='.venv' --exclude='venv' --exclude='uploads'
  --exclude='rag.db' --exclude='corpus' --exclude='__pycache__'
  --exclude='*.pyc' --exclude='.git' --exclude='.DS_Store'
  --exclude='CLAUDE.local.md'
)

c_b() { printf '\033[1m%s\033[0m\n' "$*"; }       # bold
c_g() { printf '\033[32m%s\033[0m\n' "$*"; }      # green
c_r() { printf '\033[31m%s\033[0m\n' "$*"; }      # red
c_y() { printf '\033[33m%s\033[0m\n' "$*"; }      # yellow

push() {
  rsync -a --delete-after "${EXCLUDES[@]}" "$APP_DIR/" "${PI}:~/${REMOTE_DIR}/"
}

restart() {
  ssh "$PI" "sudo systemctl restart vedavine"
}

# Poll /healthz until 200 or timeout. Returns nonzero on failure.
health() {
  local url="http://${PI_HOST}:${PORT}/healthz"
  local deadline=$(( SECONDS + ${1:-25} ))
  while (( SECONDS < deadline )); do
    if body="$(curl -fsS -m 5 "$url" 2>/dev/null)"; then
      c_g "  healthz OK  $body"
      return 0
    fi
    sleep 1
  done
  c_r "  healthz FAILED — service did not return 200 in time"
  c_y "  last 20 log lines:"
  ssh "$PI" "sudo journalctl -u vedavine -n 20 --no-pager" || true
  return 1
}

smoke() {
  local img="${1:-}"
  if [[ -z "$img" ]]; then
    c_b "==> Picking newest image in ~/${REMOTE_DIR}/uploads/ on the Pi"
    img="$(ssh "$PI" "ls -t ~/${REMOTE_DIR}/uploads/* 2>/dev/null | head -1")"
    if [[ -z "$img" ]]; then
      c_r "No images in ~/${REMOTE_DIR}/uploads/ on the Pi. Pass a path:  ./pi.sh smoke /home/${PI_USER}/somephoto.jpg"
      return 1
    fi
  fi
  c_b "==> POST /analyze  image=$img  (this runs real inference, ~3-4 min)"
  local started=$SECONDS
  # Run curl on the Pi against localhost so we don't ship the image over the LAN.
  local resp
  if ! resp="$(ssh "$PI" "curl -fsS -m 900 -F image=@'$img' http://localhost:${PORT}/analyze" 2>&1)"; then
    c_r "  request failed:"
    printf '%s\n' "$resp"
    return 1
  fi
  local wall=$(( SECONDS - started ))
  printf '%s' "$resp" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("  non-JSON response:"); print(sys.stdin.read()); sys.exit(1)
if "error" in d:
    print("  ERROR:", d["error"]); sys.exit(1)
sev = d.get("severity", "?")
print(f"  severity:     {sev}")
print(f"  elapsed_s:    {d.get(\"elapsed_s\", \"?\")} (server-measured inference)")
sm = (d.get("summary") or "").strip().replace("\n", " ")
print(f"  summary:      {sm[:200]}")
obs = d.get("observations") or []
recs = d.get("recommendations") or []
print(f"  observations: {len(obs)}   recommendations: {len(recs)}")
fb = (d.get("summary","").startswith("[unparsed]") or sev=="Monitor" and not obs)
print("  NOTE: looks like the parser fallback (placeholder) — check raw output" if fb else "  parsed OK")
' || { c_r "  could not parse response"; printf '%s\n' "$resp"; return 1; }
  c_g "==> smoke done in ${wall}s wall"
}

do_deploy() {
  c_b "==> push -> ${PI}:~/${REMOTE_DIR}/"
  push
  c_b "==> restart vedavine"
  restart
  c_b "==> health check"
  health
}

# --- watch mode -------------------------------------------------------------
# Snapshot of mtimes for the files we actually sync. Portable; no deps.
snapshot() {
  find "$APP_DIR" -type f \
    -not -path '*/.venv/*' -not -path '*/venv/*' -not -path '*/uploads/*' \
    -not -path '*/corpus/*' -not -path '*/.git/*' -not -path '*/__pycache__/*' \
    -not -name '*.pyc' -not -name '.DS_Store' \
    -exec stat -f '%m %N' {} + 2>/dev/null | sort
}

watch() {
  c_b "==> watch mode — auto push + restart + healthz on save.  Ctrl-C to stop."
  if command -v fswatch >/dev/null 2>&1; then
    c_g "    (using fswatch — event-based)"
    do_deploy || true
    fswatch -o -l 0.4 \
      -e '/\.venv/' -e '/venv/' -e '/uploads/' -e '/corpus/' \
      -e '/\.git/' -e '/__pycache__/' -e '\.pyc$' -e '\.DS_Store$' \
      "$APP_DIR" | while read -r _; do
        echo; c_y "── change detected $(date +%H:%M:%S) ──"
        do_deploy || true
      done
  else
    c_y "    (polling every 1s — install fswatch for event-based: brew install fswatch)"
    local prev cur
    prev="$(snapshot)"
    do_deploy || true
    while true; do
      sleep 1
      cur="$(snapshot)"
      if [[ "$cur" != "$prev" ]]; then
        prev="$cur"
        echo; c_y "── change detected $(date +%H:%M:%S) ──"
        do_deploy || true
      fi
    done
  fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  push)    c_b "==> push"; push; c_g "done";;
  deploy)  do_deploy;;
  restart) restart; health;;
  health)  health;;
  smoke)   smoke "${1:-}";;
  logs)    ssh -t "$PI" "sudo journalctl -u vedavine -f -n 40 --no-pager";;
  run)     [[ -n "${1:-}" ]] || { c_r "usage: ./pi.sh run \"<command>\""; exit 2; }
           ssh -t "$PI" "cd ~/${REMOTE_DIR} && source venv/bin/activate && $1";;
  watch)   watch;;
  *) c_b "VedaVine Pi helper"; sed -n '3,20p' "$0"; exit 2;;
esac
