from datetime import datetime, timezone

from x_algo_watch import cli
from x_algo_watch.models import CheckResult, WatchError
from x_algo_watch.storage import RecordStore
from x_algo_watch.evidence import digest


def test_no_change_does_not_clone_or_duplicate(tmp_path, monkeypatch):
    target = "a" * 40
    output = tmp_path / "data"
    store = RecordStore(output)
    registry = tmp_path / "facts.yaml"
    registry.write_text("[]\n", encoding="utf-8")
    store.append(CheckResult(None, target, "2026-10-20T00:00:00+00:00", registry_hash=digest([])), datetime(2026, 10, 20, tzinfo=timezone.utc), "uncommitted")
    monkeypatch.setattr(cli, "discover", lambda: target)

    def forbidden(*args, **kwargs):
        raise AssertionError("无变化时不得 clone")

    monkeypatch.setattr(cli, "run_git", forbidden)
    args = ["check", "--output", str(output), "--registry", str(registry), "--now", "2026-10-21T00:00:00+00:00"]
    assert cli.main(args) == 0
    status = (output / "status.md").read_bytes()
    modified = (output / "status.md").stat().st_mtime_ns
    args[-1] = "2026-10-21T03:00:00+00:00"
    assert cli.main(args) == 0
    assert len(store.records()) == 1
    assert (output / "status.md").read_bytes() == status
    assert (output / "status.md").stat().st_mtime_ns == modified


def test_network_failure_is_logged_without_advancing(tmp_path, monkeypatch):
    def fail():
        raise WatchError("上游连接失败；本次未检查")

    monkeypatch.setattr(cli, "discover", fail)
    output = tmp_path / "data"
    assert cli.main(["check", "--output", str(output)]) == 2
    assert RecordStore(output).last_success() is None
    assert "本次未检查" in next((output / "records").glob("*.md")).read_text(encoding="utf-8")
    assert "无变化" not in (output / "status.md").read_text(encoding="utf-8")
