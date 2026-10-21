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

from . import cli
from .evidence import digest, items_for_record
from .git import SHA, UPSTREAM, Repository, run_git
from .github import BOT, GitHub, bot_owned, issue_marker
from .models import WatchError
from .storage import EVENT_PATTERN, RECORD_PATTERN, RecordStore, atomic_write, public_text

DATA_PATH = re.compile(r"(?:status\.md|verification\.yaml|records/\d{4}-\d{2}\.md)\Z")
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
    manifest = {"schema_version": 1, "test": test, "code_commit": code, "base_hashes": hashes(original), "file_hashes": hashes(files), "result_code": result_code, "notifications": [r["commit"] for r in notify]}
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
    return manifest, files, notifications


def detect(bundle: Path, source: Path | None = None, test_from: str = "", test_to: str = "") -> dict[str, Any]:
    if bool(test_from) != bool(test_to):
        raise WatchError("测试模式必须同时提供两个历史提交")
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
    if source is None and test:
        with tempfile.TemporaryDirectory(prefix="xaw-test-") as folder:
            repo_path = Path(folder) / "upstream.git"
            run_git(["clone", "--bare", "--quiet", UPSTREAM, str(repo_path)])
            result_code = cli.main(["compare", "--repo", str(repo_path), "--from", test_from, "--to", test_to, "--output", str(working)])
    else:
        args = ["compare", "--from", test_from, "--to", test_to] if test else ["check", "--baseline", BASELINE]
        if source is not None:
            args += ["--repo", str(source)]
        result_code = cli.main(args + ["--output", str(working)])
    manifest = write_bundle(bundle, original, working, test=test, code=code, result_code=result_code)
    # 只上传最终数据与清单；working 不进入上传路径。
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("has_alerts=" + ("true" if manifest["notifications"] else "false") + "\n")
    return manifest


def evidence_body(record: dict[str, Any], repository: str, test: bool) -> str:
    prefix = "**历史测试，未推进生产状态。**\n\n" if test else ""
    prefix += issue_marker(record["commit"], test) + "\n\n"
    prefix += "公开源码证据；默认值不代表线上值，部署未知。\n\n"
    prefix += f"- 提交：[{record['commit'][:7]}]({UPSTREAM}/commit/{record['commit']})\n"
    prefix += f"- 级别：{record['severity']}；完整检查：{record['complete']}\n\n"
    details = json.dumps({"base": record["base"], "commit": record["commit"], "counts": record.get("counts", {}), "findings": record["findings"], "numeric_context": record.get("numeric_context", {})}, ensure_ascii=False, indent=2)
    if len(details) > 55_000:
        # 不把截断后的 JSON 冒充完整证据。
        details = json.dumps({"commit": record["commit"], "findings": [{k: f.get(k) for k in ("category", "severity", "path", "name", "message")} for f in record["findings"]]}, ensure_ascii=False, indent=2)
        prefix += "完整 diff 较长；请在检查记录查看原始证据。\n\n"
    body = prefix + "```json\n" + details[:55_000] + "\n```\n\n"
    body += f"完整记录：[data/records](https://github.com/{repository}/tree/main/data/records)。\n\n"
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
            client.request("POST", f"/issues/{number}/labels", {"labels": ["待确认" if draft["ready"] else "需人工"]})
    return numbers


def commit_paths(paths: list[str], message: str, repository: str, token: str) -> bool:
    if any(path != "digest.md" and not path.startswith("data/") for path in paths):
        raise WatchError("发布超出 data/ 与 digest.md 写入边界")
    run_git(["add", "--", *paths], Path.cwd())
    if not run_git(["diff", "--cached", "--name-only"], Path.cwd()).strip():
        return False
    staged = run_git(["diff", "--cached", "--name-only"], Path.cwd()).decode().splitlines()
    if any(path != "digest.md" and not (path.startswith("data/") and DATA_PATH.fullmatch(path[5:])) for path in staged):
        raise WatchError("暂存区包含发布范围外文件")
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
        allowed_commits = {record["commit"] for record in records}
        if not isinstance(drafts, dict) or set(drafts) - allowed_commits:
            raise WatchError("草稿提交不在当前通知范围")
        for draft in drafts.values():
            if not isinstance(draft, dict) or set(draft) != {"body", "ready", "version"} or not isinstance(draft["body"], str) or len(draft["body"]) > 60_000 or type(draft["ready"]) is not bool or not re.fullmatch(r"[0-9a-f]{20}", str(draft["version"])):
                raise WatchError("草稿结构或长度无效")
        for event in payload.get("events", []):
            if not isinstance(event, dict) or event.get("type") not in ("model_call", "analysis_state") or event.get("commit") not in allowed_commits:
                raise WatchError("草稿审计事件超出允许范围")
    if not manifest["test"]:
        if hashes(data_files(Path("data"))) != manifest["base_hashes"]:
            raise WatchError("main 的检测数据已变化，拒绝覆盖；请重跑检测")
        for name, content in files.items():
            atomic_write(Path("data") / name, content.decode("utf-8"))
        if draft_bundle is not None and draft_bundle.exists():
            for event in payload.get("events", []):
                RecordStore(Path("data")).append_event(event, datetime.now(timezone.utc))
        if push:
            commit_paths(["data"], "chore: update upstream inspection", client.repository, client._token)
    return publish_issues(client, records, manifest["test"], drafts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("detect", "publish", "assert-complete"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--draft-bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.stage == "detect":
            detect(args.bundle, test_from=os.environ.get("TEST_FROM", ""), test_to=os.environ.get("TEST_TO", ""))
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
