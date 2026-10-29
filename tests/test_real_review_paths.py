import json
from copy import deepcopy
from pathlib import Path

from x_algo_watch.mechanism_reviews import validate_review


def test_actual_review_prefix_error_keeps_objections_and_gives_resolvable_path():
    root = Path(__file__).resolve().parents[1]
    commit = "6258a1549ef808be88fba83d3b2d58364b587ee0"
    calls = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "data/research-runs" / commit).glob("*.json")]
    bad = next(c for c in calls if (c.get("output") or {}).get("issues") and
               any(i["field"].startswith("/analysis/segments/") for i in c["output"]["issues"]))
    draft = next(c["output"] for c in calls if c.get("item_id") == bad["item_id"] and c.get("valid") and c.get("stage") == "mechanism")
    task_file = next((root / "data/research-plans").glob(commit + "-*/tasks/" + bad["item_id"] + ".json"))
    task = json.loads(task_file.read_text(encoding="utf-8"))
    original = deepcopy(bad["output"])
    errors = validate_review(original, bad["item_id"], draft, task)
    assert any("可解析的候选路径：/segments/" in e for e in errors)
    assert original == bad["output"]
    fixed = deepcopy(original)
    for issue in fixed["issues"]:
        issue["field"] = issue["field"].removeprefix("/analysis")
    assert validate_review(fixed, bad["item_id"], draft, task) == []
    assert not fixed["agree"] and fixed["issues"]
