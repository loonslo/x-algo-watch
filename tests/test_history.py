"""可选的真实历史验收；只读取调用者明确传入的 Git 副本。"""

from datetime import date

import pytest

from x_algo_watch.cli import main
from x_algo_watch.engine import compare
from x_algo_watch.models import Severity
from x_algo_watch.storage import RecordStore


@pytest.fixture(scope="module")
def replay(upstream, tmp_path_factory):
    root = tmp_path_factory.mktemp("historical-replay")
    registry = root / "facts.yaml"
    registry.write_text("[]\n", encoding="utf-8")
    output = root / "data"
    before = (upstream.resolve("HEAD"), upstream.git("status", "--porcelain=v1", "--untracked-files=all"))
    args = ["replay", "--repo", str(upstream.path), "--from", "47c1bcd", "--to", "7f2ca2b", "--registry", str(registry), "--output", str(output), "--now", "2026-10-21T08:00:00+00:00"]
    assert main(args) == 0
    first_bytes = {p.name: p.read_bytes() for p in (output / "records").glob("*.md")}
    assert main(args) == 0
    assert first_bytes == {p.name: p.read_bytes() for p in (output / "records").glob("*.md")}
    assert before == (upstream.resolve("HEAD"), upstream.git("status", "--porcelain=v1", "--untracked-files=all"))
    return {record["commit"][:7]: record for record in RecordStore(output).records()}


def test_full_replay_count_and_read_only(replay):
    assert len(replay) == 50
    assert all(record["complete"] for record in replay.values())


@pytest.mark.parametrize("ref,name,before,after,level", [
    ("77d431a", "PhoenixColdStartMaxResults", 0, 200, "橙"),
    ("77d431a", "ColdStartImpressionThreshold", 1000, 200, "橙"),
    ("77d431a", "ColdStartFollowerCap", 1000, 50000, "橙"),
    ("77d431a", "ColdStartMaxPostAgeSecs", 172800, 7200, "橙"),
    ("77d431a", "LowImpressionsMaxPositionRatio", 0.85, 0.97, "橙"),
    ("77d431a", "EnableColdStartThompsonSampling", False, True, "红"),
    ("78460ca", "EnablePopularPostsSource", False, True, "红"),
    ("e1a0375", "FavoriteWeight", 0.5, 0.6, "橙"),
    ("17c96bf", "FavoriteWeight", 0.6, 0.66, "橙"),
    ("17c96bf", "NotInterestedWeight", -47.52, -52.272, "橙"),
    ("862a123", "EnableVideoCarouselAuthorDedupe", False, True, "红"),
    ("862a123", "EnableVideoCarouselEmbeddingDedupe", False, True, "红"),
])
def test_known_changes(replay, ref, name, before, after, level):
    finding = next(f for f in replay[ref]["findings"] if f["name"] == name)
    assert (finding["before"], finding["after"], finding["severity"]) == (before, after, level)


def test_latest_additions_and_rule_count(replay):
    latest = replay["7f2ca2b"]
    params = [f for f in latest["findings"] if f["category"] == "parameter"]
    assert len(params) == 3 and all(f["severity"] == "黄" for f in params)
    assert latest["counts"]["parameters"] == 198
    assert latest["counts"]["filter_files"] == 34
    assert latest["counts"]["filter_modules"] == 33
    filters = [f for f in latest["findings"] if f["category"] == "filters"]
    assert any("home-mixer/filters/fair_share_ranking_filter.rs" in f["after"] for f in filters)
    extra = next(f for f in replay["74d49a4"]["findings"] if f["category"] == "derived_count")
    assert (extra["before"], extra["after"], extra["severity"]) == (26, 27, "红")


def test_unrecognized_historical_formats_are_red(upstream, monkeypatch):
    from x_algo_watch import engine
    from x_algo_watch.models import ParseError

    def reject(_):
        raise ParseError("visibility-filtering/rules/levels.rs:1 模拟旧解析器不认识新写法")

    monkeypatch.setattr(engine, "parse_levels", reject)
    for ref in ("92f0918", "74d49a4"):
        target = upstream.resolve(ref)
        result = compare(upstream, upstream.parent(target), target, [], date(2026, 10, 21))
        assert not result.complete and result.severity == Severity.RED
        assert not any(f.category == "visibility" for f in result.findings)
