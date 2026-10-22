"""固定模型、逐项结构校验、单独复核调用、可恢复的调用审计。"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .automation import load_bundle
from .evidence import digest, items_for_record
from .models import WatchError
from .storage import RecordStore, atomic_write, public_text

ANALYSIS_FIELDS = {"item_id", "title", "summary", "default_state", "facts", "inferences", "affected_fact_ids", "manual_change", "action", "observe", "questions"}
NUMBER = re.compile(r"(?<![\w])[-−]?\d+(?:\.\d+)?(?![\w])")
CODE_NAME = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}[A-Za-z0-9]*\b|\b[A-Z][A-Z0-9_]{2,}\b|\b[a-z]\w*_[a-z_]\w*\b")
PATH_TOKEN = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")


class Deferred(WatchError):
    pass


def http_failure(exc: urllib.error.HTTPError) -> Deferred:
    """只输出固定分类和数字等待时间，绝不记录服务端原文或认证信息。"""
    reason = "服务或限额原因未细分"
    try:
        payload = json.loads(exc.read(16_384))
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        details = json.dumps(error, ensure_ascii=False).lower()
        if "free-models-per-day" in details or "daily limit" in details:
            reason = "账户免费每日额度已用尽"
        elif "free-models-per-min" in details:
            reason = "账户免费每分钟限流"
        elif "rate-limited upstream" in details or "provider_overloaded" in details:
            reason = "模型提供方暂时拥堵"
        elif "openrouter_key_limit" in details:
            reason = "项目密钥额度限制"
    except (OSError, ValueError):
        pass
    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
    wait = f"；建议等待 {retry} 秒" if re.fullmatch(r"\d{1,6}", retry) else ""
    return Deferred(f"免费模型请求未完成（HTTP {exc.code}；{reason}{wait}）；下一轮补起草")


def narrative_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in narrative_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in narrative_strings(item)]
    return []


def validate_analysis(value: Any, pack: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != ANALYSIS_FIELDS:
        return ["分析字段不完整或含额外字段"]
    if value["item_id"] != pack["item_id"]:
        errors.append("item_id 不匹配")
    for name in ("title", "summary", "observe"):
        if not isinstance(value[name], str) or len(value[name]) > 1600 or (name != "observe" and not value[name].strip()):
            errors.append(name + " 必须为有效短文本")
    if value["default_state"] not in ("changed", "disabled", "configured", "unknown"):
        errors.append("default_state 无效")
    if value["manual_change"] not in ("yes", "no", "unknown") or value["action"] not in ("change", "keep", "observe"):
        errors.append("建议枚举无效")
    for field in ("facts", "inferences"):
        if not isinstance(value[field], list) or (field == "facts" and not value[field]):
            errors.append(field + " 必须为列表，facts 不能为空")
            continue
        for claim in value[field]:
            if not isinstance(claim, dict) or set(claim) != {"text", "evidence_refs"} or not isinstance(claim.get("text"), str):
                errors.append("断言结构无效"); continue
            refs = claim["evidence_refs"]
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in pack["evidence"] for ref in refs):
                errors.append("断言缺少有效证据编号")
    if not isinstance(value["affected_fact_ids"], list) or any(identifier not in pack["related_facts"] for identifier in value["affected_fact_ids"]):
        errors.append("登记项不在证据包")
    if not isinstance(value["questions"], list) or any(not isinstance(q, str) for q in value["questions"]):
        errors.append("questions 必须为文本列表")
    evidence = json.dumps({"evidence": pack["evidence"], "derived": pack.get("derived", {}), "related_facts": pack["related_facts"], "fact_locations": pack.get("fact_locations", {})}, ensure_ascii=False)
    allowed_numbers = {n.replace("−", "-") for n in NUMBER.findall(evidence)}
    narrative = "\n".join(narrative_strings({key: value[key] for key in ("title", "summary", "facts", "inferences", "observe", "questions")}))
    for number in NUMBER.findall(narrative):
        if number.replace("−", "-") not in allowed_numbers:
            errors.append("数字不在证据包：" + number)
    for token in set(CODE_NAME.findall(narrative) + PATH_TOKEN.findall(narrative)):
        if token not in evidence and not re.fullmatch(r"E\d+", token):
            errors.append("名字或路径不在证据包：" + token)
    if any(phrase in narrative for phrase in ("所有人", "一定会", "平台确认", "已生效", "已上线", "全量启用")):
        errors.append("包含无依据的确定性或部署表述")
    if value["default_state"] == "disabled":
        after = [f.get("after") for f in pack["evidence"].values()]
        if not any(item is False or item == 0 or item == "" or item == [] for item in after):
            errors.append("证据没有支持默认关闭或空值")
    public_text(json.dumps(value, ensure_ascii=False))
    return errors


def validate_review(value: Any, pack: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"item_id", "agree", "issues"} or value["item_id"] != pack["item_id"] or type(value["agree"]) is not bool or not isinstance(value["issues"], list):
        return ["复核结构无效"]
    errors = []
    if value["agree"] == bool(value["issues"]):
        errors.append("复核结论和分歧列表矛盾")
    for issue in value["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"field", "reason", "evidence_refs"} or issue.get("field") not in ANALYSIS_FIELDS or not isinstance(issue.get("reason"), str):
            errors.append("分歧结构无效"); continue
        if not isinstance(issue["evidence_refs"], list) or not issue["evidence_refs"] or any(not isinstance(ref, str) or ref not in pack["evidence"] for ref in issue["evidence_refs"]):
            errors.append("分歧缺少证据")
    public_text(json.dumps(value, ensure_ascii=False))
    return errors


class OpenRouter:
    def __init__(self, key: str):
        if not key:
            raise Deferred("模型密钥尚未在专用环境配置；仅保留证据")
        self._key = key
        self._last_call = 0.0

    def call(self, model: str, system: str, payload: dict[str, Any]) -> str:
        if not model.endswith(":free"):
            raise WatchError("只允许显式 :free 模型，不使用付费或随机路由")
        delay = 3.1 - (time.monotonic() - self._last_call)
        if delay > 0:
            time.sleep(delay)
        self._last_call = time.monotonic()
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "response_format": {"type": "json_object"}, "max_tokens": 3000, "provider": {"allow_fallbacks": False}}
        request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or len(content) > 25_000:
                raise Deferred("模型返回空内容或超长内容；下一轮再补")
            return content
        except urllib.error.HTTPError as exc:
            raise http_failure(exc) from None
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise Deferred("免费模型连接或响应异常；下一轮补起草") from exc


def analyze_items(packs: list[dict[str, Any]], prior: list[dict[str, Any]], client: Any, models: dict[str, str], now: datetime, prompt_root: Path, daily_limit: int = 50, *, allow_same_model: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(daily_limit) is not int or not 1 <= daily_limit <= 50:
        raise WatchError("免费模型本地每日调用上限须为 1 到 50")
    if type(allow_same_model) is not bool:
        raise WatchError("allow_same_model 必须为布尔值")
    same_model = models.get("analysis") == models.get("review")
    if (same_model and not allow_same_model) or any(not models.get(stage, "").endswith(":free") for stage in ("analysis", "review")):
        raise WatchError("只允许固定免费模型；同模型复核须显式启用 allow_same_model")
    review_mode = "same_model" if same_model else "different_models"
    outputs: dict[str, Any] = {}; events: list[dict[str, Any]] = []
    used = sum(e["type"] == "model_call" and e.get("day") == now.date().isoformat() for e in prior)
    deferred_all = False
    for pack in packs:
        item_id = pack["item_id"]
        stages: dict[str, Any] = {}; state = "pending"
        for stage in ("analysis", "review"):
            if stage == "review" and "analysis" not in stages:
                break
            prompt = (prompt_root / (stage + "-v1.md")).read_text(encoding="utf-8")
            version = stage + "-v1:" + digest(prompt)[:16]
            payload = {"evidence_pack": pack, **({"analysis": stages["analysis"]} if stage == "review" else {})}
            key = digest({"item": item_id, "stage": stage, "model": models[stage], "version": version, "input": payload})
            calls = [e for e in prior + events if e["type"] == "model_call" and e.get("analysis_key") == key]
            passed = next((e for e in reversed(calls) if e.get("valid")), None)
            if passed:
                stages[stage] = passed["output"]
                continue
            invalid = [e for e in calls if e.get("outcome") == "invalid"]
            if len(invalid) >= 2:
                state = "manual"; break
            while len(invalid) < 2:
                if used >= daily_limit or deferred_all:
                    state = "pending"; break
                errors = invalid[-1].get("errors", []) if invalid else []
                request_payload = {**payload, **({"previous_validation_errors": errors} if errors else {})}
                event = {"type": "model_call", "id": key + ":" + str(len(calls)) + ":" + now.isoformat(), "analysis_key": key, "item_id": item_id, "commit": pack["commit"], "stage": stage, "model": models[stage], "prompt_version": version, "input_hash": digest(request_payload), "day": now.date().isoformat(), "valid": False}
                used += 1
                try:
                    raw = client.call(models[stage], prompt, request_payload)
                    public_text(raw)
                    event["raw_output"] = raw
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    errors = validate_analysis(value, pack) if stage == "analysis" else validate_review(value, pack)
                    event.update(output=value, errors=errors, valid=not errors, outcome="valid" if not errors else "invalid")
                except Deferred as exc:
                    event.update(outcome="deferred", errors=[str(exc)])
                    deferred_all = True
                except WatchError:
                    # 疑似敏感输出不留原文；发布扫描依然会再次检查全部产物。
                    event.pop("raw_output", None)
                    event.update(outcome="invalid", errors=["模型输出含疑似敏感内容，已拦截"])
                events.append(event); calls.append(event)
                if event["valid"]:
                    stages[stage] = event["output"]; break
                if event["outcome"] == "deferred":
                    break
                invalid.append(event)
            if stage not in stages:
                if len(invalid) >= 2:
                    state = "manual"
                break
        if "review" in stages:
            state = "pending_confirmation" if stages["review"]["agree"] else "disputed"
        outputs[item_id] = {"state": state, **stages}
        status_event = {"type": "analysis_state", "id": "state:" + digest({"item": item_id, "state": state, "stages": stages, "review_mode": review_mode}), "item_id": item_id, "commit": pack["commit"], "state": state, "review_mode": review_mode}
        events.append(status_event)
    return outputs, events


def assemble(record: dict[str, Any], packs: list[dict[str, Any]], outputs: dict[str, Any], *, same_model_review: bool = False) -> dict[str, Any] | None:
    # 额度耗尽或网络不可用时仅发证据，不把尚未经过复核调用的分析发成草稿。
    if any(outputs.get(pack["item_id"], {}).get("state", "pending") == "pending" for pack in packs):
        return None
    completed = [pack for pack in packs if "review" in outputs.get(pack["item_id"], {})]
    if not completed:
        return None
    ready = all(outputs.get(pack["item_id"], {}).get("state") == "pending_confirmation" for pack in packs)
    text = "**未经确认的草稿。公开代码默认值不等于线上值；部署未知。**\n\n"
    if same_model_review:
        text += "**复核方式：同一模型的两次调用，不属于不同模型的独立复核；仍须仓库所有者人工确认。**\n\n"
    text += f"### {record['committed_at'][:10]} · {record['commit'][:7]} · 源码变化待人工确认\n\n"
    for pack in packs:
        result = outputs.get(pack["item_id"], {})
        analysis = result.get("analysis") if "review" in result else None
        if not analysis:
            text += "- 【待补】此项仍待起草或需人工，不能把本条当完整速报。\n"
            continue
        text += "#### " + analysis["title"] + "\n\n"
        text += "- 改了什么：" + analysis["summary"] + "\n"
        text += "- 状态：" + {"changed": "默认值已改；部署未知", "disabled": "默认关或空值；部署未知", "configured": "配置非空；部署未知", "unknown": "默认状态待人工核对；部署未知"}[analysis["default_state"]] + "\n"
        for claim in analysis["facts"]:
            text += "- 【A】" + claim["text"] + "（" + "、".join(claim["evidence_refs"]) + "）\n"
        derived = pack.get("derived", {})
        if derived:
            relationships = ["点赞权重 ×" + derived["favorite_multiplier"]]
            if derived.get("not_interested_multiplier") is not None:
                relationships.append("不感兴趣权重 ×" + derived["not_interested_multiplier"])
            relationships.append("两者绝对比值 " + derived["negative_to_favorite_before"] + " → " + derived["negative_to_favorite_after"])
            text += "- 【A】程序计算的数值关系：" + "；".join(relationships) + "。比值为不感兴趣权重绝对值除以点赞权重；输入与精确结果见证据包 numeric_context。\n"
        for claim in analysis["inferences"]:
            text += "- 【C】" + claim["text"] + "（" + "、".join(claim["evidence_refs"]) + "）\n"
        text += "- 现在要不要改做法：" + {"change": "要", "keep": "不用", "observe": "先观察"}[analysis["action"]] + "；" + analysis["observe"] + "\n"
        locations = sorted({location for fact_id in analysis["affected_fact_ids"] for location in pack.get("fact_locations", {}).get(fact_id, [])})
        text += "- 影响手册：" + ("、".join(locations) or "、".join(analysis["affected_fact_ids"]) or "未关联登记项") + "；是否改手册：" + {"yes": "需要", "no": "不用", "unknown": "待人工判断"}[analysis["manual_change"]] + "\n"
        for finding in pack["evidence"].values():
            text += f"- 出处：`{finding['path']}` / `{finding['name']}` @ `{record['commit']}`\n"
        if result.get("state") == "disputed":
            text += "- **有分歧，需人工**：" + "；".join(issue["reason"] for issue in result["review"]["issues"]) + "\n"
        for question in analysis["questions"]:
            text += "- 待核对：" + question + "\n"
        text += "\n"
    text += "- 确认：待仓库所有者编辑核对并打标签。\n"
    public_text(text)
    return {"body": text, "ready": ready, "version": digest({"packs": packs, "outputs": outputs, "body": text})[:20]}


def run(bundle: Path, output: Path, config: Path, client: Any | None = None) -> None:
    manifest, _, records = load_bundle(bundle)
    if manifest["test"]:
        atomic_write(output, json.dumps({"schema_version": 1, "source_manifest_hash": digest(manifest), "drafts": {}, "events": []}))
        return
    settings = yaml.safe_load(config.read_text(encoding="utf-8"))
    packs = [pack for record in records for pack in items_for_record(record)]
    prior = RecordStore(bundle / "data").events()
    try:
        client = client or OpenRouter(os.environ.get("OPENROUTER_API_KEY", ""))
        outputs, events = analyze_items(packs, prior, client, settings["models"], datetime.now(timezone.utc), Path("prompts"), settings.get("daily_calls", 50), allow_same_model=settings.get("allow_same_model", False))
    except Deferred:
        outputs, events = {}, []
    drafts = {}
    for record in records:
        draft = assemble(record, items_for_record(record), outputs, same_model_review=settings["models"]["analysis"] == settings["models"]["review"])
        if draft:
            drafts[record["commit"]] = draft
    payload = {"schema_version": 1, "source_manifest_hash": digest(manifest), "drafts": drafts, "events": events}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    public_text(text)
    atomic_write(output, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/watch.yaml"))
    args = parser.parse_args(argv)
    try:
        run(args.bundle, args.output, args.config)
        return 0
    except WatchError as exc:
        print(str(exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
