"""Actions 的 detect / publish 边界；产物只包含数据，绝不执行产物代码。"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from . import cli
from .evidence import digest, items_for_record, numeric_context_for_record
from .git import SHA, UPSTREAM, Repository, run_git
from .github import BOT, GitHub, bot_owned, issue_marker
from .models import WatchError
from .storage import EVENT_PATTERN, RECORD_PATTERN, RecordStore, atomic_write, public_text
from .versions import VERSION_PATH, build_versions, filename
from .project_evidence import context_packs, pending_packs, project_pack, save_context, save_context_snapshot
from .research import valid_research_event

DATA_PATH = re.compile(r"(?:status\.md|verification\.yaml|records/\d{4}-\d{2}\.md|project-context/[0-9a-f]{40}(?:-[0-9a-f]{64})?\.json)\Z")
BASELINE = "7f2ca2b914cadac591ea7fceec0c477e473d3dee"


def data_files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if path.is_symlink():
            raise WatchError("检测输出中不能出现符号链接")
        if path.is_file() and DATA_PATH.fullmatch(path.relative_to(root).as_posix()):
            if path.stat().st_size > 16_000_000:
                raise WatchError("检测文件超过公开输出上限")
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def latest_checks(store: RecordStore) -> list[dict[str, Any]]:
    checks = store.records()
    for event in store.events():
        if event["type"] == "verification":
            result = event.get("result")
            if not isinstance(result, dict) or result.get("commit") != event.get("commit") or not isinstance(result.get("findings"), list):
                raise WatchError("登记表重核事件结构无效")
            checks.append({**result, "checked_at": event["created_at"], "verification_event_id": event["id"]})
    by_commit: dict[str, Any] = {}
    for check in sorted(checks, key=lambda item: datetime.fromisoformat(item["checked_at"])):
        by_commit[check["commit"]] = check
    return list(by_commit.values())


def write_bundle(bundle: Path, original: dict[str, bytes], output: Path, *, test: bool, code: str, result_code: int) -> dict[str, Any]:
    research = [] if test else pending_packs(output)
    for pack in research:
        save_context_snapshot(output, pack)
    files = data_files(output)
    for name, content in files.items():
        text = content.decode("utf-8")
        public_text(text)
        atomic_write(bundle / "data" / name, text)
    records = latest_checks(RecordStore(output))
    # 固定初始历史范围是研究样本；生产只通知基线之后的新记录。
    old_records = [json.loads(raw) for name, content in original.items() if name.startswith("records/") for raw in RECORD_PATTERN.findall(content.decode("utf-8"))]
    old_events = [json.loads(raw) for name, content in original.items() if name.startswith("records/") for raw in EVENT_PATTERN.findall(content.decode("utf-8"))]
    original_commits = {r["commit"] for r in old_records} if not test else set()
    old_event_ids = {event["id"] for event in old_events}
    notify = [r for r in records if r["severity"] in ("红", "橙") and (test or r["commit"] not in original_commits or (r.get("verification_event_id") and r["verification_event_id"] not in old_event_ids))]
    # 未发出 Issue 或待模型补写的记录每轮重试；通知资格留在 append-only 事件中。
    eligible = {e["commit"] for e in RecordStore(output).events() if e["type"] == "notification_eligible"}
    for record in notify:
        eligible.add(record["commit"])
        RecordStore(bundle / "data").append_event({"type": "notification_eligible", "id": "notify:" + record["commit"], "commit": record["commit"]}, datetime.now(timezone.utc))
    notify = list({r["commit"]: r for r in records if r["commit"] in eligible and r["severity"] in ("红", "橙")}.values())
    files = data_files(bundle / "data")
    manifest = {"schema_version": 1, "test": test, "code_commit": code, "base_hashes": hashes(original), "file_hashes": hashes(files), "result_code": result_code, "notifications": [r["commit"] for r in notify], "research": [p["commit"] for p in research]}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
    public_text(encoded)
    atomic_write(bundle / "manifest.json", encoded)
    return manifest


def load_bundle(bundle: Path) -> tuple[dict[str, Any], dict[str, bytes], list[dict[str, Any]]]:
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchError("检测产物清单不可读") from exc
    if manifest.get("schema_version") != 1 or type(manifest.get("test")) is not bool:
        raise WatchError("检测产物版本或模式无效")
    files = data_files(bundle / "data")
    if hashes(files) != manifest.get("file_hashes"):
        raise WatchError("检测产物哈希不一致")
    for content in files.values():
        public_text(content.decode("utf-8"))
    public_text(json.dumps(manifest, ensure_ascii=False))
    records = latest_checks(RecordStore(bundle / "data"))
    selected = manifest.get("notifications", [])
    if not isinstance(selected, list) or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in selected):
        raise WatchError("通知提交清单无效")
    notifications = [r for r in records if r["commit"] in selected and r["severity"] in ("红", "橙")]
    research = manifest.get("research", [])
    available = {p["commit"] for p in context_packs(bundle / "data")}
    known = {r["commit"] for r in RecordStore(bundle / "data").records() if r["complete"]}
    if not isinstance(research, list) or any(not isinstance(sha, str) or sha not in available or sha not in known for sha in research) or (manifest["test"] and research):
        raise WatchError("项目研究提交清单无效")
    return manifest, files, notifications


def detect(bundle: Path, source: Path | None = None, test_from: str = "", test_to: str = "", backfill_commit: str = "", research_commit: str = "", research_all: bool = False) -> dict[str, Any]:
    if bool(test_from) != bool(test_to):
        raise WatchError("测试模式必须同时提供两个历史提交")
    if backfill_commit and (test_from or not SHA.fullmatch(backfill_commit)):
        raise WatchError("补写须为完整提交号，且不能与历史测试混用")
    if (research_commit and not SHA.fullmatch(research_commit)) or ((research_commit or research_all) and test_from) or type(research_all) is not bool:
        raise WatchError("项目研究须为完整提交号或显式历史队列，不能与隔离测试混用")
    original = data_files(Path("data"))
    working = bundle / "working"
    for name, content in original.items():
        atomic_write(working / name, content.decode("utf-8"))
    now = datetime.now(timezone.utc)
    code = cli.code_version(Path.cwd())
    test = bool(test_from)
    if test:
        # 历史测试有独立输出，不推进生产进度。
        working = bundle / "test-working"
    if backfill_commit or research_commit or research_all:
        # 只补写已有完整证据；不克隆、不重核旧版本、不追加检查记录或改变进度。
        store = RecordStore(working)
        if backfill_commit:
            record = next((r for r in reversed(store.records()) if r["commit"] == backfill_commit), None)
            if not record or not record["complete"] or record["severity"] not in ("红", "橙"):
                raise WatchError("补写只接受已有完整的红橙检查记录")
            store.append_event({"type": "backfill_requested", "id": "backfill:" + backfill_commit, "commit": backfill_commit}, now)
            store.append_event({"type": "notification_eligible", "id": "notify:" + backfill_commit, "commit": backfill_commit}, now)
        result_code = 0
    elif source is None and test:
        with tempfile.TemporaryDirectory(prefix="xaw-test-") as folder:
            repo_path = Path(folder) / "upstream.git"
            run_git(["clone", "--bare", "--quiet", UPSTREAM, str(repo_path)])
            result_code = cli.main(["compare", "--repo", str(repo_path), "--from", test_from, "--to", test_to, "--output", str(working)])
    else:
        args = ["compare", "--from", test_from, "--to", test_to] if test else ["check", "--baseline", BASELINE]
        if source is not None:
            args += ["--repo", str(source)]
        result_code = cli.main(args + ["--output", str(working)])
    settings_path = Path("config/watch.yaml")
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    if not test and (settings.get("project_research") is True or research_commit or research_all):
        old_commits = {json.loads(raw)["commit"] for name, content in original.items() if name.startswith("records/") for raw in RECORD_PATTERN.findall(content.decode("utf-8"))}
        requested = {e["commit"] for e in RecordStore(working).events() if e["type"] == "research_context_requested"}
        candidates = [r for r in RecordStore(working).records() if r["complete"] and r.get("base") and (research_all or r["commit"] == research_commit or r["commit"] not in old_commits or r["commit"] in requested)]
        if research_commit and not any(r["commit"] == research_commit for r in candidates):
            raise WatchError("项目研究只接受已有完整的迭代记录")
        existing = {p["commit"] for p in context_packs(working)}
        missing = [r for r in candidates if r["commit"] not in existing]
        for record in missing:
            RecordStore(working).append_event({"type": "research_context_requested", "id": "context-request:" + record["commit"], "commit": record["commit"]}, now)

        def deferred_context(record: dict[str, Any]) -> None:
            RecordStore(working).append_event({"type": "research_context_deferred", "id": "context-deferred:" + record["commit"] + ":" + now.date().isoformat(), "commit": record["commit"], "reason": "项目上下文暂不可用；检测证据仍保留，后续重试"}, now)

        def prepare_contexts(upstream: Repository) -> None:
            for record in missing:
                try:
                    save_context(working, project_pack(upstream, record))
                except (WatchError, UnicodeError):
                    deferred_context(record)

        if missing:
            if source is not None:
                prepare_contexts(Repository(source))
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix="xaw-context-") as folder:
                        upstream_path = Path(folder) / "upstream.git"
                        run_git(["clone", "--bare", "--quiet", UPSTREAM, str(upstream_path)])
                        prepare_contexts(Repository(upstream_path))
                except WatchError:
                    for record in missing:
                        deferred_context(record)
    manifest = write_bundle(bundle, original, working, test=test, code=code, result_code=result_code)
    # 只上传最终数据与清单；working 不进入上传路径。
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("has_alerts=" + ("true" if manifest["notifications"] else "false") + "\n")
            stream.write("has_research=" + ("true" if manifest["research"] else "false") + "\n")
    return manifest


def evidence_body(record: dict[str, Any], repository: str, test: bool) -> str:
    prefix = "**历史测试，未推进生产状态。**\n\n" if test else ""
    prefix += issue_marker(record["commit"], test) + "\n\n"
    prefix += "公开源码证据；默认值不代表线上值，部署未知。\n\n"
    prefix += f"- 提交：[{record['commit'][:7]}]({UPSTREAM}/commit/{record['commit']})\n"
    prefix += f"- 级别：{record['severity']}；完整检查：{record['complete']}\n\n"
    numeric_context = numeric_context_for_record(record)
    details = json.dumps({"base": record["base"], "commit": record["commit"], "counts": record.get("counts", {}), "findings": record["findings"], "numeric_context": numeric_context}, ensure_ascii=False, indent=2)
    if len(details) > 55_000:
        # 不把截断后的 JSON 冒充完整证据。
        def compact(value: Any) -> Any:
            if isinstance(value, list) and len(value) > 8:
                return {"length": len(value), "first_items": value[:8], "full_value_in_record": True}
            if isinstance(value, str) and len(value) > 800:
                return {"prefix": value[:800], "full_value_in_record": True}
            return value
        details = json.dumps({"base": record["base"], "commit": record["commit"], "counts": record.get("counts", {}), "numeric_context": numeric_context, "findings": [{k: compact(f.get(k)) for k in ("category", "severity", "path", "name", "message", "before", "after", "line_before", "line_after")} for f in record["findings"]]}, ensure_ascii=False, indent=2)
        prefix += "完整 diff 较长；请在检查记录查看原始证据。\n\n"
    if len(details) > 55_000:
        raise WatchError("证据摘要仍超出 Issue 上限；保留完整检查记录，需人工拆分")
    body = prefix + "```json\n" + details + "\n```\n\n"
    body += f"完整记录：[data/records](https://github.com/{repository}/tree/main/data/records)。\n\n"
    if not test:
        body += f"通俗说明与可能影响：[本次版本档案](https://github.com/{repository}/blob/main/versions/{filename(record)}) · [全部版本](https://github.com/{repository}/blob/main/versions/README.md)。\n\n"
    body += "模型不可用时保留证据，下一轮补起草。请仅对机器人草稿使用 `已确认` / `驳回` 标签。"
    public_text(body)
    return body


def publish_issues(client: GitHub, records: list[dict[str, Any]], test: bool, drafts: dict[str, Any] | None = None) -> list[int]:
    client.ensure_labels()
    existing = client.issues()
    numbers: list[int] = []
    for record in records:
        marker = issue_marker(record["commit"], test)
        matches = [issue for issue in existing if bot_owned(issue) and marker in (issue.get("body") or "")]
        if len(matches) > 1:
            raise WatchError("同一提交有多个机器人 Issue，需人工去重")
        if matches:
            issue = matches[0]
            if issue["state"] == "closed":
                continue
            body = evidence_body(record, client.repository, test)
            if issue.get("body") != body:
                client.request("PATCH", f"/issues/{int(issue['number'])}", {"body": body})
        else:
            headline = next((f["name"] for f in record["findings"] if f["severity"] in ("红", "橙")), "需人工复核")
            issue = client.request("POST", "/issues", {
                "title": ("[测试] " if test else "") + f"[{record['severity']}] {record['commit'][:7]}：{headline}"[:180],
                "body": evidence_body(record, client.repository, test),
                "labels": ["x-algo-watch", "级别:" + record["severity"], "待起草"] + (["测试"] if test else []),
            })
            existing.append(issue)
        number = int(issue["number"]); numbers.append(number)
        draft = (drafts or {}).get(record["commit"])
        if draft:
            comment_marker = f"<!-- xaw-draft:{record['commit']}:{draft['version']} -->"
            comments = client.comments(number)
            if not any(bot_owned(comment) and comment_marker in comment.get("body", "") for comment in comments):
                body = comment_marker + "\n\n" + draft["body"]
                public_text(body)
                client.request("POST", f"/issues/{number}/comments", {"body": body})
            state = "待确认" if draft["ready"] else "需人工"
            labels = {label["name"] for label in issue.get("labels", [])}
            if state not in labels:
                client.request("POST", f"/issues/{number}/labels", {"labels": [state]})
            # 只清理程序自己的旧状态，不整体覆盖标签，以保留所有者自定义标签。
            for obsolete in sorted((labels & {"待起草", "待确认", "需人工"}) - {state}):
                client.request("DELETE", f"/issues/{number}/labels/{quote(obsolete, safe='')}")
    return numbers


def commit_paths(paths: list[str], message: str, repository: str, token: str) -> bool:
    if any(path not in ("data", "digest.md", "versions") and not path.startswith("data/") and not VERSION_PATH.fullmatch(path) for path in paths):
        raise WatchError("发布超出检测数据、速报与版本档案写入边界")
    if Path("versions").is_symlink():
        raise WatchError("版本档案目录不能是符号链接")
    run_git(["add", "--", *paths], Path.cwd())
    if not run_git(["diff", "--cached", "--name-only"], Path.cwd()).strip():
        return False
    staged = run_git(["diff", "--cached", "--name-only"], Path.cwd()).decode().splitlines()
    if any(path != "digest.md" and not (path.startswith("data/") and DATA_PATH.fullmatch(path[5:])) and not VERSION_PATH.fullmatch(path) for path in staged):
        raise WatchError("暂存区包含发布范围外文件")
    for path in staged:
        if VERSION_PATH.fullmatch(path):
            target = Path(path)
            if target.is_symlink():
                raise WatchError("版本档案不能是符号链接")
            if target.exists():
                public_text(target.read_text(encoding="utf-8"))
    run_git(["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", message], Path.cwd())
    header = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + header, "GIT_TERMINAL_PROMPT": "0"}
    # 令牌仅在进程环境中；不写 Git 配置，不置入命令参数或 URL。
    result = subprocess.run(["git", "-c", "core.hooksPath=", "push", "https://github.com/" + repository + ".git", "HEAD:refs/heads/main"], env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise WatchError("发布提交推送失败；没有强制推送，下轮重试")
    return True


def publish(bundle: Path, client: GitHub, *, draft_bundle: Path | None = None, push: bool = True) -> list[int]:
    manifest, files, records = load_bundle(bundle)
    drafts: dict[str, Any] = {}
    if draft_bundle is not None and draft_bundle.exists():
        raw = draft_bundle.read_text(encoding="utf-8"); public_text(raw)
        payload = json.loads(raw)
        if payload.get("schema_version") != 1 or payload.get("source_manifest_hash") != digest(manifest):
            raise WatchError("草稿不属于当前检测产物")
        drafts = payload.get("drafts", {})
        allowed_commits = {record["commit"] for record in records} | set(manifest.get("research", []))
        if not isinstance(drafts, dict) or set(drafts) - allowed_commits:
            raise WatchError("草稿提交不在当前通知范围")
        for draft in drafts.values():
            if not isinstance(draft, dict) or set(draft) != {"body", "ready", "version"} or not isinstance(draft["body"], str) or len(draft["body"]) > 60_000 or type(draft["ready"]) is not bool or not re.fullmatch(r"[0-9a-f]{20}", str(draft["version"])):
                raise WatchError("草稿结构或长度无效")
        for event in payload.get("events", []):
            if not isinstance(event, dict) or event.get("type") not in ("model_call", "analysis_state", "version_research") or event.get("commit") not in allowed_commits:
                raise WatchError("草稿审计事件超出允许范围")
            if event["type"] == "version_research":
                pack = next((p for p in context_packs(bundle / "data") if p["commit"] == event["commit"]), None)
                if not pack or not valid_research_event(event, pack):
                    raise WatchError("项目研究事件与当前上下文不匹配")
    if not manifest["test"]:
        if hashes(data_files(Path("data"))) != manifest["base_hashes"]:
            raise WatchError("main 的检测数据已变化，拒绝覆盖；请重跑检测")
        for name, content in files.items():
            atomic_write(Path("data") / name, content.decode("utf-8"))
        if draft_bundle is not None and draft_bundle.exists():
            for event in payload.get("events", []):
                RecordStore(Path("data")).append_event(event, datetime.now(timezone.utc))
        build_versions(Path.cwd())
        if push:
            commit_paths(["data", "versions"], "chore: update upstream inspection", client.repository, client._token)
    return publish_issues(client, records, manifest["test"], drafts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("detect", "publish", "assert-complete"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--draft-bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.stage == "detect":
            detect(args.bundle, test_from=os.environ.get("TEST_FROM", ""), test_to=os.environ.get("TEST_TO", ""), backfill_commit=os.environ.get("BACKFILL_COMMIT", ""), research_commit=os.environ.get("RESEARCH_COMMIT", ""), research_all=os.environ.get("RESEARCH_ALL", "false").lower() == "true")
        elif args.stage == "assert-complete":
            manifest, _, _ = load_bundle(args.bundle)
            if manifest["result_code"] != 0:
                raise WatchError("本轮检查未完成；已发布可用证据，进度未越过失败提交")
        else:
            # 同步 main 后校验 data 哈希，防止长时间起草期间的新提交被覆盖。
            run_git(["pull", "--ff-only", "origin", "main"], Path.cwd())
            client = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
            publish(args.bundle, client, draft_bundle=args.draft_bundle)
        return 0
    except WatchError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
