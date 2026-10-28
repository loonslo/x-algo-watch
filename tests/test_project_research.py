from datetime import date, datetime, timezone
from pathlib import Path
import json
import shutil

import pytest
import yaml

from tests.fakes import FakeGitHub
from tests.test_drafting import MODELS, PROMPTS, RecordedClient
from x_algo_watch.automation import detect, publish, publish_issues, write_bundle
from x_algo_watch.confirmation import confirm
from x_algo_watch.drafting import analyze_items, run, validate_analysis
from x_algo_watch.engine import compare
from x_algo_watch.evidence import digest
from x_algo_watch.project_evidence import MAX_TEXT, code_range_lengths, context_packs, pending_packs, project_pack, save_context, save_context_snapshot
from x_algo_watch.research import analysis_projection, render_research, research_event, validate_research
from x_algo_watch.storage import RecordStore
from x_algo_watch.versions import filename

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def prepare(sample_repo):
    path, repo, base, commit = sample_repo
    params = path / "home-mixer/params/param.rs"
    params.write_text(params.read_text(encoding="utf-8").replace('"enable", false', '"enable", true'), encoding="utf-8")
    logic = path / "home-mixer/sources/example_source.rs"
    logic.parent.mkdir(parents=True)
    logic.write_text("// This file is data, never run it.\nfn enabled(query: Query) -> bool { query.params.get(EnableExampleSource) }\n", encoding="utf-8")
    target = commit()
    result = compare(repo, base, target, [], date(2026, 10, 22))
    return repo, result


def analysis(pack):
    return {
        "item_id": pack["item_id"], "title": "新增候选来源可能改变参与排序的内容构成",
        "summary": "设置从默认关闭变为默认开启，来源实现也读取这一开关；若该路径运行，候选池构成可能改变。",
        "reader": {"title": "推荐帖子来源设置有变化", "what_changed": "增加了一路准备参与推荐的帖子。", "who": "可能涉及这一路找到的帖子作者。",
                   "when": "线上状态未知，需要线上采用新默认值并实际调用这一来源。", "online_status": "unverified", "action": "当前证据不足以支持调整发布策略。",
                   "impacts": [{"before": "此前这一来源的帖子不能经此入口参与推荐。", "after": "若来源开启并接入，帖子可以经此入口参与推荐。", "heading": "部分帖子可能多一个参与推荐的入口", "effect": "新来源若运行，可能改变参与推荐的帖子集合。", "conditions": "线上打开且实际调用该来源。", "limits": "不能承诺最后展示或流量增加。", "evidence_refs": ["E1"]}],
                   "terms": [], "evidence_refs": ["E1"]},
        "changes": [{"mechanism": "候选来源接入", "before": "来源开关默认关闭", "after": "默认开启，并存在读取它的实现", "evidence_refs": ["E1"]}],
        "hypotheses": [{"hypothesis": "新增来源中的内容可能获得参与排序的机会", "basis": "开关变开且来源代码读取它，候选入口可能放行", "confidence": "medium", "conditions": "线上使用该默认值并调用此来源", "alternative": "调用链可能没有实际接入这一来源", "verify": "检查候选流水线接入情况与线上参数", "creator_impact": "来源能覆盖的内容可能进入候选池，但不承诺最终被展示", "evidence_refs": ["E1"]}],
        "no_hypothesis_reason": "", "questions": ["该来源是否实际接入候选流水线？"],
    }


def review(pack, agree=True):
    return {"item_id": pack["item_id"], "agree": agree, "issues": [] if agree else [{"field": "hypotheses", "reason": "尚无调用位置证明已接入流水线", "evidence_refs": ["E1"]}]}


def test_project_pack_reads_implementation_diff_and_references_without_execution(sample_repo):
    repo, result = prepare(sample_repo)
    before = repo.git("status", "--porcelain")
    pack = project_pack(repo, result.to_dict())
    kinds = {e["kind"] for e in pack["evidence"].values()}
    assert {"project_overview", "diff", "implementation", "reference_window"} <= kinds
    assert any("query.params.get(EnableExampleSource)" in e["text"] for e in pack["evidence"].values())
    assert pack["coverage"]["text_characters"] <= MAX_TEXT
    assert pack["item_id"] == digest({k: v for k, v in pack.items() if k != "item_id"})
    assert repo.git("status", "--porcelain") == before and repo.resolve("HEAD") == result.commit


@pytest.mark.parametrize("problem", ["no_evidence", "missing_alternative", "no_hypothesis_reason", "wrong_number", "deployment", "unknown_path"])
def test_research_requires_evidence_and_testable_inferences(sample_repo, problem):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    value = analysis(pack)
    if problem == "no_evidence": value["hypotheses"][0]["evidence_refs"] = ["E999"]
    if problem == "missing_alternative": value["hypotheses"][0]["alternative"] = ""
    if problem == "no_hypothesis_reason": value["hypotheses"] = []
    if problem == "wrong_number": value["summary"] += "曝光上涨 999.9"
    if problem == "deployment": value["summary"] += "平台确认已上线"
    if problem == "unknown_path": value["summary"] += "unknown/location.rs"
    errors = validate_research(value, pack)
    if not errors:
        errors += validate_analysis(analysis_projection(value), pack)
    assert errors


def test_project_review_cache_and_quota_resume_remain_separate(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    client = RecordedClient([analysis(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, daily_limit=1, project_research=True)
    assert outputs[pack["item_id"]]["state"] == "pending"
    next_client = RecordedClient([review(pack)])
    resumed, resumed_events = analyze_items([pack], events, next_client, MODELS, NOW, PROMPTS, daily_limit=2, project_research=True)
    assert len(next_client.calls) == 1 and next_client.calls[0][0] == MODELS["review"]
    assert resumed[pack["item_id"]]["state"] == "pending_confirmation"
    assert "version-analysis-v1" in next(e for e in events if e["type"] == "model_call")["prompt_version"]
    assert "version-review-v1" in next(e for e in resumed_events if e["type"] == "model_call")["prompt_version"]


def test_project_review_disagreement_is_visible_and_never_confirmed(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    outputs, _ = analyze_items([pack], [], RecordedClient([analysis(pack), review(pack, False)]), MODELS, NOW, PROMPTS, project_research=True)
    event = research_event(pack, outputs[pack["item_id"]], MODELS)
    text = render_research(event)
    assert "复核存在分歧" in text
    assert "用户正文待修订" in text and "旧研究稿原文" in text and "模型实际读取的完整证据包" in text
    assert "新增来源中的内容可能获得参与排序的机会" not in text.split("研究附录：")[0]


def test_research_pipeline_publishes_unconfirmed_analysis_not_historical_issue_spam(sample_repo, tmp_path, monkeypatch):
    repo, result = prepare(sample_repo)
    workspace = tmp_path / "watch"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    shutil.copytree(PROMPTS, workspace / "prompts")
    config = workspace / "config/watch.yaml"
    config.parent.mkdir()
    config.write_text(yaml.safe_dump({"models": MODELS, "daily_calls": 50, "project_research": True, "extra_review_rounds": 2}), encoding="utf-8")
    RecordStore(Path("data")).append(result, NOW, "uncommitted")
    bundle = workspace / "bundle"
    manifest = detect(bundle, source=repo.path, research_commit=result.commit)
    assert manifest["research"] == [result.commit] and manifest["notifications"] == []
    assert RecordStore(bundle / "data").last_success() == result.commit
    pack = context_packs(bundle / "data")[0]
    output = workspace / "draft.json"
    model = RecordedClient([analysis(pack), review(pack), review(pack), review(pack)])
    run(bundle, output, config, client=model)
    client = FakeGitHub()
    publish(bundle, client, draft_bundle=output, push=False)
    page = (workspace / "versions" / filename(result.to_dict())).read_text(encoding="utf-8")
    tech_page = (workspace / "versions/technical" / filename(result.to_dict())).read_text(encoding="utf-8")
    assert "算法版本更新公告" in page and "推荐帖子来源设置有变化" in page
    assert "成立条件" in tech_page and "另一种解释" in tech_page
    assert "未人工确认" in page
    assert not client.rows and not (workspace / "digest.md").exists()
    assert pending_packs(workspace / "data") == []
    assert len(model.calls) == 4
    assert RecordStore(Path("data")).last_success() == result.commit
    payload = json.loads(output.read_text(encoding="utf-8"))
    # 可以直接读未确认推测；正式速报仍须单独的所有者确认。
    publish_issues(client, [result.to_dict()], False, payload["drafts"])
    client.rows[0]["labels"].append({"name": "已确认"})
    (workspace / "digest.md").write_text("# Digest\n", encoding="utf-8")
    event = {"action": "labeled", "sender": {"login": "owner"}, "label": {"name": "已确认"}, "repository": {"full_name": client.repository}, "issue": {"number": 1}}
    assert confirm(event, client, NOW, workspace, push=False) == "confirmed"
    text = (workspace / "versions" / filename(result.to_dict())).read_text(encoding="utf-8")
    assert "模型解读，已获所有者人工确认" in text
    assert text.count("## 算法版本更新公告") == 1
    assert "模型解读已人工确认" in (workspace / "versions/README.md").read_text(encoding="utf-8")


def test_context_identity_tampering_is_rejected(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    save_context(tmp_path, pack)
    path = tmp_path / "project-context" / (result.commit + ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["evidence"]["E1"]["text"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    from x_algo_watch.models import WatchError
    with pytest.raises(WatchError, match="摘要无效"):
        context_packs(tmp_path)


def test_source_contact_redaction_preserves_python_decorators(sample_repo, tmp_path):
    path, repo, base, commit = sample_repo
    source = path / "phoenix/xrex/models/example.py"
    source.parent.mkdir(parents=True)
    address = "contact" + "@" + "example.test"
    source.write_text("# " + address + "\n" + "@torch.compile" + "\ndef model(): pass\n", encoding="utf-8")
    result = compare(repo, base, commit(), [], date(2026, 10, 22))
    pack = project_pack(repo, result.to_dict())
    assert address not in json.dumps(pack)
    assert any("@torch.compile" in e["text"] and e["redacted_contacts"] for e in pack["evidence"].values())
    save_context(tmp_path, pack)
    assert context_packs(tmp_path) == [pack]


def test_large_source_context_reports_truncation_and_stays_bounded(sample_repo):
    path, repo, base, commit = sample_repo
    source = path / "phoenix/xrex/models/large.py"
    source.parent.mkdir(parents=True)
    source.write_text("// large upstream text\n" * 20_000, encoding="utf-8")
    result = compare(repo, base, commit(), [], date(2026, 10, 22))
    pack = project_pack(repo, result.to_dict())
    assert pack["coverage"]["text_characters"] <= MAX_TEXT
    assert any(e["truncated"] for e in pack["evidence"].values())
    assert "未通读整个仓库" in pack["limitations"][0]


def test_context_failure_preserves_detection_and_retries_from_durable_queue(sample_repo, tmp_path, monkeypatch):
    from x_algo_watch.models import WatchError
    repo, result = prepare(sample_repo)
    workspace = tmp_path / "watch"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "config").mkdir()
    (workspace / "config/watch.yaml").write_text("project_research: true\n", encoding="utf-8")
    RecordStore(Path("data")).append(result, NOW, "uncommitted")
    actual = project_pack
    def unavailable(*args):
        raise WatchError("暂不可用")
    monkeypatch.setattr("x_algo_watch.automation.project_pack", unavailable)
    bundle = workspace / "failed-context"
    manifest = detect(bundle, source=repo.path, research_commit=result.commit)
    assert manifest["research"] == [] and manifest["result_code"] == 0
    publish(bundle, FakeGitHub(), push=False)
    store = RecordStore(Path("data"))
    assert store.last_success() == result.commit
    assert {"research_context_requested", "research_context_deferred"} <= {e["type"] for e in store.events()}
    monkeypatch.setattr("x_algo_watch.automation.project_pack", actual)
    monkeypatch.setattr("x_algo_watch.automation.cli.main", lambda args: 0)
    later = detect(workspace / "retry", source=repo.path)
    assert later["research"] == [result.commit]


def test_project_grounding_understands_acronyms_chinese_boundaries_and_negation(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    pack["evidence"]["E1"]["text"] += "\nserde_json ttl_cache side_effect selected_candidates md5 sid"
    value = analysis_projection(analysis(pack))
    value["summary"] = "读取JSON，沿side_effect读取selected_candidates并观察TTL与MD5；不能据此断言线上已生效。"
    assert validate_analysis(value, pack, project_research=True) == []
    value["summary"] = "平台确认已上线，所有人一定会受影响。"
    assert validate_analysis(value, pack, project_research=True)


def test_range_examples_are_computed_from_literal_source_not_model_arithmetic():
    ranges = code_range_lengths({"E1": {"text": "candidates(10_000..10_220); candidates(0..440); float = 1.0..2; inclusive = 0..=5;"}})
    assert ranges == [{"evidence_ref": "E1", "start": 10000, "end_exclusive": 10220, "length": 220}, {"evidence_ref": "E1", "start": 0, "end_exclusive": 440, "length": 440}]


def test_actual_model_input_snapshot_survives_context_revision(sample_repo, tmp_path):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    save_context(tmp_path, pack)
    save_context_snapshot(tmp_path, pack)
    archived = tmp_path / "project-context" / (pack["commit"] + "-" + pack["item_id"] + ".json")
    previous = archived.read_bytes()
    pack["derived"]["new_context_fact"] = "additional evidence"
    pack["item_id"] = digest({k:v for k,v in pack.items() if k!='item_id'})
    save_context(tmp_path, pack)
    assert archived.read_bytes() == previous
    assert context_packs(tmp_path) == [pack]


@pytest.mark.parametrize("review_agrees", [True, False])
def test_project_run_with_two_candidate_limit_keeps_history(sample_repo, tmp_path, monkeypatch, review_agrees):
    repo, result = prepare(sample_repo)
    workspace = tmp_path / "watch"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    shutil.copytree(PROMPTS, workspace / "prompts")
    config = workspace / "config/watch.yaml"
    config.parent.mkdir()
    config.write_text(yaml.safe_dump({"models": MODELS, "project_research": True, "max_candidate_versions": 2}), encoding="utf-8")
    RecordStore(Path("data")).append(result, NOW, "uncommitted")
    bundle = workspace / "bundle"
    detect(bundle, source=repo.path, research_commit=result.commit)
    pack = context_packs(bundle / "data")[0]
    revised = analysis(pack)
    revised["summary"] = "按调用位置核对后，候选来源开关改变可能扩大参与排序的内容范围。"
    model = RecordedClient([analysis(pack), review(pack, False), revised, review(pack, review_agrees)])
    output = workspace / "draft.json"
    run(bundle, output, config, client=model)
    assert len(model.calls) == 4
    assert model.calls[2][2]["previous_analysis"] == analysis(pack)
    assert model.calls[2][2]["review_feedback"] == review(pack, False)
    publish(bundle, FakeGitHub(), draft_bundle=output, push=False)
    store = RecordStore(Path("data"))
    event = next(e for e in store.events() if e["type"] == "version_research")
    assert event["repair_attempted"] is True
    assert event["state"] == ("pending_confirmation" if review_agrees else "disputed")
    assert event["analysis"]["summary"] == revised["summary"]
    assert len([e for e in store.events() if e["type"] == "model_call"]) == 4
    assert pending_packs(Path("data")) == []


def test_repair_budget_resumes_from_corrected_analysis_without_repeating_calls(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    outputs, first = analyze_items([pack], [], RecordedClient([analysis(pack), review(pack, False)]), MODELS, NOW, PROMPTS, project_research=True)
    feedback = {pack["item_id"]: outputs[pack["item_id"]]}
    revised = analysis(pack)
    revised["summary"] = "修订后的机制推测仍以调用证据和线上开关为条件。"
    partial, calls = analyze_items([pack], first, RecordedClient([revised]), MODELS, NOW, PROMPTS, daily_limit=3, project_research=True, repair_feedback=feedback)
    assert partial[pack["item_id"]]["state"] == "pending"
    later = RecordedClient([review(pack)])
    done, _ = analyze_items([pack], first + calls, later, MODELS, NOW, PROMPTS, daily_limit=4, project_research=True, repair_feedback=feedback)
    assert len(later.calls) == 1 and later.calls[0][0] == MODELS["review"]
    assert done[pack["item_id"]]["state"] == "pending_confirmation"


def test_related_file_catalog_supports_real_verification_paths_without_inventing_code(sample_repo):
    path, repo, base, commit = sample_repo
    util = path / "home-mixer/util/video_carousel.rs"
    util.parent.mkdir(parents=True)
    util.write_text("pub fn select_videos() {}\n", encoding="utf-8")
    source = path / "home-mixer/filters/example_filter.rs"
    source.write_text("use crate::util::video_carousel;\n", encoding="utf-8")
    result = compare(repo, base, commit(), [], date(2026, 10, 22))
    pack = project_pack(repo, result.to_dict())
    catalog = next(e for e in pack["evidence"].values() if e["kind"] == "related_path_catalog")
    assert "home-mixer/util/video_carousel.rs" in catalog["text"]
    assert "只证明文件存在" in catalog["text"]
    assert "fake/location.rs" not in catalog["text"]


def test_live_fair_share_and_hydrator_failure_implementations_are_complete(upstream):
    target = upstream.resolve("989fb91254d461bf4b9df53fb12d5c9a61c2b354")
    result = compare(upstream, upstream.parent(target), target, [], date(2026, 10, 23))
    pack = project_pack(upstream, result.to_dict())
    required = {
        "home-mixer/filters/fair_share_ranking_filter.rs": "fn selection_mask",
        "home-mixer/candidate_hydrators/new_user_soft_nsfw_label_hydrator.rs": "fn update(",
        "candidate-pipeline/hydrator.rs": "fn update_all",
        "candidate-pipeline/candidate_pipeline.rs": "async fn execute_stages",
        "home-mixer/candidate_hydrators/in_network_candidate_hydrator.rs": "candidate.in_network = hydrated.in_network",
    }
    for path, symbol in required.items():
        entry = next(e for e in pack["evidence"].values() if e["path"] == path and e.get("scope") == "complete_production_before_tests")
        assert symbol in entry["text"] and not entry["truncated"]
    calls = "\n".join(e["text"] for e in pack["evidence"].values() if e["kind"] == "core_call_sites")
    assert "Box::new(FairShareRankingFilter)" in calls
    assert "Box::new(NewUserSoftNsfwFilter)" in calls
    assert pack["coverage"]["text_characters"] <= MAX_TEXT


def test_required_implementation_budget_stops_context_instead_of_clipping(sample_repo, monkeypatch):
    from x_algo_watch.models import WatchError
    path, repo, base, commit = sample_repo
    logic = path / "home-mixer/filters/example_filter.rs"
    logic.write_text("pub fn important() {}\n" * 100, encoding="utf-8")
    result = compare(repo, base, commit(), [], date(2026, 10, 23))
    monkeypatch.setattr("x_algo_watch.project_evidence.MAX_TEXT", 200)
    with pytest.raises(WatchError, match="核心实现"):
        project_pack(repo, result.to_dict())


def test_explicit_research_target_selects_only_that_version_even_beyond_first_batch(sample_repo, tmp_path):
    from x_algo_watch.models import CheckResult
    repo, result = prepare(sample_repo)
    template = project_pack(repo, result.to_dict())
    working = tmp_path / "working"
    for index, letter in enumerate("bcde"):
        item = json.loads(json.dumps(template))
        item["commit"] = letter * 40
        item["committed_at"] = f"2026-10-{index+1:02d}T00:00:00+00:00"
        item["item_id"] = digest({k:v for k,v in item.items() if k!='item_id'})
        RecordStore(working).append(CheckResult("a"*40, item["commit"], item["committed_at"]), NOW, "uncommitted")
        save_context(working, item)
    manifest = write_bundle(tmp_path / "bundle", {}, working, test=False, code="uncommitted", result_code=0, research_only="b"*40)
    assert manifest["research"] == ["b"*40]


def test_explicit_target_can_rerun_previously_agreed_research(sample_repo, tmp_path):
    repo, result = prepare(sample_repo); pack = project_pack(repo, result.to_dict())
    working = tmp_path / "working"
    RecordStore(working).append(result, NOW, "uncommitted")
    save_context(working, pack)
    event = research_event(pack, {"state":"pending_confirmation", "analysis":analysis(pack), "review":review(pack)}, MODELS)
    RecordStore(working).append_event(event, NOW)
    assert pending_packs(working) == []
    manifest = write_bundle(tmp_path / "bundle", {}, working, test=False, code="uncommitted", result_code=0, research_only=pack["commit"])
    assert manifest["research"] == [pack["commit"]]


def test_detection_summary_keeps_complete_long_list_deltas_without_raw_json_cut():
    from x_algo_watch.project_evidence import detection_summary
    before = ['RULE_' + str(i) for i in range(100)]
    after = before[1:] + ['NEW_RULE']
    record = {'findings': [{'category':'visibility', 'path':'rules.rs', 'name':'HOME', 'before':before, 'after':after}]}
    row = detection_summary(record)['findings'][0]
    assert row['added'] == ['NEW_RULE'] and row['removed'] == ['RULE_0']
    assert row['before_count'] == row['after_count'] == 100
    assert not row['order_changed']


def test_latest_context_has_complete_rule_delta_and_does_not_prioritize_unrelated_video(upstream):
    target = upstream.resolve('07d159b69109f069c85f84d39dd38e38adc040f0')
    result = compare(upstream, upstream.parent(target), target, [], date(2026, 10, 24))
    pack = project_pack(upstream, result.to_dict())
    first = pack['evidence']['E1']
    assert not first['truncated']
    facts = json.loads(first['text'])['findings']
    home = next(row for row in facts if row['name'] == 'TIMELINE_HOME_HYDRATION')
    assert home['before_count'] == 91 and home['after_count'] == 93
    assert home['added']
    assert 'home-mixer/filters/video_carousel_filter.rs' not in pack['coverage']['complete_implementation_paths']
    assert len(pack['coverage']['diff_files_included']) > 1
