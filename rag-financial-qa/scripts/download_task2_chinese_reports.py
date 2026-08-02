from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "evals" / "task2_chinese_report_sources.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "evals" / "task2_chinese_financial_reports"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download manifest-bound PDFs with fail-closed identity checks."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("manifest must be a non-empty array")
    required = {"filename", "pdf_url", "sha256", "size_bytes"}
    for index, report in enumerate(payload):
        if not isinstance(report, dict) or not required.issubset(report):
            raise ValueError(f"manifest row {index} is incomplete")
        filename = str(report["filename"])
        if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
            raise ValueError(f"unsafe PDF filename: {filename}")
    return payload


def verify_identity(path: Path, report: dict[str, Any]) -> str:
    actual_hash = sha256(path)
    if actual_hash != str(report["sha256"]):
        raise RuntimeError(f"SHA-256 mismatch: {report['filename']} / {actual_hash}")
    actual_size = path.stat().st_size
    if actual_size != int(report["size_bytes"]):
        raise RuntimeError(f"file size mismatch: {report['filename']} / {actual_size}")
    return actual_hash


def download_report(report: dict[str, Any], output_dir: Path) -> str:
    target = output_dir / str(report["filename"])
    if target.exists():
        try:
            return verify_identity(target, report)
        except RuntimeError:
            pass

    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        str(report["pdf_url"]), headers={"User-Agent": "portfolio-evaluation/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open(
            "wb"
        ) as output:
            if response.headers.get_content_type() != "application/pdf":
                raise RuntimeError(f"non-PDF response: {report['filename']}")
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual_hash = verify_identity(temporary, report)
        temporary.replace(target)
        return actual_hash
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reports = load_manifest(args.manifest.resolve())
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for report in reports:
        actual_hash = download_report(report, output_dir)
        print(f"OK {report['filename']} {actual_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
