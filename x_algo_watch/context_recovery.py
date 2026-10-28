"""补证跨快照保留原候选、稳定问题及读者修订责任。"""

from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from .models import WatchError
from .research_plan import load_plan_bundle


def read_plan(data: Path, commit: str, identity: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise WatchError("补证来源身份无效")
    return load_plan_bundle(data / "research-plans" / (commit + "-" + identity) / "manifest.json")


def ancestor_ids(data: Path, plan: dict[str, Any]) -> set[str]:
    identities = set()
    parent = plan.get("parent_plan_id")
    while parent:
        if parent in identities or len(identities) >= 100:
            raise WatchError("补证计划血缘循环或超过恢复边界")
        identities.add(parent)
        parent = read_plan(data, plan["commit"], parent).get("parent_plan_id")
    return identities


def context_feedback(plan: dict[str, Any], previous: dict[str, Any], data: Path, calls: list[dict[str, Any]]) -> dict[str, Any]:
    from .reader_pipeline import frozen_claims
    by_id = {e["id"]: e for e in calls if e.get("type") == "model_call"}
    old_plan = read_plan(data, previous["commit"], previous["plan_id"])
    old_rows = {r["task_id"]: r for r in previous["pipeline_loop"]["history"]}
    reader_rows = previous["pipeline_loop"].get("reader_history", [])
    reader_row = reader_rows[-1] if reader_rows and reader_rows[-1]["issues"] else None
    old_claims = frozen_claims(old_plan, reader_row["task_states"], calls) if reader_row else {}
    feedback = {}
    last_issues = {}
    for item in plan["tasks"]:
        aliases = {item["task_id"], *item.get("ancestor_task_ids", []), item.get("replaces_task_id")}
        target = next((t for t in reversed(list(old_rows)) if t in aliases), None)
        if target is None:
            continue
        row = old_rows[target]
        last_issues[item["task_id"]] = sorted(i["issue_id"] for i in row["issues"])
        if target == item["task_id"]:
            continue
        raw = by_id[row["analysis_call"]]["output"]
        relevant = [i for i in (reader_row["issues"] if reader_row else []) if old_claims.get(i["claim_id"], {}).get("task_id") == target]
        issues = [*row["issues"], *relevant]
        if not issues: continue
        reviews = {}
        for stage, identifier in row["review_calls"].items():
            own = list(by_id[identifier]["output"]["issues"])
            if relevant:
                own.extend(i for i in by_id[reader_row["review_calls"][stage]]["output"]["issues"] if old_claims.get(i["claim_id"], {}).get("task_id") == target)
            reviews[stage] = {"issues": own}
        feedback[item["task_id"]] = {"analysis": raw.get("result", raw), "reviews": reviews, "issues": issues,
            "previous_event": previous["id"], "previous_task_id": target, "analysis_call": row["analysis_call"]}
    seen = set(previous["pipeline_loop"].get("context_seen_issues", []))
    seen.update(i["issue_id"] for r in [*previous["pipeline_loop"]["history"], *reader_rows] for i in r["issues"])
    result = {"context_previous": previous["id"], "context_history": [*previous["pipeline_loop"].get("context_history", []), previous["id"]],
              "context_feedback": feedback, "context_seen_issues": sorted(seen), "context_last_issues": last_issues}
    if reader_row:
        result["context_reader_feedback"] = {"draft": reader_row["draft"], "issues": reader_row["issues"],
            "reviews": {stage: by_id[identifier]["output"] for stage, identifier in reader_row["review_calls"].items()}}
    elif previous["pipeline_loop"].get("context_reader_feedback"):
        result["context_reader_feedback"] = previous["pipeline_loop"]["context_reader_feedback"]
    return deepcopy(result)
