"""Bare sqlite-vec KNN diagnostic — isolates whether the `k = ?` error is in
retrieve.py's SQL or in sqlite-vec itself. Read-only; touches nothing.

Runs the same KNN three ways against the real rag.db:
  A) modern:   ... WHERE embedding MATCH ? ORDER BY distance LIMIT ?
  B) k-form:   ... WHERE embedding MATCH ? AND k = ? ORDER BY distance
  C) cte+join: retrieve.py's current structure (k-form inside a CTE, joined)
"""
import json, sqlite3, sys
import sqlite_vec, requests

DB = "/home/frc/vedavine/rag.db"
HOST = "http://localhost:11434"
QUERY = "downy mildew on grape leaves"
K = 3

print(f"sqlite_vec version: {sqlite_vec.__version__ if hasattr(sqlite_vec,'__version__') else '?'}")

# 1. embed the query
r = requests.post(f"{HOST}/api/embeddings",
                  json={"model": "nomic-embed-text", "prompt": QUERY}, timeout=60)
r.raise_for_status()
vec = r.json()["embedding"]
print(f"embedded query -> {len(vec)} dims")

db = sqlite3.connect(DB)
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)

def run(label, sql, params):
    try:
        rows = db.execute(sql, params).fetchall()
        print(f"\n[{label}] OK — {len(rows)} rows")
        for row in rows[:3]:
            print("   ", row[:2])
        return True
    except Exception as e:
        print(f"\n[{label}] FAILED — {type(e).__name__}: {e}")
        return False

qj = json.dumps(vec)

run("A modern LIMIT",
    "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
    (qj, K))

run("B k= form",
    "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? ORDER BY distance",
    (qj, K))

run("C cte+join (retrieve.py current)",
    """WITH matches AS (
         SELECT rowid, distance FROM vec_chunks
         WHERE embedding MATCH ? AND k = ? ORDER BY distance
       )
       SELECT chunks.source, chunks.page, matches.distance
       FROM matches JOIN chunks ON chunks.id = matches.rowid
       ORDER BY matches.distance""",
    (qj, K))

# Bonus: does the A-form join cleanly? (the likely fix for retrieve.py)
run("D modern LIMIT + join (candidate fix)",
    """WITH matches AS (
         SELECT rowid, distance FROM vec_chunks
         WHERE embedding MATCH ? ORDER BY distance LIMIT ?
       )
       SELECT chunks.source, chunks.page, matches.distance
       FROM matches JOIN chunks ON chunks.id = matches.rowid
       ORDER BY matches.distance""",
    (qj, K))

db.close()
