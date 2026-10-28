"""跨主链、只读样例和发布失败产物核对预算；导入次数，不导入结论。"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlencode
import zipfile

from .evidence import digest
from .github import GitHub
from .models import WatchError
from .storage import atomic_write, public_json

MAX_ARCHIVE = 128 * 1024 * 1024
MAX_LEDGER = 2 * 1024 * 1024
WORKFLOWS = {"check.yml": ("draft", "Analyze and review evidence", "draft"),
             "sample.yml": ("draft", "Iterate drafts and run all three review roles", "reviewed-sample")}


def utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is not None:
            return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        pass
    raise WatchError("调用预算时间无效")


def call_rows(events: Any) -> list[dict[str, str]]:
    if not isinstance(events, list):
        raise WatchError("调用预算记录必须为列表")
    rows = {}
    for event in events:
        if not isinstance(event, dict):
            raise WatchError("调用预算事件结构无效")
        if event.get("type") != "model_call":
            continue
        identifier, day = event.get("id"), event.get("day")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 1024 or any(ord(c) < 32 for c in identifier):
            raise WatchError("调用预算事件编号无效")
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or utc(day + "T00:00:00Z").date().isoformat() != day:
            raise WatchError("调用预算日期无效")
        row = {"type": "model_call", "id": identifier, "day": day}
        if identifier in rows and rows[identifier] != row:
            raise WatchError("同一调用编号出现不一致日期")
        rows[identifier] = row
    return list(rows.values())


def merge_budget(prior: list[dict[str, Any]], rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """已有完整审计优先；外部账本仅能占用预算，不能充当阶段缓存。"""
    known = {e["id"]: e for e in prior}
    for row in call_rows(rows):
        old = known.get(row["id"])
        if old and (old.get("type") != "model_call" or old.get("day") != row["day"]):
            raise WatchError("导入调用账本与已有审计冲突")
        if not old:
            known[row["id"]] = row
    return list(known.values())


def budget_status(pack: dict[str, Any], state: str, run_id: int, attempt: int, checked_at: str) -> dict[str, Any]:
    event = {"type": "research_budget_status", "commit": pack["commit"], "item_id": pack["item_id"],
             "state": state, "run_id": run_id, "attempt": attempt, "checked_at": checked_at}
    event["id"] = "budget:" + digest(event)
    return event


def valid_budget_status(event: dict[str, Any], pack: dict[str, Any] | None) -> bool:
    fields = {"id", "type", "commit", "item_id", "state", "run_id", "attempt", "checked_at"}
    try:
        return (isinstance(pack, dict) and set(event) == fields and event["type"] == "research_budget_status"
                and event["commit"] == pack["commit"] and event["item_id"] == pack["item_id"]
                and event["state"] in {"blocked", "verified"} and type(event["run_id"]) is int and event["run_id"] > 0
                and type(event["attempt"]) is int and 1 <= event["attempt"] <= 100
                and utc(event["checked_at"]).tzinfo is not None
                and event["id"] == "budget:" + digest({k: v for k, v in event.items() if k != "id"}))
    except (WatchError, ValueError, TypeError, KeyError):
        return False


def record_unavailable(bundle: Path, output: Path, run_id: int, attempt: int) -> None:
    from .automation import load_bundle
    from .project_evidence import context_packs
    manifest, _, _ = load_bundle(bundle)
    if output.exists():
        raise WatchError("拒绝用预算失败状态覆盖已有起草产物")
    events = [budget_status(p, "blocked", run_id, attempt, datetime.now(timezone.utc).isoformat())
              for p in context_packs(bundle / "data") if p["commit"] in manifest.get("research", [])]
    value = {"schema_version": 1, "source_manifest_hash": digest(manifest), "drafts": {}, "events": events}
    public_json(value)
    atomic_write(output, json.dumps(value, ensure_ascii=False, indent=2))


def archive_json(raw: bytes, *, ledger: bool) -> dict[str, Any]:
    """只读 ZIP 内唯一指定 JSON；不解压、不运行、不读取来源代码。"""
    if len(raw) > MAX_ARCHIVE:
        raise WatchError("调用审计产物超过读取边界")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 20_000:
                raise WatchError("调用审计产物文件数超限")
            matches = []
            for entry in entries:
                name = entry.orig_filename
                path = PurePosixPath(name)
                if name != entry.filename or "\\" in name or path.is_absolute() or ".." in path.parts or ":" in name or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise WatchError("调用审计产物路径无效")
                if not entry.is_dir() and path.name == ("call-ledger.json" if ledger else "draft.json"):
                    matches.append(entry)
            if len(matches) != 1:
                raise WatchError("调用审计产物缺少唯一账本")
            entry = matches[0]
            if entry.flag_bits & 1 or entry.file_size > (MAX_LEDGER if ledger else MAX_ARCHIVE) or entry.file_size > max(entry.compress_size, 1) * 2000:
                raise WatchError("调用审计 JSON 超过安全读取边界")
            result = json.loads(archive.read(entry))
        if not isinstance(result, dict):
            raise WatchError("调用审计产物 JSON 无效")
        # 旧稿件原文不导入、不公开；只扫描实际取回的计数信息。
        call_rows(result.get("calls" if ledger else "events"))
        return result
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError):
        raise WatchError("调用审计 ZIP 或 JSON 损坏") from None


def api_rows(client: GitHub, path: str, field: str) -> list[dict[str, Any]]:
    result = []
    for page in range(1, 21):
        separator = "&" if "?" in path else "?"
        payload = client.request("GET", path + separator + urlencode({"per_page": 100, "page": page}))
        rows = payload.get(field) if isinstance(payload, dict) else None
        total = payload.get("total_count") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or type(total) is not int or total < len(rows) or total >= 2000 or any(not isinstance(r, dict) for r in rows):
            raise WatchError("预算核对 API 分页不完整或超限")
        result.extend(rows)
        if len(rows) < 100:
            if len(result) != total:
                raise WatchError("预算核对 API 分页数量不一致")
            return result
    raise WatchError("预算核对 API 分页未读完，不能继续调用")


def collect_budget(client: GitHub, *, run_id: int, attempt: int, now: datetime) -> dict[str, Any]:
    """串行工作流锁内，逐尝试核对当天可能调用的两个可信工作流。"""
    day = now.astimezone(timezone.utc).date().isoformat()
    events, receipts = [], []
    for filename, (job_name, step_name, legacy_name) in WORKFLOWS.items():
        # 不按 created 过滤：很早创建的运行今天重跑，也必须读取其尝试。
        # 不按 branch 搜索，避免 GitHub 搜索接口的 1,000 条结果上限。
        runs = api_rows(client, f"/actions/workflows/{filename}/runs", "workflow_runs")
        for run in runs:
            if run.get("head_branch") != "main":
                continue  # 模型环境的分支策略只允许 main。
            if utc(run.get("updated_at")) < utc(day + "T00:00:00Z") and run.get("status") == "completed":
                continue
            rid, attempts = run.get("id"), run.get("run_attempt")
            if (type(rid) is not int or type(attempts) is not int or not 1 <= attempts <= 100
                    or run.get("path") != ".github/workflows/" + filename or run.get("head_branch") != "main"
                    or not isinstance(run.get("head_repository"), dict)
                    or run["head_repository"].get("full_name", "").lower() != client.repository.lower()
                    or run.get("event") not in {"schedule", "workflow_dispatch"}):
                raise WatchError("预算来源不是可信主分支工作流")
            if filename == "sample.yml" and run.get("actor", {}).get("login", "").lower() != client.owner.lower():
                raise WatchError("只读样例的预算来源不属于所有者")
            artifacts = None
            for number in range(1, attempts + 1):
                if rid == run_id and number == attempt:
                    continue
                jobs = api_rows(client, f"/actions/runs/{rid}/attempts/{number}/jobs", "jobs")
                drafts = [j for j in jobs if j.get("name") == job_name]
                if len(drafts) > 1:
                    raise WatchError("预算来源有重复模型任务")
                if not drafts or drafts[0].get("conclusion") == "skipped":
                    continue
                job = drafts[0]
                if job.get("started_at") and utc(job["started_at"]).date().isoformat() < day and job.get("completed_at") and utc(job["completed_at"]).date().isoformat() < day:
                    continue
                if not isinstance(job.get("steps", []), list) or any(not isinstance(s, dict) for s in job.get("steps", [])):
                    raise WatchError("预算来源的模型步骤结构无效")
                steps = [s for s in job.get("steps", []) if s.get("name") == step_name]
                if not steps and job.get("status") == "queued":
                    continue
                if len(steps) != 1:
                    raise WatchError("预算来源的模型步骤无法核实")
                step = steps[0]
                if step.get("conclusion") == "skipped" or step.get("status") == "queued":
                    continue
                if job.get("status") != "completed":
                    raise WatchError("另一个模型任务尚在运行，不能核实完整共享预算")
                if artifacts is None:
                    artifacts = api_rows(client, f"/actions/runs/{rid}/artifacts", "artifacts")
                name = f"call-ledger-{rid}-{number}"
                choices = [a for a in artifacts if a.get("name") == name]
                is_ledger = bool(choices)
                if not choices:
                    # 老工作流只在成功完成单次尝试时有可证明完整的最终审计。
                    if attempts != 1 or job.get("conclusion") != "success":
                        raise WatchError("历史失败或重跑缺少请求前账本，无法核实当日预算")
                    choices = [a for a in artifacts if a.get("name") == legacy_name]
                if len(choices) != 1 or choices[0].get("expired") is not False or type(choices[0].get("id")) is not int:
                    raise WatchError("当日模型任务账本缺失、重复或已过期；停止新调用")
                artifact = choices[0]
                raw = client.artifact_zip(artifact["id"], MAX_ARCHIVE)
                checksum = artifact.get("digest")
                if checksum is not None and checksum != "sha256:" + hashlib.sha256(raw).hexdigest():
                    raise WatchError("调用审计产物摘要不匹配")
                payload = archive_json(raw, ledger=is_ledger)
                if is_ledger and (payload.get("schema_version") != 1 or payload.get("repository") != client.repository or payload.get("run_id") != rid or payload.get("attempt") != number):
                    raise WatchError("调用账本不属于已核实的工作流尝试")
                rows = call_rows(payload.get("calls" if is_ledger else "events"))
                events.extend(e for e in rows if e["day"] == day)
                receipts.append({"run_id": rid, "attempt": number, "artifact_id": artifact["id"], "kind": "intent_ledger" if is_ledger else "legacy_success"})
    receipt = {"schema_version": 1, "repository": client.repository, "run_id": run_id, "attempt": attempt,
               "checked_at": now.astimezone(timezone.utc).isoformat(), "day": day,
               "calls": call_rows(events), "receipts": receipts}
    receipt["receipt_id"] = digest(receipt)
    public_json(receipt)
    return receipt


class BudgetClient:
    """实际请求前原子占用；账本独立于正文/发布/检查进度。"""
    def __init__(self, client: Any, receipt: dict[str, Any], path: Path, prior: list[dict[str, Any]], limit: int = 1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise WatchError("共享日预算无效")
        self.client, self.path, self.limit = client, path, limit
        self.receipt = receipt
        self.calls = []
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            if any(old.get(k) != receipt.get(k) for k in ("repository", "run_id", "attempt")):
                raise WatchError("本次调用账本身份不匹配")
            self.calls = call_rows(old.get("calls"))
        self.known = call_rows(merge_budget(prior, [*receipt["calls"], *self.calls]))
        self.save()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def save(self) -> None:
        value = {k: self.receipt[k] for k in ("schema_version", "repository", "run_id", "attempt")}
        value["calls"] = self.calls
        public_json(value)
        atomic_write(self.path, json.dumps(value, ensure_ascii=False, indent=2))

    def record_intent(self, event: dict[str, Any]) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        if day != self.receipt["day"] or event.get("day") != day:
            raise WatchError("UTC 日期已变化，须重新核对共享预算后再调用")
        row = call_rows([event])[0]
        known = {e["id"]: e for e in self.known}
        if row["id"] in known:
            raise WatchError("同一请求编号已计账，拒绝再次发出请求")
        if sum(e["day"] == day for e in self.known) >= self.limit:
            raise WatchError("共享调用账本已达到当日预算")
        self.calls.append(row)
        self.known.append(row)
        self.save()


def load_receipt(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise WatchError("共享预算核对凭单无法读取") from None
    public_json(value)
    now = now or datetime.now(timezone.utc)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("receipt_id") != digest({k: v for k, v in value.items() if k != "receipt_id"})
            or value.get("day") != now.astimezone(timezone.utc).date().isoformat()
            or not 0 <= (now - utc(value.get("checked_at"))).total_seconds() <= 15 * 60):
        raise WatchError("共享预算核对凭单无效、跨日或已过期")
    if os.environ.get("GITHUB_ACTIONS") == "true" and any(str(value.get(k)) != os.environ.get(env) for k, env in
            (("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT"))):
        raise WatchError("共享预算核对凭单不属于当前工作流尝试")
    call_rows(value.get("calls"))
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--draft-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        client = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
        receipt = collect_budget(client, run_id=int(os.environ["GITHUB_RUN_ID"]), attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]), now=datetime.now(timezone.utc))
        atomic_write(args.output, json.dumps(receipt, ensure_ascii=False, indent=2))
        BudgetClient(object(), receipt, args.ledger, [])
        return 0
    except (WatchError, KeyError, ValueError, OSError) as exc:
        try:
            record_unavailable(args.bundle, args.draft_output, int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))
        except (WatchError, KeyError, ValueError, OSError):
            print("预算失败状态未能保存；工作流仍失败，未允许新模型调用")
        print(str(exc) if isinstance(exc, WatchError) else "共享预算核对失败，未允许新模型调用")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
