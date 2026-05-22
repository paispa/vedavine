"""bench_chain.py - measure moondream -> gemma chained latency.

Two subcommands. Run each only after the relevant model is warm in Ollama
(check `ollama ps`). With OLLAMA_MAX_LOADED_MODELS=1, switching models
evicts the previous one, so warm them one at a time.

  # 1. warm moondream:
  ollama run moondream "ready"
  python3 bench_chain.py vision uploads/<photo>.jpeg
    -> writes bench_vision.txt, prints moondream timing + output

  # 2. warm gemma (~7 min cold-load the first time):
  ollama run gemma4:e2b "ready"
  python3 bench_chain.py reason           # with thinking (baseline: 251s)
  python3 bench_chain.py reason --no-think  # suppress thinking pass (hypothesis: ~60s)
    -> reads bench_vision.txt, prints gemma text-only timing + parsed JSON

Reuses preprocess_image / build_system_prompt / parse_ollama_response from
app.py so output matches the production prompt and parser exactly.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import requests

from app import (
    build_system_prompt,
    config,
    parse_ollama_response,
    preprocess_image,
)

BASE_DIR = Path(__file__).resolve().parent
VISION_CACHE = BASE_DIR / "bench_vision.txt"
VISION_MODEL = "moondream"
VISION_PROMPT = (
    "Describe this vineyard image in detail. Focus on: leaf color and texture, "
    "any signs of disease (spots, fungal growth, discoloration) or pests, canopy "
    "structure, soil moisture indicators, and overall vine vigor. Be specific and "
    "concrete. Aim for 4-8 sentences."
)


def post_generate(host: str, payload: dict, timeout: int) -> dict:
    url = host.rstrip("/") + "/api/generate"
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def cmd_vision(image_path: Path) -> int:
    raw = image_path.read_bytes()
    jpeg = preprocess_image(raw)
    print(
        f"image: {image_path.name}  "
        f"raw={len(raw)/1024:.0f} KB  compressed={len(jpeg)/1024:.0f} KB"
    )

    ocfg = config["ollama"]
    payload = {
        "model": VISION_MODEL,
        "prompt": VISION_PROMPT,
        "images": [base64.b64encode(jpeg).decode("ascii")],
        "stream": False,
    }
    print(f"calling {VISION_MODEL} at {ocfg['host']} ...")
    t0 = time.monotonic()
    body = post_generate(ocfg["host"], payload, ocfg.get("timeout", 900))
    elapsed = time.monotonic() - t0

    text = body.get("response", "").strip()
    VISION_CACHE.write_text(text, encoding="utf-8")
    print(f"\nelapsed: {elapsed:.1f}s")
    print(f"output ({len(text)} chars) -> {VISION_CACHE.name}:\n")
    print(text)
    return 0


def cmd_reason(no_think: bool = False) -> int:
    if not VISION_CACHE.is_file():
        print(
            f"missing {VISION_CACHE}; run `bench_chain.py vision <image>` first",
            file=sys.stderr,
        )
        return 2

    observations = VISION_CACHE.read_text(encoding="utf-8").strip()
    if not observations:
        print(f"{VISION_CACHE} is empty", file=sys.stderr)
        return 2

    user_prompt = (
        "Based on these visual observations of the vineyard, produce the "
        "VedaVine JSON assessment. Observations:\n\n"
        f"{observations}\n\n"
        "Respond ONLY with valid JSON."
    )

    ocfg = config["ollama"]
    payload = {
        "model": ocfg["model"],
        "system": build_system_prompt(),
        "prompt": user_prompt,
        "stream": False,
    }
    if no_think:
        payload["think"] = False

    think_label = " [think=off]" if no_think else ""
    print(f"calling {ocfg['model']} (text-only{think_label}) at {ocfg['host']} ...")
    print(f"input: {len(observations)} chars of moondream output")
    t0 = time.monotonic()
    body = post_generate(ocfg["host"], payload, ocfg.get("timeout", 900))
    elapsed = time.monotonic() - t0

    parsed = parse_ollama_response(body.get("response", ""))
    print(f"\nelapsed: {elapsed:.1f}s")
    print("parsed:")
    print(json.dumps(parsed, indent=2))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"vision", "reason"}:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "vision":
        if len(argv) != 3:
            print("usage: bench_chain.py vision <image-path>", file=sys.stderr)
            return 2
        path = Path(argv[2])
        if not path.is_file():
            print(f"not found: {path}", file=sys.stderr)
            return 2
        return cmd_vision(path)
    no_think = "--no-think" in argv
    return cmd_reason(no_think=no_think)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
