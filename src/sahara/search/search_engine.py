"""Semantic search engine for Sahara — text extraction and embedding-based retrieval."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sahara.storage.state_db import StateDB

__all__ = ["SearchEngine", "TextExtractor"]

logger = logging.getLogger(__name__)


class TextExtractor:
    """Extracts plain text from various file types for indexing."""

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".rst", ".py", ".js", ".ts", ".json",
                            ".yaml", ".yml", ".toml", ".csv", ".html", ".xml"}

    def extract(self, file_path: Path) -> Optional[str]:
        """Extract text from *file_path*.

        Returns extracted text string, or None if extraction failed or
        file type is unsupported.
        """
        suffix = file_path.suffix.lower()

        # Try PDF extraction via pypdf if available
        if suffix == ".pdf":
            return self._extract_pdf(file_path)

        # Try docx via python-docx if available
        if suffix in (".docx", ".doc"):
            return self._extract_docx(file_path)

        # Plain text fallback for known text types
        if suffix in self.SUPPORTED_EXTENSIONS or self._looks_like_text(file_path):
            try:
                return file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("Cannot read %s: %s", file_path, exc)
                return None

        return None

    def _extract_pdf(self, file_path: Path) -> Optional[str]:
        try:
            import pypdf  # type: ignore[import]
            reader = pypdf.PdfReader(str(file_path))
            parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            return "\n".join(parts) if parts else None
        except ImportError:
            logger.debug("pypdf not installed; cannot extract PDF text")
            return None
        except Exception as exc:
            logger.debug("PDF extraction failed for %s: %s", file_path, exc)
            return None

    def _extract_docx(self, file_path: Path) -> Optional[str]:
        try:
            import docx  # type: ignore[import]
            doc = docx.Document(str(file_path))
            return "\n".join(p.text for p in doc.paragraphs if p.text)
        except ImportError:
            logger.debug("python-docx not installed; cannot extract DOCX text")
            return None
        except Exception as exc:
            logger.debug("DOCX extraction failed for %s: %s", file_path, exc)
            return None

    def _looks_like_text(self, file_path: Path) -> bool:
        """Heuristic: read first 512 bytes and check for binary content."""
        try:
            with open(file_path, "rb") as fh:
                chunk = fh.read(512)
            # If more than 30% non-printable non-whitespace bytes, consider binary
            non_text = sum(
                1 for b in chunk if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F
            )
            return len(chunk) == 0 or non_text / len(chunk) < 0.30
        except OSError:
            return False


class SearchEngine:
    """Semantic search using fastembed embeddings and cosine similarity."""

    def __init__(self, db: "StateDB") -> None:
        self._db = db
        self._extractor = TextExtractor()
        self._model: Any = None  # lazy-loaded fastembed model

    def _get_model(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding  # type: ignore[import]
                self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            except ImportError:
                raise RuntimeError(
                    "fastembed is required for semantic search. "
                    "Install it with: pip install fastembed"
                )
        return self._model

    def index_file(
        self,
        file_path: Path,
        s3_prefix: str,
        relative_path: str,
        force: bool = False,
    ) -> bool:
        """Index *file_path* for semantic search.

        Returns True if the file was (re-)indexed, False if skipped (unchanged).
        """
        text = self._extractor.extract(file_path)
        if text is None:
            return False

        text = text.strip()
        if not text:
            return False

        # Compute a simple content hash to detect changes
        import hashlib
        content_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

        if not force:
            existing = self._db.get_embedding(s3_prefix, relative_path)
            if existing and existing.get("content_hash") == content_hash:
                return False

        model = self._get_model()
        # Truncate text to ~8000 chars to stay within token limits
        text_for_embed = text[:8000]
        embeddings = list(model.embed([text_for_embed]))
        if not embeddings:
            return False

        import json
        embedding_json = json.dumps(embeddings[0].tolist())
        # Snippet: first 500 chars of text
        snippet = text[:500]

        self._db.upsert_embedding(
            s3_prefix=s3_prefix,
            relative_path=relative_path,
            content_hash=content_hash,
            embedding_json=embedding_json,
            snippet=snippet,
        )
        return True

    def search(
        self,
        query: str,
        top_k: int = 5,
        s3_prefix: Optional[str] = None,
    ) -> list[dict]:
        """Search for files semantically similar to *query*.

        Returns a list of dicts with keys: s3_prefix, relative_path, score, snippet.
        """
        model = self._get_model()
        query_embeddings = list(model.embed([query]))
        if not query_embeddings:
            return []

        import json
        import numpy as np

        query_vec = np.array(query_embeddings[0], dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm > 0:
            query_vec = query_vec / query_norm

        rows = self._db.list_embeddings(s3_prefix=s3_prefix)
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
                "s3_prefix": row["s3_prefix"],
                "relative_path": row["relative_path"],
                "score": score,
                "snippet": row.get("snippet", ""),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]
