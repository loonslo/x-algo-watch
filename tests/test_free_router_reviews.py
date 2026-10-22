"""免费路由、多轮裁决、额度恢复及发布边界；全部使用模拟响应。"""

import io
import json
import shutil

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import PROMPTS, RecordedClient
from tests.test_project_research import NOW, analysis, prepare, review
from x_algo_watch.automation import detect, publish
from x_algo_watch.drafting import Deferred, OpenRouter, analyze_items, run
from x_algo_watch.models import WatchError
from x_algo_watch.project_evidence import context_packs, pending_packs, project_pack, research_profile
from x_algo_watch.research import render_research, research_event, research_schema, review_schema, valid_research_event
from x_algo_watch.storage import RecordStore

MODELS = {"analysis": "openrouter/free", "review": "openrouter/free"}
OPTIONS = {"project_research": True, "extra_review_rounds": 2}


def invoke(pack, responses, prior=(), **options):
    client = RecordedClient(responses)
    outputs, events = analyze_items([pack], list(prior), client, MODELS, NOW, PROMPTS, **(OPTIONS | options))
    return outputs[pack["item_id"]], events, client


@pytest.mark.parametrize("actual", ["vendor/selected:free", None])
def test_router_transport_does_not_fix_model_and_records_actual_response(monkeypatch, actual):
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"model": actual, "choices": [{"message": {"content": "{}"}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    client = OpenRouter("test-key")
    assert client.call("openrouter/free", "prompt", {}) == "{}"
    assert client.last_response_model == actual
    assert requests[0]["model"] == "openrouter/free"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "reasoning" not in requests[0]


@pytest.mark.parametrize("model", ["openrouter/auto", "openrouter/anything", "vendor/paid"])
def test_paid_or_other_routers_remain_blocked(model):
    with pytest.raises(WatchError):
        OpenRouter("test-key").call(model, "prompt", {})


@pytest.mark.parametrize("is_review", [False, True])
def test_project_transport_sends_strict_schema_and_supported_free_providers(monkeypatch, is_review):
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"choices":[{"message":{"content":"{}"}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    payload = {"evidence_pack":{"scope":"project_version"}, **({"analysis":{}} if is_review else {})}
    OpenRouter("test-key").call("openrouter/free", "prompt", payload)
    body = requests[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == (review_schema() if is_review else research_schema())
    assert body["provider"] == {"require_parameters":True,"allow_fallbacks":True}
    assert body["model"] == "openrouter/free"
    if is_review:
        assert body['reasoning']=={'max_tokens':3000} and body['max_tokens']==12000
    else:
        assert 'reasoning' not in body and body['max_tokens']==6000


def test_failed_content_keeps_resolved_model_and_safe_usage_in_audit(sample_repo,monkeypatch):
    repo,result=prepare(sample_repo);pack=project_pack(repo,result.to_dict())
    responses=[{"model":"vendor/reasoner:free","choices":[{"finish_reason":"length","message":{"content":None}}],"usage":{"completion_tokens":6000,"completion_tokens_details":{"reasoning_tokens":6000},"untrusted":"ignored"}}]
    monkeypatch.setattr('urllib.request.urlopen',lambda *args,**kwargs:io.BytesIO(json.dumps(responses.pop(0)).encode()))
    _,events=analyze_items([pack],[],OpenRouter('test-key'),MODELS,NOW,PROMPTS,**OPTIONS)
    call=next(e for e in events if e['type']=='model_call')
    assert call['outcome']=='deferred' and call['resolved_model']=='vendor/reasoner:free'
    assert call['usage']=={'completion_tokens':6000,'reasoning_tokens':6000}


def test_three_reviews_are_independent_inputs_share_schema_and_all_required(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    output, events, client = invoke(pack, [analysis(pack), review(pack), review(pack), review(pack)])
    assert len(client.calls) == 4 and output["state"] == "pending_confirmation"
    assert set(output["reviews"]) == {"review", "facts_review", "reader_review"}
    assert all(payload == {"evidence_pack": pack, "analysis": analysis(pack)} for _, _, payload in client.calls[1:])
    assert len({system for _, system, _ in client.calls[1:]}) == 3
    assert all(json.dumps(research_schema(), ensure_ascii=False) in system for _, system, _ in client.calls)
    assert all(model == "openrouter/free" for model, _, _ in client.calls)
    assert all(e.get("resolved_model") is None for e in events if e["type"] == "model_call")
    event = research_event(pack, output, MODELS)
    assert valid_research_event(event, pack, required_extra_reviews=2)
    text = render_research(event)
    assert "免费自动路由" in text and "实际型号可能相同" in text
    assert "事实与遗漏核对：通过" in text and "未知" in text
    repeated, _, no_calls = invoke(pack, [], events)
    assert repeated == output and no_calls.calls == []


@pytest.mark.parametrize("failed_round", [0, 1, 2])
def test_any_review_can_block_no_majority_vote(sample_repo, failed_round):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    verdicts = [review(pack), review(pack), review(pack)]
    verdicts[failed_round] = review(pack, False)
    output, _, _ = invoke(pack, [analysis(pack), *verdicts])
    assert output["state"] == "disputed"
    assert not output["review"]["agree"]
    assert output["review"]["issues"] == review(pack, False)["issues"]
    text = render_research(research_event(pack, output, MODELS)).split("研究附录：")[0]
    assert analysis(pack)["reader"]["title"] not in text


def test_quota_and_transport_resume_do_not_skip_final_check(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    partial, first, _ = invoke(pack, [analysis(pack), review(pack), review(pack)], daily_limit=3)
    assert partial["state"] == "pending" and "review" not in partial
    assert "analysis" not in research_event(pack, partial, MODELS)
    partial, second, _ = invoke(pack, [Deferred("提供方拥堵")], first, daily_limit=5)
    assert partial["state"] == "pending"
    done, _, client = invoke(pack, [review(pack)], [*first, *second], daily_limit=5)
    assert len(client.calls) == 1 and "version-reader-review" in client.calls[0][1]
    assert done["state"] == "pending_confirmation"


def test_invalid_extra_review_retries_once_then_requires_manual(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    output, events, client = invoke(pack, [analysis(pack), review(pack), {}, {}])
    assert output["state"] == "manual" and "review" not in output
    assert len(client.calls) == 4 and client.calls[-1][2]["previous_validation_errors"]
    repeated, _, client = invoke(pack, [], events)
    assert repeated["state"] == "manual" and not client.calls


def test_time_boundary_saves_completed_stages_then_resumes(sample_repo, monkeypatch):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("x_algo_watch.drafting.time.monotonic", lambda: next(ticks))
    partial, events, client = invoke(pack, [analysis(pack), review(pack)], call_deadline=1.0)
    assert partial["state"] == "pending" and len(client.calls) == 2
    done, _, client = invoke(pack, [review(pack), review(pack)], events)
    assert done["state"] == "pending_confirmation" and len(client.calls) == 2


def test_router_actual_models_survive_cache_and_event(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    class RoutedClient(RecordedClient):
        def call(self, *args):
            raw = super().call(*args)
            self.last_response_model = "vendor/selected" + str(len(self.calls)) + ":free"
            return raw
    client = RoutedClient([analysis(pack), review(pack), review(pack), review(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, **OPTIONS)
    output = outputs[pack["item_id"]]
    assert output["resolved_models"]["reader_review"] == "vendor/selected4:free"
    cached, _, _ = invoke(pack, [], events)
    assert cached == output
    event = research_event(pack, output, MODELS)
    assert valid_research_event(event, pack, required_extra_reviews=2)
    assert "vendor/selected4:free" in render_research(event)


def test_publisher_rejects_missing_round_or_forged_aggregate(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    single, _, _ = invoke(pack, [analysis(pack), review(pack)], extra_review_rounds=0)
    assert not valid_research_event(research_event(pack, single, MODELS), pack, required_extra_reviews=2)
    done, _, _ = invoke(pack, [analysis(pack), review(pack), review(pack, False), review(pack)])
    done["review"] = review(pack); done["state"] = "pending_confirmation"
    assert not valid_research_event(research_event(pack, done, MODELS), pack, required_extra_reviews=2)


def test_rule_fingerprint_changes_when_review_count_or_router_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config/watch.yaml"; config.parent.mkdir()
    config.write_text(yaml.safe_dump({"models": MODELS, "extra_review_rounds": 1}), encoding="utf-8")
    old = research_profile()
    config.write_text(yaml.safe_dump({"models": MODELS, "extra_review_rounds": 2}), encoding="utf-8")
    changed = research_profile()
    assert changed != old
    config.write_text(yaml.safe_dump({"models": {"analysis": "vendor/one:free", "review": "vendor/two:free"}, "extra_review_rounds": 2}), encoding="utf-8")
    assert research_profile() != changed


def test_repair_reruns_every_review_even_when_analysis_is_unchanged(sample_repo):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    first, events, _ = invoke(pack, [analysis(pack), review(pack), review(pack, False), review(pack)])
    fixed, _, client = invoke(pack, [analysis(pack), review(pack), review(pack), review(pack)], events, repair_feedback={pack["item_id"]: first})
    assert len(client.calls) == 4 and fixed["state"] == "pending_confirmation"
    assert client.calls[0][2]["review_feedback"]["issues"]
    assert all("revision_context" in payload for _, _, payload in client.calls[1:])


@pytest.mark.parametrize("value", [True, -1, 3, "2"])
def test_invalid_review_round_config_rejected(sample_repo, value):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    with pytest.raises(WatchError, match="extra_review_rounds"):
        invoke(pack, [], extra_review_rounds=value)


@pytest.mark.parametrize("final_agrees", [True, False])
def test_free_router_two_candidate_limit_gates_reader(sample_repo, tmp_path, monkeypatch, final_agrees):
    repo, result = prepare(sample_repo)
    workspace = tmp_path / "watch"; workspace.mkdir(); monkeypatch.chdir(workspace)
    shutil.copytree(PROMPTS, workspace / "prompts")
    config = workspace / "config/watch.yaml"; config.parent.mkdir()
    config.write_text(yaml.safe_dump({"models": MODELS, "project_research": True, "extra_review_rounds": 2, "max_candidate_versions": 2}), encoding="utf-8")
    RecordStore(workspace / "data").append(result, NOW, "uncommitted")
    bundle = workspace / "bundle"; detect(bundle, source=repo.path, research_commit=result.commit)
    pack = context_packs(bundle / "data")[0]
    initial = analysis(pack); initial["reader"]["who"] = "影响对象错误标记"
    fixed = analysis(pack)
    objection = review(pack, False); objection["issues"][0]["field"] = "reader"
    model = RecordedClient([initial, review(pack), objection, review(pack), fixed, review(pack), review(pack), review(pack, final_agrees)])
    output = workspace / "draft.json"; run(bundle, output, config, client=model)
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False)
    assert len(model.calls) == 8
    event = next(e for e in RecordStore(workspace / "data").events() if e["type"] == "version_research")
    assert event["repair_attempted"] and len(event["reviews"]) == 3
    assert event["state"] == ("pending_confirmation" if final_agrees else "disputed")
    front = next((workspace / "versions").glob("20*.md")).read_text(encoding="utf-8").split("研究附录：")[0]
    assert "影响对象错误标记" not in front
    assert (fixed["reader"]["title"] in front) == final_agrees
    assert not pending_packs(workspace / "data")
    assert not (workspace / "digest.md").exists()


def test_partial_repair_persists_then_only_missing_two_checks_run(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo)
    workspace = tmp_path / "watch"; workspace.mkdir(); monkeypatch.chdir(workspace)
    shutil.copytree(PROMPTS, workspace / "prompts")
    config = workspace / "config/watch.yaml"; config.parent.mkdir()
    settings = {"models": MODELS, "project_research": True, "extra_review_rounds": 2, "daily_calls": 6}
    config.write_text(yaml.safe_dump(settings), encoding="utf-8")
    RecordStore(workspace / "data").append(result, NOW, "uncommitted")
    bundle = workspace / "first"; detect(bundle, source=repo.path, research_commit=result.commit)
    pack = context_packs(bundle / "data")[0]
    first = RecordedClient([analysis(pack), review(pack), review(pack, False), review(pack), analysis(pack), review(pack)])
    draft = workspace / "draft.json"; run(bundle, draft, config, client=first)
    publish(bundle, FakeGitHub(), draft_bundle=draft, push=False)
    stored = [e for e in RecordStore(workspace / "data").events() if e["type"] == "version_research"][-1]
    assert stored["state"] == "pending" and stored["repair_attempted"] and "analysis" not in stored
    assert len(first.calls) == 6 and pending_packs(workspace / "data")
    settings["daily_calls"] = 8
    config.write_text(yaml.safe_dump(settings), encoding="utf-8")
    later = workspace / "later"; detect(later, source=repo.path, research_commit=result.commit)
    second = RecordedClient([review(pack), review(pack)])
    run(later, draft, config, client=second)
    publish(later, FakeGitHub(), draft_bundle=draft, push=False)
    assert len(second.calls) == 2
    assert all("revision_context" in payload for _, _, payload in second.calls)
    stored = [e for e in RecordStore(workspace / "data").events() if e["type"] == "version_research"][-1]
    assert stored["state"] == "pending_confirmation" and stored["repair_attempted"]
    assert len([e for e in RecordStore(workspace / "data").events() if e["type"] == "model_call"]) == 8
