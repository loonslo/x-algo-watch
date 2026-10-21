from datetime import datetime, timezone

import pytest

from x_algo_watch.cli import main
from x_algo_watch.models import CheckResult, Finding, Severity, WatchError
from x_algo_watch.registry import load_registry
from x_algo_watch.storage import RecordStore, public_text

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def empty_registry(tmp_path):
    path = tmp_path / "facts.yaml"
    path.write_text("[]\n", encoding="utf-8")
    return path


def test_duplicate_and_failed_retry(tmp_path):
    store = RecordStore(tmp_path / "data")
    sha = "a" * 40
    result = CheckResult(None, sha, NOW.isoformat(), complete=False)
    result.findings.append(Finding("parse_failure", Severity.RED, "", "parser", "解析失败"))
    assert store.append(result, NOW, "uncommitted")
    assert not store.append(result, NOW, "uncommitted")
    assert store.last_success() is None
    result.complete = True
    assert store.append(result, NOW, "uncommitted")
    assert store.last_success() == sha and len(store.records()) == 2


@pytest.mark.parametrize("text", ["sk-or-v1-" + "a" * 48, "ghp_" + "b" * 40, "-----BEGIN PRIVATE KEY-----", "C:" + "\\Users\\name\\file", "person@example.org"])
def test_public_output_blocked(text, tmp_path):
    with pytest.raises(WatchError):
        public_text(text)
    store = RecordStore(tmp_path / "data")
    result = CheckResult(None, "b" * 40, NOW.isoformat(), findings=[Finding("test", Severity.RED, "", "test", text)])
    with pytest.raises(WatchError):
        store.append(result, NOW, "uncommitted")
    assert not store.records()


def test_registry_rejects_duplicate_yaml_and_ignore_without_reason(tmp_path):
    path = tmp_path / "facts.yaml"
    for content in ['- id: one\n  id: two\n', '- id: one\n  kind: ignore\n  check: value\n  value: 0\n  source: {path: test.rs, name: Example}\n']:
        path.write_text(content, encoding="utf-8")
        with pytest.raises(WatchError):
            load_registry(path)


def test_replay_no_duplicates_and_restore_progress_from_records(sample_repo, tmp_path):
    path, _, base, commit = sample_repo
    params = path / "home-mixer/params/param.rs"
    params.write_text(params.read_text().replace('"favorite", 0.5', '"favorite", 0.6'))
    target = commit()
    output = tmp_path / "output"
    args = ["replay", "--repo", str(path), "--from", base, "--to", target, "--registry", str(empty_registry(tmp_path)), "--output", str(output), "--now", NOW.isoformat()]
    assert main(args) == 0
    initial = (output / "records/2026-10.md").read_bytes()
    (output / "status.md").write_text("corrupt cache", encoding="utf-8")
    assert main(args) == 0
    assert (output / "records/2026-10.md").read_bytes() == initial
    assert RecordStore(output).last_success() == target
    assert "corrupt" not in (output / "status.md").read_text(encoding="utf-8")


def test_output_inside_upstream_is_rejected_without_writing(sample_repo):
    path, _, base, _ = sample_repo
    assert main(["compare", "--repo", str(path), "--from", base, "--to", base, "--output", str(path / "forbidden")]) == 2
    assert not (path / "forbidden").exists()


def test_failed_check_logs_no_progress(sample_repo, tmp_path):
    path, _, _, _ = sample_repo
    output = tmp_path / "out"
    assert main(["check", "--repo", str(path), "--to", "nonexistent", "--registry", str(empty_registry(tmp_path)), "--output", str(output)]) == 2
    assert RecordStore(output).last_success() is None
    assert "本次未检查" in next((output / "records").glob("*.md")).read_text(encoding="utf-8")


def test_unrelated_history_is_red_full_verification(sample_repo, tmp_path):
    path, _, base, _ = sample_repo
    output = tmp_path / "out"
    store = RecordStore(output)
    store.append(CheckResult(None, "f" * 40, NOW.isoformat()), NOW, "uncommitted")
    assert main(["check", "--repo", str(path), "--to", base, "--registry", str(empty_registry(tmp_path)), "--output", str(output)]) == 0
    record = store.records()[-1]
    assert record["mode"] == "rewrite" and record["severity"] == "红"
    assert store.last_success() == base
