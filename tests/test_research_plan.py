from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from x_algo_watch.engine import compare
from x_algo_watch.evidence import digest
from x_algo_watch.models import WatchError
from x_algo_watch.research_plan import build_plan, load_plan_bundle, purpose, save_plan, save_plan_bundle, supplement_plan
from tests.test_history import repository_state


def make_change(sample_repo, *, large=False):
    path, repo, base, commit = sample_repo
    for index in range(22):
        target = path / "home-mixer/filters" / f"changed_{index}.rs"
        target.write_text(f"pub struct Example{index};\n", encoding="utf-8")
    (path / "asset.bin").write_bytes(b"\x00\xff")
    params = path / "home-mixer/params/param.rs"
    params.write_text(params.read_text(encoding="utf-8").replace('"enable", false', '"enable", true'), encoding="utf-8")
    if large:
        target = path / "home-mixer/filters/large.rs"
        target.write_text("".join(f"pub fn operation_{i}() {{}}\n" for i in range(900)), encoding="utf-8")
    result = compare(repo, base, commit(), [], date(2026, 10, 28))
    return repo, result.to_dict()


def test_inventory_covers_every_changed_file_and_never_claims_studied(sample_repo):
    repo, record = make_change(sample_repo)
    before = (repo.resolve("HEAD"), repo.git("status", "--porcelain"))
    plan = build_plan(repo, record, max_reference_hits=0)
    paths = set(repo.git("diff", "--name-only", record["base"], record["commit"], "--").splitlines())
    assert {row["path"] for row in plan["files"]} == paths
    assert len(plan["files"]) > 16 and plan["coverage_complete"]
    assert all(row["research_state"] == "not_researched" for row in plan["files"])
    binary = next(row for row in plan["files"] if row["path"] == "asset.bin")
    assert binary["disposition"] == "excluded" and binary["reason"]
    assert before == (repo.resolve("HEAD"), repo.git("status", "--porcelain"))


def test_large_change_is_partitioned_without_truncation_or_losing_added_lines(sample_repo):
    repo, record = make_change(sample_repo, large=True)
    plan = build_plan(repo, record, budget=4000, max_reference_hits=0)
    row = next(row for row in plan["files"] if row["path"].endswith("large.rs"))
    assert row["disposition"] == "needs_context" and len(row["task_ids"]) > 1
    selected = [t for t in plan["tasks"] if t["task_id"] in row["task_ids"]]
    entries = [e for t in selected for e in t["evidence"].values() if e["kind"] == "after_implementation"]
    assert "".join(e["text"] for e in entries) == repo.read(record["commit"], row["path"])
    assert all(not e["truncated"] for t in plan["tasks"] for e in t["evidence"].values())
    assert all(sum(len(e["text"]) for e in t["evidence"].values()) <= 4000 for t in plan["tasks"])
    assert selected[0]["presence"] == {"before_exists": False, "after_exists": True}


def test_previous_implementation_and_both_revision_references_are_retained(sample_repo):
    path, repo, base, commit = sample_repo
    source = path / "home-mixer/sources/source.rs"
    source.parent.mkdir()
    source.write_text("fn use_setting() { params.get(EnableExampleSource); }\n", encoding="utf-8")
    old = commit()
    params = path / "home-mixer/params/param.rs"
    params.write_text(params.read_text(encoding="utf-8").replace('"enable", false', '"enable", true'), encoding="utf-8")
    record = compare(repo, old, commit(), [], date(2026, 10, 28)).to_dict()
    plan = build_plan(repo, record)
    entries = [e for t in plan["tasks"] for e in t["evidence"].values()]
    assert any('"enable", false' in e["text"] and e["commit"] == old for e in entries)
    refs = [e for e in entries if e["path"].endswith("source.rs")]
    assert {e["commit"] for e in refs} == {record["base"], record["commit"]}
    assert all(e["production_reachability"] == "unverified" for e in entries)


def test_mock_and_test_markers_are_not_treated_as_production(sample_repo):
    path, repo, base, commit = sample_repo
    target = path / "home-mixer/filters/mock_filter.rs"
    target.write_text("struct MockQuery;\n#[cfg(test)]\nmod tests {}\n", encoding="utf-8")
    record = compare(repo, base, commit(), [], date(2026, 10, 28)).to_dict()
    plan = build_plan(repo, record, max_reference_hits=0)
    entry = next(e for t in plan["tasks"] for e in t["evidence"].values() if e["path"].endswith("mock_filter.rs"))
    assert entry["purpose"] == "mixed_or_unknown"
    assert set(entry["purpose_markers"]) == {"test", "mock"}
    assert purpose("enforcement_post.yaml", "# Prod uses a different threshold; this is a mock value\n") == {
        "purpose": "mixed_or_unknown", "purpose_markers": ["mock"], "production_reachability": "unverified"}


def test_supplement_is_same_revision_bounded_immutable_and_linked_to_issue(sample_repo, tmp_path):
    repo, record = make_change(sample_repo)
    plan = build_plan(repo, record, max_reference_hits=0)
    original = deepcopy(plan)
    request = {"issue_id": "I-missing-old", "path": "home-mixer/params/param.rs", "version": "before", "start_line": 1, "end_line": 3, "reason": "核对旧默认值"}
    updated = supplement_plan(repo, plan, [request])
    assert plan == original and updated["parent_plan_id"] == plan["plan_id"]
    assert updated["plan_id"] != plan["plan_id"]
    entry = next(iter(updated["tasks"][-1]["evidence"].values()))
    assert entry["commit"] == record["base"] and '"enable", false' in entry["text"]
    assert updated["tasks"][-1]["request"]["issue_id"] == request["issue_id"]
    old_path, new_path = save_plan(tmp_path, plan), save_plan(tmp_path, updated)
    stamp = old_path.stat().st_mtime_ns
    assert save_plan(tmp_path, plan) == old_path and old_path.stat().st_mtime_ns == stamp
    assert new_path.exists()
    with pytest.raises(WatchError, match="窗口"):
        supplement_plan(repo, plan, [{**request, "end_line": 999}])
    with pytest.raises(WatchError, match="相对路径"):
        supplement_plan(repo, plan, [{**request, "path": "../secret.rs"}])
    damaged = deepcopy(plan); damaged["files"] = []
    with pytest.raises(WatchError, match="摘要"):
        supplement_plan(repo, damaged, [request])


def test_snapshot_cannot_overwrite_other_contents(sample_repo, tmp_path):
    repo, record = make_change(sample_repo)
    plan = build_plan(repo, record, max_reference_hits=0)
    path = save_plan(tmp_path, plan)
    path.write_text("different content", encoding="utf-8")
    with pytest.raises(WatchError, match="覆盖"):
        save_plan(tmp_path, plan)


def test_sharded_snapshot_is_roundtrippable_and_rejects_missing_tasks(sample_repo, tmp_path):
    repo, record = make_change(sample_repo)
    plan = build_plan(repo, record, max_reference_hits=0)
    manifest = save_plan_bundle(tmp_path, plan)
    assert load_plan_bundle(manifest) == plan
    task_path = manifest.parent / "tasks" / (plan["tasks"][0]["task_id"] + ".json")
    task_path.write_text("{}", encoding="utf-8")
    with pytest.raises(WatchError, match="不完整或摘要"):
        load_plan_bundle(manifest)
    with pytest.raises(WatchError, match="覆盖"):
        save_plan_bundle(tmp_path, plan)


@pytest.mark.parametrize("commit", ["6258a15", "07d159b"])
def test_real_target_versions_cover_complete_git_inventory_without_modifying_upstream(upstream, tmp_path, commit):
    before = repository_state(upstream)
    target = upstream.resolve(commit)
    record = compare(upstream, upstream.parent(target), target, [], date(2026, 10, 28)).to_dict()
    plan = build_plan(upstream, record)
    expected = set(upstream.git("diff", "--no-renames", "--name-only", "-z", record["base"], target, "--").strip("\0").split("\0"))
    assert {row["path"] for row in plan["files"]} == expected
    assert all(row["reason"] and row["research_state"] == "not_researched" for row in plan["files"])
    assert all(sum(len(e["text"]) for e in item["evidence"].values()) <= plan["budget_per_task"] for item in plan["tasks"])
    assert all(not e["truncated"] for item in plan["tasks"] for e in item["evidence"].values())
    assert load_plan_bundle(save_plan_bundle(tmp_path, plan)) == plan
    assert before == repository_state(upstream)

@pytest.mark.parametrize('max_hits', [1, 8])
def test_batched_reference_search_preserves_individual_results(sample_repo, monkeypatch, max_hits):
    from x_algo_watch import research_plan as module
    repo, record = make_change(sample_repo)
    original = module.reference_tasks
    batched = build_plan(repo, record, max_reference_hits=max_hits)
    def unbatched(repo, base, commit, path, symbols, budget, max_hits, search_results=None):
        return original(repo, base, commit, path, symbols, budget, max_hits)
    monkeypatch.setattr(module, 'reference_tasks', unbatched)
    individual = build_plan(repo, record, max_reference_hits=max_hits)
    assert batched == individual


def test_plan_scans_each_revision_once(sample_repo, monkeypatch):
    from x_algo_watch import research_plan as module
    repo, record = make_change(sample_repo)
    calls = []
    original = module.reference_search
    def counted(repo, revision, symbols):
        calls.append(revision)
        return original(repo, revision, symbols)
    monkeypatch.setattr(module, 'reference_search', counted)
    build_plan(repo, record)
    assert calls == [record['base'], record['commit']]


def test_combined_search_matches_legacy_fixed_string_results(sample_repo):
    from x_algo_watch.research_plan import reference_search
    from x_algo_watch.git import run_git
    repo, record = make_change(sample_repo)
    symbols = ['Example1', 'Example10', 'Example2']
    args = ['grep', '-n', '-I', '-F', '--no-textconv']
    for symbol in symbols:
        args.extend(['-e', symbol])
    revision = record['commit']
    expected = run_git([*args, revision, '--', '*.rs', '*.py', '*.scala', '*.java', '*.go'], repo.path, allowed_codes=(0, 1)).decode('utf-8')
    assert reference_search(repo, revision, symbols) == expected
    assert reference_search(repo, revision, []) == ''
    with pytest.raises(WatchError):
        reference_search(repo, revision, ['.*'])
