"""retrieve.py - query the SQLite-vec RAG index built by build_index.py.

Module API:
  from retrieve import retrieve
  hits = retrieve("downy mildew on grape leaves", k=3)
  # -> [{"source", "page", "text", "distance"}, ...]

CLI (for sanity-checking the index):
  python3 retrieve.py "cover crops for vineyards"
  python3 retrieve.py "soil biology mycorrhizae" 5
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import TypedDict

import requests
import sqlite_vec

from app import config

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "rag.db"
# Must match the embedder build_index.py used, or query vectors land in a
# different space than the index. Shared via config to prevent drift.
EMBED_MODEL = config.get("rag", {}).get("embed_model", "all-minilm")


class Hit(TypedDict):
    source: str
    page: int
    text: str
    distance: float


def embed(host: str, text: str) -> list[float]:
    url = host.rstrip("/") + "/api/embeddings"
    resp = requests.post(
        url,
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def open_db() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise FileNotFoundError(
            f"{DB_PATH.name} not found; run `python3 build_index.py` first"
        )
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def retrieve(query: str, k: int = 3) -> list[Hit]:
    if not query.strip():
        return []
    host = config["ollama"]["host"]
    qvec = embed(host, query)
    db = open_db()
    try:
        rows = db.execute(
            """
            WITH matches AS (
                SELECT rowid, distance
                FROM vec_chunks
                WHERE embedding MATCH ?
                  AND k = ?
                ORDER BY distance
            )
            SELECT chunks.source, chunks.page, chunks.text, matches.distance
            FROM matches
            JOIN chunks ON chunks.id = matches.rowid
            ORDER BY matches.distance
            """,
            (json.dumps(qvec), k),
        ).fetchall()
    finally:
        db.close()
    return [
        {"source": s, "page": p, "text": t, "distance": d}
        for (s, p, t, d) in rows
    ]


def format_hit(hit: Hit, n: int) -> str:
    preview = hit["text"][:240].replace("\n", " ")
    ellipsis = "..." if len(hit["text"]) > 240 else ""
    return (
        f"\n[{n}] {hit['source']} p.{hit['page']}  (dist={hit['distance']:.3f})\n"
        f"    {preview}{ellipsis}"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: retrieve.py <query> [k]", file=sys.stderr)
        return 2
    query = argv[1]
    k = int(argv[2]) if len(argv) > 2 else 3
    hits = retrieve(query, k=k)
    print(f"query: {query!r}")
    print(f"returned {len(hits)} hit(s):")
    for i, hit in enumerate(hits, start=1):
        print(format_hit(hit, n=i))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
