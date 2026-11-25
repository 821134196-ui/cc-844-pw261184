"""Structured, path-carrying issues.

Every issue knows the dependency chain from the entry point to the exact node
that failed, plus the specific edge that was missing, so a partial plan can
still say *which* layer is absent and *where* it was first required.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class IssueKind(str, enum.Enum):
    NO_PLATFORM = "no-platform"
    MISSING_FILE = "missing-file"
    MISSING_DESCRIPTOR = "missing-descriptor"
    UNREADABLE_MANIFEST = "unreadable-manifest"
    MISSING_BLOB = "missing-blob"


ERROR = "error"
WARNING = "warning"

_WARNINGS: set["IssueKind"] = set()


class IssueStage(str, enum.Enum):
    RESOLVE = "resolve"
    FETCH = "fetch"
    VERIFY = "verify"
    PACK = "pack"


@dataclass
class Issue:
    kind: IssueKind
    path: tuple[str, ...]
    detail: str = ""
    digest: str | None = None
    from_digest: str | None = None
    source_path: str | None = None
    stage: str = IssueStage.RESOLVE.value
    severity: str = ""

    def __post_init__(self) -> None:
        if not self.severity:
            self.severity = WARNING if self.kind in _WARNINGS else ERROR

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def chain(self) -> str:
        return " -> ".join(self.path) if self.path else "<entry>"

    def render(self) -> str:
        suffix = f"  [{self.detail}]" if self.detail else ""
        return f"{self.kind.value}: {self.chain()}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity,
            "stage": self.stage,
            "path": list(self.path),
            "digest": self.digest,
            "fromDigest": self.from_digest,
            "sourcePath": self.source_path,
            "detail": self.detail,
            "chain": self.chain(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            kind=IssueKind(data["kind"]),
            path=tuple(data.get("path") or ()),
            detail=data.get("detail", ""),
            digest=data.get("digest"),
            from_digest=data.get("fromDigest"),
            source_path=data.get("sourcePath"),
            stage=data.get("stage", IssueStage.RESOLVE.value),
            severity=data.get("severity", ""),
        )


def short_label(digest: str, length: int = 12) -> str:
    if not isinstance(digest, str):
        return str(digest)
    hexpart = digest.split(":", 1)[-1]
    return f"sha256:{hexpart[:length]}"


def format_issues(issues: list[Issue]) -> str:
    return "\n".join(issue.render() for issue in issues)
