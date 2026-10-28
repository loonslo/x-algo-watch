from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import RecordedClient
from tests.test_mechanism_pipeline import SETTINGS
from tests.test_project_research import prepare
from x_algo_watch.automation import data_files, detect, publish, write_bundle
from x_algo_watch.drafting import run
from x_algo_watch.mechanisms import segment_ids
from x_algo_watch.pipeline_context import load_pipeline_plan, prepare_pipeline_context
from x_algo_watch.project_evidence import context_packs, save_context
from x_algo_watch.storage import RecordStore, atomic_write


class ContextClient:
    """只验证流程控制，不冒充真实模型研究质量。"""
    def __init__(self, missing=False):
        self.missing = missing
        self.calls = []

    def call(self, model, system, payload):
        self.calls.append(payload)
        task = payload["evidence_pack"]
        if payload.get("stage_kind") == "review":
            issue = {"claim_id": "", "field": "/segments/" + sorted(segment_ids(task))[0] + "/reason", "problem_key": "missing_previous_context", "category": "evidence",
                     "reason": "需要明确核对旧声明", "evidence_refs": ["E1"], "evidence_requests": [{"path": "home-mixer/params/param.rs", "version": "before", "start_line": 1, "end_line": 3, "reason": "明确定位旧默认值"}]}
            return json.dumps({"item_id": task["task_id"], "agree": not self.missing, "issues": [issue] if self.missing else []}, ensure_ascii=False)
        value = {"task_id": task["task_id"], "claims": [], "segments": [{"segment_id": identifier, "disposition": "researched", "reason": "合成流程测试，不生成真实机制结论"} for identifier in sorted(segment_ids(task))]}
        feedback = payload.get("revision_feedback")
        if feedback:
            return json.dumps({"result": value, "closure_reasons": [{"issue_id": i["issue_id"], "reason": "已核对本轮 Git 补证窗口"} for i in feedback["issues"]]}, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)


def test_git_recovery_preserves_issues_and_does_not_reset_global_candidate_limit(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo)
    root = tmp_path / "watch"; root.mkdir(); monkeypatch.chdir(root)
    shutil.copytree(Path(__file__).resolve().parents[1] / "prompts", root / "prompts")
    settings = {**SETTINGS, "daily_calls": 1000, "max_candidate_versions": 2, "research_pipeline": "mechanisms-v1", "mechanism_pilot_only": True, "mechanism_pilot_commits": [result.commit]}
    config = root / "config/watch.yaml"; config.parent.mkdir(); config.write_text(yaml.safe_dump(settings), encoding="utf-8")
    store = RecordStore(root / "data"); store.append(result, datetime.now(timezone.utc), "uncommitted")
    progress = store.last_success()
    first = root / "first"; detect(first, source=repo.path, research_commit=result.commit)
    out1 = root / "first.json"; run(first, out1, config, client=ContextClient(missing=True))
    original = json.loads(out1.read_text(encoding="utf-8"))["events"][-1]
    assert original["state"] == "insufficient_evidence"
    publish(first, FakeGitHub(), draft_bundle=out1, push=False, notify_issues=False)
    second = root / "second"; detect(second, source=repo.path, research_commit=result.commit)
    pack = context_packs(second / "data")[0]
    plan = load_pipeline_plan(second / "data", pack)
    assert plan["parent_plan_id"] == original["plan_id"]
    out2 = root / "second.json"; run(second, out2, config, client=ContextClient())
    updated = json.loads(out2.read_text(encoding="utf-8"))["events"][-1]
    assert updated["pipeline_loop"]["candidate"] == 2
    assert updated["pipeline_loop"]["context_previous"] == original["id"]
    assert updated["pipeline_loop"]["history"][0]["closure_reasons"]
    assert updated["state"] == "insufficient_evidence"  # 合成响应没有机制结论，不能生成公告。
    publish(second, FakeGitHub(), draft_bundle=out2, push=False, notify_issues=False)
    assert store.last_success() == progress
    working = root / "third-working"
    original_files = data_files(root / "data")
    for name, content in original_files.items(): atomic_write(working / name, content.decode("utf-8"))
    task = next(t for t in plan["tasks"] if t.get("replaces_task_id"))
    request = {"task_id": task["task_id"], "issue_id": "I-third-context", "path": "home-mixer/params/param.rs", "version": "before", "start_line": 1, "end_line": 2, "reason": "新的有界问题"}
    third_pack = prepare_pipeline_context(repo, result.to_dict(), working, pack, [request]); save_context(working, third_pack)
    third = root / "third"; write_bundle(third, original_files, working, test=False, code="uncommitted", result_code=0, research_only=result.commit)
    out3 = root / "third.json"; client = RecordedClient([]); run(third, out3, config, client=client)
    stopped = json.loads(out3.read_text(encoding="utf-8"))["events"][-1]
    assert client.calls == [] and stopped["iteration"]["phase"] == "paused_limit"
    assert stopped["pipeline_loop"]["candidate"] == 2
    publish(third, FakeGitHub(), draft_bundle=out3, push=False, notify_issues=False)
