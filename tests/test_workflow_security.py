from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_actions_pinned_and_no_untrusted_script_interpolation():
    for name in ("check.yml", "confirm.yml"):
        workflow = read(name)
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["concurrency"] == {"group": "x-algo-watch-main", "cancel-in-progress": "false"}
        assert "pull_request_target" not in workflow["on"]
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
                if "run" in step:
                    assert "${{" not in step["run"]


def test_key_is_only_available_to_draft_call_step():
    workflow = read("check.yml")
    assert workflow["on"]["schedule"] == [{"cron": "17 1-22/3 * * *"}]
    for name, job in workflow["jobs"].items():
        if name == "draft":
            assert job["environment"] == "openrouter"
            assert job["permissions"] == {"contents": "read"}
            assert "refs/heads/main" in job["if"] and "DRAFT_ENABLED" in job["if"]
            key_steps = [step for step in job["steps"] if "OPENROUTER_API_KEY" in step.get("env", {})]
            assert len(key_steps) == 1 and "x_algo_watch.drafting" in key_steps[0]["run"]
        else:
            assert "environment" not in job
            assert "secrets." not in str(job)
    assert "secrets." not in str(read("confirm.yml"))


def test_confirm_job_restricted_to_owner_and_two_labels():
    workflow = read("confirm.yml")
    assert workflow["on"] == {"issues": {"types": ["labeled"]}}
    condition = workflow["jobs"]["confirm"]["if"]
    assert "github.actor == github.repository_owner" in condition
    assert "已确认" in condition and "驳回" in condition
