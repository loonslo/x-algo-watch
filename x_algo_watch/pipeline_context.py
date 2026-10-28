"""分任务证据与检测产物的连接；首批限试点，显式解除后全面接新链。"""

from copy import deepcopy
from pathlib import Path
from typing import Any

from .evidence import digest
from .git import Repository, SHA, safe_path
from .models import WatchError
from .research_plan import build_plan, fragment, load_plan_bundle, save_plan_bundle
from .stage_calls import PIPELINE


def pilot_enabled(settings: dict[str, Any], commit: str) -> bool:
    mode = settings.get("research_pipeline", "legacy")
    if mode not in {"legacy", PIPELINE}:
        raise WatchError("研究流程配置无效")
    commits = settings.get("mechanism_pilot_commits", [])
    if "mechanism_pilot_only" in settings and type(settings["mechanism_pilot_only"]) is not bool:
        raise WatchError("机制试点开关必须为布尔值")
    if not isinstance(commits, list) or any(not isinstance(c, str) or not SHA.fullmatch(c) for c in commits) or len(commits) != len(set(commits)):
        raise WatchError("机制试点必须配置不重复的完整提交号")
    return mode == PIPELINE and (settings.get("mechanism_pilot_only", True) is False or commit in commits)


def load_pipeline_plan(data: Path, pack: dict[str, Any]) -> dict[str, Any]:
    if pack.get("research_pipeline") != PIPELINE or pack.get("item_id") != digest({k: v for k, v in pack.items() if k != "item_id"}):
        raise WatchError("分任务研究上下文身份无效")
    if not SHA.fullmatch(pack.get("commit", "")) or not isinstance(pack.get("plan_id"), str) or len(pack["plan_id"]) != 64:
        raise WatchError("分任务研究计划身份无效")
    expected = "research-plans/" + pack["commit"] + "-" + pack["plan_id"] + "/manifest.json"
    if pack.get("plan_path") != expected:
        raise WatchError("分任务研究计划路径不匹配")
    plan = load_plan_bundle(data / expected)
    if any(plan.get(k) != pack.get(k) for k in ("commit", "base", "plan_id")):
        raise WatchError("分任务研究计划不属于当前上下文")
    return plan


def enrich_plan(repo: Repository, plan: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
    """补证实际加入被质疑的任务，旧任务及旧快照继续保留在历史中。"""
    if not 1 <= len(requests) <= 40:
        raise WatchError("补证请求数量超出单批边界")
    result = deepcopy(plan)
    result["parent_plan_id"] = plan["plan_id"]
    result["ancestor_plan_ids"] = [*plan.get("ancestor_plan_ids", []), plan["plan_id"]]
    result["budget_per_task"] = 85_000
    grouped: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"task_id", "issue_id", "path", "version", "start_line", "end_line", "reason"}:
            raise WatchError("补证请求结构无效")
        current = next((t["task_id"] for t in plan["tasks"] if request["task_id"] == t["task_id"] or request["task_id"] in t.get("ancestor_task_ids", [])), None)
        if current is None:
            raise WatchError("补证问题不属于当前任务")
        grouped.setdefault(current, []).append(request)
    known = {item["task_id"] for item in plan["tasks"]}
    if set(grouped) - known:
        raise WatchError("补证问题不属于当前任务")
    from .context_tasks import assign_id, split_context, validate_graph
    replacements = {}
    scheduled = []
    for item in result["tasks"]:
        old_id = item["task_id"]
        if old_id not in grouped:
            scheduled.append(item)
            continue
        additions = []
        for request in grouped[old_id]:
            if request["version"] not in {"before", "after"}:
                raise WatchError("补证版本无效")
            path = safe_path(request["path"])
            start, end = request["start_line"], request["end_line"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start > 2000:
                raise WatchError("补证范围无效")
            revision = plan["base"] if request["version"] == "before" else plan["commit"]
            source = repo.read(revision, path)
            if source is None or end > len(source.splitlines()):
                raise WatchError("请求源码在精确版本中不存在或行号越界")
            text = "".join(source.splitlines(keepends=True)[start - 1:end])
            entry = fragment(path, revision, text, start, "requested_context", "requested_window")
            additions.append((entry, request))
        if sum(len(e["text"]) for e in item["evidence"].values()) + sum(len(e["text"]) for e, _ in additions) > 60_000:
            scheduled.extend(split_context(item, additions))
        else:
            if not item.get("segments"):
                item["segments"] = [{"task_id": old_id, "evidence_refs": list(item["evidence"])}]
            for entry, request in additions:
                ref = "E" + str(max([int(r[1:]) for r in item["evidence"]] or [0]) + 1)
                item["evidence"][ref] = entry
                segment_id = "S-" + digest(request)
                item["segments"].append({"task_id": segment_id, "evidence_refs": [ref], "request": request})
        item["replaces_task_id"] = old_id
        item["ancestor_task_ids"] = [*item.get("ancestor_task_ids", []), old_id]
        item["supplement_requests"] = [*item.get("supplement_requests", []), *grouped[old_id]]
        assign_id(item)
        replacements[old_id] = item["task_id"]
        scheduled.append(item)
    # 子任务补证会连带改变其归并者；按拓扑顺序重新绑定所有上层身份。
    for item in scheduled:
        dependencies = item.get("context_dependencies", [])
        updated = []
        for identifier in dependencies:
            while identifier in replacements:
                identifier = replacements[identifier]
            updated.append(identifier)
        if dependencies != updated:
            old_id = item["task_id"]
            item["context_dependencies"] = updated
            item["replaces_task_id"] = old_id
            item["ancestor_task_ids"] = [*item.get("ancestor_task_ids", []), old_id]
            assign_id(item)
            replacements[old_id] = item["task_id"]
    # 同一节点可能既被补证又由子任务变化重绑，压平替换链。
    for identifier in replacements:
        while replacements[identifier] in replacements:
            replacements[identifier] = replacements[replacements[identifier]]
    result["tasks"] = scheduled
    for row in result["files"]:
        row["task_ids"] = [replacements.get(t, t) for t in row["task_ids"]]
    result["shared_context_task_ids"] = [replacements.get(t, t) for t in result["shared_context_task_ids"]]
    validate_graph(result)
    result["plan_id"] = digest({k: v for k, v in result.items() if k != "plan_id"})
    return result


def prepare_pipeline_context(repo: Repository, record: dict[str, Any], data: Path,
                             previous: dict[str, Any] | None = None, requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if previous and previous.get("research_pipeline") == PIPELINE:
        plan = load_pipeline_plan(data, previous)
        if requests:
            for start in range(0, len(requests), 40):
                plan = enrich_plan(repo, plan, requests[start:start + 40])
                save_plan_bundle(data / "research-plans", plan)
    else:
        plan = build_plan(repo, record)
    manifest = save_plan_bundle(data / "research-plans", plan)
    pack = {"schema_version": 1, "scope": "project_version", "research_pipeline": PIPELINE,
            "commit": record["commit"], "base": record["base"], "committed_at": record["committed_at"],
            "plan_id": plan["plan_id"], "plan_path": manifest.relative_to(data).as_posix(),
            "evidence": {}, "derived": {}, "related_facts": [],
            "coverage": {"changed_source_files": len(plan["files"]), "diff_files_included": [], "task_count": len(plan["tasks"]),
                         "scope": "全文件清单与分任务快照；尚未表示已完成研究"}}
    pack["item_id"] = digest(pack)
    return pack
