"""将审核问题绑定稳定结论、具体字段及缺证据请求。"""

import re
from typing import Any

from .git import safe_path
from .mechanisms import bind_claims, validate_shape
from .models import WatchError
from .revision_audit import issue_identity

CATEGORIES = ("fact", "coverage", "evidence", "inference", "expression", "format")


def review_schema() -> dict[str, Any]:
    def obj(properties):
        return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    text = {"type": "string", "minLength": 1, "maxLength": 1600}
    request = obj({"path": text, "version": {"enum": ["before", "after"]}, "start_line": {"type": "integer", "minimum": 1},
                   "end_line": {"type": "integer", "minimum": 1}, "reason": text})
    issue = obj({"claim_id": {"type": "string", "maxLength": 100}, "field": text,
                 "problem_key": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
                 "category": {"enum": list(CATEGORIES)}, "reason": text,
                 "evidence_refs": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
                 "evidence_requests": {"type": "array", "maxItems": 4, "items": request}})
    return obj({"item_id": {"type": "string"}, "agree": {"type": "boolean"}, "issues": {"type": "array", "maxItems": 30, "items": issue}})


def resolve_field(value: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ValueError
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if key.isdigit():
                value = value[int(key)]
            else:
                value = next(item for item in value if isinstance(item, dict) and key in {item.get("key"), item.get("claim_id"), item.get("segment_id")})
        else:
            value = value[key]
    return value


def validate_review(value: Any, item_id: str, analysis: dict[str, Any], task: dict[str, Any]) -> list[str]:
    errors = validate_shape(value, review_schema())
    if errors:
        return errors
    if value["item_id"] != item_id or value["agree"] != (not value["issues"]):
        errors.append("审核身份或同意/问题列表不一致")
    claim_catalog = {c["claim_id"]: c["key"] for c in bind_claims(analysis, task, "review")}
    claims = set(claim_catalog)
    seen = set()
    for issue in value["issues"]:
        if issue["claim_id"] and issue["claim_id"] not in claims:
            errors.append("审核结论编号不属于当前机制任务")
        try:
            resolve_field(analysis, issue["field"])
        except (ValueError, KeyError, TypeError, IndexError, StopIteration):
            message = "审核字段未定位到具体存在的 JSON 路径；field 相对于 analysis 对象本身，以 /claims/ 或 /segments/ 开始，不加 /analysis 前缀"
            if issue["field"].startswith("/analysis/"):
                candidate = issue["field"][len("/analysis"):]
                try:
                    resolve_field(analysis, candidate)
                    message += "；此字段可解析的候选路径：" + candidate
                except (ValueError, KeyError, TypeError, IndexError, StopIteration):
                    pass
            errors.append(message)
        if issue["claim_id"] and "/claims/" not in issue["field"]:
            errors.append("结论问题必须定位到 claims 字段")
        elif issue["claim_id"] in claim_catalog:
            try:
                target = resolve_field(analysis, "/".join(issue["field"].split("/")[:3]))
                if not isinstance(target, dict) or target.get("key") != claim_catalog[issue["claim_id"]]:
                    errors.append("问题字段与声明的结论编号不一致")
            except (ValueError, KeyError, TypeError, IndexError, StopIteration):
                errors.append("问题字段没有定位到声明的结论")
        if any(ref not in task["evidence"] for ref in issue["evidence_refs"]):
            errors.append("审核引用不在当前证据任务")
        if issue["category"] == "evidence" and not issue["evidence_requests"]:
            errors.append("缺证据问题须给出有界补证请求；若材料已在 evidence 中但稿件遗漏，使用 coverage 并引用已有 E 编号，不请求补证")
        if issue["category"] == "coverage" and (not issue["evidence_refs"] or issue["evidence_requests"]):
            errors.append("覆盖遗漏须引用已提供证据，不能同时请求缺失材料；缺失材料应另列 evidence 问题")
        for request in issue["evidence_requests"]:
            try:
                safe_path(request["path"])
            except WatchError:
                errors.append("补证路径不安全")
            if request["end_line"] < request["start_line"] or request["end_line"] - request["start_line"] > 2000:
                errors.append("补证行号范围无效")
        identifier = issue_identity(issue)
        if identifier in seen:
            errors.append("同一审核重复提出相同稳定问题")
        seen.add(identifier)
    return errors


def combine_reviews(reviews: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """相同身份保留各角色原意见；任何一方异议都不能被抵消。"""
    result = {}
    for stage, review in reviews.items():
        for issue in review["issues"]:
            identifier = issue_identity(issue)
            row = result.setdefault(identifier, {**issue, "issue_id": identifier, "opinions": []})
            row["opinions"].append({"role": stage, "reason": issue["reason"], "evidence_refs": issue["evidence_refs"], "evidence_requests": issue["evidence_requests"]})
    return list(result.values())
