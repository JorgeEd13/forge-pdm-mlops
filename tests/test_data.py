"""F1 data-layer tests — offline, against the committed smoke fixture only.

The full-regeneration path needs the generator, which CI never installs (ADR-001),
so these exercise the fixture loader and the fallback contract.
"""

from __future__ import annotations

import warnings

import pandas as pd
import pytest

from pdm_mlops import config, data


def test_load_fixture_returns_readings_frame() -> None:
    df = data.load_fixture()
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert config.TARGET in df.columns
    assert "unit_id" in df.columns


def test_fixture_fallback_warns_when_generator_absent(monkeypatch) -> None:
    # Force the "generator missing" branch regardless of the local environment.
    monkeypatch.setattr(data, "generator_available", lambda: False)
    with pytest.warns(UserWarning, match="SMOKE FIXTURE"):
        df = data.load_readings()
    assert not df.empty


def test_season_request_is_flagged_in_fixture_fallback(monkeypatch) -> None:
    monkeypatch.setattr(data, "generator_available", lambda: False)
    with pytest.warns(UserWarning, match="season='heatwave' is IGNORED"):
        data.load_readings(season="heatwave")


def test_no_fallback_raises_when_generator_absent(monkeypatch) -> None:
    monkeypatch.setattr(data, "generator_available", lambda: False)
    with pytest.raises(data.GeneratorUnavailable):
        data.load_readings(allow_fixture_fallback=False)


def test_full_path_taken_when_generator_present(monkeypatch) -> None:
    # When the generator "is present", load_readings must NOT touch the fixture.
    monkeypatch.setattr(data, "generator_available", lambda: True)
    sentinel = pd.DataFrame({"unit_id": ["u0001"], config.TARGET: [0]})
    monkeypatch.setattr(data, "regenerate_full", lambda **_: sentinel)

    def _boom() -> pd.DataFrame:  # pragma: no cover - must never run
        raise AssertionError("fixture loaded despite generator being present")

    monkeypatch.setattr(data, "load_fixture", _boom)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any fallback warning would fail the test
        out = data.load_readings()
    assert out is sentinel
