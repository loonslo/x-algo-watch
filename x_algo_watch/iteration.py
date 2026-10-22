"""有边界的研究修订循环；已完成候选和问题清单随研究事件持久化。"""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence import digest
from .model_policy import MAX_DAILY_CALLS, review_stages
from .models import WatchError

PHASES = {
    "ready": ("达到模型验收标准，待人工确认", "由所有者检查双页；正式速报仍须人工确认"),
    "waiting_service": ("接口暂未完成，等待续跑", "下次运行从失败阶段重试；不增加候选版本号"),
    "waiting_budget": ("当日调用预算不足", "UTC 次日预算恢复后，从缺少阶段继续"),
    "waiting_time": ("本次运行时间用完", "下次运行复用已完成阶段，继续当前候选"),
    "waiting_manual": ("输出校验或接口重试连续失败", "检查调用错误；处理后指定此版本并启用 resume_iterations 恢复"),
    "paused_limit": ("本批修订次数达到上限", "核对未解决问题；处理后指定此版本并启用 resume_iterations 恢复新批次"),
    "paused_stagnation": ("连续修订未见进展", "核对证据和问题；可收窄结论，处理后显式恢复"),
}
PAUSED = {"waiting_manual", "paused_limit", "paused_stagnation"}
REASONS = {"accepted": "全部模型审核同意", "service": "接口请求未完成", "service_retry_limit": "当前阶段累计三次接口失败", "invalid_output": "当前阶段两次输出校验失败", "daily_budget": "当日共享调用预算不足", "time_budget": "本次运行时间达到边界", "candidate_limit": "本批候选次数达到上限", "stagnation": "连续修订未解决可识别的重复问题"}
NEXT_LABELS = {"analysis": "起草或修订", "review": "机制审核", "facts_review": "事实与遗漏审核", "reader_review": "普通读者理解审核", "confirmation": "所有者确认", "revision": "修订稿件", "manual": "人工处理"}
RESULT_KEYS = {"state", "analysis", "review", "reviews", "extra_review_rounds", "resolved_models"}


def iteration_policy(settings: dict[str, Any]) -> tuple[int, int]:
    maximum = settings.get("max_candidate_versions", 5)
    stagnant = settings.get("stagnation_limit", 2)
    if type(maximum) is not int or not 2 <= maximum <= 10:
        raise WatchError("max_candidate_versions 必须为 2 到 10 的整数")
    if type(stagnant) is not int or not 1 <= stagnant <= 5:
        raise WatchError("stagnation_limit 必须为 1 到 5 的整数")
    return maximum, stagnant


def issue_ledger(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅按同字段、同证据、同问题文字识别重复；不冒充语义裁决。"""
    ledger: dict[str, Any] = {}
    for candidate in history:
        result = candidate["result"]
        for row in ledger.values():
            row["status"] = "not_repeated"  # 未再提出不等于人工确认或证实原意见正确。
            row["checked_in"] = candidate["number"]
        for issue in result["review"]["issues"]:
            canonical = {"field": issue["field"], "reason": " ".join(issue["reason"].split()), "evidence_refs": sorted(set(issue["evidence_refs"]))}
            identifier = "I-" + digest(canonical)[:16]
            row = ledger.setdefault(identifier, {"id": identifier, **canonical, "severity": "blocking", "first_seen": candidate["number"]})
            row.update(status="open", last_seen=candidate["number"], checked_in=candidate["number"], reviewers=[stage for stage, review in result.get("reviews", {"review": result["review"]}).items() if issue in review["issues"]])
        if result["review"]["agree"]:
            for row in ledger.values():
                row["status"] = "rechecked"
    return list(ledger.values())


def stalled(history: list[dict[str, Any]], start: int, limit: int) -> bool:
    batch = [row for row in history if row["number"] >= start]
    if len(batch) < limit + 1:
        return False
    for previous, current in zip(batch[-limit-1:-1], batch[-limit:]):
        a, b = previous["result"], current["result"]
        old = {digest(issue) for issue in a["review"]["issues"]}
        new = {digest(issue) for issue in b["review"]["issues"]}
        if a["analysis"] != b["analysis"] and not old <= new:
            return False
    return True


def validate_iteration(value: Any, pack: dict[str, Any], models: dict[str, str], rounds: int) -> bool:
    """发布边界也校验历史及衍生问题清单，不能靠 ready 标记绕过复核。"""
    from .research import analysis_projection, research_event, valid_research_event
    from .drafting import validate_analysis

    fields = {"schema_version", "context_hash", "history", "batch_start", "resume_count", "candidate", "phase", "next_stage", "ledger", "retry_epochs", "reason"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["context_hash"] != digest(pack):
        return False
    history = value["history"]
    if not isinstance(history, list) or len(history) > 500 or not isinstance(value["phase"], str) or value["phase"] not in PHASES:
        return False
    expected = {"ready": {"accepted"}, "waiting_service": {"service"}, "waiting_budget": {"daily_budget"}, "waiting_time": {"time_budget"}, "waiting_manual": {"service_retry_limit", "invalid_output"}, "paused_limit": {"candidate_limit"}, "paused_stagnation": {"stagnation"}}
    if not isinstance(value["reason"], str) or value["reason"] not in expected[value["phase"]]:
        return False
    if any(type(value[key]) is not int for key in ("candidate", "resume_count", "batch_start")) or value["resume_count"] < 0 or not 1 <= value["batch_start"] <= len(history) + 1:
        return False
    epochs = value["retry_epochs"]
    if not isinstance(epochs, dict) or set(epochs) - {"analysis", *review_stages(2)} or any(type(epoch) is not int or epoch < 0 for epoch in epochs.values()):
        return False
    for number, row in enumerate(history, 1):
        if not isinstance(row, dict) or set(row) != {"number", "result"} or type(row["number"]) is not int or row["number"] != number:
            return False
        result = row["result"]
        if not isinstance(result, dict) or set(result) - RESULT_KEYS or result.get("state") not in ("disputed", "pending_confirmation"):
            return False
        if not valid_research_event(research_event(pack, result, models), pack, required_extra_reviews=rounds):
            return False
        if validate_analysis(analysis_projection(result["analysis"]), pack, project_research=True):
            return False
        if number < len(history) and result["state"] != "disputed":
            return False
    complete = value["phase"] in {"ready", "paused_limit", "paused_stagnation"}
    if value["candidate"] != len(history) + (0 if complete else 1) or value["candidate"] < 1:
        return False
    if complete and (not history or (value["phase"] == "ready") != (history[-1]["result"]["state"] == "pending_confirmation")):
        return False
    return value["ledger"] == issue_ledger(history) and isinstance(value["next_stage"], str) and value["next_stage"] in NEXT_LABELS


def iterate_items(packs: list[dict[str, Any]], prior: list[dict[str, Any]], client: Any, settings: dict[str, Any], prompt_root: Path, *, call_deadline: float | None = None, resume: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .drafting import analyze_items
    from .project_evidence import research_profile

    maximum, stagnant = iteration_policy(settings)
    if resume and len(packs) != 1:
        raise WatchError("恢复修订须指定一个版本")
    profile = research_profile()
    outputs, events = {}, []
    for pack in packs:
        checkpoint = next((e for e in reversed(prior) if e.get("type") == "version_research" and e.get("item_id") == pack["item_id"] and e.get("profile_hash") == profile and "iteration" in e), None)
        if checkpoint:
            from .research import valid_research_event
            clean = {k: v for k, v in checkpoint.items() if k != "created_at"}
            if not valid_research_event(clean, pack, required_extra_reviews=settings.get("extra_review_rounds", 0)):
                raise WatchError("修订检查点无效，拒绝猜测恢复")
            loop = deepcopy(checkpoint["iteration"])
            if loop["phase"] == "ready" or (loop["phase"] in PAUSED and not resume):
                outputs[pack["item_id"]] = {k: v for k, v in clean.items() if k in RESULT_KEYS | {"repair_attempted", "iteration"}}
                continue
            if loop["phase"] in PAUSED and resume:
                if loop["phase"] == "waiting_manual":
                    stage = loop["next_stage"]
                    loop["retry_epochs"][stage] = loop["retry_epochs"].get(stage, 0) + 1
                loop.update(batch_start=len(loop["history"]) + 1, resume_count=loop["resume_count"] + 1)
        else:
            loop = {"schema_version": 1, "context_hash": digest(pack), "history": [], "batch_start": 1, "resume_count": 0, "retry_epochs": {}}
        history = loop["history"]
        while True:
            number = len(history) + 1
            ledger = issue_ledger(history)
            feedback = {**history[-1]["result"], "issue_ledger": ledger} if history else None
            current, calls = analyze_items([pack], prior + events, client, settings["models"], datetime.now(timezone.utc), prompt_root,
                settings.get("daily_calls", MAX_DAILY_CALLS), allow_same_model=settings.get("allow_same_model", False), project_research=True,
                repair_feedback={pack["item_id"]: feedback} if feedback else None, extra_review_rounds=settings.get("extra_review_rounds", 0),
                call_deadline=call_deadline, iteration_context={"version": 1, "candidate": number, "stage_epochs": loop["retry_epochs"]})
            events.extend(calls)
            result = current[pack["item_id"]]
            if "review" not in result:
                reason = result.get("suspension", {}).get("reason", "invalid_output")
                phase = {"daily_budget": "waiting_budget", "service": "waiting_service", "time_budget": "waiting_time"}.get(reason, "waiting_manual")
                # 每阶段累计三次接口失败后等待显式恢复，避免定时任务无界重试。
                if phase == "waiting_service":
                    failed = next((e for e in reversed(calls) if e.get("outcome") == "deferred"), None)
                    if failed and sum(e.get("analysis_key") == failed["analysis_key"] and e.get("outcome") == "deferred" for e in prior + events) >= 3:
                        phase = "waiting_manual"
                        reason = "service_retry_limit"
                loop.update(candidate=number, phase=phase, reason=reason, next_stage=result.get("suspension", {}).get("stage", "analysis"), ledger=ledger)
                break
            result = {k: v for k, v in result.items() if k in RESULT_KEYS}
            history.append({"number": number, "result": result})
            loop["retry_epochs"] = {}
            phase = "ready" if result["review"]["agree"] else (
                "paused_limit" if number - loop["batch_start"] + 1 >= maximum else (
                    "paused_stagnation" if stalled(history, loop["batch_start"], stagnant) else ""))
            if phase:
                reason = {"ready": "accepted", "paused_limit": "candidate_limit", "paused_stagnation": "stagnation"}[phase]
                loop.update(candidate=number, phase=phase, reason=reason, next_stage="confirmation" if phase == "ready" else "revision", ledger=issue_ledger(history))
                break
        if loop["phase"] in PAUSED and history:
            result = history[-1]["result"]
        outputs[pack["item_id"]] = {**result, "repair_attempted": loop["candidate"] > 1, "iteration": loop}
        # 服务中断不再尝试队列内其他版本；预算和时间限制由同一调用层执行。
        if loop["reason"] in {"service", "service_retry_limit"}:
            break
    return outputs, events


def render_iteration(loop: dict[str, Any]) -> str:
    label, recovery = PHASES[loop["phase"]]
    open_issues = [row for row in loop["ledger"] if row["status"] == "open"]
    text = f"### 生成与修订进度\n\n当前候选 V{loop['candidate']}；{label}。未解决问题 {len(open_issues)} 项。\n\n原因：{REASONS[loop['reason']]}。恢复条件：{recovery}。下一步骤：{NEXT_LABELS[loop['next_stage']]}。\n\n"
    if loop["history"]:
        text += "| 候选版本 | 审核结果 | 问题数 |\n| --- | --- | --- |\n"
        for row in loop["history"]:
            result = row["result"]
            text += f"| V{row['number']} | " + ("全部通过，尚未人工确认" if result["review"]["agree"] else "存在异议") + f" | {len(result['review']['issues'])} |\n"
        text += "\n"
    if open_issues:
        text += "<details><summary>未解决问题（供核对者处理）</summary>\n\n"
    for row in open_issues:
        text += "- `" + row["id"] + "` · " + row["field"] + "：" + row["reason"] + "（" + "、".join(row["evidence_refs"]) + "）\n"
    if open_issues:
        text += "\n</details>\n"
    return text + "\n"
