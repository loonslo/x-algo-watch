"""固定 Gemma 的传输、缓存隔离及三审发布边界。"""

from copy import deepcopy
import io
import json

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import RecordedClient
from tests.test_mechanism_pipeline import SETTINGS, approval, setup_pipeline
from tests.test_reader_pipeline import reader_draft
from x_algo_watch.automation import publish
from x_algo_watch.drafting import OpenRouter, run
from x_algo_watch.mechanism_pipeline import validate_pipeline_event
from x_algo_watch.model_policy import FREE_ROUTER, GEMMA_MODEL, pipeline_models
from x_algo_watch.models import WatchError


@pytest.mark.parametrize("review", [False, True])
def test_gemma_transport_preserves_schema_in_payload_and_uses_json_mode(monkeypatch, review):
    requests = []
    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"model": GEMMA_MODEL, "choices": [{"message": {"content": "{}"}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    payload = {"research_protocol": "mechanisms-v1", "stage_schema": {"type": "object"}}
    if review:
        payload["stage_kind"] = "review"
    client = OpenRouter("test-key")
    client.call(GEMMA_MODEL, "仅返回精确协议 JSON", payload)
    body = requests[0]
    assert body["model"] == client.last_response_model == GEMMA_MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"] == {"require_parameters": True, "allow_fallbacks": False}
    assert json.loads(body["messages"][1]["content"])["stage_schema"] == payload["stage_schema"]
    assert body["max_tokens"] == (24000 if review else 12000)


def test_gemma_pipeline_rechecks_all_roles_and_preserves_router_budget(tmp_path, monkeypatch):
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    task = plan["tasks"][0]
    unit = task["task_id"]
    draft = reader_draft(task, mechanism, plan["plan_id"])
    values = [mechanism, *[approval(unit) for _ in range(3)], draft, *[approval(pack["item_id"]) for _ in range(3)]]
    output = tmp_path / "draft.json"
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    run(bundle, output, config, client=RecordedClient(deepcopy(values)))
    old = json.loads(output.read_text(encoding="utf-8"))
    assert old["events"][-1]["state"] == "reviewed"

    models = {"analysis": GEMMA_MODEL, "review": GEMMA_MODEL}
    # 当日已有八次旧路由调用；换型号不清零，先只留四次调用空间。
    config.write_text(yaml.safe_dump({**SETTINGS, "models": models, "allow_same_model": True, "daily_calls": 12}), encoding="utf-8")
    client = RecordedClient(deepcopy(values[:4]))
    run(bundle, output, config, client=client)
    partial = json.loads(output.read_text(encoding="utf-8"))
    assert len(client.calls) == 4
    assert partial["events"][-1]["iteration"]["phase"] == "waiting_budget"
    assert partial["events"][-1]["pipeline_profile"] != old["events"][-1]["pipeline_profile"]
    assert all(c[0] == GEMMA_MODEL for c in client.calls)

    config.write_text(yaml.safe_dump({**SETTINGS, "models": models, "allow_same_model": True, "daily_calls": 16}), encoding="utf-8")
    resumed = RecordedClient(deepcopy(values[4:]))
    run(bundle, output, config, client=resumed)
    payload = json.loads(output.read_text(encoding="utf-8"))
    event = payload["events"][-1]
    assert len(resumed.calls) == 4 and all(c[0] == GEMMA_MODEL for c in resumed.calls)
    assert event["state"] == "reviewed" and event["review_mode"] == "same_model"
    assert event["models"] == models
    assert validate_pipeline_event(event, pack, bundle / "data", payload["events"])
    assert sum(e["type"] == "model_call" for e in payload["events"]) == 16
    bad = deepcopy(payload["events"])
    next(c for c in bad if c["id"] == event["task_states"][unit]["analysis_call"])["model"] = FREE_ROUTER
    assert not validate_pipeline_event(event, pack, bundle / "data", bad)
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False, notify_issues=False)
    assert not (tmp_path / "digest.md").exists()


def test_fixed_same_model_requires_explicit_opt_in():
    with pytest.raises(WatchError, match="allow_same_model"):
        pipeline_models({"analysis": GEMMA_MODEL, "review": GEMMA_MODEL})
