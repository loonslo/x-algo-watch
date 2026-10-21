import re
from typing import Any

from .models import Parameter, Severity


def category(name: str) -> str:
    if name.endswith("ClusterId") or re.search(r"Shadow|Experiment", name):
        return "experiment"
    if re.search(r"Kafka|Logging|^Log|LogSample", name):
        return "logging"
    if "Weight" in name:
        return "weight"
    if "ColdStart" in name:
        return "cold_start"
    if name.startswith("Enable") and name.endswith("Source"):
        return "source"
    if name.startswith("Ads") or "BrandSafety" in name:
        return "ads"
    return "other"


def disabled(value: Any) -> bool:
    return value is False or value == 0 or value == "" or value == []


def closed_gate(name: str, params: dict[str, Parameter]) -> str | None:
    matches: list[tuple[int, str]] = []
    for other, param in params.items():
        if other == name or not other.startswith("Enable") or param.value is not False:
            continue
        prefix = other.removeprefix("Enable")
        prefixes = {prefix, prefix.removesuffix("Source")}
        for candidate in prefixes:
            if len(candidate) >= 4 and name.lower().startswith(candidate.lower()):
                matches.append((len(candidate), other))
    return max(matches)[1] if matches else None


def parameter_level(name: str, before: Parameter | None, after: Parameter | None, params: dict[str, Parameter], facts: list[dict[str, Any]]) -> tuple[Severity, str, tuple[str, ...]]:
    related = [f for f in facts if f["source"].get("name") == name and f["source"]["path"] == "home-mixer/params/param.rs"]
    protected = [f for f in related if f.get("kind", "fact") != "ignore"]
    if protected and (before is not None or after is None or any(f.get("value") != after.value for f in protected)):
        return Severity.RED, "登记项发生变化", tuple(f["id"] for f in protected)
    ignored = [f for f in related if f.get("kind") == "ignore"]
    if ignored:
        return Severity.GRAY, "忽略名单：" + "; ".join(f["note"] for f in ignored), tuple(f["id"] for f in ignored)
    if before is not None and after is not None and before.value is False and after.value is True:
        # 所有见过的默认关布尔参数均进入程序生成的观察表。
        return Severity.RED, "观察开关从默认关改为默认开；部署未知", ()
    if after is None:
        return Severity.YELLOW, "未登记参数删除", ()
    if before is not None:
        if before.rust_type != after.rust_type or before.key != after.key:
            return Severity.ORANGE, "参数类型或配置键变化", ()
        if category(name) in ("experiment", "logging"):
            return Severity.GRAY, "实验配置或日志类默认值变化；不代表已部署", ()
        return Severity.ORANGE, "未登记参数默认值变化；部署未知", ()
    if disabled(after.value):
        return Severity.YELLOW, "新增参数，默认关或零/空值", ()
    if category(name) in ("experiment", "logging"):
        return Severity.YELLOW, "新增实验或日志配置，取值非空；部署未知", ()
    gate = closed_gate(name, params)
    if gate:
        return Severity.YELLOW, f"新增配套参数；按名字匹配到默认关的 {gate}，控制关系待人工核对", ()
    return Severity.ORANGE, "新增参数，默认开或有取值；部署未知", ()
