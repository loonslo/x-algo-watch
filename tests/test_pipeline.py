import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from tests.fakes import FakeGitHub
from tests.test_drafting import MODELS, RecordedClient, analysis, record_and_pack, review
from x_algo_watch.automation import publish, write_bundle
from x_algo_watch.confirmation import confirm
from x_algo_watch.drafting import run
from x_algo_watch.models import CheckResult, Finding, Severity
from x_algo_watch.storage import RecordStore
from x_algo_watch.versions import filename

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("same_model", [False, True])
def test_full_detect_draft_publish_owner_edit_confirm_pipeline(tmp_path, monkeypatch, same_model):
    monkeypatch.chdir(tmp_path)
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    config = tmp_path / "watch.yaml"
    models = {**MODELS, **({"review": MODELS["analysis"]} if same_model else {})}
    config.write_text(yaml.safe_dump({"models": models, "daily_calls": 50, "allow_same_model": same_model}), encoding="utf-8")
    record, pack = record_and_pack()
    working = tmp_path / "working"
    result = CheckResult(record["base"], record["commit"], record["committed_at"], findings=[Finding("parameter", Severity.ORANGE, f["path"], f["name"], f["message"], f["before"], f["after"], f["line_before"], f["line_after"]) for f in record["findings"]], numeric_context=record["numeric_context"])
    RecordStore(working).append(result, datetime.now(timezone.utc), "uncommitted")
    bundle = tmp_path / "bundle"
    write_bundle(bundle, {}, working, test=False, code="uncommitted", result_code=0)
    draft = tmp_path / "draft.json"
    model = RecordedClient([analysis(pack), review(pack)])
    run(bundle, draft, config, client=model)
    assert len(model.calls) == 2
    client = FakeGitHub()
    assert publish(bundle, client, draft_bundle=draft, push=False) == [1]
    assert len([event for event in RecordStore(tmp_path / "data").events() if event["type"] == "model_call"]) == 2
    # 机器人草稿保持在评论；生产文件只含调用审计，不含组稿正文。
    assert "未经确认的草稿" in client.comment_rows[1][0]["body"]
    assert ("同一模型的两次调用" in client.comment_rows[1][0]["body"]) == same_model
    assert not (tmp_path / "digest.md").exists()
    version_page = tmp_path / "versions" / filename(record)
    assert "未人工确认" in version_page.read_text(encoding="utf-8")
    (tmp_path / "digest.md").write_text("# Digest\n", encoding="utf-8")
    client.comment_rows[1][0]["body"] += "\n人工补充：继续观察。"
    client.rows[0]["labels"].append({"name": "已确认"})
    event = {"action": "labeled", "sender": {"login": "owner"}, "label": {"name": "已确认"}, "repository": {"full_name": client.repository}, "issue": {"number": 1}}
    assert confirm(event, client, datetime.now(timezone.utc), tmp_path, push=False) == "confirmed"
    assert "人工补充" in (tmp_path / "digest.md").read_text(encoding="utf-8")
    archived = version_page.read_text(encoding="utf-8")
    assert "已人工确认的详细解读" in archived and "人工补充：继续观察。" in archived
    if same_model:
        confirmed = (tmp_path / "digest.md").read_text(encoding="utf-8")
        assert "同一模型的两次调用" in confirmed and "仓库所有者已人工确认" in confirmed
        assert "仍须仓库所有者人工确认" not in confirmed
    assert client.rows[0]["state"] == "closed"
