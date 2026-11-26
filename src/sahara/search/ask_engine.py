"""AskEngine — natural language question answering over local files.

Uses SearchEngine to find relevant chunks, then optionally calls an LLM to
generate a grounded answer. Standalone answer generation is disabled by
default; Ollama and OpenAI are explicit opt-in providers.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sahara.search.search_engine import SearchEngine

__all__ = ["AskEngine", "AskResult", "EgressPreview"]

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
    # What was handed to the provider; None when nothing was sent.
    egress: EgressPreview | None = None


@dataclass(frozen=True)
class EgressPreview:
    """What ask() would send to the answer provider, built without any network call.

    est_tokens is an estimate (char_count // 4), not a tokenizer count.
    sources holds (storage_prefix, relative_path, score, chunk_id) per snippet.
    """

    provider: str
    model: str | None
    destination: str | None
    leaves_machine: bool
    sources: tuple[tuple[str, str, float | None, int | None], ...]
    char_count: int
    est_tokens: int
    truncated: bool
    context: str


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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

        self._provider = provider or "none"
        if self._provider not in ("none", "ollama", "openai"):
            raise ValueError("provider must be 'none', 'ollama', or 'openai'")

        self._model: str | None
        if self._provider == "openai":
            self._model = model or self._openai_model
        elif self._provider == "ollama":
            self._model = model or DEFAULT_OLLAMA_MODEL
        else:
            self._model = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(
        self,
        question: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
    ) -> AskResult:
        chunks, context = self._retrieve(question, top_k, storage_prefix)
        if not chunks:
            return AskResult(answer=None, sources=[], degraded=True,
                             error="No indexed files matched your query.")

        if self._provider == "none":
            return AskResult(
                answer=None,
                sources=chunks[: self._max_context],
            )

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
            egress=self._egress(chunks, context),
        )

    def preview(
        self,
        question: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
    ) -> EgressPreview:
        """Show exactly what ask() would send, without contacting the provider."""
        return self._egress(*self._retrieve(question, top_k, storage_prefix))

    def _egress(self, chunks: list[dict], context: str) -> EgressPreview:
        sent = self._context_chunks(chunks)
        sources = tuple(
            (
                str(c.get("storage_prefix", c.get("s3_prefix", ""))),
                str(c.get("relative_path", "?")),
                c.get("score"),
                c.get("chunk_id"),
            )
            for c in sent
        )
        char_count = len(context)
        return EgressPreview(
            provider=self._provider,
            model=self._model,
            destination=self.destination,
            leaves_machine=self.leaves_machine,
            sources=sources,
            char_count=char_count,
            est_tokens=char_count // 4,
            truncated=len(sent) < len(chunks[: self._max_context]),
            context=context,
        )

    @property
    def destination(self) -> str | None:
        """Host the provider call goes to, or None when nothing is sent."""
        if self._provider == "openai":
            return urllib.parse.urlsplit(_OPENAI_API_URL).hostname
        if self._provider == "ollama":
            return urllib.parse.urlsplit(self._ollama_url).hostname
        return None

    @property
    def leaves_machine(self) -> bool:
        if self._provider == "openai":
            return True
        if self._provider == "ollama":
            return not _is_loopback_host(self.destination)
        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _retrieve(
        self, question: str, top_k: int, storage_prefix: str | None
    ) -> tuple[list[dict], str]:
        """Run the local search and build the context ask() would send."""
        chunks = self._search.search(question, top_k=top_k, storage_prefix=storage_prefix)
        return chunks, self._build_context(chunks)

    def _context_chunks(self, chunks: list[dict]) -> list[dict]:
        """The chunks _build_context keeps under the character limit."""
        kept: list[dict] = []
        total = 0
        for chunk in chunks[: self._max_context]:
            entry = f"[{chunk.get('relative_path', '?')}]\n{chunk.get('snippet', '')}"
            if total + len(entry) > _CONTEXT_CHAR_LIMIT:
                break
            kept.append(chunk)
            total += len(entry)
        return kept

    def _build_context(self, chunks: list[dict]) -> str:
        parts = [
            f"[{chunk.get('relative_path', '?')}]\n{chunk.get('snippet', '')}"
            for chunk in self._context_chunks(chunks)
        ]
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
