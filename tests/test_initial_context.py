from pathlib import Path
from copy import deepcopy

from x_algo_watch.pipeline_context import split_initial_context
from x_algo_watch.research_plan import load_plan_bundle
from x_algo_watch.context_tasks import validate_graph


def test_real_initial_windows_are_preserved_in_reviewable_tree():
    root = Path(__file__).resolve().parents[1]
    manifest = next((root / "data/research-plans").glob("6258*-fdaa*/manifest.json"))
    original = load_plan_bundle(manifest)
    before = deepcopy(original)
    result = split_initial_context(original)
    assert original == before
    assert result["parent_plan_id"] == original["plan_id"]
    validate_graph(result)
    assert split_initial_context(result) == result
    by_id = {t["task_id"]: t for t in result["tasks"]}
    for old in original["tasks"]:
        refs = [e for e in old["evidence"].values() if e["kind"] == "definition_or_reference"]
        if len(refs) <= 8:
            continue
        parent = next(t for t in result["tasks"] if t.get("replaces_task_id") == old["task_id"])
        assert parent["claim_namespace"] == old["claim_namespace"]
        assert all(e["kind"] != "definition_or_reference" for e in parent["evidence"].values())
        leaves = []
        def walk(identifier):
            node = by_id[identifier]
            if node["context_kind"] == "scan":
                assert len(node["evidence"]) <= 8
                assert sum(len(e["text"]) for e in node["evidence"].values()) <= 24000
                assert sorted(r for s in node["segments"] for r in s["evidence_refs"]) == sorted(node["evidence"])
                leaves.extend(node["evidence"].values())
            for child in node["context_dependencies"]:
                walk(child)
        for identifier in parent["context_dependencies"]:
            walk(identifier)
        assert sorted(e["source_digest"] for e in refs) == sorted(e["origin_source_digest"] for e in leaves)
        assert all(node["change_scope"] == "supporting_context" for node in by_id.values() if node.get("context_kind"))
