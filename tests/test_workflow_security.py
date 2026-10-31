from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_actions_pinned_and_no_untrusted_script_interpolation():
    for name in ("check.yml", "sample.yml"):
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
            assert job["permissions"] == {"contents": "read", "actions": "read"}
            assert "refs/heads/main" in job["if"] and "DRAFT_ENABLED" in job["if"]
            key_steps = [step for step in job["steps"] if "OPENROUTER_API_KEY" in step.get("env", {})]
            assert len(key_steps) == 1 and "x_algo_watch.drafting" in key_steps[0]["run"]
        else:
            assert "environment" not in job
            assert "secrets." not in str(job)

def test_sample_job_is_read_only_owner_main_and_key_only_in_model_step():
    workflow = read('sample.yml')
    assert set(workflow['on']) == {'workflow_dispatch'}
    job = workflow['jobs']['draft']
    assert job['permissions'] == {'contents':'read', 'actions':'read'} and job['environment']=='openrouter'
    assert 'github.actor == github.repository_owner' in job['if'] and 'refs/heads/main' in job['if']
    key_steps=[step for step in job['steps'] if 'OPENROUTER_API_KEY' in step.get('env',{})]
    assert len(key_steps)==1 and 'x_algo_watch.drafting' in key_steps[0]['run']
    assert not any('x_algo_watch.automation publish' in step.get('run','') for step in job['steps'])
    assert workflow['on']['workflow_dispatch']['inputs']['direct_probe']['default'] == 'false'
    assert 'x_algo_watch.model_probe' in key_steps[0]['run']
    assert key_steps[0]['env']['DIRECT_PROBE'] == '${{ inputs.direct_probe }}'


def test_queued_detection_uses_fresh_main_and_draft_uses_that_source():
    jobs = read("check.yml")["jobs"]
    detect = jobs["detect"]
    checkout = next(s for s in detect["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    source = next(s for s in detect["steps"] if s.get("id") == "source")
    assert "git rev-parse HEAD" in source["run"]
    assert detect["outputs"]["source_commit"] == "${{ steps.source.outputs.commit }}"
    draft = next(s for s in jobs["draft"]["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert draft["with"]["ref"] == "${{ needs.detect.outputs.source_commit }}"
    publish = next(s for s in jobs["publish"]["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert publish["with"]["ref"] == "main"


def test_interrupted_draft_keeps_available_audit_artifact_for_publish():
    jobs = read("check.yml")["jobs"]
    audit = next(s for s in jobs["draft"]["steps"] if s.get("id") == "audit_state")
    upload = next(s for s in jobs["draft"]["steps"] if s.get("with", {}).get("name") == "draft")
    download = next(s for s in jobs["publish"]["steps"] if s.get("with", {}).get("name") == "draft")
    assert audit["if"] == "always()"
    assert "always()" in upload["if"] and "audit_state.outputs.available" in upload["if"]
    assert "audit_available" in download["if"] and "draft.result" not in download["if"]
    assert jobs["draft"]["outputs"]["audit_available"] == "${{ steps.audit_state.outputs.available }}"


def test_every_model_workflow_reconciles_budget_and_uploads_intents_even_on_failure():
    for name in ("check.yml", "sample.yml"):
        job = read(name)["jobs"]["draft"]
        steps = job["steps"]
        budget = next(s for s in steps if "x_algo_watch.call_budget" in s.get("run", ""))
        model = next(s for s in steps if "x_algo_watch.drafting" in s.get("run", ""))
        ledger = next(s for s in steps if s.get("with", {}).get("name", "").startswith("call-ledger-"))
        assert steps.index(budget) < steps.index(model)
        assert budget["env"] == {"GH_TOKEN": "${{ github.token }}"}
        assert "--budget-receipt runtime/budget.json" in model["run"]
        assert "--call-ledger runtime/call-ledger.json" in model["run"]
        assert ledger["if"] == "always()" and ledger["with"]["path"] == "runtime/call-ledger.json"
        assert "github.run_attempt" in ledger["with"]["name"]
