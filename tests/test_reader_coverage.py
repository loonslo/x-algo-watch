from copy import deepcopy

from x_algo_watch.reader_pipeline import validate_reader_review


def test_reader_coverage_requires_existing_claims_and_never_fetches_new_evidence():
    subject = {"reader": {"summary": "摘要"}}
    issue = {"claim_id": "C-one", "field": "/reader/summary", "problem_key": "omitted-condition",
             "category": "coverage", "reason": "漏写已给条件", "evidence_refs": ["C-one"], "evidence_requests": []}
    review = {"item_id": "item", "agree": False, "issues": [issue]}
    assert validate_reader_review(review, "item", subject, {"C-one": {}}) == []
    bad = deepcopy(review)
    bad["issues"][0]["evidence_refs"] = []
    assert validate_reader_review(bad, "item", subject, {"C-one": {}})
    bad = deepcopy(review)
    bad["issues"][0]["evidence_requests"] = [{"path": "a.rs", "version": "after", "start_line": 1, "end_line": 2, "reason": "补取"}]
    assert any("覆盖遗漏" in e for e in validate_reader_review(bad, "item", subject, {"C-one": {}}))
