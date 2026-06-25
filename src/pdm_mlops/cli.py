"""``pdm`` command-line entry point.

F0 ships the skeleton: ``--version`` (smoke-tested in CI, mirroring the
generator's ``forge --version``) plus the subcommand surface stubbed out so the
shape is visible and each later phase fills one in:

    pdm train     # F2 — train both models, log to MLflow, register the winner
    pdm serve     # F4 — FastAPI serving the promoted model
    pdm flow      # F5 — the Prefect drift → retrain loop (the marquee)
    pdm monitor   # F5 — an Evidently drift report, baseline vs. a season shift

Each stub exits non-zero with a pointer to the phase that implements it, so the
command is honest about what is and isn't wired yet.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def _not_yet(phase: str) -> int:
    print(f"Not implemented yet — lands in {phase}. See docs/ROADMAP.md.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdm",
        description="MLOps pipeline over synthetic predictive-maintenance telemetry.",
    )
    parser.add_argument("--version", action="version", version=f"forge-pdm-mlops {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("train", help="train both models, track to MLflow, register the winner (F2)")
    sub.add_parser("serve", help="serve the promoted model with FastAPI (F4)")
    flow = sub.add_parser("flow", help="run the drift → retrain Prefect flow (F5)")
    flow.add_argument("--season", default=None, help="generator season used as the drift stimulus")
    sub.add_parser("monitor", help="emit an Evidently drift report (F5)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "train":
        return _not_yet("F2")
    if args.command == "serve":
        return _not_yet("F4")
    if args.command == "flow":
        return _not_yet("F5")
    if args.command == "monitor":
        return _not_yet("F5")
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
