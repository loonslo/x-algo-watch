"""只读上线审计；使用 gh 的现有登录，不读取或输出认证令牌。"""

import argparse
import json
import re
import subprocess
from typing import Any

from .models import WatchError


def query(repository: str, suffix: str) -> Any:
    try:
        result = subprocess.run(["gh", "api", "repos/" + repository + suffix], capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatchError("GitHub CLI 不可用或超时") from exc
    if result.returncode:
        return {"unavailable": True}
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise WatchError("GitHub 配置响应不是有效 JSON") from exc


def audit(repository: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise WatchError("仓库名无效")
    repo = query(repository, "")
    if repo.get("unavailable"):
        return {"repository": repository, "repository_accessible": False, "ready": False}
    permissions = query(repository, "/actions/permissions/workflow")
    environment = query(repository, "/environments/openrouter")
    branches = query(repository, "/environments/openrouter/deployment-branch-policies")
    secrets = query(repository, "/environments/openrouter/secrets")
    repository_secrets = query(repository, "/actions/secrets")
    variables = query(repository, "/actions/variables")
    security = repo.get("security_and_analysis", {})
    checks = {
        "public": repo.get("visibility") == "public",
        "default_main": repo.get("default_branch") == "main",
        "actions_default_read": permissions.get("default_workflow_permissions") == "read",
        "secret_scanning": security.get("secret_scanning", {}).get("status") == "enabled",
        "push_protection": security.get("secret_scanning_push_protection", {}).get("status") == "enabled",
        "openrouter_environment": not environment.get("unavailable", False),
        "main_only": (environment.get("deployment_branch_policy") or {}).get("custom_branch_policies") is True and [(policy["name"], policy.get("type", "branch")) for policy in branches.get("branch_policies", [])] == [("main", "branch")],
        "key_present": "OPENROUTER_API_KEY" in [secret["name"] for secret in secrets.get("secrets", [])],
        "no_repository_model_key": not repository_secrets.get("unavailable") and "OPENROUTER_API_KEY" not in [secret["name"] for secret in repository_secrets.get("secrets", [])],
        "draft_enabled": any(var["name"] == "DRAFT_ENABLED" and var["value"] == "true" for var in variables.get("variables", [])),
    }
    return {"repository": repository, "repository_accessible": True, "checks": checks, "ready": all(checks.values()), "note": "只审计配置存在性；不证明通知到达、模型质量或连续 24 小时可靠性。"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(args.repository)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 2
    except WatchError as exc:
        print(str(exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
