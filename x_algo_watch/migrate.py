"""一次性开发工具：只读指定资料，从 Git 代码建立登记初稿，不复制手册文字。"""

import argparse
import difflib
import re
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

from .classify import category
from .engine import snapshot
from .git import Repository, safe_path
from .models import WatchError
from .storage import atomic_write, public_text


def ranges(text: str) -> list[tuple[int, int]]:
    return [(int(match[1]), int(match[2] or match[1])) for match in re.finditer(r"(\d+)(?:-(\d+))?", text.split("（")[0])]


def migration_rows(report: str) -> list[dict[str, Any]]:
    section = report.split("## 5. 行号迁移表", 1)[1].split("## 6.", 1)[0]
    rows: list[dict[str, Any]] = []; path = ""
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [part.strip() for part in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] in ("文件", "---"):
            continue
        if cells[0] != "同上":
            path = safe_path(cells[0])
        old = ranges(cells[1]); new = old if cells[2] == "不变" else ranges(cells[2])
        if not path or len(old) != len(new):
            raise WatchError("迁移表的路径或区间数量不一致")
        rows.append({"row": len(rows) + 1, "path": path, "ranges": [{"old": list(a), "expected": list(b)} for a, b in zip(old, new, strict=True)]})
    if len(rows) != 29:
        raise WatchError("迁移报告不含预期的 29 行；停止，需复核输入版本")
    return rows


def anchor_fragments(old_text: str, new_text: str, start: int, end: int) -> list[dict[str, Any]]:
    old = old_text.splitlines(); new = new_text.splitlines()
    if not (1 <= start <= end <= len(old)):
        raise WatchError("来源行区间越界")
    blocks = difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks()
    fragments: list[dict[str, Any]] = []; covered = 0
    for block in blocks:
        left = max(start - 1, block.a); right = min(end, block.a + block.size)
        if left >= right:
            continue
        covered += right - left
        lo, hi = left, right
        while True:
            while lo < hi and not old[lo].strip():
                lo += 1
            snippet = "\n".join(old[lo:hi]).strip()
            if snippet and new_text.count(snippet) == 1:
                break
            if lo == block.a and hi == block.a + block.size:
                raise WatchError("原文片段不唯一，不能自动建立行号锚点")
            lo = max(block.a, lo - 2); hi = min(block.a + block.size, hi + 2)
        fragments.append({"snippet": snippet, "start_offset": left - lo, "end_offset": right - 1 - lo})
    if covered != end - start + 1:
        raise WatchError("迁移范围含删除或替换的原文；不能当作纯行号迁移")
    return fragments


def document_references(repo: Repository, base: str, target: str, documents: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tree = repo.tree(base)
    aliases = {"param.rs": "home-mixer/params/param.rs", "weights.rs": "xai-value-model/weights.rs",
               "recsys.proto": "phoenix/python/common/xai-proto/proto/recsys.proto",
               "task_filter.py": "grox/flows/reply_spam/task_filter.py"}
    references: dict[tuple[str, int, int], set[str]] = {}; unresolved: set[str] = set()
    for label, text in documents.items():
        # 提取源码引用，不提取说法或判断理由。
        for match in re.finditer(r"([A-Za-z0-9_./-]+\.(?:rs|py|scala|strato|proto|yaml|bot|df)):(\d+(?:-\d+)?(?:[,、]\s*\d+(?:-\d+)?)*)", text):
            name = match[1].split("...")[-1].lstrip("/")
            candidates = [name] if name in tree else ([aliases[name]] if name in aliases and aliases[name] in tree else [p for p in tree if p.endswith("/" + name) or (name.startswith("_") and p.rsplit("/", 1)[-1].endswith(name))])
            if len(candidates) != 1:
                unresolved.add(name); continue
            for left, right in ranges(match[2]):
                references.setdefault((candidates[0], left, right), set()).add(label)
        for match in re.finditer(r"https://github\.com/xai-org/x-algorithm/blob/[0-9a-f]+/([^\s)#]+)#L(\d+)(?:-L(\d+))?", text):
            name = urllib.parse.unquote(match[1])
            if name not in tree:
                unresolved.add(name); continue
            references.setdefault((name, int(match[2]), int(match[3] or match[2])), set()).add(label)
    facts: list[dict[str, Any]] = []; changed: list[dict[str, Any]] = []
    for (path, start, end), labels in sorted(references.items()):
        old_text = repo.read(base, path); new_text = repo.read(target, path)
        if old_text is None or new_text is None:
            changed.append({"path": path, "old_range": [start, end], "reason": "文件缺失"}); continue
        try:
            parts = ([{"snippet": "// mirrored from config feature-switch defaults; last sync", "start_offset": 0, "end_offset": 0}]
                     if path == "home-mixer/params/param.rs" and start == end == 1 else anchor_fragments(old_text, new_text, start, end))
        except WatchError:
            changed.append({"path": path, "old_range": [start, end], "reason": "片段有替换、歧义或原引用区间越界，不能自动宣称引用仍成立"}); continue
        from .evidence import digest
        reference_id = digest([path, start, end])[:16]
        for index, part in enumerate(parts, 1):
            facts.append({"id": f"reference.source.{reference_id}.{index}", "claim": "已引用源码的稳定原文锚点", "value": None,
                          "source": {"path": path, "name": "reference_" + reference_id, **part}, "check": "snippet", "evidence": "A",
                          "used_in": [], "basis_commit": base, "note": "来源类别：" + "、".join(sorted(labels)) + "；只核验原文与位置，不自动证明文稿中的语义判断。"})
    return facts, {"resolved_reference_ranges": len(references), "registered_fragments": len(facts), "unresolved_paths": sorted(unresolved), "requires_manual_mapping": changed}


def build(repo: Repository, base: str, target: str, review: str, params_doc: str, mechanisms: str, report: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = repo.resolve(base); target = repo.resolve(target)
    current = snapshot(repo, target)
    facts: list[dict[str, Any]] = []
    for name, parameter in current.params.items():
        kind = "switch" if parameter.value is False else "fact"
        # 实验或日志配置作为有明确理由的忽略初稿；不会改写权重等业务事实。
        if category(name) == "experiment" and isinstance(parameter.value, str) and parameter.value:
            kind = "ignore"
        facts.append({
            "id": "param." + name, "kind": kind,
            "claim": f"公开参数 {name} 的代码默认值", "value": parameter.value,
            "source": {"path": "home-mixer/params/param.rs", "name": name},
            "check": "value", "evidence": "A",
            "used_in": (["手册 9.1"] if name in {"FavoriteWeight", "ReplyWeight", "RetweetWeight", "ShareWeight", "ShareViaDmWeight", "ShareViaCopyLinkWeight", "QuoteWeight", "FollowAuthorWeight", "ClickWeight", "OpenLinkWeight", "DwellWeight", "NotInterestedWeight", "BlockAuthorWeight", "MuteAuthorWeight", "ReportWeight", "NotDwelledWeight", "PhotoExpandWeight", "VideoOpenWeight", "PostUnexploredWeight", "ContDwellTimeWeight", "ContClickDwellTimeWeight"}
                        else (["手册 4"] if name in {"ColdStartImpressionThreshold", "ColdStartSlotMin", "ColdStartSlotMax", "ColdStartFollowerCap", "ColdStartMaxPostAgeSecs", "ColdStartBetaAlpha0", "ColdStartBetaBeta0", "ColdStartTsTopK", "ColdStartImpressionScale", "EnableColdStartThompsonSampling"} else [])),
            "basis_commit": target,
            "note": ("按已核实的参数名归为实验或日志配置，仅记录变化；若加入业务依赖需改为 fact。此忽略初稿仍待所有者复核。" if kind == "ignore" else "公开默认值；分组实验值与线上部署未知。"),
        })
    for identifier, path, name, value, subtract in (
        ("count.filters.files", "home-mixer/filters/", "filters.files", len(current.filters), None),
        ("count.filters.modules", "home-mixer/filters/mod.rs", "filters.modules", len(current.modules), None),
        ("count.visibility.home", "visibility-filtering/rules/levels.rs", "TIMELINE_HOME", len(current.levels["TIMELINE_HOME"]), None),
        ("count.visibility.recommendations_extra", "visibility-filtering/rules/levels.rs", "TIMELINE_HOME_RECOMMENDATIONS", len(set(current.levels["TIMELINE_HOME_RECOMMENDATIONS"]) - set(current.levels["TIMELINE_HOME"])), "TIMELINE_HOME"),
    ):
        source = {"path": path, "name": name}
        if subtract:
            source["subtract"] = subtract
        facts.append({"id": identifier, "claim": "公开源码清单 " + name + (" 相对 " + subtract + " 的差集条数" if subtract else " 的条数"), "value": value, "source": source, "check": "count", "evidence": "A", "used_in": ["手册 12.4"], "basis_commit": target, "note": "代码清单计数，不代表线上实际执行的规则数。"})
    facts.append({"id": "limit.phoenix_client_max_candidates", "claim": "Phoenix 请求构造中的候选截断常量", "value": 2800, "source": {"path": "home-mixer/util/phoenix_request.rs", "name": "PHOENIX_CLIENT_MAX_CANDIDATES"}, "check": "value", "evidence": "A", "used_in": ["手册 1.1", "图 7"], "basis_commit": target, "note": "公开请求构造代码；部署未知。"})
    rows = migration_rows(report)
    for row in rows:
        old_text = repo.read(base, row["path"]); new_text = repo.read(target, row["path"])
        if old_text is None or new_text is None:
            raise WatchError("迁移表引用的源码文件缺失")
        for index, interval in enumerate(row["ranges"], 1):
            start, end = interval["old"]
            if row["row"] == 1:
                # 此行本来就包含会变化的同步时间；只登记固定前缀，不谎称日期原文未变。
                parts = [{"snippet": "// mirrored from config feature-switch defaults; last sync", "start_offset": 0, "end_offset": 0}]
                interval["note"] = "日期变化由 sync 信号单独检测；此处只定位固定注释前缀。"
            else:
                parts = anchor_fragments(old_text, new_text, start, end)
            interval["anchors"] = []
            for part_index, part in enumerate(parts, 1):
                identifier = f"reference.migration{row['row']:02d}.{index}.{part_index}"
                source = {"path": row["path"], "name": f"migration{row['row']:02d}", **part}
                facts.append({"id": identifier, "claim": f"源码引用 migration{row['row']:02d} 的稳定原文锚点", "value": None, "source": source, "check": "snippet", "evidence": "A", "used_in": [], "basis_commit": base, "note": "纯源码锚点；跨插入处拆成多个原文片段，逐段保留，不把旧整段冒称原样不变。"})
                interval["anchors"].append(identifier)
    extra, coverage = document_references(repo, base, target, {"源码复核表": review, "参数表": params_doc, "机制细节版": mechanisms})
    facts.extend(extra)
    # 外部来源只登记主题与周期；不复制原说法或把资料日期充作本次人工核验。
    for identifier, title, url in (
        ("manual.revenue", "创作者收益资格的外部资料需按月人工核对", "https://help.x.com/en/using-x/creator-revenue-sharing"),
        ("manual.premium", "Premium 功能的外部资料需按月人工核对", "https://help.x.com/en/using-x/x-premium"),
        ("manual.automation", "自动化规则的外部资料需按月人工核对", "https://help.x.com/en/rules-and-policies/x-automation"),
    ):
        facts.append({"id": identifier, "claim": title, "value": None, "source": {"path": url}, "check": "manual", "evidence": "B", "used_in": [], "basis_commit": target, "manual_review": {"last_checked": None, "interval_days": 30}, "note": "仅登记外部来源入口与周期；尚未人工核对，不声明该链接或资格政策已验证。"})
    # 文稿只用于确认输入版本与保留来源摘要；不将其内容写入公开登记表。
    from .evidence import digest
    manifest = {"schema_version": 1, "base_commit": base, "target_commit": target, "rows": rows,
                "source_digests": {"review": digest(review), "params": digest(params_doc), "mechanisms": digest(mechanisms), "migration_report": digest(report)}, "coverage": coverage,
                "note": "29 行预期区间来自指定复核报告；实际位置由原文锚点独立计算。"}
    return facts, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    for name in ("review", "params", "mechanisms", "migration-report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("registry"))
    args = parser.parse_args(argv)
    try:
        destination = args.output / "facts.yaml"
        if destination.exists() and yaml.safe_load(destination.read_text(encoding="utf-8")) not in (None, []):
            raise WatchError("登记表已有人工内容，拒绝覆盖；请使用新的输出目录审阅")
        facts, manifest = build(Repository(args.repo), "74d49a4", "7f2ca2b", *(path.read_text(encoding="utf-8") for path in (args.review, args.params, args.mechanisms, args.migration_report)))
        for name, payload in (("facts.yaml", facts), ("migrations.yaml", manifest)):
            text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120)
            public_text(text)
            atomic_write(args.output / name, text)
        print(f"已生成 {len(facts)} 个登记项、{len(manifest['rows'])} 行迁移验收样本")
        return 0
    except WatchError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
