"""Vector embeddings — encode files, cosine search, RRF fusion.

Uses all-MiniLM-L6-v2 (384 dims, ~50ms/doc on CPU). Loaded lazily.
"""

import struct
from datetime import datetime, timezone

import numpy as np

# Lazy-loaded model
_model = None
_MODEL_NAME = "all-MiniLM-L6-v2"
_DIMS = 384


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _pack_vector(vec) -> bytes:
    """Pack a numpy float32 vector into bytes."""
    return struct.pack(f"{_DIMS}f", *vec.tolist())


def _unpack_vector(blob: bytes):
    """Unpack bytes into a numpy float32 vector."""
    return np.array(struct.unpack(f"{_DIMS}f", blob), dtype=np.float32)


def embed_text(text: str):
    """Embed a text string, returns numpy array."""
    model = _get_model()
    return model.encode(text, normalize_embeddings=True)


def embed_file(conn, mem_home: str, rel_path: str) -> None:
    """Compute and store embedding for a file."""
    import os
    abs_path = os.path.join(mem_home, rel_path)
    with open(abs_path, encoding="utf-8") as f:
        content = f.read()

    # Get title for better embedding (title + content)
    row = conn.execute("SELECT title FROM nodes WHERE path = ?", (rel_path,)).fetchone()
    title = row[0] if row else ""
    embed_input = f"{title}\n{content}" if title else content

    vec = embed_text(embed_input)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT OR REPLACE INTO embeddings (path, vector, model, created_at)
           VALUES (?, ?, ?, ?)""",
        (rel_path, _pack_vector(vec), _MODEL_NAME, now),
    )
    conn.execute(
        "UPDATE nodes SET embedded_at = ? WHERE path = ?",
        (now, rel_path),
    )
    conn.commit()


def embed_all(conn, mem_home: str) -> int:
    """Embed all files that haven't been embedded yet. Returns count."""
    rows = conn.execute(
        "SELECT path FROM nodes WHERE embedded_at IS NULL"
    ).fetchall()

    count = 0
    for row in rows:
        embed_file(conn, mem_home, row[0])
        count += 1
    return count


def embed_query(query: str):
    """Embed a query string, returns numpy array."""
    return embed_text(query)


def cosine_search(conn, query: str, top_k: int = 20) -> list[tuple[str, float]]:
    """Search embeddings by cosine similarity. Returns [(path, score), ...]."""
    query_vec = embed_query(query)

    rows = conn.execute("SELECT path, vector FROM embeddings").fetchall()
    if not rows:
        return []

    paths = [r[0] for r in rows]
    vectors = np.array([_unpack_vector(r[1]) for r in rows])

    # Cosine similarity (vectors are already normalized)
    sims = vectors @ query_vec

    # Get top-k
    if len(sims) <= top_k:
        top_idx = np.argsort(sims)[::-1]
    else:
        top_idx = np.argpartition(sims, -top_k)[-top_k:]
        top_idx = top_idx[np.argsort(sims[top_idx])[::-1]]

    return [(paths[i], float(sims[i])) for i in top_idx]
