"""Compare two finished bundles.

Bundles are content addressed, so comparison is set arithmetic on digests:
shared digests are byte-identical content, which is exactly the content a
delivery run can reuse.  The diff therefore reports both *what changed* and
*how many bytes were actually reusable*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import load_bundle_manifest
from .canon import DIFF_SCHEMA, digest_json, human_bytes, short_digest


@dataclass
class BlobDiff:
    digest: str
    status: str  # shared | added | removed
    size: int
    a_size: int | None = None
    b_size: int | None = None
    chain_a: tuple[str, ...] = ()
    chain_b: tuple[str, ...] = ()
    kind: str = ""
    reference_delta: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "status": self.status,
            "size": self.size,
            "aSize": self.a_size,
            "bSize": self.b_size,
            "chainA": list(self.chain_a),
            "chainB": list(self.chain_b),
            "kind": self.kind,
            "referenceDelta": self.reference_delta,
        }


@dataclass
class BundleDiff:
    a: dict[str, Any]
    b: dict[str, Any]
    blobs: list[BlobDiff]
    notes: list[str] = field(default_factory=list)

    @property
    def shared_bytes(self) -> int:
        return sum(blob.size for blob in self.blobs if blob.status == "shared")

    @property
    def added_bytes(self) -> int:
        return sum(blob.size for blob in self.blobs if blob.status == "added")

    @property
    def removed_bytes(self) -> int:
        return sum(blob.size for blob in self.blobs if blob.status == "removed")

    @property
    def union_bytes(self) -> int:
        return self.shared_bytes + self.added_bytes + self.removed_bytes

    @property
    def reuse_ratio(self) -> float:
        if not self.union_bytes:
            return 0.0
        return self.shared_bytes / self.union_bytes

    def to_dict(self) -> dict[str, Any]:
        summary = {
            "sharedBytes": self.shared_bytes,
            "addedBytes": self.added_bytes,
            "removedBytes": self.removed_bytes,
            "unionBytes": self.union_bytes,
            "reuseRatio": round(self.reuse_ratio, 6),
            "sharedBlobs": len([b for b in self.blobs if b.status == "shared"]),
            "addedBlobs": len([b for b in self.blobs if b.status == "added"]),
            "removedBlobs": len([b for b in self.blobs if b.status == "removed"]),
        }
        body = {
            "schema": DIFF_SCHEMA,
            "a": _side(self.a),
            "b": _side(self.b),
            "summary": summary,
            "blobs": [blob.to_dict() for blob in self.blobs],
            "notes": list(self.notes),
        }
        body["digest"] = digest_json(body)
        return body

    def render(self) -> str:
        lines = [
            f"A  {self.a.get('entry')} @ {self.a.get('target')}  "
            f"{short_digest(self.a.get('digest', ''), 16)}",
            f"B  {self.b.get('entry')} @ {self.b.get('target')}  "
            f"{short_digest(self.b.get('digest', ''), 16)}",
            "",
            f"shared   {len([b for b in self.blobs if b.status == 'shared']):>4} blobs  "
            f"{human_bytes(self.shared_bytes):>10}  ({self.reuse_ratio * 100:.1f}% of union)",
            f"added    {len([b for b in self.blobs if b.status == 'added']):>4} blobs  "
            f"{human_bytes(self.added_bytes):>10}",
            f"removed  {len([b for b in self.blobs if b.status == 'removed']):>4} blobs  "
            f"{human_bytes(self.removed_bytes):>10}",
        ]
        if self.notes:
            lines.append("")
            lines.extend("note: " + note for note in self.notes)
        return "\n".join(lines)


def _side(manifest: dict[str, Any]) -> dict[str, Any]:
    summary = manifest.get("summary") or {}
    return {
        "entry": manifest.get("entry"),
        "target": manifest.get("target"),
        "digest": manifest.get("digest"),
        "planDigest": manifest.get("planDigest"),
        "blobs": summary.get("blobs"),
        "bytes": summary.get("bytes"),
        "sources": summary.get("sources") or {},
        "dedupedReferences": summary.get("dedupedReferences"),
        "dedupedBytes": summary.get("dedupedBytes"),
        "createdAt": manifest.get("createdAt"),
    }


def diff_bundles(a_dir: Path | str, b_dir: Path | str) -> BundleDiff:
    a = load_bundle_manifest(a_dir)
    b = load_bundle_manifest(b_dir)
    a_blobs = {blob["digest"]: blob for blob in a.get("blobs") or []}
    b_blobs = {blob["digest"]: blob for blob in b.get("blobs") or []}
    notes: list[str] = []
    if a.get("target") != b.get("target"):
        notes.append(
            f"targets differ: {a.get('target')} vs {b.get('target')}"
        )
    rows: list[BlobDiff] = []
    for digest in sorted(set(a_blobs) | set(b_blobs)):
        in_a = a_blobs.get(digest)
        in_b = b_blobs.get(digest)
        if in_a and in_b:
            status = "shared"
            size = in_b.get("bytes") or 0
            if in_a.get("bytes") != in_b.get("bytes"):
                notes.append(
                    f"{short_digest(digest)}: recorded sizes differ "
                    f"({in_a.get('bytes')} vs {in_b.get('bytes')})"
                )
            ref_a = _references(a, digest)
            ref_b = _references(b, digest)
            rows.append(
                BlobDiff(
                    digest=digest,
                    status=status,
                    size=size,
                    a_size=in_a.get("bytes"),
                    b_size=in_b.get("bytes"),
                    chain_a=tuple(in_a.get("chain") or ()),
                    chain_b=tuple(in_b.get("chain") or ()),
                    kind=in_b.get("kind") or in_a.get("kind") or "",
                    reference_delta=ref_b - ref_a,
                )
            )
        elif in_b:
            rows.append(
                BlobDiff(
                    digest=digest,
                    status="added",
                    size=in_b.get("bytes") or 0,
                    b_size=in_b.get("bytes"),
                    chain_b=tuple(in_b.get("chain") or ()),
                    kind=in_b.get("kind") or "",
                )
            )
        else:
            assert in_a is not None
            rows.append(
                BlobDiff(
                    digest=digest,
                    status="removed",
                    size=in_a.get("bytes") or 0,
                    a_size=in_a.get("bytes"),
                    chain_a=tuple(in_a.get("chain") or ()),
                    kind=in_a.get("kind") or "",
                )
            )
    return BundleDiff(a=a, b=b, blobs=rows, notes=notes)


def _references(manifest: dict[str, Any], digest: str) -> int:
    for blob in manifest.get("blobs") or []:
        if blob.get("digest") == digest:
            return int(blob.get("references") or 0)
    return 0
