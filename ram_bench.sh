#!/usr/bin/env bash
# One-time RAM benchmark: can the 8 GB Pi hold nomic-embed-text + gemma4:e2b
# resident at once (OLLAMA_MAX_LOADED_MODELS=2)? Run with sudo:
#   sudo bash /tmp/ram_bench.sh
# Backs up the current Ollama override; prints a revert command at the end.
set -euo pipefail

OVR=/etc/systemd/system/ollama.service.d/override.conf
BAK=/tmp/ollama-override.bak.$(date +%s)
HOST=http://localhost:11434
log() { printf '\n\033[1m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

log "Backing up current override -> $BAK"
cp "$OVR" "$BAK"
echo "  current setting:"; grep MAX_LOADED "$OVR" || true

log "Setting OLLAMA_MAX_LOADED_MODELS=2"
sed -i 's/OLLAMA_MAX_LOADED_MODELS=1/OLLAMA_MAX_LOADED_MODELS=2/' "$OVR"
grep MAX_LOADED "$OVR"

log "Reloading + restarting ollama (this UNLOADS models; gemma cold-loads next)"
systemctl daemon-reload
systemctl restart ollama
for i in $(seq 1 30); do curl -fsS -m 3 "$HOST/api/tags" >/dev/null 2>&1 && break; sleep 1; done

log "Loading nomic-embed-text (embedder)..."
t=$SECONDS
curl -fsS -m 120 "$HOST/api/embeddings" -d '{"model":"nomic-embed-text","prompt":"vine health"}' >/dev/null
echo "  nomic loaded in $((SECONDS-t))s"

log "Loading gemma4:e2b (cold load of 7.2 GB — expect several minutes)..."
t=$SECONDS
curl -fsS -m 900 "$HOST/api/generate" -d '{"model":"gemma4:e2b","prompt":"hi","stream":false,"think":false,"options":{"num_predict":1}}' >/dev/null
echo "  gemma loaded in $((SECONDS-t))s"

log "=== BOTH MODELS RESIDENT — snapshot ==="
ollama ps
echo; free -h
echo; echo "swap:"; cat /proc/swaps

log "Steady-state cost with both loaded (this is the per-request dynamic-RAG overhead):"
t=$SECONDS
curl -fsS -m 120 "$HOST/api/embeddings" -d '{"model":"nomic-embed-text","prompt":"downy mildew on grape leaves"}' >/dev/null
echo "  embed call: $((SECONDS-t))s"
t=$SECONDS
curl -fsS -m 900 "$HOST/api/generate" -d '{"model":"gemma4:e2b","prompt":"Say OK.","stream":false,"think":false,"options":{"num_predict":8}}' >/dev/null
echo "  short gemma generate (warm): $((SECONDS-t))s"

log "=== VERDICT INPUTS ==="
echo "  If both models show in 'ollama ps' and 'available' RAM stayed > ~100 MiB"
echo "  and the warm generate was in the tens of seconds (not minutes), MAX=2 is viable."
echo "  If gemma got evicted (only nomic in ps) or generate took minutes, it's thrashing."
echo
echo "  Override is currently MAX=2. To REVERT to MAX=1:"
echo "    sudo cp $BAK $OVR && sudo systemctl daemon-reload && sudo systemctl restart ollama"
