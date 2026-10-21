from datetime import date
from typing import Any

from .classify import category, disabled, parameter_level
from .git import Repository
from .models import CheckResult, Finding, ParseError, Severity, Snapshot, WatchError
from .parsers import FILTER_PATH, LEVEL_PATH, PARAM_PATH, guard_count, parse_levels, parse_modules, parse_params, parse_updates
from .registry import verify
from .evidence import digest, weight_math

# 已核实的 levels.rs 引入边界；只豁免它之前的真实祖先，不豁免现代版本丢文件。
LEVELS_INTRODUCED = "e1a0375a903899984545c663bb0bccb3cdcdf046"


def required(repo: Repository, commit: str, path: str) -> str:
    text = repo.read(commit, path)
    if text is None:
        raise ParseError(f"{path}:1 文件缺失")
    return text


def snapshot(repo: Repository, commit: str) -> Snapshot:
    tree = repo.tree(commit)
    params, sync = parse_params(required(repo, commit, PARAM_PATH))
    modules = parse_modules(required(repo, commit, FILTER_PATH))
    filters = tuple(sorted(p for p in tree if p.startswith("home-mixer/filters/") and p.endswith(".rs") and p != FILTER_PATH and "/" not in p.removeprefix("home-mixer/filters/")))
    guard_count(0, len(filters), "home-mixer/filters/")
    missing = set(modules) - {p.rsplit("/", 1)[-1].removesuffix(".rs") for p in filters}
    if missing:
        raise ParseError(f"{FILTER_PATH}:1 声明的模块文件缺失：{', '.join(sorted(missing))}")
    levels_text = repo.read(commit, LEVEL_PATH)
    if levels_text is not None:
        levels, style = parse_levels(levels_text)
    else:
        try:
            historical = commit != LEVELS_INTRODUCED and repo.is_ancestor(commit, LEVELS_INTRODUCED)
        except WatchError:
            historical = False
        if not historical:
            raise ParseError(f"{LEVEL_PATH}:1 文件缺失，且不属于已核实的引入前历史")
        levels, style = None, "legacy-inline"
    return Snapshot(commit, repo.committed_at(commit), tree, params, sync, filters, modules, levels, parse_updates(required(repo, commit, "README.md")), style)


def compare(repo: Repository, base: str | None, target: str, facts: list[dict[str, Any]], today: date, *, rewrite: bool = False) -> CheckResult:
    result = CheckResult(base, target, repo.committed_at(target), mode="rewrite" if rewrite else ("baseline" if base is None else "compare"))
    result.registry_hash = digest(facts)
    if rewrite:
        result.findings.append(Finding("history_rewrite", Severity.RED, "", "main", "上次处理的提交已不在 main 历史；对新 main 全量核验"))
    try:
        new = snapshot(repo, target)
        old = snapshot(repo, base) if base and not rewrite else None
        if old:
            active_facts = [fact for fact in facts if not fact.get("basis_commit") or repo.is_ancestor(repo.resolve(str(fact["basis_commit"])), target)]
            result.numeric_context = weight_math(
                {name: param.value for name, param in old.params.items()},
                {name: param.value for name, param in new.params.items()},
            )
            guard_count(len(old.params), len(new.params), PARAM_PATH)
            guard_count(len(old.filters), len(new.filters), "home-mixer/filters/")
            guard_count(len(old.modules), len(new.modules), FILTER_PATH)
            if old.levels is not None and new.levels is None:
                raise ParseError(f"{LEVEL_PATH}:1 已存在的文件缺失")
            if old.levels and new.levels:
                if set(old.levels) - set(new.levels):
                    raise ParseError(f"{LEVEL_PATH}:1 已解析的级别丢失")
                for level in old.levels.keys() & new.levels.keys():
                    guard_count(len(old.levels[level]), len(new.levels[level]), LEVEL_PATH + " " + level)
            result.counts = repo.stats(base, target)
            for name in sorted(old.params.keys() | new.params.keys()):
                before = old.params.get(name); after = new.params.get(name)
                if before and after and (before.value, before.key, before.rust_type) == (after.value, after.key, after.rust_type):
                    continue
                severity, message, fact_ids = parameter_level(name, before, after, new.params, active_facts)
                result.findings.append(Finding("parameter", severity, PARAM_PATH, name, message,
                    before.value if before else None, after.value if after else None,
                    before.line if before else None, after.line if after else None,
                    evidence="旧：" + (before.raw if before else "不存在") + "\n新：" + (after.raw if after else "不存在"), fact_ids=fact_ids))
            if old.sync != new.sync:
                result.findings.append(Finding("sync", Severity.GRAY, PARAM_PATH, "last_sync", "默认值同步时间变化", old.sync, new.sync, 1, 1))
            for name, before, after in (("files", old.filters, new.filters), ("modules", old.modules, new.modules)):
                if set(before) != set(after):
                    result.findings.append(Finding("filters", Severity.ORANGE, "home-mixer/filters/", name, "过滤器文件或编译模块增删", sorted(set(before) - set(after)), sorted(set(after) - set(before))))
            if old.level_format != new.level_format:
                result.findings.append(Finding("level_format", Severity.RED, LEVEL_PATH, "format", "可见性级别文件引入或写法变化；已按支持的语法核验", old.level_format, new.level_format))
            for level in sorted((old.levels or {}).keys() | (new.levels or {}).keys()):
                before = (old.levels or {}).get(level, ()); after = (new.levels or {}).get(level, ())
                if before != after:
                    result.findings.append(Finding("visibility", Severity.RED, LEVEL_PATH, level, "可见性规则名单或顺序变化", list(before), list(after), evidence="删除：" + ", ".join(sorted(set(before) - set(after))) + "\n新增：" + ", ".join(sorted(set(after) - set(before)))))
            if old.updates != new.updates:
                result.findings.append(Finding("readme", Severity.ORANGE, "README.md", "Notable Updates", "官方更新小节变化", old.updates, new.updates, evidence=new.updates))
            for path in sorted(old.tree.keys() | new.tree.keys()):
                if old.tree.get(path) == new.tree.get(path):
                    continue
                if path.startswith("botmaker-rules/") and path.endswith((".bot", ".df")):
                    kind, severity = "botmaker", Severity.RED
                elif path.startswith("abuse-enforcement-service/service-lib/rules/") and path.endswith(".yaml"):
                    kind, severity = "enforcement", Severity.ORANGE
                elif path.startswith("visibility-filtering/rules/golden_corpus/"):
                    kind, severity = "golden_corpus", Severity.GRAY
                elif path in {f["source"]["path"] for f in facts if f["check"] != "manual"}:
                    kind, severity = "watched_file", Severity.GRAY
                else:
                    continue
                result.findings.append(Finding(kind, severity, path, path.rsplit("/", 1)[-1], "文件变化；仅比较公开文本，未判断规则运行效果", old.tree.get(path), new.tree.get(path), evidence=repo.diff(base, target, path)))
        else:
            result.findings.append(Finding("baseline", Severity.GRAY, "", "baseline", "已解析支持的观察源并建立基线；不把既有参数当作新增"))
        if new.level_format == "legacy-inline":
            result.findings.append(Finding("legacy_levels", Severity.GRAY, "visibility-filtering/rules/registry.rs", "legacy", "该历史提交尚未引入 levels.rs；旧布局不提供级别名单计数"))
        if old and old.levels and new.levels:
            before_count = len(set(old.levels["TIMELINE_HOME_RECOMMENDATIONS"]) - set(old.levels["TIMELINE_HOME"]))
            after_count = len(set(new.levels["TIMELINE_HOME_RECOMMENDATIONS"]) - set(new.levels["TIMELINE_HOME"]))
            if before_count != after_count:
                result.findings.append(Finding("derived_count", Severity.RED, LEVEL_PATH, "recommendations_extra", "非粉丝推荐级比粉丝级多的规则数变化", before_count, after_count))
        result.verification, issues = verify(repo, new, facts, today)
        result.findings.extend(issues)
        # 自动观察表随核验状态重建，不改人工登记表。
        result.counts.update(parameters=len(new.params), filter_files=len(new.filters), filter_modules=len(new.modules))
        result.verification.append({
            "id": "_switch_watch", "commit": target, "status": "observed",
            "defaults_off": [name for name, param in new.params.items() if param.value is False],
            "other_observed": [
                {"name": name, "value": param.value, "category": category(name), "state": "零或空配置" if disabled(param.value) else "实验配置非空；部署未知"}
                for name, param in new.params.items()
                if param.value is not False and (disabled(param.value) or category(name) == "experiment")
            ],
        })
    except (ParseError, UnicodeDecodeError) as exc:
        result.complete = False
        result.findings.append(Finding("parse_failure", Severity.RED, "", "parser", "解析失败；停止推进，不能判断为无变化", evidence=str(exc) if isinstance(exc, ParseError) else "上游文本不是 UTF-8"))
    return result
