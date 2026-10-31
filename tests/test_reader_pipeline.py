from copy import deepcopy
import json

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import RecordedClient
from tests.test_mechanism_pipeline import SETTINGS, approval, setup_pipeline
from x_algo_watch.mechanism_reviews import combine_reviews
from x_algo_watch.automation import publish
from x_algo_watch.drafting import run
from x_algo_watch.evidence import digest
from x_algo_watch.mechanism_pipeline import validate_pipeline_event
from x_algo_watch.mechanisms import bind_claims
from x_algo_watch.reader_pipeline import narrative_fields, validate_reader_draft, verify_selection_receipts, selection_batches
from x_algo_watch.reader_pipeline import reader_schema
from x_algo_watch.mechanism_reviews import review_schema
from x_algo_watch.research import render_research
from x_algo_watch.storage import RecordStore


def reader_draft(task, mechanism, plan_id):
    claim = bind_claims(mechanism, task, plan_id)[0]
    identifier = claim["claim_id"]
    reader = {"title": "部分帖子可能多经过一次推荐筛选", "what_changed": "这次调整了推荐流程中一项筛选的默认启用设置。",
              "who": "可能涉及进入这一路推荐的帖子作者。", "when": "线上状态尚未核实，需要实际采用启用设置并运行对应入口。",
              "online_status": "unverified", "action": "当前材料不足以支持根据本次更新调整发布策略。",
              "impacts": [{"heading": "浏览推荐时可能多一道筛选", "before": "" if not claim["before"]["known"] else "此前代码设置默认关闭。",
                  "after": "当前代码默认启用；实际运行时才会加入这一筛选。", "effect": "准备推荐的帖子可能多经过一次筛选。",
                  "conditions": "采用启用设置并实际运行对应入口。", "limits": "尚不清楚最终展示内容会有何区别，不能推断曝光变化。", "evidence_refs": [identifier]}],
              "terms": [], "evidence_refs": [identifier]}
    return {"reader": reader, "field_claims": [{"field": field, "claim_ids": [identifier]} for field in sorted(narrative_fields(reader))], "closure_reasons": []}


def test_full_main_pipeline_publishes_only_after_mechanism_and_reader_three_roles(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    draft = reader_draft(task, mechanism, plan["plan_id"])
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    client = RecordedClient([mechanism, *[approval(unit) for _ in range(3)], draft, *[approval(pack["item_id"]) for _ in range(3)]])
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    payload = json.loads(output.read_text(encoding="utf-8"))
    root = payload["events"][-1]
    assert len(client.calls) == 8 and root["state"] == "reviewed"
    assert root["iteration"]["phase"] == "ready"
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    assert payload["drafts"][pack["commit"]]["ready"] is True
    progress = RecordStore(tmp_path / "data").last_success()
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False, notify_issues=False)
    assert not (tmp_path / "digest.md").exists() and RecordStore(tmp_path / "data").last_success() == progress
    page = next((tmp_path / "versions").glob("20*.md")).read_text(encoding="utf-8")
    assert draft["reader"]["title"] in page and "算法版本更新公告" in page
    assert "线上部署尚未核实" in page
    client = RecordedClient([])
    run(bundle, output, config, client=client)
    assert client.calls == []
    assert len(json.loads(output.read_text(encoding="utf-8"))["events"]) == len(payload["events"])


def test_reader_before_unknown_is_not_rendered_as_a_fabricated_before_after_table(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]
    mechanism["claims"][0]["before"] = {"known": False, "behavior": "旧行为尚不明确", "citations": []}
    draft = reader_draft(task, mechanism, plan["plan_id"])
    catalog = {c["claim_id"]: c for c in bind_claims(mechanism, task, plan["plan_id"])}
    assert not validate_reader_draft(draft, catalog)
    bad = deepcopy(draft); bad["reader"]["impacts"][0]["before"] = "以前没有过滤。"
    assert validate_reader_draft(bad, catalog)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=RecordedClient([mechanism, *[approval(task["task_id"]) for _ in range(3)], draft, *[approval(pack["item_id"]) for _ in range(3)]]))
    event = json.loads(output.read_text(encoding="utf-8"))["events"][-1]
    text = render_research(event, reader_only=True)
    assert "此前如何处理尚不明确" in text and "| 以前" not in text


def test_mock_or_contradicted_claim_cannot_support_reader_even_with_if(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]
    draft = reader_draft(task, mechanism, plan["plan_id"])
    claims = {c["claim_id"]: c for c in bind_claims(mechanism, task, plan["plan_id"])}
    claim = next(iter(claims.values())); claim.update(verdict="contradicted", execution_scope="mock", allowed_reader_claim="")
    draft["reader"]["impacts"][0]["effect"] = "如果线上使用这个测试值，帖子可能被过滤。"
    assert any("不允许" in error for error in validate_reader_draft(draft, claims))
    invented = deepcopy(draft); invented["reader"]["what_changed"] += "曝光提升 987654321。"
    assert any("数字不在" in error for error in validate_reader_draft(invented, claims))


def test_selection_receipts_cover_all_candidates_instead_of_dropping_outside_fixed_window(tmp_path, monkeypatch):
    plan, mechanism, *_ = setup_pipeline(tmp_path, monkeypatch)
    first = bind_claims(mechanism, plan["tasks"][0], plan["plan_id"])[0]
    claims = {f"C-{index:024x}": {**deepcopy(first), "claim_id": f"C-{index:024x}"} for index in range(41)}
    calls, ids, pool = {}, [], list(claims.values())
    while len(pool) > 3:
        reduced = []
        for batch in selection_batches(pool):
            selected = [c["claim_id"] for c in batch[:2]]
            name = "select-" + str(len(ids)); ids.append(name)
            calls[name] = {"valid": True, "stage": "reader_selection", "item_id": "selection:" + digest(batch),
                "output": {"selected_ids": selected, "decisions": [{"claim_id": c["claim_id"], "disposition": "candidate" if c["claim_id"] in selected else "technical_only", "reason": "按读者相关性筛选，技术发现保留"} for c in batch]}}
            reduced.extend(claims[c] for c in selected)
        pool = reduced
    assert verify_selection_receipts(claims, ids, calls) == [c["claim_id"] for c in pool]
    calls[ids[0]]["output"]["decisions"].pop()
    with pytest.raises(ValueError): verify_selection_receipts(claims, ids, calls)


def test_reader_revision_rechecks_all_roles_and_preserves_closure_and_precise_differences(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    draft = reader_draft(task, mechanism, plan["plan_id"])
    claim_id = draft["reader"]["evidence_refs"][0]
    issue = {"claim_id": claim_id, "field": "/reader/impacts/0/effect", "problem_key": "explain_intermediate_limit", "category": "expression",
             "reason": "清楚说明筛选不等于最终展示", "evidence_refs": [claim_id], "evidence_requests": []}
    objection = {"item_id": pack["item_id"], "agree": False, "issues": [issue]}
    identifier = combine_reviews({"reader_review": objection})[0]["issue_id"]
    fixed = deepcopy(draft)
    fixed["reader"]["impacts"][0]["effect"] = "帖子可能多经过一道筛选，这不代表最终会展示。"
    fixed["closure_reasons"] = [{"issue_id": identifier, "reason": "仅明确中间筛选与最终展示的区别，未改机制或条件"}]
    responses = [mechanism, *[approval(unit) for _ in range(3)], draft, approval(pack["item_id"]), approval(pack["item_id"]), objection,
                 *[approval(unit) for _ in range(3)], fixed, *[approval(pack["item_id"]) for _ in range(3)]]
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 15}), encoding="utf-8")
    client = RecordedClient(responses); output = tmp_path / "draft.json"
    run(bundle, output, config, client=client)
    payload = json.loads(output.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert len(client.calls) == 15 and root["state"] == "reviewed"
    history = root["pipeline_loop"]["reader_history"]
    assert len(history) == 2 and history[1]["changes"] and history[1]["closure_reasons"]
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    forged = deepcopy(root); forged["analysis"]["reader"]["action"] = "随意改动未审核的建议"
    forged["id"] = "research:" + digest({k: v for k, v in forged.items() if k != "id"})
    assert not validate_pipeline_event(forged, pack, bundle / "data", payload["events"])


def test_test_only_change_can_publish_an_honest_summary_without_manufactured_user_scenarios(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]; unit = task["task_id"]
    mechanism["claims"][0].update(verdict="source_fact", execution_scope="mock", allowed_reader_claim="")
    draft = reader_draft(task, mechanism, plan["plan_id"])
    draft["reader"].update(title="测试设置有调整，用户影响尚不能判断", what_changed="这次改动涉及测试中的启用设置，尚不足以判断实际使用体验会如何变化。",
        who="暂不能确定哪些用户或内容会受影响。", when="线上状态尚未核实，测试设置不能证明生产采用。", impacts=[])
    identifier = draft["reader"]["evidence_refs"][0]
    draft["field_claims"] = [{"field": field, "claim_ids": [identifier]} for field in sorted(narrative_fields(draft["reader"]))]
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=RecordedClient([mechanism, *[approval(unit) for _ in range(3)], draft, *[approval(pack["item_id"]) for _ in range(3)]]))
    payload = json.loads(output.read_text(encoding="utf-8")); root = payload["events"][-1]
    assert root["state"] == "reviewed" and root["analysis"]["reader"]["impacts"] == []
    assert validate_pipeline_event(root, pack, bundle / "data", payload["events"])
    text = render_research(root, reader_only=True)
    assert "测试设置" in text and "你可能感受到的变化" not in text


def test_new_published_protocols_match_program_schemas():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name, schema in (("reader-research-v1.json", reader_schema()), ("mechanism-review-v1.json", review_schema())):
        assert json.loads((root / "schemas" / name).read_text(encoding="utf-8")) == schema
