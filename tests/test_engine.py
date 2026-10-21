from datetime import date
from pathlib import Path

import pytest

from x_algo_watch.engine import compare, snapshot
from x_algo_watch.models import Severity
from x_algo_watch.parsers import LEVEL_PATH, PARAM_PATH

TODAY = date(2026, 10, 21)


def change(path: Path, name: str, before: str, after: str):
    target = path / name
    target.write_text(target.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")


def test_default_change_and_file_evidence(sample_repo):
    path, repo, base, commit = sample_repo
    change(path, PARAM_PATH, '"enable", false', '"enable", true')
    rule = path / "abuse-enforcement-service/service-lib/rules/test.yaml"
    rule.parent.mkdir(parents=True)
    rule.write_text("enabled: true\n", encoding="utf-8")
    result = compare(repo, base, commit(), [], TODAY)
    assert result.complete and result.severity == Severity.RED
    finding = next(f for f in result.findings if f.category == "parameter")
    assert finding.before is False and finding.after is True and finding.line_after == 2
    assert any(f.category == "enforcement" and "+enabled: true" in f.evidence for f in result.findings)


@pytest.mark.parametrize("case", ["missing_levels", "unknown_levels", "unknown_param", "missing_readme", "missing_module"])
def test_parse_failure_is_red_and_incomplete(sample_repo, case):
    path, repo, base, commit = sample_repo
    if case == "missing_levels":
        (path / LEVEL_PATH).unlink()
    elif case == "unknown_levels":
        change(path, LEVEL_PATH, "&[&Rule]", "Unknown")
    elif case == "unknown_param":
        change(path, PARAM_PATH, '"favorite", 0.5', '"favorite", compute()')
    elif case == "missing_readme":
        change(path, "README.md", "Notable Updates", "Unrecognized")
    else:
        (path / "home-mixer/filters/example_filter.rs").unlink()
    result = compare(repo, base, commit(), [], TODAY)
    assert not result.complete and result.severity == Severity.RED
    assert any(f.category == "parse_failure" for f in result.findings)
    assert not any(f.category == "visibility" for f in result.findings)


def test_line_moves_snippet_relocation_and_manual_overdue(sample_repo):
    path, repo, base, commit = sample_repo
    change(path, PARAM_PATH, "\nparam!(", "\n\nparam!(")
    (path / "reference.rs").write_text("\nconst LIMIT: u32 = 2800;\n", encoding="utf-8")
    target = commit()
    facts = [
        {"id": "weight", "check": "value", "value": 0.5, "source": {"path": PARAM_PATH, "name": "FavoriteWeight"}},
        {"id": "constant", "check": "value", "value": 2800, "source": {"path": "reference.rs", "name": "LIMIT"}},
        {"id": "quote", "check": "snippet", "source": {"path": "old.rs", "snippet": "const LIMIT: u32 = 2800;"}},
        {"id": "monthly", "check": "manual", "source": {"path": "https://example.org"}, "manual_review": {"last_checked": "2026-09-20", "interval_days": 30}},
    ]
    result = compare(repo, base, target, facts, TODAY)
    states = {r["id"]: r for r in result.verification}
    assert states["weight"]["status"] == "pass" and states["weight"]["line"] == 5
    assert states["constant"]["actual"] == 2800
    assert states["quote"]["path"] == "reference.rs" and states["quote"]["line"] == 2
    assert states["monthly"]["status"] == "overdue"
    assert result.severity == Severity.YELLOW


def test_registry_mismatch_even_with_no_diff(sample_repo):
    _, repo, base, _ = sample_repo
    facts = [{"id": "weight", "check": "value", "value": 0.66, "source": {"path": PARAM_PATH, "name": "FavoriteWeight"}}]
    result = compare(repo, base, base, facts, TODAY)
    assert result.complete and result.severity == Severity.RED
    assert result.verification[0]["status"] == "mismatch"


def test_golden_corpus_alone_is_gray(sample_repo):
    path, repo, base, commit = sample_repo
    golden = path / "visibility-filtering/rules/golden_corpus/case.rs"
    golden.parent.mkdir(parents=True)
    golden.write_text("// expected result\n", encoding="utf-8")
    result = compare(repo, base, commit(), [], TODAY)
    assert result.complete and result.severity == Severity.GRAY
    assert any(f.category == "golden_corpus" for f in result.findings)


def test_snapshot_baseline_has_counts(sample_repo):
    _, repo, base, _ = sample_repo
    new = snapshot(repo, base)
    assert len(new.params) == 2 and len(new.filters) == len(new.modules) == 1
