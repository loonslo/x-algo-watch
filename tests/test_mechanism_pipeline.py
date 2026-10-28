from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import RecordedClient
from tests.test_mechanism_runner import plan_with_tasks
from tests.test_versions import record
from x_algo_watch.automation import data_files, publish, write_bundle
from x_algo_watch.drafting import run
from x_algo_watch.evidence import digest
from x_algo_watch.mechanism_pipeline import validate_pipeline_event
from x_algo_watch.mechanism_pipeline import validate_revision
from x_algo_watch.mechanism_reviews import combine_reviews, validate_review
from x_algo_watch.mechanisms import bind_claims
from x_algo_watch.project_evidence import save_context, save_context_snapshot
from x_algo_watch.research_plan import save_plan_bundle
from x_algo_watch.storage import RecordStore

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
SETTINGS = {"project_research": True, "models": {"analysis": "openrouter/free", "review": "openrouter/free"},
            "extra_review_rounds": 2, "daily_calls": 4, "max_candidate_versions": 5, "stagnation_limit": 2}


def setup_pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copytree(PROMPTS, tmp_path / "prompts")
    result = record(tmp_path)
    plan, values = plan_with_tasks()
    plan.update(base=result["base"], budget_per_task=60000, max_reference_hits=24, parent_plan_id=None,
                files=[{"path": plan["tasks"][0]["path"], "task_ids": [plan["tasks"][0]["task_id"]], "research_state": "not_researched"}],
                shared_context_task_ids=[], coverage_complete=True)
    plan["plan_id"] = digest({k: v for k, v in plan.items() if k != "plan_id"})
    manifest = save_plan_bundle(tmp_path / "data/research-plans", plan)
    pack = {"schema_version": 1, "scope": "project_version", "research_pipeline": "mechanisms-v1", "commit": result["commit"],
            "base": result["base"], "committed_at": result["committed_at"], "plan_id": plan["plan_id"],
            "plan_path": manifest.relative_to(tmp_path / "data").as_posix(), "evidence": {}, "derived": {}, "related_facts": [],
            "coverage": {"changed_source_files": 1, "diff_files_included": []}}
    pack["item_id"] = digest(pack)
    save_context(tmp_path / "data", pack)
    save_context_snapshot(tmp_path / "data", pack)
    config = tmp_path / "config/watch.yaml"; config.parent.mkdir()
    config.write_text(yaml.safe_dump(SETTINGS), encoding="utf-8")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, data_files(tmp_path / "data"), tmp_path / "data", test=False, code="uncommitted", result_code=0, research_only=pack["commit"])
    return plan, values[0], pack, bundle, config


def approval(item_id):
    return {"item_id": item_id, "agree": True, "issues": []}


def issue_for(task, value, *, category="inference", reason="推断前提需要修正"):
    claim = bind_claims(value, task, "plan")[0]
    return {"claim_id": claim["claim_id"], "field": "/claims/filter_activation/allowed_reader_claim", "problem_key": "unsupported_effect",
            "category": category, "reason": reason, "evidence_refs": ["E2"], "evidence_requests": []}


def test_main_drafting_publish_resumes_tasks_and_never_calls_mechanism_result_a_reader(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    unit = plan["tasks"][0]["task_id"]
    client = RecordedClient([value, *[approval(unit) for _ in range(3)]])
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    payload = json.loads(output.read_text(encoding="utf-8"))
    root = [e for e in payload["events"] if e["type"] == "version_research"][-1]
    assert len(client.calls) == 4 and root["iteration"]["phase"] == "waiting_budget"
    assert root["state"] == "pending" and "analysis" not in root
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    reused = RecordedClient([])
    run(bundle, output, config, client=reused)
    assert reused.calls == []
    progress = RecordStore(tmp_path / "data").last_success()
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False, notify_issues=False)
    assert RecordStore(tmp_path / "data").last_success() == progress
    calls = [e for e in RecordStore(tmp_path / "data").events() if e["type"] == "model_call"]
    assert len(calls) == 4 and len(list((tmp_path / "data/research-runs").glob("*/*.json"))) >= 6
    page = next((tmp_path / "versions").glob("20*.md")).read_text(encoding="utf-8")
    assert "预算" in page and "算法版本更新公告" not in page
    forged = {**root, "state": "pending_confirmation"}
    forged["id"] = "research:" + digest({k: v for k, v in forged.items() if k != "id"})
    assert not validate_pipeline_event(forged, pack, bundle / "data", payload["events"])


def test_format_failure_persists_each_attempt_before_waiting_for_explicit_resume(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 6}), encoding="utf-8")
    output = tmp_path / "draft.json"
    client = RecordedClient([{}, {}])
    run(bundle, output, config, client=client)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len([e for e in payload["events"] if e["type"] == "model_call"]) == 2
    assert payload["events"][-1]["state"] == "manual"
    run(bundle, output, config, client=RecordedClient([]))
    unit = plan["tasks"][0]["task_id"]
    recovered = RecordedClient([value, *[approval(unit) for _ in range(3)]])
    run(bundle, output, config, client=recovered, resume=True)
    events = json.loads(output.read_text(encoding="utf-8"))["events"]
    assert len(recovered.calls) == 4
    assert len([e for e in events if e["type"] == "model_call"]) == 6
    assert events[-1]["iteration"]["phase"] == "waiting_budget"


def test_structured_review_keeps_same_problem_identity_across_wording_and_requires_bounded_requests(tmp_path, monkeypatch):
    plan, value, *_ = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]
    first = issue_for(task, value)
    second = {**first, "reason": "相同前提，换一种表达指出"}
    reviews = {"review": {"item_id": task["task_id"], "agree": False, "issues": [first]},
               "facts_review": {"item_id": task["task_id"], "agree": False, "issues": [second]}}
    combined = combine_reviews(reviews)
    assert len(combined) == 1 and len(combined[0]["opinions"]) == 2
    assert not validate_review(reviews["review"], task["task_id"], value, task)
    bad = deepcopy(reviews["review"]); bad["issues"][0]["category"] = "evidence"
    assert validate_review(bad, task["task_id"], value, task)
    bad["issues"][0]["evidence_requests"] = [{"path": "../outside.rs", "version": "before", "start_line": 1, "end_line": 2, "reason": "missing"}]
    assert validate_review(bad, task["task_id"], value, task)


def test_revision_keeps_conclusion_identity_records_changes_and_repeats_all_three_reviews(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    issue = issue_for(task, value)
    objection = {"item_id": unit, "agree": False, "issues": [issue]}
    issues = combine_reviews({"review": objection})
    fixed = deepcopy(value); fixed["claims"][0]["allowed_reader_claim"] = ""
    revised = {"result": fixed, "closure_reasons": [{"issue_id": issues[0]["issue_id"], "reason": "撤回缺乏支持的用户影响，保留技术机制"}]}
    client = RecordedClient([value, objection, approval(unit), approval(unit), revised, *[approval(unit) for _ in range(3)]])
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    root = json.loads(output.read_text(encoding="utf-8"))["events"][-1]
    assert len(client.calls) == 8
    assert root["pipeline_loop"]["candidate"] == 2 and root["iteration"]["phase"] == "waiting_budget"
    history = root["pipeline_loop"]["history"]
    assert history[0]["issues"][0]["claim_id"] == bind_claims(fixed, task, plan["plan_id"])[0]["claim_id"]
    assert history[-1]["closure_reasons"] and history[-1]["changes"]
    assert len(history[-1]["review_calls"]) == 3
    for call in client.calls[5:]:
        assert "revision_feedback" not in call[2] and "previous_own_issues" in call[2]


def test_evidence_gap_stops_revision_and_keeps_other_roles_objections(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    issue = issue_for(task, value, category="evidence")
    issue["evidence_requests"] = [{"path": "pipeline.rs", "version": "before", "start_line": 1, "end_line": 4, "reason": "核对旧入口"}]
    objection = {"item_id": unit, "agree": False, "issues": [issue]}
    client = RecordedClient([value, approval(unit), objection, approval(unit)])
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    events = json.loads(output.read_text(encoding="utf-8"))["events"]
    root = events[-1]
    assert len(client.calls) == 4 and root["state"] == "insufficient_evidence"
    assert root["pipeline_loop"]["evidence_requests"][0]["task_id"] == unit
    assert root["pipeline_loop"]["evidence_requests"][0]["issue_id"].startswith("I-")
    assert validate_pipeline_event(root, pack, bundle / "data", events)


def test_expression_only_revision_cannot_change_mechanism_facts(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    issue = issue_for(task, value, category="expression")
    objection = {"item_id": unit, "agree": False, "issues": [issue]}
    identifier = combine_reviews({"review": objection})[0]["issue_id"]
    wrong = deepcopy(value)
    wrong["claims"][0]["activation"]["text"] = "擅自改动默认条件"
    revised = {"result": wrong, "closure_reasons": [{"issue_id": identifier, "reason": "已改写"}]}
    client = RecordedClient([value, objection, approval(unit), approval(unit), revised, revised])
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 6}), encoding="utf-8")
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    events = json.loads(output.read_text(encoding="utf-8"))["events"]
    assert events[-1]["state"] == "manual" and len(client.calls) == 6
    assert any("定位之外" in error for e in events if e["type"] == "model_call" for error in e.get("errors", []))


def test_interrupted_request_is_counted_and_audit_can_publish_without_claiming_completion(tmp_path, monkeypatch):
    plan, value, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 1}), encoding="utf-8")
    class InterruptedClient:
        def call(self, *args): raise KeyboardInterrupt
    output = tmp_path / "draft.json"
    with pytest.raises(KeyboardInterrupt):
        run(bundle, output, config, client=InterruptedClient())
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["events"][-1]["outcome"] == "started"
    assert payload["events"][0]["state"] == "pending"
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False, notify_issues=False)
    assert any(e.get("outcome") == "started" for e in RecordStore(tmp_path / "data").events())
    client = RecordedClient([])
    run(bundle, output, config, client=client)
    assert client.calls == []
    assert json.loads(output.read_text(encoding="utf-8"))["events"][-1]["iteration"]["phase"] == "waiting_budget"


def test_revision_cannot_hide_old_problem_by_replacing_the_mechanism_key(tmp_path, monkeypatch):
    plan, value, *_ = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]
    issue = combine_reviews({"review": {"issues": [issue_for(task, value)]}})[0]
    changed = deepcopy(value); changed["claims"][0]["key"] = "renamed_to_escape_review"
    reply = {"result": changed, "closure_reasons": [{"issue_id": issue["issue_id"], "reason": "已解决"}]}
    assert any("旧机制键" in e for e in validate_revision(reply, task, {"analysis": value, "issues": [issue]}))
