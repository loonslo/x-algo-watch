from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_project_research import NOW, MODELS, analysis, prepare, review
from x_algo_watch.automation import data_files
from x_algo_watch.models import WatchError
from x_algo_watch.project_evidence import project_pack
from x_algo_watch.publication import publication_status
from x_algo_watch.research import render_research, research_event
from x_algo_watch.storage import RecordStore
from x_algo_watch.versions import build_versions, filename, validate_home_summary_change


@pytest.mark.parametrize("event,label", [
    (None, "排队"),
    ({"state": "pending"}, "处理中"),
    ({"state": "insufficient_evidence"}, "证据不足"),
    ({"state": "disputed"}, "复核分歧"),
    ({"state": "pending", "iteration": {"phase": "waiting_manual"}}, "失败暂停"),
    ({"state": "disputed", "iteration": {"phase": "paused_limit"}}, "失败暂停"),
])
def test_states_are_distinct_and_fail_closed(event, label):
    status = publication_status(event)
    assert status["label"] == label and not status["publishable"]
    assert publication_status(event, complete=False)["label"] == "🔴 检查未完成"


def approved_event(pack):
    return research_event(pack, {"state": "reviewed", "analysis": analysis(pack),
        "review": review(pack), "extra_review_rounds": 2,
        "reviews": {stage: review(pack) for stage in ("review", "facts_review", "reader_review")}}, MODELS)


def test_all_three_roles_required_and_migration_cannot_replace_reviewed_text(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    event = approved_event(pack)
    assert publication_status(event)["publishable"]
    assert "🔴 检查未完成" in render_research(event, reader_only=True, check_complete=False)
    edition = {"reader": {**event["analysis"]["reader"], "title": "UNREVIEWED_EDITION"}}
    assert "UNREVIEWED_EDITION" not in render_research(event, edition=edition, reader_only=True)
    for stage in event["reviews"]:
        broken = deepcopy(event)
        broken["reviews"][stage] = review(pack, False)
        assert not publication_status(broken)["publishable"]
        assert "算法版本更新公告" not in render_research(broken, edition=edition, reader_only=True)
    broken = deepcopy(event)
    del broken["reviews"]
    assert not publication_status(broken)["publishable"]
    broken = deepcopy(event)
    broken["reviews"]["facts_review"]["item_id"] = "other-input"
    assert not publication_status(broken)["publishable"]


def test_latest_failure_controls_home_index_and_page_without_changing_progress(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    store = RecordStore(tmp_path / "data")
    store.append(result, NOW, "uncommitted")
    store.append_event(approved_event(pack), NOW)
    failed = research_event(pack, {"state": "manual"}, MODELS)
    store.append_event(failed, NOW + timedelta(seconds=1))
    home = tmp_path / "README.md"
    home.write_text("说明\n<!-- versions-summary:begin -->\n旧表\n<!-- versions-summary:end -->\n保留说明\n", encoding="utf-8")
    before = data_files(tmp_path / "data")
    build_versions(tmp_path)
    outputs = [home, tmp_path / "versions/README.md", tmp_path / "versions" / filename(result.to_dict())]
    for path in outputs:
        text = path.read_text(encoding="utf-8")
        assert "失败暂停" in text and "推荐帖子来源设置有变化" not in text
        assert "底层架构微调" not in text and "算法版本更新公告" not in text
    stamps = [path.stat().st_mtime_ns for path in outputs]
    build_versions(tmp_path)
    assert stamps == [path.stat().st_mtime_ns for path in outputs]
    assert data_files(tmp_path / "data") == before


def test_real_6258_failure_is_not_an_announcement_and_rebound_is_preserved():
    root = Path(__file__).resolve().parents[1]
    events = [e for e in RecordStore(root / "data").events() if e["type"] == "version_research" and e["commit"].startswith("6258a15")]
    event = [e for e in events if e.get("iteration", {}).get("history")][-1]
    assert [len(row["result"]["review"]["issues"]) for row in event["iteration"]["history"]] == [11, 10, 1, 9]
    text = render_research(event, reader_only=True)
    assert "失败暂停" in text and "两次输出校验失败" in text
    assert "算法版本更新公告" not in text and "底层架构微调" not in text
    assert "排队" not in text and "无需" not in text


def test_home_publish_only_allows_generated_region():
    before = "说明\n<!-- versions-summary:begin -->旧<!-- versions-summary:end -->\n尾部"
    validate_home_summary_change(before, before.replace("旧", "新"))
    with pytest.raises(WatchError, match="生成区以外"):
        validate_home_summary_change(before, before.replace("说明", "改写说明"))
    with pytest.raises(WatchError, match="缺失或重复"):
        validate_home_summary_change("无生成区", before)


def test_failed_evidence_preparation_suppresses_earlier_approved_announcement(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    store = RecordStore(tmp_path / "data"); store.append(result, NOW, "uncommitted")
    store.append_event(approved_event(pack), NOW)
    store.append_event({"type": "research_context_deferred", "id": "context-failure", "commit": result.commit}, NOW + timedelta(seconds=1))
    build_versions(tmp_path)
    page = (tmp_path / "versions" / filename(result.to_dict())).read_text(encoding="utf-8")
    assert "证据准备失败" in page and "算法版本更新公告" not in page
    assert "证据准备失败" in (tmp_path / "versions/README.md").read_text(encoding="utf-8")


def test_new_context_without_model_result_cannot_reuse_prior_approval(sample_repo, tmp_path):
    from x_algo_watch.project_evidence import save_context
    from x_algo_watch.evidence import digest
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    store = RecordStore(tmp_path / "data"); store.append(result, NOW, "uncommitted")
    store.append_event(approved_event(pack), NOW)
    updated = {**pack, "new_snapshot_marker": "additional evidence"}
    updated["item_id"] = digest({k: v for k, v in updated.items() if k != "item_id"})
    save_context(tmp_path / "data", updated)
    build_versions(tmp_path)
    page = (tmp_path / "versions" / filename(result.to_dict())).read_text(encoding="utf-8")
    assert "新证据已准备" in page and "算法版本更新公告" not in page

@pytest.mark.parametrize("phase,expected", [
    ("waiting_service", "下次运行"), ("waiting_budget", "不会重置"),
    ("waiting_time", "复用已完成"), ("waiting_manual", "不会自动解除"),
    ("paused_limit", "保留原审核历史"), ("paused_stagnation", "不会无限重试"),
    ("waiting_evidence", "补齐"), ("waiting_reader", "三个角色"),
    ("insufficient_scope", "不生成无依据"),
])
def test_recovery_guidance_distinguishes_automatic_and_manual_actions(phase, expected):
    from x_algo_watch.publication import recovery_guidance
    event = {"state": "pending", "iteration": {"phase": phase}}
    assert expected in recovery_guidance(event)
    assert not publication_status(event)["publishable"]
    assert "先修复源码检查" in recovery_guidance(event, complete=False)
    assert "预算核实前" in recovery_guidance({**event, "budget_unverified": True})


def test_unpublished_render_explains_recovery_without_exposing_raw_error():
    from x_algo_watch.publication import render_unpublished
    text = render_unpublished({"state": "pending", "iteration": {
        "phase": "waiting_budget", "reason": "daily_budget"}, "error": "UNTRUSTED_PROVIDER_TEXT"})
    assert "后续处理" in text and "不会重置" in text
    assert "UNTRUSTED_PROVIDER_TEXT" not in text
