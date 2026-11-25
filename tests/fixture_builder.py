"""Build a small but realistic multi-arch image stack on disk.

Layout produced under ``root``::

    layers/<name>.tar              gzipped layer payloads
    manifests/amd64.json           OCI image manifests
    manifests/arm64.json
    nested/base-amd64.json ...     nested image manifests (layer = index)
    index.json                     the image index document
    deps.json                      local dependency manifest
    missing-index.json             variant whose nested layer is absent
    bad-digest-index.json          variant whose layer bytes are wrong

The stack is::

    app
     |- config   (shared with the other bundle variant)
     |- layer A          rootfs
     |- layer B          runtime, references base image
     |    `- base image
     |         |- base layer C
     |         `- base config D
     `- artifact E       local dependency of layer B (shared)
    app2 (second bundle)
     |- config (same digest as app's config)
     |- layer A'  (A + one changed byte)
     |- layer B
     `- artifact E
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _layer_tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    raw = buffer.getvalue()
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as handle:
        handle.write(raw)
    return compressed.getvalue()


def _manifest_json(config: dict, layers: list[dict], media_type: str) -> bytes:
    doc = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": config,
        "layers": layers,
    }
    return json.dumps(doc, indent=2, sort_keys=True).encode()


def build(
    root: Path,
    *,
    missing: bool = False,
    corrupt: bool = False,
    duplicate_layer: bool = False,
) -> dict:
    root = Path(root)
    layers = root / "layers"
    manifests = root / "manifests"
    nested = root / "nested"
    artifacts = root / "artifacts"
    for directory in (layers, manifests, nested, artifacts):
        directory.mkdir(parents=True, exist_ok=True)

    config_bytes = json.dumps({"os": "linux", "rootfs": {"type": "layers"}},
                              sort_keys=True).encode()
    config_path = layers / "config.json"
    config_path.write_bytes(config_bytes)
    config = {
        "digest": _digest(config_bytes),
        "size": len(config_bytes),
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "path": "../layers/config.json",
    }

    layer_a = _layer_tar({
        "etc/os-release": b"NAME=offlinepack\n",
        "usr/bin/app": b"#!/bin/sh\necho app\n",
    })
    layer_b = _layer_tar({"usr/lib/libruntime.so": b"runtime-v1"})
    base_layer = _layer_tar({"usr/lib/libbase.so": b"base-v1"})
    # Layer B's manifest references the base image as a nested layer.
    base_manifest = _manifest_json(
        {
            "digest": _digest(b"base-config"),
            "size": len(b"base-config"),
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "path": "../nested/base-config.json",
        },
        [
            {
                "digest": _digest(base_layer),
                "size": len(base_layer),
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "path": "../layers/base.tar.gz",
            }
        ],
        "application/vnd.oci.image.manifest.v1+json",
    )
    (nested / "base-amd64.json").write_bytes(base_manifest)
    base_config_path = nested / "base-config.json"
    base_config_path.write_bytes(b"base-config")

    paths = {}
    for name, data in (("a.tar.gz", layer_a), ("b.tar.gz", layer_b),
                       ("base.tar.gz", base_layer)):
        path = layers / name
        path.write_bytes(data)
        paths[name] = path

    app_arm64 = _manifest_json(config, [
        {"digest": _digest(layer_a), "size": len(layer_a),
         "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
         "path": "../layers/a.tar.gz"},
    ], "application/vnd.oci.image.manifest.v1+json")
    (manifests / "app-arm64.json").write_bytes(app_arm64)

    app_layers = [
        {"digest": _digest(layer_a), "size": len(layer_a),
         "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
         "path": "../layers/a.tar.gz"},
        {"digest": _digest(base_manifest), "size": len(base_manifest),
         "mediaType": "application/vnd.oci.image.manifest.v1+json",
         "path": "../nested/base-amd64.json"},
        {"digest": _digest(layer_b), "size": len(layer_b),
         "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
         "path": "../layers/b.tar.gz"},
    ]
    if missing:
        app_layers.insert(1, {
            "digest": "sha256:" + "0" * 63 + "1",
            "size": 4096,
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "path": "../layers/absent.tar.gz",
        })
    if duplicate_layer:
        # The same layer required twice: stored once, referenced twice.
        app_layers.insert(2, dict(app_layers[0]))
    app_manifest = _manifest_json(config, app_layers,
                                  "application/vnd.oci.image.manifest.v1+json")
    (manifests / "app-amd64.json").write_bytes(app_manifest)

    # The second bundle variant: same config and layer B, a different layer A.
    layer_a2 = _layer_tar({"etc/os-release": b"NAME=offlinepack\n",
                           "usr/bin/app": b"#!/bin/sh\necho app-v2\n"})
    (layers / "a2.tar.gz").write_bytes(layer_a2)
    app2_layers = [
        {"digest": _digest(layer_a2), "size": len(layer_a2),
         "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
         "path": "../layers/a2.tar.gz"},
        {"digest": _digest(layer_b), "size": len(layer_b),
         "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
         "path": "../layers/b.tar.gz"},
    ]
    app2 = _manifest_json(config, app2_layers,
                          "application/vnd.oci.image.manifest.v1+json")
    (manifests / "app2-amd64.json").write_bytes(app2)

    artifact_bytes = b"rpm-payload-ca-certificates"
    artifact_path = artifacts / "ca.rpm"
    artifact_path.write_bytes(artifact_bytes)

    index_doc = {
        "schema": "offlinepack/index@1",
        "name": "payments",
        "images": [
            {
                "name": "payments/app",
                "manifests": [
                    {
                        "digest": _digest(app_manifest),
                        "size": len(app_manifest),
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "path": "manifests/app-amd64.json",
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "digest": _digest(app_arm64),
                        "size": len(app_arm64),
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "path": "manifests/app-arm64.json",
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                ],
            }
        ],
    }
    index_path = root / "index.json"
    index_path.write_text(json.dumps(index_doc, indent=2) + "\n")

    # A second entry shape: a top-level OCI layout directory where blobs are
    # named by digest and layers carry no explicit path.
    layer_c = _layer_tar({"usr/lib/libextra.so": b"extra-v1"})
    layer_d = _layer_tar({"usr/share/fonts/x.ttf": b"font-bytes"})
    layout_root = root / "layout"
    (layout_root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    blobs = {}
    for name, payload in (("layer_c", layer_c), ("layer_d", layer_d)):
        blobs[name] = {"digest": _digest(payload), "size": len(payload),
                       "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
        (layout_root / "blobs" / "sha256" / _digest(payload).split(":")[1]).write_bytes(payload)
    cfg = json.dumps({"os": "linux", "architecture": "amd64"}, sort_keys=True).encode()
    cfg_desc = {"digest": _digest(cfg), "size": len(cfg),
                "mediaType": "application/vnd.oci.image.config.v1+json"}
    (layout_root / "blobs" / "sha256" / _digest(cfg).split(":")[1]).write_bytes(cfg)
    layout_manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": cfg_desc,
        "layers": [blobs["layer_c"], blobs["layer_d"]],
    }, indent=2).encode()
    manifest_digest = _digest(layout_manifest)
    (layout_root / "blobs" / "sha256" / manifest_digest.split(":")[1]).write_bytes(layout_manifest)
    (layout_root / "index.json").write_text(json.dumps({
        "schemaVersion": 2,
        "manifests": [{
            "digest": manifest_digest,
            "size": len(layout_manifest),
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "platform": {"os": "linux", "architecture": "amd64"},
        }],
    }, indent=2) + "\n")
    layout_index_doc = {
        "schema": "offlinepack/index@1",
        "name": "payments-layout",
        "images": [{
            "name": "payments/layout",
            "manifests": [{
                "digest": manifest_digest,
                "size": len(layout_manifest),
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "path": "layout",
                "platform": {"os": "linux", "architecture": "amd64"},
            }],
        }],
    }
    (root / "index-layout.json").write_text(json.dumps(layout_index_doc, indent=2) + "\n")

    index2_doc = {
        "schema": "offlinepack/index@1",
        "name": "payments",
        "images": [
            {
                "name": "payments/app",
                "manifests": [
                    {
                        "digest": _digest(app2),
                        "size": len(app2),
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "path": "manifests/app2-amd64.json",
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ],
            }
        ],
    }
    (root / "index-v2.json").write_text(json.dumps(index2_doc, indent=2) + "\n")

    deps_doc = {
        "schema": "offlinepack/deps@1",
        "name": "payments deps",
        "artifacts": [
            {
                "digest": _digest(artifact_bytes),
                "size": len(artifact_bytes),
                "path": "artifacts/ca.rpm",
            }
        ],
        "deps": {
            _digest(layer_b): [{"digest": _digest(artifact_bytes),
                                "path": "artifacts/ca.rpm"}],
        },
    }
    (root / "deps.json").write_text(json.dumps(deps_doc, indent=2) + "\n")

    if corrupt:
        payload = bytearray(paths["b.tar.gz"].read_bytes())
        payload[-1] ^= 0xFF
        paths["b.tar.gz"].write_bytes(bytes(payload))

    return {
        "root": root,
        "index": index_path,
        "index2": root / "index-v2.json",
        "index_layout": root / "index-layout.json",
        "deps": root / "deps.json",
        "layer_a": _digest(layer_a),
        "layer_a2": _digest(layer_a2),
        "layer_b": _digest(layer_b),
        "artifact": _digest(artifact_bytes),
        "base_layer": _digest(base_layer),
        "config": config["digest"],
        "layer_c": _digest(layer_c),
        "layer_d": _digest(layer_d),
    }


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture")
    info = build(target, missing="--missing" in sys.argv, corrupt="--corrupt" in sys.argv)
    print(json.dumps({k: str(v) for k, v in info.items()}, indent=2))
