"""Regenerate the example tree (small, deterministic files)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layer(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as handle:
        handle.write(buffer.getvalue())
    return out.getvalue()


def write(rel: str, data: bytes) -> dict:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"digest": digest(data), "size": len(data), "path": rel}


def main() -> None:
    cfg = json.dumps({"os": "linux", "rootfs": {"type": "layers"}}, sort_keys=True).encode()
    config = write("layers/config.json", cfg)
    config["mediaType"] = "application/vnd.oci.image.config.v1+json"
    config["path"] = "../layers/config.json"

    a = write("layers/a.tar.gz", layer({"etc/os-release": b"NAME=app\n", "usr/bin/app": b"v1"}))
    a2 = write("layers/a2.tar.gz", layer({"etc/os-release": b"NAME=app\n", "usr/bin/app": b"v2"}))
    b = write("layers/b.tar.gz", layer({"usr/lib/libruntime.so": b"runtime"}))
    base = write("layers/base.tar.gz", layer({"usr/lib/libbase.so": b"base"}))
    ca = write("artifacts/ca.rpm", b"ca-certificates-payload")

    base_cfg = write("nested/base-config.json", b'{"os":"linux","variant":"base"}')
    base_manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": base_cfg["digest"], "size": base_cfg["size"],
                   "mediaType": "application/vnd.oci.image.config.v1+json",
                   "path": "../nested/base-config.json"},
        "layers": [{"digest": base["digest"], "size": base["size"],
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "path": "../layers/base.tar.gz"}],
    }, indent=2, sort_keys=True).encode()
    write("nested/base-amd64.json", base_manifest)

    def manifest(layers: list[dict]) -> bytes:
        return json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config["digest"], "size": config["size"],
                       "mediaType": "application/vnd.oci.image.config.v1+json",
                       "path": "../layers/config.json"},
            "layers": layers,
        }, indent=2, sort_keys=True).encode()

    app_layers = [
        {**a, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "path": "../" + a["path"]},
        {"digest": digest(base_manifest), "size": len(base_manifest),
         "mediaType": "application/vnd.oci.image.manifest.v1+json",
         "path": "../nested/base-amd64.json"},
        {**b, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "path": "../" + b["path"]},
    ]
    app = write("manifests/app-amd64.json", manifest(app_layers))
    app_layers_v2 = [
        {**a2, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "path": "../" + a2["path"]},
        {**b, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "path": "../" + b["path"]},
    ]
    app_v2 = write("manifests/app2-amd64.json", manifest(app_layers_v2))

    def index(path: str, name: str, manifests: list[dict], media: str) -> None:
        (ROOT / path).write_text(json.dumps({
            "schema": "offlinepack/index@1", "name": name,
            "images": [{"name": "payments/app", "manifests": manifests}],
        }, indent=2) + "\n")

    index("index.json", "payments", [
        {"digest": app["digest"], "size": app["size"], "mediaType":
         "application/vnd.oci.image.manifest.v1+json", "path": "manifests/app-amd64.json",
         "platform": {"os": "linux", "architecture": "amd64"}},
    ], "")
    index("index-v2.json", "payments", [
        {"digest": app_v2["digest"], "size": app_v2["size"], "mediaType":
         "application/vnd.oci.image.manifest.v1+json", "path": "manifests/app2-amd64.json",
         "platform": {"os": "linux", "architecture": "amd64"}},
    ], "")

    (ROOT / "deps.json").write_text(json.dumps({
        "schema": "offlinepack/deps@1",
        "name": "payments deps",
        "artifacts": [ca],
        "deps": {b["digest"]: [{"digest": ca["digest"], "path": ca["path"]}]},
    }, indent=2) + "\n")

    # A second entry point shaped as an OCI layout directory.
    import shutil
    layout = ROOT / "layout"
    (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    c = layer({"usr/lib/libextra.so": b"extra"})
    c_desc = {"digest": digest(c), "size": len(c),
              "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
    (layout / "blobs" / "sha256" / digest(c).split(":")[1]).write_bytes(c)
    (layout / "blobs" / "sha256" / digest(cfg).split(":")[1]).write_bytes(cfg)
    layout_manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": digest(cfg), "size": len(cfg),
                   "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [c_desc],
    }, indent=2).encode()
    (layout / "blobs" / "sha256" / digest(layout_manifest).split(":")[1]).write_bytes(layout_manifest)
    (layout / "index.json").write_text(json.dumps({
        "schemaVersion": 2,
        "manifests": [{"digest": digest(layout_manifest), "size": len(layout_manifest),
                       "mediaType": "application/vnd.oci.image.manifest.v1+json",
                       "platform": {"os": "linux", "architecture": "amd64"}}],
    }, indent=2) + "\n")
    (ROOT / "index-layout.json").write_text(json.dumps({
        "schema": "offlinepack/index@1", "name": "payments-layout",
        "images": [{"name": "payments/layout", "manifests": [{
            "digest": digest(layout_manifest), "size": len(layout_manifest),
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "path": "layout", "platform": {"os": "linux", "architecture": "amd64"}}]}],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
