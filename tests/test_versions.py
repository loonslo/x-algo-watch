from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest

from tests.fakes import FakeGitHub
from x_algo_watch.automation import commit_paths, data_files, publish, write_bundle
from x_algo_watch.models import CheckResult, Finding, Severity, WatchError
from x_algo_watch.storage import RecordStore
from x_algo_watch.versions import build_versions, filename, source_link

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def record(root, commit="b" * 40, *, findings=None, complete=True, when=NOW):
    result = CheckResult("a" * 40, commit, when.isoformat(), complete=complete, findings=findings or [Finding("parameter", Severity.ORANGE, "home-mixer/params/param.rs", "FavoriteWeight", "默认值变化", 0.6, 0.66)])
    store = RecordStore(root / "data")
    store.append(result, when, "uncommitted")
    return store.records()[-1]


def test_archive_each_commit_newest_first_and_rebuild_is_read_only(tmp_path):
    first = record(tmp_path)
    second = record(tmp_path, "c" * 40, when=NOW + timedelta(days=1))
    before = data_files(tmp_path / "data")
    assert build_versions(tmp_path) == 2
    index = (tmp_path / "versions/README.md").read_text(encoding="utf-8")
    assert index.index("ccccccc") < index.index("bbbbbbb")
    assert filename(first) in index and filename(second) in index
    page = (tmp_path / "versions" / filename(first)).read_text(encoding="utf-8")
    assert "点赞信号的打分系数" in page and "0.6 → 0.66" in page
    assert "可能影响" in page and "创作者现在要做什么" in page
    assert "未人工确认" in page and "流量涨跌" in page
    stamps = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "versions").glob("*.md")}
    build_versions(tmp_path)
    assert stamps == {p.name: p.stat().st_mtime_ns for p in (tmp_path / "versions").glob("*.md")}
    assert data_files(tmp_path / "data") == before


def test_failure_recovery_keeps_one_file_and_never_claims_no_change(tmp_path):
    result = record(tmp_path, findings=[Finding("parse_failure", Severity.RED, "params.rs", "parse", "无法解析")], complete=False)
    build_versions(tmp_path)
    path = tmp_path / "versions" / filename(result)
    assert "检查未完成" in path.read_text(encoding="utf-8")
    record(tmp_path, when=NOW + timedelta(seconds=1))
    build_versions(tmp_path)
    pages = list((tmp_path / "versions").glob("*-*.md"))
    assert len(pages) == 1
    assert "检查未完成" not in pages[0].read_text(encoding="utf-8")


def test_decision_and_exact_owner_text_sync_without_model_drafts(tmp_path):
    result = record(tmp_path)
    store = RecordStore(tmp_path / "data")
    store.append_event({"type": "model_call", "id": "model:test", "commit": result["commit"], "raw": "未经审核的模型猜测"}, NOW)
    build_versions(tmp_path)
    page = tmp_path / "versions" / filename(result)
    assert "未经审核的模型猜测" not in page.read_text(encoding="utf-8")
    store.append_event({"type": "decision", "id": "confirmed:test", "commit": result["commit"], "outcome": "confirmed"}, NOW)
    with pytest.raises(WatchError, match="缺少对应速报"):
        build_versions(tmp_path)
    approved = "所有者编辑后的独特文字\n\n- 暂不改变运营方法。"
    (tmp_path / "digest.md").write_text(f"# Digest\n\n<!-- xaw-digest:{result['commit']} -->\n{approved}\n", encoding="utf-8")
    build_versions(tmp_path)
    assert approved in page.read_text(encoding="utf-8")
    assert "已人工确认" in page.read_text(encoding="utf-8")
    store.append_event({"type": "decision", "id": "rejected:test", "commit": result["commit"], "outcome": "rejected"}, NOW + timedelta(seconds=1))
    build_versions(tmp_path)
    text = page.read_text(encoding="utf-8")
    assert "解读已驳回" in text and approved not in text


def test_exact_math_and_switch_explanation_do_not_promise_deployment(tmp_path):
    result = record(tmp_path, findings=[
        Finding("parameter", Severity.ORANGE, "params.rs", "FavoriteWeight", "变化", 0.6, 0.66),
        Finding("parameter", Severity.ORANGE, "params.rs", "NotInterestedWeight", "变化", -47.52, -52.272),
        Finding("parameter", Severity.RED, "params.rs", "EnableVideoCarouselAuthorDedupe", "变化", False, True),
        Finding("parameter", Severity.YELLOW, "params.rs", "EnableFairShareRanking", "新增", None, False),
    ])
    build_versions(tmp_path)
    text = (tmp_path / "versions" / filename(result)).read_text(encoding="utf-8")
    assert "点赞权重 ×1.1" in text and "绝对比值 79.2 → 79.2" in text
    assert "代码默认从关变开" in text and "不能说明平台已经对所有用户启用" in text
    assert "此处默认关闭或为空" in text


def test_source_links_preserve_deleted_evidence_and_directory_navigation(tmp_path):
    result = record(tmp_path)
    deleted = Finding("parameter", Severity.YELLOW, "params.rs", "Old", "删除", True, None, 12, None).to_dict()
    assert f"/blob/{result['base']}/params.rs#L12" in source_link(result, deleted)
    directory = Finding("filters", Severity.ORANGE, "home-mixer/filters/", "files", "增删", [], ["new.rs"]).to_dict()
    assert f"/tree/{result['commit']}/home-mixer/filters/" in source_link(result, directory)


def test_filenames_use_utc_day_and_unknown_markup_is_escaped(tmp_path):
    result = record(tmp_path, findings=[Finding("parameter", Severity.ORANGE, "params.rs", "Example|[link](target)", "变化", "<script>old</script>", "new")], when=datetime(2026, 10, 22, 1, tzinfo=timezone(timedelta(hours=8))))
    assert filename(result).startswith("2026-10-21-")
    build_versions(tmp_path)
    text = (tmp_path / "versions" / filename(result)).read_text(encoding="utf-8")
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "Example\\|\\[link\\]" in text


def test_recheck_does_not_replace_iteration_changes(tmp_path):
    result = record(tmp_path)
    verification = CheckResult(result["commit"], result["commit"], NOW.isoformat(), findings=[Finding("registry_mismatch", Severity.RED, "params.rs", "Known", "依据变化")])
    RecordStore(tmp_path / "data").append_event({"type": "verification", "id": "recheck:test", "commit": result["commit"], "result": verification.to_dict()}, NOW)
    build_versions(tmp_path)
    text = (tmp_path / "versions" / filename(result)).read_text(encoding="utf-8")
    assert "0.6 → 0.66" in text and "后续登记核对" in text
    assert len(list((tmp_path / "versions").glob("*-*.md"))) == 1


def test_sensitive_output_is_blocked_before_any_pages_written(tmp_path):
    result = record(tmp_path)
    RecordStore(tmp_path / "data").append_event({"type": "decision", "id": "confirmed:test", "commit": result["commit"], "outcome": "confirmed"}, NOW)
    (tmp_path / "digest.md").write_text(f"<!-- xaw-digest:{result['commit']} -->\n" + "sk-or-v1-" + "a" * 40, encoding="utf-8")
    with pytest.raises(WatchError, match="疑似凭据"):
        build_versions(tmp_path)
    assert not (tmp_path / "versions").exists()


def test_history_test_publish_never_writes_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record(tmp_path / "working")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, {}, tmp_path / "working/data", test=True, code="uncommitted", result_code=0)
    publish(bundle, FakeGitHub(), push=False)
    assert not (tmp_path / "versions").exists()


@pytest.mark.parametrize("bad_path", ["versions/unapproved.md", "versions/execute.py", "versions/sub/README.md"])
def test_archive_publish_whitelist_rejects_other_files(tmp_path, monkeypatch, bad_path):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-b", "main"], check=True, capture_output=True)
    path = tmp_path / bad_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not approved", encoding="utf-8")
    with pytest.raises(WatchError, match="范围外"):
        commit_paths(["versions"], "archive", "owner/x-algo-watch", "test-token")
