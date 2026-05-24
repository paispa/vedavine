# VedaVine — Contributor Notes

VedaVine is a Raspberry Pi 5 web app that gives a vineyard owner a holistic
viticulture assessment from a vine photo. Inference runs locally on the Pi
through Ollama with Gemma 4 E2B, grounded in a local viticulture corpus
(RAG) and the current US NWS weather forecast.

- **Live:** https://vedavine.app (Cloudflare Tunnel + Access, email-gated).
- **Public repo:** https://github.com/paispa/vedavine
- **DEV.to write-up:** https://dev.to/ppais/i-taught-a-raspberry-pi-to-read-my-vines-with-gemma-4-206k

## Current state

- Production-deployed on an 8 GB Raspberry Pi 5 at `frc@192.168.5.70` (the
  vineyard's LAN). Service runs as a `vedavine` systemd unit.
- All unit tests pass: `python -m unittest test_app.py` — **19/19**.
- `config.yaml` is filled in for Free Run Cellars (Pinot Gris, Lake Michigan
  AVA, Berrien Springs MI) with a real `flask.secret_key`.
- `.venv/` exists in the project root (Python 3.9 on the macOS host) with
  the full `requirements.txt` installed. Locally: `.venv/bin/python app.py`.
- Code on GitHub matches the deployed Pi.

## Stack

- **Backend:** Flask served by Gunicorn (single worker, 15-min timeout)
  under systemd.
- **Model host:** Ollama on `localhost:11434`. `OLLAMA_KEEP_ALIVE=24h`,
  `OLLAMA_MAX_LOADED_MODELS=2`.
- **Vision/reasoning model:** `gemma4:e2b` (~7.7 GB loaded). `think: false`
  in the API payload — cuts text-generation ~3× (251s → 86s) with no JSON
  quality loss.
- **Embedder:** `all-minilm` (~73 MB loaded; 384-dim; ~256-token cap).
  Coexists with Gemma resident under `MAX_LOADED_MODELS=2`.
- **RAG index:** `sqlite-vec` at `~/vedavine/rag.db` (Pi-built, gitignored,
  ~30 min to rebuild). 1,385 chunks from ATTRA / SARE / NRCS / Vrikshayurveda
  open corpus.
- **Weather:** US NWS forecast (`api.weather.gov`), ZIP geocoded via
  `zippopotam.us` for the per-grower override path (US only).
- **Remote access:** `cloudflared` systemd service on the Pi → Cloudflare
  Tunnel → `vedavine.app`. Cloudflare Access gates to an email allow-list.
- **Frontend:** single Jinja template + vanilla JS. No build step.
- **Image handling:** Pillow + `pillow-heif` (iPhone HEIC works).

## Running locally (without a Pi)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.template config.yaml   # edit vineyard details + secret_key
ollama serve & ollama pull gemma4:e2b
python app.py                          # http://localhost:5000
```

RAG and weather fail soft — if `rag.db` is missing or NWS is unreachable
the app still serves, just without that grounding.

## Config

Everything user-specific lives in `config.yaml` (gitignored). The committed
`config.yaml.template` defines the schema. The app **hard-fails on startup**
if `config.yaml` is missing or `flask.secret_key` is still the placeholder
— intentional, so a misconfigured deploy doesn't boot with a known secret.

Sections:

- `vineyard.*` — interpolated into the system prompt.
- `flask.secret_key` — must be a real random string.
- `ollama.host` / `ollama.model` / `ollama.timeout` (900s).
- `rag.*` — `enabled`, `dynamic` (concern-driven retrieval), `k` (default 3),
  `embed_model` (`all-minilm`), `max_chars_per_chunk`.
- `weather.*` — `enabled`, `lat`, `lon`, `cache_ttl`.

`app.py` reads the config path from `VEDAVINE_CONFIG` if set, otherwise
`./config.yaml`. **Keep this contract intact** — `test_app.py` relies on it
to point at a temp config, and the previous bug rewrote the user's real one.

## Request flow

`/analyze` is **async** because Cloudflare's free-tier edge drops HTTP at
~100s but Gemma routinely takes 3–7 min:

1. **POST `/analyze`** receives the multipart upload, validates, preprocesses,
   spawns a `threading.Thread` worker, returns
   `{"job_id": "...", "status": "running"}` with HTTP 202.
2. **Background worker** (`_run_analysis_job`): builds the system prompt
   from `config.vineyard` + (optional per-grower override) + retrieved RAG
   passages + current weather, base64-encodes the JPEG, calls
   `localhost:11434/api/generate` with `think: false`.
3. **`parse_ollama_response`** strips ` ```json ` fences, falls back to the
   largest `{...}` substring, validates `severity`, coerces list items to
   strings; on total parse failure returns a `Monitor` placeholder with the
   raw text as the summary.
4. **GET `/result/<job_id>`** returns `{"status": "running" | "done" |
   "error"}`. The frontend polls every 4s.
5. Jobs older than `JOB_TTL_SECONDS` (1800s) are pruned. State is in-memory,
   keyed under `_jobs_lock`. **Single Gunicorn worker is required** — the
   job dict isn't shared across processes.

`preprocess_image` downscales to 1024 px max edge, re-encodes JPEG q85 —
keeps the body around 200–300 KB; biggest perf win.

History is in-memory, newest-first, capped at 20. Wiped on restart.

## Where to add things

- **New routes:** `app.py`. State must stay in-process (the `_jobs` dict
  isn't multi-worker safe; that's why Gunicorn is `-w 1`).
- **Prompt tuning:** `SYSTEM_PROMPT_TEMPLATE` in `app.py`. Vineyard fields
  flow in via `build_system_prompt()`; RAG passages and weather are appended
  before the JSON-only instruction.
- **New severity tiers:** four places — `parse_ollama_response` allowed set,
  the system prompt template, `static/style.css` (color), the JS
  `severityClass()` helper.
- **Frontend tweaks:** `templates/index.html` (markup + inline JS) and
  `static/style.css`. No JS framework on purpose — keep it that way.
- **New corpus material:** drop PDFs into `corpus/` on the Pi, then
  `python build_index.py`. The index is Pi-built; protected from rsync
  deploys by `--exclude='rag.db'`/`--exclude='corpus'` in both `deploy.sh`
  and `pi.sh`.

## Dev loop tooling

- **`deploy.sh`** — full deploy (rsync + restart + healthz).
- **`pi.sh`** — fast iteration alongside `deploy.sh`:
  - `push` — fast rsync, no restart
  - `deploy` — push + restart + healthz
  - `smoke [file]` — real `/analyze` (async submit + poll) against a Pi-side
    image, prints severity / elapsed / summary / cited sources
  - `logs`, `health`, `restart`, `watch`
- Both honor `VEDAVINE_PI_USER` / `VEDAVINE_PI_HOST` (default
  `frc@192.168.5.70`).

## Testing

```bash
.venv/bin/python -m unittest test_app.py   # 19/19, no live Ollama needed
```

Covers: defensive JSON parser, image downscaler, system prompt construction,
RAG/weather fail-soft paths, per-grower override, async job submit + poll
(worker monkey-patched). Treat any new parsing branch or job-state branch
as a new test.

## Privacy invariant (stated precisely)

The defining property is that **AI inference runs only on the Pi** — no
photo or assessment is sent to a third-party model service. Two optional,
clearly-scoped exceptions, both off by default in the template:

- **Weather:** sends lat/lon (or ZIP for per-grower) to `api.weather.gov` /
  `zippopotam.us`. Never sends the image.
- **Remote access via Cloudflare Tunnel:** when enabled, requests (including
  uploads) traverse Cloudflare's edge over TLS to reach the Pi. Inference
  still happens only on the Pi. Cloudflare Access gates to approved emails.

If you add any other outbound call, stop and reconsider — the contract is
that *inference* is on-device, not that nothing leaves the box.

## Non-obvious decisions (don't undo without reason)

- **Hard-fail on missing config / placeholder secret_key.** A misconfigured
  deploy must not silently boot with a known-public secret.
- **1024 px max edge for images.** Single biggest Pi-side perf knob.
  Benchmark on a real Pi before raising.
- **`think: false` in the Ollama payload.** ~3× speedup on text-generation
  with no measurable JSON quality loss. Big win — keep it.
- **`all-minilm` embedder (not nomic).** `nomic-embed-text` (274 MB) gets
  evicted by Gemma under `MAX_LOADED_MODELS=2`; `all-minilm` (73 MB)
  coexists. Gotcha: 256-token silent cap → `CHUNK_CHARS=800` +
  truncate-on-overflow retry in `build_index.embed()`.
- **In-memory history, capped at 20.** Restart wipes it. Persistence
  considered and deferred; if added, do it behind the same list interface
  so the route handler doesn't change.
- **No JS framework.** Single inline `<script>` in `index.html`. The whole
  point is "Pi serves this from a flat file with zero build step."
- **Async `/analyze` + `/result/<job_id>` polling.** Required because
  Cloudflare Free's edge drops HTTP at ~100s and inference is 3–7 min.
  Each individual HTTP hop now finishes in under a second.
- **Single Gunicorn worker (`-w 1`).** Job state lives in an in-process
  dict; multi-worker would split it. Don't bump `-w` without rewriting the
  job store.
- **Defensive JSON parser.** Gemma sometimes wraps output in ` ```json `
  fences or trails commentary. Every parse branch has a test — add one if
  you add a branch.
- **`severity` allowed set: Healthy / Monitor / Attention Needed.**
  Hardcoded in four places (prompt template, parser validation, CSS,
  JS `severityClass()`). Adding a tier means updating all four.
- **Test isolation via `VEDAVINE_CONFIG`.** `test_app.py` writes a temp
  config + sets the env var before importing `app`. Avoids the previous
  bug where the test bootstrap rewrote the user's real `config.yaml`.
- **RAG depth dial (Quick / Balanced / Thorough = k=2/3/4).** Trades
  latency for grounding richness — k=3 is ~322s, k=4 is ~430s on the Pi.
  Honest user-facing tradeoff, exposed in the UI on purpose.
- **`rag.db` and `corpus/` are rsync-excluded.** Both `deploy.sh` and
  `pi.sh` exclude them; Mac has no copy. Earlier (2026-05-10) a deploy
  wiped the Pi's index — losing ~30 min of rebuild — because of a missing
  exclude. Don't drop it.

## Verification commands that worked last session

```bash
# Syntax checks
python3 -c "import ast; ast.parse(open('app.py').read())"
bash -n setup.sh

# Unit tests (19/19, no Ollama needed)
.venv/bin/python -m unittest test_app.py

# Confirm hard-fail on placeholder secret
VEDAVINE_CONFIG=/path/to/template-shaped/config.yaml .venv/bin/python app.py
# → exits non-zero, stderr says "still the placeholder"

# Real end-to-end on the Pi (async submit + poll)
./pi.sh smoke   # uses newest image in ~/vedavine/uploads/ on the Pi
```

## Open questions / deferred work

- **Persistent history** — currently in-memory. SQLite was considered.
- **Multi-vine sessions / grouping by date or block** — not implemented.
- **Post-hoc severity rules** — e.g. "always escalate if observations
  mention 'mildew'." Would live just after `parse_ollama_response`.
- **Phase 2 second Pi** — pipeline-parallelism plan written
  (see `project_vedavine_phase2_plan` in memory); deferred indefinitely
  after `think: false` collapsed the latency case.
- **`cloudflared.token` not yet committed for reflash-reproducibility** —
  lives only on the deployed Pi today.
- **In-app `/analyze` busy guard** — when an inference is in flight, a
  second request currently hangs up to 900s instead of failing fast.
