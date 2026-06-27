"""Temporal modelling — does the *trajectory* help? (F2.7)

F2.6 **measured** that tuning is exhausted (+0.003 ROC-AUC on the full 0.2.0 data):
the per-row GBDT family is at its ceiling. That ceiling is a **representation** limit,
not a capacity one — the failure signal is a progressive pre-failure degradation *ramp*
(the generator's ADR-020), and a model that sees **one row** discards the trajectory that
*is* the signal. So the honest lever is temporal structure, **measured to earn its place**
(ADR-007), not a bigger classifier.

A three-rung honesty ladder, all on the **same unit split / seed / metric / test rows**
as the F1 tabular comparison (apples-to-apples, the F2.5 ladder discipline):

* **(a) per-row LightGBM** — :func:`pdm_mlops.models.build_lightgbm` on the signals only.
  The F2 ceiling, the bar.
* **(b) temporal-features LightGBM** — per-unit, **causal** rolling/lag window stats
  (mean / std / slope / delta over the recent window) fed to the *same* LightGBM.
  Isolates "does temporal structure help **at all**" with a cheap, CPU-only model — and
  becomes the bar the deep rung must clear (so we never conflate "temporal helps" with
  "deep helps").
* **(c) a dilated *causal* TCN** (:class:`TCNClassifier`) over per-unit windows — must
  **earn its place** over (b), reported either way (exactly as the F2.5 autoencoder had
  to). Causal padding *structurally* forbids intra-window future leakage; era-NULL enters
  as **impute + a missingness-mask channel**; every test row is scored (short histories
  are **left-padded**, the mask covers the padding).

The windowing reuses the **exact F1 unit-grouped split** (:func:`split_indices`, the same
``GroupShuffleSplit`` seed → the same train/test *rows* as the tabular comparison), so all
three rungs are scored by the same ROC-AUC on the **identical** held-out rows. The TCN is a
parallel contender, not a hack into ``build_all`` — windowing needs ``unit_id`` + time,
which the signals-only tabular ``Dataset`` deliberately drops, so it gets its own data path
keyed off ``readings`` (ADR-007).

Determinism is a hard invariant: ``torch.use_deterministic_algorithms(True)`` + seeded
everything. The **tested** path is a tiny CPU model on the fixture (offline, CI-safe); the
**reported** number is the GPU run (RTX 4050 — ``[[resources_compute]]``). Reuses the
existing ``[deep]`` torch extra (no new dependency); falls back to CPU when CUDA is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

from . import config, features, models

#: The signal channels the sequence model sees — the same fixed, ordered feature columns
#: the tabular rungs see (no keys, no labels). Era-NULL is handled per-channel below.
SIGNAL_COLUMNS: tuple[str, ...] = features.FEATURE_COLUMNS

#: Per-unit time order — windows are built over each unit's series sorted by this.
TIME_COLUMN: str = "timestamp_h"
GROUP_COLUMN: str = features.GROUP_COLUMN

#: Default lookback window (rows) for both the temporal-feature rung and the TCN. ~2 h at
#: the canonical 5-min stride; long enough to expose a degradation ramp, short enough to
#: keep windowing cheap. Overridable so the tested path can shrink it on the tiny fixture.
DEFAULT_WINDOW: int = 24


# --------------------------------------------------------------------------- #
# The shared, F1-identical unit split (row indices, not a tabular Dataset)     #
# --------------------------------------------------------------------------- #


def split_indices(readings: pd.DataFrame, *, seed: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Positional train/test row indices from the **exact F1 unit-grouped split**.

    Mirrors :func:`features.prepare` bit-for-bit — same ``GroupShuffleSplit`` (same
    ``test_size`` / ``random_state``) over the same ``unit_id`` groups in the same row
    order — so the rows it returns are the **identical** train/test rows the tabular
    comparison uses. Returned as positional indices into ``readings`` (not a frozen
    ``Dataset``) because the sequence rungs need ``unit_id`` + time, which the tabular
    ``Dataset`` deliberately drops (ADR-007).
    """
    if seed is None:
        seed = config.DEFAULT_SEED
    for required in (config.TARGET, GROUP_COLUMN):
        if required not in readings.columns:
            raise KeyError(f"readings is missing the required column {required!r}.")
    groups = readings[GROUP_COLUMN]
    splitter = GroupShuffleSplit(
        n_splits=1, test_size=features.TEST_SIZE, random_state=seed
    )
    train_idx, test_idx = next(splitter.split(readings, readings[config.TARGET], groups))
    overlap = set(groups.iloc[train_idx].unique()) & set(groups.iloc[test_idx].unique())
    if overlap:
        raise AssertionError(f"unit(s) in both train and test: {sorted(overlap)}")
    return train_idx, test_idx


# --------------------------------------------------------------------------- #
# Rung (b) — causal temporal features for the same LightGBM                     #
# --------------------------------------------------------------------------- #


def temporal_features(
    readings: pd.DataFrame, *, window: int = DEFAULT_WINDOW
) -> pd.DataFrame:
    """Per-unit, **causal** rolling/lag window stats over the raw signals (rung b).

    For each signal, four backward-looking statistics over the trailing ``window`` of the
    unit's own series: rolling **mean**, **std**, **slope** (mean first-difference, a
    trend proxy) and **delta** (current − window-start, the net move). Every statistic
    looks at row ``t`` and earlier *within the same unit only* (``groupby`` resets the
    window at each unit boundary), so there is **no future leakage and no cross-unit
    bleed** — the same no-peeking property the TCN enforces structurally, here by
    construction.

    Returns a frame aligned to ``readings`` row order: the raw signals **plus** the
    engineered columns. Era-NULL is preserved (NaN flows through; LightGBM eats it). The
    output carries only signal-derived columns, so :func:`features.assert_no_leakage`
    still passes.
    """
    missing = [c for c in SIGNAL_COLUMNS if c not in readings.columns]
    if missing:
        raise KeyError(
            f"sequence: readings is missing signal columns {missing} "
            "(generator drift vs. the pinned version, ADR-001)."
        )
    for required in (GROUP_COLUMN, TIME_COLUMN):
        if required not in readings.columns:
            raise KeyError(f"temporal_features needs {required!r} to order each unit's series.")

    # Stable time order per unit; remember the original positions to restore alignment.
    order = readings.sort_values([GROUP_COLUMN, TIME_COLUMN], kind="stable").index
    df = readings.loc[order, [GROUP_COLUMN, *SIGNAL_COLUMNS]]
    g = df.groupby(GROUP_COLUMN, sort=False)

    out = {col: df[col] for col in SIGNAL_COLUMNS}  # raw signals, era-NULL preserved
    for col in SIGNAL_COLUMNS:
        roll = g[col].rolling(window, min_periods=2)
        # rolling() yields a (unit, row) multi-index; drop the unit level to realign.
        mean = roll.mean().reset_index(level=0, drop=True)
        std = roll.std().reset_index(level=0, drop=True)
        diff = g[col].diff()
        slope = (
            diff.groupby(df[GROUP_COLUMN], sort=False)
            .rolling(window, min_periods=1)
            .mean()
            .reset_index(level=0, drop=True)
        )
        start = g[col].shift(window - 1)
        out[f"{col}__mean"] = mean
        out[f"{col}__std"] = std
        out[f"{col}__slope"] = slope
        out[f"{col}__delta"] = df[col] - start

    feat = pd.DataFrame(out)
    feat = feat.loc[readings.index]  # restore the original readings row order
    features.assert_no_leakage(feat)
    return feat.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Windowing for the TCN — left-padded causal windows + a missingness mask       #
# --------------------------------------------------------------------------- #


@dataclass
class _Windows:
    """Pre-computed, memory-bounded causal windows over the time-sorted readings.

    ``values`` / ``present`` are the standardised signal matrix and its was-present mask,
    both in **time-sorted** row order (units contiguous). ``win_idx`` (N×W, sorted-space)
    gathers each row's trailing window (clamped to the unit's first row); ``win_valid``
    marks real vs. left-pad positions. ``inv`` maps an **original** readings position to
    its sorted-space position, so a caller scoring on original-order indices (the F1 split)
    lands on the right windows.
    """

    values: np.ndarray  # (N, C) float32, standardised, NaN→0
    present: np.ndarray  # (N, C) float32, 1.0 where the signal was present
    win_idx: np.ndarray  # (N, W) int32, sorted-space gather indices
    win_valid: np.ndarray  # (N, W) float32, 1.0 real / 0.0 left-pad
    inv: np.ndarray  # (N,) int64, original-position → sorted-space position
    window: int
    n_channels: int


def _unit_starts(units_sorted: np.ndarray) -> np.ndarray:
    """Sorted-space index of the first row of each row's unit (units are contiguous)."""
    boundary = np.empty(len(units_sorted), dtype=bool)
    boundary[0] = True
    boundary[1:] = units_sorted[1:] != units_sorted[:-1]
    start = np.where(boundary, np.arange(len(units_sorted)), 0)
    np.maximum.accumulate(start, out=start)
    return start


def build_windows(
    readings: pd.DataFrame,
    *,
    window: int = DEFAULT_WINDOW,
    train_idx: np.ndarray | None = None,
) -> _Windows:
    """Standardise signals (train-only stats) and pre-compute left-padded causal windows.

    Standardisation uses **train rows only** (no test leakage into the scaler); when
    ``train_idx`` is ``None`` all rows are used (offline convenience). Each signal is
    imputed to its train mean *and* paired with a was-present channel, so era-NULL stays
    learnable as missingness-as-signal (ADR-007) rather than being dropped.
    """
    missing = [c for c in SIGNAL_COLUMNS if c not in readings.columns]
    if missing:
        raise KeyError(
            f"sequence: readings is missing signal columns {missing} "
            "(generator drift vs. the pinned version, ADR-001)."
        )
    for required in (GROUP_COLUMN, TIME_COLUMN):
        if required not in readings.columns:
            raise KeyError(f"build_windows needs {required!r} to order each unit's series.")

    raw = readings.loc[:, list(SIGNAL_COLUMNS)].to_numpy(dtype="float64")  # original order
    present_orig = np.isfinite(raw).astype("float32")

    # Train-only standardisation stats (mean/std per signal), ignoring NaN.
    fit_rows = raw if train_idx is None else raw[train_idx]
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(fit_rows, axis=0)
        std = np.nanstd(fit_rows, axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where(np.isfinite(std) & (std > 0), std, 1.0)
    z = (np.nan_to_num(raw, nan=np.nan) - mean) / std
    z = np.nan_to_num(z, nan=0.0).astype("float32")  # era-NULL/imputed → 0 (mask flags it)

    # Time-sort (units contiguous); keep the permutation to realign to original order.
    so = readings.sort_values([GROUP_COLUMN, TIME_COLUMN], kind="stable").index
    so = readings.index.get_indexer(so)  # positional sort order over original rows
    inv = np.empty(len(so), dtype="int64")
    inv[so] = np.arange(len(so))

    values = z[so]
    present = present_orig[so]
    units_sorted = readings[GROUP_COLUMN].to_numpy()[so]
    starts = _unit_starts(units_sorted)

    n = len(so)
    s = np.arange(n)[:, None]
    k = np.arange(window)[None, :]
    rawpos = s - window + 1 + k  # (N, W) the would-be window positions
    us = starts[:, None]
    win_idx = np.clip(rawpos, us, s).astype("int32")  # clamp into the unit, ≤ current row
    win_valid = (rawpos >= us).astype("float32")  # 0 for left-pad before the unit's start

    return _Windows(
        values=values,
        present=present,
        win_idx=win_idx,
        win_valid=win_valid,
        inv=inv,
        window=window,
        n_channels=len(SIGNAL_COLUMNS),
    )


def _gather_batch(w: _Windows, sorted_positions: np.ndarray) -> np.ndarray:
    """Build the ``(B, 2C, W)`` window tensor for the given sorted-space row positions.

    Channels = the C standardised signals followed by their C was-present masks. Left-pad
    positions are zeroed in **both** the value and the mask channel, so padding is
    indistinguishable from "absent" and never injects a spurious value.
    """
    idx = w.win_idx[sorted_positions]  # (B, W)
    valid = w.win_valid[sorted_positions][:, None, :]  # (B, 1, W)
    val = w.values[idx]  # (B, W, C)
    msk = w.present[idx]  # (B, W, C)
    val = np.transpose(val, (0, 2, 1))  # (B, C, W)
    msk = np.transpose(msk, (0, 2, 1))  # (B, C, W)
    x = np.concatenate([val, msk], axis=1)  # (B, 2C, W)
    x *= valid  # zero out left-pad timesteps in every channel
    return np.ascontiguousarray(x, dtype="float32")


# --------------------------------------------------------------------------- #
# Rung (c) — the dilated causal TCN                                            #
# --------------------------------------------------------------------------- #


def _build_tcn_module(in_channels: int, channels: int, layers: int, kernel: int):
    """A small dilated **causal** 1-D conv stack + a per-window head (built lazily).

    Lazy so the module imports without torch (the ``[deep]`` extra stays optional). Each
    block left-pads by ``(kernel-1)*dilation`` and crops the right overhang, so the output
    at time *t* depends only on inputs at *t* and earlier — the no-peeking property is
    *structural*, not asserted. Dilation doubles per layer for a multi-scale receptive
    field (the cheapest way to see a slope across a long window). The last timestep's
    features (a learned temporal embedding) feed a linear head → one logit per window/row.
    """
    import torch
    from torch import nn

    class _Chomp(nn.Module):
        def __init__(self, chomp: int) -> None:
            super().__init__()
            self.chomp = chomp

        def forward(self, x):  # (B, C, T) → drop the right overhang from causal padding
            return x[:, :, : -self.chomp] if self.chomp > 0 else x

    class TCN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            blocks: list[nn.Module] = []
            c_in = in_channels
            for i in range(layers):
                dilation = 2**i
                pad = (kernel - 1) * dilation
                blocks += [
                    nn.Conv1d(c_in, channels, kernel, padding=pad, dilation=dilation),
                    _Chomp(pad),
                    nn.ReLU(),
                ]
                c_in = channels
            self.tcn = nn.Sequential(*blocks)
            self.head = nn.Linear(channels, 1)

        def forward(self, x):  # x: (B, 2C, W)
            h = self.tcn(x)  # (B, channels, W)
            last = h[:, :, -1]  # the window's final (current) timestep
            return self.head(last).squeeze(-1)  # (B,) logits

    return TCN()


@dataclass
class TCNClassifier:
    """Dilated causal TCN over per-unit windows, behind ``fit`` / ``predict_proba``.

    The reported deep rung (ADR-007). ``fit(readings, train_idx)`` builds windows for the
    train rows and trains; ``predict_proba(readings, idx)`` scores any rows (their windows
    reference only that unit's own earlier rows, so a test unit is self-contained). The
    default geometry is tiny (CPU/CI-safe); the reported GPU run scales ``channels`` /
    ``epochs`` up. Determinism: a fixed torch seed + ``use_deterministic_algorithms``.
    """

    name: str = "tcn"
    window: int = DEFAULT_WINDOW
    channels: int = 32
    layers: int = 4
    kernel: int = 3
    epochs: int = 8
    batch_size: int = 4096
    lr: float = 1e-3
    weight_decay: float = 1e-4
    seed: int = config.DEFAULT_SEED
    device: str | None = None

    _model: object = field(default=None, init=False, repr=False)
    _windows: _Windows | None = field(default=None, init=False, repr=False)
    _device: str = field(default="cpu", init=False, repr=False)

    @property
    def params(self) -> dict[str, object]:
        """Flat, MLflow-friendly description of the contender."""
        return {
            "model_type": "tcn",
            "window": self.window,
            "channels": self.channels,
            "layers": self.layers,
            "kernel": self.kernel,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "lr": self.lr,
            "weight_decay": self.weight_decay,
            "seed": self.seed,
            "device": self._device,
        }

    def _resolve_device(self) -> str:
        import torch

        if self.device is not None:
            return self.device
        return "cuda" if torch.cuda.is_available() else "cpu"

    def fit(self, readings: pd.DataFrame, train_idx: np.ndarray, y: np.ndarray) -> "TCNClassifier":
        import torch
        from torch import nn

        # Determinism: CUBLAS workspace must be pinned *before* the first CUDA matmul.
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        torch.manual_seed(self.seed)
        torch.use_deterministic_algorithms(True)

        self._device = self._resolve_device()
        self._windows = build_windows(readings, window=self.window, train_idx=train_idx)
        in_channels = 2 * self._windows.n_channels
        model = _build_tcn_module(in_channels, self.channels, self.layers, self.kernel)
        model = model.to(self._device)

        sorted_pos = self._windows.inv[train_idx]
        y = np.asarray(y, dtype="float32")
        # Class imbalance: weight the rare positive so the ramp isn't drowned by negatives.
        pos = float(y.sum())
        neg = float(len(y) - pos)
        pos_weight = torch.tensor([neg / pos if pos > 0 else 1.0], device=self._device)
        loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
        opt = torch.optim.Adam(
            model.parameters(), lr=self.lr, weight_decay=self.weight_decay
        )

        rng = np.random.default_rng(self.seed)
        n = len(sorted_pos)
        model.train()
        for _ in range(self.epochs):
            perm = rng.permutation(n)  # seeded shuffle → deterministic batch order
            for start in range(0, n, self.batch_size):
                batch = perm[start : start + self.batch_size]
                xb = _gather_batch(self._windows, sorted_pos[batch])
                xt = torch.from_numpy(xb).to(self._device)
                yt = torch.from_numpy(y[batch]).to(self._device)
                opt.zero_grad()
                logits = model(xt)
                loss = loss_fn(logits, yt)
                loss.backward()
                opt.step()
        model.eval()
        self._model = model
        return self

    def predict_proba(self, readings: pd.DataFrame, idx: np.ndarray) -> np.ndarray:
        """Positive-class probability for rows ``idx`` (positional into ``readings``)."""
        import torch

        if self._model is None or self._windows is None:
            raise RuntimeError("TCNClassifier.predict_proba before fit")
        sorted_pos = self._windows.inv[np.asarray(idx)]
        out = np.empty(len(sorted_pos), dtype="float32")
        with torch.no_grad():
            for start in range(0, len(sorted_pos), self.batch_size):
                sl = slice(start, start + self.batch_size)
                xb = _gather_batch(self._windows, sorted_pos[sl])
                xt = torch.from_numpy(xb).to(self._device)
                out[sl] = torch.sigmoid(self._model(xt)).cpu().numpy()
        return out


# --------------------------------------------------------------------------- #
# The three-rung comparison (logged to the same MLflow experiment)             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RungResult:
    """One rung's tracked outcome — name, ROC-AUC on the shared test rows, run id."""

    name: str
    metric: float
    run_id: str | None


@dataclass(frozen=True)
class SequenceComparison:
    """The three-way result: every rung, the winner, the TCN verdict, registry version."""

    results: list[RungResult]
    winner: RungResult
    tcn_earns_its_place: bool
    margin_over_temporal: float
    registered_version: str | None

    @property
    def metric_name(self) -> str:
        return config.PRIMARY_METRIC


def _score_tabular(
    model: models.Model,
    X: pd.DataFrame,
    y: np.ndarray,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
) -> tuple[models.Model, float]:
    """Fit a tabular contender on the train rows, ROC-AUC on the shared test rows."""
    model.fit(X.iloc[train_idx].reset_index(drop=True), pd.Series(y[train_idx]))
    proba = model.predict_proba(X.iloc[test_idx].reset_index(drop=True))
    return model, float(roc_auc_score(y[test_idx], proba))


def compare(
    readings: pd.DataFrame,
    *,
    seed: int | None = None,
    tracking_uri: str | None = None,
    register: bool = False,
    window: int = DEFAULT_WINDOW,
    tcn: TCNClassifier | None = None,
    log_mlflow: bool = True,
) -> SequenceComparison:
    """Run the three-rung ladder on the **same split / seed / metric / test rows**.

    Rungs: (a) per-row LightGBM, (b) temporal-features LightGBM, (c) the causal TCN. Each
    is scored by ROC-AUC on the **identical** F1 held-out rows (:func:`split_indices`) and,
    when ``log_mlflow``, logged as its own run in :data:`config.EXPERIMENT_NAME` so the
    deep rung competes head-to-head and the winner (tabular *or* temporal) can register.
    The TCN must measurably beat rung (b) to "earn its place"; the verdict is reported
    either way (the F2.5 autoencoder discipline).

    ``tcn`` injects a pre-sized model (the tested path passes a tiny CPU one); ``None``
    uses the default geometry. ``register`` registers the winner in the MLflow registry.
    """
    if seed is None:
        seed = config.DEFAULT_SEED

    train_idx, test_idx = split_indices(readings, seed=seed)
    y = readings[config.TARGET].astype("int8").to_numpy()
    if len(np.unique(y[test_idx])) < 2:
        from .train import DegenerateSplit

        raise DegenerateSplit(
            "the held-out test set has a single class; ROC-AUC is undefined "
            "(only an issue on the reduced smoke fixture; the full dataset is fine)."
        )

    X_perrow = features.select_features(readings)
    X_temporal = temporal_features(readings, window=window)
    if tcn is None:
        tcn = TCNClassifier(window=window, seed=seed)
    else:
        tcn.seed = seed

    if log_mlflow:
        import mlflow

        if tracking_uri is None:
            config.MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
            tracking_uri = config.sqlite_tracking_uri(config.MLFLOW_DB)
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(config.EXPERIMENT_NAME)

    results: list[RungResult] = []

    # --- rung (a) per-row LightGBM + (b) temporal-features LightGBM ---------------
    tabular = {
        "lightgbm_perrow": X_perrow,
        "lightgbm_temporal": X_temporal,
    }
    fitted: dict[str, tuple[models.Model, float]] = {}
    for rung, X in tabular.items():
        model = models.build_lightgbm(seed=seed)
        model, metric = _score_tabular(model, X, y, train_idx, test_idx)
        fitted[rung] = (model, metric)
        run_id = None
        if log_mlflow:
            import mlflow

            with mlflow.start_run(run_name=rung) as run:
                mlflow.log_params(model.params)
                mlflow.log_param("rung", rung)
                mlflow.log_param("seed", seed)
                mlflow.log_param("window", window)
                mlflow.log_param("n_features", X.shape[1])
                mlflow.log_metric(config.PRIMARY_METRIC, metric)
                mlflow.lightgbm.log_model(model.estimator, name="model")
                run_id = run.info.run_id
        results.append(RungResult(name=rung, metric=metric, run_id=run_id))

    # --- rung (c) the causal TCN -------------------------------------------------
    tcn.fit(readings, train_idx, y[train_idx])
    tcn_proba = tcn.predict_proba(readings, test_idx)
    tcn_metric = float(roc_auc_score(y[test_idx], tcn_proba))
    tcn_run_id = None
    if log_mlflow:
        import mlflow

        with mlflow.start_run(run_name=tcn.name) as run:
            mlflow.log_params(tcn.params)
            mlflow.log_param("rung", "tcn")
            mlflow.log_metric(config.PRIMARY_METRIC, tcn_metric)
            try:
                import mlflow.pytorch

                mlflow.pytorch.log_model(tcn._model, name="model")
            except Exception:  # torch/mlflow-pytorch flavour optional; metric still tracked
                pass
            tcn_run_id = run.info.run_id
    results.append(RungResult(name="tcn", metric=tcn_metric, run_id=tcn_run_id))

    temporal_metric = fitted["lightgbm_temporal"][1]
    margin = tcn_metric - temporal_metric
    tcn_earns = tcn_metric > temporal_metric

    winner = max(results, key=lambda r: r.metric)

    registered_version: str | None = None
    if register and log_mlflow and winner.run_id is not None:
        import mlflow

        result = mlflow.register_model(
            model_uri=f"runs:/{winner.run_id}/model",
            name=config.REGISTERED_MODEL_NAME,
        )
        registered_version = result.version

    return SequenceComparison(
        results=results,
        winner=winner,
        tcn_earns_its_place=tcn_earns,
        margin_over_temporal=margin,
        registered_version=registered_version,
    )


def format_comparison(cmp: SequenceComparison) -> str:
    """A compact, human-readable three-way report for the CLI."""
    lines = [f"Temporal ladder ({cmp.metric_name}), same split / test rows:"]
    for r in sorted(cmp.results, key=lambda r: r.metric, reverse=True):
        mark = "  <- winner" if r.name == cmp.winner.name else ""
        lines.append(f"  {r.name:<20} {r.metric:.4f}{mark}")
    verdict = "EARNS its place" if cmp.tcn_earns_its_place else "does NOT earn its place"
    lines.append(
        f"TCN vs. temporal-features LightGBM: {cmp.margin_over_temporal:+.4f} "
        f"→ the TCN {verdict}."
    )
    if cmp.registered_version is not None:
        lines.append(
            f"Registered '{config.REGISTERED_MODEL_NAME}' "
            f"v{cmp.registered_version} ({cmp.winner.name})."
        )
    return "\n".join(lines)
