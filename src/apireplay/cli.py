"""Command-line interface for APIReplay.

Subcommands: record, sanitize, replay, diff, report.
Run `apireplay <subcommand> --help` for per-command options.
"""

import argparse
import json
import sys

from . import __version__
from .diff import diff_pack_vs_results, load_diff, save_diff
from .har import load_har_file
from .pack import PackError, load_pack, save_pack
from .proxy import run_proxy
from .replay import count_errors, load_results, replay_pack, save_results
from .report import write_report
from .sanitize import Rules, sanitize_pack


def _positive_int(value):
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return ivalue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apireplay",
        description=(
            "Reproduce backend bugs without production access: capture HTTP "
            "traffic into sanitized replay packs, replay them against a local "
            "service, and diff outputs, latency, and errors."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # record -------------------------------------------------------------
    p = sub.add_parser("record", help="Capture traffic into a replay pack")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--from-har", metavar="FILE",
                      help="Import request/response pairs from a HAR 1.2 file")
    mode.add_argument("--proxy", action="store_true",
                      help="Run a capture proxy that forwards to --target")
    p.add_argument("--port", type=_positive_int, default=8888,
                   help="Proxy listen port (default: 8888)")
    p.add_argument("--target", metavar="URL",
                   help="Origin server base URL for --proxy (e.g. http://localhost:8080)")
    p.add_argument("--out", required=True, metavar="FILE",
                   help="Where to write the pack JSON")
    p.set_defaults(func=_cmd_record)

    # sanitize -----------------------------------------------------------
    p = sub.add_parser("sanitize", help="Redact secrets/PII from a pack")
    p.add_argument("pack", help="Input pack JSON")
    p.add_argument("--out", required=True, metavar="FILE", help="Sanitized pack output")
    p.add_argument("--rules", metavar="FILE",
                   help="JSON file with extra redaction rules (extends defaults)")
    p.set_defaults(func=_cmd_sanitize)

    # replay -------------------------------------------------------------
    p = sub.add_parser("replay", help="Replay a pack against a target service")
    p.add_argument("pack", help="Pack JSON to replay")
    p.add_argument("--target", required=True, metavar="URL",
                   help="Base URL of the service under test (e.g. http://localhost:8080)")
    p.add_argument("--workers", type=_positive_int, default=8,
                   help="Concurrent workers (default: 8)")
    p.add_argument("--timeout", type=float, default=10.0, metavar="SEC",
                   help="Per-request timeout in seconds (default: 10)")
    p.add_argument("--out", required=True, metavar="FILE", help="Results JSON output")
    p.set_defaults(func=_cmd_replay)

    # diff ---------------------------------------------------------------
    p = sub.add_parser("diff", help="Compare recorded vs replayed responses")
    p.add_argument("pack", help="Original pack JSON (recorded expectations)")
    p.add_argument("results", help="Replay results JSON (actuals)")
    p.add_argument("--out", required=True, metavar="FILE", help="Diff JSON output")
    p.add_argument("--ignore", action="append", default=[], metavar="DOT.PATH",
                   help="Skip a JSON dot-path in body comparison; '*' wildcards "
                        "one segment (e.g. items[*].updated_at). Repeatable.")
    p.add_argument("--unordered-lists", action="store_true",
                   help="Compare JSON arrays order-insensitively")
    p.add_argument("--latency-threshold-ms", type=float, default=200.0, metavar="MS",
                   help="One half of the latency gate: flag only when replayed exceeds "
                        "recorded + this AND recorded x --latency-ratio (default: 200)")
    p.add_argument("--latency-ratio", type=float, default=2.0, metavar="RATIO",
                   help="Other half of the latency gate: flag only when replayed exceeds "
                        "recorded x this AND recorded + --latency-threshold-ms (default: 2.0)")
    p.set_defaults(func=_cmd_diff)

    # report -------------------------------------------------------------
    p = sub.add_parser("report", help="Render a human-readable report from a diff")
    p.add_argument("diff", help="Diff JSON from `apireplay diff`")
    p.add_argument("--format", choices=("md", "html"), default="md",
                   help="Report format (default: md)")
    p.add_argument("--out", required=True, metavar="FILE", help="Report output path")
    p.set_defaults(func=_cmd_report)

    return parser


def _cmd_record(args) -> int:
    if args.from_har:
        try:
            pack = load_har_file(args.from_har)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: cannot import HAR: {exc}", file=sys.stderr)
            return 1
        save_pack(pack, args.out)
        print(f"Imported {len(pack['requests'])} request(s) from {args.from_har} -> {args.out}")
        return 0
    if not args.target:
        print("error: --proxy requires --target", file=sys.stderr)
        return 2
    try:
        run_proxy(args.port, args.target, args.out)
    except OSError as exc:
        print(f"error: cannot start proxy: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_sanitize(args) -> int:
    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        rules = Rules.from_file(args.rules) if args.rules else Rules()
    except (OSError, ValueError) as exc:
        print(f"error: cannot load rules: {exc}", file=sys.stderr)
        return 1
    clean, summary = sanitize_pack(pack, rules)
    save_pack(clean, args.out)
    parts = ", ".join(f"{k}: {v}" for k, v in summary.items())
    print(f"Sanitized {args.pack} -> {args.out}  (redactions — {parts})")
    return 0


def _cmd_replay(args) -> int:
    try:
        pack = load_pack(args.pack)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        results = replay_pack(pack, args.target, workers=args.workers, timeout=args.timeout)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    save_results(results, args.out)
    errors = count_errors(results)
    print(f"Replayed {len(results['results'])} request(s) against {args.target} "
          f"-> {args.out} ({errors} error(s))")
    return 0


def _cmd_diff(args) -> int:
    try:
        pack = load_pack(args.pack)
        results = load_results(args.results)
    except (PackError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    diff = diff_pack_vs_results(
        pack, results,
        ignore=args.ignore,
        unordered_lists=args.unordered_lists,
        latency_threshold_ms=args.latency_threshold_ms,
        latency_ratio=args.latency_ratio,
    )
    save_diff(diff, args.out)
    summary = diff["summary"]
    print(f"Diff: {summary['passed']} passed, {summary['failed']} failed "
          f"(of {summary['total']}) -> {args.out}")
    return 0


def _cmd_report(args) -> int:
    try:
        diff = load_diff(args.diff)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_report(diff, args.format, args.out)
    print(f"Wrote {args.format} report -> {args.out}")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
