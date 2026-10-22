"""面向普通创作者的阅读层；保留证据绑定，改写稿不冒充模型复核通过。"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .evidence import digest
from .git import UPSTREAM, safe_path
from .models import WatchError
from .storage import public_text

READER_FIELDS = {"title", "what_changed", "who", "when", "online_status", "action", "impacts", "terms", "evidence_refs"}


def validate_reader(reader: Any, evidence: dict[str, Any]) -> list[str]:
    if not isinstance(reader, dict) or set(reader) != READER_FIELDS:
        return ["用户阅读层缺少四问、影响或证据字段"]
    errors = []
    if reader["online_status"] != "unverified":
        errors.append("公开源码不能验证线上部署；online_status 必须为 unverified")
    for key in ("title", "what_changed", "who", "when", "action"):
        maximum = {"title": 80, "what_changed": 200, "who": 160, "when": 220, "action": 160}[key]
        if not isinstance(reader[key], str) or not reader[key].strip() or len(reader[key]) > maximum:
            errors.append("用户阅读层 " + key + " 无效")
        elif re.search(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b(?:Enable|Disable)[A-Z][A-Za-z0-9]*|\b(?:Phoenix|VMRanker)\b|水合|kill switch", reader[key]):
            errors.append("用户摘要含技术术语或代码标识，请移到技术分析：" + key)
    if all(isinstance(reader[k], str) for k in ("title", "what_changed", "who", "when", "action")) and sum(len(reader[k]) for k in ("title", "what_changed", "who", "when", "action")) > 420:
        errors.append("用户摘要四问与标题合计不得超过 420 字符")
    for key, fields, maximum in (("impacts", {"heading", "effect", "conditions", "limits", "evidence_refs"}, 4),
                                 ("terms", {"term", "meaning", "evidence_refs"}, 5)):
        rows = reader[key]
        if not isinstance(rows, list) or len(rows) > maximum:
            errors.append("用户阅读层 " + key + " 列表无效"); continue
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields:
                errors.append("用户阅读层 " + key + " 条目无效"); continue
            for name in fields - {"evidence_refs"}:
                if not isinstance(row[name], str) or not row[name].strip() or len(row[name]) > 600:
                    errors.append("用户阅读层 " + name + " 无效")
            if not valid_refs(row["evidence_refs"], evidence):
                errors.append("用户阅读层引用无效")
    if not valid_refs(reader["evidence_refs"], evidence):
        errors.append("用户阅读层摘要引用无效")
    public_text(json.dumps(reader, ensure_ascii=False))
    return errors


def valid_refs(refs: Any, evidence: dict[str, Any]) -> bool:
    return isinstance(refs, list) and bool(refs) and all(isinstance(ref, str) and ref in evidence for ref in refs)


def reader_projection(reader: dict[str, Any], item_id: str) -> dict[str, Any]:
    from .research import analysis_projection

    return analysis_projection({"item_id": item_id, "title": reader["title"], "summary": reader["what_changed"],
                                "changes": [], "hypotheses": [], "no_hypothesis_reason": "", "questions": [], "reader": reader})


def load_edition(root: Path, event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event or "analysis" not in event:
        return None
    path = root / "editorial" / "versions" / (event["commit"] + ".json")
    if not path.exists():
        return None
    if path.is_symlink():
        raise WatchError("用户改写稿不能是符号链接")
    try:
        edition = json.loads(path.read_text(encoding="utf-8"))
        if edition["source_research_id"] != event["id"] or edition["item_id"] != event["item_id"]:
            return None  # 新研究已经替换旧稿；不得把旧改写套到另一份研究上。
        if edition["schema_version"] != 1:
            raise ValueError
        snapshot = root / "data/project-context" / (event["commit"] + "-" + event["item_id"] + ".json")
        if snapshot.is_symlink():
            raise ValueError
        pack = json.loads(snapshot.read_text(encoding="utf-8"))
        if pack["item_id"] != digest({k: v for k, v in pack.items() if k != "item_id"}):
            raise ValueError
        evidence = dict(pack["evidence"])
        for key, source in edition.get("supplementary_evidence", {}).items():
            if key in evidence or not key.startswith("R") or source["commit"] != event["commit"] or source["text_digest"] != digest(source["text"]):
                raise ValueError
            safe_path(source["path"])
            if type(source["start_line"]) is not int or source["start_line"] < 1:
                raise ValueError
            evidence[key] = source
        combined = {**pack, "evidence": evidence}
        from .drafting import validate_analysis
        from .research import analysis_projection, validate_research
        errors = validate_reader(edition["reader"], evidence)
        errors += validate_analysis(reader_projection(edition["reader"], event["item_id"]), combined, project_research=True)
        if "analysis" in edition:
            errors += validate_research(edition["analysis"], combined)
            errors += validate_analysis(analysis_projection(edition["analysis"]), combined, project_research=True)
        if errors:
            raise WatchError("用户改写稿校验失败：" + "；".join(errors[:6]))
        public_text(json.dumps(edition, ensure_ascii=False))
        return edition
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise WatchError("用户改写稿或其证据快照无效") from exc


def evidence_links(refs: list[str], sources: dict[str, Any], context_url: str) -> str:
    links = []
    for key in refs:
        source = sources[key]
        path = source.get("path")
        url = context_url
        if path:
            safe_path(path)
            anchor = "#L" + str(source["start_line"]) if source.get("start_line") else ""
            url = f"{UPSTREAM}/blob/{source['commit']}/{quote(path, safe='/')}{anchor}"
        links.append(f"[{key}]({url})")
    return "、".join(links)
