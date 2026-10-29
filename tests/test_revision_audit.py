from pathlib import Path

from x_algo_watch.revision_audit import audit_revisions, field_changes
from x_algo_watch.storage import RecordStore


def candidate(number, issues, **analysis):
    return {"number": number, "result": {"analysis": analysis, "review": {"agree": not issues, "issues": issues}}}


def test_reappearing_issue_is_a_regression_without_inventing_closure_reason():
    issue = {"field": "reader", "reason": "测试阈值不能推成生产变化", "evidence_refs": ["E1"]}
    history = [candidate(1, [issue], title="旧稿"), candidate(2, [], title="已撤回推断"), candidate(3, [issue], title="旧推断回来了")]
    audit = audit_revisions(history)
    assert audit["steps"][1]["not_repeated"][0]["closure_reason"] is None
    assert audit["steps"][2]["regressions"] and audit["steps"][2]["issue_count_increased"]
    assert audit["steps"][2]["causal_attribution"] == "undetermined"


def test_claim_identity_aligns_reordered_claims_and_tracks_precise_affected_field():
    old = [{"claim_id": "C-one", "activation": {"text": "关闭"}}, {"claim_id": "C-two", "activation": {"text": "未知"}}]
    new = [{"claim_id": "C-two", "activation": {"text": "未知"}}, {"claim_id": "C-one", "activation": {"text": "开启"}}]
    changes = field_changes({"claims": old}, {"claims": new})
    assert len(changes) == 1 and changes[0]["field"] == "/claims/C-one/activation/text"
    assert changes[0]["before"] == "关闭" and changes[0]["after"] == "开启"


def test_real_6258_comparison_preserves_rebound_and_exposes_exact_reader_edits():
    root = Path(__file__).resolve().parents[1]
    event = [e for e in RecordStore(root / "data").events() if e["type"] == "version_research" and e["commit"].startswith("6258a15") and e.get("iteration", {}).get("history")][-1]
    result = audit_revisions(event["iteration"]["history"])
    assert [step["issue_count"] for step in result["steps"]] == [11, 10, 1, 9]
    assert result["steps"][-1]["issue_count_increased"]
    assert any(change["field"].startswith("/reader/") for change in result["steps"][-1]["changed_fields"])
    assert all(step["causal_attribution"] == "undetermined" for step in result["steps"])
