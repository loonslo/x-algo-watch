"""独立机制判断协议：精确片段、前后行为和允许表达范围。"""

import re
import json
from typing import Any

from .evidence import digest

STAGES = ("recall", "filtering", "ranking", "display", "account", "engineering", "unknown")
VERDICTS = ("source_fact", "mechanism_hypothesis", "insufficient_evidence", "contradicted")


def mechanism_schema() -> dict[str, Any]:
    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    text = {"type": "string", "maxLength": 1200}
    citation = obj({"evidence_ref": {"type": "string", "pattern": "^E[0-9]+$"},
                    "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1},
                    "quote": {"type": "string", "minLength": 1, "maxLength": 2000}})
    citations = {"type": "array", "maxItems": 8, "items": citation}
    behavior = obj({"known": {"type": "boolean"}, "behavior": text, "citations": citations})
    grounded = obj({"text": text, "citations": citations})
    claim = obj({"key": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"}, "stage": {"enum": list(STAGES)},
                 "verdict": {"enum": list(VERDICTS)}, "execution_scope": {"enum": ["production_path", "test", "mock", "unknown"]},
                 "before": behavior, "after": behavior, "entrypoint": grounded, "activation": grounded,
                 "intermediate_effect": text, "user_effect_gap": text,
                 "support": citations, "counter_evidence": citations,
                 "unknowns": {"type": "array", "maxItems": 10, "items": text},
                 "alternative": text, "verify": text, "allowed_reader_claim": text})
    return obj({"task_id": {"type": "string"}, "claims": {"type": "array", "maxItems": 12, "items": claim},
                "segments": {"type": "array", "items": obj({"segment_id": {"type": "string"},
                    "disposition": {"enum": ["researched", "insufficient_evidence", "excluded"]}, "reason": {"type": "string", "minLength": 1, "maxLength": 1200}})}})


def segment_ids(task: dict[str, Any]) -> set[str]:
    return {segment["task_id"] for segment in task.get("segments", [])} or {task["task_id"]}


def validate_shape(value: Any, schema: dict[str, Any], path: str = "result") -> list[str]:
    """校验本协议使用的有限 JSON Schema 子集，无新增运行依赖。"""
    errors = []
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return [path + " 枚举无效"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            return [path + " 字段不完整或含额外字段"]
        for key, child in schema["properties"].items():
            errors.extend(validate_shape(value[key], child, path + "." + key))
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            return [path + " 列表长度无效"]
        for index, item in enumerate(value):
            errors.extend(validate_shape(item, schema["items"], f"{path}.{index}"))
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10000):
            return [path + " 文本长度无效"]
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            errors.append(path + " 标识无效")
    elif kind == "integer" and (type(value) is not int or value < schema.get("minimum", 0)):
        errors.append(path + " 整数无效")
    elif kind == "boolean" and type(value) is not bool:
        errors.append(path + " 布尔值无效")
    return errors


def validate_mechanisms(value: Any, task: dict[str, Any]) -> list[str]:
    errors = validate_shape(value, mechanism_schema())
    if errors:
        return errors
    if value["task_id"] != task["task_id"]:
        errors.append("task_id 与当前证据任务不匹配")
    actual = [s["segment_id"] for s in value["segments"]]
    if set(actual) != segment_ids(task) or len(actual) != len(set(actual)):
        expected = segment_ids(task)
        errors.append("每个证据区段必须且只能记录一次研究结果；" + json.dumps({
            "missing": sorted(expected - set(actual)), "unexpected": sorted(set(actual) - expected),
            "duplicates": sorted({s for s in actual if actual.count(s) > 1})}, ensure_ascii=False))
    keys = [c["key"] for c in value["claims"]]
    if len(keys) != len(set(keys)):
        errors.append("机制结论 key 重复")

    def check_citations(citations: list[dict[str, Any]], where: str, revision: str | None = None) -> None:
        for cite in citations:
            entry = task["evidence"].get(cite["evidence_ref"])
            if not entry:
                errors.append(where + " 引用不在当前证据任务")
                continue
            if not cite["quote"].strip():
                errors.append(where + " 引文不能仅为空白行")
            start, end = cite["start_line"], cite["end_line"]
            first = entry["start_line"]
            lines = entry["text"].splitlines(keepends=True)
            if start < first or end < start or end >= first + len(lines):
                errors.append(where + " 引用行号超出实际片段")
                continue
            if cite["quote"].strip() != "".join(lines[start - first:end - first + 1]).strip():
                exact = "".join(lines[start - first:end - first + 1]).strip()
                detail = {"evidence_ref": cite["evidence_ref"], "start_line": start, "end_line": end}
                if len(exact) <= 2000:
                    detail["exact_quote_for_selected_lines"] = exact
                else:
                    detail["instruction"] = "选取不超过 2000 字符的更短连续完整行，不能删行或拼接"
                errors.append(where + " 引文与具体源码行不一致；此处仅纠正选定行的转录，不证明断言成立：" + json.dumps(detail, ensure_ascii=False))
            if revision and entry["commit"] != revision:
                errors.append(where + " 引用了错误的前后版本")

    for index, claim in enumerate(value["claims"]):
        where = f"claims.{index}"
        for name, revision in (("before", task["base"]), ("after", task["commit"])):
            behavior = claim[name]
            check_citations(behavior["citations"], where + "." + name, revision)
            if behavior["known"] and (not behavior["citations"] or not behavior["behavior"].strip()):
                errors.append(where + "." + name + " 已知行为必须有该版本的具体片段")
            if not behavior["known"] and behavior["citations"]:
                errors.append(where + "." + name + " 未知与反证不能混为一谈，反证应进入 counter_evidence")
        for field in ("entrypoint", "activation"):
            check_citations(claim[field]["citations"], where + "." + field)
        check_citations(claim["support"], where + ".support")
        check_citations(claim["counter_evidence"], where + ".counter_evidence")
        if claim["verdict"] in {"source_fact", "mechanism_hypothesis"} and not claim["support"]:
            errors.append(where + " 成立的判断缺少支持片段")
        if claim["verdict"] == "contradicted" and not claim["counter_evidence"]:
            errors.append(where + " 被证据否定的判断必须列出反证")
        if claim["verdict"] in {"insufficient_evidence", "contradicted"} and claim["allowed_reader_claim"].strip():
            errors.append(where + " 缺证据或被否定的判断不得进入用户稿，即使附加如果或可能")
        if claim["verdict"] == "insufficient_evidence" and not claim["unknowns"]:
            errors.append(where + " 证据不足必须具体列出未知项")
        if claim["allowed_reader_claim"].strip():
            if task.get("change_scope") == "supporting_context":
                errors.append(where + " 共享架构或纯引用窗口不能冒充本次用户变化")
            if claim["execution_scope"] != "production_path":
                errors.append(where + " 测试、mock 或用途未知不能支撑用户影响")
            if not claim["entrypoint"]["citations"] or not claim["activation"]["citations"]:
                errors.append(where + " 用户影响必须给出调用入口及启用条件的片段")
            support = [task["evidence"].get(c["evidence_ref"], {}) for c in claim["support"]]
            if support and all(e.get("purpose") in {"test", "mock_or_fixture", "example", "documentation"} for e in support):
                errors.append(where + " 用户影响不能仅依赖测试、mock、示例或说明")
            if not claim["after"]["known"]:
                errors.append(where + " 新行为未知时不能推导用户影响")
        if claim["verdict"] == "mechanism_hypothesis" and any(not claim[k].strip() for k in ("intermediate_effect", "user_effect_gap", "alternative", "verify")):
            errors.append(where + " 机制推测须说明中间结果、用户效果缺口、替代解释及验证办法")
    return errors


def bind_claims(value: dict[str, Any], task: dict[str, Any], plan_id: str) -> list[dict[str, Any]]:
    """身份绑定版本、文件和机制键；补证或改写结论不更换同一结论身份。"""
    errors = validate_mechanisms(value, task)
    if errors:
        raise ValueError("；".join(errors))
    return [{**claim, "claim_id": "C-" + digest({"commit": task["commit"], "path": task["path"], "namespace": task.get("claim_namespace", ""), "key": claim["key"]})[:24],
             "task_id": task["task_id"], "plan_id": plan_id, "change_scope": task.get("change_scope", "changed_file"),
             "source_locations": {ref: {k: v for k, v in entry.items() if k != "text"} for ref, entry in task["evidence"].items()
                                  if any(c["evidence_ref"] == ref for c in [*claim["support"], *claim["counter_evidence"], *claim["before"]["citations"], *claim["after"]["citations"], *claim["entrypoint"]["citations"], *claim["activation"]["citations"]])}}
            for claim in value["claims"]]
