from __future__ import annotations

from pathlib import Path


HISTORICAL_CANONICAL_PATHS = frozenset(
    {
        "compare_result.json",
        "compare_result_en_10k.json",
        "evals/task2_paddleocr/chunks/router_v1_frozen_l1_corpus_v2.json",
        "evals/task2_paddleocr/reports/retrieval_router_v1_candidates_v2.json",
        "evals/task2_paddleocr/reports/retrieval_router_v1_row_strict_v2.json",
    }
)


def normalized_project_path(path: Path, project_root: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(project_root).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return ""


def is_historical_canonical(path: Path, project_root: Path) -> bool:
    return normalized_project_path(path, project_root) in HISTORICAL_CANONICAL_PATHS


def ensure_evidence_output_writable(
    path: Path,
    *,
    project_root: Path,
    force: bool = False,
) -> None:
    target = Path(path)
    relative = normalized_project_path(target, project_root)
    if relative in HISTORICAL_CANONICAL_PATHS:
        raise FileExistsError(f"historical canonical evidence is immutable: {relative}")
    if target.exists() and not force:
        raise FileExistsError(
            f"refusing to overwrite existing evidence; use --force only for scratch output: {target}"
        )
    parts = target.resolve().parts
    if "evals" in parts and "v3" in parts and "runs" in parts:
        manifest = target.parent / "manifest.json"
        if manifest.exists():
            import json

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("immutable") is True or payload.get("status") == "finalized":
                raise FileExistsError(f"finalized V3 run is immutable: {target.parent}")
