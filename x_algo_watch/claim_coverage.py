"""将指定复核表逐项映射到源码变化监测依赖，不复制文稿说法。"""

import fnmatch
import re
from typing import Any

from .evidence import digest
from .git import Repository
from .models import WatchError
from .registry import tree_digest


def claim_rows(review: str) -> list[dict[str, Any]]:
    rows = []
    for section, expected, width in (("2.1", 41, 7), ("2.2", 33, 3)):
        match = re.search(r"^### " + re.escape(section) + r" [^\n]*\n(.*?)(?=^### |\Z)", review, re.M | re.S)
        if not match:
            raise WatchError("源码说法覆盖来源缺少指定章节")
        group = []
        for line in match[1].splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]
            if cells[0] in ("#", "手册位置") or re.fullmatch(r"[-: ]+", cells[0]):
                continue
            if len(cells) != width:
                raise WatchError("源码说法表格结构改变，不能跳过未知行")
            group.append({"section": section, "row": len(group) + 1, "source_digest": digest(cells),
                          "used_in": cells[1 if width == 7 else 0], "basis": cells[4 if width == 7 else 2]})
        if len(group) != expected:
            raise WatchError("源码说法数量不符合 41+33，需复核输入版本")
        rows.extend(group)
    return rows


def resolve_path(name: str, tree: dict[str, str]) -> str:
    aliases = {"README": "README.md", "param.rs": "home-mixer/params/param.rs", "weights.rs": "xai-value-model/weights.rs"}
    name = name.replace("…", "*").lstrip("/")
    if name in aliases:
        name = aliases[name]
    if name in tree or (name.endswith("/") and any(path.startswith(name) for path in tree)):
        return name
    candidates = [path for path in tree if fnmatch.fnmatchcase(path, name) or fnmatch.fnmatchcase(path, "*/" + name)]
    if len(candidates) != 1:
        raise WatchError("源码说法的依赖路径缺失或不唯一")
    return candidates[0]


def build_claim_coverage(repo: Repository, base: str, target: str, review: str, facts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from .migrate import anchor_fragments, ranges

    base = repo.resolve(base); target = repo.resolve(target)
    tree = repo.tree(base)
    existing = {fact["id"]: fact for fact in facts}; added = []; rows = []
    indexed = {}
    for row in claim_rows(review):
        key = (row["section"], row["row"])
        basis = row.pop("basis")
        inherited = None
        if basis == "同上":
            inherited = (row["section"], row["row"] - 1)
        elif re.fullmatch(r"同第\s*1\s*条", basis):
            inherited = (row["section"], 1)
        elif basis == "同多样性":
            # 指定复核表 2.1 第 5 项明确给出多样性实现与参数依赖。
            inherited = ("2.1", 5)
        if inherited:
            if inherited not in indexed:
                raise WatchError("源码说法交叉引用不能解析")
            refs = indexed[inherited]["dependencies"]
            row["inherited_from"] = f"{inherited[0]}:{inherited[1]}"
        else:
            refs = []
            citations = re.findall(r"`([^`]+)`", basis)
            citations.extend(re.findall(r"README:\d+(?:-\d+)?", basis))
            if key == ("2.2", 32):
                citations = ["home-mixer/", "vm-ranker/"]
                row["scope_note"] = "全仓检索型说法：仅监测原检索目录的变化，不能自动证明否定结论，仍需人工复核。"
            if not citations:
                raise WatchError("源码说法缺少可解析依赖")
            for citation in citations:
                match = re.fullmatch(r"(.+?)(?::(\d+(?:-\d+)?(?:[,、]\s*\d+(?:-\d+)?)*))?", citation)
                if not match:
                    raise WatchError("源码说法引用语法未知")
                path = resolve_path(match[1], tree)
                intervals = ranges(match[2]) if match[2] else []
                identifiers = []
                if intervals:
                    for start, end in intervals:
                        reference_id = digest([path, start, end])[:16]
                        identifiers_for_range = sorted(identifier for identifier in existing if identifier.startswith(f"reference.source.{reference_id}."))
                        if not identifiers_for_range:
                            before = repo.read(base, path); after = repo.read(target, path)
                            if before is None or after is None:
                                raise WatchError("源码说法的引用文件缺失")
                            for index, part in enumerate(anchor_fragments(before, after, start, end), 1):
                                identifier = f"reference.source.{reference_id}.{index}"
                                fact = {"id": identifier, "claim": "逐项源码说法的稳定引用锚点", "value": None,
                                        "source": {"path": path, "name": "reference_" + reference_id, **part},
                                        "check": "snippet", "evidence": "A", "used_in": [], "basis_commit": base,
                                        "note": "仅监测引用原文与位置，不能自动证明文稿语义。"}
                                existing[identifier] = fact; added.append(fact); identifiers_for_range.append(identifier)
                        identifiers.extend(identifiers_for_range)
                else:
                    identifier = "dependency.tree." + digest(path)[:16]
                    if identifier not in existing:
                        value = tree_digest(repo, target, path)
                        if value is None:
                            raise WatchError("源码说法的目标文件/目录缺失")
                        fact = {"id": identifier, "claim": "未指定行区间的源码依赖内容摘要", "value": value,
                                "source": {"path": path}, "check": "tree", "evidence": "A", "used_in": [], "basis_commit": target,
                                "note": "原引用只有文件或检索范围；完整 Git 对象集合发生变化即提醒复核，可能包含无关改动，不证明说法成立。"}
                        existing[identifier] = fact; added.append(fact)
                    identifiers.append(identifier)
                refs.append({"path": path, "ranges": [list(pair) for pair in intervals], "fact_ids": identifiers,
                             "mode": "snippet" if intervals else "file_or_directory_change"})
        row.update(id=f"review.{row['section']}.{row['row']:02d}", dependencies=refs, semantic_verified=False)
        rows.append(row); indexed[key] = row
    return added, {"schema_version": 1, "source_digest": digest(review), "base_commit": base, "target_commit": target,
                   "expected_code_claims": 74, "mapped_code_claims": len(rows), "rows": rows,
                   "note": "逐项登记的是变化监测依赖，不复制原说法、不宣称语义核验。只有文件/目录名时采用保守的完整对象摘要；原文的继承引用显式标明。"}
