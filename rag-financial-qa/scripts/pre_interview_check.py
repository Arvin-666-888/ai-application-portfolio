import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


PUBLIC_DOCS = [
    "README.md",
    "docs/DEMO_RUNBOOK.md",
    "docs/EVALUATION_REPORT_TEMPLATE.md",
    "docs/PDF_ROUTER_V1_REPORT.md",
    "docs/PDF_ROUTER_V2_DESIGN.md",
    "docs/PDF_ROUTER_V2_REPORT.md",
    "docs/PDF_ROUTER_V3_DESIGN.md",
    "docs/PDF_ROUTER_V3_REPORT.md",
    "docs/RETRIEVAL_V2_REPORT.md",
    "docs/TASK2_ACCEPTANCE_REPORT.md",
]


MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


REQUIRED_FILES = [
    "README.md",
    "PROJECT_SUMMARY.md",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "app/main.py",
    "app/services/rag_service.py",
    "app/services/document_service.py",
    "app/utils/retrieval.py",
    "app/utils/vector_store.py",
    "app/utils/text_splitter.py",
    "evals/run_eval.py",
    "evals/questions.jsonl",
    "evals/fixtures/ecommerce_product_manual.txt",
    "evals/fixtures/ecommerce_customs_compliance.txt",
    "evals/fixtures/ecommerce_logistics_records.txt",
    "docs/DEMO_RUNBOOK.md",
    "docs/EVALUATION_REPORT_TEMPLATE.md",
    "docs/PDF_ROUTER_V1_REPORT.md",
    "docs/PDF_ROUTER_V2_DESIGN.md",
    "docs/PDF_ROUTER_V2_REPORT.md",
    "docs/PDF_ROUTER_V3_DESIGN.md",
    "docs/PDF_ROUTER_V3_REPORT.md",
    "docs/RETRIEVAL_V2_REPORT.md",
    "docs/TASK2_ACCEPTANCE_REPORT.md",
    "scripts/demo_e2e.py",
    "scripts/migrate_router_v2.py",
]


REQUIRED_IMPORTS = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "pydantic",
    "pydantic_settings",
    "httpx",
    "chromadb",
    "jwt",
    "passlib",
    "multipart",
    "numpy",
    "pdfplumber",
    "pytest",
]


def result(name: str, ok: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, status="PASS" if ok else "FAIL", detail=detail)


def warn(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name=name, status="WARN", detail=detail)


def check_files() -> list[CheckResult]:
    results = []
    for relative in REQUIRED_FILES:
        path = PROJECT_ROOT / relative
        results.append(result(f"file:{relative}", path.exists(), "missing" if not path.exists() else ""))
    return results


def check_imports() -> list[CheckResult]:
    results = []
    for module_name in REQUIRED_IMPORTS:
        ok = importlib.util.find_spec(module_name) is not None
        results.append(result(f"import:{module_name}", ok, "not installed" if not ok else ""))
    return results


def downgrade_missing_imports(results: list[CheckResult]) -> list[CheckResult]:
    downgraded = []
    for item in results:
        if item.name.startswith("import:") and item.status == "FAIL":
            downgraded.append(CheckResult(item.name, "WARN", f"{item.detail}; install requirements-dev.txt before running API demo"))
        else:
            downgraded.append(item)
    return downgraded


def run_command(name: str, command: list[str], cwd: Path) -> CheckResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:
        return CheckResult(name=name, status="FAIL", detail=str(exc))

    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode == 0:
        return CheckResult(name=name, status="PASS", detail=output.splitlines()[-1] if output else "")
    return CheckResult(name=name, status="FAIL", detail=output[-1000:])


def check_tests(skip_tests: bool) -> list[CheckResult]:
    if skip_tests:
        return [warn("pytest", "skipped by --skip-tests; current test status must come from a fresh pytest run")]
    with tempfile.TemporaryDirectory(prefix="rag-pytest-") as temp_dir:
        return [
            run_command(
                "pytest",
                [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--basetemp", temp_dir],
                PROJECT_ROOT,
            )
        ]


def check_syntax() -> list[CheckResult]:
    files = [
        "scripts/demo_e2e.py",
        "scripts/pre_interview_check.py",
        "app/utils/retrieval.py",
        "app/services/rag_service.py",
        "app/models/models.py",
        "app/schemas/schemas.py",
        "app/database.py",
        "app/services/document_service.py",
        "app/routers/documents.py",
    ]
    errors = []
    for relative in files:
        path = PROJECT_ROOT / relative
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            errors.append(f"{relative}: {exc}")
    return [result("python_syntax", not errors, "; ".join(errors))]


def check_public_docs() -> list[CheckResult]:
    broken_links = []
    forbidden_paths = []
    for relative in PUBLIC_DOCS:
        path = PROJECT_ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\bcd\s+demo(?:[/\\]|\b)|demo/(?:data|uploads|chroma_data)\b", line, re.IGNORECASE):
                forbidden_paths.append(f"{relative}:{line_number}")
        for target in MARKDOWN_LINK_RE.findall(text):
            target = target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                broken_links.append(f"{relative} -> {target}")

    return [
        result("public_docs:no_demo_paths", not forbidden_paths, ", ".join(forbidden_paths)),
        result("public_docs:local_links", not broken_links, "; ".join(broken_links)),
    ]


def check_release_boundaries() -> list[CheckResult]:
    config_text = (PROJECT_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    defaults_ok = all(
        pattern in config_text
        for pattern in (
            "PDF_PADDLE_ARTIFACT_ENABLED: bool = False",
            'RETRIEVAL_PROFILE: str = "legacy"',
            'RAG_ANSWER_PROFILE: str = "legacy"',
            "TOP_K: int = 3",
        )
    )

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "docs" / "DEMO_RUNBOOK.md").read_text(encoding="utf-8")
    boundary_text = readme + "\n" + runbook
    facts_ok = all(
        marker in boundary_text
        for marker in (
            "provisional `12/24`",
            "`verified_v3=0/24 accepted`",
            "sealed holdout",
            "独立人工",
        )
    )
    worker_ok = "python -m app.workers.document_worker" in readme and "停留在 `queued`" in readme

    return [
        result("release_defaults:legacy_l3_disabled", defaults_ok, "expected legacy profiles, L3 disabled, TOP_K=3"),
        result("release_docs:gate_boundaries", facts_ok, "missing Gate B/Gate C/sealed holdout/attestation boundary"),
        result("quickstart:document_worker", worker_ok, "README must explain queued status and start document worker"),
    ]


def check_runtime_data() -> list[CheckResult]:
    questions_path = PROJECT_ROOT / "evals" / "questions.jsonl"
    cases = []
    if questions_path.exists():
        with questions_path.open(encoding="utf-8") as file:
            cases = [json.loads(line) for line in file if line.strip()]
    categories = {case.get("category") for case in cases}
    required_categories = {
        "ecommerce_price", "ecommerce_inventory", "ecommerce_logistics",
        "ecommerce_compliance", "out_of_corpus_sku", "unsupported_fact",
        "multi_fact_guardrail", "complex_formula_guardrail", "insufficient_evidence",
    }
    active_contract_ok = (
        len([case for case in cases if not case.get("should_refuse")]) == 4
        and len([case for case in cases if case.get("should_refuse")]) >= 6
        and required_categories <= categories
        and {"medium", "hard"} <= {case.get("difficulty") for case in cases}
    )

    return [
        result("active_eval:ecommerce_contract", active_contract_ok, f"fresh count: {len(cases)}; expected four facts plus refusal boundary matrix"),
        warn("evaluation_boundary", "Question count and pytest totals must be quoted from fresh command output; historical fixed counts are stage-specific only."),
        warn("gate_b_boundary", "Historical/disclosed Gate B is provisional 12/24 on an AI draft without independent human attestation; it is not finalized."),
        warn("gate_c_boundary", "Gate C was really executed and failed: verified_v3=0/24 accepted; the new sealed holdout has not run."),
        warn("release_defaults", "Keep PDF_PADDLE_ARTIFACT_ENABLED=false, RETRIEVAL_PROFILE=legacy, and RAG_ANSWER_PROFILE=legacy by default."),
        warn("runtime_data_ignored", "kb_qa.db、uploads、chroma_data 应作为本地运行产物，不建议提交到作品仓库。"),
    ]


def print_table(results: list[CheckResult]) -> None:
    print("\nPre-interview check")
    print("=" * 80)
    for item in results:
        detail = f" - {item.detail}" if item.detail else ""
        print(f"[{item.status}] {item.name}{detail}")

    failed = [item for item in results if item.status == "FAIL"]
    warned = [item for item in results if item.status == "WARN"]

    print("=" * 80)
    print(f"PASS={sum(1 for item in results if item.status == 'PASS')} WARN={len(warned)} FAIL={len(failed)}")

    if failed:
        print("\nAction needed:")
        for item in failed:
            print(f"- {item.name}: {item.detail}")
    if warned:
        print("\nWarnings:")
        for item in warned:
            print(f"- {item.name}: {item.detail}")


def write_json_report(results: list[CheckResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    data = [item.__dict__ for item in results]
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether the cross-border ecommerce RAG project is ready for interview demo.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest.")
    parser.add_argument(
        "--allow-missing-deps",
        action="store_true",
        help="Report missing runtime dependencies as warnings instead of failures.",
    )
    parser.add_argument("--json-report", default="", help="Optional JSON report path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results: list[CheckResult] = []
    results.extend(check_files())
    import_results = check_imports()
    if args.allow_missing_deps:
        import_results = downgrade_missing_imports(import_results)
    results.extend(import_results)
    results.extend(check_syntax())
    results.extend(check_public_docs())
    results.extend(check_release_boundaries())
    results.extend(check_tests(skip_tests=args.skip_tests))
    results.extend(check_runtime_data())

    print_table(results)

    if args.json_report:
        write_json_report(results, Path(args.json_report))

    return 1 if any(item.status == "FAIL" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
