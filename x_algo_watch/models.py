from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    GRAY = 0
    YELLOW = 1
    ORANGE = 2
    RED = 3

    @property
    def label(self) -> str:
        return ("灰", "黄", "橙", "红")[int(self)]


class WatchError(Exception):
    """可公开的错误信息；不包含本机命令、路径或认证信息。"""


class UnsafePublicText(WatchError):
    """只携带固定命中类别，不保存匹配到的内容。"""

    def __init__(self, category: str):
        self.category = category
        super().__init__("输出含疑似凭据、本机路径或邮箱，已阻止写入；需人工复核")


class ParseError(WatchError):
    pass


@dataclass(frozen=True)
class Parameter:
    name: str
    rust_type: str
    key: str
    value: Any
    raw: str
    line: int


@dataclass(frozen=True)
class Finding:
    category: str
    severity: Severity
    path: str
    name: str
    message: str
    before: Any = None
    after: Any = None
    line_before: int | None = None
    line_after: int | None = None
    evidence: str = ""
    fact_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "severity": self.severity.label}


@dataclass
class Snapshot:
    commit: str
    committed_at: str
    tree: dict[str, str]
    params: dict[str, Parameter]
    sync: str
    filters: tuple[str, ...]
    modules: tuple[str, ...]
    levels: dict[str, tuple[str, ...]] | None
    updates: str
    level_format: str | None


@dataclass
class CheckResult:
    base: str | None
    commit: str
    committed_at: str
    findings: list[Finding] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    complete: bool = True
    mode: str = "compare"
    numeric_context: dict[str, Any] = field(default_factory=dict)
    registry_hash: str = ""

    @property
    def severity(self) -> Severity:
        return max((f.severity for f in self.findings), default=Severity.GRAY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base, "commit": self.commit,
            "committed_at": self.committed_at, "complete": self.complete,
            "mode": self.mode, "severity": self.severity.label,
            "findings": [f.to_dict() for f in self.findings],
            "verification": self.verification, "counts": self.counts,
            "numeric_context": self.numeric_context,
            "registry_hash": self.registry_hash,
        }
