"""配置型号的真实连通性诊断；直接请求 API，不生成研究或裁决。"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

import yaml

from .call_budget import BudgetClient, load_receipt
from .evidence import digest
from .model_policy import FREE_ROUTER, GEMMA_MODEL, pipeline_models
from .models import WatchError
from .storage import RecordStore, atomic_write, public_json


def safe_field(value: Any, key: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.replace(key, "[redacted]") if key else value
    value = value[:3000]
    try:
        public_json(value)
    except WatchError:
        return "[redacted]"
    return value


def request_api(key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    endpoint = "chat/completions" if body is not None else "key"
    req = urllib.request.Request("https://openrouter.ai/api/v1/" + endpoint,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            status, raw, headers = response.status, response.read(64_000), response.headers
    except urllib.error.HTTPError as exc:
        status, raw, headers = exc.code, exc.read(64_000), exc.headers
    except OSError:
        return {"status": None, "error": "connection_error"}
    result: dict[str, Any] = {"status": status, "seconds": round(time.monotonic() - start, 2)}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {**result, "error": "non_json_response"}
    if not isinstance(value, dict):
        return {**result, "error": "non_object_response"}
    if body is None:
        # 不保存 key 标签、hash、余额或其他账户详情。
        result["authenticated"] = status == 200 and isinstance(value.get("data"), dict)
    else:
        error = value.get("error")
        if isinstance(error, dict):
            result["error_message"] = safe_field(error.get("message"), key)
            if type(error.get("code")) is int:
                result["error_code"] = error["code"]
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                result["provider_name"] = safe_field(metadata.get("provider_name"), key)
                upstream = metadata.get("raw")
                if isinstance(upstream, str):
                    try:
                        upstream = json.loads(upstream)
                    except ValueError:
                        result["provider_error_message"] = safe_field(upstream, key)
                if isinstance(upstream, dict) and isinstance(upstream.get("error"), dict):
                    result["provider_error_message"] = safe_field(upstream["error"].get("message"), key)
                    result["provider_error_status"] = safe_field(upstream["error"].get("status"), key)
        result["resolved_model"] = safe_field(value.get("model"), key)
        choices = value.get("choices", [])
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            message = choice.get("message")
            result["content"] = safe_field(message.get("content"), key) if isinstance(message, dict) else None
            result["finish_reason"] = safe_field(choice.get("finish_reason"), key)
        usage = value.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(v) is int and v >= 0}
    retry = headers.get("Retry-After", "") if headers else ""
    if retry.isdigit() and len(retry) <= 6:
        result["retry_after_seconds"] = int(retry)
    public_json(result)
    return result


def review_request(commit: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """仅重建下一缺失阶段；捕获调用参数后退出，不发送请求或写研究。"""
    from .drafting import request_body
    from .mechanism_pipeline import run_pipeline
    from .project_evidence import context_packs
    from .git import SHA
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise WatchError("审核重放须指定已有完整提交")
    prior = RecordStore(Path("data")).events()
    original = next((e for e in reversed(prior) if e.get("commit") == commit and e.get("response_status") == 404
                     and e.get("stage") == "mechanism_facts_review"), None)
    if original is None:
        raise WatchError("没有可核对的事实审核404记录")
    class Captured(Exception):
        def __init__(self, model, prompt, payload):
            self.model, self.prompt, self.payload = model, prompt, payload
    class CaptureClient:
        def call(self, model, prompt, payload):
            raise Captured(model, prompt, payload)
    settings = yaml.safe_load(Path("config/watch.yaml").read_text(encoding="utf-8"))
    pack = next((p for p in context_packs(Path("data")) if p["commit"] == commit), None)
    if pack is None:
        raise WatchError("缺少可信上下文")
    try:
        run_pipeline(pack, Path("data"), prior, CaptureClient(), settings, Path("prompts"))
    except Captured as captured:
        from .stage_calls import PIPELINE
        prompt_version = digest({"prompt": captured.prompt, "schema": captured.payload["stage_schema"], "transport": PIPELINE})
        if (digest(captured.payload) != original["input_hash"] or prompt_version != original["prompt_version"]
                or captured.model != original["model"]):
            raise WatchError("重放请求与原404输入摘要不一致，停止诊断")
        body = request_body(captured.model, captured.prompt, captured.payload)
        public_json(body)
        return body, {"source_call_id": original["id"], "input_hash": original["input_hash"],
                      "body_hash": digest(body), "input_hash_matches": True, "prompt_hash_matches": True}
    raise WatchError("未捕获到原404对应的下一缺失请求")


def run_probe(receipt: Path, ledger: Path, output: Path, *, review_replay: bool = False) -> None:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise WatchError("诊断环境未配置模型密钥")
    settings = yaml.safe_load(Path("config/watch.yaml").read_text(encoding="utf-8"))
    model = pipeline_models(settings.get("models"), allow_same_model=settings.get("allow_same_model", False))["analysis"]
    budget = BudgetClient(object(), load_receipt(receipt), ledger, RecordStore(Path("data")).events())
    result: dict[str, Any] = {"model": model, "run_id": os.environ.get("GITHUB_RUN_ID"),
        "authentication": request_api(key), "requests": []}
    def save():
        public_json(result)
        atomic_write(output, json.dumps(result, ensure_ascii=False, indent=2))
    save()
    if result["authentication"].get("authenticated") is not True:
        return
    bodies = [
        ("minimal_text", {"model": model, "messages": [{"role": "user", "content": "Reply only OK."}], "max_tokens": 128}),
        ("project_json", {"model": model, "messages": [{"role": "user", "content": 'Reply only with this JSON object: {"ok":true}'}],
            "response_format": {"type": "json_object"}, "provider": {"require_parameters": True, "allow_fallbacks": model == FREE_ROUTER},
            "max_tokens": 12000, "reasoning": {"max_tokens": 3000}})]
    if model == FREE_ROUTER:
        bodies[1][1]["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "connectivity_probe", "strict": True, "schema": {"type": "object", "additionalProperties": False,
                "properties": {"ok": {"type": "boolean", "const": True}}, "required": ["ok"]}}}
    if review_replay:
        body, result["replay_source"] = review_request(os.environ.get("RESEARCH_COMMIT", ""))
        if body["model"] != model:
            raise WatchError("重放型号与当前配置不一致")
        without_reasoning = deepcopy(body)
        without_reasoning.pop("reasoning", None)
        smaller_output = deepcopy(body)
        smaller_output["max_tokens"] = 12000
        bodies = [("exact_review_replay", body), ("review_without_reasoning", without_reasoning),
                  ("review_output_12000", smaller_output)]
        save()
    for index, (name, body) in enumerate(bodies):
        if index:
            time.sleep(15)
        now = datetime.now(timezone.utc)
        event = {"type": "model_call", "id": "probe:" + digest([result["run_id"], now.isoformat(), name]), "day": now.date().isoformat()}
        budget.record_intent(event)
        row = {"name": name, "request": body, "call_id": event["id"], "response": {"status": None, "state": "started"}}
        result["requests"].append(row)
        save()
        row["response"] = request_api(key, body)
        save()
        if name == "minimal_text" and row["response"].get("status") != 200:
            result["comparison_skipped"] = "minimal_request_rejected"
            save()
            break
        if review_replay and row["response"].get("status") != 404:
            result["remaining_comparisons_skipped"] = "review_did_not_return_404"
            save()
            break
    printable = deepcopy(result)
    for row in printable["requests"]:
        if review_replay:
            row["request"].pop("messages", None)
    print(json.dumps(printable, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-receipt", type=Path, required=True)
    parser.add_argument("--call-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-replay", action="store_true")
    args = parser.parse_args()
    try:
        run_probe(args.budget_receipt, args.call_ledger, args.output, review_replay=args.review_replay)
        return 0
    except WatchError:
        print("直接诊断暂停，检查预算或环境；未输出认证信息")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
