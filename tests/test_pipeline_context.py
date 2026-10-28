from datetime import datetime, timezone
from pathlib import Path
import shutil

import pytest
import yaml

from tests.test_project_research import prepare
from x_algo_watch.automation import data_files, detect, load_bundle
from x_algo_watch.pipeline_context import load_pipeline_plan, prepare_pipeline_context, pilot_enabled
from x_algo_watch.project_evidence import context_packs
from x_algo_watch.storage import RecordStore
from x_algo_watch.models import WatchError


def test_detection_uses_full_plan_for_pilot_and_keeps_progress_and_upstream_unchanged(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo)
    before = (repo.resolve("HEAD"), repo.git("status", "--porcelain"))
    root = tmp_path / "watch"; root.mkdir(); monkeypatch.chdir(root)
    RecordStore(root / "data").append(result, datetime.now(timezone.utc), "uncommitted")
    original = data_files(root / "data")
    config = root / "config/watch.yaml"; config.parent.mkdir()
    config.write_text(yaml.safe_dump({"project_research": True, "research_pipeline": "mechanisms-v1", "mechanism_pilot_only": True,
                                      "mechanism_pilot_commits": [result.commit]}), encoding="utf-8")
    bundle = root / "bundle"
    manifest = detect(bundle, source=repo.path, research_commit=result.commit)
    assert manifest["research"] == [result.commit]
    load_bundle(bundle)
    pack = context_packs(bundle / "data")[0]
    assert pack["research_pipeline"] == "mechanisms-v1"
    plan = load_pipeline_plan(bundle / "data", pack)
    assert plan["coverage_complete"] and plan["files"]
    assert any(name.startswith("research-plans/") for name in data_files(bundle / "data"))
    assert data_files(root / "data") == original
    assert before == (repo.resolve("HEAD"), repo.git("status", "--porcelain"))


def test_git_supplement_changes_target_task_and_keeps_original_snapshot(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    data = tmp_path / "data"
    pack = prepare_pipeline_context(repo, result.to_dict(), data)
    original = load_pipeline_plan(data, pack)
    target = next(t for t in original["tasks"] if t["path"] == "home-mixer/params/param.rs")
    request = {"task_id": target["task_id"], "issue_id": "I-old-entry", "path": "home-mixer/params/param.rs", "version": "before", "start_line": 1, "end_line": 3, "reason": "核对旧默认值及声明"}
    updated = prepare_pipeline_context(repo, result.to_dict(), data, pack, [request])
    plan = load_pipeline_plan(data, updated)
    assert plan["parent_plan_id"] == original["plan_id"] and updated["item_id"] != pack["item_id"]
    enriched = next(t for t in plan["tasks"] if t.get("replaces_task_id") == target["task_id"])
    assert target["task_id"] not in {t["task_id"] for t in plan["tasks"]}
    assert any(e["kind"] == "requested_context" and e["commit"] == result.base for e in enriched["evidence"].values())
    assert load_pipeline_plan(data, pack) == original
    with pytest.raises(WatchError, match="不存在或行号越界"):
        prepare_pipeline_context(repo, result.to_dict(), data, pack, [{**request, "end_line": 999}])


def test_rollout_expands_new_pipeline_only_after_explicitly_releasing_pilot_gate():
    settings = {"research_pipeline": "mechanisms-v1", "mechanism_pilot_commits": ["a" * 40], "mechanism_pilot_only": True}
    assert pilot_enabled(settings, "a" * 40) and not pilot_enabled(settings, "b" * 40)
    assert pilot_enabled({**settings, "mechanism_pilot_only": False}, "b" * 40)
    with pytest.raises(WatchError, match="布尔"):
        pilot_enabled({**settings, "mechanism_pilot_only": "false"}, "b" * 40)
