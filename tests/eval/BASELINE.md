# Retrieval eval baseline — vector-only search

Measured on 2026-11-16 with `sahara.eval.runner.run_eval(corpus, queries, k=5)`
against the unchanged `SearchEngine` (BAAI/bge-small-en-v1.5 via fastembed).

- Corpus: 18 fictional files in `tests/eval/corpus/` (one OCR-noisy receipt).
- Queries: 44 in `tests/eval/queries.json` (12 exact_token, 14 paraphrase,
  10 date_number, 8 not_present).
- Backend: `vec_backend = false` — sqlite-vec was not loaded in the measuring
  venv, so search used the cosine fallback over each file's first-chunk
  embedding. Every corpus file fits in one chunk, so the ranking should match
  the sqlite-vec path, but the scores are on a different scale
  (`1 - distance` instead of cosine). Re-measure the threshold numbers if
  the backend changes.
- Not-present threshold: 0.5 (`DEFAULT_THRESHOLD`).

## Results (k = 5)

| Category    | Queries | Recall@5 | MRR@5 | Not-present FP rate |
|-------------|--------:|---------:|------:|--------------------:|
| exact_token |      12 |    0.917 | 0.875 |                   — |
| paraphrase  |      14 |    1.000 | 1.000 |                   — |
| date_number |      10 |    0.900 | 0.733 |                   — |
| not_present |       8 |        — |     — |               1.000 |
| **overall** |  36 + 8 | **0.944** | **0.884** |           **1.000** |

## Observations

- Misses: `INV-2047` ranks the *other* invoice (INV-2031) first and misses
  the Kestrel invoice entirely; `annual fee $18,000` misses the Quillon
  contract. Exact identifiers and amounts are where dense-only search is weak.
- Several date/number queries find the right file only at rank 2–3
  (e.g. `payment due 2025-07-05 amount $97.40`, `renewal date 2026-01-15`),
  because the INV-2031 invoice attracts most number-heavy queries.
- Vector-only search cannot reject absent answers: the best score for every
  not_present query is between 0.52 and 0.67, while the lowest correct
  top-1 score is 0.48 (`Priya Castellanos`). FP rate by threshold:
  0.5 → 1.000, 0.6 → 0.625, 0.7 → 0.000. No single threshold separates
  hits from absent answers.

## Test floor

`tests/test_eval_harness.py` asserts overall recall@5 ≥ **0.90**, a little
below the measured 0.944, so one more miss among the 36 answerable queries
still passes.

## Reproduce

```bash
FASTEMBED_CACHE_PATH=<model cache> HF_HUB_OFFLINE=1 PYTHONPATH=src \
  python -m pytest tests/test_eval_harness.py -q -rs
```

The end-to-end test is skipped, with the reason printed, when the embedding
model cannot be loaded offline.
