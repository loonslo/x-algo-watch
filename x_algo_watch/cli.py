import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .engine import compare
from .git import UPSTREAM, Repository, discover, run_git
from .models import WatchError
from .registry import load_registry
from .storage import RecordStore
from .evidence import digest


def code_version(root: Path) -> str:
    try:
        return Repository(root).resolve("HEAD")
    except WatchError:
        return "uncommitted"


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="只读检查 X 推荐算法公开源码")
    sub = cli.add_subparsers(dest="command", required=True)
    for command in ("compare", "replay", "check", "verify"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--repo", type=Path, help="上游本机副本（只读）；check 不传时使用临时裸克隆")
        cmd.add_argument("--registry", type=Path, default=Path("registry/facts.yaml"))
        cmd.add_argument("--output", type=Path, default=Path("data"))
        cmd.add_argument("--to", default="refs/remotes/origin/main", help="离线目标；check 不传 --repo 时固定远程 main")
        cmd.add_argument("--now", help="固定运行时间，必须带时区，供可重复回放")
        if command in ("compare", "replay"):
            cmd.add_argument("--from", dest="base", required=True, help="基线提交（包含）")
        elif command == "check":
            cmd.add_argument("--baseline", help="首次运行必须明确指定基线提交")
    return cli


def execute(args: argparse.Namespace, repo: Repository, target: str, now: datetime, version: str) -> int:
    facts = load_registry(args.registry)
    store = RecordStore(args.output)
    target = repo.resolve(target)
    if args.command == "verify":
        result = compare(repo, target, target, facts, now.date())
        store.write_verification(result)
        store.append_event({"type": "verification", "id": "verify:" + digest(result.to_dict()), "commit": target, "result": result.to_dict()}, now)
        store.status(now, version, None if result.complete else "解析失败，进度未推进")
        return 0 if result.complete else 2
    if args.command == "compare":
        base = repo.resolve(args.base)
        result = compare(repo, base, target, facts, now.date())
        store.append(result, now, version); store.status(now, version, None if result.complete else "解析失败，进度未推进")
        print(json.dumps({"commit": target, "severity": result.severity.label, "complete": result.complete}, ensure_ascii=False))
        return 0 if result.complete else 2
    if args.command == "replay":
        base = repo.resolve(args.base)
    else:
        base = store.last_success()
        if base is None:
            if not args.baseline:
                raise WatchError("首次检查须用 --baseline 明确指定基线提交")
            base = repo.resolve(args.baseline)
    # Git 无共同祖先也属于历史改写，不能按无变化处理。
    try:
        ancestor = repo.is_ancestor(base, target)
    except WatchError:
        ancestor = False
    if not ancestor:
        result = compare(repo, base, target, facts, now.date(), rewrite=True)
        store.append(result, now, version); store.status(now, version, "上游历史改写；已全量核验" if result.complete else "解析失败，进度未推进")
        return 0 if result.complete else 2
    completed = store.successful()
    if args.command == "check" and base == target and base in completed:
        result = compare(repo, target, target, facts, now.date())
        store.write_verification(result)
        store.append_event({"type": "verification", "id": "verify:" + digest(result.to_dict()), "commit": target, "result": result.to_dict()}, now)
        store.status(now, version, None if result.complete else "解析失败，进度未推进")
        return 0 if result.complete else 2
    commits = ([base] if base not in completed else []) + repo.commits(base, target)
    count = 0
    for commit in commits:
        if commit in completed:
            continue
        parent = None if commit == base else repo.parent(commit)
        result = compare(repo, parent, commit, facts, now.date())
        # 同次回放用微秒递增，保留拓扑处理顺序。
        timestamp = now + timedelta(microseconds=count)
        count += int(store.append(result, timestamp, version))
        print(json.dumps({"commit": commit, "severity": result.severity.label, "complete": result.complete}, ensure_ascii=False))
        if not result.complete:
            store.status(now, version, "解析失败，进度未推进；下轮从失败提交补跑")
            return 2
    store.status(now, version)
    print(json.dumps({"new_records": count, "target": target}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    version = code_version(Path.cwd())
    if args.repo and args.output.resolve().is_relative_to(args.repo.resolve()):
        print("输出目录不能位于上游副本内", file=sys.stderr)
        return 2
    try:
        if args.now:
            try:
                now = datetime.fromisoformat(args.now)
            except ValueError as exc:
                raise WatchError("--now 不是有效的 ISO 时间") from exc
            if now.tzinfo is None:
                raise WatchError("--now 必须包含时区")
            now = now.astimezone(timezone.utc)
        if args.repo:
            return execute(args, Repository(args.repo), args.to, now, version)
        if args.command != "check":
            raise WatchError("compare / replay 必须指定 --repo")
        target = discover()
        store = RecordStore(args.output)
        verification_path = args.output / "verification.yaml"
        verification = yaml.safe_load(verification_path.read_text(encoding="utf-8")) if verification_path.exists() else {}
        facts = load_registry(args.registry)
        # 登记表改变也需重新核验，不能因为 main 未动而跳过。
        if store.last_success() == target and verification.get("registry_hash") == digest(facts):
            from .registry import refresh_manual
            refresh_manual(args.output, facts, now.date())
            store.status(now, version)
            print("无新提交；已更新当日状态")
            return 0
        with tempfile.TemporaryDirectory(prefix="x-algo-watch-") as folder:
            clone = Path(folder) / "upstream.git"
            run_git(["clone", "--bare", "--quiet", UPSTREAM, str(clone)])
            repo = Repository(clone)
            # ls-remote 与 clone 之间 main 若变化，本轮仍处理已发现的固定提交。
            return execute(args, repo, target, now, version)
    except WatchError as exc:
        # 错误文字由程序生成，不打印 Git stderr 或本机命令。
        message = str(exc)
        RecordStore(args.output).failed_attempt(now, message)
        RecordStore(args.output).status(now, version, message)
        print(message, file=sys.stderr)
        return 2
