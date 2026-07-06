"""F8 — bring-your-own-data: parse an uploaded J1939 batch, map arbitrary column
names onto :data:`features.FEATURE_COLUMNS`, validate, and summarize the scoring.

This is the parse + column-mapping + validate + summarize layer that wraps the
already-shipped batch scoring core (``POST /predict`` / ``serve._score_frame``). It is a
**pure module** — no FastAPI, no I/O beyond the bytes it is handed — so the whole
"different column names we don't know of" problem is unit-tested directly, and
:mod:`serve` stays a thin HTTP wiring layer over it.

Four responsibilities:

1. **Parse, bounded.** :func:`parse_upload` reads CSV or Parquet from the raw bytes with a
   **hard size and row cap** (a huge upload must not wedge a scale-to-zero Cloud Run
   instance) and rejects a non-tabular / non-numeric file with a clear error — a 4xx, never
   a 500 (the forensic-watcher spirit: fail loud with an actionable message).
2. **Fuzzy column auto-match.** A real tester's CSV almost never uses our exact nine header
   names, so :func:`suggest_mapping` pre-fills a *map-your-columns* step: for each expected
   signal it proposes the best-matching uploaded header (a normalized-token + synonym +
   :mod:`difflib` score, stdlib only — **no new dependency**). The tester confirms or
   corrects it in the UI; the server never silently guesses.
3. **Build the frame, era-NULL for the rest.** :func:`build_frame` applies a confirmed
   mapping onto the fixed feature order; **unmapped signals are era-``NULL``** (the model
   handles missing signals natively — LightGBM), so a *partial* dataset still scores,
   honestly flagged "N of 9 signals provided".
4. **Summarize.** :func:`summarize` returns the small result the UI shows on top of the
   per-row probabilities: how many rows are flagged high-risk at a stated threshold, and a
   probability histogram.

**Honesty + privacy posture (unchanged from F7).** The predictions still come from the
``demo=fixture`` model (the caller labels the result as such). And **no raw uploaded row is
persisted** — an uploaded dataset is arbitrary, so the managed-DB posture stays "counts and
summaries, never raw rows"; F8 simply does not write uploaded rows anywhere.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from difflib import SequenceMatcher

import pandas as pd

from . import features

# --- bounds (guardrails designed first) --------------------------------------

#: Hard cap on the uploaded file size. A demo batch is tiny (a 5k-row × 9-col CSV is
#: ~300 KB); the cap exists so a hostile/huge upload can't exhaust memory on a
#: scale-to-zero instance. Enforced by reading only this many bytes + 1 (see serve.py).
MAX_UPLOAD_BYTES: int = 2_000_000

#: Hard cap on rows scored per upload — bounds the scoring work and the response size.
MAX_ROWS: int = 5_000

#: The probability at/above which a row is reported as "high-risk" in the summary. A
#: presentational threshold for the count only — the model still returns the raw
#: probability per row; nothing here thresholds the served prediction.
HIGH_RISK_THRESHOLD: float = 0.5

#: Number of equal-width bins over [0, 1] for the summary histogram.
HISTOGRAM_BINS: int = 10

#: Minimum fuzzy score for :func:`suggest_mapping` to *propose* a header for a signal.
#: Below this we propose nothing (the tester maps it by hand, or leaves it era-NULL).
_MATCH_THRESHOLD: float = 0.6


class UploadError(Exception):
    """A user-actionable upload problem — translated to a 4xx (never a 500).

    ``status_code`` lets the caller pick 413 (too large) vs. 400 (everything else) so the
    HTTP contract is honest about *why* the upload was rejected.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- fuzzy column matching ----------------------------------------------------

#: Normalized synonym tokens per expected signal — the domain knowledge that lets a
#: tester's `RPM` / `engine_rpm` / `EngineSpeed` all resolve to ``engine_speed_rpm``. Keys
#: are the canonical :data:`features.FEATURE_COLUMNS`; values are already-normalized tokens
#: (lowercase, alphanumeric-only) compared against the normalized uploaded header. Exact and
#: synonym matches beat the generic :mod:`difflib` ratio, so a well-named column maps with
#: full confidence and an oddly-named one still has a fighting chance.
_SYNONYMS: dict[str, set[str]] = {
    "engine_speed_rpm": {"rpm", "enginerpm", "enginespeed", "speedrpm", "revs", "rev", "n"},
    "coolant_temp_c": {"coolant", "coolanttemp", "coolanttemperature", "watertemp", "ect", "coolantc"},
    "oil_pressure_kpa": {"oilpressure", "oilpress", "oilp", "oil", "oilpsi"},
    "engine_load_pct": {"engineload", "load", "loadpct", "torquepct", "loadpercent"},
    "fuel_rate_lph": {"fuelrate", "fuel", "fuelconsumption", "fuellph", "fuelflow"},
    "boost_pressure_kpa": {"boost", "boostpressure", "manifoldpressure", "map", "turboboost", "boostkpa"},
    "egt_c": {"egt", "exhausttemp", "exhaustgastemp", "exhaust", "exhausttemperature"},
    "def_level_pct": {"def", "deflevel", "adblue", "urea", "defpct", "defpercent"},
    "vibration_mms": {"vibration", "vib", "vibrationlevel", "vibmms", "vibe"},
}


def _normalize(name: str) -> str:
    """Lowercase, keep only alphanumerics — so ``"Engine RPM"`` and ``"engine_rpm"`` compare equal-ish."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _match_score(signal: str, header: str) -> float:
    """A 0..1 confidence that ``header`` names ``signal``.

    Exact normalized equality is 1.0; a synonym hit is 0.95; otherwise the
    :mod:`difflib` ratio of the normalized strings (a graceful fallback for near-misses
    like ``coolant_temperature`` → ``coolant_temp_c``). Stdlib only.
    """
    s_norm, h_norm = _normalize(signal), _normalize(header)
    if not h_norm:
        return 0.0
    if s_norm == h_norm:
        return 1.0
    if h_norm in _SYNONYMS.get(signal, set()):
        return 0.95
    return SequenceMatcher(None, s_norm, h_norm).ratio()


def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    """Propose a header for each expected signal (``None`` if nothing matches well).

    A **global greedy** assignment: score every (signal, header) pair, then take the
    strongest pairs first, each signal and each header used at most once. This stops two
    signals fighting over one column (e.g. an exact ``oil_pressure_kpa`` claims that header
    before ``boost_pressure_kpa`` can grab it on a weak ``…pressure…`` overlap). Only pairs
    at or above :data:`_MATCH_THRESHOLD` are proposed; the rest are left ``None`` for the
    tester to map by hand or leave era-NULL.
    """
    scored = sorted(
        (
            (_match_score(sig, hdr), sig, hdr)
            for sig in features.FEATURE_COLUMNS
            for hdr in headers
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    mapping: dict[str, str | None] = {sig: None for sig in features.FEATURE_COLUMNS}
    used_signals: set[str] = set()
    used_headers: set[str] = set()
    for score, sig, hdr in scored:
        if score < _MATCH_THRESHOLD:
            break
        if sig in used_signals or hdr in used_headers:
            continue
        mapping[sig] = hdr
        used_signals.add(sig)
        used_headers.add(hdr)
    return mapping


# --- parsing (bounded) --------------------------------------------------------


def parse_upload(filename: str, content: bytes) -> pd.DataFrame:
    """Parse ``content`` (CSV or Parquet, by extension) into a bounded DataFrame.

    Fails loud with :class:`UploadError` (→ a 4xx) on every foreseeable bad input: too
    large, an unknown extension, an unparseable body, an empty table, or a table with **no
    numeric columns at all** (prose/garbage that isn't J1939-like data). The row cap keeps
    the scoring work bounded. This is the "fail loud, never 500" guardrail.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"file too large: {len(content)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte cap.",
            status_code=413,
        )

    lower = (filename or "").lower()
    try:
        if lower.endswith(".parquet"):
            frame = pd.read_parquet(io.BytesIO(content))
        elif lower.endswith(".csv") or not lower:
            frame = pd.read_csv(io.BytesIO(content))
        else:
            raise UploadError(
                f"unsupported file type '{filename}': upload a .csv or .parquet batch."
            )
    except UploadError:
        raise
    except Exception as exc:  # pandas raises a zoo of parse errors — normalise to a 400
        raise UploadError(f"could not parse the file: {exc}") from exc

    if frame.empty or frame.shape[1] == 0:
        raise UploadError("the file has no rows or no columns.")
    if len(frame) > MAX_ROWS:
        raise UploadError(
            f"too many rows: {len(frame)} exceeds the {MAX_ROWS}-row cap for the demo."
        )
    # A file with zero numeric-coercible columns isn't J1939 signal data at all.
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    if not bool(numeric.notna().any().any()):
        raise UploadError(
            "no numeric columns found — this doesn't look like J1939 signal data."
        )
    return frame


# --- apply a confirmed mapping + validate ------------------------------------


def build_frame(
    frame: pd.DataFrame, mapping: dict[str, str | None]
) -> pd.DataFrame:
    """Apply a confirmed ``mapping`` → the model's input frame, in fixed feature order.

    For each expected signal, pull the mapped source column (coerced to numeric); an
    **unmapped** signal becomes an all-``NaN`` era-NULL column. The result is exactly the
    :data:`features.FEATURE_COLUMNS`-ordered frame the scoring core expects — no leaky
    column can enter, since only the nine feature slots are ever populated.
    """
    n = len(frame)
    columns: dict[str, pd.Series] = {}
    for sig in features.FEATURE_COLUMNS:
        source = mapping.get(sig)
        if source is not None and source in frame.columns:
            columns[sig] = pd.to_numeric(frame[source], errors="coerce").reset_index(drop=True)
        else:
            columns[sig] = pd.Series([float("nan")] * n, dtype="float64")
    X = pd.DataFrame(columns, columns=list(features.FEATURE_COLUMNS))
    features.assert_no_leakage(X)
    return X


def resolve_mapping(
    headers: list[str], provided: dict[str, str | None]
) -> dict[str, str | None]:
    """Validate a tester-supplied mapping against the known signals and headers.

    Keeps only keys that are real signals, drops values that aren't real uploaded headers
    (treated as "unmapped"), and returns a full mapping over every signal. Defends the
    scorer against a hand-edited or stale mapping payload.
    """
    header_set = set(headers)
    resolved: dict[str, str | None] = {}
    for sig in features.FEATURE_COLUMNS:
        src = provided.get(sig)
        resolved[sig] = src if (src in header_set) else None
    return resolved


def mapped_signals(mapping: dict[str, str | None]) -> dict[str, str]:
    """Just the signals that resolved to a header (feature → source header)."""
    return {sig: src for sig, src in mapping.items() if src is not None}


def assert_scorable(X: pd.DataFrame, mapping: dict[str, str | None]) -> None:
    """Fail loud (4xx) unless the mapped data can actually be scored.

    Two ways an upload is un-scorable even after parsing: (1) **no signal mapped** at all —
    "bring your own data" with zero recognised columns; (2) every mapped column coerced to
    all-``NaN`` — the headers were mapped but the values aren't numbers. Both are a clear
    :class:`UploadError`, not a downstream 500.
    """
    mapped = mapped_signals(mapping)
    if not mapped:
        raise UploadError(
            "no signals mapped — map at least one uploaded column to a J1939 signal."
        )
    if not bool(X[list(mapped)].notna().any().any()):
        raise UploadError(
            "the mapped columns contain no numeric values — check the column mapping."
        )


# --- summarize the scored batch ----------------------------------------------


@dataclass(frozen=True)
class BatchSummary:
    """The small aggregate the UI shows on top of the per-row probabilities."""

    n_rows: int
    n_signals_provided: int
    threshold: float
    n_high_risk: int
    pct_high_risk: float
    histogram: list[int]
    bin_edges: list[float]


def summarize(
    probabilities: list[float], *, n_signals_provided: int
) -> BatchSummary:
    """Aggregate per-row probabilities into the batch summary (counts + histogram).

    ``n_high_risk`` counts rows at/above :data:`HIGH_RISK_THRESHOLD`; the histogram is
    ``HISTOGRAM_BINS`` equal-width bins over [0, 1] (a probability of exactly 1.0 falls in
    the last bin). Presentational only — it never changes a served probability.
    """
    n = len(probabilities)
    n_high = sum(1 for p in probabilities if p >= HIGH_RISK_THRESHOLD)
    counts = [0] * HISTOGRAM_BINS
    for p in probabilities:
        idx = min(int(p * HISTOGRAM_BINS), HISTOGRAM_BINS - 1)
        counts[idx] += 1
    edges = [i / HISTOGRAM_BINS for i in range(HISTOGRAM_BINS + 1)]
    return BatchSummary(
        n_rows=n,
        n_signals_provided=n_signals_provided,
        threshold=HIGH_RISK_THRESHOLD,
        n_high_risk=n_high,
        pct_high_risk=(100.0 * n_high / n) if n else 0.0,
        histogram=counts,
        bin_edges=edges,
    )
