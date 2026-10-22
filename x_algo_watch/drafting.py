"""免费模型路由、结构校验、分工复核和可恢复的调用审计。"""

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
from .model_policy import FREE_ROUTER, MAX_DAILY_CALLS, PROJECT_TRANSPORT, REVIEW_TRANSPORT, aggregate_reviews, free_model, review_mode as model_review_mode, review_stages
from .storage import RecordStore, atomic_write, public_text
from .project_evidence import context_packs
from .research import RESEARCH_FIELDS, analysis_projection, render_research, research_event, research_schema, review_schema, validate_research

ANALYSIS_FIELDS = {"item_id", "title", "summary", "default_state", "facts", "inferences", "affected_fact_ids", "manual_change", "action", "observe", "questions"}
NUMBER = re.compile(r"(?<![\w])[-−]?\d+(?:\.\d+)?(?![\w])")
CODE_NAME = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}[A-Za-z0-9]*\b|\b[A-Z][A-Z0-9_]{2,}\b|\b[a-z]\w*_[a-z_]\w*\b")
PROJECT_CODE_NAME = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Z][a-z0-9]+){2,}[A-Za-z0-9]*|(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])[a-z][A-Za-z0-9_]*_[a-z_][A-Za-z0-9_]*(?![A-Za-z0-9_])")
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


def validate_analysis(value: Any, pack: dict[str, Any], *, project_research: bool = False) -> list[str]:
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
    names = (PROJECT_CODE_NAME if project_research else CODE_NAME).findall(narrative)
    paths = PATH_TOKEN.findall(narrative)
    for token in set(names + paths):
        known_acronym = project_research and token not in paths and token.isupper() and token.casefold() in evidence.casefold()
        if token not in evidence and not known_acronym and not re.fullmatch(r"E\d+", token):
            errors.append("名字或路径不在证据包：" + token)
    for phrase in ("所有人", "一定会", "平台确认", "已生效", "已上线", "全量启用"):
        for match in re.finditer(phrase, narrative):
            negated = project_research and re.search(r"(?:不|未|无法|未知|不能|尚无)[^。；\n]{0,18}$", narrative[:match.start()])
            if not negated:
                errors.append("包含无依据的确定性或部署表述")
    if value["default_state"] == "disabled":
        after = [f.get("after") for f in pack["evidence"].values()]
        if not any(item is False or item == 0 or item == "" or item == [] for item in after):
            errors.append("证据没有支持默认关闭或空值")
    public_text(json.dumps(value, ensure_ascii=False))
    return errors


def validate_review(value: Any, pack: dict[str, Any], fields: set[str] = ANALYSIS_FIELDS) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"item_id", "agree", "issues"} or value["item_id"] != pack["item_id"] or type(value["agree"]) is not bool or not isinstance(value["issues"], list):
        return ["复核结构无效"]
    errors = []
    if value["agree"] == bool(value["issues"]):
        errors.append("复核结论和分歧列表矛盾")
    for issue in value["issues"]:
        if not isinstance(issue, dict) or set(issue) != {"field", "reason", "evidence_refs"} or issue.get("field") not in fields or not isinstance(issue.get("reason"), str):
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
        self.last_response_model: str | None = None
        self.last_response_usage: dict[str, int] = {}

    def call(self, model: str, system: str, payload: dict[str, Any]) -> str:
        self.last_response_model = None
        self.last_response_usage = {}
        if not free_model(model):
            raise WatchError("只允许 :free 型号或 openrouter/free 免费自动路由")
        delay = 3.1 - (time.monotonic() - self._last_call)
        if delay > 0:
            time.sleep(delay)
        self._last_call = time.monotonic()
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "response_format": {"type": "json_object"}, "max_tokens": 6000, "provider": {"allow_fallbacks": False}}
        if payload.get("evidence_pack", {}).get("scope") == "project_version":
            schema = review_schema() if "analysis" in payload else research_schema()
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "version_review" if "analysis" in payload else "version_research", "strict": True, "schema": schema}}
            body["provider"] = {"require_parameters": True, "allow_fallbacks": model == FREE_ROUTER}
            if model == FREE_ROUTER and "analysis" in payload:
                body["max_tokens"] = 12000
                body["reasoning"] = {"max_tokens": 3000}
        if model == "minimax/minimax-m3.1-flash:free":
            # 此型号强制推理、默认 max；限制为其支持的 low，给 JSON 留出输出空间。
            body["reasoning"] = {"effort": "low"}
        request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}, method="POST")
        try:
            timeout = 90 if payload.get("evidence_pack", {}).get("scope") == "project_version" else 30
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            actual_model = result.get("model")
            if isinstance(actual_model, str) and re.fullmatch(r"[A-Za-z0-9_./:+-]{1,200}", actual_model):
                self.last_response_model = actual_model
            usage = result.get("usage")
            if isinstance(usage, dict):
                counts = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
                details = usage.get("completion_tokens_details")
                if isinstance(details, dict):
                    counts["reasoning_tokens"] = details.get("reasoning_tokens")
                self.last_response_usage = {k: v for k, v in counts.items() if type(v) is int and 0 <= v <= 1_000_000_000}
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                if choice.get("finish_reason") == "length":
                    raise Deferred("模型输出预算用尽（finish_reason=length），未返回完整 JSON；下一轮再补")
                raise Deferred("模型未返回可用 JSON 内容；下一轮再补")
            if len(content) > 25_000:
                raise Deferred("模型返回超长内容；下一轮再补")
            return content
        except urllib.error.HTTPError as exc:
            raise http_failure(exc) from None
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise Deferred("免费模型连接或响应异常；下一轮补起草") from exc


def analyze_items(packs: list[dict[str, Any]], prior: list[dict[str, Any]], client: Any, models: dict[str, str], now: datetime, prompt_root: Path, daily_limit: int = MAX_DAILY_CALLS, *, allow_same_model: bool = False, project_research: bool = False, repair_feedback: dict[str, Any] | None = None, extra_review_rounds: int = 0, call_deadline: float | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(daily_limit) is not int or not 1 <= daily_limit <= MAX_DAILY_CALLS:
        raise WatchError(f"免费模型本地每日调用上限须为 1 到 {MAX_DAILY_CALLS}")
    if type(allow_same_model) is not bool:
        raise WatchError("allow_same_model 必须为布尔值")
    same_model = models.get("analysis") == models.get("review")
    if (same_model and not allow_same_model and models.get("analysis") != FREE_ROUTER) or any(not free_model(models.get(stage)) for stage in ("analysis", "review")):
        raise WatchError("只允许免费型号或免费自动路由；固定同模型复核须显式启用 allow_same_model")
    review_mode = model_review_mode(models)
    checks = review_stages(extra_review_rounds)
    if extra_review_rounds and not project_research:
        raise WatchError("额外复核仅用于整版本研究")
    outputs: dict[str, Any] = {}; events: list[dict[str, Any]] = []
    used = sum(e["type"] == "model_call" and e.get("day") == now.date().isoformat() for e in prior)
    deferred_all = False
    for pack in packs:
        item_id = pack["item_id"]
        stages: dict[str, Any] = {}; state = "pending"; resolved_models = {}
        for stage in ("analysis", *checks):
            if stage != "analysis" and "analysis" not in stages:
                break
            model = models["analysis" if stage == "analysis" else "review"]
            prompt_name = ("version-" if project_research else "") + stage + "-v1"
            prompt = (prompt_root / (prompt_name + ".md")).read_text(encoding="utf-8")
            if project_research:
                prompt += "\n起草输出与复核对象共用以下完整协议。起草只输出符合协议的 JSON；复核仍只输出 agree/issues，须按此协议检查 analysis。\n" + json.dumps(research_schema(), ensure_ascii=False)
            feedback = (repair_feedback or {}).get(item_id) if stage == "analysis" else None
            if feedback:
                prompt_name = "version-analysis-repair-v1"
                prompt += "\n这是唯一一轮依据复核意见的修订。previous_analysis 是上一稿，review_feedback 是对它的质疑。逐条用原始证据核对，修正成立的问题，降低无依据的置信度，删除不能支持的推测；若质疑本身不成立，在对应依据里用证据简要澄清。保留原 JSON schema，不输出修订说明或额外字段。\n"
            transport = PROJECT_TRANSPORT if stage == "analysis" else REVIEW_TRANSPORT
            version = prompt_name + ":" + digest({"prompt": prompt, "transport": transport} if project_research else prompt)[:16]
            payload = {"evidence_pack": pack, **({"analysis": stages["analysis"]} if stage != "analysis" else {}), **({"previous_analysis": feedback["analysis"], "review_feedback": feedback["review"]} if feedback else {})}
            if repair_feedback and stage != "analysis":
                # 修订后所有角色重新核对；即使修订稿与旧稿相同，也不能沿用旧裁决。
                payload["revision_context"] = digest(repair_feedback[item_id])
            key = digest({"item": item_id, "stage": stage, "model": model, "version": version, "input": payload})
            calls = [e for e in prior + events if e["type"] == "model_call" and e.get("analysis_key") == key]
            passed = next((e for e in reversed(calls) if e.get("valid")), None)
            if passed:
                stages[stage] = passed["output"]
                resolved_models[stage] = passed.get("resolved_model")
                if project_research:
                    print(pack["commit"][:7] + " " + stage + " reused", flush=True)
                continue
            invalid = [e for e in calls if e.get("outcome") == "invalid"]
            if len(invalid) >= 2:
                state = "manual"; break
            while len(invalid) < 2:
                if used >= daily_limit or deferred_all or (call_deadline is not None and time.monotonic() >= call_deadline):
                    state = "pending"; break
                errors = invalid[-1].get("errors", []) if invalid else []
                request_payload = {**payload, **({"previous_validation_errors": errors} if errors else {})}
                event = {"type": "model_call", "id": key + ":" + str(len(calls)) + ":" + now.isoformat(), "analysis_key": key, "item_id": item_id, "commit": pack["commit"], "stage": stage, "model": model, "prompt_version": version, "input_hash": digest(request_payload), "day": now.date().isoformat(), "valid": False}
                used += 1
                try:
                    raw = client.call(model, prompt, request_payload)
                    if review_mode == "free_router":
                        event["resolved_model"] = getattr(client, "last_response_model", None)
                    public_text(raw)
                    event["raw_output"] = raw
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = None
                    if project_research and stage == "analysis":
                        errors = validate_research(value, pack, require_reader=True)
                        if not errors:
                            errors += validate_analysis(analysis_projection(value), pack, project_research=True)
                    else:
                        errors = validate_analysis(value, pack) if stage == "analysis" else validate_review(value, pack, RESEARCH_FIELDS if project_research else ANALYSIS_FIELDS)
                    event.update(output=value, errors=errors, valid=not errors, outcome="valid" if not errors else "invalid")
                except Deferred as exc:
                    event.update(outcome="deferred", errors=[str(exc)])
                    deferred_all = True
                except WatchError:
                    # 疑似敏感输出不留原文；发布扫描依然会再次检查全部产物。
                    event.pop("raw_output", None)
                    event.update(outcome="invalid", errors=["模型输出含疑似敏感内容，已拦截"])
                if review_mode == "free_router":
                    event["resolved_model"] = getattr(client, "last_response_model", None)
                    usage = getattr(client, "last_response_usage", {})
                    if usage:
                        event["usage"] = usage
                events.append(event); calls.append(event)
                if project_research:
                    print(pack["commit"][:7] + " " + stage + " " + event["outcome"], flush=True)
                if event["valid"]:
                    stages[stage] = event["output"]
                    resolved_models[stage] = event.get("resolved_model")
                    break
                if event["outcome"] == "deferred":
                    break
                invalid.append(event)
            if stage not in stages:
                if len(invalid) >= 2:
                    state = "manual"
                break
        complete = all(stage in stages for stage in checks)
        if complete:
            reviews = {stage: stages[stage] for stage in checks}
            stages = {"analysis": stages["analysis"], "review": aggregate_reviews(item_id, reviews)}
            if extra_review_rounds:
                stages.update(reviews=reviews, extra_review_rounds=extra_review_rounds)
            state = "pending_confirmation" if stages["review"]["agree"] else "disputed"
        else:
            # 不能把第一关通过误标成三关通过。
            stages = {"analysis": stages["analysis"]} if "analysis" in stages else {}
        if review_mode == "free_router":
            stages["resolved_models"] = resolved_models
        outputs[item_id] = {"state": state, **stages}
        status_event = {"type": "analysis_state", "id": "state:" + digest({"item": item_id, "state": state, "stages": stages, "review_mode": review_mode}), "item_id": item_id, "commit": pack["commit"], "state": state, "review_mode": review_mode}
        events.append(status_event)
    return outputs, events


def assemble(record: dict[str, Any], packs: list[dict[str, Any]], outputs: dict[str, Any], *, same_model_review: bool = False, free_router: bool = False) -> dict[str, Any] | None:
    # 额度耗尽或网络不可用时仅发证据，不把尚未经过复核调用的分析发成草稿。
    if any(outputs.get(pack["item_id"], {}).get("state", "pending") == "pending" for pack in packs):
        return None
    completed = [pack for pack in packs if "review" in outputs.get(pack["item_id"], {})]
    if not completed:
        return None
    ready = all(outputs.get(pack["item_id"], {}).get("state") == "pending_confirmation" for pack in packs)
    text = "**未经确认的草稿。公开代码默认值不等于线上值；部署未知。**\n\n"
    if free_router:
        text += "**复核方式：免费自动路由；实际型号可能相同，不能保证异模型独立复核。**\n\n"
    elif same_model_review:
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
    if settings.get("project_research") is True:
        selected = manifest.get("research", [])
        packs = [p for p in context_packs(bundle / "data") if p["commit"] in selected]
        prior = RecordStore(bundle / "data").events()
        # 多关核对最多主动运行 20 分钟，再留出一次请求和落盘时间；避免 35 分钟任务被强杀而丢失调用审计。
        call_deadline = time.monotonic() + 20 * 60
        try:
            client = client or OpenRouter(os.environ.get("OPENROUTER_API_KEY", ""))
            outputs, events = analyze_items(packs, prior, client, settings["models"], datetime.now(timezone.utc), Path("prompts"), settings.get("daily_calls", MAX_DAILY_CALLS), allow_same_model=settings.get("allow_same_model", False), project_research=True, extra_review_rounds=settings.get("extra_review_rounds", 0), call_deadline=call_deadline)
            disputed = [p for p in packs if outputs.get(p["item_id"], {}).get("state") == "disputed"]
            if disputed:
                feedback = {p["item_id"]: outputs[p["item_id"]] for p in disputed}
                repaired, repair_events = analyze_items(disputed, prior + events, client, settings["models"], datetime.now(timezone.utc), Path("prompts"), settings.get("daily_calls", MAX_DAILY_CALLS), allow_same_model=settings.get("allow_same_model", False), project_research=True, repair_feedback=feedback, extra_review_rounds=settings.get("extra_review_rounds", 0), call_deadline=call_deadline)
                events.extend(repair_events)
                for pack in disputed:
                    result = repaired[pack["item_id"]]
                    outputs[pack["item_id"]] = {**result, "repair_attempted": True}
        except Deferred:
            outputs, events = {}, []
        drafts = {}
        for pack in packs:
            result = outputs.get(pack["item_id"])
            if not result:
                continue
            event = research_event(pack, result, settings["models"])
            events.append(event)
            if "review" in result:
                repository = os.environ.get("GITHUB_REPOSITORY", "")
                context_base = f"https://github.com/{repository}/blob/main/data/project-context" if repository else "data/project-context"
                body = "<!-- xaw-research:" + event["id"].removeprefix("research:") + " -->\n\n" + render_research(event, context_base) + "- 确认：待仓库所有者编辑核对并打标签。\n"
                drafts[pack["commit"]] = {"body": body, "ready": result["state"] == "pending_confirmation", "version": digest({"event": event, "body": body})[:20]}
        payload = {"schema_version": 1, "source_manifest_hash": digest(manifest), "drafts": drafts, "events": events}
        public_text(json.dumps(payload, ensure_ascii=False))
        atomic_write(output, json.dumps(payload, ensure_ascii=False, indent=2))
        return
    packs = [pack for record in records for pack in items_for_record(record)]
    prior = RecordStore(bundle / "data").events()
    try:
        client = client or OpenRouter(os.environ.get("OPENROUTER_API_KEY", ""))
        outputs, events = analyze_items(packs, prior, client, settings["models"], datetime.now(timezone.utc), Path("prompts"), settings.get("daily_calls", MAX_DAILY_CALLS), allow_same_model=settings.get("allow_same_model", False))
    except Deferred:
        outputs, events = {}, []
    drafts = {}
    for record in records:
        draft = assemble(record, items_for_record(record), outputs, same_model_review=settings["models"]["analysis"] == settings["models"]["review"], free_router=model_review_mode(settings["models"]) == "free_router")
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
