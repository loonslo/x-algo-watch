import os
import subprocess
from pathlib import Path

import pytest

from x_algo_watch.git import Repository
from x_algo_watch.parsers import REQUIRED_LEVELS


def pytest_addoption(parser):
    parser.addoption("--upstream-repo", default=os.environ.get("X_ALGO_UPSTREAM"), help="历史回放用的只读上游 Git 副本")


@pytest.fixture(scope="session")
def upstream(request):
    path = request.config.getoption("--upstream-repo")
    if not path:
        pytest.skip("历史验收需 --upstream-repo 或 X_ALGO_UPSTREAM")
    return Repository(Path(path))


@pytest.fixture
def sample_repo(tmp_path):
    path = tmp_path / "upstream"
    path.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True, encoding="utf-8").strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@users.noreply.github.com")
    files = {
        "home-mixer/params/param.rs": '// last sync 2026-10-20T16:00:00Z\nparam!(EnableExampleSource, bool, "enable", false);\nparam!(FavoriteWeight, f64, "favorite", 0.5);\n',
        "home-mixer/filters/mod.rs": "pub mod example_filter;\n",
        "home-mixer/filters/example_filter.rs": "// text only\n",
        "visibility-filtering/rules/levels.rs": "\n".join(f"pub(super) const {name}: &[&Rule] = &[&FIRST];" for name in sorted(REQUIRED_LEVELS)),
        "README.md": "# Test\n\n## Notable Updates\n\nInitial.\n\n## Other\nText\n",
    }
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit():
        git("add", ".")
        git("commit", "-m", "fixture")
        return git("rev-parse", "HEAD")

    baseline = commit()
    return path, Repository(path), baseline, commit
