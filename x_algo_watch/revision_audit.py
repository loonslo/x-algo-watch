"""可复现的逐字段修订差异与问题回退记录；不猜测语义关闭或因果。"""

import argparse
import json
from pathlib import Path
from typing import Any

from .evidence import digest
from .models import WatchError
from .storage import RecordStore, atomic_write, public_text


def issue_identity(issue: dict[str, Any]) -> str:
    if issue.get("problem_key"):
        identity = {k: issue.get(k) for k in ("claim_id", "field", "category", "problem_key")}
    else:
        identity = {"field": issue["field"], "reason": " ".join(issue["reason"].split()),
                    "evidence_refs": sorted(set(issue.get("evidence_refs", [])))}
    return "I-" + digest(identity)[:20]


def field_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(before.keys() | after.keys()):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                rows.append({"field": pointer, "before_exists": key in before, "after_exists": key in after,
                             "before": before.get(key), "after": after.get(key)})
            else:
                rows.extend(field_changes(before[key], after[key], pointer))
        return rows
    if isinstance(before, list) and isinstance(after, list):
        # 新机制结论按稳定 C 编号对齐；普通旧列表只能按原始位置比较。
        if before and after and all(isinstance(item, dict) and "claim_id" in item for item in before + after):
            a = {item["claim_id"]: item for item in before}
            b = {item["claim_id"]: item for item in after}
            if len(a) == len(before) and len(b) == len(after):
                return field_changes(a, b, path)
        rows = []
        for index in range(max(len(before), len(after))):
            if index >= len(before) or index >= len(after):
                rows.append({"field": path + "/" + str(index), "before_exists": index < len(before), "after_exists": index < len(after),
                             "before": before[index] if index < len(before) else None, "after": after[index] if index < len(after) else None})
            else:
                rows.extend(field_changes(before[index], after[index], path + "/" + str(index)))
        return rows
    return [{"field": path or "/", "before_exists": True, "after_exists": True, "before": before, "after": after}]


def audit_revisions(history: list[dict[str, Any]], closure_reasons: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    steps, ever_seen, previous = [], set(), {}
    for index, candidate in enumerate(history):
        result = candidate["result"]
        current = {issue_identity(issue): issue for issue in result["review"]["issues"]}
        changes = field_changes(history[index - 1]["result"]["analysis"], result["analysis"]) if index else []
        removed, introduced = previous.keys() - current.keys(), current.keys() - previous.keys()
        reasons = (closure_reasons or {}).get(str(candidate["number"]), {})
        steps.append({"candidate": candidate["number"], "issue_count": len(result["review"]["issues"]),
                      "changed_fields": changes, "new_issues": sorted(introduced - ever_seen),
                      "continued_issues": sorted(previous.keys() & current.keys()),
                      "regressions": sorted(introduced & ever_seen),
                      "not_repeated": [{"issue_id": identifier, "closure_reason": reasons.get(identifier),
                                        "closure_verified": bool(result["review"]["agree"])} for identifier in sorted(removed)],
                      "issue_count_increased": index > 0 and len(result["review"]["issues"]) > len(history[index - 1]["result"]["review"]["issues"]),
                      "causal_attribution": "undetermined"})
        ever_seen.update(current)
        previous = current
    value = {"schema_version": 1, "steps": steps,
             "limitations": ["旧审核按字段、证据和规范化问题原文匹配，不能识别所有同义回退", "问题数量反弹不证明由改稿或审核差异单独造成", "未再提出不等于已经解决；关闭理由缺失时保留 null，不补造解释"]}
    public_text(json.dumps(value, ensure_ascii=False))
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="只读比较已存候选及裁决，不改写旧审计")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--records", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        events = [e for e in RecordStore(args.records).events() if e.get("type") == "version_research" and e["commit"] == args.commit]
        if not events or "iteration" not in events[-1]:
            raise WatchError("该版本没有可比较的候选历史")
        if not args.output.resolve().is_relative_to(Path.cwd().resolve()) or args.output.is_symlink():
            raise WatchError("修订比较只能写本项目内普通文件")
        result = audit_revisions(events[-1]["iteration"]["history"])
        atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"已比较 {len(result['steps'])} 个候选；未判定改稿与审核差异的因果")
        return 0
    except WatchError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
