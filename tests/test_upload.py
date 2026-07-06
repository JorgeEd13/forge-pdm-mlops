"""F8 bring-your-own-data tests — the upload parse/map/validate layer + `/demo/upload` (ADR-017).

Two halves, both offline and deterministic:

* **Pure** (no extras): :mod:`pdm_mlops.upload` — parse bounds, the fuzzy column auto-match
  (exact / synonym / near-miss), the partial-data era-``NULL`` frame build, the fail-loud
  validations, and the summary/histogram. These need nothing but pandas.
* **Endpoint** (`[serve]`-gated, like ``test_demo.py``): a tiny model is trained on the
  committed fixture, registered and promoted in a tmp SQLite MLflow backend, and the
  two-phase upload (preview → confirmed-mapping score) is driven through a ``TestClient`` —
  the round-trip, the fuzzy-renamed headers, the partial-data path, the 4xx rejections, and
  the **no-raw-row-persistence** posture (an injected log stays empty).
"""

from __future__ import annotations

import io
import json

import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

from pdm_mlops import config, features, upload

# --- pure: parsing bounds -----------------------------------------------------


def _csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode()


def test_parse_csv_and_parquet_round_trip() -> None:
    df = pd.DataFrame({"engine_speed_rpm": [1800, 2000], "coolant_temp_c": [90, 95]})
    assert upload.parse_upload("b.csv", _csv_bytes(df)).shape == (2, 2)
    buf = io.BytesIO()
    df.to_parquet(buf)
    assert upload.parse_upload("b.parquet", buf.getvalue()).shape == (2, 2)


def test_parse_rejects_oversize_with_413() -> None:
    big = b"x" * (upload.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(upload.UploadError) as exc:
        upload.parse_upload("b.csv", big)
    assert exc.value.status_code == 413


def test_parse_rejects_too_many_rows() -> None:
    df = pd.DataFrame({"engine_speed_rpm": range(upload.MAX_ROWS + 1)})
    with pytest.raises(upload.UploadError):
        upload.parse_upload("b.csv", _csv_bytes(df))


def test_parse_rejects_unknown_extension() -> None:
    with pytest.raises(upload.UploadError):
        upload.parse_upload("b.txt", b"engine_speed_rpm\n1800\n")


def test_parse_rejects_non_numeric_prose() -> None:
    # A file that isn't J1939-like data at all → a clear error, not a 500 downstream.
    with pytest.raises(upload.UploadError):
        upload.parse_upload("b.csv", b"story,author\nonce,me\ntwice,you\n")


# --- pure: fuzzy column auto-match --------------------------------------------


def test_suggest_mapping_exact_names() -> None:
    m = upload.suggest_mapping(list(features.FEATURE_COLUMNS))
    assert all(m[c] == c for c in features.FEATURE_COLUMNS)


def test_suggest_mapping_fuzzy_and_synonyms() -> None:
    headers = ["RPM", "Coolant Temp", "oil_press", "Load%", "fuel",
               "boost", "EGT", "DEF", "vibration", "unrelated_col"]
    m = upload.suggest_mapping(headers)
    assert m["engine_speed_rpm"] == "RPM"
    assert m["coolant_temp_c"] == "Coolant Temp"
    assert m["oil_pressure_kpa"] == "oil_press"
    assert m["egt_c"] == "EGT"
    assert m["vibration_mms"] == "vibration"
    # An unrelated column is never force-mapped onto a signal.
    assert "unrelated_col" not in m.values()


def test_suggest_mapping_no_header_reused() -> None:
    # Two signals must not both claim one column; exact match wins its own header.
    headers = ["oil_pressure_kpa", "boost_pressure_kpa"]
    m = upload.suggest_mapping(headers)
    assert m["oil_pressure_kpa"] == "oil_pressure_kpa"
    assert m["boost_pressure_kpa"] == "boost_pressure_kpa"


def test_suggest_mapping_reports_none_when_nothing_matches() -> None:
    m = upload.suggest_mapping(["alpha", "beta", "gamma"])
    assert all(v is None for v in m.values())


# --- pure: build frame + validate ---------------------------------------------


def test_build_frame_partial_is_era_null() -> None:
    df = pd.DataFrame({"RPM": [1800, 2000], "Coolant Temp": [90, 95]})
    mapping = upload.resolve_mapping(list(df.columns), upload.suggest_mapping(list(df.columns)))
    X = upload.build_frame(df, mapping)
    assert list(X.columns) == list(features.FEATURE_COLUMNS)
    assert X["engine_speed_rpm"].tolist() == [1800.0, 2000.0]
    # Every unmapped signal is an all-NaN era-NULL column.
    assert X["egt_c"].isna().all()
    assert list(upload.mapped_signals(mapping)) == ["engine_speed_rpm", "coolant_temp_c"]


def test_assert_scorable_rejects_no_mapping() -> None:
    df = pd.DataFrame({"alpha": [1, 2]})
    mapping = upload.resolve_mapping(list(df.columns), {})
    X = upload.build_frame(df, mapping)
    with pytest.raises(upload.UploadError):
        upload.assert_scorable(X, mapping)


def test_assert_scorable_rejects_all_nan_mapped_column() -> None:
    # Headers map, but the values aren't numbers → fail loud, not a mystery 500.
    df = pd.DataFrame({"engine_speed_rpm": ["a", "b"]})
    mapping = upload.resolve_mapping(list(df.columns), {"engine_speed_rpm": "engine_speed_rpm"})
    X = upload.build_frame(df, mapping)
    with pytest.raises(upload.UploadError):
        upload.assert_scorable(X, mapping)


def test_resolve_mapping_drops_unknown_signals_and_headers() -> None:
    resolved = upload.resolve_mapping(
        ["h1"], {"engine_speed_rpm": "h1", "engine_speed_rpm_typo": "h1", "coolant_temp_c": "nope"}
    )
    assert resolved["engine_speed_rpm"] == "h1"
    assert resolved["coolant_temp_c"] is None
    assert "engine_speed_rpm_typo" not in resolved


# --- pure: summary ------------------------------------------------------------


def test_summarize_counts_and_histogram() -> None:
    s = upload.summarize([0.1, 0.2, 0.9, 0.95, 0.5], n_signals_provided=2)
    assert s.n_rows == 5
    assert s.n_high_risk == 3  # 0.9, 0.95, 0.5 are >= 0.5
    assert s.pct_high_risk == 60.0
    assert sum(s.histogram) == 5
    assert len(s.histogram) == upload.HISTOGRAM_BINS
    assert s.n_signals_provided == 2


def test_summarize_probability_one_lands_in_last_bin() -> None:
    s = upload.summarize([1.0], n_signals_provided=9)
    assert s.histogram[-1] == 1


# --- endpoint (needs the [serve] extra) ---------------------------------------

pytest.importorskip("fastapi", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("httpx", reason="needs the `[serve]` extra (F4/ADR-009)")
pytest.importorskip("multipart", reason="needs `python-multipart` in the `[serve]` extra (F8)")

from fastapi.testclient import TestClient  # noqa: E402

from pdm_mlops import registry, serve, store_pg, train  # noqa: E402

NAME = config.REGISTERED_MODEL_NAME

_HAS_CLOUD = True
try:
    import sqlalchemy  # noqa: F401
except ImportError:  # pragma: no cover
    _HAS_CLOUD = False

needs_cloud = pytest.mark.skipif(not _HAS_CLOUD, reason="needs the `[cloud]` extra (F7/ADR-015)")


@pytest.fixture
def tmp_tracking(tmp_path):
    return config.sqlite_tracking_uri(tmp_path / "mlflow.db")


@pytest.fixture
def fixture_readings() -> pd.DataFrame:
    return pd.read_parquet(config.SAMPLE_READINGS)


def _train_and_promote(tmp_tracking: str, readings: pd.DataFrame) -> str:
    summary = train.train(seed=0, tracking_uri=tmp_tracking, readings=readings, register=True)
    client = MlflowClient(tracking_uri=tmp_tracking, registry_uri=tmp_tracking)
    assert registry.promote(client, NAME, summary.registered_version).promoted
    return str(summary.registered_version)


def _app(tmp_tracking: str, log: store_pg.PredictionLog | None = None):
    store = serve.ModelStore(tracking_uri=tmp_tracking)
    return TestClient(serve.create_app(store=store, prediction_log=log))


def _feature_csv(readings: pd.DataFrame, n: int, rename: dict | None = None) -> bytes:
    frame = readings.loc[:, list(features.FEATURE_COLUMNS)].head(n).copy()
    if rename:
        frame = frame.rename(columns=rename)
    return frame.to_csv(index=False).encode()


def test_upload_preview_returns_suggested_mapping(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    csv = _feature_csv(fixture_readings, 5)
    resp = client.post("/demo/upload", files={"file": ("batch.csv", csv, "text/csv")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_rows"] == 5
    assert body["n_signals_matched"] == len(features.FEATURE_COLUMNS)
    assert body["suggested_mapping"]["engine_speed_rpm"] == "engine_speed_rpm"


def test_upload_score_round_trip(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    csv = _feature_csv(fixture_readings, 6)
    mapping = {c: c for c in features.FEATURE_COLUMNS}
    resp = client.post(
        "/demo/upload",
        files={"file": ("batch.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_rows"] == 6
    assert body["n_signals_provided"] == len(features.FEATURE_COLUMNS)
    assert len(body["failure_probability"]) == 6
    assert all(0.0 <= p <= 1.0 for p in body["failure_probability"])
    assert sum(body["summary"]["histogram"]) == 6
    # `demo` is tag-driven: only the F6 seed script tags a version `demo=fixture`. A model
    # registered here by `pdm train` carries no such tag, so it honestly reports False — the
    # flag reflects the served version, it isn't hard-coded on the upload path.
    assert body["demo"] is False


def test_upload_score_with_renamed_headers(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    rename = {"engine_speed_rpm": "RPM", "coolant_temp_c": "Coolant Temp"}
    csv = _feature_csv(fixture_readings, 4, rename=rename)
    # Preview auto-matches the renamed headers; confirm and score with the suggestion.
    preview = client.post("/demo/upload", files={"file": ("b.csv", csv, "text/csv")}).json()
    assert preview["suggested_mapping"]["engine_speed_rpm"] == "RPM"
    resp = client.post(
        "/demo/upload",
        files={"file": ("b.csv", csv, "text/csv")},
        data={"mapping": json.dumps(preview["suggested_mapping"])},
    )
    assert resp.status_code == 200
    assert resp.json()["n_signals_provided"] == len(features.FEATURE_COLUMNS)


def test_upload_score_partial_data_flags_era_null(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    frame = fixture_readings.loc[:, ["engine_speed_rpm", "coolant_temp_c"]].head(3)
    csv = frame.to_csv(index=False).encode()
    mapping = {"engine_speed_rpm": "engine_speed_rpm", "coolant_temp_c": "coolant_temp_c"}
    resp = client.post(
        "/demo/upload",
        files={"file": ("b.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_signals_provided"] == 2
    assert set(body["unmapped_signals"]) == set(features.FEATURE_COLUMNS) - {
        "engine_speed_rpm", "coolant_temp_c"
    }
    assert len(body["failure_probability"]) == 3


def test_upload_not_j1939_returns_400_not_500(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    resp = client.post(
        "/demo/upload", files={"file": ("prose.csv", b"story,author\na,b\nc,d\n", "text/csv")}
    )
    assert resp.status_code == 400


def test_upload_no_mapping_selected_returns_400(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    csv = _feature_csv(fixture_readings, 2)
    # A confirmed mapping that maps nothing → fail loud (not a mystery 500).
    resp = client.post(
        "/demo/upload",
        files={"file": ("b.csv", csv, "text/csv")},
        data={"mapping": json.dumps({c: None for c in features.FEATURE_COLUMNS})},
    )
    assert resp.status_code == 400


def test_upload_oversize_rejected_413(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    big = b"engine_speed_rpm\n" + b"1800\n" * (upload.MAX_UPLOAD_BYTES // 5)
    resp = client.post("/demo/upload", files={"file": ("big.csv", big, "text/csv")})
    assert resp.status_code == 413


def test_upload_503_without_a_promoted_model(tmp_tracking, fixture_readings) -> None:
    client = _app(tmp_tracking)  # nothing promoted
    csv = _feature_csv(fixture_readings, 2)
    mapping = {c: c for c in features.FEATURE_COLUMNS}
    resp = client.post(
        "/demo/upload",
        files={"file": ("b.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping)},
    )
    assert resp.status_code == 503


@needs_cloud
def test_upload_does_not_persist_raw_rows(tmp_tracking, fixture_readings, tmp_path) -> None:
    # The no-raw-row-persistence posture: an uploaded batch is scored but never logged.
    _train_and_promote(tmp_tracking, fixture_readings)
    log = store_pg.open_log(f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    client = _app(tmp_tracking, log=log)
    csv = _feature_csv(fixture_readings, 5)
    mapping = {c: c for c in features.FEATURE_COLUMNS}
    resp = client.post(
        "/demo/upload",
        files={"file": ("b.csv", csv, "text/csv")},
        data={"mapping": json.dumps(mapping)},
    )
    assert resp.status_code == 200
    assert log.recent(limit=10) == []  # nothing from the upload was written
    log.dispose()


def test_demo_page_has_the_upload_control(tmp_tracking, fixture_readings) -> None:
    _train_and_promote(tmp_tracking, fixture_readings)
    client = _app(tmp_tracking)
    page = client.get("/demo").text
    assert "Bring your own data" in page
    assert 'type="file"' in page
    assert "/demo/upload" in page
