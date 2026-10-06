"""
PetMaxi Forecasting Dashboard - Backend v8
=============================================
Extends v7 backend with planner endpoints for:
  - Order-centric open orders list (for accordion view)
  - FG inventory aggregation (for inventory table)
  - Planner page route

Additional v8 endpoints:
  POST /api/log_error         -> receive frontend API error reports
  GET  /api/error_log         -> return recent error log entries
  GET  /api/new_skus          -> SKUs in open orders but not in forecast DB

All existing v7 endpoints preserved intact.
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

import pandas as pd
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import open_orders_store as oo_store
import netpet_inventory_store as inv_store
import logging

# -- CONFIG -------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PETMAXI_DB_PATH", os.path.join(_HERE, "db", "petmaxi_v7.db"))
DATA_PATH = os.environ.get("PETMAXI_DATA_PATH", os.path.join(_HERE, "data", "vendas_1_1.xlsx"))
OPEN_ORDERS_FIXTURE = os.environ.get(
    "PETMAXI_ORDERS_FIXTURE", os.path.join(_HERE, "data", "open_orders_fixture.json")
)
TEMPLATE_NAME = "dashboard_v8_ab_api.html"
LANDING_TEMPLATE = "landing_v8_ab_api.html"
PLANNER_TEMPLATE = "dashboard_v8_planner.html"

DEFAULT_HORIZON_DAYS = 28
ATTENTION_TOP_N = 5
# 13W kept in DB but hidden from the UI horizon selector (gap-alignment Oct 2026).
HORIZON_LABELS = {0: "0", 7: "1W", 14: "2W", 28: "4W", 56: "8W", 91: "13W"}
UI_HORIZON_WEEKS = [0, 1, 2, 4, 8]  # buttons shown on the forecast dashboard
HORIZON_WEEKS_TO_DAYS = {0: 0, 1: 7, 2: 14, 4: 28, 8: 56, 13: 91}
DAILY_HORIZON_LABELS = {2: "2D", 3: "3D"}
PRIORITY_TOLERANCE_PTS = 3.0

OPEN_ORDER_GAP_BASIS = os.environ.get("PETMAXI_OO_GAP_BASIS", "pending")
GAP_PCT_BASE = os.environ.get("PETMAXI_GAP_PCT_BASE", "demand")

FRESH_COLOR = (238, 49, 36)
STALE_COLOR = (153, 153, 140)
FRESHNESS_FULLY_STALE_WEEKS = 8.0
LOG_DB_PATH = os.environ.get("PETMAXI_LOG_DB", os.path.join(_HERE, "db", "petmaxi_errors.db"))

app = Flask(__name__)
CORS(app)


# -- DB helpers ---------------------------------------------------------------

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _db_exists():
    if not os.path.exists(DB_PATH):
        return False
    conn = _db()
    try:
        conn.execute("SELECT 1 FROM batch_meta LIMIT 1")
        return True
    except Exception:
        return False
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _safe_oo_map(basis: str = None):
    try:
        return oo_store.open_order_gap_tons_all_skus(basis or OPEN_ORDER_GAP_BASIS)
    except Exception as e:
        print(f"WARN: open-orders gap map failed: {e}")
        return {}


def _resolve_inventory(conn):
    try:
        if inv_store.has_inventory():
            return inv_store.available_all_skus(), "netpet_api"
    except Exception as e:
        print(f"WARN: NETPET inventory read failed: {e}")
    if _table_exists(conn, "sku_inventory"):
        rows = pd.read_sql("SELECT sku, available FROM sku_inventory", conn)
        return ({str(r["sku"]): float(r["available"]) for _, r in rows.iterrows()
                 if pd.notna(r["available"])}, "sku_inventory_table")
    return {}, None


def _available_for_sku(sku: str):
    try:
        if inv_store.has_inventory():
            return inv_store.available_tons_for_sku(sku), "netpet_api"
    except Exception as e:
        print(f"WARN: NETPET inventory read failed for {sku}: {e}")
    conn = _db()
    try:
        if _table_exists(conn, "sku_inventory"):
            row = conn.execute("SELECT available FROM sku_inventory WHERE sku = ?", (sku,)).fetchone()
            if row and row["available"] is not None:
                return float(row["available"]), "sku_inventory_table"
    finally:
        conn.close()
    return None, None


def _safe_float(row, col: str):
    try:
        val = row[col]
        return None if val is None else round(float(val), 4)
    except (IndexError, KeyError):
        return None


def _safe_str(row, col: str):
    try:
        val = row[col]
        return str(val) if val is not None else None
    except (IndexError, KeyError):
        return None


# -- Unit conversion factors --------------------------------------------------
_conv_cache = {}

def _load_sack_weights():
    global _conv_cache
    if "sack" in _conv_cache:
        return _conv_cache["sack"]
    try:
        if not os.path.exists(DATA_PATH):
            _conv_cache["sack"] = {}
            return _conv_cache["sack"]
        fam = pd.read_excel(DATA_PATH, sheet_name="SKU_FAMILY")
        fam["SKU"] = fam["SKU"].astype(str)
        weight_col = "WEIGHT" if "WEIGHT" in fam.columns else None
        if weight_col is None:
            for c in fam.columns:
                if "weight" in c.lower():
                    weight_col = c
                    break
        _conv_cache["sack"] = dict(zip(fam["SKU"], fam[weight_col])) if weight_col else {}
    except Exception as e:
        print(f"WARN: SKU_FAMILY sack-weight load failed: {e}")
        _conv_cache["sack"] = {}
    return _conv_cache["sack"]


def _get_conversions(sku: str, inv_row) -> dict:
    sack_w = _load_sack_weights().get(str(sku))
    sack_w = float(sack_w) if sack_w is not None and not pd.isna(sack_w) and float(sack_w) > 0 else None
    kg_per_pallet = None
    if inv_row is not None:
        try:
            oh, np_ = inv_row["on_hand"], inv_row["n_pallets"]
            if oh is not None and np_ and np_ > 0:
                kg_per_pallet = round(float(oh) * 1000.0 / int(np_), 1)
        except (KeyError, IndexError, TypeError):
            pass
    return {"sack_weight_kg": sack_w, "kg_per_pallet_wes": kg_per_pallet}


# -- RM helper ----------------------------------------------------------------
_rm_cache = {}

def _load_rm_data():
    global _rm_cache
    if _rm_cache and not _rm_cache.get("sku_formula", pd.DataFrame()).empty:
        return _rm_cache
    try:
        if not os.path.exists(DATA_PATH):
            return {"sku_formula": pd.DataFrame(), "formula": pd.DataFrame()}
        sf = pd.read_excel(DATA_PATH, sheet_name="sku_formula")
        fm = pd.read_excel(DATA_PATH, sheet_name="Formula")
        sf["SKU"] = sf["SKU"].astype(str)
        _rm_cache = {"sku_formula": sf, "formula": fm}
    except Exception as e:
        print(f"WARN: RM data load error: {e}")
        return {"sku_formula": pd.DataFrame(), "formula": pd.DataFrame()}
    return _rm_cache


def _get_rm_forecast(sku: str, fg_total: Optional[float]) -> list:
    rm_data = _load_rm_data()
    sf, fm = rm_data["sku_formula"], rm_data["formula"]
    if sf.empty or fm.empty or not fg_total or fg_total <= 0:
        return []
    mapping = sf[sf["SKU"] == sku]
    if mapping.empty:
        return []
    formula_code = mapping["SKU_FORMULA"].iloc[0]
    details = fm[fm["SKU_FORMULA"] == formula_code]
    if details.empty:
        return []
    result = []
    for _, r in details.iterrows():
        ton_per = float(r.get("TON", 0))
        result.append({
            "rm_sku": r.get("SKU_RAW", ""),
            "rm_name": r.get("RAW_NAME", r.get("SKU_RAW", "")),
            "ton_per_1000kg": ton_per,
            "rm_forecast": round((fg_total / 1000) * ton_per, 3),
        })
    return result


# -- ABC velocity tiers -------------------------------------------------------

def _compute_tiers(manifest_rows, weight_volume=0.7, weight_frequency=0.3):
    df = pd.DataFrame(manifest_rows)
    if df.empty:
        return df
    df = df.drop(columns=["velocity_tier"], errors="ignore")
    vol_rank = df["total_volume"].rank(pct=True)
    freq_rank = df["n_active"].rank(pct=True)
    df["velocity_score"] = weight_volume * vol_rank + weight_frequency * freq_rank
    ranked = df.sort_values("velocity_score", ascending=False).reset_index(drop=True)
    n = len(ranked)
    n_a = max(1, int(n * 0.20))
    n_b = max(1, int(n * 0.30))
    tiers = (["A"] * n_a) + (["B"] * n_b) + (["C"] * (n - n_a - n_b))
    ranked["velocity_tier"] = tiers[:n]
    return df.merge(ranked[["sku", "velocity_tier"]], on="sku", how="left")


# -- Smart per-SKU default horizon --------------------------------------------
HIGH_ZERO_RATE_THRESHOLD = 0.3

def _priority_horizon(valid_forecasts: list) -> Optional[dict]:
    if not valid_forecasts:
        return None
    best = min(valid_forecasts, key=lambda r: r["wape"])
    ordered = sorted(valid_forecasts, key=lambda r: r["horizon_days"])
    for r in ordered:
        if r["horizon_days"] >= best["horizon_days"]:
            return r if r["horizon_days"] == best["horizon_days"] else best
        if r["wape"] <= best["wape"] + PRIORITY_TOLERANCE_PTS:
            return r
    return best


def _best_horizon(valid_forecasts: list) -> Optional[dict]:
    if not valid_forecasts:
        return None
    return min(valid_forecasts, key=lambda r: r["wape"])


def _primary_horizon(forecasts_by_horizon: dict) -> Optional[dict]:
    valid = [f for f in forecasts_by_horizon.values() if f.get("wape") is not None]
    if not valid:
        return None
    one_week = forecasts_by_horizon.get(7) or forecasts_by_horizon.get(1)
    if one_week and one_week.get("wape") is not None:
        return one_week
    return _priority_horizon(valid)


# -- Freshness color interpolation --------------------------------------------

def _freshness_color(gap_weeks: float) -> str:
    frac = max(0.0, min(1.0, gap_weeks / FRESHNESS_FULLY_STALE_WEEKS))
    rgb = tuple(round(FRESH_COLOR[i] + (STALE_COLOR[i] - FRESH_COLOR[i]) * frac) for i in range(3))
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def _freshness_label(gap_weeks: float) -> str:
    if gap_weeks <= 1:
        return "Fresh"
    if gap_weeks <= 3:
        return "Aging"
    if gap_weeks <= 6:
        return "Stale"
    return "Very Stale"


# =============================================================================
# ROUTES
# =============================================================================

BASE_PATH = os.environ.get("PETMAXI_BASE_PATH", "/petmaxi-dashboard")

@app.route("/")
def landing():
    return render_template(LANDING_TEMPLATE, base_path=BASE_PATH)

@app.route("/forecast")
def forecast():
    return render_template(TEMPLATE_NAME, base_path=BASE_PATH)

@app.route("/planner")
def planner():
    return render_template(PLANNER_TEMPLATE, base_path=BASE_PATH)

@app.route("/analytics")
def analytics():
    return render_template("Metric_Analytics_ab.html")

@app.route("/customers")
def customers():
    return render_template("Customer_Intelligence_ab.html")


# -- Existing v7 API endpoints (unchanged) ------------------------------------

@app.route("/api/freshness")
def api_freshness():
    if not _db_exists():
        return jsonify({"ready": False, "message": "No batch run yet."})
    conn = _db()
    meta = conn.execute("SELECT * FROM batch_meta ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not meta:
        return jsonify({"ready": False, "message": "No batch data found."})
    gap_weeks = _safe_float(meta, "gap_weeks") or 0.0
    gap_days = _safe_float(meta, "gap_days") or 0.0
    return jsonify({
        "ready": True, "gap_weeks": gap_weeks, "gap_days": gap_days,
        "weekly_data_as_of": _safe_str(meta, "weekly_data_as_of"),
        "daily_data_as_of": _safe_str(meta, "daily_data_as_of"),
        "run_at": meta["run_at"],
        "color": _freshness_color(gap_weeks),
        "label": _freshness_label(gap_weeks),
        "message": (
            f"Sales data as of {_safe_str(meta, 'weekly_data_as_of')}. "
            f"Forecasts bridge {int(gap_weeks)} week(s) to reach today - "
            f"treat as provisional until refreshed."
        ) if gap_weeks > 0 else "Forecasts are current as of the latest data.",
    })


@app.route("/api/batch_status")
def api_batch_status():
    if not _db_exists():
        return jsonify({"ready": False, "message": "No batch run yet."})
    conn = _db()
    meta = conn.execute("SELECT * FROM batch_meta ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not meta:
        return jsonify({"ready": False, "message": "No batch data found."})
    return jsonify({
        "ready": True, "run_at": meta["run_at"], "total_skus": meta["total_skus"],
        "forecastable": meta["forecastable"], "status": meta["status"],
        "duration_sec": meta["duration_sec"],
        "gap_weeks": _safe_float(meta, "gap_weeks"), "gap_days": _safe_float(meta, "gap_days"),
    })


@app.route("/api/readiness")
def api_readiness():
    if not _db_exists():
        return jsonify({"ready": False, "message": "No batch run yet."})
    conn = _db()
    manifest = pd.read_sql("SELECT * FROM sku_manifest", conn)
    forecasts = pd.read_sql("SELECT * FROM sku_forecasts", conn)
    last_run = conn.execute("SELECT run_at FROM batch_meta ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    total_all = len(manifest)
    fore_all = int(manifest["is_forecastable"].sum()) if total_all else 0
    tiers = {}
    for t in ("A", "B", "C"):
        sub = manifest[manifest["velocity_tier"] == t]
        tiers[t] = {"total": int(len(sub)), "forecastable": int(sub["is_forecastable"].sum())}
    grade_counts = {"Excellent": 0, "Good": 0, "Fair": 0, "Poor": 0, "Unreliable": 0,
                    "Insufficient History": 0, "No Model": 0}
    for _, m in manifest.iterrows():
        if not m["is_forecastable"]:
            grade_counts["Insufficient History"] += 1
            continue
        sku_forecasts = forecasts[forecasts["sku"] == m["sku"]]
        by_h = {int(r["horizon_days"]): r.to_dict() for _, r in sku_forecasts.iterrows()}
        primary = _primary_horizon(by_h)
        grade = primary["grade"] if primary else "No Model"
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
    return jsonify({
        "ready": True, "total_skus": total_all, "forecastable": fore_all,
        "pct_ready": round(fore_all / total_all * 100, 1) if total_all else 0,
        "last_run": last_run["run_at"] if last_run else None,
        "by_tier": tiers, "by_grade": grade_counts,
    })


@app.route("/api/skus")
def api_skus():
    if not _db_exists():
        return jsonify({"ready": False, "items": []})
    tier_filter = request.args.get("tier", "").upper()
    search = request.args.get("search", "").strip().lower()
    conn = _db()
    manifest = pd.read_sql("SELECT * FROM sku_manifest", conn)
    forecasts = pd.read_sql("SELECT * FROM sku_forecasts", conn)
    conn.close()
    if tier_filter in ("A", "B", "C"):
        manifest = manifest[manifest["velocity_tier"] == tier_filter]
    if search:
        manifest = manifest[
            manifest["sku"].str.lower().str.contains(search, na=False) |
            manifest["description"].str.lower().str.contains(search, na=False)
        ]
    items = []
    for _, m in manifest.iterrows():
        sku_forecasts = forecasts[forecasts["sku"] == m["sku"]]
        by_h = {int(r["horizon_days"]): r.to_dict() for _, r in sku_forecasts.iterrows()}
        valid = [f for f in by_h.values() if f.get("wape") is not None]
        primary = _primary_horizon(by_h)
        best = _best_horizon(valid)
        items.append({
            "sku": m["sku"], "description": m["description"], "family": m["family"],
            "velocity_tier": m["velocity_tier"], "is_forecastable": bool(m["is_forecastable"]),
            "demand_pattern": m.get("demand_pattern"),
            "default_horizon_days": primary["horizon_days"] if primary else None,
            "default_horizon_label": HORIZON_LABELS.get(primary["horizon_days"]) if primary else None,
            "default_wape": round(primary["wape"], 2) if primary else None,
            "default_grade": primary["grade"] if primary else "No Model",
            "best_horizon_days": best["horizon_days"] if best else None,
            "best_horizon_label": HORIZON_LABELS.get(best["horizon_days"]) if best else None,
            "best_wape": round(best["wape"], 2) if best else None,
            "zero_wape_override": bool(primary.get("zero_wape_override")) if primary else False,
        })
    return jsonify({"ready": True, "count": len(items), "items": items})


ATTENTION_HORIZON_DAYS = 7

# ---------------------------------------------------------------------------
# Gap-alignment rework (Oct 2026). Reference: PetMaxi_Gap_Calculation_Alignment
#
# Horizons on the UI: 0, 1W, 2W, 4W, 8W. 13W kept in DB, hidden on UI.
# For each (SKU, horizon_days h):
#     oo_tons_h    = open_order_gap_tons_all_skus_capped(basis, h)
#                    (orders with delivery_date <= today+h, or NULL date;
#                     prorated to tonnes against the chosen qty basis)
#     fcast_h      = sku_forecasts.forecast_total for that horizon
#                    (0 when h == 0, since there is no forecast at p=0)
#     on_hand      = inventory on-hand tonnes (NETPET; no allocated subtraction)
#     gap          = fcast_h + oo_tons_h - on_hand
#     gap_pct      = gap / (fcast_h + oo_tons_h) * 100   (0 if denom <= 0)
#
# Universe:
#   p = 0  -> union of (SKUs with any open order <= today). Forecast hidden,
#            sort by absolute gap tonnes desc. All SKUs eligible (NF/NEW
#            badges where applicable). ABC split preserved.
#   p >= 1W -> forecastable SKUs AND non-forecastable manifest SKUs with
#              oo_tons_h > 0 (NF badge), plus new-order SKUs that have
#              oo_tons_h > 0 but are NOT in the manifest (NEW badge, tier C).
#   Attention bucket: gap_pct > 20% (h>=1W); gap > 0 tonnes (h=0).
# ---------------------------------------------------------------------------

@app.route("/api/attention")
def api_attention():
    if not _db_exists():
        return jsonify({"ready": False, "message": "No batch run yet."})
    horizon_weeks = int(request.args.get("horizon", 1))
    horizon_days = HORIZON_WEEKS_TO_DAYS.get(horizon_weeks, horizon_weeks * 7)
    top_n = int(request.args.get("top_n", 999))
    tier_filter = request.args.get("tier", "").upper()

    conn = _db()
    manifest_all = pd.read_sql("SELECT * FROM sku_manifest", conn)
    forecasts_h = (
        pd.read_sql("SELECT * FROM sku_forecasts WHERE horizon_days = ?",
                    conn, params=(horizon_days,))
        if horizon_days > 0 else pd.DataFrame()
    )
    all_forecasts = pd.read_sql(
        "SELECT sku, horizon_days, wape, best_model FROM sku_forecasts", conn
    )
    inv_map, inv_source = _resolve_inventory(conn)
    conn.close()

    # Capped open orders for this horizon (gap-alignment rework)
    try:
        oo_map = oo_store.open_order_gap_tons_all_skus_capped(
            OPEN_ORDER_GAP_BASIS, horizon_days
        )
    except Exception as e:
        print(f"WARN: capped open-orders map failed: {e}")
        oo_map = {}

    # Lookup helpers
    manifest_by_sku = {str(r["sku"]): r for _, r in manifest_all.iterrows()}
    forecast_by_sku = (
        {str(r["sku"]): r for _, r in forecasts_h.iterrows()}
        if not forecasts_h.empty else {}
    )

    # --- Build the SKU universe for this horizon ---------------------------
    universe = set()
    if horizon_weeks == 0:
        # p = 0: every SKU that has any open order on/before today.
        universe = set(oo_map.keys())
    else:
        # p >= 1W: forecastable manifest SKUs + non-forecastable manifest
        # SKUs with open orders > 0 + new SKUs (not in manifest) with
        # open orders > 0.
        universe |= {s for s, r in manifest_by_sku.items() if bool(r.get("is_forecastable"))}
        universe |= {s for s, t in oo_map.items() if (t or 0) > 0}

    if tier_filter in ("A", "B", "C"):
        # Only applied to SKUs that are in the manifest. New SKUs are tier C
        # by default — see tier resolution below.
        universe = {
            s for s in universe
            if (manifest_by_sku.get(s, {}).get("velocity_tier") or "C") == tier_filter
            or (s not in manifest_by_sku and tier_filter == "C")
        }

    by_tier = {"A": {"attention": [], "sufficient": []},
               "B": {"attention": [], "sufficient": []},
               "C": {"attention": [], "sufficient": []}}

    for sku_str in universe:
        m = manifest_by_sku.get(sku_str)
        is_new_sku = m is None
        is_forecastable = bool(m.get("is_forecastable")) if m is not None else False

        f = forecast_by_sku.get(sku_str)
        if horizon_weeks == 0 or f is None:
            forecast_total = 0.0 if horizon_weeks == 0 else None
            if f is not None:
                ft = f.get("forecast_total")
                forecast_total = None if pd.isna(ft) else float(ft)
        else:
            ft = f.get("forecast_total")
            forecast_total = None if pd.isna(ft) else float(ft)

        # Inventory
        if inv_source == "netpet_api":
            available = float(inv_map.get(sku_str, 0.0))
        elif inv_source == "sku_inventory_table":
            available = inv_map.get(sku_str)
            available = float(available) if available is not None else None
        else:
            available = None
        on_hand = available if available is not None else 0.0

        oo_tons = round(float(oo_map.get(sku_str, 0.0)), 3)

        # --- Gap calc (additive, no allocated subtraction) -----------------
        if horizon_weeks == 0:
            # p = 0: forecast hidden, gap on open orders only
            gap = round(oo_tons - on_hand, 3)
            denom = oo_tons
            gap_pct = round((gap / denom * 100) if denom and denom > 0 else 0, 1)
            bucket = "attention" if gap > 0 else "sufficient"
            urgency = "red" if gap > 0 and (gap_pct > 50 or denom == 0) else (
                "yellow" if gap > 0 else "green"
            )
            demand_total = oo_tons
        else:
            fcast_for_gap = forecast_total if forecast_total is not None else 0.0
            demand_total = fcast_for_gap + oo_tons
            gap = round(fcast_for_gap + oo_tons - on_hand, 3)
            gap_pct = round(
                (gap / demand_total * 100) if demand_total and demand_total > 0 else 0, 1
            )
            threshold = 20
            urgency = "red" if gap_pct > 50 else ("yellow" if gap_pct > threshold else "green")
            bucket = "attention" if gap_pct > threshold else "sufficient"

        # Best-horizon reference (across all horizons in DB)
        sku_all = all_forecasts[(all_forecasts["sku"] == sku_str) & all_forecasts["wape"].notna()]
        best_row = sku_all.loc[sku_all["wape"].idxmin()] if not sku_all.empty else None

        # Tier resolution: manifest tier if present, else C for new SKUs
        tier = (m.get("velocity_tier") if m is not None else None) or "C"
        if tier not in by_tier:
            tier = "C"

        # Grade / NF-NEW flags
        if is_new_sku:
            grade = "New"
        elif not is_forecastable:
            grade = "Insufficient History"
        elif horizon_weeks == 0:
            grade = "No Forecast"
        elif f is not None and f.get("grade"):
            grade = f.get("grade")
        else:
            grade = "No Model"

        is_nf = (not is_new_sku) and (
            (not is_forecastable)
            or (horizon_weeks > 0 and (f is None or forecast_total is None))
        )

        wape_val = None
        best_model = None
        zero_wape_override = False
        data_gap_periods = None
        if f is not None:
            w = f.get("wape")
            wape_val = round(float(w), 2) if (w is not None and not pd.isna(w)) else None
            best_model = f.get("best_model")
            zero_wape_override = bool(f.get("zero_wape_override")) if "zero_wape_override" in f else False
            dgp = f.get("data_gap_periods")
            data_gap_periods = int(dgp) if (dgp is not None and not pd.isna(dgp)) else None

        card = {
            "sku": sku_str,
            "description": (m.get("description") if m is not None else None),
            "family": (m.get("family") if m is not None else None),
            "velocity_tier": tier,
            "best_model": best_model,
            "wape": wape_val,
            "grade": grade,
            "is_nf": is_nf,
            "is_new": is_new_sku,
            "forecast_total": round(forecast_total, 2) if forecast_total is not None else None,
            "forecast_hidden": (horizon_weeks == 0),
            "open_order_tons": oo_tons,
            "open_order_basis": OPEN_ORDER_GAP_BASIS,
            "demand_total": round(demand_total, 2),
            "on_hand": round(on_hand, 2),
            "available": round(on_hand, 2),  # == on_hand now (no allocated)
            "source_tag": "NETPET" if inv_source == "netpet_api" else None,
            "gap": gap,
            "gap_pct": gap_pct,
            "urgency": urgency,
            "horizon_weeks": horizon_weeks,
            "inventory_covered": available is not None,
            "best_horizon_days": int(best_row["horizon_days"]) if best_row is not None else None,
            "best_horizon_label": HORIZON_LABELS.get(int(best_row["horizon_days"])) if best_row is not None else None,
            "best_wape": round(float(best_row["wape"]), 2) if best_row is not None else None,
            "zero_wape_override": zero_wape_override,
            "data_gap_periods": data_gap_periods,
        }
        by_tier[tier][bucket].append(card)

    tier_labels = {"A": "Fast Movers", "B": "Medium Movers", "C": "Slow Movers"}
    result = {}
    for t in ("A", "B", "C"):
        if horizon_weeks == 0:
            # p = 0: sort attention by absolute gap tonnes desc (user request)
            attn = sorted(by_tier[t]["attention"], key=lambda x: -(x["gap"] or 0))
            suff = sorted(by_tier[t]["sufficient"], key=lambda x: (x["gap"] or 0))
        else:
            # Other horizons: sort by gap_pct desc inside attention, asc in sufficient
            attn = sorted(by_tier[t]["attention"], key=lambda x: -(x["gap_pct"] or 0))
            suff = sorted(by_tier[t]["sufficient"], key=lambda x: (x["gap_pct"] or 0))
        result[t] = {"label": tier_labels[t],
                     "attention": attn[:top_n], "attention_total": len(attn),
                     "sufficient": suff[:top_n], "sufficient_total": len(suff)}

    return jsonify({
        "ready": True,
        "horizon_weeks": horizon_weeks,
        "horizon_days": horizon_days,
        "ui_horizons": UI_HORIZON_WEEKS,
        "forecast_hidden": (horizon_weeks == 0),
        "sort_mode": ("gap_tons" if horizon_weeks == 0 else "gap_pct"),
        "inventory_available": inv_source is not None,
        "inventory_source": inv_source,
        "open_order_basis": OPEN_ORDER_GAP_BASIS,
        "tiers": result,
    })


@app.route("/api/sku/<sku_id>")
def api_sku_detail(sku_id: str):
    if not _db_exists():
        return jsonify({"error": "No batch data."}), 503
    conn = _db()
    manifest = conn.execute("SELECT * FROM sku_manifest WHERE sku = ?", (sku_id,)).fetchone()
    if not manifest:
        conn.close()
        return jsonify({"error": f"SKU {sku_id} not found"}), 404
    forecasts_raw = conn.execute(
        "SELECT * FROM sku_forecasts WHERE sku = ? ORDER BY horizon_days", (sku_id,)
    ).fetchall()
    daily_raw = conn.execute(
        "SELECT * FROM sku_daily_forecasts WHERE sku = ? ORDER BY horizon_days", (sku_id,)
    ).fetchall() if _table_exists(conn, "sku_daily_forecasts") else []
    has_inventory = _table_exists(conn, "sku_inventory")
    inv = conn.execute("SELECT * FROM sku_inventory WHERE sku = ?", (sku_id,)).fetchone() if has_inventory else None
    conn.close()
    forecasts = {}
    for f in forecasts_raw:
        h = f["horizon_weeks"]
        forecasts[h] = {
            "horizon_days": f["horizon_days"], "horizon_weeks": f["horizon_weeks"],
            "horizon_label": HORIZON_LABELS.get(h, h),
            "best_model": f["best_model"],
            "wape": round(f["wape"], 2) if f["wape"] is not None else None,
            "robust_mape": round(f["robust_mape"], 2) if f["robust_mape"] is not None else None,
            "grade": f["grade"] or "No Model",
            "forecast_total": round(f["forecast_total"], 2) if f["forecast_total"] is not None else None,
            "forecast_values": json.loads(f["forecast_values"]) if f["forecast_values"] else [],
            "forecast_dates": json.loads(f["forecast_dates"]) if f["forecast_dates"] else [],
            "zero_wape_override": bool(f["zero_wape_override"]),
            "untrusted_zero_model": f["untrusted_zero_model"],
            "data_gap_periods": f["data_gap_periods"],
        }
    daily = {}
    for d in daily_raw:
        h = d["horizon_days"]
        grade = d["grade"] or "No Model"
        gated = grade == "Unreliable"
        daily[h] = {
            "horizon_days": h, "horizon_label": DAILY_HORIZON_LABELS.get(h, h),
            "best_model": d["best_model"],
            "wape": round(d["wape"], 2) if d["wape"] is not None else None,
            "grade": grade, "gated": gated,
            "forecast_total": None if gated else (round(d["forecast_total"], 2) if d["forecast_total"] is not None else None),
            "forecast_values": [] if gated else (json.loads(d["forecast_values"]) if d["forecast_values"] else []),
            "forecast_dates": json.loads(d["forecast_dates"]) if d["forecast_dates"] else [],
            "display_value": "insufficient" if gated else None,
            "zero_wape_override": bool(d["zero_wape_override"]),
            "untrusted_zero_model": d["untrusted_zero_model"],
            "data_gap_periods": d["data_gap_periods"],
        }
    valid = [f for f in forecasts.values() if f.get("wape") is not None]
    primary = _primary_horizon(forecasts)
    best = _best_horizon(valid)
    priority = _priority_horizon(valid)
    available_val, av_source = _available_for_sku(sku_id)
    # Uncapped total open-order tonnes (kept for backward compat / KPI card)
    oo_tons_total = 0.0
    try:
        oo_tons_total = round(
            oo_store.open_order_gap_tons_for_sku(sku_id, OPEN_ORDER_GAP_BASIS), 3
        )
    except Exception as e:
        print(f"WARN: open-orders gap for {sku_id} failed: {e}")
    inv_val = round(available_val, 2) if available_val is not None else 0
    inventory = {
        "on_hand": inv_val,
        "available": inv_val,  # == on_hand (allocated dropped Oct 2026)
        "source_tag": "NETPET" if av_source == "netpet_api" else None,
        "covered": available_val is not None,
        "open_order_tons": oo_tons_total,
        "open_order_basis": OPEN_ORDER_GAP_BASIS,
    }
    on_hand_for_gap = available_val if available_val is not None else 0.0

    # Per-horizon gaps now use HORIZON-CAPPED open orders (gap-alignment).
    gaps = {}
    oo_tons_by_horizon = {}
    # p = 0: forecast hidden, gap = oo_cum_0 - on_hand
    try:
        oo_0 = oo_store.open_order_gap_tons_for_sku_capped(
            sku_id, OPEN_ORDER_GAP_BASIS, 0
        )
    except Exception as e:
        print(f"WARN: capped OO (h=0) for {sku_id} failed: {e}")
        oo_0 = 0.0
    oo_tons_by_horizon[0] = oo_0
    gaps[0] = round(oo_0 - on_hand_for_gap, 2)
    # p = 1W/2W/4W/8W (and 13W if present): fcast_h + oo_cum_h - on_hand
    for h_weeks, fc in forecasts.items():
        if h_weeks == 0:
            continue
        h_days = fc.get("horizon_days") or HORIZON_WEEKS_TO_DAYS.get(h_weeks, h_weeks * 7)
        try:
            oo_h = oo_store.open_order_gap_tons_for_sku_capped(
                sku_id, OPEN_ORDER_GAP_BASIS, h_days
            )
        except Exception as e:
            print(f"WARN: capped OO (h={h_days}) for {sku_id} failed: {e}")
            oo_h = 0.0
        oo_tons_by_horizon[h_weeks] = oo_h
        fcast = fc["forecast_total"] if fc["forecast_total"] is not None else 0.0
        gaps[h_weeks] = round(fcast + oo_h - on_hand_for_gap, 2)
    fg_total_for_rm = primary["forecast_total"] if primary and primary.get("forecast_total") else 0.0
    rm = _get_rm_forecast(sku_id, fg_total_for_rm)
    def _horizon_ref(h):
        if not h:
            return None
        return {"horizon_days": h["horizon_days"], "horizon_label": HORIZON_LABELS.get(h["horizon_days"]),
                "wape": round(h["wape"], 2), "model": h.get("best_model")}
    return jsonify({
        "sku": sku_id, "description": manifest["description"],
        "family": manifest["family"], "velocity_tier": manifest["velocity_tier"],
        "velocity": {"tier": manifest["velocity_tier"]},
        "forecastability": {
            "is_forecastable": bool(manifest["is_forecastable"]),
            "status": manifest["status"], "demand_pattern": manifest["demand_pattern"],
            "adi": _safe_float(manifest, "adi"), "cv2": _safe_float(manifest, "cv2"),
            "zero_pct": _safe_float(manifest, "zero_pct"),
        },
        "default_horizon_days": primary["horizon_days"] if primary else None,
        "default_horizon_label": HORIZON_LABELS.get(primary["horizon_days"]) if primary else None,
        "conversions": _get_conversions(sku_id, inv),
        "best_horizon": _horizon_ref(best),
        "priority_horizon": _horizon_ref(priority),
        "inventory": inventory, "forecasts": forecasts, "daily_forecasts": daily,
        "gaps": gaps,
        "open_order_tons_by_horizon": oo_tons_by_horizon,
        "rm_requirements": rm,
    })


@app.route("/api/external_inventory")
def api_external_inventory():
    if not _db_exists():
        return jsonify({"ready": False, "items": []})
    conn = _db()
    if not _table_exists(conn, "sku_inventory"):
        conn.close()
        return jsonify({"ready": True, "items": []})
    inv = pd.read_sql("SELECT * FROM sku_inventory", conn)
    manifest_skus = set(pd.read_sql("SELECT sku FROM sku_manifest", conn)["sku"])
    conn.close()
    new_entries = inv[~inv["sku"].isin(manifest_skus)].sort_values("on_hand", ascending=False)
    items = [{
        "sku": r["sku"], "on_hand_tons": round(r["on_hand"], 2),
        "n_pallets": int(r["n_pallets"]), "source_tag": r["source_tag"],
    } for _, r in new_entries.iterrows()]
    return jsonify({"ready": True, "items": items})


@app.route("/api/new_skus")
def api_new_skus():
    """SKUs present in open orders but NOT in forecast sku_manifest."""
    try:
        by_sku = oo_store.aggregate_all_skus()
    except Exception as e:
        return jsonify({"ready": False, "items": [], "error": str(e)})
    if not by_sku:
        return jsonify({"ready": True, "items": []})
    if not _db_exists():
        items = [{"sku": s, "pending_total": v.get("pending_total", 0)}
                 for s, v in by_sku.items()]
        items.sort(key=lambda x: x["pending_total"], reverse=True)
        return jsonify({"ready": True, "items": items})
    conn = _db()
    manifest_skus = set(pd.read_sql("SELECT sku FROM sku_manifest", conn)["sku"])
    conn.close()
    new_skus = []
    for sku, agg in by_sku.items():
        if sku not in manifest_skus:
            new_skus.append({
                "sku": sku,
                "pending_total": agg.get("pending_total", 0),
                "ordered_total": agg.get("ordered_total", 0),
                "n_orders": agg.get("n_orders", 0),
                "weight_ton_total": agg.get("weight_ton_total", 0),
            })
    new_skus.sort(key=lambda x: x["pending_total"], reverse=True)
    return jsonify({"ready": True, "items": new_skus})


# -- Open Orders endpoints (existing v7) --------------------------------------

@app.route("/api/open_orders")
def api_open_orders():
    try:
        by_sku = oo_store.aggregate_all_skus()
    except Exception as e:
        return jsonify({"ready": False, "by_sku": {}, "last_success": None,
                        "last_fetch": None, "error": str(e)})
    return jsonify({
        "ready": True, "by_sku": by_sku,
        "last_success": oo_store.get_last_success(),
        "last_fetch": oo_store.get_last_fetch(),
    })


@app.route("/api/sku/<sku_id>/open_orders")
def api_sku_open_orders(sku_id: str):
    try:
        aggregate = oo_store.aggregate_for_sku(sku_id)
        orders = oo_store.get_items_for_sku(sku_id)
    except Exception as e:
        return jsonify({"ready": False, "sku": sku_id, "aggregate": None,
                        "orders": [], "error": str(e)})
    return jsonify({
        "ready": True, "sku": sku_id,
        "aggregate": aggregate, "orders": orders,
        "last_success": oo_store.get_last_success(),
    })


@app.route("/api/open_orders/refresh", methods=["POST"])
def api_open_orders_refresh():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "live").lower()
    try:
        if source == "fixture":
            result = oo_store.load_fixture(payload.get("path") or OPEN_ORDERS_FIXTURE)
        else:
            result = oo_store.fetch_and_store()
        return jsonify({"ok": True, "source": source,
                        "last_success": oo_store.get_last_success(),
                        "last_fetch": oo_store.get_last_fetch(), **result})
    except Exception as e:
        log_error("open_orders", "/api/open_orders/refresh", 500, str(e))
        return jsonify({"ok": False, "source": source, "error": str(e),
                        "last_success": oo_store.get_last_success(),
                        "last_fetch": oo_store.get_last_fetch()}), 200


# -- NETPET Inventory endpoints (existing v7) ---------------------------------

@app.route("/api/inventory_status")
def api_inventory_status():
    try:
        return jsonify({
            "ready": True, "has_inventory": inv_store.has_inventory(),
            "last_success": inv_store.get_last_success(),
            "last_fetch": inv_store.get_last_fetch(),
        })
    except Exception as e:
        return jsonify({"ready": False, "has_inventory": False, "error": str(e)})


@app.route("/api/inventory/refresh", methods=["POST"])
def api_inventory_refresh():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "live").lower()
    try:
        if source == "fixture":
            result = inv_store.load_fixture(payload.get("path"))
        else:
            result = inv_store.fetch_and_store()
        return jsonify({"ok": True, "source": source,
                        "last_success": inv_store.get_last_success(),
                        "last_fetch": inv_store.get_last_fetch(), **result})
    except Exception as e:
        log_error("inventory", "/api/inventory/refresh", 500, str(e))
        return jsonify({"ok": False, "source": source, "error": str(e),
                        "last_success": inv_store.get_last_success(),
                        "last_fetch": inv_store.get_last_fetch()}), 200


HORIZON_SUMMARY_DAYS = [7, 14, 28, 56, 91]

@app.route("/api/horizon_summary")
def api_horizon_summary():
    if not _db_exists():
        return jsonify({"ready": False, "horizons": []})
    conn = _db()
    fc = pd.read_sql("SELECT sku, horizon_days, forecast_total, wape, grade FROM sku_forecasts", conn)
    conn.close()
    out = []
    for hd in HORIZON_SUMMARY_DAYS:
        sub = fc[fc["horizon_days"] == hd]
        modelled = sub[sub["wape"].notna()]
        n = int(len(modelled))
        total_t = float(modelled["forecast_total"].fillna(0).sum()) if n else 0.0
        good = int(modelled["grade"].isin(["Excellent", "Good"]).sum()) if n else 0
        out.append({
            "horizon_days": hd, "horizon_label": HORIZON_LABELS.get(hd, f"{hd}d"),
            "total_forecast_tons": round(total_t, 1),
            "reliability_pct": round(good / n * 100, 1) if n else 0.0,
            "n_skus_modelled": n,
        })
    return jsonify({"ready": True, "horizons": out})


def _persist_tiers(conn, recomputed, w_vol, w_freq):
    conn.execute("""CREATE TABLE IF NOT EXISTS abc_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        weight_volume REAL, weight_frequency REAL, updated_at TEXT)""")
    conn.execute(
        """INSERT INTO abc_config (id, weight_volume, weight_frequency, updated_at)
           VALUES (1, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             weight_volume = excluded.weight_volume,
             weight_frequency = excluded.weight_frequency,
             updated_at = excluded.updated_at""",
        (w_vol, w_freq, datetime.now().isoformat()),
    )
    for _, r in recomputed.iterrows():
        conn.execute("UPDATE sku_manifest SET velocity_tier = ? WHERE sku = ?",
                     (r["velocity_tier"], r["sku"]))


@app.route("/api/abc_weights", methods=["POST"])
def api_abc_weights():
    if not _db_exists():
        return jsonify({"error": "No batch data."}), 503
    payload = request.get_json(force=True) or {}
    w_vol = float(payload.get("weight_volume", 0.7))
    w_freq = float(payload.get("weight_frequency", 0.3))
    persist = bool(payload.get("persist", False))
    if abs((w_vol + w_freq) - 1.0) > 1e-6:
        return jsonify({"error": "weight_volume + weight_frequency must sum to 1.0"}), 400
    conn = _db()
    try:
        manifest = pd.read_sql("SELECT sku, total_volume, n_active, velocity_tier FROM sku_manifest", conn)
        recomputed = _compute_tiers(manifest.to_dict("records"), w_vol, w_freq)
        old_by_sku = manifest.set_index("sku")["velocity_tier"]
        moved = int((recomputed["velocity_tier"].values != old_by_sku.loc[recomputed["sku"]].values).sum())
        counts = recomputed["velocity_tier"].value_counts().to_dict()
        if persist:
            _persist_tiers(conn, recomputed, w_vol, w_freq)
            conn.commit()
    finally:
        conn.close()
    return jsonify({
        "success": True, "persisted": persist,
        "weight_volume": w_vol, "weight_frequency": w_freq,
        "tier_counts": {"A": counts.get("A", 0), "B": counts.get("B", 0), "C": counts.get("C", 0)},
        "n_moved": moved,
        "note": ("Applied and persisted - tiers updated in the database and weights "
                 "saved for the next batch run."
                 if persist else "Preview only - not persisted."),
    })


# =============================================================================
# ERROR LOGGING (frontend + backend fetch failures -> SQLite + file log)
# =============================================================================

_log_init_done = False

def _init_log_db():
    """Create the error log table if it doesn't exist. Called lazily on
    first write so the server starts even if the db dir is read-only."""
    global _log_init_done
    if _log_init_done:
        return
    try:
        os.makedirs(os.path.dirname(LOG_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            endpoint TEXT,
            status INTEGER,
            detail TEXT,
            origin TEXT DEFAULT 'backend'
        )""")
        conn.commit()
        conn.close()
        _log_init_done = True
    except Exception as e:
        print(f"WARN: could not init error log DB: {e}")


def log_error(source, endpoint, status, detail, origin="backend"):
    """Write one row to the fetch_log table. Best-effort: a failure here
    never bubbles up to crash a request."""
    _init_log_db()
    try:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.execute(
            "INSERT INTO fetch_log (ts, source, endpoint, status, detail, origin) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat() + "Z", source, endpoint, status, str(detail)[:500], origin)
        )
        # Trim to last 500 rows to prevent unbounded growth
        conn.execute("DELETE FROM fetch_log WHERE id NOT IN (SELECT id FROM fetch_log ORDER BY id DESC LIMIT 500)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"WARN: error log write failed: {e}")


@app.route("/api/log_error", methods=["POST"])
def api_log_error():
    """Receives frontend API error reports and persists them alongside
    backend errors in the same fetch_log table."""
    payload = request.get_json(silent=True) or {}
    ts = payload.get("ts", datetime.utcnow().isoformat() + "Z")
    path = payload.get("path", "unknown")
    status = payload.get("status", 0)
    detail = payload.get("detail", "")
    log_error("frontend", path, status, detail, origin="frontend")
    return jsonify({"ok": True})


@app.route("/api/error_log")
def api_error_log():
    """Returns the last N error log entries for diagnostics. Query params:
    ?limit=50 (default 50, max 200)."""
    _init_log_db()
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        conn = sqlite3.connect(LOG_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return jsonify({
            "ready": True,
            "entries": [dict(r) for r in rows]
        })
    except Exception as e:
        return jsonify({"ready": False, "error": str(e)})


# =============================================================================
# NEW v8 PLANNER ENDPOINTS
# =============================================================================

@app.route("/api/planner/open_orders")
def api_planner_open_orders():
    """Order-centric open orders for the planner accordion.
    Returns all orders with nested items, sorted by delivery_date,
    plus summary KPIs (total orders, total tonnes)."""
    try:
        orders = oo_store.get_all_orders()
        total_orders = len(orders)
        total_tons = 0.0
        total_items = 0
        for o in orders:
            for item in o.get("items", []):
                total_tons += item.get("weight_ton", 0) or 0
                total_items += 1
        return jsonify({
            "ready": True,
            "total_orders": total_orders,
            "total_items": total_items,
            "total_tons": round(total_tons, 2),
            "orders": orders,
            "last_success": oo_store.get_last_success(),
            "last_fetch": oo_store.get_last_fetch(),
        })
    except Exception as e:
        return jsonify({
            "ready": False, "total_orders": 0, "total_items": 0,
            "total_tons": 0, "orders": [], "error": str(e),
            "last_success": oo_store.get_last_success() if hasattr(oo_store, 'get_last_success') else None,
            "last_fetch": oo_store.get_last_fetch() if hasattr(oo_store, 'get_last_fetch') else None,
        })
        log_error("open_orders", "/api/planner/open_orders", 500, str(e))


@app.route("/api/planner/inventory_fg")
def api_planner_inventory_fg():
    """FG inventory aggregated by SKU for the planner inventory table.
    article_type_id IN (3, 4) = Mercadoria + Produto Acabado.
    Returns SKUs sorted desc by total tonnes with batch count and shelf life."""
    try:
        if not inv_store.has_inventory():
            return jsonify({
                "ready": False, "total_skus": 0, "total_tons": 0,
                "total_batches": 0, "items": [],
                "message": "No inventory snapshot available.",
                "last_success": inv_store.get_last_success(),
                "last_fetch": inv_store.get_last_fetch(),
            })

        # Use fg_detail_all_skus which already filters for FG types (3,4)
        fg_map = inv_store.fg_detail_all_skus()

        # Also get batch-level detail for shelf life
        inv_db_path = inv_store.INVENTORY_DB_PATH
        conn = sqlite3.connect(inv_db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT sku, description, family, subfamily,
                   SUM(weight_ton) as total_tons,
                   SUM(stock_quantity) as total_qty,
                   COUNT(DISTINCT batch) as batch_count,
                   MIN(remaining_shelf_life_days) as min_shelf_days,
                   MAX(remaining_shelf_life_days) as max_shelf_days,
                   AVG(remaining_shelf_life_days) as avg_shelf_days
            FROM inventory_batches
            WHERE article_type_id IN (3, 4) AND weight_ton > 0
            GROUP BY sku
            ORDER BY SUM(weight_ton) DESC
        """).fetchall()
        conn.close()

        items = []
        total_tons = 0.0
        total_batches = 0
        for r in rows:
            tons = round(float(r["total_tons"]), 3)
            batches = int(r["batch_count"])
            total_tons += tons
            total_batches += batches
            items.append({
                "sku": r["sku"],
                "description": r["description"],
                "family": r["family"],
                "subfamily": r["subfamily"],
                "total_tons": tons,
                "total_qty": int(r["total_qty"]) if r["total_qty"] else 0,
                "batch_count": batches,
                "min_shelf_days": int(r["min_shelf_days"]) if r["min_shelf_days"] is not None else None,
                "max_shelf_days": int(r["max_shelf_days"]) if r["max_shelf_days"] is not None else None,
                "avg_shelf_days": round(float(r["avg_shelf_days"]), 0) if r["avg_shelf_days"] is not None else None,
            })

        return jsonify({
            "ready": True,
            "total_skus": len(items),
            "total_tons": round(total_tons, 2),
            "total_batches": total_batches,
            "items": items,
            "last_success": inv_store.get_last_success(),
            "last_fetch": inv_store.get_last_fetch(),
        })
    except Exception as e:
        return jsonify({
            "ready": False, "total_skus": 0, "total_tons": 0,
            "total_batches": 0, "items": [], "error": str(e),
            "last_success": None, "last_fetch": None,
        })
        log_error("inventory", "/api/planner/inventory_fg", 500, str(e))


@app.route("/api/planner/order/<order_id>")
def api_planner_order_detail(order_id: str):
    """Full detail for one order: header info + all SKU line items with
    their inventory cross-reference (available stock from NETPET)."""
    try:
        orders = oo_store.get_all_orders()
        order = None
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                order = o
                break
        if not order:
            return jsonify({"ready": False, "error": f"Order {order_id} not found"}), 404

        # Enrich each item with inventory data
        enriched_items = []
        for item in order.get("items", []):
            sku = str(item.get("sku", ""))
            avail, source = _available_for_sku(sku)
            enriched_items.append({
                **item,
                "available_stock_tons": round(avail, 2) if avail is not None else None,
                "inventory_source": source,
            })

        return jsonify({
            "ready": True,
            "order": {
                "order_id": order.get("order_id"),
                "series": order.get("series"),
                "document_number": order.get("document_number"),
                "customer_name": order.get("customer_name"),
                "country": order.get("country"),
                "delivery_date": order.get("delivery_date"),
                "status": order.get("status"),
                "is_export": order.get("is_export"),
                "notes": order.get("notes"),
                "items": enriched_items,
            },
        })
    except Exception as e:
        log_error("open_orders", f"/api/planner/order/{order_id}", 500, str(e))
        return jsonify({"ready": False, "error": str(e)}), 500


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from werkzeug.serving import run_simple

    def _startup_open_orders_fetch():
        oo_store.init_db()
        try:
            res = oo_store.fetch_and_store()
            print(f"[open_orders] startup live fetch OK: {res}")
            return
        except Exception as e:
            print(f"[open_orders] startup live fetch FAILED (serving previous snapshot): {e}")
            log_error("open_orders", "startup-fetch", 500, str(e))
            try:
                oo_store.log_failure("live-startup", str(e))
            except Exception:
                pass
        try:
            if not oo_store.has_orders() and os.path.exists(OPEN_ORDERS_FIXTURE):
                res = oo_store.load_fixture(OPEN_ORDERS_FIXTURE)
                print(f"[open_orders] seeded demo fixture (no live snapshot available): {res}")
            elif not oo_store.has_orders():
                print(f"[open_orders] no live data and no fixture at {OPEN_ORDERS_FIXTURE} - store is empty")
        except Exception as e:
            print(f"[open_orders] fixture seed failed: {e}")

    def _startup_inventory_fetch():
        inv_store.init_db()
        try:
            res = inv_store.fetch_and_store()
            print(f"[inventory] startup live fetch OK: {res}")
        except Exception as e:
            print(f"[inventory] startup live fetch FAILED (serving previous snapshot): {e}")
            log_error("inventory", "startup-fetch", 500, str(e))
            try:
                inv_store.log_failure("live-startup", str(e))
            except Exception:
                pass

    USE_RELOADER = True
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not USE_RELOADER:
        _init_log_db()
        _startup_open_orders_fetch()
        _startup_inventory_fetch()

    print(f"DB path:   {DB_PATH}  (exists: {os.path.exists(DB_PATH)})")
    print(f"Data path: {DATA_PATH}  (exists: {os.path.exists(DATA_PATH)})")
    print(f"Serving at http://localhost:5000{BASE_PATH}/")

    application = DispatcherMiddleware(Flask("dummy_root"), {BASE_PATH or "/petmaxi-dashboard": app})
    run_simple("0.0.0.0", 5000, application, use_reloader=USE_RELOADER, use_debugger=True)
