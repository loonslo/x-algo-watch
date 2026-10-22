"""只认仓库所有者的标签事件与机器人草稿；写入后才关闭 Issue。"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .automation import commit_paths
from .github import GitHub, bot_owned, issue_marker
from .git import run_git
from .models import WatchError
from .storage import RecordStore, atomic_write, public_text

DRAFT = re.compile(r"<!-- xaw-draft:([0-9a-f]{40}):([0-9a-f]{20}) -->")
COMMIT = re.compile(r"<!-- xaw-commit:([0-9a-f]{40}) -->")
SHANGHAI = timezone(timedelta(hours=8))


def authorized(event: dict[str, Any], repository: str) -> bool:
    owner = repository.split("/")[0]
    return (
        event.get("action") == "labeled"
        and event.get("label", {}).get("name") in ("已确认", "驳回")
        and event.get("sender", {}).get("login", "").casefold() == owner.casefold()
        and event.get("repository", {}).get("full_name", "").casefold() == repository.casefold()
        and not event.get("issue", {}).get("pull_request")
    )


def confirm(event: dict[str, Any], client: GitHub, now: datetime, root: Path, *, push: bool = True) -> str:
    if not authorized(event, client.repository):
        return "ignored"
    number = event.get("issue", {}).get("number")
    if type(number) is not int or number < 1:
        raise WatchError("标签事件缺少有效 Issue 编号")
    issue = client.request("GET", f"/issues/{number}")
    if not bot_owned(issue):
        raise WatchError("确认只处理机器人创建的监测 Issue")
    labels = {label["name"] for label in issue.get("labels", [])}
    decision = event["label"]["name"]
    if decision not in labels or {"已确认", "驳回"}.issubset(labels):
        raise WatchError("当前确认标签已撤回或存在冲突")
    if "测试" in labels:
        # 历史测试只在 Issue 留痕，不进入真实速报或生产进度。
        client.request("PATCH", f"/issues/{number}", {"state": "closed"})
        return "test_closed"
    match = COMMIT.search(issue.get("body") or "")
    if not match:
        raise WatchError("Issue 缺少生产提交标记")
    commit = match[1]
    store = RecordStore(root / "data")
    if not any(record["commit"] == commit for record in store.records()):
        raise WatchError("Issue 提交不在可信检查记录中")
    outcome = "confirmed" if decision == "已确认" else "rejected"
    if outcome == "confirmed":
        comments = client.comments(number)
        candidates = [comment for comment in comments if bot_owned(comment) and (draft_match := DRAFT.search(comment.get("body", ""))) and draft_match[1] == commit]
        if not candidates:
            raise WatchError("尚无可确认的机器人草稿；先补起草或人工编辑机器人草稿")
        comment = max(candidates, key=lambda item: int(item["id"]))
        text = DRAFT.sub("", comment["body"]).strip()
        if "【待补】" in text:
            raise WatchError("草稿仍含待补项；所有者须编辑补全后重新打标签")
        public_text(text)
        date = now.astimezone(SHANGHAI).date().isoformat()
        text = text.replace("未经确认的草稿", "仓库所有者已确认的速报")
        text = text.replace("仍须仓库所有者人工确认。", "仓库所有者已人工确认。")
        text = text.replace("- 确认：待仓库所有者编辑核对并打标签。", "- 确认：" + date)
        text += f"\n- Issue：[#{number}](https://github.com/{client.repository}/issues/{number})；确认日期：{date}\n"
        marker = f"<!-- xaw-digest:{commit} -->"
        path = root / "digest.md"
        previous = path.read_text(encoding="utf-8")
        if marker not in previous:
            public_text(previous + text)
            atomic_write(path, previous.rstrip() + "\n\n" + marker + "\n" + text + "\n")
    store.append_event({"type": "decision", "id": "decision:" + commit + ":" + outcome, "commit": commit, "issue": number, "outcome": outcome, "actor": client.owner}, now)
    from .cli import code_version
    store.status(now, code_version(root))
    if push:
        commit_paths(["digest.md", "data"], "docs: record reviewed upstream change", client.repository, client._token)
    marker = "<!-- xaw-decision:" + commit + ":" + outcome + " -->"
    if not any(bot_owned(comment) and marker in comment.get("body", "") for comment in client.comments(number)):
        client.request("POST", f"/issues/{number}/comments", {"body": marker + "\n\n所有者已" + ("确认并归档到 digest.md。" if outcome == "confirmed" else "驳回；保留源码证据供追溯。")})
    client.request("PATCH", f"/issues/{number}", {"state": "closed"})
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "event.json")))
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not authorized(event, repository):
            print("忽略非所有者或无关标签事件")
            return 0
        run_git(["pull", "--ff-only", "origin", "main"], Path.cwd())
        result = confirm(event, GitHub(repository, os.environ.get("GH_TOKEN", "")), datetime.now(timezone.utc), Path.cwd())
        print(result)
        return 0
    except WatchError as exc:
        print(str(exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
