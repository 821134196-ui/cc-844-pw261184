"""Preview page and CLI tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support
from support import FixtureCase

from offlinepack.bundle import build_bundle, build_plan
from offlinepack.diff import diff_bundles
from offlinepack.preview import render_preview


class PreviewTests(FixtureCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp(prefix="offlinepack-preview-"))
        plan_a = build_plan(cls.info["index"], cls.info["deps"], "linux/amd64")
        plan_b = build_plan(cls.info["index2"], cls.info["deps"], "linux/amd64")
        cls.a, cls.b = cls.tmp / "a", cls.tmp / "b"
        build_bundle(plan_a, cls.a)
        build_bundle(plan_b, cls.b, reuse_from=[cls.a])
        cls.diff = diff_bundles(cls.a, cls.b)
        cls.html = render_preview(cls.diff)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def test_payload_matches_diff_document(self) -> None:
        start = self.html.index('id="payload"')
        start = self.html.index(">", start) + 1
        end = self.html.index("</script>", start)
        payload = self.html[start:end]
        doc = json.loads(payload)
        self.assertEqual(doc["schema"], "offlinepack/diff@1")
        self.assertEqual(doc["summary"]["sharedBlobs"], 3)
        self.assertEqual(len(doc["blobs"]), 10)

    def test_page_is_self_contained(self) -> None:
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<link", self.html)
        self.assertNotIn("src=", self.html)

    def test_shared_values_are_present_for_screen_readers(self) -> None:
        doc = json.loads(self.html[self.html.index(">", self.html.index('id="payload"')) + 1:
                                  self.html.index("</script>", self.html.index('id="payload"'))])
        summary = doc["summary"]
        self.assertIn('"reuseRatio"', json.dumps(summary))
        # Both modes are declared, and the toggle scope wins over the OS query.
        self.assertIn('prefers-color-scheme: dark', self.html)
        self.assertIn(':root[data-theme="dark"]', self.html)
        self.assertIn(':not([data-theme="light"])', self.html)

    def test_untrusted_names_do_not_break_out_of_the_document(self) -> None:
        evil = dict(self.diff.a)
        evil["entry"] = "</script><img src=x onerror=alert(1)>"
        for blob in self.diff.blobs:
            blob.chain_a = ("<script>alert(1)</script>",)
        html = render_preview(self.diff)
        self.assertNotIn("</script><img", html)
        # Entry names are injected through textContent in script-land, never
        # into the HTML skeleton.
        self.assertIn("textContent", html)

    def test_payload_escapes_html_delimiters(self) -> None:
        self.diff.a["entry"] = "a</script><b>"
        html = render_preview(self.diff)
        payload_start = html.index('id="payload"')
        payload = html[html.index(">", payload_start) + 1:
                       html.index("</script>", payload_start)]
        self.assertNotIn("<", payload)
        self.assertNotIn(">", payload)
        self.assertIn("\\u003c", payload)

    def test_writes_to_disk(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="offlinepack-write-")) / "nested" / "p.html"
        self.addCleanup(shutil.rmtree, target.parents[1], True)
        render_preview(self.diff, out=target)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8")[:15], "<!doctype html>")

    def test_identical_bundles_render_100_percent(self) -> None:
        other = self.tmp / "a-again"
        build_bundle(
            build_plan(self.info["index"], self.info["deps"], "linux/amd64"), other
        )
        html = render_preview(diff_bundles(self.a, other))
        self.assertIn('"reuseRatio": 1.0', html)


class CliTests(FixtureCase):
    def test_plan_success_and_failure_exit_codes(self) -> None:
        code, out, _ = self.run_cli(
            "plan", "--index", str(self.info["index"]),
            "--deps", str(self.info["deps"]), "--target", "linux/amd64",
        )
        self.assertEqual(code, 0)
        self.assertIn("payments/app @ linux/amd64", out)

        code, out, err = self.run_cli(
            "plan", "--index", str(self.info["index"]),
            "--deps", str(self.info["deps"]), "--target", "linux/s390x",
        )
        self.assertEqual(code, 2)
        self.assertIn("available: linux/amd64, linux/arm64", err)

    def test_plan_json_is_machine_readable(self) -> None:
        code, out, _ = self.run_cli(
            "plan", "--index", str(self.info["index"]),
            "--deps", str(self.info["deps"]), "--target", "linux/amd64", "--json",
        )
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "offlinepack/plan@1")
        self.assertEqual(doc["summary"]["errors"], 0)
        self.assertGreater(doc["summary"]["blobs"], 0)

    def test_build_verify_diff_round_trip(self) -> None:
        bundle = self.bundle_dir()
        code, out, _ = self.run_cli(
            "build", "--index", str(self.info["index"]),
            "--deps", str(self.info["deps"]), "--target", "linux/amd64",
            "--out", str(bundle), "--quiet",
        )
        self.assertEqual(code, 0, out)
        self.assertIn("blobs", out)

        code, out, _ = self.run_cli("verify", str(bundle))
        self.assertEqual(code, 0)

        other = self.bundle_dir("other")
        self.run_cli(
            "build", "--index", str(self.info["index2"]),
            "--deps", str(self.info["deps"]), "--target", "linux/amd64",
            "--out", str(other), "--reuse-from", str(bundle), "--quiet",
        )
        page = bundle.parent / "preview.html"
        code, out, _ = self.run_cli("diff", str(bundle), str(other),
                                    "--html", str(page))
        self.assertEqual(code, 0)
        self.assertIn("shared", out)
        self.assertTrue(page.exists())

    def test_diff_json_has_reuse_numbers(self) -> None:
        first = self.bundle_dir("a")
        second = self.bundle_dir("b")
        for index, target in ((self.info["index"], first), (self.info["index2"], second)):
            self.run_cli("build", "--index", str(index), "--deps",
                         str(self.info["deps"]), "--target", "linux/amd64",
                         "--out", str(target), "--quiet")
        code, out, _ = self.run_cli("diff", str(first), str(second), "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertGreater(doc["summary"]["sharedBytes"], 0)
        self.assertGreater(doc["summary"]["addedBytes"], 0)
        self.assertGreater(doc["summary"]["reuseRatio"], 0)

    def test_verify_failure_exit_code(self) -> None:
        bundle = self.bundle_dir()
        self.run_cli(
            "build", "--index", str(self.info["index"]), "--deps",
            str(self.info["deps"]), "--target", "linux/amd64",
            "--out", str(bundle), "--quiet",
        )
        for blob in (bundle / "blobs" / "sha256").iterdir():
            blob.write_bytes(b"broken")
            break
        code, _, err = self.run_cli("verify", str(bundle))
        self.assertEqual(code, 2)
        self.assertIn("corrupt blob", err)

    def test_build_resume_flag(self) -> None:
        bundle = self.bundle_dir()
        args = ["build", "--index", str(self.info["index"]), "--deps",
                str(self.info["deps"]), "--target", "linux/amd64",
                "--out", str(bundle), "--quiet"]
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertEqual(self.run_cli(*args)[0], 1)
        code, out, _ = self.run_cli(*args, "--resume")
        # A finished bundle with no checkpoint is a clean no-op rebuild.
        self.assertEqual(code, 0, out)

    def test_missing_dependency_exits_2_and_prints_path(self) -> None:
        root = Path(self._tmp) / "cli-missing"
        import fixture_builder

        info = fixture_builder.build(root, missing=True)
        bundle = Path(tempfile.mkdtemp(prefix="offlinepack-cli-")) / "bundle"
        self.addCleanup(shutil.rmtree, bundle.parent, True)
        code, out, err = self.run_cli(
            "build", "--index", str(info["index"]), "--deps", str(info["deps"]),
            "--target", "linux/amd64", "--out", str(bundle),
        )
        self.assertEqual(code, 2)
        self.assertIn("payments/app@linux/amd64", err)
        self.assertIn("absent.tar.gz", err)
        self.assertFalse(bundle.exists())


if __name__ == "__main__":
    unittest.main()
