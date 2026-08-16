"""Command line entry point.

Designed for external schedulers (cron, GitHub Actions, Airflow): one invocation is
one run, exit code 0 means "ran cleanly", including when it stopped early because
the request budget or account quota was spent.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings
from .pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pd-books", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and normalize but write nothing",
    )
    parser.add_argument(
        "--stage",
        action="append",
        choices=["metadata", "text"],
        help="run only these stages (repeatable); default is both",
    )
    parser.add_argument("--mode", choices=["full", "incremental"], help="override run.mode")
    parser.add_argument("--max-books", type=int, help="override run.max_books")
    parser.add_argument("--log-level", help="override run.log_level")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)

    if args.mode:
        settings.run.mode = args.mode
    if args.max_books is not None:
        settings.run.max_books = args.max_books
    if args.log_level:
        settings.run.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, settings.run.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not settings.enabled_sources():
        logging.error("no enabled sources in %s", args.config)
        return 2

    stages = tuple(args.stage) if args.stage else ("metadata", "text")

    try:
        reports = run(settings, dry_run=args.dry_run, stages=stages)
    except ValueError as exc:
        # Configuration problems (missing API key, unknown provider) are user errors.
        logging.error("%s", exc)
        return 2

    for report in reports:
        print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
