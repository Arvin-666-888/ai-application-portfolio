from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "router_v2_holdout"
    / "validate_run.py"
)
SPEC = importlib.util.spec_from_file_location("router_v2_holdout_validate_run", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def test_finalized_validator_rejects_run_directory_escape(tmp_path):
    root = tmp_path / "holdout"
    outside = tmp_path / "outside"

    with pytest.raises(validator.HoldoutRunValidationError, match="escapes"):
        validator.validate_run(root, outside)


def test_finalized_validator_rejects_missing_final_manifest(tmp_path):
    root = tmp_path / "holdout"
    run_dir = root / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (root / "preregistration.json").write_text("{}", encoding="utf-8")
    (root / "source_manifest.json").write_text("[]", encoding="utf-8")

    with pytest.raises(validator.HoldoutRunValidationError, match="missing artifact"):
        validator.validate_run(root, run_dir)
