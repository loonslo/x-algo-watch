import json
import io
import urllib.error
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from x_algo_watch.drafting import Deferred, OpenRouter, analyze_items, assemble, validate_analysis
from x_algo_watch.evidence import items_for_record, weight_math
from x_algo_watch.models import CheckResult, Finding, Severity, WatchError

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
MODELS = {"analysis": "minimax/minimax-m3.1-flash:free", "review": "nvidia/nemotron-3-super-120b-a12b:free"}
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def record_and_pack():
    result = CheckResult("a" * 40, "b" * 40, NOW.isoformat(), findings=[
        Finding("parameter", Severity.ORANGE, "home-mixer/params/param.rs", "FavoriteWeight", "默认值变化", 0.6, 0.66, 317, 317),
        Finding("parameter", Severity.ORANGE, "home-mixer/params/param.rs", "NotInterestedWeight", "默认值变化", -47.52, -52.272, 415, 415),
    ], numeric_context=weight_math({"FavoriteWeight": 0.6, "NotInterestedWeight": -47.52}, {"FavoriteWeight": 0.66, "NotInterestedWeight": -52.272}))
    record = result.to_dict()
    return record, items_for_record(record)[0]


def analysis(pack):
    value = json.loads((Path(__file__).parent / "fixtures/model-analysis-v1.json").read_text(encoding="utf-8"))
    value["item_id"] = pack["item_id"]
    return value


class RecordedClient:
    """合成录制响应测试替身；没有真实模型请求。"""
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []

    def call(self, model, system, payload):
        self.calls.append((model, system, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response, ensure_ascii=False)


def review(pack, agree=True):
    return {"item_id": pack["item_id"], "agree": agree, "issues": [] if agree else [{"field": "summary", "reason": "该解释仍需人工核对", "evidence_refs": ["E1"]}]}


def test_exact_decimal_relationships():
    _, pack = record_and_pack()
    assert pack["derived"]["favorite_multiplier"] == "1.1"
    assert pack["derived"]["not_interested_multiplier"] == "1.1"
    assert pack["derived"]["negative_to_favorite_before"] == pack["derived"]["negative_to_favorite_after"] == "79.2"


@pytest.mark.parametrize("mutation", ["wrong_number", "wrong_name", "wrong_path", "no_evidence", "deployment", "experiment_off", "extra_field"])
def test_invalid_model_claims_are_blocked(mutation):
    _, pack = record_and_pack(); value = analysis(pack)
    if mutation == "wrong_number": value["summary"] += "倍率 999.9"
    if mutation == "wrong_name": value["summary"] += "UnknownWeight"
    if mutation == "wrong_path": value["summary"] += "fake/location.rs"
    if mutation == "no_evidence": value["facts"][0]["evidence_refs"] = ["E999"]
    if mutation == "deployment": value["summary"] += "平台确认已上线，所有人一定会受影响。"
    if mutation == "experiment_off": value["default_state"] = "disabled"
    if mutation == "extra_field": value["execute"] = "anything"
    assert validate_analysis(value, pack)


def test_analysis_retry_review_and_idempotence():
    record, pack = record_and_pack(); good = analysis(pack); bad = deepcopy(good)
    bad["summary"] += " 999.9"
    client = RecordedClient([bad, good, review(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS)
    assert len(client.calls) == 3
    assert client.calls[0][0] != client.calls[-1][0]
    assert "previous_validation_errors" in client.calls[1][2]
    assert outputs[pack["item_id"]]["state"] == "pending_confirmation"
    assert assemble(record, [pack], outputs)["ready"]
    no_calls = RecordedClient([])
    repeated, _ = analyze_items([pack], events, no_calls, MODELS, NOW, PROMPTS)
    assert repeated == outputs and not no_calls.calls


def test_quota_defers_review_then_resumes_without_repeating_analysis():
    record, pack = record_and_pack()
    client = RecordedClient([analysis(pack), Deferred("额度不足")])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS)
    assert outputs[pack["item_id"]]["state"] == "pending"
    assert assemble(record, [pack], outputs) is None
    next_client = RecordedClient([review(pack)])
    later, _ = analyze_items([pack], events, next_client, MODELS, NOW, PROMPTS)
    assert len(next_client.calls) == 1 and next_client.calls[0][0] == MODELS["review"]
    assert later[pack["item_id"]]["state"] == "pending_confirmation"


def test_daily_budget_only_evidence_no_calls():
    record, pack = record_and_pack(); client = RecordedClient([])
    prior = [{"type": "model_call", "day": NOW.date().isoformat()} for _ in range(50)]
    outputs, _ = analyze_items([pack], prior, client, MODELS, NOW, PROMPTS)
    assert not client.calls and assemble(record, [pack], outputs) is None


def test_disagreement_and_manual_failure_states():
    record, pack = record_and_pack()
    outputs, _ = analyze_items([pack], [], RecordedClient([analysis(pack), review(pack, False)]), MODELS, NOW, PROMPTS)
    assert outputs[pack["item_id"]]["state"] == "disputed"
    assert "有分歧" in assemble(record, [pack], outputs)["body"]
    outputs, events = analyze_items([pack], [], RecordedClient([{}, {}]), MODELS, NOW, PROMPTS)
    assert outputs[pack["item_id"]]["state"] == "manual"
    client = RecordedClient([])
    analyze_items([pack], events, client, MODELS, NOW, PROMPTS)
    assert not client.calls


def test_same_or_paid_model_rejected():
    _, pack = record_and_pack()
    for models in ({"analysis": "same:free", "review": "same:free"}, {"analysis": "paid", "review": "other:free"}):
        with pytest.raises(WatchError):
            analyze_items([pack], [], RecordedClient([]), models, NOW, PROMPTS)


@pytest.mark.parametrize(("message", "reason"), [("Rate limit exceeded: free-models-per-day", "每日额度"), ("Rate limit exceeded: free-models-per-min", "每分钟限流"), ("Model is temporarily rate-limited upstream", "提供方暂时拥堵"), ("Unrecognized failure", "原因未细分")])
def test_http_failure_classifies_without_echoing_server_text(monkeypatch, message, reason):
    secret = "sk-or-v1-" + "x" * 40

    def limited(*args, **kwargs):
        body = json.dumps({"error": {"message": message, "metadata": {"raw": secret}}}).encode()
        raise urllib.error.HTTPError("https://openrouter.ai/api/v1/chat/completions", 429, "limited", {"Retry-After": "60"}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", limited)
    with pytest.raises(Deferred) as caught:
        OpenRouter(secret).call(MODELS["analysis"], "prompt", {})
    assert reason in str(caught.value) and "60" in str(caught.value)
    assert secret not in str(caught.value) and message not in str(caught.value)
