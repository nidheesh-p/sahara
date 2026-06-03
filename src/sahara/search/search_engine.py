"""Semantic search engine for Sahara — text extraction and embedding-based retrieval."""

from __future__ import annotations

import hashlib
import logging
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sahara.storage.state_db import StateDB

__all__ = ["SearchEngine", "TextExtractor"]

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1600       # chars ≈ 400 tokens
CHUNK_OVERLAP = 320     # chars ≈ 80 tokens overlap between adjacent chunks
EMBEDDING_DIM = 384     # BAAI/bge-small-en-v1.5 output dimension


class TextExtractor:
    """Extracts plain text from various file types for indexing."""

    SUPPORTED_EXTENSIONS = {
        ".txt", ".md", ".rst", ".py", ".js", ".ts", ".json",
        ".yaml", ".yml", ".toml", ".csv", ".html", ".xml",
    }
    BINARY_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tif", ".tiff",
        ".bmp", ".ico", ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".zip",
        ".gz", ".tar", ".7z", ".dmg", ".exe", ".bin",
    }

    def extract(self, file_path: Path) -> str | None:
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            return self._extract_pdf(file_path)
        if suffix in (".docx", ".doc"):
            return self._extract_docx(file_path)
        if suffix in self.BINARY_EXTENSIONS:
            return None
        if suffix in self.SUPPORTED_EXTENSIONS or self._looks_like_text(file_path):
            try:
                return file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("Cannot read %s: %s", file_path, exc)
                return None
        return None

    def _extract_pdf(self, file_path: Path) -> str | None:
        try:
            import pypdf  # type: ignore[import]
            reader = pypdf.PdfReader(str(file_path))
            parts = [page.extract_text() for page in reader.pages]
            text = "\n".join(p for p in parts if p)
            return text or None
        except ImportError:
            logger.debug("pypdf not installed; cannot extract PDF text")
            return None
        except Exception as exc:
            logger.debug("PDF extraction failed for %s: %s", file_path, exc)
            return None

    def _extract_docx(self, file_path: Path) -> str | None:
        try:
            import docx  # type: ignore[import]
            doc = docx.Document(str(file_path))
            text = "\n".join(p.text for p in doc.paragraphs if p.text)
            return text or None
        except ImportError:
            logger.debug("python-docx not installed; cannot extract DOCX text")
            return None
        except Exception as exc:
            logger.debug("DOCX extraction failed for %s: %s", file_path, exc)
            return None

    def _looks_like_text(self, file_path: Path) -> bool:
        try:
            with open(file_path, "rb") as fh:
                chunk = fh.read(512)
            non_text = sum(
                1 for b in chunk if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F
            )
            return len(chunk) == 0 or non_text / len(chunk) < 0.30
        except OSError:
            return False


def _split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character-level chunks."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    step = size - overlap
    while start < len(text):
        chunks.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return chunks


def _floats_to_bytes(vec: Any) -> bytes:
    """Serialise a float32 numpy/list vector to raw bytes for sqlite-vec."""
    try:
        return vec.astype("float32").tobytes()
    except AttributeError:
        return struct.pack(f"{len(vec)}f", *vec)


class SearchEngine:
    """Semantic search using chunked fastembed embeddings.

    Uses sqlite-vec virtual table (ANN) when available; degrades to in-memory
    cosine scan against the legacy embeddings table otherwise.
    """

    def __init__(self, db: StateDB) -> None:
        self._db = db
        self._extractor = TextExtractor()
        self._model: Any = None

    def _get_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding  # type: ignore[import]
                self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            except ImportError:
                raise RuntimeError(
                    "fastembed is required for semantic search. "
                    "Install it with: pip install 'sahara[search]'"
                )
        return self._model

    def _embed(self, texts: list[str]) -> list[Any]:
        model = self._get_model()
        return list(model.embed(texts))

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index_file(
        self,
        file_path: Path,
        storage_prefix: str,
        relative_path: str,
        force: bool = False,
    ) -> bool:
        """Index file_path for semantic search. Returns True if (re-)indexed."""
        text = self._extractor.extract(file_path)
        if not text or not text.strip():
            return False

        text = text.strip()
        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

        if not force:
            existing_hash = self._db.get_chunk_content_hash(storage_prefix, relative_path)
            if existing_hash == content_hash:
                return False

        chunks = _split_chunks(text)
        if not chunks:
            return False

        # Delete old chunks + vec rows before re-indexing
        old_ids = self._db.delete_chunks_for_file(storage_prefix, relative_path)
        if old_ids and self._db.has_vec_table():
            self._db.delete_vec_chunks(old_ids)

        embeddings = self._embed(chunks)

        use_vec = self._db.has_vec_table()
        for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
            chunk_id = self._db.upsert_chunk(
                storage_prefix=storage_prefix,
                relative_path=relative_path,
                chunk_index=i,
                content_hash=content_hash,
                chunk_text=chunk_text,
            )
            if use_vec:
                self._db.upsert_vec_chunk(chunk_id, _floats_to_bytes(emb))

        # Keep legacy embeddings table in sync (snippet + single-vector fallback)
        import json
        snippet = text[:500]
        embedding_json = json.dumps(embeddings[0].tolist())
        self._db.upsert_embedding(
            s3_prefix=storage_prefix,
            relative_path=relative_path,
            content_hash=content_hash,
            embedding_json=embedding_json,
            snippet=snippet,
        )
        return True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
    ) -> list[dict]:
        """Search for files semantically similar to query.

        Returns list of dicts: {storage_prefix, relative_path, score, snippet}.
        """
        query_embs = self._embed([query])
        if not query_embs:
            return []

        if self._db.has_vec_table():
            return self._search_vec(query_embs[0], top_k, storage_prefix)
        return self._search_cosine(query_embs[0], top_k, storage_prefix)

    def _search_vec(
        self,
        query_emb: Any,
        top_k: int,
        storage_prefix: str | None,
    ) -> list[dict]:
        query_bytes = _floats_to_bytes(query_emb)
        # Fetch top_k*4 to have enough after per-file dedup
        raw = self._db.vec_knn_search(query_bytes, k=top_k * 4, storage_prefix=storage_prefix)
        if not raw:
            return self._search_cosine(query_emb, top_k, storage_prefix)
        return self._dedup_to_top_k(raw, top_k)

    def _search_cosine(
        self,
        query_emb: Any,
        top_k: int,
        storage_prefix: str | None,
    ) -> list[dict]:
        """Fallback: O(n) cosine scan against legacy embeddings table."""
        import json

        import numpy as np

        query_vec = np.array(query_emb, dtype=np.float32)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm

        rows = self._db.list_embeddings(s3_prefix=storage_prefix)
        scored: list[dict] = []
        for row in rows:
            try:
                doc_vec = np.array(json.loads(row["embedding_json"]), dtype=np.float32)
                doc_norm = np.linalg.norm(doc_vec)
                if doc_norm > 0:
                    doc_vec = doc_vec / doc_norm
                score = float(np.dot(query_vec, doc_vec))
            except Exception:
                score = 0.0
            scored.append({
                "storage_prefix": row["s3_prefix"],
                "relative_path": row["relative_path"],
                "score": score,
                "snippet": row.get("snippet", ""),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _dedup_to_top_k(rows: list[dict], top_k: int) -> list[dict]:
        """Keep best-scoring chunk per file; return top_k files."""
        seen: dict[tuple, dict] = {}
        for row in rows:
            key = (row["storage_prefix"], row["relative_path"])
            # vec distance is lower-is-better; clamp to [0, 1] similarity score
            score = max(0.0, 1.0 - float(row.get("distance", 0.0)))
            if key not in seen or score > seen[key]["score"]:
                seen[key] = {
                    "storage_prefix": row["storage_prefix"],
                    "relative_path": row["relative_path"],
                    "score": score,
                    "snippet": row.get("chunk_text", "")[:500],
                }
        results = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
        return results[:top_k]
