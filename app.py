"""VedaVine — local-only AI viticulture advisor.

Flask app that accepts a vine photo, downscales it, sends it to Gemma 4
running on a local Ollama instance, and returns a structured assessment.
Nothing leaves the device.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from flask import Flask, jsonify, render_template, request
from PIL import Image, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

register_heif_opener()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("VEDAVINE_CONFIG", BASE_DIR / "config.yaml"))
UPLOAD_DIR = BASE_DIR / "uploads"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_EDGE = 1024
HISTORY_LIMIT = 20
ALLOWED_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
}
PLACEHOLDER_SECRET = "change-this-to-a-random-string"

SYSTEM_PROMPT_TEMPLATE = """You are VedaVine, a holistic viticulture advisor for {vineyard_name} in {location}.
Varietal(s): {varietal}.
Additional context from the grower: {notes}

Assess vine health with attention to: leaf color and texture, disease or pest signs,
canopy structure, soil moisture indicators, and overall vigor.
{weather_section}{reference_section}
Respond ONLY with this JSON structure, no other text:
{{
  "severity": "Healthy" | "Monitor" | "Attention Needed",
  "summary": "One sentence assessment",
  "observations": ["observation 1", "observation 2"],
  "recommendations": ["action 1", "action 2"]
}}"""

USER_PROMPT = "Analyze this vineyard image. Respond ONLY with valid JSON."


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        sys.stderr.write(
            "ERROR: config.yaml not found.\n"
            "  cp config.yaml.template config.yaml  and edit your details.\n"
        )
        sys.exit(1)
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    secret = cfg.get("flask", {}).get("secret_key", "")
    if not secret or secret == PLACEHOLDER_SECRET:
        sys.stderr.write(
            "ERROR: flask.secret_key in config.yaml is still the placeholder.\n"
            "  Replace it with a random string before running.\n"
        )
        sys.exit(1)
    return cfg


config = load_config()
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = config["flask"]["secret_key"]
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

history: list[dict[str, Any]] = []

_rag = config.get("rag", {})
RAG_ENABLED = bool(_rag.get("enabled", True))
RAG_DYNAMIC = bool(_rag.get("dynamic", False))
RAG_K = int(_rag.get("k", 4))
RAG_MAX_CHARS = int(_rag.get("max_chars_per_chunk", 600))

# Two retrieval modes:
#  - Static (default): ground on the vineyard profile. Identical every request,
#    so cache it — the embedder loads at most once and never evicts warm Gemma.
#  - Dynamic (rag.dynamic): ground on the grower's typed concern. Varies per
#    request so it can't be cached; safe only when the embedder is small enough
#    to coexist with Gemma (all-minilm + OLLAMA_MAX_LOADED_MODELS=2).
_rag_cache: dict[str, Any] = {"reference": None, "sources": []}

_wx = config.get("weather", {})
WEATHER_ENABLED = bool(_wx.get("enabled", False))
WEATHER_TTL = int(_wx.get("ttl_seconds", 3600))
WEATHER_UA = str(_wx.get("user_agent", "VedaVine/1.0 (local vineyard advisor)"))
# Weather changes slowly; cache it so we don't hit the NWS API every request.
# Weather changes slowly; cache per-location so we don't hit NWS every request.
_weather_cache: dict[tuple, dict[str, Any]] = {}


def get_weather_context(zip_code: str = "") -> str:
    """Cached NWS weather summary, or "" if disabled/unavailable.

    With a ZIP, geocode it (a different grower's location); otherwise use the
    vineyard's configured lat/lon. A bad ZIP yields no weather rather than the
    wrong location's.
    """
    if not WEATHER_ENABLED:
        return ""
    if zip_code:
        from weather import geocode_zip
        coords = geocode_zip(zip_code, WEATHER_UA)
        if not coords:
            return ""
        lat, lon = coords
    else:
        v = config.get("vineyard", {})
        lat, lon = v.get("lat"), v.get("lon")
    if lat is None or lon is None:
        return ""
    key = (round(float(lat), 2), round(float(lon), 2))
    now = time.monotonic()
    cached = _weather_cache.get(key)
    if cached and (now - cached["ts"]) < WEATHER_TTL:
        return cached["block"]
    from weather import weather_context  # local import keeps app import light
    block = weather_context(lat, lon, user_agent=WEATHER_UA)
    if block:  # only cache successes; let failures retry next request
        _weather_cache[key] = {"ts": now, "block": block}
    return block


def build_system_prompt(
    reference: str = "", weather: str = "", vineyard: dict[str, Any] | None = None
) -> str:
    v = vineyard if vineyard is not None else config.get("vineyard", {})
    weather_section = ""
    if weather:
        weather_section = (
            "\nCurrent local weather (factor into disease and pest pressure):\n"
            f"{weather}\n"
        )
    reference_section = ""
    if reference:
        reference_section = (
            "\nReference material from your viticulture library — ground your "
            "observations and recommendations in these sources where they apply:\n"
            f"{reference}\n"
        )
    return SYSTEM_PROMPT_TEMPLATE.format(
        vineyard_name=v.get("name", "this vineyard"),
        location=v.get("location", "an unspecified region"),
        varietal=v.get("varietal", "mixed varietals"),
        notes=v.get("notes", "none"),
        weather_section=weather_section,
        reference_section=reference_section,
    )


def _run_retrieval(query: str, k: int) -> tuple[str, list[dict[str, Any]]]:
    """Embed `query`, fetch top-k corpus chunks, format a reference block.

    Fails soft to ("", []) if the index or embedder is unavailable — a missing
    rag.db must never break /analyze.
    """
    try:
        from retrieve import retrieve  # lazy import: avoids app<->retrieve cycle
        hits = retrieve(query, k=k)
    except Exception as exc:  # noqa: BLE001 — retrieval must never break analysis
        app.logger.warning("RAG retrieval skipped: %s", exc)
        return "", []
    if not hits:
        return "", []
    lines, sources = [], []
    for h in hits:
        text = " ".join(h["text"].split())[:RAG_MAX_CHARS]
        lines.append(f"- ({h['source']} p.{h['page']}) {text}")
        sources.append({"source": h["source"], "page": h["page"]})
    return "\n".join(lines), sources


def _profile_query(vineyard: dict[str, Any] | None = None) -> str:
    v = vineyard if vineyard is not None else config.get("vineyard", {})
    return " ".join(
        str(p) for p in (
            v.get("varietal", "grapevine"),
            "vine health, disease, pest, and management in",
            v.get("location", ""),
            v.get("notes", ""),
        ) if p
    ).strip()


def retrieve_context(
    concern: str = "", vineyard: dict[str, Any] | None = None, k: int | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Corpus passages for prompt grounding.

    `k` (passages to retrieve) defaults to config rag.k but can be set per
    request (the UI "analysis depth" control). Dynamic mode (rag.dynamic + a
    typed concern): retrieve on the concern; varies per request, so not cached.
    Static mode: retrieve on the vineyard profile. Only the default (config)
    profile at default k is cached — an override grower or non-default k is
    fetched fresh. See the _rag_cache comment for the RAM rationale.
    """
    if not RAG_ENABLED:
        return "", []
    k = k or RAG_K

    if RAG_DYNAMIC and concern:
        v = vineyard if vineyard is not None else config.get("vineyard", {})
        query = f"{concern} ({v.get('varietal', 'grapevine')} in {v.get('location', '')})".strip()
        return _run_retrieval(query, k)

    if vineyard is not None:  # override grower, no concern — uncached
        return _run_retrieval(_profile_query(vineyard), k)
    if k == RAG_K and _rag_cache["reference"] is not None:
        return _rag_cache["reference"], _rag_cache["sources"]
    block, sources = _run_retrieval(_profile_query(), k)
    if k == RAG_K and block:
        _rag_cache["reference"], _rag_cache["sources"] = block, sources
    return block, sources


def preprocess_image(raw: bytes) -> bytes:
    """Open (HEIC/JPEG/PNG/etc), downscale to <=1024px edge, return JPEG bytes."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def parse_ollama_response(text: str) -> dict[str, Any]:
    """Coerce a model reply into the VedaVine result schema.

    Gemma sometimes wraps output in ```json fences or trails commentary;
    strip fences and try the largest {...} substring before falling back.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    candidates = [cleaned]
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(cleaned[first : last + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        severity = str(data.get("severity", "Monitor")).strip() or "Monitor"
        if severity not in {"Healthy", "Monitor", "Attention Needed"}:
            severity = "Monitor"
        return {
            "severity": severity,
            "summary": str(data.get("summary", "")).strip(),
            "observations": [str(x) for x in data.get("observations", []) if x],
            "recommendations": [str(x) for x in data.get("recommendations", []) if x],
        }

    return {
        "severity": "Monitor",
        "summary": text.strip()[:400] or "Model returned no parseable output.",
        "observations": [],
        "recommendations": [],
    }


def call_ollama(
    image_b64: str,
    concern: str = "",
    vineyard: dict[str, Any] | None = None,
    zip_code: str = "",
    k: int | None = None,
) -> dict[str, Any]:
    ocfg = config["ollama"]
    reference, sources = retrieve_context(concern, vineyard, k)
    weather = get_weather_context(zip_code)
    user_prompt = USER_PROMPT
    if concern:
        user_prompt += f"\n\nThe grower specifically asks: {concern}"
    payload = {
        "model": ocfg["model"],
        "system": build_system_prompt(reference, weather, vineyard),
        "prompt": user_prompt,
        "images": [image_b64],
        "stream": False,
        "think": False,
    }
    url = ocfg["host"].rstrip("/") + "/api/generate"
    resp = requests.post(url, json=payload, timeout=ocfg.get("timeout", 300))
    resp.raise_for_status()
    body = resp.json()
    result = parse_ollama_response(body.get("response", ""))
    result["sources"] = sources
    return result


@app.route("/", methods=["GET"])
def index():
    # Cloudflare Access injects the authenticated user's email on requests it
    # proxies. Absent when reached directly (local network) — display only, not
    # used for authorization (access control is enforced at the Cloudflare edge).
    user_email = request.headers.get("Cf-Access-Authenticated-User-Email")
    return render_template(
        "index.html",
        vineyard=config.get("vineyard", {}),
        history=history,
        user_email=user_email,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded."}), 400
    upload = request.files["image"]
    if not upload.filename:
        return jsonify({"error": "Empty filename."}), 400
    if upload.mimetype not in ALLOWED_MIMES:
        return jsonify({"error": f"Unsupported image type: {upload.mimetype}"}), 400

    raw = upload.read()
    if not raw:
        return jsonify({"error": "Empty file."}), 400
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "Image exceeds 10MB limit."}), 413

    concern = (request.form.get("concern") or "").strip()[:500]

    # Optional per-grower overrides (e.g. sharing with a grower elsewhere). When
    # any is set, the advisor is re-pointed away from the configured vineyard.
    varietal = (request.form.get("varietal") or "").strip()[:100]
    region = (request.form.get("region") or "").strip()[:120]
    zip_code = (request.form.get("zip") or "").strip()[:10]
    vineyard = None
    if varietal or region:
        vineyard = dict(config.get("vineyard", {}))
        if varietal:
            vineyard["varietal"] = varietal
        if region:
            vineyard["location"] = region
        vineyard["name"] = "your vineyard"
        vineyard["notes"] = ""  # config notes are Free Run Cellars-specific

    # Analysis depth: more passages = richer grounding but a bigger prompt for
    # the CPU Pi to process. Clamp to a sane range.
    try:
        k = max(1, min(int(request.form.get("k") or RAG_K), 6))
    except (TypeError, ValueError):
        k = RAG_K

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = secure_filename(upload.filename) or "upload.jpg"
    saved_path = UPLOAD_DIR / f"{timestamp}_{safe_name}"
    saved_path.write_bytes(raw)

    try:
        jpeg_bytes = preprocess_image(raw)
    except UnidentifiedImageError:
        return jsonify({"error": "Could not read this image format."}), 400
    except Exception as exc:
        return jsonify({"error": f"Image preprocessing failed: {exc}"}), 500

    image_b64 = base64.b64encode(jpeg_bytes).decode("ascii")

    started = time.monotonic()
    try:
        result = call_ollama(image_b64, concern, vineyard, zip_code, k)
    except requests.Timeout:
        return jsonify(
            {
                "error": (
                    "Gemma took too long to respond. The first run after boot can be "
                    "slow — try again in a minute, or raise ollama.timeout in config.yaml."
                )
            }
        ), 504
    except requests.ConnectionError:
        return jsonify(
            {"error": "Cannot reach Ollama at " + config["ollama"]["host"] + "."}
        ), 502
    except requests.HTTPError as exc:
        return jsonify({"error": f"Ollama returned an error: {exc}"}), 502

    elapsed = round(time.monotonic() - started, 1)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "filename": saved_path.name,
        "elapsed_s": elapsed,
        "concern": concern,
        **result,
    }
    history.insert(0, entry)
    del history[HISTORY_LIMIT:]
    return jsonify(entry)


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "model": config["ollama"]["model"]})


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(config["flask"].get("port", 5000)),
        debug=bool(config["flask"].get("debug", False)),
    )
