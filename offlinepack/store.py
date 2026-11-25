"""Content-addressed blob store with resumable fetch.

Blobs live at ``<root>/blobs/sha256/<hex>`` exactly as in an OCI layout, so the
delivered bundle can be served by any OCI registry tool.  A copy is staged to
``<name>.part`` in the destination directory and renamed into place only after
the digest and size check out, which makes a half-copied blob invisible to a
resumed run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .canon import CHUNK_SIZE, digest_hex, file_digest

ProgressCallback = Callable[[dict], None]


class CopyMismatch(Exception):
    def __init__(self, expected: str, actual: str, size: int, actual_size: int) -> None:
        self.expected = expected
        self.actual = actual
        self.size = size
        self.actual_size = actual_size
        super().__init__(
            f"copied content has digest {actual} ({actual_size} bytes), "
            f"expected {expected} ({size} bytes)"
        )


def blob_path(root: Path, digest: str) -> Path:
    return root / "blobs" / "sha256" / digest_hex(digest)


def copy_verified(
    source: Path,
    digest: str,
    root: Path,
    *,
    progress: ProgressCallback | None = None,
) -> tuple[int, bool]:
    """Copy ``source`` into the store as ``digest``.

    Returns ``(size, reused)``.  Raises :class:`CopyMismatch` when the bytes do
    not match the advertised digest.
    """
    destination = blob_path(root, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    hexpart = digest_hex(digest)
    if destination.exists():
        existing_digest, existing_size = file_digest(destination)
        if existing_digest == digest:
            return existing_size, True
        destination.unlink()
    partial = destination.with_name(hexpart + ".part")
    if partial.exists():
        partial.unlink()
    import hashlib

    hasher = hashlib.sha256()
    size = 0
    try:
        with open(source, "rb") as src, open(partial, "wb") as dst:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                dst.write(chunk)
                size += len(chunk)
                if progress is not None:
                    progress({"type": "copy", "digest": digest, "bytes": len(chunk)})
            dst.flush()
            os.fsync(dst.fileno())
        actual = "sha256:" + hasher.hexdigest()
        if actual != digest:
            raise CopyMismatch(digest, actual, size, size)
        os.replace(partial, destination)
    except BaseException:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return size, False
