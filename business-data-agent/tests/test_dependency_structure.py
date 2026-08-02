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
        normalized_name = name.split("[", 1)[0].lower().replace("_", "-")
        pins[normalized_name] = version
    return pins


def test_dependency_entrypoints_are_role_scoped():
    assert {path.name for path in ROOT.glob("requirements*.txt")} == {
        "requirements.txt",
        "requirements-dev.txt",
    }
    assert not (ROOT / "requirements-langchain.txt").exists()
    assert (ROOT / "requirements" / "README.md").is_file()
    assert (ROOT / "requirements" / "langchain-baseline.txt").is_file()


def test_daily_and_optional_profiles_are_pinned_and_self_contained():
    runtime = ROOT / "requirements.txt"
    dev = ROOT / "requirements-dev.txt"
    langchain = ROOT / "requirements" / "langchain-baseline.txt"

    assert _requirement_lines(dev)[0] == "-r requirements.txt"
    assert _requirement_lines(langchain)[0] == "-r ../requirements.txt"

    for path in (runtime, dev, langchain):
        for line in _requirement_lines(path):
            assert line.startswith("-r ") or line.count("==") == 1

    runtime_pins = _pins(runtime)
    optional_pins = _pins(langchain)
    assert "pytest" not in runtime_pins
    assert optional_pins == {
        "langchain": "1.3.13",
        "langchain-core": "1.4.9",
        "langchain-openai": "1.3.5",
    }
    assert {
        name: (runtime_pins[name], optional_pins[name])
        for name in runtime_pins.keys() & optional_pins.keys()
        if runtime_pins[name] != optional_pins[name]
    } == {}


def test_production_image_installs_runtime_only():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.txt ." in dockerfile
    assert "-r requirements.txt" in dockerfile
    assert "requirements-dev.txt" not in dockerfile
    assert "langchain-baseline.txt" not in dockerfile
