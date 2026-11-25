"""Command line interface for offlinepack."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .bundle import (
    BuildResult,
    IncompleteBundle,
    Plan,
    build_bundle,
    build_plan,
    describe_plan,
    verify_bundle,
)
from .canon import TOOL_NAME, TOOL_VERSION, human_bytes, short_digest
from .diff import diff_bundles
from .errors import BundleError, OfflinePackError, StateMismatch
from .graph import NoPlatformError
from .issue import format_issues
from .preview import render_preview

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Build verifiable offline delivery bundles from an image "
                    "index and a local dependency manifest.",
    )
    parser.add_argument("--version", action="version",
                        version=f"{TOOL_NAME} {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="resolve the graph and report missing paths")
    _graph_args(plan)
    plan.add_argument("--json", action="store_true", help="emit the plan as JSON")

    build = sub.add_parser("build", help="copy, verify and assemble a bundle")
    _graph_args(build)
    build.add_argument("--out", required=True, type=Path, help="bundle directory")
    build.add_argument("--resume", action="store_true",
                       help="continue an interrupted build")
    build.add_argument("--force", action="store_true",
                       help="discard an existing bundle or checkpoint")
    build.add_argument("--rehash-existing", action="store_true",
                       help="re-hash blobs already in the bundle instead of trusting them")
    build.add_argument("--reuse-from", action="append", default=[],
                       type=Path, metavar="BUNDLE",
                       help="seed matching blobs from an earlier bundle (repeatable)")
    build.add_argument("--quiet", action="store_true", help="suppress progress output")
    build.add_argument("--json", action="store_true", help="emit the result as JSON")

    verify = sub.add_parser("verify", help="re-hash a bundle and check its manifest")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--json", action="store_true")

    diff = sub.add_parser("diff", help="compare two bundles by content digest")
    diff.add_argument("a", type=Path, help="earlier bundle")
    diff.add_argument("b", type=Path, help="later bundle")
    diff.add_argument("--json", action="store_true", help="emit the diff as JSON")
    diff.add_argument("--html", type=Path, metavar="PATH",
                      help="write a self-contained preview page")

    return parser


def _graph_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index", required=True, type=Path, help="image index JSON")
    parser.add_argument("--deps", required=True, type=Path,
                        help="local dependency manifest JSON")
    parser.add_argument("--target", required=True,
                        help="target platform, e.g. linux/amd64")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "diff":
            return _cmd_diff(args)
    except KeyboardInterrupt:
        print(f"{TOOL_NAME}: interrupted; rerun with --resume to continue",
              file=sys.stderr)
        return 130
    except NoPlatformError as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    except StateMismatch as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    except IncompleteBundle as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        for issue in exc.issues:
            print("  " + issue.render(), file=sys.stderr)
            if issue.source_path:
                print(f"    looked for: {issue.source_path}", file=sys.stderr)
        if exc.bundle_dir is not None:
            print(f"  partial bundle kept at {exc.bundle_dir}; "
                  f"rerun with --resume", file=sys.stderr)
        return EXIT_INCOMPLETE
    except (OfflinePackError, BundleError) as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


def _load_plan(args: argparse.Namespace) -> Plan:
    return build_plan(args.index, args.deps, args.target)


def _cmd_plan(args: argparse.Namespace) -> int:
    plan = _load_plan(args)
    errors = plan.resolution.errors()
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return EXIT_INCOMPLETE if errors else EXIT_OK
    print(describe_plan(plan))
    if errors:
        print()
        print("missing dependency paths:")
        for issue in errors:
            print("  " + issue.render())
            if issue.source_path:
                print(f"    looked for: {issue.source_path}")
        return EXIT_INCOMPLETE
    return EXIT_OK


def _progress_printer(quiet: bool):
    if quiet or not sys.stderr.isatty():
        return None
    state = {"last": -1}

    def report(event: dict[str, Any]) -> None:
        if event.get("type") != "blob":
            return
        index = event.get("index", 0)
        total = event.get("total", 0) or 1
        # Throttle to ~40 updates; always print the final blob.
        step = max(1, total // 40)
        if index != total and index - state["last"] < step:
            return
        state["last"] = index
        source = event.get("source", "copy")
        sys.stderr.write(
            f"\r  [{index}/{total}] {source:<10} {short_digest(event['digest'])} "
            f"{human_bytes(event.get('bytes', 0)):<10}"
        )
        if index == total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    return report


def _cmd_build(args: argparse.Namespace) -> int:
    plan = _load_plan(args)
    result: BuildResult = build_bundle(
        plan,
        args.out,
        resume=args.resume,
        force=args.force,
        rehash_existing=args.rehash_existing,
        reuse_from=args.reuse_from,
        progress=_progress_printer(args.quiet),
    )
    payload = {
        "status": result.status,
        "bundle": str(result.bundle_dir),
        "digest": result.bundle_digest,
        "planDigest": plan.digest,
        "resumed": result.resumed,
        "stats": result.stats,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    stats = result.stats
    sources = stats.get("sources", {})
    print(f"bundle      {result.bundle_dir}")
    print(f"entry       {plan.entry} @ {plan.target}")
    print(f"blobs       {stats['blobs']}  ({human_bytes(stats['totalBytes'])})")
    for name in ("copy", "cache", "checkpoint", "existing"):
        entry = sources.get(name) or {"count": 0, "bytes": 0}
        if entry["count"]:
            print(f"  {name:<10} {entry['count']:>4} blobs  {human_bytes(entry['bytes']):>10}")
    if result.already_complete:
        print("already     complete (nothing to do)")
    elif result.resumed:
        print("resumed     yes")
    print(f"digest      {result.bundle_digest}")
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_bundle(args.bundle)
    if args.json:
        print(json.dumps({
            "ok": result.ok,
            "bundle": str(result.bundle_dir),
            "digest": result.manifest_digest,
            "checked": result.checked,
            "bytes": result.bytes_checked,
            "issues": [issue.to_dict() for issue in result.issues],
        }, indent=2))
        return EXIT_OK if result.ok else EXIT_INCOMPLETE
    if result.ok:
        print(f"ok          {result.checked} blobs, {human_bytes(result.bytes_checked)}")
        print(f"digest      {result.manifest_digest}")
        return EXIT_OK
    print("FAILED", file=sys.stderr)
    print(format_issues(result.issues), file=sys.stderr)
    return EXIT_INCOMPLETE


def _cmd_diff(args: argparse.Namespace) -> int:
    diff = diff_bundles(args.a, args.b)
    if args.html is not None:
        render_preview(diff, out=args.html)
    if args.json:
        print(json.dumps(diff.to_dict(), indent=2))
    else:
        print(diff.render())
        if args.html is not None:
            print(f"\npreview written to {args.html}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
