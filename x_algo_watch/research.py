"""整版本模型研究的结构与阅读呈现；推测必须给出机制、条件及替代解释。"""

import html
import json
from typing import Any

from .evidence import digest
from .storage import public_text
from .project_evidence import research_profile
from .readers import evidence_links, validate_reader

LEGACY_RESEARCH_FIELDS = {"item_id", "title", "summary", "changes", "hypotheses", "no_hypothesis_reason", "questions"}
RESEARCH_FIELDS = LEGACY_RESEARCH_FIELDS | {"reader"}
CHANGE_FIELDS = {"mechanism", "before", "after", "evidence_refs"}
HYPOTHESIS_FIELDS = {"hypothesis", "basis", "confidence", "conditions", "alternative", "verify", "creator_impact", "evidence_refs"}


def research_schema() -> dict[str, Any]:
    """线上起草与复核共用的精确协议；发布版 JSON 由此生成。"""
    text = {"type": "string", "minLength": 1, "maxLength": 600}
    refs = {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^E[0-9]+$"}}
    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "required": list(properties), "additionalProperties": False, "properties": properties}
    reader = obj({**{name: {**text, "maxLength": maximum} for name, maximum in (("title",80),("what_changed",200),("who",160),("when",220),("action",160))},
                  "online_status": {"const": "unverified"},
                  "impacts": {"type": "array", "maxItems": 4, "items": obj({**{name: text for name in ("heading", "effect", "conditions", "limits")}, "evidence_refs": refs})},
                  "terms": {"type": "array", "maxItems": 5, "items": obj({"term": text, "meaning": text, "evidence_refs": refs})}, "evidence_refs": refs})
    reader["description"] = "普通创作者优先；标题与四问合计不超过 420 字符，代码名、Phoenix、VMRanker、水合及 kill switch 移到技术部分。线上状态只允许 unverified。"
    change = obj({**{name: {"type": "string", "minLength": 1, "maxLength": 1000} for name in ("mechanism", "before", "after")}, "evidence_refs": refs})
    hypothesis = obj({**{name: {"type": "string", "minLength": 1, "maxLength": 1000} for name in ("hypothesis", "basis", "conditions", "alternative", "verify", "creator_impact")},
                      "confidence": {"enum": ["high", "medium", "low"]}, "evidence_refs": refs})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "X algorithm version research — reader format v2",
            **obj({"item_id": {"type": "string"}, "title": {"type": "string", "minLength": 1, "maxLength": 1200},
                   "summary": {"type": "string", "minLength": 1, "maxLength": 1200}, "reader": reader,
                   "changes": {"type": "array", "minItems": 1, "maxItems": 6, "items": change},
                   "hypotheses": {"type": "array", "maxItems": 6, "items": hypothesis},
                   "no_hypothesis_reason": {"type": "string", "maxLength": 1200},
                   "questions": {"type": "array", "maxItems": 8, "items": text}})}


def validate_research(value: Any, pack: dict[str, Any], *, require_reader: bool = False) -> list[str]:
    if not isinstance(value, dict) or set(value) not in (LEGACY_RESEARCH_FIELDS, RESEARCH_FIELDS):
        return ["整版本研究字段不完整或含额外字段"]
    errors: list[str] = []
    if "reader" in value:
        errors += validate_reader(value["reader"], pack["evidence"])
    elif require_reader:
        errors.append("新研究缺少面向非技术读者的阅读层")
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
    result = {
        "item_id": value["item_id"], "title": value["title"], "summary": value["summary"],
        "default_state": "unknown", "facts": [{"text": "；".join(c[k] for k in ("mechanism", "before", "after")), "evidence_refs": c["evidence_refs"]} for c in value["changes"]],
        "inferences": [{"text": "；".join(h[k] for k in ("hypothesis", "basis", "conditions", "alternative", "verify", "creator_impact")), "evidence_refs": h["evidence_refs"]} for h in value["hypotheses"]],
        "affected_fact_ids": [], "manual_change": "unknown", "action": "observe",
        "observe": value["no_hypothesis_reason"], "questions": value["questions"],
    }
    if "reader" in value:
        reader = value["reader"]
        result["facts"].append({"text": "；".join(reader[k] for k in ("title", "what_changed", "who", "when", "action")), "evidence_refs": reader["evidence_refs"]})
        result["inferences"].extend({"text": "；".join(row[k] for k in ("heading", "effect", "conditions", "limits")), "evidence_refs": row["evidence_refs"]} for row in reader["impacts"])
        result["facts"].extend({"text": row["term"] + "：" + row["meaning"], "evidence_refs": row["evidence_refs"]} for row in reader["terms"])
    return result


def research_event(pack: dict[str, Any], result: dict[str, Any], models: dict[str, str]) -> dict[str, Any]:
    payload = {
        "type": "version_research", "commit": pack["commit"], "item_id": pack["item_id"],
        "profile_hash": research_profile(),
        "state": result["state"], "models": models,
        "repair_attempted": result.get("repair_attempted", False),
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
        if validate_research(event.get("analysis"), pack, require_reader=True) or not isinstance(review, dict) or set(review) != {"item_id", "agree", "issues"} or review["item_id"] != pack["item_id"] or type(review["agree"]) is not bool or not isinstance(review["issues"], list) or review["agree"] == bool(review["issues"]):
            return False
        if event["state"] != ("pending_confirmation" if review["agree"] else "disputed"):
            return False
        for issue in review["issues"]:
            if not isinstance(issue, dict) or set(issue) != {"field", "reason", "evidence_refs"} or issue["field"] not in RESEARCH_FIELDS or not isinstance(issue["reason"], str) or not isinstance(issue["evidence_refs"], list) or not issue["evidence_refs"] or any(not isinstance(r, str) or r not in pack["evidence"] for r in issue["evidence_refs"]):
                return False
    elif event["state"] in ("disputed", "pending_confirmation"):
        return False
    if type(event.get("repair_attempted")) is not bool:
        return False
    result = {"state": event["state"], "repair_attempted": event["repair_attempted"], **({"analysis": event["analysis"], "review": event["review"]} if "analysis" in event else {})}
    return event == research_event(pack, result, models)


def render_research(event: dict[str, Any], context_base: str = "../data/project-context", edition: dict[str, Any] | None = None) -> str:
    if "analysis" not in event:
        return "## 30 秒摘要\n\n本版本模型研究尚未完成，当前只有源码证据。线上状态与具体影响尚不能判断；现在不据此调整发布策略。\n\n"
    original = event["analysis"]
    analysis = edition.get("analysis", original) if edition else original
    disputed = not event["review"]["agree"]
    sources = {**event["sources"], **(edition.get("supplementary_evidence", {}) if edition else {})}
    context_url = f"{context_base}/{event['commit']}-{event['item_id']}.json"
    refs = lambda values: evidence_links(values, sources, context_url)
    reader = edition["reader"] if edition else (analysis.get("reader") if not disputed else None)
    text = "## 模型对本次算法迭代的解读\n\n"
    text += "普通创作者与运营：先读“30 秒摘要”和“影响解读”。技术分析、证据与历史记录折叠在末尾，可按需展开。\n\n"
    text += "### 30 秒摘要\n\n"
    text += "| 核对项目 | 当前情况 |\n| --- | --- |\n| 源码变化 | 已发现公开源码改动 |\n"
    mechanism = ("原稿复核存在分歧；本页已按证据改写，改写稿待复核与人工确认" if disputed else "原研究已完成同型号模型复核；通俗改写尚未人工确认") if edition else ("模型推测，尚未人工确认；已完成模型复核" if not disputed else "复核存在分歧，用户正文待修订；尚未人工确认")
    text += "| 机制推断 | " + mechanism + " |\n| 线上状态 | 尚未核实部署；默认配置不等于线上状态 |\n\n"
    if reader:
        text += "**" + reader["title"] + "**\n\n"
        for field, label in (("what_changed", "改了什么？"), ("when", "已经上线了吗？何时可能生效？"), ("who", "可能影响谁？"), ("action", "现在该做什么？")):
            text += "**" + label + "** " + reader[field] + "\n\n"
        text += "摘要依据：" + refs(reader["evidence_refs"]) + "。\n\n## 影响解读\n\n"
        for row in reader["impacts"]:
            text += "### " + row["heading"] + "\n\n" + row["effect"] + "\n\n"
            text += "- 何时可能发生：" + row["conditions"] + "\n- 还不能下的结论：" + row["limits"] + "\n- 依据：" + refs(row["evidence_refs"]) + "\n\n"
        if reader["terms"]:
            text += "### 文中用词\n\n"
            for term in reader["terms"]:
                text += "- **" + term["term"] + "**：" + term["meaning"] + "（" + refs(term["evidence_refs"]) + "）\n"
            text += "\n"
    else:
        text += "**改了什么？** 当前研究稿尚未整理成可发布的用户解读。\n\n**已经上线了吗？** 尚未核实。\n\n**影响谁？** 尚不能可靠判断。\n\n**现在该做什么？** 不据此调整发帖频率、题材或互动方式。\n\n"
        if disputed:
            text += "原稿中已被质疑的标题、摘要和影响推断不继续作为用户结论展示，需先完成修订。\n\n"
    text += "<details><summary>技术分析（普通读者可跳过）</summary>\n\n"
    if not disputed or edition:
        text += "### 算法机制与技术依据\n\n"
        for row in analysis["changes"]:
            text += "- **" + row["mechanism"] + "**：以前“" + row["before"] + "”；现在“" + row["after"] + "”。（" + refs(row["evidence_refs"]) + "）\n"
        text += "\n### 技术推断与验证办法\n\n"
        for row in analysis["hypotheses"]:
            text += "#### " + row["hypothesis"] + "\n\n"
            text += "- 机制推断的证据强度：" + {"high": "较高", "medium": "中等", "low": "较低"}[row["confidence"]] + "；此处不评价线上部署或流量影响的确定性。\n"
            for field, label in (("basis", "依据"), ("conditions", "成立条件"), ("creator_impact", "影响解释"), ("alternative", "另一种解释"), ("verify", "如何验证")):
                text += "- " + label + "：" + row[field] + "\n"
            text += "- 源码：" + refs(row["evidence_refs"]) + "\n\n"
        if not analysis["hypotheses"]:
            text += analysis["no_hypothesis_reason"] + "\n\n"
        if analysis["questions"]:
            text += "### 待核对的技术问题\n\n" + "\n".join("- " + q for q in analysis["questions"]) + "\n\n"
    else:
        text += "该稿仍有未解决的复核问题，技术推断正文暂不发布；原始记录保留在下方研究附录。\n\n"
    text += "</details>\n\n<details><summary>研究附录：证据、复核与原始记录</summary>\n\n"
    text += ("起草与复核为同一型号、两个调用角色。" if event["review_mode"] == "same_model" else "起草与复核使用不同型号。")
    if edition:
        text += "本页按用户阅读规则重写并补充源码核对，未新增远程模型复核；没有更改原模型审计或将改写标为人工确认。"
    text += "\n\n"
    if edition and edition.get("resolutions"):
        text += "### 本次落实到正文的修订\n\n" + "\n".join("- " + item for item in edition["resolutions"]) + "\n\n"
    if disputed:
        text += "### 原始复核分歧（针对旧稿）\n\n" + "\n".join("- " + issue["reason"] + "（" + refs(issue["evidence_refs"]) + "）" for issue in event["review"]["issues"]) + "\n\n"
    coverage = event["coverage"]
    text += f"原模型上下文覆盖 {coverage['changed_source_files']} 个变化源码/说明文件中的 {len(coverage['diff_files_included'])} 个差异片段，另含架构说明、实现与引用窗口；并非通读整个仓库。补充核对来源以 R 编号区分，未冒充原模型读过的证据。\n\n"
    for key, source in sources.items():
        label = source.get("path") or "检测证据及计算关系"
        text += "- " + refs([key]) + "：" + label + ("（片段截断）" if source.get("truncated") else "") + "\n"
    text += f"\n[原模型实际读取的完整证据包]({context_url})\n\n"
    if disputed:
        text += "<details><summary>旧研究稿原文（未通过复核，不作为当前结论）</summary>\n\n<pre>" + html.escape(json.dumps(original, ensure_ascii=False, indent=2)) + "</pre>\n</details>\n\n"
    text += "</details>\n\n"
    public_text(text)
    return text
