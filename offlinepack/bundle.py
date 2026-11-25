"""Bundle plan, build (resumable), verification and content-addressed store.

Bundle layout::

    <bundle>/
      bundle.json              # manifest: plan + stored blobs + reuse statistics
      state.json               # resume checkpoint
      journal.jsonl            # append-only per-blob completion log
      plan.json                # frozen plan the checkpoint is pinned to
      blobs/sha256/<hex>       # verified content, OCI layout compatible
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .canon import (
    BUNDLE_SCHEMA,
    PLAN_SCHEMA,
    TOOL_NAME,
    TOOL_VERSION,
    digest_json,
    file_digest,
    human_bytes,
    read_json,
    normalize_platform,
    short_digest,
    utc_now,
    write_json_atomic,
)
from .errors import BundleError, IncompleteBundle, InputError
from .graph import (
    NODE_ENTRY,
    PlanNode,
    Resolution,
    Resolver,
    load_deps,
    load_index,
)
from .issue import Issue, IssueKind, IssueStage
from .state import (
    JOURNAL_FILE,
    STATE_FILE,
    State,
    append_journal,
    check_compatible,
    completed_digests,
    load_state,
    start_state,
    write_state,
)
from .store import blob_path, copy_verified, CopyMismatch

BUILD_COMPLETE = "complete"
BUILD_INCOMPLETE = "incomplete"
SOURCE_KINDS = ("copy", "checkpoint", "cache", "existing")


@dataclass
class Plan:
    target: str
    entry: str
    resolution: Resolution
    document: dict[str, Any]

    @property
    def digest(self) -> str:
        return digest_json(self.document)

    @property
    def nodes(self) -> list[PlanNode]:
        return self.resolution.ordered()

    @property
    def blobs(self) -> list[PlanNode]:
        return sorted(
            (node for node in self.resolution.nodes.values() if node.has_blob),
            key=lambda node: node.chain,
        )

    @property
    def total_bytes(self) -> int:
        return sum(node.size or 0 for node in self.blobs)

    def to_dict(self) -> dict[str, Any]:
        return self.document


def build_plan(
    index_path: Path | str, deps_path: Path | str, target: str
) -> Plan:
    target = normalize_platform(target, "--target")
    deps = load_deps(deps_path)
    index = load_index(index_path, target)
    resolution = Resolver(deps, target).resolve(index)
    document = _plan_document(resolution, index.entry.name, deps.name)
    return Plan(
        target=target,
        entry=index.entry.name,
        resolution=resolution,
        document=document,
    )


def _plan_document(resolution: Resolution, entry: str, deps_name: str) -> dict[str, Any]:
    nodes = []
    for node in resolution.ordered():
        record = node.to_dict()
        record["references"] = resolution.references.get(node.digest, 0)
        nodes.append(record)
    problems = [issue.to_dict() for issue in resolution.issues]
    return {
        "schema": PLAN_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "target": resolution.target,
        "entry": entry,
        "depsManifest": deps_name,
        "nodes": nodes,
        "issues": problems,
        "summary": {
            "nodes": len(nodes),
            "blobs": len([node for node in nodes if node["kind"] != NODE_ENTRY]),
            "bytes": sum(node["size"] or 0 for node in nodes),
            "issues": len(problems),
            "errors": len([issue for issue in problems if issue["severity"] == "error"]),
        },
    }


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@dataclass
class BuildResult:
    bundle_dir: Path
    status: str
    plan: Plan
    issues: list[Issue]
    stats: dict[str, Any]
    resumed: bool
    reused_from_checkpoint: int = 0
    bundle_digest: str = ""
    already_complete: bool = False

    @property
    def ok(self) -> bool:
        return self.status == BUILD_COMPLETE


def build_bundle(
    plan: Plan,
    bundle_dir: Path | str,
    *,
    resume: bool = False,
    force: bool = False,
    rehash_existing: bool = False,
    reuse_from: Iterable[Path | str] = (),
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> BuildResult:
    """Copy every reachable descriptor into the bundle, verifying digests.

    Interrupted runs leave ``state.json`` + ``journal.jsonl``; passing
    ``resume=True`` continues from completed blobs.  A checkpoint belonging to
    a different plan is refused unless ``force=True``.
    """
    bundle = Path(bundle_dir)
    issues: list[Issue] = []
    errors = [issue for issue in plan.resolution.issues if issue.is_error]
    if errors:
        raise IncompleteBundle(
            f"plan has {len(errors)} unresolved dependency path(s); "
            f"nothing was copied",
            issues=errors,
            stage="resolve",
            bundle_dir=bundle,
        )

    bundle.mkdir(parents=True, exist_ok=True)
    expected = plan.digest
    state: State | None = None
    resumed = False
    prior = set()
    if force:
        _discard(bundle)
    existing_state = load_state(bundle)
    if existing_state is not None and (bundle / "bundle.json").exists():
        # A checkpoint left next to a finished bundle is stale by definition;
        # clean it up and start a fresh one.
        _retire_checkpoint(bundle)
        existing_state = None
    if existing_state is not None and not resume:
        raise InputError(
            f"{bundle} holds an unfinished build; pass --resume to continue it "
            f"or --force to start over"
        )
    finished = bundle / "bundle.json"
    if finished.exists() and not force:
        if not resume:
            raise InputError(
                f"{bundle} already holds a finished bundle; pass --force to replace it"
            )
        # Resuming a completed delivery is a no-op, provided it is the same plan.
        doc = load_bundle_manifest(bundle)
        if doc.get("planDigest") != expected:
            raise StateMismatch(
                f"{bundle} holds a finished bundle for a different plan "
                f"({doc.get('planDigest')}); pass --force to replace it",
                stored=str(doc.get("planDigest")),
                current=expected,
            )
        return BuildResult(
            bundle_dir=bundle,
            status=BUILD_COMPLETE,
            plan=plan,
            issues=[],
            stats=_existing_stats(doc, resumed=True),
            resumed=True,
            reused_from_checkpoint=int(
                (doc.get("summary") or {}).get("sources", {})
                .get("checkpoint", {}).get("count", 0)
            ),
            bundle_digest=str(doc.get("digest") or ""),
            already_complete=True,
        )
    if resume and existing_state is not None:
        check_compatible(existing_state, expected, source=str(bundle / "state.json"))
        state = existing_state
        prior = completed_digests(state)
        resumed = True
    else:
        state = start_state(bundle, plan_digest_value=expected, target=plan.target)
    assert state is not None
    if state.plan_digest != expected:
        check_compatible(state, expected, source=str(bundle / "state.json"))

    write_json_atomic(bundle / "plan.json", plan.to_dict())

    stored: dict[str, dict[str, Any]] = {}
    cache_dirs = [Path(item) for item in reuse_from]
    sources = {name: {"count": 0, "bytes": 0} for name in SOURCE_KINDS}
    remaining = len(plan.blobs)

    for index, node in enumerate(plan.blobs, start=1):
        source_kind = "copy"
        size = node.size or 0
        if node.digest in prior:
            path = blob_path(bundle, node.digest)
            trusted = not rehash_existing and _is_verified(path, node.size)
            if trusted:
                source_kind = "checkpoint"
                remaining -= 1
                size = _size_of(path) or (node.size or 0)
                stored[node.digest] = _blob_record(node, size, source=source_kind)
                _tally(sources, source_kind, size)
                _notify(progress, {"type": "blob", "index": index,
                                   "total": len(plan.blobs), "digest": node.digest,
                                   "source": source_kind, "bytes": size})
                continue
        seeded = _seed_from(cache_dirs, node.digest, bundle)
        if seeded is not None:
            source_kind = "cache"
            remaining -= 1
            size = seeded
            stored[node.digest] = _blob_record(node, size, source=source_kind)
            append_journal(state, {"digest": node.digest, "bytes": size,
                                   "status": "present", "at": utc_now()})
            _tally(sources, source_kind, size)
            _notify(progress, {"type": "blob", "index": index, "total": len(plan.blobs),
                               "digest": node.digest, "source": source_kind, "bytes": size})
            continue
        source = Path(node.source_path or "")
        try:
            size, was_reused = copy_verified(source, node.digest, bundle, progress=progress)
        except CopyMismatch as exc:
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=node.chain,
                    digest=node.digest,
                    from_digest=node.children[0] if node.children else None,
                    source_path=str(source),
                    detail=f"digest mismatch: {exc}",
                    stage=IssueStage.FETCH.value,
                )
            )
            continue
        except OSError as exc:
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=node.chain,
                    digest=node.digest,
                    source_path=str(source),
                    detail=f"cannot copy: {exc}",
                    stage=IssueStage.FETCH.value,
                )
            )
            continue
        if node.size is not None and size != node.size:
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=node.chain,
                    digest=node.digest,
                    source_path=str(source),
                    detail=f"size is {size}, descriptor says {node.size}",
                    stage=IssueStage.VERIFY.value,
                )
            )
            continue
        entry = {
            "digest": node.digest,
            "bytes": size,
            "status": "present",
            "at": utc_now(),
        }
        append_journal(state, entry)
        if was_reused:
            # The bytes were already valid in the store but had no journal
            # entry: a blob copied just before an interruption.
            source_kind = "existing"
        else:
            source_kind = "copy"
        stored[node.digest] = _blob_record(node, size, source=source_kind)
        _tally(sources, source_kind, size)
        _notify(progress, {"type": "blob", "index": index, "total": len(plan.blobs),
                           "digest": node.digest, "source": source_kind, "bytes": size})

    if issues:
        state.completed = list(stored.values())
        write_state(state)
        raise IncompleteBundle(
            f"{len(issues)} blob(s) failed verification; bundle is incomplete",
            issues=issues,
            stage="verify",
            bundle_dir=bundle,
        )

    manifest = _bundle_document(plan, stored, sources=sources)
    manifest["createdAt"] = utc_now()
    manifest_digest = digest_json(
        {key: value for key, value in manifest.items() if key != "createdAt"}
    )
    manifest["digest"] = manifest_digest
    _clean_partials(bundle)
    write_json_atomic(bundle / "bundle.json", manifest)
    # The checkpoint is retired on success: a finished bundle is described by
    # bundle.json alone, and a leftover "everything complete" checkpoint would
    # make a later --resume look meaningful when it is not.
    _retire_checkpoint(bundle)
    total_bytes = sum(int(item.get("bytes") or 0) for item in stored.values())
    _notify(progress, {"type": "done", "bytes": total_bytes})
    return BuildResult(
        bundle_dir=bundle,
        status=BUILD_COMPLETE,
        plan=plan,
        issues=[],
        stats={
            "totalBytes": total_bytes,
            "storedBytes": total_bytes,
            "sources": sources,
            "blobs": len(plan.blobs),
            "nodes": len(plan.nodes),
            "resumed": resumed,
        },
        resumed=resumed,
        reused_from_checkpoint=sources["checkpoint"]["count"],
        bundle_digest=manifest_digest,
    )


def _existing_stats(doc: dict[str, Any], *, resumed: bool) -> dict[str, Any]:
    summary = doc.get("summary") or {}
    return {
        "totalBytes": summary.get("bytes", 0),
        "storedBytes": summary.get("bytes", 0),
        "sources": summary.get("sources") or {},
        "blobs": summary.get("blobs", 0),
        "nodes": summary.get("nodes", 0),
        "resumed": resumed,
    }


def _blob_record(node: PlanNode, size: int, *, source: str) -> dict[str, Any]:
    return {
        "digest": node.digest,
        "bytes": size,
        "kind": node.kind,
        "mediaType": node.media_type,
        "platform": node.platform,
        "path": f"blobs/sha256/{node.digest.split(':', 1)[1]}",
        "chain": list(node.chain),
        "references": 0,
        "source": source,
    }


def _tally(sources: dict[str, dict[str, int]], name: str, size: int) -> None:
    sources[name]["count"] += 1
    sources[name]["bytes"] += size


def _seed_from(cache_dirs: list[Path], digest: str, bundle: Path) -> int | None:
    """Copy a verified blob from a previously built bundle, if present."""
    for cache in cache_dirs:
        candidate = blob_path(cache, digest)
        if not candidate.exists():
            continue
        actual, size = file_digest(candidate)
        if actual != digest:
            continue
        destination = blob_path(bundle, digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".seed")
        try:
            shutil.copyfile(candidate, partial)
            os.replace(partial, destination)
        except OSError:
            partial.unlink(missing_ok=True)
            continue
        return size
    return None


def _bundle_document(
    plan: Plan, stored: dict[str, dict[str, Any]], *, sources: dict[str, dict[str, int]]
) -> dict[str, Any]:
    blobs = []
    total = 0
    for node in plan.blobs:
        record = stored.get(node.digest)
        if record is None:
            continue
        record = dict(record)
        record["references"] = plan.resolution.references.get(node.digest, 0)
        blobs.append(record)
        total += int(record.get("bytes") or 0)
    edges = {
        node.digest: list(node.children)
        for node in plan.nodes
    }
    return {
        "schema": BUNDLE_SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "target": plan.target,
        "entry": plan.entry,
        "planDigest": plan.digest,
        "blobs": blobs,
        "edges": edges,
        "summary": {
            "blobs": len(blobs),
            "nodes": len(plan.nodes),
            "bytes": total,
            "sources": sources,
            "reusedBytes": total - sources["copy"]["bytes"] - sources["existing"]["bytes"],
            "reusedBlobs": len(blobs) - sources["copy"]["count"],
            "dedupedReferences": sum(
                max(0, blob["references"] - 1) for blob in blobs
            ),
            "dedupedBytes": sum(
                blob["bytes"] * max(0, blob["references"] - 1) for blob in blobs
            ),
        },
    }


def _discard(bundle: Path) -> None:
    for name in ("state.json", "journal.jsonl", "plan.json", "bundle.json"):
        path = bundle / name
        if path.exists():
            path.unlink()
    blobs = bundle / "blobs"
    if blobs.exists():
        shutil.rmtree(blobs)


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_verified(path: Path, expected_size: int | None) -> bool:
    """Cheap trust check for a checkpointed blob.

    Blobs are renamed into the store only after hashing, so presence (and, when
    known, the advertised size) is enough unless ``--rehash-existing`` is set.
    """
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if expected_size is not None and size != expected_size:
        return False
    return size > 0


def _clean_partials(bundle: Path) -> None:
    """Drop staged ``.part``/``.seed`` files left by an interrupted copy."""
    blobs = bundle / "blobs" / "sha256"
    if not blobs.exists():
        return
    for entry in blobs.iterdir():
        if entry.name.endswith((".part", ".seed")):
            try:
                entry.unlink()
            except OSError:
                pass


def _retire_checkpoint(bundle: Path) -> None:
    for name in (STATE_FILE, JOURNAL_FILE, "plan.json"):
        try:
            (bundle / name).unlink(missing_ok=True)
        except OSError:
            pass


def _notify(progress: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if progress is not None:
        progress(event)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class VerifyResult:
    bundle_dir: Path
    issues: list[Issue]
    checked: int
    bytes_checked: int
    manifest_digest: str

    @property
    def ok(self) -> bool:
        return not any(issue.is_error for issue in self.issues)


def load_bundle_manifest(bundle_dir: Path | str) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    path = bundle / "bundle.json"
    if not path.exists():
        raise BundleError(f"{bundle}: no bundle.json (not a completed bundle)")
    doc = read_json(path, what="bundle manifest")
    if not isinstance(doc, dict) or doc.get("schema") != BUNDLE_SCHEMA:
        raise BundleError(f"{path}: unsupported bundle schema")
    return doc


def verify_bundle(bundle_dir: Path | str) -> VerifyResult:
    """Re-hash every blob and check the manifest digest."""
    bundle = Path(bundle_dir)
    doc = load_bundle_manifest(bundle)
    issues: list[Issue] = []
    stored = doc.get("digest")
    body = {key: value for key, value in doc.items() if key not in ("digest", "createdAt")}
    computed = digest_json(body)
    if stored != computed:
        issues.append(
            Issue(
                kind=IssueKind.MISSING_FILE,
                path=("<bundle>",),
                detail=f"bundle manifest digest {stored} does not match contents {computed}",
                stage=IssueStage.VERIFY.value,
            )
        )
    checked = 0
    bytes_checked = 0
    for blob in doc.get("blobs") or []:
        digest = blob.get("digest")
        path = bundle / blob.get("path", "")
        if not path.exists():
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=tuple(blob.get("chain") or (digest,)),
                    digest=digest,
                    detail=f"missing blob {blob.get('path')}",
                    stage=IssueStage.VERIFY.value,
                )
            )
            continue
        actual, size = file_digest(path)
        checked += 1
        bytes_checked += size
        if actual != digest:
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=tuple(blob.get("chain") or (digest,)),
                    digest=digest,
                    detail=f"corrupt blob: {actual}",
                    stage=IssueStage.VERIFY.value,
                )
            )
        elif blob.get("bytes") is not None and size != blob["bytes"]:
            issues.append(
                Issue(
                    kind=IssueKind.MISSING_FILE,
                    path=tuple(blob.get("chain") or (digest,)),
                    digest=digest,
                    detail=f"size {size} != recorded {blob['bytes']}",
                    stage=IssueStage.VERIFY.value,
                )
            )
    expected = set()
    for blob in doc.get("blobs") or []:
        if blob.get("path"):
            expected.add(blob["path"])
    blobs_dir = bundle / "blobs" / "sha256"
    if blobs_dir.exists():
        for extra in blobs_dir.iterdir():
            rel = f"blobs/sha256/{extra.name}"
            if rel not in expected and extra.is_file():
                issues.append(
                    Issue(
                        kind=IssueKind.MISSING_FILE,
                        path=(extra.name,),
                        detail=f"unexpected file in store: {rel}",
                        stage=IssueStage.VERIFY.value,
                    )
                )
    return VerifyResult(
        bundle_dir=bundle,
        issues=issues,
        checked=checked,
        bytes_checked=bytes_checked,
        manifest_digest=stored or "",
    )


def describe_plan(plan: Plan) -> str:
    lines = [
        f"entry      {plan.entry} @ {plan.target}",
        f"nodes      {len(plan.nodes)}",
        f"blobs      {len(plan.blobs)}",
        f"bytes      {human_bytes(plan.total_bytes)}",
        f"plan digest {short_digest(plan.digest, 16)}",
    ]
    errors = plan.resolution.errors()
    if errors:
        lines.append(f"unresolved {len(errors)}")
    return "\n".join(lines)
