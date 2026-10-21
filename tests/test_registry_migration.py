from datetime import date
from pathlib import Path

import yaml

from x_algo_watch.engine import compare, snapshot
from x_algo_watch.registry import load_registry, verify

ROOT = Path(__file__).resolve().parents[1]


def test_registry_schema_and_public_code_claims():
    facts = load_registry(ROOT / "registry/facts.yaml")
    assert len(facts) >= 420
    assert len({fact["id"] for fact in facts}) == len(facts)
    assert all(fact["evidence"] in "ABCD" for fact in facts)
    assert all(fact["basis_commit"] and isinstance(fact["claim"], str) for fact in facts)
    assert any(fact.get("kind") == "ignore" and fact["note"] for fact in facts)
    assert all(fact["manual_review"]["last_checked"] is None for fact in facts if fact["check"] == "manual")


def test_all_29_migration_rows_match_independent_report_expectations(upstream):
    facts = load_registry(ROOT / "registry/facts.yaml")
    manifest = yaml.safe_load((ROOT / "registry/migrations.yaml").read_text(encoding="utf-8"))
    results, findings = verify(upstream, snapshot(upstream, manifest["target_commit"]), facts, date(2026, 10, 21))
    indexed = {result["id"]: result for result in results}
    assert len(manifest["rows"]) == 29
    for row in manifest["rows"]:
        for interval in row["ranges"]:
            anchors = [indexed[identifier] for identifier in interval["anchors"]]
            assert all(anchor["status"] == "pass" for anchor in anchors), row
            assert [anchors[0]["reference_start"], anchors[-1]["reference_end"]] == interval["expected"], row
    assert not [finding for finding in findings if finding.category == "registry_mismatch"]
    assert manifest["coverage"]["unresolved_paths"] == []
    assert manifest["coverage"]["requires_manual_mapping"] == []


def test_sync_date_and_rule_delta_exact(upstream):
    target = upstream.resolve("74d49a4")
    result = compare(upstream, upstream.resolve("862a123"), target, [], date(2026, 10, 21))
    sync = next(f for f in result.findings if f.category == "sync")
    assert sync.before.startswith("2026-10-16") and sync.after.startswith("2026-10-19")
    extra = next(f for f in result.findings if f.category == "derived_count")
    assert (extra.before, extra.after) == (26, 27)
