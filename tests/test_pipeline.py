import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from tests.fakes import FakeGitHub
from tests.test_drafting import MODELS, RecordedClient, analysis, record_and_pack, review
from x_algo_watch.automation import publish, write_bundle
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
