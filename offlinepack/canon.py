"""Canonical serialisation, digests and small shared helpers."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import InputError

TOOL_NAME = "offlinepack"
TOOL_VERSION = "0.1.0"

BUNDLE_SCHEMA = "offlinepack/bundle@1"
INDEX_SCHEMA = "offlinepack/index@1"
DEPS_SCHEMA = "offlinepack/deps@1"
STATE_SCHEMA = "offlinepack/state@1"
PLAN_SCHEMA = "offlinepack/plan@1"
DIFF_SCHEMA = "offlinepack/diff@1"

DEFAULT_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
DEFAULT_ARTIFACT_MEDIA_TYPE = "application/octet-stream"

DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
PLATFORM_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:/v[0-9]+)?\Z")
CHUNK_SIZE = 1024 * 1024


def canonical_json(value: Any) -> bytes:
    """Stable bytes: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path: Path | str) -> tuple[str, int]:
    """Return (sha256 digest, byte count) for a file."""
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return "sha256:" + hasher.hexdigest(), size


def valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(DIGEST_RE.match(value))


def require_digest(value: Any, where: str) -> str:
    if not valid_digest(value):
        raise InputError(f"{where}: expected a sha256:<64 hex> digest, got {value!r}")
    return value


def digest_hex(digest: str) -> str:
    return digest.split(":", 1)[1]


def short_digest(digest: str, length: int = 12) -> str:
    hexpart = digest_hex(digest)
    return "sha256:" + hexpart[:length]


def normalize_platform(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}: platform must be a non-empty string")
    platform = value.strip().lower()
    if "/" not in platform:
        platform = "linux/" + platform
    if not PLATFORM_RE.match(platform):
        raise InputError(f"{where}: unsupported platform {value!r}")
    return platform


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def human_bytes(count: int) -> str:
    """Compact binary units, used in CLI text and the preview page."""
    count = int(count)
    sign = "-" if count < 0 else ""
    value = abs(count)
    if value < 1024:
        return f"{sign}{value} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    scaled = float(value)
    for unit in units:
        scaled /= 1024.0
        if scaled < 1024.0 or unit == units[-1]:
            if scaled >= 100:
                return f"{sign}{scaled:.0f} {unit}"
            if scaled >= 10:
                return f"{sign}{scaled:.1f} {unit}"
            return f"{sign}{scaled:.2f} {unit}"
    return f"{sign}{value} B"  # pragma: no cover - unreachable


def read_json(path: Path | str, *, what: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"{what}: cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InputError(f"{what}: invalid JSON in {path}: {exc}") from exc


def write_json_atomic(path: Path | str, value: Any, *, indent: int = 2) -> None:
    """Write JSON via a sibling temp file + rename, with fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    payload = json.dumps(value, indent=indent, sort_keys=False, ensure_ascii=False) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise InputError(f"cannot write {path}: {exc}") from exc
