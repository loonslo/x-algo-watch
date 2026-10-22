"""整版本模型研究的结构与阅读呈现；推测必须给出机制、条件及替代解释。"""

import json
from typing import Any
from urllib.parse import quote

from .evidence import digest
from .git import UPSTREAM
from .storage import public_text
from .project_evidence import research_profile

RESEARCH_FIELDS = {"item_id", "title", "summary", "changes", "hypotheses", "no_hypothesis_reason", "questions"}
CHANGE_FIELDS = {"mechanism", "before", "after", "evidence_refs"}
HYPOTHESIS_FIELDS = {"hypothesis", "basis", "confidence", "conditions", "alternative", "verify", "creator_impact", "evidence_refs"}


def validate_research(value: Any, pack: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict) or set(value) != RESEARCH_FIELDS:
        return ["整版本研究字段不完整或含额外字段"]
    errors: list[str] = []
    if value["item_id"] != pack["item_id"]:
        errors.append("item_id 不匹配")
    for field in ("title", "summary", "no_hypothesis_reason"):
        if not isinstance(value[field], str) or len(value[field]) > 1200 or (field != "no_hypothesis_reason" and not value[field].strip()):
            errors.append(field + " 必须为有效短文本")
    for field, fields in (("changes", CHANGE_FIELDS), ("hypotheses", HYPOTHESIS_FIELDS)):
        rows = value[field]
        if not isinstance(rows, list) or len(rows) > 6 or (field == "changes" and not rows):
            errors.append(field + " 必须为有限列表，changes 不能为空")
            continue
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields:
                errors.append(field + " 条目结构无效"); continue
            for name in fields - {"evidence_refs", "confidence"}:
                if not isinstance(row[name], str) or not row[name].strip() or len(row[name]) > 1000:
                    errors.append(name + " 必须为非空短文本")
            refs = row["evidence_refs"]
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in pack["evidence"] for r in refs):
                errors.append("研究断言缺少有效证据编号")
            if field == "hypotheses" and row["confidence"] not in ("high", "medium", "low"):
                errors.append("推测置信度无效")
    if not value["hypotheses"] and not value["no_hypothesis_reason"]:
        errors.append("未提出假设时须说明具体证据缺口")
    if not isinstance(value["questions"], list) or len(value["questions"]) > 8 or any(not isinstance(q, str) or not q.strip() or len(q) > 600 for q in value["questions"]):
        errors.append("questions 无效")
    public_text(json.dumps(value, ensure_ascii=False))
    return errors


def analysis_projection(value: dict[str, Any]) -> dict[str, Any]:
    """复用原有数字、代码名与路径校验，推测的每个部分仍绑定证据。"""
    return {
        "item_id": value["item_id"], "title": value["title"], "summary": value["summary"],
        "default_state": "unknown", "facts": [{"text": "；".join(c[k] for k in ("mechanism", "before", "after")), "evidence_refs": c["evidence_refs"]} for c in value["changes"]],
        "inferences": [{"text": "；".join(h[k] for k in ("hypothesis", "basis", "conditions", "alternative", "verify", "creator_impact")), "evidence_refs": h["evidence_refs"]} for h in value["hypotheses"]],
        "affected_fact_ids": [], "manual_change": "unknown", "action": "observe",
        "observe": value["no_hypothesis_reason"], "questions": value["questions"],
    }


def research_event(pack: dict[str, Any], result: dict[str, Any], models: dict[str, str]) -> dict[str, Any]:
    payload = {
        "type": "version_research", "commit": pack["commit"], "item_id": pack["item_id"],
        "profile_hash": research_profile(),
        "state": result["state"], "models": models,
        "review_mode": "same_model" if models["analysis"] == models["review"] else "different_models",
        "coverage": pack["coverage"], "sources": {key: {k: v for k, v in e.items() if k != "text"} for key, e in pack["evidence"].items()},
        **({"analysis": result["analysis"], "review": result["review"]} if "review" in result else {}),
    }
    payload["id"] = "research:" + digest(payload)
    return payload


def valid_research_event(event: dict[str, Any], pack: dict[str, Any]) -> bool:
    models = event.get("models")
    if not isinstance(models, dict) or set(models) != {"analysis", "review"} or any(not isinstance(m, str) or not m.endswith(":free") for m in models.values()):
        return False
    if event.get("state") not in ("pending", "manual", "disputed", "pending_confirmation"):
        return False
    if "analysis" in event or "review" in event:
        review = event.get("review")
        if validate_research(event.get("analysis"), pack) or not isinstance(review, dict) or set(review) != {"item_id", "agree", "issues"} or review["item_id"] != pack["item_id"] or type(review["agree"]) is not bool or not isinstance(review["issues"], list) or review["agree"] == bool(review["issues"]):
            return False
        if event["state"] != ("pending_confirmation" if review["agree"] else "disputed"):
            return False
        for issue in review["issues"]:
            if not isinstance(issue, dict) or set(issue) != {"field", "reason", "evidence_refs"} or issue["field"] not in RESEARCH_FIELDS or not isinstance(issue["reason"], str) or not isinstance(issue["evidence_refs"], list) or not issue["evidence_refs"] or any(not isinstance(r, str) or r not in pack["evidence"] for r in issue["evidence_refs"]):
                return False
    elif event["state"] in ("disputed", "pending_confirmation"):
        return False
    result = {"state": event["state"], **({"analysis": event["analysis"], "review": event["review"]} if "analysis" in event else {})}
    return event == research_event(pack, result, models)


def render_research(event: dict[str, Any], context_base: str = "../data/project-context") -> str:
    if "analysis" not in event:
        return "## 模型项目解读\n\n本版本模型研究尚未完成，当前只有源码证据。\n\n"
    analysis = event["analysis"]
    disputed = not event["review"]["agree"]
    text = "## 模型对本次算法迭代的解读\n\n"
    text += "**模型推测，尚未人工确认" + ("；复核存在分歧" if disputed else "；已完成模型复核") + "。**\n\n"
    text += ("起草和复核为同一型号的两次调用。" if event["review_mode"] == "same_model" else "起草和复核使用不同型号。") + "公开源码不能证明线上部署。\n\n"
    text += "### " + analysis["title"] + "\n\n" + analysis["summary"] + "\n\n"
    text += "### 算法机制可能改在哪里\n\n"
    for row in analysis["changes"]:
        text += "- **" + row["mechanism"] + "**：以前“" + row["before"] + "”；现在“" + row["after"] + "”。（" + "、".join(row["evidence_refs"]) + "）\n"
    text += "\n### 对推荐结果的推测\n\n"
    for row in analysis["hypotheses"]:
        text += "#### " + row["hypothesis"] + "\n\n"
        text += "- 把握：" + {"high": "较高", "medium": "中等", "low": "较低"}[row["confidence"]] + "（针对机制推断，不代表已部署）。\n"
        for field, label in (("basis", "为什么这样推测"), ("conditions", "成立条件"), ("creator_impact", "对创作者意味着什么"), ("alternative", "另一种解释"), ("verify", "如何验证")):
            text += "- " + label + "：" + row[field] + "\n"
        text += "- 依据：" + "、".join(row["evidence_refs"]) + "\n\n"
    if not analysis["hypotheses"]:
        text += analysis["no_hypothesis_reason"] + "\n\n"
    if disputed:
        text += "### 复核分歧\n\n" + "\n".join("- " + issue["reason"] + "（" + "、".join(issue["evidence_refs"]) + "）" for issue in event["review"]["issues"]) + "\n\n"
    text += "### 仍待验证\n\n" + "\n".join("- " + q for q in analysis["questions"]) + "\n\n"
    coverage = event["coverage"]
    text += f"本次上下文覆盖 {coverage['changed_source_files']} 个变化源码/说明文件中的 {len(coverage['diff_files_included'])} 个差异片段，另含架构说明、实现与引用窗口；并非通读整个仓库。\n\n"
    text += "<details><summary>模型引用的证据与上下文范围</summary>\n\n"
    for key, source in event["sources"].items():
        if not source["path"]:
            text += "- " + key + "：检测器记录的旧值、新值及计算关系。\n"
            continue
        anchor = "#L" + str(source["start_line"]) if source.get("start_line") else ""
        text += f"- {key}：{source['kind']} · [{source['path']}]({UPSTREAM}/blob/{source['commit']}/{quote(source['path'], safe='/')}{anchor})" + ("（片段截断）" if source["truncated"] else "") + "\n"
    text += f"\n[模型实际读取的完整证据包]({context_base}/{event['commit']}-{event['item_id']}.json)\n\n</details>\n\n"
    public_text(text)
    return text
