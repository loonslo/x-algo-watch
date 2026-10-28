"""保存的真实失败输出仍被拒绝，并得到可执行的转录反馈。"""

import json
from pathlib import Path

from x_algo_watch.mechanisms import validate_mechanisms


def test_saved_6258_outputs_remain_invalid_with_precise_repair_feedback():
    root = Path(__file__).resolve().parents[1]
    commit = "6258a1549ef808be88fba83d3b2d58364b587ee0"
    plan = "fdaa5b4682193089bdf3869f144476c32e7bbd85dff35760dedc2ff4d665f17c"
    calls = [json.loads(p.read_text(encoding="utf-8")) for p in
             (root / "data/research-runs" / commit).glob("*.json")]
    failed = [c for c in calls if c.get("type") == "model_call" and c.get("outcome") == "invalid"]
    assert len(failed) >= 2
    for call in failed[:2]:
        task_path = root / "data/research-plans" / f"{commit}-{plan}" / "tasks" / (call["item_id"] + ".json")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        errors = validate_mechanisms(call["output"], task)
        assert any('"unexpected"' in error for error in errors)
        assert any('"exact_quote_for_selected_lines"' in error for error in errors)
        assert any("引文与具体源码行不一致" in error for error in errors)
