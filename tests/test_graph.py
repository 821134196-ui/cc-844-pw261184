"""Graph resolution tests: platform selection, nesting, dedup and missing paths."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import support  # noqa: F401  (sets sys.path)
from support import FixtureCase

from offlinepack.bundle import build_plan
from offlinepack.errors import InputError
from offlinepack.graph import NODE_ARTIFACT, NODE_CONFIG, NODE_IMAGE, NODE_LAYER, NoPlatformError


class PlatformSelectionTests(FixtureCase):
    def test_selects_target_platform_manifest(self) -> None:
        amd64 = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        arm64 = build_plan(self.info["index"], self.info["deps"], "linux/arm64")
        self.assertEqual(amd64.target, "linux/amd64")
        self.assertEqual(arm64.target, "linux/arm64")
        self.assertNotEqual(amd64.digest, arm64.digest)
        arm64_layers = [n for n in arm64.nodes if n.kind == NODE_LAYER]
        self.assertEqual(len(arm64_layers), 1)

    def test_bare_architecture_expands_to_linux(self) -> None:
        plan = build_plan(self.info["index"], self.info["deps"], "amd64")
        self.assertEqual(plan.target, "linux/amd64")
        self.assertGreater(len(plan.blobs), 0)

    def test_unknown_platform_reports_available(self) -> None:
        with self.assertRaises(NoPlatformError) as ctx:
            build_plan(self.info["index"], self.info["deps"], "linux/s390x")
        self.assertIn("linux/s390x", str(ctx.exception))
        self.assertEqual(
            ctx.exception.available, ["linux/amd64", "linux/arm64"]
        )

    def test_unknown_platform_is_an_input_error(self) -> None:
        with self.assertRaises(InputError):
            build_plan(self.info["index"], self.info["deps"], "not a platform!")


class GraphWalkTests(FixtureCase):
    def test_nested_image_is_walked_recursively(self) -> None:
        plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        kinds = [node.kind for node in plan.nodes]
        self.assertIn(NODE_IMAGE, kinds)
        nested = [
            node for node in plan.nodes
            if node.kind == NODE_IMAGE and node.chain and len(node.chain) == 2
        ]
        self.assertEqual(len(nested), 1)
        base_layer = [n for n in plan.nodes if n.digest == self.info["base_layer"]]
        self.assertEqual(len(base_layer), 1)
        self.assertEqual(len(base_layer[0].chain), 3)

    def test_config_layers_and_artifacts_all_present(self) -> None:
        plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        digests = {node.digest for node in plan.blobs}
        self.assertIn(self.info["config"], digests)
        self.assertIn(self.info["layer_a"], digests)
        self.assertIn(self.info["layer_b"], digests)
        self.assertIn(self.info["base_layer"], digests)
        self.assertIn(self.info["artifact"], digests)
        artifact = [n for n in plan.nodes if n.kind == NODE_ARTIFACT][0]
        self.assertEqual(artifact.digest, self.info["artifact"])

    def test_entry_node_is_not_a_blob(self) -> None:
        plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        entry = [node for node in plan.nodes if node.kind == "entry"]
        self.assertEqual(len(entry), 1)
        self.assertFalse(entry[0].has_blob)
        self.assertEqual(entry[0].digest, "entry:payments/app")

    def test_plan_is_deterministic(self) -> None:
        first = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        second = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.document, second.document)

    def test_oci_layout_directory_is_supported(self) -> None:
        plan = build_plan(self.info["index_layout"], self.info["deps"], "linux/amd64")
        digests = {node.digest for node in plan.blobs}
        self.assertIn(self.info["layer_c"], digests)
        self.assertIn(self.info["layer_d"], digests)
        self.assertEqual(len(plan.resolution.errors()), 0)


class MissingContentTests(FixtureCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import fixture_builder

        cls.missing_root = Path(cls._tmp) / "missing"
        cls.missing = fixture_builder.build(cls.missing_root, missing=True)

    def test_missing_layer_reports_full_dependency_path(self) -> None:
        plan = build_plan(
            self.missing["index"], self.missing["deps"], "linux/amd64"
        )
        errors = plan.resolution.errors()
        self.assertEqual(len(errors), 1)
        issue = errors[0]
        self.assertEqual(issue.kind.value, "missing-file")
        self.assertEqual(issue.path[0], "payments/app@linux/amd64")
        self.assertIn("absent.tar.gz", issue.path[-1])
        self.assertTrue(issue.digest.startswith("sha256:"))
        self.assertTrue(issue.source_path.endswith("absent.tar.gz"))
        self.assertEqual(len(issue.path), 2)

    def test_plan_still_lists_the_rest_of_the_graph(self) -> None:
        plan = build_plan(
            self.missing["index"], self.missing["deps"], "linux/amd64"
        )
        self.assertIn(self.info["layer_b"], {n.digest for n in plan.blobs})

    def test_missing_nested_manifest_reports_the_locator(self) -> None:
        root = Path(self._tmp) / "missing-nested"
        import fixture_builder

        info = fixture_builder.build(root)
        manifest = json.loads((root / "manifests" / "app-amd64.json").read_text())
        manifest["layers"][1]["path"] = "../nested/gone.json"
        (root / "manifests" / "app-amd64.json").write_text(json.dumps(manifest))
        plan = build_plan(info["index"], info["deps"], "linux/amd64")
        errors = plan.resolution.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind.value, "missing-file")
        self.assertIn("gone.json", errors[0].path[-1])

    def test_unreadable_manifest_is_reported_not_fatal(self) -> None:
        root = Path(self._tmp) / "unreadable"
        import fixture_builder

        info = fixture_builder.build(root)
        (root / "nested" / "base-amd64.json").write_text("{not json")
        plan = build_plan(info["index"], info["deps"], "linux/amd64")
        kinds = [issue.kind.value for issue in plan.resolution.errors()]
        self.assertIn("unreadable-manifest", kinds)
        # The rest of the graph still resolves.
        self.assertIn(info["layer_b"], {n.digest for n in plan.blobs})

    def test_missing_blob_in_layout_is_reported(self) -> None:
        root = Path(self._tmp) / "layout-missing"
        import fixture_builder

        info = fixture_builder.build(root)
        for blob in (root / "layout" / "blobs" / "sha256").iterdir():
            text = blob.read_text(encoding="utf-8", errors="ignore")
            if "manifest" in text and "layers" in text:
                blob.unlink()
                break
        plan = build_plan(info["index_layout"], info["deps"], "linux/amd64")
        kinds = [issue.kind.value for issue in plan.resolution.errors()]
        self.assertIn("missing-blob", kinds)


class SharedContentTests(FixtureCase):
    def test_shared_digest_resolved_once(self) -> None:
        plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        digests = [node.digest for node in plan.blobs]
        self.assertEqual(len(digests), len(set(digests)))
        plan2 = build_plan(self.info["index2"], self.info["deps"], "linux/amd64")
        shared = {n.digest for n in plan.blobs} & {n.digest for n in plan2.blobs}
        self.assertIn(self.info["config"], shared)
        self.assertIn(self.info["layer_b"], shared)
        self.assertIn(self.info["artifact"], shared)


if __name__ == "__main__":
    unittest.main()
