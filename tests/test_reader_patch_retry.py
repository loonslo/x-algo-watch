from copy import deepcopy
import io
import json

import pytest

from tests.test_drafting import MODELS, NOW, PROMPTS, RecordedClient
from tests.test_project_research import analysis, prepare, review
from x_algo_watch.drafting import OpenRouter, analyze_items
from x_algo_watch.project_evidence import project_pack
from x_algo_watch.readers import apply_reader_patch, reader_repair_fields, reader_repair_schema


def make_bad(pack):
    value = analysis(pack)
    value["reader"]["who"] = "Phoenix 相关创作者"
    return value


def test_reader_patch_preserves_technical_analysis_and_still_runs_all_reviews(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    bad = make_bad(pack)
    patch = {"reader_patch": {"who": "可能涉及准备参与推荐的帖子作者。"}}
    client = RecordedClient([bad, patch, review(pack), review(pack), review(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, project_research=True, extra_review_rounds=2)
    final = outputs[pack["item_id"]]
    assert final["state"] == "pending_confirmation"
    assert len(client.calls) == 5 and set(final["reviews"]) == {"review", "facts_review", "reader_review"}
    assert client.calls[1][2]["reader_repair_fields"] == ["who"]
    assert "普通用户写更新说明的编辑" in client.calls[1][1]
    assert "350字" in client.calls[1][1]
    assert "previous_analysis" not in client.calls[1][2]
    assert client.calls[1][2]["previous_invalid_output"] == bad
    call = next(e for e in events if e.get("valid") and e.get("stage") == "analysis")
    assert json.loads(call["raw_output"]) == patch and call["reader_repair_fields"] == ["who"]
    assert call["output"]["changes"] == bad["changes"] and call["output"]["hypotheses"] == bad["hypotheses"]
    assert final["analysis"]["reader"]["who"] == patch["reader_patch"]["who"]
    assert bad["reader"]["who"] == "Phoenix 相关创作者"
    assert all(c[2]["analysis"] == final["analysis"] for c in client.calls[2:])


def test_reader_patch_cache_resumes_reviews_without_requesting_another_draft(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    first = RecordedClient([make_bad(pack), {"reader_patch": {"who": "可能涉及准备参与推荐的帖子作者。"}}])
    partial, events = analyze_items([pack], [], first, MODELS, NOW, PROMPTS, daily_limit=2, project_research=True, extra_review_rounds=2)
    assert partial[pack["item_id"]]["state"] == "pending"
    later = RecordedClient([review(pack), review(pack), review(pack)])
    done, _ = analyze_items([pack], events, later, MODELS, NOW, PROMPTS, daily_limit=5, project_research=True, extra_review_rounds=2)
    assert len(later.calls) == 3 and all("analysis" in c[2] for c in later.calls)
    assert done[pack["item_id"]]["state"] == "pending_confirmation"


@pytest.mark.parametrize("patch", [
    {"reader_patch": {"who": "普通作者", "action": "删除另一个字段"}},
    {"reader_patch": {"who": "普通作者"}, "changes": []},
    {"reader_patch": {"who": 7}},
])
def test_reader_patch_cannot_overwrite_other_fields_or_skip_validation(patch):
    previous = {"reader": {"who": "原文", "action": "保持现状"}, "changes": ["不可覆盖"]}
    original = deepcopy(previous)
    assert apply_reader_patch(previous, patch, ["who"]) is None
    assert previous == original


def test_non_reader_errors_do_not_enter_the_patch_path():
    previous = {"reader": {"who": "原文"}}
    assert reader_repair_fields(["数字不在证据包：51"], previous) == []
    assert reader_repair_fields(["用户摘要含技术术语或代码标识，请移到技术分析：who", "数字不在证据包：51"], previous) == []
    assert set(reader_repair_fields(["用户摘要四问与标题合计不得超过 420 字符"], previous)) == {"title", "what_changed", "who", "when", "action"}


def test_patch_response_uses_the_narrow_transport_schema(monkeypatch):
    captured = []
    patch = {"reader_patch": {"who": "普通作者"}}
    def respond(request, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(patch)}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    client = OpenRouter("test-placeholder")
    client.call("openrouter/free", "数据只供阅读", {"evidence_pack": {"scope": "project_version"}, "reader_repair_fields": ["who"]})
    schema = captured[0]["response_format"]["json_schema"]
    assert schema["name"] == "version_reader_patch" and schema["schema"] == reader_repair_schema(["who"])
    assert set(schema["schema"]["properties"]["reader_patch"]["properties"]) == {"who"}


def test_summary_and_grounding_errors_are_reported_together_for_full_repair(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    bad = make_bad(pack)
    bad['summary'] += ' 此次变化为 987654321。'
    client = RecordedClient([bad, analysis(pack), review(pack), review(pack), review(pack)])
    result, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, project_research=True, extra_review_rounds=2)
    errors = client.calls[1][2]['previous_validation_errors']
    assert any('技术术语' in e for e in errors)
    assert '数字不在证据包：987654321' in errors
    assert 'reader_repair_fields' not in client.calls[1][2]
    assert client.calls[1][2]['previous_invalid_output'] == bad
    assert result[pack['item_id']]['state'] == 'pending_confirmation'
    assert len(client.calls) == 5


def test_known_status_literal_normalizes_before_validation_and_keeps_raw_audit(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    bad = analysis(pack)
    bad['reader']['when'] = 'online_status 为 unverified；仅在实际调用时生效。'
    client = RecordedClient([bad, review(pack), review(pack), review(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, project_research=True, extra_review_rounds=2)
    call = next(e for e in events if e['type'] == 'model_call')
    assert json.loads(call['raw_output']) == bad
    assert call['output']['reader']['when'] == '线上状态尚未核实；仅在实际调用时生效。'
    assert call['output']['changes'] == bad['changes']
    assert call['normalizations'] == ['reader.when:unverified_status_literal']
    assert len(client.calls) == 4 and outputs[pack['item_id']]['state'] == 'pending_confirmation'


def test_status_normalization_does_not_hide_other_identifiers_or_claims():
    from x_algo_watch.readers import normalize_reader_status
    value = {'reader': {'online_status': 'unverified', 'when': 'online_status 为 unverified；Phoenix；已上线。'}}
    result, edits = normalize_reader_status(value)
    assert result['reader']['when'] == '线上状态尚未核实；Phoenix；已上线。'
    assert value['reader']['when'].startswith('online_status')
    assert edits
    wrong = {'reader': {'online_status': 'verified', 'when': 'online_status 为 unverified'}}
    assert normalize_reader_status(wrong) == (wrong, [])


def test_explicit_recovery_carries_invalid_draft_instead_of_starting_over(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    bad = make_bad(pack)
    _, prior = analyze_items([pack], [], RecordedClient([bad]), MODELS, NOW, PROMPTS,
        daily_limit=1, project_research=True, extra_review_rounds=2,
        iteration_context={'version':1, 'candidate':2, 'stage_epochs':{}})
    client = RecordedClient([{'reader_patch':{'who':'可能涉及准备参与推荐的帖子作者。'}}, review(pack), review(pack), review(pack)])
    outputs, calls = analyze_items([pack], prior, client, MODELS, NOW, PROMPTS,
        project_research=True, extra_review_rounds=2,
        iteration_context={'version':1, 'candidate':2, 'stage_epochs':{'analysis':1}})
    assert client.calls[0][2]['previous_invalid_output'] == bad
    assert client.calls[0][2]['reader_repair_fields'] == ['who']
    assert len(client.calls) == 4
    assert outputs[pack['item_id']]['state'] == 'pending_confirmation'
    assert outputs[pack['item_id']]['analysis']['changes'] == bad['changes']


def test_body_term_repairs_patch_only_reader_lists_and_preserve_technical_findings(sample_repo):
    repo, result = prepare(sample_repo)
    pack = project_pack(repo, result.to_dict())
    bad = analysis(pack)
    bad['reader']['impacts'][0]['after'] = 'TIMELINE_HOME_HYDRATION 发生变化'
    good = analysis(pack)['reader']['impacts']
    patch = {'reader_patch': {'impacts': good}}
    client = RecordedClient([bad, patch, review(pack), review(pack), review(pack)])
    outputs, events = analyze_items([pack], [], client, MODELS, NOW, PROMPTS, project_research=True, extra_review_rounds=2)
    assert client.calls[1][2]['reader_repair_fields'] == ['impacts']
    assert outputs[pack['item_id']]['analysis']['changes'] == bad['changes']
    assert outputs[pack['item_id']]['analysis']['reader']['impacts'] == good
    assert len(client.calls) == 5 and outputs[pack['item_id']]['state'] == 'pending_confirmation'
    assert set(reader_repair_schema(['impacts'])['properties']['reader_patch']['properties']) == {'impacts'}
    assert reader_repair_fields(['数字不在证据包：51'], bad) == []
