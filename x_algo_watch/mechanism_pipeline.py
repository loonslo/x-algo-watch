"""正式分任务研究链：机制起草、三角色审核、问题反馈和 Git 补证边界。"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .evidence import digest
from .iteration import iteration_policy
from .mechanisms import bind_claims, mechanism_schema, validate_mechanisms, validate_shape
from .mechanism_reviews import combine_reviews, review_schema, validate_review
from .model_policy import FREE_ROUTER, REVIEW_STAGES
from .pipeline_context import load_pipeline_plan
from .revision_audit import field_changes, issue_identity
from .stage_calls import PIPELINE, call_stage
from .models import WatchError
from .context_tasks import ContextViewTooLarge, MODEL_VIEW_LIMIT, dependencies_for, materialize, question_claims, task_schema, validate_task


def file_progress(plan: dict[str, Any], states: dict[str, Any], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {e["id"]: e for e in calls if e.get("type") == "model_call"}
    result = []
    for row in plan["files"]:
        if row.get("disposition") == "excluded":
            result.append({"path": row["path"], "state": "excluded", "reason": row["reason"], "audit_calls": []})
            continue
        required = row["task_ids"]
        done = [states[t] for t in required if states.get(t, {}).get("state") == "approved"]
        insufficient = False
        evidence_calls = []
        def inspect_context(state, depth=0):
            if depth > 100:
                raise WatchError("文件补证账本超过依赖深度边界")
            if state["analysis_call"] not in evidence_calls:
                evidence_calls.append(state["analysis_call"])
            raw = by_id[state["analysis_call"]]["output"]
            value = raw.get("result", raw)
            missing = any(s["disposition"] == "insufficient_evidence" for s in value["segments"])
            for child in state.get("dependencies", {}).values():
                missing |= inspect_context(child, depth + 1)
            return missing
        for state in done:
            insufficient |= inspect_context(state)
        complete = bool(required) and len(done) == len(required)
        state = "missing_context" if insufficient or not required else "studied" if complete else "detected_only"
        result.append({"path": row["path"], "state": state,
            "reason": "已核对当前任务，仍须区分源码与线上" if state == "studied" else ("尚未准备可研究的必要任务" if not required else "机制或补证任务仍有明确缺证据判断") if state == "missing_context" else "尚未完成本文件全部任务的三角色核对",
            "audit_calls": evidence_calls})
    return result


def revision_schema(task: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["result", "closure_reasons"], "properties": {
        "result": task_schema(task or {}), "closure_reasons": {"type": "array", "maxItems": 90, "items": {
            "type": "object", "additionalProperties": False, "required": ["issue_id", "reason"], "properties": {
                "issue_id": {"type": "string"}, "reason": {"type": "string", "minLength": 1, "maxLength": 1600}}}}}}


def validate_revision(value: Any, task: dict[str, Any], feedback: dict[str, Any]) -> list[str]:
    errors = validate_shape(value, revision_schema(task))
    if errors:
        return errors
    errors += validate_task(value["result"], task)
    before_keys = {c["key"] for c in feedback["analysis"]["claims"]}
    after_keys = {c["key"] for c in value["result"]["claims"]}
    if (before_keys - after_keys) and (after_keys - before_keys):
        errors.append("不能同版删除旧机制键并换入新键；应保留旧编号并标明证据不足或被否定，再新增独立发现")
    expected = {issue["issue_id"] for issue in feedback["issues"]}
    reasons = value["closure_reasons"]
    if {row["issue_id"] for row in reasons} != expected or len(reasons) != len(expected):
        errors.append("修订必须逐项保存旧问题的处理理由，不能只声明整体已解决")
    if all(issue["category"] == "expression" for issue in feedback["issues"]):
        allowed = [issue["field"] for issue in feedback["issues"]]
        # 将稳定机制键定位转换成原列表位置，限制表达修改范围。
        for change in field_changes(feedback["analysis"], value["result"]):
            field = change["field"]
            for index, claim in enumerate(feedback["analysis"]["claims"]):
                prefix = f"/claims/{index}/"
                if field.startswith(prefix):
                    field = "/claims/" + claim["key"] + "/" + field[len(prefix):]
            if not any(field == path or field.startswith(path + "/") for path in allowed):
                errors.append("表达修订改动了问题定位之外的字段：" + field)
    return errors


def pipeline_profile(prompt_root: Path) -> str:
    from .reader_pipeline import reader_schema, selection_schema
    paths = ["mechanism-analysis-v1", *["mechanism-" + stage + "-v1" for stage in REVIEW_STAGES]]
    paths += ["reader-analysis-v1", "reader-selection-v1", "context-analysis-v1", *["reader-" + stage + "-v1" for stage in REVIEW_STAGES]]
    return digest({"protocol": PIPELINE, "flow_revision": 4, "prompts": [(prompt_root / (p + ".md")).read_text(encoding="utf-8") for p in paths],
                   "analysis": mechanism_schema(), "review": review_schema(), "revision": revision_schema(),
                   "context": task_schema({"context_kind": "scan"}),
                   "reader": reader_schema(), "selection": selection_schema()})


def run_pipeline(pack: dict[str, Any], data: Path, prior: list[dict[str, Any]], client: Any, settings: dict[str, Any],
                 prompt_root: Path, *, resume: bool = False, deadline: float | None = None,
                 persist: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = load_pipeline_plan(data, pack)
    if settings.get("models") != {"analysis": FREE_ROUTER, "review": FREE_ROUTER} or settings.get("extra_review_rounds") != 2:
        raise WatchError("新机制流程必须使用免费路由和全部三个审核角色")
    profile = pipeline_profile(prompt_root)
    maximum, stagnant = iteration_policy(settings)
    checkpoint = next((e for e in reversed(prior) if e.get("type") == "version_research" and e.get("item_id") == pack["item_id"] and e.get("pipeline_profile") == profile), None)
    loop = deepcopy(checkpoint["pipeline_loop"]) if checkpoint else {
        "candidate": 1, "batch_start": 1, "resume_count": 0, "history": [], "feedback": {},
        "phase": "waiting_service", "reason": "service", "next_stage": "mechanism"}
    if not checkpoint and plan.get("parent_plan_id"):
        from .context_recovery import ancestor_ids, context_feedback
        ancestors = ancestor_ids(data, plan)
        previous = next((e for e in reversed(prior) if e.get("type") == "version_research" and e.get("commit") == pack["commit"]
                         and e.get("pipeline_profile") == profile and e.get("plan_id") in ancestors), None)
        if previous:
            recovered = context_feedback(plan, previous, data, prior)
            loop.update(recovered, candidate=previous["pipeline_loop"]["candidate"] + 1,
                        batch_start=previous["pipeline_loop"]["batch_start"], resume_count=previous["pipeline_loop"]["resume_count"])
            loop["feedback"] = deepcopy(recovered["context_feedback"])
            if recovered.get("context_reader_feedback"):
                loop["reader_feedback"] = deepcopy(recovered["context_reader_feedback"])
    events = []
    task_states = {}
    reader_result = None
    stop = False
    if checkpoint and (loop["phase"] == "ready" or (loop["phase"] in {"waiting_manual", "paused_limit", "paused_stagnation"} and not resume)):
        return {k: v for k, v in checkpoint.items() if k != "created_at"}, []
    if loop["phase"] in {"paused_limit", "paused_stagnation"} and resume:
        loop["candidate"] += 1
    if resume:
        loop["batch_start"] = loop["candidate"]
        loop["resume_count"] += 1
    run_id = datetime.now(timezone.utc).isoformat()
    initial_loop = deepcopy(loop)
    initial_loop.update(phase="researching", reason="in_progress")
    provisional = {"type": "version_research", "commit": pack["commit"], "item_id": pack["item_id"],
        "profile_hash": profile, "pipeline_profile": profile, "pipeline_protocol": PIPELINE, "plan_id": plan["plan_id"], "run_id": run_id,
        "state": "pending", "models": {"analysis": FREE_ROUTER, "review": FREE_ROUTER}, "repair_attempted": loop["candidate"] > 1,
        "review_mode": "free_router", "coverage": pack["coverage"], "sources": {}, "pipeline_loop": initial_loop,
        "iteration": {k: initial_loop[k] for k in ("candidate", "phase", "reason", "next_stage")}, "task_states": {},
        "file_states": file_progress(plan, {}, prior)}
    provisional["id"] = "research:" + digest(provisional)
    events.append(provisional)
    if persist: persist(provisional)
    if not resume and loop["candidate"] - loop["batch_start"] >= maximum:
        loop["candidate"] -= 1
        loop.update(phase="paused_limit", reason="candidate_limit", next_stage="revision")
        stop = True

    def invoke(task, stage, payload, schema, validator, prompt):
        if len(json.dumps(payload, ensure_ascii=False)) > MODEL_VIEW_LIMIT:
            loop.update(phase="insufficient_scope", reason="synthesis_budget", next_stage="evidence")
            return {"state": "pending", "reason": "synthesis_budget"}
        result, calls = call_stage(unit_id=task["task_id"], commit=pack["commit"], stage=stage,
            payload=payload, schema=schema, validate=validator, prompt=prompt, prior=prior + events,
            client=client, now=datetime.now(timezone.utc), daily_limit=settings.get("daily_calls", 1000),
            resume=resume, deadline=deadline, persist=persist)
        events.extend(calls)
        if result["state"] != "complete":
            loop.update(phase="waiting_manual" if result["state"] == "waiting_manual" else {
                "daily_budget": "waiting_budget", "time_budget": "waiting_time", "service": "waiting_service"}[result["reason"]],
                reason=result["reason"], next_stage=stage)
        return result

    while not stop:
        number = loop["candidate"]
        task_states = {}
        for original_task in plan["tasks"]:
            dependency_states = dependencies_for(original_task, task_states)
            try:
                task = materialize(plan, original_task, dependency_states, prior + events)
            except ContextViewTooLarge:
                loop.update(phase="insufficient_scope", reason="synthesis_budget", next_stage="evidence")
                stop = True
                break
            feedback = loop["feedback"].get(task["task_id"])
            payload = {"evidence_pack": task, "shared_context_catalog": [
                {"task_id": item["task_id"], "path": item["path"]} for item in plan["tasks"] if item["task_id"] in plan["shared_context_task_ids"]]}
            prompt = (prompt_root / "mechanism-analysis-v1.md").read_text(encoding="utf-8")
            review_questions = {}
            if task.get("context_kind"):
                prompt = (prompt_root / "context-analysis-v1.md").read_text(encoding="utf-8")
                namespace = task["context_question"]["owner_namespace"]
                owners = [t for t in plan["tasks"] if t["claim_namespace"] == namespace]
                owner_feedback = next((loop["feedback"][t["task_id"]] for t in owners if t["task_id"] in loop["feedback"]), None)
                if owner_feedback:
                    payload["original_question"] = {
                        "claims": question_claims(owner_feedback["analysis"]),
                        "issues": [{k: i[k] for k in ("issue_id", "field", "category", "reason")} for i in owner_feedback["issues"]]}
                    for role in REVIEW_STAGES:
                        review_questions[role] = {
                            "claims": payload["original_question"]["claims"],
                            "issues": [{"issue_id": issue_identity(i), **{k: i[k] for k in ("field", "category", "reason")}}
                                       for i in owner_feedback.get("reviews", {}).get(role, {}).get("issues", [])]}
            if dependency_states:
                prompt += "\ncontext_catalog 是已三关复核的分块事实，不是生产运行证据。必须逐条处理所有 D- 依赖区段，说明如何使用或为何不相关，不得丢弃反证、未知和测试/mock 用途。引用只能使用当前 evidence 中程序还原的真实源码片段。"
            if feedback:
                payload["revision_feedback"] = feedback
                prompt += "\n这是定点修订。按问题类别处理所有指定问题，保留相同机制 key。表达问题只改相应表达；事实或推断错误修正全部依赖字段；缺证据不能猜填。输出 result 及每项旧问题的 closure_reasons，理由不等于已通过，之后仍由三个角色重审。"
            schema = revision_schema(task) if feedback else task_schema(task)
            validator = (lambda value, task=task, feedback=feedback: validate_revision(value, task, feedback)) if feedback else (lambda value, task=task: validate_task(value, task))
            drafted = invoke(task, "mechanism", payload, schema, validator, prompt)
            if drafted["state"] != "complete":
                stop = True
                break
            raw = drafted["output"]
            analysis = raw["result"] if feedback else raw
            closures = raw["closure_reasons"] if feedback else []
            claims = bind_claims(analysis, task, plan["plan_id"])
            reviews, review_calls = {}, {}
            for stage in REVIEW_STAGES:
                own = feedback.get("reviews", {}).get(stage, {}).get("issues", []) if feedback else []
                payload = {"stage_kind": "review", "evidence_pack": task, "analysis": analysis,
                           "claim_catalog": [{"key": c["key"], "claim_id": c["claim_id"]} for c in claims],
                           "candidate": number, "previous_own_issues": own}
                if stage in review_questions:
                    payload["original_question"] = review_questions[stage]
                review_prompt = (prompt_root / ("mechanism-" + stage + "-v1.md")).read_text(encoding="utf-8")
                if dependency_states or task.get("context_kind"):
                    review_prompt += "\n这是补证分块/归并或依赖补证的原机制。逐项核对子报告和 D- 区段的覆盖、反证、未知及用途；仅处理一部分不能作为整体结论。分块及归并不能输出用户推测。条件句不能让 mock 变成生产依据。"
                result = invoke(task, "mechanism_" + stage, payload, review_schema(),
                    lambda value, task=task, analysis=analysis: validate_review(value, task["task_id"], analysis, task),
                    review_prompt)
                if result["state"] != "complete":
                    stop = True
                    break
                reviews[stage] = result["output"]
                review_calls[stage] = result["call_id"]
            if stop:
                break
            issues = combine_reviews(reviews)
            changes = field_changes(feedback["analysis"], analysis) if feedback else []
            seen = {i["issue_id"] for entry in loop["history"] for i in entry["issues"]} | set(loop.get("context_seen_issues", []))
            previous_row = next((r for r in reversed(loop["history"]) if r["task_id"] == task["task_id"]), None)
            previous_ids = {i["issue_id"] for i in previous_row["issues"]} if previous_row else set(loop.get("context_last_issues", {}).get(task["task_id"], []))
            current_ids = {i["issue_id"] for i in issues}
            row = {"candidate": number, "task_id": task["task_id"], "analysis_call": drafted["call_id"],
                   "review_calls": review_calls, "issues": issues, "changes": changes, "closure_reasons": closures,
                   "regressions": sorted((current_ids - previous_ids) & seen)}
            context_proof = {"dependencies": dependency_states, "context_view_hash": digest(task)} if dependency_states or task.get("context_kind") else {}
            row.update(context_proof)
            if not any(old["candidate"] == number and old["task_id"] == task["task_id"] for old in loop["history"]):
                loop["history"].append(row)
            task_states[task["task_id"]] = {"analysis_call": drafted["call_id"], "review_calls": review_calls,
                                          "state": "disputed" if issues else "approved", **context_proof}
            if not issues:
                continue
            requests = []
            for issue in issues:
                for opinion in issue["opinions"]:
                    for request in opinion["evidence_requests"]:
                        enriched = {**request, "task_id": task["task_id"], "issue_id": issue["issue_id"]}
                        if enriched not in requests:
                            requests.append(enriched)
            if requests:
                loop.update(phase="waiting_evidence", reason="evidence_gap", next_stage="evidence", evidence_requests=requests)
                stop = True
                break
            loop["feedback"][task["task_id"]] = {"analysis": analysis, "reviews": reviews, "issues": issues}
            disputed = [entry for entry in loop["history"] if entry["issues"] and entry["candidate"] >= loop["batch_start"]]
            stalled = len(disputed) >= stagnant + 1 and all(
                {i["issue_id"] for i in a["issues"]} <= {i["issue_id"] for i in b["issues"]}
                for a, b in zip(disputed[-stagnant-1:-1], disputed[-stagnant:]))
            if number - loop["batch_start"] + 1 >= maximum or stalled:
                loop.update(phase="paused_stagnation" if stalled else "paused_limit", reason="stagnation" if stalled else "candidate_limit", next_stage="revision")
                stop = True
            else:
                loop["candidate"] += 1
            break
        else:
            from .reader_pipeline import run_reader
            if any(row.get("disposition") != "excluded" and not row["task_ids"] for row in plan["files"]):
                loop.update(phase="insufficient_scope", reason="unprepared_file", next_stage="evidence")
                stop = True
                continue
            reader_result = run_reader(plan, pack, task_states, prior + events, loop, prompt_root, invoke)
            if reader_result is None:
                stop = True
                continue
            issues = reader_result["issues"]
            old = loop.get("reader_feedback")
            history = loop.setdefault("reader_history", [])
            seen_reader = {i["issue_id"] for entry in history for i in entry["issues"]} | set(loop.get("context_seen_issues", []))
            previous_reader = {i["issue_id"] for i in history[-1]["issues"]} if history else {i["issue_id"] for i in loop.get("context_reader_feedback", {}).get("issues", [])}
            history.append({"candidate": number, "analysis_call": reader_result["analysis_call"], "review_calls": reader_result["review_calls"],
                "task_states": deepcopy(task_states),
                "selection_calls": reader_result["selection_calls"], "claim_ids": [c["claim_id"] for c in reader_result["claims"]],
                "draft": reader_result["draft"], "issues": issues,
                "changes": field_changes(old["draft"], reader_result["draft"]) if old else [],
                "closure_reasons": reader_result["draft"]["closure_reasons"],
                "regressions": sorted(({i["issue_id"] for i in issues} - previous_reader) & seen_reader)})
            if not issues:
                loop.update(phase="ready", reason="accepted", next_stage="confirmation")
                stop = True
                continue
            requests = []
            catalog = {c["claim_id"]: c for c in reader_result["claims"]}
            for issue in issues:
                for opinion in issue["opinions"]:
                    for request in opinion["evidence_requests"]:
                        value = {**request, "task_id": catalog[issue["claim_id"]]["task_id"], "issue_id": issue["issue_id"]}
                        if value not in requests: requests.append(value)
            if requests:
                loop.update(phase="waiting_evidence", reason="evidence_gap", next_stage="evidence", evidence_requests=requests)
                stop = True
                continue
            loop["reader_feedback"] = {"draft": reader_result["draft"], "reviews": reader_result["reviews"], "issues": issues}
            disputed = [r for r in history if r["issues"] and r["candidate"] >= loop["batch_start"]]
            stalled = len(disputed) >= stagnant + 1 and all({i["issue_id"] for i in a["issues"]} <= {i["issue_id"] for i in b["issues"]}
                for a, b in zip(disputed[-stagnant-1:-1], disputed[-stagnant:]))
            if stalled or number - loop["batch_start"] + 1 >= maximum:
                loop.update(phase="paused_stagnation" if stalled else "paused_limit", reason="stagnation" if stalled else "candidate_limit", next_stage="revision")
                stop = True
            else:
                loop["candidate"] += 1
    state = "pending_confirmation" if loop["phase"] == "ready" else "insufficient_evidence" if loop["phase"] in {"waiting_evidence", "insufficient_scope"} else "disputed" if loop["phase"] in {"paused_limit", "paused_stagnation"} else "manual" if loop["phase"] == "waiting_manual" else "pending"
    event = {"type": "version_research", "commit": pack["commit"], "item_id": pack["item_id"],
             "run_id": run_id,
             "profile_hash": profile, "pipeline_profile": profile, "pipeline_protocol": PIPELINE,
             "plan_id": plan["plan_id"], "state": state, "models": {"analysis": FREE_ROUTER, "review": FREE_ROUTER},
             "repair_attempted": loop["candidate"] > 1, "review_mode": "free_router", "coverage": pack["coverage"], "sources": {},
             "pipeline_loop": loop, "iteration": {k: loop[k] for k in ("candidate", "phase", "reason", "next_stage")},
             "task_states": task_states, "file_states": file_progress(plan, task_states, prior + events)}
    if reader_result and loop["phase"] == "ready":
        event.update(analysis={"reader": reader_result["draft"]["reader"]}, reader_claims=reader_result["claims"],
                     reader_provenance=reader_result["draft"]["field_claims"], reviews=reader_result["reviews"],
                     review={"item_id": pack["item_id"], "agree": True, "issues": []}, extra_review_rounds=2)
    event["id"] = "research:" + digest(event)
    return event, events


def validate_pipeline_event(event: dict[str, Any], pack: dict[str, Any], data: Path, calls: list[dict[str, Any]]) -> bool:
    """发布端重新检查来源、调用结果及裁决；当前未实现读者稿时禁止 ready。"""
    try:
        if event.get("pipeline_protocol") != PIPELINE or event.get("id") != "research:" + digest({k: v for k, v in event.items() if k not in {"id", "created_at"}}):
            return False
        plan = load_pipeline_plan(data, pack)
        if event.get("models") != {"analysis": FREE_ROUTER, "review": FREE_ROUTER} or event.get("review_mode") != "free_router":
            return False
        if event["commit"] != pack["commit"] or event["item_id"] != pack["item_id"] or event["plan_id"] != plan["plan_id"]:
            return False
        if event["state"] not in {"pending", "manual", "disputed", "insufficient_evidence", "pending_confirmation"}:
            return False
        loop = event["pipeline_loop"]
        expected = "pending_confirmation" if loop["phase"] == "ready" else "insufficient_evidence" if loop["phase"] in {"waiting_evidence", "insufficient_scope"} else "disputed" if loop["phase"] in {"paused_limit", "paused_stagnation"} else "manual" if loop["phase"] == "waiting_manual" else "pending"
        if event["state"] != expected or event["iteration"] != {k: loop[k] for k in ("candidate", "phase", "reason", "next_stage")}:
            return False
        from .iteration import PHASES, REASONS
        if loop["phase"] not in PHASES or loop["reason"] not in REASONS or loop["candidate"] < 1 or loop["batch_start"] > loop["candidate"]:
            return False
        by_id = {e["id"]: e for e in calls if e.get("type") == "model_call"}
        tasks = {item["task_id"]: item for item in plan["tasks"]}
        if event.get("file_states") != file_progress(plan, event["task_states"], calls):
            return False

        def row_item(task_id, row):
            item = tasks[task_id]
            return materialize(plan, item, row.get("dependencies", {}), calls)

        def check_row(task_id, row, depth=0):
            if depth > 100:
                return False
            original_item = tasks[task_id]
            dependencies = row.get("dependencies", {})
            if set(dependencies) != set(original_item.get("context_dependencies", [])):
                return False
            if any(child.get("state") != "approved" or not check_row(t, child, depth + 1) for t, child in dependencies.items()):
                return False
            item = row_item(task_id, row)
            draft = by_id[row["analysis_call"]]
            if draft.get("valid") is not True or draft.get("stage") != "mechanism" or draft.get("item_id") != task_id or draft.get("model") != FREE_ROUTER or draft.get("commit") != pack["commit"]:
                return False
            output = draft["output"].get("result", draft["output"])
            if validate_task(output, item) or set(row["review_calls"]) != set(REVIEW_STAGES):
                return False
            if (dependencies or item.get("context_kind")) and (row.get("context_view_hash") != digest(item) or draft.get("evidence_view_hash") != digest(item)):
                return False
            reviews = {}
            for stage in REVIEW_STAGES:
                call = by_id[row["review_calls"][stage]]
                if call.get("valid") is not True or call.get("stage") != "mechanism_" + stage or call.get("item_id") != task_id or call.get("model") != FREE_ROUTER:
                    return False
                if validate_review(call["output"], task_id, output, item):
                    return False
                if (dependencies or item.get("context_kind")) and call.get("evidence_view_hash") != digest(item):
                    return False
                reviews[stage] = call["output"]
            issues = combine_reviews(reviews)
            return issues == row["issues"] if "issues" in row else (row["state"] == ("disputed" if issues else "approved"))

        if any(not check_row(task_id, row) for task_id, row in event["task_states"].items()):
            return False
        if loop.get("context_previous"):
            from .context_recovery import ancestor_ids, context_feedback
            import json
            sources = {e["id"]: e for e in calls if e.get("type") == "version_research"}
            previous_root = sources[loop["context_previous"]]
            if previous_root["plan_id"] not in ancestor_ids(data, plan): return False
            previous_pack = json.loads((data / "project-context" / (pack["commit"] + "-" + previous_root["item_id"] + ".json")).read_text(encoding="utf-8"))
            if not validate_pipeline_event(previous_root, previous_pack, data, calls): return False
            recovered = context_feedback(plan, previous_root, data, calls)
            if any(loop.get(k) != v for k, v in recovered.items()): return False
        feedback_by_task = deepcopy(loop.get("context_feedback", {}))
        last_issues = {key: set(ids) for key, ids in loop.get("context_last_issues", {}).items()}
        seen_issues = set(loop.get("context_seen_issues", []))
        for row in loop["history"]:
            task_id = row["task_id"]
            if not check_row(task_id, row): return False
            raw = by_id[row["analysis_call"]]["output"]
            analysis = raw.get("result", raw)
            old = feedback_by_task.get(task_id)
            if old and validate_revision(raw, row_item(task_id, row), old): return False
            if row["changes"] != (field_changes(old["analysis"], analysis) if old else []): return False
            if row["closure_reasons"] != (raw["closure_reasons"] if old else []): return False
            identifiers = {i["issue_id"] for i in row["issues"]}
            if row["regressions"] != sorted((identifiers - last_issues.get(task_id, set())) & seen_issues): return False
            seen_issues.update(identifiers); last_issues[task_id] = identifiers
            if row["issues"] and not any(op["evidence_requests"] for issue in row["issues"] for op in issue["opinions"]):
                feedback_by_task[task_id] = {"analysis": analysis, "issues": row["issues"]}
        if loop["phase"] == "waiting_reader" and (set(event["task_states"]) != set(tasks) or any(row["state"] != "approved" for row in event["task_states"].values())):
            return False
        if loop["phase"] == "waiting_evidence" and not loop.get("evidence_requests"):
            return False
        from .reader_pipeline import frozen_claims, verify_selection_receipts, validate_reader_draft, validate_reader_review
        previous = loop.get("context_reader_feedback")
        seen_reader = set(loop.get("context_seen_issues", []))
        last_reader = None
        for row in loop.get("reader_history", []):
            states = row["task_states"]
            if set(states) != set(tasks) or any(state["state"] != "approved" or not check_row(t, state) for t, state in states.items()):
                return False
            frozen = frozen_claims(plan, states, calls)
            selected = verify_selection_receipts(frozen, row["selection_calls"], by_id)
            if row["claim_ids"] != selected:
                return False
            catalog = {c: frozen[c] for c in selected}
            draft = by_id[row["analysis_call"]]
            if draft.get("valid") is not True or draft.get("stage") != "reader" or draft.get("item_id") != pack["item_id"] or draft.get("model") != FREE_ROUTER or draft["output"] != row["draft"]:
                return False
            if validate_reader_draft(row["draft"], catalog, previous):
                return False
            reviews = {}
            if set(row["review_calls"]) != set(REVIEW_STAGES):
                return False
            for stage, identifier in row["review_calls"].items():
                call = by_id[identifier]
                if call.get("valid") is not True or call.get("stage") != "reader_" + stage or call.get("item_id") != pack["item_id"] or call.get("model") != FREE_ROUTER:
                    return False
                if validate_reader_review(call["output"], pack["item_id"], row["draft"], catalog):
                    return False
                reviews[stage] = call["output"]
            if combine_reviews(reviews) != row["issues"]:
                return False
            identifiers = {i["issue_id"] for i in row["issues"]}
            previous_ids = {i["issue_id"] for i in previous["issues"]} if previous else set()
            if row["regressions"] != sorted((identifiers - previous_ids) & seen_reader): return False
            seen_reader.update(identifiers)
            previous = {"draft": row["draft"], "reviews": reviews, "issues": row["issues"]}
            last_reader = (row, catalog, reviews)
        if loop["phase"] == "ready":
            if not last_reader or last_reader[0]["issues"] or set(event["task_states"]) != set(tasks):
                return False
            row, catalog, reviews = last_reader
            if event["task_states"] != row["task_states"] or loop["candidate"] != row["candidate"]:
                return False
            if event.get("analysis") != {"reader": row["draft"]["reader"]} or event.get("reader_provenance") != row["draft"]["field_claims"] or event.get("reader_claims") != list(catalog.values()) or event.get("reviews") != reviews:
                return False
            if event.get("review") != {"item_id": pack["item_id"], "agree": True, "issues": []} or event.get("extra_review_rounds") != 2:
                return False
        elif any(key in event for key in ("analysis", "review", "reviews", "reader_claims")):
            return False
        return True
    except (KeyError, TypeError, ValueError, IndexError, OSError, WatchError):
        return False
