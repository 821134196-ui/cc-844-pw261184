"""Shared test helpers: a temporary fixture tree and CLI invocation."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_builder  # noqa: E402

from offlinepack import cli  # noqa: E402


class FixtureCase(unittest.TestCase):
    """Builds a fresh fixture tree per test class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="offlinepack-test-")
        cls.root = Path(cls._tmp) / "fixture"
        cls.info = fixture_builder.build(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def bundle_dir(self, name: str = "bundle") -> Path:
        path = Path(tempfile.mkdtemp(prefix="offlinepack-bundle-")) / name
        self.addCleanup(shutil.rmtree, path.parent, True)
        return path

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))
