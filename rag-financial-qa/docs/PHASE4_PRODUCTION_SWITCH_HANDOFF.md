# Phase 4 Frozen Evaluation and Future Switch Handoff

Date: 2026-07-30

## Current status: pre-Ground-Truth engineering freeze completed

The Phase 4 pre-GT evaluation candidate set is frozen at `evals/phase4_holdout_2025/` with hardened run ID `phase4-financial-v3-2025-r2`. The earlier r1 was superseded before Ground Truth after a separate engineering contract review found that its freeze did not bind enough implementation and input identities; r1 was never scored. This review was an engineering pass, not independent human Ground Truth review.

Completed:

- Reports: 长江电力、宁德时代、京东方 A、万华化学 2025 annual reports.
- Isolation: all four companies and report year 2025 are absent from the development set and the previously unsealed evaluation.
- Query-only: 24 cases, SHA-256 `503e2b2a2eaa765820ee5b7b9d823eb8bc4478a772a79aa716293cfacc497b15`.
- Source manifest SHA-256: `a956eae9df7654567131a6cf9f05388109e81118938d2737032db18feb0dc7d7`.
- OCR: 397/397 selected pages, 0 failed/missing/stale/unexpected/mapping errors, 586 tables.
- Routed corpus: 3,960 chunks, file SHA-256 `9f78b81662e4930d1e2a79d8e17e9f00cfdee92b6ce2a709428e83e5e3aa1637`.
- Embeddings: 3,984 unique texts, dimension 1,536, 0 invalid/missing.
- Candidate: `legacy + financial_v3`, Top-K 5, diagnostic/merged budget 100, all four V3 channel budgets explicitly bound to 100, cache-only 3,984/3,984, `api_called=false`, `ground_truth_loaded=false`.
- Candidate file SHA-256: `62fe170d2c132a424260d1f12509c770c4846f8641c90924762e914a6b1a95bf`.
- Retrieval config SHA-256: `9e2ab2d9b22ac81ab2162169eb4620ddda7a691e841d59e9cfab5f7dbf68c020`.
- Pre-GT freeze SHA-256: `117cddfb0464301da50ec1ee88c086d8747db61111ae5206eb7b8ab34d68e7d6`.
- Implementation aggregate SHA-256: `f5c4e7c5c8e415a28adadd3e742cbbde99965d07c738912721cc98a0fd14d4b4` over 17 local source files plus runtime package versions and dependency lock hashes.
- r2 reused 4,391 pre-candidate OCR/corpus/cache files from r1 with identical tree-inventory SHA-256 `a617aec762ee660f3b6317be96fce51ca0af03f6a734497ebe6eb943b03fee68`; retrieval config, candidate, ranking and freeze were regenerated under the hardened code.

Not completed:

- Ground Truth;
- independent human Ground Truth review or attestation;
- one-time score;
- Recall@5;
- Recall@3;
- default retrieval-profile switch.

The default remains `legacy`, Top-K 3. Phase 4 currently proves engineering isolation and content identity only; it has no authorized quality claim.

The earlier `evals/phase4_holdout/` 2024 candidate set is marked `superseded_before_candidate_generation`; it was never ranked or scored because year 2024 violated the strict unused-year requirement.

Repository durability note: the public sealed input contract (`preregistration.json`, `query_only.jsonl`, `source_manifest.json`, and `HUMAN_REVIEW.md`) is versioned. The repository-wide `.gitignore` still excludes `*.pdf`, `private/`, `runs/`, and embedding caches. Before relying on another checkout for reproduction, download or restore the four PDF bytes into `evals/phase4_holdout_2025/pdfs/` and verify them against `source_manifest.json`; a manifest without matching PDF bytes is not independently reproducible evidence.

The repository downloader can verify the Phase 4 manifest without changing the default Task 2 paths:

```bash
.venv/Scripts/python.exe scripts/download_task2_chinese_reports.py --manifest evals/phase4_holdout_2025/source_manifest.json --output-dir evals/phase4_holdout_2025/pdfs
```

## Current evidence flow

```text
[4 official 2025 A-share annual reports]
    │ 1. query-only selection; no labels loaded
    ▼
[OCR + routed corpus + embedding cache]
    │ 2. candidate generation
    ▼
[Frozen candidate/ranking + bound identities]
    │ ground_truth_loaded=false
    └── BLOCKED: no GT, attestation, score, Recall@5 or Recall@3
```

No one should inspect frozen rankings while trying to preserve the option of a future source-only review. The optional team protocol is documented in `evals/phase4_holdout_2025/HUMAN_REVIEW.md`. Code can validate declarations and hashes, but it cannot prove that two natural persons actually worked independently.

## Two honest completion paths

### Path A — recommended for the current individual portfolio

1. Keep Phase 4 as evidence of pre-GT engineering freeze, hash binding and leakage control.
2. Do not invent a second reviewer or create a false attestation.
3. Report the existing 30-question metrics only as non-independent development-set results:
   - hand-written `financial_v3`: 28/30 source-page, 22/30 row-strict, row-strict MRR 0.413333;
   - LangChain parent-page: 30/30 source-page and 10/30 row-strict.
4. Keep the default `legacy` + Top-K 3 because Phase 4 has no quality score and the repository has no formal Recall@3 benchmark.
5. Preserve the historical Chinese `5/30 → 0/30 → 7/30 → 14/30`, foreign-report `3/15 → 4/15`, Gate B `12/24 provisional` and Gate C `0/24 accepted` failures.

If the developer personally labels Phase 4, the set must be reclassified as a **non-independent self-labeled evaluation/development set**. It can still support error analysis, but it must no longer be called sealed and cannot authorize a default switch.

### Path B — optional future formal gate

Use this path only if real external reviewers become available:

```text
[Frozen query/candidate/ranking identities]
    │ 1. source-only reconstruction by real reviewers
    ▼
[Independent human-reviewed GT + attestation]
    │ 2. one-time score
    ▼
[Source-page and row-strict Recall@5]
    │ 3. gate decision
    ├── fail ──► preserve failure; keep legacy/default unchanged
    └── pass ──► run Recall@3 if runtime stays Top-K 3
                     │
                     ▼
              consider default switch + rollback test
```

Minimum future contract:

1. Ground Truth is independently reviewed and attested before rankings are unsealed.
2. Candidate generation records `ground_truth_loaded=false`.
3. Source-page Recall@5 is at least 75%.
4. Row-strict Recall@5 is greater than 70% if the user-facing claim uses strict recall.
5. No individual report is below 50% source-page Recall@5.
6. Failure analysis shows no source/company mapping regressions.
7. Existing chat/source/citation regression tests pass.
8. If the runtime remains Top-K 3, a separate Recall@3 result is required; Recall@5 cannot be substituted.

These thresholds are project-defined release criteria, not universal industry benchmarks. If the evaluation fails, preserve the result and do not tune on the same set while continuing to call it sealed.

## Runtime changes only after a future gate passes

1. Add `financial_v3` to the application retrieval profile configuration.
2. Keep the existing profile as an explicit rollback value.
3. Set runtime Top-K to 5 if the user-facing metric and citation path are based on Recall@5; otherwise run and report Recall@3 before retaining Top-K 3.
4. Wire `VectorStore.query_financial_v3()` into the runtime retrieval call site.
5. Preserve answer verification and citation provenance fields.
6. Add structured logging for:
   - retrieval profile;
   - candidate budget;
   - reranker backend;
   - fallback reason;
   - source/page identities;
   - latency.
7. Run smoke tests on one table fact, one narrative fact, one refusal question and one multi-report ambiguity case.
8. Document rollback instructions and verify rollback in a local or staging run.

These are future deployment tasks, not completed production work.

## BGE optional follow-up

If network access becomes available:

1. Download `BAAI/bge-reranker-v2-m3` once and record an immutable model revision and file hashes.
2. Add exact dependencies to a separate lock; do not rely on the system Python environment.
3. Run CPU latency and memory measurements on Top-20 and Top-50.
4. Compare against the deterministic `financial_v3` arm on a development/training split only.
5. Evaluate the selected frozen configuration once on a fresh independently reviewed set, if such a set becomes available.
6. Keep the deterministic reranker as fallback when model loading fails.

Do not replace the measured deterministic result with an unexecuted BGE claim.

## Reproduction commands for development runs

Hand-written candidate generation:

```bash
.venv/Scripts/python.exe scripts/05_evaluate_paddleocr_retrieval.py --cache-only --routed-corpus evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json --retrieval-profile financial_v3 --questions evals/task2_paddleocr/development_questions.jsonl --dense-k 100 --lexical-k 100 --candidates-output evals/v3/runs/<new-run-id>/candidate.json
```

Hand-written scoring:

```bash
.venv/Scripts/python.exe scripts/06_score_retrieval_artifact.py --candidates evals/v3/runs/<new-run-id>/candidate.json --ground-truth evals/table_ground_truth.json --output evals/v3/runs/<new-run-id>/score.json
```

LangChain candidate generation:

```bash
.venv/Scripts/python.exe scripts/08_evaluate_langchain_parent_retrieval.py --corpus evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json --questions evals/task2_paddleocr/development_questions.jsonl --embedding-cache-dir evals/task2_paddleocr/embedding_cache --dense-k 100 --lexical-k 100 --top-k 5 --output evals/v3/runs/<new-langchain-run-id>/candidate.json
```

LangChain scoring:

```bash
.venv/Scripts/python.exe scripts/09_score_langchain_parent_retrieval.py --candidates evals/v3/runs/<new-langchain-run-id>/candidate.json --ground-truth evals/table_ground_truth.json --output evals/v3/runs/<new-langchain-run-id>/score.json
```

Never point these commands at historical canonical files. Use a new run path.

## Source of truth

Read `docs/RECALL_OPTIMIZATION_REPORT.md` before changing any runtime profile. It contains the exact metric definitions, evidence hashes, dependency versions and claim boundaries.
