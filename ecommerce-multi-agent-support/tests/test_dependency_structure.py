from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _requirement_lines(path):
        if line.startswith("-r "):
            continue
        name, version = line.split("==", 1)
        pins[name.lower().replace("_", "-")] = version
    return pins


def test_dependency_entrypoints_are_role_scoped():
    assert {path.name for path in ROOT.glob("requirements*.txt")} == {
        "requirements.txt",
        "requirements-dev.txt",
    }
    assert _requirement_lines(ROOT / "requirements-dev.txt")[0] == "-r requirements.txt"


def test_runtime_keeps_agent_frameworks_but_excludes_test_runner():
    runtime = ROOT / "requirements.txt"
    dev = ROOT / "requirements-dev.txt"

    for path in (runtime, dev):
        for line in _requirement_lines(path):
            assert line.startswith("-r ") or line.count("==") == 1

    runtime_pins = _pins(runtime)
    assert "pytest" not in runtime_pins
    assert runtime_pins["tzdata"] == "2026.3"
    assert runtime_pins["langgraph"] == "1.2.9"
    assert runtime_pins["langchain-core"] == "1.4.9"
    assert runtime_pins["langchain-openai"] == "1.3.5"
    assert _pins(dev) == {"pytest": "9.1.1"}


def test_production_image_installs_runtime_only():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.txt ." in dockerfile
    assert "-r requirements.txt" in dockerfile
    assert "requirements-dev.txt" not in dockerfile
