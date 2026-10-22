import json
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tests.fakes import FakeGitHub
from x_algo_watch.automation import commit_paths, data_files, detect, evidence_body, hashes, load_bundle, publish, publish_issues, write_bundle
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


@pytest.mark.parametrize("ready", [True, False])
def test_draft_status_labels_transition_without_removing_custom_labels(ready):
    client = FakeGitHub(); record = sample_record()
    publish_issues(client, [record], False)
    client.rows[0]["labels"].append({"name": "所有者自定义"})
    draft = {"version": "1" * 20, "body": "未经确认的草稿", "ready": ready}
    publish_issues(client, [record], False, {record["commit"]: draft})
    labels = {label["name"] for label in client.rows[0]["labels"]}
    assert labels & {"待起草", "待确认", "需人工"} == {"待确认" if ready else "需人工"}
    assert {"所有者自定义", "x-algo-watch", "级别:橙"} <= labels
    before = len(client.calls)
    publish_issues(client, [record], False, {record["commit"]: draft})
    assert not any(method != "GET" and "/labels" in path for method, path, _ in client.calls[before:])
    draft.update(version="2" * 20, ready=not ready)
    publish_issues(client, [record], False, {record["commit"]: draft})
    labels = {label["name"] for label in client.rows[0]["labels"]}
    assert labels & {"待起草", "待确认", "需人工"} == {"需人工" if ready else "待确认"}
    assert "所有者自定义" in labels


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


def test_reader_page_failure_does_not_lose_inspection_or_skip_notification(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    working = tmp_path / "working"
    record = sample_record()
    result = CheckResult(record["base"], record["commit"], record["committed_at"], findings=[Finding("parameter", Severity.ORANGE, "code.rs", "FavoriteWeight", "变化", 0.6, 0.66)])
    RecordStore(working).append(result, NOW, "uncommitted")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, {}, working, test=False, code="uncommitted", result_code=0)
    commits = []
    def record_commit(paths, *args):
        assert RecordStore(Path("data")).last_success() == record["commit"]
        commits.append(paths)
    def bad_reader(root):
        assert commits == [["data"]]
        raise WatchError("派生页面失败")
    monkeypatch.setattr("x_algo_watch.automation.commit_paths", record_commit)
    monkeypatch.setattr("x_algo_watch.automation.build_versions", bad_reader)
    client = FakeGitHub()
    with pytest.raises(WatchError, match="调用审计与通知已保留"):
        publish(bundle, client)
    assert commits == [["data"]]
    assert len(client.rows) == 1 and client.rows[0]["state"] == "open"


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


def test_commit_data_directory_and_ephemeral_push_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-b", "main"], check=True, capture_output=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/status.md").write_text("checked\n", encoding="utf-8")
    original_run = subprocess.run
    pushed = []

    def local_only(args, **kwargs):
        if "push" in args:
            pushed.append((args, kwargs))
            return SimpleNamespace(returncode=0)
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_only)
    assert commit_paths(["data"], "inspection", "owner/x-algo-watch", "test-secret")
    assert len(pushed) == 1
    args, options = pushed[0]
    assert "test-secret" not in " ".join(args)
    assert options["env"]["GIT_CONFIG_COUNT"] == "1"
    assert "extraheader" not in original_run(["git", "config", "--local", "--list"], capture_output=True, text=True).stdout


def test_large_evidence_keeps_important_numbers_and_valid_json():
    record = sample_record()
    record["findings"] += [Finding("derived_count", Severity.RED, "levels.rs", "recommendations_extra", "规则差额", 26, 27).to_dict(), Finding("golden_corpus", Severity.GRAY, "case.rs", "case", "佐证", evidence="x" * 60_000).to_dict()]
    body = evidence_body(record, "owner/x-algo-watch", True)
    evidence = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
    change = next(f for f in evidence["findings"] if f["name"] == "recommendations_extra")
    assert (change["before"], change["after"]) == (26, 27)


def test_backfill_uses_recorded_evidence_without_moving_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = RecordStore(Path("data"))
    store.append(CheckResult("a" * 40, "b" * 40, NOW.isoformat(), findings=[Finding("parameter", Severity.ORANGE, "code.rs", "FavoriteWeight", "默认值变化", 0.6, 0.66)]), NOW, "uncommitted")
    store.append(CheckResult("b" * 40, "c" * 40, NOW.isoformat()), NOW + timedelta(seconds=1), "uncommitted")
    original = data_files(Path("data"))

    def no_detection(*args, **kwargs):
        raise AssertionError("补写不能重新检查上游")

    monkeypatch.setattr("x_algo_watch.automation.cli.main", no_detection)
    bundle = tmp_path / "bundle"
    manifest = detect(bundle, backfill_commit="b" * 40)
    assert not manifest["test"] and manifest["notifications"] == ["b" * 40]
    assert data_files(Path("data")) == original
    client = FakeGitHub()
    assert publish(bundle, client, push=False) == [1]
    assert store.last_success() == "c" * 40 and len(store.records()) == 2
    assert [e["type"] for e in store.events()] == ["backfill_requested", "notification_eligible"]
    assert "测试" not in {label["name"] for label in client.rows[0]["labels"]}


@pytest.mark.parametrize("mode", ["short_sha", "mixed_test", "missing", "incomplete", "gray"])
def test_backfill_rejects_untrusted_or_incompatible_inputs(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    store = RecordStore(Path("data"))
    findings = [] if mode == "gray" else [Finding("parse_failure", Severity.RED, "code.rs", "Known", "失败")]
    store.append(CheckResult(None, "b" * 40, NOW.isoformat(), complete=mode != "incomplete", findings=findings), NOW, "uncommitted")
    original = data_files(Path("data"))
    target = "bad" if mode == "short_sha" else "c" * 40 if mode == "missing" else "b" * 40
    with pytest.raises(WatchError, match="补写"):
        detect(tmp_path / "bundle", backfill_commit=target, test_from="a" * 40 if mode == "mixed_test" else "", test_to="b" * 40 if mode == "mixed_test" else "")
    assert data_files(Path("data")) == original
