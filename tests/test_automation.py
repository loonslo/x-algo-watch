import json
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeGitHub
from x_algo_watch.automation import data_files, evidence_body, hashes, load_bundle, publish, publish_issues, write_bundle
from x_algo_watch.models import CheckResult, Finding, Severity, WatchError
from x_algo_watch.storage import RecordStore

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def sample_record():
    return CheckResult("a" * 40, "b" * 40, NOW.isoformat(), findings=[Finding("parameter", Severity.ORANGE, "home-mixer/params/param.rs", "FavoriteWeight", "默认值变化；部署未知", 0.6, 0.66, 1, 1)]).to_dict()


def test_issue_creation_is_idempotent_and_closed_not_reopened():
    client = FakeGitHub(); record = sample_record()
    assert publish_issues(client, [record], False) == [1]
    assert publish_issues(client, [record], False) == [1]
    assert len(client.rows) == 1
    client.rows[0]["state"] = "closed"
    assert publish_issues(client, [record], False) == []
    assert client.rows[0]["state"] == "closed"


def test_fake_human_issue_cannot_impersonate_bot():
    client = FakeGitHub(); record = sample_record()
    publish_issues(client, [record], False)
    client.rows[0]["user"] = {"login": "attacker", "type": "User"}
    assert publish_issues(client, [record], False) == [2]


def test_test_issue_is_isolated_and_has_evidence():
    client = FakeGitHub(); record = sample_record()
    publish_issues(client, [record], False)
    publish_issues(client, [record], True)
    assert len(client.rows) == 2 and "历史测试" in client.rows[1]["body"]
    assert "测试" in {label["name"] for label in client.rows[1]["labels"]}
    assert "默认值" in evidence_body(record, client.repository, False)


def test_draft_comment_keeps_owner_edit_on_retry():
    client = FakeGitHub(); record = sample_record()
    drafts = {record["commit"]: {"version": "1" * 20, "body": "未经确认的草稿", "ready": True}}
    publish_issues(client, [record], False, drafts)
    client.comment_rows[1][0]["body"] += "\n所有者编辑后的句子"
    publish_issues(client, [record], False, drafts)
    assert len(client.comment_rows[1]) == 1
    assert "所有者编辑" in client.comment_rows[1][0]["body"]


def test_bundle_tamper_and_stale_main_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = tmp_path / "data"
    working = tmp_path / "working"
    store = RecordStore(working)
    record = CheckResult("a" * 40, "b" * 40, NOW.isoformat(), findings=[Finding("parameter", Severity.RED, "code.rs", "Example", "变化")])
    store.append(record, NOW, "uncommitted")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, {}, working, test=False, code="uncommitted", result_code=0)
    manifest, _, records = load_bundle(bundle)
    assert len(records) == 1
    original.mkdir(); (original / "status.md").write_text("newer main", encoding="utf-8")
    client = FakeGitHub()
    with pytest.raises(WatchError, match="拒绝覆盖"):
        publish(bundle, client, push=False)
    assert not client.calls
    (bundle / "data/status.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(WatchError, match="哈希"):
        load_bundle(bundle)


def test_publish_scans_draft_before_any_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    working = tmp_path / "working"
    RecordStore(working).append(CheckResult(None, "b" * 40, NOW.isoformat()), NOW, "uncommitted")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, {}, working, test=False, code="uncommitted", result_code=0)
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"drafts": {"bad": "sk-or-v1-" + "a" * 40}}), encoding="utf-8")
    client = FakeGitHub()
    with pytest.raises(WatchError):
        publish(bundle, client, draft_bundle=draft, push=False)
    assert not (tmp_path / "data").exists() and not client.calls


def test_only_approved_data_paths_are_included(tmp_path):
    (tmp_path / "status.md").write_text("safe", encoding="utf-8")
    (tmp_path / "execute.py").write_text("not data", encoding="utf-8")
    assert set(data_files(tmp_path)) == {"status.md"}


def test_registry_red_event_notifies_even_without_new_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    working = tmp_path / "working"
    store = RecordStore(working)
    store.append(CheckResult(None, "a" * 40, NOW.isoformat()), NOW, "uncommitted")
    original = data_files(working)
    result = CheckResult("a" * 40, "a" * 40, NOW.isoformat(), findings=[Finding("registry_mismatch", Severity.RED, "code.rs", "Known", "登记项不成立")])
    store.append_event({"type": "verification", "id": "verify:new", "commit": "a" * 40, "result": result.to_dict()}, NOW + timedelta(seconds=1))
    bundle = tmp_path / "bundle"
    manifest = write_bundle(bundle, original, working, test=False, code="uncommitted", result_code=0)
    assert manifest["notifications"] == ["a" * 40]
    _, _, records = load_bundle(bundle)
    assert records[0]["findings"][0]["category"] == "registry_mismatch"
