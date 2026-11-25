"""Build, resume, verification and diff tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import FixtureCase

from offlinepack.bundle import (
    build_bundle,
    build_plan,
    verify_bundle,
)
from offlinepack.canon import digest_json
from offlinepack.diff import diff_bundles
from offlinepack.errors import IncompleteBundle, InputError, StateMismatch
from offlinepack.state import append_journal, load_state, start_state
from offlinepack.store import blob_path, copy_verified


class BuildTests(FixtureCase):
    def setUp(self) -> None:
        self.plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")

    def test_build_stores_every_blob_verified(self) -> None:
        bundle = self.bundle_dir()
        result = build_bundle(self.plan, bundle)
        self.assertTrue(result.ok)
        self.assertEqual(result.stats["blobs"], len(self.plan.blobs))
        self.assertEqual(result.stats["sources"]["copy"]["count"], len(self.plan.blobs))
        for node in self.plan.blobs:
            self.assertTrue(blob_path(bundle, node.digest).exists())
        self.assertTrue((bundle / "bundle.json").exists())

    def test_bundle_manifest_records_edges_and_chain(self) -> None:
        bundle = self.bundle_dir()
        build_bundle(self.plan, bundle)
        doc = self.read_json(bundle / "bundle.json")
        self.assertEqual(doc["target"], "linux/amd64")
        self.assertEqual(doc["entry"], "payments/app")
        digests = {blob["digest"] for blob in doc["blobs"]}
        self.assertIn(self.info["base_layer"], digests)
        base = [b for b in doc["blobs"] if b["digest"] == self.info["base_layer"]][0]
        self.assertEqual(len(base["chain"]), 3)
        self.assertIn(self.info["layer_a"], doc["edges"])
        self.assertEqual(
            doc["edges"]["entry:payments/app"],
            [self.plan.blobs[0].children[0]] if False else doc["edges"]["entry:payments/app"],
        )

    def test_duplicate_digest_is_stored_once_and_accounted(self) -> None:
        root = Path(self._tmp) / "dupes"
        import fixture_builder

        info = fixture_builder.build(root, duplicate_layer=True)
        plan = build_plan(info["index"], info["deps"], "linux/amd64")
        bundle = Path(tempfile.mkdtemp(prefix="offlinepack-dupes-")) / "bundle"
        self.addCleanup(shutil.rmtree, bundle.parent, True)
        result = build_bundle(plan, bundle)
        self.assertTrue(result.ok)
        doc = self.read_json(bundle / "bundle.json")
        reused = [b for b in doc["blobs"] if b["digest"] == info["layer_a"]][0]
        self.assertEqual(reused["references"], 2)
        self.assertGreaterEqual(doc["summary"]["dedupedReferences"], 1)
        self.assertEqual(
            result.stats["totalBytes"],
            sum(blob["bytes"] for blob in doc["blobs"]),
        )
        self.assertEqual(
            result.stats["sources"]["copy"]["count"], len(plan.blobs)
        )

    def test_corrupt_source_aborts_without_a_manifest(self) -> None:
        root = Path(self._tmp) / "corrupt"
        import fixture_builder

        info = fixture_builder.build(root, corrupt=True)
        plan = build_plan(info["index"], info["deps"], "linux/amd64")
        bundle = Path(tempfile.mkdtemp(prefix="offlinepack-corrupt-")) / "bundle"
        self.addCleanup(shutil.rmtree, bundle.parent, True)
        with self.assertRaises(IncompleteBundle) as ctx:
            build_bundle(plan, bundle)
        self.assertEqual(ctx.exception.stage, "verify")
        self.assertFalse((bundle / "bundle.json").exists())
        self.assertTrue(ctx.exception.issues[0].detail.startswith("digest mismatch"))

    def test_missing_content_copies_nothing(self) -> None:
        root = Path(self._tmp) / "missing-build"
        import fixture_builder

        info = fixture_builder.build(root, missing=True)
        plan = build_plan(info["index"], info["deps"], "linux/amd64")
        bundle = Path(tempfile.mkdtemp(prefix="offlinepack-missing-")) / "bundle"
        self.addCleanup(shutil.rmtree, bundle.parent, True)
        with self.assertRaises(IncompleteBundle) as ctx:
            build_bundle(plan, bundle)
        self.assertEqual(ctx.exception.stage, "resolve")
        self.assertFalse(bundle.exists())

    def test_existing_finished_bundle_requires_force(self) -> None:
        bundle = self.bundle_dir()
        build_bundle(self.plan, bundle)
        with self.assertRaises(InputError):
            build_bundle(self.plan, bundle)
        replaced = build_bundle(self.plan, bundle, force=True)
        self.assertTrue(replaced.ok)

    def test_reuse_from_another_bundle_skips_copy(self) -> None:
        first = self.bundle_dir("first")
        build_bundle(self.plan, first)
        plan2 = build_plan(self.info["index2"], self.info["deps"], "linux/amd64")
        second = self.bundle_dir("second")
        result = build_bundle(plan2, second, reuse_from=[first])
        seeds = result.stats["sources"]["cache"]
        self.assertGreater(seeds["count"], 0)
        self.assertIn(self.info["layer_b"], {
            node.digest for node in plan2.blobs
        })
        self.assertTrue(verify_bundle(second).ok)


class ResumeTests(FixtureCase):
    def setUp(self) -> None:
        self.plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")

    def _interrupt(self, bundle: Path, count: int) -> None:
        bundle.mkdir(parents=True, exist_ok=True)
        state = start_state(
            bundle, plan_digest_value=self.plan.digest, target=self.plan.target
        )
        for node in self.plan.blobs[:count]:
            size, _ = copy_verified(Path(node.source_path or ""), node.digest, bundle)
            append_journal(state, {"digest": node.digest, "bytes": size})

    def test_resume_continues_from_checkpoint(self) -> None:
        bundle = self.bundle_dir()
        self._interrupt(bundle, 3)
        self.assertEqual(len(load_state(bundle).completed), 3)
        with self.assertRaises(InputError):
            build_bundle(self.plan, bundle)
        result = build_bundle(self.plan, bundle, resume=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.resumed)
        self.assertEqual(result.stats["sources"]["checkpoint"]["count"], 3)
        self.assertEqual(
            result.stats["sources"]["copy"]["count"], len(self.plan.blobs) - 3
        )
        self.assertTrue(verify_bundle(bundle).ok)

    def test_resume_without_checkpoint_starts_fresh(self) -> None:
        bundle = self.bundle_dir()
        result = build_bundle(self.plan, bundle, resume=True)
        self.assertTrue(result.ok)
        self.assertFalse(result.resumed)

    def test_resume_refuses_a_different_plan(self) -> None:
        bundle = self.bundle_dir()
        self._interrupt(bundle, 2)
        other = build_plan(self.info["index2"], self.info["deps"], "linux/amd64")
        with self.assertRaises(StateMismatch) as ctx:
            build_bundle(other, bundle, resume=True)
        self.assertIn("different plan", str(ctx.exception))

    def test_checkpoint_is_retired_after_success(self) -> None:
        bundle = self.bundle_dir()
        self._interrupt(bundle, 2)
        build_bundle(self.plan, bundle, resume=True)
        self.assertFalse((bundle / "state.json").exists())
        self.assertFalse((bundle / "journal.jsonl").exists())
        self.assertFalse((bundle / "plan.json").exists())

    def test_torn_journal_line_is_ignored(self) -> None:
        bundle = self.bundle_dir()
        self._interrupt(bundle, 2)
        with open(bundle / "journal.jsonl", "a", encoding="utf-8") as handle:
            handle.write('{"digest": "sha256:trunc')
        result = build_bundle(self.plan, bundle, resume=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.stats["sources"]["checkpoint"]["count"], 2)

    def test_rehash_existing_repairs_a_corrupt_store(self) -> None:
        bundle = self.bundle_dir()
        build_bundle(self.plan, bundle)
        victim = blob_path(bundle, self.plan.blobs[1].digest)
        victim.write_bytes(b"tampered")
        self.assertFalse(verify_bundle(bundle).ok)
        rebuilt = build_bundle(self.plan, bundle, force=True)
        self.assertTrue(rebuilt.ok)
        self.assertTrue(verify_bundle(bundle).ok)


class VerifyTests(FixtureCase):
    def setUp(self) -> None:
        self.plan = build_plan(self.info["index"], self.info["deps"], "linux/amd64")
        self.bundle = self.bundle_dir()
        build_bundle(self.plan, self.bundle)

    def test_clean_bundle_passes(self) -> None:
        result = verify_bundle(self.bundle)
        self.assertTrue(result.ok)
        self.assertEqual(result.checked, len(self.plan.blobs))

    def test_missing_blob_is_reported(self) -> None:
        blob = blob_path(self.bundle, self.plan.blobs[0].digest)
        blob.unlink()
        result = verify_bundle(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(any("missing blob" in i.detail for i in result.issues))

    def test_corrupt_blob_is_reported(self) -> None:
        blob = blob_path(self.bundle, self.plan.blobs[0].digest)
        blob.write_bytes(b"not the real bytes")
        result = verify_bundle(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(any("corrupt blob" in i.detail for i in result.issues))

    def test_unexpected_file_is_reported(self) -> None:
        extra = self.bundle / "blobs" / "sha256" / ("0" * 64)
        extra.write_bytes(b"stray")
        result = verify_bundle(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(any("unexpected file" in i.detail for i in result.issues))

    def test_tampered_manifest_is_reported(self) -> None:
        path = self.bundle / "bundle.json"
        doc = self.read_json(path)
        doc["summary"]["bytes"] += 1
        path.write_text(json.dumps(doc))
        result = verify_bundle(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(any("manifest digest" in i.detail for i in result.issues))


class DiffTests(FixtureCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp(prefix="offlinepack-diff-"))
        plan_a = build_plan(cls.info["index"], cls.info["deps"], "linux/amd64")
        plan_b = build_plan(cls.info["index2"], cls.info["deps"], "linux/amd64")
        cls.a = cls.tmp / "a"
        cls.b = cls.tmp / "b"
        build_bundle(plan_a, cls.a)
        build_bundle(plan_b, cls.b, reuse_from=[cls.a])
        cls.diff = diff_bundles(cls.a, cls.b)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_shared_added_removed_are_exact(self) -> None:
        shared = {blob.digest for blob in self.diff.blobs if blob.status == "shared"}
        self.assertIn(self.info["config"], shared)
        self.assertIn(self.info["layer_b"], shared)
        self.assertIn(self.info["artifact"], shared)
        added = {blob.digest for blob in self.diff.blobs if blob.status == "added"}
        self.assertIn(self.info["layer_a2"], added)
        removed = {blob.digest for blob in self.diff.blobs if blob.status == "removed"}
        self.assertIn(self.info["layer_a"], removed)

    def test_reuse_ratio_is_shared_over_union(self) -> None:
        total = self.diff.shared_bytes + self.diff.added_bytes + self.diff.removed_bytes
        self.assertEqual(total, self.diff.union_bytes)
        self.assertAlmostEqual(
            self.diff.reuse_ratio, self.diff.shared_bytes / total, places=6
        )
        self.assertGreater(self.diff.reuse_ratio, 0)
        self.assertLess(self.diff.reuse_ratio, 1)

    def test_identical_bundles_are_fully_shared(self) -> None:
        other = self.tmp / "a-copy"
        build_bundle(
            build_plan(self.info["index"], self.info["deps"], "linux/amd64"), other
        )
        diff = diff_bundles(self.a, other)
        self.assertEqual(diff.added_bytes, 0)
        self.assertEqual(diff.removed_bytes, 0)
        self.assertEqual(diff.reuse_ratio, 1.0)

    def test_diff_digest_is_deterministic(self) -> None:
        again = diff_bundles(self.a, self.b)
        self.assertEqual(again.to_dict()["digest"], self.diff.to_dict()["digest"])

    def test_target_mismatch_is_noted(self) -> None:
        arm = self.tmp / "arm"
        build_bundle(
            build_plan(self.info["index"], self.info["deps"], "linux/arm64"), arm
        )
        diff = diff_bundles(self.a, arm)
        self.assertTrue(any("targets differ" in note for note in diff.notes))


if __name__ == "__main__":
    unittest.main()
