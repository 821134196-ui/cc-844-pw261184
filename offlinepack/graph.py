"""Image index and dependency manifest parsing, plus the recursive resolver.

Two graphs feed one plan:

* **Image index** - names an entry image.  For the target platform one manifest
  is selected (inline descriptor list or a local OCI layout directory) and its
  ``config``, ``layers`` and nested image manifests are walked.  A layer whose
  media type is a manifest/index is treated as a nested image, so multi-level
  stacks recurse to arbitrary depth.
* **Local dependency manifest** - a digest-keyed registry of local files plus a
  per-digest ``deps`` map.  Local artifacts can attach to any node and are
  pinned by digest, so resolution terminates even when the graph is cyclic.

Every unreachable node records the chain from the entry point to the exact
missing edge, so a partial plan still says which layer is absent and where it
was first required.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canon import (
    DEPS_SCHEMA,
    DEFAULT_ARTIFACT_MEDIA_TYPE,
    DEFAULT_LAYER_MEDIA_TYPE,
    INDEX_SCHEMA,
    normalize_platform,
    read_json,
    require_digest,
)
from .errors import InputError
from .issue import Issue, IssueKind, IssueStage, short_label

NODE_ENTRY = "entry"
NODE_IMAGE = "image"
NODE_CONFIG = "config"
NODE_LAYER = "layer"
NODE_ARTIFACT = "artifact"

NODE_KINDS = (NODE_ENTRY, NODE_IMAGE, NODE_CONFIG, NODE_LAYER, NODE_ARTIFACT)

OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
IMAGE_MEDIA_TYPES = OCI_INDEX_MEDIA_TYPES | OCI_MANIFEST_MEDIA_TYPES

_PLATFORM_KEYS = ("os", "architecture", "variant")


# --------------------------------------------------------------------------
# Descriptors and inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Descriptor:
    digest: str
    path: str | None = None
    size: int | None = None
    media_type: str | None = None
    platform: str | None = None

    @property
    def is_image(self) -> bool:
        return self.media_type in IMAGE_MEDIA_TYPES


@dataclass
class ImageRef:
    name: str
    digest: str


@dataclass
class LoadedIndex:
    entry: ImageRef
    selected: Descriptor
    target: str


@dataclass
class DepManifest:
    artifacts: dict[str, Descriptor] = field(default_factory=dict)
    deps: dict[str, list[Descriptor]] = field(default_factory=dict)
    name: str = ""


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected a JSON object")
    return value


def _string(value: Any, where: str, key: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise InputError(f"{where}: '{key}' must be a non-empty string")
    return value


def _descriptor(raw: Any, where: str, *, require_path: bool = False) -> Descriptor:
    obj = _object(raw, where)
    digest = require_digest(_string(obj.get("digest"), where, "digest",
                                    optional=False), where)
    path = _string(obj.get("path", obj.get("localPath")), where, "path")
    if require_path and path is None:
        raise InputError(f"{where}: descriptor needs a local 'path' to fetch from")
    size = obj.get("size")
    if size is not None and (
        not isinstance(size, int) or isinstance(size, bool) or size < 0
    ):
        raise InputError(f"{where}: 'size' must be a non-negative integer")
    media_type = _string(obj.get("mediaType"), where, "mediaType")
    platform = obj.get("platform")
    if isinstance(platform, dict):
        # OCI descriptors carry platform as an object; keep the normalized
        # string so selection and reporting compare like with like.
        platform = _platform_string(platform)
    elif platform is not None:
        platform = normalize_platform(platform, where)
    return Descriptor(
        digest=digest,  # type: ignore[arg-type]
        path=path,
        size=size,
        media_type=media_type,
        platform=platform,
    )


def _relative(descriptor: Descriptor, base: Path) -> Descriptor:
    if descriptor.path is None:
        return descriptor
    path = Path(descriptor.path)
    if not path.is_absolute():
        path = base / path
    return Descriptor(
        digest=descriptor.digest,
        path=str(path.resolve()),
        size=descriptor.size,
        media_type=descriptor.media_type,
        platform=descriptor.platform,
    )


def load_index(path: Path | str, target: str) -> LoadedIndex:
    """Parse the image index and pick the manifest for ``target``.

    Shape::

        {
          "schema": "offlinepack/index@1",
          "name": "payments",
          "images": [
            {"name": "payments", "manifests": [
               {"digest": "sha256:...", "path": "m/amd64.json", "size": 812,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"os": "linux", "architecture": "amd64"}},
               {"digest": "sha256:...", "path": "m/arm64.json", ...,
                "platform": {"os": "linux", "architecture": "arm64"}}]}
          ]
        }

    ``path`` may point at a manifest JSON file or at an OCI layout directory
    containing ``index.json``.  Only ``images[0]`` is the entry point.
    """
    doc = _object(read_json(path, what="image index"), "image index")
    schema = doc.get("schema", INDEX_SCHEMA)
    if schema not in (INDEX_SCHEMA, "oci/index@1"):
        raise InputError(f"image index: unsupported schema {schema!r}")
    images = doc.get("images")
    if not isinstance(images, list) or not images:
        raise InputError("image index: 'images' must be a non-empty array")
    image = _object(images[0], "image index: images[0]")
    name = _string(image.get("name"), "image index: images[0]", "name",
                   optional=False)
    assert name is not None
    base = Path(path).resolve().parent
    digest = _string(image.get("digest"), "image index: images[0]", "digest")
    if digest is not None:
        digest = require_digest(digest, "image index: images[0]")
    selected = _select_manifest(image, name, target, base)
    return LoadedIndex(
        entry=ImageRef(name=name, digest=digest or selected.digest),
        selected=selected,
        target=target,
    )


class NoPlatformError(InputError):
    def __init__(self, message: str, *, available: list[str]) -> None:
        super().__init__(message)
        self.available = available


def _select_manifest(
    image: dict[str, Any], name: str, target: str, base: Path
) -> Descriptor:
    where = f"image index: {name}"
    entries = image.get("manifests")
    if isinstance(entries, list):
        choices: list[tuple[Descriptor, str | None, Path | None]] = []
        for pos, item in enumerate(entries):
            descriptor = _descriptor(item, f"{where}.manifests[{pos}]")
            obj = _object(item, f"{where}.manifests[{pos}]")
            platform = obj.get("platform")
            choices.append(
                (
                    descriptor,
                    _platform_string(platform) if isinstance(platform, dict) else None,
                    base,
                )
            )
        matched = [c for c in choices if c[1] == target]
        if len(matched) > 1:
            raise InputError(f"{where}: duplicate manifests for platform {target!r}")
        if not matched:
            available = sorted({platform or "<none>" for _, platform, _ in choices})
            raise NoPlatformError(
                f"{where}: no manifest for platform {target!r} "
                f"(available: {', '.join(available)})",
                available=available,
            )
        descriptor, _, _ = matched[0]
        if descriptor.path is None:
            raise InputError(f"{where}.manifests: selected descriptor has no path")
        return _relative(descriptor, base)
    manifest = image.get("manifest")
    if isinstance(manifest, dict):
        return _relative(_descriptor(manifest, f"{where}.manifest"), base)
    single = image.get("path")
    if isinstance(single, str):
        return Descriptor(
            digest=image.get("digest") or "",
            path=str((base / single).resolve()),
            media_type=image.get("mediaType"),
            platform=target,
        )
    raise InputError(f"{where}: needs 'manifests', 'manifest' or 'path'")


def _platform_string(platform: dict[str, Any]) -> str | None:
    parts = [str(platform.get(key) or "") for key in _PLATFORM_KEYS]
    parts = [part for part in parts if part]
    return "/".join(parts) if parts else None


def load_deps(path: Path | str) -> DepManifest:
    """Parse the local dependency manifest.

    Shape::

        {
          "schema": "offlinepack/deps@1",
          "artifacts": [
            {"digest": "sha256:...", "path": "artifacts/runtime.rpm", "size": 123}
          ],
          "deps": {
            "sha256:<layer>": [
              {"digest": "sha256:...", "path": "artifacts/ca.pem"},
              {"digest": "sha256:..."}          # resolved through artifacts[]
            ]
          }
        }
    """
    doc = _object(read_json(path, what="dependency manifest"), "dependency manifest")
    schema = doc.get("schema", DEPS_SCHEMA)
    if schema not in (DEPS_SCHEMA, "deps@1"):
        raise InputError(f"dependency manifest: unsupported schema {schema!r}")
    base = Path(path).resolve().parent
    raw_artifacts = doc.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise InputError("dependency manifest: 'artifacts' must be an array")
    artifacts: dict[str, Descriptor] = {}
    for pos, item in enumerate(raw_artifacts):
        descriptor = _relative(
            _descriptor(item, f"dependency manifest: artifacts[{pos}]",
                        require_path=True),
            base,
        )
        if descriptor.digest in artifacts:
            raise InputError(
                f"dependency manifest: duplicate artifact digest {descriptor.digest}"
            )
        artifacts[descriptor.digest] = descriptor
    raw_deps = doc.get("deps", {})
    if not isinstance(raw_deps, dict):
        raise InputError("dependency manifest: 'deps' must be an object")
    deps: dict[str, list[Descriptor]] = {}
    for source, items in raw_deps.items():
        if not isinstance(items, list):
            raise InputError(f"dependency manifest: deps[{source!r}] must be an array")
        resolved: list[Descriptor] = []
        for pos, item in enumerate(items):
            where = f"dependency manifest: deps[{source!r}][{pos}]"
            descriptor = _relative(_descriptor(item, where), base)
            if descriptor.path is None:
                artifact = artifacts.get(descriptor.digest)
                if artifact is None:
                    raise InputError(f"{where}: no 'path' and digest is not in artifacts")
                descriptor = Descriptor(
                    digest=descriptor.digest,
                    path=artifact.path,
                    size=descriptor.size if descriptor.size is not None else artifact.size,
                    media_type=descriptor.media_type or artifact.media_type,
                    platform=descriptor.platform,
                )
            resolved.append(descriptor)
        deps[source] = resolved
    name = doc.get("name")
    return DepManifest(
        artifacts=artifacts, deps=deps, name=name if isinstance(name, str) else ""
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass
class PlanNode:
    digest: str
    kind: str
    label: str
    media_type: str | None
    size: int | None
    platform: str | None
    source_path: str | None
    chain: tuple[str, ...] = ()
    edge: str = ""
    children: list[str] = field(default_factory=list)

    @property
    def has_blob(self) -> bool:
        return self.kind != NODE_ENTRY

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "kind": self.kind,
            "label": self.label,
            "mediaType": self.media_type,
            "size": self.size,
            "platform": self.platform,
            "sourcePath": self.source_path,
            "chain": list(self.chain),
            "edge": self.edge,
            "children": list(self.children),
        }


@dataclass
class Resolution:
    nodes: dict[str, PlanNode]
    issues: list[Issue]
    target: str
    references: dict[str, int] = field(default_factory=dict)

    def ordered(self) -> list[PlanNode]:
        return sorted(self.nodes.values(), key=lambda n: (n.chain, n.digest))

    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_error]

    def blob_nodes(self) -> list[PlanNode]:
        return [node for node in self.nodes.values() if node.has_blob]


@dataclass
class _Pending:
    descriptor: Descriptor
    kind: str
    parent: str | None
    chain: tuple[str, ...]
    label: str


class Resolver:
    def __init__(self, deps: DepManifest, target: str) -> None:
        self.deps = deps
        self.target = target
        self.nodes: dict[str, PlanNode] = {}
        self.issues: list[Issue] = []
        self._queue: deque[_Pending] = deque()
        self._scanned: dict[str, list[_Pending]] = {}
        self._adjacency: dict[str, list[str]] = {}
        # How many descriptor occurrences name each digest.  Two identical
        # descriptors in one manifest count twice: the content is stored once,
        # but the requirement is declared twice.
        self.references: dict[str, int] = {}

    def resolve(self, index: LoadedIndex) -> Resolution:
        entry = index.entry
        if not entry.digest:
            raise InputError("image index: the entry image has no digest")
        # The entry is a *named* reference, not content: key it separately so
        # an entry digest that happens to equal its manifest digest does not
        # short-circuit the walk.
        entry_key = "entry:" + entry.name
        self.nodes[entry_key] = PlanNode(
            digest=entry_key,
            kind=NODE_ENTRY,
            label=entry.name,
            media_type=None,
            size=None,
            platform=self.target,
            source_path=None,
        )
        self._queue.append(
            _Pending(
                descriptor=index.selected,
                kind=NODE_IMAGE,
                parent=entry_key,
                chain=(),
                label=f"{entry.name}@{self.target}",
            )
        )
        while self._queue:
            self._visit(self._queue.popleft())
        self._build_spanning_tree(entry_key)
        return Resolution(
            nodes=self.nodes,
            issues=self.issues,
            target=self.target,
            references=self.references,
        )

    # -- internals -------------------------------------------------------

    def _build_spanning_tree(self, root: str) -> None:
        """Keep one edge per reachable node: its first (shortest) reference.

        Deduplication is only meaningful on a tree.  A digest pulled in twice
        by the same parent, or reached by two different parents, is stored once
        and its extra in-edges are counted; its children are not reported again
        for every path that reaches it.
        """
        for node in self.nodes.values():
            node.children = []
        seen = {root}
        queue: deque[tuple[str, str | None]] = deque([(root, None)])
        while queue:
            current, parent = queue.popleft()
            if parent is not None:
                self.nodes[parent].children.append(current)
            node = self.nodes.get(current)
            if node is None:
                continue
            for child in self._adjacency.get(current, []):
                if child in seen:
                    # A shared digest keeps its first edge; the repeat is
                    # already counted in ``references``.
                    continue
                seen.add(child)
                queue.append((child, current))

    def _visit(self, pending: _Pending) -> None:
        descriptor = pending.descriptor
        digest = descriptor.digest
        chain = pending.chain + (pending.label,)
        if not digest:
            self._issue(IssueKind.MISSING_DESCRIPTOR, chain, pending.parent,
                        descriptor.path, "descriptor has no digest")
            return
        if digest in self.nodes:
            self._link(pending.parent, digest)
            return

        source, descriptor = self._locate_source(descriptor)
        if source is None:
            self._issue(
                IssueKind.MISSING_DESCRIPTOR,
                chain,
                pending.parent,
                descriptor.path,
                "no local path and no registry entry",
                digest=digest,
            )
            return
        if not source.exists():
            self._issue(
                IssueKind.MISSING_FILE,
                chain,
                pending.parent,
                str(source),
                "file does not exist",
                digest=digest,
            )
            return

        kind = pending.kind
        blob_source = source
        manifest: dict[str, Any] | None = None
        if kind == NODE_IMAGE:
            resolved = self._resolve_manifest(source)
            if isinstance(resolved, Issue):
                resolved.path = chain
                resolved.digest = digest
                resolved.from_digest = pending.parent
                self._record(resolved)
                return
            manifest, blob_source = resolved

        node = PlanNode(
            digest=digest,
            kind=kind,
            label=pending.label,
            media_type=descriptor.media_type or _default_media_type(kind),
            size=descriptor.size if descriptor.size is not None else _size(blob_source),
            platform=descriptor.platform or self.target,
            source_path=str(blob_source),
            chain=chain,
            edge=pending.label,
        )
        self.nodes[digest] = node
        self._link(pending.parent, digest)

        for child in self._children(node, digest, chain, manifest):
            self._queue.append(child)

    def _children(
        self,
        node: PlanNode,
        digest: str,
        chain: tuple[str, ...],
        manifest: dict[str, Any] | None,
    ) -> list[_Pending]:
        children: list[_Pending] = []
        if manifest is not None:
            children.extend(self._scan_manifest(digest, manifest, chain))
        for dep in self.deps.deps.get(digest, []):
            children.append(
                _Pending(
                    descriptor=dep,
                    kind=NODE_IMAGE if dep.is_image else NODE_ARTIFACT,
                    parent=digest,
                    chain=chain,
                    label=_label_for(dep),
                )
            )
        for child in children:
            self.references[child.descriptor.digest] = (
                self.references.get(child.descriptor.digest, 0) + 1
            )
        return children

    def _scan_manifest(
        self, digest: str, manifest: dict[str, Any], chain: tuple[str, ...]
    ) -> list[_Pending]:
        cached = self._scanned.get(digest)
        if cached is not None:
            return [
                _Pending(d.descriptor, d.kind, digest, chain, d.label) for d in cached
            ]
        references: list[_Pending] = []
        base = Path(self.nodes[digest].source_path or ".").parent
        config = manifest.get("config")
        if isinstance(config, dict):
            descriptor = _locate(_descriptor(config, "manifest: config"), base,
                                 self.deps.artifacts)
            references.append(
                _Pending(descriptor, NODE_CONFIG, digest, chain, _label_for(descriptor, kind=NODE_CONFIG))
            )
        layers = manifest.get("layers") or []
        if not isinstance(layers, list):
            raise InputError(f"manifest {digest}: 'layers' must be an array")
        for pos, raw in enumerate(layers):
            descriptor = _locate(
                _descriptor(raw, f"manifest {short_label(digest)}: layers[{pos}]"),
                base,
                self.deps.artifacts,
            )
            kind = NODE_IMAGE if descriptor.is_image else NODE_LAYER
            references.append(
                _Pending(descriptor, kind, digest, chain, _label_for(descriptor, pos, kind))
            )
        self._scanned[digest] = references
        return references

    def _locate_source(self, descriptor: Descriptor) -> tuple[Path | None, Descriptor]:
        if descriptor.path:
            return Path(descriptor.path), descriptor
        artifact = self.deps.artifacts.get(descriptor.digest)
        if artifact is not None and artifact.path:
            return Path(artifact.path), artifact
        return None, descriptor

    def _resolve_manifest(self, source: Path) -> tuple[dict[str, Any], Path] | Issue:
        """Return ``(manifest, manifest_blob_path)`` for an image descriptor."""
        try:
            if source.is_dir():
                return _read_oci_layout(source, self.target)
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise _Unreadable("manifest is not a JSON object")
            if "manifests" in data and "layers" not in data:
                # A nested multi-arch index stored as a single JSON file: pick
                # the target platform and read the manifest blob next to it.
                return _read_nested_index(data, source, self.target)
            return data, source
        except _Unreadable as exc:
            return Issue(
                kind=exc.kind,
                path=(),
                detail=str(exc),
                source_path=str(source),
                stage=IssueStage.RESOLVE.value,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return Issue(
                kind=IssueKind.UNREADABLE_MANIFEST,
                path=(),
                detail=f"cannot read manifest: {exc}",
                source_path=str(source),
                stage=IssueStage.RESOLVE.value,
            )

    def _link(self, parent: str | None, child: str) -> None:
        if parent is None:
            return
        children = self._adjacency.setdefault(parent, [])
        if child not in children:
            children.append(child)

    def _record(self, issue: Issue) -> None:
        self.issues.append(issue)

    def _issue(
        self,
        kind: IssueKind,
        chain: tuple[str, ...],
        parent: str | None,
        source: str | None,
        detail: str,
        *,
        digest: str | None = None,
        stage: str = IssueStage.RESOLVE.value,
    ) -> None:
        self._record(
            Issue(
                kind=kind,
                path=chain,
                digest=digest,
                from_digest=parent,
                source_path=source,
                detail=detail,
                stage=stage,
            )
        )


class _Unreadable(Exception):
    def __init__(self, message: str, *, kind: IssueKind = IssueKind.UNREADABLE_MANIFEST):
        super().__init__(message)
        self.kind = kind


def _default_media_type(kind: str) -> str | None:
    if kind in OCI_MANIFEST_MEDIA_TYPES or kind == NODE_IMAGE:
        return "application/vnd.oci.image.manifest.v1+json"
    if kind == NODE_LAYER:
        return DEFAULT_LAYER_MEDIA_TYPE
    if kind in (NODE_CONFIG, NODE_ARTIFACT):
        return DEFAULT_ARTIFACT_MEDIA_TYPE
    return None


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _label_for(
    descriptor: Descriptor, pos: int | None = None, kind: str | None = None
) -> str:
    name = Path(descriptor.path).name if descriptor.path else ""
    prefix = ""
    if kind == NODE_CONFIG:
        prefix = "config "
    elif kind == NODE_IMAGE:
        prefix = "image "
    if name:
        return f"{prefix}{short_label(descriptor.digest)} ({name})"
    if pos is not None:
        return f"{prefix}layer[{pos}] {short_label(descriptor.digest)}"
    return f"{prefix}{short_label(descriptor.digest)}".strip()


def _locate(
    descriptor: Descriptor, base: Path, artifacts: dict[str, Descriptor]
) -> Descriptor:
    if descriptor.path is not None:
        return _relative(descriptor, base)
    hexpart = descriptor.digest.split(":", 1)[-1]
    for candidate in (base / "blobs" / "sha256" / hexpart, base / hexpart):
        if candidate.exists():
            return Descriptor(
                digest=descriptor.digest,
                path=str(candidate.resolve()),
                size=descriptor.size,
                media_type=descriptor.media_type,
                platform=descriptor.platform,
            )
    registered = artifacts.get(descriptor.digest)
    if registered is not None:
        return Descriptor(
            digest=descriptor.digest,
            path=registered.path,
            size=descriptor.size if descriptor.size is not None else registered.size,
            media_type=descriptor.media_type or registered.media_type,
            platform=descriptor.platform,
        )
    return descriptor


def _read_nested_index(
    index: dict[str, Any], source: Path, target: str
) -> tuple[dict[str, Any], Path]:
    """Pick the target manifest from an index stored as a single JSON file."""
    return _select_from_index(index, source.parent, target, label=str(source))


def _read_oci_layout(directory: Path, target: str) -> tuple[dict[str, Any], Path]:
    index_path = directory / "index.json"
    if not index_path.exists():
        raise _Unreadable(f"{directory}: no index.json")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _Unreadable(f"cannot read {index_path}: {exc}") from exc
    if not isinstance(index, dict):
        raise _Unreadable(f"{index_path}: index.json is not an object")
    return _select_from_index(index, directory, target, label=str(index_path))


def _select_from_index(
    index: dict[str, Any], directory: Path, target: str, *, label: str
) -> tuple[dict[str, Any], Path]:
    """Return (manifest, blob path) for ``target`` from an OCI index document."""
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise _Unreadable(f"{label}: no manifests array")
    available = sorted(
        {
            _platform_string(item.get("platform") or {}) or "<none>"
            for item in manifests
            if isinstance(item, dict)
        }
    )
    for item in manifests:
        if not isinstance(item, dict):
            continue
        if _platform_string(item.get("platform") or {}) != target:
            continue
        digest = item.get("digest")
        if not isinstance(digest, str):
            raise _Unreadable(f"{label}: selected manifest has no digest")
        blob = directory / "blobs" / "sha256" / digest.split(":", 1)[-1]
        if not blob.exists():
            raise _Unreadable(
                f"{label} references {digest} but the blob is missing",
                kind=IssueKind.MISSING_BLOB,
            )
        try:
            manifest = json.loads(blob.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise _Unreadable(f"cannot read {blob}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise _Unreadable(f"{blob}: manifest is not an object")
        return manifest, blob
    raise _Unreadable(
        f"{label}: no manifest for {target!r} (available: {', '.join(available)})",
        kind=IssueKind.NO_PLATFORM,
    )
