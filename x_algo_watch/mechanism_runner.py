"""独立机制阶段的预算、缓存与失败恢复；结果不等于可发布正文。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

from .evidence import digest
from .mechanisms import mechanism_schema, validate_mechanisms
from .model_policy import FREE_ROUTER, MAX_DAILY_CALLS
from .models import Deferred, UnsafePublicText, WatchError
from .storage import public_text


def run_mechanism_tasks(plan: dict[str, Any], prior: list[dict[str, Any]], client: Any, now: datetime,
                        prompt_path: Path, *, daily_limit: int = MAX_DAILY_CALLS,
                        resume: bool = False, deadline: float | None = None,
                        persist: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(daily_limit) is not int or not 1 <= daily_limit <= MAX_DAILY_CALLS or now.tzinfo is None:
        raise WatchError("机制阶段需要有效共享日预算及带时区时间")
    if plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"}):
        raise WatchError("机制阶段拒绝摘要无效的证据计划")
    now = now.astimezone(timezone.utc)
    prompt = prompt_path.read_text(encoding="utf-8") + "\n" + json.dumps(mechanism_schema(), ensure_ascii=False)
    protocol = digest({"prompt": prompt, "schema": mechanism_schema(), "transport": "mechanism-json-schema-v1"})
    prior = list({e["id"]: e for e in prior}.values())
    used = sum(e.get("type") == "model_call" and e.get("day") == now.date().isoformat() for e in prior)
    events: list[dict[str, Any]] = []
    outputs = {}
    for item in plan["tasks"]:
        family = digest({"task": item["task_id"], "model": FREE_ROUTER, "protocol": protocol})
        known = [e for e in prior + events if e.get("call_family") == family]
        epoch = max((e.get("attempt_epoch", 0) for e in known), default=0)
        calls = [e for e in known if e.get("attempt_epoch", 0) == epoch]
        invalid = sum(e.get("outcome") == "invalid" for e in calls)
        failures = sum(e.get("outcome") in {"deferred", "started"} for e in calls)
        if resume and (invalid >= 2 or failures >= 3):
            epoch += 1
            calls, invalid, failures = [], 0, 0
        passed = next((e for e in reversed(calls) if e.get("valid") is True), None)
        if passed:
            if validate_mechanisms(passed.get("output"), item):
                raise WatchError("机制阶段缓存校验失败，拒绝复用")
            outputs[item["task_id"]] = {"state": "mechanisms_prepared", "output": passed["output"], "call_id": passed["id"]}
            continue
        if invalid >= 2 or failures >= 3:
            outputs[item["task_id"]] = {"state": "waiting_manual", "reason": "invalid_output" if invalid >= 2 else "service_retry_limit"}
            break
        key = digest({"family": family, "attempt_epoch": epoch})
        while invalid < 2:
            if used >= daily_limit or (deadline is not None and time.monotonic() >= deadline):
                outputs[item["task_id"]] = {"state": "pending", "reason": "daily_budget" if used >= daily_limit else "time_budget"}
                return outputs, events
            last_invalid = next((e for e in reversed(calls) if e.get("outcome") == "invalid"), None)
            payload = {"evidence_pack": {**item, "scope": "mechanism_task"}}
            if last_invalid:
                payload.update(previous_validation_errors=last_invalid.get("errors", []))
                if last_invalid.get("output") is not None:
                    payload["previous_invalid_output"] = last_invalid["output"]
            event = {"type": "model_call", "id": "mechanism:" + key + ":" + str(len(calls)) + ":" + now.isoformat(),
                     "analysis_key": key, "call_family": family, "attempt_epoch": epoch,
                     "item_id": item["task_id"], "commit": item["commit"], "plan_id": plan["plan_id"],
                     "stage": "mechanism", "model": FREE_ROUTER, "prompt_version": protocol,
                     "input_hash": digest(payload), "day": now.date().isoformat(), "valid": False}
            used += 1
            if hasattr(client, "record_intent"):
                client.record_intent(event)
            if persist:
                persist({**event, "outcome": "started", "errors": ["请求已记入预算，响应尚未保存"]})
            try:
                raw = client.call(FREE_ROUTER, prompt, payload)
                public_text(raw)
                event["raw_output"] = raw
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = None
                errors = validate_mechanisms(value, item)
                event.update(output=value, errors=errors, valid=not errors, outcome="invalid" if errors else "valid")
            except Deferred:
                event.update(outcome="deferred", errors=["机制请求暂未完成；保留阶段并从此处续跑"])
            except UnsafePublicText:
                event.pop("raw_output", None)
                event.pop("output", None)
                event.update(outcome="invalid", errors=["机制输出未通过公开文本扫描，原文不保存"])
            except WatchError:
                event.pop("raw_output", None)
                event.pop("output", None)
                event.update(outcome="invalid", errors=["机制请求或本地校验失败，未确认敏感内容命中"])
            event["resolved_model"] = getattr(client, "last_response_model", None)
            event["usage"] = getattr(client, "last_response_usage", {})
            public_text(json.dumps(event, ensure_ascii=False))
            if persist:
                persist(event)  # 每次调用后先落审计，不能等所有任务结束才保存。
            events.append(event)
            calls.append(event)
            if event["valid"]:
                outputs[item["task_id"]] = {"state": "mechanisms_prepared", "output": event["output"], "call_id": event["id"]}
                break
            if event["outcome"] == "deferred":
                failures += 1
                outputs[item["task_id"]] = {"state": "waiting_manual" if failures >= 3 else "pending", "reason": "service_retry_limit" if failures >= 3 else "service"}
                return outputs, events
            invalid += 1
        if invalid >= 2:
            outputs[item["task_id"]] = {"state": "waiting_manual", "reason": "invalid_output"}
            break
    return outputs, events
