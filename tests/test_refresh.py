import yaml
from pathlib import Path

from x_algo_watch.cli import main
from x_algo_watch.evidence import digest
from x_algo_watch.models import CheckResult
from x_algo_watch.registry import refresh_manual
from x_algo_watch.storage import RecordStore
from datetime import date, datetime, timezone


def test_registry_change_with_same_upstream_reverifies(sample_repo, tmp_path):
    path, _, commit, _ = sample_repo
    registry = tmp_path / "facts.yaml"
    registry.write_text("[]\n", encoding="utf-8")
    output = tmp_path / "data"
    args = ["check", "--repo", str(path), "--to", commit, "--baseline", commit, "--registry", str(registry), "--output", str(output)]
    assert main(args) == 0
    fact = {"id": "weight", "check": "value", "value": 0.66, "source": {"path": "home-mixer/params/param.rs", "name": "FavoriteWeight"}}
    registry.write_text(yaml.safe_dump([fact]), encoding="utf-8")
    assert main(args) == 0
    verification = yaml.safe_load((output / "verification.yaml").read_text(encoding="utf-8"))
    assert verification["items"][0]["status"] == "mismatch"
    assert len(RecordStore(output).records()) == 1
    assert any(event["type"] == "verification" for event in RecordStore(output).events())


def test_manual_overdue_can_refresh_without_upstream_clone(tmp_path):
    path = tmp_path / "verification.yaml"
    path.write_text(yaml.safe_dump({"items": [{"id": "manual", "status": "manual"}]}), encoding="utf-8")
    facts = [{"id": "manual", "check": "manual", "manual_review": {"last_checked": "2026-09-21", "interval_days": 30}}]
    refresh_manual(tmp_path, facts, date(2026, 10, 22))
    assert yaml.safe_load(path.read_text())["items"][0]["status"] == "overdue"


def test_ambiguous_snippet_does_not_falsely_relocate(sample_repo):
    from x_algo_watch.engine import compare
    path, repo, base, commit = sample_repo
    (path / "reference.rs").write_text("const TEST: u32 = 1;\nconst TEST: u32 = 1;\n", encoding="utf-8")
    (path / "other.rs").write_text("const TEST: u32 = 1;\n", encoding="utf-8")
    fact = {"id": "snippet", "check": "snippet", "source": {"path": "reference.rs", "snippet": "const TEST: u32 = 1;"}}
    result = compare(repo, base, commit(), [fact], date(2026, 10, 22))
    assert result.verification[0]["status"] == "mismatch"
    assert result.verification[0]["path"] == "reference.rs"


def test_count_source_path_cannot_be_faked(sample_repo):
    from x_algo_watch.engine import compare
    _, repo, base, _ = sample_repo
    fact = {"id": "count", "check": "count", "value": 1, "source": {"path": "wrong.rs", "name": "filters.files"}}
    result = compare(repo, base, base, [fact], date(2026, 10, 22))
    assert result.verification[0]["status"] == "mismatch"
