"""使用已有可信证据准备单版本样例；不改进度、不访问模型密钥。"""

import argparse
from pathlib import Path
import shutil

from .automation import data_files, write_bundle
from .git import SHA
from .models import WatchError
from .project_evidence import context_packs
from .storage import RecordStore


def prepare_sample(root: Path, commit: str, bundle: Path) -> None:
    if not SHA.fullmatch(commit):
        raise WatchError("样例必须指定完整提交号")
    runtime = (root / "runtime").resolve()
    if bundle.resolve() == runtime or not bundle.resolve().is_relative_to(runtime):
        raise WatchError("样例输出必须位于项目 runtime 的新子目录")
    data = root / "data"
    if not any(r["commit"] == commit and r["complete"] and r.get("base") for r in RecordStore(data).records()):
        raise WatchError("样例需要已有完整迭代记录")
    if not any(p["commit"] == commit for p in context_packs(data)):
        raise WatchError("样例没有可信项目上下文")
    if bundle.exists():
        raise WatchError("样例输出目录已存在，请使用新目录")
    working = bundle / "working"
    shutil.copytree(data, working)
    write_bundle(bundle, data_files(data), working, test=False, code="single-version-sample", result_code=0, research_only=commit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare_sample(Path.cwd(), args.commit, args.bundle)
        return 0
    except WatchError as exc:
        print(str(exc)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
