"""``pdm`` command-line entry point.

The subcommand surface mirrors the roadmap; each phase fills one in:

    pdm train     # F2  — train both models, log to MLflow, register the winner (LIVE)
    pdm detect    # F2.5 — run the outlier-detection ladder, scored vs. ground truth (LIVE)
    pdm serve     # F4  — FastAPI serving the promoted model
    pdm flow      # F5  — the Prefect drift → retrain loop (the marquee)
    pdm monitor   # F5  — an Evidently drift report, baseline vs. a season shift

Unimplemented stubs exit non-zero with a pointer to the phase that lands them, so
the command stays honest about what is and isn't wired yet.
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

    train_p = sub.add_parser(
        "train", help="train both models, track to MLflow, register the winner (F2)"
    )
    train_p.add_argument(
        "--seed", type=int, default=None, help="seed threading data split → models"
    )
    train_p.add_argument(
        "--no-register",
        action="store_true",
        help="track the runs but do not register the winner in the MLflow registry",
    )
    detect_p = sub.add_parser(
        "detect",
        help="run the outlier-detection ladder, scored vs. ground truth (F2.5)",
    )
    detect_p.add_argument(
        "--seed", type=int, default=None, help="seed threading the detectors"
    )
    detect_p.add_argument(
        "--autoencoder",
        action="store_true",
        help="include the [deep] torch autoencoder rung (needs the '[deep]' extra)",
    )

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
        from . import train as _train

        summary = _train.train(seed=args.seed, register=not args.no_register)
        print(_train.format_summary(summary))
        return 0
    if args.command == "detect":
        from . import data as _data
        from . import detect_score as _ds

        readings = _data.load_readings()
        score = _ds.score_ladder(
            readings, seed=args.seed, include_autoencoder=args.autoencoder
        )
        print(_ds.format_ladder_score(score))
        return 0
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
