from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import io
import json

from tests.test_drafting import RecordedClient
from tests.test_mechanisms import example
from x_algo_watch.evidence import digest
from x_algo_watch.mechanism_runner import run_mechanism_tasks
from x_algo_watch.models import Deferred
from x_algo_watch.drafting import OpenRouter
from x_algo_watch.mechanisms import mechanism_schema

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
PROMPT = Path(__file__).resolve().parents[1] / "prompts/mechanism-analysis-v1.md"


def plan_with_tasks(count=1):
    item, value = example()
    tasks, values = [], []
    for index in range(count):
        current = {**deepcopy(item), "path": f"source_{index}.rs"}
        current["task_id"] = "T-" + digest({k: v for k, v in current.items() if k != "task_id"})
        output = deepcopy(value)
        output["task_id"] = current["task_id"]
        output["segments"][0]["segment_id"] = current["task_id"]
        tasks.append(current); values.append(output)
    plan = {"schema_version": 1, "tasks": tasks, "commit": item["commit"]}
    plan["plan_id"] = digest(plan)
    return plan, values


def test_mechanisms_finish_before_reader_and_every_call_is_persisted():
    plan, values = plan_with_tasks()
    saved = []
    outputs, calls = run_mechanism_tasks(plan, [], RecordedClient(values), NOW, PROMPT, persist=saved.append)
    assert [e for e in saved if e["outcome"] != "started"] == calls and len(calls) == 1
    assert saved[0]["outcome"] == "started" and saved[0]["id"] == calls[0]["id"]
    assert outputs[plan["tasks"][0]["task_id"]]["state"] == "mechanisms_prepared"
    assert all("reader" not in call["output"] for call in calls)
    cached = RecordedClient([])
    reused, new = run_mechanism_tasks(plan, calls, cached, NOW, PROMPT)
    assert reused == outputs and new == [] and cached.calls == []


def test_daily_budget_counts_old_failed_calls_and_resumes_only_missing_task():
    plan, values = plan_with_tasks(2)
    old = [{"id": "old-request", "type": "model_call", "day": NOW.date().isoformat(), "outcome": "deferred", "model": "old/free-model:free"}]
    partial, calls = run_mechanism_tasks(plan, old, RecordedClient(values[:1]), NOW, PROMPT, daily_limit=2)
    assert len(calls) == 1
    assert partial[plan["tasks"][1]["task_id"]] == {"state": "pending", "reason": "daily_budget"}
    client = RecordedClient(values[1:])
    done, extra = run_mechanism_tasks(plan, old + calls, client, NOW + timedelta(days=1), PROMPT, daily_limit=2)
    assert len(extra) == len(client.calls) == 1
    assert all(row["state"] == "mechanisms_prepared" for row in done.values())


def test_two_invalid_responses_pause_and_explicit_resume_keeps_call_history():
    plan, values = plan_with_tasks()
    output, calls = run_mechanism_tasks(plan, [], RecordedClient([{}, {}]), NOW, PROMPT)
    assert len(calls) == 2 and output[plan["tasks"][0]["task_id"]]["state"] == "waiting_manual"
    output, none = run_mechanism_tasks(plan, calls, RecordedClient([]), NOW, PROMPT)
    assert none == []
    limited, none = run_mechanism_tasks(plan, calls, RecordedClient([]), NOW, PROMPT, resume=True, daily_limit=2)
    assert none == [] and limited[plan["tasks"][0]["task_id"]]["reason"] == "daily_budget"
    done, extra = run_mechanism_tasks(plan, calls, RecordedClient(values), NOW, PROMPT, resume=True, daily_limit=3)
    assert len(extra) == 1 and extra[0]["attempt_epoch"] == 1
    assert len(calls) == 2 and done[plan["tasks"][0]["task_id"]]["state"] == "mechanisms_prepared"


class UnavailableClient:
    def call(self, *args):
        raise Deferred("transport unavailable")


def test_three_service_failures_require_explicit_resume():
    plan, values = plan_with_tasks()
    prior = []
    for _ in range(3):
        outputs, events = run_mechanism_tasks(plan, prior, UnavailableClient(), NOW, PROMPT)
        prior += events
    assert len(prior) == 3 and outputs[plan["tasks"][0]["task_id"]] == {"state": "waiting_manual", "reason": "service_retry_limit"}
    _, none = run_mechanism_tasks(plan, prior, RecordedClient([]), NOW, PROMPT)
    assert none == []
    done, events = run_mechanism_tasks(plan, prior, RecordedClient(values), NOW, PROMPT, resume=True)
    assert events[0]["attempt_epoch"] == 1
    assert done[plan["tasks"][0]["task_id"]]["state"] == "mechanisms_prepared"


def test_mechanism_transport_uses_its_own_precise_schema(monkeypatch):
    sent = []
    def respond(request, **kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}], "model": "provider/actual-model"}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    client = OpenRouter("test-key")
    client.call("openrouter/free", "mechanism", {"evidence_pack": {"scope": "mechanism_task"}})
    assert sent[0]["response_format"]["json_schema"]["schema"] == mechanism_schema()
    assert sent[0]["provider"] == {"require_parameters": True, "allow_fallbacks": True}
    assert client.last_response_model == "provider/actual-model"
