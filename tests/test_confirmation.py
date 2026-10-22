from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fakes import FakeGitHub
from tests.test_automation import sample_record
from x_algo_watch.automation import publish_issues
from x_algo_watch.confirmation import authorized, confirm
from x_algo_watch.models import CheckResult, WatchError
from x_algo_watch.storage import RecordStore

NOW = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)


def event(client, actor="owner", label="已确认"):
    return {"action": "labeled", "sender": {"login": actor}, "label": {"name": label}, "repository": {"full_name": client.repository}, "issue": {"number": 1}}


def prepare(tmp_path):
    client = FakeGitHub(); record = sample_record()
    publish_issues(client, [record], False)
    RecordStore(tmp_path / "data").append(CheckResult(None, record["commit"], NOW.isoformat()), NOW, "uncommitted")
    (tmp_path / "digest.md").write_text("# Digest\n", encoding="utf-8")
    client.rows[0]["labels"].append({"name": "已确认"})
    client.comment_rows[1] = [{"id": 1, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": "<!-- xaw-draft:" + record["commit"] + ":" + "1" * 20 + " -->\n\n未经确认的草稿\n\n所有者编辑后的文字\n\n- 确认：待仓库所有者编辑核对并打标签。"}]
    return client, record


@pytest.mark.parametrize("actor,label", [("attacker", "已确认"), ("owner", "unrelated"), ("github-actions[bot]", "驳回")])
def test_only_owner_label_event_accepted(tmp_path, actor, label):
    client, _ = prepare(tmp_path)
    before = (tmp_path / "digest.md").read_bytes()
    assert confirm(event(client, actor, label), client, NOW, tmp_path, push=False) == "ignored"
    assert (tmp_path / "digest.md").read_bytes() == before
    assert client.rows[0]["state"] == "open"


def test_owner_edited_bot_comment_appended_once_and_closed(tmp_path):
    client, record = prepare(tmp_path)
    (tmp_path / "digest.md").write_text("# Digest\n\n尚无人工确认的速报。P4 接入草稿与确认流程后追加。\n", encoding="utf-8")
    client.comment_rows[1][0]["body"] += f"\n### 2026-10-14 · {record['commit'][:7]} · 源码变化待人工确认\n"
    assert confirm(event(client), client, NOW, tmp_path, push=False) == "confirmed"
    first = (tmp_path / "digest.md").read_text(encoding="utf-8")
    assert "所有者编辑后的文字" in first and "2026-10-22" in first
    assert "尚无人工确认的速报" not in first
    assert "源码变化待人工确认" not in first
    assert "源码变化已人工确认" in first
    assert client.rows[0]["state"] == "closed"
    assert confirm(event(client), client, NOW, tmp_path, push=False) == "confirmed"
    assert (tmp_path / "digest.md").read_text(encoding="utf-8") == first
    assert len([e for e in RecordStore(tmp_path / "data").events() if e["type"] == "decision"]) == 1


@pytest.mark.parametrize("problem", ["fake_author", "incomplete", "secret", "conflicting_labels"])
def test_bad_drafts_and_conflicting_labels_do_not_publish(tmp_path, problem):
    client, _ = prepare(tmp_path)
    if problem == "fake_author":
        client.comment_rows[1][0]["user"] = {"login": "attacker", "type": "User"}
    elif problem == "incomplete":
        client.comment_rows[1][0]["body"] += "【待补】"
    elif problem == "secret":
        client.comment_rows[1][0]["body"] += "sk-or-v1-" + "a" * 40
    else:
        client.rows[0]["labels"].append({"name": "驳回"})
    before = (tmp_path / "digest.md").read_bytes()
    with pytest.raises(WatchError):
        confirm(event(client), client, NOW, tmp_path, push=False)
    assert (tmp_path / "digest.md").read_bytes() == before
    assert client.rows[0]["state"] == "open"


def test_rejection_only_records_decision(tmp_path):
    client, _ = prepare(tmp_path)
    client.rows[0]["labels"] = [{"name": "驳回"}]
    before = (tmp_path / "digest.md").read_bytes()
    assert confirm(event(client, label="驳回"), client, NOW, tmp_path, push=False) == "rejected"
    assert (tmp_path / "digest.md").read_bytes() == before
    assert client.rows[0]["state"] == "closed"
    assert any(e["outcome"] == "rejected" for e in RecordStore(tmp_path / "data").events())


def test_test_issue_does_not_enter_digest(tmp_path):
    client, _ = prepare(tmp_path)
    client.rows[0]["labels"].append({"name": "测试"})
    assert confirm(event(client), client, NOW, tmp_path, push=False) == "test_closed"
    assert (tmp_path / "digest.md").read_text(encoding="utf-8") == "# Digest\n"
