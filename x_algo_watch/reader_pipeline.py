"""从已复核机制结论筛选读者场景；不与源码研究共用一次起草。"""

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable

from .evidence import digest
from .mechanisms import bind_claims, validate_shape
from .mechanism_reviews import combine_reviews, resolve_field, review_schema
from .model_policy import REVIEW_STAGES
from .readers import validate_reader, reader_projection
from .research import research_schema
from .revision_audit import field_changes
from .git import safe_path
from .models import WatchError


def reader_schema() -> dict[str, Any]:
    reader = research_schema()["properties"]["reader"]
    reader["properties"]["impacts"]["maxItems"] = 3
    reader["properties"]["impacts"]["items"]["properties"]["before"]["minLength"] = 0
    # 新链引用稳定 C 编号；原协议仍保留原来的 E 编号含义。
    def refs(node):
        if isinstance(node, dict):
            if node.get("pattern") == "^E[0-9]+$":
                node["pattern"] = "^C-[0-9a-f]{24}$"
            for child in node.values(): refs(child)
        elif isinstance(node, list):
            for child in node: refs(child)
    refs(reader)
    text = {"type": "string", "minLength": 1, "maxLength": 1200}
    binding = {"type": "object", "additionalProperties": False, "required": ["field", "claim_ids"], "properties": {
        "field": text, "claim_ids": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}}}}
    closure = {"type": "object", "additionalProperties": False, "required": ["issue_id", "reason"], "properties": {"issue_id": text, "reason": text}}
    return {"type": "object", "additionalProperties": False, "required": ["reader", "field_claims", "closure_reasons"], "properties": {
        "reader": reader, "field_claims": {"type": "array", "maxItems": 40, "items": binding},
        "closure_reasons": {"type": "array", "maxItems": 90, "items": closure}}}


def selection_schema() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1, "maxLength": 600}
    return {"type": "object", "additionalProperties": False, "required": ["selected_ids", "decisions"], "properties": {
        "selected_ids": {"type": "array", "maxItems": 3, "items": text},
        "decisions": {"type": "array", "maxItems": 30, "items": {"type": "object", "additionalProperties": False,
            "required": ["claim_id", "disposition", "reason"], "properties": {"claim_id": text,
                "disposition": {"enum": ["candidate", "technical_only", "insufficient_for_user"]}, "reason": text}}}}}


def narrative_fields(reader: dict[str, Any]) -> set[str]:
    paths = {"/reader/" + k for k in ("title", "what_changed", "who", "when", "action")}
    for key in ("impacts", "terms"):
        for index, row in enumerate(reader[key]):
            paths.update(f"/reader/{key}/{index}/{name}" for name, value in row.items() if name != "evidence_refs" and isinstance(value, str))
    return paths


def validate_reader_draft(value: Any, claims: dict[str, Any], feedback: dict[str, Any] | None = None) -> list[str]:
    errors = validate_shape(value, reader_schema())
    if errors: return errors
    reader = value["reader"]
    projected = deepcopy(reader)
    for row in projected["impacts"]:
        if not row["before"]: row["before"] = "旧行为尚不明确"
    errors += validate_reader(projected, claims, user_impact=True)
    from .drafting import validate_analysis
    evidence = {identifier: {"text": json.dumps(claim, ensure_ascii=False)} for identifier, claim in claims.items()}
    errors += validate_analysis(reader_projection(projected, "reader"), {"item_id": "reader", "evidence": evidence, "derived": {}, "related_facts": []}, project_research=True)
    if len(reader["impacts"]) > 3:
        errors.append("用户稿最多三个有依据的场景")
    for row in reader["impacts"]:
        referenced = [claims.get(ref) for ref in row["evidence_refs"]]
        if any(not c or not c["allowed_reader_claim"].strip() or c["verdict"] not in {"source_fact", "mechanism_hypothesis"} for c in referenced):
            errors.append("用户场景引用了不允许进入用户影响的结论")
        if row["before"] and any(c and not c["before"]["known"] for c in referenced):
            errors.append("旧行为未知时 before 必须留空，不能强填前后对比")
    fields = [b["field"] for b in value["field_claims"]]
    if set(fields) != narrative_fields(reader) or len(fields) != len(set(fields)):
        errors.append("每个用户表达字段必须且只能绑定一次机制结论")
    for binding in value["field_claims"]:
        if any(identifier not in claims for identifier in binding["claim_ids"]):
            errors.append("用户字段绑定未知或未复核的机制结论")
        parts = binding["field"].split("/")
        if len(parts) >= 5 and parts[2] in {"impacts", "terms"} and parts[3].isdigit() and int(parts[3]) < len(reader[parts[2]]):
            available = reader[parts[2]][int(parts[3])]["evidence_refs"]
        else:
            available = reader["evidence_refs"]
        if not set(binding["claim_ids"]) <= set(available):
            errors.append("字段依赖与对应场景引用不一致")
    body = "\n".join(str(resolve_field(value, path)) for path in narrative_fields(reader) if not path.startswith("/reader/terms/"))
    if any(term["term"] not in body for term in reader["terms"]):
        errors.append("术语表只能解释正文确实使用的词")
    if feedback:
        expected = {issue["issue_id"] for issue in feedback["issues"]}
        if {row["issue_id"] for row in value["closure_reasons"]} != expected or len(value["closure_reasons"]) != len(expected):
            errors.append("读者稿修订须记录每个旧问题的处理理由")
        if all(i["category"] == "expression" for i in feedback["issues"]):
            allowed = [i["field"] for i in feedback["issues"]]
            for change in field_changes(feedback["draft"]["reader"], reader, "/reader"):
                if not any(change["field"] == p or change["field"].startswith(p + "/") for p in allowed):
                    errors.append("表达修订越过指定读者字段")
            if value["field_claims"] != feedback["draft"]["field_claims"]:
                errors.append("表达修订不得改变结论依赖")
    elif value["closure_reasons"]:
        errors.append("初稿不能虚构旧问题关闭记录")
    return errors


def validate_reader_review(value: Any, item_id: str, subject: dict[str, Any], claims: dict[str, Any]) -> list[str]:
    errors = validate_shape(value, review_schema())
    if errors: return errors
    if value["item_id"] != item_id or value["agree"] != (not value["issues"]):
        errors.append("读者审核身份或裁决矛盾")
    for issue in value["issues"]:
        if issue["claim_id"] and issue["claim_id"] not in claims:
            errors.append("读者审核引用未知结论")
        if any(ref not in claims for ref in issue["evidence_refs"]):
            errors.append("读者审核只能引用本轮冻结的结论编号")
        if not issue["field"].startswith(("/reader/", "/field_claims/")):
            errors.append("读者问题必须定位到具体正文或依赖字段")
        try: resolve_field(subject, issue["field"])
        except (KeyError, TypeError, ValueError, IndexError, StopIteration): errors.append("读者问题字段不存在")
        if issue["category"] == "evidence" and not issue["evidence_requests"]:
            errors.append("读者缺证据问题须给出具体 Git 请求")
        if issue["category"] == "coverage" and (not issue["evidence_refs"] or issue["evidence_requests"]):
            errors.append("读者覆盖遗漏须绑定已有冻结结论且不请求补证；缺失材料另列 evidence 问题")
        if issue["evidence_requests"] and not issue["claim_id"]:
            errors.append("读者补证请求须绑定被质疑结论")
        for request in issue["evidence_requests"]:
            try: safe_path(request["path"])
            except WatchError: errors.append("读者补证路径不安全")
            if request["end_line"] < request["start_line"] or request["end_line"] - request["start_line"] > 2000:
                errors.append("读者补证窗口越界")
    return errors


def frozen_claims(plan, task_states, calls):
    from .context_tasks import materialize
    by_id = {e["id"]: e for e in calls if e.get("type") == "model_call"}
    result = {}
    for task in plan["tasks"]:
        state = task_states[task["task_id"]]
        task = materialize(plan, task, state.get("dependencies", {}), calls)
        raw = by_id[state["analysis_call"]]["output"]
        value = raw.get("result", raw)
        for claim in bind_claims(value, task, plan["plan_id"]):
            if claim["claim_id"] in result and claim != result[claim["claim_id"]]:
                raise ValueError("不同任务产生冲突的结论身份")
            result[claim["claim_id"]] = claim
    return result


def claim_view(claim: dict[str, Any]) -> dict[str, Any]:
    """编辑与阅读审核使用冻结判断；源码全文留在机制阶段及审计中。"""
    description = {k: v for k, v in claim.items() if k not in {"support", "counter_evidence"}}
    for name in ("before", "after"):
        description[name] = {k: v for k, v in claim[name].items() if k != "citations"}
    for name in ("entrypoint", "activation"):
        description[name] = {"text": claim[name]["text"]}
    description["counter_evidence"] = claim["counter_evidence"]
    return description


def selection_batches(pool: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches, batch = [], []
    for claim in pool:
        description = claim_view(claim)
        if batch and (len(batch) >= 30 or len(json.dumps(batch + [description], ensure_ascii=False)) > 50_000):
            batches.append(batch); batch = []
        batch.append(description)
    if batch: batches.append(batch)
    return batches


def validate_selection(value: Any, catalog: dict[str, Any]) -> list[str]:
    errors = validate_shape(value, selection_schema())
    if errors: return errors
    ids = [d["claim_id"] for d in value["decisions"]]
    candidates = {d["claim_id"] for d in value["decisions"] if d["disposition"] == "candidate"}
    if set(ids) != set(catalog) or len(ids) != len(set(ids)) or set(value["selected_ids"]) != candidates or len(candidates) != len(value["selected_ids"]):
        errors.append("每条机制结论都须记录筛选去向，不能遗漏或引入未知结论")
    return errors


def verify_selection_receipts(claims: dict[str, Any], ids: list[str], calls: dict[str, Any]) -> list[str]:
    pool = [c for c in claims.values() if c["verdict"] in {"source_fact", "mechanism_hypothesis"} and c.get("change_scope", "changed_file") == "changed_file"]
    cursor = 0
    while len(pool) > 3:
        reduced = []
        for batch in selection_batches(pool):
            call = calls[ids[cursor]]; cursor += 1
            if call.get("valid") is not True or call.get("item_id") != "selection:" + digest(batch) or call.get("stage") != "reader_selection":
                raise ValueError("筛选调用与完整候选批次不符")
            if validate_selection(call["output"], {c["claim_id"]: c for c in batch}):
                raise ValueError("筛选遗漏候选或引入未知结论")
            reduced.extend(claims[c] for c in call["output"]["selected_ids"])
        if len(reduced) >= len(pool):
            raise ValueError("筛选未能收敛")
        pool = reduced
    if cursor != len(ids):
        raise ValueError("存在多余的筛选调用")
    return [c["claim_id"] for c in pool]


def run_reader(plan: dict[str, Any], pack: dict[str, Any], task_states: dict[str, Any], calls: list[dict[str, Any]],
               loop: dict[str, Any], prompt_root: Path, invoke: Callable) -> dict[str, Any] | None:
    claims = frozen_claims(plan, task_states, calls)
    # 筛选只作用于已研究的结论；全部原始结论仍保留在技术研究及审计中。
    pool = [c for c in claims.values() if c["verdict"] in {"source_fact", "mechanism_hypothesis"} and c.get("change_scope", "changed_file") == "changed_file"]
    selections = []
    while len(pool) > 3:
        batches = selection_batches(pool)
        reduced = []
        for batch in batches:
            catalog = {c["claim_id"]: c for c in batch}
            unit = {"task_id": "selection:" + digest(batch)}
            selected = invoke(unit, "reader_selection", {"claims": batch}, selection_schema(), lambda v, catalog=catalog: validate_selection(v, catalog),
                              (prompt_root / "reader-selection-v1.md").read_text(encoding="utf-8"))
            if selected["state"] != "complete": return None
            selections.append(selected["call_id"])
            reduced.extend(claims[c] for c in selected["output"]["selected_ids"])
        if len(reduced) >= len(pool):
            loop.update(phase="waiting_manual", reason="synthesis_budget", next_stage="reader")
            return None
        pool = reduced
    if not pool:
        loop.update(phase="insufficient_scope", reason="no_supported_claims", next_stage="evidence")
        return None
    catalog = {c["claim_id"]: c for c in pool}
    reader_claims = [claim_view(c) for c in pool]
    if len(json.dumps(reader_claims, ensure_ascii=False)) > 85_000:
        loop.update(phase="waiting_manual", reason="synthesis_budget", next_stage="reader")
        return None
    feedback = loop.get("reader_feedback")
    prompt = (prompt_root / "reader-analysis-v1.md").read_text(encoding="utf-8")
    payload = {"claims": reader_claims, "item_id": pack["item_id"], **({"revision_feedback": feedback} if feedback else {})}
    unit = {"task_id": pack["item_id"]}
    drafted = invoke(unit, "reader", payload, reader_schema(), lambda v: validate_reader_draft(v, catalog, feedback), prompt)
    if drafted["state"] != "complete": return None
    subject = drafted["output"]
    reviews, review_calls = {}, {}
    for stage in REVIEW_STAGES:
        own = feedback.get("reviews", {}).get(stage, {}).get("issues", []) if feedback else []
        reviewed = invoke(unit, "reader_" + stage, {"stage_kind": "review", "item_id": pack["item_id"], "claims": reader_claims,
            "draft": subject, "candidate": loop["candidate"], "previous_own_issues": own}, review_schema(),
            lambda v: validate_reader_review(v, pack["item_id"], subject, catalog),
            (prompt_root / ("reader-" + stage + "-v1.md")).read_text(encoding="utf-8"))
        if reviewed["state"] != "complete": return None
        reviews[stage] = reviewed["output"]; review_calls[stage] = reviewed["call_id"]
    return {"draft": subject, "claims": pool, "selection_calls": selections, "analysis_call": drafted["call_id"],
            "reviews": reviews, "review_calls": review_calls, "issues": combine_reviews(reviews)}
