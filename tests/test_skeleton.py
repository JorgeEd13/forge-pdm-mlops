"""F0 smoke tests — the skeleton imports, the CLI runs, the sample data is present.

Offline and deterministic, mirroring the companion generator's test discipline.
Later phases add focused tests for data/features/train/registry/serve/flow.
"""

from __future__ import annotations

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


def test_unimplemented_subcommands_are_honestly_stubbed(capsys) -> None:
    # Subcommands whose phase hasn't landed report "not yet" rather than faking
    # capability (`train` went live in F2 and is covered by test_train.py).
    for cmd in ("serve", "flow", "monitor"):
        assert main([cmd]) == 2
        err = capsys.readouterr().err
        assert "Not implemented yet" in err


def test_committed_sample_exists() -> None:
    # The offline data slice the pipeline trains/tests on (ADR-001).
    assert config.SAMPLE_READINGS.exists(), "run scripts/build_sample.py to regenerate"
    assert config.SAMPLE_READINGS.suffix == ".parquet"
