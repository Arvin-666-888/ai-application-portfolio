import argparse
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    "evals/fixtures/finance_summary_2024.txt",
    "evals/fixtures/risk_notice.txt",
    "docs/DEMO_CHECKLIST.md",
    "docs/EVAL_REPORT_TEMPLATE.md",
    "scripts/demo_e2e.py",
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
        return [warn("pytest", "skipped by --skip-tests")]
    return [run_command("pytest", [sys.executable, "-m", "pytest"], PROJECT_ROOT)]


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
    return [
        run_command(
            "py_compile",
            [sys.executable, "-B", "-m", "py_compile", *files],
            PROJECT_ROOT,
        )
    ]


def check_runtime_data() -> list[CheckResult]:
    questions_path = PROJECT_ROOT / "evals" / "questions.jsonl"
    question_count = 0
    if questions_path.exists():
        with questions_path.open(encoding="utf-8") as file:
            question_count = sum(1 for line in file if line.strip())

    return [
        result("eval_questions_count", question_count >= 15, f"{question_count} questions"),
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
    parser = argparse.ArgumentParser(description="Check whether the RAG project is ready for interview demo.")
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
    results.extend(check_tests(skip_tests=args.skip_tests))
    results.extend(check_runtime_data())

    print_table(results)

    if args.json_report:
        write_json_report(results, Path(args.json_report))

    return 1 if any(item.status == "FAIL" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
