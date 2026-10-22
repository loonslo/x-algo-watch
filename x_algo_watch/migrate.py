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


def revenue_topics(chapter: str, target: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从已授权的第 3 章摘录登记核对主题，不公开政策原值与原文。"""
    from .evidence import digest

    if not re.match(r"\s*## 3\. ", chapter) or re.search(r"^## (?!3\. )", chapter, re.M):
        raise WatchError("收益输入只能是已授权的第 3 章摘录")
    sections = {}
    for number in (1, 2):
        match = re.search(rf"^### 3\.{number} [^\n]*\n(.*?)(?=^### |\Z)", chapter, re.M | re.S)
        if not match:
            raise WatchError("收益来源缺少 3.1 或 3.2，需复核输入版本")
        sections[number] = match[1]
    rows = []
    for line in sections[1].splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 3 and cells[0] not in ("条件", "---"):
            rows.append(cells)
    items = re.findall(r"^(\d+)\. (.+)$", sections[2], re.M)
    if [row[0] for row in rows] != ["订阅", "粉丝", "展示", "地区与年龄"] or [n for n, _ in items] != ["1", "2", "3"]:
        raise WatchError("收益来源不符合四项准入/三项统计口径分组，不能假定七条覆盖")
    urls = list(dict.fromkeys(re.findall(r"https://[^\s)]+", sections[1])))
    official = "https://help.x.com/en/using-x/original-content-rewards"
    if official not in urls:
        raise WatchError("收益来源缺少预期官方入口，需复核输入版本")
    groups = (
        ("subscription", "收益计划的订阅条件", "3.1", "table_row_1", rows[0]),
        ("followers", "收益计划的粉丝资格口径", "3.1", "table_row_2", rows[1]),
        ("impressions", "收益计划的展示门槛及统计周期", "3.1", "table_row_3", rows[2]),
        ("region_age", "收益计划的地区及年龄条件", "3.1", "table_row_4", rows[3]),
        ("post_scope", "合格展示所涵盖的帖子类型", "3.2", "list_item_1", items[0][1]),
        ("surface_scope", "合格展示所涵盖的流量入口", "3.2", "list_item_2", items[1][1]),
        ("viewer_counting", "合格展示的观众资格与重复统计口径", "3.2", "list_item_3", items[2][1]),
    )
    facts = []; provenance = []
    for key, title, section, locator, original in groups:
        identifier = "manual.revenue." + key
        facts.append({"id": identifier, "claim": "待人工核对的非源码事项：" + title, "value": None,
                      "source": {"path": official, "name": key}, "external_sources": urls,
                      "check": "manual", "evidence": "D", "used_in": ["手册 " + section], "basis_commit": target,
                      "manual_review": {"last_checked": None, "interval_days": 30},
                      "note": "按授权只读章节的四项准入条件与三项统计口径登记，不复述政策数值。来源文稿说明官方页未读到原文；各外部入口和规则均未核验，D 只表示本登记待核对。"})
        provenance.append({"id": identifier, "source_section": section, "locator": locator, "source_digest": digest(original)})
    return facts, {"chapter_digest": digest(chapter), "grouping": "3.1 四项准入条件 + 3.2 三项展示统计口径；原复核表未提供逐条编号，不声称还原其编号。",
                   "items": provenance, "external_content_verified": False}


def manual_topics(review: str, target: str, revenue_chapter: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """登记明确主题；额外授权的章节缺席时保留七条收益缺项。"""
    match = re.search(r"### 2\.3 源码不可核（(\d+) 条[^\n]*\n(.*?)(?=\n## |\Z)", review, re.S)
    if not match or int(match[1]) != 15 or not re.search(r"共\s*7\s*条", match[2]):
        raise WatchError("人工类来源不含预期的 15 项/7 项分组，需核对输入版本")
    section = re.sub(r"\s+", "", match[2])
    topics = (
        ("reposting", "删帖重发", "内容重新发布相关规则", []),
        ("author_replies", "作者回复", "作者回复与会话排序的关系", []),
        ("search_entry", "搜索入口", "搜索入口相关判断", []),
        ("premium_reply_priority", "Premium", "订阅权益与回复优先相关规则", []),
        ("community_notes_revenue", "社区笔记与收益", "社区笔记与收益的关系", []),
        ("scheduled_posts", "定时发布", "计划发布相关规则", []),
        ("giveaways_brands", "抽奖与品牌合作", "抽奖及品牌合作相关规则", ["手册 8.7"]),
        ("ai_disclosure", "AI生成内容", "生成内容披露相关规则", []),
    )
    facts = []
    for key, required, title, used_in in topics:
        if required not in section:
            raise WatchError("人工类来源主题缺失，不能假定覆盖完成")
        facts.append({"id": "manual.policy." + key, "claim": "待人工核对的非源码事项：" + title, "value": None,
                      "source": {"path": "registry/manual-coverage.yaml", "name": key}, "check": "manual", "evidence": "D",
                      "used_in": used_in, "basis_commit": target, "manual_review": {"last_checked": None, "interval_days": 30},
                      "note": "仅按指定复核表 2.3 登记核对主题，不复制文稿原说法。具体外部依据与原证据级别尚待补齐；D 只表示本条未核验，不对政策作肯定判断。"})
    from .evidence import digest
    coverage = {
        "schema_version": 1, "source_category": "源码复核表", "source_section": "2.3", "source_digest": digest(review),
        "expected_noncode_items": 15, "registered_noncode_topics": [fact["id"] for fact in facts],
        "unresolved_items": [{"id": f"pending.revenue.{i:02d}", "used_in": ["手册 3"], "status": "missing_item_detail"} for i in range(1, 8)],
        "existing_source_entry_ids": ["manual.revenue", "manual.premium", "manual.automation"],
        "note": "三个外部来源入口不计作 15 条说法的逐项登记。指定资料对收益部分只给出 7 条总数，缺少逐条明细，当前不可宣称覆盖完成；外部内容均未核验。",
    }
    if revenue_chapter is not None:
        revenue, provenance = revenue_topics(revenue_chapter, target)
        facts.extend(revenue)
        coverage.update(registered_noncode_topics=[fact["id"] for fact in facts], unresolved_items=[], revenue_provenance=provenance,
                        note="15 个核对主题均已登记，另保留三个来源入口。收益七项按另行授权章节的四项准入/三项统计口径拆分；登记覆盖不等于外部规则或文稿内容已验证。")
    return facts, coverage


def build(repo: Repository, base: str, target: str, review: str, params_doc: str, mechanisms: str, report: str, revenue_chapter: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    manual_facts, manual_coverage = manual_topics(review, target, revenue_chapter)
    facts.extend(manual_facts)
    from .claim_coverage import build_claim_coverage
    dependencies, code_coverage = build_claim_coverage(repo, base, target, review, facts)
    facts.extend(dependencies)
    manual_coverage["code_claim_item_mapping"] = "registry/code-claim-coverage.yaml"
    # 文稿只用于确认输入版本与保留来源摘要；不将其内容写入公开登记表。
    from .evidence import digest
    manifest = {"schema_version": 1, "base_commit": base, "target_commit": target, "rows": rows,
                "source_digests": {"review": digest(review), "params": digest(params_doc), "mechanisms": digest(mechanisms), "migration_report": digest(report)}, "coverage": coverage,
                "manual_coverage": manual_coverage,
                "code_claim_coverage": code_coverage,
                "note": "29 行预期区间来自指定复核报告；实际位置由原文锚点独立计算。"}
    return facts, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    for name in ("review", "params", "mechanisms", "migration-report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("registry"))
    parser.add_argument("--manual-chapter3", type=Path, help="另行授权后准备的第 3 章单独摘录；不传入整本手册")
    args = parser.parse_args(argv)
    try:
        destination = args.output / "facts.yaml"
        if destination.exists() and yaml.safe_load(destination.read_text(encoding="utf-8")) not in (None, []):
            raise WatchError("登记表已有人工内容，拒绝覆盖；请使用新的输出目录审阅")
        facts, manifest = build(Repository(args.repo), "74d49a4", "7f2ca2b", *(path.read_text(encoding="utf-8") for path in (args.review, args.params, args.mechanisms, args.migration_report)),
                                revenue_chapter=args.manual_chapter3.read_text(encoding="utf-8") if args.manual_chapter3 else None)
        for name, payload in (("facts.yaml", facts), ("migrations.yaml", manifest), ("manual-coverage.yaml", manifest["manual_coverage"]),
                              ("code-claim-coverage.yaml", manifest["code_claim_coverage"])):
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
