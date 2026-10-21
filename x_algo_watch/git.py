"""所有上游操作均为 Git 对象读取；不 checkout、fetch 或执行上游程序。"""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from .models import WatchError

UPSTREAM = "https://github.com/xai-org/x-algorithm"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise WatchError("来源路径必须是仓库内的相对路径")
    return value


def run_git(args: list[str], cwd: Path | None = None, data: bytes | None = None, *, allowed_codes: tuple[int, ...] = (0,)) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-c", "core.hooksPath=", *args],
            cwd=cwd, input=data, capture_output=True, timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatchError("Git 操作不可用或超时；本次未检查") from exc
    if result.returncode not in allowed_codes:
        raise WatchError("Git 对象或历史不可读；本次未检查")
    return result.stdout


def discover() -> str:
    output = run_git(["ls-remote", UPSTREAM, "refs/heads/main"]).decode()
    sha = output.split()[0] if output.split() else ""
    if not SHA.fullmatch(sha):
        raise WatchError("上游 main 未返回有效提交号；本次未检查")
    return sha


class Repository:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._blobs: dict[str, str] = {}
        self._trees: dict[str, dict[str, str]] = {}
        self._resolutions: dict[str, str] = {}
        self._ancestors: dict[tuple[str, str], bool] = {}

    def git(self, *args: str) -> str:
        return run_git(list(args), self.path).decode("utf-8")

    def resolve(self, ref: str) -> str:
        immutable = bool(re.fullmatch(r"[0-9a-f]{7,40}", ref))
        if immutable and ref in self._resolutions:
            return self._resolutions[ref]
        if not re.fullmatch(r"[A-Za-z0-9_./^~{}-]+", ref) or ref.startswith("-"):
            raise WatchError("无效的 Git 引用")
        sha = self.git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
        if not SHA.fullmatch(sha):
            raise WatchError("无效的提交号")
        if immutable:
            self._resolutions[ref] = sha
        return sha

    def tree(self, commit: str) -> dict[str, str]:
        if commit not in self._trees:
            entries = self.git("ls-tree", "-rz", "--full-tree", commit).split("\0")
            self._trees[commit] = {
                path: metadata.split()[2]
                for entry in entries if entry
                for metadata, path in [entry.split("\t", 1)]
                if metadata.split()[1] == "blob" and metadata.split()[0] != "120000"
            }
        return self._trees[commit]

    def read(self, commit: str, path: str) -> str | None:
        oid = self.tree(commit).get(safe_path(path))
        if oid is None:
            return None
        if oid not in self._blobs:
            self._blobs[oid] = self.git("cat-file", "blob", oid)
        return self._blobs[oid]

    def committed_at(self, commit: str) -> str:
        return self.git("show", "-s", "--format=%cI", commit).strip()

    def snippet_candidates(self, commit: str, snippet: str) -> list[str]:
        # 用 Git 对象内最长的一行缩小候选，不逐个启动进程遍历全仓库。
        anchor = max(snippet.splitlines(), key=len)
        raw = run_git(["grep", "-I", "-l", "-F", "--no-textconv", "-e", anchor, commit, "--"], self.path, allowed_codes=(0, 1)).decode("utf-8")
        return sorted({line.split(":", 1)[1] for line in raw.splitlines() if ":" in line})

    def parent(self, commit: str) -> str | None:
        parents = self.git("rev-list", "--parents", "-n", "1", commit).split()
        return parents[1] if len(parents) > 1 else None

    def is_ancestor(self, base: str, target: str) -> bool:
        key = (base, target)
        if key not in self._ancestors:
            self._ancestors[key] = self.git("merge-base", base, target).strip() == base
        return self._ancestors[key]

    def commits(self, base: str, target: str) -> list[str]:
        return self.git("rev-list", "--reverse", "--topo-order", base + ".." + target).split()

    def stats(self, base: str, target: str) -> dict[str, int]:
        lines = self.git("diff", "--no-ext-diff", "--no-textconv", "--numstat", base, target, "--").splitlines()
        added = removed = 0
        for line in lines:
            a, r, _ = line.split("\t", 2)
            added += int(a) if a.isdigit() else 0
            removed += int(r) if r.isdigit() else 0
        return {"files": len(lines), "added": added, "removed": removed}

    def diff(self, base: str, target: str, path: str) -> str:
        return self.git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3", base, target, "--", safe_path(path))
