"""新研究链共用的可恢复调用边界；所有角色共用旧、新调用日预算。"""

from datetime import datetime, timezone
import json
import re
import time
from typing import Any, Callable

from .evidence import digest
from .model_policy import FREE_ROUTER, MAX_DAILY_CALLS
from .models import Deferred, UnsafePublicText, WatchError
from .storage import public_text, public_json

PIPELINE = "mechanisms-v1"


def call_stage(*, unit_id: str, commit: str, stage: str, payload: dict[str, Any], schema: dict[str, Any],
               validate: Callable[[Any], list[str]], prompt: str, prior: list[dict[str, Any]], client: Any,
               now: datetime, daily_limit: int = MAX_DAILY_CALLS, resume: bool = False,
               deadline: float | None = None, persist: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(daily_limit) is not int or not 1 <= daily_limit <= MAX_DAILY_CALLS or now.tzinfo is None:
        raise WatchError("研究阶段需要有效共享日预算及带时区时间")
    now = now.astimezone(timezone.utc)
    prompt += "\n仅输出此精确协议规定的 JSON：\n" + json.dumps(schema, ensure_ascii=False)
    version = digest({"prompt": prompt, "schema": schema, "transport": PIPELINE})
    family = digest({"unit": unit_id, "stage": stage, "payload": payload, "version": version, "model": FREE_ROUTER})
    prior = list({e["id"]: e for e in prior}.values())
    family_calls = [e for e in prior if e.get("call_family") == family]
    epoch = max((e.get("attempt_epoch", 0) for e in family_calls), default=0)
    calls = [e for e in family_calls if e.get("attempt_epoch", 0) == epoch]
    invalid = sum(e.get("outcome") == "invalid" for e in calls)
    failed = sum(e.get("outcome") in {"deferred", "started"} for e in calls)
    if resume and (invalid >= 2 or failed >= 3):
        epoch += 1
        calls, invalid, failed = [], 0, 0
    passed = next((e for e in reversed(calls) if e.get("valid") is True), None)
    if passed:
        if validate(passed.get("output")):
            raise WatchError("研究阶段缓存校验失败")
        return {"state": "complete", "output": passed["output"], "call_id": passed["id"]}, []
    if invalid >= 2 or failed >= 3:
        return {"state": "waiting_manual", "reason": "invalid_output" if invalid >= 2 else "service_retry_limit"}, []
    events = []
    used = sum(e.get("type") == "model_call" and e.get("day") == now.date().isoformat() for e in prior)
    while invalid < 2:
        if used >= daily_limit or (deadline is not None and time.monotonic() >= deadline):
            return {"state": "pending", "reason": "daily_budget" if used >= daily_limit else "time_budget"}, events
        request = {**payload, "research_protocol": PIPELINE, "stage_schema": schema}
        pack = payload.get("evidence_pack")
        if isinstance(pack, dict) and "evidence" in pack and "task_id" in pack:
            from .mechanisms import segment_ids
            request["required_segment_ids"] = sorted(segment_ids(pack))
            request["segment_contract"] = "仅 required_segment_ids 各一次；shared_context_catalog 不是已读取的证据区段。quote 使用连续完整源码行，JSON 中换行写成 \\n，禁止删行、拼接或省略。"
        previous = next((e for e in reversed(calls) if e.get("outcome") == "invalid"), None)
        if previous:
            request["previous_validation_errors"] = previous.get("errors", [])
            if previous.get("output") is not None:
                request["previous_invalid_output"] = previous["output"]
        event = {"type": "model_call", "pipeline_protocol": PIPELINE, "commit": commit,
                 "item_id": unit_id, "stage": stage, "model": FREE_ROUTER, "prompt_version": version,
                 "call_family": family, "analysis_key": digest([family, epoch]), "attempt_epoch": epoch,
                 "input_hash": digest(request), "day": now.date().isoformat(), "valid": False}
        event["id"] = "stage:" + digest([family, epoch, len(calls), now.isoformat()])
        if isinstance(payload.get("evidence_pack"), dict):
            event["evidence_view_hash"] = digest(payload["evidence_pack"])
        used += 1
        if hasattr(client, "record_intent"):
            client.record_intent(event)
        if persist:
            persist({**event, "outcome": "started", "errors": ["请求已记入预算，响应尚未保存"]})
        try:
            raw = client.call(FREE_ROUTER, prompt, request)
            public_json(raw)
            event["raw_output"] = raw
            try:
                value = json.loads(raw)
            except ValueError:
                value = None
            errors = validate(value)
            event.update(output=value, errors=errors, valid=not errors, outcome="invalid" if errors else "valid")
        except Deferred:
            event.update(outcome="deferred", errors=["服务、输出长度或连接暂未完成"])
        except UnsafePublicText:
            event.pop("raw_output", None)
            event.pop("output", None)
            event.update(outcome="invalid", errors=["输出未通过公开文本扫描，原文不保存"])
        except WatchError:
            event.pop("raw_output", None)
            event.pop("output", None)
            event.update(outcome="invalid", errors=["请求或本地校验失败，未确认敏感内容命中"])
        model = getattr(client, "last_response_model", None)
        event["resolved_model"] = model if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_./:+-]{1,200}", model) else None
        usage = getattr(client, "last_response_usage", {})
        event["usage"] = {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"} and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
        public_json(event)
        if persist:
            persist(event)
        events.append(event); calls.append(event)
        if event["valid"]:
            return {"state": "complete", "output": event["output"], "call_id": event["id"]}, events
        if event["outcome"] == "deferred":
            failed += 1
            return {"state": "waiting_manual" if failed >= 3 else "pending", "reason": "service_retry_limit" if failed >= 3 else "service"}, events
        invalid += 1
    return {"state": "waiting_manual", "reason": "invalid_output"}, events
