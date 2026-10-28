from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_mechanism_pipeline import SETTINGS, approval, setup_pipeline
from tests.test_mechanisms import example
from tests.test_project_research import prepare
from tests.test_reader_pipeline import reader_draft
from x_algo_watch.automation import data_files, publish, write_bundle
from x_algo_watch.context_tasks import assign_id, materialize, split_context, validate_graph, validate_task
from x_algo_watch.drafting import run
from x_algo_watch.evidence import digest
from x_algo_watch.mechanism_pipeline import validate_pipeline_event
from x_algo_watch.mechanisms import bind_claims, mechanism_schema, segment_ids
from x_algo_watch.models import WatchError
from x_algo_watch.pipeline_context import load_pipeline_plan, prepare_pipeline_context
from x_algo_watch.project_evidence import save_context, save_context_snapshot
from x_algo_watch.research_plan import fragment, load_plan_bundle, save_plan_bundle
from x_algo_watch.storage import RecordStore


def large_source(counter=False):
    body = "".join("// retained source context " + str(i) + " " + "x" * 100 + "\n" for i in range(1200))
    return body + ("// MOCK_THRESHOLD applies only to a test mock.\n" if counter else "// unrelated source context ends.\n")


def tree_setup(tmp_path, monkeypatch, counter=False):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    parent = plan["tasks"][0]
    old = parent["task_id"]
    source = large_source(counter)
    entry = fragment("mock_support.rs", plan["commit"], source, 1, "requested_context", "requested_window")
    nodes = split_context(parent, [(entry, {"issue_id": "I-mock", "reason": "核对阈值是否只是 mock，不得推为生产"})])
    parent["ancestor_task_ids"] = [old]
    parent["replaces_task_id"] = old
    assign_id(parent)
    plan["tasks"] = [*nodes, parent]
    plan["files"][0]["task_ids"] = [parent["task_id"]]
    plan["plan_id"] = digest({k: v for k, v in plan.items() if k != "plan_id"})
    manifest = save_plan_bundle(tmp_path / "data/research-plans", plan)
    pack.update(plan_id=plan["plan_id"], plan_path=manifest.relative_to(tmp_path / "data").as_posix())
    pack["item_id"] = digest({k: v for k, v in pack.items() if k != "item_id"})
    save_context(tmp_path / "data", pack)
    save_context_snapshot(tmp_path / "data", pack)
    write_bundle(bundle, data_files(tmp_path / "data"), tmp_path / "data", test=False, code="uncommitted", result_code=0, research_only=pack["commit"])
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 100}), encoding="utf-8")
    return plan, mechanism, pack, bundle, config, source


class ContextClient:
    """合成传输：验证全部证据流转与门槛，不声称模型语义质量。"""
    def __init__(self, plan_id, counter=False, disagree_merge=False, request_at_parent=False):
        self.plan_id, self.counter, self.disagree_merge = plan_id, counter, disagree_merge
        self.calls = []
        self.parent = None
        self.request_at_parent = request_at_parent
        self.parent_reviews = 0

    def call(self, model, prompt, payload):
        self.calls.append(payload)
        item = payload.get("evidence_pack")
        if payload.get("stage_kind") == "review":
            unit = item["task_id"] if item else payload["item_id"]
            if item and not item.get("context_kind") and self.request_at_parent:
                self.parent_reviews += 1
                if self.parent_reviews % 3 == 2:
                    return json.dumps({"item_id": unit, "agree": False, "issues": [{
                        "claim_id": bind_claims(payload["analysis"], item, self.plan_id)[0]["claim_id"],
                        "field": "/claims/filter_activation/activation/text", "problem_key": "missing_activation_context",
                        "category": "evidence", "reason": "FACT_ONLY 需补充实际条件",
                        "evidence_refs": ["E2"], "evidence_requests": [{"path": "support2.rs", "version": "after",
                            "start_line": 1, "end_line": 1201, "reason": "核对完整条件，不将 mock 推为生产"}]}]})
            if item and item.get("context_kind") == "merge" and self.disagree_merge:
                identifier = next(iter(segment_ids(item)))
                return json.dumps({"item_id": unit, "agree": False, "issues": [{
                    "claim_id": "", "field": "/segments/" + identifier + "/reason", "problem_key": "unresolved_context",
                    "category": "evidence", "reason": "仍须核对完整旧入口", "evidence_refs": [],
                    "evidence_requests": [{"path": "mock_support.rs", "version": "before", "start_line": 1, "end_line": 1, "reason": "核对旧入口"}]}]})
            return json.dumps(approval(unit))
        if item is None:
            assert self.parent is not None
            return json.dumps(reader_draft(*self.parent, self.plan_id), ensure_ascii=False)
        segments = [{"segment_id": i, "disposition": "researched", "reason": "合成验证：已逐段处理并保留原问题边界"} for i in sorted(segment_ids(item))]
        value = {"task_id": item["task_id"], "claims": [], "segments": segments}
        if item.get("context_kind") == "scan":
            for ref, entry in item["evidence"].items():
                for number, line in enumerate(entry["text"].splitlines(keepends=True), entry["start_line"]):
                    if "MOCK_THRESHOLD" in line:
                        c = deepcopy(example()[1]["claims"][0])
                        cite = {"evidence_ref": ref, "start_line": number, "end_line": number, "quote": line}
                        c.update(key="mock_threshold", verdict="source_fact", execution_scope="mock",
                                 before={"known": False, "behavior": "旧行为未核实", "citations": []},
                                 after={"known": True, "behavior": "阈值只在 mock 说明中", "citations": [cite]},
                                 entrypoint={"text": "mock 片段不能证明生产入口", "citations": [cite]},
                                 activation={"text": "仅核对当前 mock 用途", "citations": [cite]},
                                 support=[cite], counter_evidence=[cite], allowed_reader_claim="")
                        value["claims"] = [c]
        elif item.get("context_kind") == "merge":
            fields = mechanism_schema()["properties"]["claims"]["items"]["properties"]
            value["claims"] = [{k: deepcopy(c[k]) for k in fields} for report in item["context_catalog"] for c in report["claims"]]
        else:
            c = deepcopy(example()[1]["claims"][0])
            if self.counter:
                sources = [c for report in item["context_catalog"] for c in report["claims"]]
                assert sources and sources[0]["execution_scope"] == "mock"
                c.update(verdict="contradicted", execution_scope="mock", allowed_reader_claim="",
                         counter_evidence=sources[0]["counter_evidence"])
            value["claims"] = [c]
            self.parent = (item, value)
        if payload.get("revision_feedback"):
            return json.dumps({"result": value, "closure_reasons": [{"issue_id": i["issue_id"], "reason": "根据本轮完整上下文核对"} for i in payload["revision_feedback"]["issues"]]}, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)


def test_split_reads_every_source_line_and_preserves_original_identity_and_snapshot(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config, source = tree_setup(tmp_path, monkeypatch)
    scans = [t for t in plan["tasks"] if t.get("context_kind") == "scan"]
    assert len(scans) >= 4
    assert "".join(t["evidence"]["E1"]["text"] for t in scans) == source
    assert max(len(t["evidence"]["E1"]["text"]) for t in scans) <= 24000
    assert all(not t["evidence"]["E1"]["truncated"] for t in scans)
    assert load_plan_bundle(bundle / "data" / pack["plan_path"]) == plan
    assert plan["tasks"][-1]["claim_namespace"] == scans[0]["context_question"]["owner_namespace"]
    forged = deepcopy(plan); forged["tasks"].reverse()
    with pytest.raises(WatchError): validate_graph(forged)


def test_hierarchical_context_main_reader_and_publisher_preserve_all_gates_and_resume(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config, _ = tree_setup(tmp_path, monkeypatch)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 4}), encoding="utf-8")
    out = tmp_path / "draft.json"
    first = ContextClient(plan["plan_id"])
    run(bundle, out, config, client=first)
    partial = json.loads(out.read_text(encoding="utf-8"))["events"][-1]
    assert partial["pipeline_loop"]["reason"] == "daily_budget" and "analysis" not in partial
    assert len(first.calls) == 4
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 100}), encoding="utf-8")
    client = ContextClient(plan["plan_id"])
    run(bundle, out, config, client=client)
    payload = json.loads(out.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert root["state"] == "pending_confirmation"
    assert len(first.calls) + len(client.calls) == 4 * len(plan["tasks"]) + 4
    assert all(len(json.dumps(c, ensure_ascii=False)) <= 120000 for c in first.calls + client.calls)
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    parent = plan["tasks"][-1]["task_id"]
    assert root["task_states"][parent]["dependencies"]
    forged = deepcopy(root); forged["task_states"][parent]["dependencies"] = {}
    forged["id"] = "research:" + digest({k: v for k, v in forged.items() if k != "id"})
    assert not validate_pipeline_event(forged, pack, bundle / "data", payload["events"])
    damaged = deepcopy(payload["events"])
    next(e for e in damaged if e["type"] == "model_call")["evidence_view_hash"] = "different-view"
    assert not validate_pipeline_event(root, pack, bundle / "data", damaged)
    before = RecordStore(tmp_path / "data").last_success()
    publish(bundle, FakeGitHub(), draft_bundle=out, push=False, notify_issues=False)
    assert RecordStore(tmp_path / "data").last_success() == before
    assert not (tmp_path / "digest.md").exists()


def test_counterexample_in_last_chunk_survives_all_merges_and_prevents_reader(tmp_path, monkeypatch):
    plan, _, pack, bundle, config, _ = tree_setup(tmp_path, monkeypatch, counter=True)
    out = tmp_path / "draft.json"
    client = ContextClient(plan["plan_id"], counter=True)
    run(bundle, out, config, client=client)
    payload = json.loads(out.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert root["pipeline_loop"]["reason"] == "no_supported_claims"
    assert "analysis" not in root and not payload["drafts"]
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    parent = next(c["evidence_pack"] for c in reversed(client.calls) if c.get("evidence_pack") and not c["evidence_pack"].get("context_kind"))
    assert any("MOCK_THRESHOLD" in e["text"] for e in parent["evidence"].values())
    assert any(c["execution_scope"] == "mock" for r in parent["context_catalog"] for c in r["claims"])
    missing = deepcopy(client.parent[1])
    missing["segments"] = [s for s in missing["segments"] if not s["segment_id"].startswith("D-C-")]
    assert validate_task(missing, parent)


def test_disputed_merge_stops_before_original_mechanism_and_asks_for_git_evidence(tmp_path, monkeypatch):
    plan, _, pack, bundle, config, _ = tree_setup(tmp_path, monkeypatch)
    out = tmp_path / "draft.json"
    client = ContextClient(plan["plan_id"], disagree_merge=True)
    run(bundle, out, config, client=client)
    payload = json.loads(out.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert root["pipeline_loop"]["phase"] == "waiting_evidence"
    assert root["pipeline_loop"]["evidence_requests"]
    assert client.parent is None
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])


def test_actual_git_supplement_is_sharded_and_child_changes_rebind_all_ancestors(sample_repo, tmp_path):
    path, repo, _, commit = sample_repo
    support = path / "mock_support.rs"; source = large_source(True)
    support.write_text(source, encoding="utf-8")
    base = commit()
    repo, result = prepare((path, repo, base, commit))
    before = (repo.resolve("HEAD"), repo.git("status", "--porcelain"))
    data = tmp_path / "data"
    pack = prepare_pipeline_context(repo, result.to_dict(), data)
    original = load_pipeline_plan(data, pack)
    parent = next(t for t in original["tasks"] if t["path"] == "home-mixer/params/param.rs")
    request = {"task_id": parent["task_id"], "issue_id": "I-mock", "path": "mock_support.rs", "version": "after",
               "start_line": 1, "end_line": len(source.splitlines()), "reason": "核对完整 mock 条件"}
    updated = prepare_pipeline_context(repo, result.to_dict(), data, pack, [request])
    tree = load_pipeline_plan(data, updated)
    scans = [t for t in tree["tasks"] if t.get("context_kind") == "scan"]
    assert "".join(t["evidence"]["E1"]["text"] for t in scans) == source
    assert load_pipeline_plan(data, pack) == original
    root = next(t for t in tree["tasks"] if t.get("replaces_task_id") == parent["task_id"])
    assert root["claim_namespace"] == parent["claim_namespace"]
    second_request = {**request, "task_id": scans[0]["task_id"], "start_line": 1, "end_line": 2}
    next_pack = prepare_pipeline_context(repo, result.to_dict(), data, updated, [second_request])
    following = load_pipeline_plan(data, next_pack)
    validate_graph(following)
    assert following["files"] != tree["files"]
    assert root["task_id"] not in {t["task_id"] for t in following["tasks"]}
    assert before == (repo.resolve("HEAD"), repo.git("status", "--porcelain"))


@pytest.mark.parametrize("prefix", ["6258a15", "07d159b"])
def test_real_pilot_oversized_supplement_preserves_both_versions_without_truncation(upstream, tmp_path, prefix):
    from datetime import date
    from x_algo_watch.engine import compare
    from tests.test_history import repository_state
    before = repository_state(upstream)
    target = upstream.resolve(prefix)
    record = compare(upstream, upstream.parent(target), target, [], date(2026, 10, 28)).to_dict()
    data = tmp_path / "data"
    old = prepare_pipeline_context(upstream, record, data)
    original = load_pipeline_plan(data, old)
    parent = next(t for t in original["tasks"] if t.get("change_scope") == "changed_file")
    path = "botmaker/src/scala/com/twitter/botmaker/FunctionUnitGen.scala"
    requests = [{"task_id": parent["task_id"], "issue_id": "I-real-window", "path": path, "version": version,
                 "start_line": 1, "end_line": 2000, "reason": "只验证实际 Git 超大补证的两侧行号、完整性及快照，尚未模型研究"} for version in ("before", "after")]
    pack = prepare_pipeline_context(upstream, record, data, old, requests)
    plan = load_pipeline_plan(data, pack)
    scans = [t for t in plan["tasks"] if t.get("context_kind") == "scan"]
    assert len(scans) >= 8
    for revision in (record["base"], target):
        expected = "".join(upstream.read(revision, path).splitlines(keepends=True)[:2000])
        fragments = [e for t in scans for e in t["evidence"].values() if e["commit"] == revision]
        assert "".join(e["text"] for e in fragments) == expected
        assert fragments[0]["start_line"] == 1 and fragments[-1]["end_line"] == 2000
    assert max(sum(len(e["text"]) for e in t["evidence"].values()) for t in plan["tasks"]) <= 85000
    assert load_pipeline_plan(data, old) == original
    assert before == repository_state(upstream)


def test_second_large_snapshot_preserves_candidate_identity_and_role_own_questions(tmp_path, monkeypatch):
    plan, _, pack, bundle, config, _ = tree_setup(tmp_path, monkeypatch)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 1000}), encoding="utf-8")
    out = tmp_path / "first.json"
    run(bundle, out, config, client=ContextClient(plan["plan_id"], request_at_parent=True))
    old = json.loads(out.read_text(encoding="utf-8"))["events"][-1]
    assert old["pipeline_loop"]["phase"] == "waiting_evidence"
    publish(bundle, FakeGitHub(), draft_bundle=out, push=False, notify_issues=False)
    class ReadOnlySource:
        def read(self, revision, path):
            assert revision == pack["commit"] and path == "support2.rs"
            return large_source()
    record = RecordStore(tmp_path / "data").records()[0]
    current = prepare_pipeline_context(ReadOnlySource(), record, tmp_path / "data", pack, old["pipeline_loop"]["evidence_requests"])
    updated = load_pipeline_plan(tmp_path / "data", current)
    save_context(tmp_path / "data", current)
    save_context_snapshot(tmp_path / "data", current)
    next_bundle = tmp_path / "next-bundle"
    write_bundle(next_bundle, data_files(tmp_path / "data"), tmp_path / "data", test=False, code="uncommitted", result_code=0, research_only=pack["commit"])
    client = ContextClient(updated["plan_id"])
    output = tmp_path / "next.json"
    run(next_bundle, output, config, client=client)
    payload = json.loads(output.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert root["state"] == "pending_confirmation"
    assert root["pipeline_loop"]["candidate"] == 2 and root["pipeline_loop"]["context_previous"] == old["id"]
    assert validate_pipeline_event(root, current, next_bundle / "data", RecordStore(tmp_path / "data").events() + payload["events"])
    child_reviews = [c for c in client.calls if c.get("stage_kind") == "review" and c.get("evidence_pack", {}).get("context_kind") and "original_question" in c]
    assert child_reviews
    for index, call in enumerate(child_reviews):
        issues = call["original_question"]["issues"]
        assert bool(issues) == (index % 3 == 1)
        assert all("FACT_ONLY" in i["reason"] for i in issues)
        assert "activation" in call["original_question"]["claims"][0]
    parent = next(t for t in updated["tasks"] if t.get("change_scope") == "changed_file")
    old_claim = old["pipeline_loop"]["history"][-1]["issues"][0]["claim_id"]
    assert root["reader_claims"][0]["claim_id"] == old_claim
    assert root["pipeline_loop"]["history"][-1]["closure_reasons"]


def test_context_schema_export_matches_program():
    from x_algo_watch.context_tasks import task_schema
    path = Path(__file__).resolve().parents[1] / "schemas/context-research-v1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == task_schema({"context_kind": "scan"})


def test_context_report_cannot_publish_a_hypothesis_or_exceed_compact_boundary():
    item, value = example()
    item.update(context_kind="scan", change_scope="supporting_context")
    value["claims"][0]["allowed_reader_claim"] = ""
    assert any("最终机制推测" in e for e in validate_task(value, item))
    value["claims"][0]["verdict"] = "source_fact"
    value["claims"][0]["unknowns"] = ["未核实的生效条件" * 80 for _ in range(9)]
    assert any("6000" in e for e in validate_task(value, item))


def test_single_line_that_cannot_be_split_does_not_replace_original_snapshot(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    data = tmp_path / "data"
    pack = prepare_pipeline_context(repo, result.to_dict(), data)
    plan = load_pipeline_plan(data, pack)
    parent = plan["tasks"][0]
    class ReadOnlySource:
        def read(self, revision, path):
            return "x" * 90000
    request = {"task_id": parent["task_id"], "issue_id": "I-long-line", "path": "large.rs",
               "version": "after", "start_line": 1, "end_line": 1, "reason": "不能以字符截断伪造完整行"}
    with pytest.raises(WatchError, match="单行"):
        prepare_pipeline_context(ReadOnlySource(), result.to_dict(), data, pack, [request])
    assert load_pipeline_plan(data, pack) == plan
    assert len(list((data / "research-plans").glob("*/manifest.json"))) == 1


def test_leaf_evidence_gap_remains_visible_in_changed_file_ledger():
    from x_algo_watch.mechanism_pipeline import file_progress
    plan = {"files": [{"path": "source.rs", "task_ids": ["parent"]}]}
    leaf = {"analysis_call": "leaf", "state": "approved"}
    parent = {"analysis_call": "parent", "state": "approved", "dependencies": {"child": leaf}}
    calls = [{"type": "model_call", "id": "leaf", "output": {"segments": [{"disposition": "insufficient_evidence"}]}},
             {"type": "model_call", "id": "parent", "output": {"segments": [{"disposition": "researched"}]}}]
    result = file_progress(plan, {"parent": parent}, calls)
    assert result[0]["state"] == "missing_context"
    assert set(result[0]["audit_calls"]) == {"leaf", "parent"}
    plan["files"][0]["task_ids"] = []
    assert "必要任务" in file_progress(plan, {}, calls)[0]["reason"]
