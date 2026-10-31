"""从检查证据重建逐版本阅读档案；不调用模型，不改变检测进度。"""

import argparse
import difflib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .evidence import numeric_context_for_record
from .git import SHA, UPSTREAM, safe_path
from .models import WatchError
from .storage import RecordStore, atomic_write, public_text
from .research import render_research
from .readers import load_edition
from .publication import publication_status, render_unpublished

VERSION_PATH = re.compile(r"versions/(?:README\.md|(?:technical/)?\d{4}-\d{2}-\d{2}-[0-9a-f]{40}\.md)\Z")
DIGEST_SECTION = re.compile(r"<!-- xaw-digest:([0-9a-f]{40}) -->\n(.*?)(?=\n<!-- xaw-digest:|\Z)", re.S)
CONTEXT_ONLY = {"sync", "legacy_levels", "golden_corpus", "baseline", "manual_overdue"}
LABELS = {
    "FavoriteWeight": "点赞信号的打分系数", "NotInterestedWeight": "不感兴趣信号的打分系数",
    "ClickWeight": "点击信号的打分系数", "DwellWeight": "停留信号的打分系数",
    "VideoOpenWeight": "视频打开信号的打分系数",
    "ColdStartMaxPostAgeSecs": "初始推荐候选帖子的最长时间范围",
    "ColdStartImpressionThreshold": "初始推荐的曝光判断阈值",
    "ColdStartFollowerCap": "初始推荐的粉丝数判断上限",
    "EnableVideoCarousel": "视频轮播开关",
    "EnableVideoCarouselAuthorDedupe": "视频轮播的作者去重开关",
    "EnableVideoCarouselEmbeddingDedupe": "视频轮播的相似内容去重开关",
    "EnablePopularPostsSource": "热门帖子候选来源开关",
    "EnableFairShareRanking": "FairShare 排序开关",
    "EnableVideoCarouselCandidatesLogging": "视频轮播候选内容的记录开关",
    "RetrievalValueModelWeights": "候选内容打分模型的权重设置",
}


def validate_home_summary_change(before: str, after: str) -> None:
    """自动发布仅允许更新首页生成区，不能顺带发布人工说明改动。"""
    pattern = r"<!-- versions-summary:begin -->.*?<!-- versions-summary:end -->"
    if len(re.findall(pattern, before, re.S)) != 1 or len(re.findall(pattern, after, re.S)) != 1:
        raise WatchError("首页生成区缺失或重复，拒绝自动发布")
    if re.sub(pattern, "", before, flags=re.S) != re.sub(pattern, "", after, flags=re.S):
        raise WatchError("首页含生成区以外改动，拒绝自动发布")
    public_text(after)


def inline(value: Any) -> str:
    text = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_[\]|])", r"\\\1", text)


def value_text(value: Any) -> str:
    if value is None:
        return "不存在／未提供"
    if value is True:
        return "开启（true）"
    if value is False:
        return "关闭（false）"
    text = json.dumps(value, ensure_ascii=False)
    return inline(text if len(text) <= 160 else text[:160] + "…（完整内容见下方证据）")


def filename(record: dict[str, Any]) -> str:
    if not SHA.fullmatch(record.get("commit", "")):
        raise WatchError("版本档案缺少有效提交号")
    try:
        stamp = datetime.fromisoformat(record["committed_at"])
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
    except (KeyError, ValueError, TypeError) as exc:
        raise WatchError("版本档案缺少有效上游提交时间") from exc
    return stamp.astimezone(timezone.utc).date().isoformat() + "-" + record["commit"] + ".md"


def topic(finding: dict[str, Any]) -> str:
    category = finding["category"]
    name = finding["name"]
    if category == "parameter":
        if "Weight" in name:
            return "推荐打分"
        if "ColdStart" in name:
            return "初始推荐"
        if "VideoCarousel" in name:
            return "视频轮播"
        return "推荐设置"
    return {
        "filters": "候选内容筛选", "visibility": "内容可见性", "enforcement": "内容处理规则",
        "derived_count": "规则数量", "readme": "官方说明", "level_format": "代码结构",
        "registry_mismatch": "手册依据核对", "parse_failure": "检查异常",
    }.get(category, "其他源码变化")


def overview(record: dict[str, Any]) -> str:
    if not record["complete"]:
        return "本次检查未完成，暂时无法判断完整变化。"
    if record.get("mode") == "baseline" or any(f["category"] == "baseline" for f in record["findings"]):
        return "建立起始快照，供后续版本比较；不把已有功能说成新增。"
    topics = list(dict.fromkeys(topic(f) for f in record["findings"] if f["category"] not in CONTEXT_ONLY))
    if topics:
        weight_names = {f["name"] for f in record["findings"] if f["category"] == "parameter"}
        if {"FavoriteWeight", "NotInterestedWeight"} <= weight_names:
            context = numeric_context_for_record(record)
            if context and context["favorite_multiplier"] == context["not_interested_multiplier"] and context["negative_to_favorite_before"] == context["negative_to_favorite_after"]:
                return "点赞与不感兴趣的打分系数同幅调整，两者比例不变。" + ("官方说明也有更新。" if "官方说明" in topics else "")
        return "、".join(topics[:4]) + ("等" if len(topics) > 4 else "") + "有变化。"
    return "监测范围内未发现主要功能设置变化；同步信息或测试样例可能有更新。"


def explanation(finding: dict[str, Any]) -> tuple[str, str]:
    category = finding["category"]
    name = finding["name"]
    if category == "parameter":
        label = LABELS.get(name, "推荐设置 " + name)
        before, after = finding.get("before"), finding.get("after")
        change = inline(label) + "：" + value_text(before) + " → " + value_text(after) + "。"
        if before is False and after is True:
            impact = "代码默认从关变开，值得优先核对适用对象；还不能说明平台已经对所有用户启用。"
        elif after is None:
            impact = "旧设置从此处移除；它可能迁移或改由其他逻辑处理，不能直接认定相关功能被取消。"
        elif before is None and (after is False or after == 0 or after == "" or after == []):
            impact = "新增了设置入口，但此处默认关闭或为空；仅凭这一项不能说新功能已开始影响推荐。"
        elif "Weight" in name:
            impact = "可能改变推荐打分中这类信号的作用；不能把系数当作互动次数，或据此推算流量涨跌。"
        elif "ColdStart" in name:
            impact = "可能改变初始推荐阶段的候选范围或判断门槛；是否影响某个账号，还需核对使用位置与线上设置。"
        elif "Experiment" in name or "Logging" in name or "Kafka" in name or "Cluster" in name or "Shadow" in name:
            impact = "这类设置可能用于实验、记录或内部路由；没有证据证明创作者需要调整发帖方式。"
        else:
            impact = "推荐系统的一项设置有调整；仅凭默认值无法判断触达人数会增加还是减少，需进一步核对调用条件。"
        return change, impact
    changes = {
        "filters": "候选内容的筛选文件或模块有增删。",
        "visibility": "内容可见性的规则名单或执行顺序有变化。",
        "enforcement": "公开的内容处理规则文本有变化。",
        "readme": "官方 README 的更新说明有修改。",
        "level_format": "可见性规则的代码写法有调整。",
        "derived_count": "非粉丝推荐级比粉丝级多的规则数：" + value_text(finding.get("before")) + " → " + value_text(finding.get("after")) + "。",
        "registry_mismatch": "已登记的手册依据与当前源码不一致。",
        "parse_failure": "程序未能完整读取或理解本次源码。",
    }
    impacts = {
        "filters": "可能关系到哪些帖子能够进入后续推荐；文件增删本身不能证明筛选变严、变松或已经启用。",
        "visibility": "值得核对帖子在哪些场景会被展示或过滤；名单变化不能直接等同于你的账号被限流。",
        "enforcement": "需要检查具体适用条件与处理动作；只看到文本变化，还不能断定违规边界或处罚力度改变。",
        "readme": "可能是说明补充或维护日期更新；文案变动本身不能证明推荐算法或名单内容改变。",
        "level_format": "代码组织方式变了；若规则内容未变，不能仅因重构判断推荐效果改变。",
        "derived_count": "两个推荐场景的规则数量差发生变化；数量不说明规则的作用大小，也不能换算成曝光损失。",
        "registry_mismatch": "引用这一依据的手册段落值得复查；暂时不要继续把旧数值当作当前结论。",
        "parse_failure": "目前无法可靠解释全部影响；先排查检查失败，不能把未检查写成没有变化。",
    }
    return changes.get(category, "检测到一处源码变化，详见证据。"), impacts.get(category, "作用尚未明确，需人工核对后再作判断。")


def source_link(record: dict[str, Any], finding: dict[str, Any]) -> str:
    path = finding.get("path", "")
    if not path:
        return ""
    safe_path(path)
    removed = finding.get("after") is None and finding.get("before") is not None and SHA.fullmatch(record.get("base") or "")
    commit = record["base"] if removed else record["commit"]
    line = finding.get("line_before" if removed else "line_after")
    suffix = f"#L{line}" if type(line) is int and line > 0 else ""
    view = "tree" if path.endswith("/") else "blob"
    return f"[源码位置]({UPSTREAM}/{view}/{commit}/{quote(path, safe='/')}{suffix})"


def render_page(record: dict[str, Any], decision: dict[str, Any], confirmed: str, rechecks: list[dict[str, Any]], research: dict[str, Any] | None = None, edition: dict[str, Any] | None = None) -> str:
    sha = record["commit"]
    status = {"confirmed": "已人工确认", "rejected": "解读已驳回"}.get(decision.get("outcome"), "未人工确认")
    if not record["complete"]:
        status += "；检查未完成"
    name = filename(record)
    text = f"# {name[:10]} · {sha[:7]} · 版本变化\n\n[返回全部版本](README.md) · [怎么读这份文档](../docs/how-to-read.md) · [项目首页](../README.md)\n\n"
    technical_link = f"technical/{name}"
    if research:
        technical_navigation = f"> 💡 **专业读者导航**：关于本版本的算法架构机制、代码行号与可验证假设，请查看 [本版本技术解读专页]({technical_link})。\n\n"
    else:
        technical_navigation = ""
    metadata = f"- 原速报解读状态：**{status}**\n- 上游提交时间：{inline(record['committed_at'])}（目录按 UTC 日期排序）\n"
    metadata += f"- 检查级别：{inline(record['severity'])}；级别表示复核优先级，不代表流量影响大小。\n"
    metadata += f"- [查看原始提交]({UPSTREAM}/commit/{sha})"
    if record.get("base") and SHA.fullmatch(record["base"]):
        metadata += f" · [对比前后代码]({UPSTREAM}/compare/{record['base']}...{sha})"
    month = datetime.fromisoformat(record["checked_at"]).astimezone(timezone.utc).strftime("%Y-%m")
    metadata += f" · [完整检查记录](../data/records/{month}.md)\n\n"
    display = publication_status(research, complete=record["complete"])
    if (research and not display["publishable"]) or not record["complete"]:
        text += render_unpublished(research, complete=record["complete"])
        text += "### 已核实的变化范围（程序检测）\n\n" + overview(record) + "\n\n"
        text += "这只说明检测范围，不能据此判断机制、用户影响或影响大小。\n\n"
        if confirmed:
            text += "原速报曾获人工确认；这不代表当前研究已经确认，原文见[速报汇总](../digest.md)。\n\n"
        text += technical_navigation
        text += "<details><summary>版本检查信息与源码链接（普通读者可跳过）</summary>\n\n" + metadata + "</details>\n"
        return text
    if research and "analysis" in research and not (confirmed and decision.get("research_id") == research.get("id")):
        text += render_research(research, edition=edition, reader_only=True)
        if confirmed:
            text += "## 已人工确认的详细解读\n\n" + confirmed.strip() + "\n\n"
        text += technical_navigation
        text += "<details><summary>版本检查信息与源码链接（普通读者可跳过）</summary>\n\n" + metadata + "</details>\n"
        return text
    elif research and "analysis" not in research and not (confirmed and decision.get("research_id") == research.get("id")):
        text += render_research(research, edition=edition, reader_only=True)
        text += f"### 本次检测代码变动概况（程序检测事实）\n\n- **代码变动摘要**：{overview(record)}\n- **复核优先级**：{inline(record['severity'])}（表示代码核对优先级，不代表实际流量影响）\n\n"
        if confirmed:
            text += "## 已人工确认的详细解读\n\n" + confirmed.strip() + "\n\n"
        text += technical_navigation
        text += "<details><summary>版本检查信息与源码链接（普通读者可跳过）</summary>\n\n" + metadata + "</details>\n"
        return text
    elif research and confirmed:
        text += confirmed.strip() + "\n\n"
        text += technical_navigation
        text += "<details><summary>版本检查信息与源码链接（普通读者可跳过）</summary>\n\n" + metadata + "</details>\n"
        return text
    elif not research and not record.get("base"):
        text += "## 30 秒摘要\n\n**这是起始基线，没有前一个版本可供比较，不属于待研究的算法迭代。**\n\n"
        text += "<details><summary>版本检查信息与源码链接（普通读者可跳过）</summary>\n\n" + metadata + "</details>\n"
        return text
    elif not research:
        text += render_unpublished(None) + "已核实的变化范围（程序检测）：" + overview(record) + "\n\n"
    text += technical_navigation
    if decision.get("outcome") == "rejected":
        text += "**先前解读已被所有者驳回，不能作为运营建议。以下仅保留源码变化与待核对问题。**\n\n"
    text += "<details><summary>检查历史与原始检测数据（普通读者可跳过）</summary>\n\n" + metadata
    if confirmed and not (research and decision.get("research_id") == research.get("id")):
        text += "## 已人工确认的详细解读\n\n" + confirmed.strip() + "\n\n"
    text += "<details><summary>程序整理的原始变化与阅读提示（不属于模型项目解读）</summary>\n\n## 这次改了什么，可能影响什么\n\n"
    findings = [f for f in record["findings"] if f["category"] not in CONTEXT_ONLY]
    for index, finding in enumerate(findings, 1):
        change, impact = explanation(finding)
        text += f"### {index}. {topic(finding)}\n\n- 变化：{change}\n- 证据阅读提示（程序模板）：{impact}\n"
        if finding.get("path"):
            text += "- " + source_link(record, finding) + "；标识：" + inline(finding["name"]) + "\n"
        if finding["category"] in ("filters", "visibility"):
            before, after = finding.get("before"), finding.get("after")
            if isinstance(before, list) and isinstance(after, list):
                added = [x for x in after if x not in before]
                removed = [x for x in before if x not in after]
                text += "- 新增项：" + ("、".join(inline(x) for x in added) or "无") + "\n"
                text += "- 移除项：" + ("、".join(inline(x) for x in removed) or "无（若名单相同，变化可能仅为顺序）") + "\n"
        if finding["category"] == "readme":
            changed = [line for line in difflib.unified_diff(str(finding.get("before", "")).splitlines(), str(finding.get("after", "")).splitlines(), n=0) if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))]
            text += "\n<details><summary>查看说明文字的具体修改（− 旧，+ 新）</summary>\n\n<pre>" + html.escape("\n".join(changed)) + "</pre>\n</details>\n"
        text += "\n"
    if not findings:
        text += overview(record) + "这只覆盖本项目监测的来源，不代表整套算法完全没变。\n\n"
    context = numeric_context_for_record(record)
    if context:
        text += "### 两项权重放在一起看\n\n"
        text += f"程序精确计算：点赞权重 ×{inline(context['favorite_multiplier'])}，不感兴趣权重 ×{inline(context['not_interested_multiplier'])}；绝对比值 {inline(context['negative_to_favorite_before'])} → {inline(context['negative_to_favorite_after'])}。"
        text += "这表示系数之间的数值关系，不能理解成一个操作抵消多少个点赞。\n\n"
    text += "</details>\n\n## 创作者现在要做什么\n\n"
    if not record["complete"]:
        text += "先等待检查恢复及人工补充证据；目前无法据此调整运营策略。\n\n"
    elif research and "analysis" in research:
        text += "阅读模型推测中的“对创作者意味着什么”“成立条件”和“如何验证”。这些是可检验的机制判断，尚未经过人工确认，不承诺流量效果。\n\n"
    elif confirmed:
        text += "优先阅读上方已确认解读中的“现在要不要改做法”和“待核对”；确认解读也不等于线上部署已获证实。\n\n"
    else:
        text += "先观察，保留自己的内容与数据记录；这份源码档案尚不足以支持改变发帖频率、选题或互动方式。若手册引用了变化中的设置，先核对出处与适用条件。\n\n"
    extras = [f for f in record["findings"] if f["category"] in CONTEXT_ONLY]
    if extras:
        text += "## 同步与检查补充\n\n"
        for category in dict.fromkeys(f["category"] for f in extras):
            grouped = [f for f in extras if f["category"] == category]
            if category == "sync":
                f = grouped[-1]
                text += f"- 源码同步标记：{value_text(f.get('before'))} → {value_text(f.get('after'))}；时间标记本身不证明功能变化。\n"
            elif category == "manual_overdue":
                text += "- 人工资料核对提醒：" + "、".join(inline(f["name"]) for f in grouped) + "；属于登记维护事项，不是本次算法逻辑变化。\n"
            else:
                text += {"baseline": "- 本次建立基线，未把既有配置当作新增。\n", "legacy_levels": "- 此历史版本采用旧规则布局，没有现代级别名单的计数；未把缺少该文件当作规则清零。\n", "golden_corpus": f"- 有 {len(grouped)} 个测试样例文件变化；测试样例不能证明线上行为已改变。\n"}[category]
        text += "\n"
    if rechecks:
        text += "## 后续登记核对\n\n"
        for event in rechecks:
            result = event["result"]
            text += f"- {inline(event['created_at'])}：登记重核级别 {inline(result['severity'])}，{'完整' if result['complete'] else '未完成'}；这是同一源码版本的依据核对，不是一次新的算法迭代。\n"
        text += "\n"
    text += "\n</details>\n"
    return text


def build_versions(root: Path) -> int:
    store = RecordStore(root / "data")
    by_commit = {record["commit"]: record for record in store.records()}
    records = sorted(by_commit.values(), key=lambda r: (datetime.fromisoformat(r["committed_at"]), r["commit"]), reverse=True)
    events = sorted(store.events(), key=lambda e: e.get("created_at", ""))
    decisions = {e["commit"]: e for e in events if e["type"] == "decision"}
    latest_research = {e["commit"]: e for e in events if e["type"] == "version_research"}
    research_by_commit = dict(latest_research)
    from .project_evidence import context_packs
    contexts = {p["commit"]: p for p in context_packs(root / "data")}
    for event in events:
        if event["type"] == "research_context_deferred" and event.get("created_at", "") > latest_research.get(event["commit"], {}).get("created_at", ""):
            research_by_commit[event["commit"]] = {"state": "manual", "preparation_failure": True, "id": event["id"], "commit": event["commit"]}
    budget_states = {e["commit"]: e for e in sorted(events, key=lambda e: e.get("checked_at", ""))
                     if e["type"] == "research_budget_status"}
    for commit, event in budget_states.items():
        if event["state"] == "blocked" and event.get("checked_at", "") > latest_research.get(commit, {}).get("created_at", ""):
            research_by_commit[commit] = {"state": "manual", "budget_unverified": True, "id": event["id"], "commit": commit}
    for commit, pack in contexts.items():
        existing = research_by_commit.get(commit)
        if existing and publication_status(existing)["publishable"] and existing.get("item_id") != pack["item_id"]:
            research_by_commit[commit] = {"state": "pending", "source_context_pending": True, "id": "context:" + pack["item_id"], "commit": commit}
    path = root / "digest.md"
    digest = path.read_text(encoding="utf-8") if path.exists() else ""
    confirmed = dict(DIGEST_SECTION.findall(digest))
    pages: dict[str, str] = {}
    index = "# 版本变化记录\n\n[返回项目首页](../README.md) · [怎么读这些文档](../docs/how-to-read.md) · [已确认速报汇总](../digest.md)\n\n"
    index += "面向普通创作者与运营：先读每版的“30 秒摘要：本次更新重点”和“具体调整与影响解读”，了解改了什么、是否上线、可能影响谁、现在该做什么。技术分析、证据和历史记录折叠在页面末尾。每个上游提交一份独立文件，按提交时间从新到旧排列。\n\n"
    index += "未通过发布门槛的版本展示状态、检测范围与缺口；三个审核角色全部通过后才展示机制推测，仍标注尚未人工确认与线上未知。正式 digest 仍需所有者确认。红／橙／黄／灰表示核对优先级，不能理解成流量影响大小。\n\n"
    index += "| 版本（UTC 日期） | 一句话变化 | 解读状态 | 核对级别 |\n| --- | --- | --- | --- |\n"
    home_rows = []
    for record in records:
        name = filename(record)
        decision = decisions.get(record["commit"], {})
        approved = confirmed.get(record["commit"], "") if decision.get("outcome") == "confirmed" else ""
        if decision.get("outcome") == "confirmed" and not approved:
            raise WatchError("确认决策缺少对应速报，不能生成已确认版本档案")
        rechecks = [e for e in events if e["type"] == "verification" and e.get("commit") == record["commit"]]
        research = research_by_commit.get(record["commit"])
        edition = load_edition(root, research)
        pages[name] = render_page(record, decision, approved, rechecks, research, edition)
        if research:
            technical = f"# {name[:10]} · {record['commit'][:7]} · 技术解读\n\n[返回通俗解读](../{name}) · [全部版本](../README.md) · [阅读说明](../../docs/how-to-read.md)\n\n"
            technical += "本页与通俗页来自同一份研究及复核记录；源码变化与条件性推断不等于线上部署。\n\n"
            technical += render_research(research, context_base="../../data/project-context", edition=edition, technical_only=True, check_complete=record["complete"], data_root=root / "data")
            pages["technical/" + name] = technical
        display = publication_status(research, complete=record["complete"])
        status = display["label"]
        if not record.get("base"):
            status = "起始基线·无前版对比"
        if approved:
            status = "模型解读已人工确认" if research and decision.get("research_id") == research.get("id") else status + "；原速报已人工确认"
        elif decision.get("outcome") == "rejected":
            status += "；原速报解读已驳回"
        if not record["complete"]:
            status += "；检查未完成"
        reader = (research or {}).get("analysis", {}).get("reader") if display["publishable"] else None
        summary = reader["title"] if reader else "用户正文尚未通过；" + overview(record) if record.get("base") else "起始基线快照"
        index += f"| [{name[:10]} · {record['commit'][:7]}]({name}) | {inline(summary)} | {status} | {inline(record['severity'])} |\n"
        # 每次变更都必须有记录：首页逐版列出；正文未通过时只写“解读生成中”，检测范围与流程状态留在档案页。
        if len(home_rows) < 5:
            point = reader["title"] if reader else ("起始基线快照" if not record.get("base") else "通俗解读生成中，暂未发布")
            home_rows.append(f"| {name[:10]} · `{record['commit'][:7]}` | {inline(point)} | [版本档案](versions/{name}) |")
    pages["README.md"] = index + f"\n共 {len(records)} 个版本（含起始基线）。没有新上游提交的检查不会新增版本文件。\n\n目录和基础说明由程序重建；人工解读通过 Issue 审核后保存在速报中，不直接编辑这些生成文件。\n"
    home_path = root / "README.md"
    home_update = None
    if home_path.is_symlink():
        raise WatchError("首页不能是符号链接")
    if home_path.exists():
        home = home_path.read_text(encoding="utf-8")
        start, end = "<!-- versions-summary:begin -->", "<!-- versions-summary:end -->"
        if start in home and end in home:
            before, rest = home.split(start, 1)
            _, after = rest.split(end, 1)
            table = "| 更新日期与版本 | 更新重点 | 阅读入口 |\n| --- | --- | --- |\n" + "\n".join(home_rows)
            updated = before + start + "\n\n" + table + "\n\n" + end + after
            public_text(updated)
            if updated != home:
                home_update = updated
    # 全量验证后才写入，避免扫描失败产生半套公开页面。
    directory = root / "versions"
    if directory.is_symlink():
        raise WatchError("版本档案目录不能是符号链接")
    for name, text in pages.items():
        public_text(text)
        if (directory / name).is_symlink() or (name.startswith("technical/") and (directory / "technical").is_symlink()):
            raise WatchError("版本档案不能是符号链接")
    for name, text in pages.items():
        target = directory / name
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            atomic_write(target, text)
    if home_update is not None:
        atomic_write(home_path, home_update)
    return len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从已存证据重建版本目录与每次迭代的独立说明，不调用模型")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        print(f"已生成 {build_versions(args.root)} 个版本档案及目录")
        return 0
    except WatchError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
