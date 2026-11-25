"""Resume checkpoints.

A build writes its plan and an append-only journal into the bundle directory
before copying anything.  The plan digest pins the checkpoint to the exact
graph and descriptor list; a resumed run refuses a checkpoint whose digest
differs.  Blobs themselves are the durable state: a blob is renamed into place
only after its digest has been checked, so touching every journal-less blob and
re-hashing it is not necessary for a fast resume (``--rehash-existing`` opts
into the slow, paranoid path).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canon import STATE_SCHEMA, TOOL_NAME, TOOL_VERSION, digest_json, utc_now
from .errors import InputError, StateMismatch

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"


@dataclass
class State:
    root: Path
    plan_digest: str
    target: str
    created_at: str
    completed: list[dict[str, Any]]
    version: int = 1

    @property
    def path(self) -> Path:
        return self.root / STATE_FILE

    @property
    def journal_path(self) -> Path:
        return self.root / JOURNAL_FILE

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
            "version": self.version,
            "createdAt": self.created_at,
            "updatedAt": utc_now(),
            "planDigest": self.plan_digest,
            "target": self.target,
            "completed": list(self.completed),
        }


def plan_digest(plan_doc: dict[str, Any]) -> str:
    """Stable identity for a plan, independent of file-system metadata."""
    return digest_json(plan_doc)


def load_state(root: Path | str) -> State | None:
    root = Path(root)
    path = root / STATE_FILE
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"checkpoint {path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
        raise InputError(f"checkpoint {path} has an unknown schema")
    completed = doc.get("completed")
    if not isinstance(completed, list):
        completed = []
    journal = _read_journal(root / JOURNAL_FILE)
    if journal:
        seen = {item.get("digest") for item in completed}
        completed = completed + [item for item in journal if item.get("digest") not in seen]
    return State(
        root=root,
        plan_digest=str(doc.get("planDigest") or ""),
        target=str(doc.get("target") or ""),
        created_at=str(doc.get("createdAt") or ""),
        completed=completed,
    )


def start_state(root: Path | str, *, plan_digest_value: str, target: str) -> State:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    state = State(
        root=root,
        plan_digest=plan_digest_value,
        target=target,
        created_at=utc_now(),
        completed=[],
    )
    write_state(state)
    (root / JOURNAL_FILE).write_text("", encoding="utf-8")
    return state


def write_state(state: State) -> None:
    state.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.path.with_name(state.path.name + f".tmp.{os.getpid()}")
    payload = json.dumps(state.to_dict(), indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, state.path)


def append_journal(state: State, entry: dict[str, Any]) -> None:
    state.completed.append(entry)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with open(state.journal_path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # torn final line from an interrupted append
            if isinstance(item, dict) and isinstance(item.get("digest"), str):
                entries.append(item)
    except OSError:
        return []
    return entries


def check_compatible(state: State, expected: str, *, source: str) -> None:
    if state.plan_digest and state.plan_digest != expected:
        raise StateMismatch(
            f"checkpoint {source} belongs to a different plan "
            f"({state.plan_digest}); pass --force to discard it",
            stored=state.plan_digest,
            current=expected,
        )


def completed_digests(state: State) -> set[str]:
    return {
        item["digest"]
        for item in state.completed
        if isinstance(item, dict) and isinstance(item.get("digest"), str)
    }
