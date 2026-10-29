import io
import json
from urllib.error import HTTPError

import pytest
import yaml

from x_algo_watch import model_probe
from x_algo_watch.models import WatchError
from x_algo_watch.evidence import digest


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_authentication_does_not_copy_key_details(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response(b'{"data":{"label":"private","hash":"private"}}'))
    result = model_probe.request_api("test-secret")
    assert result["authenticated"] is True
    assert "private" not in json.dumps(result)


def test_direct_http_error_retains_message_and_provider_without_leaking_secrets(monkeypatch):
    raw = json.dumps({"error": {"message": "Model is temporarily rate-limited upstream", "code": 429,
        "metadata": {"provider_name": "Google AI Studio", "raw": json.dumps({"error": {
            "message": "Quota exceeded test-secret", "status": "RESOURCE_EXHAUSTED"}})}}}).encode()
    def fail(*a, **k):
        raise HTTPError("https://openrouter.ai/api/v1/chat/completions", 429, "rate limit", {"Retry-After": "30"}, io.BytesIO(raw))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = model_probe.request_api("test-secret", {"model": model_probe.GEMMA_MODEL})
    assert result["status"] == 429 and result["retry_after_seconds"] == 30
    assert result["provider_name"] == "Google AI Studio"
    assert result["error_message"] == "Model is temporarily rate-limited upstream"
    assert "test-secret" not in json.dumps(result)
    assert result["provider_error_message"] == "Quota exceeded [redacted]"
    assert result["provider_error_status"] == "RESOURCE_EXHAUSTED"


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("model", [model_probe.GEMMA_MODEL, model_probe.FREE_ROUTER])
def test_canaries_reserve_budget_before_network_and_save_raw_response(tmp_path, monkeypatch, available, model):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    monkeypatch.setattr(model_probe, "load_receipt", lambda p: {})
    monkeypatch.setattr(model_probe.time, "sleep", lambda n: None)
    requests, intents = [], []
    class Budget:
        def __init__(self, *args): pass
        def record_intent(self, event):
            if not available:
                raise WatchError("budget exhausted")
            intents.append(event)
    def call(key, body=None):
        if body is None:
            return {"status": 200, "authenticated": True}
        assert len(intents) == len(requests) + 1
        requests.append(body)
        return {"status": 200, "content": "OK" if len(requests) == 1 else '{"ok":true}'}
    monkeypatch.setattr(model_probe, "BudgetClient", Budget)
    monkeypatch.setattr(model_probe, "request_api", call)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/watch.yaml").write_text(yaml.safe_dump({"models": {"analysis": model, "review": model},
        "allow_same_model": model != model_probe.FREE_ROUTER}), encoding="utf-8")
    output = tmp_path / "probe.json"
    if not available:
        with pytest.raises(WatchError):
            model_probe.run_probe(tmp_path / "receipt", tmp_path / "ledger", output)
        assert not requests
    else:
        model_probe.run_probe(tmp_path / "receipt", tmp_path / "ledger", output)
        result = json.loads(output.read_text(encoding="utf-8"))
        assert len(intents) == len(requests) == len(result["requests"]) == 2
        assert all(r["model"] == model for r in requests)
        assert "provider" not in requests[0] and "reasoning" not in requests[0] and "response_format" not in requests[0]
        assert requests[1]["provider"] == {"require_parameters": True, "allow_fallbacks": model == model_probe.FREE_ROUTER}
        assert requests[1]["response_format"]["type"] == ("json_schema" if model == model_probe.FREE_ROUTER else "json_object")
        assert result["requests"][0]["response"]["content"] == "OK"


@pytest.mark.parametrize("mismatch", [None, "input", "prompt"])
def test_review_replay_requires_original_input_prompt_and_model(tmp_path, monkeypatch, mismatch):
    commit = "a" * 40
    payload = {"stage_kind": "review", "research_protocol": "mechanisms-v1", "stage_schema": {"type": "object"}}
    prompt = "original review prompt"
    original = {"id": "source-404", "commit": commit, "response_status": 404, "stage": "mechanism_facts_review",
        "model": model_probe.FREE_ROUTER, "input_hash": digest(payload),
        "prompt_version": digest({"prompt": prompt, "schema": payload["stage_schema"], "transport": "mechanisms-v1"})}
    if mismatch == "input": original["input_hash"] = "wrong"
    if mismatch == "prompt": original["prompt_version"] = "wrong"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/watch.yaml").write_text(yaml.safe_dump({"models": {"analysis": model_probe.FREE_ROUTER,
        "review": model_probe.FREE_ROUTER}}), encoding="utf-8")
    monkeypatch.setattr(model_probe.RecordStore, "events", lambda self: [original])
    monkeypatch.setattr("x_algo_watch.project_evidence.context_packs", lambda _: [{"commit": commit}])
    def capture(pack, data, prior, client, *args):
        client.call(model_probe.FREE_ROUTER, prompt, payload)
    monkeypatch.setattr("x_algo_watch.mechanism_pipeline.run_pipeline", capture)
    if mismatch:
        with pytest.raises(WatchError, match="摘要不一致"):
            model_probe.review_request(commit)
    else:
        body, source = model_probe.review_request(commit)
        assert source["input_hash_matches"] is True and source["prompt_hash_matches"] is True
        assert body["max_tokens"] == 24000 and body["reasoning"] == {"effort": "low"}
        assert body["response_format"]["json_schema"]["strict"] is True
