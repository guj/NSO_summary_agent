"""Guardrails for nso_facts / nso_report package boundaries."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("openai", "agent.summarize", "fabric_openai")


def _scan(pkg: str) -> list[str]:
    root = _ROOT / pkg
    bad: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                bad.append(f"{path.relative_to(_ROOT)}:{token}")
    return bad


def test_nso_facts_has_no_llm_imports():
    assert _scan("nso_facts") == []


def test_nso_report_has_no_llm_imports():
    assert _scan("nso_report") == []


def test_nso_facts_does_not_import_nso_report():
    bad = []
    root = _ROOT / "nso_facts"
    for path in root.rglob("*.py"):
        if "nso_report" in path.read_text(encoding="utf-8"):
            bad.append(str(path.relative_to(_ROOT)))
    assert bad == []
