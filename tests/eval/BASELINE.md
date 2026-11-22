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

# Hybrid retrieval — FTS5 BM25 + vector, fused with RRF

Measured on 2026-11-17 with `run_eval(corpus, queries, k=5, mode=...)` on the
same corpus, queries and model as above. Hybrid fetches `3 * top_k` files from
each side and fuses them with Reciprocal Rank Fusion (k = 60).

- Backend: `vec_backend = false` (cosine fallback; sqlite-vec imports but the
  measuring Python cannot load SQLite extensions), `fts_backend = true`.
- The vector column reproduces the baseline above exactly.

## Results (k = 5)

| Category    | Queries | Vector R@5 | Hybrid R@5 | Vector MRR@5 | Hybrid MRR@5 | Vector FP | Hybrid FP |
|-------------|--------:|-----------:|-----------:|-------------:|-------------:|----------:|----------:|
| exact_token |      12 |      0.917 |  **1.000** |        0.875 |    **1.000** |         — |         — |
| paraphrase  |      14 |      1.000 |      1.000 |        1.000 |        1.000 |         — |         — |
| date_number |      10 |      0.900 |  **1.000** |        0.733 |    **0.833** |         — |         — |
| not_present |       8 |          — |          — |            — |            — |     1.000 |     1.000 |
| **overall** |  36 + 8 |      0.944 |  **1.000** |        0.884 |    **0.954** |     1.000 |     1.000 |

## Observations

- `INV-2047` now ranks the Kestrel invoice first (vector: missed);
  `HX-55902` and one renewal-date query move from rank 2 to rank 1.
- The `annual fee $18,000` miss is fixed (rank 3 in hybrid).
  `payment due 2025-07-05 amount $97.40` improves from rank 3 to rank 2.
- Not-present FP rate is unchanged: `score` keeps its vector-similarity
  meaning, so the 0.5 threshold behaves exactly as before. Keyword-only
  results carry `score = 0.0`; `rrf_score` and `match` are new keys.
- `test_hybrid_retrieval.py` asserts hybrid ≥ vector recall@5 on exact_token
  and date_number (strictly greater on at least one), no paraphrase drop,
  and the Kestrel invoice in the top 5 for `INV-2047`.

# Search confidence labels — high / medium / low

Measured on 2026-11-18, hybrid mode, same corpus, model and backends
(`vec_backend = false`, `fts_backend = true`). Every result now carries
`confidence`, set by `sahara.search.hybrid.confidence_label` from:

- **term coverage**: the share of the query's IDF weight (BM25-style IDF from
  FTS5 chunk counts, so terms in many chunks count for little) whose terms
  appear in the matched chunk;
- **agreement**: `match == "both"` and BM25 rank 1, scored as
  `vector_score + 0.5 * coverage`;
- **vector score alone**, only well above the not-present range.

Rule: high if coverage ≥ 0.50 on a keyword hit or vector ≥ 0.80; medium if
coverage ≥ 0.40, or agreement ≥ 0.70, or vector ≥ 0.72; otherwise low.
Labels never filter or reorder results.

## Holdout protocol

`queries.json` gained 22 queries marked `"holdout": true` (10 answerable:
3 exact_token, 5 paraphrase, 2 date_number; 12 not_present), written
before any tuning; no corpus files were added. Thresholds were read off the
original 44 queries only (midway between answerable and not_present
signals). `run_eval` reports the original queries in `overall` /
`categories` (unchanged numbers above) and the held-out ones under
`holdout`. The holdout was run once, after the rule was frozen.

## Results (k = 5)

| Set     | Answerable R@5 | MRR@5 | Correct top-1 labelled high/medium | Not-present FP (score > 0.5) | Not-present confident FP (top-1) | Not-present with any high/medium result |
|---------|---------------:|------:|-----------------------------------:|-----------------------------:|---------------------------------:|----------------------------------------:|
| Tuning  |  1.000 (36)    | 0.954 | 0.917 (33/36)                      | 1.000                        | **0.000** (0/8)                  | 1/8                                     |
| Holdout |  1.000 (10)    | 0.950 | **0.800** (8/10)                   | 1.000                        | **0.250** (3/12)                 | 6/12                                    |

Acceptance targets (`tests/test_search_confidence.py`): holdout confident
FP ≤ 0.35 (met, 0.25), holdout confident correct top-1 ≥ 0.80 (met exactly,
**no margin**: one more miss fails), original recall@5 unchanged at 1.000
(ranked lists are identical with labelling disabled).

## Observations

- Tuning misses on confident top-1 are the three queries whose top-1 is the
  wrong file (`payment due … $97.40`, `receipt total 42.17`,
  `annual fee $18,000`); the correct file is at rank 2–3 and labelled high.
- Holdout confident false positives come from shared vocabulary:
  `flight booking to Reykjavik in March` → Lisbon itinerary ("flight",
  "booking"), `home loan offer from Fernbrook Bank` → tax notes,
  `sourdough starter feeding schedule` → garden journal ("feeding",
  "schedule"). All three are medium via the agreement rule, none high.
- Holdout answerable misses: `Halima Strand` ranks a vector-only file first
  (low) with the exact keyword hit second (high) — a fusion-order issue that
  labels must not fix; `how long does the dough need to rise` reaches the
  recipe at rank 1 but scores 0.654 + 0.5 × coverage, below 0.70.
- Six of 12 held-out absent queries still have some medium result in the top
  5, so the CLI's "No confident matches found" notice fires for only half of
  them; the per-query top-1 metric is what the acceptance test measures.
- The margins are thin (agreement 0.726 vs 0.684 on the tuning set) and the
  thresholds are tied to the cosine-fallback score scale; re-measure on the
  sqlite-vec backend. In `vector` mode nothing is on the keyword side, so
  only vector ≥ 0.72 earns medium and most correct answers are labelled low.
