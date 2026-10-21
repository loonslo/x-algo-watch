import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .git import SHA
from .models import CheckResult, Severity, WatchError

RECORD_PATTERN = re.compile(r"<!-- xaw-record -->\n```json\n(.*?)\n```\n<!-- /xaw-record -->", re.S)
EVENT_PATTERN = re.compile(r"<!-- xaw-event -->\n```json\n(.*?)\n```\n<!-- /xaw-event -->", re.S)


def public_text(text: str) -> None:
    patterns = (
        r"sk-or-v1-[A-Za-z0-9_-]{12,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"[A-Za-z]:[\\/](?:Users|workspace|home)[\\/]", r"/(?:Users|home)/[^/\s]+/",
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    )
    if any(re.search(pattern, text) for pattern in patterns):
        raise WatchError("输出含疑似凭据、本机路径或邮箱，已阻止写入；需人工复核")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".new")
    staging.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staging, path)


class RecordStore:
    def __init__(self, root: Path):
        self.root = root

    def records(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted((self.root / "records").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            matches = RECORD_PATTERN.findall(text)
            if text.count("<!-- xaw-record") != len(matches):
                raise WatchError("检查记录格式损坏；拒绝猜测进度")
            for raw in matches:
                try:
                    record = json.loads(raw)
                except ValueError as exc:
                    raise WatchError("检查记录 JSON 损坏") from exc
                if not isinstance(record, dict) or record.get("schema_version") != 1 or not SHA.fullmatch(record.get("commit", "")) or type(record.get("complete")) is not bool:
                    raise WatchError("检查记录的提交号或结构无效")
                result.append(record)
        # 写入时间是运行顺序，不能按上游提交时间推算进度（分支/历史改写可能倒序）。
        return sorted(result, key=lambda record: record["checked_at"])

    def last_success(self) -> str | None:
        return next((r["commit"] for r in reversed(self.records()) if r["complete"]), None)

    def successful(self) -> set[str]:
        return {r["commit"] for r in self.records() if r["complete"]}

    def events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in sorted((self.root / "records").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            matches = EVENT_PATTERN.findall(text)
            if text.count("<!-- xaw-event") != len(matches):
                raise WatchError("事件记录损坏")
            for raw in matches:
                try:
                    event = json.loads(raw)
                except ValueError as exc:
                    raise WatchError("事件 JSON 损坏") from exc
                if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
                    raise WatchError("事件结构无效")
                events.append(event)
        return events

    def append_event(self, event: dict[str, Any], now: datetime) -> bool:
        if any(item["id"] == event["id"] for item in self.events()):
            return False
        payload = {**event, "created_at": now.isoformat()}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        public_text(text)
        path = self.root / "records" / (now.strftime("%Y-%m") + ".md")
        existing = path.read_text(encoding="utf-8") if path.exists() else "# 检查记录\n"
        block = "\n### 事件：" + event["type"] + "\n\n<!-- xaw-event -->\n```json\n" + text + "\n```\n<!-- /xaw-event -->\n"
        atomic_write(path, existing + block)
        return True

    def write_verification(self, result: CheckResult) -> None:
        text = yaml.safe_dump({"schema_version": 1, "commit": result.commit, "registry_hash": result.registry_hash, "complete": result.complete, "items": result.verification}, allow_unicode=True, sort_keys=False)
        public_text(text)
        path = self.root / "verification.yaml"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write(path, text)

    def append(self, result: CheckResult, now: datetime, code_commit: str) -> bool:
        payload = result.to_dict()
        identity = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        records = self.records()
        if any(r.get("identity") == identity for r in records):
            self.write_verification(result)
            return False
        if result.complete and result.mode != "rewrite" and any(r["commit"] == result.commit and r["complete"] for r in records):
            self.write_verification(result)
            return False
        payload.update(schema_version=1, identity=identity, checked_at=now.isoformat(), code_commit=code_commit)
        month = now.strftime("%Y-%m")
        path = self.root / "records" / (month + ".md")
        text = path.read_text(encoding="utf-8") if path.exists() else f"# {month} 检查记录\n\n仅记录公开代码默认值；不代表线上实际取值。\n"
        title_time = datetime.fromisoformat(result.committed_at).astimezone(timezone.utc).isoformat()
        block = f"\n## {result.commit[:7]} · {title_time} · 级别：{result.severity.label}\n\n"
        block += f"- 对比：{(result.base or '基线')[:7]} → {result.commit[:7]}\n"
        block += f"- 处理状态：{'已检查' if result.complete else '解析失败／本次未检查，进度未推进'}\n"
        if "files" in result.counts:
            block += f"- 范围：{result.counts['files']} 个文件，+{result.counts['added']}/−{result.counts['removed']}\n"
        for finding in result.findings:
            before = json.dumps(finding.before, ensure_ascii=False)
            after = json.dumps(finding.after, ensure_ascii=False)
            values = f"；{before} → {after}" if len(before + after) < 350 else "；完整变化见证据数据"
            block += f"- 【{finding.severity.label}】{finding.name}：{finding.message}{values}\n"
            if finding.path and not finding.path.startswith("https://"):
                block += f"  - 来源：`{finding.path}` @ `{result.commit[:7]}`"
                if finding.line_after is not None:
                    block += f"，新行号 {finding.line_after}"
                block += "\n"
            if finding.category == "parse_failure":
                block += "  - " + finding.evidence.replace("\n", " ") + "\n"
        if not result.findings:
            block += "- 已检查：观察范围内无变化\n"
        block += "\n<details><summary>程序证据数据（版本 1）</summary>\n\n"
        block += "<!-- xaw-record -->\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n<!-- /xaw-record -->\n\n</details>\n"
        verification = yaml.safe_dump({"schema_version": 1, "commit": result.commit, "registry_hash": result.registry_hash, "complete": result.complete, "items": result.verification}, allow_unicode=True, sort_keys=False)
        for content in (text + block, verification):
            public_text(content)
        atomic_write(self.root / "verification.yaml", verification)
        atomic_write(path, text + block)
        return True

    def status(self, now: datetime, code_commit: str, error: str | None = None) -> None:
        records = self.records()
        latest = next((r for r in reversed(records) if r["complete"]), None)
        pending = {r["commit"] for r in records if r["severity"] in ("红", "橙")}
        pending.update(event["commit"] for event in self.events() if event["type"] == "verification" and event.get("result", {}).get("severity") in ("红", "橙"))
        verification_path = self.root / "verification.yaml"
        verification = yaml.safe_load(verification_path.read_text(encoding="utf-8")) if verification_path.exists() else {}
        items = (verification or {}).get("items", (latest or {}).get("verification", []))
        overdue = [item["id"] for item in items if item.get("status") in ("overdue", "unreviewed")]
        decisions = {event["commit"] for event in self.events() if event["type"] == "decision"}
        pending -= decisions
        path = self.root / "status.md"
        text = ("# 当前状态\n\n"
                f"- 运行代码提交：{code_commit}\n"
                f"- 最后处理的上游提交：{latest['commit'] if latest else '尚未建立基线'}\n"
                f"- 最近一次成功检查：{now.astimezone(timezone.utc).date().isoformat() if not error else (latest['checked_at'] if latest else '尚无')}\n"
                f"- 最近检查日期（UTC）：{now.astimezone(timezone.utc).date().isoformat()}\n"
                f"- 待复核提交数：{len(pending)}\n"
                f"- 过期人工核对项：{', '.join(overdue) or '无'}\n"
                f"- 本次状态：{error or '检查完成'}\n\n"
                "进度以检查记录中最后一条完整记录为准；本文件可重建。\n")
        public_text(text)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write(path, text)

    def failed_attempt(self, now: datetime, message: str) -> None:
        public_text(message)
        path = self.root / "records" / (now.strftime("%Y-%m") + ".md")
        text = path.read_text(encoding="utf-8") if path.exists() else "# 检查记录\n"
        # 无提交号的网络/配置失败不生成进度标记。
        atomic_write(path, text + f"\n### 本次未检查 · {now.isoformat()}\n\n- {message}\n")
