from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import zipfile

import pytest

from x_algo_watch.call_budget import BudgetClient, archive_json, call_rows, collect_budget, load_receipt, merge_budget
from x_algo_watch.evidence import digest
from x_algo_watch.models import WatchError
from x_algo_watch.stage_calls import call_stage
from tests.test_drafting import RecordedClient

NOW = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)


def zipped(value, name="call-ledger.json"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo()
        entry.filename = name
        archive.writestr(entry, json.dumps(value))
    return stream.getvalue()


def row(identifier="paid", day="2026-10-28"):
    return {"type": "model_call", "id": identifier, "day": day}


def receipt(now=NOW, calls=None):
    value = {"schema_version": 1, "repository": "owner/watch", "run_id": 90, "attempt": 1,
             "checked_at": now.isoformat(), "day": now.date().isoformat(), "calls": calls or [], "receipts": []}
    value["receipt_id"] = digest(value)
    return value


class Artifacts:
    repository = "owner/watch"
    owner = "owner"

    def __init__(self, *, failed=True, missing=False, rerun=False, active=False, legacy=False):
        self.failed, self.missing, self.rerun, self.active, self.legacy = failed, missing, rerun, active, legacy
        self.requests = []
        self.downloaded = []

    def request(self, method, path):
        assert method == "GET"
        self.requests.append(path)
        if "/workflows/" in path:
            sample = "sample.yml" in path
            run = {"id": 10 if sample else 20, "run_attempt": 2 if self.rerun else 1,
                   "path": ".github/workflows/" + ("sample.yml" if sample else "check.yml"),
                   "head_branch": "main", "head_repository": {"full_name": self.repository},
                   "event": "workflow_dispatch", "actor": {"login": "owner"},
                   "updated_at": NOW.isoformat(), "status": "completed"}
            return {"total_count": 1, "workflow_runs": [run]}
        if "/jobs?" in path:
            sample = "/10/" in path
            job = {"name": "draft", "status": "in_progress" if self.active else "completed",
                   "conclusion": "failure" if self.failed else "success",
                   "started_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
                   "steps": [{"name": "Iterate drafts and run all three review roles" if sample else "Analyze and review evidence",
                              "status": "completed", "conclusion": "failure" if self.failed else "success"}]}
            return {"total_count": 1, "jobs": [job]}
        if "/artifacts?" in path:
            rid = 10 if "/10/" in path else 20
            rows = []
            for number in range(1, (2 if self.rerun else 1) + 1):
                name = ("reviewed-sample" if rid == 10 else "draft") if self.legacy else f"call-ledger-{rid}-{number}"
                if not self.missing:
                    rows.append({"name": name, "id": rid + number, "expired": False,
                                 "digest": "sha256:" + hashlib.sha256(self.raw(rid + number)).hexdigest()})
            return {"total_count": len(rows), "artifacts": rows}
        raise AssertionError(path)

    def raw(self, aid):
        rid = 10 if aid < 20 else 20
        number = aid - rid
        if self.legacy:
            return zipped({"events": [row(str(aid)), {"type": "version_research", "state": "reviewed"}]}, "runtime/draft.json")
        return zipped({"schema_version": 1, "repository": self.repository, "run_id": rid, "attempt": number,
                       "calls": [row(str(aid)), row("yesterday", "2026-10-27")]})

    def artifact_zip(self, aid, maximum):
        self.downloaded.append(aid)
        return self.raw(aid)


def test_failed_main_and_unmerged_sample_are_counted_without_importing_conclusions():
    source = Artifacts()
    result = collect_budget(source, run_id=90, attempt=1, now=NOW)
    assert {r["id"] for r in result["calls"]} == {"11", "21"}
    assert source.downloaded == [21, 11]
    assert all(set(r) == {"type", "id", "day"} for r in result["calls"])
    full = {**row("11"), "valid": True, "output": {"not": "a publication proof"}}
    merged = merge_budget([full], result["calls"])
    assert len(merged) == 2 and merged[0] is full
    assert result["receipt_id"] == digest({k: v for k, v in result.items() if k != "receipt_id"})


def test_every_rerun_attempt_counts_and_current_attempt_does_not_read_itself():
    source = Artifacts(rerun=True)
    result = collect_budget(source, run_id=20, attempt=2, now=NOW)
    assert {r["id"] for r in result["calls"]} == {"11", "12", "21"}
    assert 22 not in source.downloaded


@pytest.mark.parametrize("options", [{"missing": True}, {"active": True}, {"legacy": True}, {"legacy": True, "failed": False, "rerun": True}])
def test_unknown_spend_never_becomes_zero(options):
    with pytest.raises(WatchError):
        collect_budget(Artifacts(**options), run_id=90, attempt=1, now=NOW)


def test_legacy_success_counts_all_models_and_failed_calls_without_reusing_old_drafts():
    result = collect_budget(Artifacts(legacy=True, failed=False), run_id=90, attempt=1, now=NOW)
    assert len(result["calls"]) == 2
    assert all(r["kind"] == "legacy_success" for r in result["receipts"])


@pytest.mark.parametrize("filename", ["../call-ledger.json", "/call-ledger.json", "C:/call-ledger.json", "bad\\call-ledger.json"])
def test_artifact_paths_are_never_extracted_or_trusted(filename):
    with pytest.raises(WatchError):
        archive_json(zipped({"calls": []}, filename), ledger=True)


def test_bad_digest_wrong_run_and_foreign_branch_stop_collection():
    for mutation in ("digest", "run", "count"):
        source = Artifacts()
        request = source.request
        def changed(method, path):
            value = request(method, path)
            if mutation == "digest" and "artifacts" in value:
                value["artifacts"][0]["digest"] = "sha256:" + "0" * 64
            if mutation == "branch" and "workflow_runs" in value:
                value["workflow_runs"][0]["head_branch"] = "other"
            if mutation == "count":
                value["total_count"] += 1
            return value
        source.request = changed
        if mutation == "run":
            source.artifact_zip = lambda aid, maximum: zipped({"schema_version": 1, "repository": "owner/watch", "run_id": 999, "attempt": 1, "calls": []})
        with pytest.raises(WatchError):
            collect_budget(source, run_id=90, attempt=1, now=NOW)


def test_intent_survives_interrupt_and_next_run_cannot_spend_again(tmp_path):
    now = datetime.now(timezone.utc)
    value = receipt(now)
    path = tmp_path / "call-ledger.json"
    class Interrupt:
        def call(self, *args):
            raise KeyboardInterrupt()
    client = BudgetClient(Interrupt(), value, path, [], limit=1)
    with pytest.raises(KeyboardInterrupt):
        call_stage(unit_id="u", commit="a" * 40, stage="analysis", payload={}, schema={}, validate=lambda v: [],
                   prompt="p", prior=[], client=client, now=now)
    paid = json.loads(path.read_text())["calls"]
    assert len(paid) == 1
    transport = RecordedClient([{}])
    client2 = BudgetClient(transport, receipt(now, paid), tmp_path / "next-ledger.json", [], limit=1)
    state, calls = call_stage(unit_id="v", commit="a" * 40, stage="analysis", payload={}, schema={}, validate=lambda v: [],
                              prompt="p", prior=merge_budget([], paid), client=client2, now=now, daily_limit=1)
    assert state["reason"] == "daily_budget" and not calls and not transport.calls
    assert json.loads((tmp_path / "next-ledger.json").read_text())["calls"] == []


def test_receipt_bound_to_date_attempt_freshness_and_contents(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    value = receipt()
    path.write_text(json.dumps(value))
    assert load_receipt(path, now=NOW)["day"] == "2026-10-28"
    with pytest.raises(WatchError): load_receipt(path, now=NOW + timedelta(minutes=16))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/watch")
    monkeypatch.setenv("GITHUB_RUN_ID", "90")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(WatchError): load_receipt(path, now=NOW)
    value["calls"] = [row()]
    path.write_text(json.dumps(value))
    with pytest.raises(WatchError): load_receipt(path, now=NOW)


def test_id_conflict_is_rejected_instead_of_double_counting_or_undercounting():
    with pytest.raises(WatchError): call_rows([row(), row(day="2026-10-27")])
    with pytest.raises(WatchError): merge_budget([row()], [row(day="2026-10-27")])


def test_main_pipeline_uses_unmerged_budget_and_never_publishes_imported_content(tmp_path, monkeypatch):
    import yaml
    from tests.test_mechanism_pipeline import SETTINGS, setup_pipeline
    from x_algo_watch.drafting import run
    plan, mechanism, pack, bundle, config = setup_pipeline(tmp_path, monkeypatch)
    config.write_text(yaml.safe_dump({**SETTINGS, "daily_calls": 8}), encoding="utf-8")
    now = datetime.now(timezone.utc)
    value = receipt(now, [row(f"old-{i}", now.date().isoformat()) for i in range(8)])
    budget = tmp_path / "budget.json"; budget.write_text(json.dumps(value))
    transport = RecordedClient([])
    output = tmp_path / "draft.json"
    run(bundle, output, config, client=transport, budget_receipt=budget)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert not transport.calls
    assert result["events"][-1]["pipeline_loop"]["reason"] == "daily_budget"
    assert not result["drafts"]
    assert not any(e["id"].startswith("old-") for e in result["events"])
    assert json.loads((tmp_path / "call-ledger.json").read_text())["calls"] == []


def test_legacy_model_path_records_intent_before_interrupt(tmp_path):
    from tests.test_drafting import MODELS, PROMPTS, record_and_pack
    from x_algo_watch.drafting import analyze_items
    now = datetime.now(timezone.utc)
    _, pack = record_and_pack()
    class Interrupt:
        def call(self, *args):
            raise KeyboardInterrupt()
    path = tmp_path / "call-ledger.json"
    client = BudgetClient(Interrupt(), receipt(now), path, [], limit=1)
    with pytest.raises(KeyboardInterrupt):
        analyze_items([pack], [], client, MODELS, now, PROMPTS)
    assert len(json.loads(path.read_text())["calls"]) == 1


def test_budget_failure_and_recovery_are_visible_on_all_pages_without_progress_change(tmp_path, monkeypatch):
    from tests.fakes import FakeGitHub
    from tests.test_mechanism_pipeline import setup_pipeline
    from x_algo_watch.automation import publish
    from x_algo_watch.call_budget import budget_status, record_unavailable
    from x_algo_watch.storage import RecordStore
    from x_algo_watch.versions import build_versions, filename
    _, _, pack, bundle, _ = setup_pipeline(tmp_path, monkeypatch)
    home = tmp_path / "README.md"
    home.write_text("<!-- versions-summary:begin -->\n<!-- versions-summary:end -->", encoding="utf-8")
    progress = RecordStore(tmp_path / "data").last_success()
    out = tmp_path / "blocked.json"
    record_unavailable(bundle, out, 90, 1)
    assert not json.loads(out.read_text())["drafts"]
    publish(bundle, FakeGitHub(), draft_bundle=out, push=False, notify_issues=False)
    record = RecordStore(tmp_path / "data").records()[0]
    paths = [home, tmp_path / "versions/README.md", tmp_path / "versions" / filename(record)]
    assert all("共享预算尚未核实" in p.read_text(encoding="utf-8") for p in paths)
    assert all("算法版本更新公告" not in p.read_text(encoding="utf-8") for p in paths)
    verified = budget_status(pack, "verified", 90, 2, datetime.now(timezone.utc).isoformat())
    RecordStore(tmp_path / "data").append_event(verified, datetime.now(timezone.utc))
    build_versions(tmp_path)
    assert all("共享预算尚未核实" not in p.read_text(encoding="utf-8") for p in paths)
    assert RecordStore(tmp_path / "data").last_success() == progress


def test_artifact_redirect_does_not_forward_token_and_download_is_bounded(monkeypatch):
    import urllib.error
    import urllib.request
    from x_algo_watch.github import GitHub
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) % 2:
                raise urllib.error.HTTPError(request.full_url, 302, "redirect",
                    {"Location": "https://example.blob.core.windows.net/archive?opaque-signature"}, None)
            return io.BytesIO(b"test")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    client = GitHub("owner/watch", "test-token")
    assert client.artifact_zip(7, 4) == b"test"
    assert requests[0].get_header("Authorization") == "Bearer test-token"
    assert requests[1].get_header("Authorization") is None
    with pytest.raises(WatchError): client.artifact_zip(7, 3)


def test_standalone_mechanism_runner_also_reserves_before_network(tmp_path):
    from tests.test_mechanism_runner import PROMPT, plan_with_tasks
    from x_algo_watch.mechanism_runner import run_mechanism_tasks
    now = datetime.now(timezone.utc)
    plan, _ = plan_with_tasks()
    class Interrupt:
        def call(self, *args):
            raise KeyboardInterrupt()
    path = tmp_path / "call-ledger.json"
    client = BudgetClient(Interrupt(), receipt(now), path, [], limit=1)
    saved = []
    with pytest.raises(KeyboardInterrupt):
        run_mechanism_tasks(plan, [], client, now, PROMPT, persist=saved.append)
    assert len(json.loads(path.read_text())["calls"]) == 1
    assert saved[0]["outcome"] == "started"
