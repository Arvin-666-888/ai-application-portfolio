from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ScenarioMetrics:
    scenario: str
    requests: int
    average_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    failures: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.requests * 100 if self.requests else 0.0


def load_metrics(stats_path: Path) -> list[ScenarioMetrics]:
    if not stats_path.exists() or stats_path.stat().st_size == 0:
        return []

    with stats_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        required = {"Name", "Request Count", "Failure Count", "Average Response Time"}
        missing = required - fieldnames
        if missing:
            raise ValueError(f"Locust stats CSV is missing columns: {sorted(missing)}")

        p95_column = _percentile_column(fieldnames, "95%")
        p99_column = _percentile_column(fieldnames, "99%")
        metrics: list[ScenarioMetrics] = []
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            metrics.append(
                ScenarioMetrics(
                    scenario=_scenario_label(name),
                    requests=_integer(row.get("Request Count")),
                    average_ms=_number(row.get("Average Response Time")),
                    p95_ms=_number(row.get(p95_column)),
                    p99_ms=_number(row.get(p99_column)),
                    failures=_integer(row.get("Failure Count")),
                )
            )
        return metrics


def _percentile_column(fieldnames: set[str], percentile: str) -> str:
    candidates = (
        percentile,
        f"{percentile}ile",
        f"{percentile} Response Time",
    )
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    raise ValueError(
        f"Locust stats CSV has no {percentile} percentile column; available columns: "
        f"{sorted(fieldnames)}"
    )


def _scenario_label(name: str) -> str:
    normalized = name.strip().lower()
    if normalized == "agent_chat":
        return "Agent"
    if normalized == "rag_query":
        return "RAG"
    if normalized == "aggregated":
        return "Aggregated"
    return name


def _integer(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    return int(float(value))


def _number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def load_failure_summary(path: Path | None) -> list[str]:
    if path is None:
        return []
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError(f"Failures CSV is missing or empty: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        summaries = []
        for row in reader:
            error = (row.get("Error") or row.get("Exception") or "unknown failure").strip()
            name = (row.get("Name") or "unknown endpoint").strip()
            occurrences = (row.get("Occurrences") or "?").strip()
            summaries.append(f"{name}: {occurrences} 次 - {error}")
        return summaries


def validate_passing_run(
    metrics: Iterable[ScenarioMetrics],
    metadata: dict[str, object],
    failures_path: Path | None,
    failure_rows: list[str],
) -> None:
    rows = list(metrics)
    by_scenario: dict[str, ScenarioMetrics] = {}
    duplicates: set[str] = set()
    for item in rows:
        if item.scenario in by_scenario:
            duplicates.add(item.scenario)
        by_scenario[item.scenario] = item

    required = {"Agent", "RAG", "Aggregated", "Initialization"}
    missing = required - by_scenario.keys()
    if missing or duplicates:
        raise ValueError(
            "A passing report requires one Agent, RAG, Aggregated, and Initialization row; "
            f"missing={sorted(missing)}, duplicates={sorted(duplicates)}"
        )

    invalid = [
        item.scenario
        for item in by_scenario.values()
        if item.requests <= 0
        or item.failures != 0
        or item.failures > item.requests
        or any(
            value is None or not math.isfinite(value) or value < 0
            for value in (item.average_ms, item.p95_ms, item.p99_ms)
        )
    ]
    if invalid:
        raise ValueError(
            "A passing report requires positive requests, zero failures, and finite latency metrics; "
            f"invalid={sorted(invalid)}"
        )

    agent = by_scenario["Agent"]
    rag = by_scenario["RAG"]
    aggregate = by_scenario["Aggregated"]
    initialization = by_scenario["Initialization"]
    if aggregate.requests != agent.requests + rag.requests:
        raise ValueError("Aggregated request count must equal Agent plus RAG requests")
    if aggregate.failures != agent.failures + rag.failures:
        raise ValueError("Aggregated failure count must equal Agent plus RAG failures")

    if failures_path is None:
        raise ValueError("A passing report requires the Locust failures CSV")
    if failure_rows:
        raise ValueError("A passing report requires an empty failures CSV")

    users = metadata.get("users")
    spawn_rate = metadata.get("spawn_rate")
    run_time = str(metadata.get("run_time", "")).strip().lower()
    if not isinstance(users, int) or users < 10:
        raise ValueError("A passing report requires users >= 10")
    if initialization.requests != users or initialization.failures != 0:
        raise ValueError("Initialization success count must equal the target user count")
    if not isinstance(spawn_rate, (int, float)) or spawn_rate <= 0:
        raise ValueError("A passing report requires a positive spawn_rate")
    if run_time not in {"2m", "120s", "00:02:00"}:
        raise ValueError("A passing report requires a recorded 2-minute run")
    if str(metadata.get("mode", "")).strip().lower() not in {"real llm", "mock"}:
        raise ValueError("A passing report requires an explicit real LLM or mock mode")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_markdown(
    metrics: Iterable[ScenarioMetrics],
    metadata: dict[str, object],
    failures: Iterable[str] = (),
) -> str:
    rows = list(metrics)
    status = str(metadata.get("status", "阻塞"))
    lines = [
        "# 压测结果",
        "",
        f"状态：{status}",
        "",
    ]

    if status != "通过":
        lines.extend(
            [
                "未生成真实性能数字。",
                "",
                "## 原因",
                "",
            ]
        )
        reasons = metadata.get("reasons") or ["未完成 10 用户、2 分钟真实压测"]
        lines.extend(f"- {reason}" for reason in reasons)
        lines.extend(["", "## 已验证", ""])
        verified = metadata.get("verified") or []
        lines.extend(f"- {item}" for item in verified)
        lines.extend(["", "## 未验证", ""])
        unverified = metadata.get("unverified") or ["10 用户 2 分钟真实 P95/P99"]
        lines.extend(f"- {item}" for item in unverified)
    else:
        lines.extend(_metrics_table(rows))

    lines.extend(
        [
            "",
            "## 运行元数据",
            "",
            f"- 测试日期：{metadata.get('test_date', _utc_now())}",
            f"- 运行时间：{metadata.get('run_time', '未记录')}",
            f"- 并发用户数：{metadata.get('users', '未记录')}",
            f"- Spawn rate：{metadata.get('spawn_rate', '未记录')}",
            f"- Agent Base URL：{metadata.get('agent_base_url', '未记录')}",
            f"- RAG Base URL：{metadata.get('rag_base_url', '未记录')}",
            f"- 运行模式：{metadata.get('mode', '未记录')}",
            f"- 数据集/Conversation ID：{metadata.get('dataset', '未记录')}",
            f"- 硬件：{metadata.get('hardware', platform.platform())}",
            f"- Python：{metadata.get('python', platform.python_version())}",
            f"- 本机 Docker：{metadata.get('local_docker', '未记录')}",
            "- P95/P99 来源：Locust stats CSV 的 `95%` 与 `99%` 字段（生成器也校验兼容字段名）。",
            f"- Stats CSV SHA-256：{metadata.get('stats_sha256', '未记录')}",
            f"- Failures CSV SHA-256：{metadata.get('failures_sha256', '未记录')}",
            "",
        ]
    )
    notes = metadata.get("notes") or []
    if notes:
        lines.extend(["## 数据完整性说明", ""])
        lines.extend(f"- {item}" for item in notes)
        lines.append("")
    lines.extend(["## 失败摘要", ""])
    failure_rows = list(failures)
    default_failure_summary = (
        "提交的 failures CSV 只有表头。"
        if metadata.get("failures_sha256")
        else "无可用失败 CSV；正式压测未执行或没有失败。"
    )
    lines.extend(f"- {item}" for item in failure_rows or [default_failure_summary])
    return "\n".join(lines).rstrip() + "\n"


def _metrics_table(metrics: list[ScenarioMetrics]) -> list[str]:
    lines = [
        "| 场景 | 请求数 | 平均响应时间 | P95 | P99 | 失败数 | 失败率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in metrics:
        lines.append(
            "| {scenario} | {requests} | {average} | {p95} | {p99} | {failures} | {rate:.2f}% |".format(
                scenario=item.scenario,
                requests=item.requests,
                average=_ms(item.average_ms),
                p95=_ms(item.p95_ms),
                p99=_ms(item.p99_ms),
                failures=item.failures,
                rate=item.failure_rate,
            )
        )
    return lines


def _ms(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f} ms"


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate benchmark_result.md from Locust CSV output.")
    parser.add_argument("--stats", type=Path, help="Path to *_stats.csv")
    parser.add_argument("--failures", type=Path, help="Path to *_failures.csv")
    parser.add_argument("--metadata", type=Path, help="Optional JSON metadata file")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("benchmark_result.md"),
    )
    parser.add_argument("--status", choices=["通过", "部分通过", "阻塞"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metadata: dict[str, object] = {}
    if args.metadata:
        metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig"))
    if args.status:
        metadata["status"] = args.status

    metrics = load_metrics(args.stats) if args.stats else []
    failure_rows = load_failure_summary(args.failures)
    if args.stats:
        metadata["stats_sha256"] = file_sha256(args.stats)
    if args.failures:
        metadata["failures_sha256"] = file_sha256(args.failures)
    if metadata.get("status") == "通过":
        validate_passing_run(metrics, metadata, args.failures, failure_rows)

    markdown = render_markdown(metrics, metadata, failure_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
