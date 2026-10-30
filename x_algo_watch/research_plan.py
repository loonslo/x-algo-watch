"""机制研究的可重复证据任务；只读 Git 对象，不执行上游代码。"""

import argparse
from itertools import zip_longest
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .evidence import digest
from .git import Repository, SHA, run_git, safe_path
from .models import WatchError
from .storage import RecordStore, atomic_write, public_text

PLAN_VERSION = 1
TEXT_EXTENSIONS = {".rs", ".py", ".yaml", ".yml", ".md", ".proto", ".toml", ".json", ".scala", ".java", ".go", ".txt"}
HUNK = re.compile(r"(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class EvidenceWindowTooLarge(WatchError):
    """需要更窄窗口；不能靠裁剪构造看似完整的任务。"""


def purpose(path: str, text: str) -> dict[str, Any]:
    """用途仅基于可见标记；普通源码路径不能证明生产接入。"""
    parts = {p.lower() for p in PurePosixPath(path).parts}
    markers = []
    for label, expression in (("test", r"#\[cfg\(test\)\]|#\[test\]|\bdef test_"),
                              ("mock", r"\b(?:mock\b|Mock[A-Z]\w*|mock_\w+|mock!)")):
        if re.search(expression, text):
            markers.append(label)
    if parts & {"mock", "mocks", "fixtures", "golden_corpus"}:
        role = "mock_or_fixture"
    elif parts & {"test", "tests"} or PurePosixPath(path).name.startswith("test_"):
        role = "test"
    elif parts & {"example", "examples"}:
        role = "example"
    elif PurePosixPath(path).suffix == ".md":
        role = "documentation"
    else:
        role = "mixed_or_unknown" if markers else "unknown"
    return {"purpose": role, "purpose_markers": markers, "production_reachability": "unverified"}


def mechanism_group(path: str) -> str:
    """用于调度的目录分组，不把名称当作机制语义。"""
    parts = PurePosixPath(path).parts
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def clean_source(text: str) -> tuple[str, int]:
    count = 0
    if "@" in text:
        text, count = re.subn(r"\w[\w.+-]*@[\w.-]+\.[A-Za-z]{2,}", "[已隐去源码联系地址]", text)
    public_text(text.replace("@", "\\u0040"))
    return text, count


def source_chunks(text: str, start: int, limit: int) -> list[tuple[int, str]]:
    """仅在完整行之间分块；超长单行无法放下时显式停止。"""
    chunks = []
    current = ""
    line_start = start
    for number, line in enumerate(text.splitlines(keepends=True), start):
        if len(line) > limit:
            raise EvidenceWindowTooLarge("单行源码超过任务预算，必须进一步限定证据请求")
        if current and len(current) + len(line) > limit:
            chunks.append((line_start, current))
            current, line_start = "", number
        current += line
    if current:
        chunks.append((line_start, current))
    return chunks


def fragment(path: str, revision: str, text: str, start: int, kind: str, scope: str) -> dict[str, Any]:
    cleaned, redacted = clean_source(text)
    return {"path": path, "commit": revision, "start_line": start,
            "end_line": start + len(text.splitlines()) - 1, "kind": kind,
            "scope": scope, "text": cleaned, "source_digest": digest(text),
            "redacted_contacts": redacted, "truncated": False, **purpose(path, text)}


def task(path: str, base: str, commit: str, entries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    namespace = extra.pop("claim_namespace", "N-" + digest({"path": path, "spans": [{k: e[k] for k in ("path", "commit", "start_line", "end_line", "source_digest")} for e in entries]}))
    value = {"schema_version": PLAN_VERSION, "path": path, "base": base, "commit": commit,
             "claim_namespace": namespace,
             "change_scope": "changed_file" if any(e["kind"] in {"before_implementation", "after_implementation"} for e in entries) else "supporting_context",
             "mechanism_group": mechanism_group(path), "evidence": {f"E{i}": row for i, row in enumerate(entries, 1)},
             "limitations": ["目录分组不是机制结论；用途未知不能当作生产实现", "窗口不证明完整调用链；文件不存在不证明平台此前没有相同功能"], **extra}
    value["task_id"] = "T-" + digest(value)
    return value


def changed_file_tasks(repo: Repository, base: str, commit: str, path: str, budget: int) -> tuple[list[dict[str, Any]], list[str]]:
    old, new = repo.read(base, path), repo.read(commit, path)
    before, after = old or "", new or ""
    presence = {"before_exists": old is not None, "after_exists": new is not None}
    delta = repo.diff(base, commit, path)
    if old is None and new is None:
        return [], ["前后对象均不在可读文本树中；可能是符号链接或特殊对象，需要单独核对"]
    # 小文件保留完整前后实现；大文件按真实 Git hunk 准备两侧窗口。
    if len(before) + len(after) <= budget // 2:
        entries = [fragment(path, rev, source, 1, kind, "complete_file")
                   for rev, source, kind in ((base, before, "before_implementation"), (commit, after, "after_implementation")) if source]
        return [task(path, base, commit, entries, presence=presence, diff_digest=digest(delta), context_complete=True)], []
    matches = list(HUNK.finditer(delta))
    if not matches:
        return [], ["没有可解析的文本差异区段；需要单独核对文件类型或元数据变化"]
    tasks = []
    old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
    gaps = ["大文件只准备变更区段及相邻上下文；跨区段定义、完整函数和调用关系需要后续补证"]
    for index, match in enumerate(matches, 1):
        a, ac, b, bc = (int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
        old_start, new_start = max(0, a - 1 - 20), max(0, b - 1 - 20)
        old_window = "".join(old_lines[old_start:min(len(old_lines), a - 1 + ac + 20)])
        new_window = "".join(new_lines[new_start:min(len(new_lines), b - 1 + bc + 20)])
        old_chunks = source_chunks(old_window, old_start + 1, budget // 4)
        new_chunks = source_chunks(new_window, new_start + 1, budget // 4)
        for part, pair in enumerate(zip_longest(old_chunks, new_chunks), 1):
            entries = [fragment(path, rev, chunk[1], chunk[0], kind, "change_window")
                       for rev, chunk, kind in ((base, pair[0], "before_implementation"), (commit, pair[1], "after_implementation")) if chunk]
            tasks.append(task(path, base, commit, entries, presence=presence, context_complete=False,
                              hunk=index, part=part, change_range={"before_start": a, "before_count": ac, "after_start": b, "after_count": bc}, diff_digest=digest(delta)))
    return tasks, gaps


def reference_search(repo: Repository, revision: str, symbols: list[str]) -> str:
    if not symbols:
        return ""
    if any(not re.fullmatch(r"[A-Za-z_]\w*", symbol) for symbol in symbols):
        raise WatchError("引用搜索只接受已提取的定义名称")
    # 通过标准输入传模式，避免大版本在 Windows 超出命令行长度限制。
    # 名称不含正则操作符；单个择一表达式避免 Git 多固定模式的昂贵逐项匹配。
    args = ["grep", "--threads=1", "-n", "-I", "-E", "--no-textconv", "-f", "-"]
    return run_git([*args, revision, "--", "*.rs", "*.py", "*.scala", "*.java", "*.go"], repo.path,
                   data="|".join(symbols).encode("utf-8"), allowed_codes=(0, 1)).decode("utf-8")


def reference_tasks(repo: Repository, base: str, commit: str, path: str, symbols: list[str], budget: int, max_hits: int,
                    search_results: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    tasks, gaps = [], []
    for revision in (base, commit):
        raw = reference_search(repo, revision, symbols) if search_results is None else search_results[revision]
        hits = []
        for line in raw.splitlines():
            fields = line.split(":", 3)
            if len(fields) == 4 and fields[2].isdigit() and fields[1] != path and any(symbol in fields[3] for symbol in symbols):
                hits.append((fields[1], int(fields[2])))
        if len(hits) > max_hits:
            gaps.append(f"{revision} 引用命中 {len(hits)} 处，仅准备前 {max_hits} 处；不能宣称调用关系已闭合")
        if not hits:
            gaps.append(f"{revision} 未找到跨文件引用；不证明没有其他调用或已经接入生产")
        seen = set()
        for target, number in hits[:max_hits]:
            start, end = max(1, number - 12), number + 12
            if (target, start) in seen:
                continue
            seen.add((target, start))
            source = "".join((repo.read(revision, target) or "").splitlines(keepends=True)[start - 1:end])
            entries = [fragment(target, revision, chunk, line, "definition_or_reference", "reference_window")
                       for line, chunk in source_chunks(source, start, budget // 2)]
            for entry in entries:
                tasks.append(task(path, base, commit, [entry], query_symbols=symbols, context_complete=False))
    return tasks, gaps


def combine_tasks(items: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """合并同一文件的窗口，保留每个窗口的元数据及新的本地引用映射。"""
    combined = []
    batch: list[dict[str, Any]] = []
    size = 0

    def flush() -> None:
        if not batch:
            return
        first = batch[0]
        entries, segments = [], []
        for item in batch:
            refs = []
            for entry in item["evidence"].values():
                entries.append(entry)
                refs.append(f"E{len(entries)}")
            segments.append({**{k: v for k, v in item.items() if k not in {"evidence", "limitations"}}, "evidence_refs": refs})
        combined.append(task(first["path"], first["base"], first["commit"], entries,
                             claim_namespace=first["claim_namespace"],
                             context_complete=all(item["context_complete"] for item in batch),
                             presence=first.get("presence"), segments=segments))

    for item in items:
        count = sum(len(e["text"]) for e in item["evidence"].values())
        if batch and size + count > budget:
            flush()
            batch, size = [], 0
        batch.append(item)
        size += count
    flush()
    return combined


def build_plan(repo: Repository, record: dict[str, Any], *, budget: int = 60_000, max_reference_hits: int = 24) -> dict[str, Any]:
    base, commit = record.get("base"), record.get("commit")
    if not record.get("complete") or not SHA.fullmatch(base or "") or not SHA.fullmatch(commit or ""):
        raise WatchError("研究计划需要完整检查及精确前后提交")
    if not 2000 <= budget <= 85_000 or not 0 <= max_reference_hits <= 100:
        raise WatchError("研究任务预算或引用上限无效")
    repo.resolve(base); repo.resolve(commit)
    paths = repo.git("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--no-renames", base, commit, "--").split("\0")
    paths = sorted(p for p in paths if p)
    names_by_path = {}
    for path in paths:
        if PurePosixPath(path).suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            delta = repo.diff(base, commit, path)
        except UnicodeError:
            continue
        symbols = list(dict.fromkeys(re.findall(r"\b(?:struct|class|fn|def)\s+([A-Za-z_]\w*)|param!\(\s*([A-Za-z_]\w*)", delta)))
        names_by_path[path] = [a or b for a, b in symbols]
    all_symbols = list(dict.fromkeys(name for names in names_by_path.values() for name in names[:8]))
    search_results = {revision: reference_search(repo, revision, all_symbols) for revision in (base, commit)} if all_symbols and max_reference_hits else None
    files, tasks = [], {}
    for path in paths:
        safe_path(path)
        row = {"file_id": "F-" + digest(path)[:20], "path": path, "mechanism_group": mechanism_group(path),
               "research_state": "not_researched", "task_ids": [], "gaps": []}
        if PurePosixPath(path).suffix.lower() not in TEXT_EXTENSIONS:
            row.update(disposition="excluded", reason="当前文本研究器不支持此扩展名；仍保留在变化清单中")
            files.append(row)
            continue
        try:
            prepared, gaps = changed_file_tasks(repo, base, commit, path, budget)
            names = names_by_path.get(path, [])
            if len(names) > 8:
                gaps.append(f"变化区段识别 {len(names)} 个定义，仅检索前 8 个；其余定义的引用尚未准备")
            if names and max_reference_hits:
                related, reference_gaps = reference_tasks(repo, base, commit, path, names[:8], budget, max_reference_hits, search_results)
                prepared.extend(related)
                gaps.extend(reference_gaps)
            elif names:
                gaps.append("本次禁用引用检索；调用入口、参数读取及注册位置尚未闭合")
            elif PurePosixPath(path).suffix in {".rs", ".py", ".scala", ".java", ".go"}:
                gaps.append("差异区段未提取到可检索定义；调用入口或参数读取需要定向补证")
            prepared = combine_tasks(prepared, budget)
            row.update(disposition="needs_context" if gaps else "prepared", reason="待机制研究，不能仅以证据已准备标记为已研究", gaps=gaps)
            for item in prepared:
                if sum(len(e["text"]) for e in item["evidence"].values()) > budget:
                    raise WatchError("证据任务超过预算，拒绝截断")
                tasks[item["task_id"]] = item
                row["task_ids"].append(item["task_id"])
        except UnicodeError:
            row.update(disposition="excluded", reason="对象不是可解码的 UTF-8 文本，未读取为源码")
        except EvidenceWindowTooLarge:
            row.update(disposition="needs_context", reason="必要源码包含超过预算的单行，未截断或交付不完整任务", gaps=["需要更窄的语义证据请求，当前文件尚不能完成研究"])
        files.append(row)
    shared = []
    for path in ("README.md", "candidate-pipeline/candidate_pipeline.rs", "candidate-pipeline/hydrator.rs", "candidate-pipeline/filter.rs"):
        entries = []
        for revision in (base, commit):
            source = repo.read(revision, path)
            if source:
                for line, chunk in source_chunks(source, 1, budget // 2):
                    entries.append(fragment(path, revision, chunk, line, "architecture_context", "complete_file" if len(source) <= budget // 2 else "file_part"))
        for entry in entries:
            item = task(path, base, commit, [entry], context_complete=entry["scope"] == "complete_file")
            tasks[item["task_id"]] = item
            shared.append(item["task_id"])
    plan = {"schema_version": PLAN_VERSION, "base": base, "commit": commit,
            "budget_per_task": budget, "max_reference_hits": max_reference_hits,
            "files": files, "tasks": list(tasks.values()), "shared_context_task_ids": shared, "parent_plan_id": None,
            "coverage_complete": len(files) == len(paths),
            "limitations": ["已准备不等于已研究；所有任务完成且缺口已处理后才能综合版本结论", "用途标记不证明生产调用，不能把测试或 mock 条件变成线上建议"]}
    plan["plan_id"] = digest(plan)
    public_text(json.dumps(plan, ensure_ascii=False).replace("@", "\\u0040"))
    return plan


def supplement_plan(repo: Repository, plan: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
    """按审核缺口读取精确版本窗口；每次返回新快照，不更改原计划。"""
    if plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"}):
        raise WatchError("原研究计划摘要无效")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 40:
        raise WatchError("补证请求必须为有限非空列表")
    result = {**plan, "tasks": list(plan["tasks"]), "parent_plan_id": plan["plan_id"]}
    for request in requests:
        if not isinstance(request, dict) or set(request) != {"issue_id", "path", "version", "start_line", "end_line", "reason"}:
            raise WatchError("补证请求字段无效")
        if not isinstance(request["path"], str):
            raise WatchError("补证路径必须为字符串")
        path = safe_path(request["path"])
        start, end = request["start_line"], request["end_line"]
        if request["version"] not in {"before", "after"} or type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start > 2000:
            raise WatchError("补证版本或行号范围无效")
        if not isinstance(request["issue_id"], str) or not request["issue_id"].strip() or not isinstance(request["reason"], str) or not request["reason"].strip():
            raise WatchError("补证缺少问题编号或理由")
        revision = plan["base"] if request["version"] == "before" else plan["commit"]
        source = repo.read(revision, path)
        if source is None or end > len(source.splitlines()):
            raise WatchError("补证窗口不在指定版本源码中")
        text = "".join(source.splitlines(keepends=True)[start - 1:end])
        for line, chunk in source_chunks(text, start, plan["budget_per_task"] // 2):
            entry = fragment(path, revision, chunk, line, "requested_context", "requested_window")
            result["tasks"].append(task(path, plan["base"], plan["commit"], [entry], context_complete=False,
                                         request=request, parent_plan_id=plan["plan_id"]))
    result["plan_id"] = digest({k: v for k, v in result.items() if k != "plan_id"})
    public_text(json.dumps(result, ensure_ascii=False).replace("@", "\\u0040"))
    return result


def save_plan(directory: Path, plan: dict[str, Any]) -> Path:
    if plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"}) or not SHA.fullmatch(plan.get("commit", "")):
        raise WatchError("研究计划摘要或提交无效")
    if directory.is_symlink():
        raise WatchError("计划输出目录不能为符号链接")
    path = directory / (plan["commit"] + "-" + plan["plan_id"] + ".json")
    text = json.dumps(plan, ensure_ascii=False, indent=2).replace("@", "\\u0040") + "\n"
    public_text(text)
    if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") != text):
        raise WatchError("拒绝覆盖不同内容的研究计划快照")
    if not path.exists():
        atomic_write(path, text)
    return path


def save_plan_bundle(directory: Path, plan: dict[str, Any]) -> Path:
    """按任务存储不可变快照；manifest 最后写入，部分落盘不能被当作完成。"""
    if plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"}):
        raise WatchError("研究计划摘要无效")
    if not SHA.fullmatch(plan.get("commit", "")):
        raise WatchError("研究计划提交无效")
    from .context_tasks import validate_graph
    validate_graph(plan)
    target = directory / (plan["commit"] + "-" + plan["plan_id"])
    if directory.is_symlink() or target.is_symlink() or (target / "tasks").is_symlink():
        raise WatchError("计划快照不能使用符号链接")
    files = {}
    task_files = []
    for item in plan["tasks"]:
        if item.get("task_id") != "T-" + digest({k: v for k, v in item.items() if k != "task_id"}):
            raise WatchError("证据任务摘要无效")
        name = "tasks/" + item["task_id"] + ".json"
        files[name] = json.dumps(item, ensure_ascii=False, indent=2).replace("@", "\\u0040") + "\n"
        task_files.append({"task_id": item["task_id"], "path": name, "content_digest": digest(item)})
    manifest = {"bundle_version": 1, "plan": {k: v for k, v in plan.items() if k != "tasks"}, "task_files": task_files}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    # 检查全部既有文件之后再补写；同名不同内容绝不覆盖。
    for name, text in files.items():
        public_text(text)
        path = target / name
        if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") != text):
            raise WatchError("拒绝覆盖不同内容的研究任务快照")
    for name, text in files.items():
        path = target / name
        if not path.exists():
            atomic_write(path, text)
    return target / "manifest.json"


def load_plan_bundle(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.is_symlink() or manifest_path.parent.is_symlink():
        raise WatchError("计划快照不能使用符号链接")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if set(manifest) != {"bundle_version", "plan", "task_files"} or manifest["bundle_version"] != 1:
            raise ValueError
        tasks, seen = [], set()
        for row in manifest["task_files"]:
            if not re.fullmatch(r"T-[0-9a-f]{64}", row["task_id"]) or row["path"] != "tasks/" + row["task_id"] + ".json" or row["task_id"] in seen:
                raise ValueError
            path = manifest_path.parent / row["path"]
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError
            item = json.loads(path.read_text(encoding="utf-8"))
            if row["content_digest"] != digest(item) or item["task_id"] != row["task_id"] or item["task_id"] != "T-" + digest({k: v for k, v in item.items() if k != "task_id"}):
                raise ValueError
            tasks.append(item)
            seen.add(item["task_id"])
        plan = {**manifest["plan"], "tasks": tasks}
        if plan["plan_id"] != digest({k: v for k, v in plan.items() if k != "plan_id"}):
            raise ValueError
        for item in tasks:
            if item["base"] != plan["base"] or item["commit"] != plan["commit"]:
                raise ValueError
        required = {t for row in plan["files"] for t in row["task_ids"]} | set(plan["shared_context_task_ids"])
        # 补证任务通过 parent_plan_id 和 request 绑定，不要求是原始变化文件。
        if not required <= seen:
            raise ValueError
        from .context_tasks import validate_graph
        validate_graph(plan)
        public_text(json.dumps(plan, ensure_ascii=False).replace("@", "\\u0040"))
        return plan
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise WatchError("研究计划快照不完整或摘要无效") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="按已有完整检查记录准备分任务 Git 证据，不调用模型或推进进度")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--records", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=60_000)
    args = parser.parse_args()
    try:
        repo = Repository(args.repo)
        commit = repo.resolve(args.commit)
        record = next((r for r in reversed(RecordStore(args.records).records()) if r["commit"] == commit and r["complete"]), None)
        if not record:
            raise WatchError("找不到该提交的完整检查记录")
        if not args.output.resolve().is_relative_to(Path.cwd().resolve()):
            raise WatchError("计划输出必须位于本项目内")
        if args.output.resolve().is_relative_to(repo.path):
            raise WatchError("计划不能写入上游仓库")
        plan = build_plan(repo, record, budget=args.budget)
        save_plan_bundle(args.output, plan)
        print(f"已准备 {len(plan['files'])} 个变化文件、{len(plan['tasks'])} 个证据任务；尚未模型研究")
        return 0
    except (WatchError, UnicodeError) as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
