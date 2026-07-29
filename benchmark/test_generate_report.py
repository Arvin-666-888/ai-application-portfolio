from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.generate_report import (
    ScenarioMetrics,
    load_metrics,
    render_markdown,
    validate_passing_run,
)


HEADERS = [
    "Type",
    "Name",
    "Request Count",
    "Failure Count",
    "Median Response Time",
    "Average Response Time",
    "Min Response Time",
    "Max Response Time",
    "Average Content Size",
    "Requests/s",
    "Failures/s",
    "50%",
    "66%",
    "75%",
    "80%",
    "90%",
    "95%",
    "98%",
    "99%",
    "99.9%",
    "99.99%",
    "100%",
]


def write_stats(path: Path, rows: list[list[object]], headers: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers or HEADERS)
        writer.writerows(rows)


def row(name: str, requests: int, failures: int, average: float, p95: float, p99: float) -> list[object]:
    values = {
        "Type": "POST",
        "Name": name,
        "Request Count": requests,
        "Failure Count": failures,
        "Average Response Time": average,
        "95%": p95,
        "99%": p99,
    }
    return [values.get(header, 0) for header in HEADERS]


def passing_metadata() -> dict[str, object]:
    return {
        "status": "通过",
        "users": 10,
        "spawn_rate": 2,
        "run_time": "2m",
        "mode": "real LLM",
    }


def write_failures(path: Path, rows: list[list[object]] | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Method", "Name", "Error", "Occurrences"])
        writer.writerows(rows or [])


def test_load_metrics_reads_percentiles_and_failure_rate(tmp_path: Path) -> None:
    stats = tmp_path / "benchmark_stats.csv"
    write_stats(
        stats,
        [
            row("agent_chat", 100, 2, 120.5, 250, 400),
            row("rag_query", 80, 0, 300, 700, 900),
            row("Aggregated", 180, 2, 200, 600, 850),
        ],
    )

    metrics = load_metrics(stats)

    assert [item.scenario for item in metrics] == ["Agent", "RAG", "Aggregated"]
    assert metrics[0].p95_ms == 250
    assert metrics[0].p99_ms == 400
    assert metrics[0].failure_rate == pytest.approx(2.0)


@pytest.mark.parametrize("content", ["", "Name,Request Count\n"])
def test_empty_or_header_only_csv_has_no_metrics(tmp_path: Path, content: str) -> None:
    stats = tmp_path / "empty.csv"
    stats.write_text(content, encoding="utf-8")

    if content:
        with pytest.raises(ValueError, match="missing columns"):
            load_metrics(stats)
    else:
        assert load_metrics(stats) == []


def test_only_agent_or_only_rag_is_supported(tmp_path: Path) -> None:
    for name, label in (("agent_chat", "Agent"), ("rag_query", "RAG")):
        stats = tmp_path / f"{name}.csv"
        write_stats(stats, [row(name, 5, 0, 10, 20, 30)])
        metrics = load_metrics(stats)
        assert len(metrics) == 1
        assert metrics[0].scenario == label


def test_missing_percentile_is_rejected(tmp_path: Path) -> None:
    stats = tmp_path / "bad.csv"
    headers = [header for header in HEADERS if header != "99%"]
    write_stats(stats, [], headers=headers)

    with pytest.raises(ValueError, match="99% percentile"):
        load_metrics(stats)


def test_markdown_output_contains_all_metrics_and_metadata() -> None:
    metrics = [ScenarioMetrics("Agent", 10, 100, 200, 300, 1)]
    markdown = render_markdown(
        metrics,
        {
            "status": "通过",
            "users": 10,
            "spawn_rate": 2,
            "agent_base_url": "http://agent",
            "rag_base_url": "http://rag",
        },
        ["agent_chat: 1 次 - HTTP 500"],
    )

    assert "| Agent | 10 | 100.00 ms | 200.00 ms | 300.00 ms | 1 | 10.00% |" in markdown
    assert "并发用户数：10" in markdown
    assert "agent_chat: 1 次 - HTTP 500" in markdown


def test_blocked_markdown_does_not_print_fake_metrics() -> None:
    markdown = render_markdown(
        [],
        {
            "status": "阻塞",
            "reasons": ["服务不可达"],
            "verified": ["CSV 测试通过"],
            "unverified": ["正式 P95/P99"],
        },
    )

    assert "未生成真实性能数字" in markdown
    assert "服务不可达" in markdown
    assert "| 场景 | 请求数" not in markdown


def test_passing_validation_rejects_failures_and_unverified_users(tmp_path: Path) -> None:
    failures_path = tmp_path / "failures.csv"
    write_failures(failures_path)
    failing_metrics = [
        ScenarioMetrics("Agent", 3, 40, 70, 80, 3),
        ScenarioMetrics("RAG", 3, 50, 80, 90, 0),
        ScenarioMetrics("Aggregated", 6, 45, 75, 85, 3),
        ScenarioMetrics("Initialization", 10, 0, 0, 0, 0),
    ]

    with pytest.raises(ValueError, match="zero failures"):
        validate_passing_run(
            failing_metrics,
            passing_metadata(),
            failures_path,
            [],
        )

    passing_metrics = [
        ScenarioMetrics("Agent", 3, 40, 70, 80, 0),
        ScenarioMetrics("RAG", 3, 50, 80, 90, 0),
        ScenarioMetrics("Aggregated", 6, 45, 75, 85, 0),
        ScenarioMetrics("Initialization", 9, 0, 0, 0, 0),
    ]
    with pytest.raises(ValueError, match="Initialization success count"):
        validate_passing_run(passing_metrics, passing_metadata(), failures_path, [])


def test_passing_validation_rejects_missing_failures_csv() -> None:
    metrics = [
        ScenarioMetrics("Agent", 3, 40, 70, 80, 0),
        ScenarioMetrics("RAG", 3, 50, 80, 90, 0),
        ScenarioMetrics("Aggregated", 6, 45, 75, 85, 0),
        ScenarioMetrics("Initialization", 10, 0, 0, 0, 0),
    ]

    with pytest.raises(ValueError, match="failures CSV"):
        validate_passing_run(metrics, passing_metadata(), None, [])


def test_cli_generates_markdown(tmp_path: Path) -> None:
    stats = tmp_path / "benchmark_stats.csv"
    output = tmp_path / "result.md"
    metadata = tmp_path / "metadata.json"
    failures = tmp_path / "benchmark_failures.csv"
    write_failures(failures)
    write_stats(
        stats,
        [
            row("agent_chat", 3, 0, 40, 70, 80),
            row("rag_query", 3, 0, 50, 80, 90),
            row("Aggregated", 6, 0, 45, 75, 85),
            row("Initialization", 10, 0, 0, 0, 0),
        ],
    )
    metadata.write_text(json.dumps(passing_metadata(), ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmark.generate_report",
            "--stats",
            str(stats),
            "--metadata",
            str(metadata),
            "--failures",
            str(failures),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "| RAG | 3 |" in output.read_text(encoding="utf-8")


def test_cli_accepts_powershell_utf8_bom_metadata(tmp_path: Path) -> None:
    stats = tmp_path / "benchmark_stats.csv"
    output = tmp_path / "result.md"
    metadata = tmp_path / "metadata.json"
    failures = tmp_path / "benchmark_failures.csv"
    write_failures(failures)
    write_stats(
        stats,
        [
            row("agent_chat", 1, 0, 40, 70, 80),
            row("rag_query", 1, 0, 50, 80, 90),
            row("Aggregated", 2, 0, 45, 75, 85),
            row("Initialization", 10, 0, 0, 0, 0),
        ],
    )
    metadata.write_text(
        json.dumps(passing_metadata(), ensure_ascii=False),
        encoding="utf-8-sig",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmark.generate_report",
            "--stats",
            str(stats),
            "--metadata",
            str(metadata),
            "--failures",
            str(failures),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "状态：通过" in output.read_text(encoding="utf-8")


def test_cli_rejects_passing_report_without_both_scenarios(tmp_path: Path) -> None:
    stats = tmp_path / "benchmark_stats.csv"
    output = tmp_path / "result.md"
    metadata = tmp_path / "metadata.json"
    failures = tmp_path / "benchmark_failures.csv"
    write_failures(failures)
    write_stats(stats, [row("agent_chat", 3, 0, 40, 70, 80)])
    metadata.write_text(json.dumps(passing_metadata(), ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmark.generate_report",
            "--stats",
            str(stats),
            "--metadata",
            str(metadata),
            "--failures",
            str(failures),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "requires one Agent, RAG, Aggregated, and Initialization" in completed.stderr
    assert not output.exists()
