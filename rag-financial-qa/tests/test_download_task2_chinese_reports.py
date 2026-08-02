from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "download_task2_chinese_reports.py"
SPEC = importlib.util.spec_from_file_location("download_reports_test", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _report(filename: str, content: bytes) -> dict:
    return {
        "filename": filename,
        "pdf_url": "https://example.test/report.pdf",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def test_manifest_rejects_unsafe_filename(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps([_report("../report.pdf", b"pdf")]), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="unsafe PDF filename"):
        module.load_manifest(manifest)


def test_existing_verified_report_skips_network(tmp_path, monkeypatch):
    content = b"%PDF-verified"
    report = _report("report.pdf", content)
    target = tmp_path / report["filename"]
    target.write_bytes(content)
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("verified file must not be downloaded"),
    )

    assert module.download_report(report, tmp_path) == report["sha256"]
    assert target.read_bytes() == content


def test_failed_download_does_not_replace_existing_file(tmp_path, monkeypatch):
    expected = b"%PDF-expected"
    downloaded = b"%PDF-wrong"
    report = _report("report.pdf", expected)
    target = tmp_path / report["filename"]
    target.write_bytes(b"existing-invalid-file")

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/pdf"

    class Response:
        headers = Headers()

        def __init__(self):
            self._remaining = downloaded

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _size: int) -> bytes:
            chunk, self._remaining = self._remaining, b""
            return chunk

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module.download_report(report, tmp_path)

    assert target.read_bytes() == b"existing-invalid-file"
    assert not target.with_suffix(".pdf.tmp").exists()
