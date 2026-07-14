"""``pdm`` command-line entry point.

The subcommand surface mirrors the roadmap; each phase fills one in:

    pdm train     # F2  — train both models, log to MLflow, register the winner (LIVE)
    pdm detect    # F2.5 — run the outlier-detection ladder, scored vs. ground truth (LIVE)
    pdm tune      # F2.6 — grouped-CV Optuna HPO on the cleaned inputs (LIVE)
    pdm sequence  # F2.7 — three-rung temporal ladder (per-row / temporal / TCN) (LIVE)
    pdm ceiling   # F2.8 — characterize the ceiling: decomposition + bound + stack probe (LIVE)
    pdm promote   # F3  — metric-gated promotion of a registered version to production (LIVE)
    pdm rollback  # F3  — restore the previous production version (LIVE)
    pdm serve     # F4  — FastAPI serving the promoted model (LIVE)
    pdm monitor   # F5  — an Evidently drift report + decision, baseline vs. a season shift (LIVE)
    pdm flow      # F5  — the Prefect drift → retrain → gated-promote loop, the marquee (LIVE)
    pdm generate-run  # F14a — the generation WORKER: run one queued fleet (the Cloud Run Job)

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
    train_p.add_argument(
        "--tune",
        action="store_true",
        help="run the F2.6 grouped-CV HPO first and train on the tuned params (cleaned frame)",
    )
    train_p.add_argument(
        "--audit",
        action="store_true",
        help="run the F2.6 training watchers (overfit-gap + majority-baseline), fail loud",
    )
    train_p.add_argument(
        "--diagnose",
        action="store_true",
        help="log the F2.6 diagnostic artifacts (importance/calibration/threshold/learning curve)",
    )
    train_p.add_argument(
        "--clean",
        action="store_true",
        help="train on the F2.5-cleaned frame (the signal_suspect feature); implied by --tune",
    )

    tune_p = sub.add_parser(
        "tune",
        help="grouped-CV Optuna HPO on the cleaned inputs, tracked to MLflow (F2.6)",
    )
    tune_p.add_argument("--seed", type=int, default=None, help="seed threading data + search")
    tune_p.add_argument(
        "--trials", type=int, default=None, help="Optuna trials per model (default 40)"
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

    seq_p = sub.add_parser(
        "sequence",
        help="three-rung temporal ladder: per-row / temporal-features / causal TCN (F2.7)",
    )
    seq_p.add_argument("--seed", type=int, default=None, help="seed threading split → rungs")
    seq_p.add_argument(
        "--window", type=int, default=None, help="lookback window in rows (default 24)"
    )
    seq_p.add_argument(
        "--epochs", type=int, default=None, help="TCN training epochs (default 8)"
    )
    seq_p.add_argument(
        "--channels", type=int, default=None, help="TCN conv channels (default 32)"
    )
    seq_p.add_argument(
        "--device", default=None, help="torch device for the TCN (default: cuda if available)"
    )
    seq_p.add_argument(
        "--register",
        action="store_true",
        help="register the winning rung (tabular or temporal) in the MLflow registry",
    )

    ceil_p = sub.add_parser(
        "ceiling",
        help="characterize the ceiling: horizon/mode decomposition + upper-bound + stack probe (F2.8)",
    )
    ceil_p.add_argument("--seed", type=int, default=None, help="seed threading split → instruments")
    ceil_p.add_argument(
        "--window", type=int, default=None, help="temporal-features lookback in rows (default 24)"
    )

    promote_p = sub.add_parser(
        "promote",
        help="metric-gated promotion of a registered version to production (F3)",
    )
    promote_p.add_argument(
        "--version",
        default=None,
        help="registered version to promote (default: the latest, i.e. the one just trained)",
    )
    promote_p.add_argument(
        "--min-delta",
        type=float,
        default=None,
        help="gate tolerance: promote if candidate >= incumbent - min_delta (default 0.0)",
    )
    promote_p.add_argument(
        "--force",
        action="store_true",
        help="bypass the metric gate and promote unconditionally",
    )
    sub.add_parser("rollback", help="restore the previous production version (F3)")

    serve_p = sub.add_parser("serve", help="serve the promoted model with FastAPI (F4)")
    serve_p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    flow = sub.add_parser("flow", help="run the drift → retrain Prefect flow (F5)")
    flow.add_argument("--season", default=None, help="generator season used as the drift stimulus")
    flow.add_argument("--seed", type=int, default=None, help="seed threading the retrain")
    flow.add_argument(
        "--min-delta",
        type=float,
        default=None,
        help="promotion gate tolerance passed to F3 promote (default 0.0)",
    )
    monitor_p = sub.add_parser("monitor", help="emit an Evidently drift report + decision (F5)")
    monitor_p.add_argument(
        "--season", default=None, help="generator season used as the drift stimulus"
    )

    # The generation worker's entry point (F14a). This is what the Cloud Run Job runs —
    # the *only* process that executes the forge. Arguments double as env overrides
    # (RUN_ID / GENERATION_UNITS / GENERATION_DAYS / GENERATION_SEED) because that is how
    # a job execution is parametrised.
    gen_p = sub.add_parser(
        "generate-run",
        help="worker: generate one bounded fleet for a queued run and store it (F14a)",
    )
    gen_p.add_argument("--run-id", default=None, help="the queued run to execute (env RUN_ID)")
    gen_p.add_argument("--units", type=int, default=None, help="fleet size")
    gen_p.add_argument("--days", type=int, default=None, help="window length in days")
    gen_p.add_argument("--seed", type=int, default=None, help="generation seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "train":
        from . import train as _train

        tuned = None
        if args.tune:
            from . import tune as _tune

            results = _tune.tune(seed=args.seed)
            print(_tune.format_tune(results))
            tuned = {name: r.best_params for name, r in results.items()}
        summary = _train.train(
            seed=args.seed,
            register=not args.no_register,
            tuned=tuned,
            clean=True if args.clean else None,
            audit=args.audit,
            diagnose=args.diagnose,
        )
        print(_train.format_summary(summary))
        return 0
    if args.command == "tune":
        from . import tune as _tune

        results = _tune.tune(seed=args.seed, n_trials=args.trials or _tune.DEFAULT_TRIALS)
        print(_tune.format_tune(results))
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
    if args.command == "sequence":
        from . import data as _data
        from . import sequence as _seq

        readings = _data.load_readings()
        window = args.window or _seq.DEFAULT_WINDOW
        tcn_kwargs: dict[str, object] = {"window": window}
        if args.epochs is not None:
            tcn_kwargs["epochs"] = args.epochs
        if args.channels is not None:
            tcn_kwargs["channels"] = args.channels
        if args.device is not None:
            tcn_kwargs["device"] = args.device
        cmp = _seq.compare(
            readings,
            seed=args.seed,
            window=window,
            tcn=_seq.TCNClassifier(**tcn_kwargs),
            register=args.register,
        )
        print(_seq.format_comparison(cmp))
        return 0
    if args.command == "ceiling":
        from . import ceiling as _ceiling
        from . import data as _data
        from . import sequence as _seq

        readings = _data.load_readings()
        report = _ceiling.characterize(
            readings, seed=args.seed, window=args.window or _seq.DEFAULT_WINDOW
        )
        print(_ceiling.format_report(report))
        return 0
    if args.command == "promote":
        from . import config as _config
        from . import registry as _registry

        client = _registry._client()
        name = _config.REGISTERED_MODEL_NAME
        version = args.version or _registry.latest_version(client, name)
        result = _registry.promote(
            client,
            name,
            version,
            gate=not args.force,
            min_delta=(
                args.min_delta if args.min_delta is not None else _registry.DEFAULT_MIN_DELTA
            ),
        )
        print(_registry.format_promotion(result))
        return 0 if result.promoted else 1
    if args.command == "rollback":
        from . import config as _config
        from . import registry as _registry

        client = _registry._client()
        name = _config.REGISTERED_MODEL_NAME
        restored = _registry.rollback(client, name)
        print(f"Rolled back '{name}': production is now v{restored}.")
        return 0
    if args.command == "serve":
        import uvicorn

        from . import serve as _serve

        app = _serve.create_app()
        print(
            f"Serving the production-aliased model on http://{args.host}:{args.port} "
            "(GET /health, /model-info · POST /predict)",
            file=sys.stderr,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "flow":
        from . import flows as _flows

        result = _flows.run_drift_retrain(
            season=args.season, seed=args.seed, min_delta=args.min_delta
        )
        print(result.summary())
        # A stable-data cycle (no retrain) and a held candidate both exit non-zero, so a
        # scheduled runner can tell "a model was promoted" from "nothing changed".
        return 0 if result.promoted else 1
    if args.command == "monitor":
        from . import monitor as _monitor

        report = _monitor.detect_drift(season=args.season)
        print(report.summary())
        return 0 if report.drifted else 1
    if args.command == "generate-run":
        from . import generate as _generate
        from . import worker as _worker

        # An explicit flag wins; anything omitted falls back to the job's env override.
        spec = None
        if args.units is not None or args.days is not None or args.seed is not None:
            spec = _generate.GenerationSpec(
                n_units=args.units if args.units is not None else _generate.DEFAULT_UNITS,
                days=args.days if args.days is not None else _generate.DEFAULT_DAYS,
                seed=args.seed if args.seed is not None else 42,
            )
        return _worker.main(args.run_id, spec)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
