"""数字和证据由程序整理；模型只能引用有编号的证据。"""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def weight_math(parameters_before: dict[str, Any], parameters_after: dict[str, Any]) -> dict[str, Any]:
    names = ("FavoriteWeight", "NotInterestedWeight")
    if not all(name in parameters_before and name in parameters_after for name in names):
        return {}
    try:
        old_fav, old_negative = (Decimal(str(parameters_before[n])) for n in names)
        new_fav, new_negative = (Decimal(str(parameters_after[n])) for n in names)
        if not old_fav or not new_fav:
            return {}
        return {
            "inputs_before": {name: str(parameters_before[name]) for name in names},
            "inputs_after": {name: str(parameters_after[name]) for name in names},
            "favorite_multiplier": str(new_fav / old_fav),
            "not_interested_multiplier": str(new_negative / old_negative) if old_negative else None,
            "negative_to_favorite_before": str(abs(old_negative / old_fav)),
            "negative_to_favorite_after": str(abs(new_negative / new_fav)),
        }
    except (InvalidOperation, ZeroDivisionError):
        return {}


def numeric_context_for_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("numeric_context"):
        return record["numeric_context"]
    # 早期记录未保存计算上下文；只在同条证据具有两个权重的完整旧/新值时补算。
    weights = [f for f in record["findings"] if f["category"] == "parameter" and f["name"] in ("FavoriteWeight", "NotInterestedWeight")]
    if len(weights) != 2 or len({f["name"] for f in weights}) != 2:
        return {}
    return weight_math({f["name"]: f.get("before") for f in weights}, {f["name"]: f.get("after") for f in weights})


def configuration_rate_percentages(record: dict[str, Any]) -> list[dict[str, str]]:
    """仅换算已知采样率配置，不把任意权重或源码小数解释为比例。"""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for finding in record["findings"]:
        if (finding.get("category") not in {"parameter", "registry_mismatch"}
                or finding.get("path") != "home-mixer/params/param.rs"
                or finding.get("name") != "ShadowTrafficPhoenixClusterRates"
                or not isinstance(finding.get("after"), list)):
            continue
        for side in ("after", "before"):
            entries = finding.get(side)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*):(0(?:\.\d+)?|1(?:\.0+)?)", entry) if isinstance(entry, str) else None
                if match is None or (side, entry) in seen:
                    continue
                seen.add((side, entry))
                rate = Decimal(match[2])
                result.append({"parameter": finding["name"], "version": side, "entry": entry, "configured_rate": str(rate),
                    "configured_percent": format((rate * 100).normalize(), "f"),
                    "meaning": "仅为配置采样率换算，不证明线上启用、请求命中率或曝光比例"})
    return result


def items_for_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    findings = record["findings"]
    relevant = [f for f in findings if f["severity"] in ("红", "橙")]
    weight_changes = [f for f in relevant if f["category"] == "parameter" and "Weight" in f["name"]]
    other = [f for f in relevant if f not in weight_changes]
    groups = ([weight_changes] if weight_changes else []) + [[finding] for finding in other]
    context = numeric_context_for_record(record)
    result: list[dict[str, Any]] = []
    for group in groups:
        evidence = {f"E{i}": finding for i, finding in enumerate(group, 1)}
        pack = {
            "commit": record["commit"], "base": record["base"], "evidence": evidence,
            "derived": context if group is weight_changes else {},
            "related_facts": sorted({name for f in group for name in f.get("fact_ids", [])}),
            "fact_locations": {item["id"]: item.get("used_in", []) for item in record.get("verification", []) if item["id"] in {name for f in group for name in f.get("fact_ids", [])}},
            "limitations": ["公开代码默认值不等于线上值", "部署未知", "配套开关按名字匹配不证明真实控制关系"],
        }
        pack["item_id"] = digest({"commit": record["commit"], "evidence": evidence})
        result.append(pack)
    return result
