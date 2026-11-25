"""Exception types shared by the offlinepack engine."""

from __future__ import annotations

from typing import Iterable, Sequence


class OfflinePackError(Exception):
    """Base class for every expected failure."""


class InputError(OfflinePackError):
    """The index, dependency manifest or arguments are malformed."""


class StateMismatch(OfflinePackError):
    """A resume checkpoint belongs to a different plan."""

    def __init__(self, message: str, *, stored: str, current: str) -> None:
        super().__init__(message)
        self.stored = stored
        self.current = current


class BundleError(OfflinePackError):
    """A bundle directory is missing or inconsistent."""


class IncompleteBundle(OfflinePackError):
    """Content could not be assembled; carries machine readable issues."""

    def __init__(
        self,
        message: str,
        *,
        issues: Sequence,
        stage: str,
        bundle_dir=None,
    ) -> None:
        super().__init__(message)
        self.issues = list(issues)
        self.stage = stage
        self.bundle_dir = bundle_dir

    def render(self) -> str:
        lines = [str(self)]
        for issue in self.issues:
            lines.append("  " + issue.render())
        return "\n".join(lines)


def format_issues(issues: Iterable) -> str:
    return "\n".join(issue.render() for issue in issues)
