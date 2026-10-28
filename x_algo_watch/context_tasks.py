"""超大补证的源码分块、分层归并和可重建引文视图。"""

from copy import deepcopy
import json
from typing import Any

from .evidence import digest
from .mechanisms import bind_claims, mechanism_schema, validate_mechanisms
from .models import WatchError
from .research_plan import fragment, source_chunks, task

CONTEXT_REPORT_LIMIT = 6000
CONTEXT_SOURCE_LIMIT = 24_000
CONTEXT_FAN_IN = 2
MODEL_VIEW_LIMIT = 120_000


class ContextViewTooLarge(WatchError):
    """归并仍放不下时保持可恢复不足状态，不冒充服务失败。"""


def assign_id(item: dict[str, Any]) -> str:
    item["task_id"] = "T-" + digest({k: v for k, v in item.items() if k != "task_id"})
    return item["task_id"]


def split_context(parent: dict[str, Any], additions: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """将所有补证移出父任务；初始实现仍完整保留，已有归并依赖也保留。"""
    entries = []
    kept = {}
    moved_refs = set()
    for ref, entry in parent["evidence"].items():
        if entry["kind"] == "requested_context":
            entries.append((entry, {"reason": "先前补证继续纳入归并", "issue_id": "previous-context"}))
            moved_refs.add(ref)
        else:
            kept[ref] = entry
    # 保留原编号及原区段，不把既有 E 编号重新指向另一段源码。
    parent["evidence"] = kept
    parent["segments"] = [s for s in parent.get("segments", []) if not set(s.get("evidence_refs", [])) & moved_refs]
    entries.extend(additions)
    nodes = []
    questions = {}
    for request in [*parent.get("supplement_requests", []), *[r for _, r in additions]]:
        row = {k: request[k] for k in ("issue_id", "reason")}
        questions[digest(row)] = row
    question = {"owner_namespace": parent["claim_namespace"], "owner_path": parent["path"], "requests": list(questions.values())}
    for entry, request in entries:
        for start, text in source_chunks(entry["text"], entry["start_line"], CONTEXT_SOURCE_LIMIT):
            piece = fragment(entry["path"], entry["commit"], text, start, "requested_context", "requested_window")
            # 被清理过的联系地址不能被重新当作未经清理的源摘要。
            piece["origin_source_digest"] = entry["source_digest"]
            piece["purpose"] = entry["purpose"]
            piece["purpose_markers"] = entry["purpose_markers"]
            node = task(parent["path"], parent["base"], parent["commit"], [piece], context_kind="scan",
                        claim_namespace="N-" + digest({"owner": question, "request": request, "piece": piece}),
                        context_question={**question, "request": request}, context_dependencies=[])
            nodes.append(node)
    level = [*parent.get("context_dependencies", []), *[n["task_id"] for n in nodes]]
    if not level:
        raise WatchError("超大补证拆分未产生任何上下文任务")
    while len(level) > 1:
        following = []
        for start in range(0, len(level), CONTEXT_FAN_IN):
            children = level[start:start + CONTEXT_FAN_IN]
            if len(children) == 1:
                following.extend(children)
                continue
            node = task(parent["path"], parent["base"], parent["commit"], [], context_kind="merge",
                        claim_namespace="N-" + digest({"owner": question, "children": children}),
                        context_question=question, context_dependencies=children)
            nodes.append(node)
            following.append(node["task_id"])
        level = following
    parent["context_dependencies"] = level
    return nodes


def validate_graph(plan: dict[str, Any]) -> None:
    seen = {}
    for item in plan["tasks"]:
        dependencies = item.get("context_dependencies", [])
        if (not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies)
                or len(dependencies) != len(set(dependencies)) or not set(dependencies) <= set(seen) or item["task_id"] in seen
                or item.get("context_kind") not in {None, "scan", "merge"}
                or (item.get("context_kind") and item.get("change_scope") != "supporting_context")
                or any(not seen[d].get("context_kind") for d in dependencies)
                or (item.get("context_kind") == "merge" and not 1 <= len(dependencies) <= CONTEXT_FAN_IN)):
            raise WatchError("补证任务依赖重复、缺失、循环或顺序无效")
        seen[item["task_id"]] = item


def task_schema(item: dict[str, Any]) -> dict[str, Any]:
    schema = mechanism_schema()
    if item.get("context_kind"):
        schema["properties"]["claims"]["maxItems"] = 4
    return schema


def validate_task(value: Any, item: dict[str, Any]) -> list[str]:
    errors = validate_mechanisms(value, item)
    if errors:
        return errors
    if item.get("context_kind"):
        if len(value["claims"]) > 4 or len(json.dumps(value, ensure_ascii=False)) > CONTEXT_REPORT_LIMIT:
            errors.append("补证事实报告须聚焦原问题、至多四项且不超过 6000 字符；反证与缺口不能为缩短而删除")
        if any(c["verdict"] == "mechanism_hypothesis" for c in value["claims"]):
            errors.append("补证分块及归并只形成源码事实、反证或不足判断，最终机制推测留给原任务")
    return errors


def citations(claim: dict[str, Any]) -> list[dict[str, Any]]:
    return [*claim["support"], *claim["counter_evidence"],
            *claim["before"]["citations"], *claim["after"]["citations"],
            *claim["entrypoint"]["citations"], *claim["activation"]["citations"]]


def materialize(plan: dict[str, Any], item: dict[str, Any], dependencies: dict[str, Any],
                calls: list[dict[str, Any]], *, depth: int = 0) -> dict[str, Any]:
    """由已批准子任务重建模型实际可见的片段；不把模型摘要伪装成源码。"""
    if depth > 100 or set(dependencies) != set(item.get("context_dependencies", [])):
        raise WatchError("补证归并依赖不完整或超过深度边界")
    if not dependencies:
        return item
    by_task = {t["task_id"]: t for t in plan["tasks"]}
    by_call = {c["id"]: c for c in calls if c.get("type") == "model_call"}
    result = deepcopy(item)
    result["context_catalog"] = []
    result.setdefault("segments", [])
    # 原来没有显式区段的任务仍须处理本身，不能被新增依赖区段遮蔽。
    if not result["segments"]:
        result["segments"].append({"task_id": item["task_id"], "evidence_refs": list(item["evidence"])})
    used = {}
    def expose(cite, source):
        entry = source["evidence"][cite["evidence_ref"]]
        key = (entry["path"], entry["commit"], cite["start_line"], cite["end_line"], cite["quote"])
        if key not in used:
            ref = "E" + str(max([int(r[1:]) for r in result["evidence"]] or [0]) + 1)
            text = "".join(entry["text"].splitlines(keepends=True)[cite["start_line"] - entry["start_line"]:cite["end_line"] - entry["start_line"] + 1])
            value = {**entry, "start_line": cite["start_line"], "end_line": cite["end_line"], "text": text,
                     "source_digest": digest(text), "origin_source_digest": entry["source_digest"],
                     "origin_task_id": source["task_id"], "origin_evidence_ref": cite["evidence_ref"], "scope": "verified_quote"}
            result["evidence"][ref] = value
            used[key] = ref
        return {**cite, "evidence_ref": used[key]}
    for identifier in item["context_dependencies"]:
        state = dependencies[identifier]
        if state.get("state") != "approved":
            raise WatchError("未通过全部复核的补证不能进入归并")
        child = materialize(plan, by_task[identifier], state.get("dependencies", {}), calls, depth=depth + 1)
        call = by_call[state["analysis_call"]]
        if call.get("valid") is not True or call.get("item_id") != identifier:
            raise WatchError("补证判断缺少对应有效调用")
        raw = call["output"]
        output = raw.get("result", raw)
        if validate_task(output, child):
            raise WatchError("补证判断校验失败")
        claims = bind_claims(output, child, plan["plan_id"])
        reports = []
        for claim in claims:
            copy = {k: deepcopy(v) for k, v in claim.items() if k != "source_locations"}
            for field in ("support", "counter_evidence"):
                copy[field] = [expose(c, child) for c in claim[field]]
            for field in ("before", "after", "entrypoint", "activation"):
                copy[field]["citations"] = [expose(c, child) for c in claim[field]["citations"]]
            reports.append(copy)
            result["segments"].append({"task_id": "D-" + claim["claim_id"],
                                       "evidence_refs": sorted({c["evidence_ref"] for c in citations(copy)})})
        result["segments"].append({"task_id": "D-" + identifier, "evidence_refs": []})
        result["context_catalog"].append({"task_id": identifier, "claims": reports, "segments": output["segments"]})
    if len(json.dumps(result, ensure_ascii=False)) > MODEL_VIEW_LIMIT:
        raise ContextViewTooLarge("归并后的必要模型视图超过边界，须进一步聚焦原补证问题；未截断")
    return result


def dependencies_for(item: dict[str, Any], states: dict[str, Any]) -> dict[str, Any]:
    return {identifier: deepcopy(states[identifier]) for identifier in item.get("context_dependencies", [])}


def question_claims(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """补证者看到原判断和完整条件，原引文由原任务审计保留，不假作新材料。"""
    result = []
    for claim in analysis["claims"]:
        row = {k: claim[k] for k in ("key", "stage", "verdict", "execution_scope", "intermediate_effect",
                                     "user_effect_gap", "unknowns", "alternative", "verify", "allowed_reader_claim")}
        for field in ("before", "after"):
            row[field] = {k: claim[field][k] for k in ("known", "behavior")}
        for field in ("entrypoint", "activation"):
            row[field] = {"text": claim[field]["text"]}
        result.append(row)
    return result
