# Contributing to Sahara

Thank you for your interest in contributing. This document covers everything you need to go from clone to merged PR.

---

## Development setup

Python 3.11 or newer is required. The commands below use `python3` on macOS and
Linux. On Windows, replace `python3` with `py -3.11` and activate the environment
with `.venv\Scripts\Activate.ps1`.

```bash
git clone https://github.com/nidheesh-p/sahara
cd sahara

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install -e ".[search,dev]"
```

Verify the install:

```bash
sahara --version
pytest --tb=short
```

Ollama is not required to run the test suite. To exercise `sahara ask` manually,
follow [docs/ANSWER_PROVIDERS.md](docs/ANSWER_PROVIDERS.md); Sahara uses local
Ollama unless OpenAI is explicitly selected.

---

## Running tests

The test suite uses `pytest` with `moto` for S3 mocking. No real AWS account is required.

```bash
# Run everything
pytest

# With coverage
pytest --cov=src/sahara --cov-report=term-missing

# Run a specific test file
pytest tests/test_sync_engine.py -v

# Run tests matching a keyword
pytest -k "test_conflict"

# Run the index-first product-model tests
pytest tests/test_three_step_model.py -v
```

### Test conventions

- **No real AWS / MinIO / network calls in tests.** Use `moto` for S3 and `tmp_path` (pytest fixture) for local filesystem tests.
- **Fixtures live in `conftest.py`.** Add shared fixtures there rather than duplicating setup code.
- **Unit tests over integration tests for logic.** Test `DiffResult` computation directly rather than running a full sync.
- **New feature → new test file.** Don't append unrelated tests to an existing file.
- **Indexing and sync are separate.** New indexing behavior must work with
  `storage_mode = "none"` and without rows in the sync `files` table.
- **Schema changes are migrations.** Preserve old configs, `sync_targets`, chunks, and
  embeddings when adding content-root or inventory behavior.

---

## Code style

```bash
# Lint (enforced in CI)
ruff check src/ tests/

# Fix auto-fixable issues
ruff check --fix src/ tests/

# Type check
mypy src/
```

Sahara uses `ruff` for linting and `mypy` in strict mode. Both must pass before a PR is merged.

Key style points:
- No comments explaining *what* the code does — only *why* when it is non-obvious
- No `Any` unless you can explain why in a comment
- No `# type: ignore` without a comment explaining why it is unavoidable

---

## Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feature/my-thing`
2. Write your changes and matching tests
3. Confirm the full test suite passes and coverage stays at or above 85%
4. Run `ruff check` and `mypy src/` — both must be clean
5. Open a PR against `main`. Fill in the PR template (what it changes, which issue it closes, which storage backends you tested against)

Branch naming:
- `feature/<name>` — new functionality
- `fix/<name>` — bug fix
- `docs/<name>` — documentation only
- `refactor/<name>` — refactoring without behaviour change

Commit messages: imperative mood, one line summary, no trailing period. Example: `Add chunked indexing for long PDFs`.

---

## Adding a storage backend

This is the most common extension point. Sahara uses a structural Protocol (`StorageBackend`) so you do not need to inherit from a base class — you just need to implement the right methods.

### Step-by-step: a toy in-memory backend

```python
# src/sahara/storage/memory_client.py
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional


class MemoryClient:
    """In-memory storage backend for testing — not for production use."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._manifest: Optional[dict] = None

    def upload_file(
        self,
        local_path: Path,
        key: str,
        metadata: Optional[dict[str, str]] = None,
        storage_class: str = "STANDARD",
        encrypt_fn: Optional[Callable[[Path], tuple[Path, str]]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> str:
        data = local_path.read_bytes()
        self._store[key] = data
        return hashlib.sha256(data).hexdigest()

    def download_file(
        self,
        key: str,
        local_path: Path,
        decrypt_fn: Optional[Callable[[Path, Path], str]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> str:
        data = self._store[key]
        local_path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def delete_object(self, key: str) -> None:
        self._store.pop(key, None)

    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: Optional[dict[str, str]] = None,
    ) -> str:
        data = self._store[src_key]
        self._store[dst_key] = data
        return hashlib.sha256(data).hexdigest()

    def get_manifest(self, key: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
        if self._manifest is None:
            return None, None
        etag = hashlib.sha256(str(self._manifest).encode()).hexdigest()
        return self._manifest, etag

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: Optional[str] = None,
        key: Optional[str] = None,
    ) -> str:
        self._manifest = manifest_dict
        return hashlib.sha256(str(manifest_dict).encode()).hexdigest()

    def list_all_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        return [
            {"Key": k, "Size": len(v), "ETag": hashlib.sha256(v).hexdigest()}
            for k, v in self._store.items()
            if k.startswith(prefix)
        ]

    def head_object(self, key: str) -> dict[str, Any]:
        data = self._store[key]
        return {"ContentLength": len(data), "ETag": hashlib.sha256(data).hexdigest()}

    def validate_bucket_access(self) -> None:
        pass  # always accessible

    def check_conditional_put_support(self) -> bool:
        return False  # no atomic check in this toy impl

    def restore_object(self, key: str, days: int = 7, tier: str = "Bulk") -> None:
        raise NotImplementedError("MemoryClient does not support Glacier restore")
```

Then wire it into the CLI:

```python
# In cli.py, inside the backend selection block:
elif config.storage_mode == "memory":
    from sahara.storage.memory_client import MemoryClient
    backend = MemoryClient()
```

See `storage/backend.py` for the full Protocol definition and docstrings for each method.

---

## Adding a file parser

Text extraction lives in `search/search_engine.py` in the `TextExtractor` class.

To add support for a new format:

```python
def extract(self, file_path: Path) -> Optional[str]:
    suffix = file_path.suffix.lower()
    # ... existing cases ...
    if suffix == ".epub":
        return self._extract_epub(file_path)
    # ...

def _extract_epub(self, file_path: Path) -> Optional[str]:
    try:
        import ebooklib  # type: ignore[import]
        from ebooklib import epub
        book = epub.read_epub(str(file_path))
        texts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            texts.append(item.get_content().decode("utf-8", errors="replace"))
        return "\n".join(texts) or None
    except ImportError:
        logger.debug("ebooklib not installed; cannot extract EPUB text")
        return None
    except Exception as exc:
        logger.debug("EPUB extraction failed for %s: %s", file_path, exc)
        return None
```

Wrap heavy imports in `try/except ImportError` so the base install does not break for users who don't have the dependency.

---

## Adding an embedding model

The embedding model is instantiated in `SearchEngine.__init__()` in `search/search_engine.py`. The current model is `BAAI/bge-small-en-v1.5` (384-dim) via `fastembed`.

If you switch models, the vector dimension (`EMBEDDING_DIM`) must match. All existing indexed files will need to be re-indexed — add a migration note in the PR description.

---

## Release process

Releases are cut by the maintainers. If you are proposing a change that requires a version bump, note it in your PR.

1. Update `version` in `pyproject.toml`
2. Add an entry to `CHANGELOG.md` under a new `## [x.y.z]` heading
3. Tag: `git tag vx.y.z && git push --tags`
4. CI publishes to PyPI on tag push (once the publish workflow is configured)
