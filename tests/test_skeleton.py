"""F0 smoke tests — the skeleton imports, the CLI runs, the sample data is present.

Offline and deterministic, mirroring the companion generator's test discipline.
Later phases add focused tests for data/features/train/registry/serve/flow.
"""

from __future__ import annotations

import pytest

import pdm_mlops
from pdm_mlops import config
from pdm_mlops.cli import main


def test_package_imports_and_has_version() -> None:
    assert isinstance(pdm_mlops.__version__, str)
    assert pdm_mlops.__version__


def test_cli_no_args_prints_help_and_succeeds(capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "pdm" in out


def test_every_subcommand_is_wired(capsys) -> None:
    # By F5 the whole roadmap surface is live — no subcommand is a "not yet" stub
    # anymore (each phase's behaviour is covered by its own test module). This guards
    # the invariant that the CLI never silently regresses a live command back to a stub:
    # `--help` for every declared subcommand parses and exits 0, and none of them prints
    # the honest-stub sentinel.
    from pdm_mlops.cli import build_parser

    sub = next(
        a for a in build_parser()._actions if a.dest == "command"
    )  # the subparsers action
    for cmd in sub.choices:
        with pytest.raises(SystemExit) as exc:  # argparse exits 0 after printing help
            main([cmd, "--help"])
        assert exc.value.code == 0
        assert "Not implemented yet" not in capsys.readouterr().err


def test_committed_sample_exists() -> None:
    # The offline data slice the pipeline trains/tests on (ADR-001).
    assert config.SAMPLE_READINGS.exists(), "run scripts/build_sample.py to regenerate"
    assert config.SAMPLE_READINGS.suffix == ".parquet"
