"""人工登记表只读；核验结果写到独立状态文件。"""

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from .git import Repository, safe_path
from .models import Finding, Severity, Snapshot, WatchError
from .parsers import literal, uncomment
from .storage import atomic_write, public_text


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise WatchError("YAML 键必须是唯一的字符串")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def refresh_manual(output: Path, facts: list[dict[str, Any]], today: date) -> None:
    path = output / "verification.yaml"
    if not path.exists():
        return
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    indexed = {item["id"]: item for item in payload.get("items", [])}
    for fact in facts:
        if fact["check"] == "manual" and fact["id"] in indexed:
            review = fact["manual_review"]
            if review["last_checked"] is None:
                indexed[fact["id"]].update(status="unreviewed", due=today.isoformat())
                continue
            due = date.fromisoformat(str(review["last_checked"])) + timedelta(days=review.get("interval_days", 30))
            indexed[fact["id"]].update(status="overdue" if today >= due else "manual", due=due.isoformat())
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    public_text(text)
    if path.read_text(encoding="utf-8") != text:
        atomic_write(path, text)


def load_registry(path: Path) -> list[dict[str, Any]]:
    try:
        facts = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise WatchError("事实登记表不可读或 YAML 无效") from exc
    if not isinstance(facts, list):
        raise WatchError("事实登记表必须是列表（空登记表使用 []）")
    ids: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str) or fact["id"] in ids:
            raise WatchError("登记项缺少唯一 id")
        ids.add(fact["id"])
        kind = fact.get("kind", "fact")
        check = fact.get("check")
        source = fact.get("source")
        if kind not in ("fact", "switch", "ignore") or check not in ("value", "count", "snippet", "manual"):
            raise WatchError("登记项 kind / check 不支持")
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise WatchError("登记项缺少来源路径")
        if check != "manual":
            safe_path(source["path"])
            if check in ("value", "count") and not isinstance(source.get("name"), str):
                raise WatchError("取值或计数项缺少名字")
            if check in ("value", "count") and "value" not in fact:
                raise WatchError("取值或计数项缺少登记值")
            if check == "snippet" and not str(source.get("snippet", "")).strip():
                raise WatchError("原文比对项缺少非空片段")
        if kind == "ignore" and not str(fact.get("note", "")).strip():
            raise WatchError("忽略项必须写明理由 note")
        if check == "manual":
            review = fact.get("manual_review", {})
            try:
                if review["last_checked"] is not None:
                    date.fromisoformat(str(review["last_checked"]))
                days = review.get("interval_days", 30)
                if type(days) is not int or days < 1:
                    raise ValueError
            except (KeyError, ValueError, TypeError) as exc:
                raise WatchError("人工核对项需要 last_checked 和正整数 interval_days") from exc
    return facts


def verify(repo: Repository, snapshot: Snapshot, facts: list[dict[str, Any]], today: date) -> tuple[list[dict[str, Any]], list[Finding]]:
    results: list[dict[str, Any]] = []; findings: list[Finding] = []
    for fact in facts:
        source = fact["source"]; path = source["path"]; name = source.get("name", fact["id"])
        result: dict[str, Any] = {"id": fact["id"], "commit": snapshot.commit, "check": fact["check"], "path": path, "used_in": fact.get("used_in", [])}
        if fact.get("kind") == "ignore":
            result.update(status="ignored", note=fact["note"]); results.append(result); continue
        basis = fact.get("basis_commit")
        if basis and not repo.is_ancestor(repo.resolve(str(basis)), snapshot.commit):
            result["status"] = "not_applicable"; results.append(result); continue
        check = fact["check"]
        if check == "manual":
            review = fact["manual_review"]
            if review["last_checked"] is None:
                result.update(status="unreviewed", due=today.isoformat())
                findings.append(Finding("manual_overdue", Severity.YELLOW, path, name, "外部来源尚未人工核对", fact_ids=(fact["id"],)))
                results.append(result)
                continue
            due = date.fromisoformat(str(review["last_checked"])) + timedelta(days=review.get("interval_days", 30))
            result.update(status="overdue" if today >= due else "manual", due=due.isoformat())
            if today >= due:
                findings.append(Finding("manual_overdue", Severity.YELLOW, path, name, "人工核对已到期；尚未核对内容", fact_ids=(fact["id"],)))
        else:
            text = repo.read(snapshot.commit, path)
            actual: Any = None; line: int | None = None; found = False
            if check == "value" and name in snapshot.params and path == "home-mixer/params/param.rs":
                parameter = snapshot.params[name]; actual = parameter.value; line = parameter.line; found = True
            elif check == "value" and text is not None:
                match = re.search(r"\bconst\s+" + re.escape(name) + r"\s*:\s*[^=]+?=\s*([^;]+);", uncomment(text))
                if match:
                    actual = literal(match[1]); found = True; line = text.count("\n", 0, match.start()) + 1
            elif check == "count":
                lists = {"filters.files": snapshot.filters, "filters.modules": snapshot.modules, **(snapshot.levels or {})}
                expected_path = {"filters.files": "home-mixer/filters/", "filters.modules": "home-mixer/filters/mod.rs"}.get(name, "visibility-filtering/rules/levels.rs")
                if name in lists and path == expected_path:
                    subtract = source.get("subtract")
                    if not subtract or subtract in lists:
                        actual = len(set(lists[name]) - set(lists[subtract])) if subtract else len(lists[name])
                        found = True
            elif check == "snippet":
                snippet = source["snippet"].strip()
                candidates = [path] if text is not None and snippet in text else repo.snippet_candidates(snapshot.commit, snippet)
                locations: list[tuple[str, int]] = []
                for candidate in candidates:
                    try:
                        content = text if candidate == path else repo.read(snapshot.commit, candidate)
                    except UnicodeDecodeError:
                        continue
                    if content is not None:
                        for match in re.finditer(re.escape(snippet), content):
                            locations.append((candidate, content.count("\n", 0, match.start()) + 1))
                if len(locations) == 1:
                    result["path"], line = locations[0]
                    found = True
            okay = found and (check == "snippet" or (type(actual) is type(fact["value"]) and actual == fact["value"]) or (type(actual) in (int, float) and type(fact["value"]) in (int, float) and actual == fact["value"]))
            result.update(status="pass" if okay else "mismatch", actual=actual, line=line)
            if check == "snippet" and line is not None:
                result["reference_start"] = line + source.get("start_offset", 0)
                result["reference_end"] = line + source.get("end_offset", 0)
            if not okay:
                findings.append(Finding("registry_mismatch", Severity.RED, path, name, "登记的值、计数或原文不再成立", fact.get("value"), actual, line_after=line, fact_ids=(fact["id"],)))
        results.append(result)
    return results, findings
