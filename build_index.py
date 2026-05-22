"""build_index.py - embed corpus PDFs into a SQLite-vec RAG index.

Reads PDFs from corpus/, extracts text per page, chunks with overlap,
embeds via Ollama nomic-embed-text, and stores everything in rag.db
with sqlite-vec for cosine-distance retrieval.

Run after corpus changes:
  python3 build_index.py

The script wipes rag.db and rebuilds from scratch each time. For ~10 PDFs
expect a few minutes on a Pi 5 (the embedding API call is the slow part).

Requires:
  - Ollama running with nomic-embed-text pulled (`ollama pull nomic-embed-text`)
  - pypdf, sqlite-vec installed (see requirements.txt)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pypdf
import requests
import sqlite_vec

from app import config

BASE_DIR = Path(__file__).resolve().parent
CORPUS_DIR = BASE_DIR / "corpus"
DB_PATH = BASE_DIR / "rag.db"
# Embedder is shared with retrieve.py via config so the two never drift to
# different vector spaces. Dimension is auto-detected from the model at build
# time (nomic=768, all-minilm=384), so swapping models needs no code change.
EMBED_MODEL = config.get("rag", {}).get("embed_model", "all-minilm")

# all-minilm caps at ~256 tokens and 500s on longer input, so keep chunks well
# under that (~200 tokens). A larger-context embedder (e.g. nomic, 8192) could
# use bigger chunks — raise these if you swap rag.embed_model back.
CHUNK_CHARS = 800    # ~200 tokens
OVERLAP_CHARS = 120  # ~30 tokens


def embed(host: str, text: str, retries: int = 5) -> list[float]:
    """Embed one chunk, adapting to two distinct failure modes.

    - HTTP error (commonly: input over the embedder's token limit — all-minilm
      500s at ~256 tokens): truncate and retry immediately, so a dense chunk is
      shortened rather than dropped.
    - Connection/timeout (model still warming up, esp. under MAX_LOADED_MODELS=2):
      back off and retry the same text.
    """
    url = host.rstrip("/") + "/api/embeddings"
    body = text
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                url, json={"model": EMBED_MODEL, "prompt": body}, timeout=120
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except requests.HTTPError as exc:
            last_exc = exc
            body = body[: max(200, len(body) * 3 // 4)]  # shrink, retry fast
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))  # transient; model may be warming
    raise last_exc  # type: ignore[misc]


def extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    reader = pypdf.PdfReader(str(pdf_path))
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:
            print(f"  page {i}: extract error: {e}", file=sys.stderr)
            continue
        if text.strip():
            pages.append((i, text))
    return pages


def chunk_text(text: str, page: int) -> list[tuple[int, str]]:
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [(page, text)]
    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = start + CHUNK_CHARS
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append((page, tail))
            break
        space = text.rfind(" ", start, end)
        if space > start:
            end = space
        piece = text[start:end].strip()
        if piece:
            chunks.append((page, piece))
        start = max(end - OVERLAP_CHARS, start + 1)
    return chunks


def init_db(dim: int) -> sqlite3.Connection:
    if DB_PATH.exists():
        DB_PATH.unlink()
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            page INTEGER NOT NULL,
            text TEXT NOT NULL
        )
        """
    )
    db.execute(
        f"""
        CREATE VIRTUAL TABLE vec_chunks USING vec0(
            embedding float[{dim}]
        )
        """
    )
    return db


def health_check(host: str) -> int:
    """Confirm the embedder is reachable; return its embedding dimension."""
    try:
        vec = embed(host, "health check")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(
                f"model {EMBED_MODEL} not found. Run: ollama pull {EMBED_MODEL}",
                file=sys.stderr,
            )
        raise
    except requests.RequestException as e:
        print(f"cannot reach Ollama at {host}: {e}", file=sys.stderr)
        raise
    return len(vec)


def main() -> int:
    if not CORPUS_DIR.is_dir():
        print(f"missing {CORPUS_DIR}; create corpus/ and drop PDFs in", file=sys.stderr)
        return 1
    pdfs = sorted(CORPUS_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"no PDFs found in {CORPUS_DIR}", file=sys.stderr)
        return 1

    host = config["ollama"]["host"]
    print(f"ollama: {host} | embed model: {EMBED_MODEL}")
    print(f"corpus: {len(pdfs)} PDF(s) in {CORPUS_DIR.name}/")

    dim = health_check(host)
    print(f"embedding dimension: {dim}")

    db = init_db(dim)
    total = 0
    t0 = time.monotonic()

    for pdf in pdfs:
        print(f"\n{pdf.name}")
        pages = extract_pages(pdf)
        chunks = [c for page, text in pages for c in chunk_text(text, page)]
        print(f"  {len(pages)} pages, {len(chunks)} chunks; embedding...")
        for page, chunk in chunks:
            try:
                vec = embed(host, chunk)
            except Exception as e:
                print(f"  embed error p{page}: {e}", file=sys.stderr)
                continue
            cur = db.execute(
                "INSERT INTO chunks (source, page, text) VALUES (?, ?, ?)",
                (pdf.name, page, chunk),
            )
            db.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, json.dumps(vec)),
            )
            total += 1
        db.commit()

    elapsed = time.monotonic() - t0
    db.close()
    print(f"\ndone: {total} chunks in {DB_PATH.name} ({elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
