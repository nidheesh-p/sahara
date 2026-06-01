"""AskEngine — natural language question answering over local files.

Uses SearchEngine to find relevant chunks, then calls an LLM to generate a
grounded answer.  Supports OpenAI (ChatGPT) and local Ollama; auto-selects
OpenAI when OPENAI_API_KEY is set.  Degrades gracefully to search snippets
when no LLM is available.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sahara.search.search_engine import SearchEngine

__all__ = ["AskEngine", "AskResult"]

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "mistral"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MAX_CONTEXT_CHUNKS = 5
_CONTEXT_CHAR_LIMIT = 6000

# Legacy alias kept for backward compatibility
DEFAULT_MODEL = DEFAULT_OLLAMA_MODEL


@dataclass
class AskResult:
    answer: str | None
    sources: list[dict]
    degraded: bool = False
    model_used: str | None = None
    provider_used: str | None = None
    error: str | None = None


class AskEngine:
    """Wraps SearchEngine + optional LLM (OpenAI or Ollama) to answer questions."""

    def __init__(
        self,
        search_engine: SearchEngine,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        model: str | None = None,
        max_context_chunks: int = DEFAULT_MAX_CONTEXT_CHUNKS,
        provider: str | None = None,
        openai_api_key: str | None = None,
        openai_model: str = DEFAULT_OPENAI_MODEL,
    ) -> None:
        self._search = search_engine
        self._ollama_url = ollama_url.rstrip("/")
        self._max_context = max_context_chunks

        self._openai_api_key = openai_api_key or os.environ.get("OPENAI_API_KEY")
        self._openai_model = openai_model

        # Auto-detect provider: explicit > env var availability > ollama default
        if provider:
            self._provider = provider
        elif self._openai_api_key:
            self._provider = "openai"
        else:
            self._provider = "ollama"

        if self._provider == "openai":
            self._model = model or self._openai_model
        else:
            self._model = model or DEFAULT_OLLAMA_MODEL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
    ) -> AskResult:
        chunks = self._search.search(question, top_k=top_k, storage_prefix=storage_prefix)
        if not chunks:
            return AskResult(answer=None, sources=[], degraded=True,
                             error="No indexed files matched your query.")

        context = self._build_context(chunks)

        if self._provider == "openai":
            answer, model_used, provider_used, err = self._call_openai(question, context)
        else:
            answer, model_used, err = self._call_ollama(question, context)
            provider_used = "ollama" if answer else None

        return AskResult(
            answer=answer,
            sources=chunks[: self._max_context],
            degraded=(answer is None),
            model_used=model_used,
            provider_used=provider_used,
            error=err,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_context(self, chunks: list[dict]) -> str:
        parts: list[str] = []
        total = 0
        for chunk in chunks[: self._max_context]:
            snippet = chunk.get("snippet", "")
            path = chunk.get("relative_path", "?")
            entry = f"[{path}]\n{snippet}"
            if total + len(entry) > _CONTEXT_CHAR_LIMIT:
                break
            parts.append(entry)
            total += len(entry)
        return "\n\n---\n\n".join(parts)

    def _call_openai(
        self, question: str, context: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Call OpenAI chat completions API. Returns (answer, model, provider, error)."""
        if not self._openai_api_key:
            return None, None, None, "OpenAI API key not set. Set OPENAI_API_KEY env var."

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Answer the question using ONLY the provided "
                    "context. If the answer is not in the context, say 'Not found in indexed files.'"
                ),
            },
            {
                "role": "user",
                "content": f"Context:\n{context}\n\nQuestion: {question}",
            },
        ]
        payload = json.dumps({
            "model": self._model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800,
        }).encode("utf-8")

        req = urllib.request.Request(
            _OPENAI_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._openai_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                answer = body["choices"][0]["message"]["content"].strip() or None
                return answer, self._model, "openai", None
        except urllib.error.HTTPError as exc:
            logger.debug("OpenAI HTTP error: %s", exc)
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", str(exc))
            except Exception:
                detail = str(exc)
            return None, None, None, f"OpenAI error: {detail}"
        except urllib.error.URLError as exc:
            logger.debug("OpenAI unreachable: %s", exc)
            return None, None, None, f"OpenAI unreachable ({exc}). Showing search results instead."
        except Exception as exc:
            logger.debug("OpenAI error: %s", exc)
            return None, None, None, f"OpenAI error: {exc}"

    def _call_ollama(
        self, question: str, context: str
    ) -> tuple[str | None, str | None, str | None]:
        """Call Ollama generate API. Returns (answer, model, error_message)."""
        prompt = (
            "You are a helpful assistant. Answer the question using ONLY the provided "
            "context. If the answer is not in the context, say 'Not found in indexed files.'\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {question}\nAnswer:"
        )
        payload = json.dumps({
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")

        url = f"{self._ollama_url}/api/generate"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                answer = body.get("response", "").strip() or None
                return answer, self._model, None
        except urllib.error.URLError as exc:
            logger.debug("Ollama unavailable: %s", exc)
            return None, None, f"Ollama unavailable ({exc}). Showing search results instead."
        except Exception as exc:
            logger.debug("Ollama error: %s", exc)
            return None, None, f"Ollama error: {exc}"
