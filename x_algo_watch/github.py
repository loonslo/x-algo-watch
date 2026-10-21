"""GitHub 边界：固定 API 主机；所有 Issue 文本仅作为 JSON 数据传输。"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import WatchError

BOT = "github-actions[bot]"


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise WatchError("GitHub 仓库或运行令牌配置无效")
        self.repository = repository
        self.owner = repository.split("/")[0]
        self._token = token

    def request(self, method: str, path: str, data: Any = None) -> Any:
        if not path.startswith("/") or ".." in path or "\n" in path:
            raise WatchError("GitHub API 路径无效")
        request = urllib.request.Request(
            "https://api.github.com/repos/" + self.repository + path,
            data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json", "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = response.read()
            return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            raise WatchError(f"GitHub API 请求失败（HTTP {exc.code}）；下轮幂等重试") from None
        except (OSError, ValueError) as exc:
            raise WatchError("GitHub API 连接或响应异常；下轮幂等重试") from exc

    def pages(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 1001):
            separator = "&" if "?" in path else "?"
            rows = self.request("GET", path + separator + urllib.parse.urlencode({"per_page": 100, "page": page}))
            if not isinstance(rows, list):
                raise WatchError("GitHub 分页返回结构异常")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise WatchError("GitHub 分页超过上限，未完成读取")

    def issues(self) -> list[dict[str, Any]]:
        return [issue for issue in self.pages("/issues?state=all&labels=x-algo-watch") if "pull_request" not in issue]

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self.pages(f"/issues/{int(number)}/comments")

    def ensure_labels(self) -> None:
        labels = {"x-algo-watch": "5319e7", "级别:红": "d73a4a", "级别:橙": "ff8c00", "待起草": "fbca04", "待确认": "0075ca", "需人工": "b60205", "已确认": "0e8a16", "驳回": "666666", "测试": "bfdadc"}
        existing = {label["name"] for label in self.pages("/labels")}
        for name, color in labels.items():
            if name not in existing:
                self.request("POST", "/labels", {"name": name, "color": color})


def issue_marker(commit: str, test: bool = False) -> str:
    return f"<!-- xaw-{'test' if test else 'commit'}:{commit} -->"


def bot_owned(value: dict[str, Any]) -> bool:
    return value.get("user", {}).get("login") == BOT and value.get("user", {}).get("type") == "Bot"
