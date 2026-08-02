# Phase 4 Independent Human Ground Truth Review

Status: waiting for independent human review

This package is for the production-switch holdout defined by:

- `preregistration.json`
- `source_manifest.json`
- `query_only.jsonl`
- the four immutable PDFs under `pdfs/`

## Isolation boundary

Reviewers may inspect only the files listed above and the templates under `../common/`.
They must not inspect any file under `runs/`, including candidate rankings, retrieval
channels, generation artifacts, scores, diagnostics, or manifests derived from rankings.
They must not use an AI-authored Ground Truth draft.

## Required two-person process

1. Human author A reconstructs all 24 cases directly from the frozen PDFs and writes
   `private/ground_truth.json` using `../common/ground_truth.template.json`.
2. Human reviewer B independently reconstructs all 24 cases before comparing with
   author A's file. Reviewer B resolves every difference against the PDFs.
3. Both reviewers confirm 1-based physical page numbers, exact report filenames,
   company, year, statement scope, unit, metric row, and expected value.
4. Reviewer B writes `private/ground_truth_attestation.json` using
   `../common/ground_truth_attestation.template.json`.
5. `author_id` and `reviewer_id` must identify different natural persons. Do not put
   AI, Claude, agent, or automation identities in either field.
6. Compute exact-byte SHA-256 values only after both files are final. The attestation
   must bind the Ground Truth, query-only file, source manifest, and preregistration.

## Required attestation declarations

The attestation must keep all of these true:

- `reviewer_type=human`
- `review_mode=independent_reconstruction_before_comparison`
- `ranking_not_viewed=true`
- `candidate_artifacts_not_viewed=true`
- `generation_not_viewed=true`
- `scores_not_viewed=true`
- `ai_draft_not_used=true`
- `reviewer_independence_declared=true`

A missing or non-human attestation keeps scoring provisional and does not authorize a
production switch. Code can validate fields and hashes, but cannot prove that the
reviewers actually followed the independence protocol.

## Post-review commands

Run these only after candidate generation and `pre_gt_freeze.json` exist:

```bash
.venv/Scripts/python.exe scripts/run_router_v2_holdout.py --root evals/phase4_holdout_2025 --run-id phase4-financial-v3-2025-r2 validate-ground-truth --ground-truth evals/phase4_holdout_2025/private/ground_truth.json --attestation evals/phase4_holdout_2025/private/ground_truth_attestation.json
```

```bash
.venv/Scripts/python.exe scripts/run_router_v2_holdout.py --root evals/phase4_holdout_2025 --run-id phase4-financial-v3-2025-r2 score --ground-truth evals/phase4_holdout_2025/private/ground_truth.json --attestation evals/phase4_holdout_2025/private/ground_truth_attestation.json
```

```bash
.venv/Scripts/python.exe scripts/run_router_v2_holdout.py --root evals/phase4_holdout_2025 --run-id phase4-financial-v3-2025-r2 finalize
```
