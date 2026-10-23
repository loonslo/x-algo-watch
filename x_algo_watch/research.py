"""整版本模型研究的结构与阅读呈现；推测必须给出机制、条件及替代解释。"""

import html
import json
from typing import Any

from .evidence import digest
from .storage import public_text
from .project_evidence import research_profile
from .readers import SUMMARY_LIMITS, evidence_links, validate_reader
from .model_policy import aggregate_reviews, free_model, review_mode, review_stages

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
    reader = obj({**{name: {**text, "maxLength": maximum} for name, maximum in SUMMARY_LIMITS.items()},
                  "online_status": {"const": "unverified"},
                  "impacts": {"type": "array", "maxItems": 4, "items": obj({**{name: dict(text) for name in ("heading", "effect", "conditions", "limits")}, "evidence_refs": refs})},
                  "terms": {"type": "array", "maxItems": 5, "items": obj({"term": text, "meaning": text, "evidence_refs": refs})}, "evidence_refs": refs})
    reader["properties"]["what_changed"]["description"] = "版本更新重点：概括最值得关注的实际机制变化，普通中文，不罗列文件名。"
    impact = reader["properties"]["impacts"]["items"]["properties"]
    impact["heading"]["description"] = "按实际机制命名的具体调整标题；不强套新增、优化或修复分类。"
    impact["effect"]["description"] = "一条完整公告：以前怎样、现在怎样、因而哪些内容或创作者可能受何影响。源码未提供旧行为时明确缺口，不编造对比；纯重构说明尚无行为变化证据。"
    impact["conditions"]["description"] = "这条效果成立所需的默认设置、调用或实验条件。"
    impact["limits"]["description"] = "证据未支持的结论或尚待确认的效果；不把源码变化当作线上部署。"
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


def review_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["item_id", "agree", "issues"], "properties": {
        "item_id": {"type": "string"}, "agree": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["field", "reason", "evidence_refs"], "properties": {
                "field": {"type": "string", "enum": sorted(RESEARCH_FIELDS)}, "reason": {"type": "string", "minLength": 1},
                "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^E[0-9]+$"}}}}}}}


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
        "review_mode": review_mode(models),
        "coverage": pack["coverage"], "sources": {key: {k: v for k, v in e.items() if k != "text"} for key, e in pack["evidence"].items()},
        **({"analysis": result["analysis"], "review": result["review"]} if "review" in result else {}),
        **{key: result[key] for key in ("reviews", "extra_review_rounds", "resolved_models", "iteration") if key in result},
    }
    payload["id"] = "research:" + digest(payload)
    return payload


def valid_research_event(event: dict[str, Any], pack: dict[str, Any], *, required_extra_reviews: int = 0) -> bool:
    models = event.get("models")
    if not isinstance(models, dict) or set(models) != {"analysis", "review"} or any(not free_model(m) for m in models.values()):
        return False
    review_stages(required_extra_reviews)
    rounds = event.get("extra_review_rounds", 0)
    if type(rounds) is not int or not 0 <= rounds <= 2:
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
    if "iteration" in event:
        from .iteration import validate_iteration
        if not validate_iteration(event["iteration"], pack, models, required_extra_reviews):
            return False
        loop = event["iteration"]
        if (loop["phase"] == "ready") != (event["state"] == "pending_confirmation"):
            return False
        if "analysis" in event:
            if not loop["history"] or any(event.get(key) != value for key, value in loop["history"][-1]["result"].items()):
                return False
    if "review" in event:
        if rounds < required_extra_reviews:
            return False
        if rounds:
            from .drafting import validate_review
            reviews = event.get("reviews")
            if not isinstance(reviews, dict) or set(reviews) != set(review_stages(rounds)) or any(validate_review(r, pack, RESEARCH_FIELDS) for r in reviews.values()):
                return False
            if event["review"] != aggregate_reviews(pack["item_id"], reviews):
                return False
        elif "reviews" in event:
            return False
    elif "reviews" in event or rounds:
        return False
    if "resolved_models" in event:
        resolved = event["resolved_models"]
        import re
        if not isinstance(resolved, dict) or set(resolved) - {"analysis", *review_stages(2)} or any(m is not None and (not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9_./:+-]{1,200}", m)) for m in resolved.values()):
            return False
    result = {"state": event["state"], "repair_attempted": event["repair_attempted"], **({"analysis": event["analysis"], "review": event["review"]} if "analysis" in event else {}), **{key: event[key] for key in ("reviews", "extra_review_rounds", "resolved_models", "iteration") if key in event}}
    return event == research_event(pack, result, models)


def render_research(event: dict[str, Any], context_base: str = "../data/project-context", edition: dict[str, Any] | None = None, *, technical_only: bool = False) -> str:
    from .iteration import render_iteration
    progress = render_iteration(event["iteration"]) if "iteration" in event else ""
    if "analysis" not in event:
        if technical_only:
            return progress + "技术稿尚未完成；完整候选和调用审计保留在研究记录中，不发布未完成的技术结论。\n\n"
        return progress + "## 30 秒摘要\n\n本版本模型研究尚未完成，当前只有源码证据。线上状态与具体影响尚不能判断；现在不据此调整发布策略。\n\n"
    original = event["analysis"]
    analysis = edition.get("analysis", original) if edition else original
    disputed = not event["review"]["agree"]
    sources = {**event["sources"], **(edition.get("supplementary_evidence", {}) if edition else {})}
    context_url = f"{context_base}/{event['commit']}-{event['item_id']}.json"
    refs = lambda values: evidence_links(values, sources, context_url)
    reader = edition["reader"] if edition else (analysis.get("reader") if not disputed else None)
    text = progress + "## 算法版本更新公告\n\n非官方源码解读；机制效果属于推测，线上部署尚未核实。\n\n"
    text += "普通创作者与运营：先读“30 秒摘要：本次更新重点”和“具体调整与影响解读”，最后看“现在该怎么做”。技术分析、证据与历史记录折叠在末尾，可按需展开。\n\n"
    text += "### 30 秒摘要：本次更新重点\n\n"
    text += "| 核对项目 | 当前情况 |\n| --- | --- |\n| 源码变化 | 已发现公开源码改动 |\n"
    mechanism = ("原稿复核存在分歧；本页已按证据改写，改写稿待复核与人工确认" if disputed else "原研究已完成同型号模型复核；通俗改写尚未人工确认") if edition else ("模型推测，尚未人工确认；已完成模型复核" if not disputed else "复核存在分歧，用户正文待修订；尚未人工确认")
    text += "| 机制推断 | " + mechanism + " |\n| 线上状态 | 尚未核实部署；默认配置不等于线上状态 |\n\n"
    if reader:
        text += "**" + reader["title"] + "**\n\n"
        for field, label in (("what_changed", "改了什么？"), ("when", "已经上线了吗？何时可能生效？"), ("who", "可能影响谁？")):
            text += "**" + label + "** " + reader[field] + "\n\n"
        text += "摘要依据：" + refs(reader["evidence_refs"]) + "。\n\n## 具体调整与影响解读\n\n"
        for row in reader["impacts"]:
            text += "### " + row["heading"] + "\n\n" + row["effect"] + "\n\n"
            text += "- 何时可能发生：" + row["conditions"] + "\n- 还不能下的结论：" + row["limits"] + "\n- 依据：" + refs(row["evidence_refs"]) + "\n\n"
        text += "## 现在该怎么做\n\n**现在该做什么？** " + reader["action"] + "\n\n"
        if reader["terms"]:
            text += "### 文中用词\n\n"
            for term in reader["terms"]:
                text += "- **" + term["term"] + "**：" + term["meaning"] + "（" + refs(term["evidence_refs"]) + "）\n"
            text += "\n"
    else:
        text += "**改了什么？** 当前研究稿尚未整理成可发布的用户解读。\n\n**已经上线了吗？** 尚未核实。\n\n**影响谁？** 尚不能可靠判断。\n\n**现在该做什么？** 不据此调整发帖频率、题材或互动方式。\n\n"
        if disputed:
            text += "原稿中已被质疑的标题、摘要和影响推断不继续作为用户结论展示，需先完成修订。\n\n"
    if technical_only:
        text = progress
    else:
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
    if not technical_only:
        text += "</details>\n\n"
    text += "<details><summary>研究附录：证据、复核与原始记录</summary>\n\n"
    if "iteration" in event:
        records_base = context_base.removesuffix("project-context") + "records"
        text += "每版完整候选稿、审核裁决和问题清单保存在[追加研究记录](" + records_base + ")的 version_research.iteration 中；这些是审计材料，不能作为已发布结论。\n\n"
    text += {"same_model": "起草与复核为同一型号、两个调用角色。", "different_models": "起草与复核使用不同型号。", "free_router": "起草与各轮复核均通过免费自动路由选择型号；实际型号可能相同，不保证异模型独立复核。"}[event["review_mode"]]
    if event.get("reviews"):
        text += "已完成综合复核、源码事实与遗漏核对、机制推断与读者理解核对。" if event["extra_review_rounds"] == 2 else "已完成综合复核、源码事实与遗漏核对。"
        text += "修订后须重新通过全部要求的复核，不能用多数票覆盖异议。"
    if event.get("resolved_models"):
        text += "\n\n实际响应型号（接口未返回时记为未知）：\n" + "\n".join("- " + {"analysis":"起草", "review":"综合复核", "facts_review":"事实与遗漏核对", "reader_review":"推断与读者理解核对"}[stage] + "：`" + (model or "未知") + "`" for stage, model in event["resolved_models"].items())
    if event.get("reviews"):
        text += "\n\n本稿各轮裁决：\n" + "\n".join("- " + {"review":"综合复核", "facts_review":"事实与遗漏核对", "reader_review":"推断与读者理解核对"}[stage] + "：" + ("通过" if result["agree"] else "有分歧，待修订") for stage, result in event["reviews"].items())
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
