"""从已存记录计算展示边界；不产生研究结论或改变审核历史。"""

from typing import Any


def publication_status(event: dict[str, Any] | None, *, complete: bool = True) -> dict[str, Any]:
    """最新事件控制状态；旧稿或人工迁移稿不能覆盖最新失败。"""
    if not complete:
        key, label, gap = "check_failed", "🔴 检查未完成", "源码解析或检查尚未完成，变化范围也可能不完整。"
    elif not event:
        key, label, gap = "queued", "排队", "尚未完成机制研究与三个角色的审核。"
    elif event.get("budget_unverified"):
        key, label, gap = "paused", "暂停调用（共享预算尚未核实）", "尚未核实全部工作流的当日调用审计，当前不发起新模型请求；核对恢复后才可续跑。"
    elif event.get("preparation_failure"):
        key, label, gap = "processing", "证据准备失败（等待重试）", "必要 Git 证据或任务快照尚未准备成功，新研究未通过发布门槛。"
    elif event.get("source_context_pending"):
        key, label, gap = "processing", "新证据已准备，待研究", "当前快照还没有对应的完整研究与三个审核角色裁决，旧稿不能替代本轮。"
    else:
        loop = event.get("iteration", {})
        phase = loop.get("phase")
        if phase in {"waiting_manual", "paused_limit", "paused_stagnation"} or event.get("state") == "manual":
            key, label, gap = "paused", "失败暂停", "需要处理输出、接口或修订阻塞并显式恢复，当前不会正常自动续写。"
        elif event.get("state") == "disputed":
            key, label, gap = "disputed", "复核分歧", "审核意见尚未解决，当前稿件不能作为用户结论。"
        elif event.get("state") == "pending_confirmation" and "analysis" in event:
            reviews = event.get("reviews", {})
            passed = event.get("extra_review_rounds") == 2 and all(reviews.get(stage, {}).get("agree") is True and reviews[stage].get("issues") == []
                         and reviews[stage].get("item_id") == event.get("item_id")
                         for stage in ("review", "facts_review", "reader_review"))
            aggregate = event.get("review", {})
            if passed and aggregate.get("agree") is True and aggregate.get("issues") == [] and phase in (None, "ready") and event["analysis"].get("reader"):
                key, label, gap = "approved", "三角色审核通过", "机制推测尚未人工确认；正式 digest 仍需所有者确认，线上部署尚未核实。"
            else:
                key, label, gap = "processing", "处理中", "尚缺当前三角色全部通过的记录或合格用户稿，不能冒充新规则验收。"
        elif event.get("state") == "insufficient_evidence":
            key, label, gap = "insufficient_evidence", "证据不足", "缺少支持机制判断的必要证据。"
        else:
            key, label, gap = "processing", "处理中", "机制研究或审核尚未完成。"
        if phase in {"waiting_service", "waiting_budget", "waiting_time"}:
            key, label = "processing", {"waiting_service": "处理中（等待服务恢复）", "waiting_budget": "处理中（等待预算恢复）", "waiting_time": "处理中（等待下次续跑）"}[phase]
    return {"key": key, "label": label, "publishable": key == "approved", "gap": gap}


def render_unpublished(event: dict[str, Any] | None, *, complete: bool = True) -> str:
    status = publication_status(event, complete=complete)
    text = "## 版本解读状态\n\n**" + status["label"] + "；用户正文尚未通过发布门槛。**\n\n"
    text += "尚缺什么：" + status["gap"] + "\n\n"
    if event and "iteration" in event:
        from .iteration import PHASES, REASONS
        loop = event["iteration"]
        text += "当前阶段：" + PHASES[loop["phase"]][0] + "。原因：" + REASONS[loop["reason"]] + "。\n\n"
    text += "当前材料不足以支持根据本次更新调整策略；这不表示本次变化没有影响。\n\n"
    return text
