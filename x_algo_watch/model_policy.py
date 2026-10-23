"""免费路由与有限复核阶段的共享规则。"""

from typing import Any

from .models import WatchError

FREE_ROUTER = "openrouter/free"
MAX_DAILY_CALLS = 1000
PROJECT_TRANSPORT = "json-schema-context-reader-patch-v4"
REVIEW_TRANSPORT = "json-schema-review-low-effort-v3"
REVIEW_STAGES = ("review", "facts_review", "reader_review")


def free_model(model: Any) -> bool:
    return isinstance(model, str) and (model == FREE_ROUTER or model.endswith(":free"))


def review_mode(models: dict[str, str]) -> str:
    if FREE_ROUTER in models.values():
        return "free_router"
    return "same_model" if models["analysis"] == models["review"] else "different_models"


def review_stages(extra_rounds: int) -> tuple[str, ...]:
    if type(extra_rounds) is not int or not 0 <= extra_rounds <= 2:
        raise WatchError("extra_review_rounds 必须为 0 到 2 的整数")
    return REVIEW_STAGES[:extra_rounds + 1]


def aggregate_reviews(item_id: str, reviews: dict[str, Any]) -> dict[str, Any]:
    issues = []
    for review in reviews.values():
        for issue in review["issues"]:
            if issue not in issues:
                issues.append(issue)
    return {"item_id": item_id, "agree": not issues, "issues": issues}
