# VedaVine

[![CI](https://github.com/paispa/vedavine/actions/workflows/ci.yml/badge.svg)](https://github.com/paispa/vedavine/actions/workflows/ci.yml)

**A holistic AI viticulture advisor that runs entirely on your Raspberry Pi.**

Snap a photo of a vine, open the app, and get a structured assessment —
severity, observations, recommendations — from **Gemma 4 E2B** running locally
via Ollama. The advice is grounded in a local viticulture corpus (RAG) and
current local weather, and it never depends on a third-party AI service.

Built for the [DEV.to Gemma 4 Challenge](https://dev.to/devteam/join-the-gemma-4-challenge-3000-prize-pool-for-ten-winners-23in).

## What it does

1. You upload a vine photo (and optionally type a concern, e.g. *"yellowing
   leaf edges — mildew or nutrient?"*).
2. Gemma 4 assesses the image, **grounded** in:
   - **Retrieved passages** from a local viticulture corpus (Vrikshayurveda,
     ATTRA guides, SARE, NRCS soil biology, …) via on-device embeddings.
   - **Current weather** for the vineyard (humidity / rain → disease pressure).
3. You get back strict JSON — severity, summary, observations, recommendations
   — rendered with a colored severity badge.

All model inference runs **on the Pi**. The only outbound calls are an optional
weather lookup (US NWS, sends a location, never your photo) and an optional
Cloudflare tunnel for remote access — see [Privacy](#privacy).

## Features

- 🔒 **On-device Gemma 4 inference** via Ollama — no third-party AI sees your data.
- 📚 **RAG grounding** over a viticulture corpus (`all-minilm` embeddings +
  `sqlite-vec`). Retrieval is **dynamic** (driven by your typed concern) or
  static (the vineyard's profile).
- 🌦️ **Weather grounding** from the US National Weather Service.
- 🎚️ **Adjustable analysis depth** (Quick / Balanced / Thorough) trading speed
  for grounding richness.
- 🍇 **Per-grower context** — optional varietal / region / ZIP so the advisor
  can be shared with growers elsewhere.
- 🌐 **Optional remote access** via Cloudflare Tunnel + Access (email-gated).
- 🧱 **Defensive JSON parsing** — handles Gemma's markdown fences and
  surrounding commentary, with a graceful placeholder fallback.

## Hardware

- Raspberry Pi 5, **8 GB RAM** (Gemma 4 E2B is ~7.7 GB loaded; 8 GB is the
  practical floor)
- microSD card, 32 GB+
- Pi power supply

## Quick start (on the Pi)

```bash
git clone https://github.com/paispa/vedavine.git ~/vedavine
cd ~/vedavine
cp config.yaml.template config.yaml
# Edit config.yaml — vineyard details, a random flask.secret_key, and
# (optional) vineyard lat/lon for weather.
bash setup.sh
```

`setup.sh` is idempotent. It installs system packages, creates a venv, installs
Ollama and pulls the model, sets the hostname to `vedavine`, installs a systemd
unit, and (optionally) registers a Cloudflare tunnel if a token is present.

Then open **`http://vedavine.local:5000`** and upload a vine photo.

### Build the RAG index

Drop viticulture PDFs into `corpus/`, then on the Pi:

```bash
source venv/bin/activate
python build_index.py        # embeds corpus -> rag.db (sqlite-vec)
python retrieve.py "downy mildew on grape leaves"   # sanity-check retrieval
```

The embedder is configurable (`rag.embed_model`); the chunk dimension is
auto-detected, so swapping models needs no code change (rebuild the index).

## Local development (no Pi)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.template config.yaml   # edit it
ollama serve & ollama pull gemma4:e2b
python app.py                          # http://localhost:5000
```

RAG and weather fail soft — if `rag.db` or the network is unavailable, the app
still runs, just without that grounding.

## How it works

`POST /analyze` (multipart):

1. **Preprocess** — open (HEIC/JPEG/PNG/WebP), downscale to a 1024 px max edge,
   re-encode JPEG q85. This is the single biggest Pi-side latency win.
2. **Retrieve** — embed the concern (or vineyard profile) with `all-minilm`,
   KNN over `rag.db`, take the top *k* passages.
3. **Weather** — fetch + cache the NWS forecast for the location.
4. **Prompt** — build the system prompt from vineyard config + weather +
   retrieved passages, and send the image to `localhost:11434/api/generate`
   (`think: false`).
5. **Parse** into the schema:
   ```json
   {
     "severity": "Healthy" | "Monitor" | "Attention Needed",
     "summary": "...",
     "observations": ["..."],
     "recommendations": ["..."]
   }
   ```

## Privacy

VedaVine's defining property is that **the AI runs on your own hardware** — no
photo or assessment is ever sent to a third-party model service. Two optional,
clearly-scoped exceptions:

- **Weather:** if enabled, the app fetches a forecast from the US NWS using the
  vineyard's coordinates. It sends a location, never your image.
- **Remote access:** if you expose the app via Cloudflare Tunnel, requests
  (including uploads) traverse Cloudflare's edge over TLS to reach your Pi.
  Inference still happens only on the Pi. Cloudflare Access gates it to
  approved emails.

Both are off by default in `config.yaml.template`.

## Configuration

Everything user-specific lives in `config.yaml` (gitignored;
`config.yaml.template` defines the schema). The app **hard-fails on startup**
if the file is missing or `flask.secret_key` is still the placeholder.

## Tests

```bash
python -m unittest test_app.py
```

Covers the JSON parser, the image downscaler, prompt construction, and the
RAG/weather fail-soft paths. No live Ollama required.

## License

MIT — see [LICENSE](LICENSE).

## Article

📝 DEV.to write-up: [I taught a Raspberry Pi to read my vines with Gemma 4](https://dev.to/ppais/i-taught-a-raspberry-pi-to-read-my-vines-with-gemma-4-206k)
