"""同一研究状态生成用户正文及技术页，不发布未通过的正文。"""

import hashlib
import html
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from .git import UPSTREAM
from .publication import publication_status, render_unpublished


def safe(value: Any) -> str:
    return re.sub(r"([\\`*_[\]|])", r"\\\1", html.escape(str(value)))


def links(refs: list[str], catalog: dict[str, Any]) -> str:
    result = []
    for ref in refs:
        claim = catalog[ref]
        citations = claim["support"] or claim["after"]["citations"] or claim["before"]["citations"]
        if citations:
            cite = citations[0]
            entry = claim["source_locations"][cite["evidence_ref"]]
            url = f"{UPSTREAM}/blob/{entry['commit']}/{quote(entry['path'], safe='/')}#L{cite['start_line']}"
            result.append("[机制依据](" + url + ")")
    return " · ".join(dict.fromkeys(result))


def render_pipeline(event: dict[str, Any], context_base: str, *, technical_only: bool = False,
                    check_complete: bool = True, data_root: Path | None = None) -> str:
    status = publication_status(event, complete=check_complete)
    catalog = {c["claim_id"]: c for c in event.get("reader_claims", [])}
    text = render_unpublished(event, complete=check_complete) if not status["publishable"] else ""
    if not technical_only:
        if not status["publishable"]: return text
        reader = event["analysis"]["reader"]
        text = "## 算法版本更新公告\n\n非官方源码解读；机制推测尚未人工确认，正式 digest 仍需所有者确认。\n\n"
        text += "### " + safe(reader["title"]) + "\n\n" + safe(reader["what_changed"]) + "\n\n"
        text += "**线上与生效条件：** " + safe(reader["when"]) + "\n\n**可能涉及谁：** " + safe(reader["who"]) + "\n\n"
        text += links(reader["evidence_refs"], catalog) + "\n\n"
        if reader["impacts"]:
            text += "## 你可能感受到的变化\n\n"
        for row in reader["impacts"]:
            text += "### " + safe(row["heading"]) + "\n\n"
            if row["before"]:
                text += "| 以前的处理 | 现在的代码如何处理 |\n| --- | --- |\n| " + safe(row["before"]).replace("\n", " ") + " | " + safe(row["after"]).replace("\n", " ") + " |\n\n"
            else:
                text += "此前如何处理尚不明确。当前代码：" + safe(row["after"]) + "\n\n"
            text += "**可能影响：** " + safe(row["effect"]) + "\n\n- 成立条件：" + safe(row["conditions"]) + "\n- 判断边界：" + safe(row["limits"]) + "\n\n" + links(row["evidence_refs"], catalog) + "\n\n"
        text += "## 现在该怎么做\n\n" + safe(reader["action"]) + "\n\n"
        if reader["terms"]:
            text += "### 文中用词\n\n"
            text += "\n".join("- **" + safe(t["term"]) + "**：" + safe(t["meaning"]) for t in reader["terms"]) + "\n\n"
        return text
    text += "### 分任务研究进度\n\n"
    states = event.get("task_states", {})
    text += f"本轮已完成三角色核对的任务 {sum(row['state'] == 'approved' for row in states.values())} 个；任务数不代表用户体验或影响大小。\n\n"
    base = context_base.removesuffix('project-context')
    plan_url = f"{base}research-plans/{event['commit']}-{event['plan_id']}/manifest.json"
    text += f"[完整文件清单与不可变证据快照]({plan_url})\n\n"
    text += "### 每个变化文件的当前处理结果\n\n| 文件 | 研究状态 | 说明 |\n| --- | --- | --- |\n"
    labels = {"studied": "已研究", "detected_only": "仅检测", "missing_context": "缺少上下文", "excluded": "已排除"}
    for row in event.get("file_states", []):
        text += "| " + safe(row["path"]) + " | " + labels[row["state"]] + " | " + safe(row["reason"]) + " |\n"
    text += "\n"
    if data_root:
        from .research_plan import load_plan_bundle
        from .storage import RecordStore
        from .mechanisms import bind_claims
        from .context_tasks import materialize
        plan = load_plan_bundle(data_root / "research-plans" / (event['commit'] + "-" + event['plan_id']) / "manifest.json")
        calls = {e["id"]: e for e in RecordStore(data_root).events() if e["type"] == "model_call"}
        text += "### 机制研究（仅展示本轮三角色均通过的任务）\n\n"
        for item in plan["tasks"]:
            state = states.get(item["task_id"], {})
            if state.get("state") != "approved": continue
            raw = calls[state["analysis_call"]]["output"]
            item = materialize(plan, item, state.get("dependencies", {}), list(calls.values()))
            claims = bind_claims(raw.get("result", raw), item, plan["plan_id"])
            for claim in claims:
                text += "#### " + safe(claim["key"]) + "\n\n- 环节：" + safe(claim["stage"]) + "；判断：" + safe(claim["verdict"]) + "\n"
                for label, value in (("以前", claim["before"]["behavior"]), ("现在", claim["after"]["behavior"]),
                                     ("调用入口", claim["entrypoint"]["text"]), ("启用条件", claim["activation"]["text"]),
                                     ("中间结果", claim["intermediate_effect"]), ("用户效果缺口", claim["user_effect_gap"]),
                                     ("替代解释", claim["alternative"]), ("验证办法", claim["verify"])):
                    text += "- " + label + "：" + safe(value) + "\n"
                text += "- 具体源码：" + links([claim["claim_id"]], {claim["claim_id"]: claim}) + "\n\n"
            for stage, call_id in state["review_calls"].items():
                name = hashlib.sha256(call_id.encode()).hexdigest() + ".json"
                model = calls[call_id].get("resolved_model") or "接口未返回型号"
                text += f"- {safe(stage)}：[原始裁决]({base}research-runs/{event['commit']}/{name})；实际型号：{safe(model)}\n"
            text += "\n"
    loop = event["pipeline_loop"]
    for entry in loop["history"] + loop.get("reader_history", []):
        if entry["issues"]:
            text += "#### 候选 V" + str(entry["candidate"]) + " 的历史审核问题（不代表当前仍未解决）\n\n"
            for issue in entry["issues"]:
                text += "- " + safe(issue["issue_id"] + " · " + issue["field"] + "：" + issue["reason"]) + "\n"
    text += "\n完整机制稿、各角色裁决、筛选去向、修订差异和问题处理理由均保留在分文件审计中；线上部署仍未核实。\n"
    return text
