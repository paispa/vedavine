# VedaVine — Contributor Notes

VedaVine is a Raspberry Pi 5 web app that gives a vineyard owner a holistic
viticulture assessment from a vine photo. Inference runs locally on the Pi
through Ollama with Gemma 4 E2B. No images or text leave the device.

## Current state (built for the DEV.to Gemma 4 Challenge)

- All files from the original plan are in place. The build is complete and
  the unit tests pass (`python -m unittest test_app.py` — 8/8).
- `config.yaml` has been filled in for **Free Run Cellars, Lake Michigan AVA,
  Berrien Springs, MI** — Pinot Gris on 2.5 acres. The `secret_key` is a real
  random value (not the placeholder), so the app will boot.
- `.venv/` exists in the project root (Python 3.9 on macOS host) with all
  `requirements.txt` deps installed. Use `.venv/bin/python app.py` to run
  locally; you still need an Ollama instance with `gemma4:e2b` pulled.
- Nothing has been deployed to a real Pi yet. `setup.sh` is written but has
  not been executed end-to-end.
- No git repo exists yet — `is a git repository: false` at session start.
  When you `git init`, double-check that `config.yaml`, `CLAUDE.local.md`,
  and `uploads/` show up as untracked (they're in `.gitignore`).

## Stack

- **Backend:** Flask served by Gunicorn (in production via systemd).
- **Model host:** Ollama, talking HTTP on `localhost:11434`.
- **Default model:** `gemma4:e2b`.
- **Frontend:** single Jinja template + vanilla JS. No build step.
- **Image handling:** Pillow + `pillow-heif` so iPhone HEIC uploads work.

## Running locally (without a Pi)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.template config.yaml
# edit config.yaml — set vineyard details and a real flask.secret_key
python app.py
```

You also need Ollama running locally with the model pulled:

```bash
ollama serve &
ollama pull gemma4:e2b
```

Then open `http://localhost:5000`.

## Config

Everything user-specific lives in `config.yaml` (gitignored). The committed
`config.yaml.template` defines the schema. The app **hard-fails on startup**
if `config.yaml` is missing or if `flask.secret_key` is still the placeholder
— this is intentional, so a misconfigured deploy doesn't silently run with a
known secret.

Sections:

- `vineyard.*` — interpolated into the system prompt sent to Gemma.
- `flask.secret_key` — must be replaced with a random string.
- `ollama.host` / `ollama.model` / `ollama.timeout` — model endpoint.

`app.py` reads the config path from the `VEDAVINE_CONFIG` env var if set,
otherwise from `./config.yaml`. This indirection exists so `test_app.py` can
point at a temporary config without clobbering the real one — keep this
contract intact if you refactor config loading.

## How the Ollama integration works

1. `POST /analyze` receives a multipart upload.
2. `preprocess_image` opens it (HEIC supported), converts to RGB, downscales
   to a max edge of 1024px, re-encodes as JPEG (quality 85). This keeps the
   request body around ~200–300 KB and is the single biggest win for Pi-side
   inference latency.
3. The image is base64-encoded and sent to `POST {ollama.host}/api/generate`
   with the system prompt built from `config.vineyard` and a fixed user
   prompt asking for JSON-only output.
4. `parse_ollama_response` strips markdown fences, falls back to the largest
   `{...}` substring if there's surrounding commentary, validates `severity`
   against the allowed set, and coerces list items to strings. If nothing
   parses, it returns a `Monitor` placeholder with the raw text as the
   summary so the user always sees *something*.
5. The result is appended to an in-memory `history` list (newest-first,
   capped at 20). History is intentionally not persisted — restart wipes it.

## Where to add things

- **New routes:** add to `app.py`. Keep them stateless — the only mutable
  state is the `history` list.
- **Prompt tuning:** `SYSTEM_PROMPT_TEMPLATE` in `app.py`. Vineyard fields
  are formatted in via `build_system_prompt()`.
- **New severity tiers:** update the allowed set in `parse_ollama_response`,
  the system prompt, and add a CSS class in `static/style.css`. The
  frontend's `severityClass()` helper also maps the label to a class.
- **Frontend tweaks:** `templates/index.html` (markup + inline JS) and
  `static/style.css`. There is no JS framework on purpose — keep it that way.

## Testing

```bash
python -m unittest test_app.py
```

Tests cover the response parser and the image downscaler — i.e. the pieces
that don't need a live Ollama. Treat any new parsing branch as a new test.

## Privacy invariant

Nothing should ever send the user's photos or the model output to a remote
service. The `requests` import is used **only** for `localhost:11434`. If
you find yourself reaching for any other outbound call, stop and reconsider.

## Non-obvious decisions (don't undo without reason)

- **Hard-fail on missing config / placeholder secret_key.** Intentional.
  A misconfigured deploy must not silently boot with a known-public secret.
  Don't soften this to a warning.
- **1024px max edge for images.** This is the single biggest perf knob for
  Pi-side inference (5s vs 50s+). If you raise it, benchmark on a real Pi.
- **In-memory history, capped at 20.** Restart wipes it. Persistence was
  considered and deferred — if you add SQLite, do it behind the same
  history list interface so the route handler doesn't change.
- **No JS framework.** Single inline `<script>` block in `index.html`.
  Keep it that way; the whole point is "Pi can serve this from a flat
  file with zero build step."
- **Defensive JSON parser.** Gemma sometimes wraps output in ```json
  fences or trails commentary. The parser strips fences, then falls back
  to the largest `{...}` substring, then to a `Monitor`-severity placeholder
  with the raw text. Every parse branch has a test — add a test if you
  add a branch.
- **`severity` allowed set: Healthy / Monitor / Attention Needed.**
  Hardcoded in the system prompt, the parser's validation, and the CSS.
  Adding a tier means updating all three plus the JS `severityClass()`
  helper.
- **Test isolation via `VEDAVINE_CONFIG`.** `test_app.py` writes a temp
  config and points the env var at it before importing `app`. This avoids
  the previous bug where the test bootstrap rewrote the user's real
  `config.yaml`.

## Verification commands that worked in the last session

```bash
# Syntax checks
python3 -c "import ast; ast.parse(open('app.py').read())"
bash -n setup.sh

# Unit tests (8/8 pass, no Ollama needed)
.venv/bin/python -m unittest test_app.py

# Confirm hard-fail on placeholder secret
VEDAVINE_CONFIG=/path/to/template-shaped/config.yaml .venv/bin/python app.py
# → exits non-zero, stderr says "still the placeholder"
```

## Open questions / deferred work

- **Persistent history** — currently in-memory. SQLite was discussed.
- **Multi-vine sessions / grouping by date or block** — not implemented.
- **Post-hoc severity rules** — e.g. "always escalate if observations
  mention 'mildew'." Not implemented; would live just after
  `parse_ollama_response` in `app.py`.
- **DEV.to article + screenshot** — README has a placeholder reference
  to `docs/screenshot.png` that doesn't exist yet.
- **Real-Pi end-to-end run** — `setup.sh` has been syntax-checked but
  not executed on hardware.
