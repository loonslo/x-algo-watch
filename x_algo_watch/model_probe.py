"""固定型号的真实连通性诊断；直接请求 API，不生成研究或裁决。"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

from .call_budget import BudgetClient, load_receipt
from .evidence import digest
from .model_policy import GEMMA_MODEL
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


def run_probe(receipt: Path, ledger: Path, output: Path) -> None:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise WatchError("诊断环境未配置模型密钥")
    budget = BudgetClient(object(), load_receipt(receipt), ledger, RecordStore(Path("data")).events())
    result: dict[str, Any] = {"model": GEMMA_MODEL, "run_id": os.environ.get("GITHUB_RUN_ID"),
        "authentication": request_api(key), "requests": []}
    def save():
        public_json(result)
        atomic_write(output, json.dumps(result, ensure_ascii=False, indent=2))
    save()
    if result["authentication"].get("authenticated") is not True:
        return
    bodies = [
        ("minimal_text", {"model": GEMMA_MODEL, "messages": [{"role": "user", "content": "Reply only OK."}], "max_tokens": 128}),
        ("project_json", {"model": GEMMA_MODEL, "messages": [{"role": "user", "content": 'Reply only with this JSON object: {"ok":true}'}],
            "response_format": {"type": "json_object"}, "provider": {"require_parameters": True, "allow_fallbacks": False},
            "max_tokens": 12000, "reasoning": {"max_tokens": 3000}})]
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
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-receipt", type=Path, required=True)
    parser.add_argument("--call-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run_probe(args.budget_receipt, args.call_ledger, args.output)
        return 0
    except WatchError:
        print("直接诊断暂停，检查预算或环境；未输出认证信息")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
