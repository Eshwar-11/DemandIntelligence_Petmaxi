"""
PetMaxi Forecasting Engine — v7
==================================
Single unified engine, replacing the forecasting_engine_v3_1 /
forecasting_enhanced_v4 split. Consumes data_prep_v1.py's weekly clean
output (SKU, week, qtton) for the 1W/2W/4W/8W/13W horizons, AND (new
this revision) a per-SKU daily-granularity series for the 2D/3D
horizons Bruno asked about. These are two genuinely different data
resolutions, not one series sampled two ways, so they run through
separate grid searches with separate model registries below, but
share every other piece of infrastructure (metrics, rolling-origin
evaluation, champion selection, production forecasting).

Fixes carried over from the v4/run_batch_v4_1 baseline run on 2026-07-03:

  1. WAPE-STAMPING BUG: this engine grids over (horizon, model) and
     selects a genuinely independent champion PER HORIZON. No horizon's
     grade is ever borrowed from another horizon's evaluation.

  2. FORECASTABILITY / GRID-SEARCH GATE MISMATCH: the gate checks the
     actual minimum length needed for at least one working grid cell,
     anything below that is labelled INSUFFICIENT_HISTORY explicitly.

  3. ZERO-ACTUAL WAPE BLOWUP: WAPE has a defined zero-actual behavior
     instead of silently producing inf and dropping the grid row.

  4. Training uses MAX AVAILABLE HISTORY (expanding window) per SKU.

  5. (New this revision) ZERO-WAPE TRUST GATE: an exact-zero WAPE from
     a model that can only ever output a flat constant or slow trend
     (baselines, Croston family, ARIMA/SARIMA/ETS, SES, Ridge, Huber)
     is very likely a trivial "predicted 0, actual happened to be 0
     this window" coincidence on a mostly-dormant SKU, not a genuinely
     learned pattern. When that happens, champion selection re-searches
     among the non-linear pattern-matching models (tree ensembles,
     TwoStage architectures) for that horizon instead. This does not
     touch any WAPE above 0, only the exact-zero degenerate case.

  6. (New this revision) FORECAST-DATE / DATA-RECENCY GAP: forecast
     dates are anchored to the real batch-run date, not to the last
     date actually present in the data. If the data is stale (last
     actual sale weeks or months behind today), the model is asked for
     (gap + horizon) steps in one direct multi-step call and only the
     trailing `horizon` steps are kept and dated from today - this is
     forecast extrapolated across a data gap, not a validated
     horizon-ahead forecast, and callers get `data_gap_periods` back so
     that can be surfaced honestly rather than silently hidden.

Weekly horizons: [7, 14, 28, 56, 91] days (1W/2W/4W/8W/13W).
Daily horizons:  [2, 3] days, run against a separate daily series and
DAILY_MODEL_REGISTRY (3 of the 35 models swapped for 7-day-cycle
versions - SeasonalNaive/ETS/SARIMA assume a 52-WEEK annual cycle,
correct for weekly data, wrong for daily data).
"""

import json
import warnings
from itertools import product
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    from statsmodels.tools.sm_exceptions import ConvergenceWarning as SMConvergenceWarning
    warnings.filterwarnings("ignore", category=SMConvergenceWarning)
except ImportError:
    pass

try:
    from sklearn.exceptions import ConvergenceWarning as SKConvergenceWarning
    warnings.filterwarnings("ignore", category=SKConvergenceWarning)
except ImportError:
    pass

try:
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _STATSMODELS_OK = True
except ImportError:
    _STATSMODELS_OK = False
    print("WARN: statsmodels not found, ARIMA/SARIMA/ETS disabled.")

try:
    from sklearn.ensemble import (ExtraTreesRegressor, GradientBoostingRegressor,
                                   HistGradientBoostingRegressor, RandomForestRegressor)
    from sklearn.linear_model import HuberRegressor, Ridge, LogisticRegression
    from sklearn.preprocessing import StandardScaler
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False
    print("WARN: scikit-learn not found, ML regressors and TwoStage models disabled.")


# ═════════════════════════════════════════════════════════════════════════
# CONFIG
# ═════════════════════════════════════════════════════════════════════════

HORIZONS_D = [7, 14, 28, 56, 91]         # days. 1W/2W/4W/8W/13W.
DAILY_HORIZONS_D = [2, 3]                # days. 2D/3D, own series + registry.

N_ORIGINS_SIMPLE = 5
N_ORIGINS_ML     = 3    # ml_target_origins=1 was found unreliable earlier this project, use 3

# Minimum weeks (or, for the daily registry, minimum days after the x7
# scaling applied at call time) of history a model needs to even attempt
# fitting.
MODEL_MIN_LEN = {
    "arima": 26, "sarima": 52, "ets": 26,
    "ExtraTreesRegressor": 30, "GradientBoostingRegressor": 30,
    "HistGradientBoostingRegressor": 30, "HuberRegressor": 20, "Ridge": 20,
    "TwoStage_ET_ET": 30, "TwoStage_Logistic_Huber": 20, "TwoStage_Logistic_Ridge": 20,
    "TwoStage_RF_RF": 30, "TwoStage_RF_Ridge": 30,
}
DEFAULT_MIN_LEN = 8   # everything else (naive, moving average, Croston/TSB/SBA/ADIDA family, etc)

# Forecastability gate: the shortest horizon (7 days = 1 week) needs at
# least 1 train point + 1 test point + enough slack for N_ORIGINS_SIMPLE
# rolling origins to produce >=1 valid split. Weekly pipeline only - the
# daily (2D/3D) pipeline has its own, simpler per-model length check,
# matching the original diagnostic's behavior.
MIN_WEEKS_FOR_ANY_GRID_CELL = DEFAULT_MIN_LEN + 1   # = 9 weeks

# Models capable of genuinely non-linear temporal pattern-matching (tree
# ensembles + TwoStage architectures, which gate occurrence non-linearly
# even where the magnitude regressor itself is linear). Every other
# model in either registry - baselines, Croston family, ARIMA/SARIMA/
# ETS, SES, Ridge, Huber - can only ever output a flat constant or slow
# trend per horizon. See fix #5 above for why this matters.
NONLINEAR_TRUSTED_MODELS = {
    "ExtraTreesRegressor", "GradientBoostingRegressor",
    "HistGradientBoostingRegressor", "RandomForestRegressor",
    "TwoStage_ET_ET", "TwoStage_RF_RF", "TwoStage_RF_Ridge",
    "TwoStage_Logistic_Huber", "TwoStage_Logistic_Ridge",
}
ZERO_WAPE_EPS = 1e-6


# ═════════════════════════════════════════════════════════════════════════
# METRICS
# ═════════════════════════════════════════════════════════════════════════

def calc_wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """
    WAPE = sum(|actual - predicted|) / sum(actual).

    Zero-actual behavior: if the test window's actual total is zero AND
    the forecast is also ~zero, that's a correct call, WAPE = 0, not
    inf. If actual is zero but the forecast is materially nonzero,
    that's a real miss, scored as 100% (capped, since percentage error
    is undefined against a zero base but the miss is real and should
    count against the model).
    """
    total_actual = np.sum(np.abs(actual))
    if total_actual == 0:
        total_pred = np.sum(np.abs(predicted))
        return 0.0 if total_pred < 1e-6 else 100.0
    return float(np.sum(np.abs(actual - predicted)) / total_actual * 100)


def calc_robust_mape(actual: np.ndarray, predicted: np.ndarray, eps: float = 1e-3) -> float:
    denom = (np.abs(actual) + np.abs(predicted)) / 2 + eps
    return float(np.mean(np.abs(actual - predicted) / denom) * 100)


def calc_mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted)))


def all_metrics(actual: np.ndarray, predicted: np.ndarray) -> Dict[str, float]:
    predicted = np.maximum(predicted, 0)
    return {
        "wape":           calc_wape(actual, predicted),
        "robust_mape":    calc_robust_mape(actual, predicted),
        "mae":            calc_mae(actual, predicted),
        "actual_total":   float(np.sum(actual)),
        "forecast_total": float(np.sum(predicted)),
    }


# ═════════════════════════════════════════════════════════════════════════
# LAG FEATURES (for the ML regressors and TwoStage magnitude models)
# ═════════════════════════════════════════════════════════════════════════

def _build_lag_features(series: np.ndarray, lags=(1, 2, 4, 8, 12)) -> pd.DataFrame:
    """
    Simple lag + rolling-stat feature set. Row i's features are built
    only from series[:i], so this is safe to use inside a rolling-origin
    split without leaking future information.
    """
    s = pd.Series(series)
    feats = pd.DataFrame(index=s.index)
    for lag in lags:
        feats[f"lag_{lag}"] = s.shift(lag)
    feats["roll_mean_4"]  = s.shift(1).rolling(4).mean()
    feats["roll_mean_8"]  = s.shift(1).rolling(8).mean()
    feats["roll_std_4"]   = s.shift(1).rolling(4).std()
    feats["nonzero_rate_8"] = s.shift(1).rolling(8).apply(lambda x: (x > 0).mean(), raw=True)
    feats["t"] = np.arange(len(s))
    return feats


def _fit_predict_regressor(model_cls, train: np.ndarray, h: int, **kwargs) -> np.ndarray:
    """
    Fits a sklearn regressor on lag features built from train, then
    forecasts h steps ahead recursively (each step's prediction feeds
    the next step's lag features).

    Features are standardized before fitting. Lag values and the raw
    time index ('t') sit on very different scales, and gradient-based
    solvers (HuberRegressor, Ridge) are sensitive to that, unscaled
    features were causing genuine convergence failures, not just
    warning noise, an unconverged fit can also just be a worse fit.
    Tree-based models (RandomForest, ExtraTrees, GradientBoosting) are
    scale-invariant so this is harmless for them, applied universally
    rather than conditionally to keep this one code path simple.
    """
    feats = _build_lag_features(train)
    y = pd.Series(train)
    valid = feats.dropna().index
    if len(valid) < 8:
        return np.full(h, train[-1] if len(train) else 0.0)

    X_train_raw = feats.loc[valid].values
    y_train = y.loc[valid].values

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train_raw)

    model = model_cls(**kwargs)
    model.fit(X_train, y_train)

    history = list(train.astype(float))
    preds = []
    for _ in range(h):
        feat_row = _build_lag_features(np.array(history)).iloc[[-1]].values
        if np.isnan(feat_row).any():
            pred = history[-1]
        else:
            pred = float(model.predict(scaler.transform(feat_row))[0])
        pred = max(pred, 0.0)
        preds.append(pred)
        history.append(pred)
    return np.array(preds)


# ═════════════════════════════════════════════════════════════════════════
# MODELS — baselines
# ═════════════════════════════════════════════════════════════════════════

def m_naive_last_value(train, h):
    return np.full(h, train[-1] if len(train) else 0.0)

def m_last_non_zero(train, h):
    nz = train[train > 0]
    val = nz[-1] if len(nz) else 0.0
    return np.full(h, val)

def m_zero_forecast(train, h):
    return np.zeros(h)

def m_moving_average(train, h, window):
    w = min(window, len(train))
    return np.full(h, np.mean(train[-w:]) if w else 0.0)

def m_recent_weighted_average(train, h, window=4):
    w = min(window, len(train))
    if w == 0:
        return np.zeros(h)
    weights = np.arange(1, w + 1, dtype=float)
    weights /= weights.sum()
    return np.full(h, np.dot(weights, train[-w:]))

def m_rolling_median(train, h, window=8):
    w = min(window, len(train))
    return np.full(h, np.median(train[-w:]) if w else 0.0)

def m_seasonal_naive_52(train, h):
    if len(train) < 52:
        return m_moving_average(train, h, 4)
    preds = [max(train[-(52 - (i % 52))], 0) for i in range(h)]
    return np.array(preds)

def m_ses(train, h, alpha=0.3):
    if not len(train):
        return np.zeros(h)
    s = train[0]
    for v in train[1:]:
        s = alpha * v + (1 - alpha) * s
    return np.full(h, s)


# ═════════════════════════════════════════════════════════════════════════
# MODELS — daily-appropriate versions (7-day cycle, not 52-week)
# ═════════════════════════════════════════════════════════════════════════
# Only these three are period-dependent; everything else (baselines,
# Croston family, ML regressors, TwoStage) is reused as-is in the daily
# registry below, since those aren't period-dependent.

def m_seasonal_naive_7(train, h):
    if len(train) < 7:
        return m_moving_average(train, h, 4)
    return np.array([max(train[-(7 - (i % 7))], 0) for i in range(h)])


def m_ets_daily(train, h):
    if not _STATSMODELS_OK or len(train) < 21:
        return m_ses(train, h)
    try:
        seasonal = "add" if len(train) >= 14 else None
        fit = ExponentialSmoothing(
            train, trend="add", seasonal=seasonal,
            seasonal_periods=7 if seasonal else None,
            initialization_method="heuristic",
        ).fit(optimized=True, disp=False)
        return np.maximum(fit.forecast(h), 0)
    except Exception:
        return m_ses(train, h)


def m_sarima_daily(train, h):
    if not _STATSMODELS_OK or len(train) < 21:
        return m_ses(train, h)
    try:
        fit = SARIMAX(train, order=(1, 1, 0), seasonal_order=(1, 0, 0, 7),
                       enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        return np.maximum(fit.forecast(h), 0)
    except Exception:
        return m_ses(train, h)


# ═════════════════════════════════════════════════════════════════════════
# MODELS — intermittent demand family (Croston / SBA / TSB / ADIDA)
# ═════════════════════════════════════════════════════════════════════════

def _croston_core(train, h, alpha=0.1, variant="croston", bias_correction=True):
    """
    Shared Croston-family engine. Tracks demand size (z) and inter-
    demand interval (p) separately, updated only at nonzero periods.

      croston : classic Croston, forecast = z / p
      sba     : Syntetos-Boylan Approximation, forecast = (1 - alpha/2) * z / p
      tsb     : Teunter-Syntetos-Babai, updates a demand PROBABILITY
                each period (not just at nonzero events), forecast =
                prob * z
    """
    train = np.asarray(train, dtype=float)
    if len(train) == 0 or (train == 0).all():
        return np.zeros(h)

    if variant == "tsb":
        first_nz = np.argmax(train > 0)
        z = train[first_nz]
        prob = 0.5
        for t in range(first_nz + 1, len(train)):
            if train[t] > 0:
                prob = prob + alpha * (1 - prob)
                z = z + alpha * (train[t] - z)
            else:
                prob = prob + alpha * (0 - prob)
        forecast_val = max(prob * z, 0.0)
        return np.full(h, forecast_val)

    nz_idx = np.where(train > 0)[0]
    if len(nz_idx) == 0:
        return np.zeros(h)

    z = train[nz_idx[0]]
    p = nz_idx[0] + 1 if len(nz_idx) > 1 else max(nz_idx[0] + 1, 1)
    q = 1
    for i in range(nz_idx[0] + 1, len(train)):
        q += 1
        if train[i] > 0:
            z = z + alpha * (train[i] - z)
            p = p + alpha * (q - p)
            q = 1

    if p <= 0:
        p = 1.0
    forecast_val = z / p
    if variant == "sba" and bias_correction:
        forecast_val *= (1 - alpha / 2)
    return np.full(h, max(forecast_val, 0.0))


def m_croston(train, h, alpha=0.1):
    return _croston_core(train, h, alpha=alpha, variant="croston")

def m_croston_style(train, h, alpha=0.1):
    return _croston_core(train, h, alpha=max(alpha, 0.15), variant="croston")

def m_sba(train, h, alpha=0.1):
    return _croston_core(train, h, alpha=alpha, variant="sba")

def m_tsb(train, h, alpha=0.1, beta=0.1):
    return _croston_core(train, h, alpha=alpha, variant="tsb")

def m_tsb_style(train, h, alpha=0.1):
    return _croston_core(train, h, alpha=min(alpha * 1.5, 0.5), variant="tsb")

def m_adida(train, h, agg=4):
    """
    Aggregate-Disaggregate Intermittent Demand Approach: aggregate the
    series into blocks of `agg` weeks, forecast on the aggregated
    (less intermittent) series with SES, then disaggregate back down
    evenly across the forecast horizon.
    """
    train = np.asarray(train, dtype=float)
    if len(train) < agg:
        return m_moving_average(train, h, 4)
    n_blocks = len(train) // agg
    agg_series = train[-n_blocks * agg:].reshape(n_blocks, agg).sum(axis=1)
    agg_forecast = m_ses(agg_series, 1, alpha=0.2)[0]
    per_period = max(agg_forecast / agg, 0.0)
    return np.full(h, per_period)

def m_adida_style(train, h, agg=4):
    train = np.asarray(train, dtype=float)
    if len(train) < agg * 2:
        return m_adida(train, h, agg)
    recent = train[-agg * 4:] if len(train) >= agg * 4 else train
    n_blocks = len(recent) // agg
    agg_series = recent[-n_blocks * agg:].reshape(n_blocks, agg).sum(axis=1)
    agg_forecast = m_ses(agg_series, 1, alpha=0.3)[0]
    return np.full(h, max(agg_forecast / agg, 0.0))


# ═════════════════════════════════════════════════════════════════════════
# MODELS — statsmodels (ARIMA / SARIMA / ETS) — weekly (52-week cycle)
# ═════════════════════════════════════════════════════════════════════════

def m_arima(train, h):
    if not _STATSMODELS_OK or len(train) < MODEL_MIN_LEN["arima"]:
        return m_ses(train, h)
    try:
        fit = SARIMAX(train, order=(1, 1, 1),
                       enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        return np.maximum(fit.forecast(h), 0)
    except Exception:
        return m_ses(train, h)

def m_sarima(train, h):
    if not _STATSMODELS_OK or len(train) < MODEL_MIN_LEN["sarima"]:
        return m_ses(train, h)
    try:
        fit = SARIMAX(train, order=(1, 1, 0), seasonal_order=(1, 0, 0, 52),
                       enforce_stationarity=False, enforce_invertibility=False).fit(disp=False)
        return np.maximum(fit.forecast(h), 0)
    except Exception:
        return m_ses(train, h)

def m_ets(train, h):
    if not _STATSMODELS_OK or len(train) < MODEL_MIN_LEN["ets"]:
        return m_ses(train, h)
    try:
        seasonal = "add" if len(train) >= 52 else None
        fit = ExponentialSmoothing(
            train, trend="add", seasonal=seasonal,
            seasonal_periods=52 if seasonal else None,
            initialization_method="heuristic",
        ).fit(optimized=True, disp=False)
        return np.maximum(fit.forecast(h), 0)
    except Exception:
        return m_ses(train, h)


# ═════════════════════════════════════════════════════════════════════════
# MODELS — ML regressors (lag features)
# ═════════════════════════════════════════════════════════════════════════

def m_extra_trees(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(ExtraTreesRegressor, train, h, n_estimators=100, random_state=0, n_jobs=1)

def m_gradient_boosting(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(GradientBoostingRegressor, train, h, n_estimators=100, random_state=0)

def m_hist_gradient_boosting(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(HistGradientBoostingRegressor, train, h, random_state=0)

def m_huber(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(HuberRegressor, train, h)

def m_random_forest(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(RandomForestRegressor, train, h, n_estimators=100, random_state=0, n_jobs=1)

def m_ridge(train, h):
    if not _SKLEARN_OK:
        return m_ses(train, h)
    return _fit_predict_regressor(Ridge, train, h)


# ═════════════════════════════════════════════════════════════════════════
# MODELS — TwoStage (occurrence classifier + magnitude regressor)
# ═════════════════════════════════════════════════════════════════════════

def _two_stage(train, h, magnitude_cls, magnitude_kwargs=None):
    """
    Stage 1: logistic regression predicts P(demand > 0) next period.
    Stage 2: a regressor, fit only on the nonzero-demand periods,
             predicts the magnitude given demand occurs.
    Forecast = P(occurrence) * E[magnitude].
    """
    magnitude_kwargs = magnitude_kwargs or {}
    train = np.asarray(train, dtype=float)
    if not _SKLEARN_OK or len(train) < 20:
        return m_ses(train, h)

    feats = _build_lag_features(train)
    occurrence = (train > 0).astype(int)
    valid = feats.dropna().index
    if len(valid) < 10 or occurrence[valid].sum() < 3:
        return m_ses(train, h)

    X_raw = feats.loc[valid].values
    y_occ = occurrence[valid]
    clf_scaler = StandardScaler()
    X = clf_scaler.fit_transform(X_raw)
    clf = LogisticRegression(max_iter=200)
    try:
        clf.fit(X, y_occ)
    except Exception:
        return m_ses(train, h)

    nz_valid = valid[occurrence[valid] == 1]
    if len(nz_valid) < 5:
        return m_ses(train, h)
    X_mag_raw = feats.loc[nz_valid].values
    y_mag = train[nz_valid]
    mag_scaler = StandardScaler()
    X_mag = mag_scaler.fit_transform(X_mag_raw)
    reg = magnitude_cls(**magnitude_kwargs)
    try:
        reg.fit(X_mag, y_mag)
    except Exception:
        return m_ses(train, h)

    history = list(train)
    preds = []
    for _ in range(h):
        feat_row = _build_lag_features(np.array(history)).iloc[[-1]].values
        if np.isnan(feat_row).any():
            preds.append(history[-1])
            history.append(history[-1])
            continue
        try:
            p_occ = clf.predict_proba(clf_scaler.transform(feat_row))[0, 1]
            mag = max(float(reg.predict(mag_scaler.transform(feat_row))[0]), 0.0)
        except Exception:
            p_occ, mag = 0.5, history[-1]
        pred = max(p_occ * mag, 0.0)
        preds.append(pred)
        history.append(pred)
    return np.array(preds)


def m_two_stage_et_et(train, h):
    return _two_stage(train, h, ExtraTreesRegressor, {"n_estimators": 100, "random_state": 0, "n_jobs": 1}) if _SKLEARN_OK else m_ses(train, h)

def m_two_stage_rf_rf(train, h):
    return _two_stage(train, h, RandomForestRegressor, {"n_estimators": 100, "random_state": 0, "n_jobs": 1}) if _SKLEARN_OK else m_ses(train, h)

def m_two_stage_rf_ridge(train, h):
    return _two_stage(train, h, Ridge, {}) if _SKLEARN_OK else m_ses(train, h)

def m_two_stage_logistic_huber(train, h):
    return _two_stage(train, h, HuberRegressor, {}) if _SKLEARN_OK else m_ses(train, h)

def m_two_stage_logistic_ridge(train, h):
    return _two_stage(train, h, Ridge, {}) if _SKLEARN_OK else m_ses(train, h)


# ═════════════════════════════════════════════════════════════════════════
# MODEL REGISTRIES — weekly (default) and daily (2D/3D)
# ═════════════════════════════════════════════════════════════════════════

MODEL_REGISTRY: Dict[str, Any] = {
    "NaiveLastValue":            lambda tr, h: m_naive_last_value(tr, h),
    "LastNonZero":                lambda tr, h: m_last_non_zero(tr, h),
    "ZeroForecast":                lambda tr, h: m_zero_forecast(tr, h),
    "MovingAverage_4":            lambda tr, h: m_moving_average(tr, h, 4),
    "MovingAverage_8":            lambda tr, h: m_moving_average(tr, h, 8),
    "RecentWeightedAverage":      lambda tr, h: m_recent_weighted_average(tr, h, 4),
    "RollingMedian_8":            lambda tr, h: m_rolling_median(tr, h, 8),
    "SeasonalNaive_52":          lambda tr, h: m_seasonal_naive_52(tr, h),
    "SES":                        lambda tr, h: m_ses(tr, h, 0.3),

    "CrostonStyle":                lambda tr, h: m_croston_style(tr, h, 0.1),
    "CrostonStyle_alpha_02":      lambda tr, h: m_croston_style(tr, h, 0.2),
    "Croston_alpha_02":            lambda tr, h: m_croston(tr, h, 0.2),
    "SBA_alpha_01":                lambda tr, h: m_sba(tr, h, 0.1),
    "SBA_alpha_02":                lambda tr, h: m_sba(tr, h, 0.2),
    "TSBStyle":                    lambda tr, h: m_tsb_style(tr, h, 0.1),
    "TSBStyle_01_01":            lambda tr, h: m_tsb_style(tr, h, 0.1),
    "TSB_01_01":                    lambda tr, h: m_tsb(tr, h, 0.1, 0.1),
    "TSB_02_02":                    lambda tr, h: m_tsb(tr, h, 0.2, 0.2),
    "ADIDA_4":                    lambda tr, h: m_adida(tr, h, 4),
    "ADIDA_8":                    lambda tr, h: m_adida(tr, h, 8),
    "ADIDAStyle_4":                lambda tr, h: m_adida_style(tr, h, 4),

    "ETS":                          lambda tr, h: m_ets(tr, h),
    "arima":                        lambda tr, h: m_arima(tr, h),
    "sarima":                        lambda tr, h: m_sarima(tr, h),

    "ExtraTreesRegressor":        lambda tr, h: m_extra_trees(tr, h),
    "GradientBoostingRegressor":  lambda tr, h: m_gradient_boosting(tr, h),
    "HistGradientBoostingRegressor": lambda tr, h: m_hist_gradient_boosting(tr, h),
    "HuberRegressor":              lambda tr, h: m_huber(tr, h),
    "RandomForestRegressor":      lambda tr, h: m_random_forest(tr, h),
    "Ridge":                        lambda tr, h: m_ridge(tr, h),

    "TwoStage_ET_ET":              lambda tr, h: m_two_stage_et_et(tr, h),
    "TwoStage_Logistic_Huber":    lambda tr, h: m_two_stage_logistic_huber(tr, h),
    "TwoStage_Logistic_Ridge":    lambda tr, h: m_two_stage_logistic_ridge(tr, h),
    "TwoStage_RF_RF":              lambda tr, h: m_two_stage_rf_rf(tr, h),
    "TwoStage_RF_Ridge":          lambda tr, h: m_two_stage_rf_ridge(tr, h),
}

# Daily registry: same 35 models, 3 swapped for 7-day-cycle versions
# (SeasonalNaive_52 -> 7, ETS -> 7-day seasonal, sarima -> 7-day
# seasonal_order). Everything else reused unmodified since it isn't
# period-dependent.
DAILY_MODEL_REGISTRY: Dict[str, Any] = dict(MODEL_REGISTRY)
DAILY_MODEL_REGISTRY["SeasonalNaive_52"] = lambda tr, h: m_seasonal_naive_7(tr, h)
DAILY_MODEL_REGISTRY["ETS"] = lambda tr, h: m_ets_daily(tr, h)
DAILY_MODEL_REGISTRY["sarima"] = lambda tr, h: m_sarima_daily(tr, h)
assert len(DAILY_MODEL_REGISTRY) == len(MODEL_REGISTRY), "daily registry size drifted from weekly, expected equal (35)"


# ═════════════════════════════════════════════════════════════════════════
# FORECASTABILITY GATE (weekly pipeline only)
# ═════════════════════════════════════════════════════════════════════════

def assess_forecastability(series: np.ndarray) -> Dict[str, Any]:
    """
    A SKU is INSUFFICIENT_HISTORY if it doesn't have enough weeks for
    even one valid grid cell to be evaluated, regardless of what a
    composite score would otherwise say. Otherwise it's forecastable,
    with a separate demand-pattern read for reporting.
    """
    n_weeks  = len(series)
    n_active = int((series > 0).sum())
    zero_pct = float((series == 0).mean()) if n_weeks else 1.0

    if n_weeks < MIN_WEEKS_FOR_ANY_GRID_CELL:
        return {"is_forecastable": False, "status": "INSUFFICIENT_HISTORY",
                "n_weeks": n_weeks, "n_active": n_active, "zero_pct": zero_pct}

    mean_ = float(series.mean()) if n_weeks else 0.0
    std_  = float(series.std()) if n_weeks else 0.0
    cv2   = (std_ / mean_) ** 2 if mean_ > 0 else np.nan

    nz_idx = np.where(series > 0)[0]
    adi = float(np.mean(np.diff(nz_idx))) if len(nz_idx) > 1 else float(n_weeks)

    if zero_pct > 0.7:
        pattern = "Intermittent"
    elif not np.isnan(cv2) and cv2 > 1:
        pattern = "Erratic" if zero_pct <= 0.3 else "Lumpy"
    else:
        pattern = "Smooth"

    return {"is_forecastable": True, "status": "FORECASTABLE",
            "n_weeks": n_weeks, "n_active": n_active, "zero_pct": zero_pct,
            "adi": adi, "cv2": cv2, "demand_pattern": pattern}


# ═════════════════════════════════════════════════════════════════════════
# ROLLING-ORIGIN EVALUATION (expanding window, max available history)
# ═════════════════════════════════════════════════════════════════════════

def _rolling_origin_eval(series: np.ndarray, horizon_w: int, model_fn,
                          n_origins: int, min_len_floor: int = None) -> Tuple[np.ndarray, np.ndarray]:
    """
    Expanding-window rolling origin: training data is everything
    available up to each origin (no fixed train-window length), test
    window is the next horizon_w periods after that origin.

    Origin selection walks backward from the end of the series and
    SKIPS any candidate whose test window is entirely zero, applied
    uniformly to every SKU (and, since this revision, uniformly across
    both weekly and daily granularity - min_len_floor lets the daily
    caller pass its own (7x scaled) floor instead of the weekly
    DEFAULT_MIN_LEN).
    """
    floor = DEFAULT_MIN_LEN if min_len_floor is None else min_len_floor
    all_actual, all_pred = [], []
    min_len_needed = horizon_w + floor

    if len(series) < min_len_needed:
        return np.array([]), np.array([])

    latest_possible_end = len(series) - horizon_w
    earliest_possible_end = floor

    collected = 0
    end_train = latest_possible_end
    checked_any_signal = False

    while end_train >= earliest_possible_end and collected < n_origins:
        test = series[end_train:end_train + horizon_w]
        if len(test) < horizon_w:
            end_train -= 1
            continue

        if np.sum(test) > 0:
            checked_any_signal = True
            train = series[:end_train]
            pred = model_fn(train, horizon_w)
            all_actual.append(test)
            all_pred.append(pred)
            collected += 1

        end_train -= 1

    if not all_actual and not checked_any_signal:
        end_train = latest_possible_end
        test = series[end_train:end_train + horizon_w]
        if len(test) == horizon_w:
            train = series[:end_train]
            pred = model_fn(train, horizon_w)
            all_actual.append(test)
            all_pred.append(pred)

    if not all_actual:
        return np.array([]), np.array([])
    return np.concatenate(all_actual), np.concatenate(all_pred)


def grid_search_sku(series: np.ndarray,
                     horizons_days: List[int] = None,
                     models: Dict = None,
                     period_unit: str = "W",
                     min_len_scale: int = 1) -> pd.DataFrame:
    """
    Full grid over (horizon, model) for one SKU, expanding-window
    training, genuine per-horizon evaluation. Returns the full grid
    (all combos), one row per (horizon, model). Caller picks the
    per-horizon champion by filtering on horizon and taking min WAPE.

    period_unit="W" (default): horizons_days are converted to weeks
    (existing weekly behavior, unchanged). period_unit="D": horizons_days
    are used directly as the native period (for the 2D/3D daily
    pipeline), and min_len_scale lets the caller scale MODEL_MIN_LEN /
    DEFAULT_MIN_LEN from weeks to days (x7) so the same length guards
    apply consistently at the finer resolution.
    """
    horizons_days = horizons_days or HORIZONS_D
    models = models or MODEL_REGISTRY
    rows = []

    for h_days, (mname, mfn) in product(horizons_days, models.items()):
        if period_unit == "W":
            h_period = max(1, round(h_days / 7))
        else:
            h_period = h_days

        min_len = MODEL_MIN_LEN.get(mname, DEFAULT_MIN_LEN) * min_len_scale
        floor = DEFAULT_MIN_LEN * min_len_scale
        if len(series) < min_len + h_period:
            continue

        n_orig = N_ORIGINS_ML if mname in ("arima", "sarima", "ETS") or \
                 mname.startswith(("ExtraTrees", "GradientBoosting", "HistGradientBoosting",
                                    "HuberRegressor", "RandomForest", "Ridge", "TwoStage")) \
                 else N_ORIGINS_SIMPLE

        actual, pred = _rolling_origin_eval(series, h_period, mfn, n_orig, min_len_floor=floor)
        if len(actual) == 0:
            continue

        m = all_metrics(actual, pred)
        rows.append({"horizon_days": h_days,
                      "horizon_weeks": h_period if period_unit == "W" else None,
                      "model": mname, **m})

    return pd.DataFrame(rows)


def select_per_horizon_champions(grid_df: pd.DataFrame) -> Dict[int, Dict]:
    """
    For each horizon present in the grid, pick the lowest-WAPE row
    independently - each horizon's champion (and its WAPE) comes only
    from that horizon's own rows, never borrowed from another horizon.

    Zero-WAPE trust gate (new this revision): if the lowest-WAPE row is
    an exact zero AND came from a model outside NONLINEAR_TRUSTED_MODELS,
    that's treated as an untrustworthy degenerate fit (see module
    docstring fix #5), and the search is redone restricted to the
    trusted non-linear models for that horizon. If no trusted model has
    any valid row at that horizon, the original zero-WAPE champion is
    kept but flagged with zero_wape_no_alternative=True so it stays
    visible rather than silently trusted or silently dropped.
    """
    champions = {}
    if grid_df.empty:
        return champions

    for h_days, grp in grid_df.groupby("horizon_days"):
        grp_sorted = grp.sort_values("wape")
        best = grp_sorted.iloc[0].to_dict()

        if best["wape"] <= ZERO_WAPE_EPS and best["model"] not in NONLINEAR_TRUSTED_MODELS:
            trusted = grp_sorted[grp_sorted["model"].isin(NONLINEAR_TRUSTED_MODELS)]
            if not trusted.empty:
                override = trusted.iloc[0].to_dict()
                override["zero_wape_override"] = True
                override["untrusted_zero_model"] = best["model"]
                override["zero_wape_no_alternative"] = False
                champions[int(h_days)] = override
                continue
            else:
                best["zero_wape_override"] = False
                best["untrusted_zero_model"] = None
                best["zero_wape_no_alternative"] = True
                champions[int(h_days)] = best
                continue

        best["zero_wape_override"] = False
        best["untrusted_zero_model"] = None
        best["zero_wape_no_alternative"] = False
        champions[int(h_days)] = best

    return champions


# ═════════════════════════════════════════════════════════════════════════
# GRADING
# ═════════════════════════════════════════════════════════════════════════

def classify_sku(status: str, wape: Optional[float]) -> str:
    """
    Grade bands, with INSUFFICIENT_HISTORY as its own explicit state
    (never silently folded into Unreliable).
    """
    if status == "INSUFFICIENT_HISTORY":
        return "Insufficient History"
    if wape is None or np.isnan(wape):
        return "No Model"
    if wape < 20:
        return "Excellent"
    if wape < 35:
        return "Good"
    if wape < 55:
        return "Fair"
    if wape < 75:
        return "Poor"
    return "Unreliable"


# ═════════════════════════════════════════════════════════════════════════
# PRODUCTION FORECAST (actual forward-looking values)
# ═════════════════════════════════════════════════════════════════════════

def forecast_sku_production(series: np.ndarray, model_name: str, horizon_period: int,
                             models_registry: Dict = None,
                             start_date: Optional[pd.Timestamp] = None,
                             gap_periods: int = 0,
                             period_unit: str = "W") -> Dict[str, Any]:
    """
    Generates the actual forward forecast using the full available
    history (expanding window, same as evaluation) and the champion
    model for that horizon.

    gap_periods (new this revision): the number of periods between the
    last real actual data point and the real batch-run date. If > 0,
    the model is asked for (gap_periods + horizon_period) steps in ONE
    direct multi-step call (not a recursive re-feed), and only the
    trailing horizon_period values are kept - the bridged portion is
    extrapolation across a data gap, not a validated horizon-ahead
    forecast, which is why it's returned as data_gap_periods for the
    caller to surface honestly.
    """
    models_registry = models_registry or MODEL_REGISTRY
    model_fn = models_registry.get(model_name)
    if model_fn is None:
        return {"success": False}

    total_steps = gap_periods + horizon_period
    try:
        values = model_fn(series, total_steps)
        values = np.maximum(values, 0).tolist()[gap_periods:]
    except Exception as e:
        return {"success": False, "error": str(e)}

    if start_date is None:
        start_date = pd.Timestamp.today().normalize() + pd.offsets.Week(weekday=0)
    step = pd.Timedelta(weeks=1) if period_unit == "W" else pd.Timedelta(days=1)
    dates = [(start_date + step * i).strftime("%Y-%m-%d") for i in range(horizon_period)]

    return {"success": True, "values": values, "dates": dates,
            "total": float(sum(values)), "data_gap_periods": gap_periods}


# ═════════════════════════════════════════════════════════════════════════
# PER-SKU ORCHESTRATION
# ═════════════════════════════════════════════════════════════════════════

def process_sku(sku: str, series: np.ndarray,
                 start_date: Optional[pd.Timestamp] = None,
                 gap_weeks: int = 0,
                 daily_series: Optional[np.ndarray] = None,
                 daily_start_date: Optional[pd.Timestamp] = None,
                 gap_days: int = 0) -> Dict[str, Any]:
    """
    Full pipeline for one SKU. Weekly: gate -> grid search -> per-
    horizon champions -> production forecasts -> grades, for
    HORIZONS_D (1W/2W/4W/8W/13W). Daily (new this revision, only runs
    if daily_series is provided): the same grid-search/champion/
    production machinery, run separately against DAILY_MODEL_REGISTRY
    for DAILY_HORIZONS_D (2D/3D) - not gated by the weekly
    assess_forecastability (that gate's thresholds are calibrated in
    weeks and don't apply at daily resolution), a champion simply is or
    isn't found per horizon the same way the weekly grid works.

    Returns a dict with a manifest row, weekly forecast rows, the
    weekly full grid, and (if daily_series was given) daily_forecasts /
    daily_grid in the same shapes.
    """
    fc = assess_forecastability(series)

    if not fc["is_forecastable"]:
        manifest = {"sku": sku, **fc}
        forecast_rows, full_grid_rows = [], []
    else:
        grid_df = grid_search_sku(series, HORIZONS_D, MODEL_REGISTRY, period_unit="W", min_len_scale=1)
        champions = select_per_horizon_champions(grid_df)

        full_grid_rows = []
        for _, row in grid_df.iterrows():
            full_grid_rows.append({
                "sku": sku, "horizon_days": int(row["horizon_days"]), "horizon_weeks": int(row["horizon_weeks"]),
                "model": row["model"], "wape": round(float(row["wape"]), 3),
                "robust_mape": round(float(row["robust_mape"]), 3),
            })

        forecast_rows = []
        for h_days in HORIZONS_D:
            champ = champions.get(h_days)
            if champ is None:
                forecast_rows.append({
                    "sku": sku, "horizon_days": h_days, "horizon_weeks": max(1, round(h_days / 7)),
                    "best_model": None, "wape": None, "robust_mape": None,
                    "forecast_total": None, "forecast_values": None, "forecast_dates": None,
                    "grade": "No Model", "zero_wape_override": False,
                    "untrusted_zero_model": None, "data_gap_periods": None,
                })
                continue

            h_weeks = champ["horizon_weeks"]
            prod = forecast_sku_production(series, champ["model"], h_weeks, MODEL_REGISTRY,
                                            start_date, gap_weeks, period_unit="W")
            forecast_rows.append({
                "sku": sku, "horizon_days": h_days, "horizon_weeks": h_weeks,
                "best_model": champ["model"],
                "wape": round(champ["wape"], 3),
                "robust_mape": round(champ["robust_mape"], 3),
                "forecast_total": prod.get("total") if prod["success"] else None,
                "forecast_values": json.dumps(prod.get("values")) if prod["success"] else None,
                "forecast_dates": json.dumps(prod.get("dates")) if prod["success"] else None,
                "grade": classify_sku("FORECASTABLE", champ["wape"]),
                "zero_wape_override": bool(champ.get("zero_wape_override", False)),
                "untrusted_zero_model": champ.get("untrusted_zero_model"),
                "data_gap_periods": prod.get("data_gap_periods") if prod["success"] else None,
            })

        manifest = {"sku": sku, **fc}

    daily_forecasts, daily_grid_rows = [], []
    if daily_series is not None:
        d_start = daily_start_date if daily_start_date is not None else \
            (pd.Timestamp.today().normalize() + pd.Timedelta(days=1))

        dgrid = grid_search_sku(daily_series, DAILY_HORIZONS_D, DAILY_MODEL_REGISTRY,
                                 period_unit="D", min_len_scale=7)
        dchampions = select_per_horizon_champions(dgrid)

        for _, row in dgrid.iterrows():
            daily_grid_rows.append({
                "sku": sku, "horizon_days": int(row["horizon_days"]),
                "model": row["model"], "wape": round(float(row["wape"]), 3),
                "robust_mape": round(float(row["robust_mape"]), 3),
            })

        for h_days in DAILY_HORIZONS_D:
            champ = dchampions.get(h_days)
            if champ is None:
                daily_forecasts.append({
                    "sku": sku, "horizon_days": h_days,
                    "best_model": None, "wape": None,
                    "forecast_total": None, "forecast_values": None, "forecast_dates": None,
                    "grade": "No Model", "zero_wape_override": False,
                    "untrusted_zero_model": None, "data_gap_periods": None,
                })
                continue

            prod = forecast_sku_production(daily_series, champ["model"], h_days, DAILY_MODEL_REGISTRY,
                                            d_start, gap_days, period_unit="D")
            daily_forecasts.append({
                "sku": sku, "horizon_days": h_days,
                "best_model": champ["model"], "wape": round(champ["wape"], 3),
                "forecast_total": prod.get("total") if prod["success"] else None,
                "forecast_values": json.dumps(prod.get("values")) if prod["success"] else None,
                "forecast_dates": json.dumps(prod.get("dates")) if prod["success"] else None,
                "grade": classify_sku("FORECASTABLE", champ["wape"]),
                "zero_wape_override": bool(champ.get("zero_wape_override", False)),
                "untrusted_zero_model": champ.get("untrusted_zero_model"),
                "data_gap_periods": prod.get("data_gap_periods") if prod["success"] else None,
            })

    return {
        "manifest": manifest,
        "forecasts": forecast_rows,
        "full_grid": full_grid_rows,
        "daily_forecasts": daily_forecasts,
        "daily_grid": daily_grid_rows,
    }
