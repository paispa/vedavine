# VedaVine

**A holistic AI viticulture advisor that runs entirely on your Raspberry Pi.**

Snap a photo of a vine with your iPhone, open `vedavine.local` in Safari,
and get a structured assessment — severity, observations, recommendations —
from Gemma 4 E2B running locally via Ollama. No cloud, no API keys, no
photos leaving your device.

Built for the [DEV.to Gemma 4 Challenge](https://dev.to/).

![VedaVine screenshot placeholder](docs/screenshot.png)

## Why local-only

A vineyard is a private place. The vines are your trade, the photos
incidentally capture your land, and the assessments are commercially
sensitive. VedaVine never sends any of that anywhere — the only network
call the app makes is to `localhost:11434`, your own Ollama instance.

## Hardware

- Raspberry Pi 5, 8GB RAM (4GB works for smaller models — `gemma4:e2b`
  benefits from 8GB headroom)
- microSD card, 32GB+
- Pi power supply
- Same Wi-Fi network as your iPhone

## Quick start

On the Pi:

```bash
git clone <this repo> ~/vedavine
cd ~/vedavine
cp config.yaml.template config.yaml
# Edit config.yaml — fill in your vineyard details and a random secret_key
bash setup.sh
```

The setup script is idempotent. It will:

1. Install `python3-venv`, `avahi-daemon`, and other apt packages.
2. Create a virtualenv and install Python deps.
3. Install Ollama (if missing), pull the model, and run a warmup inference.
4. Set the Pi hostname to `vedavine` so it answers on `vedavine.local`.
5. Install a systemd unit so VedaVine starts on boot.

When it finishes, open **`http://vedavine.local:5000`** on your iPhone and
upload a vine photo.

## Local development (no Pi)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.yaml.template config.yaml
# edit config.yaml
ollama serve &           # in another terminal
ollama pull gemma4:e2b
python app.py
```

Then open `http://localhost:5000`.

## How Gemma 4 E2B is used

Each upload follows the same pipeline:

1. **Receive** the file via `POST /analyze` (max 10MB, JPEG/PNG/HEIC/WebP).
2. **Downscale** to a 1024px max edge with Pillow + `pillow-heif`. This is
   the difference between a 5-second and a 50-second inference on a Pi 5.
3. **Send** the resized image as base64 to `localhost:11434/api/generate`
   with a system prompt built from your `config.yaml` vineyard details.
4. **Parse** Gemma's reply into a strict JSON schema:
   ```json
   {
     "severity": "Healthy" | "Monitor" | "Attention Needed",
     "summary": "...",
     "observations": ["..."],
     "recommendations": ["..."]
   }
   ```
5. **Render** the result with a colored severity badge and append it to
   the in-memory history (last 20 assessments).

Markdown-fenced and commentary-wrapped responses from Gemma are handled
gracefully — the parser strips fences and pulls the JSON object out of any
surrounding chatter.

## Tests

```bash
python -m unittest test_app.py
```

Covers the response parser and the image downscaler. Doesn't require a live
Ollama instance.

## License

MIT — see [LICENSE](LICENSE).

## Article

📝 DEV.to write-up: _coming soon_
