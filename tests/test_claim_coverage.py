from datetime import date
from pathlib import Path

import pytest
import yaml

from x_algo_watch.claim_coverage import build_claim_coverage, claim_rows, resolve_path
from x_algo_watch.engine import compare
from x_algo_watch.models import WatchError
from x_algo_watch.registry import load_registry, tree_digest
from x_algo_watch.storage import public_text


def synthetic_review():
    rows = ["### 2.1 修正", "| # | 手册位置 | 原说法 | 源码事实 | 依据 | 结论 | 处理 |"]
    for n in range(1, 42):
        basis = "同上" if n == 2 else ("同第 1 条" if n == 39 else "`sample.rs:1`；`other.rs`")
        rows.append(f"| {n} | 4 章 | PRIVATE_CLAIM_SENTINEL | 私有解释 | {basis} | 合成 | 合成 |")
    rows.extend(["### 2.2 一致", "| 手册位置 | 说法 | 依据 |"])
    for n in range(1, 34):
        basis = "同多样性" if n in (12, 23) else ("全仓库检索 home-mixer、vm-ranker" if n == 32 else "`sample.rs:1`")
        rows.append(f"| 4 章 | PRIVATE_CLAIM_SENTINEL | {basis} |")
    rows.append("### 2.3 结束")
    return "\n".join(rows)


def test_statement_dependencies_reuse_anchors_inherit_and_avoid_private_text(sample_repo):
    path, repo, _, commit = sample_repo
    for name in ("sample.rs", "other.rs", "vm-ranker/module.rs"):
        p = path / name; p.parent.mkdir(exist_ok=True); p.write_text("const UNIQUE: u32 = 2;\n", encoding="utf-8")
    target = commit()
    facts, ledger = build_claim_coverage(repo, target, target, synthetic_review(), [])
    assert ledger["mapped_code_claims"] == 74
    assert len(facts) == 4  # 一个原文锚点、一个文件、两个目录依赖
    assert ledger["rows"][1]["inherited_from"] == "2.1:1"
    assert ledger["rows"][52]["inherited_from"] == "2.1:5"
    assert all(not row["semantic_verified"] and row["dependencies"] for row in ledger["rows"])
    text = str(facts) + str(ledger)
    assert "PRIVATE_CLAIM_SENTINEL" not in text and "私有解释" not in text
    public_text(text)
    _, second = build_claim_coverage(repo, target, target, synthetic_review(), facts)
    assert second == ledger


def test_tree_watch_detects_edits_additions_and_missing_dependencies(sample_repo):
    path, repo, _, commit = sample_repo
    (path / "home-mixer/extra.rs").write_text("optional module", encoding="utf-8")
    baseline = commit()
    facts = [{"id": name, "check": "tree", "source": {"path": name}, "value": tree_digest(repo, baseline, name)}
             for name in ("home-mixer/", "home-mixer/extra.rs")]
    unchanged = compare(repo, baseline, baseline, facts, date(2026, 10, 22))
    watched = [item for item in unchanged.verification if item.get("check") == "tree"]
    assert len(watched) == 2 and all(item["status"] == "pass" for item in watched)
    (path / "home-mixer-extra").mkdir()
    (path / "home-mixer-extra/file.rs").write_text("unrelated", encoding="utf-8")
    outside = commit()
    assert tree_digest(repo, outside, "home-mixer/") == facts[0]["value"]
    (path / "home-mixer/new.rs").write_text("new file", encoding="utf-8")
    (path / "home-mixer/extra.rs").unlink()
    changed = compare(repo, outside, commit(), facts, date(2026, 10, 22))
    watched = [item for item in changed.verification if item.get("check") == "tree"]
    assert len(watched) == 2 and all(item["status"] == "mismatch" for item in watched)
    assert watched[1]["actual"] is None


def test_tree_registry_rejects_unusable_digest(tmp_path):
    path = tmp_path / "facts.yaml"
    path.write_text(yaml.safe_dump([{"id": "bad", "check": "tree", "source": {"path": "scope/"}, "value": None}]), encoding="utf-8")
    with pytest.raises(WatchError):
        load_registry(path)


def test_claim_source_change_or_ambiguous_path_fails_closed():
    with pytest.raises(WatchError):
        claim_rows(synthetic_review().replace("| 41 |", "not a row | 41 |"))
    with pytest.raises(WatchError):
        resolve_path("sample.rs", {"a/sample.rs": "1", "b/sample.rs": "2"})


def test_real_74_statement_ledger_has_registered_dependencies():
    root = Path(__file__).resolve().parents[1]
    ledger = yaml.safe_load((root / "registry/code-claim-coverage.yaml").read_text(encoding="utf-8"))
    facts = {fact["id"]: fact for fact in load_registry(root / "registry/facts.yaml")}
    assert len(ledger["rows"]) == ledger["mapped_code_claims"] == ledger["expected_code_claims"] == 74
    assert len({row["id"] for row in ledger["rows"]}) == 74
    for row in ledger["rows"]:
        assert row["semantic_verified"] is False
        assert row["dependencies"]
        for dep in row["dependencies"]:
            assert dep["fact_ids"]
            assert all(facts[key]["source"]["path"] == dep["path"] for key in dep["fact_ids"])
