"""CLI: compare two published diagnostic_mas run directories (or case.json paths)."""

from __future__ import annotations

import argparse
import json
import sys

from diagnostic_mas.case_delta import compare_run_dirs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nso-diagnostic-delta",
        description=(
            "Compare two nso-diagnostic-run artifacts (case.json). "
            "Reports new / recovered / persistent problems and coverage changes."
        ),
    )
    p.add_argument(
        "older",
        help="Older run directory or case.json path",
    )
    p.add_argument(
        "newer",
        help="Newer run directory or case.json path",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(compare_run_dirs(args.older, args.newer), end="")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
