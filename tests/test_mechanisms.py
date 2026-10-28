from copy import deepcopy
import json
from pathlib import Path

import pytest

from x_algo_watch.mechanisms import bind_claims, mechanism_schema, validate_mechanisms
from x_algo_watch.research_plan import fragment, task


def example():
    base, commit = "a" * 40, "b" * 40
    old = fragment("params.rs", base, "const ENABLED: bool = false;\n", 1, "before_implementation", "complete_file")
    new = fragment("params.rs", commit, "const ENABLED: bool = true;\n", 1, "after_implementation", "complete_file")
    call = fragment("pipeline.rs", commit, "if params.enabled { pipeline.push(Filter); }\n", 5, "definition_or_reference", "reference_window")
    item = task("params.rs", base, commit, [old, new, call], context_complete=True)
    cite = lambda ref, line, quote: {"evidence_ref": ref, "start_line": line, "end_line": line, "quote": quote}
    before = cite("E1", 1, old["text"])
    after = cite("E2", 1, new["text"])
    entry = cite("E3", 5, call["text"])
    claim = {"key": "filter_activation", "stage": "filtering", "verdict": "mechanism_hypothesis", "execution_scope": "production_path",
             "before": {"known": True, "behavior": "默认关闭", "citations": [before]},
             "after": {"known": True, "behavior": "默认开启", "citations": [after]},
             "entrypoint": {"text": "按设置注册过滤器", "citations": [entry]},
             "activation": {"text": "读取启用设置", "citations": [entry, after]},
             "intermediate_effect": "运行路径可能加入过滤步骤", "user_effect_gap": "过滤条件和最终展示尚未给出",
             "support": [after, entry], "counter_evidence": [], "unknowns": ["线上实际设置未知"],
             "alternative": "线上覆盖默认值", "verify": "检查实际配置和筛选实现", "allowed_reader_claim": "启用且被调用时，准备推荐的帖子可能多经过一次筛选。"}
    return item, {"task_id": item["task_id"], "claims": [claim], "segments": [{"segment_id": item["task_id"], "disposition": "researched", "reason": "已核对所提供前后实现，限制保留在未知项中"}]}


def test_exact_quotes_and_two_versions_support_mechanism_and_stable_identity():
    item, value = example()
    assert not validate_mechanisms(value, item)
    original = bind_claims(value, item, "old-plan")[0]
    revised = deepcopy(value)
    revised["claims"][0]["alternative"] = "线上可能仍采用旧设置"
    updated = bind_claims(revised, item, "new-plan")[0]
    assert original["claim_id"] == updated["claim_id"]
    assert original["plan_id"] != updated["plan_id"]


@pytest.mark.parametrize("failure", ["unsupported_quote", "wrong_revision", "line_outside", "unknown_ref", "duplicate_key", "omitted_segment", "contradicted_if", "mock", "missing_entrypoint"])
def test_invalid_grounding_cannot_be_repaired_by_valid_reference_numbers_or_conditionals(failure):
    item, value = example()
    claim = value["claims"][0]
    if failure == "unsupported_quote": claim["support"][0]["quote"] = "const ENABLED: bool = false;"
    if failure == "wrong_revision": claim["before"]["citations"] = claim["after"]["citations"]
    if failure == "line_outside": claim["support"][0]["end_line"] = 500
    if failure == "unknown_ref": claim["support"][0]["evidence_ref"] = "E999"
    if failure == "duplicate_key": value["claims"].append(deepcopy(claim))
    if failure == "omitted_segment": value["segments"] = []
    if failure == "contradicted_if":
        claim["verdict"] = "contradicted"
        claim["counter_evidence"] = deepcopy(claim["support"])
        claim["allowed_reader_claim"] = "若生产采用这个测试值，用户可能受到影响。"
    if failure == "mock": claim["execution_scope"] = "mock"
    if failure == "missing_entrypoint": claim["entrypoint"]["citations"] = []
    assert validate_mechanisms(value, item)


def test_unknown_previous_behavior_and_no_user_claim_are_valid_without_forcing_advice():
    item, value = example()
    claim = value["claims"][0]
    claim.update(verdict="insufficient_evidence", execution_scope="unknown", allowed_reader_claim="")
    claim["before"] = {"known": False, "behavior": "未提供旧调用条件，不能推断以前没有过滤", "citations": []}
    assert not validate_mechanisms(value, item)


def test_mock_only_support_is_rejected_even_when_model_claims_production():
    item, value = example()
    for entry in item["evidence"].values():
        entry["purpose"] = "mock_or_fixture"
    assert any("仅依赖" in error for error in validate_mechanisms(value, item))


def test_published_mechanism_protocol_matches_program_schema():
    path = Path(__file__).resolve().parents[1] / "schemas/mechanism-research-v1.json"
    assert json.loads(path.read_text(encoding="utf-8")) == mechanism_schema()


def test_supporting_architecture_cannot_be_labeled_as_this_versions_user_change():
    item, value = example()
    item["change_scope"] = "supporting_context"
    assert any("共享架构" in error for error in validate_mechanisms(value, item))
